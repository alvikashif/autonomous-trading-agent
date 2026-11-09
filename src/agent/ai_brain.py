"""AI Trading Brain: Multi-provider quantitative analyst for swing trading.

Supports Anthropic Claude, DeepSeek, OpenAI, and Google Gemini.
Configured dynamically via environment variables (AI_PROVIDER, AI_MODEL, API keys).
Strict JSON output, conviction thresholding, and Shariah compliance validation.
"""
from __future__ import annotations

import json
import logging
import os
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
    top_candidates = candidates[:max_candidates]

    # Enrich candidates with multi-quarter fundamentals, profit growth & news
    try:
        from .research import get_cached_research
        for c in top_candidates:
            sym = c["symbol"]
            res = get_cached_research(sym)
            prof = res.get("profile", {})
            news = res.get("news", [])
            c["qoq_profit_growth_pct"] = prof.get("qoq_profit_growth_pct")
            c["qoq_revenue_growth_pct"] = prof.get("qoq_revenue_growth_pct")
            c["yoy_profit_growth_pct"] = prof.get("yoy_profit_growth_pct")
            c["yoy_revenue_growth_pct"] = prof.get("yoy_revenue_growth_pct")
            c["operating_margin_pct"] = prof.get("operating_margin_pct")
            c["pe_ratio"] = prof.get("pe_ratio")
            c["forward_pe"] = prof.get("forward_pe")
            c["annual_history"] = prof.get("annual_history", [])[:3]
            c["recent_news"] = [n.get("title", "") for n in news[:3]]
    except Exception as err:
        logger.debug(f"Candidate research enrichment skipped: {err}")

    return top_candidates


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
- Stop Loss: -{stop_pct:.0%} initial risk trigger (supports AI consultation at stop-loss).
- Max Holding Period: 10 trading days.
- Minimum Conviction Score required to trade: {min_confidence}/10.

Here are the pre-screened, 100% Shariah-compliant breakout candidates with price action, moving averages, multi-quarter QoQ/YoY profit growth, annual financial history, and recent company news:
{candidates_table}

YOUR MISSION & EVALUATION GUIDELINES:
1. Multi-Quarter Fundamental Trajectory: Pay special attention to candidates with strong quarterly profit and revenue expansion (e.g. QoQ PAT > 20%, YoY PAT > 30%) and stable operating margins. A company with accelerating earnings and steady multi-year history has high probability of sustained upward trend.
2. News & Corporate Catalysts: Review recent company news and earnings call updates. Strong earnings beats or capacity additions justify high conviction.
3. Overbought vs Breakout Retest: Differentiate between low-quality parabolic blow-offs (RSI > 80-85 without earnings backing) vs legitimate breakouts supported by fundamental inflection.
4. Select the SINGLE highest-conviction setup for immediate entry, OR declare 'PASS' (no trade) if none meet the {min_confidence}/10 bar or if market conditions pose high drawdown risk.
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


LAST_USED_PROVIDER: str = ""
LAST_USED_MODEL: str = ""


def get_last_used_provider() -> str:
    return LAST_USED_PROVIDER


def get_last_used_model() -> str:
    return LAST_USED_MODEL


def _call_claude(prompt: str, model: str, api_key: str) -> str:
    global LAST_USED_PROVIDER, LAST_USED_MODEL
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

    # Track usage & cost
    usage = data.get("usage", {})
    in_tok = usage.get("input_tokens", 0)
    out_tok = usage.get("output_tokens", 0)
    try:
        from .api_tracker import record_api_call
        record_api_call("claude", active_model, in_tok, out_tok)
    except Exception:
        pass

    LAST_USED_PROVIDER = "claude"
    LAST_USED_MODEL = active_model

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
    global LAST_USED_PROVIDER, LAST_USED_MODEL
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

    # Track usage & cost
    usage = data.get("usageMetadata", {})
    in_tok = usage.get("promptTokenCount", 0)
    out_tok = usage.get("candidatesTokenCount", 0)
    try:
        from .api_tracker import record_api_call
        record_api_call("gemini", active_model, in_tok, out_tok)
    except Exception:
        pass

    LAST_USED_PROVIDER = "gemini"
    LAST_USED_MODEL = active_model

    parts = []
    for cand in data.get("candidates", []):
        for part in cand.get("content", {}).get("parts", []):
            if "text" in part:
                parts.append(part["text"])
    if parts:
        return "\n".join(parts)
    raise ValueError(f"No text parts returned from Gemini response: {data}")


