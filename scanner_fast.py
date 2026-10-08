import os
import requests
import math
import csv
from datetime import datetime, time
from zoneinfo import ZoneInfo

# ============================================================
# FAST BREAKOUT SCANNER
# Alpaca PAPER Trading
# ============================================================

ALPACA_BASE_URL = "https://paper-api.alpaca.markets/v2"

ALPACA_API_KEY = os.getenv("ALPACA_API_KEY")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

# ------------------------------------------------------------
# FAST WATCHLIST
# ------------------------------------------------------------

SYMBOLS = [
    "SOFI",
    "PLTR",
    "SNAP",
    "RIVN",
    "NIO",
    "F",
    "T",
    "PFE",
    "BAC",
    "INTC",
    "AMD",
    "MU",
    "AAL",
    "UBER",
    "DKNG",
]

# ------------------------------------------------------------
# STRATEGY SETTINGS
# ------------------------------------------------------------

MIN_PRICE = 10.00
MAX_PRICE = 50.00

INTERVAL = "5Min"

BREAKOUT_LOOKBACK = 12
MIN_BREAKOUT_PERCENT = 0.15

VOLUME_LOOKBACK = 12
MIN_REL_VOLUME = 1.80

ATR_PERIOD = 14
ATR_MULTIPLIER = 1.50

MAX_STOP_PERCENT = 1.00

RISK_PER_TRADE = 10.00
RISK_REWARD = 2.0

COOLDOWN_MINUTES = 20

MAX_FAST_POSITIONS = 1

STATE_FILE = "breakout_state_fast.txt"
TRADES_FILE = "breakout_trades_fast.txt"
RESULTS_FILE = "breakout_results_fast.csv"


# ============================================================
# TIME
# ============================================================

NY = ZoneInfo("America/New_York")


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

    payload = {
        "chat_id": CHAT_ID,
        "text": message,
    }

    try:
        response = requests.post(
            url,
            json=payload,
            timeout=15
        )

        if response.status_code != 200:
            print("Telegram error:", response.text)

    except Exception as e:
        print("Telegram exception:", e)


# ============================================================
# ALPACA ACCOUNT
# ============================================================

def get_account():
    url = f"{ALPACA_BASE_URL}/account"

    response = requests.get(
        url,
        headers=alpaca_headers(),
        timeout=20
    )

    response.raise_for_status()

    return response.json()


# ============================================================
# ALPACA POSITIONS
# ============================================================

def get_all_positions():
    url = f"{ALPACA_BASE_URL}/positions"

    response = requests.get(
        url,
        headers=alpaca_headers(),
        timeout=20
    )

    response.raise_for_status()

    return response.json()


def get_fast_positions():
    """
    IMPORTANT:
    Only positions belonging to FAST SYMBOLS are considered.

    An unrelated position such as AAPL does NOT block FAST.
    """

    positions = get_all_positions()

    fast_positions = []

    for position in positions:
        symbol = position.get("symbol", "").upper()

        if symbol in SYMBOLS:
            fast_positions.append(position)

    return fast_positions


# ============================================================
# ALPACA OPEN ORDERS
# ============================================================

def get_open_orders_for_fast():
    url = f"{ALPACA_BASE_URL}/orders"

    params = {
        "status": "open",
        "limit": 500,
    }

    response = requests.get(
        url,
        headers=alpaca_headers(),
        params=params,
        timeout=20
    )

    response.raise_for_status()

    orders = response.json()

    fast_orders = []

    for order in orders:
        symbol = order.get("symbol", "").upper()

        if symbol in SYMBOLS:
            fast_orders.append(order)

    return fast_orders


# ============================================================
# ALPACA MARKET DATA
# ============================================================

def get_bars(symbol, limit=100):
    """
    Gets 5-minute historical bars from Alpaca.
    """

    url = "https://data.alpaca.markets/v2/stocks/bars"

    params = {
        "symbols": symbol,
        "timeframe": INTERVAL,
        "limit": limit,
        "feed": "iex",
        "adjustment": "raw",
    }

    headers = {
        "APCA-API-KEY-ID": ALPACA_API_KEY,
        "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY,
    }

    response = requests.get(
        url,
        headers=headers,
        params=params,
        timeout=20
    )

    response.raise_for_status()

    data = response.json()

    bars = data.get("bars", {}).get(symbol, [])

    return bars


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

        tr = max(
            high - low,
            abs(high - previous_close),
            abs(low - previous_close)
        )

        true_ranges.append(tr)

    if len(true_ranges) < period:
        return None

    return sum(true_ranges[-period:]) / period


