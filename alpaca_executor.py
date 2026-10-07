import os
import requests


ALPACA_BASE_URL = "https://paper-api.alpaca.markets"


def get_headers():
    return {
        "APCA-API-KEY-ID": os.environ["ALPACA_API_KEY"],
        "APCA-API-SECRET-KEY": os.environ["ALPACA_SECRET_KEY"],
        "Content-Type": "application/json",
    }


def place_bracket_order(
    symbol,
    side,
    qty,
    take_profit,
    stop_loss,
):
    """
    Odoslanie bracket orderu do Alpaca Paper.

    side:
        BUY  -> long
        SELL -> short

    qty:
        počet akcií
    """

    side = side.lower()

    order = {
        "symbol": symbol,
        "qty": str(qty),
        "side": side,
        "type": "market",
        "time_in_force": "day",
        "order_class": "bracket",
        "take_profit": {
            "limit_price": str(round(float(take_profit), 2))
        },
        "stop_loss": {
            "stop_price": str(round(float(stop_loss), 2))
        }
    }

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

    return response.json()
