#!/usr/bin/env python3
"""Build the data file used by market-conditions.html using Alpaca daily bars.

The calculation follows the published Stockbee Market Monitor concepts:
significant four-percent daily moves, 10-day cumulative breadth, 25-percent
quarterly breadth, 34/13 fast breadth, and monthly 25/50-percent extremes.

Only aggregate counts are written to the public dashboard. Alpaca credentials
and individual price histories remain inside the GitHub Actions run.
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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

ALPACA_BARS_URL = "https://data.alpaca.markets/v2/stocks/bars"
UNIVERSE_URLS = (
    "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt",
    "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt",
)
DEFAULT_OUTPUT = Path("data/market-conditions.json")
EXCHANGES = ("NASDAQ", "NYSE")
HISTORY_SESSIONS = 120
OUTPUTSIZE = 200
DISPLAY_SESSIONS = 90
MIN_PRICE = 3.0
MIN_AVG_DOLLAR_VOLUME = 250_000.0
MIN_DAILY_VOLUME = 100_000.0
BAD_NAME_WORDS = ("WARRANT", "RIGHT", "UNIT", "PREFERRED", "NOTES", "DEBENTURE")
BAD_SYMBOL_CHARS = set(".$^~=")


def chunks(values: list[str], size: int) -> Iterable[list[str]]:
    for start in range(0, len(values), size):
        yield values[start : start + size]


def to_number(value: Any) -> float | None:
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def http_text(url: str, timeout: int = 60) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": "portfolio-market-conditions/2.0"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8", errors="replace")


def build_universe() -> list[str]:
    """NASDAQ and NYSE common-stock style universe from Nasdaq Trader files."""
    symbols: set[str] = set()
    for url in UNIVERSE_URLS:
        is_nasdaq_file = "nasdaqlisted" in url
        text = http_text(url)
        lines = [line for line in text.splitlines() if "|" in line]
        if not lines:
            continue
        header = lines[0].split("|")
        index = {name: i for i, name in enumerate(header)}
        symbol_col = "Symbol" if "Symbol" in index else "ACT Symbol"
        for line in lines[1:]:
            fields = line.split("|")
            if len(fields) < len(header) or fields[0].startswith("File Creation"):
                continue
            symbol = fields[index[symbol_col]].strip().upper()
            name = fields[index["Security Name"]].upper() if "Security Name" in index else ""
            etf = fields[index["ETF"]].strip() if "ETF" in index else "N"
            test = fields[index["Test Issue"]].strip() if "Test Issue" in index else "N"
            exchange_code = fields[index["Exchange"]].strip() if "Exchange" in index else "Q"
            if not symbol or etf == "Y" or test == "Y":
                continue
            if any(char in BAD_SYMBOL_CHARS for char in symbol):
                continue
            if any(word in name for word in BAD_NAME_WORDS):
                continue
            if is_nasdaq_file:
                symbols.add(symbol)
            elif exchange_code == "N":
                symbols.add(symbol)
        print(f"[universe] loaded {url.rsplit('/', 1)[-1]}", flush=True)
    return sorted(symbols)


def alpaca_json(
    params: dict[str, Any], api_key: str, api_secret: str, requests_per_minute: int, retries: int = 6
) -> dict[str, Any]:
    query = urllib.parse.urlencode(params)
    url = f"{ALPACA_BARS_URL}?{query}"
    headers = {
        "User-Agent": "portfolio-market-conditions/2.0",
        "APCA-API-KEY-ID": api_key,
        "APCA-API-SECRET-KEY": api_secret,
    }
    delay = 1.0
    for attempt in range(retries):
        try:
            request = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(request, timeout=90) as response:
                payload = json.loads(response.read().decode("utf-8"))
            if requests_per_minute > 0:
                time.sleep(60.0 / requests_per_minute)
            return payload
        except urllib.error.HTTPError as exc:
            if exc.code == 429 and attempt < retries - 1:
                retry_after = float(exc.headers.get("Retry-After", "0") or "0")
                time.sleep(max(retry_after, delay, 60.0 / max(1, requests_per_minute)))
                delay = min(delay * 2, 30)
                continue
            if 500 <= exc.code < 600 and attempt < retries - 1:
                time.sleep(delay)
                delay = min(delay * 2, 30)
                continue
            raise
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
            if attempt == retries - 1:
                raise
            time.sleep(delay)
            delay = min(delay * 2, 30)
    raise RuntimeError("Alpaca request failed after retries")


def fetch_series_batch(
    symbols: list[str], api_key: str, api_secret: str, requests_per_minute: int
) -> dict[str, list[dict[str, Any]]]:
    """Fetch up to OUTPUTSIZE completed daily bars per symbol from Alpaca."""
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=420)
    collected: dict[str, list[dict[str, Any]]] = {symbol: [] for symbol in symbols}
    page_token: str | None = None

    while True:
        params: dict[str, Any] = {
            "symbols": ",".join(symbols),
            "timeframe": "1Day",
            "start": start.strftime("%Y-%m-%dT00:00:00Z"),
            "end": end.strftime("%Y-%m-%dT23:59:59Z"),
            "limit": 10000,
            "adjustment": "split",
            "feed": "sip",
            "sort": "asc",
        }
        if page_token:
            params["page_token"] = page_token
        payload = alpaca_json(params, api_key, api_secret, requests_per_minute)
        raw_bars = payload.get("bars") or {}
        for symbol, rows in raw_bars.items():
            if symbol not in collected or not isinstance(rows, list):
                continue
            for raw in rows:
                close = to_number(raw.get("c"))
                volume = to_number(raw.get("v"))
                date = str(raw.get("t") or "")[:10]
                if close is None or volume is None or not date:
                    continue
                collected[symbol].append({"date": date, "close": close, "volume": volume})
        page_token = payload.get("next_page_token")
        if not page_token:
            break

    for symbol in list(collected):
        rows = collected[symbol]
        rows.sort(key=lambda bar: bar["date"])
        collected[symbol] = rows[-OUTPUTSIZE:]
    return collected


def empty_count(date: str) -> dict[str, Any]:
    return {
        "date": date,
        "coverage": 0,
        "eligible": 0,
        "up_4": 0,
        "down_4": 0,
        "up_45_10": 0,
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

        if index >= 10 and closes[index - 10] > 0:
            if close / closes[index - 10] - 1 > 0.45:
                current["up_45_10"] += 1

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
    return {"date": items[index]["date"], "up_total": up_total, "down_total": down_total, "ratio": ratio, "infinite": infinite}


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
        label, colour = "Favourable", "green"
        action = "Normal long exposure is permitted, subject to setup quality and your usual risk limits."
    elif positives <= 1:
        label, colour = "Defensive", "red"
        action = "Protect capital. Avoid marginal breakouts and keep new long exposure very small."
    else:
        label, colour = "Selective", "amber"
        action = "Breadth is mixed. Take only the strongest setups and consider reduced total exposure."
    return {"label": label, "colour": colour, "score": score, "positive_signals": positives, "total_signals": 4, "action": action, "tests": tests}


def build_output(api_key: str, api_secret: str, batch_size: int, requests_per_minute: int, max_symbols: int = 0) -> dict[str, Any]:
    print("[oneq] retrieving index-proxy history from Alpaca", flush=True)
    oneq_map = fetch_series_batch(["ONEQ"], api_key, api_secret, requests_per_minute)
    oneq_bars = oneq_map.get("ONEQ", [])
    if len(oneq_bars) < HISTORY_SESSIONS:
        raise RuntimeError("ONEQ did not return enough daily history")

    target_dates = [bar["date"] for bar in oneq_bars[-HISTORY_SESSIONS:]]
    counts = {date: empty_count(date) for date in target_dates}

    universe = build_universe()
    if max_symbols > 0:
        universe = universe[:max_symbols]
    print(f"[universe] {len(universe)} unique NASDAQ/NYSE securities selected", flush=True)

    valid_symbols = 0
    eligible_symbols: set[str] = set()
    failed_symbols = 0
    batches = list(chunks(universe, batch_size))

    for batch_number, batch in enumerate(batches, start=1):
        try:
            series = fetch_series_batch(batch, api_key, api_secret, requests_per_minute)
            for symbol in batch:
                bars = series.get(symbol, [])
                if not bars:
                    failed_symbols += 1
                    continue
                valid_symbols += 1
                if aggregate_symbol(bars, counts):
                    eligible_symbols.add(symbol)
        except Exception as exc:
            failed_symbols += len(batch)
            print(f"[warning] batch {batch_number} failed: {type(exc).__name__}: {exc}", flush=True)

        if batch_number == 1 or batch_number % 10 == 0 or batch_number == len(batches):
            print(f"[progress] batch {batch_number}/{len(batches)} valid={valid_symbols} failed={failed_symbols}", flush=True)

    minimum_valid = max(500, math.ceil(len(universe) * 0.80))
    if valid_symbols < minimum_valid:
        raise RuntimeError(f"Coverage check failed: only {valid_symbols}/{len(universe)} symbols returned valid history")

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

    up45_ma_history = []
    for index, item in enumerate(history):
        if index < 9:
            continue
        window = history[index - 9 : index + 1]
        ma = sum(day["up_45_10"] for day in window) / 10
        up45_ma_history.append({"date": item["date"], "count": item["up_45_10"], "ma10": round(ma, 2)})

    current["up_45_10_ma"] = up45_ma_history[-1]["ma10"] if up45_ma_history else None
    state = final_state(current, current_ratio, oneq)
    current["strong_buying_days_10"] = sum(1 for item in history[-10:] if item["up_4"] >= 300)
    current["strong_selling_days_10"] = sum(1 for item in history[-10:] if item["down_4"] >= 300)

    return {
        "schema_version": 1,
        "status": "ok",
        "generated_at_utc": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "as_of_market_date": current["date"],
        "source": "Alpaca",
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
            "up_45_10_ma": up45_ma_history[-DISPLAY_SESSIONS:],
            "primary_breadth": [{"date": item["date"], "up": item["up_25_quarter"], "down": item["down_25_quarter"]} for item in history[-DISPLAY_SESSIONS:]],
            "fast_breadth": [{"date": item["date"], "bull": item["bull_34_13"], "bear": item["bear_34_13"]} for item in history[-DISPLAY_SESSIONS:]],
        },
        "methodology": {
            "daily": "Stocks moving at least 4% on the day, with at least 100,000 shares and volume above the prior session.",
            "liquidity": "Latest price at least $3 and 20-session average dollar volume at least $250,000.",
            "primary": "Stocks 25% above their 65-session low versus stocks 25% below their 65-session high.",
            "fast": "Stocks 13% above their 34-session low versus stocks 13% below their 34-session high.",
            "ratio": "Ten-session total of 4% up moves divided by 4% down moves.",
            "high_momentum": "Stocks up more than 45% over the previous 10 sessions; chart shows the 10-session moving average of the daily count.",
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
    api_key = (os.getenv("APCA_API_KEY_ID") or "").strip()
    api_secret = (os.getenv("APCA_API_SECRET_KEY") or "").strip()
    if not api_key or not api_secret:
        print("Missing APCA_API_KEY_ID or APCA_API_SECRET_KEY", file=sys.stderr)
        return 2

    batch_size = int(os.getenv("ALPACA_BATCH_SIZE", "50"))
    requests_per_minute = int(os.getenv("ALPACA_REQUESTS_PER_MIN", "170"))
    if batch_size < 1 or requests_per_minute < 1:
        raise ValueError("Alpaca batch size and request rate must be positive")

    result = build_output(api_key, api_secret, batch_size, requests_per_minute, max_symbols=args.max_symbols)
    write_json(args.output, result)
    print(f"[done] {result['condition']['label']} score={result['condition']['score']} as_of={result['as_of_market_date']} source={result['source']} output={args.output}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
