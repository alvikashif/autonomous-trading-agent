from __future__ import annotations

from abc import ABC, abstractmethod

from .models import Order


class InsufficientFunds(Exception):
    pass


class Broker(ABC):
    @abstractmethod
    def cash(self) -> float: ...

    @abstractmethod
    def positions(self) -> dict[str, int]: ...

    @abstractmethod
    def place_order(self, order: Order) -> None: ...


class GrowwBroker(Broker):
    """Live adapter placeholder. Intentionally not implemented.

    Build this only after: paper results look sane, live API terms (static IP,
    rate limits, auth/token refresh) are confirmed from Groww docs, and every
    order is forced to product=CNC (delivery), never margin or intraday.
    """

    def cash(self) -> float:
        raise NotImplementedError("live broker not implemented yet")

    def positions(self) -> dict[str, int]:
        raise NotImplementedError("live broker not implemented yet")

    def place_order(self, order: Order) -> None:
        raise NotImplementedError("live broker not implemented yet")
