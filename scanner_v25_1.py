import requests
import os
from datetime import datetime, timedelta, time
import time as time_module
from zoneinfo import ZoneInfo
from statistics import median

from alpaca_executor import (
    has_open_position,
    has_open_order,
    place_bracket_order,
)


# ============================================================
# V2.5.1 - BREAKOUT SCANNER + TRACKING
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

# V2.5 - sledovanie výsledkov signálov
TRADE_STATE_FILE = "breakout_trades_v25.txt"
TRADE_RESULTS_FILE = "breakout_results_v25.csv"

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
        time_module.sleep(API_REQUEST_DELAY)

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
    """ Vráti iba sviečky, ktoré už sú úplne uzavreté. """

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

def calculate_intraday_relative_volume( closed_candles, current_candle ):
    """ V2.4 Porovnáva aktuálnu uzavretú 5-minútovú sviečku s mediánom predchádzajúcich 12 sviečok V TEN ISTÝ DEŇ. Neporovnávame objem s predchádzajúcimi dňami. """

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

def get_current_session_closed_candles(candles):
    """ V2.5.1: Pre nové breakout signály používame iba uzavreté sviečky z aktuálneho kalendárneho dňa a pravidelnej US seansy 09:30-16:00 NY. Staré sviečky z predchádzajúceho dňa sa nesmú použiť ako nový signál. """
    now = now_us()
    today = now.date()

    closed = get_closed_candles(candles)

    return [
        candle for candle in closed
        if candle["datetime"].date() == today
        and MARKET_OPEN <= candle["datetime"].time() < MARKET_CLOSE
    ]


def analyze_symbol(symbol, candles):

    # V2.5.1: nové breakouty iba z aktuálnej US seansy.
    closed = get_current_session_closed_candles(candles)

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
                f"{symbol}: málo uzavretých sviečok dnešnej seansy "
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


def can_send_signal( signal, state ):

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
# V2.5 - SLEDOVANIE VÝSLEDKOV SIGNÁLOV
# ============================================================

