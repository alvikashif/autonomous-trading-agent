"""AI Trading Brain: Multi-provider quantitative analyst for swing trading.

Supports Anthropic Claude, DeepSeek, OpenAI, and Google Gemini.
Configured dynamically via environment variables (AI_PROVIDER, AI_MODEL, API keys).
Strict JSON output, conviction thresholding, and Shariah compliance validation.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import requests

from .config import AIBrainConfig, Config
from .data import load_universe
from .kill_switch import is_kill_switch_active
from .models import Order, Side

logger = logging.getLogger(__name__)


def extract_technical_candidates(
    prices_df: pd.DataFrame,
    compliant_symbols: set[str],
    fundamentals_df: pd.DataFrame | None = None,
    max_candidates: int = 10,
    max_price: float = 5000.0,
) -> list[dict[str, Any]]:
    """Scan price history and fundamentals for top swing breakout setups."""
    fund_map: dict[str, dict] = {}
    if fundamentals_df is not None and not fundamentals_df.empty:
        for r in fundamentals_df.to_dict(orient="records"):
            sym = str(r.get("symbol", "")).upper()
            fund_map[sym] = r

    candidates: list[dict[str, Any]] = []

    for sym in prices_df.columns:
        if sym not in compliant_symbols or sym.startswith("^"):
            continue

        series = prices_df[sym].dropna()
        if len(series) < 60:
            continue

        last_close = float(series.iloc[-1])
        if last_close <= 0 or (max_price and last_close > max_price):
            # Stock price exceeds capital allocation (e.g. Rs 5k test)
            continue

        high_20 = float(series.iloc[-21:-1].max()) if len(series) >= 21 else float(series.max())
        sma_50 = float(series.iloc[-50:].mean()) if len(series) >= 50 else float(series.mean())
        sma_200 = float(series.iloc[-200:].mean()) if len(series) >= 200 else sma_50

        # Multi-timeframe returns
        ret_5d = float(series.iloc[-1] / series.iloc[-6] - 1) if len(series) >= 6 else 0.0
        ret_20d = float(series.iloc[-1] / series.iloc[-21] - 1) if len(series) >= 21 else 0.0
        ret_60d = float(series.iloc[-1] / series.iloc[-61] - 1) if len(series) >= 61 else 0.0

        # 14-day RSI calculation
        delta = series.diff()
        gain = delta.clip(lower=0).rolling(14).mean()
        loss = (-delta.clip(upper=0)).rolling(14).mean()
        rs = gain / loss.replace(0, 1e-9)
        rsi_14 = float((100 - (100 / (1 + rs))).iloc[-1]) if len(series) >= 15 else 50.0

        # Swing condition: Breaking out near/above 20-day high and above 50-day average
        if last_close >= (high_20 * 0.99) and last_close > sma_50:
            fund = fund_map.get(sym, {})
            mcap = fund.get("market_cap")
            debt = fund.get("total_debt")
            roe = fund.get("roe")

            candidates.append({
                "symbol": sym,
                "current_price": round(last_close, 2),
                "high_20d": round(high_20, 2),
                "sma_50": round(sma_50, 2),
                "sma_200": round(sma_200, 2),
                "pct_above_50sma": round((last_close / sma_50 - 1) * 100, 1),
                "ret_5d_pct": round(ret_5d * 100, 1),
                "ret_20d_pct": round(ret_20d * 100, 1),
                "ret_60d_pct": round(ret_60d * 100, 1),
                "rsi_14": round(rsi_14, 1),
                "market_cap_cr": round(float(mcap) / 1e7, 1) if mcap else None,
                "debt_to_mcap": round(float(debt) / float(mcap), 3) if debt and mcap else None,
                "roe_pct": round(float(roe) * 100, 1) if roe else None,
            })

    # Sort primarily by 60-day return momentum, take top candidates
    candidates.sort(key=lambda x: x["ret_60d_pct"], reverse=True)
    return candidates[:max_candidates]


def build_analysis_prompt(
    candidates: list[dict[str, Any]],
    capital: float,
    min_confidence: float = 8.0,
    target_pct: float = 0.08,
    stop_pct: float = 0.04,
    benchmark_trend: str = "neutral",
) -> str:
    """Build the prompt instructing the LLM to act as a disciplined research analyst."""
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M IST")

    candidates_table = json.dumps(candidates, indent=2)

    return f"""You are the quantitative trading analyst and lead portfolio brain for a disciplined, Shariah-compliant swing trading fund.
