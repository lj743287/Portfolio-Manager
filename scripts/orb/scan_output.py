"""
scan_output.py
Shared output writer for all screeners (ORB, Daily, Burst, Anticipation).

Place this file in the repo root. Each scanner imports it and calls
write_scan() at the very end, replacing whatever it currently does to
write its own HTML / CSV / text output.

The scanning logic itself does not change.

Typical usage at the bottom of a scanner:

    from scan_output import write_scan, write_failure

    if __name__ == "__main__":
        try:
            rows = run_scan()                 # your existing function
            write_scan("orb", rows, label="ORB Continuation")
        except Exception as exc:
            write_failure("orb", exc, label="ORB Continuation")
            raise

Standard library only - no dependencies to install.
"""

import json
import os
import csv
import datetime
import traceback

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

OUT_DIR = "data"

# Where the NASDAQ Trader universe file is cached in the repo. The exchange
# lookup is optional - if this file is missing, symbols simply get a blank
# exchange and the builder falls back to bare tickers.
UNIVERSE_FILE = os.path.join("data", "nasdaqtraded.txt")

# NASDAQ Trader "Listing Exchange" codes -> TradingView exchange prefixes.
# Q and N cover almost all ordinary equities. The others are mostly ETFs and
# are best-effort; verify on first TradingView import if you scan ETFs.
EXCHANGE_CODES = {
    "Q": "NASDAQ",
    "N": "NYSE",
    "A": "AMEX",
    "P": "AMEX",
    "Z": "AMEX",
    "V": "AMEX",
}

# Keys a scanner might use for the ticker, in order of preference.
SYMBOL_KEYS = ("symbol", "ticker", "Symbol", "Ticker", "SYMBOL")

# Keys a scanner might use for the last close, in order of preference.
CLOSE_KEYS = ("close", "Close", "last", "price", "Price", "c")

# Keys a scanner might use for the exchange, in order of preference.
EXCHANGE_KEYS = ("exchange", "Exchange", "mic", "listing_exchange")


# ---------------------------------------------------------------------------
# Symbol handling
# ---------------------------------------------------------------------------

def normalise_symbol(raw):
    """
    Clean a ticker and put it into TradingView's preferred form.

    TradingView uses a dot for share classes (BRK.B). Source files variously
    use dot, dash or slash, so all three are normalised to a dot.
    """
    if raw is None:
        return ""
    sym = str(raw).strip().upper()
    for ch in ("-", "/", " "):
        sym = sym.replace(ch, ".")
    while ".." in sym:
        sym = sym.replace("..", ".")
    return sym.strip(".")


def load_exchange_map(path=UNIVERSE_FILE):
    """
    Build {symbol: exchange} from the cached NASDAQ Trader file.

    The file is pipe-delimited with a header row and a trailing
    "File Creation Time" line, which is skipped. Returns an empty dict if the
    file is absent or unreadable - callers must cope with that.
    """
    mapping = {}
    if not os.path.exists(path):
        return mapping

    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            reader = csv.DictReader(fh, delimiter="|")
            for row in reader:
                sym_raw = row.get("Symbol") or row.get("ACT Symbol") or ""
                if not sym_raw or sym_raw.startswith("File Creation Time"):
                    continue
                code = (row.get("Listing Exchange") or "").strip()
                exch = EXCHANGE_CODES.get(code, "")
                if exch:
                    mapping[normalise_symbol(sym_raw)] = exch
    except Exception:
        # A malformed universe file must never take down a scan.
        return {}

    return mapping


# ---------------------------------------------------------------------------
# Row normalisation
# ---------------------------------------------------------------------------

def _first_present(row, keys):
    for key in keys:
        if key in row and row[key] not in (None, ""):
            return row[key]
    return None


def _to_float(value):
    if value is None:
        return None
    try:
        return round(float(value), 4)
    except (TypeError, ValueError):
        return None


