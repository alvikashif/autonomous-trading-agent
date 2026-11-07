from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping

import pandas as pd

from .broker import Broker, InsufficientFunds
from .costs import CostConfig, trade_cost
from .models import Order, Side


class PaperBroker(Broker):
    """Simulated broker with realistic costs and slippage. State persists to JSON."""

    def __init__(self, state_path: str | Path | None = None, cost_cfg: CostConfig | None = None):
        self.cost_cfg = cost_cfg or CostConfig()
        self.state_path = Path(state_path) if state_path else None
        self._cash = 0.0
        self._positions: dict[str, int] = {}
        self.trades: list[dict] = []
        self.total_costs = 0.0
        self.deposits: list[dict] = []        # {"date": "YYYY-MM-DD", "amount": float | None}
        self.last_rebalance: str | None = None
        if self.state_path and self.state_path.exists():
            self._load()

    # --- Broker interface ---
    def cash(self) -> float:
        return self._cash

    def positions(self) -> dict[str, int]:
        return {s: q for s, q in self._positions.items() if q > 0}

    def place_order(self, order: Order, ts: str | None = None) -> None:
        slip = self.cost_cfg.slippage_pct
        fill = order.price * (1 + slip) if order.side == Side.BUY else order.price * (1 - slip)
        value = fill * order.qty
        cost = trade_cost(order.side, value, self.cost_cfg)
        if order.qty <= 0:
            raise ValueError("qty must be positive")
        if order.side == Side.BUY:
            if value + cost > self._cash + 1e-9:
                raise InsufficientFunds(f"need {value + cost:.2f}, have {self._cash:.2f}")
            self._cash -= value + cost
            self._positions[order.symbol] = self._positions.get(order.symbol, 0) + order.qty
        else:
            if order.qty > self._positions.get(order.symbol, 0):
                raise ValueError("cannot sell more than held (no short selling)")
            self._cash += value - cost
            self._positions[order.symbol] -= order.qty
        self.total_costs += cost + abs(fill - order.price) * order.qty
        self.trades.append(
            {
                "ts": ts or datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "symbol": order.symbol,
                "side": order.side.value,
                "qty": order.qty,
                "fill_price": round(fill, 4),
                "cost": round(cost, 4),
            }
        )

    # --- helpers ---
    def deposit(self, amount: float, date: str | None = None) -> None:
        if amount <= 0:
            raise ValueError("deposit must be positive")
        self._cash += amount
        self.deposits.append({"date": date or datetime.now(timezone.utc).date().isoformat(), "amount": amount})

    def deposited_in_month(self, date: str) -> bool:
        """True if a deposit is already recorded in the same calendar month as `date`."""
        return any(d["date"][:7] == date[:7] for d in self.deposits)

    def mark_rebalanced(self, date: str) -> None:
        self.last_rebalance = date

    def next_rebalance(self, months: int) -> str | None:
        if not self.last_rebalance:
            return None
        return (pd.Timestamp(self.last_rebalance) + pd.DateOffset(months=months)).date().isoformat()

    def rebalance_due(self, today: str, months: int) -> bool:
        nxt = self.next_rebalance(months)
        return nxt is None or pd.Timestamp(today) >= pd.Timestamp(nxt)

    def equity(self, prices: Mapping[str, float]) -> float:
        held = sum(q * prices[s] for s, q in self.positions().items() if s in prices)
        return self._cash + held

    # --- persistence ---
    def save(self) -> None:
        if not self.state_path:
            return
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(
            json.dumps(
                {
                    "cash": self._cash,
                    "positions": self.positions(),
                    "total_costs": self.total_costs,
                    "deposits": self.deposits,
                    "last_rebalance": self.last_rebalance,
                    "trades": self.trades,
                },
                indent=2,
            )
        )

    def _load(self) -> None:
        d = json.loads(self.state_path.read_text())
        self._cash = float(d["cash"])
        self._positions = {k: int(v) for k, v in d["positions"].items()}
        self.total_costs = float(d.get("total_costs", 0.0))
        self.trades = list(d.get("trades", []))
        self.deposits = list(d.get("deposits", []))
        self.last_rebalance = d.get("last_rebalance")
        if self.trades:  # state files from before these fields existed
            first = self.trades[0]["ts"][:10]
            if not self.deposits:
                self.deposits = [{"date": first, "amount": None}]
            if not self.last_rebalance:
                self.last_rebalance = first
