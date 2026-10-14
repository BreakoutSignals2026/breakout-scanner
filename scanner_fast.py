
import os
import re
import json
import math
import requests
from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo

# FAST BREAKOUT SCANNER V2.8 - Alpaca PAPER only
# Multiple positions across different symbols are allowed.
# One best NEW signal is submitted per workflow run.
# Also monitors existing FAST positions and sends Telegram alerts.

ALPACA_BASE_URL = "https://paper-api.alpaca.markets/v2"
DATA_URL = "https://data.alpaca.markets/v2/stocks/bars"
LATEST_TRADE_URL = "https://data.alpaca.markets/v2/stocks"

ALPACA_API_KEY = os.getenv("ALPACA_API_KEY")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

SYMBOLS = [
    "SOFI", "RIVN", "F", "T", "PFE", "AAL", "ACAD", "DNLI",
    "DKNG", "NU", "VZ", "NOK", "PCG", "IREN", "REXR", "HIMS",
    "VALE", "CLSK", "CCL", "KHC", "PBR", "CTVA", "AES", "CHWY",
    "FSLY", "PATH", "U"
]

MIN_PRICE, MAX_PRICE = 10.00, 50.00
INTERVAL = "5Min"
BREAKOUT_LOOKBACK = 12
MIN_BREAKOUT_PERCENT = 0.15
VOLUME_LOOKBACK = 12
MIN_REL_VOLUME = 1.50
ATR_PERIOD = 14
ATR_MULTIPLIER = 1.50
MAX_STOP_PERCENT = 1.00
RISK_PER_TRADE = 10.00
RISK_REWARD = 2.0

TRADES_FILE = "breakout_trades_fast.txt"
STATE_FILE = "breakout_state_fast.txt"

# A recent trade is required to avoid alerts based on stale prices.
MAX_TRADE_AGE_MINUTES = 30

NY = ZoneInfo("America/New_York")
HTTP = requests.Session()


def now_ny():
    return datetime.now(NY)


def market_is_open():
    now = now_ny()
    return (
        now.weekday() < 5
        and time(9, 30) <= now.time() < time(16, 0)
    )


def alpaca_headers():
    if not ALPACA_API_KEY or not ALPACA_SECRET_KEY:
        raise RuntimeError(
            "Missing ALPACA_API_KEY or ALPACA_SECRET_KEY GitHub Secret."
        )

    return {
        "APCA-API-KEY-ID": ALPACA_API_KEY,
        "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY,
        "Content-Type": "application/json",
    }


def send_telegram(message):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        print("Telegram secrets not configured.")
        return False

    try:
        r = HTTP.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": message},
            timeout=15,
        )

        if r.status_code != 200:
            print("Telegram error:", r.text)
            return False

        return True

    except Exception as exc:
        print("Telegram exception:", exc)
        return False


def get_fast_positions():
    r = HTTP.get(
        f"{ALPACA_BASE_URL}/positions",
        headers=alpaca_headers(),
        timeout=20,
    )
    r.raise_for_status()

    watch = set(SYMBOLS)

    return [
        p for p in r.json()
        if p.get("symbol", "").upper() in watch
    ]


def get_open_orders_for_fast():
    r = HTTP.get(
        f"{ALPACA_BASE_URL}/orders",
        headers=alpaca_headers(),
        params={
            "status": "open",
            "limit": 500,
            "nested": "true",
        },
        timeout=20,
    )
    r.raise_for_status()

    watch = set(SYMBOLS)

    return [
        o for o in r.json()
        if o.get("symbol", "").upper() in watch
    ]


def get_bars(symbol, limit=100):
    r = HTTP.get(
        DATA_URL,
        headers=alpaca_headers(),
        params={
            "symbols": symbol,
            "timeframe": INTERVAL,
            "limit": limit,
            "feed": "iex",
            "adjustment": "raw",
        },
        timeout=20,
    )
    r.raise_for_status()

    return r.json().get("bars", {}).get(symbol, [])


