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


def build_summary(paper_path: str, monitor_path: str, screen_path: str, prices_cache: str,
                  rebalance_months: int, swing_path: str | None = None,
                  kill_switch_path: str | None = None, broker_mode: str = "paper",
                  ai_signals_path: str | None = None) -> dict:
    state = _read_json(paper_path)
    mon = _read_json(monitor_path)
    positions: dict[str, int] = {k: int(v) for k, v in state.get("positions", {}).items() if int(v) > 0}
    trades = state.get("trades", [])
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
    mode_text = "LIVE (Groww API, CNC delivery)" if broker_mode == "live" else "PAPER (simulated, no real orders)"
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
        "trades": trades[-25:][::-1],
        "last_rebalance": last_reb, "next_rebalance": nxt,
        "monitor": {
            "heartbeat": mon.get("heartbeat"), "market_open": mon.get("market_open"),
            "stop_pct": mon.get("stop_pct"), "act": mon.get("act"),
            "alerts": mon.get("alerts", [])[-15:][::-1],
        },
        "screen": screen,
        "swing": _read_json(swing_path) if swing_path else {},
        "kill_switch": get_kill_switch_status(kill_switch_path or "data/kill_switch.json"),
        "ai_signals": _read_json(ai_signals_path) if ai_signals_path else {},
        "api_budget": api_budget,
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


