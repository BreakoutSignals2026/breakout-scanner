#!/usr/bin/env python3
"""FAST MICRO V1: independent, 100% virtual IEX breakout trade simulator.

Shares *technical signal filters* with FAST V3.3.1, not its PAPER broker orders.
A $1 risk budget, 0.50% fixed stop, 2.5R profit target, 0.05% estimated
slippage EACH side, whole shares; target is approximate $1-2, not guaranteed.
A bar high/low that touches both stop and target exits at the STOP (pessimistic).
A gap beyond stop can lose more than $1. IEX quotes are not consolidated NBBO;
no real order fill, spread, borrow costs or market impact is simulated.
"""
import csv
import json
import math
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import timedelta
from pathlib import Path

import fast_virtual as candle_tools

STATE_PATH = Path("fast_micro_state_v1.json")
SIGNALS_PATH = Path("fast_micro_signals_v1.jsonl")
EVENTS_PATH = Path("fast_micro_events_v1.jsonl")
RESULTS_PATH = Path("fast_micro_results_v1.csv")

RISK_USD = 1.0
STOP_PCT = 0.50
REWARD_R = 2.5
SLIP_PCT = 0.05
MAX_NOTIONAL_USD = 300.0
MAX_DAILY_ATTEMPTS = 12
MAX_SIMULTANEOUS = 2
MAX_DAILY_REALIZED_LOSS = 6.0
MIN_PROJECTED_PROFIT_USD = 1.5
BAR_MINUTES = 5

RESULT_COLUMNS = (
    "trade_id", "date", "symbol", "side", "signal_bar", "entry_bar",
    "exit_bar", "entry", "exit", "qty", "pnl_usd", "reason",
    "planned_risk_usd", "stop_pct", "reward_r", "slippage_pct_each_side",
    "virtual", "estimated_only"
)


def event(action, **fields):
    with EVENTS_PATH.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"action": action, **fields}, sort_keys=True) + "\n")


def persist(state):
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(STATE_PATH)


def load(session):
    day = session["date"]
    if STATE_PATH.exists():
        state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        if not isinstance(state, dict) or not isinstance(state.get("symbols"), dict):
            raise RuntimeError("FAST MICRO corrupt virtual state: refuse silent reset")
        if state.get("date") == day:
            if not isinstance(state.get("seen"), list) or not isinstance(state.get("daily"), dict):
                raise RuntimeError("FAST MICRO incomplete state: refuse silent reset")
            return state
        # Never make up last session's closing price if end-of-day data missing.
        for symbol, slot in state["symbols"].items():
            if slot.get("phase") in ("pending", "open"):
                event("previous_session_unresolved", symbol=symbol,
                      previous_date=state["date"], no_assumed_pnl=True)
                print(f"FAST MICRO unresolved {symbol} from {state['date']}: excluded from realized PnL")
    return {"date": day, "symbols": {}, "seen": [],
            "daily": {"attempts": 0, "closed": 0, "pnl_usd": 0.0}}


def completed_bars(raw, now, session):
    # Reuse same conservative IEX interpretation as shadow FAST; the final
    # potentially forming bar is never treated as a completed candle.
    return candle_tools.completed_bars(raw, now, session)


def projected_profit(qty, entry, side):
    slip = SLIP_PCT / 100
    distance = STOP_PCT * REWARD_R / 100
    raw_exit = entry * (1 + distance if side == "buy" else 1 - distance)
    exec_exit = raw_exit * (1 - slip if side == "buy" else 1 + slip)
    return qty * ((exec_exit - entry) if side == "buy" else (entry - exec_exit))


def make_entry(signal, next_bar):
    side = signal["side"]
    if side not in ("buy", "sell"):
        return None
    raw = float(next_bar["o"])
    if raw <= 0 or not math.isfinite(raw):
        return None
    slip = SLIP_PCT / 100
    entry = raw * (1 + slip if side == "buy" else 1 - slip)
    risk_rate = STOP_PCT / 100 + 2 * slip
    qty = min(math.floor(RISK_USD / (entry * risk_rate) + 1e-9),
              math.floor(MAX_NOTIONAL_USD / entry))
    if qty < 1 or projected_profit(qty, entry, side) < MIN_PROJECTED_PROFIT_USD:
        return None
    return {"phase": "open", "signal": signal, "entry_bar": next_bar["t"].isoformat(),
            "entry": entry, "qty": qty, "last_bar": None}


