from __future__ import annotations

import pytest

from arcus.ratelimit import PoolMeter, TokenBucket, endpoint_weight


def test_published_weights():
    assert endpoint_weight("placeOrder") == 0
    assert endpoint_weight("cancelOrder") == 0
    assert endpoint_weight("account") == 2
    assert endpoint_weight("openOrders") == 20
    assert endpoint_weight("setLeverage") == 125
    assert endpoint_weight("markets") == 20


def test_batch_weight_is_floor_n_over_40():
    assert endpoint_weight("batchPlaceOrders", 39) == 0
    assert endpoint_weight("batchPlaceOrders", 40) == 1
    assert endpoint_weight("batchCancelOrders", 100) == 2


@pytest.mark.asyncio
async def test_token_bucket_never_goes_negative():
    bucket = TokenBucket(capacity=30, refill_per_second=1000)
    await bucket.acquire(20)
    await bucket.acquire(20)
    assert bucket.tokens >= 0


@pytest.mark.asyncio
async def test_free_endpoints_skip_the_bucket():
    bucket = TokenBucket(capacity=10, refill_per_second=0.0001)
    for _ in range(100):
        await bucket.acquire(0)
    assert bucket.tokens == 10


def test_pool_meter_tracks_the_flat_cancel_all_charge():
    pools = PoolMeter()
    pools.note_order_write(3)
    pools.note_cancel(2)
    pools.note_cancel_all()
    assert pools.order_remaining == 20000 - 3
    assert pools.cancel_remaining == 40000 - 2 - 1000


def test_dead_man_daily_cap():
    pools = PoolMeter(dead_man_fires_today=10)
    assert pools.can_arm_dead_man() is False
