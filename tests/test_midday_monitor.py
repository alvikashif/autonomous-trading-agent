import json
from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

from agent.config import AIBrainConfig
from agent.costs import CostConfig
from agent.models import Order, Side
from agent.monitor import IST, run_once
from agent.quotes import MIN_FETCH_INTERVAL, fetch_resilient_quotes, fetch_single_quote


def test_quote_anti_throttling_cache():
    # Calling fetch_single_quote multiple times within MIN_FETCH_INTERVAL should use cache
    with patch("yfinance.Ticker") as mock_yf:
        mock_fi = MagicMock()
        mock_fi.fast_info = {"last_price": 1000.0, "previous_close": 980.0}
        mock_yf.return_value = mock_fi

        p1, prev1 = fetch_single_quote("TESTSYM", force=True)
        assert p1 == 1000.0
        assert mock_yf.call_count == 1

        # Second call immediately should hit memory cache (no remote call)
        p2, prev2 = fetch_single_quote("TESTSYM", force=False)
        assert p2 == 1000.0
        assert mock_yf.call_count == 1  # count remains 1!


def test_quote_google_finance_fallback_on_429():
    with patch("yfinance.Ticker", side_effect=Exception("429 Too Many Requests")), \
         patch("agent.quotes.fetch_google_finance_quote", return_value=1250.50):
        px, prev = fetch_single_quote("FALLBACKSYM", force=True)
        assert px == 1250.50
        assert prev == 1250.50


def test_midday_stop_loss_trigger(tmp_path):
    paper_path = tmp_path / "paper.json"
    paper_path.write_text(json.dumps({"cash": 10000.0, "positions": {}}))
    status_path = tmp_path / "mon.json"
    kill_switch_path = tmp_path / "kill.json"

    ai_sig_path = tmp_path / "ai_sig.json"
    ai_sig_path.write_text(json.dumps({
        "verdict": "EXECUTE",
        "selected_trade": {
            "symbol": "SHYAMMETL",
            "entry_price": 1300.0,
            "target_price": 1404.0,
            "stop_loss": 1248.0,
            "qty": 3,
        }
    }))

    mock_broker = MagicMock()
    mock_broker.positions.return_value = {"SHYAMMETL": 3}

    now = datetime(2026, 11, 9, 11, 30, tzinfo=IST)  # Monday 11:30 AM (Market Open)

    # Price drops to 1240.0 (below stop loss 1248.0)
    quotes = lambda syms: ({"SHYAMMETL": 1240.0}, {"SHYAMMETL": 1300.0})

    st = run_once(
        paper_path=str(paper_path),
        status_path=str(status_path),
        cost_cfg=CostConfig(),
        stop_pct=0.0,
        alert_pct=0.0,
        daily_loss_limit_pct=0.5,
        act=True,
        now=now,
        quote_fn=quotes,
        ai_signals_path=str(ai_sig_path),
        broker=mock_broker,
        live_mode=True,
        kill_switch_path=str(kill_switch_path),
    )

    # Broker should place a SELL order
    assert mock_broker.place_order.call_count == 1
    order_arg = mock_broker.place_order.call_args[0][0]
    assert order_arg.symbol == "SHYAMMETL"
    assert order_arg.side == Side.SELL
    assert order_arg.qty == 3

    # ai_signals should register exit
    saved_sig = json.loads(ai_sig_path.read_text())
    assert saved_sig["selected_trade"]["exit"]["type"] == "STOP_LOSS"
    assert saved_sig["selected_trade"]["exit"]["price"] == 1240.0


def test_midday_take_profit_with_ai_trail(tmp_path):
    paper_path = tmp_path / "paper.json"
    paper_path.write_text(json.dumps({"cash": 10000.0, "positions": {}}))
    status_path = tmp_path / "mon.json"
    kill_switch_path = tmp_path / "kill.json"

    ai_sig_path = tmp_path / "ai_sig.json"
    ai_sig_path.write_text(json.dumps({
        "verdict": "EXECUTE",
        "selected_trade": {
            "symbol": "SHYAMMETL",
            "entry_price": 1300.0,
            "target_price": 1404.0,  # +8%
            "stop_loss": 1248.0,
            "qty": 3,
        }
    }))

    mock_broker = MagicMock()
    mock_broker.positions.return_value = {"SHYAMMETL": 3}
    now = datetime(2026, 11, 9, 13, 0, tzinfo=IST)  # Monday 1:00 PM

    # Price hits 1410.0 (above 1404.0)
    quotes = lambda syms: ({"SHYAMMETL": 1410.0}, {"SHYAMMETL": 1300.0})

    ai_cfg = MagicMock()
    ai_cfg.resolved_api_key.return_value = "dummy_key"

    # Mock AI Brain recommending HOLD_AND_TRAIL
    with patch("agent.ai_brain.evaluate_take_profit", return_value={
        "decision": "HOLD_AND_TRAIL",
        "reason": "Strong volume breakout, extend run",
        "revised_target_price": 1480.0,
        "trailing_stop_price": 1360.0,
    }):
        st = run_once(
            paper_path=str(paper_path),
            status_path=str(status_path),
            cost_cfg=CostConfig(),
            stop_pct=0.0,
            alert_pct=0.0,
            daily_loss_limit_pct=0.5,
            act=True,
            now=now,
            quote_fn=quotes,
            ai_signals_path=str(ai_sig_path),
            broker=mock_broker,
            ai_cfg=ai_cfg,
            live_mode=True,
            kill_switch_path=str(kill_switch_path),
        )

    # Broker should NOT sell because AI advised trail
    assert mock_broker.place_order.call_count == 0

    # ai_signals should update target and stop levels
    saved_sig = json.loads(ai_sig_path.read_text())
    assert saved_sig["selected_trade"]["target_price"] == 1480.0
    assert saved_sig["selected_trade"]["stop_loss"] == 1360.0
    assert "exit" not in saved_sig["selected_trade"]