Scan Timestamp: {now_str}
Benchmark Market Trend: {benchmark_trend}
Available Capital for this Test Trade: Rs {capital:,.0f}
Fixed Rules:
- LONG ONLY, Delivery (CNC), NSE cash equities.
- NO shorting, NO margin/leverage, NO intraday, NO F&O.
- Target: +{target_pct:.0%} profit.
- Stop Loss: -{stop_pct:.0%} strict stop.
- Max Holding Period: 10 trading days.
- Minimum Conviction Score required to trade: {min_confidence}/10.

Here are the pre-screened, 100% Shariah-compliant breakout candidates (price, moving averages, 5d/20d/60d returns, RSI 14, and fundamentals):
{candidates_table}

YOUR MISSION:
1. Review each candidate with extreme skepticism. Be aware of overbought RSI (>75-80), sudden momentum exhaustion, or weak risk/reward.
2. Select the SINGLE highest-conviction setup for immediate entry, OR declare 'PASS' (no trade) if none meet the {min_confidence}/10 bar or if market conditions pose high drawdown risk.
3. For the selected stock (if any), compute:
   - entry_price: current market close price
   - target_price: entry_price * (1 + {target_pct})
   - stop_loss: entry_price * (1 - {stop_pct})
   - confidence_score: from 1.0 to 10.0
4. Formulate a crisp thesis and identify explicit risks/catalysts.

