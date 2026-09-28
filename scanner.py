import requests
import os
from datetime import datetime, timedelta, time
from zoneinfo import ZoneInfo
from statistics import median


# ============================================================
# V2.4 - BREAKOUT SCANNER
# ============================================================

# ------------------------------------------------------------
# TAJNE ÚDAJE
# ------------------------------------------------------------
TWELVE_API_KEY = os.getenv("TWELVE_API_KEY")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")

CHAT_ID = os.getenv("CHAT_ID", "8950231945")


# ------------------------------------------------------------
# NASTAVENIA
# ------------------------------------------------------------

SYMBOLS = [
    "INTC",
    "AMD",
    "NVDA",
    "MRVL",
    "AAPL",
    "TSLA",
    "META",
    "AMZN",
    "MSFT",
    "GOOGL"
]

INTERVAL = "5min"

# Koľko predchádzajúcich sviečok používame na breakout
BREAKOUT_LOOKBACK = 20

# Minimálna veľkosť prerazenia
MIN_BREAKOUT_PERCENT = 0.10

# RelVol:
# aktuálna sviečka vs. medián predchádzajúcich 12 sviečok DNES
VOLUME_LOOKBACK = 12

# Koľko sviečok musí byť minimálne dostupných
MIN_VOLUME_BARS = 10

# Minimálny relatívny objem
MIN_REL_VOLUME = 1.50

# ATR
ATR_PERIOD = 14
ATR_MULTIPLIER = 1.5

# Maximálna vzdialenosť Stop Loss
MAX_SL_PERCENT = 1.0

# Take Profit = 2R
RISK_REWARD = 2.0

# Cooldown medzi signálmi
COOLDOWN_MINUTES = 30

# Stavový súbor
STATE_FILE = "breakout_state_v24.txt"

# New York čas
US_TZ = ZoneInfo("America/New_York")

# Pravidelná burzová seansa
MARKET_OPEN = time(9, 30)
MARKET_CLOSE = time(16, 0)

# Pauza medzi požiadavkami na Twelve Data
API_REQUEST_DELAY = 8

# ============================================================
# POMOCNÉ FUNKCIE
# ============================================================

def now_us():
    """Aktuálny čas v New Yorku."""
    return datetime.now(US_TZ)


def is_market_open():
    """Kontrola pravidelnej US seansy."""
    now = now_us()

    if now.weekday() >= 5:
        return False

    current_time = now.time()

    return MARKET_OPEN <= current_time < MARKET_CLOSE


def get_data(symbol):
    """Stiahne 5-minútové dáta z Twelve Data."""

    url = "https://api.twelvedata.com/time_series"

    params = {
        "symbol": symbol,
        "interval": INTERVAL,
        "outputsize": 1000,
        "prepost": "false",
        "timezone": "America/New_York",
        "apikey": TWELVE_API_KEY
    }

    try:
        time.sleep(API_REQUEST_DELAY)

        response = requests.get(
            url,
            params=params,
            timeout=20
        )

        response.raise_for_status()

        data = response.json()

        if "status" in data and data["status"] == "error":
            print(f"{symbol} API ERROR: {data.get('message')}")
            return []

        values = data.get("values", [])

        candles = []

        for item in values:

            try:
                dt = datetime.strptime(
                    item["datetime"],
                    "%Y-%m-%d %H:%M:%S"
                )

                dt = dt.replace(tzinfo=US_TZ)

                candle = {
                    "datetime": dt,
                    "open": float(item["open"]),
                    "high": float(item["high"]),
                    "low": float(item["low"]),
                    "close": float(item["close"]),
                    "volume": float(item.get("volume", 0) or 0)
                }

                candles.append(candle)

            except Exception as e:
                print(f"{symbol} chyba pri spracovaní sviečky: {e}")

        candles.sort(key=lambda x: x["datetime"])

        return candles

    except Exception as e:
        print(f"{symbol} DATA ERROR: {e}")
        return []


