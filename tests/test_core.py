import numpy as np
import pandas as pd
import pytest

from agent.backtest import run_backtest
from agent.broker import InsufficientFunds
from agent.costs import CostConfig, trade_cost
from agent.models import Order, Side
from agent.paper import PaperBroker
from agent.portfolio import plan_orders
from agent.risk import RiskLimits, check_orders
from agent.shariah import Thresholds, purify_dividend, screen_row
from agent.strategy import StrategyConfig

TH = Thresholds()


def row(**kw):
    base = dict(symbol="ABC", industry="Information Technology", market_cap=100.0,
                total_debt=10.0, cash_and_securities=5.0, interest_income=1.0, total_income=100.0)
    base.update(kw)
    return pd.Series(base)


def test_compliant_passes():
    assert screen_row(row(), TH).compliant


def test_high_debt_fails():
    r = screen_row(row(total_debt=40.0), TH)
    assert not r.compliant and any("debt" in x for x in r.reasons)


def test_interest_income_fails():
    assert not screen_row(row(interest_income=10.0), TH).compliant


def test_bank_industry_fails():
    assert not screen_row(row(industry="Financial Services"), TH).compliant


def test_yahoo_industry_catches_hidden_tobacco():
    r = screen_row(row(industry="Fast Moving Consumer Goods", yahoo_industry="Tobacco",
                       yahoo_sector="Consumer Defensive"), TH)
    assert not r.compliant and any("tobacco" in x for x in r.reasons)


def test_yahoo_industry_missing_fails_closed_when_column_present():
    r = screen_row(row(yahoo_industry=np.nan, yahoo_sector="Technology"), TH)
    assert not r.compliant and any("yahoo_industry" in x for x in r.reasons)


def test_yahoo_clean_industry_passes():
    assert screen_row(row(yahoo_industry="Information Technology Services", yahoo_sector="Technology"), TH).compliant


def test_missing_data_fails_closed():
    r = row()
    r["total_debt"] = np.nan
    assert not screen_row(r, TH).compliant


def test_manual_exclusion():
    assert not screen_row(row(symbol="ITC"), TH, ["itc"]).compliant


def test_purification():
    assert purify_dividend(1000, 0.02) == pytest.approx(20)


def test_costs_sell_includes_dp_and_buy_includes_stamp():
    cfg = CostConfig()
    assert trade_cost(Side.SELL, 5000, cfg) > trade_cost(Side.BUY, 5000, cfg) - 5000 * cfg.stamp_buy_pct
    assert trade_cost(Side.SELL, 0, cfg) == 0


def test_paper_broker_no_leverage_no_short():
    b = PaperBroker(None, CostConfig())
    b.deposit(1000)
    with pytest.raises(InsufficientFunds):
        b.place_order(Order("X", Side.BUY, 100, 100.0))
    with pytest.raises(ValueError):
        b.place_order(Order("X", Side.SELL, 1, 100.0))


def test_risk_rejects_position_cap_and_halt():
    px = {"A": 100.0}
    big = Order("A", Side.BUY, 50, 100.0)  # 5000 of 10000 equity = 50%
    ok, bad = check_orders([big], 10000, {}, px, {}, 10000, RiskLimits(), CostConfig())
    assert not ok and "position cap" in bad[0][1]
    small = Order("A", Side.BUY, 10, 100.0)
    ok, bad = check_orders([small], 10000, {}, px, {}, 10000, RiskLimits(), CostConfig(), day_pnl_pct=-0.05)
    assert not ok and "daily loss" in bad[0][1]


def test_risk_rejects_short_sell():
    ok, bad = check_orders([Order("A", Side.SELL, 1, 100.0)], 0, {}, {"A": 100.0}, {}, 0, RiskLimits(), CostConfig())
    assert not ok


def test_plan_orders_exits_non_target_and_skips_tiny():
    px = {"A": 100.0, "B": 100.0, "C": 100.0}
    orders = plan_orders({"A": 0.5}, {"B": 5, "C": 1}, px, 10000, min_trade_value=1500)
    syms = {(o.symbol, o.side) for o in orders}
    assert ("B", Side.SELL) in syms and ("C", Side.SELL) in syms  # exits always happen
    assert orders[0].side == Side.SELL  # sells first
    assert any(o.symbol == "A" and o.side == Side.BUY for o in orders)


