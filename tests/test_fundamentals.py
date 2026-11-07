import math

import pandas as pd

from agent.fundamentals import parse_fundamentals

D1, D2 = pd.Timestamp("2025-03-31"), pd.Timestamp("2024-03-31")


def frames(interest=None, with_interest_row=True):
    bal = pd.DataFrame(
        {D1: [100.0, 50.0], D2: [90.0, 40.0]},
        index=["Total Debt", "Cash Cash Equivalents And Short Term Investments"],
    )
    rows = {"Total Revenue": [1000.0, 900.0]}
    if with_interest_row:
        rows["Interest Income"] = interest if interest is not None else [10.0, 8.0]
    inc = pd.DataFrame(rows, index=[D1, D2]).T
    return bal, inc


INFO = {"marketCap": 5000.0, "returnOnEquity": 0.2}


def test_parses_latest_values():
    bal, inc = frames()
    r = parse_fundamentals("abc", INFO, bal, inc)
    assert r["symbol"] == "ABC"
    assert (r["market_cap"], r["total_debt"], r["cash_and_securities"]) == (5000.0, 100.0, 50.0)
    assert (r["interest_income"], r["total_income"], r["roe"]) == (10.0, 1000.0, 0.2)
    assert r["interest_source"] == "reported"


def test_absent_interest_row_fails_closed_by_default():
    bal, inc = frames(with_interest_row=False)
    r = parse_fundamentals("ABC", INFO, bal, inc)
    assert math.isnan(r["interest_income"]) and r["interest_source"] == "missing"


def test_absent_interest_row_assumed_zero_when_opted_in():
    bal, inc = frames(with_interest_row=False)
    r = parse_fundamentals("ABC", INFO, bal, inc, assume_zero_interest=True)
    assert r["interest_income"] == 0.0 and r["interest_source"] == "assumed_zero"


def test_present_but_nan_interest_never_assumed_zero():
    bal, inc = frames(interest=[float("nan"), float("nan")])
    r = parse_fundamentals("ABC", INFO, bal, inc, assume_zero_interest=True)
    assert math.isnan(r["interest_income"]) and r["interest_source"] == "missing"


def test_fx_conversion_applied_to_statement_values_only():
    bal, inc = frames()
    r = parse_fundamentals("ABC", INFO, bal, inc, fx=90.0)
    assert r["total_debt"] == 100.0 * 90 and r["total_income"] == 1000.0 * 90
    assert r["interest_income"] == 10.0 * 90 and r["market_cap"] == 5000.0


def test_fx_unavailable_fails_closed():
    bal, inc = frames()
    r = parse_fundamentals("ABC", INFO, bal, inc, assume_zero_interest=True, fx=None)
    assert math.isnan(r["total_debt"]) and math.isnan(r["interest_income"])
    assert r["interest_source"] == "missing"


def test_roe_computed_when_yahoo_missing():
    bal = pd.DataFrame({D1: [400.0]}, index=["Stockholders Equity"])
    inc = pd.DataFrame({D1: [80.0, 1000.0]}, index=["Net Income", "Total Revenue"])
    r = parse_fundamentals("ABC", {"marketCap": 1.0}, bal, inc)
    assert r["roe"] == 0.2 and r["roe_source"] == "computed"


def test_roe_not_computed_for_non_positive_equity():
    bal = pd.DataFrame({D1: [-5.0]}, index=["Stockholders Equity"])
    inc = pd.DataFrame({D1: [80.0]}, index=["Net Income"])
    r = parse_fundamentals("ABC", {}, bal, inc)
    assert math.isnan(r["roe"]) and r["roe_source"] == "missing"


def test_yahoo_roe_preferred():
    bal = pd.DataFrame({D1: [400.0]}, index=["Stockholders Equity"])
    inc = pd.DataFrame({D1: [80.0]}, index=["Net Income"])
    r = parse_fundamentals("ABC", {"returnOnEquity": 0.5}, bal, inc)
    assert r["roe"] == 0.5 and r["roe_source"] == "yahoo"


def test_resolve_fx_detects_statement_currency():
    from agent.fundamentals import resolve_fx

    info = {"financialCurrency": "USD", "currency": "INR", "totalRevenue": 20e9}
    assert resolve_fx(info, 19e9, 90.0) == 90.0          # INFY: statements really USD
    assert resolve_fx(info, 19e9 * 90, 90.0) == 1.0      # HCLTECH: flagged USD, statements INR
    assert resolve_fx(info, 5e15, 90.0) is None          # matches neither: fail closed
    assert resolve_fx({"financialCurrency": "INR", "currency": "INR"}, 1.0, None) == 1.0
    assert resolve_fx({"currency": "INR"}, 1.0, 90.0) is None


def test_empty_frames_give_nan_not_crash():
    r = parse_fundamentals("ABC", {}, None, None)
    assert math.isnan(r["market_cap"]) and math.isnan(r["total_debt"])
