#!/usr/bin/env python3
"""FAST BREAKOUT V3.2.1 — Alpaca PAPER only, 28-symbol liquidity revision. Entry: market/day after validated 5-minute breakout. Exit: separate, opposite-side Alpaca trailing_stop/gtc (server side). Risk $5 is a sizing *estimate*, NOT a maximum guaranteed loss. IMPORTANT: ENABLE_FAST_TRADING must be explicitly set to 'true' to open NEW positions. Monitoring, recovery and pre-close liquidation of tracked FAST31 trades work while disabled. Keep GitHub Actions workflows NON-OVERLAPPING (concurrency: cancel-in-progress: false). At 15 minutes before the actual NYSE close (from the Alpaca calendar), only tracked FAST entries are liquidated. Never liquidate unrelated account holdings. A missed workflow run, network failure or late fill can leave positions open. """

import json
import math
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

VERSION = "3.2.1"
API = "https://paper-api.alpaca.markets/v2"
DATA = "https://data.alpaca.markets/v2/stocks"
NY = ZoneInfo("America/New_York")

ALPACA_API_KEY = os.getenv("ALPACA_API_KEY", "").strip()
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY", "").strip()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()
# Deliberately OFF on first deployment. Set explicitly to 'true' after checking.
ENABLE_NEW_TRADES = os.getenv("ENABLE_FAST_TRADING", "false").lower() == "true"
ONE_SHOT_PAPER_TEST = os.getenv("FAST_ONE_SHOT_PAPER_TEST", "false").lower() == "true"

# 13 existing candidates + 15 replacement candidates. Every symbol must still
# pass live 5-50 USD price, IEX-volume, quote/spread and asset checks.
SYMBOLS = [
    "SOFI", "RIVN", "DKNG", "HIMS", "CHWY", "FSLY", "PATH", "U",
    "CLSK", "IREN", "MARA", "RIOT", "CELH",
    "NU", "AAL", "CLF", "F", "VALE", "CCL", "BB", "SNAP",
    "RGTI", "QBTS", "GME", "PTON", "AA", "LUNR", "NOK",
]
assert len(SYMBOLS) == 28 and len(set(SYMBOLS)) == 28
MIN_PRICE, MAX_PRICE = 5.0, 50.0
INTERVAL = "5Min"
BREAKOUT_LOOKBACK = 12
MIN_BREAKOUT_PERCENT = 0.15
VOLUME_LOOKBACK = 12
MIN_REL_VOLUME = 1.50
ATR_PERIOD = 14
RISK_PER_TRADE = 5.00
TRAIL_PERCENT = 1.20
MAX_SIGNAL_AGE_MINUTES = 8
MAX_ENTRY_CHASE_PERCENT = 0.25
MAX_TRADE_AGE_SECONDS = 120  # Trade feed can be sparse; quote must be fresher.
MAX_QUOTE_AGE_SECONDS = 45
MAX_SPREAD_PERCENT = 0.30   # IEX-only best bid/ask, not consolidated NBBO.
MIN_SIGNAL_BAR_SHARES = 1_500
MIN_SIGNAL_BAR_TRADES = 15
MIN_SIGNAL_BAR_NOTIONAL_USD = 15_000
MIN_PRIOR_AVG_BAR_NOTIONAL_USD = 8_000
MIN_VALID_PRIOR_BARS = 9  # Out of 12 reference bars.
# Quote spread budget is included conservatively in the $5 sizing estimate.
ENTRY_WAIT_SECONDS = 25
MAX_WORKERS = 8
STATE_FILE = Path("breakout_state_fast_v31.json")
TRADES_FILE = Path("breakout_trades_fast_v31.jsonl")
ENTRY_PREFIX = "FAST31E_"
EXIT_PREFIX = "FAST31X_"
CLOSE_PREFIX = "FAST31C_"
EOD_EXIT_BEFORE_MIN = 15
NEW_ENTRY_STOP_BEFORE_MIN = 30
OPEN_STATES = {"new", "accepted", "pending_new", "accepted_for_bidding", "partially_filled", "pending_replace", "pending_cancel", "stopped", "suspended", "calculated"}
PROTECTED_STATES = {"new", "accepted", "pending_new", "accepted_for_bidding", "partially_filled", "stopped"}


def now_utc():
    return datetime.now(timezone.utc)


def stamp():
    return now_utc().isoformat(timespec="seconds")


def parse_alpaca_time(value):
    """Parse an Alpaca UTC timestamp; return None when unavailable."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def age_seconds(since, until=None):
    start = parse_alpaca_time(since)
    if start is None:
        return None
    stop = parse_alpaca_time(until) if until else now_utc()
    if stop is None:
        stop = now_utc()
    return max(0, round((stop - start).total_seconds()))


def duration(seconds):
    """Readable duration for Telegram; never mislabel unknown as 0."""
    if seconds is None:
        return "neznámy"
    seconds = max(0, int(seconds))
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours} h {minutes} min {secs} s"
    if minutes:
        return f"{minutes} min {secs} s"
    return f"{secs} s"


def signal_age_seconds(signal):
    """5-minute Alpaca bar's t is its START, not its close."""
    bar_start = parse_alpaca_time(signal.get("bar_key"))
    if bar_start is None:
        return None
    return max(0, round((now_utc() - bar_start).total_seconds() - 5 * 60))


def fnum(value, default=0.0):
    try:
        return float(value)
    except (ValueError, TypeError):
        return default


def headers():
    if not ALPACA_API_KEY or not ALPACA_SECRET_KEY:
        raise RuntimeError("Chýbajú GitHub Secrets ALPACA_API_KEY / ALPACA_SECRET_KEY")
    return {
        "APCA-API-KEY-ID": ALPACA_API_KEY,
        "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY,
        "Content-Type": "application/json",
    }


