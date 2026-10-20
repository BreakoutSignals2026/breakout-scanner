# FAST BREAKOUT SCANNER V3.0 (Alpaca Trailing Stop & Risk $5)
# Alpaca PAPER only
# Server-side trailing stop, live-price validation, risk management $5.

import os
import re
import json
import math
import time
import requests
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
from zoneinfo import ZoneInfo

VERSION = "3.0.0"

ALPACA_BASE_URL = "https://paper-api.alpaca.markets/v2"
DATA_URL = "https://data.alpaca.markets/v2/stocks/bars"
LATEST_TRADE_URL = "https://data.alpaca.markets/v2/stocks"

ALPACA_API_KEY = os.getenv("ALPACA_API_KEY", "").strip()
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY", "").strip()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()

SYMBOLS = [
    # Technologické a rastové dravce
    "SOFI", "RIVN", "DKNG", "HIMS", "CHWY", "FSLY", "PATH", "U", "AFRM", "HOOD",
    # Krypto a blockchain väzby (vysoká beta a volatilita)
    "CLSK", "IREN", "MARA", "RIOT", "BITF",
    # Ďalšie obľúbené momentum tituly
    "NIO", "OPEN", "LMND", "CELH", "PLUG"
]

MIN_PRICE = 5.00
MAX_PRICE = 50.00

INTERVAL = "5Min"
BREAKOUT_LOOKBACK = 12
MIN_BREAKOUT_PERCENT = 0.15
VOLUME_LOOKBACK = 12
MIN_REL_VOLUME = 1.50
ATR_PERIOD = 14
ATR_MULTIPLIER = 1.50
MAX_STOP_PERCENT = 1.00
RISK_PER_TRADE = 5.00  # Znížené riziko na 5$

# Trailing stop percento pre Alpaca (napr. 1.2% odstup)
TRAIL_PERCENT = 1.2

MAX_SIGNAL_AGE_MINUTES = 8
MAX_ENTRY_CHASE_PERCENT = 0.25
MAX_TRADE_AGE_MINUTES = 30
MAX_WORKERS = 8

TRADES_FILE = "breakout_trades_fast.txt"
STATE_FILE = "breakout_state_fast.txt"

NY = ZoneInfo("America/New_York")
HTTP = requests.Session()


def now_utc():
    return datetime.now(timezone.utc)


def now_ny():
    return datetime.now(NY)


def stamp():
    return now_utc().isoformat(timespec="seconds")


def market_is_open():
    now = now_ny()
    if now.weekday() >= 5:
        return False
    clock = now.strftime("%H:%M")
    return "09:30" <= clock < "16:00"


def alpaca_headers():
    if not ALPACA_API_KEY or not ALPACA_SECRET_KEY:
        raise RuntimeError("Chýbajú GitHub Secrets ALPACA_API_KEY alebo ALPACA_SECRET_KEY.")
    return {
        "APCA-API-KEY-ID": ALPACA_API_KEY,
        "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY,
        "Content-Type": "application/json",
    }


def alpaca_get(url, params=None, timeout=20):
    response = HTTP.get(url, headers=alpaca_headers(), params=params, timeout=timeout)
    response.raise_for_status()
    return response.json()


def send_telegram(message):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        print(f"[{stamp()}] Telegram preskočený: chýba TELEGRAM_TOKEN alebo CHAT_ID.")
        return False
    started = time.monotonic()
    try:
        response = HTTP.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": message, "parse_mode": "HTML", "disable_web_page_preview": True},
            timeout=15,
        )
        print(f"[{stamp()}] Telegram HTTP {response.status_code}, {time.monotonic()-started:.2f}s")
        response.raise_for_status()
        return True
    except Exception as exc:
        print(f"[{stamp()}] Telegram chyba: {exc}")
        return False


def get_fast_positions():
    data = alpaca_get(f"{ALPACA_BASE_URL}/positions")
    return [p for p in data if p.get("symbol") in SYMBOLS]


