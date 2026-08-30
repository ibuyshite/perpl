from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
from typing import Any


class MsgType(IntEnum):
    PING = 1
    STATUS_RESPONSE = 3
    SUBSCRIPTION_REQUEST = 5
    SUBSCRIPTION_RESPONSE = 6
    L2_BOOK_SNAPSHOT = 15
    L2_BOOK_UPDATE = 16
    WALLET_SNAPSHOT = 19
    ACCOUNT_UPDATE = 21
    ORDER_REQUEST = 22
    ORDERS_SNAPSHOT = 23
    ORDERS_UPDATE = 24
    FILLS_UPDATE = 25
    POSITIONS_SNAPSHOT = 26
    POSITIONS_UPDATE = 27
    ACCOUNT_STATS_UPDATE = 28
    API_KEY_SIGN_IN = 29
    HEARTBEAT = 100


class OrderType(IntEnum):
    OPEN_LONG = 1
    OPEN_SHORT = 2
    CLOSE_LONG = 3
    CLOSE_SHORT = 4
    CANCEL = 5
    CHANGE = 7


class OrderFlag(IntEnum):
    GTC = 0
    POST_ONLY = 1
    FOK = 2
    IOC = 4


class LiquiditySide(IntEnum):
    MAKER = 1
    TAKER = 2


class PositionType(IntEnum):
    UNSPECIFIED = 0
    LONG = 1
    SHORT = 2


class PositionStatus(IntEnum):
    UNSPECIFIED = 0
    OPEN = 1
    CLOSED = 2
    LIQUIDATED = 3
    DELEVERAGED = 4
    UNWOUND = 5
    FAILED = 6


@dataclass
class Market:
    id: int
    symbol: str
    order_ttl_blocks: int
    price_decimals: int
    size_decimals: int
    initial_margin: int
    maker_fee: int
    taker_fee: int

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> "Market":
        config = raw["config"]
        return cls(
            id=int(raw["id"]),
            symbol=str(raw.get("symbol") or raw.get("name") or raw["id"]),
            order_ttl_blocks=int(raw.get("order_ttl_blocks", 20)),
            price_decimals=int(config["price_decimals"]),
            size_decimals=int(config["size_decimals"]),
            initial_margin=int(config["initial_margin"]),
            maker_fee=int(config["maker_fee"]),
            taker_fee=int(config["taker_fee"]),
        )


@dataclass
class Level:
    price: int
    size: int
    orders: int
