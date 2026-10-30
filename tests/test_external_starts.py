import importlib.util
import json
import unittest
from pathlib import Path
from zoneinfo import ZoneInfo
from datetime import datetime

spec=importlib.util.spec_from_file_location('external_starts',Path(__file__).resolve().parents[1]/'scripts/configure_external_starts.py')
external=importlib.util.module_from_spec(spec)
spec.loader.exec_module(external)


class ExternalStartTests(unittest.TestCase):
    def test_all_three_dispatches_enable_full_session_without_one_shot(self):
        for name in external.WORKFLOWS:
            job=external.job(name,'test-token')
            self.assertEqual(job['requestMethod'],1)
            self.assertTrue(job['url'].endswith(name+'/dispatches'))
            self.assertEqual(json.loads(job['extendedData']['body']),
                {'ref':'main','inputs':{'full_paper_session':True}})
            self.assertFalse(job['saveResponses'])
            self.assertEqual(job['schedule']['timezone'],'America/New_York')
            self.assertEqual(job['schedule']['hours'],[9])
            self.assertEqual(job['schedule']['minutes'],[20,25,30])
            self.assertEqual(job['schedule']['wdays'],[1,2,3,4,5])

    def test_ny_dst_matches_ten_minutes_before_us_open(self):
        for date,local in [('2026-10-30','14:20'),('2026-11-02','15:20')]:
            ny=datetime.fromisoformat(date+'T09:20').replace(tzinfo=ZoneInfo('America/New_York'))
            self.assertEqual(ny.astimezone(ZoneInfo('Europe/Bratislava')).strftime('%H:%M'),local)
