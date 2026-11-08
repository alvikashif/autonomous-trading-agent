"""Multi-channel notification dispatcher for Alvi Quantitative Trading Terminal.
Supports Web & Mobile App Notifications (via PWA / Web Notification API),
Telegram Bot instant alerts, and external Webhooks (Discord, Slack).
"""
from __future__ import annotations

import json
import logging
import os
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")


def get_notifications_path(base_dir: str | Path = "data") -> Path:
    return Path(base_dir) / "notifications.json"


def load_notifications(path: str | Path = "data/notifications.json") -> list[dict]:
    try:
        p = Path(path)
        if not p.is_file():
            return []
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def save_notification(
    event_type: str,
    title: str,
    message: str,
    details: dict | None = None,
    path: str | Path = "data/notifications.json",
    max_history: int = 100,
) -> dict:
    """Save notification to local JSON history."""
    now = datetime.now(IST).isoformat(timespec="seconds")
    notif = {
        "id": f"notif_{int(datetime.now().timestamp() * 1000)}",
        "timestamp": now,
        "type": event_type,  # 'buy', 'sell', 'stop_loss', 'take_profit', 'alert', 'kill_switch', 'test'
        "title": title,
        "message": message,
        "details": details or {},
    }
    try:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        items = load_notifications(p)
        items.insert(0, notif)
        items = items[:max_history]
        p.write_text(json.dumps(items, indent=2), encoding="utf-8")
    except Exception as e:
        logger.warning(f"Failed to save notification: {e}")
    return notif


def send_telegram_alert(message: str, bot_token: str | None = None, chat_id: str | None = None) -> bool:
    """Send alert to Telegram chat via Bot API."""
    token = bot_token or os.environ.get("TELEGRAM_BOT_TOKEN")
    cid = chat_id or os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not cid:
        return False
    try:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        payload = json.dumps({
            "chat_id": cid,
            "text": message,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }).encode("utf-8")
        req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            return resp.status == 200
    except Exception as e:
        logger.warning(f"Telegram notification failed: {e}")
        return False


def send_webhook_alert(title: str, message: str, webhook_url: str | None = None, payload_extra: dict | None = None) -> bool:
    """Send alert to an external webhook (Discord, Slack, or custom URL)."""
    url = webhook_url or os.environ.get("NOTIFICATION_WEBHOOK_URL")
    if not url:
        return False
    try:
        data = {
            "title": title,
            "message": message,
            "timestamp": datetime.now(IST).isoformat(),
            **(payload_extra or {}),
        }
        if "discord.com" in url:
            data = {"content": f"**{title}**\n{message}"}
        req = urllib.request.Request(
            url,
            data=json.dumps(data).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=8) as resp:
            return resp.status in (200, 204)
    except Exception as e:
        logger.warning(f"Webhook notification failed: {e}")
        return False


def dispatch_notification(
    event_type: str,
    title: str,
    message: str,
    details: dict | None = None,
    path: str | Path = "data/notifications.json",
) -> dict:
    """Save and dispatch notification to all configured channels (Telegram, Webhook, Dashboard)."""
    notif = save_notification(event_type, title, message, details, path=path)

    # Format Telegram icon
    icon = {
        "buy": "🟢 [BUY ORDER]",
        "sell": "🔴 [SELL ORDER]",
        "stop_loss": "🚨 [STOP-LOSS BREACH]",
        "take_profit": "💰 [TAKE-PROFIT TARGET]",
        "kill_switch": "🛑 [KILL SWITCH]",
        "alert": "⚠️ [RISK ALERT]",
        "test": "🧪 [TEST NOTIFICATION]",
    }.get(event_type, "📢 [NOTIFICATION]")

    tg_msg = f"<b>{icon} {title}</b>\n\n{message}"
    send_telegram_alert(tg_msg)
    send_webhook_alert(title, message, payload_extra=details)
    return notif
