"""Market-hours monitor (paper). Watches holdings, raises alerts, optionally enforces a stop-loss.

NSE trades Mon-Fri 09:15-15:30 IST. Outside that window the loop only writes a heartbeat.
Exchange holidays are NOT known here: on a holiday quotes just stay unchanged.

It never buys. The only automatic action is a paper SELL when a trailing stop is set and hit.
"""
from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Mapping
from zoneinfo import ZoneInfo

from .models import Order, Side
from .paper import PaperBroker

IST = ZoneInfo("Asia/Kolkata")
OPEN, CLOSE = (9, 15), (15, 30)


def market_open(now: datetime) -> bool:
    now = now.astimezone(IST)
    if now.weekday() >= 5:
        return False
    return OPEN <= (now.hour, now.minute) < CLOSE


@dataclass(frozen=True)
class Event:
    kind: str      # ALERT_DROP | STOP_HIT | DAILY_LOSS | NO_QUOTE
    symbol: str
    detail: str


def check_positions(
    positions: Mapping[str, int],
    quotes: Mapping[str, float],
    prev_close: Mapping[str, float],
    peaks: dict[str, float],
    stop_pct: float,
    alert_pct: float,
    daily_loss_limit_pct: float,
) -> list[Event]:
    """Pure check. Mutates `peaks` (highest price seen per holding). Returns events."""
    events: list[Event] = []
    day_pnl = day_base = 0.0
    for sym, qty in positions.items():
        p = quotes.get(sym)
        if p is None or not math.isfinite(p) or p <= 0:
            events.append(Event("NO_QUOTE", sym, "no usable quote"))
            continue
        peaks[sym] = max(peaks.get(sym, p), p)
        drop = 1 - p / peaks[sym]
        if stop_pct > 0 and drop >= stop_pct:
            events.append(Event("STOP_HIT", sym, f"{drop:.1%} below peak {peaks[sym]:.2f} (stop {stop_pct:.0%})"))
        elif alert_pct > 0 and drop >= alert_pct:
            events.append(Event("ALERT_DROP", sym, f"{drop:.1%} below peak {peaks[sym]:.2f}"))
        pc = prev_close.get(sym)
        if pc and math.isfinite(pc) and pc > 0:
            day_pnl += qty * (p - pc)
            day_base += qty * pc
    if day_base > 0 and day_pnl / day_base <= -daily_loss_limit_pct:
        events.append(Event("DAILY_LOSS", "*", f"holdings down {day_pnl / day_base:.1%} today (limit {daily_loss_limit_pct:.0%})"))
    for sym in list(peaks):
        if sym not in positions:
            del peaks[sym]
    return events


def fetch_quotes(symbols: list[str]) -> tuple[dict[str, float], dict[str, float]]:
    """(last price, previous close) per symbol from Yahoo. Missing symbols are omitted."""
    import yfinance as yf  # lazy

    last: dict[str, float] = {}
    prev: dict[str, float] = {}
    for s in symbols:
        try:
            fi = yf.Ticker(f"{s}.NS").fast_info
            last[s] = float(fi["last_price"])
            prev[s] = float(fi["previous_close"])
        except Exception:
            pass
    return last, prev


def _load(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def run_once(paper_path: str, status_path: str, cost_cfg, stop_pct: float, alert_pct: float,
             daily_loss_limit_pct: float, act: bool, now: datetime | None = None, quote_fn=fetch_quotes) -> dict:
    now = now or datetime.now(IST)
    status_file = Path(status_path)
    status = _load(status_file)
    peaks: dict[str, float] = {k: float(v) for k, v in status.get("peaks", {}).items()}
    alerts: list[dict] = status.get("alerts", [])
    is_open = market_open(now)
    broker = PaperBroker(paper_path, cost_cfg)
    positions = broker.positions()
    quotes, prev = ({}, {})
    new_events: list[Event] = []
    if is_open and positions:
        quotes, prev = quote_fn(list(positions))
        new_events = check_positions(positions, quotes, prev, peaks, stop_pct, alert_pct, daily_loss_limit_pct)
        if act and stop_pct > 0:
            for ev in new_events:
                if ev.kind == "STOP_HIT" and positions.get(ev.symbol):
                    broker.place_order(Order(ev.symbol, Side.SELL, positions[ev.symbol], quotes[ev.symbol]),
                                       ts=now.date().isoformat())
                    peaks.pop(ev.symbol, None)
                    broker.save()
                    alerts.append({"ts": now.isoformat(timespec="seconds"), "kind": "STOP_SOLD",
                                   "symbol": ev.symbol, "detail": "paper sell executed"})
    stamp = now.isoformat(timespec="seconds")
    for ev in new_events:
        # one alert per (kind, symbol) per day, so a 60s loop does not spam
        key = (ev.kind, ev.symbol, stamp[:10])
        if not any((a["kind"], a["symbol"], a["ts"][:10]) == key for a in alerts):
            alerts.append({"ts": stamp, "kind": ev.kind, "symbol": ev.symbol, "detail": ev.detail})
    status = {
        "heartbeat": stamp,
        "market_open": is_open,
        "peaks": peaks,
        "quotes": quotes or status.get("quotes", {}),
        "prev_close": prev or status.get("prev_close", {}),
        "stop_pct": stop_pct,
        "act": act,
        "alerts": alerts[-50:],
    }
    status_file.parent.mkdir(parents=True, exist_ok=True)
    status_file.write_text(json.dumps(status, indent=2))
    return status


def run_forever(paper_path: str, status_path: str, cost_cfg, stop_pct: float, alert_pct: float,
                daily_loss_limit_pct: float, act: bool, open_interval: int = 60, closed_interval: int = 300,
                log=print) -> None:
    log(f"monitor started: stop={stop_pct:.0%} act={act} (paper only). Ctrl+C to stop.")
    while True:
        try:
            st = run_once(paper_path, status_path, cost_cfg, stop_pct, alert_pct, daily_loss_limit_pct, act)
            log(f"{st['heartbeat']} market_open={st['market_open']} alerts={len(st['alerts'])}")
        except Exception as exc:  # keep running through network blips
            log(f"monitor error (continuing): {exc}")
        time.sleep(open_interval if market_open(datetime.now(IST)) else closed_interval)
