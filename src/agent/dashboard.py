"""Read-only local dashboard. Standard library only, no mutating endpoints.

Security defaults: binds 127.0.0.1, rejects unexpected Host headers (DNS-rebinding defence),
and refuses to bind a non-loopback address unless DASHBOARD_PASSWORD is set (HTTP Basic auth).
For remote use put it behind a VPN (Tailscale/WireGuard); never expose the port to the internet.
"""
from __future__ import annotations

import base64
import hmac
import json
import os
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
    }


PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Shariah Trading Agent</title>
<style>
:root{--bg:#f6f7f9;--card:#fff;--ink:#14181f;--mute:#667085;--line:#e4e7ec;--up:#0a7d3e;--down:#c0262d;--warn:#b54708;--bar:#14181f;}
@media (prefers-color-scheme:dark){:root{--bg:#0e1116;--card:#171b22;--ink:#e8ebf0;--mute:#8a93a3;--line:#262c36;--up:#3ccf7a;--down:#ff6b6b;--warn:#f5a524;--bar:#e8ebf0;}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.45 system-ui,Segoe UI,Roboto,sans-serif}
.wrap{max-width:1080px;margin:0 auto;padding:16px}
.banner{background:var(--bar);color:var(--bg);padding:8px 16px;font-weight:600;letter-spacing:.02em;font-size:13px}
h1{font-size:18px;margin:16px 0 4px}h2{font-size:14px;margin:0 0 10px;color:var(--mute);font-weight:600;text-transform:uppercase;letter-spacing:.05em}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px;margin:12px 0}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px;margin-bottom:12px;overflow-x:auto}
.kpi .v{font-size:22px;font-weight:650}.kpi .l{color:var(--mute);font-size:12px}
table{border-collapse:collapse;width:100%;min-width:560px}th,td{text-align:right;padding:7px 8px;border-bottom:1px solid var(--line);white-space:nowrap}
th:first-child,td:first-child{text-align:left}th{color:var(--mute);font-weight:600;font-size:12px}
.up{color:var(--up)}.down{color:var(--down)}.warn{color:var(--warn)}.mute{color:var(--mute)}
.pill{display:inline-block;padding:2px 8px;border-radius:99px;border:1px solid var(--line);font-size:12px}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:6px;background:var(--mute)}
</style></head><body>
<div class="banner" id="mode">PAPER</div>
<div class="wrap">
<div style="display:flex;justify-content:space-between;align-items:flex-start;margin:16px 0 4px">
  <div>
    <h1 style="margin:0 0 4px">Shariah Trading Agent</h1>
    <div class="mute" id="sub">loading…</div>
  </div>
  <div>
    <button id="btn-kill" onclick="triggerKill()" style="background:#c0262d;color:#fff;border:none;border-radius:6px;padding:6px 14px;font-weight:600;font-size:12px;cursor:pointer">EMERGENCY KILL SWITCH</button>
  </div>
</div>
<div id="killbanner" style="display:none;background:#fde8e8;border:1px solid #f87171;color:#9b1c1c;padding:12px 16px;border-radius:8px;margin:12px 0;font-weight:600">
  <div style="display:flex;justify-content:space-between;align-items:center">
    <div>⚠️ EMERGENCY HALT ACTIVE: All trading is stopped! <span id="killreason" style="font-weight:normal;font-size:13px"></span></div>
    <button onclick="resetKill()" style="background:#0a7d3e;color:#fff;border:none;border-radius:4px;padding:6px 12px;font-weight:600;font-size:12px;cursor:pointer">Resume Trading</button>
  </div>
</div>
<div class="grid" id="kpis"></div>
<div class="card"><h2>Monitor</h2><div id="mon"></div></div>
<div class="card"><h2>Holdings</h2><table id="hold"></table></div>
<div class="card" id="swingcard" style="display:none"><h2>Swing sandbox (paper, experimental)</h2><div id="swsum"></div>
<table id="swhold"></table><div class="mute" style="margin-top:8px" id="swnext"></div><table id="swtr"></table></div>
<div class="card" id="aicard" style="display:none">
  <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
    <h2 style="margin:0">AI Trading Brain</h2>
    <span id="aimodel" class="pill"></span>
  </div>
  <div id="aisum"></div>
  <div id="aitrade" style="margin-top:10px;padding:10px;border-radius:6px;background:var(--bg)"></div>