OUTPUT FORMAT:
You MUST reply with ONLY a single valid JSON object, without conversational fluff. The JSON must adhere to this schema:
{{
  "scan_timestamp": "{now_str}",
  "market_regime": "bullish" | "neutral" | "cautious" | "bearish",
  "verdict": "EXECUTE" | "PASS",
  "decision_summary": "1-2 sentence executive explanation of your decision",
  "selected_trade": {{
    "symbol": "SYMBOL",
    "action": "BUY",
    "current_price": 1234.50,
    "entry_price": 1234.50,
    "target_price": 1333.25,
    "target_pct": {target_pct},
    "stop_loss": 1185.12,
    "stop_pct": {stop_pct},
    "max_hold_days": 10,
    "confidence_score": 8.5,
    "thesis": "Specific technical and fundamental reason for this trade...",
    "catalysts": ["catalyst 1", "catalyst 2"],
    "risks": ["risk 1", "risk 2"]
  }} or null if verdict is PASS,
  "evaluated_candidates": [
    {{
      "symbol": "SYMBOL",
      "verdict": "SELECTED" | "REJECT",
      "reason": "Brief reason why accepted or rejected"
    }}
  ]
}}
"""


def _normalize_claude_model(model: str) -> str:
    m = model.strip()
    if not m or "latest" in m or "3-5-sonnet" in m or "sonnet-3" in m:
        return "claude-sonnet-5-5"
    return m


def _normalize_gemini_model(model: str) -> str:
    m = model.strip()
    if not m or "2.5-flash" in m or "flash-latest" in m:
        return "gemini-3.5-flash"
    return m


def _call_claude(prompt: str, model: str, api_key: str) -> str:
    active_model = _normalize_claude_model(model)
    url = "https://api.anthropic.com/v1/messages"
    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    payload = {
        "model": active_model,
        "max_tokens": 4096,
        "messages": [{"role": "user", "content": prompt}],
    }
    resp = requests.post(url, headers=headers, json=payload, timeout=60)
    if not resp.ok:
        raise RuntimeError(f"Claude API Error {resp.status_code}: {resp.text}")
    data = resp.json()
    texts = [b["text"] for b in data.get("content", []) if b.get("type", "text") == "text" and "text" in b]
    if texts:
        return "\n".join(texts)
    raise ValueError(f"No text blocks found in Claude response: {data}")


def _call_deepseek(prompt: str, model: str, api_key: str) -> str:
    url = "https://api.deepseek.com/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": "You are a quantitative swing trading research analyst. Output ONLY valid JSON."},
            {"role": "user", "content": prompt},
        ],
    }
    resp = requests.post(url, headers=headers, json=payload, timeout=90)
    if not resp.ok:
        raise RuntimeError(f"DeepSeek API Error {resp.status_code}: {resp.text}")
    data = resp.json()
    return data["choices"][0]["message"]["content"]


def _call_openai(prompt: str, model: str, api_key: str) -> str:
    url = "https://api.openai.com/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": "You are a quantitative swing trading research analyst. Output ONLY valid JSON."},
            {"role": "user", "content": prompt},
        ],
    }
    resp = requests.post(url, headers=headers, json=payload, timeout=60)
    if not resp.ok:
        raise RuntimeError(f"OpenAI API Error {resp.status_code}: {resp.text}")
    data = resp.json()
    return data["choices"][0]["message"]["content"]


def _call_gemini(prompt: str, model: str, api_key: str) -> str:
    active_model = _normalize_gemini_model(model)
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{active_model}:generateContent?key={api_key}"
    headers = {"Content-Type": "application/json"}
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.1,
            "responseMimeType": "application/json",
        },
    }
    resp = requests.post(url, headers=headers, json=payload, timeout=60)
    if not resp.ok:
        raise RuntimeError(f"Gemini API Error {resp.status_code}: {resp.text}")
    data = resp.json()
    parts = []
    for cand in data.get("candidates", []):
        for part in cand.get("content", {}).get("parts", []):
            if "text" in part:
                parts.append(part["text"])
    if parts:
        return "\n".join(parts)
    raise ValueError(f"No text parts returned from Gemini response: {data}")


def query_llm(prompt: str, provider: str, model: str, api_key: str) -> str:
    """Universal dispatcher for the configured AI provider."""
    if not api_key:
        raise ValueError(
            f"No API key found for provider '{provider}'. Please set {provider.upper()}_API_KEY in ENV."
        )

    prov = provider.lower()
    if prov in ("claude", "anthropic"):
        return _call_claude(prompt, model, api_key)
    elif prov == "deepseek":
        return _call_deepseek(prompt, model, api_key)
    elif prov == "openai":
        return _call_openai(prompt, model, api_key)
    elif prov in ("gemini", "google"):
        return _call_gemini(prompt, model, api_key)
    else:
        raise ValueError(
            f"Unsupported AI provider '{provider}'. Supported options: claude, deepseek, openai, gemini."
        )


def clean_json_response(raw: str) -> dict[str, Any]:
    """Parse JSON from model response, handling markdown blocks or extra surrounding text."""
    text = raw.strip()
    try:
        return json.loads(text)
    except Exception:
        pass

    # Extract code fence
    if "```" in text:
        parts = text.split("```")
        for p in parts:
            p = p.strip()
            if p.startswith("json"):
                p = p[4:].strip()
            try:
                return json.loads(p)
            except Exception:
                continue

    # Extract substring between first { and last }
    s = text.find("{")
    e = text.rfind("}")
    if s != -1 and e != -1 and e > s:
        candidate = text[s : e + 1]
        try:
            return json.loads(candidate)
        except Exception:
            cleaned = re.sub(r',\s*([\]}])', r'\1', candidate)
            try:
                return json.loads(cleaned)
            except Exception:
                pass

    raise ValueError(f"Could not parse valid JSON from AI response: {text[:200]}...")


def validate_ai_decision(
    decision: dict[str, Any],
    compliant_symbols: set[str],
    min_confidence: float = 8.0,
) -> dict[str, Any]:
    """Ensure the model's output satisfies all safety and Shariah constraints."""
    verdict = decision.get("verdict", "PASS")
    selected = decision.get("selected_trade")

    if verdict != "EXECUTE" or not selected:
        decision["verdict"] = "PASS"
        decision["selected_trade"] = None
        return decision

    symbol = str(selected.get("symbol", "")).upper()
    if symbol not in compliant_symbols:
        logger.warning("AI selected symbol %s which is NOT in the compliant universe! Vetoing.", symbol)
        decision["verdict"] = "PASS"
        decision["selected_trade"] = None
        decision["decision_summary"] = f"VETOED: {symbol} is not certified Shariah-compliant."
        return decision

    confidence = float(selected.get("confidence_score", 0.0))
    if confidence < min_confidence:
        logger.info("AI selected %s but confidence %.1f is below required %.1f threshold. Passing.", symbol, confidence, min_confidence)
        decision["verdict"] = "PASS"
        decision["selected_trade"] = None
        decision["decision_summary"] = f"PASS: {symbol} confidence ({confidence:.1f}) below threshold ({min_confidence:.1f})."
        return decision

    entry_px = float(selected.get("entry_price", 0.0))
    target_px = float(selected.get("target_price", 0.0))
    stop_px = float(selected.get("stop_loss", 0.0))

    if not (stop_px < entry_px < target_px):
        logger.warning("Invalid price logic for %s: stop=%f, entry=%f, target=%f. Vetoing.", symbol, stop_px, entry_px, target_px)
        decision["verdict"] = "PASS"
        decision["selected_trade"] = None
        decision["decision_summary"] = f"VETOED: Incoherent price targets for {symbol}."
        return decision

    return decision


