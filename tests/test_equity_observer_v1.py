"""Offline tests: read-only IEX 5Min recorder, no synthetic volume or fills."""
import csv
import tempfile
import unittest
from datetime import datetime,timedelta,timezone
from pathlib import Path
from unittest.mock import patch

import scanner_equity_observer_v1 as rec

UTC=timezone.utc
OPEN=datetime(2026,10,27,13,30,tzinfo=UTC)

def candle(i,volume=100):
    t=OPEN+timedelta(minutes=5*i)
    return {"t":t.isoformat(),"o":10,"h":11,"l":9,"c":10.5,"v":volume,"n":3,"vw":10.2}

class RecordingTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root=Path(tmp.name)
        for name,path in (("BARS_PATH","bars.csv"),("QUALITY_PATH","quality.csv")):
            p=patch.object(rec,name,root/path)
            p.start()
            self.addCleanup(p.stop)

    def test_missing_candle_not_interpolated(self):
        bars=[candle(i) for i in range(20) if i!=11]
        now=OPEN+timedelta(minutes=105)
        self.assertEqual(rec.store_bars({"SOFI":bars},now),19)
        self.assertEqual(rec.store_bars({"SOFI":bars},now),0)
        with rec.BARS_PATH.open(newline="") as f:
            rows=list(csv.DictReader(f))
        self.assertEqual(len(rows),19)
        q=rec.quality("SOFI",bars,now)
        self.assertEqual(q["missing_5min_slots"],1)
        self.assertIn("missing_or_duplicate_5m_bar",q["reference_integrity"])

    def test_zero_volume_preserved_and_partial_bar_excluded(self):
        bars=[candle(i,volume=0 if i==2 else 5) for i in range(21)]
        now=OPEN+timedelta(minutes=100,seconds=20)
        self.assertEqual(rec.store_bars({"AAPL":bars},now),20)
        with rec.BARS_PATH.open(newline="") as f:
            rows=list(csv.DictReader(f))
        self.assertEqual(rows[2]["volume"],"0.0")
        self.assertEqual(rows[2]["trades"],"3")
        self.assertEqual(rows[-1]["bar_utc"],bars[-2]["t"])

    def test_quality_records_zero_bar_symbols_and_deduplicates_bucket(self):
        bars=[candle(i) for i in range(19)]
        now=OPEN+timedelta(minutes=100,seconds=15)
        self.assertEqual(rec.store_quality({"SOFI":bars},now),len(rec.SYMBOLS))
        self.assertEqual(rec.store_quality({"SOFI":bars},now),0)
        with rec.QUALITY_PATH.open(newline="") as f:
            data=list(csv.DictReader(f))
        self.assertEqual(len(data),len(rec.SYMBOLS))
        self.assertEqual(next(r for r in data if r["symbol"]=="NOK")["data_status"],
                         "NO_COMPLETED_CANDLES")

    def test_page_token_and_completeness(self):
        class Response:
            def __init__(self,chunk): self.chunk=chunk
            def raise_for_status(self): pass
            def json(self): return self.chunk
        tokens=[]
        def get(url,params,headers,timeout):
            tokens.append(params.get("page_token"))
            return Response({"bars":{"SOFI":[candle(0)]},
                             "next_page_token":"page2" if len(tokens)==1 else None})
        with patch.dict("os.environ",{"ALPACA_API_KEY":"dummy","ALPACA_SECRET_KEY":"dummy"}):
            data=rec.fetch_market_bars(OPEN+timedelta(minutes=60),request_get=get)
        self.assertEqual(len(data["SOFI"]),2)
        self.assertEqual(tokens,[None,"page2"])

    def test_no_broker_endpoints_and_symbol_universe(self):
        src=Path(rec.__file__).read_text()
        self.assertNotIn("/v2/orders",src)
        self.assertNotIn("paper-api.alpaca",src)
        self.assertNotIn("requests.post(",src)
        self.assertEqual(len(rec.FAST_SYMBOLS),28)
        self.assertEqual(len(rec.BREAKOUT_SYMBOLS),10)
        self.assertEqual(len(rec.SYMBOLS),38)

if __name__=="__main__":
    unittest.main()