def get_latest_trade(symbol):
    url = f"{LATEST_TRADE_URL}/{symbol}/trades/latest"

    r = HTTP.get(
        url,
        headers=alpaca_headers(),
        params={"feed": "iex"},
        timeout=20,
    )
    r.raise_for_status()

    trade = r.json().get("trade")

    if not trade:
        return None

    price = float(trade["p"])
    timestamp = trade.get("t")

    if price <= 0 or not timestamp:
        return None

    trade_time = datetime.fromisoformat(
        timestamp.replace("Z", "+00:00")
    )

    if trade_time.tzinfo is None:
        trade_time = trade_time.replace(tzinfo=timezone.utc)

    age_seconds = (
        datetime.now(timezone.utc) - trade_time
    ).total_seconds()

    if age_seconds < -60:
        print(f"{symbol}: latest trade timestamp is in the future.")
        return None

    if age_seconds > MAX_TRADE_AGE_MINUTES * 60:
        print(
            f"{symbol}: latest trade is stale "
            f"({age_seconds / 60:.1f} minutes old); "
            "no threshold alert based on this price."
        )
        return None

    return {
        "price": price,
        "time": trade_time,
        "age_seconds": max(0, age_seconds),
    }


def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            state = json.load(f)

        return state if isinstance(state, dict) else {}

    except FileNotFoundError:
        return {}

    except (OSError, json.JSONDecodeError) as exc:
        print("Warning: could not read state file:", exc)
        return {}


def save_state(state):
    tmp = STATE_FILE + ".tmp"

    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, sort_keys=True)

    os.replace(tmp, STATE_FILE)


def bar_key(value):
    digits = re.sub(r"\D", "", str(value))
    return digits[:14] if digits else "unknown"


def calculate_atr(bars, period=14):
    if len(bars) < period + 1:
        return None

    trs = []

    for i in range(1, len(bars)):
        high = float(bars[i]["h"])
        low = float(bars[i]["l"])
        prev = float(bars[i - 1]["c"])

        trs.append(
            max(
                high - low,
                abs(high - prev),
                abs(low - prev),
            )
        )

    return (
        sum(trs[-period:]) / period
        if len(trs) >= period
        else None
    )


def calculate_relative_volume(bars, lookback=12):
    if len(bars) < lookback + 1:
        return None

    current = float(bars[-1]["v"])

    previous = [
        float(b["v"])
        for b in bars[-lookback - 1:-1]
    ]

    average = sum(previous) / len(previous) if previous else 0

    return current / average if average > 0 else None


def analyze_symbol(symbol):
    bars = get_bars(symbol, 100)

    if not bars:
        print(" REJECT: No market data.")
        return None

    required = max(
        BREAKOUT_LOOKBACK + 2,
        ATR_PERIOD + 2,
        VOLUME_LOOKBACK + 2,
    )

    if len(bars) < required:
        print(
            f" REJECT: Not enough bars "
            f"({len(bars)}; need {required})."
        )
        return None

    # Exclude the currently forming candle.
    closed = bars[:-1]
    latest = closed[-1]

    entry = float(latest["c"])
    candle_high = float(latest["h"])
    candle_low = float(latest["l"])

    if not MIN_PRICE <= entry <= MAX_PRICE:
        print(
            f" REJECT: Price ${entry:.2f} outside "
            f"${MIN_PRICE:.2f}-${MAX_PRICE:.2f}."
        )
        return None

    previous = closed[-BREAKOUT_LOOKBACK - 1:-1]

    previous_high = max(float(b["h"]) for b in previous)
    previous_low = min(float(b["l"]) for b in previous)

    atr = calculate_atr(closed, ATR_PERIOD)
    rel = calculate_relative_volume(closed, VOLUME_LOOKBACK)

    if not atr or atr <= 0 or rel is None:
        print(" REJECT: ATR or relative volume unavailable.")
        return None

    up = (entry - previous_high) / previous_high * 100
    down = (previous_low - entry) / previous_low * 100

    long_ok = (
        candle_high > previous_high
        and entry > previous_high
        and up >= MIN_BREAKOUT_PERCENT
    )

    short_ok = (
        candle_low < previous_low
        and entry < previous_low
        and down >= MIN_BREAKOUT_PERCENT
    )

    volume_ok = rel >= MIN_REL_VOLUME

    print(
        f" DATA: Price=${entry:.2f} "
        f"High=${previous_high:.2f} "
        f"Low=${previous_low:.2f} "
        f"Up={up:.3f}% Down={down:.3f}% "
        f"RelVol={rel:.2f}x ATR=${atr:.4f}"
    )

    print(
        f" CHECK: Breakout="
        f"{'YES' if long_ok or short_ok else 'NO'}; "
        f"Volume={'YES' if volume_ok else 'NO'}"
    )

    if not (long_ok or short_ok) or not volume_ok:
        return None

    side = "buy" if long_ok else "sell"
    breakout_pct = up if side == "buy" else down

    distance = min(
        atr * ATR_MULTIPLIER,
        entry * MAX_STOP_PERCENT / 100,
    )

    if distance <= 0:
        return None

    if side == "buy":
        stop = entry - distance
        target = entry + distance * RISK_REWARD
    else:
        stop = entry + distance
        target = entry - distance * RISK_REWARD

    stop = round(stop, 2)
    target = round(target, 2)

    if side == "buy" and not stop < entry < target:
        print(" REJECT: Invalid rounded BUY stop/target.")
        return None

    if side == "sell" and not target < entry < stop:
        print(" REJECT: Invalid rounded SELL stop/target.")
        return None

    print(f" VALID SIGNAL: {side.upper()} {symbol}")

    return {
        "symbol": symbol,
        "side": side,
        "entry": entry,
        "breakout": (
            previous_high if side == "buy" else previous_low
        ),
        "stop": stop,
        "target": target,
        "atr": atr,
        "rel_volume": rel,
        "breakout_percent": breakout_pct,
        "bar_time": latest["t"],
    }


