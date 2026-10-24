"""FAST V3.3.1 regression: IEX data-quality failures never imply Alpaca order submission.

These tests make no external requests and require no API keys.
"""
import inspect
import json
import tempfile
import unittest
from pathlib import Path
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import scanner_fast as fast


class FakeResponse:
    status_code = 200

    def __init__(self, data):
        self.data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self.data


class DataQualityTests(unittest.TestCase):
    def setUp(self):
        fake_auth = patch.object(fast, 'headers', return_value={'TEST': 'NO REAL SECRET'})
        fake_auth.start()
        self.addCleanup(fake_auth.stop)

    def test_spread_over_030_percent_rejected_without_order(self):
        quote = {
            "quote": {
                "bp": 10.00, "ap": 10.16, "bs": 100, "as": 100,
                "t": datetime.now(timezone.utc).isoformat(),
            }
        }
        with patch.object(fast.requests, "get", return_value=FakeResponse(quote)), \
             patch.object(fast, "api", side_effect=AssertionError("ALPACA ORDER API MUST NOT BE CALLED")):
            with self.assertRaises(fast.SignalDataRejected) as ctx:
                fast.latest_quote("FSLY")
            self.assertIn("IEX spread", str(ctx.exception))

    def test_stale_iex_trade_rejected_without_order(self):
        old = datetime.now(timezone.utc) - timedelta(seconds=147)
        result = {"trade": {"p": 10.21, "t": old.isoformat()}}
        with patch.object(fast.requests, "get", return_value=FakeResponse(result)), \
             patch.object(fast, "api", side_effect=AssertionError("ALPACA ORDER API MUST NOT BE CALLED")):
            with self.assertRaises(fast.SignalDataRejected) as ctx:
                fast.latest_trade("PTON")
            self.assertIn("IEX cena stará", str(ctx.exception))

    def test_normal_live_quote_passes(self):
        quote = {"quote": {
            "bp": 10.00, "ap": 10.01, "bs": 100, "as": 100,
            "t": datetime.now(timezone.utc).isoformat(),
        }}
        with patch.object(fast.requests, "get", return_value=FakeResponse(quote)):
            q = fast.latest_quote("SNAP")
            self.assertLess(q["spread_percent"], fast.MAX_SPREAD_PERCENT)

    def test_exception_branch_skips_signals_before_post(self):
        main_source = inspect.getsource(fast.main)
        self.assertIn("except SignalDataRejected as exc:", main_source)
        self.assertIn('vynechaný signál (bez objednávky)', main_source)
        self.assertLess(main_source.index("except SignalDataRejected as exc:"),
                        main_source.rindex("except Exception as exc:"))
        self.assertIn("continue\n        except Exception as exc:", main_source)

    def test_paper_endpoint_kept(self):
        self.assertEqual(fast.API, "https://paper-api.alpaca.markets/v2")
        self.assertEqual(fast.RISK_PER_TRADE, 3.00)
        self.assertEqual(fast.MAX_DAILY_ENTRIES, 8)
        self.assertEqual(fast.MAX_OPEN_FAST_POSITIONS, 2)
        self.assertEqual(fast.DAILY_LOSS_STOP_USD, 12.00)
        self.assertEqual(fast.TRAIL_PERCENT, 1.20)



class DailyGuardTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        file = Path(tmp.name) / "fast.jsonl"
        file.write_text("", encoding="utf-8")
        p = patch.object(fast, "TRADES_FILE", file)
        p.start()
        self.addCleanup(p.stop)
        self.file = file
        self.session = {"date": "2026-10-23",
                        "open": datetime(2026, 10, 23, 9, 30, tzinfo=fast.NY)}

    def event(self, action, symbol, order_id=None, pnl=None):
        data = {"timestamp": "2026-10-23T16:00:00Z",
                "action": action, "symbol": symbol}
        if order_id:
            data["order_id"] = order_id
        if pnl is not None:
            data["pnl"] = pnl
        with self.file.open("a", encoding="utf-8") as out:
            out.write(json.dumps(data) + "\n")

    def guard(self, orders=None, positions=None, state=None):
        with patch.object(fast, "api", return_value=[] if orders is None else orders):
            return fast.daily_entry_guard(self.session, state or {"trades": {}}, positions or {})

    def test_eight_entries_in_persisted_journal(self):
        for i, sym in enumerate(("BB", "PTON", "NOK", "CHWY", "GME", "LUNR", "U", "QBTS")):
            self.event("entry_submitted", sym, f"order{i}")
        result = self.guard()
        self.assertFalse(result["allowed"])
        self.assertEqual(result["entries"], 8)

    def test_no_repeat_for_closed_symbol(self):
        self.event("entry_submitted", "BB", "order1")
        self.event("closed", "BB", pnl=0.2)
        result = self.guard()
        self.assertTrue(result["allowed"])
        self.assertIn("BB", result["symbols"])

    def test_two_positions_block_new_entry(self):
        result = self.guard(positions={"BB": {"unrealized_pl": "2"},
                                       "NOK": {"unrealized_pl": "-1"}})
        self.assertFalse(result["allowed"])

    def test_daily_loss_with_open_positions(self):
        self.event("closed", "HIMS", pnl=-10.50)
        result = self.guard(positions={"GME": {"unrealized_pl": "-2"}})
        self.assertFalse(result["allowed"])
        self.assertEqual(result["daily_pnl"], -12.5)

    def test_alpaca_history_survives_missing_journal_entry(self):
        orders = [{"id": str(i), "client_order_id": f"FAST31E_{sym}_10231400000000",
                   "symbol": sym, "created_at": "2026-10-23T16:00:00Z"}
                  for i, sym in enumerate(("BB", "PTON", "NOK", "CHWY", "GME", "LUNR", "U", "QBTS"))]
        self.assertFalse(self.guard(orders=orders)["allowed"])

    def test_alpaca_and_journal_do_not_double_count(self):
        self.event("entry_submitted", "BB", "same-id")
        order = {"id": "same-id", "client_order_id": "FAST31E_BB_10231400000000",
                 "symbol": "BB", "created_at": "2026-10-23T16:00:00Z"}
        self.assertEqual(self.guard(orders=[order])["entries"], 1)

    def test_missing_journal_blocks_new_entries(self):
        self.file.unlink()
        with self.assertRaises(RuntimeError):
            self.guard()

    def test_incomplete_500_order_page_blocks_new_entries(self):
        with self.assertRaises(RuntimeError):
            self.guard(orders=[{}] * 500)

    def test_pre_post_safety_recheck(self):
        source = inspect.getsource(fast.main)
        self.assertIn("daily_entry_guard(session, state, positions)", source)
        self.assertIn("daily_entry_guard(session, state, fresh_positions)", source)


if __name__ == "__main__":
    unittest.main()
