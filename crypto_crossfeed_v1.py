#!/usr/bin/env python3
"""CRYPTO V1.2: cross-venue observations and strict trend RESEARCH only.

Alpaca US ('us') is the PAPER venue feed. Kraken US ('us-1') is independent
secondary *market data*. Never use Kraken OHLCV to authorize broker trades.
Never infer fills at Kraken from Alpaca candle price; trend VirtualBook
simulates hypothetical Alpaca executions at the next completed US candle.
"""
import math
from datetime import timedelta
from pathlib import Path

from crypto_observer_v1 import _load, _save, RETENTION_DAYS
from market_virtual_core import dt, full_bars

KRAKEN_CANDLES = Path("crypto_kraken_candles_v1.csv")
COMPARE_PATH = Path("crypto_crossfeed_v1.csv")
COMPARE_COLUMNS = (
    "bucket_utc","symbol","us_latest","kraken_latest",
    "aligned_bars_1h","us_nonzero_12","kraken_nonzero_12",
    "us_last_volume","kraken_last_volume","close_deviation_pct",
    "kraken_rel_volume","trend_reason","trend_signal","secondary_status"
)
LOOKBACK = 12
MAX_PRICE_DIVERGENCE_PCT = 0.75
MIN_KRAKEN_PRIOR_POSITIVE = 8
MIN_KRAKEN_REL_VOLUME = 1.20
BREAKOUT_MIN_PERCENT = 0.10


def _round(x, precision=4):
    return None if x is None or not math.isfinite(x) else round(x, precision)