def calculate_quantity(entry, stop):
    distance = abs(entry - stop)

    return (
        math.floor(RISK_PER_TRADE / distance)
        if distance > 0
        else 0
    )


def submit_bracket_trade(signal, quantity):
    bar_id = bar_key(signal["bar_time"])

    payload = {
        "symbol": signal["symbol"],
        "qty": str(quantity),
        "side": signal["side"],
        "type": "market",
        "time_in_force": "gtc",
        "order_class": "bracket",
        "take_profit": {
            "limit_price": f"{signal['target']:.2f}"
        },
        "stop_loss": {
            "stop_price": f"{signal['stop']:.2f}"
        },
        "client_order_id": (
            f"FAST_{signal['symbol']}_{bar_id}"
        ),
    }

    r = HTTP.post(
        f"{ALPACA_BASE_URL}/orders",
        headers=alpaca_headers(),
        json=payload,
        timeout=20,
    )

    if r.status_code >= 400:
        print("Alpaca order error:", r.status_code, r.text)

        send_telegram(
            f"FAST PAPER ORDER ERROR\n"
            f"{signal['symbol']} {signal['side'].upper()}\n"
            f"HTTP {r.status_code}\n{r.text[:1000]}"
        )

        return None

    return r.json()


def log_trade(signal, qty, order):
    with open(TRADES_FILE, "a", encoding="utf-8") as f:
        f.write(
            f"{now_ny():%Y-%m-%d %H:%M:%S} | FAST V2.8 | "
            f"{signal['symbol']} | {signal['side'].upper()} | "
            f"Qty={qty} | EntryReference={signal['entry']:.2f} | "
            f"SL={signal['stop']:.2f} | "
            f"TP={signal['target']:.2f} | "
            f"RelVol={signal['rel_volume']:.2f} | "
            f"ATR={signal['atr']:.4f} | "
            f"Bar={signal['bar_time']} | "
            f"OrderID={order.get('id')} | "
            f"Status={order.get('status')}\n"
        )


def get_protective_levels(symbol, orders, position_side):
    """
    Extract SL and TP prices from bracket-order legs.
    Only use closing legs for the current position direction.
    """
    exit_side = "sell" if position_side == "long" else "buy"
    stop_price = None
    target_price = None

    for order in orders:
        if order.get("symbol", "").upper() != symbol:
            continue

        candidates = []

        # Nested bracket legs are the preferred source.
        if order.get("order_class") == "bracket":
            candidates.extend(order.get("legs") or [])

        # Also support APIs returning child orders separately.
        candidates.append(order)

        for leg in candidates:
            if leg.get("symbol", symbol).upper() != symbol:
                continue

            if leg.get("side", "").lower() != exit_side:
                continue

            order_type = str(leg.get("type", "")).lower()

            if order_type in ("stop", "stop_limit"):
                raw = leg.get("stop_price")
                if raw is not None:
                    stop_price = float(raw)

            elif order_type == "limit":
                raw = leg.get("limit_price")
                if raw is not None:
                    target_price = float(raw)

    return stop_price, target_price


