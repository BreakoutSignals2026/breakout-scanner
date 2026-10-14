import os, re, json, math, requests
from datetime import datetime, time
from zoneinfo import ZoneInfo

# FAST BREAKOUT SCANNER V2.7 - Alpaca PAPER only
# Multiple positions across different symbols are allowed.
# One best NEW signal is submitted per workflow run.
ALPACA_BASE_URL = "https://paper-api.alpaca.markets/v2"
DATA_URL = "https://data.alpaca.markets/v2/stocks/bars"
ALPACA_API_KEY = os.getenv("ALPACA_API_KEY")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
SYMBOLS = ["SOFI", "RIVN", "F", "T", "PFE", "AAL", "ACAD", "DNLI", "DKNG", "NU", "VZ", "NOK", "PCG", "IREN", "REXR", "HIMS", "VALE", "CLSK", "CCL", "KHC", "PBR", "CTVA", "AES", "CHWY", "FSLY", "PATH", "U"]
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
NY = ZoneInfo("America/New_York")
HTTP = requests.Session()


def now_ny():
    return datetime.now(NY)


def market_is_open():
    now = now_ny()
    return now.weekday() < 5 and time(9, 30) <= now.time() < time(16, 0)


def alpaca_headers():
    if not ALPACA_API_KEY or not ALPACA_SECRET_KEY:
        raise RuntimeError("Missing ALPACA_API_KEY or ALPACA_SECRET_KEY GitHub Secret.")
    return {"APCA-API-KEY-ID": ALPACA_API_KEY, "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY, "Content-Type": "application/json"}


def send_telegram(message):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        print("Telegram secrets not configured.")
        return
    try:
        r = HTTP.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", json={"chat_id": CHAT_ID, "text": message}, timeout=15)
        if r.status_code != 200:
            print("Telegram error:", r.text)
    except Exception as exc:
        print("Telegram exception:", exc)


def get_fast_positions():
    r = HTTP.get(f"{ALPACA_BASE_URL}/positions", headers=alpaca_headers(), timeout=20)
    r.raise_for_status()
    watch = set(SYMBOLS)
    return [p for p in r.json() if p.get("symbol", "").upper() in watch]


def get_open_orders_for_fast():
    r = HTTP.get(f"{ALPACA_BASE_URL}/orders", headers=alpaca_headers(), params={"status": "open", "limit": 500, "nested": "true"}, timeout=20)
    r.raise_for_status()
    watch = set(SYMBOLS)
    return [o for o in r.json() if o.get("symbol", "").upper() in watch]


def get_bars(symbol, limit=100):
    r = HTTP.get(DATA_URL, headers=alpaca_headers(), params={"symbols": symbol, "timeframe": INTERVAL, "limit": limit, "feed": "iex", "adjustment": "raw"}, timeout=20)
    r.raise_for_status()
    return r.json().get("bars", {}).get(symbol, [])


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
        high, low = float(bars[i]["h"]), float(bars[i]["l"])
        prev = float(bars[i - 1]["c"])
        trs.append(max(high - low, abs(high - prev), abs(low - prev)))
    return sum(trs[-period:]) / period if len(trs) >= period else None


def calculate_relative_volume(bars, lookback=12):
    if len(bars) < lookback + 1:
        return None
    current = float(bars[-1]["v"])
    previous = [float(b["v"]) for b in bars[-lookback-1:-1]]
    average = sum(previous) / len(previous) if previous else 0
    return current / average if average > 0 else None


def analyze_symbol(symbol):
    bars = get_bars(symbol, 100)
    if not bars:
        print(" REJECT: No market data.")
        return None
    required = max(BREAKOUT_LOOKBACK + 2, ATR_PERIOD + 2, VOLUME_LOOKBACK + 2)
    if len(bars) < required:
        print(f" REJECT: Not enough bars ({len(bars)}; need {required}).")
        return None
    closed = bars[:-1]  # exclude forming candle
    latest = closed[-1]
    entry, candle_high, candle_low = float(latest["c"]), float(latest["h"]), float(latest["l"])
    if not MIN_PRICE <= entry <= MAX_PRICE:
        print(f" REJECT: Price ${entry:.2f} outside ${MIN_PRICE:.2f}-${MAX_PRICE:.2f}.")
        return None
    previous = closed[-BREAKOUT_LOOKBACK-1:-1]
    previous_high = max(float(b["h"]) for b in previous)
    previous_low = min(float(b["l"]) for b in previous)
    atr = calculate_atr(closed, ATR_PERIOD)
    rel = calculate_relative_volume(closed, VOLUME_LOOKBACK)
    if not atr or atr <= 0 or rel is None:
        print(" REJECT: ATR or relative volume unavailable.")
        return None
    up = (entry - previous_high) / previous_high * 100
    down = (previous_low - entry) / previous_low * 100
    long_ok = candle_high > previous_high and entry > previous_high and up >= MIN_BREAKOUT_PERCENT
    short_ok = candle_low < previous_low and entry < previous_low and down >= MIN_BREAKOUT_PERCENT
    volume_ok = rel >= MIN_REL_VOLUME
    print(f" DATA: Price=${entry:.2f} High=${previous_high:.2f} Low=${previous_low:.2f} Up={up:.3f}% Down={down:.3f}% RelVol={rel:.2f}x ATR=${atr:.4f}")
    print(f" CHECK: Breakout={'YES' if long_ok or short_ok else 'NO'}; Volume={'YES' if volume_ok else 'NO'}")
    if not (long_ok or short_ok) or not volume_ok:
        return None
    side = "buy" if long_ok else "sell"
    breakout_pct = up if side == "buy" else down
    distance = min(atr * ATR_MULTIPLIER, entry * MAX_STOP_PERCENT / 100)
    if distance <= 0:
        return None
    stop, target = ((entry - distance, entry + distance * RISK_REWARD) if side == "buy" else (entry + distance, entry - distance * RISK_REWARD))
    stop, target = round(stop, 2), round(target, 2)
    if side == "buy" and not stop < entry < target:
        print(" REJECT: Invalid rounded BUY stop/target.")
        return None
    if side == "sell" and not target < entry < stop:
        print(" REJECT: Invalid rounded SELL stop/target.")
        return None
    print(f" VALID SIGNAL: {side.upper()} {symbol}")
    return {"symbol": symbol, "side": side, "entry": entry, "breakout": previous_high if side == "buy" else previous_low, "stop": stop, "target": target, "atr": atr, "rel_volume": rel, "breakout_percent": breakout_pct, "bar_time": latest["t"]}


def calculate_quantity(entry, stop):
    distance = abs(entry - stop)
    return math.floor(RISK_PER_TRADE / distance) if distance > 0 else 0


def submit_bracket_trade(signal, quantity):
    bar_id = bar_key(signal["bar_time"])
    payload = {
        "symbol": signal["symbol"], "qty": str(quantity), "side": signal["side"],
        "type": "market", "time_in_force": "gtc", "order_class": "bracket",
        "take_profit": {"limit_price": f"{signal['target']:.2f}"},
        "stop_loss": {"stop_price": f"{signal['stop']:.2f}"},
        "client_order_id": f"FAST_{signal['symbol']}_{bar_id}",
    }
    r = HTTP.post(f"{ALPACA_BASE_URL}/orders", headers=alpaca_headers(), json=payload, timeout=20)
    if r.status_code >= 400:
        print("Alpaca order error:", r.status_code, r.text)
        send_telegram(f"FAST PAPER ORDER ERROR\n{signal['symbol']} {signal['side'].upper()}\nHTTP {r.status_code}\n{r.text[:1000]}")
        return None
    return r.json()


def log_trade(signal, qty, order):
    with open(TRADES_FILE, "a", encoding="utf-8") as f:
        f.write(f"{now_ny():%Y-%m-%d %H:%M:%S} | FAST V2.7 | {signal['symbol']} | {signal['side'].upper()} | Qty={qty} | EntryReference={signal['entry']:.2f} | SL={signal['stop']:.2f} | TP={signal['target']:.2f} | RelVol={signal['rel_volume']:.2f} | ATR={signal['atr']:.4f} | Bar={signal['bar_time']} | OrderID={order.get('id')} | Status={order.get('status')}\n")


def main():
    print("=" * 60)
    print("FAST BREAKOUT SCANNER V2.7 - AUTOMATIC PAPER TRADING")
    print("Alpaca PAPER Trading | Multiple positions allowed")
    print("=" * 60)
    print("New York time:", now_ny().strftime("%Y-%m-%d %H:%M:%S"))
    if not market_is_open():
        print("US market is CLOSED.")
        return
    print("US market is OPEN.")
    try:
        positions = get_fast_positions()
        orders = get_open_orders_for_fast()
    except Exception as exc:
        msg = f"FAST scanner could not check Alpaca positions/orders:\n{exc}"
        print(msg)
        send_telegram(msg)
        return
    held_symbols = {p.get("symbol", "").upper() for p in positions}
    pending_symbols = {o.get("symbol", "").upper() for o in orders}
    state = load_state()
    print(f"Existing FAST-watchlist positions: {len(held_symbols)}")
    print(f"Symbols with open orders: {len(pending_symbols)}")
    if held_symbols:
        print("Open position symbols:", ", ".join(sorted(held_symbols)))

    print(f"\nScanning {len(SYMBOLS)} FAST symbols...")
    signals = []
    for symbol in SYMBOLS:
        print(f"\nScanning {symbol}...")
        if symbol in held_symbols:
            print(" SKIP: A position in this symbol is already open; other symbols will still be scanned.")
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
    # Preserve original behaviour: choose strongest signal, one new order per run.
    # Later runs may add positions in other symbols; no global one-position lock.
    signals.sort(key=lambda x: (x["rel_volume"], x["breakout_percent"]), reverse=True)
    signal = signals[0]
    qty = calculate_quantity(signal["entry"], signal["stop"])
    if qty < 1:
        print("Trade skipped: quantity would be below 1 share.")
        return
    risk = abs(signal["entry"] - signal["stop"]) * qty
    print("\nBEST NEW FAST SIGNAL:")
    print(f"{signal['symbol']} {signal['side'].upper()} | Reference=${signal['entry']:.2f} | SL=${signal['stop']:.2f} | TP=${signal['target']:.2f}")
    print(f"Quantity: {qty} | Estimated risk: ${risk:.2f} | Candle: {signal['bar_time']}")
    print("\nSubmitting Alpaca PAPER bracket order...")
    try:
        order = submit_bracket_trade(signal, qty)
    except Exception as exc:
        msg = f"FAST order submission exception for {signal['symbol']}:\n{exc}"
        print(msg)
        send_telegram(msg)
        return
    if not order:
        print("Trade was not confirmed as submitted; state not updated.")
        return
    print("Order submitted. ID:", order.get("id"), "Status:", order.get("status"))
    try:
        log_trade(signal, qty, order)
        state[signal["symbol"]] = bar_key(signal["bar_time"])
        save_state(state)
    except Exception as exc:
        print("WARNING: order submitted but state/log save failed:", exc)
        send_telegram(f"FAST WARNING: order may already exist; scanner state/log save failed. Check Alpaca Orders before rerunning.\n{signal['symbol']} OrderID={order.get('id')}\n{exc}")
    send_telegram(
        "FAST ALPACA PAPER ORDER SUBMITTED\n\n"
        f"{signal['symbol']} {signal['side'].upper()}\nQuantity: {qty}\n"
        f"Signal reference: ${signal['entry']:.2f}\nStop-loss: ${signal['stop']:.2f}\n"
        f"Take-profit: ${signal['target']:.2f}\nEstimated risk at signal price: ${risk:.2f}\n"
        f"Relative volume: {signal['rel_volume']:.2f}x\nStatus: {order.get('status')}\n"
        f"Order ID: {order.get('id')}\n\nPaper trading. Verify entry fill and both protective orders in Alpaca."
    )
    print("FAST trade submission completed.")


if __name__ == "__main__":
    main()
