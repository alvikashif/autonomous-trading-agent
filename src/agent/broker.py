from __future__ import annotations

import json
import logging
import os
from abc import ABC, abstractmethod
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .models import Order, Side

logger = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")


class InsufficientFunds(Exception):
    pass


class Broker(ABC):
    @abstractmethod
    def cash(self) -> float: ...

    @abstractmethod
    def positions(self) -> dict[str, int]: ...

    @abstractmethod
    def place_order(self, order: Order) -> Any: ...


def get_last_6am_ist(now: datetime | None = None) -> datetime:
    """Return the most recent 6:00 AM IST cutoff datetime."""
    if now is None:
        now = datetime.now(IST)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc).astimezone(IST)
    else:
        now = now.astimezone(IST)
    six_am_today = now.replace(hour=6, minute=0, second=0, microsecond=0)
    if now >= six_am_today:
        return six_am_today
    return six_am_today - timedelta(days=1)


def is_token_fresh(created_at_iso: str, now: datetime | None = None) -> bool:
    """Check if token was created AFTER the most recent 6:00 AM IST reset."""
    try:
        created = datetime.fromisoformat(created_at_iso)
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc).astimezone(IST)
        else:
            created = created.astimezone(IST)
        cutoff = get_last_6am_ist(now)
        return created > cutoff
    except Exception:
        return False


class GrowwBroker(Broker):
    """Live broker adapter for Groww API with automatic daily TOTP re-authentication.

    By SEBI regulations and Groww's design, access tokens expire daily around 6:00 AM IST.
    This adapter:
    1. Tracks token creation time and automatically re-generates an access token using pyotp
       and your TOTP secret whenever the 6:00 AM reset window has passed.
    2. Caches the access token in a local JSON file to prevent redundant token requests.
    3. Seamlessly catches mid-session 401/Authentication errors and retries once with a fresh token.
    4. Strictly forces product=CNC (Cash-and-Carry delivery) on NSE segment CASH for 100% Shariah compliance.
    """

    def __init__(
        self,
        api_key: str | None = None,
        totp_secret: str | None = None,
        token_cache_path: str | Path = "data/groww_token.json",
        client: Any | None = None,
    ) -> None:
        self.api_key = (api_key or os.environ.get("GROWW_API_KEY", "")).strip()
        self.totp_secret = (totp_secret or os.environ.get("GROWW_TOTP_SECRET", "")).strip()
        self.token_cache_path = Path(token_cache_path)
        self._client = client

    def _get_cached_token(self) -> str | None:
        if not self.token_cache_path.exists():
            return None
        try:
            data = json.loads(self.token_cache_path.read_text(encoding="utf-8"))
            token = data.get("token")
            created_at = data.get("created_at")
            if token and created_at and is_token_fresh(created_at):
                return token
        except Exception as e:
            logger.warning("Failed to read token cache: %s", e)
        return None

    def _save_cached_token(self, token: str) -> None:
        try:
            self.token_cache_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "token": token,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
            self.token_cache_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        except Exception as e:
            logger.warning("Failed to write token cache: %s", e)

    def authenticate(self, force: bool = False) -> str:
        """Authenticate with Groww API, using cached token if fresh, or generating via TOTP."""
        if not force:
            cached = self._get_cached_token()
            if cached:
                self._init_client(cached)
                return cached

        if not self.api_key or not self.totp_secret:
            raise ValueError(
                "Groww API credentials missing. Please set api_key and totp_secret in config.yaml "
                "or export GROWW_API_KEY and GROWW_TOTP_SECRET."
            )

        import pyotp
        from growwapi import GrowwAPI

        totp_code = pyotp.TOTP(self.totp_secret).now()
        access_token = GrowwAPI.get_access_token(api_key=self.api_key, totp=totp_code)
        if not access_token or not isinstance(access_token, str):
            raise RuntimeError(f"Unexpected token response from Groww API: {access_token}")

        self._save_cached_token(access_token)
        self._init_client(access_token)
        return access_token

    def _init_client(self, token: str) -> None:
        from growwapi import GrowwAPI

        self._client = GrowwAPI(token)

    def _ensure_client(self) -> Any:
        if self._client is None:
            self.authenticate()
        return self._client

    def _execute_with_retry(self, fn, *args, **kwargs) -> Any:
        self._ensure_client()
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            from growwapi.groww.exceptions import (
                GrowwAPIAuthenticationException,
                GrowwAPIAuthorisationException,
            )

            is_auth_error = (
                isinstance(e, (GrowwAPIAuthenticationException, GrowwAPIAuthorisationException))
                or "401" in str(e)
                or "unauthorized" in str(e).lower()
                or "token" in str(e).lower()
            )
            if is_auth_error:
                logger.info("Groww API token expired or unauthorized. Regenerating via TOTP...")
                self.authenticate(force=True)
                return fn(*args, **kwargs)
            raise

    def cash(self) -> float:
        """Fetch available cash balance for delivery (CNC) equity trading."""
        resp = self._execute_with_retry(self._ensure_client().get_available_margin_details)
        if not isinstance(resp, dict):
            return 0.0
        equity_details = resp.get("equity_margin_details", {})
        if "cnc_balance_available" in equity_details:
            return float(equity_details["cnc_balance_available"])
        if "clear_cash" in resp:
            return float(resp["clear_cash"])
        return 0.0

    def positions(self) -> dict[str, int]:
        """Fetch current equity holdings quantity from Demat account."""
        resp = self._execute_with_retry(self._ensure_client().get_holdings_for_user)
        holdings = []
        if isinstance(resp, dict):
            if "payload" in resp and isinstance(resp["payload"], dict):
                holdings = resp["payload"].get("holdings", [])
            elif "holdings" in resp:
                holdings = resp["holdings"]
        pos: dict[str, int] = {}
        for h in holdings:
            sym = h.get("trading_symbol", "")
            qty = int(float(h.get("quantity", 0)))
            if sym and qty > 0:
                pos[sym.upper()] = qty
        return pos

    def place_order(self, order: Order) -> dict:
        """Place a strictly Shariah-compliant Delivery (CNC) equity order on NSE."""
        if order.qty <= 0:
            raise ValueError(f"Invalid order quantity: {order.qty}")
        if order.side not in (Side.BUY, Side.SELL):
            raise ValueError(f"Invalid order side: {order.side}")

        from growwapi import GrowwAPI

        txn_type = (
            GrowwAPI.TRANSACTION_TYPE_BUY
            if order.side == Side.BUY
            else GrowwAPI.TRANSACTION_TYPE_SELL
        )

        client = self._ensure_client()
        resp = self._execute_with_retry(
            client.place_order,
            validity=GrowwAPI.VALIDITY_DAY,
            exchange=GrowwAPI.EXCHANGE_NSE,
            segment=GrowwAPI.SEGMENT_CASH,
            product=GrowwAPI.PRODUCT_CNC,
            order_type=GrowwAPI.ORDER_TYPE_MARKET,
            quantity=int(order.qty),
            trading_symbol=order.symbol.upper(),
            transaction_type=txn_type,
        )
        return resp
