#!/usr/bin/env python3
"""
Build the existing ORB bar cache from Alpaca's free historical market-data API.

This file deliberately does NOT contain any screening logic. It writes the
same cache structure as fetch_bars.py so the existing screener.py can run
unchanged. That makes the side-by-side test a provider comparison rather than
a strategy-code comparison.

Important compatibility choices:
- Uses the exact same NASDAQ Trader universe builder as fetch_bars.py.
- Requests 1Day SIP historical bars. Alpaca Basic restricts only the latest
  15 minutes of historical data, which is irrelevant for an overnight run.
- Uses split-adjusted bars to match Twelve Data time_series default behaviour.
- Stores newest-first rows in the same compact d/o/h/l/c/v schema.
- Keeps up to 250 completed daily bars per symbol.

Required env vars:
  APCA_API_KEY_ID
  APCA_API_SECRET_KEY

Optional env vars:
  CACHE_FILE          default cache/alpaca_bars.json.gz
  BARS                default 250
  BATCH_SIZE          default 50
  REQUESTS_PER_MIN    default 170 (below Alpaca Basic's 200/min limit)
  LOOKBACK_DAYS       default 450 calendar days
  MAX_SYMBOLS         optional cap for testing
"""

import gzip
import json
import os
import sys
import time
from datetime import datetime, timezone, timedelta

import requests

# Reuse the current production universe and incomplete-session logic verbatim.
from fetch_bars import load_universe, incomplete_today


BASE_URL = "https://data.alpaca.markets/v2/stocks/bars"
API_KEY = os.environ.get("APCA_API_KEY_ID", "")
API_SECRET = os.environ.get("APCA_API_SECRET_KEY", "")
CACHE_FILE = os.environ.get("CACHE_FILE", "cache/alpaca_bars.json.gz")
BARS = int(os.environ.get("BARS", "250"))
BATCH_SIZE = int(os.environ.get("BATCH_SIZE", "50"))
REQUESTS_PER_MIN = int(os.environ.get("REQUESTS_PER_MIN", "170"))
LOOKBACK_DAYS = int(os.environ.get("LOOKBACK_DAYS", "450"))
MAX_SYMBOLS = int(os.environ.get("MAX_SYMBOLS", "0") or "0")
ALPACA_FEED = os.environ.get("ALPACA_FEED", "sip").strip().lower() or "sip"

HEADERS = {
    "APCA-API-KEY-ID": API_KEY,
    "APCA-API-SECRET-KEY": API_SECRET,
}


def chunks(items, n):
    for i in range(0, len(items), n):
        yield items[i:i + n]


def request_page(params, tries=6):
    """GET one Alpaca page with conservative retry/backoff behaviour."""
    delay = 1.0
    for attempt in range(tries):
        try:
            r = requests.get(BASE_URL, headers=HEADERS, params=params, timeout=60)
            if r.status_code in (401, 403):
                raise PermissionError(
                    f"Alpaca denied {ALPACA_FEED} feed access ({r.status_code})"
                )
            if r.status_code == 429:
                retry_after = float(r.headers.get("Retry-After", "0") or "0")
                time.sleep(max(retry_after, 60.0 / max(1, REQUESTS_PER_MIN), delay))
                delay = min(delay * 2, 30)
                continue
            if r.status_code >= 500:
                time.sleep(delay)
                delay = min(delay * 2, 30)
                continue
            r.raise_for_status()
            return r.json()
        except requests.RequestException as exc:
            if attempt == tries - 1:
                raise
            print(f"Alpaca request error: {exc}; retrying", flush=True)
            time.sleep(delay)
            delay = min(delay * 2, 30)
    raise RuntimeError("Alpaca request failed after retries")


def fetch_batch(symbols, start_date, end_date, skip_date):
    """Fetch a batch of symbols, following Alpaca pagination."""
    collected = {s: [] for s in symbols}
    page_token = None
    calls = 0

    while True:
        params = {
            "symbols": ",".join(symbols),
            "timeframe": "1Day",
            "start": start_date,
            "end": end_date,
            "limit": 10000,
            "adjustment": "split",
            "feed": ALPACA_FEED,
            "sort": "asc",
        }
        if page_token:
            params["page_token"] = page_token

        payload = request_page(params)
        calls += 1

        bars = payload.get("bars") or {}
        for sym, rows in bars.items():
            if sym not in collected:
                continue
            for row in rows:
                ts = str(row.get("t", ""))
                day = ts[:10]
                if not day or (skip_date and day == skip_date):
                    continue
                try:
                    collected[sym].append({
                        "d": day,
                        "o": float(row["o"]),
                        "h": float(row["h"]),
                        "l": float(row["l"]),
                        "c": float(row["c"]),
                        "v": float(row["v"]),
                    })
                except (KeyError, TypeError, ValueError):
                    continue

        page_token = payload.get("next_page_token")
        if not page_token:
            break

        # Basic plan allows 200 historical API calls/min. Stay below it.
        if REQUESTS_PER_MIN:
            time.sleep(60.0 / REQUESTS_PER_MIN)

    # Existing cache is newest-first and capped to BARS.
    out = {}
    for sym, rows in collected.items():
        if not rows:
            continue
        rows.sort(key=lambda x: x["d"])
        out[sym] = list(reversed(rows[-BARS:]))

    return out, calls


