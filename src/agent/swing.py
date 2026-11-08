"""Short-term (swing) PAPER sandbox: fixed rules, tested against costs and a random-entry baseline.

Daily closes only (no volume in the price cache). Signal at close of day t, fill at close of day t+1,
same convention as backtest.py. Delivery only, long only. Nothing here talks to a broker.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from .costs import CostConfig, trade_cost
from .models import Side


@dataclass(frozen=True)
class SwingParams:
    lookback: int = 20       # breakout = close above the highest close of the previous N days
    trend_days: int = 50     # and above its N-day average close
    rank_days: int = 60      # among candidates, prefer the strongest N-day return
    target_pct: float = 0.08
    stop_pct: float = 0.04
    max_hold_days: int = 10
    max_positions: int = 2
    capital: float = 5000.0
    cooldown_days: int = 10  # no re-entry into the same stock right after an exit


@dataclass
class SwingResult:
    trades: pd.DataFrame
    equity: pd.Series
    total_costs: float
    cash: float = 0.0
    open: dict | None = None            # symbol -> qty, entry_px, entry_i, entry_cost
    pending_exit: dict | None = None    # symbol -> reason, executes at the next close
    pending_buy: list | None = None     # symbols to buy at the next close
    last_prices: dict | None = None
    last_date: pd.Timestamp | None = None

    def summary(self) -> dict:
        t = self.trades
        if t.empty:
            return dict(n=0, win_rate=0.0, avg_net_pct=0.0, total_net=0.0, total_costs=0.0, final=self.equity.iloc[-1])
        return dict(
            n=len(t), win_rate=float((t.net > 0).mean()), avg_net_pct=float(t.net_pct.mean()),
            total_net=float(t.net.sum()), total_costs=self.total_costs, final=float(self.equity.iloc[-1]),
        )


def params_from_config(sc) -> SwingParams:
    return SwingParams(
        lookback=sc.lookback, trend_days=sc.trend_days, rank_days=sc.rank_days, target_pct=sc.target_pct,
        stop_pct=sc.stop_pct, max_hold_days=sc.max_hold_days, max_positions=sc.max_positions, capital=sc.capital,
    )


def swing_status(res: SwingResult, sc, benchmark: pd.Series | None = None) -> dict:
    """JSON-ready snapshot for the dashboard. Open positions are marked at the last close."""
    px = res.last_prices or {}
    held = []
    for s, h in sorted((res.open or {}).items()):
        last = px.get(s)
        held.append({
            "symbol": s, "qty": h["qty"], "entry_px": h["entry_px"], "entry_date": str(h["entry_date"].date()),
            "last": last, "pnl_pct": (last / h["entry_px"] - 1) if last else None,
            "pending": (res.pending_exit or {}).get(s),
        })
    t = res.trades
    recent = []
    if not t.empty:
        for r in t.tail(25).iloc[::-1].itertuples():
            recent.append({"symbol": r.symbol, "entry": str(r.entry.date()), "exit": str(r.exit.date()),
                           "reason": r.reason, "net": float(r.net), "net_pct": float(r.net_pct)})
    s = res.summary()
    equity = float(res.equity.iloc[-1]) if len(res.equity) else sc.capital
    out = {
        "mode": "PAPER sandbox (simulated)", "start_date": sc.start_date, "capital": sc.capital,
        "as_of": str(res.last_date.date()) if res.last_date is not None else None,
        "equity": equity, "cash": res.cash, "pnl": equity - sc.capital, "pnl_pct": equity / sc.capital - 1,
        "trades": s["n"], "win_rate": s["win_rate"], "total_costs": s["total_costs"],
        "holdings": held, "pending_buy": res.pending_buy or [], "recent": recent,
        "rule": f"{sc.lookback}d high + above {sc.trend_days}d avg; +{sc.target_pct:.0%} / -{sc.stop_pct:.0%} / {sc.max_hold_days}d",
    }
    if benchmark is not None and len(benchmark.dropna()):
        b = benchmark.dropna()
        b = b[b.index >= pd.Timestamp(sc.start_date)]
        if len(b) > 1:
            out["benchmark_pct"] = float(b.iloc[-1] / b.iloc[0] - 1)
    return out


def _fill(price: float, side: Side, slip: float) -> float:
    return price * (1 + slip) if side == Side.BUY else price * (1 - slip)


def run_swing(
    prices: pd.DataFrame, symbols: list[str], p: SwingParams, costs: CostConfig,
    entry: str = "breakout", seed: int = 0, start: str | None = None,
) -> SwingResult:
    """entry='breakout' uses the rule; entry='random' picks affordable stocks at random with the SAME exits
    (the baseline: if the rule cannot beat it, the entry signal adds nothing)."""
    px = prices[[s for s in symbols if s in prices.columns]].ffill()
    hi = px.shift(1).rolling(p.lookback).max()
    ma = px.rolling(p.trend_days).mean()
    mom = px.pct_change(p.rank_days)
    rng = np.random.default_rng(seed)
    dates = px.index
    first = max(p.lookback, p.trend_days, p.rank_days) + 1
    if start:
        first = max(first, int(dates.searchsorted(pd.Timestamp(start))))

    cash = p.capital
    pos: dict[str, dict] = {}      # symbol -> qty, entry_px, entry_i, entry_cost
    pending_exit: dict[str, str] = {}
    pending_buy: list[str] = []
    last_exit: dict[str, int] = {}
    rows, eq = [], {}
    total_costs = 0.0
    slot = p.capital / p.max_positions

    for i in range(first, len(dates)):
        d = dates[i]
        row = px.iloc[i]
        # 1) execute yesterday's decisions at today's close
        for s in list(pending_exit):
            if s in pos and pd.notna(row[s]):
                q = pos[s]["qty"]
                fp = _fill(row[s], Side.SELL, costs.slippage_pct)
                c = trade_cost(Side.SELL, q * fp, costs)
                cash += q * fp - c
                total_costs += c
                gross = q * (fp - pos[s]["entry_px"])
                net = q * fp - c - (q * pos[s]["entry_px"] + pos[s]["entry_cost"])
                rows.append(dict(symbol=s, entry=dates[pos[s]["entry_i"]], exit=d, reason=pending_exit[s],
                                 qty=q, buy=pos[s]["entry_px"], sell=fp, gross=gross, net=net,
                                 net_pct=net / (q * pos[s]["entry_px"] + pos[s]["entry_cost"])))
                last_exit[s] = i
                del pos[s]
        pending_exit.clear()
        for s in pending_buy:
            if s in pos or len(pos) >= p.max_positions or pd.isna(row[s]):
                continue
            fp = _fill(row[s], Side.BUY, costs.slippage_pct)
            budget = min(slot, cash)
            q = int(budget // (fp * 1.003))   # leave room for charges
            if q < 1:
                continue
            c = trade_cost(Side.BUY, q * fp, costs)
            if q * fp + c > cash:
                continue
            cash -= q * fp + c
            total_costs += c
            pos[s] = dict(qty=q, entry_px=fp, entry_i=i, entry_cost=c)
        pending_buy = []

        # 2) decide for tomorrow, using only data up to today's close
        for s, h in pos.items():
            r = row[s] / h["entry_px"] - 1
            if r >= p.target_pct:
                pending_exit[s] = "target"
            elif r <= -p.stop_pct:
                pending_exit[s] = "stop"
            elif i - h["entry_i"] >= p.max_hold_days:
                pending_exit[s] = "time"
        free = p.max_positions - len(pos)
        if free > 0:
            ok = row.notna() & (row <= slot * 0.95)
            ok &= ~row.index.isin(list(pos))
            ok &= pd.Series({s: i - last_exit.get(s, -10**9) > p.cooldown_days for s in row.index})
            if entry == "breakout":
                cand = ok & (row > hi.iloc[i]) & (row > ma.iloc[i])
                names = mom.iloc[i][cand].dropna().sort_values(ascending=False).index.tolist()
            else:
                names = list(rng.permutation(row.index[ok].tolist()))
            pending_buy = names[:free]

        eq[d] = cash + sum(h["qty"] * row[s] for s, h in pos.items())

    trades = pd.DataFrame(rows)
    return SwingResult(
        trades, pd.Series(eq, dtype=float), total_costs, cash=cash,
        open={s: dict(h, entry_date=dates[h["entry_i"]]) for s, h in pos.items()},
        pending_exit=dict(pending_exit), pending_buy=list(pending_buy),
        last_prices=px.iloc[-1].dropna().to_dict(), last_date=dates[-1],
    )
