from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


@dataclass(frozen=True)
class Order:
    """Delivery (CNC) order only. No margin, no short selling, no derivatives."""

    symbol: str
    side: Side
    qty: int
    price: float  # expected fill price / reference price
    order_type: str = "LIMIT"  # "LIMIT" or "MARKET"
    slippage_buffer_pct: float = 0.003  # 0.3% max slippage buffer for marketable limit
