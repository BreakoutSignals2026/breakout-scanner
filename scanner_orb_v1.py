#!/usr/bin/env python3
"""ORB V1.0 – opening-range breakout SIGNAL-ONLY simulator.

NEVER places Alpaca orders. Only GET market data/calendar and optionally
POSTs Telegram messages. All entries and exits are HYPOTHETICAL.

Opening range: the first three *fully closed* 5-minute US regular-session
IEX candles (09:30, 09:35, 09:40 America/New_York). A breakout is recognized
only on a fully closed later candle; hypothetical fill = the NEXT candle's
open, observed after that candle has fully closed. TP/SL are evaluated
conservatively, including stop priority if the same bar touches both.
The IEX feed is a minority subset of US trades. This is not a backtest of
consolidated prices and not a guarantee of real fillability or profit.
"""
import csv
import json
import math
import os
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

VERSION = "1.0"
NY = ZoneInfo("America/New_York")
UTC = timezone.utc
CALENDAR_URL = "https://paper-api.alpaca.markets/v2/calendar"
BARS_URL = "https://data.alpaca.markets/v2/stocks/bars"
KEY = os.getenv("ALPACA_API_KEY", "").strip()
SECRET = os.getenv("ALPACA_SECRET_KEY", "").strip()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()

# Same universe as FAST; entirely separate, hypothetical ORB trades.
SYMBOLS = [
    "SOFI", "RIVN", "DKNG", "HIMS", "CHWY", "FSLY", "PATH", "U",
    "CLSK", "IREN", "MARA", "RIOT", "CELH",
    "NU", "AAL", "CLF", "F", "VALE", "CCL", "BB", "SNAP",
    "RGTI", "QBTS", "GME", "PTON", "AA", "LUNR", "NOK",
]
assert len(SYMBOLS) == 28 and len(set(SYMBOLS)) == len(SYMBOLS)

STATE_FILE = Path("orb_state_v1.json")
RESULTS_FILE = Path("orb_results_v1.csv")
RISK_USD = 5.0
MAX_NOTIONAL = 1000.0  # Hypothetical margin/capital realism, not an entry frequency cap.
REWARD_RISK = 2.0
SLIPPAGE_PCT_PER_SIDE = 0.05
MIN_PRICE = 5.0
MAX_PRICE = 50.0
MIN_OR_WIDTH_PCT = 0.25
MAX_OR_WIDTH_PCT = 5.0
MIN_BREAKOUT_PCT = 0.15
MIN_REL_VOL = 1.10
MIN_SIGNAL_NOTIONAL = 15000.0
MIN_OPENING_BAR_SHARES = 300.0
MAX_STOP_DIST_PCT = 2.0
MAX_SIGNAL_AGE_SECONDS = 8 * 60
MINUTES_BEFORE_CLOSE_TO_EXIT = 15
SCAN_END_HOUR, SCAN_END_MIN = 11, 30  # Signals only before 11:30 NY.
INTERVAL_SECONDS = 90

RESULT_COLUMNS = [
    "trade_id", "date", "symbol", "side", "signal_bar", "entry_bar",
    "exit_bar", "orb_high", "orb_low", "entry", "stop", "tp",
    "exit", "qty", "risk_estimate_usd", "gross_pnl_usd",
    "gross_pnl_r", "exit_reason", "ambiguous_stop_first",
    "simulated", "slippage_pct_per_side",
]


def now_ny():
    return datetime.now(NY)


def iso(dt):
    return dt.isoformat(timespec="seconds")


def parse_time(value):
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(NY)


def price(x):
    try:
        v = float(x)
        return v if math.isfinite(v) else 0.0
    except (ValueError, TypeError):
        return 0.0


def headers():
    if not KEY or not SECRET:
        raise RuntimeError("ORB needs the existing Alpaca PAPER data credentials")
    return {"APCA-API-KEY-ID": KEY, "APCA-API-SECRET-KEY": SECRET}


def get_json(url, params):
    response = requests.get(url, headers=headers(), params=params, timeout=20)
    response.raise_for_status()
    return response.json()


