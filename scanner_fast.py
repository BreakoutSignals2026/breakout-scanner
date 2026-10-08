import os
import csv
import math
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests


# ============================================================
# FAST BREAKOUT SCANNER + ALPACA PAPER TRADING
# ============================================================

ALPACA_KEY = os.getenv("ALPACA_API_KEY", "")
ALPACA_SECRET = os.getenv("ALPACA_SECRET_KEY", "")

TRADING_URL = "https://paper-api.alpaca.markets"
DATA_URL = "https://data.alpaca.markets"

# Free Alpaca market data is normally IEX.
DATA_FEED = "iex"

# ------------------------------------------------------------
# FAST SETTINGS
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

MIN_PRICE = 10.0
MAX_PRICE = 50.0

TIMEFRAME = "5Min"

BREAKOUT_LOOKBACK = 12
MIN_BREAKOUT_PERCENT = 0.15

VOLUME_LOOKBACK = 12
MIN_REL_VOLUME = 1.80

ATR_PERIOD = 14
ATR_MULTIPLIER = 1.50

MAX_SL_PERCENT = 1.00
RR = 2.0

MAX_RISK_USD = 10.00
MAX_POSITION_NOTIONAL = 500.00

MAX_OPEN_FAST_POSITIONS = 1

COOLDOWN_MINUTES = 20

STATE_FILE = "breakout_state_fast.txt"
TRADES_FILE = "breakout_trades_fast.txt"
RESULTS_FILE = "breakout_results_fast.csv"

NY = ZoneInfo("America/New_York")


# ============================================================
# HTTP
# ============================================================

def trading_headers():
    return {
        "APCA-API-KEY-ID": ALPACA_KEY,
        "APCA-API-SECRET-KEY": ALPACA_SECRET,
        "Content-Type": "application/json",
    }


def data_headers():
    return {
        "APCA-API-KEY-ID": ALPACA_KEY,
        "APCA-API-SECRET-KEY": ALPACA_SECRET,
    }


def alpaca_get(url, params=None):
    r = requests.get(
        url,
        headers=trading_headers(),
        params=params,
        timeout=20,
    )

    if not r.ok:
        raise RuntimeError(
            f"GET {url} -> {r.status_code}: {r.text}"
        )

    return r.json()


def alpaca_data_get(url, params=None):
    r = requests.get(
        url,
        headers=data_headers(),
        params=params,
        timeout=20,
    )

    if not r.ok:
        raise RuntimeError(
            f"DATA GET {url} -> {r.status_code}: {r.text}"
        )

    return r.json()


def alpaca_post(url, payload):
    r = requests.post(
        url,
        headers=trading_headers(),
        json=payload,
        timeout=20,
    )

    if not r.ok:
        raise RuntimeError(
            f"POST {url} -> {r.status_code}: {r.text}"
        )

    return r.json()


# ============================================================
# TIME / MARKET
# ============================================================

def now_ny():
    return datetime.now(timezone.utc).astimezone(NY)


def market_is_open():
    try:
        clock = alpaca_get(f"{TRADING_URL}/v2/clock")
        return bool(clock.get("is_open", False))
    except Exception as e:
        print(f"Clock error: {e}")
        return False


# ============================================================
# ACCOUNT / POSITIONS
# ============================================================

def get_account():
    return alpaca_get(f"{TRADING_URL}/v2/account")


def get_positions():
    return alpaca_get(f"{TRADING_URL}/v2/positions")


def get_open_orders():
    return alpaca_get(
        f"{TRADING_URL}/v2/orders",
        params={
            "status": "open",
            "limit": 100,
            "nested": "true",
        },
    )


def fast_position_count():
    """
    FAST uses one position at a time.
    We count any current position because FAST is intentionally
    isolated to one active position.
    """
    positions = get_positions()
    return len(positions)


# ============================================================
# MARKET DATA
# ============================================================

def get_bars(symbol):
    """
    Get recent 5-minute bars from Alpaca.

    We request enough history for:
      - breakout lookback
      - volume lookback
      - ATR
    """

    end = datetime.now(timezone.utc)
    start = end - timedelta(hours=6)

    params = {
        "timeframe": TIMEFRAME,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "limit": 500,
        "feed": DATA_FEED,
        "sort": "asc",
    }

    data = alpaca_data_get(
        f"{DATA_URL}/v2/stocks/{symbol}/bars",
        params=params,
    )

    return data.get("bars", [])


# ============================================================
# INDICATORS
# ============================================================

