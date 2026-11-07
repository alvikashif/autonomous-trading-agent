"""Momentum + quality sleeve with a trend filter. Deterministic, no LLM."""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Collection

import pandas as pd


@dataclass(frozen=True)
class StrategyConfig:
    top_n: int = 6
    momentum_lookback_days: int = 126
    momentum_skip_days: int = 21
    use_trend_filter: bool = True
    trend_window_days: int = 200
    min_roe: float = 0.10
    benchmark: str = "^NSEI"
    mode: str = "momentum"  # "momentum" or "size" (equal-weight top_n largest by market cap)
    scale_top_n: bool = False      # size mode: shrink N for small portfolios, only buy affordable stocks
    min_names: int = 5             # never fewer names than this (also raised to satisfy the position cap)
    min_position_value: float = 1500.0  # smallest slot worth opening when scaling N


def momentum_scores(prices: pd.DataFrame, asof: pd.Timestamp, lookback: int, skip: int) -> pd.Series:
    """Return over `lookback` days ending `skip` days ago (skips short-term reversal)."""
    hist = prices.loc[:asof]
    if len(hist) < lookback + skip + 1:
        return pd.Series(dtype=float)
    end = hist.iloc[-(skip + 1)]
    start = hist.iloc[-(lookback + skip + 1)]
    return (end / start - 1).dropna()


def trend_ok(benchmark: pd.Series, asof: pd.Timestamp, window: int) -> bool:
    hist = benchmark.loc[:asof].dropna()
    if len(hist) < window:
        return False
    return bool(hist.iloc[-1] > hist.iloc[-window:].mean())


_INVESTABLE = 0.99   # matches the 1% cash buffer for costs
_PRICE_MARGIN = 1.05  # tolerate a 5% price move between signal and execution


def _scaled_size_weights(
    ranked: pd.Series,
    live_px: pd.Series,
    cfg: StrategyConfig,
    max_position_pct: float,
    equity: float,
    held: set[str],
) -> dict[str, float]:
    """Pick N from what the portfolio can afford.

    N is at most top_n and at most equity / min_position_value, but never below
    max(min_names, 1/max_position_pct) so the position cap still holds. Only stocks
    whose share price fits one slot are used. Stocks already held that are still in the
    top_n by size are kept first, so growing the account adds names instead of churning.
    """
    n_floor = max(cfg.min_names, math.ceil(1.0 / max_position_pct - 1e-9))
    investable = equity * _INVESTABLE
    n_max = min(cfg.top_n, max(n_floor, int(investable // cfg.min_position_value)))
    top_rank = set(ranked.index[: cfg.top_n])
    for n in range(n_max, n_floor - 1, -1):
        slot = investable / n
        afford = [s for s in ranked.index if live_px[s] * _PRICE_MARGIN <= slot]
        kept = [s for s in afford if s in held and s in top_rank]
        chosen = kept[:n]
        chosen += [s for s in afford if s not in chosen][: n - len(chosen)]
        if len(chosen) == n or n == n_floor:
            return {s: min(1.0 / n, max_position_pct) for s in chosen}
    return {}


def target_weights(
    prices: pd.DataFrame,
    benchmark: pd.Series,
    asof: pd.Timestamp,
    eligible: Collection[str],
    cfg: StrategyConfig,
    max_position_pct: float,
    quality: pd.Series | None = None,
    size: pd.Series | None = None,
    equity: float | None = None,
    held: Collection[str] = (),
) -> dict[str, float]:
    """Equal-weight top-N among Shariah-eligible stocks. Empty dict = all cash.

    mode "momentum": rank by trailing return. mode "size": rank by market cap (`size`, today's
    values, so look-ahead biased in backtests) among stocks that have a price at `asof`.
    """
    if cfg.use_trend_filter and not trend_ok(benchmark, asof, cfg.trend_window_days):
        return {}
    cols = [c for c in prices.columns if c in set(eligible)]
    if not cols:
        return {}
    if cfg.mode == "size":
        if size is None:
            raise ValueError("mode 'size' needs market-cap data")
        live_px = prices.loc[:asof, cols].iloc[-1].dropna()
        ranked = size.reindex(live_px.index).dropna().sort_values(ascending=False)
        if quality is not None:
            ranked = ranked[ranked.index.isin(quality[quality >= cfg.min_roe].index)]
        if ranked.empty:
            return {}
        if not (cfg.scale_top_n and equity):
            return {s: min(1.0 / cfg.top_n, max_position_pct) for s in ranked.index[: cfg.top_n]}
        return _scaled_size_weights(ranked, live_px, cfg, max_position_pct, equity, set(held))
    scores = momentum_scores(prices[cols], asof, cfg.momentum_lookback_days, cfg.momentum_skip_days)
    if quality is not None:
        good = quality[quality >= cfg.min_roe].index
        scores = scores[scores.index.isin(good)]
    scores = scores[scores > 0]  # absolute momentum: never buy a falling stock
    top = scores.nlargest(cfg.top_n).index
    if len(top) == 0:
        return {}
    weight = min(1.0 / cfg.top_n, max_position_pct)
    return {s: weight for s in top}
