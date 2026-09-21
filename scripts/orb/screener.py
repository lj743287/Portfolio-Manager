#!/usr/bin/env python3
"""
Overnight ORB-continuation screener (NYSE/NASDAQ).

Surfaces stocks in an intact uptrend that are COILING below a recent high
(set up for a breakout next session).

Prints a filter funnel and sorts candidates calmest-first by ADR.

No liquidity filter.
No market-regime gate (assess the market yourself).

Current core screen:
- Price >= $3
- ADR >= 2% and < 10%
- Run-up between 45% and 200%
- Contracting base
- Price pulled back at least 0.5% from recent high
- Recent high at least 2 bars ago
- Higher lows
- Near at least one rising 10/20/50-day moving average
- Above 50-day moving average
- Above 200-day moving average where sufficient history exists

Env:
    ORB_DATA_PROVIDER (alpaca or twelve_data; default alpaca)
    TWELVE_DATA_KEY (required only for twelve_data fallback mode)
    MAX_SYMBOLS (optional cap)
    THROTTLE_SEC (default 1.2)

Outputs:
    output/candidates.csv
    docs/watchlist.txt
    docs/index.html
    data/orb.json
"""

import os
import io
import time
import datetime as dt

import requests
import numpy as np
import pandas as pd


# Shared output writer used by all four scans.
from scan_output import write_scan, write_failure

# Shared bar cache, filled once per night by fetch_bars.py.
# If absent, this screener falls back to fetching its own data.
from bars_cache import CACHE


API_KEY = os.environ.get("TWELVE_DATA_KEY", "")
DATA_PROVIDER = os.environ.get("ORB_DATA_PROVIDER", "alpaca").strip().lower()
MAX_SYMBOLS = int(os.environ.get("MAX_SYMBOLS", "0") or "0")
THROTTLE = float(os.environ.get("THROTTLE_SEC", "1.2"))

OUT_DIR = "output"
DOCS_DIR = "docs"


# ---------------------------------------------------------------------------
# Scan identity
# ---------------------------------------------------------------------------

SCAN_ID = "orb"
SCAN_LABEL = "ORB Continuation"


# ---------------------------------------------------------------------------
# Screen parameters
# ---------------------------------------------------------------------------

P = dict(
    ADR_MIN=3.5,
    ADR_MAX=7.5,
    RUNUP_MIN=40.0,
    RUNUP_MAX=1000.0,
    PRICE_MIN=3.0,
    BASE_MIN=6,
    RUNUP_LB=60,
    MA_TOL=7.0,
    PEAK_MIN_BACK=2,
    PULLBACK_MIN=0.5,
)


# ---------------------------------------------------------------------------
# Universe
# ---------------------------------------------------------------------------

def load_universe():
    """
    Load NASDAQ/NYSE-listed stocks from NASDAQ Trader.

    Removes:
    - ETFs
    - Test issues
    - Units
    - Warrants
    - Rights
    - Preferred shares
    - Depositary securities
    - Symbols containing ., $ or ^
    - Common five-character unit/warrant/right suffixes
    """

    urls = [
        "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt",
        "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt",
    ]

    syms = []

    for u in urls:
        txt = requests.get(u, timeout=30).text

        df = pd.read_csv(
            io.StringIO(txt),
            sep="|",
            dtype=str,
        )

        first = df.columns[0]

        df = df[
            ~df[first]
            .astype(str)
            .str.contains("File Creation Time", na=False)
        ]

        etf_col = "ETF" if "ETF" in df.columns else None
        test_col = "Test Issue" if "Test Issue" in df.columns else None
        name_col = "Security Name" if "Security Name" in df.columns else None

        sym_col = (
            "Symbol"
            if "Symbol" in df.columns
            else "ACT Symbol"
        )

        if etf_col:
            df = df[df[etf_col] != "Y"]

        if test_col:
            df = df[df[test_col] != "Y"]

        if name_col:
            bad = (
                r"\b(?:unit|units|warrant|warrants|right|rights|"
                r"preferred|depositary)\b"
            )

            df = df[
                ~df[name_col]
                .astype(str)
                .str.contains(
                    bad,
                    case=False,
                    regex=True,
                    na=False,
                )
            ]

        s = (
            df[sym_col]
            .dropna()
            .astype(str)
            .str.strip()
        )

        s = s[
            (s.str.len() > 0)
            & (s.str.upper() != "NAN")
        ]

        s = s[
            ~s.str.contains(
                r"[.$^]",
                regex=True,
                na=False,
            )
        ]

        s = s[
            ~(
                (s.str.len() == 5)
                & (s.str[-1].isin(["U", "W", "R"]))
            )
        ]

        syms += s.tolist()

    return sorted(
        {
            x
            for x in syms
            if isinstance(x, str) and x
        }
    )