def calculate_relative_volume(bars, lookback=12):
    if len(bars) < lookback + 1:
        return None

    current_volume = float(bars[-1]["v"])

    previous_volumes = [
        float(bar["v"])
        for bar in bars[-lookback-1:-1]
    ]

    if not previous_volumes:
        return None

    average_volume = sum(previous_volumes) / len(previous_volumes)

    if average_volume <= 0:
        return None

    return current_volume / average_volume


# ============================================================
# BREAKOUT DETECTION
# ============================================================

def analyze_symbol(symbol):
    bars = get_bars(symbol, 100)

    if len(bars) < max(
        BREAKOUT_LOOKBACK + 2,
        ATR_PERIOD + 2,
        VOLUME_LOOKBACK + 2
    ):
        return None

    # Ignore currently forming candle.
    closed_bars = bars[:-1]

    if len(closed_bars) < BREAKOUT_LOOKBACK + 1:
        return None

    latest = closed_bars[-1]

    entry_price = float(latest["c"])
    candle_high = float(latest["h"])
    candle_low = float(latest["l"])

    # --------------------------------------------------------
    # PRICE FILTER
    # --------------------------------------------------------

    if entry_price < MIN_PRICE or entry_price > MAX_PRICE:
        return None

    # --------------------------------------------------------
    # PREVIOUS RANGE
    # --------------------------------------------------------

    previous = closed_bars[-BREAKOUT_LOOKBACK-1:-1]

    previous_high = max(
        float(bar["h"])
        for bar in previous
    )

    previous_low = min(
        float(bar["l"])
        for bar in previous
    )

    # --------------------------------------------------------
    # ATR
    # --------------------------------------------------------

    atr = calculate_atr(
        closed_bars,
        ATR_PERIOD
    )

    if atr is None or atr <= 0:
        return None

    # --------------------------------------------------------
    # RELATIVE VOLUME
    # --------------------------------------------------------

    rel_volume = calculate_relative_volume(
        closed_bars,
        VOLUME_LOOKBACK
    )

    if rel_volume is None:
        return None

    if rel_volume < MIN_REL_VOLUME:
        return None

    # --------------------------------------------------------
    # LONG BREAKOUT
    # --------------------------------------------------------

    long_breakout_percent = (
        (entry_price - previous_high)
        / previous_high
    ) * 100

    if (
        candle_high > previous_high
        and entry_price > previous_high
        and long_breakout_percent >= MIN_BREAKOUT_PERCENT
    ):

        stop_distance = atr * ATR_MULTIPLIER

        max_stop_distance = (
            entry_price * MAX_STOP_PERCENT / 100
        )

        stop_distance = min(
            stop_distance,
            max_stop_distance
        )

        if stop_distance <= 0:
            return None

        stop_price = entry_price - stop_distance

        target_price = (
            entry_price
            + stop_distance * RISK_REWARD
        )

        return {
            "symbol": symbol,
            "side": "buy",
            "entry": entry_price,
            "breakout": previous_high,
            "stop": stop_price,
            "target": target_price,
            "atr": atr,
            "rel_volume": rel_volume,
            "breakout_percent": long_breakout_percent,
            "bar_time": latest["t"],
        }

    # --------------------------------------------------------
    # SHORT BREAKOUT
    # --------------------------------------------------------

    short_breakout_percent = (
        (previous_low - entry_price)
        / previous_low
    ) * 100

    if (
        candle_low < previous_low
        and entry_price < previous_low
        and short_breakout_percent >= MIN_BREAKOUT_PERCENT
    ):

        stop_distance = atr * ATR_MULTIPLIER

        max_stop_distance = (
            entry_price * MAX_STOP_PERCENT / 100
        )

        stop_distance = min(
            stop_distance,
            max_stop_distance
        )

        if stop_distance <= 0:
            return None

        stop_price = entry_price + stop_distance

        target_price = (
            entry_price
            - stop_distance * RISK_REWARD
        )

        return {
            "symbol": symbol,
            "side": "sell",
            "entry": entry_price,
            "breakout": previous_low,
            "stop": stop_price,
            "target": target_price,
            "atr": atr,
            "rel_volume": rel_volume,
            "breakout_percent": short_breakout_percent,
            "bar_time": latest["t"],
        }

    return None


