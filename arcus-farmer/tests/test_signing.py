from __future__ import annotations

import json

from arcus import signing


def test_canonical_json_is_compact_and_sorted():
    assert signing.canonical_json({"b": 1, "a": 2}) == b'{"a":2,"b":1}'


def test_place_payload_matches_documented_key_order():
    priv, pub = signing.generate_keypair()
    raw = signing.place_order_payload(
        address="0xABCDEF0000000000000000000000000000000001",
        account_index=0,
        client_id="c1",
        ts_ns=1700000000000000000,
        good_til_ns=1700000000000000000 + 40 * 86400 * 10**9,
        market_id=24,
        price_ticks=75000,
        qty_quantums=1000,
        reduce_only=False,
        side="BUY",
        tif="ALO",
    )
    text = raw.decode()
    # keys must be alphabetical, no whitespace
    keys = list(json.loads(text).keys())
    assert keys == sorted(keys)
    assert ", " not in text and ": " not in text
    assert '"ad":"0xabcdef0000000000000000000000000000000001"' in text  # lowercased
    assert '"t":3' in text  # ALO
    assert '"s":0' in text  # BUY
    assert '"op":1' in text
    assert '"v":1' in text


def test_client_id_omitted_when_empty():
    raw = signing.place_order_payload(
        address="0xabc",
        account_index=0,
        client_id=None,
        ts_ns=1,
        good_til_ns=2,
        market_id=1,
        price_ticks=1,
        qty_quantums=1,
        reduce_only=True,
        side="SELL",
        tif="ALO",
    )
    assert '"c"' not in raw.decode()
    assert '"r":1' in raw.decode()
    assert '"s":1' in raw.decode()


def test_good_til_is_nanoseconds_of_microsecond_field():
    good_til_us = 1_700_000_000_000_000
    raw = signing.place_order_payload(
        address="0xabc", account_index=2, client_id="x", ts_ns=5,
        good_til_ns=good_til_us * 1000, market_id=1, price_ticks=1,
        qty_quantums=1, reduce_only=False, side="BUY", tif="GTT",
    )
    assert f'"g":{good_til_us * 1000}' in raw.decode()


def test_cancel_payload_requires_an_identifier():
    try:
        signing.cancel_order_payload(address="0xabc", account_index=0, ts_ns=1, market_id=1)
    except ValueError:
        pass
    else:  # pragma: no cover
        raise AssertionError("expected ValueError")
    by_id = signing.cancel_order_payload(
        address="0xabc", account_index=0, ts_ns=1, market_id=1, order_id="OID"
    ).decode()
    assert '"id":"OID"' in by_id and '"op":2' in by_id


def test_legacy_message_is_concatenated_without_delimiters():
    msg = signing.legacy_message(123, "cancelAllOrders", {"b": 2, "a": 1})
    assert msg == b'123cancelAllOrders{"a":1,"b":2}'


def test_auth_headers_are_128_hex_chars():
    priv, pub = signing.generate_keypair()
    headers = signing.auth_headers(pub, priv, b"payload", 42)
    assert headers["X-API-Key"] == pub
    assert headers["X-Timestamp"] == "42"
    assert len(headers["X-Signature"]) == 128
    int(headers["X-Signature"], 16)


def test_to_units_is_exact():
    assert signing.to_units("750.22", "0.01") == 75022
    assert signing.to_units("0.001", "0.0000001") == 10000
    try:
        signing.to_units("750.225", "0.01")
    except ValueError:
        pass
    else:  # pragma: no cover
        raise AssertionError("expected ValueError for non-multiple")


def test_private_key_roundtrip_and_permissions(tmp_path):
    priv, pub = signing.generate_keypair()
    path = signing.save_private_key(priv, tmp_path / "k.pem")
    assert (path.stat().st_mode & 0o777) == 0o600
    assert signing.public_key_hex(signing.load_private_key(path)) == pub

    path.chmod(0o644)
    try:
        signing.load_private_key(path)
    except PermissionError:
        pass
    else:  # pragma: no cover
        raise AssertionError("expected PermissionError for a world-readable key")
