"""Auto-deleveraging (ADL) exposure model for the Arcus perp legs.

Arcus exposes ADL on the wire but publishes neither the trigger rule nor the
counterparty ranking, so nothing in this module is a true probability.  It puts
numbers on the three things that have to line up before we get deleveraged, and
each piece is kept separate so it can be audited against the tape:

1. **Fact.** A forced closure on our own fill carries
   ``liquidation.method == "ADL"`` (or ``LIQUIDATION`` when *we* were the
   account being closed).  No modelling involved - see :func:`forced_from_fill`.
2. **Market pressure.** The engine's forced-close orders are visible in the
   public trade tape as order ids with the ``liq:`` prefix, so forced closures
   per hour in our market can be counted directly - see
   :func:`forced_from_trade` and :func:`pressure_window`.
3. **Our attractiveness.** ADL picks counterparties on the opposite side of a
   failed position, conventionally ranked by profit and leverage.  We can rank
   ourselves against same-side holders we can see through the public
   leaderboard and positions endpoints - see :func:`peer_percentile`.

The composite in :func:`exposure_score` is a **reporting device**: it orders
observations so an operator can see when the setup turns favourable for ADL.
It is not a calibrated probability and must not gate an order by itself.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Iterable, Mapping, Sequence

_D = Decimal

#: Prefix of the engine's synthetic order ids for forced closures.  The docs
#: call this an internal detail, so treat a miss as "no forced closure seen"
#: rather than "none happened".
FORCED_ORDER_PREFIX = "liq:"

#: ``liquidation.method`` values seen on the wire.
ADL = "ADL"
LIQUIDATION = "LIQUIDATION"


def is_forced_order_id(order_id: str) -> bool:
    return order_id.startswith(FORCED_ORDER_PREFIX)


_PLAIN_ORDER_ID = re.compile(r"^[0-9a-f]{16}$")


def is_unrecognised_order_id(order_id: str) -> bool:
    """True for an id that is neither an ordinary order nor the forced prefix.

    The tape-based pressure count rests entirely on the ``liq:`` prefix, which
    we have never actually seen on a live event.  Counting the ids that match
    neither shape turns "the engine renamed the prefix" from a silent zero into
    a visible number.
    """
    if not order_id or is_forced_order_id(order_id):
        return False
    return not _PLAIN_ORDER_ID.match(order_id)


@dataclass(frozen=True)
class ForcedClosure:
    """One forced-close fill: either ours (from the account feed) or the tape's."""

    market: str
    price: Decimal
    size: Decimal
    ts_us: int
    order_id: str
    method: str = "UNKNOWN"          # ADL / LIQUIDATION / UNKNOWN (tape)
    forced_leg: str = ""             # MAKER / TAKER / ACCOUNT
    forced_party: str | None = None  # who was closed
    counterparty: str | None = None  # their counterparty (the ADL cohort)
    trade_id: str | None = None
    #: Side of the engine's own close order (SELL closes a long).
    order_side: str = ""
    #: True when the *other* leg of this forced close is our own account, i.e.
    #: we just traded against a liquidation and are sitting in the cohort ADL
    #: would draw from if the engine had to deleverage instead of match.
    counterparty_is_us: bool = False

    @property
    def notional(self) -> Decimal:
        return abs(self.price * self.size)

    @property
    def counts_against_us(self) -> bool:
        """True when the engine closed us rather than merely matched us."""
        return self.method in {ADL, LIQUIDATION}

    @property
    def we_were_counterparty(self) -> bool:
        """True when we traded against a forced close (the ADL candidate pool)."""
        return self.method == "UNKNOWN"

    @property
    def liquidated_side(self) -> str:
        """Which side of the book was closed: ``LONG``, ``SHORT`` or ``""``.

        Only a failed position on the *opposite* side can make us the ADL
        counterparty, so this is what separates market pain from our pain.
        """
        if self.order_side == "SELL":
            return "LONG"
        if self.order_side == "BUY":
            return "SHORT"
        return ""


