#!/usr/bin/env python3
"""
fetch_bars.py — one pass over the universe, so the scans do not each repeat it.

Downloads 250 daily bars for every NASDAQ/NYSE/AMEX common stock and writes
them to a single gzipped file:

    cache/bars.json.gz

The ORB, burst and anticipation scans then read that file instead of calling
Twelve Data at all. That is the whole point of this script: those three scans
were each fetching daily history for the same ~5,760 symbols separately,
about 17,000 API calls a night, taking nearly nine hours in total. One shared
pass costs ~5,760 calls and takes around two and a half hours.

WHY 250 BARS: the ORB screener's 200-day moving average and the anticipation
scan's Double Trouble test (which looks back 252 bars) both need long
history. Fetching less would not save a single API call -- the cost is one
call per symbol regardless of size -- but it would quietly disable the
200-day trend filter and loosen Double Trouble. 250 keeps both working.

Requires env var TWELVE_DATA_API_KEY. Paced to ~50 requests/min (Grow 55).

Env:
  TWELVE_DATA_API_KEY  (required)
  CACHE_FILE           default cache/bars.json.gz
  BARS                 default 250
  REQUESTS_PER_MIN     default 50
  MAX_SYMBOLS          optional cap, for testing
"""

import gzip
import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timezone, timedelta, date

TD_URL = "https://api.twelvedata.com/time_series"
UNIVERSE_URLS = [
    "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt",
    "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt",
]

API_KEY = os.environ.get("TWELVE_DATA_API_KEY", "")
CACHE_FILE = os.environ.get("CACHE_FILE", "cache/bars.json.gz")
BARS = int(os.environ.get("BARS", "250"))
REQUESTS_PER_MIN = int(os.environ.get("REQUESTS_PER_MIN", "50"))
MAX_SYMBOLS = int(os.environ.get("MAX_SYMBOLS", "0") or "0")

# Same exclusions the scans already applied when building their own universes,
# kept identical so the cached universe matches what they expect to see.
BAD_NAME_WORDS = ("WARRANT", "RIGHT", "UNIT", "PREFERRED", "NOTES", "DEBENTURE")
BAD_SYMBOL_CHARS = set(".$^~=")

# A symbol that fails on the first pass gets another go at the end. Transient
# provider errors are common over a two-hour run and a retry is far cheaper
# than a scan missing names for the day.
RETRY_ROUNDS = 2


