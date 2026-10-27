"""Offline US startup recovery tests (never submit actual broker requests)."""
import os
import unittest
from datetime import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo
import scripts.us_start_recovery as recovery

NY=ZoneInfo("America/New_York")
NOW=datetime(2026,10,27,11,15,tzinfo=NY)

class StartupTests(unittest.TestCase):
    def test_preflight_success_without_scanner_is_not_running(self):
        self.assertFalse(recovery.run_has_started(
            {"status":"completed","conclusion":"success"},
            [{"name":"preflight","conclusion":"success"},
             {"name":"first-session","conclusion":"skipped"}]))
        self.assertTrue(recovery.run_has_started(
            {"status":"completed","conclusion":"success"},
            [{"name":"first-session","conclusion":"success"}]))
        self.assertTrue(recovery.run_has_started({"status":"queued"},[]))

    def test_recovery_only_weekday_mid_session(self):
        self.assertTrue(recovery.safe_window(NOW))
        self.assertFalse(recovery.safe_window(datetime(2026,10,27,9,20,tzinfo=NY)))
        self.assertFalse(recovery.safe_window(datetime(2026,10,27,15,0,tzinfo=NY)))
        self.assertFalse(recovery.safe_window(datetime(2026,10,25,11,15,tzinfo=NY)))

    def test_no_duplicate_dispatch_for_active_and_real_completed(self):
        seen=[]
        def get(method,url,headers):
            if "/runs?" in url:
                return {"workflow_runs":[{"id":42,"status":"completed","conclusion":"success",
                                          "created_at":"2026-10-27T14:00:00Z"}]}
            if "/runs/42/jobs?" in url:
                return {"jobs":[{"name":"first-session","conclusion":"success"}]}
            raise AssertionError(url)
        def post(*args,**kwargs):
            seen.append(kwargs)
        with patch.dict(os.environ,{"GITHUB_TOKEN":"x","GITHUB_REPOSITORY":"BreakoutSignals2026/breakout-scanner"}):
            result=recovery.recover(NOW,get=get,post=post,verify_calendar=False)
        self.assertEqual(result["dispatched"],[])
        self.assertEqual(seen,[])

    def test_missing_all_four_safe_dispatch_inputs(self):
        dispatched=[]
        def get(method,url,headers):
            return {"workflow_runs":[]}
        def post(method,url,payload,headers):
            self.assertEqual(method,"POST")
            self.assertTrue(url.startswith(recovery.API))
            self.assertEqual(payload["ref"],"main")
            dispatched.append((url,payload["inputs"]))
        with patch.dict(os.environ,{"GITHUB_TOKEN":"x","GITHUB_REPOSITORY":"BreakoutSignals2026/breakout-scanner"}):
            result=recovery.recover(NOW,get=get,post=post,verify_calendar=False)
        self.assertEqual(len(dispatched),4)
        self.assertEqual(result["dispatched"],list(recovery.TARGETS))
        self.assertEqual(dispatched[0][1],{"full_paper_session":"true"})
        self.assertEqual(dispatched[1][1],{"full_paper_session":"true"})
        self.assertEqual(dispatched[2][1],{})
        self.assertEqual(dispatched[3][1],{})

    def test_calendar_holiday_never_dispatches(self):
        got=[]
        def get(method,url,headers):
            return []
        with patch.dict(os.environ,{"GITHUB_TOKEN":"x","GITHUB_REPOSITORY":"BreakoutSignals2026/breakout-scanner","ALPACA_API_KEY":"x","ALPACA_SECRET_KEY":"x"}):
            result=recovery.recover(NOW,get=get,post=lambda *a,**k:got.append(a))
        self.assertEqual(result["skipped"],"calendar")
        self.assertEqual(got,[])

if __name__=="__main__":
    unittest.main()
