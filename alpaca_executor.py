import os
import requests


# ============================================================
# ALPACA PAPER EXECUTOR
# ============================================================

ALPACA_BASE_URL = "https://paper-api.alpaca.markets"


def get_headers():
    return {
        "APCA-API-KEY-ID": os.environ["ALPACA_API_KEY"],
        "APCA-API-SECRET-KEY": os.environ["ALPACA_SECRET_KEY"],
        "Content-Type": "application/json",
    }


def get_open_positions():
    """Vráti otvorené pozície v Alpaca Paper účte."""

    response = requests.get(
        f"{ALPACA_BASE_URL}/v2/positions",
        headers=get_headers(),
        timeout=20,
    )

    if response.status_code != 200:
        raise RuntimeError(
            f"Alpaca positions failed: "
            f"{response.status_code} {response.text}"
        )

    return response.json()


def has_open_position(symbol):
    """Kontrola, či už máme otvorenú pozíciu pre symbol."""

    positions = get_open_positions()

    for position in positions:
        if position.get("symbol") == symbol:
            return True

    return False


def get_open_orders(symbol=None):
    """Vráti otvorené objednávky."""

    params = {
        "status": "open",
        "limit": 100,
    }

    if symbol:
        params["symbols"] = symbol

    response = requests.get(
        f"{ALPACA_BASE_URL}/v2/orders",
        headers=get_headers(),
        params=params,
        timeout=20,
    )

    if response.status_code != 200:
        raise RuntimeError(
            f"Alpaca orders failed: "
            f"{response.status_code} {response.text}"
        )

    return response.json()


def has_open_order(symbol):
    """Kontrola, či už pre symbol čaká otvorená objednávka."""

    orders = get_open_orders(symbol)

    return len(orders) > 0


def place_bracket_order(
    symbol,
    side,
    qty,
    take_profit,
    stop_loss,
):
    """
    Otvorí Alpaca Paper pozíciu s automatickým TP + SL.

    BUY:
        vstup long
        TP nad vstupom
        SL pod vstupom

    SELL:
        otvorenie short pozície
        TP pod vstupom
        SL nad vstupom
    """

    side = side.lower()

    if side not in ("buy", "sell"):
        raise ValueError(
            f"Neplatný side: {side}"
        )

    order = {
        "symbol": symbol,
        "qty": str(qty),
        "side": side,
        "type": "market",
        "time_in_force": "day",
        "order_class": "bracket",
        "take_profit": {
            "limit_price": str(
                round(float(take_profit), 2)
            )
        },
        "stop_loss": {
            "stop_price": str(
                round(float(stop_loss), 2)
            )
        }
    }

    print("")
    print("======================================")
    print("ALPACA PAPER ORDER")
    print("======================================")
    print(f"Symbol: {symbol}")
    print(f"Side: {side.upper()}")
    print(f"Quantity: {qty}")
    print(f"Take Profit: {take_profit:.2f}")
    print(f"Stop Loss: {stop_loss:.2f}")
    print("======================================")

    response = requests.post(
        f"{ALPACA_BASE_URL}/v2/orders",
        headers=get_headers(),
        json=order,
        timeout=20,
    )

    if response.status_code not in (200, 201):
        raise RuntimeError(
            f"Alpaca order failed: "
            f"{response.status_code} {response.text}"
        )

    data = response.json()

    print("ALPACA ORDER: OK")
    print(f"Order ID: {data.get('id')}")
    print(f"Order status: {data.get('status')}")

    return data