def http_get(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": "fetch-bars/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", errors="replace")


def load_universe():
    """Symbols with their TradingView exchange, matching burst_scan.py's rules."""
    symbols = []
    for url in UNIVERSE_URLS:
        is_nasdaq_file = "nasdaqlisted" in url
        text = http_get(url)
        lines = [l for l in text.splitlines() if "|" in l]
        header = lines[0].split("|")
        idx = {name: i for i, name in enumerate(header)}
        sym_col = "Symbol" if "Symbol" in idx else "ACT Symbol"
        for line in lines[1:]:
            f = line.split("|")
            if len(f) < len(header) or f[0].startswith("File Creation"):
                continue
            sym = f[idx[sym_col]].strip()
            name = f[idx["Security Name"]].upper() if "Security Name" in idx else ""
            etf = f[idx["ETF"]].strip() if "ETF" in idx else "N"
            test = f[idx["Test Issue"]].strip() if "Test Issue" in idx else "N"
            exch = f[idx["Exchange"]].strip() if "Exchange" in idx else "Q"
            if not sym or etf == "Y" or test == "Y":
                continue
            if exch not in ("Q", "N", "A", ""):
                continue
            if any(c in BAD_SYMBOL_CHARS for c in sym):
                continue
            if any(w in name for w in BAD_NAME_WORDS):
                continue
            tv_exch = "NASDAQ" if is_nasdaq_file else ("NYSE" if exch == "N" else "AMEX")
            symbols.append((sym, tv_exch))
    seen, out = set(), []
    for sym, ex in sorted(symbols):
        if sym not in seen:
            seen.add(sym)
            out.append((sym, ex))
    return out


def _nth_weekday(year, month, weekday, n):
    d = date(year, month, 1)
    shift = (weekday - d.weekday()) % 7
    return d + timedelta(days=shift + 7 * (n - 1))


def us_eastern_offset(utc_dt):
    """Correct US Eastern offset (second Sunday in March to first in November)."""
    year = utc_dt.year
    dst_start = datetime.combine(_nth_weekday(year, 3, 6, 2), datetime.min.time(),
                                 tzinfo=timezone.utc) + timedelta(hours=7)
    dst_end = datetime.combine(_nth_weekday(year, 11, 6, 1), datetime.min.time(),
                               tzinfo=timezone.utc) + timedelta(hours=6)
    return -4 if dst_start <= utc_dt < dst_end else -5


def incomplete_today():
    """Today's ET date if the session has not finished, else None.

    The scans all drop an in-progress bar so their tests apply to the last
    COMPLETED day. Doing it once here means every scan reading the cache gets
    the same treatment, and cannot disagree about what "yesterday" was.
    """
    utc = datetime.now(timezone.utc)
    et = utc + timedelta(hours=us_eastern_offset(utc))
    if et.weekday() >= 5:
        return None
    if et.hour < 16 or (et.hour == 16 and et.minute < 10):
        return et.strftime("%Y-%m-%d")
    return None


def fetch_one(symbol, skip_date, tries=3):
    """Fetch one symbol's bars, NEWEST FIRST, or None."""
    url = (f"{TD_URL}?symbol={symbol}&interval=1day&outputsize={BARS}"
           f"&order=DESC&timezone=America/New_York&apikey={API_KEY}")
    for attempt in range(tries):
        try:
            data = json.loads(http_get(url))
        except Exception:
            time.sleep(2 * (attempt + 1))
            continue

        vals = data.get("values")
        if vals:
            if skip_date and vals[0].get("datetime", "")[:10] == skip_date:
                vals = vals[1:]
            out = []
            for v in vals:
                try:
                    out.append({
                        "d": v.get("datetime", "")[:10],
                        "o": float(v["open"]), "h": float(v["high"]),
                        "l": float(v["low"]), "c": float(v["close"]),
                        "v": float(v["volume"]),
                    })
                except (KeyError, ValueError, TypeError):
                    return None
            return out or None

        msg = str(data.get("message", "")).lower()
        if any(k in msg for k in ("credit", "run out", "limit")):
            time.sleep(61)
            continue
        return None
    return None


def main():
    if not API_KEY:
        sys.exit("TWELVE_DATA_API_KEY not set")

    skip = incomplete_today()
    if skip:
        print(f"US session in progress — dropping the {skip} bar so every scan "
              f"sees the same last completed day", flush=True)

    universe = load_universe()
    if MAX_SYMBOLS:
        universe = universe[:MAX_SYMBOLS]
        print(f"TEST MODE: capped at {MAX_SYMBOLS} symbols", flush=True)

    exchanges = {sym: exch for sym, exch in universe}
    todo = [sym for sym, _ in universe]
    print(f"Universe: {len(todo)} symbols, {BARS} bars each", flush=True)

    delay = 60.0 / REQUESTS_PER_MIN
    bars = {}
    started = time.time()

    for rnd in range(RETRY_ROUNDS + 1):
        if not todo:
            break
        if rnd:
            print(f"\nRetry round {rnd}: {len(todo)} symbols that failed",
                  flush=True)
        failed = []
        for i, sym in enumerate(todo, 1):
            got = fetch_one(sym, skip)
            if got:
                bars[sym] = got
            else:
                failed.append(sym)
            if i % 500 == 0:
                mins = (time.time() - started) / 60
                print(f"  {i}/{len(todo)} — {len(bars)} cached, "
                      f"{len(failed)} failed, {mins:.0f} min elapsed", flush=True)
            time.sleep(delay)
        todo = failed

    os.makedirs(os.path.dirname(CACHE_FILE) or ".", exist_ok=True)
    payload = {
        "generated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "bars_requested": BARS,
        "dropped_date": skip or "",
        "universe": len(universe),
        "count": len(bars),
        "failed": sorted(todo),
        "exchanges": exchanges,
        "bars": bars,
    }
    tmp = CACHE_FILE + ".tmp"
    with gzip.open(tmp, "wt", encoding="utf-8") as fh:
        json.dump(payload, fh, separators=(",", ":"))
    os.replace(tmp, CACHE_FILE)

    size_mb = os.path.getsize(CACHE_FILE) / 1e6
    mins = (time.time() - started) / 60
    print(f"\nWrote {CACHE_FILE}: {len(bars)} symbols, {size_mb:.1f} MB, "
          f"{mins:.0f} min", flush=True)
    if todo:
        print(f"Could not fetch {len(todo)} symbols after {RETRY_ROUNDS} "
              f"retries: {', '.join(todo[:20])}"
              f"{' ...' if len(todo) > 20 else ''}", flush=True)

    # A cache this far short of the universe means something went wrong at the
    # provider, and every scan downstream would silently under-report. Fail
    # loudly instead; the scans fall back to their own API calls.
    if len(bars) < 0.5 * len(universe):
        sys.exit(f"ERROR: only cached {len(bars)} of {len(universe)} symbols")
    return 0


if __name__ == "__main__":
    sys.exit(main())