def calculate_atr(bars, period=14):
    if len(bars) < period + 1:
        return None

    trs = []

    for i in range(1, len(bars)):
        high = float(bars[i]["h"])
        low = float(bars[i]["l"])
        prev_close = float(bars[i - 1]["c"])

        tr = max(
            high - low,
            abs(high - prev_close),
            abs(low - prev_close),
        )

        trs.append(tr)

    if len(trs) < period:
        return None

    return sum(trs[-period:]) / period


def median(values):
    values = sorted(values)

    if not values:
        return 0

    n = len(values)

    if n % 2 == 1:
        return values[n // 2]

    return (values[n // 2 - 1] + values[n // 2]) / 2


# ============================================================
# SIGNAL
# ============================================================

def analyze_symbol(symbol):
    bars = get_bars(symbol)

    # Need:
    # previous 12 breakout bars
    # 12 volume bars
    # 14 ATR bars
    if len(bars) < 40:
        return None

    # Use the latest CLOSED 5-minute candle.
    # Alpaca can return the currently forming bar.
    # We therefore remove it if its timestamp is still inside
    # the current 5-minute interval.
    now = datetime.now(timezone.utc)

    cleaned = []

    for bar in bars:
        ts = datetime.fromisoformat(
            bar["t"].replace("Z", "+00:00")
        )

        bar_end = ts + timedelta(minutes=5)

        if bar_end <= now:
            cleaned.append(bar)

    bars = cleaned

    if len(bars) < 40:
        return None

    current = bars[-1]

    close = float(current["c"])
    high = float(current["h"])
    low = float(current["l"])
    volume = float(current["v"])

    # --------------------------------------------------------
    # PRICE FILTER
    # --------------------------------------------------------

    if close < MIN_PRICE or close > MAX_PRICE:
        return None

    # --------------------------------------------------------
    # BREAKOUT LEVEL
    # --------------------------------------------------------

    if len(bars) < BREAKOUT_LOOKBACK + 2:
        return None

    previous = bars[-BREAKOUT_LOOKBACK - 1:-1]

    highest = max(float(x["h"]) for x in previous)
    lowest = min(float(x["l"]) for x in previous)

    breakout_up = (
        close > highest
        and close >= highest * (1 + MIN_BREAKOUT_PERCENT / 100)
    )

    breakout_down = (
        close < lowest
        and close <= lowest * (1 - MIN_BREAKOUT_PERCENT / 100)
    )

    if not breakout_up and not breakout_down:
        return None

    # --------------------------------------------------------
    # RELATIVE VOLUME
    # --------------------------------------------------------

    volume_bars = bars[-VOLUME_LOOKBACK - 1:-1]

    historical_volumes = [
        float(x["v"])
        for x in volume_bars
        if float(x["v"]) > 0
    ]

    if not historical_volumes:
        return None

    median_volume = median(historical_volumes)

    if median_volume <= 0:
        return None

    rel_volume = volume / median_volume

    if rel_volume < MIN_REL_VOLUME:
        return None

    # --------------------------------------------------------
    # ATR
    # --------------------------------------------------------

    atr = calculate_atr(bars, ATR_PERIOD)

    if atr is None or atr <= 0:
        return None

    # --------------------------------------------------------
    # ENTRY / SL / TP
    # --------------------------------------------------------

    entry = close

    if breakout_up:
        side = "buy"

        sl_distance = atr * ATR_MULTIPLIER

        max_allowed_distance = entry * (
            MAX_SL_PERCENT / 100
        )

        sl_distance = min(
            sl_distance,
            max_allowed_distance,
        )

        stop = entry - sl_distance

        if stop <= 0:
            return None

        target = entry + sl_distance * RR

    else:
        side = "sell"

        sl_distance = atr * ATR_MULTIPLIER

        max_allowed_distance = entry * (
            MAX_SL_PERCENT / 100
        )

        sl_distance = min(
            sl_distance,
            max_allowed_distance,
        )

        stop = entry + sl_distance

        target = entry - sl_distance

        if target <= 0:
            return None

    # --------------------------------------------------------
    # POSITION SIZE
    # --------------------------------------------------------

    risk_per_share = abs(entry - stop)

    if risk_per_share <= 0:
        return None

    qty_by_risk = math.floor(
        MAX_RISK_USD / risk_per_share
    )

    qty_by_capital = math.floor(
        MAX_POSITION_NOTIONAL / entry
    )

    qty = min(
        qty_by_risk,
        qty_by_capital,
    )

    if qty < 1:
        return None

    planned_risk = qty * risk_per_share
    position_value = qty * entry

    return {
        "symbol": symbol,
        "side": side,
        "entry": entry,
        "breakout": highest if side == "buy" else lowest,
        "stop": stop,
        "target": target,
        "atr": atr,
        "rel_volume": rel_volume,
        "qty": qty,
        "planned_risk": planned_risk,
        "position_value": position_value,
        "bar_time": current["t"],
    }


# ============================================================
# ROUNDING
# ============================================================

def round_price(price):
    """
    Our universe is $10-$50, so 2 decimal places are appropriate.
    """
    return round(float(price), 2)


# ============================================================
# COOLDOWN
# ============================================================

def load_state():
    if not os.path.exists(STATE_FILE):
        return {}

    state = {}

    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()

                if not line:
                    continue

                parts = line.split("|")

                if len(parts) >= 2:
                    state[parts[0]] = parts[1]

    except Exception as e:
        print(f"State read error: {e}")

    return state


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        for symbol, timestamp in state.items():
            f.write(f"{symbol}|{timestamp}\n")


def cooldown_active(symbol, state):
    if symbol not in state:
        return False

    try:
        previous = datetime.fromisoformat(
            state[symbol]
        )

        now = datetime.now(timezone.utc)

        age = now - previous

        return age < timedelta(
            minutes=COOLDOWN_MINUTES
        )

    except Exception:
        return False


# ============================================================
# LOGGING
# ============================================================

def log_trade(signal, order_id):
    exists = os.path.exists(TRADES_FILE)

    with open(
        TRADES_FILE,
        "a",
        encoding="utf-8",
    ) as f:

        if not exists:
            f.write(
                "time|symbol|side|qty|entry|stop|target|"
                "atr|rel_volume|risk|notional|order_id\n"
            )

        f.write(
            f"{datetime.now(timezone.utc).isoformat()}|"
            f"{signal['symbol']}|"
            f"{signal['side']}|"
            f"{signal['qty']}|"
            f"{signal['entry']:.2f}|"
            f"{signal['stop']:.2f}|"
            f"{signal['target']:.2f}|"
            f"{signal['atr']:.4f}|"
            f"{signal['rel_volume']:.2f}|"
            f"{signal['planned_risk']:.2f}|"
            f"{signal['position_value']:.2f}|"
            f"{order_id}\n"
        )


def ensure_results_file():
    if os.path.exists(RESULTS_FILE):
        return

    with open(
        RESULTS_FILE,
        "w",
        newline="",
        encoding="utf-8",
    ) as f:

        writer = csv.writer(f)

        writer.writerow([
            "time",
            "symbol",
            "side",
            "qty",
            "entry",
            "stop",
            "target",
            "planned_risk",
            "position_value",
            "order_id",
        ])


# ============================================================
# TELEGRAM
# ============================================================

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
CHAT_ID = os.getenv("CHAT_ID", "")


def send_telegram(message):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        print("Telegram credentials not configured.")
        return

    url = (
        f"https://api.telegram.org/bot"
        f"{TELEGRAM_TOKEN}/sendMessage"
    )

    payload = {
        "chat_id": CHAT_ID,
        "text": message,
    }

    try:
        r = requests.post(
            url,
            json=payload,
            timeout=20,
        )

        if not r.ok:
            print(
                f"Telegram error: "
                f"{r.status_code} {r.text}"
            )

    except Exception as e:
        print(f"Telegram exception: {e}")


# ============================================================
# ALPACA ORDER
# ============================================================

def submit_bracket(signal):
    symbol = signal["symbol"]
    side = signal["side"]
    qty = signal["qty"]

    entry = round_price(signal["entry"])
    stop = round_price(signal["stop"])
    target = round_price(signal["target"])

    if side == "buy":
        order_side = "buy"

        if stop >= entry:
            raise RuntimeError(
                "Invalid BUY stop price."
            )

        if target <= entry:
            raise RuntimeError(
                "Invalid BUY target price."
            )

    else:
        order_side = "sell"

        if stop <= entry:
            raise RuntimeError(
                "Invalid SELL stop price."
            )

        if target >= entry:
            raise RuntimeError(
                "Invalid SELL target price."
            )

    payload = {
        "symbol": symbol,
        "qty": str(qty),
        "side": order_side,
        "type": "market",
        "time_in_force": "day",
        "order_class": "bracket",
        "take_profit": {
            "limit_price": f"{target:.2f}"
        },
        "stop_loss": {
            "stop_price": f"{stop:.2f}"
        },
    }

    return alpaca_post(
        f"{TRADING_URL}/v2/orders",
        payload,
    )


# ============================================================
# MAIN
# ============================================================

def main():

    print("=" * 60)
    print("FAST BREAKOUT SCANNER")
    print("Alpaca PAPER Trading")
    print("=" * 60)

    if not ALPACA_KEY or not ALPACA_SECRET:
        print(
            "ERROR: Missing ALPACA_API_KEY "
            "or ALPACA_SECRET_KEY."
        )
        return

    now = now_ny()

    print(
        "New York time:",
        now.strftime("%Y-%m-%d %H:%M:%S")
    )

    # --------------------------------------------------------
    # MARKET
    # --------------------------------------------------------

    if not market_is_open():
        print("US market is closed.")
        return

    print("US market is OPEN.")

    # --------------------------------------------------------
    # EXISTING POSITION
    # --------------------------------------------------------

    positions = get_positions()

    if len(positions) >= MAX_OPEN_FAST_POSITIONS:
        print(
            "FAST position already exists. "
            "No new trade."
        )
        return

    # --------------------------------------------------------
    # EXISTING ORDERS
    # --------------------------------------------------------

    open_orders = get_open_orders()

    if open_orders:
        print(
            f"Open Alpaca orders: {len(open_orders)}"
        )

        # Do not submit another trade if there is
        # already an open FAST order.
        return

    # --------------------------------------------------------
    # STATE
    # --------------------------------------------------------

    state = load_state()

    # --------------------------------------------------------
    # SCAN
    # --------------------------------------------------------

    candidates = []

    for symbol in SYMBOLS:

        try:
            if cooldown_active(symbol, state):
                print(
                    f"{symbol}: cooldown active"
                )
                continue

            print(f"Scanning {symbol}...")

            signal = analyze_symbol(symbol)

            if signal is None:
                print(
                    f"{symbol}: no FAST signal"
                )
                continue

            candidates.append(signal)

            print(
                f"{symbol}: SIGNAL "
                f"{signal['side'].upper()} "
                f"entry={signal['entry']:.2f} "
                f"SL={signal['stop']:.2f} "
                f"TP={signal['target']:.2f} "
                f"RelVol={signal['rel_volume']:.2f} "
                f"risk=${signal['planned_risk']:.2f}"
            )

        except Exception as e:
            print(
                f"{symbol}: scan error: {e}"
            )

    if not candidates:
        print("No FAST signals.")
        return

    # --------------------------------------------------------
    # SELECT BEST SIGNAL
    # --------------------------------------------------------

    # Highest relative volume first.
    candidates.sort(
        key=lambda x: x["rel_volume"],
        reverse=True,
    )

    signal = candidates[0]

    print("=" * 60)
    print("SELECTED FAST SIGNAL")
    print(
        signal["symbol"],
        signal["side"].upper(),
    )
    print(
        f"Entry: {signal['entry']:.2f}"
    )
    print(
        f"Breakout: {signal['breakout']:.2f}"
    )
    print(
        f"SL: {signal['stop']:.2f}"
    )
    print(
        f"TP: {signal['target']:.2f}"
    )
    print(
        f"Qty: {signal['qty']}"
    )
    print(
        f"Risk: ${signal['planned_risk']:.2f}"
    )
    print(
        f"Position: ${signal['position_value']:.2f}"
    )
    print(
        f"RelVol: {signal['rel_volume']:.2f}"
    )
    print("=" * 60)

    # --------------------------------------------------------
    # BUYING POWER CHECK
    # --------------------------------------------------------

    account = get_account()

    buying_power = float(
        account.get("buying_power", 0)
    )

    required_cash = signal["position_value"]

    if signal["side"] == "buy":

        if buying_power < required_cash:
            print(
                "Not enough buying power."
            )
            return

    # --------------------------------------------------------
    # SUBMIT
    # --------------------------------------------------------

    try:

        order = submit_bracket(signal)

        order_id = order.get("id", "")

        print(
            "ALPACA PAPER ORDER SUBMITTED"
        )
        print(
            "Order ID:",
            order_id
        )

        log_trade(
            signal,
            order_id,
        )

        state[signal["symbol"]] = (
            datetime.now(timezone.utc).isoformat()
        )

        save_state(state)

        telegram_message = (
            "⚡ FAST PAPER TRADE\n\n"
            f"{signal['side'].upper()} "
            f"{signal['symbol']}\n"
            f"Qty: {signal['qty']}\n"
            f"Entry: {signal['entry']:.2f}\n"
            f"SL: {signal['stop']:.2f}\n"
            f"TP: {signal['target']:.2f}\n"
            f"Risk: ${signal['planned_risk']:.2f}\n"
            f"Position: ${signal['position_value']:.2f}\n"
            f"RelVol: {signal['rel_volume']:.2f}\n\n"
            "Alpaca PAPER"
        )

        send_telegram(
            telegram_message
        )

    except Exception as e:

        print(
            "ORDER ERROR:",
            e
        )

        send_telegram(
            "⚠️ FAST Alpaca PAPER order error\n\n"
            f"{signal['symbol']}\n"
            f"{e}"
        )


if __name__ == "__main__":
    main()
