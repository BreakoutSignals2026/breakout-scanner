"""FAST virtual shadow: offline, no keys, no broker orders."""
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import fast_virtual as shadow


def tm(m):
    return datetime(2026, 10, 23, 13, 30, tzinfo=timezone.utc) + timedelta(minutes=m)


SESSION = {
    "date": "2026-10-23",
    "open": tm(0), "close": tm(390),
    "entry_stop_at": tm(360), "liquidate_at": tm(375)
}


def raw(t, o=10, high=10.05, low=9.96, close=10.01):
    return {"t": t.isoformat(), "o": o, "h": high, "l": low,
            "c": close, "v": 5000, "n": 80}


def signal():
    return {"symbol": "BB", "side": "buy", "bar_key": tm(15).isoformat(),
            "breakout_percent": 0.2, "rel_volume": 1.8,
            "signal_bar_notional_usd": 150000}


class ShadowTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name)
        for key, name in (( "VIRTUAL_STATE","state.json"),
                          ("VIRTUAL_SIGNALS","signals.jsonl"),
                          ("VIRTUAL_RESULTS","results.csv")):
            p = patch.object(shadow, key, base / name)
            p.start()
            self.addCleanup(p.stop)

    def test_pending_requires_closed_next_bar(self):
        s = {"phase": "pending", "signal": signal()}
        bars = [shadow.completed_bars([raw(tm(15)),raw(tm(20))],tm(22),SESSION)]
        self.assertEqual(shadow.advance(s,bars[0],SESSION["date"],"BB",tm(22),SESSION,3,1.2)[0]["phase"],"pending")

    def test_trailing_closes_with_slippage(self):
        slot = {"phase": "pending", "signal": signal()}
        candles = [{"t": tm(20),"o":10,"h":10.05,"l":9.99,"c":10.04}]
        slot,result = shadow.advance(slot,candles,SESSION["date"],"BB",tm(25),SESSION,3,1.2)
        self.assertEqual(slot["phase"],"open")
        candles.append({"t": tm(25),"o":10.05,"h":10.3,"l":10.1,"c":10.2})
        slot,result = shadow.advance(slot,candles,SESSION["date"],"BB",tm(30),SESSION,3,1.2)
        self.assertEqual(slot["phase"],"closed")
        self.assertEqual(result["reason"],"virtual_trailing_stop")

    def test_process_records_signal_while_trading_is_disabled(self):
        scan=[raw(tm(0)),raw(tm(5)),raw(tm(10)),raw(tm(15)),raw(tm(20))]
        def get_bars(sym): return scan
        def analyze(sym,bars): return signal()
        got=shadow.process(SESSION,["BB"],get_bars,analyze,tm(22),3,1.2,workers=1)
        self.assertEqual(len(got),1)
        self.assertTrue(shadow.VIRTUAL_SIGNALS.is_file())
        self.assertEqual(shadow.load_state(SESSION)["symbols"]["BB"]["phase"],"pending")
        shadow.process(SESSION,["BB"],get_bars,analyze,tm(22),3,1.2,workers=1)
        self.assertEqual(len(shadow.VIRTUAL_SIGNALS.read_text().splitlines()),1)

    def test_no_order_capital_and_risk_bounds(self):
        entry=shadow.make_entry(signal(),{"t":tm(20),"o":10},3,1.2)
        self.assertLessEqual(entry["qty"]*entry["entry"],1000)
        self.assertLessEqual(entry["qty"]*entry["entry"]*(0.012+0.0005),3)


if __name__ == "__main__":
    unittest.main()