def query_llm(prompt: str, provider: str, model: str, api_key: str) -> str:
    """Universal dispatcher for the configured AI provider with automatic Gemini failover."""
    from .api_tracker import is_provider_budget_exhausted

    prov = provider.lower()
    gemini_key = os.environ.get("GEMINI_API_KEY", "").strip()

    # Automatic Failover for Claude:
    # If Claude budget is exhausted or call fails, seamlessly fall back to Gemini
    if prov in ("claude", "anthropic"):
        if is_provider_budget_exhausted("claude"):
            if gemini_key:
                logger.warning(
                    "Claude prepaid budget ($5 limit) is exhausted! Automatically failing over to Google Gemini (gemini-3.5-flash)..."
                )
                return _call_gemini(prompt, "gemini-3.5-flash", gemini_key)
            else:
                raise RuntimeError("Claude budget exhausted and no GEMINI_API_KEY configured for failover.")

        if not api_key:
            if gemini_key:
                logger.warning("Claude API key missing; failing over to Gemini (gemini-3.5-flash)...")
                return _call_gemini(prompt, "gemini-3.5-flash", gemini_key)
            raise ValueError(
                f"No API key found for Claude, and no GEMINI_API_KEY available for failover."
            )

        try:
            return _call_claude(prompt, model, api_key)
        except Exception as err:
            logger.warning("Claude invocation failed: %s", err)
            if gemini_key:
                logger.warning("Automatically failing over to Google Gemini (gemini-3.5-flash)...")
                return _call_gemini(prompt, "gemini-3.5-flash", gemini_key)
            raise

    elif prov == "deepseek":
        if not api_key:
            raise ValueError(f"No API key found for provider '{provider}'.")
        return _call_deepseek(prompt, model, api_key)

    elif prov == "openai":
        if not api_key:
            raise ValueError(f"No API key found for provider '{provider}'.")
        return _call_openai(prompt, model, api_key)

    elif prov in ("gemini", "google"):
        if not api_key:
            raise ValueError(f"No API key found for provider '{provider}'.")
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