def notify(message):
    body = "[ORB V1.0 SIMULATION, NO ALPACA ORDER] " + message
    print(body, flush=True)
    if not TELEGRAM_TOKEN or not CHAT_ID:
        return
    try:
        reply = requests.post(
            "https://api.telegram.org/bot" + TELEGRAM_TOKEN + "/sendMessage",
            json={"chat_id": CHAT_ID, "text": body}, timeout=8,
        )
        reply.raise_for_status()
    except requests.RequestException as exc:
        print("ORB Telegram send failed: " + str(exc), flush=True)


def new_state():
    return {"version": VERSION, "session_date": None, "symbols": {},
            "daily_summary_sent": False}


def load_state():
    if not STATE_FILE.exists():
        return new_state()
    state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    if (not isinstance(state, dict) or state.get("version") != VERSION
            or not isinstance(state.get("symbols"), dict)):
        raise RuntimeError("ORB state invalid; preserving data and stopping")
    state.setdefault("daily_summary_sent", False)
    return state


def save_state(state):
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True,
                              ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(STATE_FILE)


def ensure_results():
    if not RESULTS_FILE.exists() or RESULTS_FILE.stat().st_size == 0:
        with RESULTS_FILE.open("w", newline="", encoding="utf-8") as out:
            csv.DictWriter(out, fieldnames=RESULT_COLUMNS).writeheader()


def append_result(result):
    # Deduplicate after crash/replay or delayed GitHub workflow merges.
    ensure_results()
    with RESULTS_FILE.open(newline="", encoding="utf-8") as f:
        previous = {row["trade_id"] for row in csv.DictReader(f)}
    if result["trade_id"] in previous:
        return
    with RESULTS_FILE.open("a", newline="", encoding="utf-8") as out:
        writer = csv.DictWriter(out, fieldnames=RESULT_COLUMNS)
        writer.writerow({k: result.get(k, "") for k in RESULT_COLUMNS})


def get_session(now):
    day = now.date().isoformat()
    rows = get_json(CALENDAR_URL, {"start": day, "end": day})
    if not isinstance(rows, list):
        raise RuntimeError("Alpaca calendar unavailable")
    row = next((x for x in rows if x.get("date") == day), None)
    if not row:
        return None

    def clock_value(key):
        return datetime.combine(now.date(),
                                datetime.strptime(row[key], "%H:%M").time(), NY)

    start, close = clock_value("open"), clock_value("close")
    if not start < close or close - start > timedelta(hours=9):
        raise RuntimeError("Invalid Alpaca session calendar")
    return {"day": day, "open": start, "close": close,
            "liquidate": close - timedelta(minutes=MINUTES_BEFORE_CLOSE_TO_EXIT)}


def get_bars(symbols, start, now):
    """Fetch whole session with pagination; no partial results if a page fails."""
    all_bars = {}
    chunk_size = 4
    for offset in range(0, len(symbols), chunk_size):
        group = symbols[offset:offset + chunk_size]
        token = None
        seen = set()
        for _ in range(10):
            query = {
                "symbols": ",".join(group), "timeframe": "5Min",
                "start": start.astimezone(UTC).isoformat(),
                "end": now.astimezone(UTC).isoformat(),
                "feed": "iex", "adjustment": "raw", "limit": 10000,
                "sort": "asc",
            }
            if token:
                query["page_token"] = token
            payload = get_json(BARS_URL, query)
            if not isinstance(payload.get("bars"), dict):
                raise RuntimeError("Invalid Alpaca bar response")
            for symbol, rows in payload["bars"].items():
                if symbol in group:
                    all_bars.setdefault(symbol, []).extend(rows)
            token = payload.get("next_page_token")
            if not token:
                break
            if token in seen:
                raise RuntimeError("Repeated Alpaca pagination token")
            seen.add(token)
        else:
            raise RuntimeError("Alpaca data pagination exceeded limit")
    return all_bars


def closed_bars(raw, session, now):
    """Use only fully closed regular-session candles, never forming bars."""
    cleaned = {}
    last = min(session["close"], now - timedelta(seconds=10))
    for row in raw:
        try:
            t = parse_time(row["t"])
            if t.date() != now.date():
                continue
            if t < session["open"] or t + timedelta(minutes=5) > last:
                continue
            if t.second or t.minute % 5:
                continue
            o, h, l, c, v = (price(row.get(key)) for key in ("o", "h", "l", "c", "v"))
            if o <= 0 or l <= 0 or h < max(o, c) or l > min(o, c) or v <= 0:
                continue
            cleaned[iso(t)] = {"t": t, "o": o, "h": h, "l": l, "c": c,
                               "v": v, "n": price(row.get("n"))}
        except (ValueError, KeyError, TypeError):
            continue
    return sorted(cleaned.values(), key=lambda b: b["t"])


