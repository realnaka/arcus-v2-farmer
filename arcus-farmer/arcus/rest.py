"""Async REST client for Arcus.

Design notes
------------
* Order writes cost **zero** IP weight but do draw on the per-subaccount
  order/cancel pools; reads are what burn the IP bucket, so every read goes
  through the token bucket first.
* Async retries reuse the **same** signed message (and the same ``clientId``),
  which keeps the order idempotent: the timestamp inside the payload is what
  makes a replay safe rather than a duplicate.
* ``scheduleCancel`` is not listed in the docs' two signing schemes.  We send
  it with scheme 2 and flag it, because the endpoint page contradicts the
  WebSocket overview about whether a dead man's switch exists at all.  Verify
  on testnet before relying on it.
"""

from __future__ import annotations

import asyncio
import logging
from decimal import Decimal
from typing import Any, Mapping, Sequence

import httpx

from . import signing
from .errors import ArcusError, AuthError, RateLimited
from .ratelimit import PoolMeter, TokenBucket, endpoint_weight

log = logging.getLogger(__name__)


class ArcusRest:
    def __init__(
        self,
        *,
        base_url: str,
        address: str,
        account_index: int,
        public_key: str,
        private_key: Any,
        bucket: TokenBucket | None = None,
        pools: PoolMeter | None = None,
        client: httpx.AsyncClient | None = None,
        max_attempts: int = 4,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.address = address
        self.account_index = int(account_index)
        self.public_key = public_key
        self.private_key = private_key
        self.bucket = bucket or TokenBucket()
        self.pools = pools or PoolMeter()
        if client is not None and not isinstance(client, httpx.AsyncClient):
            # A sync httpx.Client slips through silently until the first request
            # returns an un-awaited Response, so fail loudly and early instead.
            raise TypeError(
                f"ArcusRest needs an httpx.AsyncClient, got {type(client).__name__}"
            )
        self._client = client
        self._owns_client = client is None
        self.max_attempts = max_attempts
        self.calls: dict[str, int] = {}

    async def __aenter__(self) -> "ArcusRest":
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def start(self) -> None:
        if self._client is None:
            self._client = httpx.AsyncClient(base_url=self.base_url, timeout=httpx.Timeout(15.0))

    async def close(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    # ---------------------------------------------------------------- core

    async def _request(
        self,
        *,
        endpoint: str,
        method: str,
        path: str,
        params: Mapping[str, Any] | None = None,
        body: Mapping[str, Any] | None = None,
        signed_message: bytes | None = None,
        ts_ns: int | None = None,
        batch_len: int = 0,
        expect: Sequence[int] = (200, 201, 202),
    ) -> dict[str, Any]:
        assert self._client is not None, "call start() first"
        weight = endpoint_weight(endpoint, batch_len)
        self.calls[endpoint] = self.calls.get(endpoint, 0) + 1

        attempts = 0
        while True:
            attempts += 1
            await self.bucket.acquire(weight)

            headers: dict[str, str] = {}
            if signed_message is not None:
                headers = signing.auth_headers(
                    self.public_key, self.private_key, signed_message, ts_ns or signing.now_ns()
                )
            params_full: dict[str, Any] = dict(params or {})
            if signed_message is not None or endpoint in {
                "account", "positions", "openOrders", "fills", "orders", "order",
                "rateLimit", "accountStats", "leverages", "apiKeys", "accountTransferUpdates",
            }:
                params_full.setdefault("address", self.address)

            try:
                resp = await self._client.request(
                    method, path, params=params_full or None, json=dict(body) if body else None, headers=headers
                )
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                if attempts >= self.max_attempts:
                    raise ArcusError(f"{endpoint}: transport failure: {exc}") from exc
                await asyncio.sleep(min(2 ** attempts, 10))
                continue

            if resp.status_code in expect:
                if not resp.content:
                    return {}
                try:
                    payload = resp.json()
                except ValueError:
                    return {"raw": resp.text}
                self._absorb_rate_limit(payload)
                return payload

            payload: Any
            try:
                payload = resp.json()
            except ValueError:
                payload = {"raw": resp.text[:400]}

            if resp.status_code == 429:
                retry_after, bucket_name = self._parse_429(resp, payload)
                if bucket_name == "ip":
                    self.bucket.penalise(retry_after)
                if attempts >= self.max_attempts:
                    raise RateLimited(
                        f"{endpoint} rate limited ({bucket_name})", retry_after_s=retry_after,
                        bucket=bucket_name, status=429, body=payload,
                    )
                log.warning("429 on %s (%s), sleeping %.2fs", endpoint, bucket_name, retry_after)
                await asyncio.sleep(retry_after + 0.05)
                continue

            if resp.status_code in (401, 403):
                raise AuthError(f"{endpoint}: {payload}", status=resp.status_code, body=payload)

            raise ArcusError(f"{endpoint}: HTTP {resp.status_code}: {payload}", status=resp.status_code, body=payload)

    @staticmethod
    def _parse_429(resp: httpx.Response, payload: Any) -> tuple[float, str]:
        """Return ``(seconds_to_wait, bucket)``.

        Only order writes carry ``reason`` / ``retryAfterMs``; reads answer with
        a bare error body and a whole-second ``Retry-After`` header.
        """
        body = payload if isinstance(payload, Mapping) else {}
        reason = str(body.get("reason") or "")
        if body.get("retryAfterMs") is not None:
            return float(body["retryAfterMs"]) / 1000.0, reason or "account"
        header = resp.headers.get("Retry-After")
        if header:
            try:
                return max(1.0, float(header)), reason or "ip"
            except ValueError:
                pass
        return 2.0, reason or "ip"

    def _absorb_rate_limit(self, payload: Any) -> None:
        """Mirror the exchange's own pool accounting when it echoes it back.

        Order writes answer with ``{"rateLimit": {"pool": "cancel", "remaining": N}}``
        which is authoritative - better than decrementing a local guess, because
        the pool also drains from any other process using the same subaccount.
        """
        if not isinstance(payload, Mapping):
            return
        rl = payload.get("rateLimit")
        if not isinstance(rl, Mapping):
            return
        remaining = rl.get("remaining")
        if remaining is None:
            return
        pool = str(rl.get("pool") or "")
        try:
            value = int(remaining)
        except (TypeError, ValueError):
            return
        if pool == "cancel":
            self.pools.cancel_spent = max(self.pools.cancel_spent, self.pools.cancel_pool - value)
        elif pool == "order":
            self.pools.order_spent = max(self.pools.order_spent, self.pools.order_pool - value)

    # ---------------------------------------------------------------- reads

    async def markets(self) -> dict[str, Any]:
        return await self._request(endpoint="markets", method="GET", path="/v1/markets")

    async def account(self, account_index: int | None = None) -> dict[str, Any]:
        params = {}
        if account_index is not None:
            params["accountIndex"] = account_index
        return await self._request(endpoint="account", method="GET", path="/v1/account", params=params)

    async def positions(
        self,
        account_index: int | None = None,
        *,
        address: str | None = None,
    ) -> dict[str, Any]:
        """Open positions.  ``address`` reads someone else's book - the endpoint
        is public, which is what lets the ADL monitor rank us against peers."""
        params = {}
        if account_index is not None:
            params["accountIndex"] = account_index
        if address is not None:
            params["address"] = address
        return await self._request(endpoint="positions", method="GET", path="/v1/positions", params=params)

    async def open_orders(self, market_id: int | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if market_id is not None:
            params["market"] = market_id
        return await self._request(endpoint="openOrders", method="GET", path="/v1/openOrders", params=params)

    async def fills(
        self,
        *,
        from_us: int | None = None,
        to_us: int | None = None,
        market: str | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if from_us:
            params["from"] = from_us
        if to_us:
            params["to"] = to_us
        if market:
            params["market"] = market
        if limit is not None:
            params["limit"] = limit
        return await self._request(endpoint="fills", method="GET", path="/v1/fills", params=params)

    async def order_status(self, order_id: str) -> dict[str, Any]:
        return await self._request(
            endpoint="order", method="GET", path="/v1/order", params={"orderId": order_id}
        )

    async def rate_limit(self) -> dict[str, Any]:
        return await self._request(endpoint="rateLimit", method="GET", path="/v1/rateLimit")

    async def fee_tiers(self) -> dict[str, Any]:
        return await self._request(endpoint="feeTiers", method="GET", path="/v1/feeTiers")

    async def api_keys(self) -> dict[str, Any]:
        """Public listing of the keys registered to this address."""
        return await self._request(endpoint="apiKeys", method="GET", path="/v1/apiKeys")

    async def mids(self) -> dict[str, Any]:
        return await self._request(endpoint="mids", method="GET", path="/v1/mids")

    async def prices(self) -> dict[str, Any]:
        return await self._request(endpoint="prices", method="GET", path="/v1/prices")

    async def trades(self, market: str, *, limit: int | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {"market": market}
        if limit is not None:
            params["limit"] = limit
        return await self._request(endpoint="trades", method="GET", path="/v1/trades", params=params)

    async def leaderboard(
        self,
        *,
        sort_by: str = "volume",
        window: str = "24h",
        limit: int = 20,
    ) -> dict[str, Any]:
        """Top traders by volume/pnl/fees.  Public, used to sample ADL peers."""
        return await self._request(
            endpoint="leaderboard",
            method="GET",
            path="/v1/leaderboard",
            params={"sortBy": sort_by, "window": window, "limit": limit},
        )

    async def funding_rates(self, market: str) -> dict[str, Any]:
        return await self._request(
            endpoint="fundingRates", method="GET", path="/v1/fundingRates", params={"market": market}
        )

    async def leverages(self) -> dict[str, Any]:
        return await self._request(endpoint="leverages", method="GET", path="/v1/leverages")

    # ---------------------------------------------------------------- writes

    async def place_order(
        self,
        *,
        market_id: int,
        side: str,
        price: Decimal,
        quantity: Decimal,
        tif: str = "ALO",
        reduce_only: bool = False,
        client_id: str | None = None,
        good_til_us: int,
        market_tick: Decimal,
        market_step: Decimal,
    ) -> dict[str, Any]:
        ts = signing.now_ns()
        message = signing.place_order_payload(
            address=self.address,
            account_index=self.account_index,
            client_id=client_id,
            ts_ns=ts,
            good_til_ns=good_til_us * 1000,
            market_id=market_id,
            price_ticks=signing.to_units(price, market_tick),
            qty_quantums=signing.to_units(quantity, market_step),
            reduce_only=reduce_only,
            side=side,
            tif=tif,
        )
        body: dict[str, Any] = {
            "address": self.address,
            "accountIndex": self.account_index,
            "marketId": market_id,
            "orderSide": side.upper(),
            "orderType": "LIMIT",
            "quantity": str(quantity),
            "price": str(price),
            "timeInForce": tif.upper(),
            "goodTilTime": str(good_til_us),
            "timestamp": ts,
        }
        if reduce_only:
            body["reduceOnly"] = True
        if client_id:
            body["clientId"] = client_id
        self.pools.note_order_write()
        return await self._request(
            endpoint="placeOrder",
            method="POST",
            path="/v1/placeOrder",
            body=body,
            signed_message=message,
            ts_ns=ts,
        )

    async def cancel_order(
        self, *, market_id: int, order_id: str | None = None, client_id: str | None = None
    ) -> dict[str, Any]:
        ts = signing.now_ns()
        message = signing.cancel_order_payload(
            address=self.address,
            account_index=self.account_index,
            ts_ns=ts,
            market_id=market_id,
            order_id=order_id,
            client_id=client_id,
        )
        body: dict[str, Any] = {
            "address": self.address,
            "accountIndex": self.account_index,
            "marketId": market_id,
            "timestamp": ts,
        }
        # The gateway rejects a request carrying both identifiers:
        # "specify only one of order_id or client_id, not both".
        if order_id:
            body["kind"] = "orderId"
            body["orderId"] = order_id
        elif client_id:
            body["kind"] = "clientId"
            body["clientId"] = client_id
        self.pools.note_cancel()
        return await self._request(
            endpoint="cancelOrder", method="POST", path="/v1/cancelOrder", body=body, signed_message=message, ts_ns=ts
        )

    async def cancel_all_orders(self) -> dict[str, Any]:
        """Scheme 2 (legacy) message.  Charges the cancel pool a flat 1,000."""
        ts = signing.now_ns()
        body = {"address": self.address, "accountIndex": self.account_index, "timestamp": ts}
        message = signing.legacy_message(ts, "cancelAllOrders", body)
        self.pools.note_cancel_all()
        return await self._request(
            endpoint="cancelAllOrders",
            method="POST",
            path="/v1/cancelAllOrders",
            body=body,
            signed_message=message,
            ts_ns=ts,
        )

    async def set_leverage(self, *, market_id: int, leverage: int) -> dict[str, Any]:
        """Scheme 2 (legacy) message.  Integer in [1, market max]."""
        ts = signing.now_ns()
        body = {
            "address": self.address,
            "accountIndex": self.account_index,
            "marketId": market_id,
            "leverage": int(leverage),
            "timestamp": ts,
        }
        message = signing.legacy_message(ts, "setLeverage", body)
        return await self._request(
            endpoint="setLeverage",
            method="POST",
            path="/v1/setLeverage",
            body=body,
            signed_message=message,
            ts_ns=ts,
            expect=(200, 202, 422),
        )

    async def schedule_cancel(self, *, deadline_us: int | None) -> dict[str, Any]:
        """Arm / refresh (``deadline_us``) or disarm (``None``) the dead man's switch.

        Confirmed working on testnet (2026-10-04) - the WebSocket overview's
        claim that "Arcus has no dead-man's-switch" is stale.  Arming answers
        ``{"status": "scheduled", "time": <us>, "rateLimit": {"pool": "cancel"}}``.
        Each arm/disarm costs 1 from the cancel pool; an actual *fire* costs a
        flat 1,000 and counts against the 10-per-UTC-day cap.
        """
        ts = signing.now_ns()
        body: dict[str, Any] = {"address": self.address, "accountIndex": self.account_index, "timestamp": ts}
        if deadline_us is not None:
            body["time"] = int(deadline_us)
        message = signing.legacy_message(ts, "scheduleCancel", body)
        return await self._request(
            endpoint="scheduleCancel",
            method="POST",
            path="/v1/scheduleCancel",
            body=body,
            signed_message=message,
            ts_ns=ts,
            expect=(200, 202),
        )

    async def batch_place(self, orders: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        """Batch writes are free on IP for N <= 39; each element needs its own signature."""
        return await self._request(
            endpoint="batchPlaceOrders",
            method="POST",
            path="/v1/batchPlaceOrders",
            body={"orders": list(orders)},
            batch_len=len(orders),
        )

    async def batch_cancel(self, cancels: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        return await self._request(
            endpoint="batchCancelOrders",
            method="POST",
            path="/v1/batchCancelOrders",
            body={"cancels": list(cancels)},
            batch_len=len(cancels),
        )
