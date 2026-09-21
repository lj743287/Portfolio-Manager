"""
bars_cache.py — read side of the shared bar cache.

fetch_bars.py downloads 250 daily bars for the whole universe once and writes
cache/bars.json.gz. This module lets the scans read from it instead of calling
Twelve Data themselves.

The important behaviour is the fallback. If the cache is missing, unreadable,
or does not contain a symbol, `get()` returns None and the calling scan drops
back to its own API fetch. So a failed fetch step makes the night slow, not
broken -- the scans still run, they just pay for their own data as they did
before.

Typical use in a scan:

    from bars_cache import CACHE

    def fetch_bars(symbol):
        cached = CACHE.get(symbol)
        if cached is not None:
            return cached
        ... existing API call ...

Standard library only.
"""

import gzip
import json
import os

DEFAULT_PATH = os.environ.get("CACHE_FILE", "cache/bars.json.gz")


class BarCache:
    """Lazily-loaded view of the shared cache. Safe to use when absent."""

    def __init__(self, path=DEFAULT_PATH):
        self.path = path
        self._bars = None
        self._exchanges = {}
        self._meta = {}
        self._loaded = False
        self._hits = 0
        self._misses = 0

    # -- loading ---------------------------------------------------------

    def load(self):
        """Read the cache once. Never raises -- an unusable cache is simply
        an empty one, and every caller falls back to the API."""
        if self._loaded:
            return self._bars is not None
        self._loaded = True

        if not os.path.exists(self.path):
            print(f"[bars_cache] no cache at {self.path} — scans will fetch "
                  f"their own data")
            return False

        try:
            with gzip.open(self.path, "rt", encoding="utf-8") as fh:
                payload = json.load(fh)
        except Exception as exc:
            print(f"[bars_cache] could not read {self.path} ({exc}) — scans "
                  f"will fetch their own data")
            return False

        self._bars = payload.get("bars") or {}
        self._exchanges = payload.get("exchanges") or {}
        self._meta = {k: v for k, v in payload.items()
                      if k not in ("bars", "exchanges")}

        print(f"[bars_cache] loaded {len(self._bars)} symbols from "
              f"{self.path} (built {self._meta.get('generated_utc', '?')}, "
              f"{self._meta.get('bars_requested', '?')} bars)")
        if self._meta.get("dropped_date"):
            print(f"[bars_cache] in-progress {self._meta['dropped_date']} bar "
                  f"was already dropped at fetch time")
        return True

    @property
    def available(self):
        self.load()
        return bool(self._bars)

    @property
    def symbols(self):
        """Every symbol in the cache, sorted. Empty if there is no cache."""
        self.load()
        return sorted(self._bars) if self._bars else []

    def universe(self):
        """(symbol, exchange) pairs, matching what the scans built themselves."""
        self.load()
        if not self._bars:
            return []
        return [(s, self._exchanges.get(s, "")) for s in sorted(self._bars)]

    def exchange(self, symbol):
        self.load()
        return self._exchanges.get(symbol, "")

    # -- reading ---------------------------------------------------------

    def get(self, symbol, newest_first=True, limit=None):
        """Bars for one symbol, or None if not cached.

        newest_first=True  matches burst_scan.py / the raw API order
        newest_first=False matches anticipation_scan.py, which works oldest-first

        Returns a copy, so a scan mutating its rows cannot corrupt the cache
        for the scans that run after it.
        """
        self.load()
        if not self._bars:
            return None
        rows = self._bars.get(symbol)
        if not rows:
            self._misses += 1
            return None
        self._hits += 1
        out = [dict(r) for r in rows]
        if not newest_first:
            out.reverse()
        if limit:
            out = out[:limit] if newest_first else out[-limit:]
        return out

    def stats(self):
        return {"hits": self._hits, "misses": self._misses}

    def report(self):
        if not self._loaded or self._bars is None:
            return
        total = self._hits + self._misses
        if total:
            print(f"[bars_cache] served {self._hits}/{total} symbols from cache "
                  f"({self._misses} fell back to the API)")


# Shared instance. Importing modules use this rather than making their own,
# so the file is read from disk once per process however many scans use it.
CACHE = BarCache()

