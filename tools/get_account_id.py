from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import websockets

from peprl_mm_bot.config import Config
from peprl_mm_bot.models import MsgType
from peprl_mm_bot.perpl import make_api_key_sign_in


async def main() -> None:
    config = Config.from_env()
    if not config.api_key or not config.api_key_secret_hex:
        raise SystemExit("Set PERPL_API_KEY and PERPL_API_KEY_SECRET in .env first.")

    async with websockets.connect(f"{config.ws_url}/ws/v1/trading", ping_interval=None) as ws:
        await ws.send(json.dumps(await make_api_key_sign_in(config)))
        async for raw in ws:
            message: dict[str, Any] = json.loads(raw)
            mt = message.get("mt")
            if mt == MsgType.WALLET_SNAPSHOT:
                accounts = message.get("as") or []
                if not accounts:
                    print("No exchange account found for this API key's wallet.")
                    print("Open Perpl in the browser and use Deposit / Enable Trading to create one.")
                    return

                print("Perpl account id(s):")
                for account in accounts:
                    print(
                        f"  id={account.get('id')} "
                        f"fee_tier={account.get('ft')} "
                        f"balance={account.get('b')} "
                        f"last_request={account.get('lfr')}"
                    )
                return

            if mt == MsgType.STATUS_RESPONSE:
                status = message.get("status", {})
                if status.get("code") not in (None, 0):
                    raise SystemExit(f"Auth/status error: {status}")


if __name__ == "__main__":
    asyncio.run(main())
