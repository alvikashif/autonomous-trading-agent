"""Fetch fundamentals for the Shariah screen from Yahoo Finance (via yfinance).

Free but imperfect data. Rules:
- Missing numbers stay NaN, so the screen fails closed (stock excluded).
- Interest income is the weak spot: Yahoo often omits the row for non-financial companies.
  `assume_zero_interest=True` treats an ABSENT row as 0 (opt-in, recorded in the
  `interest_source` column). A present-but-NaN value is never treated as 0.
- cash_and_securities uses cash + short-term investments, a conservative proxy for
  interest-bearing deposits and securities.
Spot-check a few results against company filings before trusting them.
"""
from __future__ import annotations

import math
import time
from pathlib import Path
from typing import Iterable

import pandas as pd

COLUMNS = [
    "symbol", "market_cap", "total_debt", "cash_and_securities",
    "interest_income", "total_income", "roe", "roe_source", "interest_source",
    "yahoo_industry", "yahoo_sector", "fetched_at",
]

_NET_INCOME = ("Net Income Common Stockholders", "Net Income")
_EQUITY = ("Stockholders Equity", "Common Stock Equity", "Total Equity Gross Minority Interest")

_DEBT = ("Total Debt",)
_CASH = ("Cash Cash Equivalents And Short Term Investments", "Cash And Cash Equivalents")
_INTEREST = ("Interest Income", "Interest Income Non Operating")
_REVENUE = ("Total Revenue", "Operating Revenue")


def _latest(df: pd.DataFrame | None, names: Iterable[str]) -> tuple[float, bool]:
    """Return (value, row_present). Newest non-NaN value of the first matching row."""
    if df is None or df.empty:
        return float("nan"), False
    for name in names:
        if name in df.index:
            s = df.loc[name].dropna()
            if len(s):
                s = s.sort_index(ascending=False)
                return float(s.iloc[0]), True
            return float("nan"), True
    return float("nan"), False


def parse_fundamentals(
    symbol: str,
    info: dict,
    balance: pd.DataFrame | None,
    income: pd.DataFrame | None,
    assume_zero_interest: bool = False,
    fx: float | None = 1.0,
) -> dict:
    """`fx` converts statement currency into market-cap currency (1.0 if identical).
    fx=None means conversion was needed but unavailable: statement values become NaN."""
    k = float("nan") if fx is None else fx
    debt, _ = _latest(balance, _DEBT)
    cash, _ = _latest(balance, _CASH)
    revenue, _ = _latest(income, _REVENUE)
    interest, interest_present = _latest(income, _INTEREST)
    debt, cash, revenue, interest = debt * k, cash * k, revenue * k, interest * k
    source = "reported"
    if fx is None:
        source = "missing"
    elif not interest_present:
        if assume_zero_interest:
            interest, source = 0.0, "assumed_zero"
        else:
            source = "missing"
    elif math.isnan(interest):
        source = "missing"

    def num(key: str) -> float:
        v = info.get(key)
        return float(v) if isinstance(v, (int, float)) and math.isfinite(v) else float("nan")

    roe = num("returnOnEquity")
    roe_source = "yahoo"
    if math.isnan(roe):
        # Same-statement ratio, so currency cancels out. Needs positive equity.
        net_income, _ = _latest(income, _NET_INCOME)
        equity, _ = _latest(balance, _EQUITY)
        if math.isfinite(net_income) and math.isfinite(equity) and equity > 0:
            roe, roe_source = net_income / equity, "computed"
        else:
            roe_source = "missing"

    return {
        "symbol": symbol.upper(),
        "market_cap": num("marketCap"),
        "total_debt": debt,
        "cash_and_securities": cash,
        "interest_income": interest,
        "total_income": revenue,
        "roe": roe,
        "roe_source": roe_source,
        "interest_source": source,
        "yahoo_industry": info.get("industry"),
        "yahoo_sector": info.get("sector"),
        "fetched_at": pd.Timestamp.today().strftime("%Y-%m-%d"),
    }


def fetch_one(symbol: str, assume_zero_interest: bool = False) -> dict:
    import yfinance as yf  # lazy

    t = yf.Ticker(f"{symbol}.NS")
    info = t.info or {}
    income = t.financials
    stmt_rev, _ = _latest(income, _REVENUE)
    fx = resolve_fx(info, stmt_rev, _fx_rate(info))
    return parse_fundamentals(symbol, info, t.balance_sheet, income, assume_zero_interest, fx)


