"""Risk layer. Has veto over every order. Delivery only, no leverage, no shorting."""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Mapping

from .costs import CostConfig, trade_cost
from .models import Order, Side


@dataclass(frozen=True)
class RiskLimits:
    max_position_pct: float = 0.20
    max_sector_pct: float = 0.35
    daily_loss_limit_pct: float = 0.03


def _valid_price(p: object) -> bool:
    return isinstance(p, (int, float)) and math.isfinite(p) and p > 0


def check_orders(
    orders: list[Order],
    cash: float,
    positions: Mapping[str, int],
    prices: Mapping[str, float],
    sectors: Mapping[str, str],
    equity: float,
    limits: RiskLimits,
    cost_cfg: CostConfig,
    day_pnl_pct: float = 0.0,
) -> tuple[list[Order], list[tuple[Order, str]]]:
    """Return (approved, rejected_with_reason). Sells are processed first."""
    approved: list[Order] = []
    rejected: list[tuple[Order, str]] = []
    pos = dict(positions)
    cash_left = cash

    for o in (o for o in orders if o.side == Side.SELL):
        if o.qty <= 0 or o.qty > pos.get(o.symbol, 0):
            rejected.append((o, "sell exceeds holding (no short selling)"))
            continue
        if not _valid_price(o.price):
            rejected.append((o, "stale or invalid price"))
            continue
        pos[o.symbol] -= o.qty
        cash_left += o.qty * o.price - trade_cost(Side.SELL, o.qty * o.price, cost_cfg)
        approved.append(o)

    halted = day_pnl_pct <= -limits.daily_loss_limit_pct

    for o in (o for o in orders if o.side == Side.BUY):
        if halted:
            rejected.append((o, "daily loss limit hit, buys halted"))
            continue
        if o.qty <= 0 or not _valid_price(o.price):
            rejected.append((o, "stale or invalid price"))
            continue
        value = o.qty * o.price
        total = value + trade_cost(Side.BUY, value, cost_cfg)
        if total > cash_left:
            rejected.append((o, "insufficient cash (no leverage allowed)"))
            continue
        if equity > 0:
            new_pos_value = pos.get(o.symbol, 0) * o.price + value
            if new_pos_value / equity > limits.max_position_pct + 1e-9:
                rejected.append((o, f"position cap {limits.max_position_pct:.0%} exceeded"))
                continue
            sector = sectors.get(o.symbol)
            if sector:
                sector_value = value + sum(
                    q * prices[s]
                    for s, q in pos.items()
                    if sectors.get(s) == sector and _valid_price(prices.get(s))
                )
                if sector_value / equity > limits.max_sector_pct + 1e-9:
                    rejected.append((o, f"sector cap {limits.max_sector_pct:.0%} exceeded ({sector})"))
                    continue
        pos[o.symbol] = pos.get(o.symbol, 0) + o.qty
        cash_left -= total
        approved.append(o)

    return approved, rejected