def monitor_fast_positions(positions, orders, state):
    """
    Sends one Telegram alert per position when a fresh latest trade
    reaches the known bracket SL or TP level.

    This function DOES NOT close positions or replace Alpaca orders.
    """
    alerts = state.get("_monitor_alerts", {})

    if not isinstance(alerts, dict):
        alerts = {}

    current_symbols = {
        p.get("symbol", "").upper()
        for p in positions
    }

    changed = False

    # Clear old alert records when a FAST position has closed.
    for symbol in list(alerts.keys()):
        if symbol not in current_symbols:
            del alerts[symbol]
            changed = True

    for position in positions:
        symbol = position.get("symbol", "").upper()

        try:
            qty = float(position.get("qty", 0))
            entry_price = float(position.get("avg_entry_price", 0))
            position_side = str(
                position.get("side", "")
            ).lower()

            if position_side not in ("long", "short"):
                position_side = "long" if qty > 0 else "short"

            if not qty or entry_price <= 0:
                print(f"MONITOR {symbol}: invalid position data.")
                continue

            stop, target = get_protective_levels(
                symbol, orders, position_side
            )

            if stop is None or target is None:
                print(
                    f"MONITOR {symbol}: could not confirm both "
                    "protective bracket prices. Check Alpaca Orders."
                )
                continue

            latest = get_latest_trade(symbol)

            if latest is None:
                continue

            price = latest["price"]
            trade_time_ny = latest["time"].astimezone(NY)

            # Identify a position so a new trade can alert again.
            position_key = (
                f"{position_side}|{abs(qty)}|{entry_price:.4f}"
            )

            previous_record = alerts.get(symbol, {})

            if previous_record.get("position_key") != position_key:
                record = {
                    "position_key": position_key,
                    "stop_alerted": False,
                    "target_alerted": False,
                }
            else:
                record = previous_record

            print(
                f"MONITOR {symbol}: {position_side.upper()} "
                f"qty={abs(qty):g} latest=${price:.2f} "
                f"SL=${stop:.2f} TP=${target:.2f} "
                f"trade_time={trade_time_ny:%Y-%m-%d %H:%M:%S} NY"
            )

            if position_side == "long":
                stop_hit = price <= stop
                target_hit = price >= target
            else:
                stop_hit = price >= stop
                target_hit = price <= target

            if stop_hit and not record.get("stop_alerted", False):
                message = (
                    f"⚠️ FAST PAPER STOP-LOSS PRICE ALERT\n\n"
                    f"Symbol: {symbol}\n"
                    f"Position: {position_side.upper()}\n"
                    f"Quantity: {abs(qty):g}\n"
                    f"Entry reference: ${entry_price:.2f}\n"
                    f"Latest trade: ${price:.2f}\n"
                    f"Stop-loss level: ${stop:.2f}\n"
                    f"Take-profit level: ${target:.2f}\n"
                    f"Latest trade time: "
                    f"{trade_time_ny:%Y-%m-%d %H:%M:%S} NY\n\n"
                    "ALERT ONLY: no closing order was sent. "
                    "Check the actual position and orders in Alpaca."
                )

                if send_telegram(message):
                    record["stop_alerted"] = True
                    changed = True

            elif (
                target_hit
                and not record.get("target_alerted", False)
            ):
                message = (
                    f"🎯 FAST PAPER TAKE-PROFIT PRICE ALERT\n\n"
                    f"Symbol: {symbol}\n"
                    f"Position: {position_side.upper()}\n"
                    f"Quantity: {abs(qty):g}\n"
                    f"Entry reference: ${entry_price:.2f}\n"
                    f"Latest trade: ${price:.2f}\n"
                    f"Stop-loss level: ${stop:.2f}\n"
                    f"Take-profit level: ${target:.2f}\n"
                    f"Latest trade time: "
                    f"{trade_time_ny:%Y-%m-%d %H:%M:%S} NY\n\n"
                    "ALERT ONLY: no closing order was sent. "
                    "Check the actual position and orders in Alpaca."
                )

                if send_telegram(message):
                    record["target_alerted"] = True
                    changed = True

            if alerts.get(symbol) != record:
                alerts[symbol] = record
                changed = True

        except Exception as exc:
            print(f"MONITOR ERROR {symbol}: {exc}")

    state["_monitor_alerts"] = alerts

    if changed:
        try:
            save_state(state)
        except Exception as exc:
            print("WARNING: monitor state could not be saved:", exc)


