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
            if not symbol or etf == "Y" or test == "Y": continue
            if any(char in BAD_SYMBOL_CHARS for char in symbol): continue
            if any(word in name for word in BAD_NAME_WORDS): continue
            if is_nasdaq_file: symbols.add(symbol)
            elif exchange_code == "N": symbols.add(symbol)
        print(f"[universe] loaded {url.rsplit('/', 1)[-1]}", flush=True)
    return sorted(symbols)


def alpaca_json(params, api_key, api_secret, requests_per_minute, retries=6):
    query = urllib.parse.urlencode(params)
    url = f"{ALPACA_BARS_URL}?{query}"
    headers = {"User-Agent":"portfolio-market-conditions/2.0","APCA-API-KEY-ID":api_key,"APCA-API-SECRET-KEY":api_secret}
    delay = 1.0
    for attempt in range(retries):
        try:
            request = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(request, timeout=90) as response:
                payload = json.loads(response.read().decode("utf-8"))
            if requests_per_minute > 0: time.sleep(60.0 / requests_per_minute)
            return payload
        except urllib.error.HTTPError as exc:
            if exc.code == 429 and attempt < retries-1:
                retry_after=float(exc.headers.get("Retry-After","0") or "0")
                time.sleep(max(retry_after,delay,60.0/max(1,requests_per_minute))); delay=min(delay*2,30); continue
            if 500 <= exc.code < 600 and attempt < retries-1:
                time.sleep(delay); delay=min(delay*2,30); continue
            raise
        except (urllib.error.URLError,TimeoutError,json.JSONDecodeError):
            if attempt == retries-1: raise
            time.sleep(delay); delay=min(delay*2,30)
    raise RuntimeError("Alpaca request failed after retries")


def fetch_series_batch(symbols, api_key, api_secret, requests_per_minute):
    end=datetime.now(timezone.utc); start=end-timedelta(days=420)
    collected={symbol:[] for symbol in symbols}; page_token=None
    while True:
        params={"symbols":",".join(symbols),"timeframe":"1Day","start":start.strftime("%Y-%m-%dT00:00:00Z"),"end":end.strftime("%Y-%m-%dT23:59:59Z"),"limit":10000,"adjustment":"split","feed":"iex","sort":"asc"}
        if page_token: params["page_token"]=page_token
        payload=alpaca_json(params,api_key,api_secret,requests_per_minute)
        for symbol,rows in (payload.get("bars") or {}).items():
            if symbol not in collected or not isinstance(rows,list): continue
            for raw in rows:
                close=to_number(raw.get("c")); volume=to_number(raw.get("v")); date=str(raw.get("t") or "")[:10]
                if close is not None and volume is not None and date: collected[symbol].append({"date":date,"close":close,"volume":volume})
        page_token=payload.get("next_page_token")
        if not page_token: break
    for symbol in collected:
        collected[symbol].sort(key=lambda bar:bar["date"]); collected[symbol]=collected[symbol][-OUTPUTSIZE:]
    return collected


def empty_count(date):
    return {"date":date,"coverage":0,"eligible":0,"up_4":0,"down_4":0,"up_45_10":0,"up_25_quarter":0,"down_25_quarter":0,"bull_34_13":0,"bear_34_13":0,"up_25_month":0,"down_25_month":0,"up_50_month":0,"down_50_month":0}


def aggregate_symbol(bars,counts):
    if len(bars)<21:return False
    dates=[b["date"] for b in bars]; closes=[b["close"] for b in bars]; volumes=[b["volume"] for b in bars]; date_to_index={d:i for i,d in enumerate(dates)}; contributed=False
    for date,current in counts.items():
        index=date_to_index.get(date)
        if index is None:continue
        current["coverage"]+=1; close=closes[index]
        if index<19 or close<MIN_PRICE:continue
        dollar_volume=[closes[i]*volumes[i] for i in range(index-19,index+1)]
        if sum(dollar_volume)/len(dollar_volume)<MIN_AVG_DOLLAR_VOLUME:continue
        current["eligible"]+=1; contributed=True
        if index>=1 and volumes[index]>=MIN_DAILY_VOLUME and volumes[index]>volumes[index-1]:
            daily_change=close/closes[index-1]-1
            if daily_change>=.04:current["up_4"]+=1
            elif daily_change<=-.04:current["down_4"]+=1
        if index>=10 and closes[index-10]>0 and close/closes[index-10]-1>.45:current["up_45_10"]+=1
        if index>=64:
            q=closes[index-64:index+1]
            if close/min(q)-1>=.25:current["up_25_quarter"]+=1
            if close/max(q)-1<=-.25:current["down_25_quarter"]+=1
        if index>=33:
            f=closes[index-33:index+1]
            if close/min(f)-1>=.13:current["bull_34_13"]+=1
            if close/max(f)-1<=-.13:current["bear_34_13"]+=1
        if index>=20 and closes[index-20]>=5:
            m=close/closes[index-20]-1
            if m>=.25:current["up_25_month"]+=1
            if m<=-.25:current["down_25_month"]+=1
            if m>=.50:current["up_50_month"]+=1
            if m<=-.50:current["down_50_month"]+=1
    return contributed


def ratio_record(items,index):
    if index<9:return None
    w=items[index-9:index+1]; u=sum(x["up_4"] for x in w); d=sum(x["down_4"] for x in w)
    return {"date":items[index]["date"],"up_total":u,"down_total":d,"ratio":None if d==0 else round(u/d,3),"infinite":d==0 and u>0}

def moving_average(values,period):return None if len(values)<period else sum(values[-period:])/period

