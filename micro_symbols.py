"""Exclusive MICRO universe for a shared Alpaca PAPER account."""
SYMBOLS = ("PFE", "T", "LYFT", "UPST", "KGC", "CDE",
           "HPQ", "XPEV", "NCLH", "AG", "HBAN", "KEY")


def validate_universe():
    import scanner_fast as fast
    import scanner_v25_2 as classic
    overlap = set(SYMBOLS) & (set(fast.SYMBOLS) | set(classic.SYMBOLS))
    if len(set(SYMBOLS)) != len(SYMBOLS) or overlap:
        raise RuntimeError(f"MICRO universe must be unique and exclusive; overlap={sorted(overlap)}")
    return SYMBOLS