def resolve_fx(info: dict, stmt_revenue: float, rate: float | None) -> float | None:
    """Factor converting statement values to market-cap currency.

    Yahoo's `financialCurrency` flag is unreliable (e.g. HCLTECH is flagged USD but its
    statements are INR; INFY statements really are USD). So infer the statements' currency
    by comparing statement revenue with Yahoo's own `totalRevenue` (in the flagged currency):
    ratio near 1 means statements are in the flagged currency, ratio near `rate` means they
    are already in market currency. Anything else returns None (fail closed).
    """
    fin, mkt = info.get("financialCurrency"), info.get("currency")
    if fin and mkt and fin == mkt:
        return 1.0
    ref = info.get("totalRevenue")
    if not rate or not fin or not mkt:
        return None
    if not isinstance(ref, (int, float)) or not math.isfinite(ref) or ref <= 0:
        return None
    if not math.isfinite(stmt_revenue) or stmt_revenue <= 0:
        return None
    ratio = stmt_revenue / ref
    if 0.5 <= ratio <= 2.0:
        return rate  # statements are in the flagged (foreign) currency
    if 0.5 <= ratio / rate <= 2.0:
        return 1.0   # statements already in market currency
    return None


_FX_CACHE: dict[str, float | None] = {}


def _fx_rate(info: dict) -> float | None:
    """Statement currency to market currency. Yahoo reports some Indian ADR-style
    companies (e.g. INFY) in USD while market cap is INR."""
    import yfinance as yf

    fin, mkt = info.get("financialCurrency"), info.get("currency")
    if not fin or not mkt:
        return None  # unknown currencies: cannot trust ratios, fail closed
    if fin == mkt:
        return 1.0
    pair = f"{fin}{mkt}=X"
    if pair not in _FX_CACHE:
        try:
            rate = yf.Ticker(pair).fast_info["last_price"]
            _FX_CACHE[pair] = float(rate) if rate and math.isfinite(rate) and rate > 0 else None
        except Exception:
            _FX_CACHE[pair] = None
    return _FX_CACHE[pair]


def fetch_fundamentals(
    symbols: Iterable[str],
    out_path: str | Path,
    assume_zero_interest: bool = False,
    max_age_days: int = 90,
    delay_s: float = 0.5,
    progress=print,
    refetch_missing_roe: bool = False,
) -> pd.DataFrame:
    """Fetch for each symbol, merging into out_path. Rows younger than max_age_days are kept
    (resumable). Failures are logged and skipped, leaving no row, so the screen excludes them."""
    out = Path(out_path)
    existing = pd.DataFrame(columns=COLUMNS)
    if out.exists():
        existing = pd.read_csv(out)
        for c in COLUMNS:
            if c not in existing.columns:
                existing[c] = float("nan")
    cutoff = pd.Timestamp.today().normalize() - pd.Timedelta(days=max_age_days)
    fresh = set()
    if not existing.empty:
        ts = pd.to_datetime(existing["fetched_at"], errors="coerce")
        fresh = set(existing.loc[ts >= cutoff, "symbol"])
        if refetch_missing_roe:
            missing = existing["roe"].isna() & (existing["roe_source"] != "missing")
            # also rows saved before roe_source existed (NaN source) with no ROE
            missing |= existing["roe"].isna() & existing["roe_source"].isna()
            fresh -= set(existing.loc[missing, "symbol"])

    rows, failed = [], []
    todo = [s for s in dict.fromkeys(x.upper() for x in symbols) if s not in fresh]
    for i, sym in enumerate(todo, 1):
        try:
            rows.append(fetch_one(sym, assume_zero_interest))
        except Exception as exc:  # network, delisted, rate limit
            failed.append((sym, str(exc)[:80]))
        if i % 25 == 0:
            progress(f"  {i}/{len(todo)} fetched")
        time.sleep(delay_s)

    merged = pd.concat([existing[~existing["symbol"].isin([r["symbol"] for r in rows])], pd.DataFrame(rows, columns=COLUMNS)])
    merged = merged[COLUMNS].sort_values("symbol")
    out.parent.mkdir(parents=True, exist_ok=True)
    merged.to_csv(out, index=False)
    progress(f"fetched {len(rows)}, skipped fresh {len(fresh)}, failed {len(failed)} -> {out}")
    for sym, why in failed[:10]:
        progress(f"  FAIL {sym}: {why}")
    return merged