def load_universe_cached():
    """
    Prefer the universe used by the shared cache.

    fetch_bars.py already downloaded and filtered the NASDAQ Trader
    universe, so reusing that list saves two downloads and guarantees
    the screener and cache agree on which symbols exist.

    Returns bare symbols, matching load_universe().
    """

    if CACHE.available:
        syms = CACHE.symbols

        if syms:
            print(
                f"Universe from cache: "
                f"{len(syms)} symbols"
            )
            return syms

    print(
        "No cache — building universe "
        "from NASDAQ Trader files"
    )

    return load_universe()


# ---------------------------------------------------------------------------
# Bar handling
# ---------------------------------------------------------------------------

def bars_to_frame(rows):
    """
    Convert cached bars into the same DataFrame structure returned
    by td_daily().

    Requirements:
    - datetime index
    - sorted oldest-first
    - long OHLCV column names
    """

    d = pd.DataFrame(rows).rename(
        columns={
            "d": "datetime",
            "o": "open",
            "h": "high",
            "l": "low",
            "c": "close",
            "v": "volume",
        }
    )

    d["datetime"] = pd.to_datetime(
        d["datetime"]
    )

    d = (
        d
        .set_index("datetime")
        .sort_index()
    )

    for c in [
        "open",
        "high",
        "low",
        "close",
        "volume",
    ]:
        d[c] = pd.to_numeric(
            d[c],
            errors="coerce",
        )

    return d[
        [
            "open",
            "high",
            "low",
            "close",
            "volume",
        ]
    ]


def td_daily(
    symbol,
    outputsize=260,
    tries=6,
):
    """
    Return daily bars for a symbol.

    Uses the shared cache first. In Alpaca mode a cache miss is not allowed to
    fall through to Twelve Data, so the production ORB run can never consume
    Twelve Data credits accidentally.
    """

    cached = CACHE.get(symbol)

    if cached:
        return bars_to_frame(cached)

    if DATA_PROVIDER == "alpaca":
        return None

    params = dict(
        symbol=symbol,
        interval="1day",
        outputsize=outputsize,
        order="ASC",
        timezone="America/New_York",
        apikey=API_KEY,
    )

    for _ in range(tries):

        j = requests.get(
            "https://api.twelvedata.com/time_series",
            params=params,
            timeout=30,
        ).json()

        if "values" in j:

            d = pd.DataFrame(
                j["values"]
            )

            d["datetime"] = pd.to_datetime(
                d["datetime"]
            )

            d = (
                d
                .set_index("datetime")
                .sort_index()
            )

            for c in [
                "open",
                "high",
                "low",
                "close",
                "volume",
            ]:
                d[c] = pd.to_numeric(
                    d[c],
                    errors="coerce",
                )

            # Only throttle real API calls.
            if THROTTLE:
                time.sleep(THROTTLE)

            return d[
                [
                    "open",
                    "high",
                    "low",
                    "close",
                    "volume",
                ]
            ]

        msg = str(
            j.get("message", "")
        ).lower()

        if any(
            k in msg
            for k in (
                "credit",
                "run out",
                "limit",
            )
        ):
            time.sleep(61)
            continue

        return None

    return None


# ---------------------------------------------------------------------------
# Screen
# ---------------------------------------------------------------------------

