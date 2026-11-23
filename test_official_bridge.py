import copy
import json
import os
import sys
import tempfile
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

# These tests exercise validation/delivery state with no HTTP dependency or network.
curl = types.ModuleType("curl_cffi")
curl.requests = types.SimpleNamespace()
sys.modules.setdefault("curl_cffi", curl)
import notifier
import official_bridge as bridge


def sample():
    now = datetime.now(timezone.utc).isoformat()
    return {"source": "upwork_official_mcp", "account_role": "Freelancer", "items": [{
        "kind": "connects", "observed_at": now,
        "evidence": {"tool": "get_freelancer_financials", "action": "connects_balance",
                     "trace_id": "test-trace", "result": {
                         "status": "ok", "trace_id": "test-trace", "balance": {"connectsBalance": 31}}},
    }]}


class OfficialBridgeTests(unittest.TestCase):
    def test_retired_discovery_never_contacts_any_network(self):
        with patch.object(notifier.requests, "get", create=True) as get, \
                patch.object(notifier.requests, "post", create=True) as post:
            for fn in (notifier.get_token, notifier.fetch_page, notifier.fetch_all_jobs,
                       notifier.run_job_check, notifier.get_proxy_dict):
                with self.assertRaisesRegex(RuntimeError, "disabled"):
                    fn(None)
            get.assert_not_called()
            post.assert_not_called()

    def test_preview_is_offline_and_rejects_failed_or_unproven_reads(self):
        with patch.object(bridge, "post_json") as post:
            rows = bridge.prepare(sample())
            self.assertIn("31", rows[0]["parts"][0])
            post.assert_not_called()
        for field, value in (("trace_id", "mismatch"), ("status", "error")):
            batch = sample()
            batch["items"][0]["evidence"]["result"][field] = value
            with self.assertRaises(ValueError):
                bridge.prepare(batch)

    def test_stale_data_and_non_personal_accounts_are_rejected(self):
        batch = sample()
        batch["items"][0]["observed_at"] = "2020-01-01T00:00:00Z"
        with self.assertRaises(ValueError):
            bridge.prepare(batch)
        batch = sample()
        batch["account_role"] = "Agency"
        with self.assertRaises(ValueError):
            bridge.prepare(batch)

    def test_submitted_and_viewed_are_not_reported_as_hired(self):
        batch = sample()
        item = batch["items"][0]
        item["kind"] = "proposal_snapshot"
        item["evidence"].update(tool="list_freelancer_proposals", action="list")
        item["evidence"]["result"] = {
            "status": "ok", "trace_id": "test-trace", "hasMore": True,
            "data": {"vendorProposals": {"edges": [
                {"node": {"status": {"status": "Accepted"}, "viewedByClient": True}},
                {"node": {"status": {"status": "Pending"}, "viewedByClient": False}},
            ]}},
        }
        row = bridge.prepare(batch)[0]
        self.assertIn("Submitted proposals on this page: 1", row["parts"][0])
        self.assertIn("partial", row["parts"][0])
        self.assertIn("unknown", row["parts"][0])
        self.assertEqual(row["tracker"], [])

    def test_confirmed_delivery_is_not_sent_twice(self):
        with tempfile.TemporaryDirectory() as temp, \
                patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "test", "TELEGRAM_CHAT_ID": "1"}), \
                patch.object(bridge, "post_json", return_value={"ok": True, "result": {"message_id": 42}}) as post:
            rows = bridge.prepare(sample())
            path = Path(temp) / "state.sqlite3"
            first = bridge.deliver(rows, path)
            second = bridge.deliver(rows, path)
            self.assertEqual(first[0]["receipt"], "42")
            self.assertFalse(second[0]["replayed"])
            self.assertEqual(post.call_count, 1)

    def test_uncertain_send_is_not_blindly_retried(self):
        with tempfile.TemporaryDirectory() as temp, \
                patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "test", "TELEGRAM_CHAT_ID": "1"}), \
                patch.object(bridge, "post_json", side_effect=TimeoutError) as post:
            rows = bridge.prepare(sample())
            path = Path(temp) / "state.sqlite3"
            self.assertEqual(bridge.deliver(rows, path)[0]["status"], "uncertain")
            self.assertEqual(bridge.deliver(rows, path)[0]["status"], "uncertain")
            self.assertEqual(post.call_count, 1)

    def test_application_leads_require_active_eligible_official_details(self):
        batch = sample()
        item = batch["items"][0]
        item["kind"] = "job"
        item["evidence"].update(tool="find_jobs", action="get")
        result = {"status": "ok", "trace_id": "test-trace", "applied": False, "can_apply": True,
                  "data": {"marketplaceJobPosting": {"id": "123", "url": "https://www.upwork.com/jobs/~02123",
                           "content": {"title": "Flutter app"}, "workFlowState": {"status": "ACTIVE"}}}}
        item["evidence"]["result"] = result
        self.assertEqual(bridge.prepare(batch)[0]["key"], "job:123")
        for mutation in ({"applied": True}, {"can_apply": False}):
            changed = copy.deepcopy(batch)
            changed["items"][0]["evidence"]["result"].update(mutation)
            with self.assertRaises(ValueError):
                bridge.prepare(changed)
        result["data"]["marketplaceJobPosting"]["url"] = "https://evil.example/jobs/~02123"
        with self.assertRaises(ValueError):
            bridge.prepare(batch)


if __name__ == "__main__":
    unittest.main()
