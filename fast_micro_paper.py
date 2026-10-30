#!/usr/bin/env python3
"""MICRO PAPER execution on a separate Alpaca PAPER account.

Reuse the classic scanner's bracket reconciliation/EOD implementation in an
isolated module instance. Virtual MICRO results remain in their original files.
No fallback to FAST's trading credentials and no LIVE endpoint are permitted.
"""
import csv
import importlib.util
import math
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import fast_micro_v1 as micro
import scanner_fast as fast

STATE_FILE = Path("fast_micro_paper_state_v1.json")
RESULTS_FILE = Path("fast_micro_paper_results_v1.csv")
ENTRY_PREFIX = "MICRO1_"
EOD_PREFIX = "MICRO1_EOD_"
ENABLED = os.getenv("ENABLE_MICRO_PAPER_TRADING", "false").lower() == "true"


def configured():
    return bool(os.getenv("MICRO_ALPACA_API_KEY", "").strip()
                and os.getenv("MICRO_ALPACA_SECRET_KEY", "").strip())


def create_engine():
    spec = importlib.util.spec_from_file_location(
        "_micro_bracket_engine", Path(__file__).with_name("scanner_v25_2.py"))
    engine = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(engine)
    engine.VERSION = "MICRO_PAPER_1"
    engine.ENTRY_PREFIX = ENTRY_PREFIX
    engine.EOD_PREFIX = EOD_PREFIX
    engine.STATE_FILE = STATE_FILE
    engine.RESULTS_FILE = RESULTS_FILE
    engine.ONE_SHOT_PAPER_TEST = False
    engine.RISK_PER_TRADE = micro.RISK_USD
    engine.RISK_REWARD = micro.REWARD_R
    engine.SYMBOLS = list(fast.SYMBOLS)

    def headers():
        if not configured():
            raise RuntimeError("MICRO PAPER account keys are missing; no fallback to FAST account")
        return {"APCA-API-KEY-ID": os.environ["MICRO_ALPACA_API_KEY"].strip(),
                "APCA-API-SECRET-KEY": os.environ["MICRO_ALPACA_SECRET_KEY"].strip(),
                "Content-Type": "application/json"}
    engine.alpaca_headers = headers
    original_telegram = engine.telegram
    engine.telegram = lambda text: original_telegram(text.replace("BREAKOUT", "FAST MICRO PAPER"))
    assert engine.ALPACA_API == "https://paper-api.alpaca.markets/v2"
    return engine


def prices(signal, live):
    """Fixed 0.50% stop/2.5R target with conservative whole-share sizing."""
    price, spread = float(live["price"]), float(live["spread"])
    if not all(math.isfinite(x) for x in (price, spread)) or price <= 0 or spread < 0:
        return None
    distance = price * micro.STOP_PCT / 100
    if signal["side"] == "buy":
        stop = math.floor((price-distance)*100 + 1e-9)/100
        target = math.ceil((price+micro.REWARD_R*(price-stop))*100 - 1e-9)/100
        if stop >= float(live["bid"]):
            return None
    elif signal["side"] == "sell":
        stop = math.ceil((price+distance)*100 - 1e-9)/100
        target = math.floor((price-micro.REWARD_R*(stop-price))*100 + 1e-9)/100
        if stop <= float(live["ask"]):
            return None
    else:
        return None
    if min(stop, target) <= 0:
        return None
    per_share = abs(price-stop) + spread + price * 2*micro.SLIP_PCT/100
    qty = min(math.floor((micro.RISK_USD+1e-9)/per_share),
              math.floor(micro.MAX_NOTIONAL_USD/price))
    projected = abs(target-price)*qty - qty*(spread+price*2*micro.SLIP_PCT/100)
    if qty < 1 or projected < micro.MIN_PROJECTED_PROFIT_USD:
        return None
    return {"reference_entry": price, "stop": stop, "tp": target,
            "qty": qty, "planned_risk": qty*per_share, "spread": spread}


