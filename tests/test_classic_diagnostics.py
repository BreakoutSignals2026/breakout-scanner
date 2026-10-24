"""Offline classic scanner diagnostics: entry algorithm remains unchanged."""
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

import scanner_v25_2 as classic


class ClassicSignalsTests(unittest.TestCase):
    def test_insufficient_history_is_reported(self):
        d = {}
        self.assertIsNone(classic.analyze("AAPL", [], d))
        self.assertEqual(d["malo_5min_sviecok"], 1)

    def test_breakout_rejection_is_reported(self):
        now = datetime(2026, 10, 23, 12, 0, tzinfo=ZoneInfo("America/New_York"))
        bars = []
        for n in range(24):
            ts = now - timedelta(minutes=5*(24-n))
            bars.append({"time": ts, "o": 100, "h": 101,
                         "l": 99, "c": 100, "v": 2000})
        bars[-1]["v"] = 5000  # Pass relative-volume filter to test breakout specifically.
        with patch.object(classic, "now_ny", return_value=now):
            diag = {}
            result = classic.analyze("AAPL", bars, diag)
        self.assertIsNone(result)
        self.assertEqual(diag["bez_potvrdeneho_breakoutu"], 1)

    def test_diagnostics_are_optional_and_do_not_change_trades(self):
        self.assertIsNone(classic.analyze("AAPL", []))


    def test_daily_digest_sent_only_once(self):
        session = {"open": datetime(2026, 10, 23, 9, 30, tzinfo=ZoneInfo("America/New_York"))}
        state = {"daily_diagnostics": {
            "date": "2026-10-23", "scans": 3,
            "totals": {"symbols_checked": 30, "bez_potvrdeneho_breakoutu": 25}}}
        with patch.object(classic, "save_state"), patch.object(classic, "telegram") as alert:
            classic.send_classic_daily_diagnostics_once(state, session)
            classic.send_classic_daily_diagnostics_once(state, session)
        alert.assert_called_once()
        self.assertIn("30", alert.call_args.args[0])

    def test_session_start_alert_only_once(self):
        session = {"open": datetime(2026, 10, 23, 9, 30, tzinfo=ZoneInfo("America/New_York"))}
        state = {}
        with patch.object(classic, "save_state"), patch.object(classic, "telegram") as alert:
            classic.announce_classic_started_once(state, session)
            classic.announce_classic_started_once(state, session)
        alert.assert_called_once()


if __name__ == "__main__":
    unittest.main()
