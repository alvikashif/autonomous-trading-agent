"""Emergency kill switch state management.

Provides an immediate circuit breaker to halt all trading operations,
reject orders, and alert the monitor and dashboard.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path


def is_kill_switch_active(path: str | Path = "data/kill_switch.json") -> bool:
    """Return True if the emergency kill switch is currently engaged."""
    p = Path(path)
    if not p.exists():
        return False
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return bool(data.get("active", False))
    except Exception:
        return False


def get_kill_switch_status(path: str | Path = "data/kill_switch.json") -> dict:
    """Return dict with full kill switch details."""
    p = Path(path)
    if not p.exists():
        return {"active": False, "reason": None, "triggered_at": None}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return {
            "active": bool(data.get("active", False)),
            "reason": data.get("reason"),
            "triggered_at": data.get("triggered_at"),
            "reset_at": data.get("reset_at"),
        }
    except Exception:
        return {"active": False, "reason": None, "triggered_at": None}


def trigger_kill_switch(
    reason: str = "Manual emergency halt",
    path: str | Path = "data/kill_switch.json",
) -> dict:
    """Engage the kill switch. Halts all automated trading."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    status = {
        "active": True,
        "reason": reason,
        "triggered_at": datetime.now(timezone.utc).isoformat(),
    }
    p.write_text(json.dumps(status, indent=2), encoding="utf-8")
    return status


def reset_kill_switch(path: str | Path = "data/kill_switch.json") -> dict:
    """Disengage the kill switch and restore normal operations."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    status = {
        "active": False,
        "reason": None,
        "triggered_at": None,
        "reset_at": datetime.now(timezone.utc).isoformat(),
    }
    p.write_text(json.dumps(status, indent=2), encoding="utf-8")
    return status
