"""FAST V3.2.4 regression: IEX data-quality failures never imply Alpaca order submission.

These tests make no external requests and require no API keys.
"""
import inspect
import unittest
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
                        main_source.index("except Exception as exc:"))
        self.assertIn("continue\n        except Exception as exc:", main_source)

    def test_paper_endpoint_kept(self):
        self.assertEqual(fast.API, "https://paper-api.alpaca.markets/v2")
        self.assertEqual(fast.RISK_PER_TRADE, 5.00)
        self.assertEqual(fast.TRAIL_PERCENT, 1.20)


if __name__ == "__main__":
    unittest.main()