def get_opening_range(bars, session):
    expected = [session["open"] + timedelta(minutes=i) for i in (0, 5, 10)]
    by_time = {b["t"]: b for b in bars}
    if not all(t in by_time for t in expected):
        return None
    first = [by_time[t] for t in expected]
    if any(b["v"] < MIN_OPENING_BAR_SHARES for b in first):
        return None
    hi, lo = max(b["h"] for b in first), min(b["l"] for b in first)
    if not lo > 0:
        return None
    width_pct = (hi - lo) / lo * 100
    if not MIN_OR_WIDTH_PCT <= width_pct <= MAX_OR_WIDTH_PCT:
        return None
    return {"high": hi, "low": lo,
            "avg_volume": sum(b["v"] for b in first) / 3,
            "width_pct": width_pct}


def pick_signal(symbol, bars, opening, session, now):
    """Closed-bar break of 15m range; require previous close within range."""
    if len(bars) < 4:
        return None
    last = bars[-1]
    previous = bars[-2]
    bar_close = last["t"] + timedelta(minutes=5)
    scan_start = session["open"] + timedelta(minutes=15)
    scan_end = session["open"].replace(hour=SCAN_END_HOUR, minute=SCAN_END_MIN)
    if last["t"] < scan_start or bar_close > scan_end:
        return None
    signal_age = (now - bar_close).total_seconds()
    if not 0 <= signal_age <= MAX_SIGNAL_AGE_SECONDS:
        return None
    if not MIN_PRICE <= last["c"] <= MAX_PRICE:
        return None
    if last["v"] * last["c"] < MIN_SIGNAL_NOTIONAL:
        return None
    rel = last["v"] / opening["avg_volume"]
    if rel < MIN_REL_VOL:
        return None
    high, low = opening["high"], opening["low"]
    if last["c"] > high and previous["c"] <= high:
        side, breakout = "LONG", (last["c"] / high - 1) * 100
    elif last["c"] < low and previous["c"] >= low:
        side, breakout = "SHORT", (1 - last["c"] / low) * 100
    else:
        return None
    if breakout < MIN_BREAKOUT_PCT:
        return None
    return {"symbol": symbol, "side": side, "signal_bar": iso(last["t"]),
            "orb_high": high, "orb_low": low,
            "rel_vol": round(rel, 3), "breakout_pct": round(breakout, 3)}


def simulated_entry(signal, bar, day):
    """Always model next-bar open, never the already-known signal candle close."""
    side = signal["side"]
    slip = SLIPPAGE_PCT_PER_SIDE / 100
    entry = bar["o"] * (1 + slip if side == "LONG" else 1 - slip)
    width = signal["orb_high"] - signal["orb_low"]
    stop = (signal["orb_high"] - width * 0.25 if side == "LONG"
            else signal["orb_low"] + width * 0.25)
    dist = entry - stop if side == "LONG" else stop - entry
    if entry <= 0 or dist <= 0:
        return None
    if not MIN_PRICE <= entry <= MAX_PRICE or dist / entry * 100 > MAX_STOP_DIST_PCT:
        return None
    # Include assumed slippage on BOTH legs of a stopped trade.
    risk_per_share = dist + entry * slip
    qty = min(math.floor(RISK_USD / risk_per_share),
              math.floor(MAX_NOTIONAL / entry))
    if qty < 1:
        return None
    target = (entry + REWARD_RISK * dist if side == "LONG"
              else entry - REWARD_RISK * dist)
    if target <= 0:
        return None
    trade_id = day.replace("-", "") + "-" + signal["symbol"] + "-" + \
        signal["signal_bar"].replace("-", "").replace(":", "").replace("+", "")
    return {**signal, "trade_id": trade_id, "entry_bar": iso(bar["t"]),
            "entry": entry, "stop": stop, "tp": target, "qty": qty,
            "last_evaluated_bar": None,
            "risk_estimate_usd": round(qty * risk_per_share, 2)}