def compute_screen(
    d,
    p=P,
    debug=False,
):
    """
    Return:

        passed
        metrics
        criteria

    Pattern and trend-quality gates only.

    Dollar volume is calculated for information/display purposes but
    is NOT used as a pass/fail criterion.
    """

    if d is None or len(d) < 60:
        return False, {}, {}

    c = d["close"]
    h = d["high"]
    l = d["low"]
    v = d["volume"]

    # -----------------------------------------------------------------------
    # Moving averages
    # -----------------------------------------------------------------------

    sma10 = c.rolling(10).mean()
    sma20 = c.rolling(20).mean()
    sma50 = c.rolling(50).mean()
    sma200 = c.rolling(200).mean()

    # -----------------------------------------------------------------------
    # ADR
    # -----------------------------------------------------------------------

    adr = 100.0 * (
        (h / l)
        .rolling(20)
        .mean()
        .iloc[-1]
        - 1.0
    )

    price = float(
        c.iloc[-1]
    )

    # -----------------------------------------------------------------------
    # Dollar volume
    #
    # INFORMATION ONLY.
    #
    # There is deliberately no minimum dollar-volume requirement.
    # -----------------------------------------------------------------------

    dvol = float(
        (c * v)
        .rolling(20)
        .mean()
        .iloc[-1]
    )

    # -----------------------------------------------------------------------
    # Previous run-up
    # -----------------------------------------------------------------------

    run_lb = min(
        p["RUNUP_LB"],
        len(d) - 1,
    )

    run_low = float(
        l.iloc[-run_lb:].min()
    )

    run_high = float(
        h.iloc[
            -(p["BASE_MIN"] + 5):
        ].max()
    )

    runup = (
        (run_high - run_low)
        / run_low
        * 100.0
        if run_low > 0
        else np.nan
    )

    # -----------------------------------------------------------------------
    # Base / coil
    #
    # Must be coiling beneath a recent high rather than already
    # printing a new breakout high.
    # -----------------------------------------------------------------------

    win = d.iloc[
        -(p["BASE_MIN"] + 4):
    ]

    hh = win[
        "high"
    ].to_numpy()

    peak_back = (
        len(hh)
        - 1
        - int(hh.argmax())
    )

    recent_high = float(
        hh.max()
    )

    base_low = float(
        win["low"].min()
    )

    base_depth = (
        (recent_high - base_low)
        / recent_high
        * 100.0
        if recent_high > 0
        else np.nan
    )

    # -----------------------------------------------------------------------
    # Range contraction
    #
    # Average range over latest BASE_MIN days must be less than the
    # average range during the preceding BASE_MIN days.
    # -----------------------------------------------------------------------

    rng = h - l

    contracting = (
        rng.iloc[
            -p["BASE_MIN"]:
        ].mean()
        <
        rng.iloc[
            -2 * p["BASE_MIN"]:
            -p["BASE_MIN"]
        ].mean()
    )

    # -----------------------------------------------------------------------
    # Must be pulled in beneath the recent high
    # -----------------------------------------------------------------------

    pulled_in = (
        price
        <
        recent_high
        * (
            1
            - p["PULLBACK_MIN"] / 100
        )
    )

    coiling = (
        peak_back
        >= p["PEAK_MIN_BACK"]
        and pulled_in
    )

    base_ok = bool(
        contracting
        and coiling
    )

    # -----------------------------------------------------------------------
    # Higher lows
    #
    # Lowest low of latest BASE_MIN days must be higher than lowest
    # low of preceding BASE_MIN days.
    # -----------------------------------------------------------------------

    lows_up = (
        l.iloc[
            -p["BASE_MIN"]:
        ].min()
        >
        l.iloc[
            -2 * p["BASE_MIN"]:
            -p["BASE_MIN"]
        ].min()
    )

    # -----------------------------------------------------------------------
    # Moving-average support
    # -----------------------------------------------------------------------

    def ma_status(ma):

        rising = bool(
            ma.iloc[-1]
            >
            ma.iloc[-6]
        )

        nearby = bool(
            l.iloc[-1]
            <=
            ma.iloc[-1]
            * (
                1
                + p["MA_TOL"] / 100
            )
        )

        return rising, nearby

    r10, n10 = ma_status(sma10)
    r20, n20 = ma_status(sma20)
    r50, n50 = ma_status(sma50)

    surf = bool(
        (r10 and n10)
        or
        (r20 and n20)
        or
        (r50 and n50)
    )

    # Distance above/below 10-day MA.
    # Informational only.
    ext10 = (
        (
            price
            - sma10.iloc[-1]
        )
        / sma10.iloc[-1]
        * 100
        if sma10.iloc[-1]
        else np.nan
    )

    # -----------------------------------------------------------------------
    # Trend gates
    #
    # Above 50MA is mandatory.
    #
    # Above 200MA is mandatory when a 200MA exists.
    # Younger stocks without enough history automatically pass.
    # -----------------------------------------------------------------------

    above50 = bool(
        not pd.isna(
            sma50.iloc[-1]
        )
        and
        price > sma50.iloc[-1]
    )

    has200 = bool(
        not pd.isna(
            sma200.iloc[-1]
        )
    )

    above200 = bool(
        (not has200)
        or
        (
            price
            > sma200.iloc[-1]
        )
    )

    # -----------------------------------------------------------------------
    # Pass/fail criteria
    #
    # NO LIQUIDITY FILTER.
    # -----------------------------------------------------------------------

    crit = dict(

        price=bool(
            price
            >= p["PRICE_MIN"]
        ),

        adr=bool(
            p["ADR_MIN"]
            <= adr
            < p["ADR_MAX"]
        ),

        runup=bool(
            (
                not np.isnan(runup)
            )
            and
            p["RUNUP_MIN"]
            <= runup
            <= p["RUNUP_MAX"]
        ),

        base=base_ok,

        hl=bool(
            lows_up
        ),

        surf=surf,

        t50=above50,

        t200=above200,
    )

    # Every criterion must pass.
    passed = all(
        crit.values()
    )

    # -----------------------------------------------------------------------
    # Output metrics
    # -----------------------------------------------------------------------

    metrics = dict(

        price=round(
            price,
            2,
        ),

        adr=round(
            adr,
            2,
        ),

        runup=(
            round(
                float(runup),
                1,
            )
            if not np.isnan(runup)
            else None
        ),

        base_depth=(
            round(
                float(base_depth),
                1,
            )
            if not np.isnan(base_depth)
            else None
        ),

        ext10=(
            round(
                float(ext10),
                1,
            )
            if not np.isnan(ext10)
            else None
        ),

        # Still shown for information,
        # but not used as a filter.
        dvolM=round(
            dvol / 1e6,
            1,
        ),

        trend200=(
            "yes"
            if (
                has200
                and
                price > sma200.iloc[-1]
            )
            else (
                "n/a"
                if not has200
                else "no"
            )
        ),
    )

    # -----------------------------------------------------------------------
    # Debug information
    # -----------------------------------------------------------------------

    if debug:

        metrics.update(
            dict(

                peak_back=int(
                    peak_back
                ),

                contracting=bool(
                    contracting
                ),

                pulled_in=bool(
                    pulled_in
                ),

                rising=(
                    f"{int(r10)}"
                    f"{int(r20)}"
                    f"{int(r50)}"
                ),

                nearby=(
                    f"{int(n10)}"
                    f"{int(n20)}"
                    f"{int(n50)}"
                ),

                above50=above50,

                above200=above200,
            )
        )

    return (
        passed,
        metrics,
        crit,
    )