def test_size_mode_picks_largest_with_price_and_requires_size():
    from agent.strategy import target_weights

    idx = pd.bdate_range("2020-01-01", periods=30)
    prices = pd.DataFrame({"A": 1.0, "B": 1.0, "C": 1.0, "D": np.nan}, index=idx)
    size = pd.Series({"A": 10.0, "B": 30.0, "C": 20.0, "D": 99.0})
    cfg = StrategyConfig(top_n=2, mode="size", use_trend_filter=False)
    w = target_weights(prices, prices["A"], idx[-1], list(prices.columns), cfg, 0.6, None, size)
    assert set(w) == {"B", "C"}  # D is biggest but has no price
    with pytest.raises(ValueError):
        target_weights(prices, prices["A"], idx[-1], list(prices.columns), cfg, 0.6, None, None)


def _scaled_setup():
    from agent.strategy import target_weights

    idx = pd.bdate_range("2020-01-01", periods=30)
    # 8 stocks, size rank A (largest) .. H; share prices: A is expensive
    px = {"A": 9000.0, "B": 400.0, "C": 300.0, "D": 250.0, "E": 200.0, "F": 150.0, "G": 120.0, "H": 100.0}
    prices = pd.DataFrame({k: v for k, v in px.items()}, index=idx)
    size = pd.Series({k: 100.0 - i for i, k in enumerate(px)})
    cfg = StrategyConfig(top_n=8, mode="size", use_trend_filter=False, scale_top_n=True,
                         min_names=5, min_position_value=1500.0)
    def call(equity, held=()):
        return target_weights(prices, prices["B"], idx[-1], list(px), cfg, 0.2, None, size, equity, held)
    return call


def test_scaled_small_portfolio_skips_unaffordable_and_keeps_five_names():
    w = _scaled_setup()(5000.0)
    assert "A" not in w                      # 9000/share cannot fit a 5000 account
    assert len(w) == 5 and all(abs(v - 0.2) < 1e-9 for v in w.values())


def test_scaled_large_portfolio_uses_full_top_n_including_big_names():
    w = _scaled_setup()(200000.0)
    assert len(w) == 8 and "A" in w


def test_scaled_keeps_held_names_when_growing():
    call = _scaled_setup()
    small = call(5000.0)
    grown = call(12000.0, held=small.keys())  # N grows to 7 (12000*0.99//1500 = 7)
    assert set(small) <= set(grown)


def test_quarterly_rebalance_trades_less_than_monthly():
    idx = pd.bdate_range("2020-01-01", periods=700)
    rng = np.random.default_rng(1)
    cols = {f"S{i}": 100 * np.cumprod(1 + 0.0006 + rng.normal(0, 0.012, len(idx))) for i in range(8)}
    prices = pd.DataFrame(cols, index=idx)
    bench = prices.mean(axis=1)
    def run(m):
        return run_backtest(prices, bench, list(cols), {}, StrategyConfig(top_n=4, use_trend_filter=False),
                            RiskLimits(max_position_pct=0.5), CostConfig(), 5000, 500, 0.01, rebalance_months=m)
    monthly, quarterly = run(1), run(3)
    assert quarterly.n_trades < monthly.n_trades
    assert quarterly.total_contributed == monthly.total_contributed


def test_backtest_smoke_uptrend_picks_winner():
    idx = pd.bdate_range("2020-01-01", periods=600)
    rng = np.random.default_rng(0)
    up = 100 * np.cumprod(1 + 0.0012 + rng.normal(0, 0.005, len(idx)))
    flat = 100 * np.cumprod(1 + rng.normal(0, 0.005, len(idx)))
    prices = pd.DataFrame({"UP": up, "FLAT": flat}, index=idx)
    bench = pd.Series(up, index=idx)
    res = run_backtest(
        prices, bench, ["UP", "FLAT"], {}, StrategyConfig(top_n=2), RiskLimits(max_position_pct=0.6),
        CostConfig(), 5000, 500, 0.01,
    )
    assert res.total_contributed > 0
    assert res.n_trades > 0
    assert set(res.universe_metrics) == {"cagr", "max_drawdown", "sharpe"}
    assert len(res.universe_returns) == len(prices)
    assert res.equity.iloc[-1] > 0
    assert res.total_costs >= 0


