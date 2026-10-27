#!/usr/bin/env python3
"""READ-ONLY US equity IEX 5Min journal and missing-candle diagnostics.

This observer can never submit orders. Records only original completed IEX bars,
never interpolates missing prices or volume. Data are a PARTIAL US market feed.
Both FAST (28) and classic BREAKOUT (10) names are observed on the same source;
Twelve Data remains the separate live classic source.
"""
import csv
import math
import os
from datetime import datetime, timedelta, timezone, time
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from equity_candle_guard import fully_closed_iex, verify_contiguous

NY=ZoneInfo("America/New_York")
API="https://data.alpaca.markets/v2/stocks/bars"
RETENTION_DAYS=14
BARS_PATH=Path("equity_iex_candles_v1.csv")
QUALITY_PATH=Path("equity_iex_quality_v1.csv")
BAR_FIELDS=("bar_utc","symbol","open","high","low","close","volume","trades","vwap","feed")
QUALITY_FIELDS=("bucket_utc","symbol","latest_bar_utc","completed_today",
                "missing_5min_slots","zero_volume_bars","latest_age_minutes",
                "reference_integrity","feed","data_status")
FAST_SYMBOLS=("SOFI","RIVN","DKNG","HIMS","CHWY","FSLY","PATH","U",
              "CLSK","IREN","MARA","RIOT","CELH","NU","AAL","CLF",
              "F","VALE","CCL","BB","SNAP","RGTI","QBTS","GME",
              "PTON","AA","LUNR","NOK")
BREAKOUT_SYMBOLS=("INTC","AMD","NVDA","MRVL","AAPL","TSLA",
                  "META","AMZN","MSFT","GOOGL")
SYMBOLS=tuple(dict.fromkeys(FAST_SYMBOLS+BREAKOUT_SYMBOLS))


