"""Ed25519 request signing for the Arcus API.

Two schemes exist (see https://docs.arcus.xyz/api-reference/authentication):

* Scheme 1 - typed payload.  Used by ``placeOrder`` / ``cancelOrder`` /
  ``modifyOrder`` (and, per element, by the batch variants).  The signed bytes
  are the compact, key-sorted JSON object itself::

      signature = ed25519(canonical_payload)

* Scheme 2 - legacy message.  Used by ``cancelAllOrders``, ``setLeverage`` and
  the WebSocket ``authenticate`` call::

      signature = ed25519(timestamp + action + canonical_json(body))

Field notes that are easy to get wrong:

* ``ct`` equals the ``X-Timestamp`` you send, in **nanoseconds**.
* ``g`` is ``goodTilTime`` in **nanoseconds** (the request body carries the
  same instant in **microseconds**).  Required on every order, must be at
  least one month ahead.
* ``ad`` is the only case-folded field (lowercase).  Every other string -
  including ``c`` (clientId) and ``id`` (orderId) - is signed verbatim.
* ``p`` / ``q`` are integers derived from the decimal price/size by exact
  division with the market's ``tickSize`` / ``stepSize``.
"""

from __future__ import annotations

import json
import time
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

# ---------------------------------------------------------------- enums

OP_PLACE = 1
OP_CANCEL = 2
OP_MODIFY = 3

SIDE = {"BUY": 0, "SELL": 1}
TIF = {"GTT": 0, "FOK": 1, "IOC": 2, "ALO": 3}

PAYLOAD_VERSION = 1


# ---------------------------------------------------------------- json

def canonical_json(obj: Any) -> bytes:
    """Compact, key-sorted JSON - the exact bytes Arcus signs."""
    return json.dumps(obj, separators=(",", ":"), sort_keys=True).encode()


# ---------------------------------------------------------------- keys

def generate_keypair() -> tuple[ed25519.Ed25519PrivateKey, str]:
    """Return ``(private_key, public_key_hex)``.  The public half *is* the API key."""
    priv = ed25519.Ed25519PrivateKey.generate()
    return priv, public_key_hex(priv)


def public_key_hex(priv: ed25519.Ed25519PrivateKey) -> str:
    return priv.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    ).hex()


def save_private_key(priv: ed25519.Ed25519PrivateKey, path: str | Path) -> Path:
    """Persist a private key as PKCS#8 PEM with 0600 permissions."""
    p = Path(path).expanduser()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(
        priv.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    p.chmod(0o600)
    return p


def load_private_key(path: str | Path) -> ed25519.Ed25519PrivateKey:
    p = Path(path).expanduser()
    if not p.exists():
        raise FileNotFoundError(f"API private key not found: {p}")
    mode = p.stat().st_mode & 0o777
    if mode & 0o077:
        raise PermissionError(f"API key {p} is group/world readable (mode {oct(mode)}); run: chmod 600 {p}")
    return serialization.load_pem_private_key(p.read_bytes(), password=None)  # type: ignore[return-value]


def sign(priv: ed25519.Ed25519PrivateKey, message: bytes) -> str:
    return priv.sign(message).hex()


# ---------------------------------------------------------------- timestamps

def now_ns() -> int:
    return time.time_ns()


def now_us() -> int:
    return time.time_ns() // 1_000


# ---------------------------------------------------------------- scheme 1

def place_order_payload(
    *,
    address: str,
    account_index: int,
    client_id: str | None,
    ts_ns: int,
    good_til_ns: int,
    market_id: int,
    price_ticks: int,
    qty_quantums: int,
    reduce_only: bool,
    side: str,
    tif: str,
) -> bytes:
    payload: dict[str, Any] = {
        "ad": address.lower(),
        "ai": int(account_index),
        "ct": int(ts_ns),
        "g": int(good_til_ns),
        "m": int(market_id),
        "op": OP_PLACE,
        "p": int(price_ticks),
        "q": int(qty_quantums),
        "r": 1 if reduce_only else 0,
        "s": SIDE[side.upper()],
        "t": TIF[tif.upper()],
        "v": PAYLOAD_VERSION,
    }
    if client_id:
        payload["c"] = client_id
    return canonical_json(payload)


def cancel_order_payload(
    *,
    address: str,
    account_index: int,
    ts_ns: int,
    market_id: int,
    order_id: str | None = None,
    client_id: str | None = None,
) -> bytes:
    payload: dict[str, Any] = {
        "ad": address.lower(),
        "ai": int(account_index),
        "ct": int(ts_ns),
        "m": int(market_id),
        "op": OP_CANCEL,
        "v": PAYLOAD_VERSION,
    }
    if order_id:
        payload["id"] = order_id
    if client_id:
        payload["c"] = client_id
    if not order_id and not client_id:
        raise ValueError("cancelOrder needs exactly one of order_id / client_id")
    return canonical_json(payload)


def modify_order_payload(
    *,
    address: str,
    account_index: int,
    ts_ns: int,
    good_til_ns: int,
    market_id: int,
    order_id: str,
    price_ticks: int,
    qty_quantums: int,
    reduce_only: bool,
    side: str,
    tif: str,
    client_id: str | None = None,
) -> bytes:
    payload: dict[str, Any] = {
        "ad": address.lower(),
        "ai": int(account_index),
        "ct": int(ts_ns),
        "g": int(good_til_ns),
        "id": order_id,
        "m": int(market_id),
        "op": OP_MODIFY,
        "p": int(price_ticks),
        "q": int(qty_quantums),
        "r": 1 if reduce_only else 0,
        "s": SIDE[side.upper()],
        "t": TIF[tif.upper()],
        "v": PAYLOAD_VERSION,
    }
    if client_id:
        payload["c"] = client_id
    return canonical_json(payload)


# ---------------------------------------------------------------- scheme 2

def legacy_message(ts_ns: int | str, action: str, body: Mapping[str, Any]) -> bytes:
    """``timestamp + action + canonical_json(body)`` with no delimiters."""
    return f"{ts_ns}{action}".encode() + canonical_json(dict(body))


# ---------------------------------------------------------------- headers

def auth_headers(public_key: str, priv: ed25519.Ed25519PrivateKey, message: bytes, ts_ns: int) -> dict[str, str]:
    return {
        "X-API-Key": public_key,
        "X-Timestamp": str(ts_ns),
        "X-Signature": sign(priv, message),
    }


# ---------------------------------------------------------------- helpers

def to_units(value: str | float | Decimal, unit: str | float | Decimal) -> int:
    """Exact decimal -> integer ticks/quantums.  Raises when not a multiple."""
    n = Decimal(str(value)) / Decimal(str(unit))
    if n != n.to_integral_value():
        raise ValueError(f"{value} is not a multiple of {unit}")
    return int(n)