def api(method, path, *, params=None, payload=None):
    # Do not automatically retry a POST: an accepted order might otherwise double.
    r = requests.request(method, API + path, headers=headers(), params=params, json=payload, timeout=15)
    if r.status_code >= 400:
        raise RuntimeError(f"Alpaca {method} {path}: HTTP {r.status_code}: {r.text[:500]}")
    return r.json() if r.content else {}


def notify(message):
    print(f"[{stamp()}] {re.sub('<[^>]+>', '', message)}")
    if not TELEGRAM_TOKEN or not CHAT_ID:
        return
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": message, "parse_mode": "HTML"}, timeout=10,
        )
        r.raise_for_status()
    except requests.RequestException as exc:
        print(f"Telegram error: {exc}")


def load_state():
    if not STATE_FILE.exists():
        return {"version": VERSION, "trades": {}, "last_signal_bar": {}}
    # Fail CLOSED on corrupt state. Never silently reset active-trade history.
    state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    if not isinstance(state, dict) or not isinstance(state.get("trades"), dict):
        raise RuntimeError("Neplatný state file — nové obchody sú zablokované")
    state.setdefault("last_signal_bar", {})
    return state


def save_state(state):
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(STATE_FILE)


def log_event(**event):
    with TRADES_FILE.open("a", encoding="utf-8") as out:
        out.write(json.dumps({"timestamp": stamp(), **event}, ensure_ascii=False) + "\n")


def get_positions():
    return {p["symbol"]: p for p in api("GET", "/positions") if p.get("symbol") in SYMBOLS}


def get_open_orders():
    orders = api("GET", "/orders", params={"status": "open", "limit": 500, "nested": "false"})
    return [o for o in orders if o.get("symbol") in SYMBOLS]


def order_by_id(order_id):
    return api("GET", f"/orders/{order_id}")


def order_by_client_id(cid):
    # Explicit lookup for recovery from timeouts after placing orders.
    r = requests.get(API + "/orders:by_client_order_id", headers=headers(),
                     params={"client_order_id": cid}, timeout=12)
    if r.status_code == 404:
        return None
    if r.status_code >= 400:
        raise RuntimeError(f"Lookup order {cid}: HTTP {r.status_code}: {r.text[:300]}")
    return r.json()


def latest_trade(symbol):
    data = requests.get(f"{DATA}/{symbol}/trades/latest", headers=headers(),
                        params={"feed": "iex"}, timeout=12)
    data.raise_for_status()
    trade = data.json().get("trade", {})
    price = fnum(trade.get("p"))
    if price <= 0 or not trade.get("t"):
        raise RuntimeError(f"{symbol}: neplatná live cena")
    tm = datetime.fromisoformat(trade["t"].replace("Z", "+00:00"))
    age = (now_utc() - tm).total_seconds()
    if age < -10 or age > MAX_TRADE_AGE_SECONDS:
        raise RuntimeError(f"{symbol}: IEX cena stará {age:.0f}s")
    return price


def latest_quote(symbol):
    """Fail-closed IEX bid/ask and quote freshness check. IEX quotes are not the consolidated NBBO; spreads are a screening proxy. """
    r = requests.get(f"{DATA}/{symbol}/quotes/latest", headers=headers(),
                     params={"feed": "iex"}, timeout=12)
    r.raise_for_status()
    q = r.json().get("quote") or {}
    bid, ask = fnum(q.get("bp")), fnum(q.get("ap"))
    bid_size, ask_size = fnum(q.get("bs")), fnum(q.get("as"))
    timestamp = parse_alpaca_time(q.get("t"))
    if bid <= 0 or ask <= 0 or ask <= bid or bid_size <= 0 or ask_size <= 0 or timestamp is None:
        raise RuntimeError(f"{symbol}: neplatný alebo prázdny IEX bid/ask")
    age = (now_utc() - timestamp).total_seconds()
    if age < -10 or age > MAX_QUOTE_AGE_SECONDS:
        raise RuntimeError(f"{symbol}: IEX kotácia stará {age:.0f} s")
    midpoint = (bid + ask) / 2
    spread = ask - bid
    spread_pct = spread / midpoint * 100
    if spread_pct > MAX_SPREAD_PERCENT:
        raise RuntimeError(f"{symbol}: IEX spread {spread_pct:.3f}% > {MAX_SPREAD_PERCENT:.2f}%")
    return {"bid": bid, "ask": ask, "spread": spread,
            "spread_percent": spread_pct, "quote_age_seconds": age}


def asset_is_eligible(symbol, side):
    """Do not short non-shortable/HTB assets; check asset before sending order."""
    asset = api("GET", f"/assets/{symbol}")
    if not asset.get("tradable", False) or asset.get("status") != "active":
        return False, "akcia nie je aktívna alebo obchodovateľná"
    if side == "sell" and not (asset.get("shortable", False) and asset.get("easy_to_borrow", False)):
        return False, "SHORT nie je easy-to-borrow/shortable"
    return True, None


def get_bars(symbol):
    data = requests.get(DATA + "/bars", headers=headers(), params={
        "symbols": symbol, "timeframe": INTERVAL, "limit": 100,
        "feed": "iex", "adjustment": "raw",
    }, timeout=12)
    data.raise_for_status()
    return data.json().get("bars", {}).get(symbol, [])


