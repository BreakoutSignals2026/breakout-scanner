#!/usr/bin/env python3
"""Isolated Alpaca CRYPTO PAPER execution. Never uses a live API URL.

US crypto spot is long-only; protection is server-side SELL STOP-LIMIT/GTC,
NOT an equities trailing stop nor guaranteed fill. A gap below the limit may
leave the position open. No bracket/OCO, no borrowing, no manual positions.
State and broker order IDs are reconciled before allowing new entries.
"""
import json
import math
import os
import time
from decimal import Decimal, ROUND_DOWN
from datetime import datetime, timezone, timedelta
from pathlib import Path
import requests

API="https://paper-api.alpaca.markets/v2"
DATA="https://data.alpaca.markets/v1beta3/crypto/us"
STATE=Path("crypto_paper_state_v1.json")
EVENTS=Path("crypto_paper_events_v1.jsonl")
SYMBOLS=("BTC/USD","ETH/USD","SOL/USD")
ENTRY_PREFIX="CRPT1E_"
STOP_PREFIX="CRPT1S_"
TAKE_PREFIX="CRPT1T_"
EST_RISK_USD=3.0
MAX_NOTIONAL=220.0
DAILY_ENTRIES=3
MAX_OPEN_POSITIONS=1
DAILY_LOSS_STOP=12.0
STOP_PCT=1.2
STOP_LIMIT_SLIPPAGE_PCT=0.8
TARGET_PCT=2.4
MAX_SPREAD_PCT=0.4
MAX_QUOTE_AGE_SEC=120
OPEN={"new","accepted","pending_new","partially_filled","pending_cancel","pending_replace","accepted_for_bidding"}

def utcnow(): return datetime.now(timezone.utc)
def normalize(s): return str(s).replace("/","").upper()
def dec(x): return Decimal(str(x))
def parse_time(x):
    return datetime.fromisoformat(str(x).replace("Z","+00:00")).astimezone(timezone.utc)

def headers():
    k=os.getenv("ALPACA_API_KEY","").strip()
    s=os.getenv("ALPACA_SECRET_KEY","").strip()
    if not k or not s: raise RuntimeError("Missing Alpaca PAPER credentials")
    return {"APCA-API-KEY-ID":k,"APCA-API-SECRET-KEY":s,"Content-Type":"application/json"}

def api(method,path,*,params=None,body=None,allow404=False):
    if not API.startswith("https://paper-api.alpaca.markets/"):
        raise RuntimeError("LIVE trading endpoint forbidden")
    res=requests.request(method,API+path,headers=headers(),params=params,
                         json=body,timeout=15)
    if allow404 and res.status_code==404: return None
    if res.status_code>=400:
        raise RuntimeError(f"Alpaca PAPER {method} {path} HTTP {res.status_code}: {res.text[:200]}")
    return res.json() if res.content else {}

def bycid(cid):
    return api("GET","/orders:by_client_order_id",params={"client_order_id":cid},allow404=True)

def alert(text):
    print("CRYPTO PAPER "+text,flush=True)
    token=os.getenv("TELEGRAM_TOKEN","")
    chat=os.getenv("CHAT_ID","")
    if token and chat:
        try:
            r=requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                            json={"chat_id":chat,"text":"🪙 CRYPTO PAPER: "+text},timeout=10)
            r.raise_for_status()
        except requests.RequestException as exc:
            print(f"Telegram notification error: {exc}",flush=True)

def load():
    if not STATE.exists():
        return {"trades":{}}
    d=json.loads(STATE.read_text(encoding="utf-8"))
    if not isinstance(d,dict) or not isinstance(d.get("trades"),dict):
        raise RuntimeError("Invalid crypto PAPER state — no new entries")
    return d

def save(state):
    temp=STATE.with_suffix(".tmp")
    temp.write_text(json.dumps(state,indent=2,sort_keys=True)+"\n",encoding="utf-8")
    temp.replace(STATE)

