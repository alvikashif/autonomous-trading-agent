from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd
import pytest

from agent.ai_brain import (
    build_analysis_prompt,
    clean_json_response,
    compute_order_size,
    extract_technical_candidates,
    load_ai_signals,
    query_llm,
    save_ai_signals,
    validate_ai_decision,
)
from agent.config import AIBrainConfig, load_dotenv


def test_env_resolution_and_defaults(monkeypatch):
    monkeypatch.delenv("AI_PROVIDER", raising=False)
    monkeypatch.delenv("AI_MODEL", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("AI_MIN_CONFIDENCE", raising=False)
    monkeypatch.delenv("AI_MAX_ALLOCATION", raising=False)

    cfg = AIBrainConfig()
    assert cfg.resolved_provider() == "claude"
    assert cfg.resolved_model() == "claude-3-5-sonnet-latest"
    assert cfg.resolved_api_key() == ""
    assert cfg.resolved_min_confidence() == 8.0
    assert cfg.resolved_max_allocation() == 5000.0

    # Switch to DeepSeek via ENV
    monkeypatch.setenv("AI_PROVIDER", "deepseek")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-deepseek-123")
    monkeypatch.setenv("AI_MIN_CONFIDENCE", "8.5")
    monkeypatch.setenv("AI_MAX_ALLOCATION", "10000")

    assert cfg.resolved_provider() == "deepseek"
    assert cfg.resolved_model() == "deepseek-reasoner"
    assert cfg.resolved_api_key() == "sk-deepseek-123"
    assert cfg.resolved_min_confidence() == 8.5
    assert cfg.resolved_max_allocation() == 10000.0

    # Override model explicitly
    monkeypatch.setenv("AI_MODEL", "deepseek-chat")
    assert cfg.resolved_model() == "deepseek-chat"

    # Switch to Gemini
    monkeypatch.setenv("AI_PROVIDER", "gemini")
    monkeypatch.delenv("AI_MODEL", raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "AIzaSyMock")
    assert cfg.resolved_provider() == "gemini"
    assert cfg.resolved_model() == "gemini-2.5-flash"
    assert cfg.resolved_api_key() == "AIzaSyMock"

    # Switch to OpenAI
    monkeypatch.setenv("AI_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-xyz")
    assert cfg.resolved_provider() == "openai"
    assert cfg.resolved_model() == "o3-mini"
    assert cfg.resolved_api_key() == "sk-openai-xyz"


def test_load_dotenv(tmp_path, monkeypatch):
    monkeypatch.delenv("TEST_DOTENV_KEY", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text("TEST_DOTENV_KEY=hello_world\n# comment\nOTHER_KEY='quoted_value'\n")

    load_dotenv(env_file)
    import os
    assert os.environ.get("TEST_DOTENV_KEY") == "hello_world"
    assert os.environ.get("OTHER_KEY") == "quoted_value"


def test_extract_technical_candidates():
    # Build 70 business days of prices
    dates = pd.bdate_range("2026-01-01", periods=70)
    # STOCKA: upward breakout above 20d high and 50 SMA
    prices_a = np.linspace(100, 150, 70)
    # STOCKB: downward trend (close < 50 SMA)
    prices_b = np.linspace(200, 100, 70)
    # STOCKC: price Rs 10,000 (exceeds max_price 5,000)
    prices_c = np.linspace(9000, 10000, 70)

    df = pd.DataFrame({"STOCKA": prices_a, "STOCKB": prices_b, "STOCKC": prices_c}, index=dates)
    compliant = {"STOCKA", "STOCKB", "STOCKC"}

    candidates = extract_technical_candidates(df, compliant, max_candidates=5, max_price=5000.0)
    symbols = [c["symbol"] for c in candidates]

    assert "STOCKA" in symbols
    assert "STOCKB" not in symbols  # downward trend rejected
    assert "STOCKC" not in symbols  # exceeds max_price rejected

    cand_a = next(c for c in candidates if c["symbol"] == "STOCKA")
    assert cand_a["current_price"] == 150.0
    assert cand_a["rsi_14"] > 50.0


def test_clean_json_response():
    # 1. Plain JSON string
    raw1 = '{"verdict": "PASS", "confidence": 7.0}'
    assert clean_json_response(raw1) == {"verdict": "PASS", "confidence": 7.0}

    # 2. Markdown fenced JSON
    raw2 = '```json\n{"verdict": "EXECUTE", "confidence": 9.0}\n```'
    assert clean_json_response(raw2) == {"verdict": "EXECUTE", "confidence": 9.0}

    # 3. Fluff before and after
    raw3 = 'Here is the analysis:\n```json\n{"verdict": "EXECUTE"}\n```\nHope this helps!'
    assert clean_json_response(raw3) == {"verdict": "EXECUTE"}

    # 4. Invalid JSON raises ValueError
    with pytest.raises(ValueError):
        clean_json_response("This is not valid JSON at all")


def test_validate_ai_decision():
    compliant = {"TCS", "INFY", "WIPRO"}

    # Valid execution
    valid = {
        "verdict": "EXECUTE",
        "selected_trade": {
            "symbol": "TCS",
            "entry_price": 3500.0,
            "target_price": 3780.0,
            "stop_loss": 3360.0,
            "confidence_score": 8.5,
        },
    }
    res = validate_ai_decision(dict(valid), compliant, min_confidence=8.0)
    assert res["verdict"] == "EXECUTE"
    assert res["selected_trade"]["symbol"] == "TCS"

    # Non-compliant symbol vetoed
    non_compliant = dict(valid)
    non_compliant["selected_trade"] = dict(valid["selected_trade"], symbol="NONCOMPLIANT")
    res = validate_ai_decision(non_compliant, compliant, min_confidence=8.0)
    assert res["verdict"] == "PASS"
    assert res["selected_trade"] is None
    assert "VETOED" in res["decision_summary"]

    # Low conviction score below threshold -> PASS
    low_conf = dict(valid)
    low_conf["selected_trade"] = dict(valid["selected_trade"], confidence_score=7.2)
    res = validate_ai_decision(low_conf, compliant, min_confidence=8.0)
    assert res["verdict"] == "PASS"
    assert res["selected_trade"] is None

    # Incoherent prices (stop loss higher than entry) -> VETOED
    bad_price = dict(valid)
    bad_price["selected_trade"] = dict(valid["selected_trade"], stop_loss=3600.0)
    res = validate_ai_decision(bad_price, compliant, min_confidence=8.0)
    assert res["verdict"] == "PASS"
    assert res["selected_trade"] is None


def test_compute_order_size():
    trade = {
        "symbol": "SHYAMMETL",
        "entry_price": 1300.0,
        "target_price": 1404.0,
        "stop_loss": 1248.0,
    }

    # Allocation Rs 5,000 -> 3 shares @ 1300 = Rs 3,900
    sizing = compute_order_size(trade, max_allocation=5000.0)
    assert sizing["qty"] == 3
    assert sizing["trade_value"] == 3900.0
    assert sizing["is_executable"] is True

    # High priced stock (Rs 6,000) exceeds Rs 5,000 capital
    expensive_trade = dict(trade, entry_price=6000.0)
    exp_sizing = compute_order_size(expensive_trade, max_allocation=5000.0)
    assert exp_sizing["qty"] == 0
    assert exp_sizing["is_executable"] is False

    # Broker cash limit constraint (available cash Rs 2,000)
    cash_limited = compute_order_size(trade, max_allocation=5000.0, cash_balance=2000.0)
    assert cash_limited["qty"] == 1
    assert cash_limited["trade_value"] == 1300.0


def test_query_llm_dispatchers():
    # Test Claude
    with patch("requests.post") as mock_post:
        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.json.return_value = {"content": [{"text": '{"verdict": "EXECUTE"}'}]}
        mock_post.return_value = mock_resp

        out = query_llm("test prompt", "claude", "claude-3-5-sonnet-latest", "sk-ant-test")
        assert '{"verdict": "EXECUTE"}' in out
        assert "api.anthropic.com" in mock_post.call_args[0][0]

    # Test DeepSeek
    with patch("requests.post") as mock_post:
        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.json.return_value = {"choices": [{"message": {"content": '{"verdict": "PASS"}'}}]}
        mock_post.return_value = mock_resp

        out = query_llm("test prompt", "deepseek", "deepseek-reasoner", "sk-deepseek-test")
        assert '{"verdict": "PASS"}' in out
        assert "api.deepseek.com" in mock_post.call_args[0][0]

    # Test OpenAI
    with patch("requests.post") as mock_post:
        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.json.return_value = {"choices": [{"message": {"content": '{"verdict": "EXECUTE"}'}}]}
        mock_post.return_value = mock_resp

        out = query_llm("test prompt", "openai", "o3-mini", "sk-openai-test")
        assert '{"verdict": "EXECUTE"}' in out
        assert "api.openai.com" in mock_post.call_args[0][0]

    # Test Gemini
    with patch("requests.post") as mock_post:
        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.json.return_value = {
            "candidates": [{"content": {"parts": [{"text": '{"verdict": "EXECUTE"}'}]}}]
        }
        mock_post.return_value = mock_resp

        out = query_llm("test prompt", "gemini", "gemini-2.5-flash", "AIzaSyTest")
        assert '{"verdict": "EXECUTE"}' in out
        assert "googleapis.com" in mock_post.call_args[0][0]


def test_save_and_load_ai_signals(tmp_path):
    sig_file = tmp_path / "ai_signals.json"
    data = {
        "verdict": "EXECUTE",
        "provider": "claude",
        "selected_trade": {"symbol": "TCS", "confidence_score": 9.0},
    }
    save_ai_signals(data, sig_file)
    loaded = load_ai_signals(sig_file)
    assert loaded["verdict"] == "EXECUTE"
    assert loaded["selected_trade"]["symbol"] == "TCS"
