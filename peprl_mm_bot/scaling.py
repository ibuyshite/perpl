from __future__ import annotations

from .models import Market


def scale_price(price: float, market: Market) -> int:
    return round(price * 10**market.price_decimals)


def unscale_price(price: int | float, market: Market) -> float:
    return price / 10**market.price_decimals


def scale_size(size: float, market: Market) -> int:
    return round(size * 10**market.size_decimals)


def unscale_size(size: int | float, market: Market) -> float:
    return size / 10**market.size_decimals


def fmt_price(price: int | None, market: Market) -> str:
    if price is None:
        return "market"
    return f"{unscale_price(price, market):.{market.price_decimals}f}"


def fmt_size(size: int, market: Market) -> str:
    return f"{unscale_size(size, market):.{market.size_decimals}f}"
