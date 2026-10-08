import os
import requests
from datetime import datetime, time
from zoneinfo import ZoneInfo

# ============================================================
# FAST BREAKOUT SCANNER V1
# US stocks $10-$50
# 5-minute breakout scanner
# ============================================================

TWELVE_API_KEY = os.environ.get("TWELVE_API_KEY")
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")

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

INTERVAL = "5min"

# FAST parameters
BREAKOUT_LOOKBACK = 12
MIN_BREAKOUT_PERCENT = 0.15

VOLUME_LOOKBACK = 12
MIN_REL_VOLUME = 1.80

ATR_PERIOD = 14
ATR_MULTIPLIER = 1.50

MAX_SL_PERCENT = 1.00
RISK_REWARD = 2.00

COOLDOWN_MINUTES = 20

STATE_FILE = "breakout_state_fast.txt"
TRADES_FILE = "breakout_trades_fast.txt"

NY = ZoneInfo("America/New_York")


# ============================================================
# TELEGRAM
# ============================================================

def send_telegram(message):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        print("Telegram secrets missing.")
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"

    try:
        requests.post(
            url,
            data={
                "chat_id": CHAT_ID,
                "text": message,
            },
            timeout=20,
        )
    except Exception as e:
        print("Telegram error:", e)


# ============================================================
# MARKET HOURS
# ============================================================

def market_is_open():
    now = datetime.now(NY)

    if now.weekday() >= 5:
        return False

    current = now.time()

    return time(9, 30) <= current < time(16, 0)


# ============================================================
# TWELVE DATA
# ============================================================

def get_data(symbol):
    url = "https://api.twelvedata.com/time_series"

    params = {
        "symbol": symbol,
        "interval": INTERVAL,
        "outputsize": 100,
        "apikey": TWELVE_API_KEY,
        "prepost": "false",
        "timezone": "America/New_York",
    }

    try:
        r = requests.get(url, params=params, timeout=20)
        data = r.json()

        if "values" not in data:
            print(symbol, "API error:", data)
            return []

        return list(reversed(data["values"]))

    except Exception as e:
        print(symbol, "data error:", e)
        return []


# ============================================================
# ATR
# ============================================================

def calculate_atr(candles, period=14):
    if len(candles) < period + 1:
        return None

    trs = []

    for i in range(1, len(candles)):
        high = float(candles[i]["high"])
        low = float(candles[i]["low"])
        previous_close = float(candles[i - 1]["close"])

        tr = max(
            high - low,
            abs(high - previous_close),
            abs(low - previous_close),
        )

        trs.append(tr)

    recent = trs[-period:]

    return sum(recent) / len(recent)


# ============================================================
# RELATIVE VOLUME
# ============================================================

def relative_volume(candles):
    if len(candles) < VOLUME_LOOKBACK + 1:
        return None

    current_volume = float(candles[-1]["volume"])

    previous_volumes = [
        float(x["volume"])
        for x in candles[-VOLUME_LOOKBACK - 1:-1]
    ]

    average_volume = sum(previous_volumes) / len(previous_volumes)

    if average_volume <= 0:
        return None

    return current_volume / average_volume


# ============================================================
# STATE
# ============================================================

def load_state():
    state = {}

    if not os.path.exists(STATE_FILE):
        return state

    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            for line in f:
                parts = line.strip().split("|")

                if len(parts) >= 2:
                    state[parts[0]] = parts[1]

    except Exception as e:
        print("State read error:", e)

    return state


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        for symbol, timestamp in state.items():
            f.write(f"{symbol}|{timestamp}\n")


# ============================================================
# FAST SIGNAL
# ============================================================