# ---------------------------------------------------------------------------
# Outputs
# ---------------------------------------------------------------------------

def write_outputs(
    rows,
    universe_n=None,
):

    os.makedirs(
        OUT_DIR,
        exist_ok=True,
    )

    os.makedirs(
        DOCS_DIR,
        exist_ok=True,
    )

    df = pd.DataFrame(
        rows
    )

    # Calmest ADR first.
    if (
        len(df)
        and
        "adr" in df.columns
    ):

        df = (
            df
            .sort_values(
                "adr",
                ascending=True,
            )
            .reset_index(
                drop=True
            )
        )

    stamp = (
        dt.datetime
        .now(
            dt.timezone.utc
        )
        .strftime(
            "%Y-%m-%d %H:%M UTC"
        )
    )

    # -----------------------------------------------------------------------
    # CSV
    # -----------------------------------------------------------------------

    df.to_csv(
        os.path.join(
            OUT_DIR,
            "candidates.csv",
        ),
        index=False,
    )

    # -----------------------------------------------------------------------
    # TradingView watchlist
    # -----------------------------------------------------------------------

    tickers = (
        df["symbol"].tolist()
        if len(df)
        else []
    )

    tv_list = ",".join(
        tickers
    )

    for directory in (
        DOCS_DIR,
        OUT_DIR,
    ):

        with open(
            os.path.join(
                directory,
                "watchlist.txt",
            ),
            "w",
        ) as f:

            f.write(
                tv_list
            )

    # -----------------------------------------------------------------------
    # HTML table
    # -----------------------------------------------------------------------

    cols = [
        c
        for c in [
            "symbol",
            "price",
            "adr",
            "runup",
            "base_depth",
            "ext10",
            "dvolM",
            "trend200",
        ]
        if c in df.columns
    ]

    body = (
        df[cols].to_html(
            index=False,
            border=0,
        )
        if len(df)
        else
        "<p>No candidates today.</p>"
    )

    html = f"""
<!doctype html>

<meta charset="utf-8">

<title>ORB Continuation Watchlist</title>

<style>

body {{
    font-family: system-ui;
    margin: 2rem;
    background: #0f1115;
    color: #e6e6e6;
}}

table {{
    border-collapse: collapse;
    width: 100%;
}}

th,
td {{
    padding: .5rem .8rem;
    border-bottom: 1px solid #333;
    text-align: right;
}}

th:first-child,
td:first-child {{
    text-align: left;
    font-weight: 600;
}}

h1 {{
    font-size: 1.2rem;
}}

small {{
    color: #9aa;
}}

.btn {{
    display: inline-block;
    margin: .2rem .5rem .2rem 0;
    padding: .55rem .9rem;
    border: 0;
    border-radius: 8px;
    background: #2d7dff;
    color: #fff;
    font: inherit;
    font-weight: 600;
    cursor: pointer;
    text-decoration: none;
}}

.btn.copy {{
    background: #3a3f4b;
}}

textarea {{
    width: 100%;
    height: 3.2rem;
    margin-top: .6rem;
    background: #161a22;
    color: #cdd3df;
    border: 1px solid #333;
    border-radius: 8px;
    padding: .5rem;
    font-family: ui-monospace, monospace;
}}

</style>

<h1>
ORB Continuation Watchlist
<small>
({len(df)} candidates &middot; {stamp})
</small>
</h1>

<p>
<small>
Price $3+ &middot;
ADR {P['ADR_MIN']}%-{P['ADR_MAX']}% &middot;
{P['RUNUP_MIN']}%-{P['RUNUP_MAX']}% prior run-up &middot;
above 50-MA &middot;
coiling below a recent high &middot;
no liquidity filter &middot;
sorted by ADR (calmest first).
</small>
</p>

<p>

<a
    class="btn"
    href="watchlist.txt"
    download="orb_watchlist.txt"
>
&#11015; Download TradingView list (.txt)
</a>

<button
    class="btn copy"
    onclick="
        navigator.clipboard.writeText(
            document.getElementById('tv').value
        );
        this.textContent='Copied!'
    "
>
Copy tickers
</button>

</p>

<textarea
    id="tv"
    readonly
    onclick="this.select()"
>{tv_list}</textarea>

{body}
"""

    with open(
        os.path.join(
            DOCS_DIR,
            "index.html",
        ),
        "w",
    ) as f:

        f.write(
            html
        )

    print(
        f"Wrote {len(df)} candidates -> "
        f"{OUT_DIR}/candidates.csv, "
        f"{DOCS_DIR}/watchlist.txt, "
        f"{DOCS_DIR}/index.html"
    )

    # -----------------------------------------------------------------------
    # Shared dashboard output
    # -----------------------------------------------------------------------

    rows_sorted = sorted(
        rows,
        key=lambda r: (
            r.get("adr")
            if isinstance(
                r.get("adr"),
                (int, float),
            )
            else float("inf")
        ),
    )

    meta = {
        "sort":
            "ADR ascending "
            "(calmest first)",

        "criteria": {
            "price_min": P["PRICE_MIN"],
            "adr_min": P["ADR_MIN"],
            "adr_max": P["ADR_MAX"],
            "runup_min": P["RUNUP_MIN"],
            "runup_max": P["RUNUP_MAX"],
            "liquidity_filter": False,
        },
    }

    if universe_n is not None:
        meta["universe"] = universe_n

    write_scan(
        SCAN_ID,
        rows_sorted,
        label=SCAN_LABEL,
        meta=meta,
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():

    if DATA_PROVIDER not in {"alpaca", "twelve_data"}:

        raise SystemExit(
            "ORB_DATA_PROVIDER must be alpaca or twelve_data"
        )

    if DATA_PROVIDER == "alpaca" and not CACHE.available:

        raise SystemExit(
            "Alpaca bar cache is missing or empty; refusing to fall back "
            "to Twelve Data"
        )

    if DATA_PROVIDER == "twelve_data" and not API_KEY:

        raise SystemExit(
            "TWELVE_DATA_KEY not set for twelve_data mode"
        )

    # -----------------------------------------------------------------------
    # Debug mode
    #
    # Example:
    #
    # DEBUG_SYMBOLS="NVDA AMD TSLA" python orb.py
    # -----------------------------------------------------------------------

    dbg = os.environ.get(
        "DEBUG_SYMBOLS",
        "",
    ).strip()

    if dbg:

        syms = [
            s.strip().upper()
            for s in (
                dbg
                .replace(",", " ")
                .split()
            )
            if s.strip()
        ]

        print(
            f"DEBUG mode: "
            f"{len(syms)} symbols\n"
        )

        for sym in syms:

            passed, m, crit = (
                compute_screen(
                    td_daily(sym),
                    debug=True,
                )
            )

            if not crit:

                print(
                    f"{sym:<6} "
                    "insufficient data "
                    "(<60 bars)"
                )

                continue

            fails = [
                k
                for k, vv
                in crit.items()
                if not vv
            ]

            flags = " ".join(
                f"{k}"
                f"{'.' if vv else 'X'}"
                for k, vv
                in crit.items()
            )

            print(
                f"{sym:<6} "
                f"{'PASS' if passed else 'REJECT':<6} "
                f"fails={fails}"
            )

            print(
                f"        {flags}"
            )

            print(
                f"        "
                f"price={m['price']} "
                f"adr={m['adr']} "
                f"runup={m['runup']} "
                f"dvolM={m['dvolM']} "
                f"base_depth={m['base_depth']} "
                f"ext10={m['ext10']} "
                f"trend200={m['trend200']}"
            )

            print(
                f"        "
                f"peak_back="
                f"{m.get('peak_back')} "
                f"contracting="
                f"{m.get('contracting')} "
                f"pulled_in="
                f"{m.get('pulled_in')} "
                f"rising(10/20/50)="
                f"{m.get('rising')} "
                f"near(10/20/50)="
                f"{m.get('nearby')}"
                "\n"
            )

        return

    # -----------------------------------------------------------------------
    # Load universe
    # -----------------------------------------------------------------------

    universe = (
        load_universe_cached()
    )

    if MAX_SYMBOLS:

        universe = universe[
            :MAX_SYMBOLS
        ]

    print(
        f"Universe: "
        f"{len(universe)} symbols."
    )

    # -----------------------------------------------------------------------
    # Funnel criteria
    #
    # Liquidity has intentionally been removed.
    # -----------------------------------------------------------------------

    keys = [
        "price",
        "adr",
        "runup",
        "base",
        "hl",
        "surf",
        "t50",
        "t200",
    ]

    crits = []
    rows = []

    # -----------------------------------------------------------------------
    # Run screen
    # -----------------------------------------------------------------------

    for i, sym in enumerate(
        universe,
        1,
    ):

        try:

            passed, m, crit = (
                compute_screen(
                    td_daily(sym)
                )
            )

            if not crit:
                continue

            crits.append(
                crit
            )

            if passed:

                rows.append(
                    dict(
                        symbol=sym,
                        **m,
                    )
                )

                print(
                    f"  "
                    f"[{i}/{len(universe)}] "
                    f"HIT {sym}"
                )

        except Exception as e:

            print(
                f"  "
                f"[{i}/{len(universe)}] "
                f"{sym} err: "
                f"{str(e)[:60]}"
            )

    # -----------------------------------------------------------------------
    # Filter funnel
    # -----------------------------------------------------------------------

    print(
        f"\nFUNNEL "
        f"({len(crits)} symbols "
        f"with >=60 bars):"
    )

    for k in keys:

        print(
            f"  {k:>6}: "
            f"{sum(1 for cr in crits if cr[k]):>5} "
            f"pass individually"
        )

    print(
        "  stacked (in order):"
    )

    cum = crits

    for k in keys:

        cum = [
            cr
            for cr in cum
            if cr[k]
        ]

        print(
            f"    + {k:<6} "
            f"-> {len(cum):>5}"
        )

    # -----------------------------------------------------------------------
    # Write outputs
    # -----------------------------------------------------------------------

    write_outputs(
        rows,
        universe_n=len(universe),
    )

    CACHE.report()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":

    try:

        main()

    except Exception as exc:

        try:

            write_failure(
                SCAN_ID,
                exc,
                label=SCAN_LABEL,
            )

        except Exception:
            pass

        raise
