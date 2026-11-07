from __future__ import annotations

from dataclasses import dataclass

from .models import Side


@dataclass(frozen=True)
class CostConfig:
    brokerage_flat: float = 20.0
    brokerage_pct: float = 0.001
    brokerage_min: float = 5.0
    stt_pct: float = 0.001
    exchange_pct: float = 0.0000297
    sebi_pct: float = 0.000001
    stamp_buy_pct: float = 0.00015
    gst_pct: float = 0.18
    dp_charge_sell: float = 16.5  # per sell day per scrip, assumed GST-inclusive
    slippage_pct: float = 0.001   # applied by brokers to fill price, not in trade_cost


def trade_cost(side: Side, value: float, cfg: CostConfig) -> float:
    """Approximate total charges (excluding slippage) for one delivery trade."""
    if value <= 0:
        return 0.0
    brokerage = min(cfg.brokerage_flat, max(cfg.brokerage_min, cfg.brokerage_pct * value))
    exchange = cfg.exchange_pct * value
    sebi = cfg.sebi_pct * value
    gst = cfg.gst_pct * (brokerage + exchange + sebi)
    stt = cfg.stt_pct * value
    stamp = cfg.stamp_buy_pct * value if side == Side.BUY else 0.0
    dp = cfg.dp_charge_sell if side == Side.SELL else 0.0
    return brokerage + exchange + sebi + gst + stt + stamp + dp