def daily_guard(engine, state, session, positions):
    day = session["open"].date()
    attempts = [t for t in state["trades"].values()
                if engine.dt_parse(t["submitted"]).astimezone(engine.NY).date() == day]
    active = [t for t in state["trades"].values() if engine.active_record(t)]
    # Detect incomplete history instead of silently missing an earlier attempt.
    orders = engine.api("GET", "/orders", params={"status": "all", "limit": 500,
        "nested": "false", "after": session["open"].astimezone(timezone.utc).isoformat()})
    if not isinstance(orders, list) or len(orders) >= 500:
        raise RuntimeError("MICRO PAPER daily order history is incomplete")
    recorded = {t["client_id"] for t in attempts}
    historical = {str(o.get("client_order_id") or "") for o in orders
                  if str(o.get("client_order_id") or "").startswith(ENTRY_PREFIX)
                  and not str(o.get("client_order_id") or "").startswith(EOD_PREFIX)}
    if not historical.issubset(recorded):
        raise RuntimeError("MICRO PAPER order is missing from persisted state")
    pnl = 0.0
    if RESULTS_FILE.exists():
        with RESULTS_FILE.open(newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                closed = engine.dt_parse(row["closed_at"])
                if closed and closed.astimezone(engine.NY).date() == day:
                    value = float(row["pnl_usd"])
                    if not math.isfinite(value):
                        raise RuntimeError("Invalid MICRO PAPER realized PnL")
                    pnl += value
    for t in active:
        pos = positions.get(t["symbol"])
        if pos:
            value = float(pos["unrealized_pl"])
            if not math.isfinite(value):
                raise RuntimeError("Invalid MICRO PAPER unrealized PnL")
            pnl += min(0.0, value)
    allowed = (len(attempts) < micro.MAX_DAILY_ATTEMPTS
               and len(active) < micro.MAX_SIMULTANEOUS
               and pnl > -micro.MAX_DAILY_REALIZED_LOSS)
    return allowed, {t["symbol"] for t in attempts}


def process():
    if not configured():
        if STATE_FILE.exists():
            raise RuntimeError("MICRO PAPER state exists but account keys are unavailable")
        print("MICRO PAPER NOT CONFIGURED: set separate MICRO_ALPACA_API_KEY and MICRO_ALPACA_SECRET_KEY; virtual collection continues", flush=True)
        return
    engine = create_engine()
    state = engine.load_state()
    account = engine.api("GET", "/account")
    if not account.get("id"):
        raise RuntimeError("MICRO PAPER account identity unavailable")
    # Prove this is a different account before any broker mutation.
    primary = fast.api("GET", "/account")
    if not primary.get("id") or primary["id"] == account["id"]:
        raise RuntimeError("MICRO must use a different PAPER account from FAST")
    if state.get("account_id") not in (None, account["id"]):
        raise RuntimeError("MICRO PAPER account changed; refusing to adopt another account")
    state["account_id"] = account["id"]
    engine.save_state(state)
    session = engine.market_session_today()
    positions = engine.all_positions()
    safe = engine.reconcile(state, positions)
    engine.save_state(state)
    if session is None:
        return
    now = engine.now_ny()
    if now >= session["liquidate_at"]:
        engine.end_of_day(state, session)
        return
    if not ENABLED or not safe or not session["open"] <= now < session["entry_stop"]:
        print(f"MICRO PAPER monitor: entries_enabled={ENABLED}, protected={safe}", flush=True)
        return
    if (account.get("status") != "ACTIVE" or account.get("trading_blocked")
            or account.get("account_blocked") or not engine.clock_open()):
        return
    engine.ensure_no_untracked_classic_orders(state)
    allowed, seen = daily_guard(engine, state, session, positions)
    if not allowed:
        print("MICRO PAPER daily/position guard reached", flush=True)
        return
    signals = sorted(fast.collect_signals(), key=lambda s:(s["rel_volume"],s["breakout_percent"]), reverse=True)
    for signal in signals:
        symbol = signal["symbol"]
        if symbol in seen:
            continue
        # Reject stale signals from previous sessions or a delayed API response.
        bar = fast.parse_alpaca_time(signal.get("bar_key"))
        age = fast.signal_age_seconds(signal)
        if (bar is None or bar < session["open"].astimezone(timezone.utc)
                or age is None or not 0 <= age <= fast.MAX_SIGNAL_AGE_MINUTES*60):
            continue
        live = fast.validate_live(signal)
        if not live:
            continue
        settings = prices(signal, live)
        if not settings or not engine.eligible_asset(symbol, signal["side"].upper()):
            continue
        positions = engine.all_positions()
        orders = engine.open_orders()
        if symbol in positions or any(o.get("symbol")==symbol for o in engine.walk_orders(orders)):
            continue
        allowed, seen = daily_guard(engine, state, session, positions)
        if not allowed:
            break
        if symbol in seen or not engine.clock_open() or engine.now_ny() >= session["entry_stop"]:
            continue
        age = fast.signal_age_seconds(signal)
        if age is None or not 0 <= age <= fast.MAX_SIGNAL_AGE_MINUTES*60:
            continue
        converted = {"symbol": symbol, "side": signal["side"].upper(), "bar_time": bar}
        # The engine persists intent before POST, refuses duplicate IDs and
        # reconciles real bracket fills. An ambiguous POST stops this cycle.
        engine.send_bracket(converted, settings, state)
        safe = engine.reconcile(state, engine.all_positions())
        engine.save_state(state)
        if not safe:
            break
