#!/usr/bin/env python3
"""FAST V1 virtual shadow tracker. NO BROKER ORDER API/NO ALPACA ORDERS.

Track all distinct *technical* FAST breakout signals from IEX 5-minute bars.
Simulate one position per symbol per day using next-bar open, 0.05% slippage
per side and 1.2% trailing stop. Results are HYPOTHETICAL and are not PAPER
fills: spread, trading halts, true intrabar order and borrow costs are unknown.
If a bar could both tighten and hit trailing stop, resolve conservatively as
a stop. This module neither imports nor calls Alpaca trading endpoints.
"""
import csv
import json
import math
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from equity_candle_guard import fully_closed_iex
from pathlib import Path

VIRTUAL_STATE = Path("fast_virtual_state_v1.json")
VIRTUAL_SIGNALS = Path("fast_virtual_signals_v1.jsonl")
VIRTUAL_RESULTS = Path("fast_virtual_results_v1.csv")
SLIPPAGE_PCT = 0.05
MAX_NOTIONAL = 1000.0
COLUMNS = ["trade_id", "date", "symbol", "side", "signal_bar", "entry_bar",
           "exit_bar", "entry", "exit", "qty", "pnl_usd", "reason",
           "slippage_pct_per_side", "trailing_percent", "virtual"]


def parse_dt(value):
    if not value:
        return None
    try:
        x = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return x if x.tzinfo else x.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def write_state(state):
    temp = VIRTUAL_STATE.with_suffix(".tmp")
    temp.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temp.replace(VIRTUAL_STATE)


def load_state(session):
    day = session["date"]
    if VIRTUAL_STATE.is_file():
        state = json.loads(VIRTUAL_STATE.read_text(encoding="utf-8"))
        if not isinstance(state, dict) or not isinstance(state.get("symbols"), dict):
            raise RuntimeError("Invalid FAST virtual state; never silently reset")
        if state.get("date") == day:
            return state
        active = [k for k,v in state["symbols"].items()
                  if v.get("phase") in {"open","pending"}]
        if active:
            print(f"FAST VIRTUAL: previous day unresolved (no assumed exits): {active}")
    return {"date": day, "symbols": {}, "seen": []}


def save_result(result):
    exists = VIRTUAL_RESULTS.exists()
    if exists:
        with VIRTUAL_RESULTS.open(newline="", encoding="utf-8") as f:
            if any(x.get("trade_id") == result["trade_id"] for x in csv.DictReader(f)):
                return
    with VIRTUAL_RESULTS.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=COLUMNS)
        if not exists or VIRTUAL_RESULTS.stat().st_size == 0:
            writer.writeheader()
        writer.writerow(result)


def completed_bars(bars, now, session):
    """Ignore an in-progress last bar and all outside-session bars."""
    result = []
    for bar in fully_closed_iex(bars, now, lag_seconds=0):
        t = parse_dt(bar.get("t"))
        if t is None:
            continue
        if session["open"] <= t.astimezone(session["open"].tzinfo) < session["close"] and t + timedelta(minutes=5) <= now:
            if all(k in bar for k in ("o", "h", "l", "c")):
                result.append({"t": t, "o": float(bar["o"]), "h": float(bar["h"]),
                               "l": float(bar["l"]), "c": float(bar["c"])})
    return sorted(result, key=lambda x: x["t"])


def make_entry(signal, bar, risk, trail):
    side = signal["side"]
    slip = SLIPPAGE_PCT / 100
    raw = bar["o"]
    price = raw * (1 + slip if side == "buy" else 1 - slip)
    if price <= 0 or not math.isfinite(price):
        return None
    qty = min(math.floor(risk / (price * (trail / 100 + slip))),
              math.floor(MAX_NOTIONAL / price))
    if qty < 1:
        return None
    return {"phase": "open", "signal": signal, "entry_bar": bar["t"].isoformat(),
            "entry": price, "qty": qty, "high_water": price, "low_water": price,
            "last_bar": None}


def simulate_bar(slot, bar, trail):
    """Conservative intrabar high/low ordering; possible stop-first exits."""
    side, entry = slot["signal"]["side"], slot["entry"]
    rate = trail / 100
    slip = SLIPPAGE_PCT / 100
    if side == "buy":
        peak = max(slot["high_water"], bar["h"])
        stop = peak * (1 - rate)
        if bar["l"] <= stop:
            # Gap through the stop is filled at unfavorable opening price.
            price = min(stop, bar["o"]) * (1 - slip)
            return price, "virtual_trailing_stop"
        slot["high_water"] = peak
    else:
        trough = min(slot["low_water"], bar["l"])
        stop = trough * (1 + rate)
        if bar["h"] >= stop:
            price = max(stop, bar["o"]) * (1 + slip)
            return price, "virtual_trailing_stop"
        slot["low_water"] = trough
    return None


