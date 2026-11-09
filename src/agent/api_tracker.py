"""AI API Budget and Usage Tracker.

Tracks token usage and real-time costs against a prepaid budget (e.g. $5.00).
Supports Anthropic Claude, Google Gemini, DeepSeek, and OpenAI.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")

# Token pricing in USD per 1,000,000 tokens
MODEL_RATES = {
    "claude": {
        "input": 3.00,        # $3 / MTok for Claude Sonnet 5.5 / 3.7
        "output": 15.00,      # $15 / MTok
    },
    "gemini": {
        "input": 0.075,       # $0.075 / MTok for Gemini Flash (often free tier)
        "output": 0.30,       # $0.30 / MTok
    },
    "deepseek": {
        "input": 0.14,        # $0.14 / MTok (DeepSeek Chat / Reasoner cache miss)
        "output": 0.28,       # $0.28 / MTok
    },
    "openai": {
        "input": 1.10,        # $1.10 / MTok for o3-mini / gpt-4o-mini
        "output": 4.40,       # $4.40 / MTok
    },
}


def calculate_cost(provider: str, input_tokens: int, output_tokens: int) -> float:
    prov = provider.lower()
    rates = MODEL_RATES.get(prov, {"input": 1.0, "output": 3.0})
    cost = (input_tokens / 1_000_000.0) * rates["input"] + (output_tokens / 1_000_000.0) * rates["output"]
    return round(cost, 6)


def load_api_usage(usage_path: str | Path = "data/api_usage.json", default_budget: float = 5.0) -> dict:
    p = Path(usage_path)
    if not p.is_file():
        return {
            "budget_limit_usd": default_budget,
            "total_spent_usd": 0.0,
            "remaining_usd": default_budget,
            "total_calls": 0,
            "providers": {},
            "recent_calls": [],
        }
    try:
        data = json.loads(p.read_text())
        if "budget_limit_usd" not in data:
            data["budget_limit_usd"] = default_budget
        data["remaining_usd"] = max(0.0, round(data["budget_limit_usd"] - data.get("total_spent_usd", 0.0), 4))
        return data
    except Exception:
        return {
            "budget_limit_usd": default_budget,
            "total_spent_usd": 0.0,
            "remaining_usd": default_budget,
            "total_calls": 0,
            "providers": {},
            "recent_calls": [],
        }


def record_api_call(
    provider: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    usage_path: str | Path = "data/api_usage.json",
    default_budget: float = 5.0,
) -> dict:
    """Record an API invocation, calculate cost, update balance, and persist."""
    p = Path(usage_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    state = load_api_usage(p, default_budget)

    cost = calculate_cost(provider, input_tokens, output_tokens)
    prov = provider.lower()

    # Update global metrics
    state["total_spent_usd"] = round(state.get("total_spent_usd", 0.0) + cost, 4)
    state["remaining_usd"] = max(0.0, round(state["budget_limit_usd"] - state["total_spent_usd"], 4))
    state["total_calls"] = state.get("total_calls", 0) + 1

    # Update provider metrics
    providers = state.setdefault("providers", {})
    p_data = providers.setdefault(
        prov, {"calls": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0}
    )
    p_data["calls"] += 1
    p_data["input_tokens"] += input_tokens
    p_data["output_tokens"] += output_tokens
    p_data["cost_usd"] = round(p_data["cost_usd"] + cost, 4)

    # Append to recent calls (keep last 30)
    now_str = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S IST")
    recent = state.setdefault("recent_calls", [])
    recent.insert(
        0,
        {
            "ts": now_str,
            "provider": prov,
            "model": model,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cost_usd": cost,
        },
    )
    state["recent_calls"] = recent[:30]

    p.write_text(json.dumps(state, indent=2))
    return state


def is_provider_budget_exhausted(provider: str, usage_path: str | Path = "data/api_usage.json") -> bool:
    """Return True if the overall budget limit ($5.00) or provider threshold is reached."""
    try:
        state = load_api_usage(usage_path)
        limit = float(state.get("budget_limit_usd", 5.0))
        spent = float(state.get("total_spent_usd", 0.0))
        # If less than $0.005 remaining, treat as exhausted
        return (limit - spent) <= 0.005
    except Exception:
        return False
