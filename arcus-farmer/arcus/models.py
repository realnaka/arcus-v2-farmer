"""Typed views over Arcus REST/WS payloads."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Mapping, Sequence

_D = Decimal


def _d(value: Any, default: str = "0") -> Decimal:
    if value is None or value == "":
        return _D(default)
    return _D(str(value))


def _i(value: Any, default: int = 0) -> int:
    if value is None or value == "":
        return default
    return int(value)


@dataclass(frozen=True)
class TradingHours:
    start_seconds_of_day: int
    end_seconds_of_day: int
    timezone: str
    is_overnight: bool = False

    @classmethod
    def from_api(cls, raw: Mapping[str, Any] | None) -> "TradingHours | None":
        if not raw:
            return None
        return cls(
            start_seconds_of_day=_i(raw.get("startSecondsOfDay")),
            end_seconds_of_day=_i(raw.get("endSecondsOfDay")),
            timezone=str(raw.get("timezone", "America/New_York")),
            is_overnight=bool(raw.get("isOvernight", False)),
        )


# Not frozen: the ``markets`` WebSocket channel streams mark/oracle/funding
# updates into the live instance held by the state object.
@dataclass
class Market:
    market_id: int
    symbol: str
    category: str
    status: str
    tick_size: Decimal
    step_size: Decimal
    min_order_size: Decimal
    min_order_notional: Decimal
    max_order_size: Decimal
    initial_margin_fraction: Decimal
    maintenance_margin_fraction: Decimal
    off_hours_initial_margin_fraction: Decimal
    is_outside_rth: bool
    regular_trading_hours: TradingHours | None
    volume_24h_notional: Decimal
    trades_24h: int
    open_interest: Decimal
    funding_rate: Decimal
    next_funding_at: int
    oracle_price: Decimal
    mark_price: Decimal
    last_trade_price: Decimal
    upper_trading_bound: Decimal | None = None
    lower_trading_bound: Decimal | None = None
    open_interest_cap_notional: Decimal | None = None
    # Per-market liquidation penalty (ppm of fill notional) that the engine
    # routes to the insurance fund.  ``null``/absent on the wire means the
    # market has no penalty configured, i.e. the fund takes nothing from it.
    liquidation_fee_ppm: Decimal | None = None

    @classmethod
    def from_api(cls, raw: Mapping[str, Any]) -> "Market":
        def opt_dec(key: str) -> Decimal | None:
            v = raw.get(key)
            return None if v in (None, "") else _D(v)

        return cls(
            market_id=_i(raw.get("marketId")),
            symbol=str(raw.get("marketDisplayName") or raw.get("symbol") or ""),
            category=str(raw.get("category", "")),
            status=str(raw.get("status", "")),
            tick_size=_d(raw.get("tickSize")),
            step_size=_d(raw.get("stepSize"), "0.00000001"),
            min_order_size=_d(raw.get("minOrderSize")),
            min_order_notional=_d(raw.get("minOrderNotional")),
            max_order_size=_d(raw.get("maxOrderSize"), "1000000000"),
            initial_margin_fraction=_d(raw.get("initialMarginFraction")),
            maintenance_margin_fraction=_d(raw.get("maintenanceMarginFraction")),
            off_hours_initial_margin_fraction=_d(raw.get("offHoursInitialMarginFraction")),
            is_outside_rth=bool(raw.get("isOutsideRth", False)),
            regular_trading_hours=TradingHours.from_api(raw.get("regularTradingHours")),
            volume_24h_notional=_d(raw.get("volume24hNotional")),
            trades_24h=_i(raw.get("trades24h")),
            open_interest=_d(raw.get("openInterest")),
            funding_rate=_d(raw.get("fundingRate")),
            next_funding_at=_i(raw.get("nextFundingAt")),
            oracle_price=_d(raw.get("oraclePrice")),
            mark_price=_d(raw.get("markPrice")),
            last_trade_price=_d(raw.get("lastTradePrice")),
            upper_trading_bound=opt_dec("upperTradingBound"),
            lower_trading_bound=opt_dec("lowerTradingBound"),
            open_interest_cap_notional=opt_dec("openInterestCapNotional"),
            liquidation_fee_ppm=opt_dec("liquidationFeePpm"),
        )

    @property
    def active_initial_margin_fraction(self) -> Decimal:
        return self.off_hours_initial_margin_fraction if self.is_outside_rth else self.initial_margin_fraction

    def tp_ticks(self, take_profit_bps: Decimal) -> Decimal:
        """How many ticks the profit target spans at the mark price."""
        if self.tick_size == 0 or self.mark_price == 0:
            return _D(0)
        return self.mark_price * (Decimal(take_profit_bps) / _D(10000)) / self.tick_size


ORDER_OPEN_STATES = {"OPEN", "NEW", "PENDING", "PARTIALLY_FILLED", "CANCELING"}
ORDER_DONE_STATES = {"FILLED", "CANCELED", "CANCELLED", "REJECTED", "EXPIRED"}


@dataclass
class Order:
    order_id: str
    market_id: int
    side: str
    price: Decimal
    size: Decimal
    filled_size: Decimal = _D(0)
    status: str = "OPEN"
    reduce_only: bool = False
    tif: str = "ALO"
    client_id: str | None = None
    created_at: int = 0
    updated_at: int = 0

    @property
    def open(self) -> bool:
        return self.status.upper() in ORDER_OPEN_STATES

    @property
    def remaining(self) -> Decimal:
        return max(_D(0), self.size - self.filled_size)

    @classmethod
    def from_api(cls, raw: Mapping[str, Any]) -> "Order":
        # Live field names: originalSize / filledSize / remainingSize /
        # reduceOnly.  Missing originalSize reads the size as 0, which makes an
        # existing closing order look like it covers nothing - and then every
        # restart places another one.
        size = raw.get("originalSize")
        if size in (None, ""):
            size = raw.get("quantity", raw.get("size"))
        filled = raw.get("filledSize")
        if filled in (None, ""):
            filled = raw.get("filledQuantity")
        return cls(
            order_id=str(raw.get("orderId") or raw.get("id") or ""),
            market_id=_i(raw.get("marketId")),
            side=str(raw.get("orderSide") or raw.get("side") or "").upper(),
            price=_d(raw.get("price")),
            size=_d(size),
            filled_size=_d(filled),
            status=str(raw.get("status") or raw.get("orderStatus") or "OPEN").upper(),
            reduce_only=bool(raw.get("reduceOnly", False)),
            tif=str(raw.get("timeInForce") or "ALO").upper(),
            client_id=(str(raw["clientId"]) if raw.get("clientId") else None),
            created_at=_i(raw.get("createdAt")),
            updated_at=_i(raw.get("updatedAt")),
        )


@dataclass
class Fill:
    fill_id: str
    order_id: str
    market_id: int
    side: str
    price: Decimal
    size: Decimal
    fee: Decimal
    is_maker: bool
    created_at: int
    client_id: str | None = None
    # Forced-closure marker.  ``liquidation.method`` is ``LIQUIDATION`` when we
    # were the liquidated account and ``ADL`` when we were the deleveraged
    # counterparty; ``liquidated_user`` names the account that was closed.  The
    # feed omits the object entirely on voluntary fills, so a missing value is
    # the normal case and never a parsing failure.
    liquidation_method: str | None = None
    liquidated_user: str | None = None

    @classmethod
    def from_api(cls, raw: Mapping[str, Any]) -> "Fill":
        # The live payload names the id ``tradeId`` (``fillId`` never appears);
        # maker/taker comes back as a ``role`` string, not an ``isMaker`` flag.
        role = str(raw.get("role") or "").upper()
        is_maker = role == "MAKER" if role else bool(raw.get("isMaker", True))
        marker = raw.get("liquidation")
        marker = marker if isinstance(marker, Mapping) else {}
        method = str(marker.get("method") or "").upper() or None
        liquidated = marker.get("liquidatedUser")
        return cls(
            fill_id=str(raw.get("tradeId") or raw.get("fillId") or raw.get("id") or ""),
            order_id=str(raw.get("orderId") or ""),
            market_id=_i(raw.get("marketId")),
            side=str(raw.get("side") or raw.get("orderSide") or "").upper(),
            price=_d(raw.get("price")),
            size=_d(raw.get("size") if raw.get("size") is not None else raw.get("quantity")),
            fee=_d(raw.get("fee")),
            is_maker=is_maker,
            created_at=_i(raw.get("createdAt")),
            client_id=(str(raw["clientId"]) if raw.get("clientId") else None),
            liquidation_method=method,
            liquidated_user=(str(liquidated) if liquidated else None),
        )

    @property
    def forced(self) -> bool:
        """True when the engine closed (part of) this position for us."""
        return self.liquidation_method is not None

    @property
    def is_adl(self) -> bool:
        return self.liquidation_method == "ADL"


@dataclass
class Position:
    market_id: int
    size: Decimal
    entry_price: Decimal
    unrealized_pnl: Decimal = _D(0)
    liquidation_price: Decimal | None = None
    leverage: Decimal | None = None
    notional: Decimal | None = None
    margin_used: Decimal | None = None
    margin_mode: str | None = None

    @classmethod
    def from_api(cls, raw: Mapping[str, Any]) -> "Position":
        def opt_dec(key: str) -> Decimal | None:
            v = raw.get(key)
            return None if v in (None, "") else _D(v)

        size = _d(raw.get("size") if raw.get("size") is not None else raw.get("positionSize"))
        # Newer payloads sign the size themselves ("size": "-42.7", side SHORT);
        # older ones send it unsigned.  Only negate when it is not already signed,
        # otherwise a short position is read back as a long one - which makes
        # flatten() sell into a short instead of buying it back.
        if size > 0 and str(raw.get("side") or "").upper() == "SHORT":
            size = -size
        # The live payload calls the entry price ``averageEntryPrice``.
        entry = raw.get("averageEntryPrice")
        if entry in (None, ""):
            entry = raw.get("entryPrice")
        # ``positionValueNotional`` arrives signed (negative on shorts); keep
        # the magnitude so sizing math does not have to remember which way round
        # it is.
        notional = opt_dec("positionValueNotional")
        return cls(
            market_id=_i(raw.get("marketId")),
            size=size,
            entry_price=_d(entry),
            unrealized_pnl=_d(raw.get("unrealizedPnl") if raw.get("unrealizedPnl") is not None else raw.get("unrealizedPnlUsd")),
            liquidation_price=opt_dec("liquidationPrice"),
            leverage=opt_dec("leverage"),
            notional=(abs(notional) if notional is not None else None),
            margin_used=opt_dec("marginUsed"),
            margin_mode=(str(raw["marginMode"]).upper() if raw.get("marginMode") else None),
        )


@dataclass
class Account:
    equity: Decimal = _D(0)
    free_collateral: Decimal = _D(0)
    maintenance_margin: Decimal = _D(0)
    initial_margin: Decimal = _D(0)
    unrealized_pnl: Decimal = _D(0)
    net_deposits: Decimal = _D(0)
    positions: Sequence[Position] = field(default_factory=list)

    @property
    def margin_usage(self) -> Decimal:
        if self.equity <= 0:
            return _D(0)
        return (self.equity - self.free_collateral) / self.equity

    @classmethod
    def from_api(cls, raw: Mapping[str, Any]) -> "Account":
        # The REST read (/v1/account) reports ``equity``; the WebSocket
        # ``account`` channel reports the same number as ``accountEquity``.
        # Reading only one of them silently zeroes equity - and a zero equity
        # collapses the inventory cap to zero, which stops all opening.
        equity = raw.get("equity")
        if equity in (None, ""):
            equity = raw.get("accountEquity")

        raw_positions = raw.get("positions") or []
        if isinstance(raw_positions, Mapping):
            raw_positions = list(raw_positions.values())
        positions = [Position.from_api(p) for p in raw_positions if isinstance(p, Mapping)]

        unrealized = raw.get("unrealizedPnl")
        if unrealized in (None, ""):
            unrealized = sum((p.unrealized_pnl for p in positions), _D(0))

        return cls(
            equity=_d(equity),
            free_collateral=_d(raw.get("freeCollateral")),
            maintenance_margin=_d(raw.get("maintenanceMargin")),
            initial_margin=_d(raw.get("initialMargin")),
            unrealized_pnl=_d(unrealized),
            net_deposits=_d(raw.get("netDeposits")),
            positions=positions,
        )
