"""FAST MICRO V1: deterministic, no credentials, NO Alpaca broker orders."""
import csv
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import fast_micro_v1 as micro
import scanner_fast_micro_v1 as entrypoint

NY = ZoneInfo("America/New_York")
def moment(n):
    return datetime(2026, 10, 26, 9, 30, tzinfo=NY) + timedelta(minutes=n)

SESSION = {
    "date": "2026-10-26", "open": moment(0), "close": moment(390),
    "entry_stop_at": moment(360), "liquidate_at": moment(375),
}

def bar(i, o=10, h=10.02, l=9.98, c=10, v=20000, n=60):
    return {"t": moment(i*5).isoformat(), "o": o, "h": h, "l": l,
            "c": c, "v": v, "n": n}

def signal(symbol="BB", at=80, side="buy"):
    return {"symbol": symbol, "side": side,
            "bar_key": moment(at).isoformat(), "entry_close": 10,
            "breakout_percent": 0.2, "rel_volume": 1.8,
            "signal_bar_notional_usd": 200000}

class MicroTradingTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root=Path(tmp.name)
        for attr,filename in (("STATE_PATH","state.json"),
                              ("SIGNALS_PATH","signals.jsonl"),
                              ("RESULTS_PATH","trades.csv"),
                              ("EVENTS_PATH","events.jsonl")):
            p=patch.object(micro,attr,root/filename)
            p.start()
            self.addCleanup(p.stop)

    def test_1_dollar_estimated_risk_and_minimum_target(self):
        s=signal()
        entry=micro.make_entry(s, {"t": moment(85),"o":10,"c":10})
        self.assertIsNotNone(entry)
        qty, price=entry["qty"], entry["entry"]
        self.assertLessEqual(qty*price, micro.MAX_NOTIONAL_USD+1e-5)
        modeled_loss=qty*price*(micro.STOP_PCT/100+2*micro.SLIP_PCT/100)
        self.assertLessEqual(modeled_loss,micro.RISK_USD+1e-6)
        self.assertGreaterEqual(
            micro.projected_profit(qty,price,"buy"),
            micro.MIN_PROJECTED_PROFIT_USD)
        # The spread-aware model lowers permitted size/profit.
        with_spread=micro.make_entry({**s,"quote_spread":0.01},
                                     {"t":moment(85),"o":10})
        self.assertIsNotNone(with_spread)
        self.assertLessEqual(with_spread["qty"],qty)

    def test_no_over_sizing_and_do_not_fill_bad_small_target(self):
        nofit=micro.make_entry(signal(),{"t":moment(85),"o":100})
        self.assertIsNone(nofit)
        self.assertIsNone(micro.make_entry(signal(),{"t":moment(85),"o":-1}))

    def test_stop_priority_if_stop_and_target_same_bar(self):
        slot=micro.make_entry(signal(),{"t":moment(85),"o":10})
        decision=micro.exit_check(slot,{"t":moment(85),"o":10,"h":11,"l":9,"c":10})
        self.assertEqual(decision[1],"SL")
        self.assertLess(decision[0],slot["entry"])

    def test_short_stop_and_take_profit(self):
        slot=micro.make_entry(signal(side="sell"),{"t":moment(85),"o":10})
        self.assertIsNotNone(slot)
        self.assertEqual(micro.exit_check(slot,{"o":10,"h":11,"l":9})[1],"SL")
        self.assertEqual(micro.exit_check(slot,{"o":10,"h":10,"l":9})[1],"TP")

    def test_only_enter_after_completed_next_bar(self):
        pending={"phase":"pending","signal":signal()}
        raws=[bar(i) for i in range(18)]
        completed=micro.completed_bars(raws,moment(85),SESSION)
        slot,result=micro.advance(pending,completed,moment(85),SESSION,"BB")
        self.assertEqual(slot["phase"],"pending")
        self.assertIsNone(result)
        raws.append(bar(18))
        completed=micro.completed_bars(raws,moment(90),SESSION)
        slot,result=micro.advance(pending,completed,moment(90),SESSION,"BB")
        self.assertEqual(slot["phase"],"open")
        self.assertIsNone(result)
        self.assertEqual(slot["entry_bar"],moment(85).isoformat())

    def test_missing_next_bar_does_not_invent_fill(self):
        pending={"phase":"pending","signal":signal()}
        raws=[bar(i) for i in range(17)] + [bar(18),bar(19)]
        bars=micro.completed_bars(raws,moment(95),SESSION)
        slot,result=micro.advance(pending,bars,moment(95),SESSION,"BB")
        self.assertEqual(slot["phase"],"expired")
        self.assertEqual(slot["reason"],"missing_next_complete_bar")
        self.assertIsNone(result)

    def test_full_scan_persists_state_closes_one_trade_and_dedupes(self):
        raws=[bar(i) for i in range(18)]
        fetch=lambda sym: raws
        analyze=lambda sym,bars: signal(sym)
        stats=micro.process(SESSION,["BB"],fetch,analyze,moment(85),workers=1)
        self.assertEqual(stats["attempts"],1)
        self.assertEqual(stats["signals"],1)
        self.assertEqual(micro.load(SESSION)["symbols"]["BB"]["phase"],"pending")
        raws=[bar(i) for i in range(19)]
        micro.process(SESSION,["BB"],fetch,analyze,moment(90),workers=1)
        self.assertEqual(micro.load(SESSION)["symbols"]["BB"]["phase"],"open")
        raws=[bar(i, l=9.0) if i==18 else bar(i) for i in range(20)]
        final=micro.process(SESSION,["BB"],fetch,analyze,moment(95),workers=1)
        self.assertEqual(final["closed"],1)
        self.assertLess(final["pnl_usd"],0)
        self.assertEqual(len(micro.SIGNALS_PATH.read_text().splitlines()),1)
        with micro.RESULTS_PATH.open(newline="") as r:
            self.assertEqual(len(list(csv.DictReader(r))),1)
        micro.process(SESSION,["BB"],fetch,analyze,moment(95),workers=1)
        with micro.RESULTS_PATH.open(newline="") as r:
            self.assertEqual(len(list(csv.DictReader(r))),1)

    def test_limit_daily_attempts(self):
        s=micro.load(SESSION)
        s["daily"]["attempts"]=micro.MAX_DAILY_ATTEMPTS
        micro.persist(s)
        micro.process(SESSION,["BB"],lambda sym:[bar(i) for i in range(18)],
                      lambda sym,bars:signal(sym),moment(85),workers=1)
        self.assertEqual(micro.load(SESSION)["daily"]["attempts"],
                         micro.MAX_DAILY_ATTEMPTS)
        self.assertEqual(micro.load(SESSION)["symbols"]["BB"]["phase"],"watching")

    def test_stale_or_wide_quote_rejects_micro_entry(self):
        with patch.object(micro,"EVENTS_PATH",micro.EVENTS_PATH):
            micro.process(SESSION,["BB"],lambda s:[bar(i) for i in range(18)],
                          lambda s,b:signal(s),moment(85),workers=1,
                          validate=lambda s: None)
        self.assertEqual(micro.load(SESSION)["daily"]["attempts"],0)
        self.assertEqual(len(micro.SIGNALS_PATH.read_text().splitlines()),1)

    def test_approved_quote_records_spread(self):
        micro.process(SESSION,["BB"],lambda s:[bar(i) for i in range(18)],
                      lambda s,b:signal(s),moment(85),workers=1,
                      validate=lambda s:{"spread":0.01,"spread_percent":0.1})
        state=micro.load(SESSION)
        self.assertEqual(state["daily"]["attempts"],1)
        self.assertEqual(state["symbols"]["BB"]["signal"]["quote_spread"],0.01)

    def test_max_two_virtual_positions(self):
        symbols=["BB","SOFI","F"]
        micro.process(SESSION,symbols,lambda s:[bar(i) for i in range(18)],
                      lambda s,b:signal(s),moment(85),workers=1)
        stored=micro.load(SESSION)
        self.assertEqual(sum(x["phase"]=="pending" for x in stored["symbols"].values()),2)
        self.assertEqual(stored["daily"]["attempts"],2)

    def test_realized_loss_blocks_new_entries(self):
        s=micro.load(SESSION)
        s["daily"]["pnl_usd"]=-micro.MAX_DAILY_REALIZED_LOSS
        micro.persist(s)
        result=micro.process(SESSION,["BB"],lambda s:[bar(i) for i in range(18)],
                            lambda s,b:signal(s),moment(85),workers=1)
        self.assertEqual(result["attempts"],0)

    def test_previous_day_unresolved_is_not_counted_as_profit(self):
        old=micro.load(SESSION)
        old["date"]="2026-10-23"
        old["symbols"]["BB"]={"phase":"open","signal":signal()}
        micro.persist(old)
        new=micro.load(SESSION)
        self.assertEqual(new["daily"]["pnl_usd"],0)
        journal=micro.EVENTS_PATH.read_text()
        self.assertIn("previous_session_unresolved",journal)
        self.assertIn("no_assumed_pnl",journal)

    def test_readonly_microscan_entrypoint(self):
        for path in (Path(micro.__file__),Path(entrypoint.__file__)):
            src=path.read_text(encoding="utf-8")
            self.assertNotIn("POST",src)
            self.assertNotIn("/orders",src)
            self.assertNotIn("submit_order",src)

    def test_entrypoint_uses_exclusive_micro_universe(self):
        with patch.object(entrypoint.fast,"market_session_today",return_value=SESSION), \
             patch.object(entrypoint.fast,"SYMBOLS",["BB"]), \
             patch.object(entrypoint.fast,"get_bars") as fetch, \
             patch.object(entrypoint.fast,"analyze_symbol") as analyze, \
             patch.object(entrypoint.micro,"process",return_value={}) as run:
            entrypoint.main(now=moment(60))
        self.assertEqual(tuple(run.call_args.args[1]),entrypoint.SYMBOLS)

