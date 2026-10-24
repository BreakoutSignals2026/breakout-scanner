"""Offline safety and deterministic market behavior for Europe/Crypto scanners."""
import csv
import tempfile
import unittest
from datetime import datetime,timedelta,timezone
from pathlib import Path
from unittest.mock import patch

import market_virtual_core as core
import crypto_paper_v1 as paper
import scanner_crypto_v1 as crypto
import scanner_xetra_v1 as xetra

UTC=timezone.utc
def now(minutes):
    return datetime(2026,10,24,12,0,tzinfo=UTC)+timedelta(minutes=minutes)

def candles(n=16, start=None):
    base=start or now(0)
    bars=[]
    for i in range(n):
        t=base+timedelta(minutes=i*5)
        b={"t":t.isoformat(),"o":100,"h":100.1,"l":99.9,"c":100,"v":5000}
        bars.append(b)
    bars[-1]["h"]=101.5
    bars[-1]["c"]=101.1
    bars[-1]["v"]=11000
    return bars

class VirtualCoreTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base=Path(tmp.name)/"testv"
        self.book=core.VirtualBook(str(self.base),risk=3,stop_pct=1.2,
                                   reward=2,fee_pct=.25,slippage_pct=.08,
                                   max_day_entries=3,max_open=2)

    def test_only_closed_candles_signal(self):
        data=candles()
        last=now(75)
        self.assertIsNone(core.find_signal("BTC/USD",data,last))
        ready=now(80)
        sig=core.find_signal("BTC/USD",data,ready)
        self.assertIsNotNone(sig)
        self.assertEqual(sig["side"],"buy")
        self.assertEqual(sig["symbol"],"BTC/USD")

    def test_seen_signals_persist_after_restart(self):
        sig=core.find_signal("BTC/USD",candles(),now(80))
        self.assertTrue(self.book.signal(sig,"2026-10-24"))
        other=core.VirtualBook(str(self.base))
        self.assertFalse(other.signal(sig,"2026-10-24"))
        self.assertEqual(len(self.book.signal_path.read_text().splitlines()),1)

    def test_no_entry_until_next_bar_completed(self):
        sig=core.find_signal("BTC/USD",candles(),now(80))
        self.book.signal(sig,"2026-10-24")
        self.assertTrue(self.book.maybe_enter(sig,"2026-10-24"))
        self.book.progress("BTC/USD",candles(),now(80))
        self.assertEqual(self.book.state["positions"]["BTC/USD"]["phase"],"pending")
        nextbar={"t":now(80).isoformat(),"o":101,"h":101.2,"l":100.9,"c":101.1,"v":7000}
        self.book.progress("BTC/USD",candles()+[nextbar],now(85))
        self.assertEqual(self.book.state["positions"]["BTC/USD"]["phase"],"open")

    def test_stop_priority_same_candle(self):
        sig=core.find_signal("BTC/USD",candles(),now(80))
        self.book.maybe_enter(sig,"2026-10-24")
        # Entry close and highly volatile next bar; stop first, even if TP hit.
        huge={"t":now(80).isoformat(),"o":101,"h":120,"l":90,"c":100,"v":7000}
        result=self.book.progress("BTC/USD",candles()+[huge],now(85))
        self.assertEqual(result["reason"],"SL")
        self.assertLess(result["pnl"],0)
        with self.book.result_path.open(newline="") as f:
            self.assertEqual(len(list(csv.DictReader(f))),1)

    def test_different_virtual_books_do_not_mix(self):
        book2=core.VirtualBook(str(Path(self.base.parent)/"europe"))
        sig=core.find_signal("BTC/USD",candles(),now(80))
        self.book.signal(sig,"2026-10-24")
        self.assertFalse(book2.signal_path.exists())
        self.assertFalse(book2.existing("BTC/USD"))

