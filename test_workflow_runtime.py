import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent
CHECK_WORKFLOW = (ROOT / ".github" / "workflows" / "notifier.yml").read_text()
DEPLOY_WORKFLOW = (ROOT / ".github" / "workflows" / "deploy-notifier.yml").read_text()
SERVICE = (ROOT / "deploy" / "upwork-notifier.service").read_text()


class WorkflowRuntimeTests(unittest.TestCase):
    def test_digitalocean_is_the_only_continuous_runtime(self):
        self.assertIn("ExecStart=/opt/upwork-notifier/current/.venv/bin/python", SERVICE)
        self.assertIn("Restart=always", SERVICE)
        self.assertIn('"SERVE_SECONDS": "31536000"', DEPLOY_WORKFLOW)
        self.assertNotIn("schedule:", CHECK_WORKFLOW)
        self.assertNotIn("- name: Cycle", CHECK_WORKFLOW)

    def test_only_offline_tests_run_without_an_explicit_delivery_batch(self):
        self.assertIn("workflow_dispatch:", CHECK_WORKFLOW)
        self.assertIn("deliver_results:", CHECK_WORKFLOW)
        self.assertNotIn("--check-openai", CHECK_WORKFLOW)
        self.assertNotIn("schedule:", CHECK_WORKFLOW)
        self.assertIn("python -m unittest -q", CHECK_WORKFLOW)
        self.assertIn("Preserve receipts including uncertain sends", CHECK_WORKFLOW)
        self.assertIn("initialize_delivery_state", CHECK_WORKFLOW)
        self.assertIn("branches: [main]", DEPLOY_WORKFLOW)
        self.assertNotIn("WEBSHARE_URL", DEPLOY_WORKFLOW)


if __name__ == "__main__":
    unittest.main()