def analyze_symbol(symbol):
    try:
        bars = get_bars(symbol)
        required = max(BREAKOUT_LOOKBACK, VOLUME_LOOKBACK, ATR_PERIOD) + 3
        if len(bars) < required:
            return None
        # Keep V3 strategy unchanged: use the last fully closed candle only.
        closed = bars[:-1]
        candle = closed[-1]
        entry_close = fnum(candle["c"])
        if not MIN_PRICE <= entry_close <= MAX_PRICE:
            return None
        candle_time = datetime.fromisoformat(candle["t"].replace("Z", "+00:00"))
        age_min = (now_utc() - candle_time).total_seconds() / 60 - 5
        if age_min < -1 or age_min > MAX_SIGNAL_AGE_MINUTES:
            return None
        prior = closed[-BREAKOUT_LOOKBACK - 1:-1]
        hi = max(fnum(b["h"]) for b in prior)
        lo = min(fnum(b["l"]) for b in prior)
        # IEX volume is a *subset* of US market volume: use it for
        # within-feed consistency only, not a claim about full-market liquidity.
        prior_bars = closed[-VOLUME_LOOKBACK - 1:-1]
        vols = [fnum(b.get("v")) for b in prior_bars if fnum(b.get("v")) > 0]
        notionals = [fnum(b.get("v")) * fnum(b.get("c")) for b in prior_bars
                     if fnum(b.get("v")) > 0 and fnum(b.get("c")) > 0]
        if len(vols) < MIN_VALID_PRIOR_BARS or len(notionals) < MIN_VALID_PRIOR_BARS:
            return None
        candle_volume = fnum(candle.get("v"))
        candle_trades = fnum(candle.get("n"))
        candle_notional = candle_volume * entry_close
        avg_prior_notional = sum(notionals) / len(notionals)
        if (candle_volume < MIN_SIGNAL_BAR_SHARES or
                candle_trades < MIN_SIGNAL_BAR_TRADES or
                candle_notional < MIN_SIGNAL_BAR_NOTIONAL_USD or
                avg_prior_notional < MIN_PRIOR_AVG_BAR_NOTIONAL_USD):
            print(f"{symbol}: IEX likvidita nedostatočná: "
                  f"5m v={candle_volume:.0f}, n={candle_trades:.0f}, "
                  f"$={candle_notional:.0f}, prior_avg_$={avg_prior_notional:.0f}")
            return None
        rel_volume = candle_volume / (sum(vols) / len(vols))
        if rel_volume < MIN_REL_VOLUME:
            return None
        if fnum(candle["h"]) > hi and entry_close > hi:
            side, level = "buy", hi
            breakout = (entry_close - hi) / hi * 100
        elif fnum(candle["l"]) < lo and entry_close < lo:
            side, level = "sell", lo
            breakout = (lo - entry_close) / lo * 100
        else:
            return None
        if breakout < MIN_BREAKOUT_PERCENT:
            return None
        return {"symbol": symbol, "side": side, "entry_close": entry_close,
                "breakout_level": level, "breakout_percent": breakout,
                "rel_volume": rel_volume, "bar_key": candle_time.isoformat(),
                "signal_bar_shares": int(candle_volume),
                "signal_bar_trades": int(candle_trades),
                "signal_bar_notional_usd": round(candle_notional, 2),
                "prior_avg_bar_notional_usd": round(avg_prior_notional, 2)}
    except Exception as exc:
        print(f"{symbol}: scanner error: {exc}")
        return None


def validate_live(signal):
    """Require a fresh trade AND fresh, narrow IEX quote before a market entry."""
    symbol = signal["symbol"]
    traded = latest_trade(symbol)
    quote = latest_quote(symbol)
    entry_reference = quote["ask"] if signal["side"] == "buy" else quote["bid"]
    ref = signal["entry_close"]
    delta = abs(entry_reference - ref) / ref * 100
    level = signal["breakout_level"]
    if not MIN_PRICE <= entry_reference <= MAX_PRICE:
        return None
    if delta > MAX_ENTRY_CHASE_PERCENT:
        return None
    # Entire quoted market must remain above/below breakout threshold.
    if signal["side"] == "buy" and (quote["bid"] <= level or traded <= level):
        return None
    if signal["side"] == "sell" and (quote["ask"] >= level or traded >= level):
        return None
    return {"price": entry_reference, "last_trade": traded, **quote, "chase_pct": delta}


def quantity_for_risk(price, spread=0.0):
    """Conservative approximation: initial trail distance PLUS current spread. Market slippage, later HWM, trading halts and gaps can exceed this amount. """
    if not math.isfinite(price) or price <= 0 or not math.isfinite(spread) or spread < 0:
        return 0
    per_share = price * TRAIL_PERCENT / 100 + spread
    return max(0, math.floor((RISK_PER_TRADE / per_share) + 1e-9))


def inverse(side):
    return "sell" if side == "buy" else "buy"


def position_matches(pos, trade):
    qty = fnum(pos.get("qty"))
    return (qty > 0 and trade["side"] == "buy") or (qty < 0 and trade["side"] == "sell")


def cancel_unfilled_entry(order):
    if order.get("status") in OPEN_STATES:
        api("DELETE", "/orders/" + order["id"])
        return False  # Wait until cancellation is confirmed on NEXT run.
    return True


