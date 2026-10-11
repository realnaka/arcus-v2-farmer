#!/usr/bin/env python3
"""Measure the strategy on the axis Arcus actually scores: OI-time.

The point pool tracks *sustained open interest*, not raw volume.  A scalper
that completes hundreds of rounds a day can therefore show a large turnover
number and almost no score: the notional it keeps resting on the venue is the
zero remainder between rungs.  This tool measures the thing that matters.

    OI-time    = integral of |net notional| over the window
    average OI = OI-time / window_hours
    points     = OI-time in $M-week x (rate band), see --rate-low/--rate-high

The ledger comes from the venue itself (``GET /v1/fills``), so the number does
not depend on what this repository happened to record locally:

    .venv/bin/python tools/points_report.py --config config/mainnet.yaml --days 7
    .venv/bin/python tools/points_report.py --config config/mainnet.yaml \
        --since 2026-10-08T11:11:00+08:00
    .venv/bin/python tools/points_report.py --fills-json state/fills.json --days 3

``--fills-json`` reads a dump of the same endpoint (a JSON array, or an object
with a ``fills`` key) and needs no key and no network.  Fetched pages are
cached in ``state/fills_cache.json`` so re-running over the same window is free.

Open interest is counted gross of direction: a delta-neutral basket earns on
both legs.  That is the venue's own definition of open interest.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import sys
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ET = ZoneInfo("America/New_York")
STEP_S = 300  # resolution used to split an interval against the bonus window

# Points per $1M of notional held for one week.  A non-RWA book is worth about
# 900-1,100 and a stock/RWA book about 3,000-4,000, so the default band spans
# both.  The platform average ceiling is 3,903 (250k pool / $64.06M OI), which
# the RWA figure nearly touches - treat the anchors as orders of magnitude.
RATE_LOW = 1_000.0
RATE_HIGH = 4_000.0
PLATFORM_POOL_WEEK = 250_000.0
PLATFORM_OI_USD = 64_060_000.0
#: Platform-average OI rate if the whole pool were OI-weighted.  Any credible
#: individual estimate must sit at or below it.
RATE_CEILING = PLATFORM_POOL_WEEK / (PLATFORM_OI_USD / 1e6)
WEEK_HOURS = 168.0
M = 1_000_000.0

ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / "state" / "fills_cache.json"
PAGE_WINDOW_US = 6 * 3600 * 1_000_000  # /v1/fills paging window
PAGE_LIMIT = 1000


# --------------------------------------------------------------- time helpers

def parse_when(value: str) -> dt.datetime:
    text = value.replace("Z", "+00:00")
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        parsed = dt.datetime.strptime(value, "%Y-%m-%d")
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()
    return parsed


def is_bonus(moment: dt.datetime) -> bool:
    """Whether an instant falls in the RWA off-hours bonus window.

    Arcus defines regular hours as 04:00-20:00 ET on a US trading day.
    Everything else - 20:00-04:00 ET, weekends, holidays - pays more.  Holidays
    are not modelled, so the bonus share reported here is a mild under-estimate.
    """
    local = moment.astimezone(ET)
    if local.weekday() >= 5:  # Sat / Sun ET
        return True
    return local.hour >= 20 or local.hour < 4


def bonus_seconds(start_us: int, end_us: int) -> float:
    if end_us <= start_us:
        return 0.0
    start = dt.datetime.fromtimestamp(start_us / 1e6, tz=dt.timezone.utc)
    end = dt.datetime.fromtimestamp(end_us / 1e6, tz=dt.timezone.utc)
    total = 0.0
    cursor = start
    while cursor < end:
        nxt = min(cursor + dt.timedelta(seconds=STEP_S), end)
        if is_bonus(cursor + (nxt - cursor) / 2):
            total += (nxt - cursor).total_seconds()
        cursor = nxt
    return total


# ---------------------------------------------------------------- the ledger

def _is_maker(raw: dict[str, Any]) -> bool:
    """The live payload reports maker/taker as a ``role`` string."""
    role = str(raw.get("role") or "").upper()
    if role:
        return role == "MAKER"
    return bool(raw.get("isMaker", True))


def _liquidation(raw: dict[str, Any]) -> str | None:
    marker = raw.get("liquidation")
    if not isinstance(marker, dict):
        return None
    return str(marker.get("method") or "").upper() or None


def normalize_fill(raw: dict[str, Any]) -> dict[str, Any]:
    """Pull the fields the integral needs out of a venue fill payload."""
    size = raw.get("size")
    if size is None:
        size = raw.get("quantity")
    symbol = str(
        raw.get("marketDisplayName")
        or raw.get("market")
        or raw.get("symbol")
        or raw.get("marketId")
        or "?"
    )
    return {
        "id": str(raw.get("tradeId") or raw.get("fillId") or raw.get("id") or ""),
        "symbol": symbol,
        "side": str(raw.get("side") or raw.get("orderSide") or "").upper(),
        "price": float(raw.get("price") or 0.0),
        "size": float(size or 0.0),
        "is_maker": _is_maker(raw),
        "created_at": int(raw.get("createdAt") or raw.get("created_at") or 0),
        "liquidation": _liquidation(raw),
    }


def dedupe(fills: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Sort by time and drop repeated trade ids, so paging overlaps are free."""
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for fill in sorted(fills, key=lambda f: f["created_at"]):
        key = fill["id"]
        if key and key in seen:
            continue
        if key:
            seen.add(key)
        out.append(fill)
    return out


