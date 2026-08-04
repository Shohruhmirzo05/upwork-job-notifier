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

    def test_github_deploy_and_openai_safety_guards_remain_enabled(self):
        self.assertIn("workflow_dispatch: {}", CHECK_WORKFLOW)
        self.assertIn("Verify OpenAI fallback credentials", CHECK_WORKFLOW)
        self.assertIn("python notifier.py --check-openai", CHECK_WORKFLOW)
        self.assertIn("OPENAI_API_KEY: ${{ secrets.OPENAI_API_KEY }}", CHECK_WORKFLOW)
        self.assertIn("branches: [main]", DEPLOY_WORKFLOW)
        self.assertIn("DEPLOY_OPENAI_API_KEY: ${{ secrets.OPENAI_API_KEY }}", DEPLOY_WORKFLOW)
        self.assertNotRegex(
            CHECK_WORKFLOW,
            r"Verify OpenAI fallback credentials\n\s+if:",
        )


if __name__ == "__main__":
    unittest.main()
