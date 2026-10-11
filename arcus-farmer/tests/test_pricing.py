from __future__ import annotations

from decimal import Decimal

from arcus import pricing

TICK = Decimal("0.01")


def test_open_price_sits_inside_the_spread():
    # BUY one tick below the ask == join the bid
    assert pricing.open_price("BUY", Decimal("750.10"), Decimal("750.12"), TICK) == Decimal("750.11")
    assert pricing.open_price("SELL", Decimal("750.10"), Decimal("750.12"), TICK) == Decimal("750.11")


def test_open_price_never_crosses_on_a_wide_spread():
    buy = pricing.open_price("BUY", Decimal("100.00"), Decimal("100.50"), TICK)
    assert buy < Decimal("100.50")
    sell = pricing.open_price("SELL", Decimal("100.00"), Decimal("100.50"), TICK)
    assert sell > Decimal("100.00")


def test_close_price_rounds_away_from_the_fill():
    # 750.00 * 1.0003 = 750.225 -> rounds UP to a tick so the target is never eroded
    price = pricing.close_price(
        side="BUY", fill_price=Decimal("750.00"), take_profit_bps=Decimal("3"), tick=TICK
    )
    assert price == Decimal("750.23")
    assert pricing.target_close_distance_ok(
        side="BUY", fill_price=Decimal("750.00"), price=price, take_profit_bps=Decimal("3")
    )


def test_close_price_stays_a_maker_order():
    price = pricing.close_price(
        side="BUY",
        fill_price=Decimal("750.00"),
        take_profit_bps=Decimal("3"),
        tick=TICK,
        best_bid=Decimal("760.00"),  # market ran away above our target
    )
    assert price > Decimal("760.00")


def test_close_price_rounds_down_for_a_sell_grid():
    price = pricing.close_price(
        side="SELL", fill_price=Decimal("750.00"), take_profit_bps=Decimal("3"), tick=TICK
    )
    assert price == Decimal("749.77")
    assert pricing.target_close_distance_ok(
        side="SELL", fill_price=Decimal("750.00"), price=price, take_profit_bps=Decimal("3")
    )


def test_distance_check_rejects_an_eroded_target():
    assert not pricing.target_close_distance_ok(
        side="BUY", fill_price=Decimal("750.00"), price=Decimal("750.20"), take_profit_bps=Decimal("3")
    )


def test_grid_step_requires_spacing_between_closing_orders():
    assert pricing.grid_step_ok(
        side="BUY", new_close=Decimal("747.70"), worst_close=Decimal("750.00"), grid_step_bps=Decimal("30")
    )
    assert not pricing.grid_step_ok(
        side="BUY", new_close=Decimal("749.90"), worst_close=Decimal("750.00"), grid_step_bps=Decimal("30")
    )


def test_size_for_notional_rounds_down_to_step():
    size = pricing.size_for_notional(Decimal("200"), Decimal("750.22"), Decimal("0.0000001"))
    assert size == Decimal("0.2665884")
    assert size * Decimal("750.22") <= Decimal("200")


def test_rounding_down_can_undershoot_the_minimum_notional():
    """The $5 case: naive rounding gives a notional just under the floor."""
    price, step = Decimal("773.74"), Decimal("0.0000001")
    rounded_down = pricing.size_for_notional(Decimal("5"), price, step)
    assert rounded_down * price < Decimal("5")  # would be rejected by the exchange


def test_size_for_min_notional_rounds_up_to_clear_the_floor():
    price, step = Decimal("773.74"), Decimal("0.0000001")
    size = pricing.size_for_min_notional(price, Decimal("5"), step)
    assert size * price >= Decimal("5")
    assert size == Decimal("0.0064622")


def test_bps_between_signs():
    assert pricing.bps_between(Decimal("100"), Decimal("100.03")) == Decimal("3.00")
    assert pricing.bps_between(Decimal("100"), Decimal("99.97")) == Decimal("-3.00")