def load_fills_json(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text())
    if isinstance(payload, dict):
        payload = payload.get("fills") or payload.get("data") or []
    return dedupe(normalize_fill(f) for f in payload)


async def fetch_fills(config_path: str, since_us: int, until_us: int) -> list[dict[str, Any]]:
    """Page ``/v1/fills`` over the window in fixed slices.

    The endpoint takes absolute microsecond bounds, so slicing by time is the
    only paging contract this tool relies on.  A slice that comes back full is
    reported: it means fills may have been dropped and the window should be
    narrowed.
    """
    from arcus import signing
    from arcus.config import load_config
    from arcus.rest import ArcusRest

    cfg = load_config(config_path)
    priv = signing.load_private_key(cfg.account.api_key_path)
    rest = ArcusRest(
        base_url=cfg.account.base_url,
        address=cfg.account.address,
        account_index=cfg.account.account_index,
        public_key=cfg.account.api_key_public or signing.public_key_hex(priv),
        private_key=priv,
    )
    await rest.start()
    raw: list[dict[str, Any]] = []
    try:
        cursor = since_us
        while cursor < until_us:
            nxt = min(cursor + PAGE_WINDOW_US, until_us)
            page = await rest.fills(from_us=cursor, to_us=nxt, limit=PAGE_LIMIT)
            rows = page.get("fills") or page.get("data") or []
            if len(rows) >= PAGE_LIMIT:
                print(f"!! {len(rows)} fills in one {PAGE_WINDOW_US / 3.6e9:.0f}h slice; "
                      f"some may be missing", file=sys.stderr)
            raw.extend(rows)
            cursor = nxt
    finally:
        await rest.close()
    return dedupe(normalize_fill(f) for f in raw)


def cached_fills(window_key: str) -> list[dict[str, Any]] | None:
    if not CACHE.exists():
        return None
    try:
        blob = json.loads(CACHE.read_text())
    except (OSError, ValueError):
        return None
    if blob.get("key") != window_key:
        return None
    return blob.get("fills") or []


def write_cache(window_key: str, fills: list[dict[str, Any]]) -> None:
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    CACHE.write_text(json.dumps({"key": window_key, "fills": fills}))


# ------------------------------------------------------------------ the maths

def fills_to_series(fills: list[dict[str, Any]]) -> dict[str, list[tuple[int, float, float]]]:
    """Rebuild each symbol's net position over time from the fill ledger.

    Returns ``{symbol: [(ts_us, signed_size, last_price), ...]}``.
    """
    out: dict[str, list[tuple[int, float, float]]] = {}
    running: dict[str, list[float]] = {}
    sign = {"BUY": 1.0, "SELL": -1.0}
    for fill in fills:
        state = running.setdefault(fill["symbol"], [0.0, 0.0])
        state[0] += sign.get(fill["side"], 0.0) * fill["size"]
        if fill["price"]:
            state[1] = fill["price"]
        out.setdefault(fill["symbol"], []).append(
            (int(fill["created_at"]), state[0], state[1])
        )
    return out


def oi_time_from_positions(
    series: dict[str, list[tuple[int, float, float]]], since_us: int, until_us: int
) -> dict[str, dict[str, float]]:
    """Integrate |net notional| over the window, split by the bonus window.

    Per market and gross of direction, which is how the venue reports open
    interest - so a delta-neutral book earns on both legs.
    """
    result: dict[str, dict[str, float]] = {}
    for symbol, points in series.items():
        total = bonus = 0.0
        previous_ts = since_us
        size, price = 0.0, 0.0
        for ts, size_now, price_now in points:
            if ts <= since_us:
                size, price = size_now, price_now
                continue
            if size and previous_ts < ts:
                lo, hi = max(previous_ts, since_us), min(ts, until_us)
                if hi > lo:
                    hours = (hi - lo) / 3.6e9
                    notional = abs(size * price)
                    total += notional * hours
                    bonus += notional * bonus_seconds(lo, hi) / 3600.0
            previous_ts = ts
            size, price = size_now, price_now
        if size and previous_ts < until_us:
            lo, hi = max(previous_ts, since_us), until_us
            if hi > lo:
                hours = (hi - lo) / 3.6e9
                notional = abs(size * price)
                total += notional * hours
                bonus += notional * bonus_seconds(lo, hi) / 3600.0
        result[symbol] = {"oi_dollar_hours": total, "bonus_oi_hours": bonus}
    return result


