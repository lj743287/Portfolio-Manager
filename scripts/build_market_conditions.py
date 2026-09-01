#!/usr/bin/env python3
"""Build the data file used by market-conditions.html.

The calculation follows the published Stockbee Market Monitor concepts:
significant four-percent daily moves, 10-day cumulative breadth, 25-percent
quarterly breadth, 34/13 fast breadth, and monthly 25/50-percent extremes.

The script intentionally writes only aggregate counts. Individual price data
and the Twelve Data API key never reach the public dashboard.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


API_ROOT = "https://api.twelvedata.com"
ALLOWED_TYPES = {
    "Common Stock",
    "Depositary Receipt",
    "American Depositary Receipt",
    "REIT",
}
EXCHANGES = ("NASDAQ", "NYSE")
DEFAULT_OUTPUT = Path("data/market-conditions.json")
HISTORY_SESSIONS = 120
OUTPUTSIZE = 200
DISPLAY_SESSIONS = 90
MIN_PRICE = 3.0
MIN_AVG_DOLLAR_VOLUME = 250_000.0
MIN_DAILY_VOLUME = 100_000.0


@dataclass
class CreditLimiter:
    limit: int
    window_started: float = 0.0
    used: int = 0

    def __post_init__(self) -> None:
        self.window_started = time.monotonic()

    def acquire(self, credits: int) -> None:
        credits = max(1, credits)
        if credits > self.limit:
            raise ValueError(f"A request for {credits} credits exceeds the {self.limit}/minute limit")

        now = time.monotonic()
        elapsed = now - self.window_started
        if elapsed >= 60:
            self.window_started = now
            self.used = 0
            elapsed = 0

        if self.used + credits > self.limit:
            wait_for = max(0.0, 60.25 - elapsed)
            print(f"[pacing] waiting {wait_for:.1f}s for the Twelve Data credit window", flush=True)
            time.sleep(wait_for)
            self.window_started = time.monotonic()
            self.used = 0

        self.used += credits


def api_json(path: str, params: dict[str, Any], retries: int = 4) -> dict[str, Any]:
    query = urllib.parse.urlencode(params)
    url = f"{API_ROOT}{path}?{query}"
    last_error: Exception | None = None

    for attempt in range(1, retries + 1):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "portfolio-market-conditions/1.0"})
            with urllib.request.urlopen(request, timeout=60) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            last_error = exc
            if exc.code == 429 and attempt < retries:
                time.sleep(15 * attempt)
                continue
            if 500 <= exc.code < 600 and attempt < retries:
                time.sleep(5 * attempt)
                continue
            raise
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            last_error = exc
            if attempt < retries:
                time.sleep(5 * attempt)
                continue
            raise

    raise RuntimeError(f"Twelve Data request failed: {type(last_error).__name__}")


def build_universe(api_key: str, limiter: CreditLimiter) -> list[str]:
    symbols: set[str] = set()
    for exchange in EXCHANGES:
        limiter.acquire(1)
        payload = api_json("/stocks", {"exchange": exchange, "format": "JSON", "apikey": api_key})
        if payload.get("status") != "ok":
            raise RuntimeError(f"Could not retrieve the {exchange} stock catalogue")
        for record in payload.get("data", []):
            symbol = str(record.get("symbol") or "").strip().upper()
            security_type = str(record.get("type") or "").strip()
            if symbol and security_type in ALLOWED_TYPES:
                symbols.add(symbol)
        print(f"[universe] {exchange} catalogue loaded", flush=True)
    return sorted(symbols)


def chunks(values: list[str], size: int) -> Iterable[list[str]]:
    for start in range(0, len(values), size):
        yield values[start : start + size]


def to_number(value: Any) -> float | None:
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def normalise_bars(payload: dict[str, Any]) -> list[dict[str, Any]]:
    if not isinstance(payload, dict) or payload.get("status") == "error":
        return []

    bars: list[dict[str, Any]] = []
    for raw in payload.get("values", []):
        close = to_number(raw.get("close"))
        volume = to_number(raw.get("volume"))
        date = str(raw.get("datetime") or "")[:10]
        if close is None or volume is None or not date:
            continue
        bars.append({"date": date, "close": close, "volume": volume})

    bars.sort(key=lambda bar: bar["date"])
    return bars


def fetch_series(api_key: str, symbols: list[str], limiter: CreditLimiter) -> dict[str, Any]:
    limiter.acquire(len(symbols))
    return api_json(
        "/time_series",
        {
            "symbol": ",".join(symbols),
            "interval": "1day",
            "outputsize": OUTPUTSIZE,
            "order": "ASC",
            "format": "JSON",
            "apikey": api_key,
        },
    )


def payload_for_symbol(payload: dict[str, Any], symbol: str, batch_size: int) -> dict[str, Any]:
    if batch_size == 1 and "values" in payload:
        return payload
    value = payload.get(symbol, {}) if isinstance(payload, dict) else {}
    return value if isinstance(value, dict) else {}


def empty_count(date: str) -> dict[str, Any]:
    return {
        "date": date,
        "coverage": 0,
        "eligible": 0,
        "up_4": 0,
        "down_4": 0,
        "up_25_quarter": 0,
        "down_25_quarter": 0,
        "bull_34_13": 0,
        "bear_34_13": 0,
        "up_25_month": 0,
        "down_25_month": 0,
        "up_50_month": 0,
        "down_50_month": 0,
    }


def aggregate_symbol(bars: list[dict[str, Any]], counts: dict[str, dict[str, Any]]) -> bool:
    if len(bars) < 21:
        return False

    dates = [bar["date"] for bar in bars]
    closes = [bar["close"] for bar in bars]
    volumes = [bar["volume"] for bar in bars]
    date_to_index = {date: index for index, date in enumerate(dates)}
    contributed = False

    for date, current in counts.items():
        index = date_to_index.get(date)
        if index is None:
            continue

        current["coverage"] += 1
        close = closes[index]
        if index < 19 or close < MIN_PRICE:
            continue

        dollar_volume = [closes[i] * volumes[i] for i in range(index - 19, index + 1)]
        if sum(dollar_volume) / len(dollar_volume) < MIN_AVG_DOLLAR_VOLUME:
            continue

        current["eligible"] += 1
        contributed = True

        if index >= 1 and volumes[index] >= MIN_DAILY_VOLUME and volumes[index] > volumes[index - 1]:
            daily_change = close / closes[index - 1] - 1
            if daily_change >= 0.04:
                current["up_4"] += 1
            elif daily_change <= -0.04:
                current["down_4"] += 1

        if index >= 64:
            quarter = closes[index - 64 : index + 1]
            if close / min(quarter) - 1 >= 0.25:
                current["up_25_quarter"] += 1
            if close / max(quarter) - 1 <= -0.25:
                current["down_25_quarter"] += 1

        if index >= 33:
            fast_window = closes[index - 33 : index + 1]
            if close / min(fast_window) - 1 >= 0.13:
                current["bull_34_13"] += 1
            if close / max(fast_window) - 1 <= -0.13:
                current["bear_34_13"] += 1

        if index >= 20 and closes[index - 20] >= 5:
            month_change = close / closes[index - 20] - 1
            if month_change >= 0.25:
                current["up_25_month"] += 1
            if month_change <= -0.25:
                current["down_25_month"] += 1
            if month_change >= 0.50:
                current["up_50_month"] += 1
            if month_change <= -0.50:
                current["down_50_month"] += 1

    return contributed


def ratio_record(items: list[dict[str, Any]], index: int) -> dict[str, Any] | None:
    if index < 9:
        return None
    window = items[index - 9 : index + 1]
    up_total = sum(item["up_4"] for item in window)
    down_total = sum(item["down_4"] for item in window)
    infinite = down_total == 0 and up_total > 0
    ratio = None if down_total == 0 else round(up_total / down_total, 3)
    return {
        "date": items[index]["date"],
        "up_total": up_total,
        "down_total": down_total,
        "ratio": ratio,
        "infinite": infinite,
    }


def moving_average(values: list[float], period: int) -> float | None:
    if len(values) < period:
        return None
    return sum(values[-period:]) / period


def final_state(current: dict[str, Any], current_ratio: dict[str, Any], oneq: dict[str, Any]) -> dict[str, Any]:
    ratio_value = current_ratio.get("ratio")
    ratio_positive = bool(current_ratio.get("infinite")) or (ratio_value is not None and ratio_value >= 1)
    tests = {
        "primary_breadth": current["up_25_quarter"] > current["down_25_quarter"],
        "fast_breadth": current["bull_34_13"] > current["bear_34_13"],
        "ten_day_breadth": ratio_positive,
        "oneq_trend": bool(oneq.get("ma10") is not None and oneq.get("ma20") is not None and oneq["ma10"] > oneq["ma20"]),
    }
    positives = sum(tests.values())
    score = positives * 25

    if positives >= 3:
        label = "Favourable"
        colour = "green"
        action = "Normal long exposure is permitted, subject to setup quality and your usual risk limits."
    elif positives <= 1:
        label = "Defensive"
        colour = "red"
        action = "Protect capital. Avoid marginal breakouts and keep new long exposure very small."
    else:
        label = "Selective"
        colour = "amber"
        action = "Breadth is mixed. Take only the strongest setups and consider reduced total exposure."

    return {
        "label": label,
        "colour": colour,
        "score": score,
        "positive_signals": positives,
        "total_signals": 4,
        "action": action,
        "tests": tests,
    }


def build_output(api_key: str, credit_limit: int, batch_size: int, max_symbols: int = 0) -> dict[str, Any]:
    limiter = CreditLimiter(credit_limit)

    print("[oneq] retrieving index-proxy history", flush=True)
    oneq_payload = fetch_series(api_key, ["ONEQ"], limiter)
    oneq_bars = normalise_bars(payload_for_symbol(oneq_payload, "ONEQ", 1))
    if len(oneq_bars) < HISTORY_SESSIONS:
        raise RuntimeError("ONEQ did not return enough daily history")

    target_dates = [bar["date"] for bar in oneq_bars[-HISTORY_SESSIONS:]]
    counts = {date: empty_count(date) for date in target_dates}

    universe = build_universe(api_key, limiter)
    if max_symbols > 0:
        universe = universe[:max_symbols]
    print(f"[universe] {len(universe)} unique NASDAQ/NYSE securities selected", flush=True)

    valid_symbols = 0
    eligible_symbols: set[str] = set()
    failed_symbols = 0
    batches = list(chunks(universe, batch_size))

    for batch_number, batch in enumerate(batches, start=1):
        try:
            payload = fetch_series(api_key, batch, limiter)
            for symbol in batch:
                bars = normalise_bars(payload_for_symbol(payload, symbol, len(batch)))
                if not bars:
                    failed_symbols += 1
                    continue
                valid_symbols += 1
                if aggregate_symbol(bars, counts):
                    eligible_symbols.add(symbol)
        except Exception as exc:
            failed_symbols += len(batch)
            print(f"[warning] batch {batch_number} failed: {type(exc).__name__}", flush=True)

        if batch_number == 1 or batch_number % 10 == 0 or batch_number == len(batches):
            print(
                f"[progress] batch {batch_number}/{len(batches)} "
                f"valid={valid_symbols} failed={failed_symbols}",
                flush=True,
            )

    minimum_valid = max(500, math.ceil(len(universe) * 0.80))
    if valid_symbols < minimum_valid:
        raise RuntimeError(
            f"Coverage check failed: only {valid_symbols}/{len(universe)} symbols returned valid history"
        )

    history = [counts[date] for date in target_dates]
    ratio_history = [record for index in range(len(history)) if (record := ratio_record(history, index))]
    current = history[-1]
    current_ratio = ratio_history[-1]

    oneq_closes = [bar["close"] for bar in oneq_bars]
    ma10 = moving_average(oneq_closes, 10)
    ma20 = moving_average(oneq_closes, 20)
    oneq = {
        "date": oneq_bars[-1]["date"],
        "close": round(oneq_closes[-1], 4),
        "ma10": round(ma10, 4) if ma10 is not None else None,
        "ma20": round(ma20, 4) if ma20 is not None else None,
        "bullish": bool(ma10 is not None and ma20 is not None and ma10 > ma20),
    }

    state = final_state(current, current_ratio, oneq)
    buying_days = sum(1 for item in history[-10:] if item["up_4"] >= 300)
    selling_days = sum(1 for item in history[-10:] if item["down_4"] >= 300)
    current["strong_buying_days_10"] = buying_days
    current["strong_selling_days_10"] = selling_days

    return {
        "schema_version": 1,
        "status": "ok",
        "generated_at_utc": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "as_of_market_date": current["date"],
        "source": "Twelve Data",
        "universe": {
            "exchanges": list(EXCHANGES),
            "catalogue_symbols": len(universe),
            "valid_symbols": valid_symbols,
            "eligible_symbols": current["eligible"],
            "failed_symbols": failed_symbols,
            "minimum_price": MIN_PRICE,
            "minimum_average_dollar_volume": MIN_AVG_DOLLAR_VOLUME,
        },
        "condition": state,
        "current": current,
        "ten_day_ratio": current_ratio,
        "oneq": oneq,
        "history": {
            "daily_breadth": history[-DISPLAY_SESSIONS:],
            "ten_day_ratio": ratio_history[-DISPLAY_SESSIONS:],
            "primary_breadth": [
                {
                    "date": item["date"],
                    "up": item["up_25_quarter"],
                    "down": item["down_25_quarter"],
                }
                for item in history[-DISPLAY_SESSIONS:]
            ],
            "fast_breadth": [
                {
                    "date": item["date"],
                    "bull": item["bull_34_13"],
                    "bear": item["bear_34_13"],
                }
                for item in history[-DISPLAY_SESSIONS:]
            ],
        },
        "methodology": {
            "daily": "Stocks moving at least 4% on the day, with at least 100,000 shares and volume above the prior session.",
            "liquidity": "Latest price at least $3 and 20-session average dollar volume at least $250,000.",
            "primary": "Stocks 25% above their 65-session low versus stocks 25% below their 65-session high.",
            "fast": "Stocks 13% above their 34-session low versus stocks 13% below their 34-session high.",
            "ratio": "Ten-session total of 4% up moves divided by 4% down moves.",
            "condition": "Four equal signals: primary breadth, fast breadth, 10-day breadth dominance and ONEQ 10/20-day trend.",
        },
    }


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    temporary.replace(path)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--max-symbols", type=int, default=0, help="Test-only cap; zero scans the full universe")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    api_key = (os.getenv("TWELVEDATA_API_KEY") or os.getenv("TWELVE_DATA_API_KEY") or "").strip()
    if not api_key:
        print("Missing TWELVEDATA_API_KEY", file=sys.stderr)
        return 2

    credit_limit = int(os.getenv("TD_CREDITS_PER_MINUTE", "55"))
    batch_size = min(int(os.getenv("TD_BATCH_SIZE", "50")), credit_limit)
    if credit_limit < 1 or batch_size < 1:
        raise ValueError("Credit limit and batch size must be positive")

    result = build_output(api_key, credit_limit, batch_size, max_symbols=args.max_symbols)
    write_json(args.output, result)
    print(
        f"[done] {result['condition']['label']} score={result['condition']['score']} "
        f"as_of={result['as_of_market_date']} output={args.output}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
