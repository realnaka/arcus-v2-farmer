from __future__ import annotations

from decimal import Decimal

from arcus.models import Fill
from engine import adl


def test_fill_marker_is_parsed_and_absent_by_default():
    plain = Fill.from_api({"tradeId": "1", "orderId": "abc", "price": "10", "size": "1"})
    assert plain.forced is False
    assert plain.is_adl is False
    assert plain.liquidation_method is None

    adl_fill = Fill.from_api(
        {
            "tradeId": "2",
            "orderId": "liq:9",
            "price": "10",
            "size": "2",
            "fee": "0",
            "liquidation": {"method": "ADL", "liquidatedUser": "0xdead"},
        }
    )
    assert adl_fill.forced is True
    assert adl_fill.is_adl is True
    assert adl_fill.liquidated_user == "0xdead"

    liq_fill = Fill.from_api(
        {
            "tradeId": "3",
            "price": "10",
            "size": "2",
            "liquidation": {"method": "LIQUIDATION"},
        }
    )
    assert liq_fill.forced is True
    assert liq_fill.is_adl is False


def test_forced_from_fill_needs_a_marker_object():
    assert adl.forced_from_fill({"tradeId": "1", "price": "1", "size": "1"}) is None
    assert adl.forced_from_fill({"liquidation": "ADL"}) is None  # not an object
    forced = adl.forced_from_fill(
        {"tradeId": "9", "price": "10", "size": "2", "liquidation": {"method": "ADL", "liquidatedUser": "0xaa"}}
    )
    assert forced is not None
    assert forced.method == adl.ADL
    assert forced.forced_party == "0xaa"
    assert forced.notional == Decimal("20")
    assert forced.counts_against_us is True


def test_forced_from_trade_flags_the_leg_that_was_forced():
    ours = "0xAbC0000000000000000000000000000000000001"
    row = {
        "marketDisplayName": "MU-USD",
        "price": "100",
        "size": "3",
        "timestamp": 1_700_000_000_000_000,
        "tradeId": "77",
        "makerOrderId": "liq:0001",
        "takerOrderId": "ffee001122334455",
        "makerAddress": "0xforced",
        "takerAddress": ours,
    }
    forced = adl.forced_from_trade(row, our_address=ours)
    assert forced is not None
    assert forced.forced_leg == "MAKER"
    assert forced.order_id == "liq:0001"
    assert forced.counterparty_is_us is True
    assert forced.we_were_counterparty is True
    assert forced.notional == Decimal("300")

    quiet = dict(row, makerOrderId="ffee001122334455", takerOrderId="aabb001122334455")
    assert adl.forced_from_trade(quiet, our_address=ours) is None

    taker_side = dict(row, makerOrderId="ffee001122334455", takerOrderId="liq:0002", makerAddress="0xmm")
    got = adl.forced_from_trade(taker_side, our_address=ours)
    assert got is not None and got.forced_leg == "TAKER" and got.counterparty_is_us is False


def test_pressure_window_keeps_only_recent_rows():
    now = 10_000_000_000
    fresh = adl.ForcedClosure("MU-USD", Decimal("100"), Decimal("2"), now - 60_000_000, "liq:1")
    stale = adl.ForcedClosure("MU-USD", Decimal("100"), Decimal("5"), now - 7_200_000_000, "liq:2")
    short = adl.ForcedClosure(
        "SKHY-USD", Decimal("50"), Decimal("1"), now - 1000, "liq:3", counterparty_is_us=True
    )
    out = adl.pressure_window([fresh, stale, short], now_us=now, window_s=3600)
    assert out["count"] == 2
    assert out["notional"] == Decimal("250")
    assert out["our_hits"] == 1
    assert out["by_market"]["MU-USD"]["count"] == 1


def test_forced_order_side_says_which_side_was_liquidated():
    # The row's ``side`` is the resting order's side.  A forced SELL closes a
    # long; a forced taker leg against a maker BUY is the same kind of order.
    long_closed = adl.ForcedClosure("MU-USD", Decimal("10"), Decimal("1"), 1, "liq:1", order_side="SELL")
    short_closed = adl.ForcedClosure("MU-USD", Decimal("10"), Decimal("1"), 1, "liq:2", order_side="BUY")
    unknown = adl.ForcedClosure("MU-USD", Decimal("10"), Decimal("1"), 1, "liq:3")
    assert long_closed.liquidated_side == "LONG"
    assert short_closed.liquidated_side == "SHORT"
    assert unknown.liquidated_side == ""

    row = {
        "marketDisplayName": "MU-USD",
        "price": "10",
        "size": "1",
        "side": "BUY",
        "timestamp": 1_000,
        "makerOrderId": "ffee001122334455",
        "takerOrderId": "liq:0002",
        "makerAddress": "0xmm",
        "takerAddress": "0xff",
    }
    taker_forced = adl.forced_from_trade(row)
    assert taker_forced is not None
    assert taker_forced.order_side == "SELL"   # forced taker sells into the bid
    assert taker_forced.liquidated_side == "LONG"


