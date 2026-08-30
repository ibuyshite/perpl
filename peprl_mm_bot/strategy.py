from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable

from .book import OrderBook, sweep_for_ioc
from .config import Config
from .models import LiquiditySide, Market, OrderFlag, OrderType, PositionStatus, PositionType
from .scaling import fmt_price, fmt_size, scale_size

SendOrder = Callable[[dict, str], bool]


@dataclass
class ManagedQuote:
    side: str  # "bid" or "ask"
    oid: int | None = None
    rq: int | None = None
    price: int | None = None
    pending: bool = False
    size: int = 0


@dataclass
class PositionState:
    """Tracks net exposure for the single market we trade."""
    side: str | None = None  # "long" or "short" or None
    size: int = 0  # scaled size
    pid: int | None = None
    entry_price: int | None = None


class InstantCloseMarketMaker:
    """
    Maker-open + instant taker-close market maker.

    Desired behaviour:
      1. Maintain at most one post-only bid and one post-only ask.
      2. On maker fill of either side → immediately send IOC close of the
         filled size (highest priority) AND cancel the opposite quote.
      3. Once position is flat, cancel any leftover orders and re-quote both sides.
      4. Never leave inventory open for more than a few hundred ms.
      5. Never permanently stop quoting because of a temporary depth problem.
    """

    def __init__(
        self,
        config: Config,
        market: Market,
        book: OrderBook,
        send_order: SendOrder,
    ) -> None:
        self.config = config
        self.market = market
        self.book = book
        self.send_order = send_order

        self.quotes: dict[str, ManagedQuote] = {
            "bid": ManagedQuote(side="bid"),
            "ask": ManagedQuote(side="ask"),
        }
        self.position = PositionState()
        self.request_id = 0
        self.frame_sn = 0
        self.account_lfr = 0
        self.last_requote_at = 0.0
        self.close_pending: dict[int, tuple[str, int]] = {}  # rq -> (side, size)
        self.close_retry_at = 0.0
        self.close_attempts = 0
        self.max_close_attempts = 12
        self.inventory_mode = False  # True while we have (or expect) exposure

    # ------------------------------------------------------------------
    # Request-ID / frame helpers
    # ------------------------------------------------------------------
    def seed_request_id(self, lfr: int) -> None:
        lfr = int(lfr or 0)
        prev = self.request_id
        self.account_lfr = max(self.account_lfr, lfr)
        self.request_id = max(self.request_id, lfr)
        if self.request_id != prev:
            print(f"[rq] synchronized local request_id={self.request_id} (lfr={lfr})")

    def bump_request_id(self, rejected_rq: int = 0) -> None:
        """Advance past a stale rq without reconnecting the websocket."""
        floor = max(self.request_id, self.account_lfr, int(rejected_rq or 0))
        self.account_lfr = max(self.account_lfr, floor)
        self.request_id = floor
        print(f"[rq] bumped local request_id={self.request_id} after stale reject")

    def _next_request_id(self) -> int:
        self.request_id = max(self.request_id, self.account_lfr) + 1
        return self.request_id

    def _next_frame_sn(self) -> int:
        self.frame_sn += 1
        return self.frame_sn

    def _account_id(self) -> int:
        if self.config.dry_run:
            return self.config.account_id or 0
        if self.config.account_id is None:
            raise ValueError("Account not resolved yet; waiting for WalletSnapshot")
        return self.config.account_id

    def _last_exec_block(self, current_block: int) -> int:
        offset = max(1, min(self.config.order_lb_offset_blocks, self.market.order_ttl_blocks))
        return int(current_block) + offset

    # ------------------------------------------------------------------
    # Position reconciliation (source of truth)
    # ------------------------------------------------------------------
    def on_positions(self, positions: list[dict]) -> None:
        """Called with PositionsSnapshot or PositionsUpdate payload (list of Position)."""
        our = None
        for pos in positions or []:
            if int(pos.get("mkt", -1)) != self.config.market_id:
                continue
            st = int(pos.get("st", 0) or 0)
            size = int(pos.get("s", 0) or 0)
            if st == PositionStatus.OPEN and size > 0:
                our = pos
                break

        if our is None:
            # Flat
            if self.position.size > 0 or self.inventory_mode:
                print(
                    f"[position] FLAT (was {self.position.side} "
                    f"{fmt_size(self.position.size, self.market)})"
                )
            self.position = PositionState()
            self.inventory_mode = False
            self.close_attempts = 0
            self.close_pending.clear()
            # Force requote as soon as we are flat
            self.last_requote_at = 0
            return

        sd = int(our.get("sd", 0) or 0)
        side = "long" if sd == PositionType.LONG else "short" if sd == PositionType.SHORT else None
        size = int(our.get("s", 0) or 0)
        pid = int(our.get("pid", 0) or 0) or None
        ep = int(our.get("ep", 0) or 0) or None

        prev_side = self.position.side
        prev_size = self.position.size
        self.position = PositionState(side=side, size=size, pid=pid, entry_price=ep)

        if side != prev_side or size != prev_size:
            print(
                f"[position] {side} size={fmt_size(size, self.market)} "
                f"pid={pid} entry={fmt_price(ep, self.market) if ep else '-'}"
            )

        if size > 0:
            self.inventory_mode = True
            # Cancel any open maker quotes while we have inventory
            self._cancel_all_quotes("inventory detected")
            # Kick off / retry close immediately
            self._ensure_close()

    # ------------------------------------------------------------------
    # Order lifecycle
    # ------------------------------------------------------------------
    def on_order(self, order: dict) -> None:
        if int(order.get("mkt", -1)) != self.config.market_id:
            return

        rq = int(order.get("rq", 0) or 0)
        oid = int(order.get("oid", 0) or 0)
        st = int(order.get("st", 0) or 0)
        removed = bool(order.get("r"))
        order_type = int(order.get("t", 0) or 0)

        # --- Close-order lifecycle ---
        if rq in self.close_pending:
            side, requested = self.close_pending[rq]
            filled = int(order.get("fs", 0) or 0)
            terminal = st in (3, 4, 5, 6, 7, 10) or removed

            if terminal:
                remaining = max(0, requested - filled)
                self.close_pending.pop(rq, None)
                if remaining > 0 and st in (3, 5, 6):  # partial or cancelled/expired IOC
                    print(
                        f"[close] remainder rq={rq} filled={fmt_size(filled, self.market)} "
                        f"remaining={fmt_size(remaining, self.market)}; will retry"
                    )
                    self.close_attempts += 1
                    self._retry_close_soon()
                elif remaining > 0 and st == 7:
                    print(f"[close] FAILED rq={rq} sr={order.get('sr')}; will retry")
                    self.close_attempts += 1
                    self._retry_close_soon()
                else:
                    print(f"[close] done rq={rq} st={st} filled={fmt_size(filled, self.market)}")
            return

        # --- Match to our managed quotes ---
        matched: ManagedQuote | None = None
        for quote in self.quotes.values():
            if (quote.rq is not None and quote.rq == rq) or (quote.oid is not None and quote.oid == oid):
                matched = quote
                break

        if matched is None:
            # Any open maker order that is not our current managed quote is unwanted.
            # Always cancel it so we never leave ghost orders in the book.
            if not removed and st in (1, 2, 3) and oid and order_type in (
                OrderType.OPEN_LONG,
                OrderType.OPEN_SHORT,
            ):
                side = "bid" if order_type == OrderType.OPEN_LONG else "ask"
                candidate = self.quotes[side]
                if candidate.oid is None and not self.inventory_mode and not candidate.pending:
                    # Safe to adopt only if we have no managed order on this side yet
                    candidate.oid = oid
                    candidate.rq = rq
                    candidate.price = int(order.get("p", 0) or 0)
                    candidate.pending = False
                    candidate.size = int(order.get("os", order.get("s", 0)) or 0)
                    print(
                        f"[reconcile] adopt {side} oid={oid} rq={rq} "
                        f"price={fmt_price(candidate.price, self.market)}"
                    )
                else:
                    print(f"[reconcile] cancel extra {side} oid={oid}")
                    self._cancel_oid(oid, int(order.get("os", order.get("s", 0)) or 0), f"extra {side}")
            return

        if removed or st in (5, 6, 7):
            matched.oid = None
            matched.price = None
            matched.pending = False
            matched.rq = rq
            matched.size = 0
            # PostOnly rejected because book moved (sr=13 CrossesBook) or other fail.
            # Force a fresh quote on the next book tick instead of waiting the full interval.
            sr = int(order.get("sr", 0) or 0)
            if st == 7 and sr in (13, 40, 42):  # CrossesBook / PriceOutOfRange / SizeOutOfRange
                self.last_requote_at = 0
                print(f"[quote-reject] {matched.side} sr={sr} → will requote immediately")
            return

        if oid:
            matched.oid = oid
        if order.get("p") is not None:
            matched.price = int(order["p"])
        matched.pending = False
        if order.get("os") is not None or order.get("s") is not None:
            matched.size = int(order.get("os", order.get("s", 0)) or 0)

        if st in (2, 3, 4, 8, 9, 10):
            print(
                f"[order] {matched.side} oid={matched.oid} rq={rq} st={st} "
                f"price={fmt_price(matched.price or 0, self.market)}"
            )

    def on_fill(self, fill: dict) -> None:
        if int(fill.get("mkt", -1)) != self.config.market_id:
            return

        size = int(fill.get("s", 0) or 0)
        if size <= 0:
            return

        liq = int(fill.get("l", 0) or 0)
        order_type = int(fill.get("t", 0) or 0)
        price = int(fill.get("p", 0) or 0)

        # We only care about maker fills of opening orders for the fast path.
        # Position updates remain the ultimate source of truth.
        if liq != LiquiditySide.MAKER:
            return
        if order_type not in (OrderType.OPEN_LONG, OrderType.OPEN_SHORT):
            return

        side = "bid" if order_type == OrderType.OPEN_LONG else "ask"
        print(
            f"[FILL MAKER] {side} size={fmt_size(size, self.market)} "
            f"@ {fmt_price(price, self.market)}"
        )

        # Clear the filled quote immediately
        q = self.quotes[side]
        q.oid = None
        q.price = None
        q.pending = False
        q.size = 0

        # Enter inventory mode and cancel the opposite quote.
        # Do NOT close from fill size here — partial fills would race and over/under-close.
        # PositionsUpdate is the source of truth; _ensure_close uses full current position size.
        self.inventory_mode = True
        opposite = "ask" if side == "bid" else "bid"
        self._cancel_quote(opposite, reason="maker fill on opposite")
        # Kick close from current known position (or next PositionsUpdate will)
        self._ensure_close()

    def on_status_error(self, rq: int, code: int, error: str) -> None:
        print(f"[command rejected] rq={rq} code={code} error={error}")
        for q in self.quotes.values():
            if q.rq == rq:
                q.pending = False
                q.oid = None
                q.price = None
                q.size = 0
        if rq in self.close_pending:
            side, size = self.close_pending.pop(rq)
            print(f"[close] status error; will retry {side} size={fmt_size(size, self.market)}")
            self.close_attempts += 1
            self._retry_close_soon()

    # ------------------------------------------------------------------
    # Quoting
    # ------------------------------------------------------------------
    def requote(self, current_block: int, force: bool = False) -> None:
        # Never quote while we still have (or are closing) inventory
        if self.inventory_mode or self.position.size > 0 or self.close_pending:
            # While in inventory, keep trying to close on every book update
            if self.position.size > 0 or self.close_pending:
                self._ensure_close()
            return

        now = time.monotonic() * 1000
        if not force and now - self.last_requote_at < self.config.requote_interval_ms:
            return

        best_bid = self.book.best_bid()
        best_ask = self.book.best_ask()
        if best_bid is None or best_ask is None or current_block <= 0:
            return

        quote_size = scale_size(self.config.quote_size, self.market)
        max_open_size = scale_size(self.config.max_open_position, self.market)
        if quote_size > max_open_size:
            raise ValueError("QUOTE_SIZE cannot be greater than MAX_OPEN_POSITION")

        # Prefer exact best for tight maker fills. When spread is only 1 tick,
        # still quote both sides; CrossesBook rejects are handled by immediate re-quote.
        if self.config.quote_offset_bps == 0:
            bid = best_bid.price
            ask = best_ask.price
        else:
            raw_bid = int(best_bid.price * (1 - self.config.quote_offset_bps / 10_000))
            raw_ask = int(best_ask.price * (1 + self.config.quote_offset_bps / 10_000) + 0.999999)
            bid = min(raw_bid, best_ask.price - 1)
            ask = max(raw_ask, best_bid.price + 1)

        # Safety: never cross our own quotes
        if bid >= ask:
            return
        # Also never send a PostOnly that is already through the opposite side
        if bid >= best_ask.price or ask <= best_bid.price:
            return

        changed = False
        changed |= self._upsert_quote("bid", bid, quote_size, current_block)
        changed |= self._upsert_quote("ask", ask, quote_size, current_block)
        if changed:
            self.last_requote_at = now

    def _upsert_quote(self, side: str, price: int, size: int, current_block: int) -> bool:
        quote = self.quotes[side]

        if quote.pending:
            return False

        # Already live at desired price → nothing to do
        if quote.oid is not None and quote.price == price and quote.size == size:
            return False

        open_type = OrderType.OPEN_LONG if side == "bid" else OrderType.OPEN_SHORT
        rq = self._next_request_id()
        order: dict = {
            "mt": 22,
            "sn": self._next_frame_sn(),
            "rq": rq,
            "mkt": self.config.market_id,
            "acc": self._account_id(),
            "t": OrderType.CHANGE if quote.oid else open_type,
            "p": price,
            "s": size,
            "fl": OrderFlag.POST_ONLY,
            "lv": int(self.config.leverage_x * 100),
            "lb": self._last_exec_block(current_block),
        }
        if quote.oid is not None:
            order["oid"] = quote.oid

        action = "change" if quote.oid is not None else "place"
        sent = self.send_order(
            order,
            f"{action} {side} @ {fmt_price(price, self.market)} size={fmt_size(size, self.market)}",
        )
        if not sent:
            return False

        quote.rq = rq
        quote.price = price
        quote.size = size
        # In dry-run there is no OrdersUpdate, so clear pending immediately
        # so the bot continues re-quoting as the book moves.
        quote.pending = not self.config.dry_run
        if self.config.dry_run:
            quote.oid = 900000 + rq  # fake oid so CHANGE path is exercised next time
        print(
            f"[quote] {action} {side} price={fmt_price(price, self.market)} "
            f"rq={rq} oid={quote.oid or '-'}"
        )
        return True

    # ------------------------------------------------------------------
    # Cancel helpers
    # ------------------------------------------------------------------
    def _cancel_quote(self, side: str, reason: str = "") -> None:
        quote = self.quotes[side]
        if quote.oid is None:
            quote.pending = False
            quote.price = None
            quote.size = 0
            return
        if quote.pending:
            return
        # Keep local oid until OrdersUpdate confirms removal (st=5 / r=true).
        # Clearing early loses track and leaves ghost orders in the book.
        self._cancel_oid(
            quote.oid,
            quote.size or scale_size(self.config.quote_size, self.market),
            f"{side} ({reason})",
        )
        quote.pending = True  # treat as in-flight cancel so we do not place another

    def _cancel_all_quotes(self, reason: str) -> None:
        self._cancel_quote("bid", reason)
        self._cancel_quote("ask", reason)

    def _cancel_oid(self, oid: int, size: int, label: str) -> None:
        # Cancel is identified by oid only. Sending size/fl/lv often produces sr=42 SizeOutOfRange.
        rq = self._next_request_id()
        cancel = {
            "mt": 22,
            "sn": self._next_frame_sn(),
            "rq": rq,
            "mkt": self.config.market_id,
            "acc": self._account_id(),
            "t": OrderType.CANCEL,
            "oid": oid,
            "lb": 0,
        }
        self.send_order(cancel, f"CANCEL {label} oid={oid}")

    # ------------------------------------------------------------------
    # Instant taker close
    # ------------------------------------------------------------------
    def _ensure_close(self) -> None:
        """If we have position and no close currently in-flight, send one."""
        if self.position.size <= 0:
            return
        if self.close_pending:
            return  # already working on it
        now = time.monotonic()
        if now < self.close_retry_at:
            return
        side = "bid" if self.position.side == "long" else "ask"
        self._send_close(side, self.position.size, self.position.entry_price)

    def _retry_close_soon(self) -> None:
        self.close_retry_at = time.monotonic() + 0.12

    def _send_close(self, side: str, size: int, fill_price: int | None) -> None:
        if size <= 0:
            return
        if self.close_pending:
            return

        reference = int(
            fill_price
            or (
                self.book.best_bid().price
                if side == "bid" and self.book.best_bid()
                else self.book.best_ask().price
                if side == "ask" and self.book.best_ask()
                else 0
            )
        )
        if reference <= 0:
            print("[close] no reference price; waiting for book")
            self._retry_close_soon()
            return

        if side == "bid":  # closing a long → sell into bids
            sweep = sweep_for_ioc(
                self.book.bids(), size, "sell", reference, self.config.max_close_slippage_bps
            )
            order_type = OrderType.CLOSE_LONG
            label = "long"
        else:
            sweep = sweep_for_ioc(
                self.book.asks(), size, "buy", reference, self.config.max_close_slippage_bps
            )
            order_type = OrderType.CLOSE_SHORT
            label = "short"

        use_emergency = False
        if not sweep.ok:
            if self.config.allow_emergency_market_close or self.close_attempts >= 3:
                use_emergency = True
                print(
                    f"[close] depth insufficient (avail={fmt_size(sweep.available_size, self.market)}); "
                    f"using emergency market IOC (attempt={self.close_attempts})"
                )
            else:
                print(
                    f"[close-blocked] {label}: insufficient depth "
                    f"avail={fmt_size(sweep.available_size, self.market)}; "
                    f"will retry (attempt={self.close_attempts})"
                )
                self.close_attempts += 1
                self._retry_close_soon()
                return

        rq = self._next_request_id()
        order: dict = {
            "mt": 22,
            "sn": self._next_frame_sn(),
            "rq": rq,
            "mkt": self.config.market_id,
            "acc": self._account_id(),
            "t": order_type,
            "p": 0 if use_emergency else (sweep.price or 0),
            "s": size,
            "fl": OrderFlag.IOC,
            "lv": int(self.config.leverage_x * 100),
            "lb": 0,
        }
        if use_emergency or self.config.allow_emergency_market_close:
            order["ms"] = max(int(self.config.max_close_slippage_bps), 15)

        sent = self.send_order(
            order,
            f"INSTANT TAKER CLOSE {label} "
            f"@ {fmt_price(order['p'], self.market)} size={fmt_size(size, self.market)}"
            f"{' EMERGENCY' if use_emergency else ''}",
        )
        if sent:
            self.close_pending[rq] = (side, size)
            self.inventory_mode = True
            print(
                f"[close-order] rq={rq} side={side} size={fmt_size(size, self.market)} "
                f"attempt={self.close_attempts}"
            )
        else:
            self._retry_close_soon()

    def status_line(self) -> str:
        pos = (
            f"{self.position.side}:{fmt_size(self.position.size, self.market)}"
            if self.position.size
            else "flat"
        )
        bid = self.quotes["bid"]
        ask = self.quotes["ask"]
        return (
            f"pos={pos} inv={self.inventory_mode} "
            f"bid={bid.oid or '-'}@{fmt_price(bid.price, self.market) if bid.price else '-'} "
            f"ask={ask.oid or '-'}@{fmt_price(ask.price, self.market) if ask.price else '-'} "
            f"closes={len(self.close_pending)}"
        )
