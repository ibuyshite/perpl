from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os


def load_env_file(path: str = ".env") -> None:
    env_path = Path(path)
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


def _number(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number") from exc


def _integer(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc


def _optional_integer(name: str) -> int | None:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return None
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc


def _boolean(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return raw.lower() in {"1", "true", "yes", "y"}


@dataclass
class Config:
    api_url: str
    ws_url: str
    chain_id: int
    api_key: str
    api_key_secret_hex: str
    market_id: int
    account_id: int | None
    dry_run: bool
    quote_size: float
    leverage_x: float
    quote_offset_bps: float
    requote_interval_ms: int
    min_requote_ticks: int
    max_close_slippage_bps: float
    max_open_position: float
    order_lb_offset_blocks: int
    allow_emergency_market_close: bool

    @classmethod
    def from_env(cls) -> "Config":
        load_env_file()
        return cls(
            api_url=os.getenv("PERPL_API_URL", "https://app.perpl.xyz/api"),
            ws_url=os.getenv("PERPL_WS_URL", "wss://app.perpl.xyz"),
            chain_id=_integer("PERPL_CHAIN_ID", 143),
            api_key=os.getenv("PERPL_API_KEY", ""),
            api_key_secret_hex=os.getenv("PERPL_API_KEY_SECRET", "").removeprefix("0x"),
            market_id=_integer("PERPL_MARKET_ID", 1),
            account_id=_optional_integer("PERPL_ACCOUNT_ID"),
            dry_run=_boolean("DRY_RUN", True),
            quote_size=_number("QUOTE_SIZE", 0.001),
            leverage_x=_number("LEVERAGE_X", 10),
            quote_offset_bps=_number("QUOTE_OFFSET_BPS", 2),
            requote_interval_ms=_integer("REQUOTE_INTERVAL_MS", 1500),
            min_requote_ticks=_integer("MIN_REQUOTE_TICKS", 1),
            max_close_slippage_bps=_number("MAX_CLOSE_SLIPPAGE_BPS", 3),
            max_open_position=_number("MAX_OPEN_POSITION", 0.003),
            order_lb_offset_blocks=_integer("ORDER_LB_OFFSET_BLOCKS", 20),
            allow_emergency_market_close=_boolean("ALLOW_EMERGENCY_MARKET_CLOSE", False),
        )

    def assert_live_ready(self) -> None:
        if self.dry_run:
            return
        if not self.api_key:
            raise ValueError("PERPL_API_KEY is required when DRY_RUN=false")
        if not self.api_key_secret_hex:
            raise ValueError("PERPL_API_KEY_SECRET is required when DRY_RUN=false")
        # account_id is resolved automatically from WalletSnapshot after API key sign-in
