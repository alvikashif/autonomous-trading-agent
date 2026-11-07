"""Shariah screening. Fails closed: missing data means NOT compliant.

Two screens:
1. Business: exclude industries by keyword plus a manual symbol blocklist.
   Industry labels are coarse (e.g. tobacco sits inside FMCG), so the NSE Shariah
   index list is the base universe and `excluded_symbols` covers known gaps.
2. Financial ratios (AAOIFI-style, thresholds configurable):
   debt / market cap, cash+interest securities / market cap, interest income / total income.

This is a mechanical filter, not a fatwa. Confirm the standard with your scholar.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

import pandas as pd

EXCLUDED_INDUSTRY_KEYWORDS = (
    "financial",
    "bank",
    "insurance",
    "nbfc",
    "finance",
    "alcohol",
    "liquor",
    "brewer",
    "distiller",
    "tobacco",
    "cigarette",
    "gambling",
    "casino",
    "betting",
    "defence",
    "defense",
    "weapon",
    "entertainment",
    "wineries",
    "credit services",
    "mortgage",
    "capital markets",
    "private sector bank",
    "public sector bank",
)

REQUIRED_FUNDAMENTALS = (
    "market_cap",
    "total_debt",
    "cash_and_securities",
    "interest_income",
    "total_income",
)


@dataclass(frozen=True)
class Thresholds:
    max_debt_to_mcap: float = 0.33
    max_cash_to_mcap: float = 0.33
    max_interest_income_ratio: float = 0.05


@dataclass
class ScreenResult:
    symbol: str
    compliant: bool
    reasons: list[str] = field(default_factory=list)
    impure_ratio: float = 0.0  # interest income / total income, used for dividend purification


def business_violation(industry: object) -> str | None:
    if industry is None or pd.isna(industry) or not str(industry).strip():
        return "missing industry"
    low = str(industry).lower()
    for kw in EXCLUDED_INDUSTRY_KEYWORDS:
        if kw in low:
            return f"excluded business: {kw}"
    return None


def screen_row(
    row: pd.Series,
    th: Thresholds,
    excluded_symbols: Iterable[str] = (),
) -> ScreenResult:
    symbol = str(row["symbol"]).strip().upper()
    reasons: list[str] = []

    if symbol in {s.upper() for s in excluded_symbols}:
        reasons.append("manually excluded")

    violation = business_violation(row.get("industry"))
    if violation:
        reasons.append(violation)
    # Finer labels from Yahoo (when present) catch what coarse NSE labels hide,
    # e.g. tobacco inside FMCG. If the column exists but is empty, fail closed.
    for col in ("yahoo_industry", "yahoo_sector"):
        if col in row.index:
            v = row[col]
            if v is None or pd.isna(v) or not str(v).strip():
                reasons.append(f"missing {col}")
            else:
                low = str(v).lower()
                hit = next((kw for kw in EXCLUDED_INDUSTRY_KEYWORDS if kw in low), None)
                if hit:
                    reasons.append(f"excluded business ({col}={v}): {hit}")

    values: dict[str, float] = {}
    for col in REQUIRED_FUNDAMENTALS:
        v = row.get(col)
        if v is None or pd.isna(v):
            reasons.append(f"missing {col}")
        else:
            values[col] = float(v)

    impure = 0.0
    if len(values) == len(REQUIRED_FUNDAMENTALS):
        mcap, income = values["market_cap"], values["total_income"]
        if mcap <= 0:
            reasons.append("non-positive market cap")
        else:
            debt_ratio = values["total_debt"] / mcap
            cash_ratio = values["cash_and_securities"] / mcap
            if debt_ratio > th.max_debt_to_mcap:
                reasons.append(f"debt/mcap {debt_ratio:.2f} > {th.max_debt_to_mcap}")
            if cash_ratio > th.max_cash_to_mcap:
                reasons.append(f"cash/mcap {cash_ratio:.2f} > {th.max_cash_to_mcap}")
        if income <= 0:
            reasons.append("non-positive total income")
        else:
            impure = values["interest_income"] / income
            if impure > th.max_interest_income_ratio:
                reasons.append(
                    f"interest income {impure:.3f} > {th.max_interest_income_ratio}"
                )

    return ScreenResult(symbol, not reasons, reasons, impure)


def screen_universe(
    df: pd.DataFrame,
    th: Thresholds,
    excluded_symbols: Iterable[str] = (),
) -> pd.DataFrame:
    """Return one row per symbol: symbol, industry, compliant, impure_ratio, reasons."""
    excluded = list(excluded_symbols)
    rows = []
    for _, row in df.iterrows():
        res = screen_row(row, th, excluded)
        rows.append(
            {
                "symbol": res.symbol,
                "industry": row.get("industry"),
                "compliant": res.compliant,
                "impure_ratio": res.impure_ratio,
                "reasons": "; ".join(res.reasons),
            }
        )
    return pd.DataFrame(rows, columns=["symbol", "industry", "compliant", "impure_ratio", "reasons"])


def purify_dividend(dividend_received: float, impure_ratio: float) -> float:
    """Amount of a dividend to donate to charity (interest-derived share)."""
    return max(0.0, dividend_received) * max(0.0, impure_ratio)
