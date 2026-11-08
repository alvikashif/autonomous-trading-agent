from datetime import datetime
import json
import pytest

from agent.notify import (
    dispatch_notification,
    load_notifications,
    save_notification,
    send_telegram_alert,
    send_webhook_alert,
)


def test_save_and_load_notifications(tmp_path):
    notif_path = tmp_path / "notifications.json"
    n1 = save_notification("buy", "BUY 10 TATAMOTORS", "Executed @ 750", {"symbol": "TATAMOTORS", "qty": 10}, path=notif_path)
    assert n1["type"] == "buy"
    assert "TATAMOTORS" in n1["title"]

    n2 = save_notification("stop_loss", "STOP LOSS: TATAMOTORS", "Sold @ 720", {"symbol": "TATAMOTORS"}, path=notif_path)
    assert n2["type"] == "stop_loss"

    history = load_notifications(notif_path)
    assert len(history) == 2
    assert history[0]["id"] == n2["id"]  # Most recent first


def test_dispatch_notification_without_external_credentials(tmp_path):
    notif_path = tmp_path / "notifications.json"
    n = dispatch_notification(
        event_type="take_profit",
        title="PROFIT TAKEN: SHYAMMETL",
        message="Target reached +8%",
        details={"symbol": "SHYAMMETL", "gain_pct": 0.08},
        path=notif_path,
    )
    assert n["type"] == "take_profit"
    items = load_notifications(notif_path)
    assert len(items) == 1
    assert items[0]["title"] == "PROFIT TAKEN: SHYAMMETL"


def test_telegram_and_webhook_guards():
    # When tokens/URLs are missing, returns False safely without error
    assert not send_telegram_alert("test", bot_token=None, chat_id=None)
    assert not send_webhook_alert("test", "test message", webhook_url=None)
