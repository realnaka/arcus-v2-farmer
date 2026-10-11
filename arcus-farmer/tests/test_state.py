from __future__ import annotations

from decimal import Decimal

from arcus.models import Account, Fill, Market, Order, Position
from arcus.state import Book, LocalState


def test_book_snapshot_and_delta():
    b = Book(market_id=1)
    b.apply_snapshot({"bids": [["100.00", "5"]], "asks": [["100.02", "3"]], "lastSequenceId": 10})
    assert b.best_bid == Decimal("100.00")
    assert b.best_ask == Decimal("100.02")
    assert b.ready

    assert b.apply_delta({"bids": [["100.01", "2"]], "asks": [], "lastSequenceId": 11}) is True
    assert b.best_bid == Decimal("100.01")

    # size 0 removes a level
    assert b.apply_delta({"bids": [["100.01", "0"]], "asks": [], "lastSequenceId": 12}) is True
    assert b.best_bid == Decimal("100.00")


def test_book_detects_a_sequence_gap():
    b = Book(market_id=1)
    b.apply_snapshot({"bids": [["100", "1"]], "asks": [["101", "1"]], "lastSequenceId": 10})
    assert b.apply_delta({"bids": [["99", "1"]], "asks": [], "lastSequenceId": 15}) is False
    assert b.gap_count == 1


def test_stale_delta_is_ignored():
    b = Book(market_id=1)
    b.apply_snapshot({"bids": [["100", "1"]], "asks": [["101", "1"]], "lastSequenceId": 10})
    assert b.apply_delta({"bids": [["98", "1"]], "asks": [], "lastSequenceId": 9}) is True
    assert b.best_bid == Decimal("100")


def test_depth_notional_uses_the_touch():
    b = Book(market_id=1)
    b.apply_snapshot({"bids": [["100", "2"], ["99", "3"]], "asks": [["101", "1"], ["102", "4"]], "lastSequenceId": 1})
    assert b.depth_notional("BUY", levels=1) == Decimal("101")
    assert b.depth_notional("BUY", levels=2) == Decimal("509")


def _market() -> Market:
    return Market.from_api(
        {
            "marketId": 24, "marketDisplayName": "QQQ-USD", "category": "INDICES", "status": "ONLINE",
            "tickSize": "0.01", "stepSize": "0.0000001", "minOrderSize": "0.001", "minOrderNotional": "5",
            "maxOrderSize": "1000", "initialMarginFraction": "0.04", "maintenanceMarginFraction": "0.03",
            "offHoursInitialMarginFraction": "0.06", "isOutsideRth": False,
            "volume24hNotional": "1000000", "trades24h": 100, "openInterest": "10",
            "fundingRate": "0.000005", "markPrice": "750.00", "oraclePrice": "750.01", "lastTradePrice": "750.00",
        }
    )


def test_drawdown_uses_the_first_seen_mark_as_reference():
    state = LocalState()
    state.set_markets([_market()])
    assert state.session_reference(24) == Decimal("750.00")
    # the live market object is mutable so streaming frames can update it
    state.markets[24].mark_price = Decimal("735.00")
    assert state.drawdown_pct(24) == Decimal("-2.00")


def test_account_margin_usage():
    acct = Account.from_api({"equity": "1000", "freeCollateral": "800", "positions": []})
    assert acct.margin_usage == Decimal("0.2")


def test_account_parses_the_websocket_frame_shape():
    """The live ``account`` channel calls equity ``accountEquity``.

    Captured from mainnet on 2026-10-04.  Missing this field reads equity as 0,
    which collapses the inventory cap to 0 and silently blocks all opening.
    """
    ws_frame = {
        "isSnapshot": True,
        "address": "0x1111111111111111111111111111111111111111",
        "accountIndex": 0,
        "lastSequenceId": 2,
        "netQuoteBalance": "30",
        "freeCollateral": "30",
        "accountEquity": "30",
        "netDeposits": "30",
        "positions": {},
    }
    acct = Account.from_api(ws_frame)
    assert acct.equity == Decimal("30")
    assert acct.free_collateral == Decimal("30")
    assert acct.margin_usage == Decimal("0")


def test_account_accepts_positions_as_a_dict_or_a_list():
    as_dict = Account.from_api({"equity": "10", "positions": {"24": {"marketId": 24, "size": "0.5"}}})
    assert as_dict.positions[0].market_id == 24
    as_list = Account.from_api({"accountEquity": "10", "positions": [{"marketId": 9, "size": "1"}]})
    assert as_list.positions[0].market_id == 9
    assert as_list.equity == Decimal("10")


def test_account_unrealized_pnl_falls_back_to_the_position_sum():
    acct = Account.from_api(
        {"accountEquity": "10", "positions": [{"marketId": 24, "size": "1", "unrealizedPnl": "-2.5"}]}
    )
    assert acct.unrealized_pnl == Decimal("-2.5")