def forced_from_fill(raw: Mapping[str, Any]) -> ForcedClosure | None:
    """Read the ``liquidation`` marker off our own fill, if the feed set one."""
    marker = raw.get("liquidation")
    if not isinstance(marker, Mapping):
        return None
    method = str(marker.get("method") or "").upper()
    if not method:
        return None
    liquidated = marker.get("liquidatedUser")
    return ForcedClosure(
        market=str(raw.get("marketDisplayName") or raw.get("market") or ""),
        price=_dec(raw.get("price")),
        size=_dec(raw.get("size") if raw.get("size") is not None else raw.get("quantity")),
        ts_us=int(raw.get("createdAt") or 0),
        order_id=str(raw.get("orderId") or ""),
        method=method,
        forced_leg="ACCOUNT",
        forced_party=(str(liquidated) if liquidated else None),
        trade_id=(str(raw["tradeId"]) if raw.get("tradeId") else None),
    )


def forced_from_trade(
    raw: Mapping[str, Any],
    *,
    our_address: str | None = None,
) -> ForcedClosure | None:
    """Pick the forced close out of one public trade row, if it carries one.

    A trade row is one maker/taker pairing.  Either leg can be the engine's
    synthetic close; the other leg is a voluntary counterparty - which is the
    cohort ADL draws from, so it is worth flagging when that leg is ours.
    """
    maker_id = str(raw.get("makerOrderId") or "")
    taker_id = str(raw.get("takerOrderId") or "")
    maker_forced = is_forced_order_id(maker_id)
    taker_forced = is_forced_order_id(taker_id)
    if not (maker_forced or taker_forced):
        return None
    leg = "MAKER" if maker_forced else "TAKER"
    me = (our_address or "").lower()
    forced_party = raw.get("makerAddress") if maker_forced else raw.get("takerAddress")
    counter = raw.get("takerAddress") if maker_forced else raw.get("makerAddress")
    # The row's ``side`` is the resting (maker) order's side, so the forced
    # order's own side has to be derived when the forced leg is the taker.
    maker_side = str(raw.get("side") or "").upper()
    if maker_forced:
        order_side = maker_side
    elif maker_side == "SELL":
        order_side = "BUY"
    elif maker_side == "BUY":
        order_side = "SELL"
    else:
        order_side = ""
    return ForcedClosure(
        market=str(raw.get("marketDisplayName") or raw.get("market") or ""),
        price=_dec(raw.get("price")),
        size=_dec(raw.get("size")),
        ts_us=int(raw.get("timestamp") or 0),
        order_id=maker_id if maker_forced else taker_id,
        method="UNKNOWN",
        forced_leg=leg,
        forced_party=(str(forced_party) if forced_party else None),
        counterparty=(str(counter) if counter else None),
        trade_id=(str(raw["tradeId"]) if raw.get("tradeId") else None),
        counterparty_is_us=bool(me and str(counter or "").lower() == me),
        order_side=order_side,
    )


def page_may_be_truncated(trades: Sequence[Mapping[str, Any]], *, limit: int) -> bool:
    """True when the returned page filled up and may not reach back far enough.

    ``/v1/trades`` returns a fixed-size recent page.  A busy market can consume
    the whole page inside the window we care about, so a "no forced closures"
    reading has to be labelled a floor rather than a true zero.
    """
    return bool(limit) and len(trades) >= limit


