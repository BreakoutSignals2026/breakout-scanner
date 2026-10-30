import csv
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import fast_micro_paper as paper
import scanner_fast_micro_v1 as entrypoint
import scanner_v25_2 as classic

NOW = datetime(2026,10,30,15,0,tzinfo=timezone.utc)


class MicroPaperTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        for attr, name in [('STATE_FILE','state.json'),('RESULTS_FILE','results.csv')]:
            p=patch.object(paper,attr,root/name);p.start();self.addCleanup(p.stop)
        self.engine=paper.create_engine()
        self.session={'open':NOW.replace(hour=13,minute=30),
                      'entry_stop':NOW.replace(hour=19,minute=30),
                      'liquidate_at':NOW.replace(hour=19,minute=45),
                      'close':NOW.replace(hour=20)}
        self.signal={'symbol':'BB','side':'buy','bar_key':(NOW-timedelta(minutes=6)).isoformat(),
                     'rel_volume':2,'breakout_percent':0.2}
        self.live={'price':10,'spread':0.01,'bid':9.99,'ask':10}

    def engine_api(self, method, path, **kwargs):
        if method=='GET' and path=='/account':
            return {'id':'separate-micro','status':'ACTIVE','trading_blocked':False,'account_blocked':False}
        if method=='GET' and path=='/orders':return []
        raise AssertionError(f'Unexpected broker call {method} {path}')

    def process_patches(self, enabled=True, safe=True):
        from contextlib import ExitStack
        ctx=ExitStack();self.addCleanup(ctx.close)
        ctx.enter_context(patch.object(paper,'configured',return_value=True))
        ctx.enter_context(patch.object(paper,'create_engine',return_value=self.engine))
        ctx.enter_context(patch.object(paper,'ENABLED',enabled))
        ctx.enter_context(patch.object(paper.fast,'api',return_value={'id':'primary-fast'}))
        ctx.enter_context(patch.object(self.engine,'api',side_effect=self.engine_api))
        ctx.enter_context(patch.object(self.engine,'market_session_today',return_value=self.session))
        ctx.enter_context(patch.object(self.engine,'now_ny',return_value=NOW))
        ctx.enter_context(patch.object(self.engine,'all_positions',return_value={}))
        ctx.enter_context(patch.object(self.engine,'open_orders',return_value=[]))
        ctx.enter_context(patch.object(self.engine,'reconcile',return_value=safe))
        ctx.enter_context(patch.object(self.engine,'clock_open',return_value=True))
        ctx.enter_context(patch.object(self.engine,'ensure_no_untracked_classic_orders'))
        ctx.enter_context(patch.object(self.engine,'eligible_asset',return_value=True))
        ctx.enter_context(patch.object(paper.fast,'collect_signals',return_value=[self.signal]))
        ctx.enter_context(patch.object(paper.fast,'validate_live',return_value=self.live))
        ctx.enter_context(patch.object(paper.fast,'signal_age_seconds',return_value=60))
        return ctx

    def test_engine_isolation_and_prefix(self):
        self.assertEqual(classic.VERSION,'2.5.2')
        self.assertEqual(classic.ENTRY_PREFIX,'B252_')
        self.assertEqual(self.engine.ENTRY_PREFIX,'MICRO1_')
        self.assertEqual(self.engine.ALPACA_API,'https://paper-api.alpaca.markets/v2')
        signal={'symbol':'BB','bar_time':NOW}
        self.assertTrue(self.engine.my_entry_client_id(signal).startswith('MICRO1_'))
        self.assertTrue(classic.my_entry_client_id(signal).startswith('B252_'))

    def test_missing_keys_never_use_primary_keys(self):
        with patch.dict('os.environ',{'ALPACA_API_KEY':'primary','ALPACA_SECRET_KEY':'primary'},clear=True):
            with self.assertRaises(RuntimeError):self.engine.alpaca_headers()
        with patch.object(paper,'configured',return_value=False),patch.object(paper,'create_engine') as create:
            paper.process();create.assert_not_called()

    def test_state_without_keys_raises_instead_of_silent_monitor_skip(self):
        paper.STATE_FILE.write_text('{}')
        with patch.object(paper,'configured',return_value=False):
            with self.assertRaises(RuntimeError):paper.process()

    def test_same_account_rejected_before_reconcile_or_orders(self):
        self.process_patches()
        with patch.object(paper.fast,'api',return_value={'id':'separate-micro'}):
            with self.assertRaisesRegex(RuntimeError,'different PAPER account'):paper.process()
        self.engine.reconcile.assert_not_called()

    def test_account_change_rejected(self):
        self.process_patches()
        state=self.engine.load_state();state['account_id']='old-account';self.engine.save_state(state)
        with self.assertRaisesRegex(RuntimeError,'account changed'):paper.process()

    def test_safe_mode_monitors_without_submitting(self):
        self.process_patches(enabled=False)
        with patch.object(self.engine,'send_bracket') as send:
            paper.process();send.assert_not_called()
        self.engine.reconcile.assert_called_once()
        paper.fast.collect_signals.assert_not_called()

    def test_unverified_protection_blocks_new_entries(self):
        self.process_patches(safe=False)
        with patch.object(self.engine,'send_bracket') as send:
            paper.process();send.assert_not_called()

    def test_paper_signal_is_converted_to_micro_bracket(self):
        self.process_patches()
        with patch.object(self.engine,'send_bracket',return_value=True) as send:
            paper.process()
            self.assertEqual(send.call_args.args[0]['side'],'BUY')
            settings=send.call_args.args[1]
            self.assertEqual(settings['stop'],9.95)
            self.assertEqual(settings['tp'],10.13)
            self.assertLessEqual(settings['planned_risk'],1.0+1e-9)

    def test_stale_signal_cannot_submit(self):
        self.process_patches()
        with patch.object(paper.fast,'signal_age_seconds',return_value=1000),patch.object(self.engine,'send_bracket') as send:
            paper.process();send.assert_not_called()

    def test_shared_symbol_position_blocks_micro_entry(self):
        self.process_patches()
        with patch.object(self.engine,'all_positions',return_value={'BB':{'qty':'2'}}),patch.object(self.engine,'send_bracket') as send:
            paper.process();send.assert_not_called()

    def test_eod_runs_even_with_entry_toggle_off(self):
        self.process_patches(enabled=False)
        with patch.object(self.engine,'now_ny',return_value=self.session['liquidate_at']),patch.object(self.engine,'end_of_day') as close,patch.object(self.engine,'send_bracket') as send:
            paper.process();close.assert_called_once();send.assert_not_called()

    def test_price_sizing_and_short_geometry(self):
        long=paper.prices(self.signal,self.live)
        short=paper.prices({**self.signal,'side':'sell'},{**self.live,'price':9.99})
        self.assertLess(long['stop'],long['reference_entry']);self.assertGreater(long['tp'],long['reference_entry'])
        self.assertGreater(short['stop'],short['reference_entry']);self.assertLess(short['tp'],short['reference_entry'])
        for p in (long,short):
            self.assertLessEqual(p['planned_risk'],1+1e-9)
            self.assertLessEqual(p['qty']*p['reference_entry'],300)
        self.assertIsNone(paper.prices(self.signal,{**self.live,'price':float('nan')}))

    def test_submit_intent_survives_ambiguous_post(self):
        state=self.engine.load_state()
        signal={'symbol':'BB','side':'BUY','bar_time':NOW}
        with patch.object(self.engine,'api',side_effect=RuntimeError('timeout')):
            with self.assertRaises(RuntimeError):self.engine.send_bracket(signal,paper.prices(self.signal,self.live),state)
        saved=self.engine.load_state();record=next(iter(saved['trades'].values()))
        self.assertEqual(record['status'],'verify_order')
        self.assertTrue(record['client_id'].startswith('MICRO1_'))
        with patch.object(self.engine,'api') as api:
            self.assertFalse(self.engine.send_bracket(signal,paper.prices(self.signal,self.live),saved))
            api.assert_not_called()

    def test_daily_limits_and_incomplete_history(self):
        state=self.engine.load_state()
        for i in range(12):
            state['trades'][str(i)]={'client_id':str(i),'symbol':str(i),'submitted':NOW.isoformat(),'status':'closed'}
        with patch.object(self.engine,'api',return_value=[]):
            allowed,seen=paper.daily_guard(self.engine,state,self.session,{})
            self.assertFalse(allowed);self.assertEqual(len(seen),12)
        with patch.object(self.engine,'api',return_value=[{}]*500):
            with self.assertRaises(RuntimeError):paper.daily_guard(self.engine,state,self.session,{})

    def test_micro_two_position_cap_and_daily_loss_remain(self):
        state=self.engine.load_state()
        state['trades']={str(i):{'client_id':str(i),'symbol':str(i),
            'submitted':NOW.isoformat(),'status':'open'} for i in range(2)}
        with patch.object(self.engine,'api',return_value=[]):
            self.assertFalse(paper.daily_guard(self.engine,state,self.session,{})[0])
        state['trades'].pop('1')
        with patch.object(self.engine,'api',return_value=[]):
            self.assertFalse(paper.daily_guard(self.engine,state,self.session,
                {'0':{'unrealized_pl':'-6'}})[0])

    def test_eod_prefix_and_no_foreign_order_cancellation(self):
        state=self.engine.load_state()
        t={'symbol':'BB','side':'BUY','client_id':'MICRO1_BB_202610301100',
           'status':'open','entry_id':'entry'}
        parent={'id':'entry','status':'filled','filled_qty':'2','side':'buy',
                'legs':[{'id':'stop','type':'stop','status':'new'}]}
        with patch.object(self.engine,'get_tracked_order',return_value=parent), \
             patch.object(self.engine,'all_positions',return_value={'BB':{'qty':'2'}}), \
             patch.object(self.engine,'verify_owned_position',return_value=True), \
             patch.object(self.engine,'symbol_open_orders',return_value=[{'id':'foreign'}]), \
             patch.object(self.engine,'cancel_order') as cancel:
            with self.assertRaises(RuntimeError):self.engine.eod_close_trade(t,state)
            cancel.assert_not_called()
        with patch.object(self.engine,'get_tracked_order',return_value=parent), \
             patch.object(self.engine,'all_positions',return_value={'BB':{'qty':'2'}}), \
             patch.object(self.engine,'verify_owned_position',return_value=True), \
             patch.object(self.engine,'symbol_open_orders',return_value=[]), \
             patch.object(self.engine,'cancel_order'), \
             patch.object(self.engine,'order_by_client',return_value=None), \
             patch.object(self.engine,'api',return_value={'id':'eod','status':'new'}) as api, \
             patch.object(self.engine,'telegram'):
            self.engine.eod_close_trade(t,state)
            self.assertTrue(api.call_args.kwargs['payload']['client_order_id'].startswith('MICRO1_EOD_'))

    def test_verified_pnl_uses_micro_fill_file_only(self):
        state=self.engine.load_state()
        t={'symbol':'BB','side':'BUY','client_id':'MICRO1_BB_202610301100',
           'submitted':NOW.isoformat(),'status':'open'}
        parent={'filled_qty':'2','filled_avg_price':'10','filled_at':NOW.isoformat(),
                'legs':[{'filled_qty':'2','filled_avg_price':'10.13',
                         'filled_at':(NOW+timedelta(minutes=5)).isoformat(),'type':'limit'}]}
        with patch.object(self.engine,'telegram'):
            self.engine.report_close_if_verified(t,parent,state)
            self.engine.report_close_if_verified(t,parent,state)
        with paper.RESULTS_FILE.open() as f: rows=list(csv.DictReader(f))
        self.assertEqual(len(rows),1)
        self.assertAlmostEqual(float(rows[0]['pnl_usd']),.26)
        self.assertEqual(t['status'],'closed')

    def test_virtual_collection_survives_paper_cycle_failure(self):
        session={'open':NOW-timedelta(hours=1),'close':NOW+timedelta(hours=4)}
        with patch.object(entrypoint.fast,'market_session_today',return_value=session),patch.object(entrypoint.paper,'process',side_effect=RuntimeError('paper failed')),patch.object(entrypoint.fast,'notify'),patch.object(entrypoint.micro,'process',return_value={}) as virtual:
            with self.assertRaises(RuntimeError):entrypoint.main(now=NOW)
            virtual.assert_called_once()


if __name__=='__main__':unittest.main()
