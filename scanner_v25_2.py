#!/usr/bin/env python3
"""BREAKOUT V2.5.2 — Alpaca PAPER only.

10 Twelve Data 5-minute symbols; BRACKET entry with Alpaca-server SL/TP.
Planned risk <= $5 based on estimated execution and fixed stop distance;
not a guaranteed loss cap (slippage, gaps, fees and partial fills).

Security posture:
- New entries DISABLED unless ENABLE_BREAKOUT_TRADING=true in the job env.
- Attribution by B252_ client IDs and persisted order IDs, no global liquidation.
- Close only provably tracked positions 15 minutes before actual Alpaca session close.
- Fail closed on missing/mixed states, API errors, unknown same-symbol orders.
- Requires the corresponding workflow to persist both state and result files.
- No same-symbol manual/other-bot trading on a shared Alpaca account.
"""
import csv
import json
import math
import os
import sys
import time
from datetime import datetime, date, timedelta, timezone
from equity_candle_guard import verify_contiguous
from pathlib import Path
from statistics import median
from zoneinfo import ZoneInfo

import requests

VERSION = "2.5.2"
ALPACA_API = "https://paper-api.alpaca.markets/v2"
ALPACA_DATA = "https://data.alpaca.markets/v2/stocks"
TWELVE_URL = "https://api.twelvedata.com/time_series"
NY = ZoneInfo("America/New_York")
SYMBOLS = ["INTC", "AMD", "NVDA", "MRVL", "AAPL", "TSLA", "META", "AMZN", "MSFT", "GOOGL"]
INTERVAL = "5min"
BREAKOUT_LOOKBACK = 20
ATR_PERIOD = 14
ATR_MULTIPLIER = 1.5
MIN_BREAKOUT_PERCENT = 0.10
VOLUME_LOOKBACK = 12
MIN_VOLUME_BARS = 10
MIN_REL_VOLUME = 1.50
MAX_SL_PERCENT = 1.0
RISK_REWARD = 2.0
RISK_PER_TRADE = 5.0
COOLDOWN_MINUTES = 30
MAX_SIGNAL_AGE_SECONDS = 480
MAX_QUOTE_AGE_SECONDS = 45
MAX_SPREAD_PERCENT = 0.30
MAX_CHASE_PERCENT = 0.25
TWELVE_REQUEST_INTERVAL = 8
MAX_POSITION_DEVIATION_SHARES = 1e-6
ENTRY_STOP_BEFORE_CLOSE_MINUTES = 30
LIQUIDATE_BEFORE_CLOSE_MINUTES = 15
STATE_FILE = Path("breakout_state_v252.json")
RESULTS_FILE = Path("breakout_results_v252.csv")
ALLOW_ENTRY = os.getenv("ENABLE_BREAKOUT_TRADING", "false").strip().lower() == "true"
ONE_SHOT_PAPER_TEST = os.getenv("BREAKOUT_ONE_SHOT_PAPER_TEST", "false").strip().lower() == "true"
HTTP = requests.Session()
TERMINAL = {"filled", "canceled", "expired", "rejected", "done_for_day", "replaced", "stopped", "suspended"}
CLOSED = {"closed", "rejected", "canceled_no_fill"}


def now_ny():
    return datetime.now(NY)


def dt_parse(value):
    if not value:
        return None
    result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if result.tzinfo is None:
        result = result.replace(tzinfo=NY)
    return result.astimezone(timezone.utc)


def when(d):
    return d.isoformat(timespec="seconds")


def duration(seconds):
    sec = max(0, round(seconds))
    return f"{sec//3600}h {(sec//60)%60}min {sec%60}s" if sec >= 3600 else f"{sec//60}min {sec%60}s"


def alpaca_headers():
    key, secret = os.getenv("ALPACA_API_KEY"), os.getenv("ALPACA_SECRET_KEY")
    if not key or not secret:
        raise RuntimeError("Chýba ALPACA_API_KEY alebo ALPACA_SECRET_KEY")
    return {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret, "Content-Type": "application/json"}


def api(method, path, *, params=None, payload=None, allow_404=False):
    r = HTTP.request(method, ALPACA_API + path, headers=alpaca_headers(),
                     params=params, json=payload, timeout=20)
    if allow_404 and r.status_code == 404:
        return None
    if r.status_code >= 400:
        raise RuntimeError(f"Alpaca {method} {path}: HTTP {r.status_code}: {r.text[:400]}")
    return r.json() if r.content else {}


def telegram(msg):
    token, chat = os.getenv("TELEGRAM_TOKEN"), os.getenv("CHAT_ID")
    if not token or not chat:
        print("TELEGRAM NOT CONFIGURED:", msg, flush=True)
        return False
    try:
        r = HTTP.post(f"https://api.telegram.org/bot{token}/sendMessage",
                      json={"chat_id": chat, "text": msg}, timeout=12)
        if not r.ok:
            print(f"Telegram HTTP {r.status_code}: {r.text[:250]}", file=sys.stderr)
        return r.ok
    except Exception as exc:
        print(f"Telegram ERROR: {exc}", file=sys.stderr)
        return False


def load_state():
    if not STATE_FILE.exists():
        return {"version": VERSION, "trades": {}, "cooldown": {}}
    with STATE_FILE.open(encoding="utf-8") as f:
        s = json.load(f)  # Corrupt file -> raise and HALT (never assume zero positions).
    if not isinstance(s, dict) or s.get("version") != VERSION or not isinstance(s.get("trades"), dict):
        raise RuntimeError("Neplatný state V2.5.2, nové obchody ZABLOKOVANÉ")
    s.setdefault("cooldown", {})
    return s