def log(**e):
    with EVENTS.open("a",encoding="utf-8") as f:
        f.write(json.dumps({"at":utcnow().isoformat(),**e})+"\n")

def positions():
    data=api("GET","/positions")
    return {normalize(p.get("symbol")):p for p in data
            if normalize(p.get("symbol")) in {normalize(s) for s in SYMBOLS}}

def orders(status="open",after=None):
    params={"status":status,"limit":500,"nested":"false"}
    if after: params["after"]=after
    rows=api("GET","/orders",params=params)
    if not isinstance(rows,list) or len(rows)>=500:
        raise RuntimeError("Incomplete Alpaca broker orders page; stop new entries")
    return rows

def quote(symbol):
    r=requests.get(DATA+"/latest/quotes",params={"symbols":symbol},
                   headers=headers(),timeout=12)
    r.raise_for_status()
    q=(r.json().get("quotes") or {}).get(symbol)
    if not q: raise RuntimeError(f"{symbol}: crypto quote missing")
    bid,ask=float(q.get("bp",0)),float(q.get("ap",0))
    if not bid>0 or not ask>=bid: raise RuntimeError("Invalid crypto bid/ask")
    age=(utcnow()-parse_time(q["t"])).total_seconds()
    spread=(ask-bid)/((ask+bid)/2)*100
    if not -2<=age<=MAX_QUOTE_AGE_SEC or spread>MAX_SPREAD_PCT:
        raise RuntimeError(f"{symbol}: old/wide crypto quote age={age:.1f}s spread={spread:.3f}%")
    return {"bid":bid,"ask":ask,"spread":spread}

def asset(symbol):
    a=api("GET","/assets/"+normalize(symbol))
    if a.get("class")!="crypto" or not a.get("tradable") or a.get("status")!="active":
        raise RuntimeError(f"{symbol} not eligible crypto asset")
    return a

def round_step(x,step):
    step=dec(step)
    if step<=0: raise RuntimeError("Invalid exchange quantity/price increment")
    return (dec(x)/step).to_integral_value(rounding=ROUND_DOWN)*step

def size_for_price(ask,a):
    if ask<=0: raise ValueError("Invalid ask")
    # Includes 0.25% entry + exit estimated taker fees and 0.3% extra execution cost.
    cost=(STOP_PCT+0.50+0.30)/100
    budget=min(MAX_NOTIONAL,EST_RISK_USD/cost)
    step=a.get("min_trade_increment") or "0.000000001"
    qty=round_step(dec(str(budget))/dec(str(ask)),step)
    minimum=dec(a.get("min_order_size") or "0")
    if qty<minimum or qty<=0 or float(qty)*ask<10:
        raise RuntimeError("Crypto quantity below min order size")
    return qty

def history_today(now):
    day=now.date().isoformat()
    start=datetime.combine(now.date(),datetime.min.time(),timezone.utc)
    records=orders("all",start.isoformat())
    own=[o for o in records if str(o.get("client_order_id") or "").startswith(ENTRY_PREFIX)
         and parse_time(o.get("submitted_at") or o.get("created_at")).date().isoformat()==day]
    return day,own

def stop_payload(trade, pos, entry, a):
    qty=dec(pos["qty"])
    ep=dec(entry["filled_avg_price"])
    if qty<=0 or ep<=0:
        raise RuntimeError("Unknown filled qty / entry price")
    if qty>dec(entry["filled_qty"]):
        raise RuntimeError("Position qty exceeds verified own filled entry")
    # Adjust to available precision; NEVER oversell other positions.
    qty=round_step(qty,a.get("min_trade_increment") or "0.000000001")
    if qty<=0: raise RuntimeError("Quantity rounds to zero")
    incr=a.get("price_increment") or "0.01"
    stop=round_step(ep*(1-dec(STOP_PCT)/100),incr)
    limit=round_step(stop*(1-dec(STOP_LIMIT_SLIPPAGE_PCT)/100),incr)
    if not 0<limit<stop<ep: raise RuntimeError("Invalid stop-limit price ladder")
    return {"symbol":trade["symbol"],"side":"sell","type":"stop_limit",
            "time_in_force":"gtc","qty":format(qty,"f"),
            "stop_price":format(stop,"f"),"limit_price":format(limit,"f"),
            "client_order_id":trade["stop_cid"]}

