#!/usr/bin/env python3
"""Independent virtual breakout engine; NEVER sends broker orders.

State survives workflow restarts. Entries use NEXT confirmed 5-minute bar
opening (after signal bar close). Intrabar stop/TP ambiguity goes to stop.
No future bar is used before it closes; no synthetic fills if data is absent.
"""
import csv
import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path

def dt(value):
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return datetime.fromisoformat(value.replace("Z", "+00:00"))

def full_bars(bars, now, minutes=5):
    seen = {}
    for item in bars:
        try:
            t = dt(item["t"])
            if t + timedelta(minutes=minutes) > now:
                continue
            bar = {"t": t, **{k: float(item[k]) for k in ("o","h","l","c","v")}}
            if min(bar[k] for k in ("o","h","l","c")) <= 0 or bar["v"] < 0:
                continue
            seen[t.isoformat()] = bar
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
    return sorted(seen.values(), key=lambda x: x["t"])

def find_signal(symbol, bars, now, *, lookback=12, min_breakout=0.15,
                min_relvol=1.5, max_age_minutes=8, allow_short=False, minutes=5):
    """Check a fully completed candle against earlier candles; no future leakage."""
    candles = full_bars(bars, now, minutes)
    if len(candles) < lookback+1:
        return None
    last = candles[-1]
    age = (now - (last["t"]+timedelta(minutes=minutes))).total_seconds()/60
    if age < -0.1 or age > max_age_minutes:
        return None
    prior = candles[-lookback-1:-1]
    volumes = [b["v"] for b in prior if b["v"] > 0]
    if len(volumes) < max(8, lookback-2):
        return None
    relative = last["v"] / (sum(volumes)/len(volumes))
    if relative < min_relvol:
        return None
    upper = max(b["h"] for b in prior)
    lower = min(b["l"] for b in prior)
    if last["c"] > upper and last["h"] > upper:
        side, breakout = "buy", (last["c"]/upper-1)*100
    elif allow_short and last["c"] < lower and last["l"] < lower:
        side, breakout = "sell", (1-last["c"]/lower)*100
    else:
        return None
    if breakout < min_breakout:
        return None
    return {"symbol": symbol,"side": side,"bar": last["t"].isoformat(),
            "bar_close":(last["t"]+timedelta(minutes=minutes)).isoformat(),
            "reference_price":last["c"],"breakout_pct":round(breakout,4),
            "relvol":round(relative,3),"age_minutes":round(age,2)}

FIELDS = ("trade_id","symbol","side","signal_at","entry_at","exit_at",
          "qty","entry","exit","pnl","reason","currency","simulated")

