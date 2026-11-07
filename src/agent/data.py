from __future__ import annotations

from pathlib import Path
from typing import Sequence

import pandas as pd


def load_universe(path: str | Path) -> pd.DataFrame:
    """Load an NSE index constituent CSV. Needs Symbol and Industry columns."""
    df = pd.read_csv(path)
    cols = {c.strip().lower(): c for c in df.columns}
    if "symbol" not in cols:
        raise ValueError(f"{path}: no Symbol column (found {list(df.columns)})")
    out = pd.DataFrame({"symbol": df[cols["symbol"]].astype(str).str.strip().str.upper()})
    out["industry"] = df[cols["industry"]] if "industry" in cols else None
    return out.drop_duplicates("symbol").reset_index(drop=True)


def load_fundamentals(path: str | Path) -> pd.DataFrame:
    """CSV with symbol, market_cap, total_debt, cash_and_securities, interest_income,
    total_income (same currency units) and optional roe (fraction, 0.15 = 15%)."""
    df = pd.read_csv(path)
    df.columns = [c.strip().lower() for c in df.columns]
    df["symbol"] = df["symbol"].astype(str).str.strip().str.upper()
    return df.drop_duplicates("symbol").reset_index(drop=True)


def fetch_prices(
    symbols: Sequence[str],
    start: str,
    end: str | None = None,
    cache_path: str | Path | None = None,
    refresh: bool = False,
) -> pd.DataFrame:
    """Wide DataFrame of dividend-adjusted closes (symbols as columns). NSE symbols get .NS;
    index symbols starting with ^ are used as is. Cached to CSV."""
    cache = Path(cache_path) if cache_path else None
    wanted = list(dict.fromkeys(symbols))
    cached = pd.DataFrame()
    if cache and cache.exists():
        # always load it so a download can be merged into it, even when refreshing
        cached = pd.read_csv(cache, index_col=0, parse_dates=True)
        missing = [s for s in wanted if s not in cached.columns]
        stale = (
            not cached.empty
            and (pd.Timestamp.today().normalize() - cached.index.max()).days > 4
        )
        # cache must reach back to the requested start (allow a week for weekends/holidays)
        too_short = cached.empty or cached.index.min() > pd.Timestamp(start) + pd.Timedelta(days=7)
        if not refresh and not missing and not stale and not too_short:
            return cached[wanted].loc[start:end]

    import yfinance as yf  # lazy: only needed when downloading

    tickers = [s if s.startswith("^") else f"{s}.NS" for s in wanted]
    raw = yf.download(tickers, start=start, end=end, auto_adjust=True, progress=False)
    close = raw["Close"]
    if isinstance(close, pd.Series):
        close = close.to_frame(tickers[0])
    close.columns = [c.replace(".NS", "") for c in close.columns]
    close = close.dropna(how="all")
    if cache:
        cache.parent.mkdir(parents=True, exist_ok=True)
        # merge, never shrink: a short refresh must not wipe earlier history or other symbols
        merged = close.combine_first(cached) if not cached.empty else close
        merged.sort_index().to_csv(cache)
    return close[[c for c in wanted if c in close.columns]]