def get_open_orders_for_fast():
    data = alpaca_get(
        f"{ALPACA_BASE_URL}/orders",
        params={"status": "open", "limit": 500, "nested": "true"},
    )
    return [o for o in data if o.get("symbol") in SYMBOLS]


def parse_time(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def get_bars(symbol, limit=100):
    data = alpaca_get(
        DATA_URL,
        params={
            "symbols": symbol,
            "timeframe": INTERVAL,
            "limit": limit,
            "feed": "iex",
            "adjustment": "raw",
        },
    )
    return data.get("bars", {}).get(symbol, [])


def get_latest_trade(symbol):
    data = alpaca_get(
        f"{LATEST_TRADE_URL}/{symbol}/trades/latest",
        params={"feed": "iex"},
        timeout=12,
    )
    trade = data.get("trade", {})
    price = float(trade.get("p", 0) or 0)
    trade_time = parse_time(trade.get("t"))
    if price <= 0 or trade_time is None:
        raise RuntimeError(f"{symbol}: neplatná posledná transakcia")
    age_seconds = max(0.0, (now_utc() - trade_time).total_seconds())
    if age_seconds > MAX_TRADE_AGE_MINUTES * 60:
        raise RuntimeError(f"{symbol}: posledný obchod je starý {age_seconds/60:.1f} min")
    return {"price": price, "time": trade_time, "age_seconds": age_seconds}


def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            obj = json.load(f)
            return obj if isinstance(obj, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def save_state(state):
    temp = STATE_FILE + ".tmp"
    with open(temp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, sort_keys=True)
    os.replace(temp, STATE_FILE)


def calculate_atr(bars, period):
    if len(bars) < period + 1:
        return None
    true_ranges = []
    for i in range(1, len(bars)):
        high = float(bars[i]["h"])
        low = float(bars[i]["l"])
        prev_close = float(bars[i - 1]["c"])
        true_ranges.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
    sample = true_ranges[-period:]
    return sum(sample) / len(sample) if sample else None


def calculate_relative_volume(bars, lookback):
    if len(bars) < lookback + 1:
        return None
    current_volume = float(bars[-1].get("v", 0) or 0)
    previous = [float(b.get("v", 0) or 0) for b in bars[-lookback-1:-1]]
    previous = [v for v in previous if v > 0]
    if not previous:
        return None
    average = sum(previous) / len(previous)
    return current_volume / average if average > 0 else None


def analyze_symbol(symbol):
    started = time.monotonic()
    try:
        bars = get_bars(symbol, limit=100)
        required = max(BREAKOUT_LOOKBACK + 2, ATR_PERIOD + 2, VOLUME_LOOKBACK + 2)
        if len(bars) < required:
            return None, f"{symbol}: málo sviečok ({len(bars)})"

        closed = bars[:-1]
        latest = closed[-1]
        entry_close = float(latest["c"])
        candle_high = float(latest["h"])
        candle_low = float(latest["l"])
        candle_time = parse_time(latest.get("t"))
        if candle_time is None:
            return None, f"{symbol}: neplatný čas sviečky"

        signal_age = max(0.0, (now_utc() - candle_time).total_seconds() / 60.0 - 5.0)
        if signal_age > MAX_SIGNAL_AGE_MINUTES:
            return None, f"{symbol}: signál starý {signal_age:.1f} min"
        if not MIN_PRICE <= entry_close <= MAX_PRICE:
            return None, f"{symbol}: cena {entry_close:.2f} mimo rozsahu"

        prior = closed[-BREAKOUT_LOOKBACK-1:-1]
        prior_high = max(float(b["h"]) for b in prior)
        prior_low = min(float(b["l"]) for b in prior)
        atr = calculate_atr(closed, ATR_PERIOD)
        rel_volume = calculate_relative_volume(closed, VOLUME_LOOKBACK)
        if atr is None or atr <= 0 or rel_volume is None:
            return None, f"{symbol}: ATR/objem sa nedá vypočítať"
        if rel_volume < MIN_REL_VOLUME:
            return None, f"{symbol}: relatívny objem {rel_volume:.2f}x"

        side = None
        breakout_pct = 0.0
        breakout_level = None
        if candle_high > prior_high and entry_close > prior_high:
            breakout_pct = (entry_close - prior_high) / prior_high * 100
            if breakout_pct >= MIN_BREAKOUT_PERCENT:
                side = "buy"  # v Alpaca terminológii pre otvorená long pozícia
                breakout_level = prior_high
        elif candle_low < prior_low and entry_close < prior_low:
            breakout_pct = (prior_low - entry_close) / prior_low * 100
            if breakout_pct >= MIN_BREAKOUT_PERCENT:
                side = "sell"  # v Alpaca pre otvorená short pozícia (sell order)
                breakout_level = prior_low

        if side is None:
            return None, f"{symbol}: bez potvrdeného breakoutu"

        stop_distance = min(atr * ATR_MULTIPLIER, entry_close * MAX_STOP_PERCENT / 100)
        if stop_distance <= 0:
            return None, f"{symbol}: neplatná vzdialenosť SL"

        if side == "buy":
            stop = entry_close - stop_distance
        else:
            stop = entry_close + stop_distance

        return {
            "symbol": symbol,
            "side": side,
            "entry_close": round(entry_close, 4),
            "stop": round(stop, 2),
            "stop_distance": stop_distance,
            "rel_volume": round(rel_volume, 2),
            "breakout_percent": round(breakout_pct, 3),
            "breakout_level": breakout_level,
            "bar_time": candle_time.isoformat(),
            "bar_key": candle_time.isoformat(),
            "signal_age_minutes": round(signal_age, 2),
            "analysis_seconds": round(time.monotonic() - started, 2),
        }, None
    except Exception as exc:
        return None, f"{symbol}: chyba analýzy: {exc}"


def validate_live_entry(signal):
    live = get_latest_trade(signal["symbol"])
    price = live["price"]
    reference = signal["entry_close"]
    chase_pct = abs(price - reference) / reference * 100
    if chase_pct > MAX_ENTRY_CHASE_PERCENT:
        return None, f"cena ušla {chase_pct:.2f}% od signálu (limit {MAX_ENTRY_CHASE_PERCENT:.2f}%)"

    level = float(signal["breakout_level"])
    if signal["side"] == "buy" and price <= level:
        return None, f"cena {price:.2f} už nie je nad breakout úrovňou {level:.2f}"
    if signal["side"] == "sell" and price >= level:
        return None, f"cena {price:.2f} už nie je pod breakout úrovňou {level:.2f}"

    distance = float(signal["stop_distance"])
    updated = dict(signal)
    updated["entry"] = round(price, 2)
    if updated["side"] == "buy":
        updated["stop"] = round(price - distance, 2)
    else:
        updated["stop"] = round(price + distance, 2)
    updated["live_trade_time"] = live["time"].isoformat()
    updated["live_trade_age_seconds"] = round(live["age_seconds"], 1)
    updated["chase_percent"] = round(chase_pct, 3)
    return updated, None


def calculate_quantity(entry, stop):
    distance = abs(float(entry) - float(stop))
    if distance <= 0:
        return 0
    return max(0, math.floor(RISK_PER_TRADE / distance))


def submit_trailing_stop_trade(signal, quantity):
    if quantity < 1:
        raise RuntimeError("Množstvo je 0 akcií; riziko $5 je príliš malé pre túto volatilitu.")
    
    client_id = "FAST30_" + signal["symbol"] + "_" + re.sub(
        r"[^0-9]", "", signal["bar_time"]
    )[-12:]
    
    # Použijeme Order Class "trailing_stop" priamo na serveroch Alpaca
    payload = {
        "symbol": signal["symbol"],
        "qty": str(quantity),
        "side": signal["side"],
        "type": "market",
        "time_in_force": "gtc",
        "order_class": "trailing_stop",
        "trail_percent": str(TRAIL_PERCENT),
        "client_order_id": client_id,
    }
    
    started = time.monotonic()
    response = HTTP.post(
        f"{ALPACA_BASE_URL}/orders",
        headers=alpaca_headers(),
        json=payload,
        timeout=20,
    )
    elapsed = time.monotonic() - started
    print(f"[{stamp()}] TRAILING ORDER {signal['symbol']} HTTP {response.status_code}, {elapsed:.2f}s")
    if response.status_code >= 400:
        raise RuntimeError(f"Alpaca objednávka odmietnutá ({response.status_code}): {response.text}")
    return response.json(), elapsed


def log_trade(record):
    with open(TRADES_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def check_closed_positions(current_positions, state):
    """
    Skontroluje uzavreté pozície a odošle PnL report do Telegramu.
    """
    previous_positions = state.get("last_positions", {})
    current_symbols = {p.get("symbol") for p in current_positions}
    
    for symbol, pos_info in previous_positions.items():
        if symbol not in current_symbols:
            side = pos_info.get("side", "unknown")
            qty = float(pos_info.get("qty", 0))
            entry_price = float(pos_info.get("avg_entry_price", 0))
            
            try:
                orders_resp = alpaca_get(f"{ALPACA_BASE_URL}/orders", params={"status": "closed", "limit": 10})
                exit_price = entry_price
                
                for ord_item in orders_resp:
                    if ord_item.get("symbol") == symbol and ord_item.get("status") == "filled":
                        filled_avg_price = ord_item.get("filled_avg_price")
                        if filled_avg_price:
                            exit_price = float(filled_avg_price)
                            break
                
                if side == "long":
                    diff = exit_price - entry_price
                else:
                    diff = entry_price - exit_price
                
                total_pnl = diff * qty
                pnl_pct = (diff / entry_price * 100) if entry_price > 0 else 0
                
                emoji = "🟢" if total_pnl >= 0 else "🔴"
                
                msg = (
                    f"📊 <b>VYHODNOTENIE UZATVORENÉHO OBCHODU</b>\n\n"
                    f"• <b>Symbol:</b> {symbol} ({side.upper()})\n"
                    f"• <b>Množstvo:</b> {qty} ks\n"
                    f"• <b>Vstup:</b> ${entry_price:.2f} ➡️ <b>Výstup:</b> ${exit_price:.2f}\n"
                    f"• <b>Celkový výsledok:</b> <b>{total_pnl:+.2f}$ ({pnl_pct:+.2f}%)</b> {emoji}"
                )
                send_telegram(msg)
                
            except Exception as e:
                print(f"[{stamp()}] Chyba pri vyhodnotení zatvorenej pozície {symbol}: {e}")

    new_state_positions = {}
    for p in current_positions:
        new_state_positions[p.get("symbol")] = {
            "side": "long" if float(p.get("qty", 0)) > 0 else "short",
            "qty": p.get("qty"),
            "avg_entry_price": p.get("avg_entry_price")
        }
    state["last_positions"] = new_state_positions
    return state


def main():
    run_started = time.monotonic()
    print(f"=== FAST BREAKOUT SCANNER V{VERSION} (Alpaca Trailing) ===")
    print(f"[{stamp()}] NY time: {now_ny().isoformat(timespec='seconds')}")
    print(f"[{stamp()}] Symbols={len(SYMBOLS)} interval={INTERVAL} risk=${RISK_PER_TRADE}")
    state = load_state()

    try:
        positions = get_fast_positions()
        orders = get_open_orders_for_fast()
        print(f"[{stamp()}] FAST positions={len(positions)}, open orders={len(orders)}")
        
        # 1. Skontrolujeme uzatvorené pozície a odošleme PnL report
        state = check_closed_positions(positions, state)
        save_state(state)
    except Exception as exc:
        print(f"[{stamp()}] Monitor/API chyba: {exc}")
        send_telegram(f"⚠️ FAST V{VERSION}: chyba pri kontrole pozícií:\n{exc}")
        return

    if not market_is_open():
        print(f"[{stamp()}] Trh je zatvorený; nové obchody sa neotvárajú.")
        print(f"Celkový čas: {time.monotonic() - run_started:.2f}s")
        return

    held = {p.get("symbol") for p in positions}
    pending = {o.get("symbol") for o in orders}
    blocked = held | pending
    print(f"[{stamp()}] Blokované symboly: {sorted(blocked)}")

    scan_started = time.monotonic()
    signals = []
    errors = []
    to_scan = [symbol for symbol in SYMBOLS if symbol not in blocked]

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(analyze_symbol, symbol): symbol for symbol in to_scan}
        for future in as_completed(futures):
            signal, error = future.result()
            if signal:
                symbol = signal["symbol"]
                if state.get("last_signal_bar", {}).get(symbol) == signal["bar_key"]:
                    print(f"[{stamp()}] {symbol}: táto sviečka už bola spracovaná")
                    continue
                signals.append(signal)
            elif error:
                errors.append(error)

    print(f"[{stamp()}] Súbežný scan: {time.monotonic()-scan_started:.2f}s; kandidáti={len(signals)}")
    if not signals:
        print("[INFO] Nenašiel sa žiadny nový breakout.")
        print(f"Celkový čas: {time.monotonic() - run_started:.2f}s")
        return

    signals.sort(key=lambda item: (item["rel_volume"], item["breakout_percent"]), reverse=True)

    selected = None
    for candidate in signals:
        try:
            live_signal, reason = validate_live_entry(candidate)
            if live_signal is None:
                print(f"[{stamp()}] {candidate['symbol']}: vynechaný — {reason}")
                state.setdefault("last_signal_bar", {})[candidate["symbol"]] = candidate["bar_key"]
                continue
            selected = live_signal
            break
        except Exception as exc:
            print(f"[{stamp()}] {candidate['symbol']}: kontrola aktuálnej ceny zlyhala: {exc}")

    if selected is None:
        save_state(state)
        print(f"[{stamp()}] Žiadny kandidát neprešiel kontrolou aktuálnej ceny.")
        print(f"Celkový čas: {time.monotonic() - run_started:.2f}s")
        return

    qty = calculate_quantity(selected["entry"], selected["stop"])
    if qty < 1:
        state.setdefault("last_signal_bar", {})[selected["symbol"]] = selected["bar_key"]
        save_state(state)
        print(f"[{stamp()}] {selected['symbol']}: preskočené, vypočítané množstvo je 0.")
        return

    print(
        f"[{stamp()}] ODOSIELAM {selected['symbol']} {selected['side'].upper()} "
        f"entry~${selected['entry']:.2f} qty={qty} trail_pct={TRAIL_PERCENT}%"
    )
    try:
        order, order_seconds = submit_trailing_stop_trade(selected, qty)
    except Exception as exc:
        print(f"[{stamp()}] Objednávka zlyhala: {exc}")
        send_telegram(f"❌ FAST V{VERSION}: objednávka {selected['symbol']} zlyhala.\n{exc}")
        return

    selected.update({
        "quantity": qty,
        "order_id": order.get("id"),
        "order_status": order.get("status"),
        "client_order_id": order.get("client_order_id"),
        "submitted_at": stamp(),
        "order_api_seconds": round(order_seconds, 2),
        "run_seconds_to_order": round(time.monotonic() - run_started, 2),
    })

    state.setdefault("last_signal_bar", {})[selected["symbol"]] = selected["bar_key"]
    save_state(state)
    log_trade(selected)

    send_telegram(
        f"🚀 FAST V{VERSION} ALPACA TRAILING OBJEDNÁVKA\n"
        f"{selected['symbol']} {selected['side'].upper()}\n"
        f"Vstup: ${selected['entry']:.2f}\n"
        f"Množstvo: {qty} akcií | Plánované riziko: ${RISK_PER_TRADE:.2f}\n"
        f"Alpaca Server Trailing Stop: {TRAIL_PERCENT}%\n"
        f"Rel. objem: {selected['rel_volume']:.2f}x | breakout: {selected['breakout_percent']:.3f}%\n"
        f"Stav v Alpaca: {order.get('status', 'neznámy')}"
    )
    print(f"[{stamp()}] Hotovo. Celkový čas: {time.monotonic() - run_started:.2f}s")


if __name__ == "__main__":
    main()