def main():
    if not API_KEY or not API_SECRET:
        sys.exit("APCA_API_KEY_ID and APCA_API_SECRET_KEY must both be set")
    if ALPACA_FEED not in {"iex", "sip"}:
        sys.exit("ALPACA_FEED must be either iex or sip")

    universe = load_universe()
    if MAX_SYMBOLS:
        universe = universe[:MAX_SYMBOLS]
        print(f"TEST MODE: capped at {MAX_SYMBOLS} symbols", flush=True)

    exchanges = {sym: exch for sym, exch in universe}
    symbols = [sym for sym, _ in universe]

    skip = incomplete_today()
    if skip:
        print(f"US session in progress - dropping {skip} so the ORB screen sees "
              "the same last completed day as production", flush=True)

    now = datetime.now(timezone.utc)
    start_date = (now - timedelta(days=LOOKBACK_DAYS)).date().isoformat()
    end_date = now.date().isoformat()

    print(
        f"Alpaca free ORB cache: {len(symbols)} symbols, up to {BARS} daily bars, "
        f"{start_date} to {end_date}, batch size {BATCH_SIZE}, feed {ALPACA_FEED}",
        flush=True,
    )

    all_bars = {}
    failed_batches = []
    total_calls = 0
    started = time.time()
    groups = list(chunks(symbols, BATCH_SIZE))

    for idx, group in enumerate(groups, 1):
        try:
            got, calls = fetch_batch(group, start_date, end_date, skip)
            all_bars.update(got)
            total_calls += calls
        except PermissionError as exc:
            sys.exit(f"ERROR: {exc}")
        except Exception as exc:
            failed_batches.append({"symbols": group, "error": str(exc)})
            print(f"Batch {idx}/{len(groups)} failed: {exc}", flush=True)

        if idx % 10 == 0 or idx == len(groups):
            elapsed = (time.time() - started) / 60
            print(
                f"  {idx}/{len(groups)} batches - {len(all_bars)} symbols cached, "
                f"{total_calls} API calls, {elapsed:.1f} min",
                flush=True,
            )

        if REQUESTS_PER_MIN:
            time.sleep(60.0 / REQUESTS_PER_MIN)

    missing = sorted(set(symbols) - set(all_bars))

    os.makedirs(os.path.dirname(CACHE_FILE) or ".", exist_ok=True)
    payload = {
        "generated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "provider": "alpaca",
        "feed": ALPACA_FEED,
        "adjustment": "split",
        "bars_requested": BARS,
        "lookback_days": LOOKBACK_DAYS,
        "dropped_date": skip or "",
        "universe": len(universe),
        "count": len(all_bars),
        "failed": missing,
        "failed_batches": failed_batches,
        "api_calls": total_calls,
        "exchanges": exchanges,
        "bars": all_bars,
    }

    tmp = CACHE_FILE + ".tmp"
    with gzip.open(tmp, "wt", encoding="utf-8") as fh:
        json.dump(payload, fh, separators=(",", ":"))
    os.replace(tmp, CACHE_FILE)

    elapsed = (time.time() - started) / 60
    size_mb = os.path.getsize(CACHE_FILE) / 1e6
    print(
        f"Wrote {CACHE_FILE}: {len(all_bars)}/{len(symbols)} symbols, "
        f"{size_mb:.1f} MB, {total_calls} API calls, {elapsed:.1f} min",
        flush=True,
    )

    # Do not silently compare a badly incomplete provider run with production.
    if len(all_bars) < 0.90 * len(symbols):
        sys.exit(
            f"ERROR: Alpaca cache coverage only {len(all_bars)}/{len(symbols)} "
            "(<90%). Refusing to treat this as a valid comparison run."
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())