def test_midday_take_profit_with_ai_exit(tmp_path):
    paper_path = tmp_path / "paper.json"
    paper_path.write_text(json.dumps({"cash": 10000.0, "positions": {}}))
    status_path = tmp_path / "mon.json"
    kill_switch_path = tmp_path / "kill.json"

    ai_sig_path = tmp_path / "ai_sig.json"
    ai_sig_path.write_text(json.dumps({
        "verdict": "EXECUTE",
        "selected_trade": {
            "symbol": "SHYAMMETL",
            "entry_price": 1300.0,
            "target_price": 1404.0,
            "stop_loss": 1248.0,
            "qty": 3,
        }
    }))

    mock_broker = MagicMock()
    mock_broker.positions.return_value = {"SHYAMMETL": 3}
    now = datetime(2026, 11, 9, 14, 0, tzinfo=IST)

    quotes = lambda syms: ({"SHYAMMETL": 1410.0}, {"SHYAMMETL": 1300.0})

    ai_cfg = MagicMock()
    ai_cfg.resolved_api_key.return_value = "dummy_key"

    # Mock AI Brain recommending TAKE_PROFIT
    with patch("agent.ai_brain.evaluate_take_profit", return_value={
        "decision": "TAKE_PROFIT",
        "reason": "RSI reached 88, lock in gain",
    }):
        st = run_once(
            paper_path=str(paper_path),
            status_path=str(status_path),
            cost_cfg=CostConfig(),
            stop_pct=0.0,
            alert_pct=0.0,
            daily_loss_limit_pct=0.5,
            act=True,
            now=now,
            quote_fn=quotes,
            ai_signals_path=str(ai_sig_path),
            broker=mock_broker,
            ai_cfg=ai_cfg,
            live_mode=True,
            kill_switch_path=str(kill_switch_path),
        )

    # Broker should place SELL order
    assert mock_broker.place_order.call_count == 1
    assert mock_broker.place_order.call_args[0][0].side == Side.SELL

    saved_sig = json.loads(ai_sig_path.read_text())
    assert saved_sig["selected_trade"]["exit"]["type"] == "TAKE_PROFIT"
    assert saved_sig["selected_trade"]["exit"]["price"] == 1410.0


def test_midday_groww_gtt_floor_stop_executed_detection_and_notification(tmp_path):
    """When Groww executes the GTT 7% floor stop loss on exchange, monitor detects 0 shares, marks exit, notifies, and prevents duplicate sell."""
    paper_path = tmp_path / "paper.json"
    paper_path.write_text(json.dumps({"cash": 10000.0, "positions": {"SHYAMMETL": 3}}))
    status_path = tmp_path / "mon.json"
    kill_switch_path = tmp_path / "kill.json"

    ai_sig_path = tmp_path / "ai_sig.json"
    ai_sig_path.write_text(json.dumps({
        "verdict": "EXECUTE",
        "selected_trade": {
            "symbol": "SHYAMMETL",
            "entry_price": 1320.50,
            "target_price": 1584.60,
            "stop_loss": 1267.68,
            "hard_stop_price": 1228.05,
            "gtt_trigger_price": 1228.05,
            "gtt_order_id": "GTT_9999",
            "qty": 3,
        }
    }))

    # Groww Demat returns 0 shares because GTT floor stop loss executed
    mock_broker = MagicMock()
    mock_broker.positions.return_value = {}

    now = datetime(2026, 11, 9, 12, 0, tzinfo=IST)
    quotes = lambda syms: ({"SHYAMMETL": 1225.0}, {"SHYAMMETL": 1320.50})

    st = run_once(
        paper_path=str(paper_path),
        status_path=str(status_path),
        cost_cfg=CostConfig(),
        stop_pct=0.0,
        alert_pct=0.0,
        daily_loss_limit_pct=0.5,
        act=True,
        now=now,
        quote_fn=quotes,
        ai_signals_path=str(ai_sig_path),
        broker=mock_broker,
        live_mode=True,
        kill_switch_path=str(kill_switch_path),
    )

    # 1. Broker should NOT place any duplicate order (conflict prevented)
    assert mock_broker.place_order.call_count == 0

    # 2. Exit must be registered as GROWW_GTT_STOP_LOSS
    saved_sig = json.loads(ai_sig_path.read_text())
    assert saved_sig["selected_trade"]["exit"]["type"] == "GROWW_GTT_STOP_LOSS"
    assert saved_sig["selected_trade"]["exit"]["price"] == 1228.05

    # 3. Notification should be saved in notifications.json
    notif_file = tmp_path / "notifications.json"
    assert notif_file.is_file()
    notifs = json.loads(notif_file.read_text())
    assert any("GROWW GTT FLOOR STOP-LOSS EXECUTED" in n["title"] for n in notifs)