def submit_exit(trade, pos, state):
    symbol = trade["symbol"]
    qty = abs(fnum(pos.get("qty")))
    if qty <= 0 or not qty.is_integer():
        raise RuntimeError(f"{symbol}: position qty is not a whole number ({qty})")
    open_for_symbol = [o for o in get_open_orders() if o.get("symbol") == symbol]
    # Must not send duplicate exits or compete with manual/legacy orders.
    if open_for_symbol:
        raise RuntimeError(f"{symbol}: iné otvorené objednávky: " +
                           ", ".join(f"{o.get('id')}:{o.get('type')}" for o in open_for_symbol))
    cid = trade["exit_cid"]
    existing = order_by_client_id(cid)
    if existing:
        trade["exit_order_id"] = existing["id"]
        save_state(state)
        if existing.get("status") in PROTECTED_STATES:
            return existing
        raise RuntimeError(f"{symbol}: existujúci exit {cid} má stav {existing.get('status')}")
    payload = {"symbol": symbol, "qty": str(int(qty)), "side": inverse(trade["side"]),
               "type": "trailing_stop", "trail_percent": str(TRAIL_PERCENT),
               "time_in_force": "gtc", "client_order_id": cid}
    # Store client id in state BEFORE POST to allow recovery after a timeout.
    save_state(state)
    result = api("POST", "/orders", payload=payload)
    trade["exit_order_id"] = result["id"]
    save_state(state)
    if result.get("status") not in PROTECTED_STATES:
        raise RuntimeError(f"{symbol}: exit not accepted: {result.get('status')}")
    entry = order_by_client_id(trade["entry_cid"])
    filled_at = (entry or {}).get("filled_at")
    opened_at = filled_at or trade.get("submitted_at")
    trade["entry_filled_at"] = filled_at
    trade["entry_fill_price"] = fnum(pos.get("avg_entry_price"))
    trade["filled_qty"] = int(qty)
    save_state(state)
    notify(f"🛡️ FAST V{VERSION} OBCHOD OTVORENÝ — OCHRANA PRIJATÁ\n"
           f"{symbol} {'LONG' if trade['side'] == 'buy' else 'SHORT'}\n"
           f"Vstup: ${trade['entry_fill_price']:.4f} | {int(qty)} ks\n"
           f"Vek vstupnej objednávky pri vyplnení: "
           f"{duration(age_seconds(trade.get('submitted_at'), filled_at))}\n"
           f"Vek otvoreného obchodu: {duration(age_seconds(opened_at))}\n"
           f"Alpaca trailing stop: {TRAIL_PERCENT:.2f}% | stav: {result['status']}\n"
           "Ochrana prijatá, nie garantovaná cena vykonania.")
    log_event(action="exit_accepted", symbol=symbol, order_id=result["id"], qty=int(qty))
    return result


def reconcile(state, positions, orders):
    """Recover fills and ensure every tracked open position has an Alpaca trailing exit. Returns True only if all tracked positions are protected and there are no unresolved entry orders. It does NOT adopt unrelated V2/V3/manual positions. """
    healthy = True
    trades = state["trades"]
    for symbol, trade in list(trades.items()):
        if trade.get("status") == "closed":
            continue
        pos = positions.get(symbol)
        entry = order_by_client_id(trade["entry_cid"])
        if not entry:
            notify(f"🚨 FAST V{VERSION} {symbol}: vstupná objednávka sa nenašla. Skontroluj Alpaca!")
            healthy = False
            continue
        trade["entry_order_id"] = entry["id"]
        status = entry.get("status")
        filled_qty = fnum(entry.get("filled_qty"))
        if status in OPEN_STATES:
            # If a prior workflow crashed, proactively cancel a stale pending
            # entry rather than leaving it alive indefinitely. A canceled
            # partially-filled order is protected on the NEXT check.
            submitted_at = trade.get("submitted_at", "")
            try:
                age = (now_utc() - datetime.fromisoformat(submitted_at)).total_seconds()
            except (ValueError, TypeError):
                age = ENTRY_WAIT_SECONDS + 60
            if status != "pending_cancel" and (filled_qty > 0 or age > ENTRY_WAIT_SECONDS + 30):
                try:
                    api("DELETE", "/orders/" + entry["id"])
                    print(f"{symbol}: cancellation requested for incomplete entry")
                except Exception as exc:
                    notify(f"🚨 FAST {symbol}: nevyplnený vstup sa nepodarilo zrušiť: {exc}")
            healthy = False
            continue
        if filled_qty <= 0:
            trade["status"] = "closed"
            save_state(state)
            log_event(action="entry_unfilled", symbol=symbol, status=status)
            continue
        if not pos:
            # Entry may have closed already. Verify with the known exit order.
            exit_order = order_by_client_id(trade["exit_cid"])
            close_order = order_by_client_id(trade["close_cid"]) if trade.get("close_cid") else None
            if close_order and close_order.get("status") == "filled":
                report_closed(trade, entry, close_order, state, "koniec seansy")
            elif exit_order and exit_order.get("status") == "filled":
                report_closed(trade, entry, exit_order, state)
            else:
                notify(f"⚠️ FAST V{VERSION} {symbol}: pozícia chýba, výstup nie je potvrdený. "
                       "Over Alpaca Activities/Orders.")
                healthy = False
            continue
        if not position_matches(pos, trade):
            notify(f"🚨 FAST V{VERSION} {symbol}: smer pozície nesedí! Nutná ručná kontrola.")
            healthy = False
            continue
        expected_qty = abs(fnum(pos.get("qty")))
        if trade.get("status") in {"eod_canceling_stop", "eod_closing"}:
            healthy = False  # EOD handler owns this trade; do not re-arm stops.
            continue
        exit_order = order_by_client_id(trade["exit_cid"])
        if exit_order and exit_order.get("status") in PROTECTED_STATES:
            trade["exit_order_id"] = exit_order["id"]
            ok = (exit_order.get("type") == "trailing_stop"
                  and exit_order.get("side") == inverse(trade["side"])
                  and abs(fnum(exit_order.get("qty")) - expected_qty) < 1e-7
                  and abs(fnum(exit_order.get("trail_percent")) - TRAIL_PERCENT) < 1e-7)
            if not ok:
                notify(f"🚨 FAST V{VERSION} {symbol}: trailing ochrana má nesprávny smer/počet/%.")
                healthy = False
            else:
                trade["status"] = "protected"
                save_state(state)
            continue
        if exit_order is not None:
            notify(f"🚨 FAST V{VERSION} {symbol}: exit má stav {exit_order.get('status')}, "
                   "ale pozícia stále existuje. SKONTROLUJ IHNEĎ!")
            healthy = False
            continue
        # If another order exists, leave it alone and do not create duplicates.
        try:
            submit_exit(trade, pos, state)
            trade["status"] = "protected"
            trade["entry_fill_price"] = fnum(pos.get("avg_entry_price"))
            trade["filled_qty"] = int(expected_qty)
            save_state(state)
        except Exception as exc:
            notify(f"🚨 FAST V{VERSION}: {symbol} NEMÁ OVERENÚ OCHRANU! {exc}. "
                   "Okamžite over/uzavri pozíciu ručne v Alpaca.")
            healthy = False

    # Do not adopt and modify other workflows' positions or V3 legacy orders.
    unknown_positions = [s for s in positions if s not in trades or trades[s].get("status") == "closed"]
    unknown_orders = [o for o in orders if o.get("symbol") not in trades or
                      trades[o["symbol"]].get("status") == "closed"]
    if unknown_positions or unknown_orders:
        details = ("Pozície: " + ", ".join(unknown_positions) if unknown_positions else "")
        if unknown_orders:
            details += (" | " if details else "") + "Objednávky: " + ", ".join(
                f"{o.get('symbol')}({o.get('type')})" for o in unknown_orders)
        notify(f"⚠️ FAST V{VERSION}: zistené nespravované obchody. "
               f"Nové vstupy blokované. {details}. "
               "Staré objednávky nemaž automaticky; over ich v Alpaca.")
        healthy = False
    return healthy


