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

    @patch("notifier.requests.post", create=True)
    def test_openai_successfully_backs_up_gemini(self, post):
        post.side_effect = [
            FakeResponse(429, {"error": {"code": "RESOURCE_EXHAUSTED"}}),
            FakeResponse(200, {"choices": [{"message": {"content": "Fallback proposal"}}]}),
        ]
        with patch.object(notifier, "GEMINI_API_KEY", "gemini-key"), \
                patch.object(notifier, "GEMINI_MODELS", ["test-gemini"]), \
                patch.object(notifier, "OPENAI_API_KEY", "openai-key"):
            self.assertEqual(notifier._generate("test"), "Fallback proposal")

        self.assertEqual(post.call_count, 2)
        self.assertIn("generativelanguage.googleapis.com", post.call_args_list[0].args[0])
        self.assertEqual(post.call_args_list[1].args[0],
                         "https://api.openai.com/v1/chat/completions")

    @patch("notifier._openai_generate")
    @patch("notifier._gemini_generate")
    def test_proposal_qa_switches_from_gemini_to_openai(self, gemini, openai):
        gemini.return_value = draft("Hi,\n\nI have not used MCP, but I can implement it.")
        openai.return_value = draft(
            "Hi,\n\nSalom AI Business proves the production agent architecture needed here: "
            "isolated knowledge, operational actions, human handoff, and monitored API flows."
        )
        with patch.object(notifier, "OPENAI_API_KEY", "openai-key"):
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

        self.assertEqual(result, openai.return_value)
        gemini.assert_called_once()
        openai.assert_called_once()

    @patch("notifier._generate")
    def test_quality_rejection_is_not_reported_as_missing_providers(self, generate):
        generate.return_value = draft(
            "Hi,\n\nI have not used this framework, but I can learn it."
        )
        self.assertIsNone(notifier.generate_proposal({
            "title": "Mobile MVP",
            "description": "Build it",
            "skills": [],
            "matched": [],
            "score": 30,
            "job_type": "FIXED",
            "fixed": 2000,
        }))
        message = notifier._ai_failure_message("a proposal")
        self.assertIn("quality checks", message)
        self.assertNotIn("No AI provider", message)

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

    def test_team_mode_is_conservative(self):
        individual_jobs = (
            {"title": "Senior Flutter Developer", "description": "Join our team and collaborate with our designer."},
            {"title": "iOS Engineer", "description": "Individual freelancers only. No agencies."},
            {"title": "React Native Developer", "description": "Work directly with the developer on our product team."},
        )
        for job in individual_jobs:
            job["skills"] = []
            with self.subTest(job=job["title"]):
                self.assertEqual(notifier._proposal_mode(job), "individual")

        team_jobs = (
            {"title": "Senior AI Full-Stack Development Team Needed", "description": "Enhance our production pipeline."},
            {"title": "Flutter Developer or Small Team", "description": "A small team is welcome."},
            {"title": "Product build", "description": "We are looking for a development agency to handle mobile and backend."},
        )
        for job in team_jobs:
            job["skills"] = []
            with self.subTest(job=job["title"]):
                self.assertEqual(notifier._proposal_mode(job), "team")

    def test_rejects_formulaic_or_confrontational_preview(self):
        examples = (
            "Hi,\n\nThe main risk is not the UI, it is the backend architecture.",
            "Hi,\n\nThis product lives or dies on realtime latency.",
            "Hi,\n\nBefore touching the code, I would audit every dependency.",
            "Hi,\n\nThe hard part is getting Apple approval.",
        )
        for cover in examples:
            with self.subTest(cover=cover):
                self.assertIn(
                    "uses a confrontational or formulaic preview",
                    notifier._proposal_hard_failures(draft(cover), "individual"),
                )

    def test_individual_mode_rejects_fera_tech_pitch(self):
        cover = "Hi,\n\nLaunchcast is a close match. Our team at Fera Tech can deliver the app."
        self.assertIn(
            "mentions a company or delivery team in individual mode",
            notifier._proposal_hard_failures(draft(cover), "individual"),
        )

    def test_team_mode_requires_fera_tech_and_personal_lead(self):
        missing = "Hi,\n\nI can assemble a team to deliver the mobile and backend work."
        failures = notifier._proposal_hard_failures(draft(missing), "team")
        self.assertIn("team mode does not mention Fera Tech", failures)
        self.assertIn("team mode does not say Shohruh will lead personally", failures)

        valid = (
            "Hi,\n\nFera Tech can cover the mobile and backend work in parallel, and I'll "
            "personally lead the architecture, implementation reviews, and communication."
        )
        failures = notifier._proposal_hard_failures(draft(valid), "team")
        self.assertNotIn("team mode does not mention Fera Tech", failures)
        self.assertNotIn("team mode does not say Shohruh will lead personally", failures)

        alternate = (
            "Hi,\n\nFera Tech can cover the mobile and backend work in parallel. I will serve "
            "as your technical lead and remain accountable for delivery and communication."
        )
        self.assertNotIn(
            "team mode does not say Shohruh will lead personally",
            notifier._proposal_hard_failures(draft(alternate), "team"),
        )

    def test_fera_tech_is_not_counted_as_a_portfolio_project(self):
        cover = (
            "Hi,\n\nFera Tech can cover this build in parallel, and I'll personally lead it. "
            "BandMate proves the realtime audio architecture, while Salom AI Business proves "
            "the production AI workflow and deployment layer."
        )
        self.assertNotIn(
            "uses 3 portfolio projects",
            notifier._proposal_hard_failures(draft(cover), "team"),
        )

    def test_proposal_buttons_require_application_confirmation(self):
        keyboard = notifier._proposal_buttons("~job123")["inline_keyboard"]
        callbacks = [button["callback_data"] for row in keyboard for button in row]
        self.assertIn("a:~job123", callbacks)
        self.assertIn("s:~job123", callbacks)
        self.assertIn("q:~job123", callbacks)

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
