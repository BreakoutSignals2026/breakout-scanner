"""Regression tests for BREAKOUT V2.5.2 lost-state reconciliation.

Run: python -m unittest discover -s tests -p 'test_breakout_recovery.py'
No Alpaca credentials needed; no orders are sent.
"""
import unittest
from unittest.mock import patch
import scanner_v25_2 as scanner


CID = "B252_INTC_202610271200"


def parent(exit_status="filled", exit_qty="8", entry_qty="8"):
    return {
        "id": "entry-1", "client_order_id": CID, "symbol": "INTC",
        "side": "buy", "status": "filled", "filled_qty": entry_qty,
        "legs": [
            {"id": "tp-1", "side": "sell", "type": "limit",
             "status": "canceled", "filled_qty": "0"},
            {"id": "sl-1", "side": "sell", "type": "stop",
             "status": exit_status, "filled_qty": exit_qty},
        ],
    }


class RecoveryTests(unittest.TestCase):
    def check_case(self, order, positions=None, open_orders=None, blocked=False):
        with patch.object(scanner, "api", return_value=[{"id": "entry-1", "client_order_id": CID}]), \
             patch.object(scanner, "order_by_client", return_value=order), \
             patch.object(scanner, "order_by_id", return_value=order), \
             patch.object(scanner, "all_positions", return_value=positions or {}), \
             patch.object(scanner, "open_orders", return_value=open_orders or []):
            if blocked:
                with self.assertRaises(RuntimeError):
                    scanner.ensure_no_untracked_classic_orders({"trades": {}})
            else:
                scanner.ensure_no_untracked_classic_orders({"trades": {}})

    def test_closed_bracket_is_not_unknown(self):
        self.check_case(parent())

    def test_partial_exit_remains_blocked(self):
        self.check_case(parent(exit_qty="3"), blocked=True)

    def test_open_stop_remains_blocked(self):
        self.check_case(parent(exit_status="new", exit_qty="0"), blocked=True)

    def test_open_position_remains_blocked(self):
        self.check_case(parent(), positions={"INTC": {"qty": "8"}}, blocked=True)

    def test_live_same_symbol_order_remains_blocked(self):
        self.check_case(parent(), open_orders=[
            {"symbol": "INTC", "status": "new"}], blocked=True)

    def test_missing_parent_remains_blocked(self):
        self.check_case(None, blocked=True)

    def test_known_order_not_reclassified(self):
        with patch.object(scanner, "api", return_value=[{"client_order_id": CID}]), \
             patch.object(scanner, "all_positions", return_value={}), \
             patch.object(scanner, "open_orders", return_value=[]):
            scanner.ensure_no_untracked_classic_orders(
                {"trades": {CID: {"client_id": CID}}})


    def test_external_next_session_close_blocks_second_eod_sell(self):
        tsla = {
            "symbol": "TSLA", "side": "BUY", "status": "external_exit_pending",
            "client_id": "B252_TSLA_202610271120",
            "external_exit_order_id": "manual-tsla-2-share-sell",
        }
        with patch.object(scanner, "api", side_effect=AssertionError("no broker mutation")), \
             patch.object(scanner, "get_tracked_order", side_effect=AssertionError("no parent needed")):
            scanner.eod_close_trade(tsla, {"trades": {tsla["client_id"]: tsla}})

    def test_pending_external_close_is_reconciled_without_duplicate_sell(self):
        tsla = {
            "symbol": "TSLA", "side": "BUY", "status": "external_exit_pending",
            "client_id": "B252_TSLA_202610271120",
            "entry_id": "entry-tsla", "external_exit_order_id": "manual-tsla",
        }
        parent = {
            "id": "entry-tsla", "client_order_id": tsla["client_id"],
            "symbol": "TSLA", "side": "buy", "status": "filled",
            "filled_qty": "2",
        }
        ext = {"id": "manual-tsla", "symbol": "TSLA", "side": "sell",
               "status": "accepted", "filled_qty": "0"}
        with patch.object(scanner, "get_tracked_order", return_value=parent), \
             patch.object(scanner, "order_by_id", return_value=ext), \
             patch.object(scanner, "api", side_effect=AssertionError("no order POST")), \
             patch.object(scanner, "telegram"), \
             patch.object(scanner, "report_close_if_verified") as report:
            safe = scanner.reconcile(
                {"trades": {tsla["client_id"]: tsla}},
                {"TSLA": {"symbol": "TSLA", "qty": "2"}})
        self.assertFalse(safe)
        report.assert_not_called()

    def test_filled_manual_close_reconciles_and_reports_results(self):
        tsla = {
            "symbol": "TSLA", "side": "BUY", "status": "external_exit_pending",
            "client_id": "B252_TSLA_202610271120",
            "entry_id": "entry-tsla", "external_exit_order_id": "manual-tsla",
        }
        parent = {
            "id": "entry-tsla", "client_order_id": tsla["client_id"],
            "symbol": "TSLA", "side": "buy", "status": "filled",
            "filled_qty": "2",
        }
        ext = {"id": "manual-tsla", "symbol": "TSLA", "side": "sell",
               "status": "filled", "filled_qty": "2"}
        with patch.object(scanner, "get_tracked_order", return_value=parent), \
             patch.object(scanner, "order_by_id", return_value=ext), \
             patch.object(scanner, "report_close_if_verified") as report:
            scanner.reconcile({"trades": {tsla["client_id"]: tsla}}, {})
        report.assert_called_once()



if __name__ == "__main__":
    unittest.main()
