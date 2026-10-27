#!/usr/bin/env python3
"""Xetra Germany 5m breakout, 100% virtual, EUR, via Yahoo chart data.

Data are unofficial/possibly delayed and may rate-limit or disappear.
No broker integration for Xetra on the user's Alpaca PAPER US account;
do not present virtual fills as executed Xetra orders.
"""
import math
import csv
import requests
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
from market_virtual_core import VirtualBook, find_signal, full_bars
from equity_candle_guard import verify_contiguous

BERLIN=ZoneInfo("Europe/Berlin")
# Yahoo .DE instruments are labelled XETRA / GER; validate metadata.
SYMBOLS=("SAP.DE","SIE.DE","BMW.DE","ALV.DE","BAS.DE","DTE.DE")
START=time(9,0)
CLOSE=time(17,30)
PREFIX="xetra_virtual_v1"
QUALITY_FILE=Path("xetra_quality_v1.csv")
QUALITY_COLS=("bucket_utc","symbol","latest_bar_utc","total_bars","grid_offset_minutes","integrity","signal_status")
HEADERS={"User-Agent":"Mozilla/5.0 (compatible; VirtualResearchScanner/1.0)",
         "Accept":"application/json"}

def now_utc(): return datetime.now(timezone.utc)

def is_trading_day(now):
    local=now.astimezone(BERLIN)
    return local.weekday()<5 and START<=local.time()<time(17,45)

def xetra_session(now):
    local=now.astimezone(BERLIN)
    return (datetime.combine(local.date(),START,BERLIN).astimezone(timezone.utc),
            datetime.combine(local.date(),CLOSE,BERLIN).astimezone(timezone.utc))

def yahoo_bars(symbol, now):
    r=requests.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}",
                   params={"interval":"5m","range":"2d","includePrePost":"false"},
                   headers=HEADERS,timeout=20)
    r.raise_for_status()
    payload=r.json()["chart"]
    if payload.get("error") or not payload.get("result"):
        raise RuntimeError(f"{symbol}: Yahoo chart missing: {payload.get('error')}")
    result=payload["result"][0]
    meta=result.get("meta") or {}
    if meta.get("currency")!="EUR":
        raise RuntimeError(f"{symbol}: expected EUR, got {meta.get('currency')}")
    exchange=str(meta.get("exchangeName") or "").upper()
    # Yahoo sometimes uses GER and sometimes XETRA for .DE
    if exchange not in ("GER","XETRA"):
        raise RuntimeError(f"{symbol}: exchange '{exchange}' not verified Xetra")
    timestamps=result.get("timestamp") or []
    quotes=(result["indicators"]["quote"] or [{}])[0]
    opens=quotes.get("open") or []
    highs=quotes.get("high") or []
    lows=quotes.get("low") or []
    closes=quotes.get("close") or []
    volumes=quotes.get("volume") or []
    bars=[]
    first,last=xetra_session(now)
    for i,epoch in enumerate(timestamps):
        if i>=min(map(len,(opens,highs,lows,closes,volumes))):
            break
        t=datetime.fromtimestamp(epoch,timezone.utc)
        if not first<=t<last:
            continue
        v=(opens[i],highs[i],lows[i],closes[i],volumes[i])
        if any(x is None or not isinstance(x,(int,float)) or not math.isfinite(float(x)) for x in v):
            continue
        bars.append({"t":t.isoformat(),"o":opens[i],"h":highs[i],"l":lows[i],
                     "c":closes[i],"v":volumes[i]})
    return bars