# ============================================================
# UZAVRETÉ SVIEČKY
# ============================================================

def get_closed_candles(candles):
    """
    Vráti iba sviečky, ktoré už sú úplne uzavreté.
    """

    now = now_us()

    closed = []

    for candle in candles:

        candle_end = candle["datetime"] + timedelta(minutes=5)

        if candle_end <= now:
            closed.append(candle)

    return closed


# ============================================================
# ATR
# ============================================================

def calculate_atr(candles, period=14):

    if len(candles) < period + 1:
        return None

    true_ranges = []

    for i in range(1, len(candles)):

        current = candles[i]
        previous = candles[i - 1]

        high = current["high"]
        low = current["low"]
        previous_close = previous["close"]

        tr1 = high - low
        tr2 = abs(high - previous_close)
        tr3 = abs(low - previous_close)

        true_range = max(tr1, tr2, tr3)

        true_ranges.append(true_range)

    if len(true_ranges) < period:
        return None

    recent = true_ranges[-period:]

    atr = sum(recent) / len(recent)

    return atr


# ============================================================
# INTRADAY RELATIVE VOLUME
# ============================================================

def calculate_intraday_relative_volume(
    closed_candles,
    current_candle
):
    """
    V2.4

    Porovnáva aktuálnu uzavretú 5-minútovú sviečku
    s mediánom predchádzajúcich 12 sviečok V TEN ISTÝ DEŇ.

    Neporovnávame objem s predchádzajúcimi dňami.
    """

    current_date = current_candle["datetime"].date()
    current_time = current_candle["datetime"]

    previous_today = []

    for candle in closed_candles:

        if candle["datetime"].date() != current_date:
            continue

        if candle["datetime"] >= current_time:
            continue

        volume = candle["volume"]

        if volume > 0:
            previous_today.append(candle)

    # Posledných 12 sviečok pred aktuálnou
    previous_today = previous_today[-VOLUME_LOOKBACK:]

    if len(previous_today) < MIN_VOLUME_BARS:
        return None, None, len(previous_today)

    volumes = [
        candle["volume"]
        for candle in previous_today
        if candle["volume"] > 0
    ]

    if len(volumes) < MIN_VOLUME_BARS:
        return None, None, len(volumes)

    reference_volume = median(volumes)

    current_volume = current_candle["volume"]

    if reference_volume <= 0:
        return None, None, len(volumes)

    relative_volume = current_volume / reference_volume

    return relative_volume, reference_volume, len(volumes)


# ============================================================
# BREAKOUT ANALÝZA
# ============================================================