def evaluate(symbol, primary_raw, kraken_raw, now):
    """Return per-cycle diagnostics and hypothetical trend breakout signal.

    The *same timestamp* and at least 21 contiguous completed bars are
    required on both feeds; never substitute delayed quotes or fabricate
    missing candles. All signals are LONG, virtual and PAPER-ineligible.
    """
    us=full_bars(primary_raw,now,5)
    kr=full_bars(kraken_raw,now,5)
    report={"symbol":symbol,"us_latest":us[-1]["t"].isoformat() if us else "",
            "kraken_latest":kr[-1]["t"].isoformat() if kr else "",
            "aligned_bars_1h":0,"us_nonzero_12":0,"kraken_nonzero_12":0,
            "us_last_volume":us[-1]["v"] if us else None,
            "kraken_last_volume":kr[-1]["v"] if kr else None,
            "close_deviation_pct":None,"kraken_rel_volume":None,
            "trend_reason":"INSUFFICIENT_BARS","trend_signal":False,
            "secondary_status":"OK" if kr else "NO_SECONDARY_DATA"}
    if not us:
        report["trend_reason"]="NO_ALPACA_US_BARS"
        return report,None
    if not kr:
        report["trend_reason"]="KRAKEN_DATA_MISSING"
        return report,None
    kr_by={bar["t"]:bar for bar in kr}
    now_stamp=us[-1]["t"]
    aligned=[(bar,kr_by[bar["t"]]) for bar in us if bar["t"] in kr_by]
    latest_pairs=[(a,b) for a,b in aligned if a["t"]>=now_stamp-timedelta(minutes=55)]
    report["aligned_bars_1h"]=len(latest_pairs)
    up=us[-LOOKBACK-1:-1]
    kp=kr[-LOOKBACK-1:-1]
    report["us_nonzero_12"]=sum(bar["v"]>0 for bar in up)
    report["kraken_nonzero_12"]=sum(bar["v"]>0 for bar in kp)
    if len(kp)>0:
        positive=[b["v"] for b in kp if b["v"]>0]
        if positive:
            report["kraken_rel_volume"]=_round(kr[-1]["v"]/(sum(positive)/len(positive)))
    if now_stamp not in kr_by:
        report["trend_reason"]="LATEST_BAR_NOT_ALIGNED"
        return report,None
    k_last=kr_by[now_stamp]
    deviation=abs(k_last["c"]/us[-1]["c"]-1)*100
    report["close_deviation_pct"]=_round(deviation)
    if len(us)<22 or len(kr)<22:
        return report,None
    window_us=us[-22:]
    window_k=[kr_by.get(b["t"]) for b in window_us]
    if any(x is None for x in window_k):
        report["trend_reason"]="MISSING_ALIGNED_TREND_HISTORY"
        return report,None
    if any(window_us[i]["t"]-window_us[i-1]["t"]!=timedelta(minutes=5)
           for i in range(1,len(window_us))):
        report["trend_reason"]="GAPPED_FIVE_MINUTE_BARS"
        return report,None
    age=(now-(us[-1]["t"]+timedelta(minutes=5))).total_seconds()/60
    if not -0.1<=age<=8:
        report["trend_reason"]="STALE_CLOSED_BAR"
        return report,None
    if deviation>MAX_PRICE_DIVERGENCE_PCT:
        report["trend_reason"]="PRICE_VENUE_DIVERGENCE"
        return report,None
    # For a meaningful volume filter use KRAKEN trade volume, not Alpaca's
    # synthetic quote-derived zero-volume bars. Never merge raw volumes.
    recent_kraken=window_k[-LOOKBACK-1:-1]
    positives=[b["v"] for b in recent_kraken if b["v"]>0]
    if len(positives)<MIN_KRAKEN_PRIOR_POSITIVE or k_last["v"]<=0:
        report["trend_reason"]="SPARSE_KRAKEN_TRADE_VOLUME"
        return report,None
    rv=k_last["v"]/(sum(positives)/len(positives))
    report["kraken_rel_volume"]=_round(rv)
    if rv<MIN_KRAKEN_REL_VOLUME:
        report["trend_reason"]="LOW_KRAKEN_REL_VOLUME"
        return report,None
    closes_us=[b["c"] for b in window_us]
    closes_kr=[b["c"] for b in window_k]
    us9=sum(closes_us[-9:])/9
    us21=sum(closes_us[-21:])/21
    k9=sum(closes_kr[-9:])/9
    k21=sum(closes_kr[-21:])/21
    if not (closes_us[-1]>us9>us21 and closes_kr[-1]>k9>k21):
        report["trend_reason"]="TREND_NOT_CONFIRMED_ON_BOTH_FEEDS"
        return report,None
    upper=max(b["h"] for b in window_us[-LOOKBACK-1:-1])
    breakout=(us[-1]["c"]/upper-1)*100
    if breakout<BREAKOUT_MIN_PERCENT or us[-1]["c"]<=upper:
        report["trend_reason"]="NO_ALPACA_US_PRICE_BREAKOUT"
        return report,None
    report["trend_reason"]="TREND_CONFIRMED_RESEARCH"
    report["trend_signal"]=True
    sig={"symbol":symbol,"side":"buy",
         "bar":us[-1]["t"].isoformat(),
         "bar_close":(us[-1]["t"]+timedelta(minutes=5)).isoformat(),
         "reference_price":us[-1]["c"],
         "breakout_pct":round(breakout,4),
         "relvol":_round(rv,3),"age_minutes":round(age,2),
         "research_only":True,"paper_eligible":False,
         "kraken_trend_confirmed":True,
         "kraken_volume_confirmed":True,
         "us_kraken_deviation_pct":report["close_deviation_pct"],
         "feed_note":"US_KRAKEN_RESEARCH_NEVER_BROKER_SIGNAL"}
    return report,sig


def record_comparison(report, now):
    """One record per symbol/30 minutes; durable even when Kraken is missing."""
    bucket=now.replace(minute=(now.minute//30)*30,second=0,microsecond=0)
    cutoff=now-timedelta(days=RETENTION_DAYS)
    old={}
    for row in _load(COMPARE_PATH):
        try:
            if dt(row["bucket_utc"])>=cutoff:
                old[(row["bucket_utc"],row["symbol"])]=row
        except (ValueError,KeyError,TypeError):
            continue
    key=(bucket.isoformat(),report["symbol"])
    if key in old:
        return False
    row={"bucket_utc":bucket.isoformat(),
         **{c:report.get(c,"") for c in COMPARE_COLUMNS if c!="bucket_utc"}}
    old[key]=row
    _save(COMPARE_PATH,COMPARE_COLUMNS,
          sorted(old.values(),key=lambda x:(x["bucket_utc"],x["symbol"])))
    return True
