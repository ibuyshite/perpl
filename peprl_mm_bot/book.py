from __future__ import annotations

from dataclasses import dataclass

from .models import Level


@dataclass
class SweepResult:
    ok: bool
    available_size: int
    price: int | None = None
    vwap: float | None = None


class OrderBook:
    def __init__(self) -> None:
        self._bids: dict[int, Level] = {}
        self._asks: dict[int, Level] = {}

    def apply(self, message: dict) -> None:
        self._merge(self._bids, message.get("bid", []))
        self._merge(self._asks, message.get("ask", []))

    def best_bid(self) -> Level | None:
        bids = self.bids()
        return bids[0] if bids else None

    def best_ask(self) -> Level | None:
        asks = self.asks()
        return asks[0] if asks else None

    def bids(self) -> list[Level]:
        return sorted(self._bids.values(), key=lambda level: level.price, reverse=True)

    def asks(self) -> list[Level]:
        return sorted(self._asks.values(), key=lambda level: level.price)

    def _merge(self, target: dict[int, Level], raw_levels: list[dict]) -> None:
        for raw in raw_levels:
            level = Level(price=int(raw["p"]), size=int(raw["s"]), orders=int(raw["o"]))
            if level.orders == 0 or level.size <= 0:
                target.pop(level.price, None)
            else:
                target[level.price] = level


def sweep_for_ioc(
    levels: list[Level],
    size: int,
    side: str,
    reference_price: int,
    max_slippage_bps: float,
) -> SweepResult:
    remaining = size
    filled = 0
    notional = 0
    last_price: int | None = None

    for level in levels:
        take = min(remaining, level.size)
        if take <= 0:
            continue
        filled += take
        notional += take * level.price
        remaining -= take
        last_price = level.price
        if remaining <= 0:
            break

    if filled < size or last_price is None:
        return SweepResult(ok=False, available_size=filled)

    vwap = notional / filled
    if side == "buy":
        limit = reference_price * (1 + max_slippage_bps / 10_000)
        ok = vwap <= limit
    else:
        limit = reference_price * (1 - max_slippage_bps / 10_000)
        ok = vwap >= limit

    return SweepResult(ok=ok, available_size=filled, price=last_price, vwap=vwap)