def analyze_symbol(symbol, candles):

    closed = get_closed_candles(candles)

    # Potrebujeme minimálne:
    # 20 sviečok na breakout
    # + ATR
    # + aktuálnu sviečku

    minimum_needed = max(
        BREAKOUT_LOOKBACK + 1,
        ATR_PERIOD + 2
    )

    if len(closed) < minimum_needed:

        return {
            "signal": None,
            "message": (
                f"{symbol}: málo uzavretých sviečok "
                f"({len(closed)})"
            )
        }

    current = closed[-1]

    previous = closed[:-1]

    # --------------------------------------------------------
    # BREAKOUT HIGH / LOW
    # --------------------------------------------------------

    breakout_window = previous[-BREAKOUT_LOOKBACK:]

    highest_high = max(
        candle["high"]
        for candle in breakout_window
    )

    lowest_low = min(
        candle["low"]
        for candle in breakout_window
    )

    close_price = current["close"]

    # --------------------------------------------------------
    # ATR
    # --------------------------------------------------------

    atr = calculate_atr(
        closed,
        ATR_PERIOD
    )

    if atr is None:

        return {
            "signal": None,
            "message": f"{symbol}: ATR nie je dostupné"
        }

    # --------------------------------------------------------
    # RELATIVE VOLUME
    # --------------------------------------------------------

    rel_volume, reference_volume, volume_bars = \
        calculate_intraday_relative_volume(
            closed,
            current
        )

    # --------------------------------------------------------
    # AK RELVOL EŠTE NIE JE DOSTUPNÝ
    # --------------------------------------------------------

    if rel_volume is None:

        return {
            "signal": None,
            "message": (
                f"{symbol}: čakám na objemový základ | "
                f"čas {current['datetime'].strftime('%H:%M')} | "
                f"CurrentVol {current['volume']:.0f} | "
                f"Reference bars {volume_bars}"
            )
        }

    # ========================================================
    # BUY BREAKOUT
    # ========================================================

    if close_price > highest_high:

        breakout_percent = (
            (close_price - highest_high)
            / highest_high
        ) * 100

        # Minimálne prerazenie
        if breakout_percent >= MIN_BREAKOUT_PERCENT:

            # Objem musí byť dostatočný
            if rel_volume >= MIN_REL_VOLUME:

                entry = close_price

                sl_distance = atr * ATR_MULTIPLIER

                # Maximálny SL 1 %
                max_sl_distance = (
                    entry * MAX_SL_PERCENT / 100
                )

                sl_distance = min(
                    sl_distance,
                    max_sl_distance
                )

                stop_loss = entry - sl_distance

                risk = entry - stop_loss

                take_profit = (
                    entry
                    + risk * RISK_REWARD
                )

                sl_percent = (
                    risk / entry
                ) * 100

                return {
                    "signal": "BUY",
                    "symbol": symbol,
                    "entry": entry,
                    "breakout": highest_high,
                    "breakout_percent": breakout_percent,
                    "atr": atr,
                    "sl": stop_loss,
                    "sl_percent": sl_percent,
                    "tp": take_profit,
                    "risk": risk,
                    "rr": RISK_REWARD,
                    "rel_volume": rel_volume,
                    "reference_volume": reference_volume,
                    "volume_bars": volume_bars,
                    "candle_time": current["datetime"]
                }

    # ========================================================
    # SELL BREAKOUT
    # ========================================================

    if close_price < lowest_low:

        breakout_percent = (
            (lowest_low - close_price)
            / lowest_low
        ) * 100

        if breakout_percent >= MIN_BREAKOUT_PERCENT:

            if rel_volume >= MIN_REL_VOLUME:

                entry = close_price

                sl_distance = atr * ATR_MULTIPLIER

                max_sl_distance = (
                    entry * MAX_SL_PERCENT / 100
                )

                sl_distance = min(
                    sl_distance,
                    max_sl_distance
                )

                stop_loss = entry + sl_distance

                risk = stop_loss - entry

                take_profit = (
                    entry
                    - risk * RISK_REWARD
                )

                sl_percent = (
                    risk / entry
                ) * 100

                return {
                    "signal": "SELL",
                    "symbol": symbol,
                    "entry": entry,
                    "breakout": lowest_low,
                    "breakout_percent": breakout_percent,
                    "atr": atr,
                    "sl": stop_loss,
                    "sl_percent": sl_percent,
                    "tp": take_profit,
                    "risk": risk,
                    "rr": RISK_REWARD,
                    "rel_volume": rel_volume,
                    "reference_volume": reference_volume,
                    "volume_bars": volume_bars,
                    "candle_time": current["datetime"]
                }

    # ========================================================
    # ŽIADNY BREAKOUT
    # ========================================================

    return {
        "signal": None,
        "symbol": symbol,
        "price": close_price,
        "high": highest_high,
        "low": lowest_low,
        "atr": atr,
        "current_volume": current["volume"],
        "reference_volume": reference_volume,
        "rel_volume": rel_volume,
        "volume_bars": volume_bars,
        "candle_time": current["datetime"]
    }


# ============================================================
# TELEGRAM
# ============================================================

