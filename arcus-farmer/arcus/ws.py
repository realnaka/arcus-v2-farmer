"""WebSocket client: one connection multiplexing every channel and RPC.

The order lifecycle only exists on the socket - a placed order answers ``202``
and nothing more - so this client is a hard dependency of the strategy, not an
optimisation.  Subscriptions are never authenticated; account-scoped channels
take the address.

Subscription envelope (confirmed against testnet with ``tools/ws_probe.py``)::

    {"type": "subscribe", "channel": "l2Orderbook", "id": "QQQ-USD", "nLevels": 5}
    {"type": "subscribe", "channel": "orders", "id": "0x<address>"}

The ``id`` field *is* the market symbol for market channels and the account
address for account channels - there is no separate ``market`` / ``address``
key, and sending one makes the server read the ``id`` as a market name and
reject the frame.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import random
import time
from collections.abc import Awaitable, Callable, Iterable
from typing import Any

import websockets

log = logging.getLogger(__name__)

ChannelHandler = Callable[[str, str, dict[str, Any]], Awaitable[None] | None]

# Snapshot-only vs delta streams (docs/api-reference/websocket).
SNAPSHOT_ONLY = {"markets"}


class ArcusWS:
    def __init__(
        self,
        url: str,
        *,
        handler: ChannelHandler | None = None,
        on_connect: Callable[[], Awaitable[None] | None] | None = None,
        max_message_bytes: int | None = 16 * 1024 * 1024,
        proxy: str | None = None,
    ) -> None:
        self.url = url
        self.handler = handler
        self.on_connect = on_connect
        self.max_message_bytes = max_message_bytes
        # `proxy=None` explicitly disables proxy detection.  On macOS the
        # websockets library reads the *system* proxy settings, so a SOCKS entry
        # in Network preferences makes every connect fail with
        # "requires python-socks" even though a direct connection works fine.
        self.proxy = proxy
        self._ws: Any | None = None
        self._subscriptions: dict[str, dict[str, Any]] = {}
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._next_id = 1
        self._send_lock = asyncio.Lock()
        self._closing = False
        self.connected = False
        self.last_message_ts = 0.0
        self.reconnects = 0
        self.raw_log: list[dict[str, Any]] = []

    # ---------------------------------------------------------------- run

    async def run(self) -> None:
        """Connect, resubscribe and dispatch until :meth:`close` is called."""
        backoff = 1.0
        while not self._closing:
            try:
                async with websockets.connect(
                    self.url,
                    max_size=self.max_message_bytes,
                    ping_interval=20,
                    ping_timeout=20,
                    proxy=self.proxy,
                ) as ws:
                    self._ws = ws
                    self.connected = True
                    backoff = 1.0
                    log.info("WS connected: %s", self.url)
                    if self.on_connect:
                        result = self.on_connect()
                        if asyncio.iscoroutine(result):
                            await result
                    for sub in list(self._subscriptions.values()):
                        await self._send(sub)
                    await self._receive_loop(ws)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - reconnect on anything
                if self._closing:
                    break
                log.warning("WS dropped (%s); reconnecting in %.1fs", exc, backoff)
            finally:
                self.connected = False
                self._ws = None
                for fut in self._pending.values():
                    if not fut.done():
                        fut.set_exception(ConnectionError("websocket closed"))
                self._pending.clear()
            if self._closing:
                break
            self.reconnects += 1
            await asyncio.sleep(backoff + random.uniform(0, 0.5))
            backoff = min(backoff * 2, 30.0)

    async def close(self) -> None:
        self._closing = True
        if self._ws is not None:
            with contextlib.suppress(Exception):
                await self._ws.close()

    async def _receive_loop(self, ws: Any) -> None:
        async for raw in ws:
            self.last_message_ts = time.time()
            try:
                msg = json.loads(raw)
            except ValueError:
                log.warning("WS: non-JSON frame (%d bytes)", len(raw))
                continue
            if len(self.raw_log) < 200:
                self.raw_log.append(msg)
            await self._dispatch(msg)

    async def _dispatch(self, msg: dict[str, Any]) -> None:
        mtype = msg.get("type")
        if mtype in {"get_result", "post_result", "response", "result", "error"} or "result" in msg or "error" in msg:
            mid = msg.get("id")
            if isinstance(mid, int) and mid in self._pending:
                fut = self._pending.pop(mid)
                if not fut.done():
                    fut.set_result(msg)
                return
        if mtype in {"channel_data", "subscribed"}:
            channel = str(msg.get("channel") or "")
            sub_id = str(msg.get("id") or msg.get("subscriptionId") or "")
            contents = msg.get("contents")
            if isinstance(contents, list):
                for item in contents:
                    await self._emit(channel, sub_id, item if isinstance(item, dict) else {"value": item})
            elif isinstance(contents, dict):
                await self._emit(channel, sub_id, contents)
            return
        log.debug("WS unhandled frame: %s", str(msg)[:200])

    async def _emit(self, channel: str, sub_id: str, contents: dict[str, Any]) -> None:
        if self.handler is None:
            return
        try:
            result = self.handler(channel, sub_id, contents)
            if asyncio.iscoroutine(result):
                await result
        except Exception:  # noqa: BLE001 - a bad handler must not kill the socket
            log.exception("channel handler failed for %s", channel)

    # ---------------------------------------------------------------- send

    async def _send(self, payload: dict[str, Any]) -> None:
        if self._ws is None:
            raise ConnectionError("websocket is not connected")
        async with self._send_lock:
            await self._ws.send(json.dumps(payload))

    async def subscribe(
        self,
        channel: str,
        *,
        sub_id: str | None = None,
        address: str | None = None,
        market: str | None = None,
        snapshot: bool | None = None,
        n_levels: int | None = None,
        n_recent_closed: int | None = None,
        n_fills: int | None = None,
        **extra: Any,
    ) -> str:
        # `id` doubles as the market symbol / account address on the wire.
        sid = sub_id or market or address or channel
        frame: dict[str, Any] = {"type": "subscribe", "channel": channel, "id": sid}
        if snapshot is not None and channel not in SNAPSHOT_ONLY:
            frame["snapshot"] = snapshot
        if n_levels is not None:
            frame["nLevels"] = n_levels
        if n_recent_closed is not None:
            frame["nRecentClosed"] = n_recent_closed
        if n_fills is not None:
            frame["nFills"] = n_fills
        frame.update(extra)
        self._subscriptions[sid] = frame
        if self._ws is not None:
            await self._send(frame)
        return sid

    async def unsubscribe(self, sub_id: str) -> None:
        frame = self._subscriptions.pop(sub_id, None)
        if frame and self._ws is not None:
            await self._send({"type": "unsubscribe", "channel": frame["channel"], "id": sub_id})

    async def request(
        self, request_type: str, payload: dict[str, Any] | None = None, *, timeout: float = 10.0
    ) -> dict[str, Any]:
        """One-shot RPC (``get``) - e.g. a fresh ``l2orderbook`` snapshot."""
        rid = self._next_id
        self._next_id += 1
        fut: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        await self._send({"type": "get", "id": rid, "request": {"type": request_type, "payload": payload or {}}})
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        finally:
            self._pending.pop(rid, None)

    async def orderbook_snapshot(self, market: str, n_levels: int = 20) -> dict[str, Any]:
        return await self.request("l2orderbook", {"market": market, "nLevels": n_levels})

    def subscription_ids(self) -> Iterable[str]:
        return tuple(self._subscriptions)
