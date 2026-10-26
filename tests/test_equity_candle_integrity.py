"""Equity IEX/Twelve/Yahoo five-minute completeness safety regression.

Tests are read-only, deterministic and require no broker credentials.
"""
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import equity_candle_guard as guard
import scanner_fast as fast
import scanner_v25_2 as classic
import scanner_orb_v1 as orb
import fast_virtual as shadow

UTC=timezone.utc
START=datetime(2026,10,26,13,30,tzinfo=UTC)


def raw(i, *, signal=False):
    at=START+timedelta(minutes=5*i)
    close=10.13 if signal else 10.02
    return {"t":at.isoformat(),"o":10.00,
            "h":10.16 if signal else 10.05,
            "l":9.95,"c":close,"v":6500 if signal else 3000,
            "n":65 if signal else 40}


class IntegrityTests(unittest.TestCase):
    def test_complete_series_passes(self):
        candles=[raw(i) for i in range(20)]
        self.assertEqual(guard.verify_contiguous(
            candles,key="t",count=15,session_open=START),(True,"OK"))

    def test_gap_rejected_with_exact_delay(self):
        candles=[raw(i) for i in range(20) if i!=11]
        ok,reason=guard.verify_contiguous(candles,key="t",count=15)
        self.assertFalse(ok)
        self.assertIn("delta=10m",reason)

    def test_duplicate_and_offgrid_rejected(self):
        candles=[raw(i) for i in range(19)]+[raw(18)]
        self.assertFalse(guard.verify_contiguous(candles,key="t",count=15)[0])
        candles=[raw(i) for i in range(20)]
        candles[-1]["t"]=(START+timedelta(minutes=96)).isoformat()
        self.assertEqual(guard.verify_contiguous(candles,key="t",count=15)[1],
                         "off_grid_timestamp")

    def test_previous_day_bar_not_accepted_in_early_session(self):
        candles=[raw(i) for i in range(-5,10)]
        good,reason=guard.verify_contiguous(candles,key="t",count=15,session_open=START)
        self.assertFalse(good)
        self.assertEqual(reason,"previous_session_reference")

    def test_does_not_invent_price_for_zero_volume_or_missing_bar(self):
        candles=[raw(i) for i in range(18)]
        candles[5]["v"]=0
        self.assertTrue(guard.verify_contiguous(candles,key="t",count=15)[0])
        missing=[b for i,b in enumerate(candles) if i!=10]
        self.assertFalse(guard.verify_contiguous(missing,key="t",count=15)[0])

    def test_closed_candle_not_blindly_discarded(self):
        candles=[raw(i) for i in range(20)]
        now=START+timedelta(minutes=100,seconds=20)
        got=guard.fully_closed_iex(candles,now)
        self.assertEqual(len(got),20)
        self.assertEqual(got[-1]["t"],candles[-1]["t"])

    def test_last_forming_bar_excluded(self):
        candles=[raw(i) for i in range(21)]
        now=START+timedelta(minutes=100,seconds=20)
        got=guard.fully_closed_iex(candles,now)
        self.assertEqual(len(got),20)

    def test_fast_valid_breakout_is_not_disabled_by_filter(self):
        candles=[raw(i,signal=i==19) for i in range(20)]
        now=START+timedelta(minutes=100,seconds=20)
        with patch.object(fast,"now_utc",return_value=now):
            signal=fast.analyze_symbol("BB",candles)
        self.assertIsNotNone(signal)
        self.assertEqual(signal["bar_key"],candles[19]["t"])

    def test_fast_missing_bar_rejects_new_entry(self):
        candles=[raw(i,signal=i==19) for i in range(20) if i!=11]
        now=START+timedelta(minutes=100,seconds=20)
        with patch.object(fast,"now_utc",return_value=now):
            signal=fast.analyze_symbol("BB",candles)
        self.assertIsNone(signal)

    def test_fast_skips_yesterdays_bar_at_open(self):
        candles=[raw(i,signal=i==11) for i in range(-4,12)]
        now=START+timedelta(minutes=60,seconds=20)
        with patch.object(fast,"now_utc",return_value=now):
            self.assertIsNone(fast.analyze_symbol("BB",candles))

    def test_fast_shadow_includes_last_completed_bar(self):
        candles=[raw(i) for i in range(20)]
        now=START+timedelta(minutes=100,seconds=20)
        session={"open":START,"close":START+timedelta(minutes=390)}
        completed=shadow.completed_bars(candles,now,session)
        self.assertEqual(len(completed),20)
        self.assertEqual(completed[-1]["t"],START+timedelta(minutes=95))

    def test_classic_gap_block_reported_in_diagnostics(self):
        bars=[{"time":START.astimezone(classic.NY)+timedelta(minutes=5*i),
               "o":10.0,"h":10.05 if i!=19 else 10.16,"l":9.95,
               "c":10.02 if i!=19 else 10.13,
               "v":3000 if i!=19 else 6500} for i in range(20)]
        check=bars[-1]["time"]+timedelta(minutes=5,seconds=30)
        with patch.object(classic,"now_ny",return_value=check):
            good={}
            self.assertIsNotNone(classic.analyze("BB",bars,good))
            bad={}
            self.assertIsNone(classic.analyze("BB",
                         [b for i,b in enumerate(bars) if i!=11],bad))
        self.assertEqual(bad.get("nesuvisle_alebo_chybajuce_5min_sviecky"),1)

    def test_orb_missing_intermediate_bar_is_not_breakout(self):
        opening=orb.get_opening_range([
            {"t":datetime(2026,10,26,9,m,tzinfo=orb.NY),
             "o":10,"h":10.15 if m==35 else 10.10,
             "l":9.90,"c":10.01,"v":5000,"n":80} for m in [30,35,40]],
            {"open":datetime(2026,10,26,9,30,tzinfo=orb.NY)})
        session={"open":datetime(2026,10,26,9,30,tzinfo=orb.NY),
                 "close":datetime(2026,10,26,16,0,tzinfo=orb.NY)}
        gaps=[
          {"t":datetime(2026,10,26,9,40,tzinfo=orb.NY),"o":10,
           "h":10.10,"l":9.90,"c":10.01,"v":4000,"n":80},
          {"t":datetime(2026,10,26,9,50,tzinfo=orb.NY),"o":10.10,
           "h":10.45,"l":10.10,"c":10.30,"v":8000,"n":100}]
        self.assertIsNone(orb.pick_signal("RIOT",gaps,opening,session,
                          datetime(2026,10,26,9,55,seconds=12,tzinfo=orb.NY)))


if __name__=="__main__":
    unittest.main()
