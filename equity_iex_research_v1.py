#!/usr/bin/env python3
"""Retrospective NON-TRADING 30m equity breakout/filter comparison on stored IEX.

Signals use only candles at or BEFORE each signal timestamp. A sample is
published only once the next-entry candle and 30min forward candle are real
and fully closed in the IEX observation journal. Hypothetical mark-to-market
return after assumed 0.05% slippage EACH side, NOT broker PnL or a fill model.
Quote spread, market impact, short borrow and actual execution are excluded.
This code never fetches broker data or transmits orders.
"""
import csv
import math
from collections import defaultdict
from datetime import datetime,timedelta,timezone
from pathlib import Path

from scanner_equity_observer_v1 import BARS_PATH, FAST_SYMBOLS, BREAKOUT_SYMBOLS

RESULTS_PATH=Path("equity_iex_research_v1.csv")
COLS=("symbol","model","signal_bar_utc","side","breakout_percent",
      "relative_volume","trend_ema9_21","strong_candle_close",
      "entry_bar_utc","exit_bar_utc","entry_model_price","exit_model_price",
      "net_return_percent","net_pnl_per_share_usd","status","data_feed")
SLIP=0.0005
HORIZON=6  # 30m after NEXT observed entry candle open: i+1 -> i+7
FAST_SET=set(FAST_SYMBOLS)
CLASSIC_SET=set(BREAKOUT_SYMBOLS)


def ts(raw):
    return datetime.fromisoformat(str(raw).replace("Z","+00:00")).astimezone(timezone.utc)


def load_candles(path=BARS_PATH):
    if not path.exists():
        return {}
    out=defaultdict(list)
    with path.open(newline="",encoding="utf-8") as f:
        for row in csv.DictReader(f):
            try:
                if row.get("feed")!="ALPACA_IEX_ONLY":
                    continue
                t=ts(row["bar_utc"])
                vals={k:float(row[k]) for k in ("open","high","low","close","volume")}
                v=vals["volume"]
                if not all(math.isfinite(z) and z>0 for z in (
                        vals["open"],vals["high"],vals["low"],vals["close"])) or v<0:
                    continue
                out[(row["symbol"],t.date())].append({"t":t,**vals,
                                   "trades":float(row.get("trades") or 0)})
            except (ValueError,KeyError,TypeError):
                continue
    return {key:sorted(rows,key=lambda r:r["t"]) for key,rows in out.items()}


def ema(values,n):
    alpha=2/(n+1)
    result=values[0]
    for value in values[1:]:
        result=alpha*value+(1-alpha)*result
    return result


def valid_window(bars,start,end):
    sample=bars[start:end+1]
    return all(b["t"]-a["t"]==timedelta(minutes=5)
               for a,b in zip(sample,sample[1:]))


def candidates(symbol,bars):
    model="FAST_IEX_PROXY" if symbol in FAST_SET else "CLASSIC_IEX_SHADOW"
    lookback=12 if symbol in FAST_SET else 20
    min_breakout=0.15 if symbol in FAST_SET else 0.10
    result=[]
    # 21 completed previous candles for EMA21; 7 later candles for evaluation.
    for i in range(max(21,lookback),len(bars)-HORIZON-1):
        b=bars[i]
        start=i-max(21,lookback)
        exit_i=i+HORIZON+1
        if not valid_window(bars,start,exit_i):
            continue
        price=b["close"]
        if symbol in FAST_SET and not 5<=price<=50:
            continue
        before=bars[i-lookback:i]
        positive=[x["volume"] for x in before[-12:] if x["volume"]>0]
        if len(positive)<10 or b["volume"]<=0:
            continue
        ref=sum(positive)/len(positive)
        rv=b["volume"]/ref
        if rv<1.5:
            continue
        if symbol in FAST_SET and (
             b["volume"]<1500 or b["trades"]<15 or b["volume"]*price<15000):
            continue
        ceiling=max(x["high"] for x in before)
        floor=min(x["low"] for x in before)
        if price>ceiling:
            side="LONG"
            br=(price/ceiling-1)*100
        elif price<floor:
            side="SHORT"
            br=(1-price/floor)*100
        else:
            continue
        if br<min_breakout:
            continue
        hist=[x["close"] for x in bars[:i+1]]
        ema9=ema(hist,9)
        ema21=ema(hist,21)
        trend=(price>ema9>ema21) if side=="LONG" else (price<ema9<ema21)
        width=b["high"]-b["low"]
        location=(price-b["low"])/width if width>0 else .5
        body_good=(location>=.7 if side=="LONG" else location<=.3)
        entry=bars[i+1]
        exit_bar=bars[exit_i]
        slip=SLIP
        in_px=entry["open"]*(1+slip if side=="LONG" else 1-slip)
        out_px=exit_bar["close"]*(1-slip if side=="LONG" else 1+slip)
        net=out_px-in_px if side=="LONG" else in_px-out_px
        result.append({"symbol":symbol,"model":model,
           "signal_bar_utc":b["t"].isoformat(),"side":side,
           "breakout_percent":round(br,4),"relative_volume":round(rv,4),
           "trend_ema9_21":str(trend),"strong_candle_close":str(body_good),
           "entry_bar_utc":entry["t"].isoformat(),
           "exit_bar_utc":exit_bar["t"].isoformat(),
           "entry_model_price":round(in_px,6),"exit_model_price":round(out_px,6),
           "net_return_percent":round(net/in_px*100,4),
           "net_pnl_per_share_usd":round(net,5),
           "status":"VIRTUAL_30MIN_FIXED_HORIZON",
           "data_feed":"ALPACA_IEX_ONLY"})
    return result


def write_results(out, path=RESULTS_PATH):
    tmp=path.with_suffix(path.suffix+".tmp")
    with tmp.open("w",newline="",encoding="utf-8") as f:
        w=csv.DictWriter(f,fieldnames=COLS)
        w.writeheader()
        w.writerows(out)
    tmp.replace(path)


def run():
    groups=load_candles()
    out=[]
    for (sym,day),bars in sorted(groups.items()):
        if sym not in FAST_SET|CLASSIC_SET:
            continue
        out.extend(candidates(sym,bars))
    out.sort(key=lambda x:(x["signal_bar_utc"],x["symbol"]))
    # Never replace valid archived results with an empty file on a provider
    # outage. The original OHLCV journal is the source of truth.
    if out:
        write_results(out)
    baseline=sum(x["net_return_percent"] for x in out)/len(out) if out else 0
    filtered=[x for x in out if x["trend_ema9_21"]=="True" and x["strong_candle_close"]=="True"]
    avg=sum(x["net_return_percent"] for x in filtered)/len(filtered) if filtered else 0
    print(f"IEX RESEARCH (NO ORDERS): completed30m={len(out)} "
          f"baseline_avg_return_pct={baseline:.3f} "
          f"ema21_and_body_candidates={len(filtered)} avg_pct={avg:.3f}; "
          "hypothetical and not an Alpaca broker fill",flush=True)
    return len(out)


if __name__=="__main__":
    run()
