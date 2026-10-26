#!/usr/bin/env python3
"""CRYPTO V1.1: 24/7 Alpaca crypto recorder + distinct PAPER-safe signal path.

STRICT volume-confirmed signals alone may reach the PAPER reconciler.
PRICE_ONLY experimental signals are always virtual, never executable.
Records 14-day rolling five-minute OHLCV including zero-volume bars; records
half-hour quality diagnostics and separate research-only virtual PnL.
"""
import os
import requests
from datetime import datetime, timedelta, timezone
from market_virtual_core import VirtualBook, find_signal
import crypto_observer_v1 as observer
import crypto_crossfeed_v1 as crossfeed

SYMBOLS=("BTC/USD","ETH/USD","SOL/USD")
DATA="https://data.alpaca.markets/v1beta3/crypto/us/bars"
LOOKBACK=12
STOP_PERCENT=1.2
RISK_USD=3.0
VIRTUAL_PREFIX="crypto_virtual_v1"
RESEARCH_PREFIX="crypto_research_v1"
TREND_PREFIX="crypto_trend_v1"
KRAKEN_DATA="https://data.alpaca.markets/v1beta3/crypto/us-1/bars"

def now_utc(): return datetime.now(timezone.utc)

def headers():
    k=os.getenv("ALPACA_API_KEY","").strip()
    s=os.getenv("ALPACA_SECRET_KEY","").strip()
    if not k or not s:
        raise RuntimeError("Missing ALPACA_API_KEY / ALPACA_SECRET_KEY; CRYPTO scan unavailable")
    return {"APCA-API-KEY-ID":k,"APCA-API-SECRET-KEY":s}

def fetch_bars(symbol, now, loc="us"):
    start=now-timedelta(hours=5)
    if loc not in ("us","us-1"):
        raise ValueError("Unsupported crypto data location")
    url=DATA if loc=="us" else KRAKEN_DATA
    r=requests.get(url,params={"symbols":symbol,"timeframe":"5Min",
                    "start":start.isoformat(),"end":now.isoformat(),
                    "limit":300,"sort":"asc"},headers=headers(),timeout=20)
    r.raise_for_status()
    data=r.json()
    if data.get("next_page_token"):
        raise RuntimeError(f"{symbol} {loc}: incomplete paginated crypto history")
    bars=data.get("bars",{}).get(symbol,[])
    if not isinstance(bars,list):
        raise RuntimeError(f"{symbol} {loc}: invalid crypto bars")
    return bars