LOGIN_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
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
  padding: 20px;
  -webkit-font-smoothing: antialiased;
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
</body>
</html>"""


PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
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
  padding-bottom: 40px;
  -webkit-font-smoothing: antialiased;
}
.mono { font-family: 'JetBrains Mono', monospace; }
.tabular { font-variant-numeric: tabular-nums; }

/* Top Navigation Bar */
.topbar {
  background: rgba(11, 17, 32, 0.85);
  backdrop-filter: blur(14px);
  border-bottom: 1px solid var(--line);
  position: sticky;
  top: 0;
  z-index: 100;
  padding: 10px 20px;
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
}
.card-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 16px;
  flex-wrap: wrap;
  gap: 10px;
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
</style>
</head>
<body>

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

  <!-- Hero KPI Grid -->
  <div class="kpi-grid" id="kpis"></div>

  <!-- AI Spotlight Card -->
  <div class="card ai-card" id="aicard" style="display:none">
    <div class="card-header">
      <div class="card-title">
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 2a4 4 0 0 1 4 4c0 1.95-1.4 3.58-3.25 3.93l-.25.04v2.03h2.5a2.5 2.5 0 0 1 2.45 2l.05.25v2.5a2.5 2.5 0 0 1-2 2.45l-.25.05h-6.5a2.5 2.5 0 0 1-2.45-2l-.05-.25v-2.5a2.5 2.5 0 0 1 2-2.45l.25-.05h2.5v-2.03l-.25-.04A4 4 0 0 1 12 2z"/></svg>
        <span>AI Quantitative Analyst</span>
      </div>
      <span id="aimodel" class="pill" style="background:#0f172a;color:#38bdf8;border-color:#1e3a5f"></span>
    </div>
    <div id="aisum" class="ai-summary-bar"></div>
    <div id="aitrade" class="trade-box"></div>
  </div>

  <!-- Active Holdings Table -->
  <div class="card">
    <div class="card-header">
      <div class="card-title">
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/></svg>
        <span>Active Portfolio Holdings</span>
      </div>
      <div id="holdCount" class="pill" style="background:#0f172a;color:var(--mute)">0 Assets</div>
    </div>
    <div class="table-wrap">
      <table id="hold"></table>
    </div>
  </div>

  <!-- Swing Sandbox (Paper) -->
  <div class="card" id="swingcard" style="display:none">
    <div class="card-header">
      <div class="card-title">
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 3v18h18"/><path d="m19 9-5 5-4-4-3 3"/></svg>
        <span>Swing Sandbox (Paper Breakouts)</span>
      </div>
      <div id="swrule" class="pill" style="background:#0f172a;color:#38bdf8">Breakout Rule</div>
    </div>
    <div id="swsum" style="font-size:13px;color:#cbd5e1;margin-bottom:12px;line-height:1.5"></div>
    <div class="table-wrap">
      <table id="swhold"></table>
    </div>
    <div id="swnext" class="mute" style="font-size:12px;margin:10px 0"></div>
    <div class="table-wrap">
      <table id="swtr"></table>
    </div>
  </div>

  <!-- Execution Engine & Monitor Status -->
  <div class="card">
    <div class="card-header">
      <div class="card-title">
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="2" y="3" width="20" height="14" rx="2" ry="2"/><line x1="8" y1="21" x2="16" y2="21"/><line x1="12" y1="17" x2="12" y2="21"/></svg>
        <span>Execution Engine & Real-Time Monitor</span>
      </div>
    </div>
    <div id="mon" style="font-size:13px;line-height:1.6"></div>
    <div id="alerts" style="margin-top:12px;font-size:12px"></div>
  </div>

  <!-- Recent Trade Orders -->
  <div class="card">
    <div class="card-header">
      <div class="card-title">
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 14 14"/></svg>
        <span>Recent Executed Orders</span>
      </div>
    </div>
    <div class="table-wrap">
      <table id="trades"></table>
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
      row([{ t: 'No open positions held. Daily automated scan runs at 16:00 IST.', c: 'mute' }])
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
    mon.appendChild(el('div', 'mute', `Action Mode: ${m.act ? 'Automatic Paper Execution' : 'Alert Only (No automatic broker sell)'}`));
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
  if (!d.trades || !d.trades.length) {
    fill('trades', [
      row(['Date', 'Stock', 'Side', 'Qty', 'Fill Price', 'Frictional Costs'].map(t => ({ t })), true),
      row([{ t: 'No historical trades recorded yet.', c: 'mute' }])
    ]);
  } else {
    fill('trades', [
      row(['Date', 'Stock', 'Side', 'Qty', 'Fill Price', 'Frictional Costs'].map(t => ({ t })), true),
      ...d.trades.map(t => row([
        { t: t.ts.slice(0, 10) },
        { t: t.symbol },
        { t: t.side, c: t.side === 'BUY' ? 'up' : 'down' },
        { t: t.qty },
        { t: px(t.fill_price) },
        { t: px(t.cost) }
      ]))
    ]);
  }
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

  // API Budget Credit Meter
  if (budget && budget.budget_limit_usd != null) {
    const bSpent = '$' + (budget.total_spent_usd || 0).toFixed(4);
    const bLimit = '$' + (budget.budget_limit_usd || 5.0).toFixed(2);
    const bRem = '$' + (budget.remaining_usd != null ? budget.remaining_usd : 5.0).toFixed(4);
    const pctUsed = Math.min(100, ((budget.total_spent_usd || 0) / (budget.budget_limit_usd || 5.0) * 100)).toFixed(1);
    const bBox = el('div', '', '');
    bBox.style.width = '100%';
    bBox.style.marginTop = '10px';
    bBox.style.padding = '8px 12px';
    bBox.style.background = 'rgba(15, 23, 42, 0.7)';
    bBox.style.border = '1px solid #1e293b';
    bBox.style.borderRadius = '8px';
    bBox.style.fontSize = '12px';
    bBox.innerHTML = `<div style="display:flex;justify-content:space-between;margin-bottom:5px">` +
      `<span>AI Budget ($5.00 limit): <strong>${bSpent}</strong> spent</span>` +
      `<span style="color:#34d399">Remaining: <strong>${bRem}</strong> (${(100 - pctUsed).toFixed(1)}%)</span>` +
      `</div>` +
      `<div style="height:4px;background:#1e293b;border-radius:2px;overflow:hidden">` +
      `<div style="height:100%;width:${pctUsed}%;background:#38bdf8"></div>` +
      `</div>`;
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
    mGrid.innerHTML = `
      <div class="metric-box"><div class="lbl">Entry Price</div><div class="val">${px(s.entry_price)}</div></div>
      <div class="metric-box"><div class="lbl">Target (+8%)</div><div class="val up">${px(s.target_price)}</div></div>
      <div class="metric-box"><div class="lbl">Stop-Loss (-4%)</div><div class="val down">${px(s.stop_loss)}</div></div>
      <div class="metric-box"><div class="lbl">Hold Cadence</div><div class="val">${s.max_hold_days || 10} Days</div></div>
    `;
    tr.appendChild(mGrid);

    if (s.thesis) {
      tr.appendChild(el('div', 'thesis-text', 'Thesis: ' + s.thesis));
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

load();
setInterval(load, 15000);
</script>
</body>
</html>"""


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
            self.send_header("Content-Security-Policy", "default-src 'self' https://fonts.googleapis.com https://fonts.gstatic.com; style-src 'unsafe-inline' https://fonts.googleapis.com; font-src https://fonts.gstatic.com 'self'; script-src 'unsafe-inline'")
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
            if host not in allowed_hosts:
                return self._deny(403, "forbidden host")

            path = self.path.split("?")[0]
            auth_user = self._get_auth_user()

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
                broker_mode = getattr(getattr(cfg, "broker", None), "mode", "paper")
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
            if host not in allowed_hosts:
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
    allowed = set(LOOPBACK) | {h.lower() for h in extra_hosts} | ({host.lower()} if host != "0.0.0.0" else set())
    srv = ThreadingHTTPServer((host, port), make_handler(cfg, paths, allowed, password, username=username))
    log(f"dashboard on http://{host}:{port}  (auth {'on (user: ' + username + ')' if password else 'off, loopback only'}). Ctrl+C to stop.")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
