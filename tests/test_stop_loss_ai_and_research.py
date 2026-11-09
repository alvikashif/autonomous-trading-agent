import json
from unittest.mock import MagicMock, patch
from datetime import datetime

import pytest

from agent.config import AIBrainConfig
from agent.costs import CostConfig
from agent.models import Side
from agent.monitor import IST, run_once
from agent.research import fetch_company_deep_profile, fetch_company_news, get_cached_research
from agent.ai_brain import build_stop_loss_prompt, evaluate_stop_loss


def test_build_stop_loss_prompt():
    holding = {"symbol": "SHYAMMETL", "entry_price": 1300.0, "current_price": 1245.0, "stop_loss": 1248.0}
    technicals = {"sma_50": 1220.0, "sma_200": 1100.0, "rsi_14": 48.5, "high_20d": 1350.0}
    research = {
        "profile": {
            "qoq_profit_growth_pct": 43.0,
            "yoy_profit_growth_pct": 96.1,
            "operating_margin_pct": 12.0,
            "annual_history": [{"year": "2026", "revenue_cr": 18487.5, "net_profit_cr": 1070.2}],
        },
        "news": [{"title": "Q2 Earnings Call Highlights: Profit Nearly Doubled", "source": "Yahoo Finance"}],
    }
    prompt = build_stop_loss_prompt(holding, technicals, research, hard_stop_pct=0.07)
    assert "SHYAMMETL" in prompt
    assert "43.0%" in prompt
    assert "96.1%" in prompt
    assert "HOLD_AND_GIVE_ROOM" in prompt
    assert "EXIT_CUT_LOSS" in prompt


def test_evaluate_stop_loss_hold_and_give_room(monkeypatch):
    holding = {"symbol": "SHYAMMETL", "entry_price": 1300.0, "current_price": 1245.0}
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    cfg = AIBrainConfig(provider="claude")

    mock_resp = json.dumps({
        "symbol": "SHYAMMETL",
        "decision": "HOLD_AND_GIVE_ROOM",
        "confidence_score": 8.8,
        "reason": "Normal pullback testing 50-DMA with +96% YoY earnings growth.",
        "revised_stop_loss": 1220.0,
    })

    with patch("agent.ai_brain.query_llm", return_value=mock_resp):
        res = evaluate_stop_loss(holding, {}, {}, cfg)
        assert res["decision"] == "HOLD_AND_GIVE_ROOM"
        assert res["confidence_score"] == 8.8
        assert res["revised_stop_loss"] == 1220.0


def test_evaluate_stop_loss_exit_cut_loss(monkeypatch):
    holding = {"symbol": "TESTSYM", "entry_price": 1000.0, "current_price": 955.0}
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    cfg = AIBrainConfig(provider="gemini")

    mock_resp = json.dumps({
        "symbol": "TESTSYM",
        "decision": "EXIT_CUT_LOSS",
        "confidence_score": 9.2,
        "reason": "Technical breakdown below key support.",
        "revised_stop_loss": None,
    })

    with patch("agent.ai_brain.query_llm", return_value=mock_resp):
        res = evaluate_stop_loss(holding, {}, {}, cfg)
        assert res["decision"] == "EXIT_CUT_LOSS"
        assert res["confidence_score"] == 9.2


def test_monitor_ai_stop_loss_gives_room(tmp_path, monkeypatch):
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
    now = datetime(2026, 11, 9, 11, 30, tzinfo=IST)

    # Price drops to 1245.0 (below initial stop 1248.0)
    quotes = lambda syms: ({"SHYAMMETL": 1245.0}, {"SHYAMMETL": 1300.0})

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    ai_cfg = AIBrainConfig(provider="claude")

    mock_eval = {
        "symbol": "SHYAMMETL",
        "decision": "HOLD_AND_GIVE_ROOM",
        "confidence_score": 8.5,
        "reason": "Retesting 50 SMA support after strong Q2 PAT. Give room.",
        "revised_stop_loss": 1220.0,
    }

    with patch("agent.ai_brain.evaluate_stop_loss", return_value=mock_eval), \
         patch("agent.research.get_cached_research", return_value={"profile": {}, "news": []}):
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

    # Broker should NOT place SELL order because AI gave room!
    assert mock_broker.place_order.call_count == 0

    # ai_signals should be updated with revised stop
    saved = json.loads(ai_sig_path.read_text())
    assert saved["selected_trade"]["stop_loss"] == 1220.0
    assert saved["selected_trade"]["stop_consulted"] is True
    assert "stop_consultations" in saved
    assert saved["stop_consultations"]["SHYAMMETL"]["decision"] == "HOLD_AND_GIVE_ROOM"