def report_closed(trade, entry, exit_order, state, reason="trailing stop"):
    """Calculate realized PnL from filled orders, including partial stop exits."""
    entry_qty = fnum(entry.get("filled_qty"))
    ep = fnum(entry.get("filled_avg_price"))
    legs = [exit_order]
    if reason == "koniec seansy":
        stop = order_by_client_id(trade["exit_cid"])
        if stop and stop.get("id") != exit_order.get("id") and fnum(stop.get("filled_qty")) > 0:
            legs.append(stop)
    qty = sum(fnum(leg.get("filled_qty")) for leg in legs)
    if entry_qty <= 0 or ep <= 0 or any(fnum(x.get("filled_avg_price")) <= 0 for x in legs) or qty <= 0:
        notify(f"⚠️ FAST V{VERSION} {trade['symbol']}: chýbajú vykonávacie ceny/množstvá. "
               "Výsledok over v Alpaca Activities.")
        return
    if abs(qty - entry_qty) > 1e-7:
        notify(f"🚨 FAST V{VERSION} {trade['symbol']}: počet výstupných akcií "
               f"({qty:g}) nie je rovnaký ako vstupný ({entry_qty:g}). "
               "Automatický PnL sa nedá spoľahlivo vyhodnotiť!")
        return
    avg_exit = sum(fnum(leg["filled_qty"]) * fnum(leg["filled_avg_price"]) for leg in legs) / qty
    pnl = (avg_exit - ep) * qty if trade["side"] == "buy" else (ep - avg_exit) * qty
    pct = pnl / (ep * qty) * 100
    trade["status"] = "closed"
    trade["realized_pnl"] = round(pnl, 2)
    save_state(state)
    log_event(action="closed", symbol=trade["symbol"], entry=ep, exit=avg_exit,
              qty=qty, side=trade["side"], pnl=round(pnl, 2), pnl_pct=round(pct, 3),
              reason=reason)
    trade_duration = duration(age_seconds(entry.get("filled_at"), exit_order.get("filled_at")))
    notify(f"📊 FAST V{VERSION} UZATVORENÝ OBCHOD\n{trade['symbol']} "
           f"{'LONG' if trade['side'] == 'buy' else 'SHORT'}\n"
           f"Vstup ${ep:.4f} → priemerný výstup ${avg_exit:.4f}\n"
           f"Trvanie obchodu: {trade_duration}\n"
           f"Množstvo {qty:g} ks | Výsledok {pnl:+.2f} USD ({pct:+.2f}%)\n"
           f"Dôvod výstupu: {reason}; výsledok pred poplatkami.")


def market_session_today():
    """Alpaca calendar includes exchange holidays and early closes. Raise rather than guess 16:00 when calendar isn't available. """
    today = now_utc().astimezone(NY).date().isoformat()
    rows = api("GET", "/calendar", params={"start": today, "end": today})
    if not rows:
        return None
    item = next((r for r in rows if r.get("date") == today), None)
    if not item or not item.get("open") or not item.get("close"):
        raise RuntimeError(f"Neplatný kalendár Alpaca pre {today}")
    def to_ny(key):
        value = str(item[key])
        parsed = datetime.fromisoformat(f"{today}T{value}")
        return parsed.replace(tzinfo=NY)
    start, end = to_ny("open"), to_ny("close")
    if not start < end or (end - start).total_seconds() > 12 * 3600:
        raise RuntimeError(f"Nereálny kalendár burzy pre {today}")
    return {"date": today, "open": start, "close": end,
            "liquidate_at": end - timedelta(minutes=EOD_EXIT_BEFORE_MIN),
            "entry_stop_at": end - timedelta(minutes=NEW_ENTRY_STOP_BEFORE_MIN)}


def eod_alert(state, symbol, key, message):
    """Report persistent EOD problems once per trade and specific condition."""
    trade = state["trades"][symbol]
    if trade.get("last_eod_alert") != key:
        trade["last_eod_alert"] = key
        save_state(state)
        notify(message)


def owned_position_ok(pos, trade, entry):
    """Only liquidate positions provably attributable to FAST31's entry. This guard will reject added shares and unknown, manual position changes. Use a dedicated Alpaca PAPER account to avoid same-symbol mixing. """
    if not position_matches(pos, trade):
        return False
    qty = abs(fnum(pos.get("qty")))
    filled = fnum(entry.get("filled_qty"))
    if not qty or not filled or qty > filled + 1e-7:
        return False
    if not trade.get("entry_cid", "").startswith(ENTRY_PREFIX):
        return False
    return bool(trade.get("exit_cid", "").startswith(EXIT_PREFIX))