def save_state(s):
    temp = STATE_FILE.with_suffix(".json.tmp")
    with temp.open("w", encoding="utf-8") as f:
        json.dump(s, f, ensure_ascii=False, indent=2, sort_keys=True)
    os.replace(temp, STATE_FILE)


def market_session_today():
    today = now_ny().date()
    calendar = api("GET", "/calendar", params={"start": today.isoformat(), "end": today.isoformat()})
    matching = [x for x in calendar if x.get("date") == today.isoformat()]
    if not matching:
        return None
    day = matching[0]
    close_time = datetime.strptime(day["close"], "%H:%M").time()
    open_time = datetime.strptime(day["open"], "%H:%M").time()
    market_open = datetime.combine(today, open_time, NY)
    close = datetime.combine(today, close_time, NY)
    return {"open": market_open, "close": close,
            "entry_stop": close - timedelta(minutes=ENTRY_STOP_BEFORE_CLOSE_MINUTES),
            "liquidate_at": close - timedelta(minutes=LIQUIDATE_BEFORE_CLOSE_MINUTES)}


def clock_open():
    return bool(api("GET", "/clock").get("is_open"))


def all_positions():
    return {p["symbol"]: p for p in api("GET", "/positions")}


def open_orders():
    return api("GET", "/orders", params={"status": "open", "limit": 500, "nested": "true"})


def walk_orders(records):
    for item in records:
        yield item
        yield from walk_orders(item.get("legs") or [])


def order_by_id(order_id):
    return api("GET", f"/orders/{order_id}", params={"nested": "true"}, allow_404=True)


def order_by_client(client_id):
    return api("GET", "/orders:by_client_order_id", params={"client_order_id": client_id}, allow_404=True)


def qty_of(o):
    return abs(float(o.get("filled_qty") or 0)) if o else 0.0


def same_qty(a, b):
    return abs(float(a) - float(b)) <= MAX_POSITION_DEVIATION_SHARES


def my_entry_client_id(signal):
    return f"B252_{signal['symbol']}_{signal['bar_time'].astimezone(NY).strftime('%Y%m%d%H%M')}"


def quote(symbol):
    result = HTTP.get(f"{ALPACA_DATA}/{symbol}/quotes/latest",
                      headers=alpaca_headers(), params={"feed": "iex"}, timeout=15)
    result.raise_for_status()
    q = result.json().get("quote") or {}
    bid, ask = float(q.get("bp") or 0), float(q.get("ap") or 0)
    stamp = dt_parse(q.get("t"))
    if bid <= 0 or ask <= bid or stamp is None:
        raise RuntimeError(f"{symbol}: neplatný IEX bid/ask")
    age = (datetime.now(timezone.utc) - stamp).total_seconds()
    spread_percent = (ask - bid) / ((ask + bid) / 2) * 100
    if not -10 <= age <= MAX_QUOTE_AGE_SECONDS:
        raise RuntimeError(f"{symbol}: zastaraná kotácia {age:.1f}s")
    if spread_percent > MAX_SPREAD_PERCENT:
        raise RuntimeError(f"{symbol}: IEX spread {spread_percent:.3f}% nad {MAX_SPREAD_PERCENT}%")
    return {"bid": bid, "ask": ask, "spread": ask-bid, "spread_pct": spread_percent, "age": age}


def eligible_asset(symbol, side):
    a = api("GET", f"/assets/{symbol}")
    if not a.get("tradable", False) or a.get("status") != "active":
        return False
    if side == "SELL" and not (a.get("shortable") and a.get("easy_to_borrow")):
        return False
    return True


