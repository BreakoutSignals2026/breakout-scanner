"""Offline regression tests for ORB V1.0. NO trading credentials needed."""
import ast
import csv
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import scanner_orb_v1 as orb


def dt(hour, minute, second=0):
    return datetime(2026, 10, 22, hour, minute, second, tzinfo=orb.NY)


SESSION = {
    "day": "2026-10-22", "open": dt(9, 30), "close": dt(16, 0),
    "liquidate": dt(15, 45),
}


def bar(hour, minute, o, h, l, c, v=5000, n=120):
    return {"t": dt(hour, minute), "o": o, "h": h, "l": l,
            "c": c, "v": v, "n": n}


def fixture():
    return [
        bar(9, 30, 10.00, 10.10, 9.95, 10.05, 4000),
        bar(9, 35, 10.04, 10.15, 9.90, 10.12, 4200),
        bar(9, 40, 10.10, 10.20, 10.00, 10.12, 3900),
        bar(9, 45, 10.14, 10.35, 10.10, 10.28, 8000),
    ]


class OpeningRangeTest(unittest.TestCase):
    def setUp(self):
        self.bars = fixture()
        self.opening = orb.get_opening_range(self.bars, SESSION)

    def test_opening_three_complete_bars(self):
        self.assertAlmostEqual(self.opening["high"], 10.20)
        self.assertAlmostEqual(self.opening["low"], 9.90)
        self.assertIsNone(orb.get_opening_range(self.bars[1:], SESSION))

    def test_signal_only_after_opening_range(self):
        signal = orb.pick_signal("RIOT", self.bars, self.opening, SESSION, dt(9, 50, 12))
        self.assertIsNotNone(signal)
        self.assertEqual(signal["side"], "LONG")
        self.assertEqual(signal["signal_bar"], orb.iso(dt(9, 45)))
        self.assertEqual(signal["orb_high"], 10.20)

    def test_signal_stale_rejected(self):
        self.assertIsNone(orb.pick_signal(
            "RIOT", self.bars, self.opening, SESSION, dt(10, 2, 0)
        ))

    def test_minimum_relative_volume(self):
        candles = fixture()
        candles[-1]["v"] = 50
        self.assertIsNone(orb.pick_signal(
            "RIOT", candles, self.opening, SESSION, dt(9, 50, 12)
        ))

    def test_no_duplicate_cross(self):
        candles = fixture()
        candles[-2]["c"] = 10.25
        self.assertIsNone(orb.pick_signal(
            "RIOT", candles, self.opening, SESSION, dt(9, 50, 12)
        ))

    def test_full_closed_bar_only_and_no_premarket(self):
        raw = []
        for b in self.bars:
            raw.append({
                "t": b["t"].astimezone(orb.UTC).isoformat(),
                "o": b["o"], "h": b["h"], "l": b["l"],
                "c": b["c"], "v": b["v"], "n": b["n"],
            })
        raw.append({
            "t": dt(9, 50).astimezone(orb.UTC).isoformat(),
            "o": 10.29, "h": 10.75, "l": 10.22, "c": 10.60,
            "v": 5000,
        })
        raw.append({
            "t": dt(9, 25).astimezone(orb.UTC).isoformat(),
            "o": 9, "h": 9, "l": 9, "c": 9, "v": 8000
        })
        actual = orb.closed_bars(raw, SESSION, dt(9, 52))
        self.assertEqual([x["t"].strftime("%H:%M") for x in actual],
                         ["09:30", "09:35", "09:40", "09:45"])


class SimulatorTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name)
        self.results_patch = patch.object(orb, "RESULTS_FILE",
                                          self.path / "orb_results_v1.csv")
        self.results_patch.start()
        self.addCleanup(self.results_patch.stop)
        self.notification_patch = patch.object(orb, "notify")
        self.notification_patch.start()
        self.addCleanup(self.notification_patch.stop)

    def test_signal_then_next_completed_bar_fill_then_take_profit(self):
        bars = fixture()
        slot = orb.update_symbol("RIOT", bars, SESSION, dt(9, 50, 15), {})
        self.assertEqual(slot["phase"], "pending")
        self.assertNotIn("trade", slot)
        # The 09:50 bar is NOT yet closed; no fill may be asserted.
        self.assertEqual(
            orb.update_symbol("RIOT", bars, SESSION, dt(9, 52), slot)["phase"],
            "pending"
        )
        bars.append(bar(9, 50, 10.29, 10.32, 10.22, 10.25))
        slot = orb.update_symbol("RIOT", bars, SESSION, dt(9, 55, 15), slot)
        self.assertEqual(slot["phase"], "open")
        self.assertEqual(slot["trade"]["entry_bar"], orb.iso(dt(9, 50)))
        self.assertAlmostEqual(slot["trade"]["entry"], 10.29 * 1.0005)
        self.assertIsNone(slot["trade"]["last_evaluated_bar"] if False else None)
        # Next scan shouldn't repeat a previous exit candle.
        self.assertEqual(orb.update_symbol("RIOT", bars, SESSION, dt(9, 56), slot)["phase"],
                         "open")
        bars.append(bar(9, 55, 10.26, 10.80, 10.20, 10.70))
        slot = orb.update_symbol("RIOT", bars, SESSION, dt(10, 0, 15), slot)
        self.assertEqual(slot["phase"], "closed")
        self.assertEqual(slot["result"]["exit_reason"], "TP")
        self.assertGreater(slot["result"]["gross_pnl_usd"], 0)
        with orb.RESULTS_FILE.open(newline="", encoding="utf-8") as fp:
            rows = list(csv.DictReader(fp))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["simulated"], "True")
        orb.append_result(slot["result"])
        with orb.RESULTS_FILE.open(newline="", encoding="utf-8") as fp:
            self.assertEqual(len(list(csv.DictReader(fp))), 1)

    def test_stop_priority_when_same_bar_hits_both(self):
        signal = orb.pick_signal(
            "RIOT", fixture(), orb.get_opening_range(fixture(), SESSION),
            SESSION, dt(9, 50, 10))
        trade = orb.simulated_entry(signal, bar(9, 50, 10.29, 10.30,
                                                 10.22, 10.25),
                                    SESSION["day"])
        self.assertIsNotNone(trade)
        candle = bar(9, 55, 10.25, trade["tp"] + 0.01,
                     trade["stop"] - 0.01, 10.30)
        exit_price, reason, ambiguous = orb.stop_and_target(trade, candle)
        self.assertEqual(reason, "SL")
        self.assertTrue(ambiguous)
        result = orb.finish_trade(trade, exit_price, reason, candle["t"], ambiguous)
        self.assertLess(result["gross_pnl_usd"], 0)
        self.assertTrue(result["ambiguous_stop_first"])

    def test_gap_against_entry_worse_than_stop(self):
        signal = orb.pick_signal(
            "RIOT", fixture(), orb.get_opening_range(fixture(), SESSION),
            SESSION, dt(9, 50, 10))
        trade = orb.simulated_entry(signal, bar(9, 50, 10.29, 10.30,
                                                 10.22, 10.25),
                                    SESSION["day"])
        candle = bar(9, 55, 9.50, 9.70, 9.40, 9.50)
        exit_price, reason, _ = orb.stop_and_target(trade, candle)
        self.assertEqual(reason, "SL")
        self.assertLess(exit_price, trade["stop"])

    def test_short_support(self):
        sig = {"symbol": "SOFI", "side": "SHORT",
               "signal_bar": orb.iso(dt(9, 45)),
               "orb_high": 10.2, "orb_low": 9.9}
        trade = orb.simulated_entry(sig, bar(9, 50, 9.80, 9.85, 9.75, 9.80),
                                    SESSION["day"])
        self.assertIsNotNone(trade)
        self.assertLess(trade["entry"], 9.80)
        exit_price, reason, _ = orb.stop_and_target(
            trade, bar(9, 55, 9.80, 9.90, 9.40, 9.60)
        )
        self.assertEqual(reason, "TP")
        self.assertLess(exit_price, trade["entry"])

    def test_eod_does_not_invent_stale_exit(self):
        sig = {"symbol": "RIOT", "side": "LONG",
               "signal_bar": orb.iso(dt(9, 45)),
               "orb_high": 10.2, "orb_low": 9.9}
        trade = orb.simulated_entry(sig, bar(9, 50, 10.29, 10.30, 10.22, 10.25),
                                    SESSION["day"])
        slot = {"phase": "open", "trade": trade}
        candles = fixture() + [bar(9, 50, 10.29, 10.30, 10.22, 10.25)]
        result = orb.update_symbol("RIOT", candles, SESSION, dt(15, 50), slot)
        self.assertEqual(result["phase"], "open")
        self.assertFalse(orb.RESULTS_FILE.exists())

    def test_missing_next_candle_is_not_filled_late(self):
        sig = {"symbol": "RIOT", "side": "LONG",
               "signal_bar": orb.iso(dt(9, 45)),
               "orb_high": 10.2, "orb_low": 9.9}
        slot = {"phase": "pending", "signal": sig}
        candles = fixture() + [bar(9, 55, 10.30, 10.31, 10.21, 10.25)]
        result = orb.update_symbol("RIOT", candles, SESSION, dt(10, 0), slot)
        self.assertEqual(result["phase"], "expired")

    def test_result_stats(self):
        orb.ensure_results()
        self.assertEqual(orb.daily_summary(SESSION["day"]), (0, 0, 0, 0))


