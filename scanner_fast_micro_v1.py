#!/usr/bin/env python3
"""US FAST MICRO 5m: shared PAPER account, exclusive symbols plus independent virtual comparison."""
import sys
from datetime import datetime, timezone
import fast_micro_paper as paper

import scanner_fast as fast
import fast_micro_v1 as micro
from micro_symbols import SYMBOLS, validate_universe


def main(now=None):
    validate_universe()
    now = now or datetime.now(timezone.utc)
    session = fast.market_session_today()  # Alpaca PAPER CALENDAR GET, never orders
    if session is None:
        print("FAST MICRO: NYSE holiday/weekend; no scan")
        return
    paper_error = None
    try:
        paper.process()  # Reconciliation/EOD first, including when entries are disabled.
    except Exception as exc:
        paper_error = exc
        fast.notify(f"FAST MICRO PAPER ERROR: {exc}; new entries halted this cycle")
    if now < session["open"]:
        print("FAST MICRO: NYSE not yet open; no scan")
        return
    if now > session["close"]:
        print("FAST MICRO: NYSE session closed")
        return
    result = micro.process(session, SYMBOLS, fast.get_bars,
                         fast.analyze_symbol, now, workers=8,
                         validate=fast.validate_live)
    if paper_error is not None:
        raise RuntimeError("MICRO PAPER cycle failed; virtual collection completed") from paper_error
    return result


if __name__ == "__main__":
    if sys.argv[1:] == ["--close-epoch"]:
        session = fast.market_session_today()
        print(int(session["close"].timestamp()) if session else 0)
    elif not sys.argv[1:]:
        main()
    else:
        raise SystemExit("Usage: python scanner_fast_micro_v1.py [--close-epoch]")