def _read(path):
    if not path.exists():
        return []
    with path.open(newline="",encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _write(path,cols,rows):
    tmp=path.with_suffix(path.suffix+".tmp")
    with tmp.open("w",newline="",encoding="utf-8") as f:
        out=csv.DictWriter(f,fieldnames=cols)
        out.writeheader()
        out.writerows(rows)
    tmp.replace(path)


def _stamp(value):
    return datetime.fromisoformat(str(value).replace("Z","+00:00")).astimezone(timezone.utc)


def session_open(now):
    local=now.astimezone(NY)
    return datetime.combine(local.date(),time(9,30),NY).astimezone(timezone.utc)


def fetch_market_bars(now, request_get=requests.get):
    """Authenticated read-only multi-symbol pages; refuse silent truncation."""
    api_key=os.environ.get("ALPACA_API_KEY","").strip()
    secret=os.environ.get("ALPACA_SECRET_KEY","").strip()
    if not api_key or not secret:
        raise RuntimeError("Missing Alpaca read credentials; no data fabricated")
    start=session_open(now)
    if now.astimezone(NY).weekday()>=5 or now<start:
        return {}
    params={"symbols":",".join(SYMBOLS),"timeframe":"5Min",
            "start":start.isoformat(),"end":now.isoformat(),
            "adjustment":"raw","feed":"iex","sort":"asc","limit":10000}
    hdr={"APCA-API-KEY-ID":api_key,"APCA-API-SECRET-KEY":secret}
    found={}
    token=None
    seen=set()
    for page in range(12):
        req=dict(params)
        if token:
            req["page_token"]=token
        r=request_get(API,params=req,headers=hdr,timeout=25)
        r.raise_for_status()
        data=r.json()
        chunk=data.get("bars")
        if not isinstance(chunk,dict):
            raise RuntimeError("Malformed IEX bars response")
        for sym,items in chunk.items():
            if sym not in SYMBOLS or not isinstance(items,list):
                raise RuntimeError("Unrecognized IEX response")
            found.setdefault(sym,[]).extend(items)
        next_token=data.get("next_page_token")
        if not next_token:
            return found
        if next_token in seen:
            raise RuntimeError("Repeated IEX page token")
        seen.add(next_token)
        token=next_token
    raise RuntimeError("IEX pagination exceeded 12 pages; not publishing partial observations")


def store_bars(feed,now):
    """Merge only fully closed, valid original candles; idempotent and bounded."""
    cutoff=now-timedelta(days=RETENTION_DAYS)
    old={}
    for row in _read(BARS_PATH):
        try:
            if _stamp(row["bar_utc"])>=cutoff and row["symbol"] in SYMBOLS:
                old[(row["bar_utc"],row["symbol"])]=row
        except (KeyError,TypeError,ValueError,OverflowError):
            continue
    count=0
    for symbol,bars in feed.items():
        for candle in fully_closed_iex(bars,now,lag_seconds=10):
            try:
                stamp=_stamp(candle["t"])
                if stamp<cutoff or stamp<session_open(stamp):
                    continue
                if stamp.second or stamp.microsecond or stamp.minute%5:
                    continue
                o,h,l,c=[float(candle[k]) for k in ("o","h","l","c")]
                v=float(candle["v"])
                if not all(math.isfinite(z) and z>0 for z in (o,h,l,c)):
                    continue
                if not math.isfinite(v) or v<0 or h<max(o,c) or l>min(o,c) or h<l:
                    continue
                key=(stamp.isoformat(),symbol)
                if key in old:
                    continue
                n=candle.get("n")
                vw=candle.get("vw")
                old[key]={"bar_utc":stamp.isoformat(),"symbol":symbol,
                          "open":o,"high":h,"low":l,"close":c,"volume":v,
                          "trades":n if n is not None else "",
                          "vwap":vw if vw is not None else "",
                          "feed":"ALPACA_IEX_ONLY"}
                count+=1
            except (ValueError,KeyError,OverflowError,TypeError):
                continue
    if count or (BARS_PATH.exists() and len(old)!=len(_read(BARS_PATH))):
        _write(BARS_PATH,BAR_FIELDS,
               sorted(old.values(),key=lambda x:(x["bar_utc"],x["symbol"])))
    return count


def quality(symbol,raw,now):
    completed=fully_closed_iex(raw,now,lag_seconds=10)
    opened=session_open(now)
    this=[]
    for b in completed:
        try:
            t=_stamp(b["t"])
            if opened<=t<opened+timedelta(hours=6,minutes=30):
                this.append((t,b))
        except (ValueError,KeyError,TypeError):
            continue
    this.sort(key=lambda x:x[0])
    # Count missing actual IEX intervals only. No assumption of a trade in
    # every slot: IEX legitimately has minutes with no trades.
    missing=0
    for (a,_),(b,_) in zip(this,this[1:]):
        distance=int((b-a).total_seconds()//300)
        if distance>1:
            missing+=distance-1
    integrity="insufficient_bars"
    if this:
        valid,reason=verify_contiguous([x[1] for x in this],key="t",
                         count=15,session_open=opened)
        integrity=reason
    last=this[-1][0] if this else None
    age=(now-(last+timedelta(minutes=5))).total_seconds()/60 if last else None
    return {"symbol":symbol,
            "latest_bar_utc":last.isoformat() if last else "",
            "completed_today":len(this),"missing_5min_slots":missing,
            "zero_volume_bars":sum(float(b.get("v",0))==0 for _,b in this),
            "latest_age_minutes":round(age,2) if age is not None else "",
            "reference_integrity":integrity,"feed":"ALPACA_IEX_ONLY",
            "data_status":"OK" if this else "NO_COMPLETED_CANDLES"}


def store_quality(feed,now):
    bucket=now.replace(minute=(now.minute//30)*30,second=0,microsecond=0)
    cutoff=now-timedelta(days=RETENTION_DAYS)
    old={}
    for row in _read(QUALITY_PATH):
        try:
            if _stamp(row["bucket_utc"])>=cutoff:
                old[(row["bucket_utc"],row["symbol"])]=row
        except (KeyError,ValueError,TypeError):
            continue
    count=0
    for symbol in SYMBOLS:
        q=quality(symbol,feed.get(symbol,[]),now)
        key=(bucket.isoformat(),symbol)
        if key in old:
            continue
        old[key]={"bucket_utc":bucket.isoformat(),**q}
        count+=1
    if count or (QUALITY_PATH.exists() and len(old)!=len(_read(QUALITY_PATH))):
        _write(QUALITY_PATH,QUALITY_FIELDS,
               sorted(old.values(),key=lambda x:(x["bucket_utc"],x["symbol"])))
    return count


def run(now=None):
    now=now or datetime.now(timezone.utc)
    if now.astimezone(NY).weekday()>=5 or now<session_open(now):
        print("IEX observer outside regular US session; no trades; no journal additions")
        return {"out_of_session":True}
    feed=fetch_market_bars(now)
    if not feed:
        raise RuntimeError("No IEX data; no synthetic replacement")
    bars=store_bars(feed,now)
    diagnostics=store_quality(feed,now)
    reasons={}
    for sym in SYMBOLS:
        key=quality(sym,feed.get(sym,[]),now)["reference_integrity"]
        reasons[key]=reasons.get(key,0)+1
    print(f"EQUITY IEX OBSERVER READ ONLY {now.isoformat()} "
          f"symbols={len(SYMBOLS)} pairs_with_data={len(feed)} "
          f"new_bars={bars} new_quality={diagnostics} "
          f"integrity={reasons} NO PAPER ORDERS",flush=True)
    return {"new_bars":bars,"quality_rows":diagnostics,"sources":len(feed)}


if __name__=="__main__":
    run()