def compute_order_size(
    selected_trade: dict[str, Any],
    max_allocation: float,
    cash_balance: float | None = None,
) -> dict[str, Any]:
    """Calculate integer share quantity and total investment capital."""
    entry_price = float(selected_trade["entry_price"])
    if entry_price <= 0:
        raise ValueError(f"Invalid entry price: {entry_price}")

    usable_cash = max_allocation
    if cash_balance is not None and cash_balance < usable_cash:
        usable_cash = cash_balance

    qty = int(usable_cash // entry_price)
    trade_value = qty * entry_price

    return {
        "symbol": selected_trade["symbol"],
        "qty": qty,
        "entry_price": entry_price,
        "trade_value": round(trade_value, 2),
        "stop_loss": float(selected_trade["stop_loss"]),
        "target_price": float(selected_trade["target_price"]),
        "usable_cash": usable_cash,
        "is_executable": qty > 0 and trade_value >= 500.0,
    }


def save_ai_signals(decision: dict[str, Any], path: str | Path = "data/ai_signals.json") -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(decision, indent=2), encoding="utf-8")


def load_ai_signals(path: str | Path = "data/ai_signals.json") -> dict[str, Any]:
    p = Path(path)
    if not p.is_file():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def get_symbol_technicals(symbol: str, prices_df: pd.DataFrame) -> dict[str, Any]:
    """Calculate key momentum and trend technicals for a specific stock."""
    if symbol not in prices_df.columns:
        return {}
    series = prices_df[symbol].dropna()
    if len(series) < 20:
        return {}

    last = float(series.iloc[-1])
    high_20 = float(series.iloc[-21:-1].max()) if len(series) >= 21 else float(series.max())
    sma_50 = float(series.iloc[-50:].mean()) if len(series) >= 50 else float(series.mean())
    sma_200 = float(series.iloc[-200:].mean()) if len(series) >= 200 else sma_50
    ret_5d = float(series.iloc[-1] / series.iloc[-6] - 1) if len(series) >= 6 else 0.0
    ret_20d = float(series.iloc[-1] / series.iloc[-21] - 1) if len(series) >= 21 else 0.0

    delta = series.diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = (-delta.clip(upper=0)).rolling(14).mean()
    rs = gain / loss.replace(0, 1e-9)
    rsi_14 = float((100 - (100 / (1 + rs))).iloc[-1]) if len(series) >= 15 else 50.0

    return {
        "current_price": round(last, 2),
        "high_20d": round(high_20, 2),
        "pct_from_20d_high": round((last / high_20 - 1) * 100, 1),
        "sma_50": round(sma_50, 2),
        "pct_above_50sma": round((last / sma_50 - 1) * 100, 1),
        "sma_200": round(sma_200, 2),
        "ret_5d_pct": round(ret_5d * 100, 1),
        "ret_20d_pct": round(ret_20d * 100, 1),
        "rsi_14": round(rsi_14, 1),
    }