def send_telegram(message):

    url = (
        f"https://api.telegram.org/bot"
        f"{TELEGRAM_TOKEN}/sendMessage"
    )

    payload = {
        "chat_id": CHAT_ID,
        "text": message
    }

    try:

        response = requests.post(
            url,
            data=payload,
            timeout=20
        )

        data = response.json()

        if data.get("ok"):

            print("Telegram: OK")
            return True

        print(
            "Telegram ERROR:",
            data.get("description")
        )

        return False

    except Exception as e:

        print(
            "Telegram ERROR:",
            e
        )

        return False


# ============================================================
# STAV / DUPLICITY
# ============================================================

def load_state():

    if not os.path.exists(STATE_FILE):
        return {}

    state = {}

    try:

        with open(
            STATE_FILE,
            "r",
            encoding="utf-8"
        ) as f:

            for line in f:

                line = line.strip()

                if not line:
                    continue

                parts = line.split("|")

                if len(parts) != 3:
                    continue

                symbol = parts[0]
                direction = parts[1]
                candle_time = parts[2]

                key = (
                    symbol,
                    direction
                )

                state[key] = candle_time

    except Exception as e:

        print(
            "Chyba pri načítaní state:",
            e
        )

    return state


def save_state(state):

    try:

        with open(
            STATE_FILE,
            "w",
            encoding="utf-8"
        ) as f:

            for key, candle_time in state.items():

                symbol, direction = key

                f.write(
                    f"{symbol}|"
                    f"{direction}|"
                    f"{candle_time}\n"
                )

    except Exception as e:

        print(
            "Chyba pri ukladaní state:",
            e
        )


def can_send_signal(
    signal,
    state
):

    symbol = signal["symbol"]
    direction = signal["signal"]

    candle_time = signal[
        "candle_time"
    ]

    key = (
        symbol,
        direction
    )

    candle_string = candle_time.isoformat()

    # --------------------------------------------------------
    # Rovnaká sviečka = neposielať znova
    # --------------------------------------------------------

    if key in state:

        if state[key] == candle_string:

            return False

    # --------------------------------------------------------
    # Cooldown
    # --------------------------------------------------------

    for stored_key, stored_time in state.items():

        stored_symbol, stored_direction = stored_key

        if stored_symbol != symbol:
            continue

        try:

            old_time = datetime.fromisoformat(
                stored_time
            )

            if old_time.tzinfo is None:

                old_time = old_time.replace(
                    tzinfo=US_TZ
                )

            difference = (
                candle_time - old_time
            ).total_seconds() / 60

            if difference < COOLDOWN_MINUTES:

                return False

        except Exception:
            continue

    return True


# ============================================================
# FORMÁT TELEGRAM SPRÁVY
# ============================================================

def create_telegram_message(signal):

    direction = signal["signal"]

    emoji = "🟢" if direction == "BUY" else "🔴"

    message = (
        f"{emoji} BREAKOUT {direction}\n"
        f"\n"
        f"Symbol: {signal['symbol']}\n"
        f"Entry: {signal['entry']:.2f}\n"
        f"Breakout: {signal['breakout']:.2f}\n"
        f"Breakout: {signal['breakout_percent']:.2f}%\n"
        f"\n"
        f"ATR(14): {signal['atr']:.2f}\n"
        f"SL: {signal['sl']:.2f}\n"
        f"SL: {signal['sl_percent']:.2f}%\n"
        f"TP: {signal['tp']:.2f}\n"
        f"\n"
        f"Risk: {signal['risk']:.2f}\n"
        f"R:R: 1:{signal['rr']:.0f}\n"
        f"\n"
        f"Current Vol: "
        f"{signal['rel_volume'] * signal['reference_volume']:.0f}\n"
        f"Today Ref Vol: "
        f"{signal['reference_volume']:.0f}\n"
        f"Intraday RelVol: "
        f"{signal['rel_volume']:.2f}x\n"
        f"Reference bars: "
        f"{signal['volume_bars']}\n"
        f"\n"
        f"5-min candle: "
        f"{signal['candle_time'].strftime('%Y-%m-%d %H:%M')}"
    )

    return message


# ============================================================
# HLAVNÝ PROGRAM
# ============================================================

