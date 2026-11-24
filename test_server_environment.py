import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from deploy.run_notifier import load_environment


class ServerEnvironmentTests(unittest.TestCase):
    def test_server_environment_loads_without_a_github_connection(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ):
            path = Path(temp) / "env.json"
            path.write_text('{"TELEGRAM_CHAT_ID":"test-owner", "SERVE_SECONDS":"3600"}')
            load_environment(path)
            self.assertEqual(os.environ["TELEGRAM_CHAT_ID"], "test-owner")
            self.assertEqual(os.environ["SERVE_SECONDS"], "3600")

    def test_invalid_environment_never_partially_overwrites_configuration(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {"TELEGRAM_CHAT_ID": "original"}):
            path = Path(temp) / "env.json"
            path.write_text('{"TELEGRAM_CHAT_ID":"replacement", "INVALID":42}')
            with self.assertRaises(RuntimeError):
                load_environment(path)
            self.assertEqual(os.environ["TELEGRAM_CHAT_ID"], "original")