def eod_close_one(state, trade, now):
    """Idempotent per-symbol EOD exit. Never touches untagged account holdings. Steps: verify owned entry -> cancel outstanding entry/stop -> confirm cancellation -> post unique tagged market/day exit -> later verify fill. """
    symbol = trade["symbol"]
    entry = order_by_client_id(trade["entry_cid"])
    if not entry or entry.get("client_order_id") != trade["entry_cid"]:
        raise RuntimeError("nedá sa overiť pôvodná FAST vstupná objednávka")
    if entry.get("status") in OPEN_STATES:
        if entry.get("status") != "pending_cancel":
            api("DELETE", f"/orders/{entry['id']}")
        raise RuntimeError("čaká sa na potvrdenie zrušenia nedokončeného vstupu")

    close_cid = trade.setdefault("close_cid", CLOSE_PREFIX + trade["entry_cid"][len(ENTRY_PREFIX):])
    save_state(state)
    pos = get_positions().get(symbol)
    protective = order_by_client_id(trade["exit_cid"])
    prior_close = order_by_client_id(close_cid)
    if prior_close:
        trade["close_order_id"] = prior_close.get("id")
        save_state(state)
        if prior_close.get("status") in OPEN_STATES:
            return "Čaká sa na vykonanie uzatváracej objednávky"
        if not pos and prior_close.get("status") == "filled":
            report_closed(trade, entry, prior_close, state, "koniec seansy")
            return "Uzatvorené"
        raise RuntimeError(f"exit objednávka už existuje ({prior_close.get('status')}); ručné preverenie")

    if not pos:
        if protective and protective.get("status") == "filled":
            report_closed(trade, entry, protective, state, "trailing stop")
            return "Uzatvorené trailing stopom"
        if fnum(entry.get("filled_qty")) == 0:
            trade["status"] = "closed"
            save_state(state)
            return "Vstup nebol vykonaný"
        raise RuntimeError("pozícia chýba, ale výstup nie je overený")

    if not owned_position_ok(pos, trade, entry):
        raise RuntimeError("pozícia nie je bezpečne overená ako FAST (smer/množstvo/pôvod)")

    # Never interfere with manually placed, legacy, or other-strategy orders.
    live_orders = [o for o in get_open_orders() if o.get("symbol") == symbol]
    allowed = {entry.get("id"), (protective or {}).get("id")}
    foreign = [o for o in live_orders if o.get("id") not in allowed]
    if foreign:
        raise RuntimeError("pri symbole sú cudzie otvorené objednávky")
    if protective and protective.get("status") in OPEN_STATES:
        if protective.get("status") != "pending_cancel":
            api("DELETE", f"/orders/{protective['id']}")
            trade["status"] = "eod_canceling_stop"
            save_state(state)
            log_event(action="eod_stop_cancel_requested", symbol=symbol, id=protective.get("id"))
        return "Čaká sa na potvrdené zrušenie trailing stopu"
    if protective and protective.get("status") not in {"canceled", "expired", "filled"}:
        raise RuntimeError(f"neznámy stav trailing stopu: {protective.get('status')}")
    if protective and protective.get("status") == "filled":
        # Position snapshot may lag behind stop execution; never double-sell.
        raise RuntimeError("trailing stop vyplnený, ale pozícia stále evidovaná")

    # Close only if the trading session remains open; no next-day queued market orders.
    if not market_open_now():
        raise RuntimeError("trh je zatvorený, neodosielam oneskorený market príkaz")
    # Re-read position/order book immediately before exit, avoiding a double close.
    pos = get_positions().get(symbol)
    if not pos:
        return "Pozícia už zmizla, čaká sa na potvrdenie výstupu"
    if not owned_position_ok(pos, trade, entry):
        raise RuntimeError("po zrušení stopu nesedí pôvod/veľkosť pozície")
    if any(o.get("symbol") == symbol for o in get_open_orders()):
        return "Čakajú ešte otvorené objednávky pre tento symbol"
    qty = abs(fnum(pos["qty"]))
    if not qty.is_integer():
        raise RuntimeError("množstvo pozície nie je celé číslo")
    payload = {"symbol": symbol, "qty": str(int(qty)), "side": inverse(trade["side"]),
               "type": "market", "time_in_force": "day", "client_order_id": close_cid}
    # Persist immutable close ID before POST; ambiguous timeouts must not duplicate exits.
    trade["status"] = "eod_closing"
    save_state(state)
    result = api("POST", "/orders", payload=payload)
    trade["close_order_id"] = result.get("id")
    save_state(state)
    log_event(action="eod_close_submitted", symbol=symbol, qty=int(qty), order_id=result.get("id"))
    notify(f"🌆 FAST V{VERSION} UKONČENIE PRED KONCOM SEANSY\n"
           f"{symbol} {'LONG' if trade['side'] == 'buy' else 'SHORT'} | {int(qty)} ks\n"
           f"Trvanie obchodu: {duration(age_seconds(entry.get('filled_at')))}\n"
           f"Odoslaný trhový výstup, čaká sa na vyplnenie v Alpaca."
           "\nOstatných pozícií na účte sa to netýka.")
    return "Exit odoslaný"


def close_fast_before_end(state, session):
    """Invoke repeatedly during last 15min; close ONLY tracked FAST31 positions."""
    now = now_utc().astimezone(NY)
    if now < session["liquidate_at"]:
        return
    trades = state["trades"]
    if not state.get("eod_notice", {}).get(session["date"]):
        notify(f"🌆 FAST V{VERSION}: KONIEC SEANSY – likvidácia vlastných FAST obchodov. "
               f"Alpaca close {session['close'].strftime('%H:%M')} NY; "
               f"cieľ {session['liquidate_at'].strftime('%H:%M')} NY. "
               "Iné stratégie a ručné pozície ostávajú nedotknuté.")
        state.setdefault("eod_notice", {})[session["date"]] = stamp()
        save_state(state)
    for symbol, trade in list(trades.items()):
        if trade.get("status") == "closed":
            continue
        try:
            outcome = eod_close_one(state, trade, now)
            print(f"EOD {symbol}: {outcome}")
        except Exception as exc:
            eod_alert(state, symbol, f"{session['date']}:{exc}",
                      f"🚨 FAST V{VERSION} EOD {symbol}: {exc}. "
                      "Pozícia môže zostať otvorená; okamžite skontroluj Alpaca!")


