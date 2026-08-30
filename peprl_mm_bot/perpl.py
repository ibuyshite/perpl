from __future__ import annotations

import asyncio
import base64
import json
import secrets
import time
from typing import Any, Awaitable, Callable
from urllib.request import Request, urlopen

from nacl.signing import SigningKey
import websockets

from .config import Config
from .models import Market, MsgType


def _b64url_no_padding(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


async def make_api_key_sign_in(config: Config) -> dict[str, Any]:
    timestamp = str(int(time.time() * 1000))
    nonce = _b64url_no_padding(secrets.token_bytes(16))
    canonical = "\n".join([str(config.chain_id), "trading-ws-signin", timestamp, nonce])
    private_key = SigningKey(bytes.fromhex(config.api_key_secret_hex))
    signature = private_key.sign(canonical.encode("utf-8")).signature
    return {
        "mt": MsgType.API_KEY_SIGN_IN,
        "chain_id": config.chain_id,
        "api_key": config.api_key,
        "timestamp": timestamp,
        "nonce": nonce,
        "signature": _b64url_no_padding(signature),
    }


def fetch_market(config: Config) -> Market:
    request = Request(
        f"{config.api_url}/v1/pub/context",
        headers={"User-Agent": "peprl-mm-bot/0.1"},
    )
    with urlopen(request, timeout=10) as response:
        context = json.loads(response.read().decode("utf-8"))
    for raw in context["markets"]:
        if int(raw["id"]) == config.market_id:
            return Market.from_api(raw)
    raise ValueError(f"market {config.market_id} not found in /v1/pub/context")


MessageHandler = Callable[[dict[str, Any], Any], Awaitable[None]]


async def run_ws_loop(
    url: str,
    name: str,
    on_open: Callable[[Any], Awaitable[None]],
    on_message: MessageHandler,
    on_disconnect: Callable[[], None] | None = None,
) -> None:
    delay = 1.0
    while True:
        try:
            async with websockets.connect(url, ping_interval=None) as ws:
                print(f"{name} connected")
                delay = 1.0
                await on_open(ws)
                async for raw in ws:
                    await on_message(json.loads(raw), ws)
        except Exception as exc:
            print(f"{name} disconnected: {exc}")
        finally:
            if on_disconnect is not None:
                result = on_disconnect()
                if asyncio.iscoroutine(result):
                    await result
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30.0)
