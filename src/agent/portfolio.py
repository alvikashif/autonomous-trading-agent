from __future__ import annotations

import math
from typing import Mapping

from .models import Order, Side


def plan_orders(
    targets: Mapping[str, float],
    positions: Mapping[str, int],
    prices: Mapping[str, float],
    equity: float,
    min_trade_value: float,
    cash_buffer_pct: float = 0.0,
) -> list[Order]:
    """Turn target weights into whole-share delivery orders. Sells first.

    Anything held but absent from `targets` is sold in full (this is how a stock
    that fails the quarterly Shariah re-screen gets exited).
    """
    investable = equity * (1 - cash_buffer_pct)
    sells: list[Order] = []
    buys: list[Order] = []
    for sym in sorted(set(targets) | set(positions)):
        px = prices.get(sym)
        if px is None or not math.isfinite(px) or px <= 0:
            continue
        target_qty = int(targets.get(sym, 0.0) * investable // px)
        cur = positions.get(sym, 0)
        diff = target_qty - cur
        if diff == 0:
            continue
        exiting = target_qty == 0
        if abs(diff) * px < min_trade_value and not exiting:
            continue
        if diff < 0:
            sells.append(Order(sym, Side.SELL, -diff, px))
        else:
            buys.append(Order(sym, Side.BUY, diff, px))
    return sells + buys
