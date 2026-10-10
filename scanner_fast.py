
import os
import requests
import math
from datetime import datetime, time
from zoneinfo import ZoneInfo

# ============================================================
# FAST BREAKOUT SCANNER - V2.6 DIAGNOSTIC
# Alpaca PAPER Trading only
# ============================================================

ALPACA_BASE_URL = "https://paper-api.alpaca.markets/v2"
DATA_URL = "https://data.alpaca.markets/v2/stocks/bars"

ALPACA_API_KEY = os.getenv("ALPACA_API_KEY")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

# ============================================================
# WATCHLIST
# Selected from the last run's $10-$50 price range
# Prices change; the price filter is checked on every run.
# ============================================================

SYMBOLS = [
    "SOFI",
    "RIVN",
    "F",
    "T",
    "PFE",
    "AAL",
    "DKNG",
]

# ============================================================
# STRATEGY SETTINGS
# ============================================================

MIN_PRICE = 10.00
MAX_PRICE = 50.00

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

MAX_FAST_POSITIONS = 1

TRADES_FILE = "breakout_trades_fast.txt"
STATE_FILE = "breakout_state_fast.txt"
RESULTS_FILE = "breakout_results_fast.csv"

NY = ZoneInfo("America/New_York")


# ============================================================
# TIME
# ============================================================

def now_ny():
    return datetime.now(NY)


def market_is_open():
    now = now_ny()

    if now.weekday() >= 5:
        return False

    current = now.time()
    return time(9, 30) <= current < time(16, 0)


# ============================================================
# ALPACA HEADERS
# ============================================================

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


# ============================================================
# TELEGRAM
# ============================================================

def send_telegram(message):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        print("Telegram secrets not configured.")
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"

    try:
        response = requests.post(
            url,
            json={"chat_id": CHAT_ID, "text": message},
            timeout=15,
        )

        if response.status_code != 200:
            print("Telegram error:", response.text)

    except Exception as exc:
        print("Telegram exception:", exc)


# ============================================================
# ALPACA POSITIONS AND ORDERS
# ============================================================

def get_all_positions():
    response = requests.get(
        f"{ALPACA_BASE_URL}/positions",
        headers=alpaca_headers(),
        timeout=20,
    )
    response.raise_for_status()
    return response.json()


def get_fast_positions():
    return [
        position
        for position in get_all_positions()
        if position.get("symbol", "").upper() in SYMBOLS
    ]


def get_open_orders_for_fast():
    response = requests.get(
        f"{ALPACA_BASE_URL}/orders",
        headers=alpaca_headers(),
        params={"status": "open", "limit": 500},
        timeout=20,
    )
    response.raise_for_status()

    return [
        order
        for order in response.json()
        if order.get("symbol", "").upper() in SYMBOLS
    ]


# ============================================================
# MARKET DATA
# ============================================================

