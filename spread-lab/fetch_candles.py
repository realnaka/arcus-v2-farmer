#!/usr/bin/env python3
"""Fetch Arcus public candles for the memory-complex spread study.

Usage:  python3 fetch_candles.py [--symbols A,B] [--timeframe 1m] [--days 60]

Pages /v1/candles backwards with the `to` cursor until `days` of history is
covered or the venue stops serving older bars.  Output: data/candles_<SYM>_<tf>.json
"""

from __future__ import annotations

import argparse
import json
import time
import urllib.parse
import urllib.request
from pathlib import Path

BASE = "https://api.arcus.xyz"
HERE = Path(__file__).resolve().parent
DATA = HERE / "data"


def get(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "spread-lab/1.0"})
    for attempt in range(6):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode())
        except Exception as exc:  # noqa: BLE001
            wait = 1.5 * (attempt + 1)
            print(f"  retry {attempt + 1} after {exc} ({wait:.1f}s)")
            time.sleep(wait)
    raise RuntimeError(f"giving up on {url}")


def fetch(symbol: str, timeframe: str, days: float, end_us: int) -> list[dict]:
    per_page = 1500
    minutes = {"1m": 1, "5m": 5, "15m": 15, "1h": 60, "4h": 240, "1d": 1440}[timeframe]
    want = int(days * 1440 / minutes) + 20
    seen: dict[int, dict] = {}
    to = end_us
    while len(seen) < want:
        qs = urllib.parse.urlencode({"market": symbol, "timeframe": timeframe, "to": to})
        payload = get(f"{BASE}/v1/candles?{qs}")
        rows = payload.get("candles") or []
        if not rows:
            break
        new = 0
        for row in rows:
            ts = int(row["openTime"])
            if ts not in seen:
                seen[ts] = row
                new += 1
        oldest = min(int(r["openTime"]) for r in rows)
        print(f"  {symbol} {timeframe}: +{new} bars, cursor -> "
              f"{time.strftime('%Y-%m-%d %H:%M', time.gmtime(oldest / 1e6))} UTC, total {len(seen)}")
        if new == 0:
            break
        to = oldest - 1
        if len(rows) < per_page and len(seen) < want:
            # venue returned a short page: it is the end of available history
            break
        time.sleep(0.12)
    return [seen[k] for k in sorted(seen)]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default="DRAM-USD,MU-USD,SKHY-USD,SNDK-USD")
    ap.add_argument("--timeframe", default="1m")
    ap.add_argument("--days", type=float, default=60.0)
    args = ap.parse_args()
    DATA.mkdir(parents=True, exist_ok=True)
    end_us = int(time.time() * 1e6)
    for symbol in args.symbols.split(","):
        symbol = symbol.strip()
        if not symbol:
            continue
        rows = fetch(symbol, args.timeframe, args.days, end_us)
        out = DATA / f"candles_{symbol}_{args.timeframe}.json"
        out.write_text(json.dumps(rows))
        if rows:
            first = time.strftime("%Y-%m-%d %H:%M", time.gmtime(int(rows[0]["openTime"]) / 1e6))
            last = time.strftime("%Y-%m-%d %H:%M", time.gmtime(int(rows[-1]["openTime"]) / 1e6))
        else:
            first = last = "-"
        print(f"{symbol}: {len(rows)} bars {first} -> {last} UTC -> {out.name}")


if __name__ == "__main__":
    main()
