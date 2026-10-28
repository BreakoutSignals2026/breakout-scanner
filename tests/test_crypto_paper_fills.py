"""Regression tests: Alpaca crypto fee-in-base qty and broker-safe reconciliation.

Run: python -m unittest discover -s tests -p test_crypto_paper_fills.py -v
No credentials, network requests, paper orders or manual broker changes.
"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import crypto_paper_v1 as paper


ENTRY_QTY = "0.001839213"
EXIT_QTY = "0.001834614"
ENTRY_PRICE = "81549.0765"
EXIT_PRICE = "80533.548"

def fixture():
    trade = {
        "symbol": "BTC/USD",
        "entry_cid": "CRPT1E_BTCUSD_20261028T0835",
        "stop_cid": "CRPT1S_BTCUSD_20261028T0835",
        "take_cid": "CRPT1T_BTCUSD_20261028T0835",
        "status": "protected",
    }
    entry = {
        "id": "entryid", "client_order_id": trade["entry_cid"],
        "symbol": "BTC/USD", "side": "buy",
        "type": "market", "status": "filled",
        "filled_qty": ENTRY_QTY, "filled_avg_price": ENTRY_PRICE,
        "created_at": "2026-10-28T08:40:18.481206Z",
        "filled_at": "2026-10-28T08:40:18.48319Z",
    }
    stop = {
        "id": "stopid", "client_order_id": trade["stop_cid"],
        "symbol": "BTC/USD", "side": "sell",
        "type": "stop_limit", "status": "filled",
        "filled_qty": EXIT_QTY, "filled_avg_price": EXIT_PRICE,
        "created_at": "2026-10-28T08:40:20.373131Z",
        "filled_at": "2026-10-28T13:03:44.153831Z",
    }
    return trade, entry, stop

class CryptoOrderAccounting(unittest.TestCase):
    def test_real_alpaca_btc_qty_difference_is_fee_sized(self):
        trade, entry, stop = fixture()
        values = paper.verified_closed_crypto_fills(trade, entry, stop, [entry, stop], [])
        self.assertEqual(values["implied_base_fee_qty"], paper.dec("0.000004599"))
        self.assertAlmostEqual(float(values["price_only_pnl"]), -1.863102803499, places=7)
        self.assertAlmostEqual(float(values["pnl_after_entry_fee_before_exit_fee"]),
                               -2.238147006322, places=7)

    def test_exact_same_quantity_is_valid(self):
        trade, entry, stop = fixture()
        stop["filled_qty"] = ENTRY_QTY
        result = paper.verified_closed_crypto_fills(trade, entry, stop, [entry, stop], [])
        self.assertEqual(result["implied_base_fee_qty"], paper.dec("0"))

    def test_partial_exit_cannot_be_mistaken_for_fee(self):
        trade, entry, stop = fixture()
        stop["filled_qty"] = "0.001000000"
        with self.assertRaisesRegex(RuntimeError, "NOT a plausible"):
            paper.verified_closed_crypto_fills(trade, entry, stop, [entry, stop], [])

    def test_exit_larger_than_entry_cannot_be_accepted(self):
        trade, entry, stop = fixture()
        stop["filled_qty"] = "0.001840000"
        with self.assertRaisesRegex(RuntimeError, "NOT a plausible"):
            paper.verified_closed_crypto_fills(trade, entry, stop, [entry, stop], [])

    def test_unfilled_stop_cannot_confirm_closed_position(self):
        trade, entry, stop = fixture()
        stop["status"] = "accepted"
        with self.assertRaisesRegex(RuntimeError, "identity/status"):
            paper.verified_closed_crypto_fills(trade, entry, stop, [entry, stop], [])

    def test_wrong_exit_client_id_or_symbol_rejected(self):
        trade, entry, stop = fixture()
        stop["client_order_id"] = "FOREIGN_EXIT"
        with self.assertRaisesRegex(RuntimeError, "identity/status"):
            paper.verified_closed_crypto_fills(trade, entry, stop, [entry, stop], [])
        trade, entry, stop = fixture()
        stop["symbol"] = "ETH/USD"
        with self.assertRaisesRegex(RuntimeError, "identity/status"):
            paper.verified_closed_crypto_fills(trade, entry, stop, [entry, stop], [])

    def test_open_order_blocks_final_pnl(self):
        trade, entry, stop = fixture()
        with self.assertRaisesRegex(RuntimeError, "Open crypto broker"):
            paper.verified_closed_crypto_fills(
                trade, entry, stop, [entry, stop],
                [{"symbol": "BTC/USD", "status": "new"}],
            )

    def test_other_filled_sale_blocks_attribution(self):
        trade, entry, stop = fixture()
        foreign = {
            "client_order_id": "MANUAL_ORDER", "symbol": "BTC/USD",
            "filled_qty": "0.0001", "created_at": "2026-10-28T09:00:00Z",
        }
        with self.assertRaisesRegex(RuntimeError, "Other filled crypto"):
            paper.verified_closed_crypto_fills(
                trade, entry, stop, [entry, stop, foreign], []
            )

    def test_order_timestamp_cannot_precede_entry(self):
        trade, entry, stop = fixture()
        stop["filled_at"] = "2026-10-27T23:00:00Z"
        with self.assertRaisesRegex(RuntimeError, "timestamp"):
            paper.verified_closed_crypto_fills(trade, entry, stop, [entry, stop], [])

    def test_finish_trade_records_only_estimate_and_no_order_submission(self):
        trade, entry, stop = fixture()
        state = {"trades": {"BTCUSD": trade}}
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(paper, "STATE", Path(directory) / "state.json"), \
                 patch.object(paper, "EVENTS", Path(directory) / "events.jsonl"), \
                 patch.object(paper, "positions", return_value={}), \
                 patch.object(paper, "bycid", return_value=None), \
                 patch.object(paper, "alert") as notify, \
                 patch.object(paper, "api", side_effect=AssertionError("Do not call broker API")):
                paper.finish_trade(state, trade, entry, stop, "STOP_LIMIT",
                                   [entry, stop], [])
                self.assertEqual(trade["status"], "closed")
                self.assertEqual(trade["pnl_before_fees"], -1.86)
                self.assertEqual(trade["pnl_after_entry_fee_before_exit_fee"], -2.24)
                self.assertEqual(trade["entry_base_fee_inferred_qty"], "0.000004599")
                self.assertTrue(trade["entry_fee_is_inferred_not_confirmed"])
                self.assertTrue(trade["exit_usd_fee_not_yet_confirmed"])
                self.assertEqual(trade["exit_order_id"], "stopid")
                self.assertTrue(notify.called)
                self.assertTrue(Path(directory, "state.json").exists())
                self.assertEqual(len(Path(directory, "events.jsonl").read_text().splitlines()), 1)

    def test_reappearing_position_keeps_state_unclosed(self):
        trade, entry, stop = fixture()
        state = {"trades": {"BTCUSD": trade}}
        with patch.object(paper, "positions", return_value={"BTCUSD": {"qty": "0.0001"}}):
            with self.assertRaisesRegex(RuntimeError, "reappeared"):
                paper.finish_trade(state, trade, entry, stop, "STOP_LIMIT",
                                   [entry, stop], [])
        self.assertEqual(trade["status"], "protected")

    def test_both_exit_legs_filled_keeps_state_unclosed(self):
        trade, entry, stop = fixture()
        other = {"filled_qty": "0.00001", "status": "filled"}
        with patch.object(paper, "positions", return_value={}), \
             patch.object(paper, "bycid", return_value=other):
            with self.assertRaisesRegex(RuntimeError, "Both crypto exits"):
                paper.finish_trade({"trades": {"BTCUSD": trade}},
                                   trade, entry, stop, "STOP_LIMIT",
                                   [entry, stop], [])
        self.assertEqual(trade["status"], "protected")

    def test_paper_only_endpoint_and_protection_constants(self):
        self.assertEqual(paper.API, "https://paper-api.alpaca.markets/v2")
        self.assertEqual(paper.EST_RISK_USD, 3.0)
        self.assertEqual(paper.STOP_PCT, 1.2)
        self.assertEqual(paper.MAX_OPEN_POSITIONS, 1)

if __name__ == "__main__":
    unittest.main(verbosity=2)
