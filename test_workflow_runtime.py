import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent
WORKFLOW = (ROOT / ".github" / "workflows" / "notifier.yml").read_text()
RUN_CYCLE = (ROOT / "run_cycle.sh").read_text()


class WorkflowRuntimeTests(unittest.TestCase):
    def test_worker_runs_continuously_for_exactly_five_hours(self):
        seconds = int(re.search(r'CYCLE_SECONDS: "(\d+)"', WORKFLOW).group(1))
        cycles = len(re.findall(r"^      - name: Cycle \d+$", WORKFLOW, re.MULTILINE))

        self.assertEqual(cycles, 8)
        self.assertEqual(seconds * cycles, 5 * 60 * 60)
        self.assertIn('SERVE_SECONDS="${CYCLE_SECONDS:-2250}"', RUN_CYCLE)

    def test_restart_and_openai_safety_guards_remain_enabled(self):
        self.assertIn("github.event_name != 'schedule'", WORKFLOW)
        self.assertIn("Verify OpenAI fallback credentials", WORKFLOW)
        self.assertIn("python notifier.py --check-openai", WORKFLOW)
        self.assertIn("OPENAI_API_KEY: ${{ secrets.OPENAI_API_KEY }}", WORKFLOW)
        self.assertNotRegex(
            WORKFLOW,
            r"Verify OpenAI fallback credentials\n\s+if:",
        )


if __name__ == "__main__":
    unittest.main()
