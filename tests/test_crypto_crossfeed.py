"""CRYPTO V1.2: deterministic aligned Alpaca/Kraken comparison, NO ORDERS."""
import csv
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import requests
import crypto_crossfeed_v1 as x
import crypto_observer_v1 as observer
import scanner_crypto_v1 as scanner
import market_virtual_core as core

UTC = timezone.utc
START = datetime(2026,10,26,12,0,tzinfo=UTC)

def t(i):
    return START + timedelta(minutes=5*i)

def samples(n=24):
    primary, kraken = [], []
    for i in range(n):
        p = 100 + i*0.06
        if i==n-1:
            p += 0.9
        kr=p*1.0003
        for target,c,v in ((primary,p,0), (kraken,kr,300 if i==n-1 else 100)):
            target.append({"t":t(i).isoformat(),
                           "o":c-0.08,"h":c+0.02,"l":c-0.13,"c":c,
                           "v":v,"n":0 if not v else 25})
    return primary, kraken

def now(n=24):
    return t(n)+timedelta(seconds=16)


class CrossfeedTests(unittest.TestCase):
    def setUp(self):
        folder=tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        base=Path(folder.name)
        for obj,name,path in ((x,"KRAKEN_CANDLES","kraken.csv"),
                              (x,"COMPARE_PATH","compare.csv"),
                              (observer,"CANDLE_PATH","us.csv"),
                              (observer,"QUALITY_PATH","quality.csv"),
                              (scanner,"VIRTUAL_PREFIX","strict"),
                              (scanner,"RESEARCH_PREFIX","research"),
                              (scanner,"TREND_PREFIX","trend")):
            value=str(base/path) if name.endswith("PREFIX") else base/path
            p=patch.object(obj,name,value)
            p.start()
            self.addCleanup(p.stop)

    def test_signal_requires_both_trends_and_kraken_trade_volume(self):
        a,k=samples()
        report,signal=x.evaluate("BTC/USD",a,k,now())
        self.assertEqual(report["secondary_status"],"OK")
        self.assertEqual(report["trend_reason"],"TREND_CONFIRMED_RESEARCH")
        self.assertEqual(report["kraken_nonzero_12"],12)
        self.assertEqual(report["us_nonzero_12"],0)
        self.assertEqual(report["aligned_bars_1h"],12)
        self.assertIsNotNone(signal)
        self.assertTrue(signal["research_only"])
        self.assertFalse(signal["paper_eligible"])
        self.assertTrue(signal["kraken_volume_confirmed"])
        self.assertIsNone(core.find_signal("BTC/USD",a,now()))

    def test_missing_kraken_data_never_generates_trend_trade(self):
        a,_=samples()
        r,s=x.evaluate("BTC/USD",a,[],now())
        self.assertEqual(r["trend_reason"],"KRAKEN_DATA_MISSING")
        self.assertEqual(r["secondary_status"],"NO_SECONDARY_DATA")
        self.assertIsNone(s)

    def test_no_signal_when_kraken_latest_bar_missing(self):
        a,k=samples()
        r,s=x.evaluate("ETH/USD",a,k[:-1],now())
        self.assertEqual(r["trend_reason"],"LATEST_BAR_NOT_ALIGNED")
        self.assertIsNone(s)

    def test_gapped_primary_history_is_rejected(self):
        a,k=samples()
        a.pop(11)
        r,s=x.evaluate("SOL/USD",a,k,now())
        self.assertIn(r["trend_reason"],{"GAPPED_FIVE_MINUTE_BARS",
                                        "MISSING_ALIGNED_TREND_HISTORY"})
        self.assertIsNone(s)

    def test_divergent_venue_prices_are_not_confirmed(self):
        a,k=samples()
        k[-1]["c"]=a[-1]["c"]*1.03
        r,s=x.evaluate("SOL/USD",a,k,now())
        self.assertEqual(r["trend_reason"],"PRICE_VENUE_DIVERGENCE")
        self.assertIsNone(s)

    def test_zero_kraken_volume_is_rejected(self):
        a,k=samples()
        for b in k:
            b["v"]=0
        r,s=x.evaluate("SOL/USD",a,k,now())
        self.assertEqual(r["trend_reason"],"SPARSE_KRAKEN_TRADE_VOLUME")
        self.assertIsNone(s)

    def test_stale_last_candle_not_traded(self):
        a,k=samples()
        r,s=x.evaluate("BTC/USD",a,k,now()+timedelta(minutes=20))
        self.assertEqual(r["trend_reason"],"STALE_CLOSED_BAR")
        self.assertIsNone(s)

    def test_unfinished_current_candle_excluded(self):
        a,k=samples()
        r,s=x.evaluate("BTC/USD",a,k,t(23)+timedelta(minutes=3))
        self.assertIsNone(s)
        self.assertNotEqual(r["trend_reason"],"TREND_CONFIRMED_RESEARCH")

    def test_crossfeed_diagnostics_one_per_half_hour(self):
        a,k=samples()
        r,_=x.evaluate("BTC/USD",a,k,now())
        self.assertTrue(x.record_comparison(r,now()))
        self.assertFalse(x.record_comparison(r,now()+timedelta(seconds=20)))
        self.assertTrue(x.record_comparison(r,now()+timedelta(minutes=30)))
        with x.COMPARE_PATH.open(newline="",encoding="utf-8") as fh:
            rows=list(csv.DictReader(fh))
        self.assertEqual(len(rows),2)
        self.assertEqual(rows[0]["kraken_nonzero_12"],"12")

    def test_kraken_candles_stored_independently_from_alpaca(self):
        a,k=samples()
        count=observer.record_bars("ETH/USD",k,now(),path=x.KRAKEN_CANDLES)
        self.assertEqual(count,len(k))
        self.assertFalse(observer.CANDLE_PATH.exists())
        self.assertEqual(observer.record_bars("ETH/USD",k,now(),path=x.KRAKEN_CANDLES),0)

    def test_scan_stores_trend_signals_without_broker_candidates(self):
        a,k=samples()
        def fetch(symbol,at,loc="us"):
            return k if loc=="us-1" else a
        with patch.object(scanner,"fetch_bars",side_effect=fetch):
            stats,candidates=scanner.scan_once(now())
        self.assertEqual(stats["pairs"],3)
        self.assertEqual(stats["kraken_failures"],0)
        self.assertEqual(stats["strict_signals"],0)
        self.assertEqual(stats["trend_signals"],3)
        self.assertEqual(stats["new_trend"],2)
        self.assertEqual(candidates,[])
        self.assertTrue(x.KRAKEN_CANDLES.exists())
        self.assertTrue(x.COMPARE_PATH.exists())
        self.assertEqual(len(
            (Path(scanner.TREND_PREFIX+"_signals.jsonl")).read_text().splitlines()
        ),3)
        with patch.object(scanner,"fetch_bars",side_effect=fetch):
            again,again_candidates=scanner.scan_once(now())
        self.assertEqual(again["trend_signals"],0)
        self.assertEqual(again["new_trend"],0)
        self.assertEqual(again_candidates,[])

    def test_fallback_records_kraken_failure_but_keeps_primary(self):
        a,k=samples()
        def fetch(symbol,at,loc="us"):
            if loc=="us-1":
                raise requests.exceptions.HTTPError("Kraken unavailable")
            return a
        with patch.object(scanner,"fetch_bars",side_effect=fetch):
            stats,candidates=scanner.scan_once(now())
        self.assertEqual(stats["pairs"],3)
        self.assertEqual(stats["errors"],0)
        self.assertEqual(stats["kraken_failures"],3)
        self.assertEqual(stats["new_kraken_candles"],0)
        self.assertEqual(stats["trend_signals"],0)
        self.assertEqual(candidates,[])
        with x.COMPARE_PATH.open(newline="",encoding="utf-8") as fh:
            rows=list(csv.DictReader(fh))
        self.assertTrue(all(row["secondary_status"]=="UNAVAILABLE" for row in rows))

    def test_location_is_explicitly_restricted(self):
        with self.assertRaises(ValueError):
            scanner.fetch_bars("BTC/USD",now(),loc="eu-1")

    def test_only_strict_signals_can_reach_paper(self):
        src=Path(scanner.__file__).read_text(encoding="utf-8")
        self.assertIn("paper_candidates.append(strict)",src)
        self.assertNotIn("paper_candidates.append(trial_trend)",src)
        self.assertIn("paper.run(candidates,allow_new=True)",src)
        self.assertIn('loc="us-1"',src)
        self.assertNotIn('api("POST"',Path(x.__file__).read_text(encoding="utf-8"))
        self.assertNotIn('requests.post(',Path(x.__file__).read_text(encoding="utf-8"))

    def test_kraken_history_is_not_primary_ohlcv(self):
        self.assertNotEqual(x.KRAKEN_CANDLES,observer.CANDLE_PATH)
        self.assertEqual(scanner.KRAKEN_DATA,
                         "https://data.alpaca.markets/v1beta3/crypto/us-1/bars")

if __name__=="__main__":
    unittest.main()