def test_adverse_pressure_only_counts_the_far_side():
    now = 9_000_000_000
    long_closed = adl.ForcedClosure("MU-USD", Decimal("100"), Decimal("2"), now - 1000, "liq:1", order_side="SELL")
    short_closed = adl.ForcedClosure("MU-USD", Decimal("100"), Decimal("3"), now - 1000, "liq:2", order_side="BUY")
    stale = adl.ForcedClosure("MU-USD", Decimal("100"), Decimal("9"), now - 9_000_000_000, "liq:3", order_side="BUY")
    assert adl.adverse_pressure([long_closed, short_closed, stale], our_side="LONG", now_us=now, window_s=3600) == {
        "side": "SHORT",
        "count": 1,
        "notional": Decimal("300"),
        "our_hits": 0,
    }
    # Our own side failing is not our problem; it is the other side we hedge.
    assert adl.adverse_pressure([long_closed], our_side="SHORT", now_us=now, window_s=3600)["notional"] == Decimal("200")


def test_page_may_be_truncated_only_when_the_page_filled():
    assert adl.page_may_be_truncated([{}] * 1000, limit=1000) is True
    assert adl.page_may_be_truncated([{}] * 999, limit=1000) is False
    assert adl.page_may_be_truncated([], limit=1000) is False


def test_unrecognised_order_ids_surface_a_renamed_forced_prefix():
    assert adl.is_unrecognised_order_id("liq:0001") is False
    assert adl.is_unrecognised_order_id("ffee001122334455") is False
    assert adl.is_unrecognised_order_id("") is False
    assert adl.is_unrecognised_order_id("LQ-0001") is True
    assert adl.is_unrecognised_order_id("liq0001") is True   # missing colon


def test_attractiveness_needs_both_profit_and_leverage():
    assert adl.profit_leverage_rank(upnl_margin_pct=0, leverage_eff=10) == 0.0
    assert adl.profit_leverage_rank(upnl_margin_pct=50, leverage_eff=0) == 0.0
    assert adl.profit_leverage_rank(upnl_margin_pct=25, leverage_eff=10) == 1.0
    assert adl.profit_leverage_rank(upnl_margin_pct=100, leverage_eff=20) == 1.0
    half = adl.profit_leverage_rank(upnl_margin_pct=12.5, leverage_eff=5)
    assert 0.2 < half < 0.3


def test_peer_percentile_needs_a_sample_worth_ranking_against():
    assert adl.peer_percentile(0.5, [0.1, 0.2, 0.3]) is None
    assert adl.peer_percentile(0.5, [0.1] * 5) == 1.0
    assert adl.peer_percentile(0.5, [0.9] * 5) == 0.0
    assert adl.peer_percentile(0.5, [0.1, 0.2, 0.9, 0.95, 0.99]) == 0.4


def test_exposure_score_and_levels():
    assert adl.exposure_score(attract=0, pressure=0, crowding=0) == 0.0
    assert adl.exposure_score(attract=1, pressure=1, crowding=1) == 100.0
    # Attractiveness carries the largest single weight.
    assert adl.exposure_score(attract=1, pressure=0, crowding=0) > adl.exposure_score(
        attract=0, pressure=1, crowding=0
    )
    # Out-of-range inputs clamp per component rather than saturating the total:
    # a negative attractiveness is zero, not a compensated one.
    assert abs(adl.exposure_score(attract=-5, pressure=9, crowding=9) - 55.0) < 1e-9
    assert adl.level(44.9) == "OK"
    assert adl.level(45) == "L1"
    assert adl.level(65) == "L2"
    assert adl.level(85) == "L3"


def test_components_are_scaled_against_open_interest():
    assert adl.pressure_component(forced_notional=Decimal(0), oi_notional=Decimal(0)) == 0.0
    assert adl.pressure_component(forced_notional=Decimal("10"), oi_notional=Decimal("10000")) == 0.5
    assert adl.crowding_component(our_notional=Decimal("250"), oi_notional=Decimal("10000")) == 0.5
    assert adl.crowding_component(our_notional=Decimal("5000"), oi_notional=Decimal("10000")) == 1.0