def scan_once(now=None):
    now=now or now_utc()
    # Original volume-confirmed strategy unchanged; no trade on bad volumes.
    book=VirtualBook(VIRTUAL_PREFIX,risk=RISK_USD,stop_pct=STOP_PERCENT,
                     reward=2,fee_pct=0.25,slippage_pct=0.08,
                     notional_cap=250,fractional=True,currency="USD",
                     max_day_entries=6,max_open=2)
    # Experimental OHLC-only virtual book: separate names, separate fills,
    # never submitted to Alpaca PAPER broker module.
    research=VirtualBook(RESEARCH_PREFIX,risk=1.5,stop_pct=1.2,
                         reward=2,fee_pct=0.25,slippage_pct=0.08,
                         notional_cap=150,fractional=True,currency="USD",
                         max_day_entries=6,max_open=2)
    trend=VirtualBook(TREND_PREFIX,risk=1.5,stop_pct=1.2,
                      reward=2,fee_pct=0.25,slippage_pct=0.08,
                      notional_cap=150,fractional=True,currency="USD",
                      max_day_entries=6,max_open=2)
    summary={"pairs":0,"strict_signals":0,"research_signals":0,
             "trend_signals":0,"new_virtual":0,"new_research":0,
             "new_trend":0,"closed":0,"closed_research":0,"closed_trend":0,
             "new_candles":0,"new_kraken_candles":0,
             "kraken_failures":0,"errors":0}
    paper_candidates=[]
    day=now.date().isoformat()
    for symbol in SYMBOLS:
        try:
            bars=fetch_bars(symbol,now)
            if not bars:
                raise RuntimeError(f"{symbol}: no bars returned by Alpaca")
            summary["pairs"]+=1
            # Every usable completed candle goes into our rolling journal,
            # whether or not there was a tradable signal. Backfills API gaps.
            summary["new_candles"]+=observer.record_bars(symbol,bars,now)
            for trial,name,key in ((book,"STRICT","closed"),
                                   (research,"PRICE_ONLY","closed_research"),
                                   (trend,"KRAKEN_TREND","closed_trend")):
                exited=trial.progress(symbol,bars,now)
                if exited:
                    summary[key]+=1
                    print(f"CRYPTO {name} VIRTUAL EXIT {symbol}: "
                          f"{exited['pnl']:+.2f} USD, {exited['reason']}",flush=True)
            kraken=[]
            secondary_error=None
            try:
                kraken=fetch_bars(symbol,now,loc="us-1")
                if not kraken:
                    raise RuntimeError("empty Kraken US history")
                summary["new_kraken_candles"]+=observer.record_bars(
                    symbol,kraken,now,path=crossfeed.KRAKEN_CANDLES)
            except (requests.RequestException,RuntimeError,ValueError,KeyError,OSError) as exc:
                secondary_error=str(exc)
                summary["kraken_failures"]+=1
                print(f"CRYPTO KRAKEN DATA {symbol}: {exc}; "
                      "crossfeed research skipped (STRICT remains unchanged)",flush=True)
            report_cross,trial_trend=crossfeed.evaluate(symbol,bars,kraken,now)
            if secondary_error:
                report_cross["secondary_status"]="UNAVAILABLE"
                report_cross["trend_reason"]="KRAKEN_FETCH_FAILED"
            crossfeed.record_comparison(report_cross,now)
            print(f"CRYPTO CROSSFEED {symbol}: "
                  f"status={report_cross['secondary_status']} "
                  f"aligned={report_cross['aligned_bars_1h']} "
                  f"US volume={report_cross['us_nonzero_12']}/12 "
                  f"Kraken volume={report_cross['kraken_nonzero_12']}/12 "
                  f"trend={report_cross['trend_reason']}",flush=True)
            if trial_trend:
                if trend.signal(trial_trend,day):
                    summary["trend_signals"]+=1
                    print(f"CRYPTO KRAKEN TREND RESEARCH SIGNAL {symbol}: "
                          f"{trial_trend['breakout_pct']:.3f}% "
                          f"RV={trial_trend['relvol']:.2f} (VIRTUAL ONLY)",flush=True)
                if trend.maybe_enter(trial_trend,day):
                    summary["new_trend"]+=1
            strict=find_signal(symbol,bars,now,lookback=LOOKBACK,
                               min_breakout=.15,min_relvol=1.5,
                               max_age_minutes=8,allow_short=False)
            trial=observer.price_only_signal(symbol,bars,now)
            report=observer.quality(symbol,bars,now,strict)
            observer.record_quality(report,now)
            print(f"CRYPTO QUALITY {symbol}: reference_nonzero="
                  f"{report['nonzero_previous12']}/12 "
                  f"last_volume={report['latest_volume']} "
                  f"strict={report['strict_reason']} "
                  f"price_only={bool(trial)}",flush=True)
            if strict:
                # Unchanged STRICT behavior; no experimental candidate ever
                # flows into paper.run(...).
                paper_candidates.append(strict)
                if book.signal(strict,day):
                    summary["strict_signals"]+=1
                    print(f"CRYPTO STRICT SIGNAL {symbol}: "
                          f"{strict['breakout_pct']:.3f}% RV {strict['relvol']:.2f}",flush=True)
                if book.maybe_enter(strict,day):
                    summary["new_virtual"]+=1
            if trial:
                if research.signal(trial,day):
                    summary["research_signals"]+=1
                    print(f"CRYPTO RESEARCH PRICE ONLY {symbol}: "
                          f"{trial['breakout_pct']:.3f}% (NO BROKER ORDER)",flush=True)
                if research.maybe_enter(trial,day):
                    summary["new_research"]+=1
        except (requests.RequestException,RuntimeError,ValueError,KeyError,OSError) as exc:
            summary["errors"]+=1
            print(f"CRYPTO DATA ERROR {symbol}: {exc} "
                  "(NO PAPER SIGNAL / NO SYNTHETIC FILL)",flush=True)
    if summary["pairs"]==0:
        raise RuntimeError("No usable Alpaca crypto data; data-scanner failed closed")
    # Even with zero signals, make the state files visible/persistent.
    book.persist()
    research.persist()
    trend.persist()
    print(f"CRYPTO V1.2 {now.isoformat()} {summary} "
          "(STRICT PAPER, PRICE-ONLY and KRAKEN-TREND virtual)",flush=True)
    return summary,paper_candidates

if __name__=="__main__":
    import crypto_paper_v1 as paper
    try:
        paper.run(allow_new=False)
    except Exception as exc:
        paper.alert(f"🚨 Broker reconciliation failed: {exc}")
        raise
    stats, candidates = scan_once()
    enabled=os.getenv("ENABLE_CRYPTO_PAPER_TRADING","false").lower()=="true"
    if enabled:
        print(f"CRYPTO PAPER enabled: STRICT volume-confirmed candidates="
              f"{len(candidates)}; RESEARCH never enters broker",flush=True)
        try:
            paper.run(candidates,allow_new=True)
        except Exception as exc:
            paper.alert(f"🚨 PAPER protection/entry failure: {exc}. Check Alpaca!")
            raise
    else:
        print("CRYPTO PAPER disabled for this run; virtual and research only",flush=True)
