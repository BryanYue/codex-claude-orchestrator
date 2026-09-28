"""Shareable diagnostics must not expose task-scoped provider identifiers."""
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import diagnostics


class RecentProviderCallTests(unittest.TestCase):
    def test_shareable_recent_call_omits_raw_run_and_session_identifiers(self):
        report = diagnostics._recent_real_provider_call([{
            "run_id": "run-private-123",
            "session_id": "session-private-456",
            "updated_at": "2026-09-22T01:02:03Z",
            "actual_model": "provider-model",
            "actual_models": ["provider-model"],
            "actual_model_source": "assistant_message",
            "provider_response_observed": True,
            "status": "reported",
            "environment": {"cli_version": "2.1.279"},
        }])

        self.assertEqual(report["status"], "recorded")
        self.assertEqual(report["actual_model"], "provider-model")
        self.assertEqual(report["omitted_fields"], ["run_id", "session_id"])
        self.assertIn("corresponding run details", report["detail_note"])
        self.assertNotIn("run_id", report)
        self.assertNotIn("session_id", report)
        self.assertNotIn("run-private-123", str(report))
        self.assertNotIn("session-private-456", str(report))

    def test_absent_provider_session_does_not_claim_identifier_omission(self):
        report = diagnostics._recent_real_provider_call([])

        self.assertEqual(report["status"], "not_recorded")
        self.assertNotIn("omitted_fields", report)

    def test_init_only_and_legacy_model_never_claim_a_provider_response(self):
        for record in ({"session_id": "private-init", "initialized_model": "init-model"},
                       {"session_id": "private-legacy", "actual_model": "legacy-init-model"}):
            with self.subTest(record=record):
                report = diagnostics._recent_real_provider_call([record])
                self.assertEqual(report["status"], "unconfirmed")
                self.assertNotIn("actual_model", report)
                self.assertNotIn(record["session_id"], str(report))

    def test_later_initialization_does_not_hide_an_earlier_proven_response(self):
        report = diagnostics._recent_real_provider_call([
            {"session_id": "new-init-only", "updated_at": 2},
            {"session_id": "older-response", "updated_at": 1, "execution_evidence": "provider_event",
             "actual_models": ["provider-a", "provider-b"], "actual_model_source": "result_model_usage"},
        ])
        self.assertEqual(report["status"], "recorded")
        self.assertEqual(report["at"], 1)
        self.assertEqual(report["actual_models"], ["provider-a", "provider-b"])
        self.assertIsNone(report["actual_model"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