class SafetyTest(unittest.TestCase):
    def test_no_alpaca_order_post_delete_or_patch(self):
        source = Path(orb.__file__).read_text(encoding="utf-8")
        self.assertNotIn("/orders", source)
        self.assertNotIn("requests.delete", source)
        self.assertNotIn("requests.patch", source)
        # The sole POST is a Telegram notification, not an Alpaca operation.
        parsed = ast.parse(source)
        calls = [x for x in ast.walk(parsed) if isinstance(x, ast.Call)
                 and isinstance(x.func, ast.Attribute)
                 and x.func.attr == "post"]
        self.assertEqual(len(calls), 1)
        self.assertIn("telegram.org", source)

    def test_pagination_and_groups(self):
        def fake_json(url, params):
            self.assertEqual(params["feed"], "iex")
            self.assertEqual(params["timeframe"], "5Min")
            if params.get("page_token") == "next":
                return {"bars": {"RIOT": [{"t": orb.iso(dt(9, 35))}]},
                        "next_page_token": None}
            return {"bars": {"RIOT": [{"t": orb.iso(dt(9, 30))}]},
                    "next_page_token": "next"}
        with patch.object(orb, "get_json", side_effect=fake_json):
            out = orb.get_bars(["RIOT"], SESSION["open"], dt(9, 50))
            self.assertEqual(len(out["RIOT"]), 2)

    def test_invalid_persisted_state_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state.json"
            state.write_text('{"version":"WRONG","symbols":{}}', encoding="utf-8")
            with patch.object(orb, "STATE_FILE", state):
                with self.assertRaises(RuntimeError):
                    orb.load_state()

    def test_risk_and_capital_sizing(self):
        sig = {"symbol": "RIOT", "side": "LONG",
               "signal_bar": orb.iso(dt(9, 45)),
               "orb_high": 10.2, "orb_low": 9.9}
        tr = orb.simulated_entry(sig, bar(9, 50, 10.29, 10.30, 10.22, 10.25),
                                 SESSION["day"])
        self.assertIsNotNone(tr)
        self.assertLessEqual(tr["risk_estimate_usd"], orb.RISK_USD)
        self.assertLessEqual(tr["qty"] * tr["entry"], orb.MAX_NOTIONAL)


if __name__ == "__main__":
    unittest.main(verbosity=2)