def _normalise_row(row, exchange_map):
    """
    Turn one scanner result into the standard shape.

    Accepts a dict, or a bare string treated as a ticker. Any key that is not
    a recognised symbol / close / exchange alias is preserved verbatim
    inside "extra" and becomes a column on the dashboard.
    """
    if isinstance(row, str):
        row = {"symbol": row}
    if not isinstance(row, dict):
        raise TypeError("scan rows must be dicts or strings, got %r" % type(row))

    symbol = normalise_symbol(_first_present(row, SYMBOL_KEYS))
    if not symbol:
        return None

    exchange = _first_present(row, EXCHANGE_KEYS)
    exchange = str(exchange).strip().upper() if exchange else ""
    if not exchange:
        exchange = exchange_map.get(symbol, "")

    close = _to_float(_first_present(row, CLOSE_KEYS))

    consumed = set(SYMBOL_KEYS) | set(CLOSE_KEYS) | set(EXCHANGE_KEYS)
    extra = {}
    for key, value in row.items():
        if key in consumed:
            continue
        # Keep it JSON-safe. Anything exotic becomes its string form.
        if isinstance(value, (int, float, str, bool)) or value is None:
            extra[key] = value
        else:
            extra[key] = str(value)

    return {"symbol": symbol, "exchange": exchange, "close": close, "extra": extra}


# ---------------------------------------------------------------------------
# Writers
# ---------------------------------------------------------------------------

def _utc_now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _write_json(scan_id, payload, out_dir):
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, "%s.json" % scan_id)
    tmp = path + ".tmp"
    # Write to a temp file then rename, so a crash mid-write can never leave a
    # half-written JSON file for the builder to choke on.
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, sort_keys=False)
    os.replace(tmp, path)
    return path


def write_scan(scan_id, rows, label=None, out_dir=OUT_DIR, meta=None,
               exchange_map=None):
    """
    Write a successful scan result.

    scan_id  - short identifier, becomes the filename: "orb", "daily",
               "burst", "anticipation"
    rows     - list of dicts (or strings) from the scan, in the order you
               want them displayed
    label    - human-readable tab title. Defaults to scan_id.
    meta     - optional dict of extra info shown in the tab header, e.g.
               {"universe": 5581, "regime": "bull"}

    Returns the path written.
    """
    if exchange_map is None:
        exchange_map = load_exchange_map()

    clean = []
    for row in (rows or []):
        norm = _normalise_row(row, exchange_map)
        if norm is not None:
            clean.append(norm)

    # Column order follows the first row's extra keys, then any keys that
    # only appear in later rows, so the table stays stable run to run.
    columns = []
    for row in clean:
        for key in row["extra"]:
            if key not in columns:
                columns.append(key)

    payload = {
        "scan_id": scan_id,
        "label": label or scan_id,
        "generated_utc": _utc_now(),
        "status": "ok" if clean else "empty",
        "count": len(clean),
        "columns": columns,
        "meta": meta or {},
        "rows": clean,
    }

    path = _write_json(scan_id, payload, out_dir)
    print("[scan_output] %s: wrote %d rows to %s" % (scan_id, len(clean), path))
    return path


def write_failure(scan_id, error, label=None, out_dir=OUT_DIR, meta=None):
    """
    Record that a scan failed.

    The dashboard shows a red banner on that tab and keeps displaying the
    previous good result if one exists, so a single failure never leaves you
    with a blank page at 7am.
    """
    detail = "".join(
        traceback.format_exception_only(type(error), error)
    ).strip() if isinstance(error, BaseException) else str(error)

    payload = {
        "scan_id": scan_id,
        "label": label or scan_id,
        "generated_utc": _utc_now(),
        "status": "failed",
        "count": 0,
        "columns": [],
        "meta": meta or {},
        "error": detail,
        "rows": [],
    }

    path = _write_json(scan_id, payload, out_dir)
    print("[scan_output] %s: FAILED - %s" % (scan_id, detail))
    return path