def stop_and_target(trade, bar):
    """Conservatively resolve SL and TP using completed candle OHLC."""
    side = trade["side"]
    stop, target, opening = trade["stop"], trade["tp"], bar["o"]
    if side == "LONG":
        hit_stop = bar["l"] <= stop
        hit_target = bar["h"] >= target
    else:
        hit_stop = bar["h"] >= stop
        hit_target = bar["l"] <= target
    if not hit_stop and not hit_target:
        return None
    ambiguous = hit_stop and hit_target
    if hit_stop:
        raw_exit = min(stop, opening) if side == "LONG" else max(stop, opening)
        reason = "SL"
    else:
        raw_exit = target  # No favorable gap bonus in simulation.
        reason = "TP"
    slip = SLIPPAGE_PCT_PER_SIDE / 100
    exit_price = raw_exit * (1 - slip if side == "LONG" else 1 + slip)
    return exit_price, reason, ambiguous


def finish_trade(trade, exit_price, reason, exit_bar, ambiguous=False):
    qty, entry = trade["qty"], trade["entry"]
    gross = ((exit_price - entry) if trade["side"] == "LONG"
             else (entry - exit_price)) * qty
    result = {
        "trade_id": trade["trade_id"], "date": trade["signal_bar"][:10],
        "symbol": trade["symbol"], "side": trade["side"],
        "signal_bar": trade["signal_bar"], "entry_bar": trade["entry_bar"],
        "exit_bar": iso(exit_bar), "orb_high": round(trade["orb_high"], 4),
        "orb_low": round(trade["orb_low"], 4),
        "entry": round(entry, 4), "stop": round(trade["stop"], 4),
        "tp": round(trade["tp"], 4), "exit": round(exit_price, 4),
        "qty": qty, "risk_estimate_usd": trade["risk_estimate_usd"],
        "gross_pnl_usd": round(gross, 2),
        "gross_pnl_r": round(gross / RISK_USD, 3),
        "exit_reason": reason, "ambiguous_stop_first": ambiguous,
        "simulated": True, "slippage_pct_per_side": SLIPPAGE_PCT_PER_SIDE,
    }
    append_result(result)
    notify("CLOSED " + trade["symbol"] + " " + reason
           + " | simulated PnL " + str(result["gross_pnl_usd"]) + " USD"
           + (" | SL FIRST, SL+TP in same bar" if ambiguous else ""))
    return result


def update_symbol(symbol, bars, session, now, slot):
    """Advance exactly one symbol; all writes persist separately by workflow."""
    opening = get_opening_range(bars, session)
    if opening is None:
        return slot
    phase = slot.get("phase", "watching")
    if phase in {"closed", "rejected", "expired"}:
        return slot
    if phase == "watching":
        signal = pick_signal(symbol, bars, opening, session, now)
        if signal is not None:
            slot = {"phase": "pending", "signal": signal}
            notify("SIGNAL " + symbol + " " + signal["side"]
                   + " | breakout " + str(signal["breakout_pct"]) + "%"
                   + " | relVol " + str(signal["rel_vol"]) + "x"
                   + " | next-bar open will be simulated, NO ORDER")
        return slot

    if phase == "pending":
        signal = slot["signal"]
        sig = parse_time(signal["signal_bar"])
        next_time = sig + timedelta(minutes=5)
        following = next((bar for bar in bars if bar["t"] >= next_time), None)
        if following is not None:
            if following["t"] != next_time:
                slot = {"phase": "expired", "reason": "next IEX 5m bar missing",
                        "signal": signal}
                return slot
            trade = simulated_entry(signal, following, session["day"])
            if trade is None:
                return {"phase": "rejected", "reason": "sizing or stop range",
                        "signal": signal}
            slot = {"phase": "open", "trade": trade}
            notify("VIRTUAL ENTRY " + symbol + " " + trade["side"]
                   + " | " + str(trade["qty"]) + " shares at "
                   + str(round(trade["entry"], 4)) + " USD"
                   + " | SL " + str(round(trade["stop"], 4))
                   + " | TP " + str(round(trade["tp"], 4))
                   + " | NO ALPACA ORDER")
        elif now >= next_time + timedelta(minutes=12):
            return {"phase": "expired", "reason": "next bar unavailable",
                    "signal": signal}
        else:
            return slot

    if slot.get("phase") == "open":
        trade = slot["trade"]
        first_time = parse_time(trade["entry_bar"])
        evaluated = parse_time(trade["last_evaluated_bar"]) if trade.get("last_evaluated_bar") else None
        for bar in bars:
            if bar["t"] < first_time or (evaluated and bar["t"] <= evaluated):
                continue
            exit_info = stop_and_target(trade, bar)
            trade["last_evaluated_bar"] = iso(bar["t"])
            if exit_info:
                out, reason, ambiguous = exit_info
                result = finish_trade(trade, out, reason, bar["t"], ambiguous)
                return {"phase": "closed", "trade": trade, "result": result}
        if now >= session["liquidate"]:
            # EOD close is hypothetical and uses the last freshly closed bar.
            # Stale data must not produce an invented fill price.
            recent = [b for b in bars if b["t"] + timedelta(minutes=5) <= now]
            if recent and (now - (recent[-1]["t"] + timedelta(minutes=5))).total_seconds() <= 10 * 60:
                last = recent[-1]
                slip = SLIPPAGE_PCT_PER_SIDE / 100
                fill = last["c"] * (1 - slip if trade["side"] == "LONG" else 1 + slip)
                result = finish_trade(trade, fill, "EOD", last["t"])
                return {"phase": "closed", "trade": trade, "result": result}
            print("ORB " + symbol + ": stale EOD data; no invented exit", flush=True)
        return slot
    return slot