def pressure_window(
    forced: Iterable[ForcedClosure],
    *,
    now_us: int,
    window_s: int,
) -> dict[str, Any]:
    """Aggregate forced closes over a trailing window."""
    cutoff = now_us - window_s * 1_000_000
    rows = [f for f in forced if f.ts_us >= cutoff]
    by_market: dict[str, dict[str, Any]] = {}
    by_liquidated_side: dict[str, dict[str, Any]] = {}
    for f in rows:
        slot = by_market.setdefault(f.market, {"count": 0, "notional": _D(0), "our_hits": 0})
        slot["count"] += 1
        slot["notional"] += f.notional
        if f.counterparty_is_us:
            slot["our_hits"] += 1
        side_slot = by_liquidated_side.setdefault(
            f.liquidated_side, {"count": 0, "notional": _D(0)}
        )
        side_slot["count"] += 1
        side_slot["notional"] += f.notional
    return {
        "window_s": window_s,
        "count": len(rows),
        "notional": sum((f.notional for f in rows), _D(0)),
        "our_hits": sum(1 for f in rows if f.counterparty_is_us),
        "by_market": by_market,
        "by_liquidated_side": by_liquidated_side,
    }


def adverse_pressure(
    forced: Iterable[ForcedClosure],
    *,
    our_side: str,
    now_us: int,
    window_s: int,
) -> dict[str, Any]:
    """Forced-close notional on the side opposite ours over the window.

    ``our_side`` is ``LONG``/``SHORT``.  A failed position on our own side is
    market news; one on the other side is the event that drags us into the
    counterparty pool.
    """
    other = "SHORT" if our_side.upper().startswith("L") else "LONG"
    cutoff = now_us - window_s * 1_000_000
    rows = [f for f in forced if f.ts_us >= cutoff and f.liquidated_side == other]
    return {
        "side": other,
        "count": len(rows),
        "notional": sum((f.notional for f in rows), _D(0)),
        "our_hits": sum(1 for f in rows if f.counterparty_is_us),
    }


# --------------------------------------------------------------------- score


def profit_leverage_rank(*, upnl_margin_pct: float, leverage_eff: float) -> float:
    """How attractive we look as an ADL counterparty, 0..1.

    Full marks at 25% profit on margin *and* 10x effective leverage; both terms
    have to be large, because a profitable-but-unlevered book is a costly ADL
    target and a levered-but-underwater one is not a target at all.
    """
    profit = _clamp01(max(0.0, upnl_margin_pct) / 25.0)
    lev = _clamp01(leverage_eff / 10.0)
    return profit * lev


def peer_percentile(ours: float, peers: Sequence[float]) -> float | None:
    """Fraction of sampled same-side holders we out-rank on the ADL metric.

    ``None`` when the sample is too small to say anything - a two-account
    sample would otherwise read as a confident 0% or 100%.
    """
    if len(peers) < 5:
        return None
    below = sum(1 for p in peers if p < ours)
    return below / len(peers)


def exposure_score(*, attract: float, pressure: float, crowding: float) -> float:
    """0..100 composite over the three ADL preconditions.

    Attractiveness carries the most weight because it is the one we control:
    pressure and crowding are properties of the market we chose to quote.
    """
    return 100.0 * (
        0.45 * _clamp01(attract)
        + 0.35 * _clamp01(pressure)
        + 0.20 * _clamp01(crowding)
    )


def pressure_component(*, forced_notional: Decimal, oi_notional: Decimal) -> float:
    """Forced-close notional over the window, scaled against market open interest."""
    if oi_notional <= 0:
        return 0.0
    return _clamp01(float(forced_notional) / float(oi_notional) / 0.002)


def crowding_component(*, our_notional: Decimal, oi_notional: Decimal) -> float:
    """Our share of the market's open interest."""
    if oi_notional <= 0:
        return 0.0
    return _clamp01(float(our_notional) / float(oi_notional) / 0.05)


def level(score: float) -> str:
    if score >= 85:
        return "L3"
    if score >= 65:
        return "L2"
    if score >= 45:
        return "L1"
    return "OK"


LEVEL_LABEL = {"OK": "低", "L1": "中", "L2": "高", "L3": "极高"}


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, float(x)))


def _dec(value: Any) -> Decimal:
    if value in (None, ""):
        return _D(0)
    return _D(str(value))