def recover_own_missing(state,positions_now,recent):
    # Recover ONLY uniquely identifiable CRPT1 entry fills. Never adopt
    # manual holdings or old FAST positions.
    for symbol,position in positions_now.items():
        if symbol in state["trades"] and state["trades"][symbol].get("status")!="closed":
            continue
        matched=[o for o in recent if normalize(o.get("symbol"))==symbol and
                 str(o.get("client_order_id") or "").startswith(ENTRY_PREFIX) and
                 dec(o.get("filled_qty") or "0")>=dec(position["qty"])]
        if len(matched)!=1: continue
        original=matched[0]
        suffix=original["client_order_id"][len(ENTRY_PREFIX):]
        state["trades"][symbol]={"symbol":original["symbol"],
                                "entry_cid":original["client_order_id"],
                                "stop_cid":STOP_PREFIX+suffix,
                                "take_cid":TAKE_PREFIX+suffix,
                                "status":"entry_pending",
                                "created_at":original.get("created_at")}
        save(state)
        alert(f"Recovered own PAPER crypto entry {symbol}; verifying server stop.")

def reconcile(state):
    """Protect verified existing CRYPTO positions before considering new buys."""
    current=positions()
    open_orders=orders()
    recent=orders("all")
    recover_own_missing(state,current,recent)
    safe=True
    for symbol,trade in list(state["trades"].items()):
        if trade.get("status")=="closed": continue
        entry=bycid(trade["entry_cid"])
        if not entry:
            alert(f"{symbol}: entry order missing; no new trades.")
            safe=False
            continue
        if entry.get("status")!="filled":
            if entry.get("status") in OPEN:
                alert(f"{symbol}: entry pending/partial, not yet fully protected.")
                safe=False
                continue
            if dec(entry.get("filled_qty") or 0)==0:
                trade["status"]="closed"
                save(state)
                continue
            alert(f"{symbol}: partial entry status {entry.get('status')}; manual check.")
            safe=False
            continue
        pos=current.get(symbol)
        protective=bycid(trade["stop_cid"])
        target=bycid(trade["take_cid"])
        if target:
            if target.get("status")=="filled" and not pos:
                finish_trade(state,trade,entry,target,"TAKE_PROFIT")
                continue
            if target.get("status") in OPEN:
                safe=False
                continue
            if pos:
                alert(f"{symbol}: profit exit status {target.get('status')}, still position.")
                safe=False
                continue
        if not pos:
            if protective and protective.get("status")=="filled":
                finish_trade(state,trade,entry,protective,"STOP_LIMIT")
            else:
                alert(f"{symbol}: position missing but no confirmed crypto exit.")
                safe=False
            continue
        if dec(pos["qty"])>dec(entry.get("filled_qty") or 0):
            alert(f"{symbol}: position qty larger than owned entry: no modifications.")
            safe=False
            continue
        this_orders=[x for x in open_orders if normalize(x.get("symbol"))==symbol]
        foreign=[x for x in this_orders if x.get("client_order_id") not in
                 {trade["entry_cid"],trade["stop_cid"],trade["take_cid"]}]
        if foreign:
            alert(f"{symbol}: foreign open orders; no automatic interference.")
            safe=False
            continue
        if protective is None:
            if this_orders:
                alert(f"{symbol}: other order exists; do not place duplicate stop")
                safe=False
                continue
            try:
                a=asset(trade["symbol"])
                payload=stop_payload(trade,pos,entry,a)
                # ID saved BEFORE POST; a timed-out POST is never retried blindly.
                save(state)
                result=api("POST","/orders",body=payload)
                trade["status"]="protected"
                save(state)
                log(event="stop_submitted",symbol=symbol,cid=trade["stop_cid"],
                    order_id=result.get("id"),stop=payload["stop_price"])
                alert(f"{symbol}: PAPER crypto server STOP-LIMIT protection submitted "
                      f"({payload['stop_price']} / limit {payload['limit_price']}); "
                      "limit may not fill during a gap.")
            except Exception as exc:
                alert(f"🚨 {symbol}: NO VERIFIED PROTECTIVE STOP: {exc}. Check Alpaca PAPER!")
                safe=False
            continue
        if trade.get("status")=="tp_canceling":
            # The server stop must be CONFIRMED cancelled before sending a
            # SELL market order. A fill during cancellation must not oversell.
            if protective.get("status") in OPEN:
                safe=False
                continue
            if protective.get("status")=="filled":
                alert(f"{symbol}: stop filled during profit cancellation; verify position.")
                safe=False
                continue
            if protective.get("status")!="canceled":
                alert(f"{symbol}: stop cancellation unresolved ({protective.get('status')}).")
                safe=False
                continue
            if this_orders:
                alert(f"{symbol}: open broker order remains after stop cancel.")
                safe=False
                continue
            if not target:
                # This ID is immutable. If POST times out, next run queries
                # Alpaca by client_order_id rather than submitting twice.
                try:
                    a=asset(trade["symbol"])
                    qty=round_step(dec(pos["qty"]),a.get("min_trade_increment") or "0.000000001")
                    if qty<=0 or qty>dec(entry.get("filled_qty") or 0):
                        raise RuntimeError("unverifiable own position qty")
                    payload={"symbol":trade["symbol"],"side":"sell","type":"market",
                             "time_in_force":"gtc","qty":format(qty,"f"),
                             "client_order_id":trade["take_cid"]}
                    save(state)
                    resp=api("POST","/orders",body=payload)
                    trade["status"]="tp_sent"
                    save(state)
                    log(event="take_profit_sent",symbol=symbol,order_id=resp.get("id"))
                    alert(f"{symbol}: profit target reached; PAPER market SELL submitted.")
                except Exception as exc:
                    alert(f"🚨 {symbol}: profit close failed AFTER stop cancel: {exc}. "
                          "MANUAL PAPER CHECK REQUIRED!")
            safe=False
            continue
        if protective.get("status") not in OPEN:
            alert(f"{symbol}: protection status {protective.get('status')} while position exists.")
            safe=False
            continue
        if protective.get("type")!="stop_limit" or protective.get("side")!="sell":
            alert(f"{symbol}: wrong server-side protection!")
            safe=False
            continue
        if dec(protective.get("qty") or "0")!=dec(pos["qty"]):
            alert(f"{symbol}: stop quantity mismatch!")
            safe=False
            continue
        trade["status"]="protected"
        # Crypto has no OCO/bracket. Software TP requires careful cancel-first:
        # stop stays active until Alpaca confirms cancellation.
        try:
            fresh=quote(trade["symbol"])
            price=float(entry.get("filled_avg_price") or "0")
            if price>0 and fresh["bid"]>=price*(1+TARGET_PCT/100):
                if protective.get("id"):
                    trade["status"]="tp_canceling"
                    save(state)
                    api("DELETE","/orders/"+protective["id"])
                    alert(f"{symbol}: TP threshold reached, awaiting confirmed "
                          "cancellation of protective stop before market SELL.")
                    safe=False
        except Exception as exc:
            print(f"CRYPTO PAPER TP quote/cancel: {exc}; protective STOP retained if possible",flush=True)
            # Do not compromise existing protection or open new entries.
            safe=False
    unknown=[sym for sym in current if sym not in state["trades"] or
             state["trades"][sym].get("status")=="closed"]
    if unknown:
        alert(f"Untracked crypto PAPER holdings {unknown}; block all NEW orders.")
        safe=False
    save(state)
    return safe,current

