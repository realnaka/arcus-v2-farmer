"""Per-IP weight budget and per-subaccount pool accounting.

Arcus throttles on two independent layers (docs/api-reference/rate-limits):

* a per-IP token bucket of 1,500 weight refilling at 25/s, and
* per-subaccount order / cancel pools that only order writes draw on.

Order writes cost **zero** IP weight, so the client only needs to pace its
reads.  We still track writes against the subaccount pools so the dashboard
can show when a pool is about to run dry.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field

# Published endpoint weights (docs/api-reference/rate-limits).
WEIGHT = {
    # free
    "health": 0,
    "placeOrder": 0,
    "modifyOrder": 0,
    "cancelOrder": 0,
    "cancelAllOrders": 0,
    "batchPlaceOrders": 0,
    "batchCancelOrders": 0,
    # cheap single-object reads
    "bbo": 2,
    "mids": 2,
    "account": 2,
    "positions": 2,
    "order": 2,
    "feeTiers": 2,
    "leverages": 2,
    "accountStats": 2,
    "rateLimit": 2,
    "affiliateCode": 2,
    # standard reads / management
    "prices": 20,
    "markets": 20,
    "trade": 20,
    "trades": 20,
    "candles": 20,
    "portfolio": 20,
    "openOrders": 20,
    "orders": 20,
    "fills": 20,
    "funding": 20,
    "fundingRates": 20,
    "accountTransferUpdates": 20,
    "apiKeys": 20,
    "createApiKey": 20,
    "revokeApiKey": 20,
    "userPreferences": 20,
    # heavy
    "setLeverage": 125,
    "withdraw": 125,
    "transfer": 125,
}
DEFAULT_WEIGHT = 20


def endpoint_weight(name: str, batch_len: int = 0) -> int:
    """Weight including the documented ``floor(N/40)`` charge on batch writes."""
    base = WEIGHT.get(name, DEFAULT_WEIGHT)
    if name in {"batchPlaceOrders", "batchCancelOrders"} and batch_len:
        return base + batch_len // 40
    return base


class TokenBucket:
    """Async token bucket mirroring the gateway's per-IP budget."""

    def __init__(self, capacity: float = 1500, refill_per_second: float = 25.0) -> None:
        self.capacity = float(capacity)
        self.refill_per_second = float(refill_per_second)
        self._tokens = float(capacity)
        self._updated = time.monotonic()
        self._lock = asyncio.Lock()

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self._updated
        if elapsed > 0:
            self._tokens = min(self.capacity, self._tokens + elapsed * self.refill_per_second)
            self._updated = now

    @property
    def tokens(self) -> float:
        self._refill()
        return self._tokens

    async def acquire(self, weight: float) -> None:
        if weight <= 0:
            return
        async with self._lock:
            while True:
                self._refill()
                if self._tokens >= weight:
                    self._tokens -= weight
                    return
                deficit = weight - self._tokens
                await asyncio.sleep(deficit / self.refill_per_second)

    def penalise(self, seconds: float) -> None:
        """Drain the bucket - used when the gateway answers 429 on a read."""
        self._refill()
        self._tokens = 0.0
        self._updated = time.monotonic() + max(0.0, seconds)


@dataclass
class PoolMeter:
    """Tracks (but does not hard-enforce) the per-subaccount order/cancel pools."""

    order_pool: int = 20000
    cancel_pool: int = 40000
    order_spent: int = 0
    cancel_spent: int = 0
    _day: str = ""
    dead_man_fires_today: int = 0
    max_dead_man_fires_per_day: int = 10

    def note_order_write(self, n: int = 1) -> None:
        self.order_spent += n

    def note_cancel(self, n: int = 1) -> None:
        self.cancel_spent += n

    def note_cancel_all(self) -> None:
        # cancelAllOrders charges the cancel pool a flat 1,000.
        self.cancel_spent += 1000

    @property
    def order_remaining(self) -> int:
        return max(0, self.order_pool - self.order_spent)

    @property
    def cancel_remaining(self) -> int:
        return max(0, self.cancel_pool - self.cancel_spent)

    def can_arm_dead_man(self) -> bool:
        return self.dead_man_fires_today < self.max_dead_man_fires_per_day


@dataclass
class RateLimitSnapshot:
    ip_tokens: float = 0.0
    order_remaining: int = 0
    cancel_remaining: int = 0
    dead_man_fires_today: int = 0
    raw: dict = field(default_factory=dict)