class WorkflowTests(unittest.TestCase):
    def test_script_syntax(self):
        import subprocess
        s=Path(__file__).resolve().parent.parent/"scripts/fast_micro_session.sh"
        cmd=subprocess.run(["bash","-n",str(s)],capture_output=True,text=True)
        self.assertEqual(cmd.returncode,0,cmd.stderr)

    def test_schedule_and_2_session_persistence(self):
        p=Path(__file__).resolve().parent.parent/".github/workflows/fast_micro_scanner.yml"
        w=p.read_text(encoding="utf-8")
        for cron in ("20 13","20 14"):
            self.assertIn('cron: "'+cron+' * * 1-5"',w)
        self.assertIn("group: fast-micro-virtual-v1",w)
        self.assertIn("cancel-in-progress: false",w)
        self.assertIn("needs: first-session",w)
        self.assertNotIn("preflight",w)
        self.assertNotIn("ENABLE_FAST_TRADING",w)
        self.assertNotIn("MICRO_ALPACA_SECRET_KEY",w)
        self.assertIn("secrets.ALPACA_SECRET_KEY",w)
        self.assertIn("ENABLE_MICRO_PAPER_TRADING",w)

    def test_state_saved_each_cycle_with_no_order_secrets(self):
        script=(Path(__file__).resolve().parent.parent/
                "scripts/fast_micro_session.sh").read_text(encoding="utf-8")
        self.assertLess(script.index("persist_micro\n\n  utc_epoch="),
                        script.index("slot=$(( (utc_epoch"))
        self.assertIn("git push origin HEAD:main",script)
        self.assertIn('[[ ! -f "$file" ]] || files+=("$file")',script)
        self.assertIn("fast_micro_results_v1.csv",script)

if __name__=="__main__":
    unittest.main()
