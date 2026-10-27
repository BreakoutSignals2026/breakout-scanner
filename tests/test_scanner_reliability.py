"""Guardrails for resilient stock scanner launch and XETRA source integrity."""
import csv
import tempfile
import unittest
from datetime import datetime,timedelta,timezone
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from equity_candle_guard import verify_contiguous
import scanner_xetra_v1 as xetra

ROOT=Path(__file__).resolve().parent.parent
NY=ZoneInfo("America/New_York")
UTC=timezone.utc


def sample(start, count=13, step=5):
    return [{"t":(start+timedelta(minutes=i*step)).isoformat(),
             "o":100.0,"h":101.0,"l":99.0,"c":100.3,"v":300.0}
            for i in range(count)]


class VenueCandleGuardTests(unittest.TestCase):
    def test_xetra_accepts_fixed_nonzero_grid_offset_without_retiming(self):
        first=datetime(2026,10,26,8,1,tzinfo=UTC)
        bars=sample(first)
        self.assertEqual(verify_contiguous(bars,key="t",count=13)[1],
                         "off_grid_timestamp")
        valid,reason=verify_contiguous(bars,key="t",count=13,
                          allow_fixed_grid_offset=True,session_open=first-timedelta(minutes=1))
        self.assertTrue(valid)
        self.assertEqual(reason,"OK_fixed_offset_1m")
        self.assertEqual(bars[0]["t"],first.isoformat())

    def test_xetra_rejects_holes_even_with_fixed_offset(self):
        bars=sample(datetime(2026,10,26,8,1,tzinfo=UTC))
        bars[7]["t"]=(datetime(2026,10,26,8,41,tzinfo=UTC)).isoformat()
        valid,reason=verify_contiguous(bars,key="t",count=13,allow_fixed_grid_offset=True)
        self.assertFalse(valid)
        self.assertIn("missing_or_duplicate_5m_bar",reason)

    def test_xetra_rejects_mixed_offgrid_and_standard(self):
        bars=sample(datetime(2026,10,26,8,1,tzinfo=UTC))
        bars[5]["t"]=datetime(2026,10,26,8,25,tzinfo=UTC).isoformat()
        valid,reason=verify_contiguous(bars,key="t",count=13,allow_fixed_grid_offset=True)
        self.assertFalse(valid)
        self.assertEqual(reason,"off_grid_timestamp")

    def test_reject_nonfinite_prices_even_with_offset(self):
        bars=sample(datetime(2026,10,26,8,1,tzinfo=UTC))
        bars[-1]["h"]=float("nan")
        self.assertEqual(verify_contiguous(bars,key="t",count=13,
                                         allow_fixed_grid_offset=True)[1],"invalid_price")

    def test_conventional_us_i_ex_grid_restriction_unchanged(self):
        bars=sample(datetime(2026,10,26,13,0,tzinfo=UTC))
        self.assertEqual(verify_contiguous(bars,key="t",count=13),(True,"OK"))
        off=sample(datetime(2026,10,26,13,2,tzinfo=UTC))
        self.assertEqual(verify_contiguous(off,key="t",count=13)[1],"off_grid_timestamp")


    def test_live_offgrid_tail_is_dropped_without_rewriting_prices(self):
        first=datetime(2026,10,27,13,30,tzinfo=UTC)
        raw=sample(first,14)
        original=[dict(x) for x in raw]
        raw.append({"t":(first+timedelta(minutes=71)).isoformat(),
                    "o":100,"h":101,"l":99,"c":100.3,"v":200})
        now=first+timedelta(minutes=77)
        good,removed=xetra.drop_unstable_tail(raw,now,first)
        self.assertEqual(removed,1)
        self.assertEqual(good,original)
        self.assertEqual(len(raw),15)

    def test_internal_gap_must_not_be_repaired(self):
        first=datetime(2026,10,27,13,30,tzinfo=UTC)
        raw=sample(first,14)
        raw.pop(6)
        raw.append({"t":(first+timedelta(minutes=71)).isoformat(),
                    "o":100,"h":101,"l":99,"c":100.3,"v":200})
        clean,removed=xetra.drop_unstable_tail(raw,
                         first+timedelta(minutes=77),first)
        self.assertEqual(removed,0)
        self.assertEqual(clean,raw)

    def test_xetra_quality_journal_dedup(self):
        with tempfile.TemporaryDirectory() as tmp:
            quality=Path(tmp)/"quality.csv"
            first=datetime(2026,10,26,8,1,tzinfo=UTC)
            bars=sample(first)
            now=first+timedelta(minutes=70)
            with patch.object(xetra,"QUALITY_FILE",quality):
                self.assertTrue(xetra.log_quality("SAP.DE",bars,now,
                                               "OK_fixed_offset_1m","REFERENCE_VALID"))
                self.assertFalse(xetra.log_quality("SAP.DE",bars,now+timedelta(minutes=1),
                                                "OK_fixed_offset_1m","REFERENCE_VALID"))
                self.assertTrue(xetra.log_quality("SIE.DE",bars,now,
                                                "missing_or_duplicate_5m_bar","ENTRY_BLOCKED"))
                with quality.open(newline="") as fh:
                    rows=list(csv.DictReader(fh))
                self.assertEqual(len(rows),2)
                self.assertEqual(rows[0]["symbol"],"SAP.DE")
                self.assertEqual(rows[0]["grid_offset_minutes"],"1")

    def test_xetra_empty_no_signal_still_persists_virtual_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            now=datetime(2026,10,26,12,0,tzinfo=UTC)
            start=datetime(2026,10,26,8,1,tzinfo=UTC)
            data=sample(start,13)
            with patch.object(xetra,"PREFIX",str(Path(tmp)/"simulation")), \
                 patch.object(xetra,"QUALITY_FILE",Path(tmp)/"quality.csv"), \
                 patch.object(xetra,"yahoo_bars",return_value=data):
                result=xetra.scan_once(now)
                self.assertEqual(result["symbols"],6)
                self.assertTrue(Path(tmp,"simulation_state.json").exists())
                self.assertTrue(Path(tmp,"quality.csv").exists())


