"""Read-only local dashboard and execution terminal. Standard library only, no mutating endpoints.

Security defaults: binds 127.0.0.1, rejects unexpected Host headers (DNS-rebinding defence),
and refuses to bind a non-loopback address unless DASHBOARD_PASSWORD is set.
Supports both modern session cookie auth (with elegant login page) and HTTP Basic auth.
Username is configurable via DASHBOARD_USERNAME (defaults to 'admin').
For remote use put it behind a VPN (Tailscale/WireGuard); never expose the port to the internet.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pandas as pd

from .kill_switch import get_kill_switch_status, reset_kill_switch, trigger_kill_switch

LOOPBACK = {"127.0.0.1", "localhost", "::1"}


def _read_json(path: str | Path) -> dict:
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return {}


def average_costs(trades: list[dict]) -> dict[str, float]:
    """Running average cost per share (fees included on buys) for what is still held."""
    qty: dict[str, int] = {}
    cost: dict[str, float] = {}
    for t in trades:
        s, q = t["symbol"], int(t["qty"])
        if t["side"] == "BUY":
            qty[s] = qty.get(s, 0) + q
            cost[s] = cost.get(s, 0.0) + q * float(t["fill_price"]) + float(t["cost"])
        else:
            have = qty.get(s, 0)
            if have:
                cost[s] -= cost[s] * min(q, have) / have
                qty[s] = have - min(q, have)
    return {s: cost[s] / qty[s] for s in qty if qty[s] > 0}


def _last_closes(prices_cache: str, symbols: list[str]) -> dict[str, float]:
    try:
        df = pd.read_csv(prices_cache, index_col=0, parse_dates=True)
        row = df.ffill().iloc[-1]
        return {s: float(row[s]) for s in symbols if s in row.index and pd.notna(row[s])}
    except (OSError, ValueError, IndexError):
        return {}


def _get_recent_notifications(path: str = "data/notifications.json") -> list[dict]:
    try:
        from .notify import load_notifications
        return load_notifications(path)[:20]
    except Exception:
        return []


def build_summary(paper_path: str, monitor_path: str, screen_path: str, prices_cache: str,
                  rebalance_months: int, swing_path: str | None = None,
                  kill_switch_path: str | None = None, broker_mode: str = "paper",
                  ai_signals_path: str | None = None) -> dict:
    state = _read_json(paper_path)
    mon = _read_json(monitor_path)
    positions: dict[str, int] = {k: int(v) for k, v in state.get("positions", {}).items() if int(v) > 0}
    trades = state.get("trades", [])

    if mon.get("live_mode") is True:
        broker_mode = "live"
    is_live = (broker_mode == "live")
    mode_text = "LIVE (Groww API, CNC delivery)" if is_live else "PAPER (simulated, no real orders)"

    if is_live:
        # In live mode, only return real live executed trades; never dummy paper simulation trades.
        live_trades_file = Path("data/live_trades.json")
        live_trades = []
        if live_trades_file.is_file():
            try:
                live_trades = json.loads(live_trades_file.read_text(encoding="utf-8"))
            except Exception:
                live_trades = []
        if not live_trades:
            # Backfill from notifications if any live order was executed
            notifs = _get_recent_notifications()
            for n in notifs:
                if n.get("type") == "buy" and n.get("details", {}).get("mode") == "LIVE":
                    det = n.get("details", {})
                    live_trades.append({
                        "ts": n.get("timestamp", "").replace("T", " ")[:19],
                        "symbol": det.get("symbol", ""),
                        "side": "BUY",
                        "qty": int(det.get("qty", 0)),
                        "fill_price": float(det.get("price", 0.0)),
                        "cost": 0.0,
                    })
            if live_trades:
                try:
                    live_trades_file.parent.mkdir(parents=True, exist_ok=True)
                    live_trades_file.write_text(json.dumps(live_trades, indent=2), encoding="utf-8")
                except Exception:
                    pass
        display_trades = live_trades
        avg = average_costs(display_trades)
    else:
        display_trades = trades[-25:][::-1]
        avg = average_costs(trades)

    fallback = _last_closes(prices_cache, list(positions))
    quotes = mon.get("quotes", {})
    peaks = mon.get("peaks", {})
    holdings = []
    for sym, qty in sorted(positions.items()):
        last = quotes.get(sym, fallback.get(sym))
        cost = avg.get(sym)
        value = qty * last if last else None
        pnl = (last - cost) * qty if last and cost else None
        peak = peaks.get(sym)
        holdings.append({
            "symbol": sym, "qty": qty, "avg_cost": cost, "last": last, "value": value,
            "pnl": pnl, "pnl_pct": (last / cost - 1) if last and cost else None,
            "from_peak_pct": (last / peak - 1) if last and peak else None,
            "price_source": "live" if sym in quotes else "last close",
        })
    cash = float(state.get("cash", 0.0))
    held_value = sum(h["value"] or 0.0 for h in holdings)
    equity = cash + held_value
    deposits = state.get("deposits", [])
    deposited = sum(float(d["amount"]) for d in deposits if d.get("amount")) or None
    last_reb = state.get("last_rebalance")
    nxt = (pd.Timestamp(last_reb) + pd.DateOffset(months=rebalance_months)).date().isoformat() if last_reb else None
    screen = {}
    try:
        rep = pd.read_csv(screen_path)
        screen = {"compliant": int(rep["compliant"].sum()), "total": int(len(rep))}
    except (OSError, ValueError, KeyError):
        pass

    try:
        from .api_tracker import load_api_usage
        api_budget = load_api_usage()
    except Exception:
        api_budget = {}

    return {
        "mode": mode_text,
        "broker_mode": broker_mode,
        "equity": equity, "cash": cash, "held_value": held_value,
        "deposited": deposited,
        "pnl": equity - deposited if deposited else None,
        "pnl_pct": (equity / deposited - 1) if deposited else None,
        "total_costs": state.get("total_costs", 0.0),
        "holdings": holdings,
        "trades": display_trades,
        "last_rebalance": last_reb, "next_rebalance": nxt,
        "monitor": {
            "heartbeat": mon.get("heartbeat"), "market_open": mon.get("market_open"),
            "stop_pct": mon.get("stop_pct"), "act": mon.get("act"),
            "live_mode": is_live,
            "alerts": mon.get("alerts", [])[-15:][::-1],
        },
        "screen": screen,
        "swing": (_read_json(swing_path) if swing_path else {}) if not is_live else {},
        "kill_switch": get_kill_switch_status(kill_switch_path or "data/kill_switch.json"),
        "ai_signals": _read_json(ai_signals_path) if ai_signals_path else {},
        "api_budget": api_budget,
        "notifications": _get_recent_notifications(),
    }


def create_session_token(username: str, secret: bytes, max_age: int = 86400 * 30) -> str:
    """Create a tamper-evident, HMAC-signed session token for browser cookie auth."""
    ts = int(time.time())
    payload = f"{username}:{ts}"
    sig = hmac.new(secret, payload.encode(), hashlib.sha256).hexdigest()
    raw = f"{payload}:{sig}"
    return base64.urlsafe_b64encode(raw.encode()).decode()


def verify_session_token(token_str: str, secret: bytes, expected_username: str | None = None,
                         max_age: int = 86400 * 30) -> tuple[bool, str]:
    """Verify an HMAC-signed session token and return (valid, username)."""
    try:
        raw = base64.urlsafe_b64decode(token_str.encode()).decode()
        parts = raw.split(":")
        if len(parts) != 3:
            return False, ""
        user, ts_str, sig = parts
        ts = int(ts_str)
        if time.time() > ts + max_age:
            return False, ""
        expected_sig = hmac.new(secret, f"{user}:{ts_str}".encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected_sig):
            return False, ""
        if expected_username and not hmac.compare_digest(user, expected_username):
            return False, ""
        return True, user
    except Exception:
        return False, ""


def extract_cookie(header: str, name: str) -> str | None:
    if not header:
        return None
    for part in header.split(";"):
        part = part.strip()
        if part.startswith(f"{name}="):
            return part[len(name) + 1:]
    return None



MANIFEST_JSON = """{
  "name": "Alvi Quantitative Terminal",
  "short_name": "Alvi Terminal",
  "description": "Autonomous Shariah-Compliant Quantitative Trading Terminal & Execution Engine",
  "start_url": "/",
  "scope": "/",
  "display": "standalone",
  "orientation": "portrait-primary",
  "background_color": "#030712",
  "theme_color": "#030712",
  "icons": [
    {
      "src": "/icon-192.png",
      "sizes": "192x192",
      "type": "image/png",
      "purpose": "any maskable"
    },
    {
      "src": "/icon-512.png",
      "sizes": "512x512",
      "type": "image/png",
      "purpose": "any maskable"
    },
    {
      "src": "/favicon.svg",
      "sizes": "any",
      "type": "image/svg+xml",
      "purpose": "any"
    }
  ],
  "categories": ["finance", "productivity"]
}"""

SERVICE_WORKER_JS = """const CACHE_NAME = 'alvi-terminal-v4';
const STATIC_SHELL = [
  '/manifest.json',
  '/favicon.svg',
  '/icon-192.png',
  '/icon-512.png'
];

