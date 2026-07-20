import json
import sys
import types
import unittest
from unittest.mock import patch

# Proposal QA does not make network calls. Stub the optional runtime HTTP dependency so
# these guardrail tests remain runnable with the standard library alone.
curl_cffi = types.ModuleType("curl_cffi")
curl_cffi.requests = types.SimpleNamespace()
sys.modules.setdefault("curl_cffi", curl_cffi)

import notifier


class FakeResponse:
    def __init__(self, status_code, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text or json.dumps(self._payload)

    def json(self):
        return self._payload


def draft(cover_letter):
    return json.dumps({"hook_type": "proof-led", "cover_letter": cover_letter})


class ProposalQualityTests(unittest.TestCase):
    @patch("notifier._generate", return_value=None)
    def test_provider_failure_does_not_trigger_qa_retries(self, generate):
        self.assertIsNone(notifier.generate_proposal({
            "title": "Mobile MVP", "description": "Build it", "skills": [],
            "matched": [], "score": 30, "job_type": "FIXED", "fixed": 2000,
        }))
        generate.assert_called_once()

    @patch("notifier.time.sleep")
    @patch("notifier.requests.post", create=True)
    def test_openai_retries_a_transient_rate_limit(self, post, _sleep):
        post.side_effect = [
            FakeResponse(429, {"error": {"code": "rate_limit_exceeded"}}),
            FakeResponse(200, {"choices": [{"message": {"content": "OK"}}]}),
        ]
        with patch.object(notifier, "OPENAI_API_KEY", "test-key"):
            self.assertEqual(notifier._openai_generate("test", max_tokens=32), "OK")
        self.assertEqual(post.call_count, 2)

    @patch("notifier.requests.post", create=True)
    def test_openai_quota_is_reported_after_gemini_fallback(self, post):
        post.side_effect = [
            FakeResponse(429, {"error": {"code": "RESOURCE_EXHAUSTED"}}),
            FakeResponse(429, {"error": {"code": "insufficient_quota"}}),
        ]
        with patch.object(notifier, "GEMINI_API_KEY", "gemini-key"), \
                patch.object(notifier, "GEMINI_MODELS", ["test-gemini"]), \
                patch.object(notifier, "OPENAI_API_KEY", "openai-key"):
            self.assertIsNone(notifier._generate("test"))
        self.assertIn("billing quota", notifier._ai_failure_message("a proposal"))
        self.assertEqual(post.call_count, 2)

    def test_rejects_self_disqualifying_capability_language(self):
        examples = (
            "Hi,\n\nI have not shipped LangGraph, but I can learn it quickly.",
            "Hi,\n\nI don't have direct DrChrono experience, but I can integrate it.",
            "Hi,\n\nPlaid would be a new integration for me.",
            "Hi,\n\nI have never used Capacitor in production.",
        )
        for cover in examples:
            with self.subTest(cover=cover):
                self.assertIn(
                    "uses self-disqualifying capability-gap language",
                    notifier._proposal_hard_failures(draft(cover)),
                )

    def test_accepts_truthful_positive_comparative_framing(self):
        cover = (
            "Hi,\n\nLaunchcast and CrisisPath are published iOS deliveries I carried through "
            "signing, TestFlight, and App Review. Those releases used SwiftUI and Flutter; for "
            "this Capacitor build I would apply the same native permissions, review, and release "
            "discipline around the web wrapper."
        )
        self.assertNotIn(
            "uses self-disqualifying capability-gap language",
            notifier._proposal_hard_failures(draft(cover)),
        )

    def test_private_fit_warning_is_not_mixed_into_cover_letter(self):
        raw = json.dumps({
            "hook_type": "proof-led",
            "cover_letter": "Hi,\n\nLaunchcast proves end-to-end iOS release ownership.",
            "fit_warning": (
                "The client explicitly requires published Capacitor or Ionic examples; the "
                "verified portfolio currently proves native iOS and Flutter releases."
            ),
        })
        messages = notifier.format_proposal_messages(raw)

        self.assertEqual(messages[0], "Hi,\n\nLaunchcast proves end-to-end iOS release ownership.")
        self.assertIn("Private fit warning", messages[1])
        self.assertNotIn("Capacitor", messages[0])

    @patch("notifier._generate")
    def test_repaired_draft_is_validated_before_return(self, generate):
        weak = draft("Hi,\n\nI have not used MCP, but I can implement it.")
        strong = draft(
            "Hi,\n\nSalom AI Business proves the multi-tenant AI workflow core: isolated "
            "knowledge, operational actions, human handoff, and production monitoring. I would "
            "map those same boundaries to MCP tools with explicit permissions and trace logs."
        )
        generate.side_effect = [weak, strong]

        result = notifier.generate_proposal({
            "title": "Production MCP platform",
            "description": "Build a secure multi-agent platform.",
            "skills": ["MCP", "FastAPI"],
            "matched": ["ai agent"],
            "score": 90,
            "job_type": "HOURLY",
            "hourly_min": 30,
            "hourly_max": 60,
            "fixed": 0,
        })

        self.assertEqual(result, strong)
        self.assertEqual(generate.call_count, 2)


if __name__ == "__main__":
    unittest.main()
