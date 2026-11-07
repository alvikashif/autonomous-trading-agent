"""Monthly-rebalance backtest with costs, slippage, whole shares and monthly contributions.

Signals use data up to month-end close; trades execute at the NEXT trading day close.

Known biases (read before trusting results):
- Survivorship: the eligible list is today's Shariah-compliant set.
- Look-ahead: today's ROE and compliance are applied to the past.
Treat results as an upper bound, not a forecast.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Collection, Mapping

import numpy as np
import pandas as pd

from .costs import CostConfig
from .models import Order, Side
from .paper import PaperBroker
from .portfolio import plan_orders
from .risk import RiskLimits, check_orders
from .strategy import StrategyConfig, target_weights


@dataclass
class BacktestResult:
    equity: pd.Series
    twr_index: pd.Series
    total_contributed: float
    total_costs: float
    n_trades: int
    metrics: dict
    benchmark_metrics: dict
    universe_metrics: dict   # equal-weight of ALL eligible stocks: isolates strategy skill from survivorship
    twr: pd.Series
    bench_returns: pd.Series
    universe_returns: pd.Series
    n_stops: int = 0


def window_metrics(returns: pd.Series, start: str | None, end: str | None, rf: float = 0.065) -> dict:
    return perf_metrics(returns.loc[start:end], rf)


def perf_metrics(returns: pd.Series, rf: float = 0.065) -> dict:
    r = returns.dropna()
    if len(r) < 2:
        return {"cagr": float("nan"), "max_drawdown": float("nan"), "sharpe": float("nan")}
    index = (1 + r).cumprod()
    years = len(r) / 252
    cagr = index.iloc[-1] ** (1 / years) - 1 if years > 0 and index.iloc[-1] > 0 else float("nan")
    dd = (index / index.cummax() - 1).min()
    std = r.std()
    sharpe = (r.mean() - rf / 252) / std * math.sqrt(252) if std > 0 else float("nan")
    return {"cagr": float(cagr), "max_drawdown": float(dd), "sharpe": float(sharpe)}


def run_backtest(
    prices: pd.DataFrame,
    benchmark: pd.Series,
    eligible: Collection[str],
    sectors: Mapping[str, str],
    strat: StrategyConfig,
    limits: RiskLimits,
    costs: CostConfig,
    monthly_contribution: float,
    min_trade_value: float,
    cash_buffer_pct: float,
    quality: pd.Series | None = None,
    rf: float = 0.065,
    rebalance_months: int = 1,
    size: pd.Series | None = None,
    stop_loss_pct: float | None = None,
) -> BacktestResult:
    prices = prices.sort_index().ffill()
    benchmark = benchmark.reindex(prices.index).ffill()
    idx = prices.index

    month_ends = idx.to_series().groupby(idx.to_period("M")).last().tolist()
    exec_to_signal: dict[pd.Timestamp, pd.Timestamp] = {}
    for d in month_ends:
        pos = idx.get_loc(d)
        if pos + 1 < len(idx):
            exec_to_signal[idx[pos + 1]] = d

    broker = PaperBroker(None, costs)
    equity_vals: list[float] = []
    contribs: list[float] = []
    total_contributed = 0.0
    exec_count = 0  # contributions arrive monthly; trades only every `rebalance_months`
    # Trailing stop: a close <= peak*(1-stop) queues a sell, executed at the NEXT day's close.
    peak: dict[str, float] = {}
    pending_stops: list[str] = []
    stopped: set[str] = set()   # not re-bought until the next rebalance
    n_stops = 0

    for date in idx:
        px = prices.loc[date].dropna().to_dict()
        contrib = 0.0
        for sym in pending_stops:
            qty = broker.positions().get(sym, 0)
            if qty and sym in px:
                broker.place_order(Order(sym, Side.SELL, qty, px[sym]), ts=str(date.date()))
                n_stops += 1
                stopped.add(sym)
        pending_stops = []
        if date in exec_to_signal:
            broker.deposit(monthly_contribution)
            contrib = monthly_contribution
            total_contributed += contrib
            signal_date = exec_to_signal[date]
            trade_now = exec_count % rebalance_months == 0
            exec_count += 1
            if trade_now:
                eq_now = broker.equity(px)
                targets = target_weights(
                    prices.loc[:signal_date], benchmark.loc[:signal_date], signal_date,
                    set(eligible) - stopped, strat, limits.max_position_pct, quality, size,
                    equity=eq_now, held=broker.positions().keys(),
                )
                orders = plan_orders(targets, broker.positions(), px, eq_now, min_trade_value, cash_buffer_pct)
                approved, _ = check_orders(
                    orders, broker.cash(), broker.positions(), px, sectors, eq_now, limits, costs
                )
                for o in approved:
                    broker.place_order(o, ts=str(date.date()))
                stopped.clear()
        if stop_loss_pct:
            held = broker.positions()
            for sym in list(peak):
                if sym not in held:
                    del peak[sym]
            for sym in held:
                p = px.get(sym)
                if p is None:
                    continue
                peak[sym] = max(peak.get(sym, p), p)
                if p <= peak[sym] * (1 - stop_loss_pct):
                    pending_stops.append(sym)
        equity_vals.append(broker.equity(px))
        contribs.append(contrib)

    equity = pd.Series(equity_vals, index=idx)
    contrib_s = pd.Series(contribs, index=idx)
    prev = equity.shift(1)
    twr = ((equity - contrib_s) / prev - 1).where(prev > 0, 0.0).fillna(0.0)
    twr_index = (1 + twr).cumprod()

    bench_ret = benchmark.pct_change().fillna(0.0)
    elig_cols = [c for c in prices.columns if c in set(eligible)]
    universe_ret = prices[elig_cols].pct_change().mean(axis=1).fillna(0.0)
    return BacktestResult(
        equity=equity,
        twr_index=twr_index,
        total_contributed=total_contributed,
        total_costs=broker.total_costs,
        n_trades=len(broker.trades),
        metrics=perf_metrics(twr, rf),
        benchmark_metrics=perf_metrics(bench_ret, rf),
        universe_metrics=perf_metrics(universe_ret, rf),
        twr=twr,
        bench_returns=bench_ret,
        universe_returns=universe_ret,
        n_stops=n_stops,
    )