class VirtualBook:
    def __init__(self, prefix, *, risk=3.0, stop_pct=1.2, reward=2.0,
                 fee_pct=0.25, slippage_pct=0.08, notional_cap=250.0,
                 fractional=True, currency="USD", max_day_entries=6,
                 max_open=2, bar_minutes=5):
        self.state_path = Path(prefix+"_state.json")
        self.signal_path = Path(prefix+"_signals.jsonl")
        self.result_path = Path(prefix+"_trades.csv")
        self.risk, self.stop_pct, self.reward = risk, stop_pct, reward
        self.fee_pct, self.slippage_pct = fee_pct, slippage_pct
        self.notional_cap, self.fractional = notional_cap, fractional
        self.currency, self.max_day_entries = currency, max_day_entries
        self.max_open, self.bar_minutes = max_open, bar_minutes
        if self.state_path.exists():
            self.state = json.loads(self.state_path.read_text(encoding="utf-8"))
            if not isinstance(self.state,dict) or not isinstance(self.state.get("positions"),dict):
                raise RuntimeError("Invalid virtual state. No silent reset.")
        else:
            self.state = {"positions":{}, "seen":{}, "daily":{}}

    def persist(self):
        tmp=self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state,sort_keys=True,indent=2)+"\n",encoding="utf-8")
        tmp.replace(self.state_path)

    def signal(self, sig, day):
        """Record every unique signal, even when entry or limits blocked."""
        key=sig["symbol"]+":"+sig["bar"]+":"+sig["side"]
        if key in self.state["seen"]:
            return False
        self.state["seen"][key]=day
        # Prune old keys after retention window to keep JSON compact.
        if len(self.state["seen"])>2500:
            items=list(self.state["seen"].items())[-1800:]
            self.state["seen"]=dict(items)
        with self.signal_path.open("a",encoding="utf-8") as out:
            out.write(json.dumps({"date":day,**sig,"type":"TECHNICAL_ONLY",
                                  "no_broker_order":True})+"\n")
        self.persist()
        return True

    def existing(self, symbol):
        return symbol in self.state["positions"]

    def maybe_enter(self,sig,day):
        if self.existing(sig["symbol"]):
            return False
        d=self.state["daily"].setdefault(day,{"attempts":0,"symbols":[]})
        if d["attempts"]>=self.max_day_entries or len(self.state["positions"])>=self.max_open or sig["symbol"] in d["symbols"]:
            return False
        # A pending entry must be recorded now so a restarted runner won't
        # retroactively fill multiple signals.
        self.state["positions"][sig["symbol"]]={"phase":"pending","signal":sig}
        d["attempts"]+=1
        d["symbols"].append(sig["symbol"])
        self.persist()
        return True

    def _save_result(self,result):
        exists=self.result_path.exists()
        if exists:
            with self.result_path.open(newline="",encoding="utf-8") as f:
                if any(x["trade_id"]==result["trade_id"] for x in csv.DictReader(f)):
                    return
        with self.result_path.open("a",newline="",encoding="utf-8") as f:
            out=csv.DictWriter(f,fieldnames=FIELDS)
            if not exists or self.result_path.stat().st_size==0: out.writeheader()
            out.writerow(result)

    def _exit(self,symbol,pos,bar,price,reason):
        side=pos["signal"]["side"]
        cost=(self.fee_pct+self.slippage_pct)/100
        executed=price*(1-cost if side=="buy" else 1+cost)
        gross=(executed-pos["entry"]) if side=="buy" else (pos["entry"]-executed)
        # Include estimated fee at entry, too. Fee schedule varies.
        pnl=pos["qty"]*(gross-pos["entry"]*self.fee_pct/100)
        result={"trade_id":symbol+":"+pos["signal"]["bar"],
                "symbol":symbol,"side":side,"signal_at":pos["signal"]["bar"],
                "entry_at":pos["entry_at"],"exit_at":bar["t"].isoformat(),
                "qty":pos["qty"],"entry":round(pos["entry"],6),
                "exit":round(executed,6),"pnl":round(pnl,2),"reason":reason,
                "currency":self.currency,"simulated":True}
        self._save_result(result)
        del self.state["positions"][symbol]
        self.persist()
        return result

    def progress(self, symbol, raw, now, *, session_end=None):
        """Virtual exits use completed bars; EOD at latest closed bar only."""
        pos=self.state["positions"].get(symbol)
        if not pos: return None
        bars=full_bars(raw,now,self.bar_minutes)
        if pos["phase"]=="pending":
            expected=dt(pos["signal"]["bar"])+timedelta(minutes=self.bar_minutes)
            follow=next((b for b in bars if b["t"]>=expected),None)
            if not follow: return None
            if follow["t"]!=expected:
                print(f"VIRTUAL {symbol}: next bar missing; no invented fill")
                del self.state["positions"][symbol]
                self.persist()
                return None
            price=follow["o"]*(1+self.slippage_pct/100 if pos["signal"]["side"]=="buy" else 1-self.slippage_pct/100)
            loss_rate=(self.stop_pct+self.fee_pct*2+self.slippage_pct*2)/100
            qty=min(self.notional_cap/price,self.risk/(price*loss_rate))
            if not self.fractional: qty=math.floor(qty)
            else: qty=math.floor(qty*100000000)/100000000
            if qty<=0:
                del self.state["positions"][symbol]
                self.persist()
                return None
            pos.update({"phase":"open","entry":price,"qty":qty,
                        "entry_at":follow["t"].isoformat(),"last_bar":None})
            self.persist()
        last=dt(pos["last_bar"]) if pos.get("last_bar") else None
        started=dt(pos["entry_at"])
        for bar in bars:
            if bar["t"]<started or (last and bar["t"]<=last): continue
            ep=pos["entry"]
            risk=self.stop_pct/100
            if pos["signal"]["side"]=="buy":
                stop,target=ep*(1-risk),ep*(1+risk*self.reward)
                if bar["l"]<=stop:
                    return self._exit(symbol,pos,bar,min(bar["o"],stop),"SL")
                if bar["h"]>=target:
                    return self._exit(symbol,pos,bar,max(bar["o"],target),"TP")
            else:
                stop,target=ep*(1+risk),ep*(1-risk*self.reward)
                if bar["h"]>=stop:
                    return self._exit(symbol,pos,bar,max(bar["o"],stop),"SL")
                if bar["l"]<=target:
                    return self._exit(symbol,pos,bar,min(bar["o"],target),"TP")
            pos["last_bar"]=bar["t"].isoformat()
        self.persist()
        if session_end and now>=session_end:
            final=next((b for b in reversed(bars) if b["t"]+timedelta(minutes=self.bar_minutes)<=session_end),None)
            if final and (session_end-(final["t"]+timedelta(minutes=self.bar_minutes))).total_seconds()<=self.bar_minutes*60:
                return self._exit(symbol,pos,final,final["c"],"EOD")
            print(f"VIRTUAL {symbol}: EOD candle absent, no synthetic close")
        return None
