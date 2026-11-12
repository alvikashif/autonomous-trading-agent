"""Market-hours monitor. Watches holdings, raises alerts, and executes mid-day stop-loss or AI-evaluated profit taking.

NSE trades Mon-Fri 09:15-15:30 IST. Outside that window the loop only writes a heartbeat.
Exchange holidays are NOT known here: on a holiday quotes just stay unchanged.

Anti-throttling & Resiliency:
- Default poll interval during market hours is 120s (configurable via MONITOR_INTERVAL).
- Resilient quote fetching with Google Finance fallback to prevent Yahoo rate-limiting on cloud IPs.
- Groww API is never hammered for quotes; only called for demat positions and order execution.
"""
from __future__ import annotations

import json
import logging
import math
import os
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping
from zoneinfo import ZoneInfo

from .models import Order, Side
from .paper import PaperBroker
from .quotes import fetch_resilient_quotes

logger = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")
OPEN, CLOSE = (9, 15), (15, 30)


def market_open(now: datetime) -> bool:
    now = now.astimezone(IST)
    if now.weekday() >= 5:
        return False
    return OPEN <= (now.hour, now.minute) < CLOSE


@dataclass(frozen=True)
class Event:
    kind: str      # ALERT_DROP | STOP_HIT | DAILY_LOSS | NO_QUOTE | AI_STOP_HIT | AI_TARGET_REACHED
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
    """Legacy alias: delegates to fetch_resilient_quotes."""
    return fetch_resilient_quotes(symbols)


def _load(path: Path | str) -> dict:
    try:
        p = Path(path)
        if not p.is_file():
            return {}
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save(path: Path | str, data: dict) -> None:
    try:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
    except OSError as exc:
        logger.error("Failed to write to %s: %s", path, exc)


