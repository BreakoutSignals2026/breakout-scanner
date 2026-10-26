#!/usr/bin/env python3
"""Shared READ-ONLY 5-minute candle integrity gates.

Do not interpolate missing prices/volumes. Reject ONLY *new entry signals*
when the reference sequence contains holes; do not prevent position protection,
EOD reconciliation, or exits. Legitimate no-trade minutes on IEX remain gaps.
"""
from datetime import datetime, timedelta, timezone
from math import isfinite


def parse_time(value):
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError("naive timestamp")
        return value.astimezone(timezone.utc)
    parsed=datetime.fromisoformat(str(value).replace("Z","+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("naive timestamp")
    return parsed.astimezone(timezone.utc)


def verify_contiguous(candles, *, key, count, session_open=None, allow_fixed_grid_offset=False):
    """Return (valid, reason), checking the latest COUNT bars.

    No requirement of 5-minute bars across overnight or lunch/session breaks
    unless the user chooses COUNT crossing those boundaries. For new signals,
    a partial lookback is rejected; no synthetic data is produced.
    """
    if len(candles)<count:
        return False, f"insufficient_bars {len(candles)}/{count}"
    tail=candles[-count:]
    try:
        times=[parse_time(c[key]) for c in tail]
    except (ValueError,KeyError,TypeError):
        return False, "invalid_timestamp"
    # XETRA Yahoo virtual feed may supply a consistent fixed-minute offset.
    # Only explicit virtual research opt-in permits it. Never retime candles.
    offset=times[0].minute%5 if allow_fixed_grid_offset else 0
    for t in times:
        if t.second or t.microsecond or t.minute%5!=offset:
            return False, "off_grid_timestamp"
    if session_open is not None:
        opened=parse_time(session_open)
        if times[0]<opened:
            return False, "previous_session_reference"
    for a,b in zip(times,times[1:]):
        if b-a!=timedelta(minutes=5):
            minutes=(b-a).total_seconds()/60
            return False, f"missing_or_duplicate_5m_bar delta={minutes:g}m"
    try:
        for c in tail:
            prices=[float(c[x]) for x in ("o","h","l","c")]
            if not all(isfinite(x) and x>0 for x in prices):
                return False,"invalid_price"
            o,h,l,close=prices
            if l>min(o,close) or h<max(o,close) or h<l:
                return False,"invalid_OHLC"
            v=float(c["v"])
            if not isfinite(v) or v<0:
                return False,"invalid_volume"
    except (KeyError,TypeError,ValueError,OverflowError):
        return False,"missing_OHLCV"
    return True, (f"OK_fixed_offset_{offset}m" if offset else "OK")


def fully_closed_iex(bars, now, *, lag_seconds=10):
    """Use timestamps, not bars[:-1]; when no forming bar exists, keep last closed.

    Alpaca IEX may return only completed bars, or omit an illiquid minute.
    A close is eligible after its 5-minute interval plus a 10s lag.
    """
    end=parse_time(now)-timedelta(seconds=lag_seconds)
    latest={}
    for bar in bars:
        try:
            timestamp=parse_time(bar["t"])
            if timestamp+timedelta(minutes=5)>end:
                continue
            latest[timestamp]=bar
        except (KeyError,ValueError,TypeError):
            continue
    return [latest[t] for t in sorted(latest)]