</div>
<div class="card"><h2>Alerts</h2><div id="alerts" class="mute">none</div></div>
<div class="card"><h2>Recent trades</h2><table id="trades"></table></div>
<div class="mute" style="font-size:12px;margin:8px 0 24px">Read-only view. Simulated trades only. Not investment advice.</div>
</div>
<script>
const inr=n=>n==null?'–':'₹'+Number(n).toLocaleString('en-IN',{maximumFractionDigits:0});
const px=n=>n==null?'–':Number(n).toLocaleString('en-IN',{minimumFractionDigits:2,maximumFractionDigits:2});
const pct=n=>n==null?'–':(n>=0?'+':'')+(n*100).toFixed(1)+'%';
const cls=n=>n==null?'':(n>=0?'up':'down');
function el(t,c,x){const e=document.createElement(t);if(c)e.className=c;if(x!=null)e.textContent=x;return e}
function row(cells,head){const tr=el('tr');cells.forEach(c=>{const td=el(head?'th':'td',c.c||'',c.t);tr.appendChild(td)});return tr}
function fill(id,rows){const t=document.getElementById(id);t.replaceChildren(...rows)}
async function triggerKill(){
 if(!confirm('EMERGENCY: Halt all trading and activate Kill Switch?'))return;
 try{const r=await fetch('/api/kill-switch/trigger',{method:'POST'});if(r.ok)load();else alert('Failed: HTTP '+r.status)}catch(e){alert('Network error: '+e)}
}
async function resetKill(){
 if(!confirm('RESUME: Disengage Kill Switch and restore trading?'))return;
 try{const r=await fetch('/api/kill-switch/reset',{method:'POST'});if(r.ok)load();else alert('Failed: HTTP '+r.status)}catch(e){alert('Network error: '+e)}
}
async function load(){
 let d;try{const r=await fetch('/api/summary',{cache:'no-store'});d=await r.json()}catch(e){document.getElementById('sub').textContent='cannot reach server';return}
 const ks=d.kill_switch||{};
 const kb=document.getElementById('killbanner');
 const bk=document.getElementById('btn-kill');
 if(ks.active){
  kb.style.display='block';
  bk.style.display='none';
  document.getElementById('killreason').textContent=ks.reason?('('+ks.reason+')'):'';
  document.getElementById('mode').textContent='EMERGENCY HALT (TRADING KILLED)';
  document.getElementById('mode').style.background='var(--down)';
 } else {
  kb.style.display='none';
  bk.style.display='inline-block';
  document.getElementById('mode').textContent=d.mode;
  document.getElementById('mode').style.background='var(--bar)';
 }
 document.getElementById('sub').textContent='Updated '+new Date().toLocaleTimeString()+(d.screen.total?` · ${d.screen.compliant} of ${d.screen.total} stocks pass the Shariah screen`:'');
 const k=document.getElementById('kpis');k.replaceChildren();
 [['Equity',inr(d.equity),''],['Cash',inr(d.cash),''],['Deposited',inr(d.deposited),''],
  ['Profit / loss',d.pnl==null?'–':inr(d.pnl)+' ('+pct(d.pnl_pct)+')',cls(d.pnl)],
  ['Costs paid',inr(d.total_costs),''],['Next rebalance',d.next_rebalance||'due now','']].forEach(([l,v,c])=>{
  const b=el('div','card kpi');b.appendChild(el('div','v '+c,v));b.appendChild(el('div','l',l));k.appendChild(b)});
 const m=d.monitor,mon=document.getElementById('mon');mon.replaceChildren();
 const st=el('div');const dot=el('span','dot');dot.style.background=m.heartbeat?(m.market_open?'var(--up)':'var(--warn)'):'var(--mute)';st.appendChild(dot);
 st.appendChild(document.createTextNode(m.heartbeat?(m.market_open?'Market open, watching':'Market closed (NSE Mon–Fri 09:15–15:30 IST)'):'Monitor not running'));mon.appendChild(st);
 if(m.heartbeat){mon.appendChild(el('div','mute','Last check '+m.heartbeat+' · stop-loss '+(m.stop_pct?(m.stop_pct*100)+'% trailing'+(m.act?' (auto paper sell)':' (alert only)'):'off')))}
 fill('hold',[row(['Stock','Qty','Avg cost','Last','Value','P/L','P/L %','vs peak','Price'].map(t=>({t})),true),
  ...d.holdings.map(h=>row([{t:h.symbol},{t:h.qty},{t:px(h.avg_cost)},{t:px(h.last)},{t:inr(h.value)},{t:inr(h.pnl),c:cls(h.pnl)},{t:pct(h.pnl_pct),c:cls(h.pnl_pct)},{t:pct(h.from_peak_pct),c:cls(h.from_peak_pct)},{t:h.price_source,c:'mute'}]))]);
 const a=document.getElementById('alerts');a.replaceChildren();
 if(!m.alerts.length)a.textContent='none';else m.alerts.forEach(x=>{const p=el('div','warn',x.ts.replace('T',' ')+'  '+x.kind+'  '+x.symbol+'  '+x.detail);a.appendChild(p)});
 swing(d.swing);
 renderAI(d.ai_signals);
 fill('trades',[row(['Date','Stock','Side','Qty','Price','Fees'].map(t=>({t})),true),
  ...d.trades.map(t=>row([{t:t.ts.slice(0,10)},{t:t.symbol},{t:t.side,c:t.side==='BUY'?'up':'down'},{t:t.qty},{t:px(t.fill_price)},{t:px(t.cost)}]))]);
}
function swing(s){
 const c=document.getElementById('swingcard');if(!s||!s.mode){c.style.display='none';return}c.style.display='';
 const sum=document.getElementById('swsum');sum.replaceChildren();
 if(s.waiting){sum.textContent='Starts '+s.start_date+' with '+inr(s.capital)+' of paper money. No data yet.';fill('swhold',[]);fill('swtr',[]);document.getElementById('swnext').textContent='';return}
 sum.appendChild(el('div','',`Rule: ${s.rule}. Since ${s.start_date}: equity ${inr(s.equity)} (${pct(s.pnl_pct)}) vs Nifty ${pct(s.benchmark_pct)} · ${s.trades} closed trades, ${s.win_rate==null?'–':(s.win_rate*100).toFixed(0)+'% won' } · costs ${inr(s.total_costs)}`));
 fill('swhold',[row(['Open','Qty','Entry','Since','Last','P/L %','Next close'].map(t=>({t})),true),
  ...s.holdings.map(h=>row([{t:h.symbol},{t:h.qty},{t:px(h.entry_px)},{t:h.entry_date,c:'mute'},{t:px(h.last)},{t:pct(h.pnl_pct),c:cls(h.pnl_pct)},{t:h.pending?('sell: '+h.pending):'hold',c:'mute'}]))]);
 document.getElementById('swnext').textContent=s.pending_buy.length?('Buys at next close (as of '+s.as_of+'): '+s.pending_buy.join(', ')):'No buys queued.';
 fill('swtr',[row(['Closed','Entry','Exit','Why','Net','Net %'].map(t=>({t})),true),
  ...s.recent.map(r=>row([{t:r.symbol},{t:r.entry,c:'mute'},{t:r.exit,c:'mute'},{t:r.reason},{t:inr(r.net),c:cls(r.net)},{t:pct(r.net_pct),c:cls(r.net_pct)}]))]);
}
function renderAI(ai){
 const c=document.getElementById('aicard');if(!ai||!ai.verdict){c.style.display='none';return}c.style.display='';
 const p=(ai.provider||'AI').toUpperCase();const m=ai.model||'';
 document.getElementById('aimodel').textContent=p+(m?(' · '+m):'');
 const sum=document.getElementById('aisum');sum.replaceChildren();
 const vClass=ai.verdict==='EXECUTE'?'up':'warn';
 const top=el('div','',`Verdict: `);
 top.appendChild(el('strong',vClass,ai.verdict));
 top.appendChild(document.createTextNode(` · Regime: ${(ai.market_regime||'neutral').toUpperCase()} · Scanned: ${ai.scan_timestamp||''}`));
 sum.appendChild(top);
 if(ai.decision_summary)sum.appendChild(el('div','mute',ai.decision_summary));
 const tr=document.getElementById('aitrade');tr.replaceChildren();
 const s=ai.selected_trade;
 if(s){
  tr.appendChild(el('div','',`Recommended Trade: ${s.symbol} (${s.action}) · Entry: ${px(s.entry_price)} · Target: ${px(s.target_price)} (+8%) · Stop: ${px(s.stop_loss)} (-4%) · Conviction: ${s.confidence_score!=null?s.confidence_score.toFixed(1):'–'}/10`));
  if(s.thesis)tr.appendChild(el('div','mute','Thesis: '+s.thesis));
 }else{
  tr.appendChild(el('div','mute','No trade executed (insufficient conviction or unfavorable market regime).'));
 }
 if(ai.exit_evaluations&&Object.keys(ai.exit_evaluations).length){
  const evBox=el('div','','');evBox.style.marginTop='8px';evBox.style.padding='8px';evBox.style.border='1px solid var(--line)';evBox.style.borderRadius='6px';
  evBox.appendChild(el('strong','','AI Take-Profit Maximizer: '));
  for(const [sym,ev] of Object.entries(ai.exit_evaluations)){
    const line=el('div',ev.decision==='HOLD_AND_TRAIL'?'up':'warn',
      `${sym}: ${ev.decision} · ${ev.reason||''} ${ev.revised_target_pct?('(Target: +'+(ev.revised_target_pct*100).toFixed(0)+'%, Stop: +'+(ev.trailing_stop_pct*100).toFixed(0)+'%)'):''}`);
    evBox.appendChild(line);
  }
  tr.appendChild(evBox);
 }
}
load();setInterval(load,15000);
</script></body></html>"""


def make_handler(cfg, paths: dict, allowed_hosts: set[str], password: str | None):
    class Handler(BaseHTTPRequestHandler):
        server_version = "agent-dashboard"

        def log_message(self, *a):  # keep the console quiet
            pass

        def _deny(self, code: int, msg: str, headers: dict | None = None):
            body = msg.encode()
            self.send_response(code)
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _authorized(self) -> bool:
            if not password:
                return True
            hdr = self.headers.get("Authorization", "")
            if not hdr.startswith("Basic "):
                return False
            try:
                _, _, given = base64.b64decode(hdr[6:]).decode().partition(":")
            except Exception:
                return False
            return hmac.compare_digest(given.encode(), password.encode())

        def do_GET(self):
            host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]").lower()
            if host not in allowed_hosts:
                return self._deny(403, "forbidden host")
            if not self._authorized():
                return self._deny(401, "login required", {"WWW-Authenticate": 'Basic realm="agent"'})
            path = self.path.split("?")[0]
            if path == "/api/summary":
                ks_path = paths.get("kill_switch", "data/kill_switch.json")
                broker_mode = getattr(getattr(cfg, "broker", None), "mode", "paper")
                ai_path = paths.get("ai_signals", "data/ai_signals.json")
                data = json.dumps(build_summary(
                    paths["paper"], paths["monitor"], paths["screen"],
                    paths["prices"], cfg.portfolio.rebalance_months, paths.get("swing"),
                    kill_switch_path=ks_path, broker_mode=broker_mode,
                    ai_signals_path=ai_path,
                )).encode()
                ctype = "application/json"
            elif path in ("/", "/index.html"):
                data, ctype = PAGE.encode(), "text/html; charset=utf-8"
            else:
                return self._deny(404, "not found")
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'")
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):
            host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]").lower()
            if host not in allowed_hosts:
                return self._deny(403, "forbidden host")
            if not self._authorized():
                return self._deny(401, "login required", {"WWW-Authenticate": 'Basic realm="agent"'})
            path = self.path.split("?")[0]
            ks_path = paths.get("kill_switch", "data/kill_switch.json")
            if path == "/api/kill-switch/trigger":
                status = trigger_kill_switch("Manual emergency halt from dashboard", ks_path)
                data = json.dumps(status).encode()
            elif path == "/api/kill-switch/reset":
                status = reset_kill_switch(ks_path)
                data = json.dumps(status).encode()
            else:
                return self._deny(405, "read-only endpoint or unknown action")

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'")
            self.end_headers()
            self.wfile.write(data)

        do_PUT = do_DELETE = do_PATCH = do_POST

    return Handler


def serve(cfg, paths: dict, host: str = "127.0.0.1", port: int = 8765, extra_hosts: tuple[str, ...] = (),
          log=print) -> None:
    password = os.environ.get("DASHBOARD_PASSWORD") or None
    if host not in LOOPBACK and not password:
        raise SystemExit("refusing to bind a non-loopback address without DASHBOARD_PASSWORD set")
    allowed = set(LOOPBACK) | {h.lower() for h in extra_hosts} | ({host.lower()} if host != "0.0.0.0" else set())
    srv = ThreadingHTTPServer((host, port), make_handler(cfg, paths, allowed, password))
    log(f"dashboard on http://{host}:{port}  (auth {'on' if password else 'off, loopback only'}). Ctrl+C to stop.")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
