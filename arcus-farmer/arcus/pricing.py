"""Tick / quantum math for the maker grid.

The grid only ever quotes two prices: an opening price one tick inside the
opposite side, and a closing price at least ``take_profit_bps`` away from the
fill.  Rounding direction matters - rounding toward the fill would silently
drop the round below the profit target, and rounding the wrong way on a
maker order would make it marketable and get it rejected by ``ALO``.
"""

from __future__ import annotations

from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR

ONE_BP = Decimal("0.0001")


def to_ticks(price: Decimal, tick: Decimal) -> int:
    n = Decimal(price) / Decimal(tick)
    if n != n.to_integral_value():
        raise ValueError(f"price {price} is not a multiple of tick {tick}")
    return int(n)


def round_up_to_tick(price: Decimal, tick: Decimal) -> Decimal:
    return (Decimal(price) / Decimal(tick)).to_integral_value(rounding=ROUND_CEILING) * Decimal(tick)


def round_down_to_tick(price: Decimal, tick: Decimal) -> Decimal:
    return (Decimal(price) / Decimal(tick)).to_integral_value(rounding=ROUND_FLOOR) * Decimal(tick)


def bps_between(frm: Decimal, to: Decimal) -> Decimal:
    """Signed distance from ``frm`` to ``to`` in basis points."""
    return (Decimal(to) - Decimal(frm)) / Decimal(frm) * Decimal(10000)


def open_price(side: str, best_bid: Decimal, best_ask: Decimal, tick: Decimal) -> Decimal:
    """Opening quote one tick inside the opposite side (stays a maker order)."""
    side = side.upper()
    if side == "BUY":
        raw = Decimal(best_ask) - Decimal(tick)
    elif side == "SELL":
        raw = Decimal(best_bid) + Decimal(tick)
    else:
        raise ValueError(f"unknown side {side}")
    # Snap toward the inside of the spread so we never cross.
    return round_down_to_tick(raw, tick) if side == "BUY" else round_up_to_tick(raw, tick)


def close_price(
    *,
    side: str,
    fill_price: Decimal,
    take_profit_bps: Decimal,
    tick: Decimal,
    best_bid: Decimal | None = None,
    best_ask: Decimal | None = None,
) -> Decimal:
    """Closing quote for a filled opening order.

    ``side`` is the *opening* side.  A filled BUY is closed with a SELL, which
    must rest at or above ``fill * (1 + tp)`` (rounded up, so rounding can
    never eat into the target) and above the current best bid so it stays a
    maker order.
    """
    side = side.upper()
    fill = Decimal(fill_price)
    tp = Decimal(take_profit_bps) * ONE_BP

    if side == "BUY":
        target = fill * (Decimal(1) + tp)
        price = round_up_to_tick(target, tick)
        if best_bid is not None:
            price = max(price, round_up_to_tick(Decimal(best_bid) + Decimal(tick), tick))
    elif side == "SELL":
        target = fill * (Decimal(1) - tp)
        price = round_down_to_tick(target, tick)
        if best_ask is not None:
            price = min(price, round_down_to_tick(Decimal(best_ask) - Decimal(tick), tick))
    else:
        raise ValueError(f"unknown side {side}")
    return price


def target_close_distance_ok(*, side: str, fill_price: Decimal, price: Decimal, take_profit_bps: Decimal) -> bool:
    """True when the rounded closing price still meets the profit target."""
    signed = bps_between(fill_price, price)
    return signed >= Decimal(take_profit_bps) if side.upper() == "BUY" else -signed >= Decimal(take_profit_bps)


def grid_step_ok(*, side: str, new_close: Decimal, worst_close: Decimal, grid_step_bps: Decimal) -> bool:
    """Enforce spacing between consecutive closing orders.

    ``worst_close`` is the closest already-resting closing price on the side
    that a new opening would add to: the lowest closing price for a BUY grid.
    """
    step = Decimal(grid_step_bps) * ONE_BP
    if side.upper() == "BUY":
        return Decimal(new_close) <= Decimal(worst_close) * (Decimal(1) - step)
    return Decimal(new_close) >= Decimal(worst_close) * (Decimal(1) + step)


def size_for_notional(notional: Decimal, price: Decimal, step: Decimal) -> Decimal:
    """Round a USD notional down to a tradable size (multiple of ``step``)."""
    raw = Decimal(notional) / Decimal(price)
    return round_down_to_tick(raw, step)


def size_for_min_notional(price: Decimal, min_notional: Decimal, step: Decimal) -> Decimal:
    """Smallest tradable size whose notional still clears ``min_notional``.

    Rounding *down* to the step can land a hair under the exchange minimum -
    at $5 notional on a $773 market that is the difference between an order and
    a rejection - so the minimum case rounds up instead.
    """
    raw = Decimal(min_notional) / Decimal(price)
    return round_up_to_tick(raw, step)
