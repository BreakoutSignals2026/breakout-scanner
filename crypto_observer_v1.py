#!/usr/bin/env python3
"""CRYPTO research-only market recorder and price-breakout candidate engine.

No broker credentials, no trading API and no orders. Alpaca US crypto bars
often contain zero trade volume: keep those bars as explicitly quality-flagged
OBSERVATIONS, but NEVER pass price-only candidates to PAPER execution.
"""
import csv
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path

from market_virtual_core import dt, full_bars

CANDLE_PATH = Path("crypto_candles_v1.csv")
QUALITY_PATH = Path("crypto_quality_v1.csv")
RETENTION_DAYS = 14
LOOKBACK = 12
RESEARCH_BREAKOUT_PCT = 0.10
CANDLE_COLUMNS = ("bar_utc","symbol","open","high","low","close","volume","trades","zero_volume")
QUALITY_COLUMNS = ("bucket_utc","symbol","latest_bar_utc","closed_bars",
                   "nonzero_previous12","latest_volume","breakout_pct",
                   "relative_volume","strict_reason","price_only_signal")


def _save(path, columns, rows):
    tmp=path.with_suffix(path.suffix+".tmp")
    with tmp.open("w",newline="",encoding="utf-8") as handle:
        writer=csv.DictWriter(handle,fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)
    tmp.replace(path)


