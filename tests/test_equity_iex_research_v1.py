"""Offline retrospective study: only historical candles, no broker entry."""
import tempfile
import unittest
from datetime import datetime,timedelta,timezone
from pathlib import Path
from unittest.mock import patch

import scanner_equity_observer_v1 as obs
import equity_iex_research_v1 as m

UTC=timezone.utc
START=datetime(2026,10,27,13,30,tzinfo=UTC)

def series(gap=False):
    ret=[]
    for i in range(42):
        t=START+timedelta(minutes=5*i)
        if gap and i==18:
            t+=timedelta(minutes=1)
        price=10+i*0.015
        if i==25:
            price+=0.50
        ret.append({"t":t,"open":price-0.02,"high":price+0.02,
             "low":price-0.08,"close":price,"volume":10000 if i==25 else 3000,
             "trades":40})
    return ret

class ResearchTests(unittest.TestCase):
    def test_no_future_candles_cannot_evaluate_or_fabricate_entry(self):
        self.assertEqual(m.candidates("SOFI",series()[:27]),[])
        full=m.candidates("SOFI",series())
        self.assertTrue(full)
        sample=next(x for x in full if x["signal_bar_utc"]==series()[25]["t"].isoformat())
        self.assertEqual(sample["entry_bar_utc"],series()[26]["t"].isoformat())
        self.assertEqual(sample["exit_bar_utc"],series()[32]["t"].isoformat())
        self.assertEqual(sample["status"],"VIRTUAL_30MIN_FIXED_HORIZON")

    def test_history_gap_rejects_crossing_study_window(self):
        good=m.candidates("SOFI",series())
        bad=m.candidates("SOFI",series(gap=True))
        self.assertGreaterEqual(len(good),len(bad))
        self.assertFalse(any(
            x["signal_bar_utc"]==series()[25]["t"].isoformat()
            for x in bad))

    def test_saved_data_no_broker_and_no_dummy_prices(self):
        with tempfile.TemporaryDirectory() as root:
            path=Path(root)/"study.csv"
            rows=m.candidates("SOFI",series())
            m.write_results(rows,path)
            self.assertIn("VIRTUAL_30MIN",path.read_text())
        source=Path(m.__file__).read_text()
        self.assertNotIn("paper-api.alpaca",source)
        self.assertNotIn("requests.post(",source)
        self.assertNotIn("http_json(",source)

if __name__=="__main__":
    unittest.main()