def market_open_now():
    # Alpaca clock handles US holidays and shortened sessions correctly.
    return bool(api("GET", "/clock").get("is_open"))


def submit_entry(signal, price, qty, state, live=None):
    symbol = signal["symbol"]
    timepart = re.sub("[^0-9]", "", signal["bar_key"])[-14:]
    cid = f"{ENTRY_PREFIX}{symbol}_{timepart}"
    exit_cid = f"{EXIT_PREFIX}{symbol}_{timepart}"
    # Persist first. No automatic duplicate POST after ambiguous HTTP response.
    state["trades"][symbol] = {
        "symbol": symbol, "side": signal["side"], "entry_cid": cid,
        "exit_cid": exit_cid, "status": "entry_pending", "reference_price": price,
        "requested_qty": qty, "bar_key": signal["bar_key"], "submitted_at": stamp(),
        "spread_at_entry": round(fnum((live or {}).get("spread")), 4),
    }
    state["last_signal_bar"][symbol] = signal["bar_key"]
    save_state(state)
    existing = order_by_client_id(cid)
    if existing:
        print(f"{symbol}: recovered existing entry {existing['id']}")
        return existing
    payload = {"symbol": symbol, "qty": str(qty), "side": signal["side"],
               "type": "market", "time_in_force": "day", "client_order_id": cid}
    result = api("POST", "/orders", payload=payload)
    state["trades"][symbol]["entry_order_id"] = result["id"]
    save_state(state)
    log_event(action="entry_submitted", symbol=symbol, order_id=result["id"],
              requested_qty=qty, reference=price, side=signal["side"],
              estimated_risk=round((price * TRAIL_PERCENT / 100 +
                                    fnum((live or {}).get("spread"))) * qty, 2),
              spread_percent=round(fnum((live or {}).get("spread_percent")), 3))
    submitted_at = result.get("submitted_at") or state["trades"][symbol]["submitted_at"]
    notify(f"🚀 FAST V{VERSION} VSTUPNÁ OBJEDNÁVKA ODOSLANÁ\n{symbol} "
           f"{'LONG' if signal['side'] == 'buy' else 'SHORT'} | {qty} ks\n"
           f"Referenčná cena ${price:.4f} | plánované riziko ~"
           f"${(price * TRAIL_PERCENT / 100 + fnum((live or {}).get('spread'))) * qty:.2f}\n"
           f"Spread IEX: {fnum((live or {}).get('spread_percent')):.3f}%\n"
           f"Vek breakout signálu: {duration(signal_age_seconds(signal))}\n"
           f"Vek objednávky: {duration(age_seconds(submitted_at))}\n"
           f"Stav objednávky: {result.get('status', 'neznámy')}\n"
           "Vyplnenie sa ešte overuje; trailing ochrana sa zadáva až po vyplnení.")
    return result


def watch_new_entry(symbol, state):
    """Immediately protect once fill is confirmed, without waiting for next cron."""
    started = time.monotonic()
    while time.monotonic() - started < ENTRY_WAIT_SECONDS:
        entry = order_by_client_id(state["trades"][symbol]["entry_cid"])
        if entry and entry.get("status") not in OPEN_STATES:
            break
        if entry and fnum(entry.get("filled_qty")) > 0:
            try:
                api("DELETE", "/orders/" + entry["id"])
            except Exception as exc:
                print(f"Partial entry cancellation: {exc}")
        time.sleep(2)
    else:
        entry = order_by_client_id(state["trades"][symbol]["entry_cid"])
        if entry and entry.get("status") in OPEN_STATES:
            try:
                api("DELETE", "/orders/" + entry["id"])
                print(f"{symbol}: entry timeout; cancellation requested")
            except Exception as exc:
                print(f"{symbol}: entry cancel failed: {exc}")
    # Poll for cancellation confirmation and position visibility.
    for _ in range(7):
        positions = get_positions()
        orders = get_open_orders()
        ok = reconcile(state, positions, orders)
        if state["trades"][symbol].get("status") in {"protected", "closed"}:
            return
        time.sleep(2)
    notify(f"🚨 FAST V{VERSION} {symbol}: ochrana NIE JE potvrdená. "
           "Skontroluj Alpaca Orders a Positions OKAMŽITE!")


def one_shot_already_consumed(state):
    """Never submit a second FAST entry during the one-shot PAPER test.

    Check both persisted local state and recent Alpaca order history. A
    missing/invalid API result fails closed. The marker is persisted before
    POST, so a failed/ambiguous submission also consumes the test permit.
    """
    if state.get("one_shot_paper_entry_attempted"):
        return True
    recent = api("GET", "/orders", params={"status": "all", "limit": 500,
                                           "nested": "false"})
    if not isinstance(recent, list):
        raise RuntimeError("PAPER test: orders history unavailable")
    return any(str(order.get("client_order_id") or "").startswith(ENTRY_PREFIX)
               for order in recent)