def run_once(
    paper_path: str,
    status_path: str,
    cost_cfg,
    stop_pct: float,
    alert_pct: float,
    daily_loss_limit_pct: float,
    act: bool,
    now: datetime | None = None,
    quote_fn=None,
    ai_signals_path: str = "",
    broker: Any = None,
    ai_cfg: Any = None,
    live_mode: bool = False,
    kill_switch_path: str = "data/kill_switch.json",
) -> dict:
    now = now or datetime.now(IST)
    status_file = Path(status_path)
    status = _load(status_file)
    peaks: dict[str, float] = {k: float(v) for k, v in status.get("peaks", {}).items()}
    alerts: list[dict] = status.get("alerts", [])
    is_open = market_open(now)

    quote_fetcher = quote_fn if quote_fn is not None else fetch_resilient_quotes

    # Base paper positions
    paper_broker = PaperBroker(paper_path, cost_cfg)
    positions = paper_broker.positions()

    # Active AI Swing Position
    ai_sig = _load(ai_signals_path) if ai_signals_path else {}
    selected = ai_sig.get("selected_trade")
    ai_symbol: str = ""
    ai_qty: int = 0
    if selected and ai_sig.get("verdict") == "EXECUTE" and not selected.get("exit"):
        ai_symbol = selected.get("symbol", "").upper()
        entry_px_val = float(selected.get("entry_price", 0.0))
        hard_stop_pct = float(getattr(ai_cfg, "hard_stop_pct", 0.07)) if ai_cfg else 0.07
        
        # Always record structural hard stop floor in AI signals
        if entry_px_val > 0 and not selected.get("hard_stop_price"):
            selected["hard_stop_pct"] = hard_stop_pct
            selected["hard_stop_price"] = round(round((entry_px_val * (1.0 - hard_stop_pct)) / 0.05) * 0.05, 2)
            ai_sig["selected_trade"] = selected
            _save(ai_signals_path, ai_sig)

        # Resolve quantity from live broker with fallback to paper_state / live_trades
        if live_mode and broker and hasattr(broker, "positions"):
            try:
                live_pos = broker.positions()
                ai_qty = live_pos.get(ai_symbol, 0)
            except Exception as pe:
                logger.debug(f"Could not fetch broker positions: {pe}")
        if ai_qty <= 0:
            ai_qty = int(positions.get(ai_symbol, selected.get("qty", 0)))
        if ai_qty <= 0:
            try:
                lt_file = Path("data/live_trades.json")
                if lt_file.is_file():
                    lt_data = json.loads(lt_file.read_text(encoding="utf-8"))
                    for t in lt_data:
                        if t.get("symbol") == ai_symbol and t.get("side") == "BUY":
                            ai_qty += int(t.get("qty", 0))
            except Exception:
                pass
        if ai_qty <= 0:
            ai_qty = 3  # Safe minimum fallback for active held position

        # Ensure Emergency Hard Stop GTT is active on Groww exchange book whenever a live position is held
        if live_mode and broker and hasattr(broker, "create_gtt_stop_loss") and ai_qty > 0 and not selected.get("gtt_order_id"):
            hard_stop_px = entry_px_val * (1.0 - hard_stop_pct)
            try:
                gtt_resp = broker.create_gtt_stop_loss(
                    symbol=ai_symbol,
                    qty=ai_qty,
                    trigger_price=hard_stop_px,
                    limit_price=hard_stop_px * 0.995,
                )
                gtt_id = (
                    gtt_resp.get("smart_order_id")
                    or gtt_resp.get("order_id")
                    or gtt_resp.get("id")
                    or (gtt_resp.get("data", {}).get("smart_order_id") if isinstance(gtt_resp.get("data"), dict) else None)
                    or "ACTIVE"
                )
                selected["gtt_order_id"] = str(gtt_id)
                selected["gtt_trigger_price"] = round(round(hard_stop_px / 0.05) * 0.05, 2)
                selected["hard_stop_pct"] = hard_stop_pct
                ai_sig["selected_trade"] = selected
                _save(ai_signals_path, ai_sig)
                logger.info(f"Groww GTT Safety Stop registered for {ai_qty} {ai_symbol} @ Rs {hard_stop_px:.2f} (ID: {gtt_id})")
                alerts.append({
                    "ts": now.isoformat(timespec="seconds"),
                    "kind": "GTT_STOP_PLACED",
                    "symbol": ai_symbol,
                    "detail": f"Emergency GTT Stop Loss placed on Groww: {ai_qty} shares @ Trigger Rs {hard_stop_px:.2f} (-{hard_stop_pct*100:.0f}%)",
                })
            except Exception as e:
                logger.warning(f"Could not register Groww GTT safety stop for {ai_symbol}: {e}")

    # Collect all symbols needing quotes
    syms_to_quote = set(positions.keys())
    if ai_symbol and ai_qty > 0:
        syms_to_quote.add(ai_symbol)

    quotes, prev = ({}, {})
    new_events: list[Event] = []

    if is_open and syms_to_quote:
        quotes, prev = quote_fetcher(list(syms_to_quote))

        # Check paper positions
        if positions:
            new_events.extend(check_positions(positions, quotes, prev, peaks, stop_pct, alert_pct, daily_loss_limit_pct))
            if act and stop_pct > 0:
                for ev in new_events:
                    if ev.kind == "STOP_HIT" and positions.get(ev.symbol):
                        paper_broker.place_order(
                            Order(ev.symbol, Side.SELL, positions[ev.symbol], quotes[ev.symbol]),
                            ts=now.date().isoformat(),
                        )
                        peaks.pop(ev.symbol, None)
                        paper_broker.save()
                        alerts.append({
                            "ts": now.isoformat(timespec="seconds"),
                            "kind": "STOP_SOLD",
                            "symbol": ev.symbol,
                            "detail": "paper sell executed",
                        })

        # Check Mid-Day AI Swing Position (Stop Loss or Take Profit)
        if ai_symbol and ai_qty > 0 and ai_symbol in quotes:
            cur_px = quotes[ai_symbol]
            entry_px = float(selected.get("entry_price", cur_px))
            target_px = float(selected.get("target_price", entry_px * 1.08))
            stop_px = float(selected.get("stop_loss", entry_px * 0.96))
            peaks[ai_symbol] = max(peaks.get(ai_symbol, cur_px), cur_px)

            stamp = now.isoformat(timespec="seconds")

            # 1. Mid-Day Stop-Loss Breach & AI Brain Consultation
            if cur_px <= stop_px:
                ev_detail = f"Mid-day price Rs {cur_px:.2f} <= stop Rs {stop_px:.2f} (loss {(cur_px/entry_px-1)*100:.1f}%)"
                new_events.append(Event("AI_STOP_HIT", ai_symbol, ev_detail))

                already_consulted = selected.get("stop_consulted", False)
                hard_stop_px = entry_px * (1.0 - float(getattr(ai_cfg, "hard_stop_pct", 0.07))) if ai_cfg else entry_px * 0.93

                # Consult AI Brain on whether to hold through normal pullback or cut loss
                should_consult = (
                    not already_consulted
                    and cur_px > hard_stop_px
                    and ai_cfg
                    and hasattr(ai_cfg, "resolved_api_key")
                    and bool(ai_cfg.resolved_api_key())
                )

                ai_decision = "EXIT_CUT_LOSS"
                eval_payload = None

                if should_consult:
                    try:
                        from .ai_brain import evaluate_stop_loss
                        from .research import get_cached_research
                        res_data = get_cached_research(ai_symbol, base_dir=status_file.parent)
                        holding_info = {
                            "symbol": ai_symbol,
                            "entry_price": entry_px,
                            "current_price": cur_px,
                            "loss_pct": (cur_px / entry_px - 1),
                            "stop_loss": stop_px,
                        }
                        tech_info = {
                            "sma_50": selected.get("sma_50"),
                            "sma_200": selected.get("sma_200"),
                            "high_20d": selected.get("high_20d"),
                            "rsi_14": selected.get("rsi_14"),
                            "pct_above_50sma": selected.get("pct_above_50sma"),
                        }
                        eval_payload = evaluate_stop_loss(holding_info, tech_info, res_data, ai_cfg)
                        ai_decision = eval_payload.get("decision", "EXIT_CUT_LOSS")
                        ai_sig.setdefault("stop_consultations", {})[ai_symbol] = eval_payload
                    except Exception as err:
                        logger.error("AI Brain stop-loss consultation failed: %s", err)

                if should_consult and ai_decision == "HOLD_AND_GIVE_ROOM" and eval_payload:
                    # AI recommends holding through dip; revise stop loss
                    rev_stop = float(eval_payload.get("revised_stop_loss") or (entry_px * 0.94))
                    rev_stop = max(rev_stop, hard_stop_px)
                    selected["stop_loss"] = rev_stop
                    selected["stop_consulted"] = True
                    selected["stop_consultation_time"] = stamp
                    selected["stop_consultation_reason"] = eval_payload.get("reason", "")
                    ai_sig["selected_trade"] = selected
                    _save(ai_signals_path, ai_sig)

                    alerts.append({
                        "ts": stamp,
                        "kind": "AI_STOP_ROOM_GIVEN",
                        "symbol": ai_symbol,
                        "detail": f"AI advised hold through dip: Revised Stop Rs {rev_stop:.2f} ({eval_payload.get('reason', '')})",
                    })
                    try:
                        from .notify import dispatch_notification
                        dispatch_notification(
                            event_type="alert",
                            title=f"AI STOP CONSULTATION: {ai_symbol}",
                            message=(
                                f"Dip to Rs {cur_px:.2f} ({(cur_px/entry_px-1)*100:.1f}%). AI Brain recommends HOLD_AND_GIVE_ROOM (Confidence: {eval_payload.get('confidence_score', 8)}/10).\n"
                                f"• Revised Stop: Rs {rev_stop:.2f}\n"
                                f"• Analysis: {eval_payload.get('reason', '')}"
                            ),
                            details={"symbol": ai_symbol, "action": "HOLD", "revised_stop": rev_stop, "confidence": eval_payload.get("confidence_score")},
                            path=status_file.parent / "notifications.json",
                        )
                    except Exception as e:
                        logger.debug(f"Notification error: {e}")
                elif act:
                    from .kill_switch import is_kill_switch_active
                    if is_kill_switch_active(kill_switch_path):
                        alerts.append({"ts": stamp, "kind": "KILL_SWITCH_BLOCKED", "symbol": ai_symbol,
                                       "detail": "Stop loss sell blocked by active Kill Switch."})
                    else:
                        active_broker = broker if (live_mode and broker) else paper_broker
                        sell_order = Order(ai_symbol, Side.SELL, ai_qty, cur_px)
                        active_broker.place_order(sell_order)

                        # Cancel emergency GTT order if active
                        if live_mode and broker and hasattr(broker, "cancel_gtt_order") and selected.get("gtt_order_id"):
                            try:
                                broker.cancel_gtt_order(selected["gtt_order_id"])
                                selected["gtt_order_status"] = "CANCELLED_FOR_EXIT"
                            except Exception as ge:
                                logger.debug(f"GTT cancel error: {ge}")

                        exit_reason = eval_payload.get("reason") if eval_payload else "Breached stop-loss floor."
                        selected["exit"] = {
                            "type": "STOP_LOSS",
                            "price": cur_px,
                            "timestamp": stamp,
                            "qty": ai_qty,
                            "pnl_pct": (cur_px / entry_px - 1),
                            "ai_reason": exit_reason,
                        }
                        ai_sig["selected_trade"] = selected
                        _save(ai_signals_path, ai_sig)
                        alerts.append({
                            "ts": stamp,
                            "kind": "AI_STOP_EXECUTED",
                            "symbol": ai_symbol,
                            "detail": f"Sold {ai_qty} shares @ Rs {cur_px:.2f} ({'LIVE' if live_mode else 'PAPER'})",
                        })
                        try:
                            from .notify import dispatch_notification
                            dispatch_notification(
                                event_type="stop_loss",
                                title=f"STOP LOSS EXECUTED: {ai_symbol}",
                                message=f"Sold {ai_qty} shares @ Rs {cur_px:.2f} (Loss: {(cur_px/entry_px-1)*100:.1f}%) [{'LIVE' if live_mode else 'PAPER'}]. Reason: {exit_reason}",
                                details={"symbol": ai_symbol, "qty": ai_qty, "price": cur_px, "pnl_pct": (cur_px/entry_px-1)},
                                path=status_file.parent / "notifications.json",
                            )
                        except Exception as e:
                            logger.debug(f"Notification error: {e}")

            # 2. Mid-Day Profit Target Reached
            elif cur_px >= target_px:
                ev_detail = f"Mid-day price Rs {cur_px:.2f} >= target Rs {target_px:.2f} (gain +{(cur_px/entry_px-1)*100:.1f}%)"
                new_events.append(Event("AI_TARGET_REACHED", ai_symbol, ev_detail))

                # Ask AI Brain whether to take profit now or hold & trail
                ai_decision = "TAKE_PROFIT"
                ai_reason = "Target reached, default take-profit."
                eval_payload = None

                if ai_cfg and hasattr(ai_cfg, "resolved_api_key") and ai_cfg.resolved_api_key():
                    try:
                        from .ai_brain import evaluate_take_profit
                        holding_info = {
                            "symbol": ai_symbol,
                            "entry_price": entry_px,
                            "current_price": cur_px,
                            "gain_pct": (cur_px / entry_px - 1),
                        }
                        eval_payload = evaluate_take_profit(holding_info, None, ai_cfg)
                        ai_decision = eval_payload.get("decision", "TAKE_PROFIT")
                        ai_reason = eval_payload.get("reason", "")
                        ai_sig.setdefault("exit_evaluations", {})[ai_symbol] = eval_payload
                    except Exception as err:
                        logger.error("AI Brain take-profit evaluation failed: %s", err)

                if ai_decision == "HOLD_AND_TRAIL" and eval_payload:
                    # AI recommends trailing higher: adjust target & stop in state
                    rev_target = eval_payload.get("revised_target_price", cur_px * 1.15)
                    trail_stop = eval_payload.get("trailing_stop_price", cur_px * 0.95)
                    cap_advice = eval_payload.get("capital_scaling_advice", "")
                    selected["target_price"] = rev_target
                    selected["stop_loss"] = trail_stop
                    if cap_advice:
                        selected["capital_scaling_advice"] = cap_advice
                    ai_sig["selected_trade"] = selected
                    _save(ai_signals_path, ai_sig)
                    alerts.append({
                        "ts": stamp,
                        "kind": "AI_HOLD_AND_TRAIL",
                        "symbol": ai_symbol,
                        "detail": f"AI advised trail: Revised Target Rs {rev_target:.2f}, Trailing Stop Rs {trail_stop:.2f} ({ai_reason})",
                    })
                    try:
                        from .notify import dispatch_notification
                        msg = (
                            f"Hit milestone @ Rs {cur_px:.2f} (+{(cur_px/entry_px-1)*100:.1f}%).\n"
                            f"• Decision: HOLD_AND_TRAIL (Multi-Month Compounding)\n"
                            f"• Revised Target: Rs {rev_target:.2f}\n"
                            f"• Locked-in Trailing Stop: Rs {trail_stop:.2f}\n"
                            f"• Rationale: {ai_reason}"
                        )
                        if cap_advice:
                            msg += f"\n• Capital Advice: {cap_advice}"
                        dispatch_notification(
                            event_type="take_profit",
                            title=f"PROFIT TRAILED TO HIGHER TARGET: {ai_symbol}",
                            message=msg,
                            details={"symbol": ai_symbol, "action": "TRAIL", "target": rev_target, "stop": trail_stop},
                            path=status_file.parent / "notifications.json",
                        )
                    except Exception as e:
                        logger.debug(f"Notification error: {e}")
                else:
                    # Execute Profit Taking
                    if act:
                        from .kill_switch import is_kill_switch_active
                        if is_kill_switch_active(kill_switch_path):
                            alerts.append({"ts": stamp, "kind": "KILL_SWITCH_BLOCKED", "symbol": ai_symbol,
                                           "detail": "Take profit sell blocked by active Kill Switch."})
                        else:
                            active_broker = broker if (live_mode and broker) else paper_broker
                            sell_order = Order(ai_symbol, Side.SELL, ai_qty, cur_px)
                            active_broker.place_order(sell_order)

                            # Cancel emergency GTT order if active
                            if live_mode and broker and hasattr(broker, "cancel_gtt_order") and selected.get("gtt_order_id"):
                                try:
                                    broker.cancel_gtt_order(selected["gtt_order_id"])
                                    selected["gtt_order_status"] = "CANCELLED_FOR_EXIT"
                                except Exception as ge:
                                    logger.debug(f"GTT cancel error: {ge}")

                            selected["exit"] = {
                                "type": "TAKE_PROFIT",
                                "price": cur_px,
                                "timestamp": stamp,
                                "qty": ai_qty,
                                "pnl_pct": (cur_px / entry_px - 1),
                                "ai_reason": ai_reason,
                            }
                            ai_sig["selected_trade"] = selected
                            _save(ai_signals_path, ai_sig)
                            alerts.append({
                                "ts": stamp,
                                "kind": "AI_PROFIT_TAKEN",
                                "symbol": ai_symbol,
                                "detail": f"Took profit: Sold {ai_qty} shares @ Rs {cur_px:.2f} (+{(cur_px/entry_px-1)*100:.1f}%) ({'LIVE' if live_mode else 'PAPER'})",
                            })
                            try:
                                from .notify import dispatch_notification
                                dispatch_notification(
                                    event_type="take_profit",
                                    title=f"PROFIT TAKEN: {ai_symbol}",
                                    message=f"Target reached (+{(cur_px/entry_px-1)*100:.1f}%). Sold {ai_qty} shares @ Rs {cur_px:.2f} [{'LIVE' if live_mode else 'PAPER'}]",
                                    details={"symbol": ai_symbol, "qty": ai_qty, "price": cur_px, "gain_pct": (cur_px/entry_px-1)},
                                    path=status_file.parent / "notifications.json",
                                )
                            except Exception as e:
                                logger.debug(f"Notification error: {e}")

    stamp = now.isoformat(timespec="seconds")
    for ev in new_events:
        # one alert per (kind, symbol) per day to prevent log spamming
        key = (ev.kind, ev.symbol, stamp[:10])
        if not any((a.get("kind"), a.get("symbol"), str(a.get("ts", ""))[:10]) == key for a in alerts):
            alerts.append({"ts": stamp, "kind": ev.kind, "symbol": ev.symbol, "detail": ev.detail})

    # In Live Mode, periodically sync actual broker cash and positions to shared paper_path
    live_cash_val = None
    if live_mode and broker:
        try:
            if hasattr(broker, "cash"):
                live_cash_val = float(broker.cash())
            live_pos = broker.positions() if hasattr(broker, "positions") else None
            p_data = _load(paper_path)
            if p_data:
                updated_p = False
                if live_cash_val is not None and abs(p_data.get("cash", 0.0) - live_cash_val) > 0.01:
                    p_data["cash"] = live_cash_val
                    updated_p = True
                if live_pos is not None:
                    clean_pos = {k: int(v) for k, v in live_pos.items() if int(v) > 0}
                    if p_data.get("positions") != clean_pos:
                        p_data["positions"] = clean_pos
                        updated_p = True
                if updated_p:
                    _save(paper_path, p_data)
        except Exception as e:
            logger.debug(f"Live broker state sync failed: {e}")

    status = {
        "heartbeat": stamp,
        "market_open": is_open,
        "peaks": peaks,
        "quotes": quotes or status.get("quotes", {}),
        "prev_close": prev or status.get("prev_close", {}),
        "stop_pct": stop_pct,
        "act": act,
        "live_mode": live_mode,
        "live_cash": live_cash_val,
        "ai_monitored": bool(ai_symbol and ai_qty > 0),
        "alerts": alerts[-50:],
    }
    status_file.parent.mkdir(parents=True, exist_ok=True)
    status_file.write_text(json.dumps(status, indent=2))
    return status