def build_stop_loss_prompt(
    holding: dict[str, Any],
    technicals: dict[str, Any],
    research: dict[str, Any] | None = None,
    hard_stop_pct: float = 0.07,
) -> str:
    """Build prompt when a position hits its initial -4% stop to evaluate if room should be given or cut now."""
    sym = holding.get("symbol", "")
    entry_px = float(holding.get("entry_price", holding.get("entry_px", 0.0)))
    cur_px = float(holding.get("current_price", holding.get("last", entry_px)))
    loss_pct = (cur_px / entry_px - 1) if entry_px > 0 else 0.0
    initial_stop = float(holding.get("stop_loss", entry_px * 0.96))
    profile = (research or {}).get("profile", {})
    news = (research or {}).get("news", [])

    news_text = "\n".join([f"- [{n.get('source', 'News')}] {n.get('title', '')} ({n.get('published', '')})" for n in news[:4]]) if news else "No adverse news reported."
    ann_hist = json.dumps(profile.get("annual_history", []))

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M IST")

    return f"""You are the Chief Risk Officer and Quantitative Swing Portfolio Brain for a disciplined, Shariah-compliant fund.
Evaluation Timestamp: {now_str}
A live holding ({sym}) has reached its initial risk trigger (-4.0% drop).

POSITION STATUS:
- Symbol: {sym}
- Entry Price: Rs {entry_px:.2f}
- Current Market Price: Rs {cur_px:.2f}
- Drawdown from Entry: {loss_pct * 100:.1f}%
- Initial Stop-Loss Level: Rs {initial_stop:.2f}

TECHNICAL HEALTH & STRUCTURE:
- 50-day SMA: Rs {technicals.get('sma_50', 'N/A')}
- 200-day SMA: Rs {technicals.get('sma_200', 'N/A')}
- Distance to 50-day SMA: {technicals.get('pct_above_50sma', 'N/A')}%
- 14-day RSI: {technicals.get('rsi_14', 'N/A')}
- Prior 20-day High (Breakout Level): Rs {technicals.get('high_20d', 'N/A')}

MULTI-QUARTER FINANCIAL PERFORMANCE (PAST TRAJECTORY):
- Latest QoQ Profit Growth: {profile.get('qoq_profit_growth_pct', 'N/A')}%
- Latest QoQ Revenue Growth: {profile.get('qoq_revenue_growth_pct', 'N/A')}%
- Latest YoY Profit Growth: {profile.get('yoy_profit_growth_pct', 'N/A')}%
- Latest YoY Revenue Growth: {profile.get('yoy_revenue_growth_pct', 'N/A')}%
- Operating Margin: {profile.get('operating_margin_pct', 'N/A')}%
- Trailing P/E: {profile.get('pe_ratio', 'N/A')} · Forward P/E: {profile.get('forward_pe', 'N/A')}
- 4-Year Annual Revenue & Net Profit History: {ann_hist}

RECENT COMPANY NEWS & CORPORATE ANNOUNCEMENTS:
{news_text}

CRITICAL STRATEGIC RISK DECISION:
We want to avoid prematurely shaking out of high-quality steady compounders during routine market pullbacks or tests of key support before an upward resumption, while strictly preventing catastrophic losses.

Evaluate whether:
1. 'HOLD_AND_GIVE_ROOM': The dip is a normal, healthy pullback or shakeout testing key support (e.g., 50-day SMA or prior breakout level), the company's multi-quarter earnings and fundamentals remain strong, and there are NO adverse corporate/regulatory developments. Recommend a revised structural stop-loss (cannot be below the -{hard_stop_pct * 100:.0%} maximum drawdown limit of Rs {entry_px * (1 - hard_stop_pct):.2f}).
2. 'EXIT_CUT_LOSS': Technical breakdown below key support, distribution volume, or thesis invalidation. Sell immediately to preserve capital.

OUTPUT FORMAT (Respond with ONLY a single valid JSON object):
{{
  "symbol": "{sym}",
  "decision": "HOLD_AND_GIVE_ROOM" | "EXIT_CUT_LOSS",
  "confidence_score": 8.5,
  "reason": "Clear 2-sentence explanation of why to hold through the dip or exit now.",
  "revised_stop_loss": {round(max(cur_px * 0.98, entry_px * (1 - hard_stop_pct)), 2)},
  "revised_stop_pct": -0.06,
  "support_level_tested": "50 SMA / Breakout Retest"
}}
"""


def evaluate_stop_loss(
    holding: dict[str, Any],
    technicals: dict[str, Any],
    research: dict[str, Any] | None,
    ai_config: AIBrainConfig,
    hard_stop_pct: float = 0.07,
) -> dict[str, Any]:
    """Query AI Brain at stop loss to decide if position should be held through a dip or exited."""
    prompt = build_stop_loss_prompt(holding, technicals, research, hard_stop_pct=hard_stop_pct)
    provider = ai_config.resolved_provider()
    model = ai_config.resolved_model()
    api_key = ai_config.resolved_api_key()

    if not api_key:
        logger.warning("No API key configured for AI stop evaluation. Defaulting to EXIT_CUT_LOSS.")
        return {
            "symbol": holding.get("symbol"),
            "decision": "EXIT_CUT_LOSS",
            "confidence_score": 10.0,
            "reason": "Default safety exit: Stop reached, no AI API key configured.",
        }

    try:
        raw = query_llm(prompt, provider, model, api_key)
        parsed = clean_json_response(raw)
        dec = parsed.get("decision", "EXIT_CUT_LOSS").upper()
        if dec not in ("HOLD_AND_GIVE_ROOM", "EXIT_CUT_LOSS"):
            dec = "EXIT_CUT_LOSS"
        parsed["decision"] = dec
        parsed["provider"] = provider
        parsed["model"] = model
        parsed["evaluated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M IST")
        return parsed
    except Exception as e:
        logger.error("Failed to query AI brain for stop loss decision: %s. Defaulting to EXIT_CUT_LOSS.", e)
        return {
            "symbol": holding.get("symbol"),
            "decision": "EXIT_CUT_LOSS",
            "confidence_score": 10.0,
            "reason": f"Default safety exit: AI query error ({e}).",
        }