def twelve_candles(symbol):
    key = os.getenv("TWELVE_API_KEY")
    if not key:
        raise RuntimeError("Chýba TWELVE_API_KEY")
    time.sleep(TWELVE_REQUEST_INTERVAL)
    r = HTTP.get(TWELVE_URL, params={"symbol": symbol, "interval": "5min",
                "outputsize": 120, "prepost": "false", "timezone": "America/New_York",
                "apikey": key}, timeout=20)
    r.raise_for_status()
    data = r.json()
    if data.get("status") == "error" or "values" not in data:
        raise RuntimeError(f"{symbol}: Twelve Data {data.get('message', 'no values')}")
    candles = []
    for x in data["values"]:
        dt = datetime.strptime(x["datetime"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=NY)
        candles.append({"time": dt, "o": float(x["open"]), "h": float(x["high"]),
                        "l": float(x["low"]), "c": float(x["close"]),
                        "v": float(x.get("volume") or 0)})
    candles.sort(key=lambda x: x["time"])
    now = now_ny()
    return [c for c in candles if c["time"].date() == now.date()
            and c["time"].time() >= datetime.strptime("09:30", "%H:%M").time()
            and c["time"] + timedelta(minutes=5) <= now]


def atr(candles):
    tr = [max(candles[i]["h"] - candles[i]["l"],
              abs(candles[i]["h"] - candles[i-1]["c"]),
              abs(candles[i]["l"] - candles[i-1]["c"])) for i in range(1, len(candles))]
    return sum(tr[-ATR_PERIOD:]) / ATR_PERIOD


def analyze(symbol, candles, diagnostics=None):
    """Preserve trading rules; optionally count *why* a symbol was skipped."""
    def reject(reason):
        if diagnostics is not None:
            diagnostics[reason] = diagnostics.get(reason, 0) + 1
        return None

    if len(candles) < BREAKOUT_LOOKBACK + 1:
        return reject("malo_5min_sviecok")
    # Time jumps in Twelve Data silently change what 12-bar breakout and
    # ATR(14) mean. Reject NEW entries without touching existing brackets.
    session_start=datetime.combine(now_ny().date(),datetime.min.time().replace(
                                   hour=9,minute=30),tzinfo=NY)
    good,why=verify_contiguous(
        candles,key="time",
        count=max(BREAKOUT_LOOKBACK,VOLUME_LOOKBACK,ATR_PERIOD)+1,
        session_open=session_start)
    if not good:
        print(f"{symbol}: TWELVE 5Min DATA QUALITY: {why}; no new entry",
              flush=True)
        return reject("nesuvisle_alebo_chybajuce_5min_sviecky")
    last = candles[-1]
    bar_age = (now_ny() - (last["time"] + timedelta(minutes=5))).total_seconds()
    if not -5 <= bar_age <= MAX_SIGNAL_AGE_SECONDS:
        return reject("stara_alebo_neplatna_sviecka")
    earlier = candles[:-1]
    prior = earlier[-BREAKOUT_LOOKBACK:]
    h, l = max(x["h"] for x in prior), min(x["l"] for x in prior)
    previous = [x["v"] for x in earlier[-VOLUME_LOOKBACK:] if x["v"] > 0]
    if len(previous) < MIN_VOLUME_BARS:
        return reject("malo_objemovych_sviecok")
    ref_vol = median(previous)
    relvol = last["v"] / ref_vol
    if relvol < MIN_REL_VOLUME:
        return reject("nizky_relativny_objem")
    side = None
    breakout = 0
    if last["c"] > h:
        breakout = (last["c"] / h - 1) * 100
        side = "BUY" if breakout >= MIN_BREAKOUT_PERCENT else None
    elif last["c"] < l:
        breakout = (1 - last["c"] / l) * 100
        side = "SELL" if breakout >= MIN_BREAKOUT_PERCENT else None
    if side is None:
        return reject("bez_potvrdeneho_breakoutu")
    distance = min(atr(candles) * ATR_MULTIPLIER, last["c"] * MAX_SL_PERCENT / 100)
    if distance <= 0:
        return reject("neplatny_ATR_stop")
    if diagnostics is not None:
        diagnostics["technicky_signal"] = diagnostics.get("technicky_signal", 0) + 1
    return {"symbol": symbol, "side": side, "bar_time": last["time"], "bar_age": bar_age,
            "close": last["c"], "breakout": breakout, "relvol": relvol, "atr": atr(candles),
            "sl_distance": distance}


def cents_down(x):
    return math.floor((x + 1e-9)*100) / 100


def cents_up(x):
    return math.ceil((x - 1e-9)*100) / 100


def build_trade_prices(signal, q):
    """Conservative stop/TP rounding away from the entry; use quote-side entry, not candle close."""
    side = signal["side"]
    price = q["ask"] if side == "BUY" else q["bid"]
    ref = signal["close"]
    chase = abs(price - ref) / ref * 100
    if chase > MAX_CHASE_PERCENT:
        raise RuntimeError(f"{signal['symbol']}: cena mimo signálu ({chase:.3f}%)")
    dist = signal["sl_distance"]
    if side == "BUY":
        stop = cents_down(price - dist)
        tp = cents_up(price + RISK_REWARD * (price - stop))
        if stop >= q["bid"]:
            raise RuntimeError("Stop LONG musí byť pod aktuálnym BID")
    else:
        stop = cents_up(price + dist)
        tp = cents_down(price - RISK_REWARD * (stop - price))
        if stop <= q["ask"]:
            raise RuntimeError("Stop SHORT musí byť nad aktuálnym ASK")
    if stop <= 0 or tp <= 0:
        raise RuntimeError("Neplatný SL/TP")
    dist = abs(price - stop)
    # Allow for spread and a 0.05% notional execution cushion, not a loss guarantee.
    per_share = dist + q["spread"] + price * .0005
    qty = math.floor((RISK_PER_TRADE + 1e-9) / per_share)
    if qty <= 0:
        raise RuntimeError("Rozpočet $5 je príliš malý (qty=0)")
    return {"reference_entry": price, "stop": stop, "tp": tp, "qty": qty,
            "planned_risk": qty * per_share, "spread": q["spread"], "chase": chase}


def send_bracket(signal, p, state):
    symbol = signal["symbol"]
    cid = my_entry_client_id(signal)
    if cid in (t.get("client_id") for t in state["trades"].values()):
        return False
    if ONE_SHOT_PAPER_TEST:
        if one_shot_already_consumed(state):
            print("PAPER ONE-SHOT: entry permit already consumed", flush=True)
            return False
        state["one_shot_paper_entry_attempted"] = when(datetime.now(timezone.utc))
    state["trades"][cid] = {
        "symbol": symbol, "side": signal["side"], "client_id": cid,
        "entry_id": None, "status": "submitting", "submitted": when(datetime.now(timezone.utc)),
        "signal_candle": when(signal["bar_time"]), "reference_entry": p["reference_entry"],
        "stop": p["stop"], "tp": p["tp"], "qty": p["qty"],
        "planned_risk": round(p["planned_risk"], 3), "result_logged": False}
    save_state(state)  # Persist intent before network call (also see workflow commit).
    payload = {"symbol": symbol, "qty": str(p["qty"]), "side": signal["side"].lower(),
               "type": "market", "time_in_force": "day", "order_class": "bracket",
               "take_profit": {"limit_price": f"{p['tp']:.2f}"},
               "stop_loss": {"stop_price": f"{p['stop']:.2f}"}, "client_order_id": cid}
    try:
        order = api("POST", "/orders", payload=payload)
    except Exception:
        # Ambiguous HTTP response: preserve intent; NEVER blindly resend.
        state["trades"][cid]["status"] = "verify_order"
        save_state(state)
        raise
    state["trades"][cid]["entry_id"] = order.get("id")
    state["trades"][cid]["status"] = "pending"
    save_state(state)
    age = (now_ny() - (signal["bar_time"] + timedelta(minutes=5))).total_seconds()
    telegram(f"🚀 BREAKOUT V{VERSION} VSTUPNÁ OBJEDNÁVKA\n{symbol} {signal['side']} | {p['qty']} ks\n"
             f"Referenčný vstup: ${p['reference_entry']:.2f}\nSL: ${p['stop']:.2f} | TP: ${p['tp']:.2f}\n"
             f"Plánované riziko: ${p['planned_risk']:.2f} (limit ${RISK_PER_TRADE:.2f})\n"
             f"Vek breakout signálu: {duration(age)}\nStav: {order.get('status')} – čaká na potvrdenie vykonania")
    return True


def active_record(t):
    return t.get("status") not in CLOSED


def ensure_no_untracked_classic_orders(state):
    """Fail closed for unknown active orders; recognize fully exited historic brackets.

    A lost GitHub state commit must not make an old, verified flat trade block
    the scanner forever. Never infer closure solely from the parent being filled.
    """
    after = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
    records = api("GET", "/orders", params={"status": "all", "limit": 500,
                                             "after": after, "nested": "true"})
    if not isinstance(records, list):
        raise RuntimeError("História Alpaca objednávok nie je dostupná")
    known = {t["client_id"] for t in state["trades"].values()}
    positions = all_positions()
    live = open_orders()
    extras = []
    for o in records:
        cid = str(o.get("client_order_id") or "")
        if not cid.startswith("B252_") or cid.startswith("B252_EOD_") or cid in known:
            continue
        # Query the canonical parent and its legs: historical list responses
        # can omit legs or include incomplete snapshots.
        parent = order_by_client(cid)
        if not parent or parent.get("id") != o.get("id"):
            extras.append(cid)
            continue
        parent = order_by_id(parent["id"]) or parent
        legs = parent.get("legs") or []
        entry_qty = qty_of(parent)
        exits = [leg for leg in legs if leg.get("side") != parent.get("side")]
        exit_qty = sum(qty_of(leg) for leg in exits)
        symbol = parent.get("symbol")
        has_live_order = any(
            x.get("symbol") == symbol and x.get("status") not in TERMINAL
            for x in walk_orders(live)
        )
        fully_closed = (
            parent.get("status") == "filled"
            and entry_qty > 0
            and exits
            and same_qty(exit_qty, entry_qty)
            and all(leg.get("status") in TERMINAL for leg in legs)
            and symbol not in positions
            and not has_live_order
        )
        if fully_closed:
            print(f"BREAKOUT recovered closed historical order {cid}; "
                  "not reopening or modifying Alpaca order", flush=True)
            continue
        extras.append(cid)
    if extras:
        raise RuntimeError(
            f"Neznáme alebo neuzavreté BREAKOUT objednávky: {extras[:6]} – ručná kontrola"
        )


def one_shot_already_consumed(state):
    """Conservative PAPER test permit; block repeats after restarts."""
    if state.get("one_shot_paper_entry_attempted") or state["trades"]:
        return True
    after = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
    records = api("GET", "/orders", params={"status": "all", "limit": 500,
                                             "after": after, "nested": "false"})
    if not isinstance(records, list):
        raise RuntimeError("PAPER test: no order history available")
    return any(
        str(order.get("client_order_id") or "").startswith("B252_")
        and not str(order.get("client_order_id") or "").startswith("B252_EOD_")
        and not order.get("parent_order_id")
        for order in records
    )


def get_tracked_order(t):
    order = order_by_id(t["entry_id"]) if t.get("entry_id") else None
    if order is None:
        order = order_by_client(t["client_id"])
        if order and order.get("id"):
            order = order_by_id(order["id"]) or order
    if order is not None and order.get("client_order_id") != t["client_id"]:
        raise RuntimeError(f"{t['symbol']}: order ID mismatch")
    return order


def verified_legs(parent):
    return list(parent.get("legs") or [])


def symbol_open_orders(symbol):
    # nested=true may return a FILLED parent merely to hold live bracket children.
    return [o for o in walk_orders(open_orders())
            if o.get("symbol") == symbol and o.get("status") not in TERMINAL]


def verify_bracket_protection(t, parent, current_qty):
    """Require an active server-side STOP and TAKE-PROFIT with opposite side.

    Bracket legs can be held until activated; that still counts as accepted by
    Alpaca. We do not infer protection merely because legs exist historically.
    """
    legs = verified_legs(parent)
    opposite = "sell" if t["side"] == "BUY" else "buy"
    active = [o for o in legs if str(o.get("status", "")).lower() not in TERMINAL]
    stops = [o for o in active if o.get("type") in {"stop", "stop_limit"}]
    profits = [o for o in active if o.get("type") == "limit"]
    if len(stops) != 1 or len(profits) != 1:
        raise RuntimeError(f"{t['symbol']}: bracket Stop Loss alebo Take Profit nie je aktívny")
    for leg, expected_key, expected in ((stops[0], "stop_price", t["stop"]),
                                        (profits[0], "limit_price", t["tp"])):
        if (leg.get("side", "").lower() != opposite or
            abs(float(leg.get(expected_key) or 0) - expected) > 0.011 or
            float(leg.get("qty") or 0) - qty_of(leg) + 1e-6 < current_qty):
            raise RuntimeError(f"{t['symbol']}: nevhodný ochranný order {leg.get('id')}")
    return True


def verify_owned_position(t, pos, parent):
    """Position must match remaining fills of the exact tracked entry & bracket.

    Exit legs can fill partially before EOD. Never claim ownership of an
    unrelated share; fail closed if Alpaca's net position differs.
    """
    filled = qty_of(parent)
    if filled <= 0 or not pos:
        return False
    expected_direction = 1 if t["side"] == "BUY" else -1
    position_qty = float(pos.get("qty") or 0)
    exit_qty = sum(qty_of(leg) for leg in verified_legs(parent))
    if t.get("eod_order_id"):
        eod = order_by_id(t["eod_order_id"])
        if eod:
            exit_qty += qty_of(eod)
    if t.get("external_exit_order_id"):
        ext = order_by_id(t["external_exit_order_id"])
        if ext:
            exit_qty += qty_of(ext)
    remaining = filled - exit_qty
    if (remaining <= 0 or position_qty * expected_direction <= 0
            or not same_qty(abs(position_qty), remaining)):
        raise RuntimeError(f"{t['symbol']}: net pozícia {position_qty} ks "
                           f"sa nezhoduje so zvyškom BREAKOUT fillov {remaining} ks")
    return True


def report_close_if_verified(t, parent, state):
    """Report actual realized execution prices, not simulated candle hits."""
    if t.get("status") == "closed" and t.get("result_logged"):
        return
    entry_qty = qty_of(parent)
    entry_price = float(parent.get("filled_avg_price") or 0)
    if entry_qty <= 0 or entry_price <= 0:
        return
    exit_records = verified_legs(parent)
    if t.get("eod_order_id"):
        o = order_by_id(t["eod_order_id"])
        if o:
            exit_records.append(o)
    if t.get("external_exit_order_id"):
        o = order_by_id(t["external_exit_order_id"])
        if o:
            if o.get("symbol") != t["symbol"] or o.get("side") != ("sell" if t["side"] == "BUY" else "buy"):
                raise RuntimeError(f"{t['symbol']}: unrelated external exit ID")
            exit_records.append(o)
    filled_exits = [(qty_of(o), float(o.get("filled_avg_price") or 0), o.get("filled_at"), o.get("type"))
                    for o in exit_records if qty_of(o) > 0]
    if any(p <= 0 for qty, p, _, _ in filled_exits):
        raise RuntimeError(f"{t['symbol']}: chýbajú exekučné ceny výstupov")
    total_exit_qty = sum(q for q, _, _, _ in filled_exits)
    if not same_qty(total_exit_qty, entry_qty):
        raise RuntimeError(f"{t['symbol']}: výstupy {total_exit_qty} ks != vstup {entry_qty} ks; PnL sa nehlási")
    exit_price = sum(q*p for q,p,_,_ in filled_exits)/total_exit_qty
    pnl = (exit_price-entry_price)*entry_qty*(1 if t['side']=='BUY' else -1)
    result = {"client_id": t["client_id"], "symbol": t["symbol"], "direction": t["side"],
              "qty": entry_qty, "entry": entry_price, "exit": exit_price, "pnl_usd": round(pnl, 4),
              "submitted_at": t["submitted"],
              "closed_at": max((z for _,_,z,_ in filled_exits if z), default=""),
              "exit_reason": ("MANUAL_NEXT_SESSION" if t.get("external_exit_order_id")
                              else "EOD" if t.get("eod_order_id") else "BRACKET_SL_OR_TP")}
    existing = set()
    if RESULTS_FILE.exists():
        with RESULTS_FILE.open(newline="", encoding="utf-8") as f:
            existing = {x.get("client_id") for x in csv.DictReader(f)}
    if t["client_id"] not in existing:
        first = not RESULTS_FILE.exists() or RESULTS_FILE.stat().st_size == 0
        with RESULTS_FILE.open("a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=list(result))
            if first:
                writer.writeheader()
            writer.writerow(result)
    t["status"] = "closed"
    t["result_logged"] = True
    save_state(state)
    started = dt_parse(parent.get("filled_at")) or dt_parse(t["submitted"])
    ended = dt_parse(result["closed_at"]) or datetime.now(timezone.utc)
    telegram(f"📊 BREAKOUT V{VERSION} UKONČENÝ OBCHOD\n{t['symbol']} {t['side']} | {entry_qty:g} ks\n"
             f"Skutočný vstup: ${entry_price:.4f} | výstup: ${exit_price:.4f}\n"
             f"Výsledok: ${pnl:+.2f} | trvanie: {duration((ended-started).total_seconds())}\n"
             f"Dôvod: {result['exit_reason']} (skutočné vyplnenia Alpaca)")


def reconcile(state, positions, *, is_eod=False):
    """Returns True only when all tracked trades are unambiguous and protected."""
    safe = True
    for t in state["trades"].values():
        if not active_record(t):
            continue
        symbol = t["symbol"]
        parent = get_tracked_order(t)
        if not parent:
            safe = False
            telegram(f"🚨 BREAKOUT {symbol}: objednávka {t['client_id']} sa nenašla; ručne over Alpaca")
            continue
        if t.get("entry_id") is None:
            t["entry_id"] = parent["id"]
        filled = qty_of(parent)
        pos = positions.get(symbol)
        if t.get("external_exit_order_id"):
            # A separately authorized user-requested close may be queued for
            # next session. Never submit a second SELL or treat absent bracket
            # legs as an emergency while that exact order is pending.
            ext = order_by_id(t["external_exit_order_id"])
            expected_side = "sell" if t["side"] == "BUY" else "buy"
            if not ext or ext.get("symbol") != symbol or ext.get("side") != expected_side:
                safe = False
                print(f"BREAKOUT {symbol}: manual exit ID unavailable/mismatched; NO NEW ORDER", flush=True)
                continue
            if not pos and ext.get("status") == "filled":
                try:
                    report_close_if_verified(t, parent, state)
                except Exception as exc:
                    safe = False
                    print(f"BREAKOUT {symbol}: manual exit filled but PnL unresolved: {exc}", flush=True)
                continue
            safe = False
            if ext.get("status") in {"canceled", "expired", "rejected"}:
                print(f"BREAKOUT {symbol}: manual exit {ext['status']}, needs human review", flush=True)
            else:
                print(f"BREAKOUT {symbol}: authorized manual exit {ext.get('status')} pending; NO DUPLICATE", flush=True)
            continue
        if t.get("status") == "eod_sent":
            eod = order_by_id(t.get("eod_order_id")) if t.get("eod_order_id") else order_by_client(t["eod_client_id"])
            if eod and not t.get("eod_order_id"):
                t["eod_order_id"] = eod["id"]
            if eod and eod.get("status") in {"rejected", "canceled", "expired"}:
                safe = False
                telegram(f"🚨 BREAKOUT {symbol}: EOD výstup {eod['status']} – okamžitá ručná kontrola")
                continue
            if not pos and eod and eod.get("status") == "filled":
                report_close_if_verified(t, parent, state)
            else:
                safe = False
                if not eod:
                    telegram(f"🚨 BREAKOUT {symbol}: EOD objednávku nemožno nájsť podľa klientského ID; "
                             "NEODOSIELAM druhý výstup. Over pozíciu ručne.")
            continue
        if filled <= 0:
            if parent.get("status") in {"canceled", "expired", "rejected"}:
                t["status"] = "canceled_no_fill"
                continue
            t["status"] = "pending"
            safe = False  # no other entries while this one unresolved
            continue
        if pos:
            try:
                verify_owned_position(t, pos, parent)
            except Exception as exc:
                telegram(f"🚨 BREAKOUT V{VERSION}: {exc}")
                safe = False
                continue
            if parent.get("status") != "filled":
                safe = False
                telegram(f"⚠️ BREAKOUT {symbol}: čiastočne vykonaný vstup – skontroluj")
                continue
            try:
                verify_bracket_protection(t, parent, abs(float(pos["qty"])))
            except Exception as exc:
                safe = False
                telegram(f"🚨 BREAKOUT {symbol}: ochrana nie je overená: {exc}")
                continue
            if t.get("status") != "open":
                t["status"] = "open"
                started = dt_parse(parent.get("filled_at")) or datetime.now(timezone.utc)
                telegram(f"🛡️ BREAKOUT V{VERSION} OTVORENÁ POZÍCIA\n{symbol} {t['side']} | "
                         f"{filled:g} ks\nVstup: ${float(parent.get('filled_avg_price') or 0):.4f}\n"
                         f"SL: ${t['stop']:.2f} | TP: ${t['tp']:.2f}\n"
                         f"Čakanie na vstup: {duration((started-dt_parse(t['submitted'])).total_seconds())}\n"
                         "Ochrana: bracket potvrdený v Alpaca")
        else:
            try:
                report_close_if_verified(t, parent, state)
            except Exception as exc:
                safe = False
                telegram(f"🚨 BREAKOUT {symbol}: PnL sa nedá overiť: {exc}")
            if t["status"] != "closed":
                safe = False
    return safe


def cancel_order(order):
    if order.get("status") in TERMINAL:
        return
    try:
        api("DELETE", f"/orders/{order['id']}")
    except Exception as exc:
        # A simultaneous fill/cancel is possible; recheck below.
        print(f"Cancel order {order['id']}: {exc}", file=sys.stderr)


def eod_close_trade(t, state):
    symbol = t["symbol"]
    parent = get_tracked_order(t)
    if not parent:
        raise RuntimeError("Pôvodná objednávka nedostupná; nič nezatváram naslepo")
    if t.get("external_exit_order_id"):
        return  # user-authorized next-session exit; NEVER submit duplicate
    if t.get("status") == "eod_sent":
        return  # reconcile only, don't send duplicate market exits
    if parent.get("status") != "filled":
        cancel_order(parent)  # Cancel pending entry; partial fill handled on next run.
        parent = get_tracked_order(t)
        if not parent or parent.get("status") not in TERMINAL:
            raise RuntimeError("Vstup ešte môže byť vyplnený – čakám na potvrdenie zrušenia")
    pos = all_positions().get(symbol)
    if not pos:
        return  # reconcile after protective fill or canceled entry
    verify_owned_position(t, pos, parent)
    legs = verified_legs(parent)
    if not legs:
        raise RuntimeError("Bracket legs chýbajú – ručná kontrola")
    # Only known parent and its children may be canceled. Do NOT touch other orders.
    authorized_ids = {parent['id']} | {x['id'] for x in legs}
    for o in symbol_open_orders(symbol):
        if o.get("id") not in authorized_ids:
            raise RuntimeError("Cudzia objednávka na tom istom symbole – pozíciu automaticky neuzatváram")
    for leg in legs:
        cancel_order(leg)
    # Wait for both stops to be confirmed inactive BEFORE submitting the market close.
    for _ in range(6):
        still_open = symbol_open_orders(symbol)
        if not still_open:
            break
        if any(o.get("id") not in authorized_ids for o in still_open):
            raise RuntimeError("Neznáma objednávka pri zatváraní")
        time.sleep(2)
    else:
        raise RuntimeError("SL/TP stále čakajú na zrušenie; market close ZABLOKOVANÝ")
    pos = all_positions().get(symbol)
    if not pos:
        return  # an exit filled during cancellation; reconcile next run
    verify_owned_position(t, pos, parent)
    eod_client = f"B252_EOD_{symbol}_{t['client_id'].rsplit('_', 1)[-1]}"
    t["eod_client_id"] = eod_client
    t["status"] = "eod_sent"
    save_state(state)  # crash recovery via client ID
    order = order_by_client(eod_client)
    if not order:
        order = api("POST", "/orders", payload={"symbol": symbol,
            "qty": str(abs(float(pos['qty']))), "side": "sell" if t['side']=='BUY' else "buy",
            "type": "market", "time_in_force": "day", "client_order_id": eod_client})
    t["eod_order_id"] = order['id']
    save_state(state)
    telegram(f"🌆 BREAKOUT V{VERSION} VEČERNÉ UKONČENIE\n{symbol} {t['side']}\n"
             f"Odoslaná výstupná objednávka {abs(float(pos['qty'])):g} ks, stav {order.get('status')}\n"
             "Overuje sa vykonanie; nie je to ešte potvrdené uzatvorenie.")


def end_of_day(state, session):
    print(f"BREAKOUT V{VERSION} EOD: close={session['close']}, liquidation={session['liquidate_at']}")
    for t in state["trades"].values():
        if (not active_record(t) or t.get("status") == "eod_sent"
                or t.get("external_exit_order_id")):
            continue
        try:
            eod_close_trade(t, state)
        except Exception as exc:
            telegram(f"🚨 BREAKOUT V{VERSION} {t['symbol']}: nepodarilo sa uzatvoriť EOD: {exc}. "
                     "Skontroluj Alpaca Orders a Positions okamžite!")
    positions = all_positions()
    reconcile(state, positions, is_eod=True)
    save_state(state)


def scan_for_new(state, session):
    """Record useful diagnostic data even when no new order is submitted."""
    positions = all_positions()
    blockers = {o.get('symbol') for o in walk_orders(open_orders())} | set(positions)
    blockers |= {t['symbol'] for t in state['trades'].values() if active_record(t)}
    diagnostics = {"symbols_checked": 0, "blocked_positions": 0,
                   "technical_signals": 0, "submitted": 0, "exceptions": 0}
    details = {}
    started = now_ny()
    for symbol in SYMBOLS:
        if ONE_SHOT_PAPER_TEST and state.get("one_shot_paper_entry_attempted"):
            print("PAPER ONE-SHOT: one attempt already used", flush=True)
            break
        if now_ny() >= session["entry_stop"]:
            break
        if symbol in blockers:
            diagnostics["blocked_positions"] += 1
            details[symbol] = "position_or_order_blocked"
            continue
        diagnostics["symbols_checked"] += 1
        try:
            candles = twelve_candles(symbol)
            reasons = {}
            signal = analyze(symbol, candles, reasons)
            if not signal:
                why = next(iter(reasons), "no_technical_signal")
                diagnostics[why] = diagnostics.get(why, 0) + 1
                details[symbol] = why
                continue
            diagnostics["technical_signals"] += 1
            old = dt_parse(state["cooldown"].get(symbol))
            if old and (signal['bar_time'].astimezone(timezone.utc) - old).total_seconds() < 60*COOLDOWN_MINUTES:
                diagnostics["cooldown"] = diagnostics.get("cooldown", 0) + 1
                details[symbol] = "cooldown"
                continue
            live = quote(symbol)
            settings = build_trade_prices(signal, live)
            if not eligible_asset(symbol, signal['side']):
                print(f"{symbol}: short/trade eligibility failed")
                diagnostics["asset_not_eligible"] = diagnostics.get("asset_not_eligible", 0) + 1
                details[symbol] = "asset_not_eligible"
                continue
            if (now_ny()-(signal['bar_time']+timedelta(minutes=5))).total_seconds() > MAX_SIGNAL_AGE_SECONDS:
                diagnostics["signal_expired_before_order"] = diagnostics.get("signal_expired_before_order", 0) + 1
                details[symbol] = "signal_expired_before_order"
                continue
            if symbol in all_positions() or any(o['symbol'] == symbol for o in walk_orders(open_orders())):
                diagnostics["position_changed"] = diagnostics.get("position_changed", 0) + 1
                details[symbol] = "position_changed"
                continue
            telegram(f"🔔 BREAKOUT V{VERSION} {signal['side']}\n{symbol}\n"
                     f"Breakout: {signal['breakout']:.3f}% | RelVol: {signal['relvol']:.2f}x\n"
                     f"Vek signálu: {duration((now_ny()-(signal['bar_time']+timedelta(minutes=5))).total_seconds())}\n"
                     f"Spread IEX: {live['spread_pct']:.3f}% | plánované riziko ${settings['planned_risk']:.2f}\n"
                     "Objednávka sa ešte len odosiela.")
            if send_bracket(signal, settings, state):
                state['cooldown'][symbol] = when(signal['bar_time'])
                save_state(state)
                blockers.add(symbol)
                diagnostics["submitted"] += 1
                details[symbol] = "order_submitted"
        except Exception as exc:
            diagnostics["exceptions"] += 1
            details[symbol] = str(exc)[:160]
            print(f"{symbol} SKIP: {exc}", flush=True)
            if symbol in (t['symbol'] for t in state['trades'].values()
                          if t['status'] in {'submitting','verify_order'}):
                telegram(f"🚨 BREAKOUT {symbol}: nejasný stav odoslanej objednávky! {exc}")
                break  # fail closed on uncertain order submission

    diagnostics["time_ny"] = when(now_ny())
    diagnostics["scan_seconds"] = round((now_ny()-started).total_seconds(), 1)
    day = session["open"].date().isoformat()
    state["last_scan_diagnostics"] = {
        "date": day, "counts": diagnostics, "symbols": details
    }
    # Aggregate every cycle so a Telegram end-of-day report is meaningful.
    daily = state.get("daily_diagnostics")
    if not isinstance(daily, dict) or daily.get("date") != day:
        daily = {"date": day, "scans": 0, "totals": {}}
    daily["scans"] += 1
    totals = daily["totals"]
    for key, value in diagnostics.items():
        if isinstance(value, int):
            totals[key] = totals.get(key, 0) + value
    state["daily_diagnostics"] = daily
    save_state(state)
    print(f"CLASSIC BREAKOUT SCAN DIAGNOSTIKA: {diagnostics} | podrobnosti: {details}", flush=True)


def announce_classic_started_once(state, session):
    """A real scan started, not merely a green GitHub preflight job."""
    day = session["open"].date().isoformat()
    if state.get("daily_start_notice") == day:
        return
    state["daily_start_notice"] = day
    save_state(state)
    telegram(f"📡 BREAKOUT V{VERSION}: spustené skenovanie 10 akcií, "
             f"NY {now_ny().strftime('%H:%M')}; PAPER nové vstupy povolené. "
             "Správa potvrdzuje spustenie scanneru, nie existenciu signálu.")


def send_classic_daily_diagnostics_once(state, session):
    """End-of-day visibility; never creates, cancels or modifies an Alpaca order."""
    day = session["open"].date().isoformat()
    if state.get("daily_diagnostics_notice") == day:
        return
    state["daily_diagnostics_notice"] = day
    save_state(state)
    diag = state.get("daily_diagnostics") or {}
    if diag.get("date") != day:
        telegram(f"📊 BREAKOUT V{VERSION} {day}: nebol zaznamenaný žiadny "
                 "diagnostický sken dnes; skontroluj GitHub workflow štart.")
        return
    totals = diag.get("totals", {})
    telegram(f"📊 BREAKOUT V{VERSION} {day} DIAGNOSTIKA\n"
             f"Počet cyklov: {diag.get('scans', 0)} | kontrolované symboly: {totals.get('symbols_checked', 0)}\n"
             f"Technické signály: {totals.get('technical_signals', 0)} | odoslané vstupy: {totals.get('submitted', 0)}\n"
             f"Bez breakoutu: {totals.get('bez_potvrdeneho_breakoutu', 0)} | "
             f"nízky objem: {totals.get('nizky_relativny_objem', 0)} | "
             f"málo dát: {totals.get('malo_5min_sviecok', 0)} | "
             f"staré sviečky: {totals.get('stara_alebo_neplatna_sviecka', 0)} | "
             f"výnimky: {totals.get('exceptions', 0)}.\n"
             "Ide o súčty kontrol, nie počet jedinečných obchodov.")


def main():
    print(f"BREAKOUT V{VERSION} PAPER | NY={when(now_ny())} | entry enabled={ALLOW_ENTRY} | one-shot={ONE_SHOT_PAPER_TEST}", flush=True)
    state = load_state()
    session = market_session_today()
    if session and now_ny() >= session["close"]:
        # Out-of-hours market close orders are not valid; never queue surprise exits.
        positions = all_positions()
        reconcile(state, positions)
        remaining = [t["symbol"] for t in state["trades"].values()
                     if active_record(t)]
        if remaining:
            telegram(f"🚨 BREAKOUT V{VERSION}: BURZA JE ZATVORENÁ, "
                     f"nepotvrdené pozície/objednávky: {remaining}. Ručná kontrola!")
        save_state(state)
        if session:
            send_classic_daily_diagnostics_once(state, session)
        return
    if session and now_ny() >= session["liquidate_at"]:
        end_of_day(state, session)
        send_classic_daily_diagnostics_once(state, session)
        return
    positions = all_positions()
    safe = reconcile(state, positions)
    save_state(state)
    if not ALLOW_ENTRY:
        print("SAFE MODE: no NEW classic BREAKOUT entries; reconciliation/EOD remain active", flush=True)
        return
    if not safe:
        print("UNSAFE/UNRESOLVED positions/orders, new entries blocked", flush=True)
        return
    ensure_no_untracked_classic_orders(state)
    if ONE_SHOT_PAPER_TEST and one_shot_already_consumed(state):
        print("PAPER ONE-SHOT: prior BREAKOUT attempt/order found; entry blocked", flush=True)
        return
    if not session or now_ny() < session["open"] or now_ny() >= session["entry_stop"] or not clock_open():
        print("Market outside entry window", flush=True)
        return
    announce_classic_started_once(state, session)
    scan_for_new(state, session)


def sleep_seconds():
    session = market_session_today()
    if not session:
        return 270
    remaining = (session["liquidate_at"] - now_ny()).total_seconds()
    if remaining <= 0:
        return 20
    return max(5, min(270, int(remaining)))


if __name__ == '__main__':
    try:
        if '--sleep-seconds' in sys.argv:
            print(sleep_seconds())
        elif '--close-epoch' in sys.argv:
            s = market_session_today()
            print(int(s['close'].timestamp()) if s else 0)
        else:
            main()
    except Exception as exc:
        print(f"BREAKOUT V{VERSION} CRITICAL: {exc}", file=sys.stderr, flush=True)
        if '--sleep-seconds' not in sys.argv and '--close-epoch' not in sys.argv:
            telegram(f"🚨 BREAKOUT V{VERSION} CRITICAL: {exc}\nOver Alpaca. Nové vstupy NEOTVÁRAŤ.")
        raise