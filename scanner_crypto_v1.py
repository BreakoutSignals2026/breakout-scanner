#!/usr/bin/env python3
"""Crypto Breakout V1.0 — 24/7 signal scanner and virtual trade engine.

For Alpaca crypto bars (USD pairs) only. This entrypoint never submits an
Alpaca order. A separate CRYPTO PAPER order module may be enabled only after
a dedicated protective stop-limit and recovery check passes. No LIVE hosts.
"""
import os
import requests
from datetime import datetime, timedelta, timezone
from market_virtual_core import VirtualBook, find_signal

SYMBOLS=("BTC/USD","ETH/USD","SOL/USD")
DATA="https://data.alpaca.markets/v1beta3/crypto/us/bars"
LOOKBACK=12
STOP_PERCENT=1.2
RISK_USD=3.0
VIRTUAL_PREFIX="crypto_virtual_v1"

def now_utc(): return datetime.now(timezone.utc)

def headers():
    k=os.getenv("ALPACA_API_KEY","").strip()
    s=os.getenv("ALPACA_SECRET_KEY","").strip()
    if not k or not s:
        raise RuntimeError("Missing ALPACA_API_KEY / ALPACA_SECRET_KEY; CRYPTO scan unavailable")
    return {"APCA-API-KEY-ID":k,"APCA-API-SECRET-KEY":s}

def fetch_bars(symbol, now):
    # Explicit small window prevents pagination truncating the most recent
    # (and therefore actionable) closed candle.
    start=now-timedelta(hours=5)
    r=requests.get(DATA,params={"symbols":symbol,"timeframe":"5Min",
                    "start":start.isoformat(),"end":now.isoformat(),
                    "limit":300,"sort":"asc"},headers=headers(),timeout=20)
    r.raise_for_status()
    data=r.json()
    if data.get("next_page_token"):
        raise RuntimeError(f"{symbol}: incomplete paginated crypto history")
    bars=data.get("bars",{}).get(symbol,[])
    if not isinstance(bars,list):
        raise RuntimeError(f"{symbol}: invalid Alpaca crypto bars")
    return bars

def scan_once(now=None):
    now=now or now_utc()
    book=VirtualBook(VIRTUAL_PREFIX,risk=RISK_USD,stop_pct=STOP_PERCENT,
                     reward=2,fee_pct=0.25,slippage_pct=0.08,
                     notional_cap=250,fractional=True,currency="USD",
                     max_day_entries=6,max_open=2)
    summary={"pairs":0,"signals":0,"new_virtual":0,"closed":0,"errors":0}
    signals=[]
    # Store UTC day rather than NY exchange day; crypto operates every day.
    day=now.date().isoformat()
    for symbol in SYMBOLS:
        try:
            bars=fetch_bars(symbol,now)
            summary["pairs"]+=1
            closed=book.progress(symbol,bars,now)
            if closed:
                summary["closed"]+=1
                print(f"CRYPTO VIRTUAL EXIT {symbol} {closed['pnl']:+.2f} USD {closed['reason']}",flush=True)
            signal=find_signal(symbol,bars,now,lookback=LOOKBACK,
                               min_breakout=.15,min_relvol=1.5,
                               max_age_minutes=8,allow_short=False)
            if signal:
                signals.append(signal)
                if book.signal(signal,day):
                    summary["signals"]+=1
                    print(f"CRYPTO SIGNAL {symbol}: breakout {signal['breakout_pct']:.3f}% "
                          f"relvol {signal['relvol']:.2f}x; VIRTUAL only",flush=True)
                if book.maybe_enter(signal,day):
                    summary["new_virtual"]+=1
        except (requests.RequestException,RuntimeError,ValueError,KeyError) as exc:
            summary["errors"]+=1
            print(f"CRYPTO DATA ERROR {symbol}: {exc} (NO ORDER / NO SYNTHETIC FILL)",flush=True)
    if summary["pairs"]==0:
        raise RuntimeError("No usable Alpaca crypto data; data-scanner failed closed")
    print(f"CRYPTO V1 {now.isoformat()} {summary} "
          "(VIRTUAL, no Alpaca orders; fees/slippage estimated)",flush=True)
    return summary, signals

if __name__=="__main__":
    scan_once()