# ============================================================
# POSITION SIZE
# ============================================================

def calculate_quantity(entry, stop):
    risk_per_share = abs(entry - stop)

    if risk_per_share <= 0:
        return 0

    quantity = math.floor(
        RISK_PER_TRADE / risk_per_share
    )

    if quantity < 1:
        return 0

    return quantity


# ============================================================
# ALPACA BRACKET ORDER
# ============================================================

def submit_bracket_trade(signal):
    symbol = signal["symbol"]
    side = signal["side"]

    entry = signal["entry"]
    stop = signal["stop"]
    target = signal["target"]

    quantity = calculate_quantity(
        entry,
        stop
    )

    if quantity < 1:
        print(
            f"{symbol}: risk distance too large "
            f"for ${RISK_PER_TRADE:.2f} risk."
        )
        return None

    # --------------------------------------------------------
    # BUY BRACKET
    # --------------------------------------------------------

    if side == "buy":

        order_side = "buy"

        take_profit = {
            "limit_price": f"{target:.2f}"
        }

        stop_loss = {
            "stop_price": f"{stop:.2f}"
        }

    # --------------------------------------------------------
    # SELL / SHORT BRACKET
    # --------------------------------------------------------

    else:

        order_side = "sell"

        take_profit = {
            "limit_price": f"{target:.2f}"
        }

        stop_loss = {
            "stop_price": f"{stop:.2f}"
        }

    payload = {
        "symbol": symbol,
        "qty": str(quantity),
        "side": order_side,
        "type": "market",
        "time_in_force": "day",
        "order_class": "bracket",
        "take_profit": take_profit,
        "stop_loss": stop_loss,
        "client_order_id": (
            f"FAST_{symbol}_"
            f"{datetime.now().strftime('%Y%m%d%H%M%S')}"
        ),
    }

    url = f"{ALPACA_BASE_URL}/orders"

    response = requests.post(
        url,
        headers=alpaca_headers(),
        json=payload,
        timeout=20
    )

    if response.status_code >= 400:
        print(
            "Alpaca order error:",
            response.status_code,
            response.text
        )
        return None

    return response.json()


# ============================================================
# LOG TRADE
# ============================================================

def log_trade(signal, quantity, order):
    timestamp = now_ny().strftime(
        "%Y-%m-%d %H:%M:%S"
    )

    with open(
        TRADES_FILE,
        "a",
        encoding="utf-8"
    ) as f:

        f.write(
            f"{timestamp} | "
            f"FAST | "
            f"{signal['symbol']} | "
            f"{signal['side'].upper()} | "
            f"Qty={quantity} | "
            f"Entry={signal['entry']:.2f} | "
            f"SL={signal['stop']:.2f} | "
            f"TP={signal['target']:.2f} | "
            f"RelVol={signal['rel_volume']:.2f} | "
            f"ATR={signal['atr']:.4f} | "
            f"OrderID={order.get('id')}\n"
        )


# ============================================================
# TELEGRAM TRADE MESSAGE
# ============================================================

def send_trade_alert(signal, quantity, order):
    message = (
        "⚡ FAST ALPACA PAPER TRADE\n\n"
        f"{signal['symbol']} "
        f"{signal['side'].upper()}\n"
        f"Qty: {quantity}\n"
        f"Entry: {signal['entry']:.2f}\n"
        f"Breakout: {signal['breakout']:.2f}\n"
        f"SL: {signal['stop']:.2f}\n"
        f"TP: {signal['target']:.2f}\n"
        f"Risk: ${RISK_PER_TRADE:.2f}\n"
        f"R:R: 1:{RISK_REWARD:.0f}\n"
        f"RelVol: {signal['rel_volume']:.2f}\n"
        f"ATR: {signal['atr']:.4f}\n\n"
        "Alpaca Paper Trading"
    )

    send_telegram(message)


# ============================================================
# MAIN
# ============================================================

