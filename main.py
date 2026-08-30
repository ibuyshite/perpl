from __future__ import annotations

import asyncio
import json
import time
from typing import Any

from peprl_mm_bot.book import OrderBook
from peprl_mm_bot.config import Config
from peprl_mm_bot.models import MsgType, OrderType
from peprl_mm_bot.perpl import fetch_market, make_api_key_sign_in, run_ws_loop
from peprl_mm_bot.strategy import InstantCloseMarketMaker

config = Config.from_env()
config.assert_live_ready()

book = OrderBook()
current_block = 0
trading_ws: Any | None = None
trading_ready = False
strategy: InstantCloseMarketMaker

# Serialize commands so request/order lifecycle stays deterministic.
# Close / cancel always jump the queue (priority 0).
command_queue: asyncio.PriorityQueue[tuple[int, int, dict, str]] = asyncio.PriorityQueue()
command_seq = 0
command_sender_task: asyncio.Task | None = None
command_in_flight: dict | None = None
command_wakeup = asyncio.Event()


async def _command_sender() -> None:
    global command_in_flight
    while True:
        _priority, _seq, order, reason = await command_queue.get()
        try:
            while not trading_ready or trading_ws is None:
                await asyncio.sleep(0.05)

            command_in_flight = order
            payload = json.dumps(order, separators=(",", ":"))
            print(
                f"[send] {reason}: rq={order['rq']} sn={order['sn']} "
                f"type={order['t']} oid={order.get('oid', '-') }"
            )
            await trading_ws.send(payload)

            try:
                await asyncio.wait_for(command_wakeup.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                print(
                    f"[command-timeout] rq={order['rq']}; "
                    "reconnecting trading websocket for reconciliation"
                )
                if trading_ws is not None:
                    try:
                        await trading_ws.close()
                    except Exception:
                        pass
            finally:
                command_wakeup.clear()
                command_in_flight = None
        except Exception as exc:
            command_in_flight = None
            print(f"[command-sender] {exc}")
        finally:
            command_queue.task_done()


def send_order(order: dict, reason: str) -> bool:
    global command_seq
    if config.dry_run:
        print(f"[dry-run] {reason}: {json.dumps(order, separators=(',', ':'))}")
        return True
    if trading_ws is None or not trading_ready:
        print(f"[send-blocked] trading websocket/account not ready: {reason}")
        return False
    # Closes and cancels jump ahead of quote changes.
    priority = 0 if (
        reason.startswith("INSTANT TAKER CLOSE")
        or reason.startswith("CANCEL")
    ) else 1
    command_seq += 1
    command_queue.put_nowait((priority, command_seq, order, reason))
    return True


def can_quote() -> bool:
    return config.dry_run or trading_ready


def _select_account(accounts: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not accounts:
        print("No exchange account found for this API key's wallet.")
        return None

    if config.account_id is not None:
        account = next(
            (item for item in accounts if int(item.get("id", -1)) == config.account_id),
            None,
        )
        if account is None:
            ids = ", ".join(str(item.get("id")) for item in accounts)
            print(f"PERPL_ACCOUNT_ID={config.account_id} not found: {ids}")
            return None
        return account

    if len(accounts) > 1:
        ids = ", ".join(str(item.get("id")) for item in accounts)
        print(f"Multiple accounts found ({ids}); using first account id={accounts[0]['id']}.")
    return accounts[0]


async def on_market_open(ws: Any) -> None:
    await ws.send(
        json.dumps(
            {
                "mt": MsgType.SUBSCRIPTION_REQUEST,
                "subs": [
                    {"stream": f"heartbeat@{config.chain_id}", "subscribe": True},
                    {"stream": f"order-book@{config.market_id}", "subscribe": True},
                ],
            }
        )
    )


async def on_market_message(message: dict[str, Any], _ws: Any) -> None:
    global current_block
    mt = message.get("mt")
    if mt in {MsgType.L2_BOOK_SNAPSHOT, MsgType.L2_BOOK_UPDATE}:
        book.apply(message)
        if can_quote():
            strategy.requote(current_block)
    elif mt == MsgType.HEARTBEAT:
        current_block = int(message.get("h", 0))
        if can_quote():
            strategy.requote(current_block)
    elif mt == MsgType.SUBSCRIPTION_RESPONSE:
        print(f"market subscriptions: {message.get('subs')}")


async def on_trading_open(ws: Any) -> None:
    global trading_ws, trading_ready
    trading_ws = ws
    trading_ready = False
    command_wakeup.clear()
    await ws.send(json.dumps(await make_api_key_sign_in(config)))
    print("[trading] authentication frame sent")


async def on_trading_disconnect() -> None:
    global trading_ws, trading_ready, command_in_flight
    trading_ws = None
    trading_ready = False
    command_in_flight = None
    command_wakeup.set()
    print("[trading] websocket disconnected; quoting paused until re-auth")


async def on_trading_message(message: dict[str, Any], ws: Any) -> None:
    global current_block, trading_ready
    mt = message.get("mt")

    if mt == MsgType.WALLET_SNAPSHOT:
        account = _select_account(message.get("as") or [])
        if account:
            config.account_id = int(account["id"])
            strategy.seed_request_id(int(account.get("lfr", 0)))
            trading_ready = True
            print(
                f"account ready: id={account['id']} lfr={account.get('lfr')} "
                f"ft={account.get('ft')} bal={account.get('b')}"
            )

    elif mt == MsgType.ACCOUNT_UPDATE and int(message.get("id", -1)) == int(
        config.account_id or -1
    ):
        if message.get("lfr") is not None:
            strategy.seed_request_id(int(message["lfr"]))

    elif mt in {MsgType.ORDERS_SNAPSHOT, MsgType.ORDERS_UPDATE}:
        for order in message.get("d") or []:
            strategy.on_order(order)

            sr = int(order.get("sr", 0) or 0)
            st = int(order.get("st", 0) or 0)
            if sr == 32 and st == 7:
                # Stale rq only. Do NOT drop the trading socket that pauses
                # quoting and can leave a close in-flight. Just advance local rq.
                rejected_rq = int(order.get("rq", 0) or 0)
                print(
                    f"[rq-stale] server rejected rq={rejected_rq} with sr=32 "
                    "(OrderDescIdTooLow); bumping local rq, staying connected"
                )
                strategy.bump_request_id(rejected_rq)
                continue

            if st == 7:
                print(
                    f"[order failed] rq={order.get('rq')} sr={sr} "
                    f"price={order.get('p')} t={order.get('t')}"
                )

    elif mt == MsgType.FILLS_UPDATE:
        for fill in message.get("d") or []:
            strategy.on_fill(fill)

    elif mt in {MsgType.POSITIONS_SNAPSHOT, MsgType.POSITIONS_UPDATE}:
        # Source of truth for inventory. Always react.
        strategy.on_positions(message.get("d") or [])

    elif mt == MsgType.STATUS_RESPONSE:
        status = message.get("status") or {}
        cid = int(message.get("cid", 0) or 0)
        code = int(status.get("code", 0) or 0)
        if code != 0:
            print(f"[STATUS ERROR] cid={cid} code={code} error={status.get('error')}")
            if command_in_flight is not None and int(command_in_flight.get("sn", -1)) == cid:
                strategy.on_status_error(
                    int(command_in_flight.get("rq", 0)),
                    code,
                    str(status.get("error", "")),
                )
        else:
            print(f"[STATUS OK] cid={cid}")
        if command_in_flight is not None and int(command_in_flight.get("sn", -1)) == cid:
            command_wakeup.set()

    elif mt == MsgType.HEARTBEAT:
        current_block = int(message.get("h", 0))


async def main() -> None:
    global strategy, command_sender_task

    market = fetch_market(config)
    strategy = InstantCloseMarketMaker(config, market, book, send_order)

    print(
        f"loaded {market.symbol} market={market.id} "
        f"mode={'dry-run' if config.dry_run else 'live'} "
        f"price_decimals={market.price_decimals} size_decimals={market.size_decimals}"
    )
    print(
        f"maker_fee_micros={market.maker_fee} taker_fee_micros={market.taker_fee} "
        f"quote_size={config.quote_size} leverage={config.leverage_x}x"
    )
    print(
        f"[strategy] EXACT BEST BID/ASK offset={config.quote_offset_bps}bps "
        f"requote={config.requote_interval_ms}ms "
        f"close_slippage={config.max_close_slippage_bps}bps "
        f"emergency_close={config.allow_emergency_market_close}"
    )
    print(
        "Behaviour: quote both sides ? maker fill ? INSTANT IOC close + cancel opposite "
        "? wait until flat ? re-quote both sides."
    )

    command_sender_task = asyncio.create_task(_command_sender())

    tasks = [
        run_ws_loop(
            f"{config.ws_url}/ws/v1/market-data",
            "market-data",
            on_market_open,
            on_market_message,
        )
    ]

    if not config.dry_run:
        tasks.append(
            run_ws_loop(
                f"{config.ws_url}/ws/v1/trading",
                "trading",
                on_trading_open,
                on_trading_message,
                on_trading_disconnect,
            )
        )
    else:
        print("dry run mode: trading websocket disabled; no real orders will be sent")

    try:
        await asyncio.gather(*tasks)
    finally:
        if command_sender_task:
            command_sender_task.cancel()
            await asyncio.gather(command_sender_task, return_exceptions=True)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nBot stopped by user.")
