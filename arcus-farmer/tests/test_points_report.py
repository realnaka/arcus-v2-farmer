"""The points report has to agree with the arithmetic it claims to do.

The number that matters is OI-time, so these tests pin the four places it can
go wrong: the bonus-window split (an ET calendar question), the units (the
venue reports microseconds, the report prints hours), the position integral
(a position still open at the end of the window is open interest), and hold
time (flat to flat, not fill to fill).
"""

from __future__ import annotations

import datetime as dt
import json
from zoneinfo import ZoneInfo

from tools.points_report import (
    bonus_seconds,
    dedupe,
    episodes,
    fills_to_series,
    is_bonus,
    load_fills_json,
    normalize_fill,
    oi_time_from_positions,
)

ET = ZoneInfo("America/New_York")


def _us(moment: dt.datetime) -> int:
    return int(moment.timestamp() * 1e6)


def _fill(trade_id, side, size, price, moment):
    return {
        "tradeId": trade_id,
        "orderId": f"o-{trade_id}",
        "marketDisplayName": "DRAM-USD",
        "side": side,
        "price": price,
        "size": size,
        "role": "MAKER",
        "createdAt": _us(moment),
    }


def test_is_bonus_uses_et_clock_and_weekend() -> None:
    weekday = dt.datetime(2026, 10, 7, tzinfo=ET)  # Wednesday
    assert is_bonus(weekday.replace(hour=22)) is True   # after 20:00 ET
    assert is_bonus(weekday.replace(hour=2)) is True    # before 04:00 ET
    assert is_bonus(weekday.replace(hour=12)) is False  # regular hours
    assert is_bonus(weekday.replace(hour=20)) is True   # boundary is bonus
    assert is_bonus(weekday.replace(hour=4)) is False   # boundary is regular
    assert is_bonus(dt.datetime(2026, 10, 10, 12, tzinfo=ET)) is True  # Saturday


def test_bonus_seconds_splits_a_straddling_interval() -> None:
    weekday = dt.datetime(2026, 10, 7, tzinfo=ET)
    inside = bonus_seconds(_us(weekday.replace(hour=22)), _us(weekday.replace(hour=23)))
    assert inside == 3600.0
    regular = bonus_seconds(_us(weekday.replace(hour=12)), _us(weekday.replace(hour=13)))
    assert regular == 0.0
    # 19:30 -> 20:30 ET is half regular hours and half bonus window.
    straddle = bonus_seconds(
        _us(weekday.replace(hour=19, minute=30)), _us(weekday.replace(hour=20, minute=30))
    )
    assert straddle == 1800.0


def test_normalize_fill_reads_the_live_payload_shape() -> None:
    """Captured shape: ``tradeId`` / ``role`` / ``marketDisplayName``."""
    raw = {
        "tradeId": "11410641",
        "orderId": "cb7aa8dea59f4a16",
        "marketId": 24,
        "marketDisplayName": "QQQ-USD",
        "side": "SELL",
        "price": "612.34",
        "size": "0.5",
        "role": "TAKER",
        "createdAt": 1791124732969000,
        "liquidation": {"method": "ADL", "liquidatedUser": "0xabc"},
    }
    fill = normalize_fill(raw)
    assert fill["symbol"] == "QQQ-USD"
    assert fill["is_maker"] is False
    assert fill["liquidation"] == "ADL"
    assert fill["size"] == 0.5


def test_integral_counts_the_still_open_position() -> None:
    """$100 for 1h, flat for 1h, then $200 for 2h with no closing fill."""
    base = dt.datetime(2026, 10, 7, 12, 0, tzinfo=ET)
    fills = [
        normalize_fill(_fill("f1", "BUY", "1", "100", base)),
        normalize_fill(_fill("f2", "SELL", "1", "110", base + dt.timedelta(hours=1))),
        normalize_fill(_fill("f3", "BUY", "2", "100", base + dt.timedelta(hours=2))),
    ]
    series = fills_to_series(fills)
    integral = oi_time_from_positions(
        series, _us(base), _us(base + dt.timedelta(hours=4))
    )
    assert integral["DRAM-USD"]["oi_dollar_hours"] == 500.0


def test_a_delta_neutral_book_earns_on_both_legs() -> None:
    """Open interest is gross of direction, which is the point of the v2 book."""
    base = dt.datetime(2026, 10, 7, 12, 0, tzinfo=ET)
    long_leg = normalize_fill(_fill("f1", "BUY", "1", "100", base))
    short_leg = normalize_fill(_fill("f2", "SELL", "1", "100", base))
    short_leg["symbol"] = "MU-USD"
    series = fills_to_series([long_leg, short_leg])
    integral = oi_time_from_positions(
        series, _us(base), _us(base + dt.timedelta(hours=1))
    )
    assert integral["DRAM-USD"]["oi_dollar_hours"] == 100.0
    assert integral["MU-USD"]["oi_dollar_hours"] == 100.0


def test_episodes_measure_flat_to_flat_hold_time() -> None:
    base = dt.datetime(2026, 10, 7, 12, 0, tzinfo=ET)
    fills = [
        normalize_fill(_fill("f1", "BUY", "1", "100", base)),
        normalize_fill(_fill("f2", "SELL", "1", "100", base + dt.timedelta(hours=3))),
        normalize_fill(_fill("f3", "BUY", "1", "100", base + dt.timedelta(hours=5))),
    ]
    spans = episodes(fills_to_series(fills), _us(base + dt.timedelta(hours=6)))
    assert spans["DRAM-USD"] == [
        (_us(base), _us(base + dt.timedelta(hours=3))),
        (_us(base + dt.timedelta(hours=5)), _us(base + dt.timedelta(hours=6))),
    ]


def test_dedupe_sorts_and_drops_paging_overlap() -> None:
    base = dt.datetime(2026, 10, 7, 12, 0, tzinfo=ET)
    later = normalize_fill(_fill("b", "BUY", "1", "100", base + dt.timedelta(minutes=5)))
    earlier = normalize_fill(_fill("a", "BUY", "1", "100", base))
    again = normalize_fill(_fill("a", "BUY", "1", "100", base))
    assert [f["id"] for f in dedupe([later, earlier, again])] == ["a", "b"]


def test_load_fills_json_accepts_both_shapes(tmp_path) -> None:
    base = dt.datetime(2026, 10, 7, 12, 0, tzinfo=ET)
    rows = [_fill("f1", "BUY", "1", "100", base)]
    array = tmp_path / "array.json"
    array.write_text(json.dumps(rows))
    wrapped = tmp_path / "wrapped.json"
    wrapped.write_text(json.dumps({"fills": rows}))
    assert load_fills_json(array)[0]["symbol"] == "DRAM-USD"
    assert load_fills_json(wrapped)[0]["price"] == 100.0