def exit_check(slot, bar):
    """Stops always win ambiguous bars. Bad gaps receive a worse open fill."""
    side, entry = slot["signal"]["side"], slot["entry"]
    rate = STOP_PCT / 100
    target_rate = rate * REWARD_R
    slip = SLIP_PCT / 100
    if side == "buy":
        stop, tp = entry * (1 - rate), entry * (1 + target_rate)
        if bar["l"] <= stop:
            return min(stop, bar["o"]) * (1 - slip), "SL"
        if bar["h"] >= tp:
            return tp * (1 - slip), "TP"
    else:
        stop, tp = entry * (1 + rate), entry * (1 - target_rate)
        if bar["h"] >= stop:
            return max(stop, bar["o"]) * (1 + slip), "SL"
        if bar["l"] <= tp:
            return tp * (1 + slip), "TP"
    return None


def result(slot, session, symbol, bar, exit_price, reason):
    side = slot["signal"]["side"]
    pnl = (exit_price - slot["entry"] if side == "buy"
           else slot["entry"] - exit_price) * slot["qty"]
    return {"trade_id": session["date"] + ":" + symbol + ":" + slot["signal"]["bar_key"],
            "date": session["date"], "symbol": symbol, "side": side,
            "signal_bar": slot["signal"]["bar_key"], "entry_bar": slot["entry_bar"],
            "exit_bar": bar["t"].isoformat(), "entry": round(slot["entry"], 6),
            "exit": round(exit_price, 6), "qty": slot["qty"],
            "pnl_usd": round(pnl, 2), "reason": reason,
            "planned_risk_usd": RISK_USD, "stop_pct": STOP_PCT,
            "reward_r": REWARD_R, "slippage_pct_each_side": SLIP_PCT,
            "virtual": True, "estimated_only": True}


def append_result(outcome):
    exists = RESULTS_PATH.exists()
    if exists:
        with RESULTS_PATH.open(newline="", encoding="utf-8") as source:
            if any(row["trade_id"] == outcome["trade_id"] for row in csv.DictReader(source)):
                return False
    with RESULTS_PATH.open("a", newline="", encoding="utf-8") as output:
        writer = csv.DictWriter(output, fieldnames=RESULT_COLUMNS)
        if not exists or RESULTS_PATH.stat().st_size == 0:
            writer.writeheader()
        writer.writerow(outcome)
    return True


def advance(slot, completed, now, session, symbol):
    """Process ONLY previously confirmed five-minute bars, no fabricated fills."""
    if slot.get("phase") == "pending":
        expected = candle_tools.parse_dt(slot["signal"]["bar_key"]) + timedelta(minutes=BAR_MINUTES)
        next_bar = next((b for b in completed if b["t"] >= expected), None)
        if next_bar is None:
            return slot, None
        if next_bar["t"] != expected:
            return {"phase": "expired", "reason": "missing_next_complete_bar"}, None
        opened = make_entry(slot["signal"], next_bar)
        if not opened:
            return {"phase": "expired", "reason": "insufficient_whole_share_target"}, None
        slot = opened
    if slot.get("phase") != "open":
        return slot, None
    last = candle_tools.parse_dt(slot.get("last_bar"))
    start = candle_tools.parse_dt(slot["entry_bar"])
    for bar in completed:
        if bar["t"] < start or (last is not None and bar["t"] <= last):
            continue
        checked = exit_check(slot, bar)
        slot["last_bar"] = bar["t"].isoformat()
        if checked:
            price, reason = checked
            return {"phase": "closed"}, result(slot, session, symbol, bar, price, reason)
    # Exit at most recently observed COMPLETED candle at/before liquidation.
    # An absent EOD candle leaves an unresolved virtual position, not fake PnL.
    if now >= session["liquidate_at"]:
        eligible = [b for b in completed if b["t"] + timedelta(minutes=BAR_MINUTES)
                    <= session["liquidate_at"]]
        if eligible and (session["liquidate_at"] -
                         (eligible[-1]["t"] + timedelta(minutes=BAR_MINUTES))).total_seconds() <= 600:
            bar = eligible[-1]
            slip = SLIP_PCT / 100
            price = bar["c"] * (1 - slip if slot["signal"]["side"] == "buy"
                                else 1 + slip)
            return {"phase": "closed"}, result(slot, session, symbol, bar, price, "EOD")
    return slot, None


