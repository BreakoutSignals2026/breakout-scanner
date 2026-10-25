#!/usr/bin/env python3
"""US FAST MICRO 5m virtual-only execution entrypoint, separate from FAST PAPER."""
import sys
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor

import scanner_fast as fast
import fast_micro_v1 as micro


def main(now=None):
    now = now or datetime.now(timezone.utc)
    session = fast.market_session_today()  # Alpaca PAPER CALENDAR GET, never orders
    if session is None:
        print("FAST MICRO: NYSE holiday/weekend; no scan")
        return
    if now < session["open"]:
        print("FAST MICRO: NYSE not yet open; no scan")
        return
    if now > session["close"]:
        print("FAST MICRO: NYSE session closed")
        return
    return micro.process(session, fast.SYMBOLS, fast.get_bars,
                         fast.analyze_symbol, now, workers=8)


if __name__ == "__main__":
    if sys.argv[1:] == ["--close-epoch"]:
        session = fast.market_session_today()
        print(int(session["close"].timestamp()) if session else 0)
    elif not sys.argv[1:]:
        main()
    else:
        raise SystemExit("Usage: python scanner_fast_micro_v1.py [--close-epoch]")