def main():

    print("=" * 60)
    print("FAST BREAKOUT SCANNER")
    print("Alpaca PAPER Trading")
    print("=" * 60)

    current_time = now_ny()

    print(
        "New York time:",
        current_time.strftime(
            "%Y-%m-%d %H:%M:%S"
        )
    )

    # --------------------------------------------------------
    # MARKET CHECK
    # --------------------------------------------------------

    if not market_is_open():

        print("US market is CLOSED.")

        return

    print("US market is OPEN.")

    # --------------------------------------------------------
    # CHECK FAST POSITIONS ONLY
    # --------------------------------------------------------

    try:

        fast_positions = get_fast_positions()

    except Exception as e:

        print(
            "Could not retrieve Alpaca positions:",
            e
        )

        return

    if fast_positions:

        print(
            "FAST position already exists:"
        )

        for position in fast_positions:

            print(
                f"  {position.get('symbol')} "
                f"Qty={position.get('qty')} "
                f"AvgEntry={position.get('avg_entry_price')}"
            )

        print(
            "No new FAST trade."
        )

        return

    # --------------------------------------------------------
    # CHECK FAST OPEN ORDERS ONLY
    # --------------------------------------------------------

    try:

        fast_orders = get_open_orders_for_fast()

    except Exception as e:

        print(
            "Could not retrieve Alpaca orders:",
            e
        )

        return

    if fast_orders:

        print(
            "FAST open order already exists:"
        )

        for order in fast_orders:

            print(
                f"  {order.get('symbol')} "
                f"{order.get('side')} "
                f"{order.get('type')} "
                f"Status={order.get('status')}"
            )

        print(
            "No new FAST trade."
        )

        return

    # --------------------------------------------------------
    # SCAN
    # --------------------------------------------------------

    print()
    print(
        f"Scanning {len(SYMBOLS)} FAST symbols..."
    )

    signals = []

    for symbol in SYMBOLS:

        try:

            print(
                f"Scanning {symbol}..."
            )

            signal = analyze_symbol(symbol)

            if signal:

                signals.append(signal)

                print(
                    f"  SIGNAL: "
                    f"{signal['side'].upper()} "
                    f"{symbol} "
                    f"Entry={signal['entry']:.2f} "
                    f"SL={signal['stop']:.2f} "
                    f"TP={signal['target']:.2f} "
                    f"RelVol={signal['rel_volume']:.2f}"
                )

            else:

                print(
                    f"  No valid breakout."
                )

        except Exception as e:

            print(
                f"  ERROR {symbol}: {e}"
            )

    # --------------------------------------------------------
    # NO SIGNAL
    # --------------------------------------------------------

    if not signals:

        print()
        print(
            "No FAST breakout signal found."
        )

        return

    # --------------------------------------------------------
    # SELECT BEST SIGNAL
    # --------------------------------------------------------

    signals.sort(
        key=lambda x: (
            x["rel_volume"],
            x["breakout_percent"]
        ),
        reverse=True
    )

    signal = signals[0]

    print()
    print(
        "BEST FAST SIGNAL:"
    )

    print(
        f"{signal['symbol']} "
        f"{signal['side'].upper()} "
        f"Entry={signal['entry']:.2f} "
        f"SL={signal['stop']:.2f} "
        f"TP={signal['target']:.2f}"
    )

    # --------------------------------------------------------
    # POSITION SIZE
    # --------------------------------------------------------

    quantity = calculate_quantity(
        signal["entry"],
        signal["stop"]
    )

    if quantity < 1:

        print(
            "Trade skipped: quantity would be below 1 share."
        )

        return

    print(
        f"Calculated quantity: {quantity}"
    )

    estimated_risk = (
        abs(signal["entry"] - signal["stop"])
        * quantity
    )

    print(
        f"Estimated maximum risk: "
        f"${estimated_risk:.2f}"
    )

    # --------------------------------------------------------
    # PLACE ALPACA PAPER BRACKET
    # --------------------------------------------------------

    print()
    print(
        "Submitting Alpaca PAPER bracket order..."
    )

    order = submit_bracket_trade(
        signal
    )

    if not order:

        print(
            "Trade was NOT submitted."
        )

        return

    print(
        "ORDER SUBMITTED:"
    )

    print(
        "Order ID:",
        order.get("id")
    )

    print(
        "Status:",
        order.get("status")
    )

    # --------------------------------------------------------
    # LOG
    # --------------------------------------------------------

    log_trade(
        signal,
        quantity,
        order
    )

    # --------------------------------------------------------
    # TELEGRAM
    # --------------------------------------------------------

    send_trade_alert(
        signal,
        quantity,
        order
    )

    print()
    print(
        "FAST trade completed successfully."
    )


if __name__ == "__main__":
    main()