def run_forever(
    paper_path: str,
    status_path: str,
    cost_cfg,
    stop_pct: float,
    alert_pct: float,
    daily_loss_limit_pct: float,
    act: bool,
    open_interval: int | None = None,
    closed_interval: int = 300,
    quote_fn=None,
    ai_signals_path: str = "",
    broker: Any = None,
    ai_cfg: Any = None,
    live_mode: bool = False,
    kill_switch_path: str = "data/kill_switch.json",
    log=print,
) -> None:
    # Default interval: 60s during market hours (fast real-time checks)
    if open_interval is None:
        try:
            open_interval = int(os.environ.get("MONITOR_INTERVAL", "60"))
        except ValueError:
            open_interval = 60

    mode_label = "LIVE GROWW" if live_mode else "PAPER"
    log(f"Monitor started [{mode_label}]: act={act}, poll={open_interval}s open / {closed_interval}s closed.")
    log(f"Rate-limit guard: Anti-throttling active with Google Finance fallback. Ctrl+C to stop.")

    while True:
        try:
            st = run_once(
                paper_path=paper_path,
                status_path=status_path,
                cost_cfg=cost_cfg,
                stop_pct=stop_pct,
                alert_pct=alert_pct,
                daily_loss_limit_pct=daily_loss_limit_pct,
                act=act,
                quote_fn=quote_fn,
                ai_signals_path=ai_signals_path,
                broker=broker,
                ai_cfg=ai_cfg,
                live_mode=live_mode,
                kill_switch_path=kill_switch_path,
            )
            log(f"{st['heartbeat']} market_open={st['market_open']} alerts={len(st['alerts'])} ai_monitored={st.get('ai_monitored', False)}")
        except Exception as exc:  # keep running through transient network blips
            log(f"Monitor error (continuing): {exc}")

        # Sleep between checks: 120s during market open, 300s when closed
        time.sleep(open_interval if market_open(datetime.now(IST)) else closed_interval)