class BrokerSafetyTests(unittest.TestCase):
    def test_paper_endpoint_only(self):
        self.assertEqual(paper.API,"https://paper-api.alpaca.markets/v2")
        self.assertFalse("api.alpaca.markets/v2" in paper.API.replace("paper-api",""))
        self.assertEqual(paper.STOP_PCT,1.2)

    def test_fees_and_risk_restrict_qty(self):
        a={"min_trade_increment":"0.00000001","min_order_size":"0.00001"}
        qty=paper.size_for_price(100000,a)
        self.assertLessEqual(float(qty)*100000,220)
        self.assertLessEqual(float(qty)*100000*.02,3.01)

    def test_stop_limit_price_is_worse_than_trigger(self):
        item={"symbol":"BTC/USD","stop_cid":"CRPT1S_test"}
        position={"qty":"0.001"}
        entry={"filled_avg_price":"100000","filled_qty":"0.001"}
        asset={"price_increment":"1","min_trade_increment":"0.000001"}
        payload=paper.stop_payload(item,position,entry,asset)
        self.assertEqual(payload["type"],"stop_limit")
        self.assertEqual(payload["side"],"sell")
        self.assertLess(float(payload["limit_price"]),float(payload["stop_price"]))
        self.assertEqual(payload["time_in_force"],"gtc")

    def test_invalid_position_quantity_never_oversold(self):
        with self.assertRaises(RuntimeError):
            paper.stop_payload({"symbol":"BTC/USD","stop_cid":"abc"},
                              {"qty":"0.1"},{"filled_avg_price":"10","filled_qty":"0.01"},
                              {"price_increment":".01","min_trade_increment":".001"})

    def test_no_entries_when_safe_mode(self):
        with patch.object(paper,"load",return_value={"trades":{}}), \
             patch.object(paper,"reconcile",return_value=(True,{})), \
             patch.object(paper,"submit_one",side_effect=AssertionError("no broker buys")):
            paper.run(allow_new=False)

    def test_short_crypto_signal_is_not_bought(self):
        with patch.object(paper,"positions",return_value={}), \
             patch.object(paper,"history_today",return_value=("2026-10-24",[])), \
             patch.object(paper,"orders",return_value=[]), \
             patch.object(paper,"quote",side_effect=AssertionError("short forbidden")):
            paper.submit_one({"trades":{}},[{"symbol":"BTC/USD","side":"sell",
                "bar_close":now(0).isoformat(),"bar":now(0).isoformat(),
                "reference_price":100,"relvol":2,"breakout_pct":1}],now(1))


    def test_pending_stop_cancellation_cannot_send_take_profit(self):
        state={"trades":{"BTCUSD":{"symbol":"BTC/USD",
               "entry_cid":"CRPT1E_BTCUSD_a","stop_cid":"CRPT1S_BTCUSD_a",
               "take_cid":"CRPT1T_BTCUSD_a","status":"tp_canceling"}}}
        entry={"status":"filled","filled_qty":"0.001","filled_avg_price":"100000"}
        stop={"status":"pending_cancel","type":"stop_limit","side":"sell",
              "qty":"0.001"}
        def resolve(cid):
            return entry if "1E_" in cid else stop if "1S_" in cid else None
        with patch.object(paper,"positions",return_value={"BTCUSD":{"qty":"0.001"}}), \
             patch.object(paper,"orders",return_value=[]), \
             patch.object(paper,"bycid",side_effect=resolve), \
             patch.object(paper,"save"), patch.object(paper,"alert"), \
             patch.object(paper,"recover_own_missing"), \
             patch.object(paper,"api",side_effect=AssertionError("NO PAPER MARKET SELL UNTIL CANCEL CONFIRMED")):
            safe,_=paper.reconcile(state)
        self.assertFalse(safe)

    def test_confirmed_stop_cancel_allows_single_paper_profit_exit(self):
        state={"trades":{"BTCUSD":{"symbol":"BTC/USD",
               "entry_cid":"CRPT1E_BTCUSD_a","stop_cid":"CRPT1S_BTCUSD_a",
               "take_cid":"CRPT1T_BTCUSD_a","status":"tp_canceling"}}}
        entry={"status":"filled","filled_qty":"0.001","filled_avg_price":"100000"}
        stop={"status":"canceled","type":"stop_limit","side":"sell",
              "qty":"0.001"}
        def resolve(cid):
            return entry if "1E_" in cid else stop if "1S_" in cid else None
        with patch.object(paper,"positions",return_value={"BTCUSD":{"qty":"0.001"}}), \
             patch.object(paper,"orders",return_value=[]), \
             patch.object(paper,"bycid",side_effect=resolve), \
             patch.object(paper,"asset",return_value={"min_trade_increment":"0.000001"}), \
             patch.object(paper,"save"), patch.object(paper,"log"), \
             patch.object(paper,"alert"),patch.object(paper,"recover_own_missing"), \
             patch.object(paper,"api",return_value={"id":"paper-order-1"}) as submit:
            safe,_=paper.reconcile(state)
        self.assertFalse(safe)
        self.assertEqual(state["trades"]["BTCUSD"]["status"],"tp_sent")
        submit.assert_called_once()
        self.assertEqual(submit.call_args.args,("POST","/orders"))
        payload=submit.call_args.kwargs["body"]
        self.assertEqual(payload["side"],"sell")
        self.assertEqual(payload["client_order_id"],"CRPT1T_BTCUSD_a")


    def test_broker_order_queries_are_crypto_only(self):
        with patch.object(paper,"api",return_value=[]) as get:
            paper.orders("all")
        self.assertEqual(get.call_args.args,("GET","/orders"))
        self.assertEqual(get.call_args.kwargs["params"]["symbols"],
                         "BTCUSD,ETHUSD,SOLUSD")

    def test_crypto_virtual_and_broker_are_distinct(self):
        self.assertEqual(crypto.SYMBOLS,tuple(paper.SYMBOLS))
        self.assertNotIn('api("POST"',Path(crypto.__file__).read_text(encoding="utf-8"))

class EuropeTests(unittest.TestCase):
    def test_weekend_no_scan(self):
        self.assertFalse(xetra.is_trading_day(now(10)))
        with patch.object(xetra,"yahoo_bars",side_effect=AssertionError("weekend no provider")):
            self.assertEqual(xetra.scan_once(now(10)),{"closed":True})

    def test_europe_has_no_broker_endpoint(self):
        import inspect
        src=inspect.getsource(xetra)
        self.assertNotIn("paper-api.alpaca",src)
        self.assertNotIn("requests.post",src)
        self.assertEqual(xetra.BERLIN.key,"Europe/Berlin")

    def test_yahoo_missing_feed_fail_closed(self):
        t=datetime(2026,10,26,10,0,tzinfo=xetra.BERLIN)
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(xetra,"PREFIX",str(Path(tmp)/"xetra")), \
             patch.object(xetra,"yahoo_bars",side_effect=RuntimeError("429")):
            with self.assertRaises(RuntimeError):
                xetra.scan_once(t)

if __name__=="__main__":
    unittest.main()