def check_symbol(symbol, state):

    candles = get_data(symbol)

    if len(candles) < 40:
        print(symbol, "not enough data")
        return

    # Use only the latest CLOSED candle
    candle = candles[-1]

    close = float(candle["close"])
    high = float(candle["high"])
    low = float(candle["low"])

    # Price filter
    if close < 10 or close > 50:
        return

    # Previous range
    lookback = candles[-BREAKOUT_LOOKBACK - 1:-1]

    previous_high = max(float(x["high"]) for x in lookback)
    previous_low = min(float(x["low"]) for x in lookback)

    breakout_up = (
        close > previous_high
        and ((close - previous_high) / previous_high * 100)
        >= MIN_BREAKOUT_PERCENT
    )

    breakout_down = (
        close < previous_low
        and ((previous_low - close) / previous_low * 100)
        >= MIN_BREAKOUT_PERCENT
    )

    if not breakout_up and not breakout_down:
        return

    # Relative volume
    relvol = relative_volume(candles)

    if relvol is None or relvol < MIN_REL_VOLUME:
        print(symbol, "breakout but RelVol too low:", relvol)
        return

    # ATR
    atr = calculate_atr(candles, ATR_PERIOD)

    if atr is None:
        return

    # Cooldown
    candle_time = candle.get("datetime", "")

    if symbol in state:
        previous_signal = state[symbol]

        if previous_signal == candle_time:
            return

    # ========================================================
    # BUY
    # ========================================================

    if breakout_up:

        entry = close

        sl = entry - (atr * ATR_MULTIPLIER)

        max_sl = entry * (1 - MAX_SL_PERCENT / 100)

        sl = max(sl, max_sl)

        risk = entry - sl

        if risk <= 0:
            return

        tp = entry + (risk * RISK_REWARD)

        breakout_percent = (
            (entry - previous_high) / previous_high * 100
        )

        message = (
            "⚡ FAST BREAKOUT BUY\n\n"
            f"Symbol: {symbol}\n"
            f"Entry: {entry:.2f}\n"
            f"Breakout: {previous_high:.2f}\n"
            f"Breakout: {breakout_percent:.2f}%\n\n"
            f"ATR(14): {atr:.2f}\n"
            f"SL: {sl:.2f}\n"
            f"SL: {(risk / entry) * 100:.2f}%\n"
            f"TP: {tp:.2f}\n\n"
            f"RelVol: {relvol:.2f}x\n"
            f"R:R = 1:{RISK_REWARD:.0f}\n"
            f"Time: {candle_time}"
        )

        print(message)
        send_telegram(message)

        state[symbol] = candle_time

        with open(TRADES_FILE, "a", encoding="utf-8") as f:
            f.write(
                f"{candle_time}|BUY|{symbol}|"
                f"{entry:.2f}|{sl:.2f}|{tp:.2f}|"
                f"{relvol:.2f}\n"
            )

    # ========================================================
    # SELL
    # ========================================================

    elif breakout_down:

        entry = close

        sl = entry + (atr * ATR_MULTIPLIER)

        max_sl = entry * (1 + MAX_SL_PERCENT / 100)

        sl = min(sl, max_sl)

        risk = sl - entry

        if risk <= 0:
            return

        tp = entry - (risk * RISK_REWARD)

        breakout_percent = (
            (previous_low - entry) / previous_low * 100
        )

        message = (
            "⚡ FAST BREAKOUT SELL\n\n"
            f"Symbol: {symbol}\n"
            f"Entry: {entry:.2f}\n"
            f"Breakout: {previous_low:.2f}\n"
            f"Breakout: {breakout_percent:.2f}%\n\n"
            f"ATR(14): {atr:.2f}\n"
            f"SL: {sl:.2f}\n"
            f"SL: {(risk / entry) * 100:.2f}%\n"
            f"TP: {tp:.2f}\n\n"
            f"RelVol: {relvol:.2f}x\n"
            f"R:R = 1:{RISK_REWARD:.0f}\n"
            f"Time: {candle_time}"
        )

        print(message)
        send_telegram(message)

        state[symbol] = candle_time

        with open(TRADES_FILE, "a", encoding="utf-8") as f:
            f.write(
                f"{candle_time}|SELL|{symbol}|"
                f"{entry:.2f}|{sl:.2f}|{tp:.2f}|"
                f"{relvol:.2f}\n"
            )


# ============================================================
# MAIN
# ============================================================

def main():

    print("===================================")
    print("FAST BREAKOUT SCANNER V1")
    print("===================================")

    now = datetime.now(NY)

    print("New York time:", now)

    if not TWELVE_API_KEY:
        print("ERROR: TWELVE_API_KEY missing")
        return

    if not market_is_open():
        print("US market closed.")
        return

    state = load_state()

    for symbol in SYMBOLS:

        try:
            check_symbol(symbol, state)

        except Exception as e:
            print(symbol, "ERROR:", e)

    save_state(state)

    print("FAST scan finished.")


if __name__ == "__main__":
    main()
