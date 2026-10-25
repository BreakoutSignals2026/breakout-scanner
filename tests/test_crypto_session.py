"""Offline checks for CRYPTO continuous 5-minute GitHub schedule.

No broker credentials and no actual trading. Workflow regression guardrails.
"""
from pathlib import Path
import re
import subprocess
import unittest

BASE = Path(__file__).resolve().parent.parent
WORKFLOW = BASE / ".github/workflows/crypto_scanner.yml"
SCRIPT = BASE / "scripts/crypto_session_loop.sh"


class CryptoContinuousTests(unittest.TestCase):
    def test_continuous_runner_bash_syntax(self):
        result = subprocess.run(["bash", "-n", str(SCRIPT)],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_scheduled_handover_without_five_minute_cron(self):
        yaml = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn('cron: "17 0,3,6,9,12,15,18,21 * * *"', yaml)
        self.assertNotIn('cron: "*/5 * * * *"', yaml)
        self.assertIn("timeout-minutes: 240", yaml)
        self.assertIn("group: crypto-breakout-v1", yaml)
        self.assertIn("cancel-in-progress: false", yaml)

    def test_only_scheduled_runs_loop(self):
        yaml = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("CRYPTO_CONTINUOUS:", yaml)
        self.assertIn("github.event_name == 'schedule' && 'true' || 'false'", yaml)
        self.assertIn("bash scripts/crypto_session_loop.sh", yaml)
        shell = SCRIPT.read_text(encoding="utf-8")
        self.assertIn('if [[ "$continuous" != "true" ]]; then', shell)
        self.assertIn('exit "$scan_status"', shell)

    def test_state_persisted_each_cycle_before_sleep(self):
        shell = SCRIPT.read_text(encoding="utf-8")
        self.assertLess(shell.index("git add --"),
                        shell.index("next_epoch="))
        self.assertIn("git pull --no-rebase -X ours origin main", shell)
        self.assertIn("git push origin HEAD:main", shell)
        self.assertIn("Could not persist PAPER broker state", shell)
        self.assertIn("python -u scanner_crypto_v1.py", shell)
        self.assertIn('if [[ -f "$state_file" ]]; then', shell)
        self.assertIn("(now_epoch / 300 + 1) * 300 + 15", shell)

    def test_safe_startup_guards(self):
        shell = SCRIPT.read_text(encoding="utf-8")
        self.assertIn("GITHUB_EVENT_NAME",shell)
        self.assertIn("3 * 3600 + 45 * 60",shell)
        yaml = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("paper-api", (BASE / "crypto_paper_v1.py").read_text())
        self.assertIn("inputs.enable_paper_entries == true",yaml)


if __name__ == "__main__":
    unittest.main()