def log_quality(symbol,bars,now,integrity,signal_status):
    """Persist one 30min data-quality sample per symbol, including no-signal scans."""
    bucket=now.replace(minute=(now.minute//30)*30,second=0,microsecond=0)
    cutoff=now-timedelta(days=14)
    candles=full_bars(bars,now,5)
    stamp=candles[-1]["t"].isoformat() if candles else ""
    existing={}
    if QUALITY_FILE.exists():
        with QUALITY_FILE.open(newline="",encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                try:
                    if datetime.fromisoformat(row["bucket_utc"])>=cutoff:
                        existing[(row["bucket_utc"],row["symbol"])]=row
                except (ValueError,TypeError,KeyError):
                    continue
    key=(bucket.isoformat(),symbol)
    if key in existing:
        return False
    existing[key]={"bucket_utc":bucket.isoformat(),"symbol":symbol,
                   "latest_bar_utc":stamp,"total_bars":len(candles),
                   "grid_offset_minutes":candles[-1]["t"].minute%5 if candles else "",
                   "integrity":integrity,"signal_status":signal_status}
    tmp=QUALITY_FILE.with_suffix(".csv.tmp")
    with tmp.open("w",newline="",encoding="utf-8") as handle:
        writer=csv.DictWriter(handle,fieldnames=QUALITY_COLS)
        writer.writeheader()
        writer.writerows(sorted(existing.values(),key=lambda x:(x["bucket_utc"],x["symbol"])))
    tmp.replace(QUALITY_FILE)
    return True

def scan_once(now=None):
    now=now or now_utc()
    if not is_trading_day(now):
        print(f"XETRA market closed {now.astimezone(BERLIN).isoformat()}; no new entries",flush=True)
        return {"closed":True}
    session_open,session_close=xetra_session(now)
    book=VirtualBook(PREFIX,risk=3.0,stop_pct=1.0,reward=2.0,
                     fee_pct=0.05,slippage_pct=0.05,notional_cap=600,
                     fractional=False,currency="EUR",max_day_entries=6,max_open=2)
    day=now.astimezone(BERLIN).date().isoformat()
    stats={"symbols":0,"new_signals":0,"entries":0,"exits":0,"errors":0,
           "invalid_reference":0,"quality_logged":0,"offset_accepted":0}
    for symbol in SYMBOLS:
        try:
            bars=yahoo_bars(symbol,now)
            stats["symbols"]+=1
            result=book.progress(symbol,bars,now,session_end=session_close if now>=session_close else None)
            if result:
                stats["exits"]+=1
                print(f"XETRA VIRTUAL EXIT {symbol} {result['pnl']:+.2f} EUR {result['reason']}",flush=True)
            if now>=session_close-timedelta(minutes=30):
                continue
            # Yahoo may publish delayed/partial Xetra data. Fail-closed for
            # NEW simulated signals if the 12-bar reference has a time gap.
            completed=full_bars(bars,now,5)
            good,why=verify_contiguous(completed,key="t",count=13,
                                        session_open=session_open,
                                        allow_fixed_grid_offset=True)
            if good and why.startswith("OK_fixed_offset_"):
                print(f"XETRA {symbol}: validated Yahoo fixed-grid offset {why}; original timestamps retained",flush=True)
            if not good:
                stats["invalid_reference"]+=1
                stats["quality_logged"]+=int(log_quality(symbol,bars,now,why,"ENTRY_BLOCKED"))
                print(f"XETRA {symbol}: 5Min DATA QUALITY {why}; no new signal",flush=True)
                continue
            if why.startswith("OK_fixed_offset_"):
                stats["offset_accepted"]+=1
            stats["quality_logged"]+=int(log_quality(symbol,bars,now,why,"REFERENCE_VALID"))
            signal=find_signal(symbol,bars,now,lookback=12,
                               min_breakout=.15,min_relvol=1.5,
                               max_age_minutes=30,allow_short=False)
            if signal:
                if book.signal(signal,day):
                    stats["new_signals"]+=1
                    print(f"XETRA VIRTUAL SIGNAL {symbol} {signal['breakout_pct']:.3f}% "
                          f"relvol {signal['relvol']:.2f}x, age {signal['age_minutes']}m",flush=True)
                if book.maybe_enter(signal,day):
                    stats["entries"]+=1
        except (requests.RequestException,ValueError,KeyError,RuntimeError,TypeError) as exc:
            stats["errors"]+=1
            print(f"XETRA DATA WARNING {symbol}: {exc} (virtual, no invented fill)",flush=True)
    if stats["symbols"]==0:
        raise RuntimeError("XETRA: no usable stock history; check Yahoo data access/terms")
    # A successful no-signal run still persists a restart-safe virtual state.
    book.persist()
    print(f"XETRA V1 {now.astimezone(BERLIN).isoformat()} {stats}. "
          "UNOFFICIAL possibly DELAYED Yahoo IEX? No: Yahoo Xetra research feed; "
          "100% VIRTUAL, NOT Alpaca PAPER fills",flush=True)
    return stats

if __name__=="__main__":
    scan_once()