def episodes(
    series: dict[str, list[tuple[int, float, float]]], until_us: int
) -> dict[str, list[tuple[int, int]]]:
    """Contiguous stretches where a symbol's net position is not flat.

    Hold time is the number a human checks the engine against ("minutes" is a
    scalper, "hours" is a position), and it needs no round bookkeeping - flat
    to flat is the whole definition.
    """
    out: dict[str, list[tuple[int, int]]] = {}
    for symbol, points in series.items():
        spans: list[tuple[int, int]] = []
        opened: int | None = None
        for ts, size, _price in points:
            if abs(size) > 1e-12 and opened is None:
                opened = ts
            elif abs(size) <= 1e-12 and opened is not None:
                spans.append((opened, ts))
                opened = None
        if opened is not None and until_us > opened:
            spans.append((opened, until_us))  # still open at the end of the window
        out[symbol] = spans
    return out


def quantile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, max(0, int(round(q * (len(ordered) - 1)))))]


def load_config_caps(path: Path) -> dict[str, dict[str, float]]:
    """Read per-symbol inventory caps from a grid-style YAML config.

    Optional: the spread strategy has no per-rung ladder, so this only feeds
    the ``cap use`` column when the config carries ``strategies:``.
    """
    if not path.exists():
        return {}
    try:
        import yaml
    except ImportError:
        return {}
    data = yaml.safe_load(path.read_text()) or {}
    caps: dict[str, dict[str, float]] = {}
    for leg in data.get("strategies", []) or []:
        symbol = str(leg.get("symbol", "")).upper()
        caps[symbol] = {
            "cap": float(leg.get("order_size_notional", 0) or 0)
            * float(leg.get("max_orders", 0) or 0)
        }
    return caps


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", help="account config: used to fetch /v1/fills and for the cap column")
    ap.add_argument("--fills-json", help="offline dump of /v1/fills (array, or {'fills': [...]})")
    ap.add_argument("--days", type=float, default=7.0, help="window length (default 7)")
    ap.add_argument("--since", help="ISO start; overrides --days")
    ap.add_argument("--until", help="ISO end; defaults to now")
    ap.add_argument("--rate-low", type=float, default=RATE_LOW, help="pts per $1M-week of OI, low anchor")
    ap.add_argument("--rate-high", type=float, default=RATE_HIGH, help="pts per $1M-week of OI, high anchor")
    ap.add_argument("--no-cache", action="store_true", help="ignore state/fills_cache.json")
    args = ap.parse_args()

    now = parse_when(args.until) if args.until else dt.datetime.now().astimezone()
    since = parse_when(args.since) if args.since else now - dt.timedelta(days=args.days)
    since_us, until_us = int(since.timestamp() * 1e6), int(now.timestamp() * 1e6)
    window_h = max((until_us - since_us) / 3.6e9, 1e-9)

    if args.fills_json:
        fills = load_fills_json(Path(args.fills_json))
    else:
        if not args.config:
            ap.error("give --config (fetch from the venue) or --fills-json (offline)")
        key = f"{since_us}:{until_us}"
        cached = None if args.no_cache else cached_fills(key)
        if cached is not None:
            fills = cached
            print(f"fills       from cache {CACHE}")
        else:
            fills = asyncio.run(fetch_fills(args.config, since_us, until_us))
            write_cache(key, fills)
    fills = [f for f in fills if since_us <= f["created_at"] <= until_us]

    series = fills_to_series(fills)
    oi_by_symbol = oi_time_from_positions(series, since_us, until_us)
    spans = episodes(series, until_us)
    caps = load_config_caps(Path(args.config)) if args.config else {}

    turnover: dict[str, float] = {}
    fill_count: dict[str, int] = {}
    taker_fills = 0
    for fill in fills:
        turnover[fill["symbol"]] = turnover.get(fill["symbol"], 0.0) + fill["price"] * fill["size"]
        fill_count[fill["symbol"]] = fill_count.get(fill["symbol"], 0) + 1
        taker_fills += 0 if fill["is_maker"] else 1

    print(f"window      {since:%Y-%m-%d %H:%M} -> {now:%Y-%m-%d %H:%M}   "
          f"({window_h:.1f} h / {window_h / 24:.2f} d)")
    print(f"fills       ${sum(turnover.values()):,.0f} notional, {len(fills)} fills, "
          f"{taker_fills} of them taker")
    print()
    header = (
        f"{'symbol':10} {'fills':>6} {'turnover':>11} {'OI $h':>10} {'avg OI':>9} "
        f"{'p50 hold':>9} {'p90 hold':>9} {'bonus':>6} {'cap use':>8} {'pts':>15}"
    )
    print(header)
    print("-" * len(header))

    total_oi_h = total_bonus_h = total_turnover = 0.0
    for symbol in sorted(oi_by_symbol, key=lambda s: -oi_by_symbol[s]["oi_dollar_hours"]):
        oi_h = oi_by_symbol[symbol]["oi_dollar_hours"]
        bonus_h = oi_by_symbol[symbol]["bonus_oi_hours"]
        avg_oi = oi_h / window_h
        oi_m_weeks = oi_h / (WEEK_HOURS * M)
        lo, hi = oi_m_weeks * args.rate_low, oi_m_weeks * args.rate_high
        cap = caps.get(symbol.upper(), {}).get("cap", 0.0)
        holds = [(end - start) / 3.6e9 for start, end in spans.get(symbol, [])]
        print(
            f"{symbol:10} {fill_count.get(symbol, 0):>6} {turnover.get(symbol, 0.0):>11,.0f} "
            f"{oi_h:>10,.0f} {avg_oi:>9,.0f} "
            f"{quantile(holds, 0.5):>8.2f}h {quantile(holds, 0.9):>8.2f}h "
            f"{(bonus_h / oi_h if oi_h else 0):>6.0%} "
            f"{(f'{avg_oi / cap:.1%}' if cap else '-'):>8} {lo:>6,.2f}-{hi:<7,.2f}"
        )
        total_oi_h += oi_h
        total_bonus_h += bonus_h
        total_turnover += turnover.get(symbol, 0.0)

    all_holds = [(end - start) / 3.6e9 for spans_ in spans.values() for start, end in spans_]
    avg_oi = total_oi_h / window_h
    oi_m_weeks = total_oi_h / (WEEK_HOURS * M)
    lo, hi = oi_m_weeks * args.rate_low, oi_m_weeks * args.rate_high
    week_scale = WEEK_HOURS / window_h
    print("-" * len(header))
    print(f"{'TOTAL':10} {len(fills):>6} {total_turnover:>11,.0f} {total_oi_h:>10,.0f} {avg_oi:>9,.0f}")
    print()

    print(
        f"hold time   p10 {quantile(all_holds, .10):.2f}h  p50 {quantile(all_holds, .50):.2f}h  "
        f"p90 {quantile(all_holds, .90):.2f}h   max {max(all_holds or [0.0]):.2f}h"
    )
    print(f"OI-time     {oi_m_weeks:.6f} $M-week   (avg OI ${avg_oi:,.0f} resting through the window)")
    if total_oi_h:
        print(f"bonus share {total_bonus_h / total_oi_h:.0%} of OI-time sits in the 20:00-04:00 ET "
              f"+ weekend window")
    print()
    print(f"points      {lo * week_scale:,.1f}-{hi * week_scale:,.1f} pts/week "
          f"at {args.rate_low:,.0f}-{args.rate_high:,.0f} pts per $1M-week of OI")
    print(f"            the pooled {PLATFORM_POOL_WEEK:,.0f} pts over ${PLATFORM_OI_USD / 1e6:,.0f}M "
          f"of platform OI caps the")
    print(f"            platform average at {RATE_CEILING:,.0f} pts/$M-week, and the other scored paths")
    print( "            take a share of that pool - so the low end is the honest expectation for a")
    print( "            plain account, and the top needs the RWA + off-hours multipliers plus")
    print( "            deposit/referral credit on top.")
    print()
    print("Levers, in order of effect:")
    print("  avg OI     how much notional stays open.  Raise it with capital or leverage; no")
    print("             parameter creates open interest out of nothing.  Dominant lever.")
    print("  bonus      share of OI-time inside the RWA off-hours window (20:00-04:00 ET +")
    print("             weekend).  Free multiplier; a position held 24/7 already banks ~52%.")
    print("  volume     rounds x notional.  Worth little on its own for a short-hold maker loop,")
    print("             and rapid churning is explicitly discounted by the venue.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