def daily_summary(day):
    ensure_results()
    with RESULTS_FILE.open(newline="", encoding="utf-8") as f:
        rows = [r for r in csv.DictReader(f) if r.get("date") == day]
    wins = sum(float(r["gross_pnl_usd"]) > 0 for r in rows)
    losses = sum(float(r["gross_pnl_usd"]) < 0 for r in rows)
    total = round(sum(float(r["gross_pnl_usd"]) for r in rows), 2)
    return len(rows), wins, losses, total


def main():
    now = now_ny()
    print("ORB V" + VERSION + " SIMULATION ONLY | NY " + iso(now)
          + " | NO ALPACA ORDERS", flush=True)
    session = get_session(now)
    if not session:
        print("No US trading session according to Alpaca calendar")
        return
    state = load_state()
    if state.get("session_date") != session["day"]:
        unresolved = [k for k, v in state["symbols"].items()
                      if v.get("phase") in {"pending", "open"}]
        if unresolved:
            notify("PREVIOUS DAY unresolved hypothetical positions: "
                   + ", ".join(unresolved) + "; NO retrospective exit assumed")
        state = new_state()
        state["session_date"] = session["day"]
        save_state(state)

    if now < session["open"] + timedelta(minutes=15):
        print("Waiting for fully closed 09:30-09:45 opening range")
        return
    candles = get_bars(SYMBOLS, session["open"], now)
    for symbol in SYMBOLS:
        bars = closed_bars(candles.get(symbol, []), session, now)
        old = state["symbols"].get(symbol, {"phase": "watching"})
        new = update_symbol(symbol, bars, session, now, old)
        state["symbols"][symbol] = new
        save_state(state)
    if now >= session["close"] and not state["daily_summary_sent"]:
        count, wins, losses, pnl = daily_summary(session["day"])
        pending = [sym for sym, x in state["symbols"].items()
                   if x.get("phase") in {"open", "pending"}]
        notify("DAILY SUMMARY " + session["day"] + ": "
               + str(count) + " virtual trades; wins=" + str(wins)
               + ", losses=" + str(losses) + ", hypothetical gross PnL="
               + str(pnl) + " USD; unresolved=" + str(pending))
        state["daily_summary_sent"] = True
        save_state(state)


if __name__ == "__main__":
    try:
        if "--self-check" in sys.argv:
            assert VERSION == "1.0" and len(SYMBOLS) == 28
            print("ORB V1.0 simulation-only structure OK")
        elif "--close-epoch" in sys.argv:
            session = get_session(now_ny())
            print(int(session["close"].timestamp()) if session else 0)
        else:
            main()
    except Exception as exc:
        notify("CRITICAL: " + str(exc) + "; signal simulation paused")
        raise