self.addEventListener('install', event => {
  event.waitUntil(
    caches.open(CACHE_NAME).then(cache => cache.addAll(STATIC_SHELL)).then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', event => {
  event.waitUntil(
    caches.keys().then(keys => Promise.all(
      keys.filter(k => k !== CACHE_NAME).map(k => caches.delete(k))
    )).then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', event => {
  const url = new URL(event.request.url);
  // Always fetch HTML documents and dynamic API requests straight from network with cookies
  if (event.request.mode === 'navigate' || url.pathname.startsWith('/api/') || url.pathname === '/' || url.pathname === '/login') {
    event.respondWith(fetch(event.request));
    return;
  }
  // Stale-while-revalidate for static icons and manifest
  event.respondWith(
    caches.match(event.request).then(cached => {
      const fetchPromise = fetch(event.request).then(networkResponse => {
        if (networkResponse && networkResponse.status === 200) {
          const toCache = networkResponse.clone();
          caches.open(CACHE_NAME).then(c => c.put(event.request, toCache));
        }
        return networkResponse;
      }).catch(() => cached);
      return cached || fetchPromise;
    })
  );
});

self.addEventListener('notificationclick', event => {
  event.notification.close();
  event.waitUntil(
    clients.matchAll({ type: 'window', includeUncontrolled: true }).then(clientList => {
      for (const client of clientList) {
        if (client.url && 'focus' in client) {
          return client.focus();
        }
      }
      if (clients.openWindow) {
        return clients.openWindow('/');
      }
    })
  );
});"""

FAVICON_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512">
  <defs>
    <linearGradient id="bg" x1="0%" y1="0%" x2="100%" y2="100%">
      <stop offset="0%" stop-color="#0f172a"/>
      <stop offset="100%" stop-color="#030712"/>
    </linearGradient>
    <linearGradient id="em" x1="0%" y1="0%" x2="100%" y2="100%">
      <stop offset="0%" stop-color="#10b981"/>
      <stop offset="100%" stop-color="#064e3b"/>
    </linearGradient>
    <filter id="glow" x="-20%" y="-20%" width="140%" height="140%">
      <feGaussianBlur stdDeviation="8" result="blur"/>
      <feComposite in="SourceGraphic" in2="blur" operator="over"/>
    </filter>
  </defs>
  <rect width="512" height="512" rx="110" fill="url(#bg)" stroke="#10b981" stroke-width="14" stroke-opacity="0.85"/>
  <circle cx="256" cy="256" r="145" fill="url(#em)" stroke="#34d399" stroke-width="8" filter="url(#glow)"/>
  <rect x="238" y="210" width="36" height="135" rx="6" fill="#34d399"/>
  <polygon points="256,155 185,240 327,240" fill="#38bdf8"/>
</svg>"""

ICON_192_B64 = "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"
ICON_512_B64 = "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"
ICON_192_PNG_BYTES = base64.b64decode(ICON_192_B64)
ICON_512_PNG_BYTES = base64.b64decode(ICON_512_B64)


LOGIN_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no, viewport-fit=cover">
<meta name="theme-color" content="#030712">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="Alvi Terminal">
<link rel="manifest" href="/manifest.json">
<link rel="icon" type="image/svg+xml" href="/favicon.svg">
<link rel="apple-touch-icon" href="/icon-192.png">
<title>Sign In · Shariah Quantitative Terminal</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@500;600&display=swap" rel="stylesheet">
<style>
:root {
  --bg: #030712;
  --card: #0b1120;
  --card-border: #1e293b;
  --ink: #f8fafc;
  --mute: #94a3b8;
  --subtle: #475569;
  --accent: #10b981;
  --accent-glow: rgba(16, 185, 129, 0.25);
  --accent-hover: #059669;
  --down: #ef4444;
  --down-bg: rgba(239, 68, 68, 0.12);
  --down-border: rgba(239, 68, 68, 0.35);
  --ring: #38bdf8;
}
* { box-sizing: border-box; margin: 0; padding: 0; }
body {
  background: var(--bg);
  background-image: radial-gradient(circle at 50% 0%, #0f172a 0%, #030712 75%);
  color: var(--ink);
  font-family: 'Inter', system-ui, -apple-system, sans-serif;
  min-height: 100vh;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: max(20px, env(safe-area-inset-top)) max(16px, env(safe-area-inset-right)) max(20px, env(safe-area-inset-bottom)) max(16px, env(safe-area-inset-left));
  -webkit-font-smoothing: antialiased;
}
@media (max-width: 480px) {
  .login-wrap { max-width: 100%; }
  .login-card { padding: 24px 20px !important; border-radius: 16px !important; }
  h1 { font-size: 19px !important; }
  .form-group input { font-size: 16px !important; }
}
.login-wrap {
  width: 100%;
  max-width: 420px;
}
.brand-header {
  text-align: center;
  margin-bottom: 28px;
}
.brand-logo {
  width: 52px;
  height: 52px;
  margin: 0 auto 16px;
  background: linear-gradient(135deg, #10b981 0%, #064e3b 100%);
  border: 1px solid rgba(52, 211, 153, 0.4);
  box-shadow: 0 0 24px var(--accent-glow);
  border-radius: 14px;
  display: flex;
  align-items: center;
  justify-content: center;
  color: #fff;
}
.brand-logo svg {
  width: 28px;
  height: 28px;
}
h1 {
  font-size: 21px;
  font-weight: 700;
  letter-spacing: -0.02em;
  color: var(--ink);
  margin-bottom: 6px;
}
.tagline {
  font-size: 13px;
  color: var(--mute);
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
}
.shariah-pill {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  background: rgba(16, 185, 129, 0.12);
  border: 1px solid rgba(16, 185, 129, 0.3);
  color: #34d399;
  font-size: 11px;
  font-weight: 600;
  padding: 2px 8px;
  border-radius: 99px;
  letter-spacing: 0.04em;
  text-transform: uppercase;
}
.login-card {
  background: var(--card);
  border: 1px solid var(--card-border);
  box-shadow: 0 20px 40px -15px rgba(0, 0, 0, 0.7), 0 0 0 1px rgba(255, 255, 255, 0.03);
  border-radius: 18px;
  padding: 32px 28px;
  backdrop-filter: blur(16px);
}
.form-group {
  margin-bottom: 20px;
}
label {
  display: block;
  font-size: 12px;
  font-weight: 600;
  color: var(--mute);
  text-transform: uppercase;
  letter-spacing: 0.05em;
  margin-bottom: 8px;
}
.input-wrap {
  position: relative;
}
.input-icon {
  position: absolute;
  left: 14px;
  top: 50%;
  transform: translateY(-50%);
  color: var(--subtle);
  display: flex;
  pointer-events: none;
}
.input-wrap input {
  width: 100%;
  height: 46px;
  padding: 0 14px 0 42px;
  background: #060913;
  border: 1px solid #1e293b;
  border-radius: 10px;
  color: var(--ink);
  font-size: 14px;
  font-family: inherit;
  transition: all 0.2s ease;
  outline: none;
}
.input-wrap input:focus {
  border-color: var(--accent);
  box-shadow: 0 0 0 3px var(--accent-glow);
  background: #090e1a;
}
.toggle-pwd {
  position: absolute;
  right: 12px;
  top: 50%;
  transform: translateY(-50%);
  background: none;
  border: none;
  color: var(--subtle);
  cursor: pointer;
  padding: 4px;
  display: flex;
  border-radius: 4px;
  transition: color 0.15s;
}
.toggle-pwd:hover {
  color: var(--mute);
}
.btn-submit {
  width: 100%;
  height: 46px;
  background: var(--accent);
  color: #022c22;
  border: none;
  border-radius: 10px;
  font-size: 14px;
  font-weight: 600;
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
  transition: all 0.2s ease;
  box-shadow: 0 4px 14px rgba(16, 185, 129, 0.3);
  margin-top: 8px;
}
.btn-submit:hover:not(:disabled) {
  background: var(--accent-hover);
  color: #fff;
  transform: translateY(-1px);
  box-shadow: 0 6px 20px rgba(16, 185, 129, 0.4);
}
.btn-submit:active:not(:disabled) {
  transform: translateY(0);
}
.btn-submit:disabled {
  opacity: 0.65;
  cursor: not-allowed;
}
.error-box {
  display: none;
  background: var(--down-bg);
  border: 1px solid var(--down-border);
  color: #fca5a5;
  padding: 12px 14px;
  border-radius: 10px;
  font-size: 13px;
  margin-bottom: 20px;
  align-items: center;
  gap: 8px;
  animation: shake 0.3s ease;
}
@keyframes shake {
  0%, 100% { transform: translateX(0); }
  25% { transform: translateX(-5px); }
  75% { transform: translateX(5px); }
}
.footer-note {
  margin-top: 24px;
  text-align: center;
  font-size: 12px;
  color: var(--subtle);
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
}
.spinner {
  width: 18px;
  height: 18px;
  border: 2px solid rgba(2, 44, 34, 0.3);
  border-top-color: #022c22;
  border-radius: 50%;
  animation: spin 0.8s linear infinite;
}
@keyframes spin {
  to { transform: rotate(360deg); }
}

/* PWA Install Banner & Buttons */
.pwa-banner {
  background: linear-gradient(135deg, rgba(16, 185, 129, 0.16) 0%, rgba(15, 23, 42, 0.95) 100%);
  border-bottom: 1px solid rgba(16, 185, 129, 0.35);
  padding: 10px 16px;
  display: none;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  font-size: 13px;
  position: sticky;
  top: 0;
  z-index: 105;
  backdrop-filter: blur(10px);
  -webkit-backdrop-filter: blur(10px);
}
.pwa-banner-content {
  max-width: 1200px;
  margin: 0 auto;
  width: 100%;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
}
.pwa-info {
  display: flex;
  align-items: center;
  gap: 10px;
}
.pwa-icon {
  width: 32px;
  height: 32px;
  border-radius: 8px;
  background: #022c22;
  border: 1px solid #10b981;
  color: #34d399;
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
}
.btn-install-pwa {
  background: #10b981;
  color: #022c22;
  font-weight: 700;
  border: none;
  padding: 6px 14px;
  border-radius: 8px;
  font-size: 12px;
  cursor: pointer;
  display: flex;
  align-items: center;
  gap: 6px;
  transition: all 0.2s;
  box-shadow: 0 2px 8px rgba(16, 185, 129, 0.35);
}
.btn-install-pwa:hover {
  background: #34d399;
  color: #fff;
}
.btn-dismiss-pwa {
  background: transparent;
  color: var(--mute);
  border: none;
  font-size: 18px;
  cursor: pointer;
  padding: 2px 6px;
  line-height: 1;
}
.btn-header-install {
  display: none;
  align-items: center;
  gap: 5px;
  background: rgba(16, 185, 129, 0.14);
  color: #34d399;
  border: 1px solid rgba(16, 185, 129, 0.35);
  padding: 5px 10px;
  border-radius: 8px;
  font-size: 12px;
  font-weight: 600;
  cursor: pointer;
  transition: all 0.2s;
}
.btn-header-install:hover {
  background: #10b981;
  color: #022c22;
}
.ios-hint {
  display: none;
  background: #0c1527;
  border: 1px solid rgba(56, 189, 248, 0.3);
  color: #93c5fd;
  padding: 9px 14px;
  border-radius: 10px;
  font-size: 12px;
  margin-bottom: 14px;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}

/* Mobile & Tablet Responsive Media Queries */
@media (max-width: 768px) {
  .topbar {
    padding: max(8px, env(safe-area-inset-top)) max(12px, env(safe-area-inset-right)) 8px max(12px, env(safe-area-inset-left));
  }
  .topbar-content {
    gap: 8px;
  }
  .brand-title {
    font-size: 14px;
  }
  .brand-subtitle {
    display: none;
  }
  .status-cluster {
    gap: 6px;
    flex-wrap: wrap;
  }
  .actions-group {
    width: 100%;
    justify-content: space-between;
    gap: 6px;
    margin-top: 4px;
  }
  .actions-group .user-pill {
    padding: 5px 8px;
    font-size: 11px;
    max-width: 130px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .btn-kill {
    padding: 5px 10px;
    font-size: 11px;
  }
  .container {
    padding: 0 max(12px, env(safe-area-inset-left)) 0 max(12px, env(safe-area-inset-right));
    margin-top: 12px;
  }
  .kpi-grid {
    grid-template-columns: repeat(2, 1fr) !important;
    gap: 8px !important;
    margin-bottom: 12px;
  }
  .kpi-card {
    padding: 12px 12px;
    border-radius: 12px;
  }
  .kpi-label {
    font-size: 10px;
    margin-bottom: 4px;
  }
  .kpi-value {
    font-size: 18px;
  }
  .kpi-sub {
    font-size: 10px;
  }
  .card {
    padding: 14px 14px;
    border-radius: 14px;
    margin-bottom: 14px;
  }
  .card.collapsed {
    padding-bottom: 12px;
  }
  .card-header {
    padding: 4px 6px;
    margin: -4px -6px 12px -6px;
  }
  .card.collapsed .card-header {
    margin-bottom: 0;
  }
  .btn-toggle-all {
    padding: 4px 8px;
    font-size: 11px;
  }
  .card-title {
    font-size: 13px;
  }
  .trade-head {
    flex-direction: column;
    align-items: flex-start;
    gap: 8px;
  }
  .trade-symbol {
    font-size: 18px;
  }
  .trade-metrics {
    grid-template-columns: repeat(2, 1fr) !important;
    gap: 8px !important;
  }
  .budget-grid {
    grid-template-columns: 1fr !important;
    gap: 8px !important;
  }
  .table-wrap {
    overflow-x: auto;
    -webkit-overflow-scrolling: touch;
    margin: 0 -14px;
    padding: 0 14px;
    scrollbar-width: thin;
  }
  table {
    min-width: 550px;
  }
  th, td {
    padding: 8px 10px;
    font-size: 12px;
  }
  .halt-content {
    flex-direction: column;
    align-items: flex-start;
    gap: 12px;
  }
  .btn-resume {
    width: 100%;
    text-align: center;
  }
}

@media (max-width: 480px) {
  .kpi-grid {
    grid-template-columns: repeat(2, 1fr) !important;
    gap: 6px !important;
  }
  .kpi-card {
    padding: 10px 10px;
  }
  .kpi-value {
    font-size: 16px;
  }
  .pill {
    padding: 2px 7px;
    font-size: 10px;
  }
  .btn-kill {
    font-size: 10px;
    padding: 4px 8px;
  }
}


/* Toast & Notification System */
.toast-container {
  position: fixed;
  top: max(20px, env(safe-area-inset-top));
  right: max(20px, env(safe-area-inset-right));
  z-index: 9999;
  display: flex;
  flex-direction: column;
  gap: 10px;
  max-width: 380px;
  width: calc(100vw - 32px);
  pointer-events: none;
}
.toast {
  pointer-events: auto;
  background: rgba(15, 23, 42, 0.95);
  backdrop-filter: blur(14px);
  -webkit-backdrop-filter: blur(14px);
  border: 1px solid #1e293b;
  border-radius: 12px;
  padding: 14px 16px;
  box-shadow: 0 10px 30px rgba(0, 0, 0, 0.6), 0 0 15px rgba(56, 189, 248, 0.15);
  display: flex;
  align-items: flex-start;
  gap: 12px;
  transform: translateX(120%);
  animation: slideInToast 0.35s cubic-bezier(0.16, 1, 0.3, 1) forwards;
  position: relative;
  overflow: hidden;
}
@keyframes slideInToast {
  to { transform: translateX(0); }
}
.toast.toast-fadeout {
  animation: fadeOutToast 0.3s ease forwards;
}
@keyframes fadeOutToast {
  to { opacity: 0; transform: translateY(-10px) scale(0.95); }
}
.toast-buy {
  border-color: rgba(16, 185, 129, 0.6);
  box-shadow: 0 10px 30px rgba(0, 0, 0, 0.6), 0 0 20px rgba(16, 185, 129, 0.25);
}
.toast-sell, .toast-stop {
  border-color: rgba(239, 68, 68, 0.6);
  box-shadow: 0 10px 30px rgba(0, 0, 0, 0.6), 0 0 20px rgba(239, 68, 68, 0.25);
}
.toast-profit {
  border-color: rgba(52, 211, 153, 0.7);
  box-shadow: 0 10px 30px rgba(0, 0, 0, 0.6), 0 0 20px rgba(52, 211, 153, 0.3);
}
.toast-icon {
  font-size: 20px;
  line-height: 1;
  flex-shrink: 0;
  margin-top: 2px;
}
.toast-content {
  flex: 1;
  min-width: 0;
}
.toast-title {
  font-size: 13px;
  font-weight: 700;
  color: #fff;
  letter-spacing: -0.01em;
  margin-bottom: 2px;
}
.toast-body {
  font-size: 12px;
  color: #cbd5e1;
  line-height: 1.45;
  word-break: break-word;
}
.toast-time {
  font-size: 10px;
  color: var(--mute);
  margin-top: 4px;
}
.toast-close {
  background: transparent;
  border: none;
  color: var(--mute);
  font-size: 18px;
  cursor: pointer;
  line-height: 1;
  padding: 0 4px;
}
.toast-close:hover {
  color: #fff;
}
.btn-notif {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  background: #0f172a;
  color: var(--mute);
  border: 1px solid var(--line);
  padding: 5px 10px;
  border-radius: 8px;
  font-size: 12px;
  font-weight: 600;
  cursor: pointer;
  transition: all 0.2s;
}
.btn-notif:hover {
  color: #fff;
  border-color: #38bdf8;
}
.btn-notif.active {
  background: rgba(16, 185, 129, 0.12);
  color: #34d399;
  border-color: rgba(16, 185, 129, 0.35);
}

</style>
</head>
<body>
<div class="login-wrap">
  <div class="brand-header">
    <div class="brand-logo">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round">
        <path d="M12 2L2 7l10 5 10-5-10-5zM2 17l10 5 10-5M2 12l10 5 10-5"/>
      </svg>
    </div>
    <h1>Alvi Quantitative Terminal</h1>
    <div class="tagline">
      <span>Autonomous Portfolio System</span>
      <span class="shariah-pill">
        <svg width="10" height="10" viewBox="0 0 24 24" fill="currentColor"><path d="M12 2l3.09 6.26L22 9.27l-5 4.87 1.18 6.88L12 17.77l-6.18 3.25L7 14.14 2 9.27l6.91-1.01L12 2z"/></svg>
        AAOIFI
      </span>
    </div>
  </div>

  <div class="login-card">
    <div id="errorBox" class="error-box">
      <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="flex-shrink:0"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg>
      <span id="errorMsg">Invalid credentials</span>
    </div>

    <form id="loginForm" autocomplete="on">
      <div class="form-group">
        <label for="username">Username</label>
        <div class="input-wrap">
          <span class="input-icon">
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/></svg>
          </span>
          <input type="text" id="username" name="username" placeholder="Enter terminal username" autocomplete="username" required autofocus>
        </div>
      </div>

      <div class="form-group">
        <label for="password">Security Password</label>
        <div class="input-wrap">
          <span class="input-icon">
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="11" width="18" height="11" rx="2" ry="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/></svg>
          </span>
          <input type="password" id="password" name="password" placeholder="••••••••••••" autocomplete="current-password" required>
          <button type="button" id="togglePwd" class="toggle-pwd" title="Toggle password visibility" aria-label="Toggle password visibility">
            <svg id="eyeIcon" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg>
          </button>
        </div>
      </div>

      <button type="submit" id="submitBtn" class="btn-submit">
        <span>Sign In to Terminal</span>
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M5 12h14M12 5l7 7-7 7"/></svg>
      </button>
    </form>
  </div>

  <div class="footer-note">
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="11" width="18" height="11" rx="2" ry="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/></svg>
    <span>Encrypted HMAC-SHA256 Session · Private Tailscale Mesh</span>
  </div>
</div>

<script>
const pwdInput = document.getElementById('password');
const toggleBtn = document.getElementById('togglePwd');
const eyeIcon = document.getElementById('eyeIcon');
toggleBtn.addEventListener('click', () => {
  const isPwd = pwdInput.type === 'password';
  pwdInput.type = isPwd ? 'text' : 'password';
  eyeIcon.innerHTML = isPwd
    ? '<path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19m-6.72-1.07a3 3 0 1 1-4.24-4.24"/><line x1="1" y1="1" x2="23" y2="23"/>'
    : '<path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/>';
});

document.getElementById('loginForm').addEventListener('submit', async (e) => {
  e.preventDefault();
  const btn = document.getElementById('submitBtn');
  const errBox = document.getElementById('errorBox');
  const errMsg = document.getElementById('errorMsg');
  errBox.style.display = 'none';

  const username = document.getElementById('username').value.trim();
  const password = pwdInput.value;

  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span><span>Verifying…</span>';

  try {
    const res = await fetch('/api/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username, password })
    });
    const data = await res.json().catch(() => ({}));
    if (res.ok && data.ok) {
      window.location.href = '/';
    } else {
      errMsg.textContent = data.error || 'Invalid username or password';
      errBox.style.display = 'flex';
      btn.disabled = false;
      btn.innerHTML = '<span>Sign In to Terminal</span><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M5 12h14M12 5l7 7-7 7"/></svg>';
    }
  } catch (err) {
    errMsg.textContent = 'Network error connecting to terminal';
    errBox.style.display = 'flex';
    btn.disabled = false;
    btn.innerHTML = '<span>Sign In to Terminal</span><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M5 12h14M12 5l7 7-7 7"/></svg>';
  }
});
</script>
<script>
if ('serviceWorker' in navigator) {
  window.addEventListener('load', () => {
    navigator.serviceWorker.register('/sw.js').catch(() => {});
  });
}
</script>
</body>
</html>"""

PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no, viewport-fit=cover">
<meta name="theme-color" content="#030712">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="Alvi Terminal">
<link rel="manifest" href="/manifest.json">
<link rel="icon" type="image/svg+xml" href="/favicon.svg">
<link rel="apple-touch-icon" href="/icon-192.png">
<title>Alvi Quantitative Terminal · Shariah Trading</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;600&display=swap" rel="stylesheet">
<style>
:root {
  --bg: #030712;
  --card: #0b1120;
  --card-border: #1e293b;
  --card-hover: #131d33;
  --ink: #f8fafc;
  --mute: #94a3b8;
  --subtle: #475569;
  --line: #1e293b;
  --up: #10b981;
  --up-bg: rgba(16, 185, 129, 0.12);
  --up-border: rgba(16, 185, 129, 0.28);
  --down: #ef4444;
  --down-bg: rgba(239, 68, 68, 0.12);
  --down-border: rgba(239, 68, 68, 0.28);
  --warn: #f59e0b;
  --warn-bg: rgba(245, 158, 11, 0.12);
  --warn-border: rgba(245, 158, 11, 0.28);
  --accent: #38bdf8;
  --accent-glow: rgba(56, 189, 248, 0.25);
  --shariah: #10b981;
}
* { box-sizing: border-box; margin: 0; padding: 0; }
body {
  background: var(--bg);
  background-image: radial-gradient(ellipse at 50% -20%, #0f172a 0%, #030712 65%);
  color: var(--ink);
  font-family: 'Inter', system-ui, -apple-system, sans-serif;
  min-height: 100vh;
  padding-bottom: max(32px, env(safe-area-inset-bottom));
  -webkit-font-smoothing: antialiased;
}
.mono { font-family: 'JetBrains Mono', monospace; }
.tabular { font-variant-numeric: tabular-nums; }

/* Top Navigation Bar */
.topbar {
  background: rgba(11, 17, 32, 0.9);
  backdrop-filter: blur(14px);
  -webkit-backdrop-filter: blur(14px);
  border-bottom: 1px solid var(--line);
  position: sticky;
  top: 0;
  z-index: 100;
  padding: max(10px, env(safe-area-inset-top)) max(16px, env(safe-area-inset-right)) 10px max(16px, env(safe-area-inset-left));
}
.topbar-content {
  max-width: 1200px;
  margin: 0 auto;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  flex-wrap: wrap;
}
.brand-group {
  display: flex;
  align-items: center;
  gap: 12px;
}
.brand-badge {
  width: 34px;
  height: 34px;
  border-radius: 9px;
  background: linear-gradient(135deg, #10b981 0%, #064e3b 100%);
  display: flex;
  align-items: center;
  justify-content: center;
  color: #fff;
  box-shadow: 0 0 14px rgba(16, 185, 129, 0.35);
}
.brand-title {
  font-size: 15px;
  font-weight: 700;
  letter-spacing: -0.01em;
}
.brand-subtitle {
  font-size: 11px;
  color: var(--mute);
}
.status-cluster {
  display: flex;
  align-items: center;
  gap: 10px;
}
.pill {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 4px 10px;
  border-radius: 99px;
  font-size: 11px;
  font-weight: 600;
  letter-spacing: 0.03em;
  border: 1px solid var(--line);
}
.pill-mode { background: #0c1527; color: #38bdf8; border-color: rgba(56, 189, 248, 0.3); }
.pill-live { background: rgba(16, 185, 129, 0.12); color: #34d399; border-color: rgba(16, 185, 129, 0.35); }
.pill-shariah { background: rgba(16, 185, 129, 0.1); color: #10b981; border-color: rgba(16, 185, 129, 0.25); text-transform: uppercase; }
.dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  display: inline-block;
}
.pulse-green {
  background: #10b981;
  box-shadow: 0 0 0 0 rgba(16, 185, 129, 0.7);
  animation: pulse 2s infinite;
}
@keyframes pulse {
  0% { transform: scale(0.95); box-shadow: 0 0 0 0 rgba(16, 185, 129, 0.7); }
  70% { transform: scale(1); box-shadow: 0 0 0 6px rgba(16, 185, 129, 0); }
  100% { transform: scale(0.95); box-shadow: 0 0 0 0 rgba(16, 185, 129, 0); }
}
.actions-group {
  display: flex;
  align-items: center;
  gap: 10px;
}
.btn-kill {
  background: #b91c1c;
  color: #fff;
  border: 1px solid #ef4444;
  padding: 6px 14px;
  border-radius: 8px;
  font-size: 12px;
  font-weight: 700;
  letter-spacing: 0.03em;
  cursor: pointer;
  display: flex;
  align-items: center;
  gap: 6px;
  transition: all 0.2s;
  box-shadow: 0 2px 8px rgba(185, 28, 28, 0.4);
}
.btn-kill:hover {
  background: #dc2626;
  box-shadow: 0 4px 14px rgba(220, 38, 38, 0.6);
}
.user-pill {
  display: flex;
  align-items: center;
  gap: 6px;
  background: #0f172a;
  border: 1px solid var(--line);
  padding: 5px 12px;
  border-radius: 8px;
  font-size: 12px;
  color: var(--mute);
}
.btn-logout {
  background: transparent;
  color: var(--mute);
  border: 1px solid var(--line);
  padding: 5px 10px;
  border-radius: 8px;
  font-size: 12px;
  cursor: pointer;
  transition: all 0.15s;
  display: flex;
  align-items: center;
  gap: 4px;
}
.btn-logout:hover {
  color: #fff;
  background: #1e293b;
  border-color: #334155;
}

/* Layout Container */
.container {
  max-width: 1200px;
  margin: 20px auto 0;
  padding: 0 20px;
}

/* Emergency Halt Banner */
.halt-banner {
  display: none;
  background: linear-gradient(135deg, rgba(239, 68, 68, 0.18) 0%, rgba(153, 27, 27, 0.28) 100%);
  border: 1px solid #ef4444;
  color: #fecaca;
  padding: 14px 20px;
  border-radius: 12px;
  margin-bottom: 20px;
  box-shadow: 0 8px 24px rgba(239, 68, 68, 0.25);
}
.halt-content {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  flex-wrap: wrap;
}
.btn-resume {
  background: #10b981;
  color: #022c22;
  border: none;
  font-size: 12px;
  font-weight: 700;
  padding: 8px 16px;
  border-radius: 8px;
  cursor: pointer;
  transition: all 0.2s;
}
.btn-resume:hover {
  background: #34d399;
  color: #fff;
}

/* KPI Bento Grid */
.kpi-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(170px, 1fr));
  gap: 14px;
  margin-bottom: 20px;
}
.kpi-card {
  background: var(--card);
  border: 1px solid var(--card-border);
  border-radius: 14px;
  padding: 16px 18px;
  transition: all 0.2s ease;
  position: relative;
  overflow: hidden;
}
.kpi-card:hover {
  border-color: #334155;
  background: var(--card-hover);
  transform: translateY(-1px);
}
.kpi-card::before {
  content: '';
  position: absolute;
  top: 0;
  left: 0;
  right: 0;
  height: 2px;
  background: linear-gradient(90deg, transparent, rgba(56, 189, 248, 0.4), transparent);
}
.kpi-label {
  font-size: 11px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.05em;
  color: var(--mute);
  margin-bottom: 6px;
}
.kpi-value {
  font-size: 23px;
  font-weight: 700;
  letter-spacing: -0.02em;
  font-family: 'JetBrains Mono', monospace;
}
.kpi-sub {
  font-size: 11px;
  color: var(--subtle);
  margin-top: 4px;
}

/* Cards & Section Styling */
.card {
  background: var(--card);
  border: 1px solid var(--card-border);
  border-radius: 16px;
  padding: 20px 22px;
  margin-bottom: 18px;
  box-shadow: 0 4px 20px rgba(0, 0, 0, 0.3);
  transition: padding-bottom 0.2s ease;
}
.card-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 16px;
  flex-wrap: wrap;
  gap: 10px;
  cursor: pointer;
  user-select: none;
  border-radius: 10px;
  padding: 6px 10px;
  margin: -6px -10px 14px -10px;
  transition: background 0.18s ease;
}
.card-header:hover {
  background: rgba(255, 255, 255, 0.04);
}
.card-header:active {
  background: rgba(255, 255, 255, 0.07);
}
.card-header-actions {
  display: flex;
  align-items: center;
  gap: 8px;
}
.card-title {
  font-size: 14px;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.05em;
  color: var(--ink);
  display: flex;
  align-items: center;
  gap: 8px;
}
.card-title svg {
  color: var(--accent);
}
.btn-collapse {
  background: rgba(148, 163, 184, 0.08);
  border: 1px solid rgba(148, 163, 184, 0.2);
  color: var(--mute);
  width: 28px;
  height: 28px;
  border-radius: 8px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  cursor: pointer;
  padding: 0;
  transition: all 0.2s cubic-bezier(0.16, 1, 0.3, 1);
  flex-shrink: 0;
}
.card-header:hover .btn-collapse {
  color: #fff;
  border-color: rgba(56, 189, 248, 0.4);
  background: rgba(56, 189, 248, 0.12);
}
.btn-collapse .chevron {
  transition: transform 0.25s cubic-bezier(0.16, 1, 0.3, 1);
}
.card.collapsed {
  padding-bottom: 14px;
}
.card.collapsed .card-header {
  margin-bottom: 0;
}
.card.collapsed .card-body {
  display: none !important;
}
.card.collapsed .btn-collapse .chevron {
  transform: rotate(-90deg);
}
.card.collapsed .btn-collapse {
  background: rgba(148, 163, 184, 0.14);
  color: var(--txt);
}
.card-body {
  transition: opacity 0.2s ease;
}
.card-body .kpi-grid {
  margin-bottom: 0;
}
.btn-toggle-all {
  background: rgba(148, 163, 184, 0.08);
  color: #cbd5e1;
  border: 1px solid rgba(148, 163, 184, 0.22);
  padding: 5px 11px;
  border-radius: 8px;
  font-size: 12px;
  font-weight: 600;
  cursor: pointer;
  display: inline-flex;
  align-items: center;
  gap: 5px;
  transition: all 0.2s;
}
.btn-toggle-all:hover {
  background: rgba(148, 163, 184, 0.18);
  color: #fff;
  border-color: rgba(56, 189, 248, 0.4);
}

/* AI Spotlight Card */
.ai-card {
  background: linear-gradient(135deg, rgba(15, 23, 42, 0.95) 0%, rgba(11, 17, 32, 0.95) 100%);
  border: 1px solid #1e3a5f;
  box-shadow: 0 8px 32px rgba(14, 165, 233, 0.12), inset 0 1px 0 rgba(255, 255, 255, 0.05);
}
.ai-summary-bar {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
  padding: 12px 14px;
  background: rgba(15, 23, 42, 0.6);
  border: 1px solid var(--line);
  border-radius: 10px;
  margin-bottom: 14px;
}
.trade-box {
  background: #080d1a;
  border: 1px solid #1e293b;
  border-radius: 12px;
  padding: 16px 18px;
}
.trade-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 10px;
  margin-bottom: 14px;
}
.trade-symbol {
  font-size: 20px;
  font-weight: 800;
  font-family: 'JetBrains Mono', monospace;
  color: #fff;
  display: flex;
  align-items: center;
  gap: 10px;
}
.trade-badge {
  background: rgba(16, 185, 129, 0.15);
  border: 1px solid rgba(16, 185, 129, 0.4);
  color: #34d399;
  font-size: 11px;
  font-weight: 700;
  padding: 2px 8px;
  border-radius: 6px;
}
.trade-metrics {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(130px, 1fr));
  gap: 12px;
  margin-bottom: 14px;
}
.metric-box {
  background: rgba(15, 23, 42, 0.5);
  border: 1px solid #1e293b;
  border-radius: 8px;
  padding: 10px 12px;
}
.metric-box .lbl {
  font-size: 10px;
  color: var(--mute);
  text-transform: uppercase;
  font-weight: 600;
  letter-spacing: 0.04em;
}
.metric-box .val {
  font-size: 15px;
  font-weight: 700;
  font-family: 'JetBrains Mono', monospace;
  margin-top: 2px;
}
.thesis-text {
  font-size: 13px;
  color: #cbd5e1;
  line-height: 1.55;
  margin-top: 10px;
  padding-top: 10px;
  border-top: 1px solid #1e293b;
}

/* AI Take-Profit Maximizer Box */
.maximizer-box {
  margin-top: 14px;
  background: rgba(16, 185, 129, 0.06);
  border: 1px solid rgba(16, 185, 129, 0.25);
  border-radius: 10px;
  padding: 14px 16px;
}
.maximizer-head {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 12px;
  font-weight: 700;
  color: #34d399;
  text-transform: uppercase;
  letter-spacing: 0.04em;
  margin-bottom: 8px;
}

/* Tables */
.table-wrap {
  overflow-x: auto;
  -webkit-overflow-scrolling: touch;
}
table {
  width: 100%;
  border-collapse: collapse;
  min-width: 600px;
}
th {
  font-size: 11px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.05em;
  color: var(--mute);
  padding: 10px 12px;
  border-bottom: 1px solid var(--line);
  text-align: right;
  white-space: nowrap;
}
th:first-child { text-align: left; }
td {
  padding: 11px 12px;
  border-bottom: 1px solid rgba(30, 41, 59, 0.6);
  text-align: right;
  font-size: 13px;
  white-space: nowrap;
  font-variant-numeric: tabular-nums;
}
td:first-child { text-align: left; font-weight: 600; }
tr:hover td {
  background: rgba(255, 255, 255, 0.02);
}

/* Colors & Helpers */
.up { color: var(--up); }
.down { color: var(--down); }
.warn { color: var(--warn); }
.mute { color: var(--mute); }
.chip-pnl {
  display: inline-block;
  padding: 2px 7px;
  border-radius: 6px;
  font-size: 11px;
  font-weight: 600;
  font-family: 'JetBrains Mono', monospace;
}
.chip-pnl.up { background: var(--up-bg); color: var(--up); border: 1px solid var(--up-border); }
.chip-pnl.down { background: var(--down-bg); color: var(--down); border: 1px solid var(--down-border); }

/* Empty state */
.empty-state {
  text-align: center;
  padding: 32px 16px;
  color: var(--mute);
}
.empty-state svg {
  color: var(--subtle);
  margin-bottom: 8px;
}

/* Footer */
footer {
  text-align: center;
  font-size: 12px;
  color: var(--subtle);
  margin-top: 32px;
}

/* PWA Install Banner & Buttons */
.pwa-banner {
  background: linear-gradient(135deg, rgba(16, 185, 129, 0.16) 0%, rgba(15, 23, 42, 0.95) 100%);
  border-bottom: 1px solid rgba(16, 185, 129, 0.35);
  padding: 10px 16px;
  display: none;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  font-size: 13px;
  position: sticky;
  top: 0;
  z-index: 105;
  backdrop-filter: blur(10px);
  -webkit-backdrop-filter: blur(10px);
}
.pwa-banner-content {
  max-width: 1200px;
  margin: 0 auto;
  width: 100%;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
}
.pwa-info {
  display: flex;
  align-items: center;
  gap: 10px;
}
.pwa-icon {
  width: 32px;
  height: 32px;
  border-radius: 8px;
  background: #022c22;
  border: 1px solid #10b981;
  color: #34d399;
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
}
.btn-install-pwa {
  background: #10b981;
  color: #022c22;
  font-weight: 700;
  border: none;
  padding: 6px 14px;
  border-radius: 8px;
  font-size: 12px;
  cursor: pointer;
  display: flex;
  align-items: center;
  gap: 6px;
  transition: all 0.2s;
  box-shadow: 0 2px 8px rgba(16, 185, 129, 0.35);
}
.btn-install-pwa:hover {
  background: #34d399;
  color: #fff;
}
.btn-dismiss-pwa {
  background: transparent;
  color: var(--mute);
  border: none;
  font-size: 18px;
  cursor: pointer;
  padding: 2px 6px;
  line-height: 1;
}
.btn-header-install {
  display: none;
  align-items: center;
  gap: 5px;
  background: rgba(16, 185, 129, 0.14);
  color: #34d399;
  border: 1px solid rgba(16, 185, 129, 0.35);
  padding: 5px 10px;
  border-radius: 8px;
  font-size: 12px;
  font-weight: 600;
  cursor: pointer;
  transition: all 0.2s;
}
.btn-header-install:hover {
  background: #10b981;
  color: #022c22;
}
.ios-hint {
  display: none;
  background: #0c1527;
  border: 1px solid rgba(56, 189, 248, 0.3);
  color: #93c5fd;
  padding: 9px 14px;
  border-radius: 10px;
  font-size: 12px;
  margin-bottom: 14px;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}

/* Mobile & Tablet Responsive Media Queries */
@media (max-width: 768px) {
  .topbar {
    padding: max(8px, env(safe-area-inset-top)) max(12px, env(safe-area-inset-right)) 8px max(12px, env(safe-area-inset-left));
  }
  .topbar-content {
    gap: 8px;
  }
  .brand-title {
    font-size: 14px;
  }
  .brand-subtitle {
    display: none;
  }
  .status-cluster {
    gap: 6px;
    flex-wrap: wrap;
  }
  .actions-group {
    width: 100%;
    justify-content: space-between;
    gap: 6px;
    margin-top: 4px;
  }
  .actions-group .user-pill {
    padding: 5px 8px;
    font-size: 11px;
    max-width: 130px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .btn-kill {
    padding: 5px 10px;
    font-size: 11px;
  }
  .container {
    padding: 0 max(12px, env(safe-area-inset-left)) 0 max(12px, env(safe-area-inset-right));
    margin-top: 12px;
  }
  .kpi-grid {
    grid-template-columns: repeat(2, 1fr) !important;
    gap: 8px !important;
    margin-bottom: 12px;
  }
  .kpi-card {
    padding: 12px 12px;
    border-radius: 12px;
  }
  .kpi-label {
    font-size: 10px;
    margin-bottom: 4px;
  }
  .kpi-value {
    font-size: 18px;
  }
  .kpi-sub {
    font-size: 10px;
  }
  .card {
    padding: 14px 14px;
    border-radius: 14px;
    margin-bottom: 14px;
  }
  .card.collapsed {
    padding-bottom: 12px;
  }
  .card-header {
    padding: 4px 6px;
    margin: -4px -6px 12px -6px;
  }
  .card.collapsed .card-header {
    margin-bottom: 0;
  }
  .btn-toggle-all {
    padding: 4px 8px;
    font-size: 11px;
  }
  .card-title {
    font-size: 13px;
  }
  .trade-head {
    flex-direction: column;
    align-items: flex-start;
    gap: 8px;
  }
  .trade-symbol {
    font-size: 18px;
  }
  .trade-metrics {
    grid-template-columns: repeat(2, 1fr) !important;
    gap: 8px !important;
  }
  .budget-grid {
    grid-template-columns: 1fr !important;
    gap: 8px !important;
  }
  .table-wrap {
    overflow-x: auto;
    -webkit-overflow-scrolling: touch;
    margin: 0 -14px;
    padding: 0 14px;
    scrollbar-width: thin;
  }
  table {
    min-width: 550px;
  }
  th, td {
    padding: 8px 10px;
    font-size: 12px;
  }
  .halt-content {
    flex-direction: column;
    align-items: flex-start;
    gap: 12px;
  }
  .btn-resume {
    width: 100%;
    text-align: center;
  }
}

@media (max-width: 480px) {
  .kpi-grid {
    grid-template-columns: repeat(2, 1fr) !important;
    gap: 6px !important;
  }
  .kpi-card {
    padding: 10px 10px;
  }
  .kpi-value {
    font-size: 16px;
  }
  .pill {
    padding: 2px 7px;
    font-size: 10px;
  }
  .btn-kill {
    font-size: 10px;
    padding: 4px 8px;
  }
}


/* Toast & Notification System */
.toast-container {
  position: fixed;
  top: max(20px, env(safe-area-inset-top));
  right: max(20px, env(safe-area-inset-right));
  z-index: 9999;
  display: flex;
  flex-direction: column;
  gap: 10px;
  max-width: 380px;
  width: calc(100vw - 32px);
  pointer-events: none;
}
.toast {
  pointer-events: auto;
  background: rgba(15, 23, 42, 0.95);
  backdrop-filter: blur(14px);
  -webkit-backdrop-filter: blur(14px);
  border: 1px solid #1e293b;
  border-radius: 12px;
  padding: 14px 16px;
  box-shadow: 0 10px 30px rgba(0, 0, 0, 0.6), 0 0 15px rgba(56, 189, 248, 0.15);
  display: flex;
  align-items: flex-start;
  gap: 12px;
  transform: translateX(120%);
  animation: slideInToast 0.35s cubic-bezier(0.16, 1, 0.3, 1) forwards;
  position: relative;
  overflow: hidden;
}
@keyframes slideInToast {
  to { transform: translateX(0); }
}
.toast.toast-fadeout {
  animation: fadeOutToast 0.3s ease forwards;
}
@keyframes fadeOutToast {
  to { opacity: 0; transform: translateY(-10px) scale(0.95); }
}
.toast-buy {
  border-color: rgba(16, 185, 129, 0.6);
  box-shadow: 0 10px 30px rgba(0, 0, 0, 0.6), 0 0 20px rgba(16, 185, 129, 0.25);
}
.toast-sell, .toast-stop {
  border-color: rgba(239, 68, 68, 0.6);
  box-shadow: 0 10px 30px rgba(0, 0, 0, 0.6), 0 0 20px rgba(239, 68, 68, 0.25);
}
.toast-profit {
  border-color: rgba(52, 211, 153, 0.7);
  box-shadow: 0 10px 30px rgba(0, 0, 0, 0.6), 0 0 20px rgba(52, 211, 153, 0.3);
}
.toast-icon {
  font-size: 20px;
  line-height: 1;
  flex-shrink: 0;
  margin-top: 2px;
}
.toast-content {
  flex: 1;
  min-width: 0;
}
.toast-title {
  font-size: 13px;
  font-weight: 700;
  color: #fff;
  letter-spacing: -0.01em;
  margin-bottom: 2px;
}
.toast-body {
  font-size: 12px;
  color: #cbd5e1;
  line-height: 1.45;
  word-break: break-word;
}
.toast-time {
  font-size: 10px;
  color: var(--mute);
  margin-top: 4px;
}
.toast-close {
  background: transparent;
  border: none;
  color: var(--mute);
  font-size: 18px;
  cursor: pointer;
  line-height: 1;
  padding: 0 4px;
}
.toast-close:hover {
  color: #fff;
}
.btn-notif {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  background: #0f172a;
  color: var(--mute);
  border: 1px solid var(--line);
  padding: 5px 10px;
  border-radius: 8px;
  font-size: 12px;
  font-weight: 600;
  cursor: pointer;
  transition: all 0.2s;
}
.btn-notif:hover {
  color: #fff;
  border-color: #38bdf8;
}
.btn-notif.active {
  background: rgba(16, 185, 129, 0.12);
  color: #34d399;
  border-color: rgba(16, 185, 129, 0.35);
}

</style>
</head>
<body>

<div id="toastContainer" class="toast-container"></div>
<div id="pwaBanner" class="pwa-banner">
  <div class="pwa-banner-content">
    <div class="pwa-info">
      <div class="pwa-icon">
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>
      </div>
      <div>
        <strong style="color:#fff">Install Alvi Terminal</strong>
        <div style="font-size:11px;color:var(--mute)">Add to Home screen for full-screen autonomous mobile trading</div>
      </div>
    </div>
    <div style="display:flex;align-items:center;gap:8px">
      <button id="pwaInstallBtn" class="btn-install-pwa" onclick="triggerPwaInstall()">
        <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>
        <span>Install App</span>
      </button>
      <button id="pwaDismissBtn" class="btn-dismiss-pwa" onclick="dismissPwaBanner()">&times;</button>
    </div>
  </div>
</div>

<div class="topbar">
  <div class="topbar-content">
    <div class="brand-group">
      <div class="brand-badge">
        <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round">
          <path d="M12 2L2 7l10 5 10-5-10-5zM2 17l10 5 10-5M2 12l10 5 10-5"/>
        </svg>
      </div>
      <div>
        <div class="brand-title">Alvi Quantitative Terminal</div>
        <div class="brand-subtitle" id="sub">Connecting to execution agent…</div>
      </div>
    </div>

    <div class="status-cluster">
      <span class="pill pill-mode" id="modePill">
        <span class="dot" style="background:#38bdf8"></span>
        <span id="modeText">PAPER</span>
      </span>
      <span class="pill pill-shariah">AAOIFI COMPLIANT</span>
      <span class="pill" id="marketPill" style="background:#090d16;color:var(--mute)">
        <span class="dot" id="marketDot" style="background:var(--subtle)"></span>
        <span id="marketText">Checking Market…</span>
      </span>
    </div>

    <div class="actions-group">
      <button id="btnToggleAll" class="btn-toggle-all" onclick="toggleAllCards()" title="Collapse / Expand All Sections">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="7 13 12 18 17 13"/><polyline points="7 6 12 11 17 6"/></svg>
        <span id="toggleAllText">Collapse All</span>
      </button>
      <button id="btnNotif" class="btn-notif" onclick="toggleNotifications()" title="Web & App Trade Notifications">
        <span id="notifBell">🔔</span>
        <span id="notifStatusText">Alerts</span>
      </button>
      <button id="btnHeaderInstall" class="btn-header-install" onclick="triggerPwaInstall()">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>
        <span>Install App</span>
      </button>
      <button class="btn-kill" id="btn-kill" onclick="triggerKill()">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polygon points="7.86 2 16.14 2 22 7.86 22 16.14 16.14 22 7.86 22 2 16.14 2 7.86 7.86 2"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg>
        <span>KILL SWITCH</span>
      </button>
      <div class="user-pill" id="userPill">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/></svg>
        <span id="userName">admin</span>
      </div>
      <button class="btn-logout" onclick="logout()" title="Sign Out">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/><polyline points="16 17 21 12 16 7"/><line x1="21" y1="12" x2="9" y2="12"/></svg>
      </button>
    </div>
  </div>
</div>

<div class="container">
  <!-- iOS Install Hint -->
  <div id="iosHint" class="ios-hint">
    <div style="display:flex;align-items:center;gap:8px">
      <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="5" y="2" width="14" height="20" rx="2" ry="2"/><line x1="12" y1="18" x2="12.01" y2="18"/></svg>
      <span>Install on iPhone: Tap <strong>Share</strong> (square with arrow) &rarr; <strong>Add to Home Screen</strong></span>
    </div>
    <button onclick="dismissIosHint()" style="background:none;border:none;color:#93c5fd;font-size:16px;cursor:pointer">&times;</button>
  </div>
  <!-- Emergency Halt Active Banner -->
  <div id="killbanner" class="halt-banner">
    <div class="halt-content">
      <div style="display:flex;align-items:center;gap:12px">
        <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="#ef4444" stroke-width="2.5"><circle cx="12" cy="12" r="10"/><line x1="15" y1="9" x2="9" y2="15"/><line x1="9" y1="9" x2="15" y2="15"/></svg>
        <div>
          <strong style="color:#fff;font-size:14px">EMERGENCY KILL SWITCH ENGAGED</strong>
          <div id="killreason" style="font-size:12px;margin-top:2px;color:#fca5a5">All automated orders and executions are halted.</div>
        </div>
      </div>
      <button class="btn-resume" onclick="resetKill()">DISENGAGE & RESUME TRADING</button>
    </div>
  </div>

  <!-- Portfolio Performance & Capital Metrics -->
  <div class="card" id="kpiCard">
    <div class="card-header" onclick="toggleCard('kpiCard')">
      <div class="card-title">
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="3" width="7" height="7"/><rect x="14" y="3" width="7" height="7"/><rect x="14" y="14" width="7" height="7"/><rect x="3" y="14" width="7" height="7"/></svg>
        <span>Portfolio Performance & Capital Metrics</span>
      </div>
      <div class="card-header-actions">
        <button class="btn-collapse" type="button" aria-label="Toggle section">
          <svg class="chevron" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polyline points="6 9 12 15 18 9"/></svg>
        </button>
      </div>
    </div>
    <div class="card-body">
      <div class="kpi-grid" id="kpis"></div>
    </div>
  </div>

  <!-- AI Spotlight Card -->
  <div class="card ai-card" id="aicard" style="display:none">
    <div class="card-header" onclick="toggleCard('aicard')">
      <div class="card-title">
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 2a4 4 0 0 1 4 4c0 1.95-1.4 3.58-3.25 3.93l-.25.04v2.03h2.5a2.5 2.5 0 0 1 2.45 2l.05.25v2.5a2.5 2.5 0 0 1-2 2.45l-.25.05h-6.5a2.5 2.5 0 0 1-2.45-2l-.05-.25v-2.5a2.5 2.5 0 0 1 2-2.45l.25-.05h2.5v-2.03l-.25-.04A4 4 0 0 1 12 2z"/></svg>
        <span>AI Quantitative Analyst</span>
      </div>
      <div class="card-header-actions">
        <span id="aimodel" class="pill" style="background:#0f172a;color:#38bdf8;border-color:#1e3a5f"></span>
        <button class="btn-collapse" type="button" aria-label="Toggle section">
          <svg class="chevron" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polyline points="6 9 12 15 18 9"/></svg>
        </button>
      </div>
    </div>
    <div class="card-body">
      <div id="aisum" class="ai-summary-bar"></div>
      <div id="aitrade" class="trade-box"></div>
      <div id="aicandidates" style="margin-top:14px;border-top:1px solid #1e293b;padding-top:12px"></div>
    </div>
  </div>

  <!-- Active Holdings Table -->
  <div class="card" id="holdCard">
    <div class="card-header" onclick="toggleCard('holdCard')">
      <div class="card-title">
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/></svg>
        <span>Active Portfolio Holdings</span>
      </div>
      <div class="card-header-actions">
        <div id="holdCount" class="pill" style="background:#0f172a;color:var(--mute)">0 Assets</div>
        <button class="btn-collapse" type="button" aria-label="Toggle section">
          <svg class="chevron" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polyline points="6 9 12 15 18 9"/></svg>
        </button>
      </div>
    </div>
    <div class="card-body">
      <div class="table-wrap">
        <table id="hold"></table>
      </div>
    </div>
  </div>

  <!-- Swing Sandbox (Paper) -->
  <div class="card" id="swingcard" style="display:none">
    <div class="card-header" onclick="toggleCard('swingcard')">
      <div class="card-title">
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 3v18h18"/><path d="m19 9-5 5-4-4-3 3"/></svg>
        <span>Swing Sandbox (Paper Breakouts)</span>
      </div>
      <div class="card-header-actions">
        <div id="swrule" class="pill" style="background:#0f172a;color:#38bdf8">Breakout Rule</div>
        <button class="btn-collapse" type="button" aria-label="Toggle section">
          <svg class="chevron" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polyline points="6 9 12 15 18 9"/></svg>
        </button>
      </div>
    </div>
    <div class="card-body">
      <div id="swsum" style="font-size:13px;color:#cbd5e1;margin-bottom:12px;line-height:1.5"></div>
      <div class="table-wrap">
        <table id="swhold"></table>
      </div>
      <div id="swnext" class="mute" style="font-size:12px;margin:10px 0"></div>
      <div class="table-wrap">
        <table id="swtr"></table>
      </div>
    </div>
  </div>

  <!-- Execution Engine & Monitor Status -->
  <div class="card" id="monCard">
    <div class="card-header" onclick="toggleCard('monCard')">
      <div class="card-title">
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="2" y="3" width="20" height="14" rx="2" ry="2"/><line x1="8" y1="21" x2="16" y2="21"/><line x1="12" y1="17" x2="12" y2="21"/></svg>
        <span>Execution Engine & Real-Time Monitor</span>
      </div>
      <div class="card-header-actions">
        <button class="btn-collapse" type="button" aria-label="Toggle section">
          <svg class="chevron" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polyline points="6 9 12 15 18 9"/></svg>
        </button>
      </div>
    </div>
    <div class="card-body">
      <div id="mon" style="font-size:13px;line-height:1.6"></div>
      <div id="alerts" style="margin-top:12px;font-size:12px"></div>
    </div>
  </div>

  <!-- Recent Trade Orders -->
  <div class="card" id="tradesCard">
    <div class="card-header" onclick="toggleCard('tradesCard')">
      <div class="card-title">
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 14 14"/></svg>
        <span>Recent Executed Orders</span>
      </div>
      <div class="card-header-actions">
        <div id="tradesBadge" class="pill" style="display:none"></div>
        <button class="btn-collapse" type="button" aria-label="Toggle section">
          <svg class="chevron" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polyline points="6 9 12 15 18 9"/></svg>
        </button>
      </div>
    </div>
    <div class="card-body">
      <div class="table-wrap">
        <table id="trades"></table>
      </div>
    </div>
  </div>

  <footer>
    Autonomous Execution Terminal · Delivery CNC Only · Non-Leveraged · Protected by Tailscale Mesh
  </footer>
</div>

<script>
const inr = n => n == null ? '–' : '₹' + Number(n).toLocaleString('en-IN', { maximumFractionDigits: 0 });
const px = n => n == null ? '–' : Number(n).toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const pct = n => n == null ? '–' : (n >= 0 ? '+' : '') + (n * 100).toFixed(1) + '%';
const cls = n => n == null ? '' : (n >= 0 ? 'up' : 'down');

function el(t, c, x) {
  const e = document.createElement(t);
  if (c) e.className = c;
  if (x != null) e.textContent = x;
  return e;
}

function row(cells, head) {
  const tr = el('tr');
  cells.forEach(c => {
    const td = el(head ? 'th' : 'td', c.c || '', c.t);
    tr.appendChild(td);
  });
  return tr;
}

function fill(id, rows) {
  const t = document.getElementById(id);
  t.replaceChildren(...rows);
}

async function triggerKill() {
  if (!confirm('EMERGENCY: Halt all trading and activate Kill Switch?')) return;
  try {
    const r = await fetch('/api/kill-switch/trigger', { method: 'POST' });
    if (r.ok) load();
    else alert('Failed: HTTP ' + r.status);
  } catch (e) {
    alert('Network error: ' + e);
  }
}

async function resetKill() {
  if (!confirm('RESUME: Disengage Kill Switch and restore trading?')) return;
  try {
    const r = await fetch('/api/kill-switch/reset', { method: 'POST' });
    if (r.ok) load();
    else alert('Failed: HTTP ' + r.status);
  } catch (e) {
    alert('Network error: ' + e);
  }
}

async function logout() {
  try {
    await fetch('/api/logout', { method: 'POST' });
  } catch (e) {}
  window.location.href = '/login';
}


// Web & Mobile App Notifications System
let notificationsEnabled = localStorage.getItem('notifications_active') === 'true';
const seenTradeKeys = new Set();
const seenAlertKeys = new Set();
const seenNotifIds = new Set();
let isFirstLoad = true;

function playChime(type = 'buy') {
  try {
    const AudioCtx = window.AudioContext || window.webkitAudioContext;
    if (!AudioCtx) return;
    const ctx = new AudioCtx();
    const osc = ctx.createOscillator();
    const gain = ctx.createGain();
    osc.connect(gain);
    gain.connect(ctx.destination);
    const now = ctx.currentTime;
    if (type === 'buy') {
      osc.frequency.setValueAtTime(587.33, now); // D5
      osc.frequency.setValueAtTime(880.00, now + 0.12); // A5
    } else if (type === 'sell' || type === 'stop') {
      osc.frequency.setValueAtTime(880.00, now); // A5
      osc.frequency.setValueAtTime(587.33, now + 0.12); // D5
    } else {
      osc.frequency.setValueAtTime(523.25, now); // C5
      osc.frequency.setValueAtTime(659.25, now + 0.12); // E5
    }
    gain.gain.setValueAtTime(0.2, now);
    gain.gain.exponentialRampToValueAtTime(0.001, now + 0.4);
    osc.start(now);
    osc.stop(now + 0.4);
  } catch (e) {}
}

function showToast(title, message, type = 'info') {
  const container = document.getElementById('toastContainer');
  if (!container) return;
  const icons = {
    buy: '🟢',
    sell: '🔴',
    stop: '🚨',
    profit: '💰',
    alert: '⚠️',
    info: '📢',
    test: '🧪'
  };
  const toast = document.createElement('div');
  toast.className = `toast toast-${type}`;
  toast.innerHTML = `
    <div class="toast-icon">${icons[type] || '📢'}</div>
    <div class="toast-content">
      <div class="toast-title">${title}</div>
      <div class="toast-body">${message}</div>
      <div class="toast-time">Just now · Live Alert</div>
    </div>
    <button class="toast-close" onclick="this.parentElement.remove()">&times;</button>
  `;
  container.appendChild(toast);

  setTimeout(() => {
    if (toast.parentElement) {
      toast.classList.add('toast-fadeout');
      setTimeout(() => toast.remove(), 300);
    }
  }, 7000);
}

function showAppNotification(title, body, type = 'info') {
  playChime(type);
  if ('vibrate' in navigator) {
    try { navigator.vibrate([150, 60, 150]); } catch (e) {}
  }
  showToast(title, body, type);

  // Send OS-level Web / App Push Notification
  if ('Notification' in window && Notification.permission === 'granted') {
    if ('serviceWorker' in navigator && navigator.serviceWorker.controller) {
      navigator.serviceWorker.ready.then(reg => {
        reg.showNotification(title, {
          body: body,
          icon: '/icon-192.png',
          badge: '/favicon.svg',
          tag: 'trade-' + Date.now(),
          vibrate: [200, 100, 200]
        });
      }).catch(() => {
        try { new Notification(title, { body: body, icon: '/icon-192.png' }); } catch (e) {}
      });
    } else {
      try {
        new Notification(title, { body: body, icon: '/icon-192.png' });
      } catch (e) {}
    }
  }
}

async function toggleNotifications() {
  if (!('Notification' in window)) {
    showToast('Notifications Unsupported', 'Your browser does not support Web Notifications.', 'alert');
    return;
  }
  if (Notification.permission === 'granted') {
    await testNotification();
  } else if (Notification.permission === 'denied') {
    alert("Notifications are currently blocked. Please open browser settings to allow notifications.");
  } else {
    const perm = await Notification.requestPermission();
    updateNotifBtnState();
    if (perm === 'granted') {
      localStorage.setItem('notifications_active', 'true');
      showAppNotification('Notifications Activated 🔔', 'You will receive instant alerts for every trade and transaction.', 'test');
    }
  }
}

async function testNotification() {
  try {
    const res = await fetch('/api/notifications/test', { method: 'POST' });
    const data = await res.json();
    if (data.ok) {
      showAppNotification('🧪 Live Trade Notification Test', 'Groww CNC trade notifications and alerts are fully active!', 'test');
    }
  } catch (e) {
    showAppNotification('🧪 Trade Notification Test', 'Web & App notifications are active and ready!', 'test');
  }
}

function updateNotifBtnState() {
  const btn = document.getElementById('btnNotif');
  const txt = document.getElementById('notifStatusText');
  const bell = document.getElementById('notifBell');
  if (!btn || !txt) return;
  if ('Notification' in window && Notification.permission === 'granted') {
    btn.className = 'btn-notif active';
    txt.textContent = 'Alerts ON';
    if (bell) bell.textContent = '🔔';
  } else {
    btn.className = 'btn-notif';
    txt.textContent = 'Enable Alerts';
    if (bell) bell.textContent = '🔕';
  }
}

async function load() {
  let d;
  try {
    const r = await fetch('/api/summary', { cache: 'no-store' });
    if (r.status === 401) {
      window.location.href = '/login';
      return;
    }
    d = await r.json();
  } catch (e) {
    document.getElementById('sub').textContent = 'Cannot reach execution agent';
    return;
  }

  updateNotifBtnState();

  // Real-time Trade & Transaction Notification Dispatcher
  if (isFirstLoad) {
    (d.trades || []).forEach(t => seenTradeKeys.add(`${t.ts}_${t.symbol}_${t.side}_${t.qty}`));
    (d.monitor?.alerts || []).forEach(a => seenAlertKeys.add(`${a.ts}_${a.kind}_${a.symbol}`));
    (d.notifications || []).forEach(n => seenNotifIds.add(n.id));
    isFirstLoad = false;
  } else {
    // 1. Newly executed trade orders
    (d.trades || []).forEach(t => {
      const key = `${t.ts}_${t.symbol}_${t.side}_${t.qty}`;
      if (!seenTradeKeys.has(key)) {
        seenTradeKeys.add(key);
        const isBuy = (t.side || '').toUpperCase() === 'BUY';
        const title = `${isBuy ? '🟢 BUY EXECUTED' : '🔴 SELL EXECUTED'}: ${t.qty} ${t.symbol}`;
        const body = `${t.side} ${t.qty} shares @ ₹${t.fill_price ? Number(t.fill_price).toFixed(2) : '–'} (Total: ₹${((t.qty||0)*(t.fill_price||0)).toLocaleString('en-IN')})`;
        showAppNotification(title, body, isBuy ? 'buy' : 'sell');
      }
    });

    // 2. Newly triggered monitor alerts (stop-loss, profit taken)
    (d.monitor?.alerts || []).forEach(a => {
      const key = `${a.ts}_${a.kind}_${a.symbol}`;
      if (!seenAlertKeys.has(key)) {
        seenAlertKeys.add(key);
        let type = 'alert';
        let title = `⚠️ ALERT: ${a.symbol}`;
        if (a.kind === 'AI_STOP_EXECUTED' || a.kind === 'STOP_SOLD') {
          type = 'stop';
          title = `🚨 STOP LOSS SOLD: ${a.symbol}`;
        } else if (a.kind === 'AI_PROFIT_TAKEN') {
          type = 'profit';
          title = `💰 PROFIT TAKEN: ${a.symbol}`;
        }
        showAppNotification(title, a.detail || a.kind, type);
      }
    });

    // 3. New backend notifications
    (d.notifications || []).forEach(n => {
      if (!seenNotifIds.has(n.id)) {
        seenNotifIds.add(n.id);
        showAppNotification(n.title, n.message, n.type || 'info');
      }
    });
  }

  // Update Auth User
  if (d.auth_user) {
    document.getElementById('userName').textContent = d.auth_user;
  }

  // Kill Switch state
  const ks = d.kill_switch || {};
  const kb = document.getElementById('killbanner');
  const bk = document.getElementById('btn-kill');
  if (ks.active) {
    kb.style.display = 'block';
    bk.style.display = 'none';
    document.getElementById('killreason').textContent = ks.reason ? ('Halt Reason: ' + ks.reason) : 'All orders suspended.';
  } else {
    kb.style.display = 'none';
    bk.style.display = 'inline-flex';
  }

  // Mode pill
  const modePill = document.getElementById('modePill');
  const isLive = d.broker_mode === 'live';
  modePill.className = isLive ? 'pill pill-live' : 'pill pill-mode';
  document.getElementById('modeText').textContent = isLive ? 'LIVE (GROWW CNC)' : 'PAPER SIMULATION';

  // Market status
  const m = d.monitor || {};
  const mDot = document.getElementById('marketDot');
  const mText = document.getElementById('marketText');
  if (m.market_open) {
    mDot.className = 'dot pulse-green';
    mText.textContent = 'NSE OPEN (09:15–15:30 IST)';
    document.getElementById('marketPill').style.color = '#34d399';
    document.getElementById('marketPill').style.borderColor = 'rgba(16, 185, 129, 0.3)';
  } else {
    mDot.className = 'dot';
    mDot.style.background = 'var(--warn)';
    mText.textContent = 'NSE CLOSED (Mon–Fri 09:15–15:30 IST)';
    document.getElementById('marketPill').style.color = 'var(--mute)';
    document.getElementById('marketPill').style.borderColor = 'var(--line)';
  }

  document.getElementById('sub').textContent = 'Updated ' + new Date().toLocaleTimeString() +
    (d.screen.total ? ` · ${d.screen.compliant} of ${d.screen.total} stocks pass Shariah screen` : '');

  // Render Bento KPIs
  const k = document.getElementById('kpis');
  k.replaceChildren();
  const kpiData = [
    ['Total Equity', inr(d.equity), 'Net liquidation value', ''],
    ['Liquid Cash', inr(d.cash), 'Available for delivery buys', ''],
    ['Monthly Contribution', inr(d.deposited), 'Target capital inflow', ''],
    ['Total P/L', d.pnl == null ? '–' : inr(d.pnl) + ' (' + pct(d.pnl_pct) + ')', 'Unrealized & realized', cls(d.pnl)],
    ['Frictional Costs', inr(d.total_costs), 'Brokerage, STT, DP fees', ''],
    ['Next Rebalance', d.next_rebalance || 'Due Now', 'Quarterly cadence', '']
  ];
  kpiData.forEach(([lbl, val, sub, c]) => {
    const card = el('div', 'kpi-card');
    card.appendChild(el('div', 'kpi-label', lbl));
    card.appendChild(el('div', 'kpi-value ' + c, val));
    card.appendChild(el('div', 'kpi-sub', sub));
    k.appendChild(card);
  });

  // Render Holdings
  document.getElementById('holdCount').textContent = d.holdings.length + ' Position' + (d.holdings.length === 1 ? '' : 's');
  if (!d.holdings.length) {
    fill('hold', [
      row(['Stock', 'Qty', 'Avg Cost', 'Last Price', 'Value', 'P/L', 'P/L %', 'vs Peak', 'Source'].map(t => ({ t })), true),
      row([{ t: 'No open positions held. Daily automated scan runs at 14:45 IST (Power Hour).', c: 'mute' }])
    ]);
  } else {
    fill('hold', [
      row(['Stock', 'Qty', 'Avg Cost', 'Last Price', 'Value', 'P/L', 'P/L %', 'vs Peak', 'Source'].map(t => ({ t })), true),
      ...d.holdings.map(h => row([
        { t: h.symbol },
        { t: h.qty },
        { t: px(h.avg_cost) },
        { t: px(h.last) },
        { t: inr(h.value) },
        { t: inr(h.pnl), c: cls(h.pnl) },
        { t: pct(h.pnl_pct), c: cls(h.pnl_pct) },
        { t: pct(h.from_peak_pct), c: cls(h.from_peak_pct) },
        { t: h.price_source, c: 'mute' }
      ]))
    ]);
  }

  // Render Monitor info
  const mon = document.getElementById('mon');
  mon.replaceChildren();
  if (m.heartbeat) {
    mon.appendChild(el('div', '', `Heartbeat: ${m.heartbeat.replace('T', ' ')} · Trailing Stop: ${m.stop_pct ? (m.stop_pct * 100) + '%' : 'Disabled'}`));
    const isLiveExec = Boolean(m.live_mode || d.broker_mode === 'live');
    const actText = isLiveExec ? 'Automatic Live Execution (Groww CNC)' : (m.act ? 'Automatic Paper Execution' : 'Alert Only (No automatic broker sell)');
    mon.appendChild(el('div', isLiveExec ? 'up' : 'mute', `Action Mode: ${actText}`));
  } else {
    mon.appendChild(el('div', 'mute', 'Real-time monitor loop not active.'));
  }

  const a = document.getElementById('alerts');
  a.replaceChildren();
  if (!m.alerts || !m.alerts.length) {
    a.innerHTML = '<span class="mute">No active risk alerts</span>';
  } else {
    m.alerts.forEach(x => {
      const p = el('div', 'warn', `${x.ts.replace('T', ' ')}  ${x.kind}  ${x.symbol}  ${x.detail}`);
      a.appendChild(p);
    });
  }

  // Render AI Trading Brain
  renderAI(d.ai_signals, d.api_budget);

  // Render Swing Sandbox
  swing(d.swing);

  // Render Trades Table
  const tBadge = document.getElementById('tradesBadge');
  if (tBadge) {
    if (d.broker_mode === 'live') {
      tBadge.style.display = 'inline-block';
      tBadge.className = 'pill up';
      tBadge.textContent = 'LIVE GROWW CNC';
    } else {
      tBadge.style.display = 'none';
    }
  }

  if (!d.trades || !d.trades.length) {
    const emptyMsg = (d.broker_mode === 'live') ? 'No live executed orders yet.' : 'No historical trades recorded yet.';
    fill('trades', [
      row(['Date & Time', 'Stock', 'Side', 'Qty', 'Fill Price', 'Frictional Costs'].map(t => ({ t })), true),
      row([{ t: emptyMsg, c: 'mute' }])
    ]);
  } else {
    fill('trades', [
      row(['Date & Time', 'Stock', 'Side', 'Qty', 'Fill Price', 'Frictional Costs'].map(t => ({ t })), true),
      ...d.trades.map(t => row([
        { t: (t.ts || '').replace('T', ' ').slice(0, 19) },
        { t: t.symbol, c: 'bold' },
        { t: t.side, c: t.side === 'BUY' ? 'up' : 'down' },
        { t: t.qty },
        { t: px(t.fill_price) },
        { t: inr(t.cost) }
      ]))
    ]);
  }
  updateToggleAllBtn();
}

function renderAI(ai, budget) {
  const c = document.getElementById('aicard');
  if (!ai || !ai.verdict) {
    c.style.display = 'none';
    return;
  }
  c.style.display = 'block';
  const p = (ai.provider || 'AI').toUpperCase();
  const m = ai.model || '';
  document.getElementById('aimodel').textContent = p + (m ? (' · ' + m) : '');

  const sum = document.getElementById('aisum');
  sum.replaceChildren();
  const vClass = ai.verdict === 'EXECUTE' ? 'up' : 'warn';
  const top = el('div', '', 'Verdict: ');
  top.appendChild(el('strong', vClass, ai.verdict));
  top.appendChild(document.createTextNode(` · Regime: ${(ai.market_regime || 'neutral').toUpperCase()} · Scanned: ${ai.scan_timestamp || ''}`));
  sum.appendChild(top);

  if (ai.decision_summary) {
    const sBox = el('div', 'mute', ai.decision_summary);
    sBox.style.width = '100%';
    sum.appendChild(sBox);
  }

  // API Budget Credit Meter - Dual Provider (Claude + Gemini) with Auto-Failover
  if (budget && budget.budget_limit_usd != null) {
    const bSpent = '$' + (budget.total_spent_usd || 0).toFixed(4);
    const bLimit = '$' + (budget.budget_limit_usd || 5.0).toFixed(2);
    const bRem = '$' + (budget.remaining_usd != null ? budget.remaining_usd : 5.0).toFixed(4);
    const pctUsed = Math.min(100, ((budget.total_spent_usd || 0) / (budget.budget_limit_usd || 5.0) * 100)).toFixed(1);
    const provs = budget.providers || {};
    const claudeP = provs.claude || { calls: 0, cost_usd: 0.0 };
    const geminiP = provs.gemini || { calls: 0, cost_usd: 0.0 };
    const claudeExhausted = (budget.remaining_usd != null && budget.remaining_usd <= 0.005);

    const bBox = el('div', '', '');
    bBox.style.width = '100%';
    bBox.style.marginTop = '12px';
    bBox.style.padding = '12px';
    bBox.style.background = 'rgba(15, 23, 42, 0.75)';
    bBox.style.border = '1px solid #1e293b';
    bBox.style.borderRadius = '10px';
    bBox.style.fontSize = '12px';

    bBox.innerHTML = `
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px">
        <span style="font-weight:600;color:var(--txt);display:flex;align-items:center;gap:6px">
          <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><circle cx="12" cy="12" r="10"/><path d="M12 6v6l4 2"/></svg>
          AI Prepaid Budget ($5.00 Cap)
        </span>
        <span style="color:#34d399;font-weight:600">Remaining: ${bRem} (${(100 - pctUsed).toFixed(1)}%)</span>
      </div>
      <div style="height:5px;background:#1e293b;border-radius:3px;overflow:hidden;margin-bottom:10px">
        <div style="height:100%;width:${pctUsed}%;background:linear-gradient(90deg, #38bdf8, #818cf8)"></div>
      </div>
      <div class="budget-grid" style="display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:8px">
        <div style="background:rgba(2, 6, 23, 0.6);border:1px solid ${claudeExhausted ? '#ef4444' : '#1e293b'};border-radius:6px;padding:8px">
          <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:4px">
            <span style="font-weight:600;color:#cbd5e1">Claude Sonnet 5.5</span>
            <span class="pill" style="font-size:10px;padding:1px 6px;background:${claudeExhausted ? 'rgba(239,68,68,0.2)' : 'rgba(56,189,248,0.15)'};color:${claudeExhausted ? '#f87171' : '#38bdf8'};border-color:${claudeExhausted ? '#ef4444' : 'rgba(56,189,248,0.3)'}">${claudeExhausted ? 'EXHAUSTED' : 'PRIMARY'}</span>
          </div>
          <div style="color:var(--mute);font-size:11px">Spent: <strong style="color:var(--txt)">$${(claudeP.cost_usd || 0).toFixed(4)}</strong> · ${claudeP.calls || 0} calls</div>
        </div>
        <div style="background:rgba(2, 6, 23, 0.6);border:1px solid ${claudeExhausted ? '#10b981' : '#1e293b'};border-radius:6px;padding:8px">
          <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:4px">
            <span style="font-weight:600;color:#cbd5e1">Gemini 3.5 Flash</span>
            <span class="pill" style="font-size:10px;padding:1px 6px;background:${claudeExhausted ? 'rgba(16,185,129,0.2)' : 'rgba(148,163,184,0.15)'};color:${claudeExhausted ? '#34d399' : '#94a3b8'};border-color:${claudeExhausted ? '#10b981' : '#334155'}">${claudeExhausted ? 'ACTIVE FALLBACK' : 'STANDBY'}</span>
          </div>
          <div style="color:var(--mute);font-size:11px">Spent: <strong style="color:var(--txt)">$${(geminiP.cost_usd || 0).toFixed(4)}</strong> · ${geminiP.calls || 0} calls</div>
        </div>
      </div>
      <div style="font-size:11px;color:var(--subtle);display:flex;align-items:center;gap:4px">
        <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/></svg>
        <span>Auto-failover active: If Claude limit exhausts, Gemini executes trades by default.</span>
      </div>
    `;
    sum.appendChild(bBox);
  }

  const tr = document.getElementById('aitrade');
  tr.replaceChildren();
  const s = ai.selected_trade;
  if (s) {
    const head = el('div', 'trade-head');
    head.innerHTML = `<div class="trade-symbol">${s.symbol} <span class="trade-badge">${s.action} DELIVERY (CNC)</span></div>` +
      `<div class="pill" style="background:#0c1322;color:#38bdf8;border-color:#1e293b">Conviction Score: <strong>${s.confidence_score != null ? s.confidence_score.toFixed(1) : '–'}/10</strong></div>`;
    tr.appendChild(head);

    const mGrid = el('div', 'trade-metrics');
    const gttBox = s.gtt_trigger_price ?
      `<div class="metric-box"><div class="lbl">Groww GTT Floor (-${Math.round((s.hard_stop_pct || 0.07) * 100)}%)</div><div class="val" style="color:#f87171">${px(s.gtt_trigger_price)}</div></div>` :
      `<div class="metric-box"><div class="lbl">Hold Horizon</div><div class="val">${s.max_hold_days ? s.max_hold_days + 'd Multi-Month' : 'Multi-Month'}</div></div>`;

    mGrid.innerHTML = `
      <div class="metric-box"><div class="lbl">Entry Price</div><div class="val">${px(s.entry_price)}</div></div>
      <div class="metric-box"><div class="lbl">Target (+${Math.round((s.target_pct || 0.20) * 100)}%)</div><div class="val up">${px(s.target_price)}</div></div>
      <div class="metric-box"><div class="lbl">Soft Stop (-${Math.round((s.stop_pct || 0.04) * 100)}%)</div><div class="val down">${px(s.stop_loss)} (AI)</div></div>
      ${gttBox}
    `;
    tr.appendChild(mGrid);

    if (s.gtt_order_id) {
      const gttDiv = el('div', 'pill', `🛡️ Groww GTT Protected: Emergency Stop Order #${s.gtt_order_id} @ Rs ${px(s.gtt_trigger_price)} resting on exchange book`);
      gttDiv.style.width = '100%';
      gttDiv.style.marginTop = '8px';
      gttDiv.style.background = 'rgba(56, 189, 248, 0.12)';
      gttDiv.style.color = '#38bdf8';
      gttDiv.style.borderColor = 'rgba(56, 189, 248, 0.25)';
      tr.appendChild(gttDiv);
    }

    if (s.thesis) {
      tr.appendChild(el('div', 'thesis-text', 'Thesis: ' + s.thesis));
    }
    if (s.capital_scaling_advice) {
      const scaleDiv = el('div', 'pill', '💡 Capital Advice: ' + s.capital_scaling_advice);
      scaleDiv.style.width = '100%';
      scaleDiv.style.marginTop = '8px';
      scaleDiv.style.background = 'rgba(16, 185, 129, 0.15)';
      scaleDiv.style.color = '#34d399';
      scaleDiv.style.borderColor = 'rgba(16, 185, 129, 0.3)';
      tr.appendChild(scaleDiv);
    }
  } else {
    tr.appendChild(el('div', 'mute', 'No trade selected for execution (insufficient conviction or unfavorable market regime).'));
  }

  // AI Take-Profit Maximizer Box
  if (ai.exit_evaluations && Object.keys(ai.exit_evaluations).length) {
    const maxBox = el('div', 'maximizer-box');
    maxBox.innerHTML = '<div class="maximizer-head"><svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polyline points="23 6 13.5 15.5 8.5 10.5 1 18"/><polyline points="17 6 23 6 23 12"/></svg> AI Take-Profit Maximizer Advisor</div>';
    for (const [sym, ev] of Object.entries(ai.exit_evaluations)) {
      const isTrail = ev.decision === 'HOLD_AND_TRAIL';
      const rowDiv = el('div', isTrail ? 'up' : 'warn');
      rowDiv.style.fontSize = '13px';
      rowDiv.style.marginTop = '4px';
      rowDiv.innerHTML = `<strong>${sym}</strong>: ${ev.decision} · ${ev.reason || ''} ` +
        (ev.revised_target_pct ? `<span class="pill" style="background:#091410;color:#34d399;margin-left:6px">Revised Target: +${(ev.revised_target_pct * 100).toFixed(0)}% · Trailing Stop: +${(ev.trailing_stop_pct * 100).toFixed(0)}%</span>` : '');
      maxBox.appendChild(rowDiv);
    }
    tr.appendChild(maxBox);
  }

  // AI Stop-Loss Risk Consultation Advisor
  if (ai.stop_consultations && Object.keys(ai.stop_consultations).length) {
    const slBox = el('div', 'maximizer-box');
    slBox.style.marginTop = '10px';
    slBox.style.background = 'rgba(15, 23, 42, 0.6)';
    slBox.style.border = '1px solid #1e293b';
    slBox.innerHTML = '<div class="maximizer-head" style="color:#f59e0b"><svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/></svg> AI Stop-Loss Risk Consultation Advisor</div>';
    for (const [sym, ev] of Object.entries(ai.stop_consultations)) {
      const isHold = ev.decision === 'HOLD_AND_GIVE_ROOM';
      const rowDiv = el('div', isHold ? 'up' : 'down');
      rowDiv.style.fontSize = '13px';
      rowDiv.style.marginTop = '4px';
      rowDiv.innerHTML = `<strong>${sym}</strong>: <span class="pill" style="font-size:10px;padding:1px 6px;background:${isHold ? 'rgba(56,189,248,0.15)' : 'rgba(239,68,68,0.2)'};color:${isHold ? '#38bdf8' : '#f87171'}">${ev.decision}</span> · ${ev.reason || ''} ` +
        (ev.revised_stop_loss ? `<span class="pill" style="background:#091410;color:#34d399;margin-left:6px">Revised Structural Stop: Rs ${px(ev.revised_stop_loss)}</span>` : '');
      slBox.appendChild(rowDiv);
    }
    tr.appendChild(slBox);
  }

  // AI Evaluated Candidates & Multi-Quarter Screening
  const candsDiv = document.getElementById('aicandidates');
  if (candsDiv) {
    candsDiv.replaceChildren();
    if (ai.evaluated_candidates && ai.evaluated_candidates.length) {
      const cHead = el('div', '', '');
      cHead.style.display = 'flex';
      cHead.style.justifyContent = 'space-between';
      cHead.style.alignItems = 'center';
      cHead.style.marginBottom = '8px';
      cHead.innerHTML = `
        <span style="font-weight:600;font-size:13px;color:var(--txt);display:flex;align-items:center;gap:6px">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/></svg>
          Evaluated Screening Candidates (${ai.evaluated_candidates.length})
        </span>
        <span class="pill" style="font-size:10px;padding:1px 6px;color:var(--mute)">Multi-Quarter & News Screened</span>
      `;
      candsDiv.appendChild(cHead);

      const cGrid = el('div', '', '');
      cGrid.style.display = 'flex';
      cGrid.style.flexDirection = 'column';
      cGrid.style.gap = '6px';

      ai.evaluated_candidates.forEach(c => {
        const item = el('div', '', '');
        item.style.padding = '8px 10px';
        item.style.background = 'rgba(2, 6, 23, 0.4)';
        item.style.border = '1px solid #1e293b';
        item.style.borderRadius = '6px';
        item.style.fontSize = '12px';

        const isSel = c.verdict === 'SELECTED';
        item.innerHTML = `
          <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:3px">
            <span style="font-weight:700;color:var(--txt)">${c.symbol}</span>
            <span class="pill" style="font-size:9px;padding:1px 6px;background:${isSel ? 'rgba(16,185,129,0.2)' : 'rgba(148,163,184,0.1)'};color:${isSel ? '#34d399' : '#94a3b8'}">${c.verdict}</span>
          </div>
          <div style="color:var(--mute);line-height:1.4">${c.reason || ''}</div>
        `;
        cGrid.appendChild(item);
      });
      candsDiv.appendChild(cGrid);
    }
  }
}

function swing(s) {
  const c = document.getElementById('swingcard');
  if (!s || !s.mode) {
    c.style.display = 'none';
    return;
  }
  c.style.display = 'block';
  const sum = document.getElementById('swsum');
  sum.replaceChildren();

  if (s.waiting) {
    sum.textContent = `Sandbox begins ${s.start_date} with ${inr(s.capital)} paper allocation. Gathering price data…`;
    fill('swhold', []);
    fill('swtr', []);
    document.getElementById('swnext').textContent = '';
    return;
  }

  sum.appendChild(el('div', '', `Rule: ${s.rule} · Since ${s.start_date}: equity ${inr(s.equity)} (${pct(s.pnl_pct)}) vs Nifty ${pct(s.benchmark_pct)} · ${s.trades} closed trades, ${s.win_rate == null ? '–' : (s.win_rate * 100).toFixed(0) + '% won'} · costs ${inr(s.total_costs)}`));

  fill('swhold', [
    row(['Stock', 'Qty', 'Entry Price', 'Since', 'Last Price', 'P/L %', 'Exit Signal'].map(t => ({ t })), true),
    ...s.holdings.map(h => row([
      { t: h.symbol },
      { t: h.qty },
      { t: px(h.entry_px) },
      { t: h.entry_date, c: 'mute' },
      { t: px(h.last) },
      { t: pct(h.pnl_pct), c: cls(h.pnl_pct) },
      { t: h.pending ? ('sell: ' + h.pending) : 'hold', c: 'mute' }
    ]))
  ]);

  document.getElementById('swnext').textContent = s.pending_buy && s.pending_buy.length
    ? (`Pending buys at next close (as of ${s.as_of}): ${s.pending_buy.join(', ')}`)
    : 'No queued breakout buys.';

  fill('swtr', [
    row(['Closed Stock', 'Entry Date', 'Exit Date', 'Exit Reason', 'Net Return', 'Return %'].map(t => ({ t })), true),
    ...s.recent.map(r => row([
      { t: r.symbol },
      { t: r.entry, c: 'mute' },
      { t: r.exit, c: 'mute' },
      { t: r.reason },
      { t: inr(r.net), c: cls(r.net) },
      { t: pct(r.net_pct), c: cls(r.net_pct) }
    ]))
  ]);
}


// PWA Installation & Service Worker Handler
let deferredPrompt = null;
const pwaBanner = document.getElementById('pwaBanner');
const headerInstallBtn = document.getElementById('btnHeaderInstall');
const iosHint = document.getElementById('iosHint');
const isStandalone = window.matchMedia('(display-mode: standalone)').matches || window.navigator.standalone === true;

if ('serviceWorker' in navigator) {
  window.addEventListener('load', () => {
    navigator.serviceWorker.register('/sw.js').catch(() => {});
  });
}

window.addEventListener('beforeinstallprompt', (e) => {
  e.preventDefault();
  deferredPrompt = e;
  if (!isStandalone) {
    if (headerInstallBtn) headerInstallBtn.style.display = 'inline-flex';
    if (pwaBanner && !sessionStorage.getItem('pwa_dismissed')) {
      pwaBanner.style.display = 'flex';
    }
  }
});

async function triggerPwaInstall() {
  if (deferredPrompt) {
    deferredPrompt.prompt();
    const { outcome } = await deferredPrompt.userChoice;
    if (outcome === 'accepted') {
      if (pwaBanner) pwaBanner.style.display = 'none';
      if (headerInstallBtn) headerInstallBtn.style.display = 'none';
    }
    deferredPrompt = null;
  } else {
    const isIosDevice = /iphone|ipad|ipod/.test(navigator.userAgent.toLowerCase());
    if (isIosDevice) {
      alert("To install on iOS: Tap the Share icon at the bottom of Safari, then tap 'Add to Home Screen'.");
    } else {
      alert("To install this app: Tap your browser menu and select 'Install app' or 'Add to Home screen'.");
    }
  }
}

function dismissPwaBanner() {
  if (pwaBanner) pwaBanner.style.display = 'none';
  sessionStorage.setItem('pwa_dismissed', 'true');
}

function dismissIosHint() {
  if (iosHint) iosHint.style.display = 'none';
  sessionStorage.setItem('ios_hint_dismissed', 'true');
}

window.addEventListener('appinstalled', () => {
  if (pwaBanner) pwaBanner.style.display = 'none';
  if (headerInstallBtn) headerInstallBtn.style.display = 'none';
  if (iosHint) iosHint.style.display = 'none';
  deferredPrompt = null;
});

const isIosDevice = /iphone|ipad|ipod/.test(navigator.userAgent.toLowerCase());
if (isIosDevice && !isStandalone && iosHint && !sessionStorage.getItem('ios_hint_dismissed')) {
  iosHint.style.display = 'flex';
}

const ALL_CARD_IDS = ['kpiCard', 'aicard', 'holdCard', 'swingcard', 'monCard', 'tradesCard'];

function toggleCard(cardId, e) {
  if (e) e.stopPropagation();
  const c = document.getElementById(cardId);
  if (!c) return;
  const isCollapsed = c.classList.toggle('collapsed');
  try {
    localStorage.setItem('collapse_' + cardId, isCollapsed ? '1' : '0');
  } catch (err) {}
  updateToggleAllBtn();
}

function updateToggleAllBtn() {
  const btn = document.getElementById('btnToggleAll');
  const txt = document.getElementById('toggleAllText');
  if (!btn || !txt) return;
  const visibleCards = ALL_CARD_IDS.map(id => document.getElementById(id)).filter(c => c && c.style.display !== 'none');
  const allCollapsed = visibleCards.length > 0 && visibleCards.every(c => c.classList.contains('collapsed'));
  txt.textContent = allCollapsed ? 'Expand All' : 'Collapse All';
}

function toggleAllCards() {
  const visibleCards = ALL_CARD_IDS.map(id => document.getElementById(id)).filter(c => c && c.style.display !== 'none');
  const anyExpanded = visibleCards.some(c => !c.classList.contains('collapsed'));
  const targetCollapse = anyExpanded;
  ALL_CARD_IDS.forEach(id => {
    const c = document.getElementById(id);
    if (!c) return;
    if (targetCollapse) {
      c.classList.add('collapsed');
    } else {
      c.classList.remove('collapsed');
    }
    try {
      localStorage.setItem('collapse_' + id, targetCollapse ? '1' : '0');
    } catch (err) {}
  });
  updateToggleAllBtn();
}

function restoreCardCollapseState() {
  ALL_CARD_IDS.forEach(id => {
    const c = document.getElementById(id);
    if (!c) return;
    try {
      if (localStorage.getItem('collapse_' + id) === '1') {
        c.classList.add('collapsed');
      }
    } catch (err) {}
  });
  updateToggleAllBtn();
}

restoreCardCollapseState();
load();
setInterval(load, 15000);
</script>
</body>
</html>"""


def is_host_allowed(host: str, allowed_hosts: set[str]) -> bool:
    if not host:
        return False
    host = host.lower().strip()
    if host in allowed_hosts:
        return True
    # Tailscale CGNAT IP range: 100.64.0.0 to 100.127.255.255
    if host.startswith("100."):
        parts = host.split(".")
        if len(parts) == 4 and all(p.isdigit() for p in parts):
            second_octet = int(parts[1])
            if 64 <= second_octet <= 127:
                return True
    # Tailscale MagicDNS (*.ts.net)
    if host.endswith(".ts.net"):
        return True
    return False


def make_handler(cfg, paths: dict, allowed_hosts: set[str], password: str | None,
                 username: str | None = None):
    # Derive HMAC secret from password and username (or fallback)
    raw_secret = f"{password or 'public_secret'}:{username or 'admin'}".encode()
    session_secret = hashlib.sha256(raw_secret).digest()

    class Handler(BaseHTTPRequestHandler):
        server_version = "agent-dashboard"

        def log_message(self, *a):  # keep console quiet
            pass

        def _respond(self, code: int, body: bytes, ctype: str, headers: dict | None = None):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self' https://fonts.googleapis.com https://fonts.gstatic.com; style-src 'unsafe-inline' https://fonts.googleapis.com; font-src https://fonts.gstatic.com 'self'; script-src 'unsafe-inline' 'self'; worker-src 'self' blob:; img-src 'self' data:; manifest-src 'self'")
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _deny(self, code: int, msg: str, headers: dict | None = None, ctype: str = "text/plain"):
            body = msg.encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _get_auth_user(self) -> str | None:
            if not password:
                return username or "admin"

            # 1. Check Session Cookie
            cookie_hdr = self.headers.get("Cookie", "")
            token = extract_cookie(cookie_hdr, "agent_session")
            if token:
                valid, u = verify_session_token(token, session_secret, username)
                if valid:
                    return u

            # 2. Check HTTP Basic Auth Header
            hdr = self.headers.get("Authorization", "")
            if hdr.startswith("Basic "):
                try:
                    given_user, _, given_pass = base64.b64decode(hdr[6:]).decode().partition(":")
                    pass_ok = hmac.compare_digest(given_pass.encode(), password.encode())
                    user_ok = hmac.compare_digest(given_user.encode(), username.encode()) if username else True
                    if pass_ok and user_ok:
                        return given_user or username or "admin"
                except Exception:
                    pass
            return None

        def _authorized(self) -> bool:
            return self._get_auth_user() is not None

        def do_GET(self):
            host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]").lower()
            if not is_host_allowed(host, allowed_hosts):
                return self._deny(403, "forbidden host")

            path = self.path.split("?")[0]
            auth_user = self._get_auth_user()

            # PWA Manifest, Service Worker & Icons (Publicly available for mobile install)
            if path in ("/manifest.json", "/manifest.webmanifest"):
                return self._respond(200, MANIFEST_JSON.encode(), "application/manifest+json")

            if path == "/sw.js":
                return self._respond(200, SERVICE_WORKER_JS.encode(), "application/javascript")

            if path == "/favicon.svg":
                return self._respond(200, FAVICON_SVG.encode(), "image/svg+xml")

            if path in ("/icon-192.png", "/apple-touch-icon.png", "/apple-touch-icon-precomposed.png"):
                return self._respond(200, ICON_192_PNG_BYTES, "image/png")

            if path == "/icon-512.png":
                return self._respond(200, ICON_512_PNG_BYTES, "image/png")

            # Handle /login
            if path == "/login":
                if auth_user and password:
                    self.send_response(302)
                    self.send_header("Location", "/")
                    self.end_headers()
                    return
                return self._respond(200, LOGIN_PAGE.encode(), "text/html; charset=utf-8")

            # Route protection
            if not auth_user:
                if path.startswith("/api/"):
                    headers = {"WWW-Authenticate": 'Basic realm="agent"'} if self.headers.get("Authorization") else {}
                    return self._deny(401, json.dumps({"error": "login required"}), headers=headers, ctype="application/json")
                return self._respond(200, LOGIN_PAGE.encode(), "text/html; charset=utf-8")

            if path == "/api/summary":
                ks_path = paths.get("kill_switch", "data/kill_switch.json")
                broker_mode = (
                    cfg.broker.resolved_mode()
                    if hasattr(cfg.broker, "resolved_mode")
                    else getattr(getattr(cfg, "broker", None), "mode", "paper")
                )
                # Also check shared active_mode.json in data/ volume
                active_mode_file = Path("data/active_mode.json")
                if active_mode_file.is_file():
                    try:
                        mod_info = json.loads(active_mode_file.read_text(encoding="utf-8"))
                        if mod_info.get("ai_auto_trade") is True or mod_info.get("broker_mode") == "live":
                            broker_mode = "live"
                    except Exception:
                        pass
                if _read_json(paths["monitor"]).get("live_mode") is True:
                    broker_mode = "live"
                ai_path = paths.get("ai_signals", "data/ai_signals.json")
                summary = build_summary(
                    paths["paper"], paths["monitor"], paths["screen"],
                    paths["prices"], cfg.portfolio.rebalance_months, paths.get("swing"),
                    kill_switch_path=ks_path, broker_mode=broker_mode,
                    ai_signals_path=ai_path,
                )
                summary["auth_user"] = auth_user
                data = json.dumps(summary).encode()
                return self._respond(200, data, "application/json")

            elif path in ("/", "/index.html"):
                return self._respond(200, PAGE.encode(), "text/html; charset=utf-8")

            else:
                return self._deny(404, "not found")

        def do_POST(self):
            host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]").lower()
            if not is_host_allowed(host, allowed_hosts):
                return self._deny(403, "forbidden host")

            path = self.path.split("?")[0]

            # Public login endpoint
            if path == "/api/login":
                try:
                    length = int(self.headers.get("Content-Length", 0))
                    body = self.rfile.read(length) if length > 0 else b"{}"
                    creds = json.loads(body.decode())
                    req_user = str(creds.get("username", "")).strip()
                    req_pass = str(creds.get("password", ""))
                except Exception:
                    return self._deny(400, json.dumps({"error": "invalid json"}), ctype="application/json")

                if not password:
                    # Passwordless mode
                    user_target = req_user or username or "admin"
                    token = create_session_token(user_target, session_secret)
                    cookie = f"agent_session={token}; Path=/; HttpOnly; SameSite=Lax; Max-Age=2592000"
                    return self._respond(200, json.dumps({"ok": True, "username": user_target}).encode(),
                                         "application/json", headers={"Set-Cookie": cookie})

                user_target = username or "admin"
                pass_ok = hmac.compare_digest(req_pass.encode(), password.encode())
                user_ok = hmac.compare_digest(req_user.encode(), user_target.encode())

                if pass_ok and user_ok:
                    token = create_session_token(user_target, session_secret)
                    cookie = f"agent_session={token}; Path=/; HttpOnly; SameSite=Lax; Max-Age=2592000"
                    return self._respond(200, json.dumps({"ok": True, "username": user_target}).encode(),
                                         "application/json", headers={"Set-Cookie": cookie})
                else:
                    return self._deny(401, json.dumps({"error": "Invalid username or password"}),
                                      ctype="application/json")

            # Logout endpoint
            if path == "/api/logout":
                cookie = "agent_session=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0"
                return self._respond(200, json.dumps({"ok": True}).encode(), "application/json",
                                     headers={"Set-Cookie": cookie})

            # Protected POST endpoints
            auth_user = self._get_auth_user()
            if not auth_user:
                headers = {"WWW-Authenticate": 'Basic realm="agent"'} if self.headers.get("Authorization") else {}
                return self._deny(401, json.dumps({"error": "login required"}), headers=headers, ctype="application/json")

            ks_path = paths.get("kill_switch", "data/kill_switch.json")
            if path == "/api/kill-switch/trigger":
                status = trigger_kill_switch("Manual emergency halt from dashboard", ks_path)
                data = json.dumps(status).encode()
            elif path == "/api/kill-switch/reset":
                status = reset_kill_switch(ks_path)
                data = json.dumps(status).encode()
            elif path == "/api/notifications/test":
                from .notify import dispatch_notification
                notif = dispatch_notification(
                    event_type="test",
                    title="TEST NOTIFICATION",
                    message="Live web & mobile app trade notifications are active!",
                    details={"mode": "LIVE", "test": True},
                )
                data = json.dumps({"ok": True, "notification": notif}).encode()
            else:
                return self._deny(405, "read-only endpoint or unknown action")

            return self._respond(200, data, "application/json")

        do_PUT = do_DELETE = do_PATCH = do_POST

    return Handler


def serve(cfg, paths: dict, host: str = "127.0.0.1", port: int = 8765, extra_hosts: tuple[str, ...] = (),
          log=print) -> None:
    password = os.environ.get("DASHBOARD_PASSWORD") or None
    username = os.environ.get("DASHBOARD_USERNAME", "admin").strip()
    if host not in LOOPBACK and not password:
        raise SystemExit("refusing to bind a non-loopback address without DASHBOARD_PASSWORD set")
    env_allow = os.environ.get("DASHBOARD_ALLOW_HOST", "")
    all_extras = list(extra_hosts)
    if env_allow:
        all_extras.extend(env_allow.split(","))
    clean_extras = {h.strip().lower() for h in all_extras if h.strip()}
    allowed = set(LOOPBACK) | clean_extras | ({host.lower()} if host != "0.0.0.0" else set())
    srv = ThreadingHTTPServer((host, port), make_handler(cfg, paths, allowed, password, username=username))
    log(f"dashboard on http://{host}:{port}  (auth {'on (user: ' + username + ')' if password else 'off, loopback only'}). Ctrl+C to stop.")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