def build_take_profit_prompt(
    holding: dict[str, Any],
    technicals: dict[str, Any],
    target_pct: float = 0.08,
) -> str:
    """Build prompt when a position hits its target gain to decide whether to take profit or trail higher."""
    sym = holding.get("symbol", "")
    entry_px = float(holding.get("entry_price", holding.get("entry_px", 0.0)))
    cur_px = float(holding.get("current_price", holding.get("last", entry_px)))
    gain_pct = (cur_px / entry_px - 1) if entry_px > 0 else 0.0
    days_held = holding.get("days_held", 0)

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M IST")

    return f"""You are the lead portfolio risk manager for a disciplined swing trading fund.
Evaluation Timestamp: {now_str}
A currently held stock ({sym}) has reached its profit target of +{target_pct:.0%}.

POSITION STATUS:
- Symbol: {sym}
- Entry Price: Rs {entry_px:.2f}
- Current Market Price: Rs {cur_px:.2f}
- Unrealized P&L: +{gain_pct * 100:.1f}%
- Days Held: {days_held} trading days

CURRENT TECHNICAL INDICATORS:
- 14-day RSI: {technicals.get('rsi_14', 50.0):.1f}
- % Above 50-day SMA: {technicals.get('pct_above_50sma', 0.0):.1f}%
- 5-day Return: {technicals.get('ret_5d_pct', 0.0):.1f}%
- 20-day Return: {technicals.get('ret_20d_pct', 0.0):.1f}%
- Distance from 20-day High: {technicals.get('pct_from_20d_high', 0.0):.1f}%

CRITICAL STRATEGIC DECISION:
The position has hit its target gain. Should we:
1. 'TAKE_PROFIT': Lock in the profit now. Choose this if RSI is overbought (>75-80), momentum is exhausting, or risk of a sharp mean-reversion pullback is high.
2. 'HOLD_AND_TRAIL': Let the winner run to maximize profit in this trade. Choose this ONLY if momentum is accelerating, the breakout has powerful continuation, and higher gains (+12% to +15%) are achievable.
   If HOLD_AND_TRAIL is chosen, you MUST provide:
   - revised_target_pct: extended target (e.g. 0.12 or 0.15 for +12% to +15%)
   - trailing_stop_pct: guaranteed lock-in stop (e.g. 0.05 to protect at least +5% gain)

OUTPUT FORMAT (Respond with ONLY a single valid JSON object):
{{
  "symbol": "{sym}",
  "decision": "TAKE_PROFIT" | "HOLD_AND_TRAIL",
  "confidence_score": 8.5,
  "reason": "Clear 1-2 sentence technical reason for decision",
  "revised_target_pct": 0.12,
  "revised_target_price": {cur_px * 1.04:.2f},
  "trailing_stop_pct": 0.05,
  "trailing_stop_price": {entry_px * 1.05:.2f},
  "max_additional_days": 5
}}
"""


def evaluate_take_profit(
    holding: dict[str, Any],
    technicals: dict[str, Any],
    ai_config: AIBrainConfig,
) -> dict[str, Any]:
    """Query AI Brain to decide if profit should be taken immediately or trailed higher."""
    prompt = build_take_profit_prompt(holding, technicals, target_pct=ai_config.target_pct)
    provider = ai_config.resolved_provider()
    model = ai_config.resolved_model()
    api_key = ai_config.resolved_api_key()

    if not api_key:
        logger.warning("No API key configured for AI exit evaluation. Defaulting to TAKE_PROFIT.")
        return {
            "symbol": holding.get("symbol"),
            "decision": "TAKE_PROFIT",
            "confidence_score": 10.0,
            "reason": "Default safety exit: Target reached, no AI API key configured.",
        }

    try:
        raw = query_llm(prompt, provider, model, api_key)
        parsed = clean_json_response(raw)
        dec = parsed.get("decision", "TAKE_PROFIT").upper()
        if dec not in ("TAKE_PROFIT", "HOLD_AND_TRAIL"):
            dec = "TAKE_PROFIT"
        parsed["decision"] = dec
        parsed["provider"] = provider
        parsed["model"] = model
        parsed["evaluated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M IST")
        return parsed
    except Exception as e:
        logger.error("Failed to query AI brain for exit decision: %s. Defaulting to TAKE_PROFIT.", e)
        return {
            "symbol": holding.get("symbol"),
            "decision": "TAKE_PROFIT",
            "confidence_score": 10.0,
            "reason": f"Default safety exit: Target reached; AI query error ({e}).",
        }