class ScannerScheduleTests(unittest.TestCase):
    def test_equity_workflows_have_recovery_slots_both_dst_seasons(self):
        for name in ("fast_scanner.yml","scanner.yml",
                     "fast_micro_scanner.yml","orb_scanner.yml"):
            with self.subTest(name=name):
                src=(ROOT/".github/workflows"/name).read_text()
                for cron in ("40 13","40 14","0 14","0 15"):
                    self.assertIn('cron: "'+cron+' * * 1-5"',src)
                self.assertIn("cancel-in-progress: false",src)
                self.assertIn("concurrency:",src)

    def test_all_recovery_slots_are_limited_to_correct_dst(self):
        for name in ("fast_scanner.yml","scanner.yml","fast_micro_scanner.yml","orb_scanner.yml"):
            src=(ROOT/".github/workflows"/name).read_text()
            with self.subTest(name=name):
                self.assertIn('"-0400:40 13 * * 1-5"',src)
                self.assertIn('"-0500:40 14 * * 1-5"',src)
                self.assertIn('"-0400:0 14 * * 1-5"',src)
                self.assertIn('"-0500:0 15 * * 1-5"',src)

    def test_orb_accepts_valid_delayed_sessions_but_never_orders(self):
        w=(ROOT/".github/workflows/orb_scanner.yml").read_text()
        self.assertIn('NOW_MIN" -lt 945',w)
        self.assertNotIn('NY_TIME" -lt 1000',w)
        self.assertIn("SCHEDULE_CRON:",w)
        self.assertIn("scripts/",(ROOT/".github/workflows/fast_micro_scanner.yml").read_text())
        orb_src=(ROOT/"scanner_orb_v1.py").read_text()
        self.assertNotIn("/orders",orb_src)
        self.assertNotIn("requests.delete(",orb_src)
        self.assertNotIn("requests.patch(",orb_src)

    def test_xetra_workflow_persists_quality(self):
        w=(ROOT/".github/workflows/xetra_scanner.yml").read_text()
        self.assertIn("xetra_quality_v1.csv",w)
        self.assertIn('cron: "7,17,27,37,47,57 7-17 * * 1-5"',w)
        self.assertIn("Could not persist XETRA virtual state",w)


if __name__=="__main__":
    unittest.main()