def process(session, symbols, fetch, analyze, now, workers=8):
    """Persist independent virtual results. No Alpaca order endpoints called."""
    if now < session["open"]:
        print("FAST MICRO: waiting for US session open")
        return {"checks": 0, "signals": 0, "attempts": 0, "closed": 0, "pnl_usd": 0}
    state = load(session)
    stats = {"checks": 0, "signals": 0, "attempts": state["daily"]["attempts"],
             "closed": state["daily"]["closed"], "pnl_usd": state["daily"]["pnl_usd"]}
    with ThreadPoolExecutor(max_workers=workers) as executor:
        jobs = {executor.submit(fetch, sym): sym for sym in symbols}
        samples = {}
        for job in as_completed(jobs):
            sym = jobs[job]
            try:
                samples[sym] = job.result()
            except Exception as exc:
                print(f"FAST MICRO data error {sym}: {exc}", flush=True)
    seen = set(state["seen"])
    for symbol in symbols:
        if symbol not in samples:
            continue
        bars = samples[symbol]
        stats["checks"] += 1
        completed = completed_bars(bars, now, session)
        slot = state["symbols"].get(symbol, {"phase": "watching"})
        previous_phase = slot["phase"]
        slot, outcome = advance(slot, completed, now, session, symbol)
        state["symbols"][symbol] = slot
        if outcome:
            append_result(outcome)
            state["daily"]["closed"] += 1
            state["daily"]["pnl_usd"] = round(
                state["daily"]["pnl_usd"] + outcome["pnl_usd"], 2)
            event("close", **outcome)
            print(f"FAST MICRO virtual CLOSE {symbol} {outcome['pnl_usd']:+.2f} USD ({outcome['reason']})", flush=True)
        elif previous_phase == "pending" and slot["phase"] == "expired":
            event("expired", date=session["date"], symbol=symbol, reason=slot["reason"])
        signal = None
        if now < session["entry_stop_at"]:
            try:
                signal = analyze(symbol, bars)
            except Exception as exc:
                print(f"FAST MICRO signal error {symbol}: {exc}", flush=True)
        if signal:
            signal_time = candle_tools.parse_dt(signal.get("bar_key"))
            if signal_time is None or signal_time < session["open"] or (
                signal_time + timedelta(minutes=BAR_MINUTES) > now
            ):
                signal = None
        if signal:
            key = symbol + ":" + signal["bar_key"] + ":" + signal["side"]
            if key not in seen:
                with SIGNALS_PATH.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps({"date": session["date"],
                                             "symbol": symbol, "signal": signal,
                                             "virtual_only": True}) + "\n")
                seen.add(key)
                stats["signals"] += 1
            if (slot["phase"] == "watching" and
                    state["daily"]["attempts"] < MAX_DAILY_ATTEMPTS and
                    state["daily"]["pnl_usd"] > -MAX_DAILY_REALIZED_LOSS and
                    sum(p.get("phase") in {"pending", "open"}
                        for p in state["symbols"].values()) < MAX_SIMULTANEOUS):
                slot = {"phase": "pending", "signal": signal}
                state["symbols"][symbol] = slot
                state["daily"]["attempts"] += 1
                print(f"FAST MICRO PENDING {symbol} {signal['side']} (VIRTUAL ONLY)", flush=True)
        # Save per-symbol so other completed signals survive a partial API fault.
        state["seen"] = sorted(seen)
        persist(state)
    persist(state)
    stats.update({"attempts": state["daily"]["attempts"],
                  "closed": state["daily"]["closed"],
                  "pnl_usd": state["daily"]["pnl_usd"]})
    print(f"FAST MICRO V1 {session['date']} {stats} (NO BROKER ORDERS)", flush=True)
    return stats
