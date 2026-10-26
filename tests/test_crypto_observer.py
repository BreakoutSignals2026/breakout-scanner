"""Offline CRYPTO V1.1 source quality, real samples and safe experiment split."""
import csv
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import crypto_observer_v1 as obs
import crypto_crossfeed_v1 as cross
import scanner_crypto_v1 as crypto
import market_virtual_core as core

UTC=timezone.utc
START=datetime(2026,10,25,12,0,tzinfo=UTC)


def candles(volume=0):
    """Last bar crosses previous 12 high while reference volumes are zero."""
    bars=[]
    for i in range(16):
        t=START+timedelta(minutes=i*5)
        bar={"t":t.isoformat(),"o":100,"h":100.1,
             "l":99.9,"c":100.0,"v":volume,"n":int(volume>0)}
        bars.append(bar)
    bars[-1].update({"h":100.55,"c":100.4,"v":0,"n":0})
    return bars


class CryptoResearchTests(unittest.TestCase):
    def setUp(self):
        folder=tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        base=Path(folder.name)
        for key,p in (("CANDLE_PATH","ohlcv.csv"),
                      ("QUALITY_PATH","quality.csv")):
            with_patch=patch.object(obs,key,base/p)
            with_patch.start()
            self.addCleanup(with_patch.stop)
        for attr,filename in (("KRAKEN_CANDLES","kraken_ohlcv.csv"),
                              ("COMPARE_PATH","crossfeed.csv")):
            p=patch.object(cross,attr,base/filename)
            p.start()
            self.addCleanup(p.stop)
        for k,p in (("VIRTUAL_PREFIX","strict"),
                    ("RESEARCH_PREFIX","research"),
                    ("TREND_PREFIX","trend")):
            pp=patch.object(crypto,k,str(base/p))
            pp.start()
            self.addCleanup(pp.stop)

    def test_price_only_signal_when_all_volume_zero_but_strict_refuses(self):
        bars=candles()
        now=START+timedelta(minutes=80)
        self.assertIsNone(core.find_signal("SOL/USD",bars,now))
        research=obs.price_only_signal("SOL/USD",bars,now)
        self.assertIsNotNone(research)
        self.assertTrue(research["research_only"])
        self.assertFalse(research["volume_confirmed"])
        quality=obs.quality("SOL/USD",bars,now,None)
        self.assertEqual(quality["strict_reason"],"SPARSE_REFERENCE_VOLUME")
        self.assertEqual(quality["nonzero_previous12"],0)
        self.assertTrue(quality["price_only_signal"])

    def test_does_not_create_synthetic_volume(self):
        bars=candles()
        now=START+timedelta(minutes=80)
        self.assertEqual(obs.record_bars("SOL/USD",bars,now),16)
        self.assertEqual(obs.record_bars("SOL/USD",bars,now),0)
        with obs.CANDLE_PATH.open(newline="",encoding="utf-8") as f:
            rows=list(csv.DictReader(f))
        self.assertEqual(len(rows),16)
        self.assertTrue(all(x["zero_volume"]=="1" for x in rows))
        self.assertTrue(all(x["volume"]=="0.0" for x in rows))
        self.assertEqual(rows[-1]["symbol"],"SOL/USD")

    def test_market_data_backfill_beyond_one_cycle(self):
        bars=candles()
        now=START+timedelta(minutes=80)
        first=bars[:8]
        self.assertEqual(obs.record_bars("ETH/USD",first,now),8)
        self.assertEqual(obs.record_bars("ETH/USD",bars,now),8)
        self.assertEqual(obs.record_bars("ETH/USD",bars,now),0)

    def test_half_hour_quality_is_idempotent(self):
        bars=candles()
        now=START+timedelta(minutes=80)
        q=obs.quality("SOL/USD",bars,now)
        self.assertTrue(obs.record_quality(q,now))
        self.assertFalse(obs.record_quality(q,now+timedelta(minutes=1)))
        self.assertTrue(obs.record_quality(q,now+timedelta(minutes=30)))
        with obs.QUALITY_PATH.open(newline="",encoding="utf-8") as f:
            rows=list(csv.DictReader(f))
        self.assertEqual(len(rows),2)

    def test_stale_candle_not_used_for_research(self):
        bars=candles()
        t=START+timedelta(minutes=95)
        self.assertIsNone(obs.price_only_signal("BTC/USD",bars,t))

    def test_scan_collects_without_broker_eligible_candidate(self):
        bars=candles()
        now=START+timedelta(minutes=80)
        with patch.object(crypto,"fetch_bars",return_value=bars):
            stats,paper_candidates=crypto.scan_once(now)
        self.assertEqual(stats["pairs"],3)
        self.assertEqual(stats["errors"],0)
        self.assertEqual(stats["strict_signals"],0)
        self.assertEqual(paper_candidates,[])
        self.assertGreaterEqual(stats["new_candles"],3*16)
        self.assertEqual(stats["research_signals"],3)
        self.assertGreaterEqual(stats["new_research"],1)
        self.assertTrue((Path(crypto.RESEARCH_PREFIX+"_state.json")).is_file())
        self.assertTrue((Path(crypto.VIRTUAL_PREFIX+"_state.json")).is_file())
        with patch.object(crypto,"fetch_bars",return_value=bars):
            again,paper_candidates=crypto.scan_once(now)
        self.assertEqual(again["new_candles"],0)
        self.assertEqual(again["research_signals"],0)
        self.assertEqual(again["new_research"],0)
        self.assertEqual(paper_candidates,[])

    def test_price_only_candidate_never_leaks_to_paper(self):
        source=Path(crypto.__file__).read_text(encoding="utf-8")
        self.assertIn("paper.run(candidates,allow_new=True)",source)
        self.assertIn("paper_candidates.append(strict)",source)
        self.assertNotIn("paper_candidates.append(trial)",source)
        self.assertNotIn('api("POST"',source)

    def test_rolling_data_removes_bars_older_than_14d(self):
        before=START-timedelta(days=15)
        old=[{"t":(before+timedelta(minutes=i*5)).isoformat(),
              "o":100,"h":101,"l":99,"c":100,"v":1,"n":2} for i in range(13)]
        old_now=before+timedelta(minutes=70)
        self.assertEqual(obs.record_bars("BTC/USD",old,old_now),13)
        now=START+timedelta(minutes=80)
        self.assertEqual(obs.record_bars("BTC/USD",candles(volume=1),now),16)
        with obs.CANDLE_PATH.open(newline="",encoding="utf-8") as f:
            rows=list(csv.DictReader(f))
        self.assertEqual(len(rows),16)
        self.assertTrue(all(
            datetime.fromisoformat(x["bar_utc"])>=now-timedelta(days=14)
            for x in rows))

if __name__=="__main__":
    unittest.main()