def final_state(current,current_ratio,oneq):
    rv=current_ratio.get("ratio"); rp=bool(current_ratio.get("infinite")) or (rv is not None and rv>=1)
    tests={"primary_breadth":current["up_25_quarter"]>current["down_25_quarter"],"fast_breadth":current["bull_34_13"]>current["bear_34_13"],"ten_day_breadth":rp,"oneq_trend":bool(oneq.get("ma10") is not None and oneq.get("ma20") is not None and oneq["ma10"]>oneq["ma20"])}
    positives=sum(tests.values()); score=positives*25
    if positives>=3: label,colour,action="Favourable","green","Normal long exposure is permitted, subject to setup quality and your usual risk limits."
    elif positives<=1: label,colour,action="Defensive","red","Protect capital. Avoid marginal breakouts and keep new long exposure very small."
    else: label,colour,action="Selective","amber","Breadth is mixed. Take only the strongest setups and consider reduced total exposure."
    return {"label":label,"colour":colour,"score":score,"positive_signals":positives,"total_signals":4,"action":action,"tests":tests}


def build_output(api_key,api_secret,batch_size,requests_per_minute,max_symbols=0):
    print("[oneq] retrieving index-proxy history from Alpaca IEX",flush=True)
    oneq_bars=fetch_series_batch(["ONEQ"],api_key,api_secret,requests_per_minute).get("ONEQ",[])
    if len(oneq_bars)<HISTORY_SESSIONS:raise RuntimeError("ONEQ did not return enough daily history")
    target_dates=[b["date"] for b in oneq_bars[-HISTORY_SESSIONS:]]; counts={d:empty_count(d) for d in target_dates}
    universe=build_universe(); universe=universe[:max_symbols] if max_symbols>0 else universe
    print(f"[universe] {len(universe)} unique NASDAQ/NYSE securities selected",flush=True)
    valid_symbols=0; eligible_symbols=set(); failed_symbols=0; batches=list(chunks(universe,batch_size))
    for batch_number,batch in enumerate(batches,1):
        try:
            series=fetch_series_batch(batch,api_key,api_secret,requests_per_minute)
            for symbol in batch:
                bars=series.get(symbol,[])
                if not bars:failed_symbols+=1;continue
                valid_symbols+=1
                if aggregate_symbol(bars,counts):eligible_symbols.add(symbol)
        except Exception as exc:
            failed_symbols+=len(batch);print(f"[warning] batch {batch_number} failed: {type(exc).__name__}: {exc}",flush=True)
        if batch_number==1 or batch_number%10==0 or batch_number==len(batches):print(f"[progress] batch {batch_number}/{len(batches)} valid={valid_symbols} failed={failed_symbols}",flush=True)
    minimum_valid=max(500,math.ceil(len(universe)*.80))
    if valid_symbols<minimum_valid:raise RuntimeError(f"Coverage check failed: only {valid_symbols}/{len(universe)} symbols returned valid history")
    history=[counts[d] for d in target_dates]; ratio_history=[r for i in range(len(history)) if (r:=ratio_record(history,i))]; current=history[-1]; current_ratio=ratio_history[-1]
    oneq_closes=[b["close"] for b in oneq_bars]; ma10=moving_average(oneq_closes,10); ma20=moving_average(oneq_closes,20)
    oneq={"symbol":"ONEQ","close":round(oneq_closes[-1],4),"ma10":round(ma10,4) if ma10 is not None else None,"ma20":round(ma20,4) if ma20 is not None else None,"above":bool(ma10 is not None and ma20 is not None and ma10>ma20)}
    for i,item in enumerate(history):
        window=history[max(0,i-9):i+1]; item["up_45_10_ma"]=round(sum(x["up_45_10"] for x in window)/len(window),2)
    state=final_state(current,current_ratio,oneq)
    return {"schema_version":1,"generated_at_utc":datetime.now(timezone.utc).isoformat(),"as_of_market_date":current["date"],"source":"Alpaca IEX","universe":{"exchanges":list(EXCHANGES),"catalogue_symbols":len(universe),"valid_history_symbols":valid_symbols,"eligible_symbols":len(eligible_symbols),"failed_symbols":failed_symbols,"filters":{"min_price":MIN_PRICE,"min_avg_dollar_volume_20d":MIN_AVG_DOLLAR_VOLUME,"min_daily_volume_for_4pct":MIN_DAILY_VOLUME}},"current":current,"ten_day_ratio":current_ratio,"oneq":oneq,"condition":state,"history":history[-DISPLAY_SESSIONS:],"ten_day_ratio_history":ratio_history[-DISPLAY_SESSIONS:],"methodology":{"provider":"Alpaca","feed":"IEX","high_momentum":"Close more than 45% above its close 10 sessions earlier.","note":"IEX is a single-exchange feed and may materially undercount volume-based breadth versus consolidated SIP data."}}


def main():
    parser=argparse.ArgumentParser(); parser.add_argument("--output",default=str(DEFAULT_OUTPUT)); parser.add_argument("--max-symbols",type=int,default=0); args=parser.parse_args()
    api_key=os.environ.get("APCA_API_KEY_ID","").strip(); api_secret=os.environ.get("APCA_API_SECRET_KEY","").strip()
    if not api_key or not api_secret:print("Missing APCA_API_KEY_ID or APCA_API_SECRET_KEY",file=sys.stderr);return 2
    batch_size=max(1,int(os.environ.get("ALPACA_BATCH_SIZE","50"))); rpm=max(1,int(os.environ.get("ALPACA_REQUESTS_PER_MIN","170")))
    result=build_output(api_key,api_secret,batch_size,rpm,max_symbols=args.max_symbols); output=Path(args.output); output.parent.mkdir(parents=True,exist_ok=True); output.write_text(json.dumps(result,indent=2)+"\n",encoding="utf-8"); print(f"Wrote {output}",flush=True); return 0

if __name__=="__main__":raise SystemExit(main())