def main():
    print("=" * 60)
    print("FAST BREAKOUT SCANNER V2.8 - ALPACA PAPER")
    print("Multiple FAST positions allowed")
    print("Position monitoring and Telegram threshold alerts enabled")
    print("=" * 60)
    print("New York time:", now_ny().strftime("%Y-%m-%d %H:%M:%S"))

    state = load_state()

    # Monitor existing positions before checking whether new trades
    # may be opened. This also runs when the market is closed.
    try:
        positions = get_fast_positions()
        orders = get_open_orders_for_fast()

        monitor_fast_positions(positions, orders, state)

    except Exception as exc:
        msg = (
            "FAST monitor could not check Alpaca positions/orders:\n"
            f"{exc}"
        )
        print(msg)
        send_telegram(msg)

        # Do not open new trades if position/order checks failed.
        return

    if not market_is_open():
        print(
            "US regular market is CLOSED. "
            "Monitoring finished; no new trades will be opened."
        )
        return

    print("US regular market is OPEN.")

    held_symbols = {
        p.get("symbol", "").upper()
        for p in positions
    }

    pending_symbols = {
        o.get("symbol", "").upper()
        for o in orders
    }

    print(f"Existing FAST-watchlist positions: {len(held_symbols)}")
    print(f"Symbols with open orders: {len(pending_symbols)}")

    if held_symbols:
        print("Open position symbols:", ", ".join(sorted(held_symbols)))

    print(f"\nScanning {len(SYMBOLS)} FAST symbols...")

    signals = []

    for symbol in SYMBOLS:
        print(f"\nScanning {symbol}...")

        if symbol in held_symbols:
            print(
                " SKIP: A position in this symbol is already open; "
                "other symbols will still be scanned."
            )
            continue

        if symbol in pending_symbols:
            print(" SKIP: An open order already exists for this symbol.")
            continue

        try:
            signal = analyze_symbol(symbol)

            if not signal:
                print(" No valid breakout.")
                continue

            key = bar_key(signal["bar_time"])

            if state.get(symbol) == key:
                print(" SKIP: This signal candle was already submitted.")
                continue

            signals.append(signal)

        except Exception as exc:
            print(f" ERROR {symbol}: {exc}")

    if not signals:
        print("\nNo new FAST breakout signal found.")
        return

    # Preserve original behaviour: one best new order per run.
    signals.sort(
        key=lambda x: (
            x["rel_volume"],
            x["breakout_percent"],
        ),
        reverse=True,
    )

    signal = signals[0]

    qty = calculate_quantity(
        signal["entry"],
        signal["stop"],
    )

    if qty < 1:
        print("Trade skipped: quantity would be below 1 share.")
        return

    risk = abs(signal["entry"] - signal["stop"]) * qty

    print("\nBEST NEW FAST SIGNAL:")
    print(
        f"{signal['symbol']} {signal['side'].upper()} | "
        f"Reference=${signal['entry']:.2f} | "
        f"SL=${signal['stop']:.2f} | "
        f"TP=${signal['target']:.2f}"
    )
    print(
        f"Quantity: {qty} | Estimated risk: ${risk:.2f} | "
        f"Candle: {signal['bar_time']}"
    )

    print("\nSubmitting Alpaca PAPER bracket order...")

    try:
        order = submit_bracket_trade(signal, qty)

    except Exception as exc:
        msg = (
            f"FAST order submission exception for "
            f"{signal['symbol']}:\n{exc}"
        )
        print(msg)
        send_telegram(msg)
        return

    if not order:
        print(
            "Trade was not confirmed as submitted; "
            "state not updated."
        )
        return

    print(
        "Order submitted. ID:",
        order.get("id"),
        "Status:",
        order.get("status"),
    )

    try:
        log_trade(signal, qty, order)
        state[signal["symbol"]] = bar_key(signal["bar_time"])
        save_state(state)

    except Exception as exc:
        print(
            "WARNING: order submitted but state/log save failed:",
            exc,
        )
        send_telegram(
            "FAST WARNING: order may already exist; "
            "scanner state/log save failed. Check Alpaca Orders "
            "before rerunning.\n"
            f"{signal['symbol']} OrderID={order.get('id')}\n{exc}"
        )

    send_telegram(
        "FAST ALPACA PAPER ORDER SUBMITTED\n\n"
        f"{signal['symbol']} {signal['side'].upper()}\n"
        f"Quantity: {qty}\n"
        f"Signal reference: ${signal['entry']:.2f}\n"
        f"Stop-loss: ${signal['stop']:.2f}\n"
        f"Take-profit: ${signal['target']:.2f}\n"
        f"Estimated risk at signal price: ${risk:.2f}\n"
        f"Relative volume: {signal['rel_volume']:.2f}x\n"
        f"Status: {order.get('status')}\n"
        f"Order ID: {order.get('id')}\n\n"
        "Paper trading. Verify entry fill and both protective "
        "orders in Alpaca."
    )

    print("FAST trade submission completed.")


if __name__ == "__main__":
    main()
