import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

# Proposal QA does not make network calls. Stub the optional runtime HTTP dependency so
# these guardrail tests remain runnable with the standard library alone.
curl_cffi = types.ModuleType("curl_cffi")
curl_cffi.requests = types.SimpleNamespace()
sys.modules.setdefault("curl_cffi", curl_cffi)

import notifier


class FakeResponse:
    def __init__(self, status_code, payload=None, text="", headers=None):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text or json.dumps(self._payload)
        self.headers = headers or {}

    def json(self):
        return self._payload


def draft(cover_letter):
    return json.dumps({"hook_type": "proof-led", "cover_letter": cover_letter})


class ProposalQualityTests(unittest.TestCase):
    def test_dedupe_survives_rotated_upwork_ciphertext(self):
        original = {
            "job_id": "stable-123",
            "cipher": "~old-cipher",
            "title": "Flutter Crisis Safety App",
            "publish": "2026-08-17T08:00:00Z",
        }
        rotated = {**original, "cipher": "~new-cipher"}
        seen = {key: 9_999_999_999 for key in notifier._seen_keys(original)}

        self.assertEqual(notifier._job_identity(original), notifier._job_identity(rotated))
        self.assertTrue(notifier._was_seen(rotated, seen))

    def test_fingerprint_dedupes_when_stable_id_is_missing(self):
        original = {
            "cipher": "~old-cipher",
            "title": "Flutter Crisis Safety App",
            "description": "Build a mobile safety workflow.",
            "publish": "2026-08-17T08:00:00Z",
        }
        rotated = {**original, "cipher": "~new-cipher"}
        seen = {key: 9_999_999_999 for key in notifier._seen_keys(original)}

        self.assertTrue(notifier._was_seen(rotated, seen))

    def test_corrupt_seen_state_reseeds_instead_of_replaying_jobs(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "seen.json"
            path.write_text("{truncated")
            with patch.object(notifier, "SEEN_PATH", path):
                self.assertIsNone(notifier.load_seen())
                notifier.save_seen({"id:stable-123": 9_999_999_999})
                self.assertEqual(notifier.load_seen(), {"id:stable-123": 9_999_999_999})

    def test_parse_job_retains_stable_upwork_id(self):
        parsed = notifier.parse_job({
            "id": "result-123",
            "title": "Flutter app",
            "description": "",
            "ontologySkills": [],
            "jobTile": {"job": {"id": "job-123", "ciphertext": "~cipher"}},
        })
        self.assertEqual(parsed["job_id"], "job-123")
        self.assertEqual(parsed["cipher"], "~cipher")

    def test_backfill_window_is_limited_to_two_hours(self):
        self.assertEqual(notifier.MAX_AGE_HOURS, 2)
        self.assertEqual(notifier.MAX_BURST_NOTIFS, 8)

    def test_legacy_seen_state_migrates_without_sending(self):
        job = {
            "job_id": "stable-123", "cipher": "~new-cipher", "title": "Flutter app",
            "description": "", "skills": ["Flutter"], "job_type": "FIXED",
            "hourly_min": None, "hourly_max": None, "fixed": 1000, "tier": "Intermediate",
            "publish": "", "link": "https://www.upwork.com/jobs/~new-cipher",
        }
        cfg = {"hot_min": 60, "good_min": 30, "search_queries": ["flutter"]}
        legacy_seen = {"~old-cipher": 9_999_999_999}
        with patch("notifier.get_token", return_value="token"), \
                patch("notifier.fetch_all_jobs", return_value=[job]), \
                patch("notifier.score_job", return_value=(True, 30, ["flutter"])), \
                patch("notifier.load_seen", return_value=legacy_seen), \
                patch("notifier.save_seen") as save_seen, \
                patch("notifier.send") as send, \
                patch("notifier.ping_healthcheck"):
            notifier.run_job_check(cfg, None)

        send.assert_not_called()
        save_seen.assert_called_once()
        self.assertIn("id:stable-123", legacy_seen)

    def test_notification_burst_overflow_is_not_replayed(self):
        jobs = []
        for index in range(10):
            jobs.append({
                "job_id": f"stable-{index}", "cipher": f"~cipher-{index}",
                "title": f"Flutter app {index}", "description": "", "skills": ["Flutter"],
                "job_type": "FIXED", "hourly_min": None, "hourly_max": None,
                "fixed": 1000, "tier": "Intermediate", "publish": "",
                "link": f"https://www.upwork.com/jobs/~cipher-{index}",
            })
        cfg = {"hot_min": 60, "good_min": 30, "search_queries": ["flutter"]}
        seen = {"id:existing": 9_999_999_999}
        with patch("notifier.get_token", return_value="token"), \
                patch("notifier.fetch_all_jobs", return_value=jobs), \
                patch("notifier.score_job", return_value=(True, 30, ["flutter"])), \
                patch("notifier.load_seen", return_value=seen), \
                patch("notifier.save_seen"), \
                patch("notifier.load_store", return_value={"offset": 0, "jobs": {}}), \
                patch("notifier.save_store"), \
                patch("notifier.send", return_value=True) as send, \
                patch("notifier.tracker_ingest"), \
                patch("notifier.ping_healthcheck"), \
                patch("notifier.time.sleep"):
            notifier.run_job_check(cfg, None)

        self.assertEqual(send.call_count, 8)
        for index in range(10):
            self.assertIn(f"id:stable-{index}", seen)

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

    @patch("notifier.time.sleep")
    @patch("notifier.requests.post", create=True)
    def test_openai_waits_through_a_full_temporary_rate_limit_window(self, post, sleep):
        post.side_effect = [
            FakeResponse(429, {"error": {"code": "rate_limit_exceeded"}}),
            FakeResponse(429, {"error": {"code": "rate_limit_exceeded"}}),
            FakeResponse(429, {"error": {"code": "rate_limit_exceeded"}}),
            FakeResponse(429, {"error": {"code": "rate_limit_exceeded"}}),
            FakeResponse(200, {"choices": [{"message": {"content": "OK"}}]}),
        ]
        with patch.object(notifier, "OPENAI_API_KEY", "test-key"):
            self.assertEqual(notifier._openai_generate("test", max_tokens=32), "OK")

        self.assertEqual(post.call_count, 5)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [3, 7, 15, 30])

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

    @patch("notifier.time.sleep")
    @patch("notifier.requests.post", create=True)
    def test_openai_credit_balance_exhausted_is_not_retried(self, post, sleep):
        post.return_value = FakeResponse(429, {
            "error": {
                "code": "credit_balance_exhausted",
                "type": "insufficient_quota",
            }
        })
        with patch.object(notifier, "OPENAI_API_KEY", "test-key"):
            self.assertIsNone(notifier._openai_generate("test", max_tokens=32))

        self.assertEqual(post.call_count, 1)
        sleep.assert_not_called()
        self.assertIn("billing quota", notifier._ai_failure_message("a proposal"))

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

    def test_crisis_flutter_job_requires_crisispath_instead_of_launchcast(self):
        job = {
            "title": "Flutter crisis response app with wearable support",
            "description": (
                "Build our safety app for iOS and Android, add Apple Watch and Wear OS support, "
                "then publish through the App Store and Google Play."
            ),
            "skills": ["Flutter", "Dart", "iOS", "Android"],
            "matched": ["mobile app"],
        }
        wrong = (
            "Hi,\n\nLaunchcast proves my iOS release experience, including TestFlight and "
            "App Store delivery. I would extend that process to the Android app."
        )
        failures = notifier._proposal_hard_failures(draft(wrong), "individual", job)
        self.assertIn("crisis/safety mobile job must lead with CrisisPath proof", failures)
        self.assertIn("Flutter job is missing CrisisPath or BandMate proof", failures)

        wrong_order = (
            "Hi,\n\nLaunchcast proves my App Store work, while CrisisPath proves Flutter "
            "delivery across iOS and Android for a crisis-response product."
        )
        failures = notifier._proposal_hard_failures(draft(wrong_order), "individual", job)
        self.assertIn(
            "crisis/safety mobile job names another project before CrisisPath",
            failures,
        )
        self.assertIn("cross-platform job incorrectly leads with Launchcast", failures)

        relevant = (
            "Hi,\n\nCrisisPath is the closest match: a production crisis-response Flutter "
            "application I delivered on both iOS and Android. That work covered the existing "
            "mobile codebase, backend-connected flows, testing, and both store releases."
        )
        failures = notifier._proposal_hard_failures(draft(relevant), "individual", job)
        self.assertNotIn("crisis/safety mobile job must lead with CrisisPath proof", failures)
        self.assertNotIn("dual-store job is missing published iOS/Android CrisisPath proof", failures)

    def test_cross_platform_roles_reject_launchcast_only_proof(self):
        job = {
            "title": "React Native Expo developer",
            "description": "Complete a cross-platform iOS and Android marketplace app.",
            "skills": ["React Native", "Expo"],
            "matched": [],
        }
        wrong = "Hi,\n\nLaunchcast is my closest mobile example and is live on the App Store."
        failures = notifier._proposal_hard_failures(draft(wrong), "individual", job)
        self.assertIn("React Native/Expo job is missing cross-platform mobile proof", failures)
        self.assertIn("cross-platform job incorrectly uses Launchcast without Flutter proof", failures)

    def test_wearable_jobs_reject_unverified_past_delivery_claims(self):
        job = {
            "title": "Flutter wearable safety app",
            "description": "Add Apple Watch and Wear OS support to an iOS and Android app.",
            "skills": ["Flutter"],
            "matched": [],
        }
        cover = (
            "Hi,\n\nCrisisPath proves the crisis-response Flutter and dual-store work. "
            "I built and shipped an Apple Watch wearable app with the same workflow."
        )
        self.assertIn(
            "claims unverified prior wearable delivery",
            notifier._proposal_hard_failures(draft(cover), "individual", job),
        )

    def test_bandmate_publication_status_is_current_and_guarded(self):
        stale = (
            "Hi,\n\nBandMate is a Flutter voice platform whose mobile apps are in release "
            "preparation, not published."
        )
        inflated = "Hi,\n\nBandMate is now published on Google Play and supports production voice."
        self.assertIn(
            "uses stale BandMate mobile publication status",
            notifier._proposal_hard_failures(draft(stale)),
        )
        self.assertIn(
            "claims BandMate is already published on Google Play",
            notifier._proposal_hard_failures(draft(inflated)),
        )

    def test_job_specific_portfolio_directive_is_injected(self):
        job = {
            "title": "Emergency Flutter app",
            "description": "Ship an iOS and Android crisis app with wearable support.",
            "skills": ["Flutter", "Wear OS"],
            "matched": [],
            "score": 90,
            "job_type": "FIXED",
            "fixed": 2000,
        }
        prompt = notifier._fill_prompt(job, "{{PORTFOLIO_DIRECTIVE}}")
        self.assertIn("MANDATORY: lead with CrisisPath", prompt)
        self.assertIn("Do not lead with Launchcast", prompt)
        self.assertIn("Do not claim prior wearable delivery", prompt)

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

        pipeline = notifier._pipeline_buttons("~job123")["inline_keyboard"]
        pipeline_callbacks = [button["callback_data"] for row in pipeline for button in row]
        self.assertEqual(
            pipeline_callbacks,
            ["v:~job123", "r:~job123", "i:~job123", "w:~job123", "l:~job123"],
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