def main():

    current_time = now_us()

    print("=" * 60)
    print("BREAKOUT SCANNER V2.4")
    print(
        "US čas:",
        current_time.strftime(
            "%Y-%m-%d %H:%M:%S %Z"
        )
    )
    print("=" * 60)

    # --------------------------------------------------------
    # Kontrola trhu
    # --------------------------------------------------------

    if not is_market_open():

        print(
            "US burza je momentálne zatvorená."
        )

        print(
            "Pravidelná seansa: "
            "09:30 - 16:00 New York time."
        )

        return

    # --------------------------------------------------------
    # Načítanie state
    # --------------------------------------------------------

    state = load_state()

    signals_found = 0

    # --------------------------------------------------------
    # Symboly
    # --------------------------------------------------------

    for symbol in SYMBOLS:

        candles = get_data(symbol)

        if not candles:

            print(
                f"{symbol}: žiadne dáta"
            )

            continue

        result = analyze_symbol(
            symbol,
            candles
        )

        # ====================================================
        # SIGNÁL
        # ====================================================

        if result.get("signal"):

            signals_found += 1

            direction = result["signal"]

            print()
            print(
                f"{symbol}: "
                f"{direction} BREAKOUT"
            )

            print(
                f"Entry: "
                f"{result['entry']:.2f}"
            )

            print(
                f"Breakout: "
                f"{result['breakout']:.2f}"
            )

            print(
                f"Breakout %: "
                f"{result['breakout_percent']:.2f}%"
            )

            print(
                f"ATR: "
                f"{result['atr']:.2f}"
            )

            print(
                f"SL: "
                f"{result['sl']:.2f}"
            )

            print(
                f"TP: "
                f"{result['tp']:.2f}"
            )

            print(
                f"RelVol: "
                f"{result['rel_volume']:.2f}x"
            )

            # ------------------------------------------------
            # Kontrola duplicity / cooldown
            # ------------------------------------------------

            if can_send_signal(
                result,
                state
            ):

                message = create_telegram_message(
                    result
                )

                telegram_ok = send_telegram(
                    message
                )

                # Stav uložíme iba ak Telegram
                # správu skutočne prijal

                if telegram_ok:

                    key = (
                        result["symbol"],
                        result["signal"]
                    )

                    state[key] = (
                        result["candle_time"].isoformat()
                    )

                    save_state(state)

                    print(
                        "SIGNÁL ODOSLANÝ DO TELEGRAMU."
                    )

                else:

                    print(
                        "Telegram správu neodoslal."
                    )

            else:

                print(
                    "Signál zablokovaný "
                    "(duplicitný/cooldown)."
                )

            print()

        # ====================================================
        # ŽIADNY SIGNÁL
        # ====================================================

        else:

            if "price" in result:

                print(
                    f"{symbol}: NO BREAKOUT | "
                    f"Cena {result['price']:.2f} | "
                    f"High {result['high']:.2f} | "
                    f"Low {result['low']:.2f} | "
                    f"CurrentVol "
                    f"{result['current_volume']:.0f} | "
                    f"RefVol "
                    f"{result['reference_volume']:.0f} | "
                    f"RelVol "
                    f"{result['rel_volume']:.2f}x | "
                    f"ATR "
                    f"{result['atr']:.2f} | "
                    f"čas "
                    f"{result['candle_time'].strftime('%H:%M')}"
                )

            else:

                print(
                    result.get(
                        "message",
                        f"{symbol}: bez signálu"
                    )
                )

    # --------------------------------------------------------
    # Súhrn
    # --------------------------------------------------------

    print()
    print(
        f"{signals_found} signals."
    )
    print()
    print(
        "RelVol V2.4 = aktuálny objem / "
        "medián predchádzajúcich 12 "
        "5-min sviečok DNES."
    )
    print("=" * 60)


# ============================================================
# SPUSTENIE
# ============================================================

if __name__ == "__main__":
    main()