def get_bars(symbol, limit=100):
    response = requests.get(
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
    response.raise_for_status()

    data = response.json()
    return data.get("bars", {}).get(symbol, [])


# ============================================================
# INDICATORS
# ============================================================

def calculate_atr(bars, period=14):
    if len(bars) < period + 1:
        return None

    true_ranges = []

    for i in range(1, len(bars)):
        high = float(bars[i]["h"])
        low = float(bars[i]["l"])
        previous_close = float(bars[i - 1]["c"])

        true_ranges.append(
            max(
                high - low,
                abs(high - previous_close),
                abs(low - previous_close),
            )
        )

    if len(true_ranges) < period:
        return None

    return sum(true_ranges[-period:]) / period


def calculate_relative_volume(bars, lookback=12):
    if len(bars) < lookback + 1:
        return None

    current_volume = float(bars[-1]["v"])

    previous_volumes = [
        float(bar["v"])
        for bar in bars[-lookback - 1:-1]
    ]

    if not previous_volumes:
        return None

    average_volume = sum(previous_volumes) / len(previous_volumes)

    if average_volume <= 0:
        return None

    return current_volume / average_volume


# ============================================================
# BREAKOUT ANALYSIS WITH INDEPENDENT DIAGNOSTICS
# ============================================================

def analyze_symbol(symbol):
    bars = get_bars(symbol, 100)

    if not bars:
        print("  REJECT: No market data returned.")
        return None

    required = max(
        BREAKOUT_LOOKBACK + 2,
        ATR_PERIOD + 2,
        VOLUME_LOOKBACK + 2,
    )

    if len(bars) < required:
        print(f"  REJECT: Not enough bars ({len(bars)}; need {required}).")
        return None

    # Exclude the newest candle because it may still be forming.
    closed_bars = bars[:-1]

    latest = closed_bars[-1]
    entry = float(latest["c"])
    candle_high = float(latest["h"])
    candle_low = float(latest["l"])

    if not MIN_PRICE <= entry <= MAX_PRICE:
        print(
            f"  REJECT: PRICE OUT OF RANGE | "
            f"${entry:.2f}; allowed ${MIN_PRICE:.2f}-${MAX_PRICE:.2f}"
        )
        return None

    previous = closed_bars[-BREAKOUT_LOOKBACK - 1:-1]

    previous_high = max(float(bar["h"]) for bar in previous)
    previous_low = min(float(bar["l"]) for bar in previous)

    atr = calculate_atr(closed_bars, ATR_PERIOD)
    rel_volume = calculate_relative_volume(
        closed_bars, VOLUME_LOOKBACK
    )

    if atr is None or atr <= 0:
        print("  REJECT: ATR unavailable or invalid.")
        return None

    if rel_volume is None:
        print("  REJECT: Relative volume unavailable.")
        return None

    up_percent = (entry - previous_high) / previous_high * 100
    down_percent = (previous_low - entry) / previous_low * 100

    # Evaluate breakout and volume independently.
    long_breakout = (
        candle_high > previous_high
        and entry > previous_high
        and up_percent >= MIN_BREAKOUT_PERCENT
    )

    short_breakout = (
        candle_low < previous_low
        and entry < previous_low
        and down_percent >= MIN_BREAKOUT_PERCENT
    )

    volume_ok = rel_volume >= MIN_REL_VOLUME

    print(
        f"  DATA: Price=${entry:.2f} | "
        f"High=${previous_high:.2f} | "
        f"Low=${previous_low:.2f} | "
        f"Up={up_percent:.3f}% | "
        f"Down={down_percent:.3f}% | "
        f"RelVol={rel_volume:.2f}x | ATR=${atr:.4f}"
    )

    print(
        f"  CHECK: Breakout={'YES' if long_breakout or short_breakout else 'NO'} "
        f"(minimum {MIN_BREAKOUT_PERCENT:.2f}%) | "
        f"Volume={'YES' if volume_ok else 'NO'} "
        f"(minimum {MIN_REL_VOLUME:.2f}x)"
    )

    if not long_breakout and not short_breakout:
        if entry <= previous_high and entry >= previous_low:
            print("  REJECT: Close remained inside the previous range.")
        elif entry > previous_high:
            print("  REJECT: Upside breakout did not reach the required percentage.")
        else:
            print("  REJECT: Downside breakout did not reach the required percentage.")

    if not volume_ok:
        print(
            f"  REJECT: Relative volume {rel_volume:.2f}x "
            f"is below {MIN_REL_VOLUME:.2f}x."
        )

    if not (long_breakout or short_breakout) or not volume_ok:
        return None

    side = "buy" if long_breakout else "sell"
    breakout_level = previous_high if side == "buy" else previous_low
    breakout_percent = up_percent if side == "buy" else down_percent

    stop_distance = min(
        atr * ATR_MULTIPLIER,
        entry * MAX_STOP_PERCENT / 100,
    )

    if stop_distance <= 0:
        print("  REJECT: Invalid stop distance.")
        return None

    if side == "buy":
        stop = entry - stop_distance
        target = entry + stop_distance * RISK_REWARD
    else:
        stop = entry + stop_distance
        target = entry - stop_distance * RISK_REWARD

    print(f"  VALID SIGNAL: {side.upper()} {symbol}")

    return {
        "symbol": symbol,
        "side": side,
        "entry": entry,
        "breakout": breakout_level,
        "stop": stop,
        "target": target,
        "atr": atr,
        "rel_volume": rel_volume,
        "breakout_percent": breakout_percent,
        "bar_time": latest["t"],
    }


# ============================================================
# POSITION SIZE
# ============================================================

def calculate_quantity(entry, stop):
    risk_per_share = abs(entry - stop)

    if risk_per_share <= 0:
        return 0

    return math.floor(RISK_PER_TRADE / risk_per_share)


# ============================================================
# SUBMIT PAPER BRACKET ORDER
# ============================================================

def submit_bracket_trade(signal, quantity):
    side = signal["side"]

    payload = {
        "symbol": signal["symbol"],
        "qty": str(quantity),
        "side": side,
        "type": "market",
        "time_in_force": "day",
        "order_class": "bracket",
        "take_profit": {
            "limit_price": f"{signal['target']:.2f}"
        },
        "stop_loss": {
            "stop_price": f"{signal['stop']:.2f}"
        },
        "client_order_id": (
            f"FAST_{signal['symbol']}_"
            f"{datetime.now(NY).strftime('%Y%m%d%H%M%S')}"
        ),
    }

    response = requests.post(
        f"{ALPACA_BASE_URL}/orders",
        headers=alpaca_headers(),
        json=payload,
        timeout=20,
    )

    if response.status_code >= 400:
        print("Alpaca order error:", response.status_code, response.text)
        return None

    return response.json()


# ============================================================
# LOGGING
# ============================================================

def log_trade(signal, quantity, order):
    timestamp = now_ny().strftime("%Y-%m-%d %H:%M:%S")

    with open(TRADES_FILE, "a", encoding="utf-8") as file:
        file.write(
            f"{timestamp} | FAST | {signal['symbol']} | "
            f"{signal['side'].upper()} | Qty={quantity} | "
            f"EntryReference={signal['entry']:.2f} | "
            f"SL={signal['stop']:.2f} | TP={signal['target']:.2f} | "
            f"RelVol={signal['rel_volume']:.2f} | "
            f"ATR={signal['atr']:.4f} | OrderID={order.get('id')}\n"
        )


def send_trade_alert(signal, quantity, order):
    message = (
        "FAST ALPACA PAPER TRADE\n\n"
        f"{signal['symbol']} {signal['side'].upper()}\n"
        f"Quantity: {quantity}\n"
        f"Signal reference: ${signal['entry']:.2f}\n"
        f"Stop-loss: ${signal['stop']:.2f}\n"
        f"Take-profit: ${signal['target']:.2f}\n"
        f"Estimated risk: ${abs(signal['entry'] - signal['stop']) * quantity:.2f}\n"
        f"Relative volume: {signal['rel_volume']:.2f}x\n"
        f"Order ID: {order.get('id')}\n\n"
        "Alpaca Paper Trading"
    )
    send_telegram(message)


# ============================================================
# MAIN
# ============================================================

def main():
    print("=" * 60)
    print("FAST BREAKOUT SCANNER V2.6")
    print("Alpaca PAPER Trading")
    print("=" * 60)
    print("New York time:", now_ny().strftime("%Y-%m-%d %H:%M:%S"))

    if not market_is_open():
        print("US market is CLOSED.")
        return

    print("US market is OPEN.")

    try:
        positions = get_fast_positions()
        if positions:
            print("FAST position already exists:")
            for position in positions:
                print(
                    f"  {position.get('symbol')} "
                    f"Qty={position.get('qty')} "
                    f"AvgEntry={position.get('avg_entry_price')}"
                )
            print("No new FAST trade.")
            return

        orders = get_open_orders_for_fast()
        if orders:
            print("FAST open order already exists:")
            for order in orders:
                print(
                    f"  {order.get('symbol')} {order.get('side')} "
                    f"Status={order.get('status')}"
                )
            print("No new FAST trade.")
            return

    except Exception as exc:
        print("Could not check Alpaca positions/orders:", exc)
        return

    print(f"\nScanning {len(SYMBOLS)} FAST symbols...")
    signals = []

    for symbol in SYMBOLS:
        print(f"\nScanning {symbol}...")
        try:
            signal = analyze_symbol(symbol)
            if signal:
                signals.append(signal)
            else:
                print("  No valid breakout.")
        except Exception as exc:
            print(f"  ERROR {symbol}: {exc}")

    if not signals:
        print("\nNo FAST breakout signal found.")
        return

    signals.sort(
        key=lambda item: (
            item["rel_volume"],
            item["breakout_percent"],
        ),
        reverse=True,
    )

    signal = signals[0]
    quantity = calculate_quantity(signal["entry"], signal["stop"])

    if quantity < 1:
        print("Trade skipped: quantity would be below 1 share.")
        return

    estimated_risk = abs(signal["entry"] - signal["stop"]) * quantity

    print("\nBEST FAST SIGNAL:")
    print(
        f"{signal['symbol']} {signal['side'].upper()} | "
        f"Reference=${signal['entry']:.2f} | "
        f"SL=${signal['stop']:.2f} | TP=${signal['target']:.2f}"
    )
    print(f"Quantity: {quantity}")
    print(f"Estimated risk: ${estimated_risk:.2f}")

    print("\nSubmitting Alpaca PAPER bracket order...")
    order = submit_bracket_trade(signal, quantity)

    if not order:
        print("Trade was NOT submitted.")
        return

    print("Order submitted.")
    print("Order ID:", order.get("id"))
    print("Status:", order.get("status"))

    log_trade(signal, quantity, order)
    send_trade_alert(signal, quantity, order)

    print("FAST trade submission completed.")


if __name__ == "__main__":
    main()
        