def _load(path):
    if not path.exists():
        return []
    with path.open(newline="",encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def record_bars(symbol, raw_bars, now):
    """Backfill available *closed* 5m bars; bounded rolling market-data journal.

    Crucially, zero-volume bars are not silently removed or assigned volume.
    The study dataset captures broker-fed OHLCV, not exchange-wide turnover.
    """
    raw_by_t={str(b.get("t")):b for b in raw_bars}
    valid=full_bars(raw_bars,now,5)
    cutoff=now-timedelta(days=RETENTION_DAYS)
    existing={}
    for item in _load(CANDLE_PATH):
        try:
            if dt(item["bar_utc"])>=cutoff:
                existing[(item["symbol"],item["bar_utc"])]=item
        except (KeyError,ValueError,TypeError):
            continue
    added=0
    for bar in valid:
        if bar["t"]<cutoff:
            continue
        stamp=bar["t"].isoformat()
        key=(symbol,stamp)
        if key in existing:
            continue
        original=raw_by_t.get(stamp) or next(
            (r for r in raw_bars if r.get("t") and dt(r["t"])==bar["t"]),{})
        count=original.get("n")
        volume=bar["v"]
        existing[key]={"bar_utc":stamp,"symbol":symbol,
                       "open":str(bar["o"]),"high":str(bar["h"]),
                       "low":str(bar["l"]),"close":str(bar["c"]),
                       "volume":str(volume),
                       "trades":str(count) if count is not None else "",
                       "zero_volume":str(int(volume==0))}
        added+=1
    if added or (CANDLE_PATH.exists() and len(existing)!=len(_load(CANDLE_PATH))):
        rows=sorted(existing.values(),key=lambda x:(x["bar_utc"],x["symbol"]))
        _save(CANDLE_PATH,CANDLE_COLUMNS,rows)
    return added


def quality(symbol, raw_bars, now, strict_signal=None):
    bars=full_bars(raw_bars,now,5)
    sample={"symbol":symbol,"closed_bars":len(bars),
            "nonzero_previous12":0,"latest_volume":None,
            "breakout_pct":None,"relative_volume":None,
            "strict_reason":"INSUFFICIENT_BARS","price_only_signal":False}
    if not bars:
        return sample
    last=bars[-1]
    sample["latest_bar_utc"]=last["t"].isoformat()
    sample["latest_volume"]=last["v"]
    if len(bars)<LOOKBACK+1:
        return sample
    prior=bars[-LOOKBACK-1:-1]
    vols=[x["v"] for x in prior if x["v"]>0]
    upper=max(x["h"] for x in prior)
    pct=(last["c"]/upper-1)*100
    sample.update(nonzero_previous12=len(vols),
                  breakout_pct=round(pct,4),
                  relative_volume=round(last["v"]/(sum(vols)/len(vols)),4) if vols else None)
    if strict_signal:
        sample["strict_reason"]="STRICT_BREAKOUT"
    elif len(vols)<10:
        sample["strict_reason"]="SPARSE_REFERENCE_VOLUME"
    elif last["v"]<=0:
        sample["strict_reason"]="ZERO_LATEST_VOLUME"
    elif pct<0.15 or last["c"]<=upper:
        sample["strict_reason"]="NO_STRICT_PRICE_BREAKOUT"
    elif sample["relative_volume"]<1.5:
        sample["strict_reason"]="LOW_RELATIVE_VOLUME"
    else:
        sample["strict_reason"]="NOT_CURRENT_OR_OTHER_FILTER"
    sample["price_only_signal"]=bool(price_only_signal(symbol,raw_bars,now))
    return sample


def record_quality(report, now):
    """Write one diagnostic per pair per half-hour; 14-day rolling history."""
    bucket=now.replace(minute=(now.minute//30)*30,second=0,microsecond=0)
    cutoff=now-timedelta(days=RETENTION_DAYS)
    old={}
    for row in _load(QUALITY_PATH):
        try:
            if dt(row["bucket_utc"])>=cutoff:
                old[(row["bucket_utc"],row["symbol"])]=row
        except (ValueError,KeyError,TypeError):
            continue
    stamp=bucket.isoformat()
    key=(stamp,report["symbol"])
    if key in old:
        return False
    row={"bucket_utc":stamp,
         "symbol":report["symbol"],
         "latest_bar_utc":report.get("latest_bar_utc",""),
         "closed_bars":report.get("closed_bars",0),
         "nonzero_previous12":report.get("nonzero_previous12",0),
         "latest_volume":report.get("latest_volume"),
         "breakout_pct":report.get("breakout_pct"),
         "relative_volume":report.get("relative_volume"),
         "strict_reason":report.get("strict_reason",""),
         "price_only_signal":str(bool(report.get("price_only_signal",False)))}
    old[key]=row
    _save(QUALITY_PATH,QUALITY_COLUMNS,
          sorted(old.values(),key=lambda x:(x["bucket_utc"],x["symbol"])))
    return True


def price_only_signal(symbol, raw_bars, now):
    """Experimental LONG research candidate, never broker-eligible.

    Uses *closed* OHLC only and deliberately ignores traded volume, which
    Alpaca often reports as 0. Requires a fresh non-flat closed candle with a
    0.10% breakout. No fabricated volume or look-ahead bars.
    """
    bars=full_bars(raw_bars,now,5)
    if len(bars)<LOOKBACK+1:
        return None
    last=bars[-1]
    age=(now-(last["t"]+timedelta(minutes=5))).total_seconds()/60
    if not -0.1<=age<=8:
        return None
    prev=bars[-LOOKBACK-1:-1]
    upper=max(b["h"] for b in prev)
    pct=(last["c"]/upper-1)*100
    bar_range=(last["h"]-last["l"])/last["c"]*100
    if last["c"]<=upper or pct<RESEARCH_BREAKOUT_PCT or bar_range<0.02:
        return None
    return {"symbol":symbol,"side":"buy","bar":last["t"].isoformat(),
            "bar_close":(last["t"]+timedelta(minutes=5)).isoformat(),
            "reference_price":last["c"],"breakout_pct":round(pct,4),
            "relvol":0.0,"age_minutes":round(age,2),
            "research_only":True,"volume_confirmed":False,
            "zero_volume":last["v"]==0,
            "feed_note":"ALPACA_OHLC_PRICE_ONLY_NOT_PAPER_ELIGIBLE"}