def test_fill_parses_the_live_payload_shape():
    """Captured from mainnet on 2026-10-04 (the first real fill).

    The id field is ``tradeId`` and maker/taker arrives as ``role``.  Reading
    ``fillId`` / ``isMaker`` yields an empty id, which made the dispatcher drop
    every live fill.
    """
    live_fill = {
        "tradeId": "11410641",
        "orderId": "cb7aa8dea59f4a16",
        "address": "0x1111111111111111111111111111111111111111",
        "accountIndex": 0,
        "marketId": 24,
        "marketDisplayName": "QQQ-USD",
        "clientId": "main-QQQ-USD-open-1791124420390-1",
        "side": "BUY",
        "originalSize": "0.0066586",
        "size": "0.0066586",
        "price": "750.91",
        "fee": "0",
        "closedPnl": "0",
        "role": "MAKER",
        "remainingSize": "0",
        "positionEffect": "OPEN_LONG",
        "createdAt": 1791124437275592,
        "sequenceNumber": 5,
    }
    fill = Fill.from_api(live_fill)
    assert fill.fill_id == "11410641"
    assert fill.order_id == "cb7aa8dea59f4a16"
    assert fill.market_id == 24
    assert fill.size == Decimal("0.0066586")
    assert fill.price == Decimal("750.91")
    assert fill.fee == Decimal("0")
    assert fill.is_maker is True
    assert fill.side == "BUY"


def test_position_parses_the_live_payload_shape():
    """Captured from mainnet: ``averageEntryPrice`` + an unsigned size + ``side``."""
    live = {
        "address": "0x1111111111111111111111111111111111111111",
        "accountIndex": 0,
        "marketId": 24,
        "marketDisplayName": "QQQ-USD",
        "side": "LONG",
        "size": "0.0066586",
        "averageEntryPrice": "750.91",
        "leverage": "10",
        "marginMode": "CROSS",
        "marginUsed": "0.499974298",
        "positionValueNotional": "4.999742982",
        "unrealizedPnl": "-0.000266344",
        "markPx": "750.87",
    }
    pos = Position.from_api(live)
    assert pos.size == Decimal("0.0066586")
    assert pos.entry_price == Decimal("750.91")       # not zero
    assert pos.unrealized_pnl == Decimal("-0.000266344")
    assert Position.from_api({**live, "side": "SHORT"}).size == Decimal("-0.0066586")


def test_account_reads_positions_keyed_by_market_id():
    acct = Account.from_api(
        {"equity": "30", "positions": {"24": {"marketId": 24, "side": "LONG", "size": "1",
                                              "averageEntryPrice": "100"}}}
    )
    assert acct.positions[0].market_id == 24
    assert acct.positions[0].entry_price == Decimal("100")


def test_order_parses_the_live_payload_shape():
    """Captured from mainnet openOrders: originalSize, not quantity/size."""
    live = {
        "orderId": "7897c8ff58ba4218",
        "clientId": "main-QQQ-USD-adopt-1791124732969-1",
        "address": "0x1111111111111111111111111111111111111111",
        "marketId": 24,
        "marketDisplayName": "QQQ-USD",
        "side": "SELL",
        "type": "LIMIT",
        "status": "OPEN",
        "price": "751.14",
        "originalSize": "0.0066586",
        "filledSize": "0",
        "remainingSize": "0.0066586",
        "timeInForce": "ALO",
        "goodTilTime": "1795012732970578",
        "reduceOnly": True,
        "createdAt": 1791124733178737,
    }
    order = Order.from_api(live)
    assert order.size == Decimal("0.0066586")       # not zero
    assert order.remaining == Decimal("0.0066586")
    assert order.filled_size == Decimal("0")
    assert order.reduce_only is True
    assert order.side == "SELL"
    assert order.open is True


def test_a_partially_filled_order_reports_the_right_remaining_size():
    order = Order.from_api({"orderId": "x", "marketId": 24, "side": "BUY", "price": "10",
                            "originalSize": "1", "filledSize": "0.4", "status": "OPEN"})
    assert order.remaining == Decimal("0.6")


def test_a_partial_account_frame_must_not_be_parsed_as_a_full_snapshot():
    """Captured from mainnet: the account channel streams partial frames.

    This one carries order fields and netQuoteBalance but no accountEquity.
    Parsing it alone yields equity=0, which the risk guard reads as a 100%
    drawdown - it halted the bot while the account was actually up.
    """
    partial = {
        "clientId": "main-SOL-USD-open-1791126383645-1",
        "goodTilTime": "1795016783645443",
        "id": "7f3a1c9e5b2d4a86",
        "marketDisplayName": "SOL-USD",
        "marketId": 3,
        "netQuoteBalance": "29.998514398",
        "orderState": "OPEN",
        "originalSize": "0.041086",
        "price": "121.697",
        "remainingSize": "0.041086",
        "sequenceNumber": 42,
        "side": "BUY",
        "timeInForce": "ALO",
        "type": "LIMIT",
    }
    naive = Account.from_api(partial)
    assert naive.equity == Decimal("0")          # the dangerous reading

    # the merge the supervisor performs instead
    previous = Account.from_api({"accountEquity": "30.002866704", "freeCollateral": "29.5"})
    merged_raw = {
        "equity": str(previous.equity),
        "freeCollateral": str(previous.free_collateral),
        **{k: v for k, v in partial.items() if v is not None},
    }
    merged = Account.from_api(merged_raw)
    assert merged.equity == Decimal("30.002866704")


def test_position_and_reference_reporting():
    state = LocalState()
    state.set_markets([_market()])
    state.set_account(Account.from_api({"equity": "1000", "freeCollateral": "900"}), [Position.from_api({"marketId": 24, "size": "0.5"})])
    assert state.position_size(24) == Decimal("0.5")
    assert state.position_size(999) == Decimal("0")
