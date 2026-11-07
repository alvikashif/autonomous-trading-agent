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
    price: float  # expected fill price