def main():
    print(f"FAST V{VERSION} PAPER | {stamp()} | entry enabled={ENABLE_NEW_TRADES} | one-shot={ONE_SHOT_PAPER_TEST} | "
          f"risk ${RISK_PER_TRADE:.2f}, trail {TRAIL_PERCENT:.2f}%")
    state = load_state()
    session = market_session_today()  # fail closed if API unavailable
    positions = get_positions()
    orders = get_open_orders()

    if session and now_utc().astimezone(NY) >= session["liquidate_at"]:
        # Most important: do NOT enter reconcile, which can rearm a stop
        # while the EOD handler is trying to cancel it.
        close_fast_before_end(state, session)
        save_state(state)
        return

    safe = reconcile(state, positions, orders)
    save_state(state)
    if not ENABLE_NEW_TRADES:
        print("SAFE MODE: no NEW entries; monitoring/EOD enabled")
        return
    if not safe:
        notify(f"⚠️ FAST V{VERSION}: nové obchody pozastavené: "
               "neznáme objednávky/pozície alebo neoverená ochrana")
        return
    if ONE_SHOT_PAPER_TEST and one_shot_already_consumed(state):
        print("PAPER ONE-SHOT: previous FAST entry attempt/order found; no more NEW entries")
        return
    if not session:
        print("No US trading session today")
        return
    if now_utc().astimezone(NY) >= session["entry_stop_at"]:
        print("Less than 30 minutes until real Alpaca close: no new entry")
        return
    if not market_open_now():
        print("US market is closed — no entries")
        return
    blocked = set(positions) | {o.get("symbol") for o in orders}
    blocked |= {symbol for symbol, t in state["trades"].items() if t.get("status") != "closed"}
    available = [s for s in SYMBOLS if s not in blocked]
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = [pool.submit(analyze_symbol, s) for s in available]
        signals = [f.result() for f in as_completed(futures)]
    signals = [s for s in signals if s and
               state["last_signal_bar"].get(s["symbol"]) != s["bar_key"]]
    signals.sort(key=lambda x: (x["rel_volume"], x["breakout_percent"]), reverse=True)
    for signal in signals:
        try:
            live = validate_live(signal)
            if live is None:
                continue
            price = live["price"]
            qty = quantity_for_risk(price, live["spread"])
            if qty < 1:
                print(f"{signal['symbol']}: risk budget too small")
                continue
            eligible, reason = asset_is_eligible(signal["symbol"], signal["side"])
            if not eligible:
                print(f"{signal['symbol']}: asset filtered: {reason}")
                continue
            if get_positions().get(signal["symbol"]):
                continue
            if any(o.get("symbol") == signal["symbol"] for o in get_open_orders()):
                continue
            if not market_open_now() or now_utc().astimezone(NY) >= session["entry_stop_at"]:
                return
            age = signal_age_seconds(signal)
            if age is None or age > MAX_SIGNAL_AGE_MINUTES * 60:
                print(f"{signal['symbol']}: breakout expired before order submission")
                continue
            notify(f"🔔 FAST V{VERSION} BREAKOUT "
                   f"{'BUY' if signal['side'] == 'buy' else 'SELL'}\n"
                   f"{signal['symbol']} | breakout: {signal['breakout_percent']:.3f}%\n"
                   f"Relatívny objem: {signal['rel_volume']:.2f}x\n"
                   f"Objem 5min IEX: {signal['signal_bar_shares']} akcií, "
                   f"{signal['signal_bar_trades']} obchodov\n"
                   f"Hodnota obchodov 5min IEX: ${signal['signal_bar_notional_usd']:.0f}\n"
                   f"Spread IEX: {live['spread_percent']:.3f}% "
                   f"(kotácia stará {duration(live['quote_age_seconds'])})\n"
                   f"Vek signálu: {duration(age)}\n"
                   f"Referenčná {'ASK' if signal['side']=='buy' else 'BID'} cena: ${price:.4f}\n"
                   "Signál vybraný na odoslanie; obchod ešte NIE JE otvorený.")
            if ONE_SHOT_PAPER_TEST:
                if one_shot_already_consumed(state):
                    print("PAPER ONE-SHOT already consumed before POST")
                    return
                # Persist before network POST; a timeout must not grant a second attempt.
                state["one_shot_paper_entry_attempted"] = stamp()
                save_state(state)
                notify(f"🧪 FAST V{VERSION}: jednorazový PAPER test – najviac 1 vstup. "
                       "Ďalšie nové vstupy zostanú zablokované.")
            submit_entry(signal, price, qty, state, live=live)
            watch_new_entry(signal["symbol"], state)
            return
        except Exception as exc:
            notify(f"🚨 FAST V{VERSION} {signal['symbol']}: "
                   f"výnimka pri odosielaní/ochrane: {exc}. "
                   "Over objednávky a pozície v Alpaca!")
            return
    print("No eligible breakout")


def wait_interval():
    """Aim for exactly close-15m; avoid oversleep near regular/early close. Runtime helper prints a number used by the GitHub workflow, not a trade. """
    try:
        session = market_session_today()
        if session is None:
            return 270
        remaining = (session["liquidate_at"] - now_utc().astimezone(NY)).total_seconds()
        if remaining <= 0:
            return 30  # verify liquidation repeatedly before market close
        if remaining < 300:
            return max(5, min(60, int(remaining)))
        return 270
    except Exception as exc:
        print(f"Calendar lookup failed during sleep calculation: {exc}", file=__import__('sys').stderr)
        return 30  # prefer frequent monitoring after clock failure


if __name__ == "__main__":
    import sys
    if "--sleep-seconds" in sys.argv:
        print(wait_interval())
        sys.exit(0)
    if "--close-epoch" in sys.argv:
        try:
            today = market_session_today()
            print(int(today["close"].timestamp()) if today else 0)
        except Exception as exc:
            print(f"Market calendar error: {exc}", file=sys.stderr)
            sys.exit(2)
        sys.exit(0)
    try:
        main()
    except Exception as exc:
        notify(f"🚨 FAST V{VERSION}: CRITICAL: {exc}. "
               "NOVÉ OBCHODY NEOTVÁRAŤ, over Alpaca!")
        raise
