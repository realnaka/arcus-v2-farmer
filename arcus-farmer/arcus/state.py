"""Local mirror of the exchange book, orders, positions and account.

Everything the grid decides is read from here, never from a REST round trip.
The order book keeps its own sequence number so a dropped delta is detected
instead of silently producing a wrong best bid.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Iterable, Mapping, Sequence

from .models import Account, Fill, Market, Order, Position

_D = Decimal


def _levels(raw: Any) -> list[tuple[Decimal, Decimal]]:
    """Normalise ``[[price, size], ...]`` (strings or dicts) into tuples."""
    out: list[tuple[Decimal, Decimal]] = []
    if not raw:
        return out
    for lvl in raw:
        if isinstance(lvl, Mapping):
            price, size = lvl.get("price"), lvl.get("size")
        elif isinstance(lvl, (list, tuple)) and len(lvl) >= 2:
            price, size = lvl[0], lvl[1]
        else:
            continue
        if price in (None, ""):
            continue
        out.append((_D(str(price)), _D(str(size or 0))))
    return out


@dataclass
class Book:
    market_id: int
    symbol: str = ""
    bids: dict[Decimal, Decimal] = field(default_factory=dict)
    asks: dict[Decimal, Decimal] = field(default_factory=dict)
    last_sequence_id: int = 0
    last_update_ts: float = 0.0
    gap_count: int = 0

    @property
    def ready(self) -> bool:
        return bool(self.bids) and bool(self.asks)

    @property
    def best_bid(self) -> Decimal:
        return max(self.bids) if self.bids else _D(0)

    @property
    def best_ask(self) -> Decimal:
        return min(self.asks) if self.asks else _D(0)

    @property
    def mid(self) -> Decimal:
        if not self.ready:
            return _D(0)
        return (self.best_bid + self.best_ask) / 2

    def apply_snapshot(self, contents: Mapping[str, Any]) -> None:
        self.bids = dict(_levels(contents.get("bids")))
        self.asks = dict(_levels(contents.get("asks")))
        self.last_sequence_id = int(contents.get("lastSequenceId") or 0)
        self.last_update_ts = time.time()

    def apply_delta(self, contents: Mapping[str, Any]) -> bool:
        """Apply an incremental update.  Returns False when a gap was detected."""
        seq = int(contents.get("lastSequenceId") or 0)
        if self.last_sequence_id and seq and seq <= self.last_sequence_id:
            return True  # stale, ignore
        if self.last_sequence_id and seq and seq != self.last_sequence_id + 1:
            # We missed at least one delta: the local book is untrustworthy.
            self.gap_count += 1
            self.last_sequence_id = seq
            return False
        for price, size in _levels(contents.get("bids")):
            if size == 0:
                self.bids.pop(price, None)
            else:
                self.bids[price] = size
        for price, size in _levels(contents.get("asks")):
            if size == 0:
                self.asks.pop(price, None)
            else:
                self.asks[price] = size
        self.last_sequence_id = seq or self.last_sequence_id
        self.last_update_ts = time.time()
        return True

    def depth_notional(self, side: str, levels: int = 3) -> Decimal:
        book = self.asks if side.upper() == "BUY" else self.bids
        if not book:
            return _D(0)
        prices = sorted(book)[:levels] if side.upper() == "BUY" else sorted(book, reverse=True)[:levels]
        return sum((p * book[p] for p in prices), _D(0))


@dataclass
class LocalState:
    markets: dict[int, Market] = field(default_factory=dict)
    books: dict[int, Book] = field(default_factory=dict)
    orders: dict[str, Order] = field(default_factory=dict)
    positions: dict[int, Position] = field(default_factory=dict)
    account: Account = field(default_factory=Account)
    reference_price: dict[int, Decimal] = field(default_factory=dict)
    last_reconcile_ts: float = 0.0
    ws_connected: bool = False
    ws_last_message_ts: float = 0.0
    ws_reconnects: int = 0

    # ------------------------------------------------------------- markets
    def set_markets(self, markets: Iterable[Market]) -> None:
        for m in markets:
            self.markets[m.market_id] = m
            self.books.setdefault(m.market_id, Book(market_id=m.market_id, symbol=m.symbol))

    def market_by_symbol(self, symbol: str) -> Market | None:
        for m in self.markets.values():
            if m.symbol.upper() == symbol.upper():
                return m
        return None

    def book(self, market_id: int) -> Book:
        return self.books.setdefault(market_id, Book(market_id=market_id))

    # ------------------------------------------------------------- orders
    def upsert_order(self, order: Order) -> None:
        if not order.order_id:
            return
        prev = self.orders.get(order.order_id)
        if prev and order.filled_size < prev.filled_size:
            order.filled_size = prev.filled_size
        self.orders[order.order_id] = order

    def open_orders(self, market_id: int | None = None) -> list[Order]:
        return [
            o
            for o in self.orders.values()
            if o.open and (market_id is None or o.market_id == market_id)
        ]

    def open_orders_by_side(self, market_id: int, side: str) -> list[Order]:
        return [o for o in self.open_orders(market_id) if o.side == side.upper()]

    def replace_open_orders(self, market_id: int, orders: Sequence[Order]) -> None:
        """Reconcile a REST snapshot against the local order table."""
        seen = {o.order_id for o in orders}
        for o in orders:
            self.upsert_order(o)
        for oid, o in list(self.orders.items()):
            if o.market_id == market_id and o.open and oid not in seen:
                o.status = "CANCELED"

    # ------------------------------------------------------------- fills
    def apply_fill(self, fill: Fill) -> None:
        order = self.orders.get(fill.order_id)
        if order:
            order.filled_size = max(order.filled_size, order.filled_size + fill.size)

    # ------------------------------------------------------------- account
    def set_account(self, account: Account, positions: Sequence[Position]) -> None:
        self.account = account
        self.positions = {p.market_id: p for p in positions}
        self.last_reconcile_ts = time.time()

    def position_size(self, market_id: int) -> Decimal:
        p = self.positions.get(market_id)
        return p.size if p else _D(0)

    # ------------------------------------------------------------- reference
    def session_reference(self, market_id: int) -> Decimal:
        """Price the drawdown guard measures against (first seen mark price)."""
        ref = self.reference_price.get(market_id)
        if ref:
            return ref
        market = self.markets.get(market_id)
        price = market.mark_price if market and market.mark_price else self.book(market_id).mid
        if price:
            self.reference_price[market_id] = price
        return price

    def drawdown_pct(self, market_id: int) -> Decimal:
        ref = self.session_reference(market_id)
        market = self.markets.get(market_id)
        if not ref or not market or not market.mark_price:
            return _D(0)
        return (market.mark_price - ref) / ref * _D(100)

    # ------------------------------------------------------------- health
    def stale(self, seconds: float = 15.0) -> bool:
        return (time.time() - self.ws_last_message_ts) > seconds