def close_trade(slot, date, symbol, bar, exit_price, reason, trail):
    side = slot["signal"]["side"]
    pnl = ((exit_price - slot["entry"]) if side == "buy"
           else (slot["entry"] - exit_price)) * slot["qty"]
    return {"trade_id": date + "-" + symbol + "-" + slot["signal"]["bar_key"],
            "date": date, "symbol": symbol, "side": side,
            "signal_bar": slot["signal"]["bar_key"], "entry_bar": slot["entry_bar"],
            "exit_bar": bar["t"].isoformat(), "entry": round(slot["entry"], 4),
            "exit": round(exit_price, 4), "qty": slot["qty"],
            "pnl_usd": round(pnl, 2), "reason": reason,
            "slippage_pct_per_side": SLIPPAGE_PCT, "trailing_percent": trail,
            "virtual": True}


def advance(slot, bars, date, symbol, now, session, risk, trail):
    """Advance one virtual trade only after full IEX five-minute bars close."""
    if slot["phase"] == "pending":
        bar_start = parse_dt(slot["signal"]["bar_key"]) + timedelta(minutes=5)
        next_bar = next((b for b in bars if b["t"] >= bar_start), None)
        if next_bar is None:
            return slot, None
        if next_bar["t"] != bar_start:
            return {"phase": "expired", "reason": "missing_next_iex_bar"}, None
        opened = make_entry(slot["signal"], next_bar, risk, trail)
        if not opened:
            return {"phase": "expired", "reason": "virtual_sizing"}, None
        slot = opened

    if slot["phase"] != "open":
        return slot, None
    last = parse_dt(slot.get("last_bar"))
    start = parse_dt(slot["entry_bar"])
    for bar in bars:
        if bar["t"] < start or last and bar["t"] <= last:
            continue
        outcome = simulate_bar(slot, bar, trail)
        slot["last_bar"] = bar["t"].isoformat()
        if outcome:
            price, reason = outcome
            return {"phase": "closed"}, close_trade(slot, date, symbol, bar, price, reason, trail)
    if now >= session["liquidate_at"]:
        if bars and (now - (bars[-1]["t"] + timedelta(minutes=5))).total_seconds() <= 10 * 60:
            bar = bars[-1]
            slip = SLIPPAGE_PCT / 100
            price = bar["c"] * (1 - slip if slot["signal"]["side"] == "buy" else 1 + slip)
            return {"phase": "closed"}, close_trade(slot, date, symbol, bar, price, "virtual_eod", trail)
    return slot, None


def process(session, symbols, get_bars, analyze, now, risk, trail, workers=8):
    """Returns technical FAST breakout signals for PAPER candidate selection.

    Virtual collection and results are independent of whether PAPER entries
    are disabled or daily-risk limits are already reached.
    """
    if now < session["open"].astimezone(timezone.utc):
        return []
    state = load_state(session)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(get_bars, sym): sym for sym in symbols}
        samples = {}
        for future in as_completed(futures):
            sym = futures[future]
            try:
                samples[sym] = future.result()
            except Exception as exc:
                print(f"FAST VIRTUAL {sym}: IEX request failed: {exc}", flush=True)
    paper_signals = []
    seen = set(state.get("seen", []))
    for symbol in symbols:
        if symbol not in samples:
            continue
        bars = samples[symbol]
        completed = completed_bars(bars, now, session)
        slot = state["symbols"].get(symbol, {"phase": "watching"})
        slot, outcome = advance(slot, completed, session["date"], symbol, now, session, risk, trail)
        if outcome:
            save_result(outcome)
            print(f"FAST VIRTUAL CLOSED {symbol}: {outcome['pnl_usd']:+.2f} USD ({outcome['reason']})", flush=True)
        # Do not create new virtual entries during liquidation / final 30min.
        signal = None
        if now < session["entry_stop_at"].astimezone(timezone.utc):
            try:
                signal = analyze(symbol, bars)
            except Exception as exc:
                print(f"FAST VIRTUAL {symbol}: signal analysis failed: {exc}", flush=True)
        if signal:
            paper_signals.append(signal)
            key = symbol + ":" + signal["bar_key"] + ":" + signal["side"]
            if key not in seen:
                event = {"date": session["date"], "symbol": symbol,
                         "signal_bar": signal["bar_key"],
                         "side": signal["side"],
                         "technical_only": True, "real_order": False,
                         "breakout_percent": signal["breakout_percent"],
                         "rel_volume": signal["rel_volume"],
                         "signal_notional_usd": signal["signal_bar_notional_usd"]}
                with VIRTUAL_SIGNALS.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(event) + "\n")
                seen.add(key)
                print(f"FAST VIRTUAL SIGNAL {symbol} {signal['side']} {signal['bar_key']} (not a PAPER order)", flush=True)
            if slot["phase"] == "watching":
                slot = {"phase": "pending", "signal": signal}
        state["symbols"][symbol] = slot
        # Persist each symbol to recover from API or workflow interruption.
        state["seen"] = sorted(seen)
        write_state(state)
    return paper_signals