def test_price_cache_merge_never_shrinks(tmp_path, monkeypatch):
    import sys, types
    from agent import data

    cache = tmp_path / "p.csv"
    old_idx = pd.bdate_range("2019-01-01", periods=10)
    pd.DataFrame({"AAA": 1.0, "BBB": 2.0}, index=old_idx).to_csv(cache)
    new_idx = pd.bdate_range(pd.Timestamp.today().normalize() - pd.Timedelta(days=5), periods=3)
    fake = types.SimpleNamespace(download=lambda *a, **k: pd.DataFrame(
        {("Close", "AAA.NS"): 5.0}, index=new_idx).pipe(lambda d: d.set_axis(pd.MultiIndex.from_tuples(d.columns), axis=1)))
    monkeypatch.setitem(sys.modules, "yfinance", fake)
    data.fetch_prices(["AAA"], "2019-01-01", cache_path=cache, refresh=True)
    merged = pd.read_csv(cache, index_col=0, parse_dates=True)
    assert merged.index.min() == old_idx[0] and "BBB" in merged.columns and len(merged) == 13


def test_paper_state_deposit_guard_and_rebalance_cadence(tmp_path):
    path = tmp_path / "s.json"
    b = PaperBroker(path, CostConfig())
    assert b.rebalance_due("2026-11-06", 3)             # never rebalanced
    b.deposit(5000, "2026-11-06")
    b.mark_rebalanced("2026-11-06")
    b.save()
    b2 = PaperBroker(path, CostConfig())                # reload from disk
    assert b2.deposited_in_month("2026-11-20") and not b2.deposited_in_month("2026-12-01")
    assert b2.next_rebalance(3) == "2027-02-06"
    assert not b2.rebalance_due("2027-02-05", 3) and b2.rebalance_due("2027-02-06", 3)


def test_legacy_state_without_new_fields_is_backfilled(tmp_path):
    import json

    path = tmp_path / "old.json"
    path.write_text(json.dumps({"cash": 10.0, "positions": {"X": 1}, "total_costs": 1.0,
                                "trades": [{"ts": "2026-11-06", "symbol": "X", "side": "BUY",
                                            "qty": 1, "fill_price": 1.0, "cost": 1.0}]}))
    b = PaperBroker(path, CostConfig())
    assert b.last_rebalance == "2026-11-06" and b.deposited_in_month("2026-11-30")


def test_stop_loss_sells_crashing_stock_and_does_not_rebuy_until_rebalance():
    idx = pd.bdate_range("2020-01-01", periods=120)
    a = np.full(len(idx), 100.0)
    a[40:100] = np.linspace(100.0, 40.0, 60)    # A bleeds 60% over 60 days, then stays flat
    a[100:] = 40.0
    b = np.full(len(idx), 100.0)
    prices = pd.DataFrame({"A": a, "B": b}, index=idx)
    kw = dict(prices=prices, benchmark=pd.Series(b, index=idx), eligible=["A", "B"], sectors={},
              strat=StrategyConfig(top_n=2, use_trend_filter=False, mode="size"),
              limits=RiskLimits(max_position_pct=0.6), costs=CostConfig(),
              monthly_contribution=50000, min_trade_value=500, cash_buffer_pct=0.01,
              size=pd.Series({"A": 2.0, "B": 1.0}), rebalance_months=12)
    no_stop = run_backtest(**kw)
    with_stop = run_backtest(**kw, stop_loss_pct=0.15)
    assert with_stop.n_stops >= 1 and no_stop.n_stops == 0
    assert with_stop.equity.iloc[-1] > no_stop.equity.iloc[-1]  # sold part-way down, so lost less