def load_trade_tracking():
    """Načíta obchody, ktoré ešte čakajú na výsledok."""
    if not os.path.exists(TRADE_STATE_FILE):
        return {}

    trades = {}

    try:
        with open(TRADE_STATE_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue

                parts = line.split("|")
                if len(parts) != 9:
                    continue

                symbol, direction, signal_time = parts[0], parts[1], parts[2]
                key = (symbol, direction, signal_time)

                trades[key] = {
                    "symbol": symbol,
                    "direction": direction,
                    "signal_time": signal_time,
                    "entry": float(parts[3]),
                    "sl": float(parts[4]),
                    "tp": float(parts[5]),
                    "last_checked": parts[6],
                    "max_favorable": float(parts[7]),
                    "max_adverse": float(parts[8])
                }

    except Exception as e:
        print("Chyba pri načítaní trade tracking:", e)

    return trades


def save_trade_tracking(trades):
    """Uloží ešte nevyhodnotené obchody."""
    try:
        with open(TRADE_STATE_FILE, "w", encoding="utf-8") as f:
            for trade in trades.values():
                f.write(
                    f"{trade['symbol']}|"
                    f"{trade['direction']}|"
                    f"{trade['signal_time']}|"
                    f"{trade['entry']:.8f}|"
                    f"{trade['sl']:.8f}|"
                    f"{trade['tp']:.8f}|"
                    f"{trade['last_checked']}|"
                    f"{trade['max_favorable']:.8f}|"
                    f"{trade['max_adverse']:.8f}\n"
                )
    except Exception as e:
        print("Chyba pri ukladaní trade tracking:", e)


def append_trade_result(trade, status, exit_time=None):
    """Pridá ukončený obchod do CSV."""
    import csv

    entry = trade["entry"]
    direction = trade["direction"]
    max_favorable = trade["max_favorable"]
    max_adverse = trade["max_adverse"]

    max_favorable_percent = max_favorable / entry * 100 if entry else 0
    max_adverse_percent = max_adverse / entry * 100 if entry else 0

    if status == "TP HIT":
        result_per_share = (
            trade["tp"] - entry if direction == "BUY"
            else entry - trade["tp"]
        )
    elif status == "SL HIT":
        result_per_share = (
            trade["sl"] - entry if direction == "BUY"
            else entry - trade["sl"]
        )
    else:
        result_per_share = None

    file_exists = os.path.exists(TRADE_RESULTS_FILE)

    try:
        with open(TRADE_RESULTS_FILE, "a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)

            if not file_exists:
                writer.writerow([
                    "symbol", "direction", "signal_time", "entry", "sl", "tp",
                    "result", "result_dollars_per_share", "result_percent",
                    "max_favorable_dollars", "max_favorable_percent",
                    "max_adverse_dollars", "max_adverse_percent", "exit_time"
                ])

            writer.writerow([
                trade["symbol"], direction, trade["signal_time"],
                f"{entry:.4f}", f"{trade['sl']:.4f}", f"{trade['tp']:.4f}",
                status,
                "" if result_per_share is None else f"{result_per_share:.4f}",
                "" if result_per_share is None else f"{result_per_share / entry * 100:.4f}",
                f"{max_favorable:.4f}", f"{max_favorable_percent:.4f}",
                f"{max_adverse:.4f}", f"{max_adverse_percent:.4f}",
                exit_time.isoformat() if exit_time else ""
            ])

    except Exception as e:
        print("Chyba pri ukladaní výsledku:", e)


def send_trade_result(trade, status, candle_time=None):
    """Pošle výsledok obchodu do Telegramu."""
    direction = trade["direction"]
    entry = trade["entry"]

    if status == "TP HIT":
        result = (
            trade["tp"] - entry if direction == "BUY"
            else entry - trade["tp"]
        )
        result_text = (
            f"Výsledok: {result:+.2f} $/akciu\n"
            f"Výsledok: {result / entry * 100:+.2f}%\n"
        )
        emoji = "🟢"
    elif status == "SL HIT":
        result = (
            trade["sl"] - entry if direction == "BUY"
            else entry - trade["sl"]
        )
        result_text = (
            f"Výsledok: {result:+.2f} $/akciu\n"
            f"Výsledok: {result / entry * 100:+.2f}%\n"
        )
        emoji = "🔴"
    elif status == "NEITHER":
        result_text = "Výsledok: ani TP ani SL do konca seansy\n"
        emoji = "⚪"
    else:
        result_text = "Výsledok: AMBIGUOUS – jedna 5-min sviečka zasiahla TP aj SL\n"
        emoji = "⚠️"

    candle_text = (
        candle_time.strftime("%Y-%m-%d %H:%M")
        if candle_time else "koniec seansy"
    )

    message = (
        f"{emoji} BREAKOUT RESULT\n\n"
        f"Symbol: {trade['symbol']}\n"
        f"Direction: {direction}\n"
        f"Entry: {entry:.2f}\n"
        f"SL: {trade['sl']:.2f}\n"
        f"TP: {trade['tp']:.2f}\n"
        f"{result_text}"
        f"Max favorable: {trade['max_favorable']:.2f} "
        f"({trade['max_favorable'] / entry * 100:.2f}%)\n"
        f"Max adverse: {trade['max_adverse']:.2f} "
        f"({trade['max_adverse'] / entry * 100:.2f}%)\n"
        f"Kontrolná 5-min sviečka: {candle_text}"
    )

    send_telegram(message)


def evaluate_open_trades_for_symbol(symbol, candles, trades, finalize_session=False):
    """Vyhodnotí všetky otvorené obchody daného symbolu."""
    keys = [key for key in trades if key[0] == symbol]
    closed = get_closed_candles(candles)

    for key in keys:
        if key not in trades:
            continue

        trade = trades[key]
        signal_time = datetime.fromisoformat(trade["signal_time"])
        last_checked = datetime.fromisoformat(trade["last_checked"])

        new_candles = [
            candle for candle in closed
            if candle["datetime"] > signal_time
            and candle["datetime"] > last_checked
        ]
        new_candles.sort(key=lambda x: x["datetime"])

        finished = False

        for candle in new_candles:
            entry = trade["entry"]
            direction = trade["direction"]

            if direction == "BUY":
                favorable = candle["high"] - entry
                adverse = entry - candle["low"]
                hit_tp = candle["high"] >= trade["tp"]
                hit_sl = candle["low"] <= trade["sl"]
            else:
                favorable = entry - candle["low"]
                adverse = candle["high"] - entry
                hit_tp = candle["low"] <= trade["tp"]
                hit_sl = candle["high"] >= trade["sl"]

            trade["max_favorable"] = max(
                trade["max_favorable"], max(0.0, favorable)
            )
            trade["max_adverse"] = max(
                trade["max_adverse"], max(0.0, adverse)
            )
            trade["last_checked"] = candle["datetime"].isoformat()

            if hit_tp and hit_sl:
                status = "AMBIGUOUS"
            elif hit_tp:
                status = "TP HIT"
            elif hit_sl:
                status = "SL HIT"
            else:
                continue

            append_trade_result(trade, status, candle["datetime"])
            send_trade_result(trade, status, candle["datetime"])
            del trades[key]
            finished = True
            break

        if finished:
            continue

        # Ak už je po konci pravidelnej seansy a máme dáta až po 15:55,
        # obchod, ktorý nezasiahol TP ani SL, uzavrieme ako NEITHER.
        if finalize_session:
            signal_date = signal_time.date()
            session_candles = [
                c for c in closed
                if c["datetime"].date() == signal_date
            ]

            if session_candles:
                latest = max(c["datetime"] for c in session_candles)
                if latest.time() >= time(15, 55):
                    trade["last_checked"] = latest.isoformat()
                    append_trade_result(trade, "NEITHER", latest)
                    send_trade_result(trade, "NEITHER", latest)
                    del trades[key]


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
# HLAVNÝ PROGRAM V2.5
# ============================================================

def main():

    current_time = now_us()

    print("=" * 60)
    print("BREAKOUT SCANNER V2.5.1")
    print(
        "US čas:",
        current_time.strftime("%Y-%m-%d %H:%M:%S %Z")
    )
    print("=" * 60)

    # Víkend = nič neskenujeme ani nevyhodnocujeme.
    if current_time.weekday() >= 5:
        print("US burza je cez víkend zatvorená.")
        return

    market_open = is_market_open()
    state = load_state()
    trades = load_trade_tracking()
    signals_found = 0

    for symbol in SYMBOLS:

        # Počas trhu potrebujeme všetky symboly.
        # Po zatvorení potrebujeme iba symboly s otvoreným obchodom.
        has_open_trade = any(
            trade_key[0] == symbol
            for trade_key in trades
        )

        if not market_open and not has_open_trade:
            continue

        candles = get_data(symbol)

        if not candles:
            print(f"{symbol}: žiadne dáta")
            continue

        # Najprv vyhodnotíme starší otvorený obchod.
        if has_open_trade:
            before = len(trades)
            evaluate_open_trades_for_symbol(
                symbol,
                candles,
                trades,
                finalize_session=(current_time.time() >= MARKET_CLOSE)
            )
            after = len(trades)

            if before != after:
                print(f"{symbol}: výsledok otvoreného obchodu vyhodnotený.")
            elif any(k[0] == symbol for k in trades):
                print(f"{symbol}: otvorený obchod sa ďalej sleduje.")

        # Po zatvorení už nevytvárame nové signály.
        if not market_open:
            continue

        result = analyze_symbol(symbol, candles)

        if result.get("signal"):
            signals_found += 1
            direction = result["signal"]

            print()
            print(f"{symbol}: {direction} BREAKOUT")
            print(f"Entry: {result['entry']:.2f}")
            print(f"Breakout: {result['breakout']:.2f}")
            print(f"Breakout %: {result['breakout_percent']:.2f}%")
            print(f"ATR: {result['atr']:.2f}")
            print(f"SL: {result['sl']:.2f}")
            print(f"TP: {result['tp']:.2f}")
            print(f"RelVol: {result['rel_volume']:.2f}x")

            if can_send_signal(result, state):

                # ==================================================
                # ALPACA PAPER – ochrana pred duplicitnou pozíciou
                # ==================================================

                symbol = result["symbol"]

                if has_open_position(symbol):
                    print(
                        f"{symbol}: Alpaca už má otvorenú pozíciu – SKIP."
                    )
                    continue

                if has_open_order(symbol):
                    print(
                        f"{symbol}: Alpaca už má otvorenú objednávku – SKIP."
                    )
                    continue

                # ==================================================
                # ALPACA PAPER – AUTOMATICKÝ OBCHOD
                # ==================================================

                try:
                    alpaca_order = place_bracket_order(
                        symbol=result["symbol"],
                        side=result["signal"],
                        qty=1,
                        take_profit=result["tp"],
                        stop_loss=result["sl"],
                    )

                    print(
                        f"{result['symbol']}: "
                        f"ALPACA PAPER ORDER ÚSPEŠNÝ."
                    )

                except Exception as e:
                    print(
                        f"{result['symbol']}: "
                        f"ALPACA ORDER ZLYHAL: {e}"
                    )
                    continue

                # ==================================================
                # ALPACA OBCHOD JE ÚSPEŠNÝ
                # – najprv zapíšeme obchod do scanner state
                # a trade tracking.
                # – nezávisle od Telegramu.
                # ==================================================

                state_key = (
                    result["symbol"],
                    result["signal"]
                )

                state[state_key] = result["candle_time"].isoformat()
                save_state(state)

                signal_time = result["candle_time"].isoformat()
                trade_key = (
                    result["symbol"],
                    result["signal"],
                    signal_time
                )

                trades[trade_key] = {
                    "symbol": result["symbol"],
                    "direction": result["signal"],
                    "signal_time": signal_time,
                    "entry": result["entry"],
                    "sl": result["sl"],
                    "tp": result["tp"],
                    "last_checked": signal_time,
                    "max_favorable": 0.0,
                    "max_adverse": 0.0
                }

                save_trade_tracking(trades)

                print(
                    "ALPACA OBCHOD ZAPÍSANÝ DO "
                    "SCANNER STATE A TRADE TRACKING."
                )

                # ==================================================
                # TELEGRAM – až po úspešnom Alpaca obchode
                # ==================================================

                message = create_telegram_message(result)
                telegram_ok = send_telegram(message)

                if telegram_ok:
                    print(
                        "SIGNÁL ODOSLANÝ DO TELEGRAMU."
                    )
                else:
                    print(
                        "ALPACA OBCHOD JE ÚSPEŠNÝ, "
                        "ALE TELEGRAM SPRÁVU SA NEPODARILO ODOSLAŤ."
                    )
            else:
                print("Signál zablokovaný (duplicitný/cooldown).")

    save_trade_tracking(trades)

    print()
    print(f"{signals_found} signals.")
    print(f"V2.5.1 otvorené obchody na sledovanie: {len(trades)}")
    if os.path.exists(TRADE_STATE_FILE):
        print(f"Trade state: {TRADE_STATE_FILE} uložený.")
    else:
        print(f"Trade state: {TRADE_STATE_FILE} zatiaľ neexistuje.")
    print("RelVol V2.4 = aktuálny objem / medián predchádzajúcich 12 5-min sviečok DNES.")
    print("V2.5.1 = sledovanie TP / SL / AMBIGUOUS / NEITHER + iba aktuálna US seansa pre nové signály.")
    print("=" * 60)


# ============================================================
# SPUSTENIE
# ============================================================

if __name__ == "__main__":
    main()