def finish_trade(state,trade,entry,exit_order,reason):
    qty=float(entry.get("filled_qty") or 0)
    xqty=float(exit_order.get("filled_qty") or 0)
    ep=float(entry.get("filled_avg_price") or 0)
    xp=float(exit_order.get("filled_avg_price") or 0)
    if qty<=0 or abs(qty-xqty)>0.0000001 or ep<=0 or xp<=0:
        raise RuntimeError("Cannot calculate reliable crypto order fills")
    pnl=round(qty*(xp-ep),2)   # Fees are NOT in this gross PnL
    trade["status"]="closed"
    trade["pnl_before_fees"]=pnl
    trade["closed_at"]=utcnow().isoformat()
    save(state)
    log(event="closed",symbol=trade["symbol"],pnl_before_fees=pnl,
        reason=reason,qty=qty,entry=ep,exit=xp)
    alert(f"{trade['symbol']} closed: {pnl:+.2f} USD BEFORE CRYPTO FEES ({reason}).")

def submit_one(state,signals,now):
    if not signals: return
    current=positions()
    if current: return
    day,history=history_today(now)
    if len(history)>=DAILY_ENTRIES: return
    realized=sum(float(t.get("pnl_before_fees",0)) for t in state["trades"].values()
                 if t.get("status")=="closed" and
                 str(t.get("closed_at","")).startswith(day))
    if realized<=-DAILY_LOSS_STOP: return
    already={normalize(o.get("symbol")) for o in history}
    for signal in sorted(signals,key=lambda s:(s["relvol"],s["breakout_pct"]),reverse=True):
        if signal["side"]!="buy": continue  # Spot crypto cannot be shorted.
        symbol=signal["symbol"]
        key=normalize(symbol)
        if key in already or key in state["trades"] and state["trades"][key].get("status")!="closed":
            continue
        if any(normalize(o.get("symbol"))==key for o in orders()):
            continue
        if (now-parse_time(signal["bar_close"])).total_seconds()>8*60:
            continue
        q=quote(symbol)
        ref=signal["reference_price"]
        if abs(q["ask"]-ref)/ref*100>0.35:
            continue
        a=asset(symbol)
        qty=size_for_price(q["ask"],a)
        suffix=key+"_"+parse_time(signal["bar"]).strftime("%Y%m%dT%H%M")
        cid=ENTRY_PREFIX+suffix
        trade={"symbol":symbol,"entry_cid":cid,
               "stop_cid":STOP_PREFIX+suffix,"take_cid":TAKE_PREFIX+suffix,
               "status":"entry_pending","created_at":now.isoformat()}
        state["trades"][key]=trade
        save(state)  # persist before POST (locally; GitHub commit follows job)
        payload={"symbol":symbol,"side":"buy","type":"market","time_in_force":"gtc",
                 "qty":format(qty,"f"),"client_order_id":cid}
        # No retry after ambiguous network timeout. Next run checks by CID.
        order=api("POST","/orders",body=payload)
        log(event="entry_submitted",symbol=symbol,cid=cid,order_id=order.get("id"),
            qty=payload["qty"],estimated_risk_usd=EST_RISK_USD)
        alert(f"BUY {symbol} {qty} PAPER sent. Monitoring fill and protective stop.")
        # For market orders, poll so server-side protection is placed promptly.
        for _ in range(8):
            latest=bycid(cid)
            if latest and latest.get("status")=="filled":
                reconcile(state)
                break
            time.sleep(1)
        return

def run(signals=(),now=None,allow_new=False):
    now=now or utcnow()
    state=load()
    safe,existing=reconcile(state)
    if not allow_new:
        print("CRYPTO PAPER safe monitoring only; new orders disabled",flush=True)
        return
    if not safe:
        print("CRYPTO PAPER entry halted: unresolved broker positions/orders/protection",flush=True)
        return
    submit_one(state,signals,now)
