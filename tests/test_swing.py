import numpy as np
import pandas as pd

from agent.costs import CostConfig
from agent.swing import SwingParams, run_swing


def _prices(path):
    idx = pd.bdate_range("2024-01-01", periods=len(path))
    return pd.DataFrame({"AAA": path}, index=idx)


def test_breakout_hits_target_and_pays_costs():
    flat = [100.0] * 80
    up = [101, 103, 106, 110, 114, 118, 122, 126, 130, 134]
    res = run_swing(_prices(flat + up + [134] * 5), ["AAA"], SwingParams(capital=5000, max_positions=1), CostConfig())
    t = res.trades
    assert len(t) == 1 and t.iloc[0].reason == "target"
    assert t.iloc[0].net < t.iloc[0].gross       # charges and slippage always reduce the result
    assert res.total_costs > 0


def test_no_breakout_means_no_trades():
    path = 100 + np.sin(np.arange(200) / 5)      # range-bound: never closes above its trend and 20d high together for long
    res = run_swing(_prices(list(path)), ["AAA"], SwingParams(trend_days=50, lookback=20, target_pct=0.5), CostConfig())
    assert (res.trades.reason != "target").all()


def test_stop_exits_on_drop():
    flat = [100.0] * 80
    path = flat + [101, 103, 100, 95, 90, 90, 90, 90, 90, 90, 90, 90]
    res = run_swing(_prices(path), ["AAA"], SwingParams(capital=5000, max_positions=1), CostConfig())
    assert len(res.trades) >= 1 and (res.trades.reason == "stop").any()


def test_swing_status_snapshot_and_dashboard_summary(tmp_path):
    import json

    from agent.config import SwingConfig
    from agent.dashboard import build_summary
    from agent.swing import params_from_config, swing_status

    flat = [100.0] * 80
    up = [101, 103, 106, 110, 114]            # breakout bought, still open at the end
    px = _prices(flat + up)
    sc = SwingConfig(start_date="2024-01-01", capital=5000, max_positions=1)
    res = run_swing(px, ["AAA"], params_from_config(sc), CostConfig(), start=sc.start_date)
    st = swing_status(res, sc, px["AAA"])
    assert st["capital"] == 5000 and st["as_of"] == str(px.index[-1].date())
    assert st["holdings"] and st["holdings"][0]["symbol"] == "AAA"
    p = tmp_path / "swing.json"
    p.write_text(json.dumps(st, default=str))
    summary = build_summary(str(tmp_path / "none.json"), str(tmp_path / "none2.json"), str(tmp_path / "s.csv"),
                            str(tmp_path / "p.csv"), 3, str(p))
    assert summary["swing"]["rule"] == st["rule"]
