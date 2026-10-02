"""Shareable diagnostics must not expose task-scoped provider identifiers."""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import cli_validation
import diagnostics


class ReadOnlyValidationHistoryTests(unittest.TestCase):
    JOB_ID = "qualification-abcdefghijklmnop"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cli_root = Path(self.tmp.name) / "historical-cli"
        self.job = self.cli_root / "jobs" / self.JOB_ID
        self.job.mkdir(parents=True)
        (self.cli_root / "selection.json").write_text('{"mode":"explicit","active":"retained-identity","generation":3}\n')
        (self.cli_root / "update-policy.json").write_text('{"mode":"manual"}\n')

    def tearDown(self):
        self.tmp.cleanup()

    def record(self, **fields):
        now = time.time()
        state = {"job_id": self.JOB_ID, "identity_id": "fixture-identity", "status": "running",
                 "phase": "testing", "groups": ["core"], "activate_on_success": True,
                 "selection_generation": 3, "contract_id": "fixture-contract",
                 "created_at": now, "updated_at": now, "nonce": "fixture-nonce"}
        state.update(fields)
        (self.job / "job.json").write_text(json.dumps(state))
        return state

    def tree(self):
        entries = {}
        for path in sorted(self.cli_root.rglob("*")):
            key = str(path.relative_to(self.cli_root))
            entries[key] = "dir" if path.is_dir() else hashlib.sha256(path.read_bytes()).hexdigest()
        return entries

    def diagnose(self):
        with mock.patch.dict(os.environ, {"CLAUDE_ORCHESTRATOR_CLI_ROOT": str(self.cli_root)}):
            return diagnostics._validation_status(self.JOB_ID)

    def test_nonterminal_history_is_reported_without_locks_reconciliation_or_writes(self):
        for status, phase in (("running", "testing"), ("cancel_requested", "cancelling"),
                              ("running", "committing")):
            with self.subTest(status=status, phase=phase):
                self.record(status=status, phase=phase)
                if phase == "committing":
                    report = {"schema_version": 1, "status": "completed", "phase": "completed",
                              "job_id": self.JOB_ID, "identity_id": "fixture-identity",
                              "bridge_contract_id": "fixture-contract",
                              "matrix": {name: {"status": "pass"} for name in cli_validation.GROUPS}}
                    (self.job / "report.json").write_text(json.dumps(report))
                before = self.tree()
                result = self.diagnose()
                self.assertEqual(self.tree(), before)
                self.assertEqual((result["status"], result["phase"]), (status, phase))
                self.assertIs(result["read_only"], True)
                self.assertEqual(result["reconciliation"], "not_performed")
                self.assertEqual(result["worker_lock"], "absent")
                for name in ("state.lock", "events.lock", "worker.lock", "events.jsonl"):
                    self.assertFalse((self.job / name).exists(), name)

    def test_existing_worker_lock_is_observed_through_a_read_only_descriptor(self):
        self.record()
        lock = self.job / "worker.lock"
        lock.write_bytes(b"")
        before = self.tree()
        self.assertEqual(self.diagnose()["worker_lock"], "free")
        with lock.open("a+") as holder:
            fcntl.flock(holder.fileno(), fcntl.LOCK_EX)
            self.assertEqual(self.diagnose()["worker_lock"], "held")
        self.assertEqual(self.tree(), before)

    def test_terminal_and_active_history_reads_never_reconcile(self):
        self.record(status="completed", phase="completed", outcome="inconclusive")
        before = self.tree()
        result = self.diagnose()
        self.assertEqual(self.tree(), before)
        self.assertEqual(result["status"], "completed")
        self.assertNotIn("reconciliation", result)

        self.record()
        before = self.tree()
        legacy = cli_validation.read_status(self.JOB_ID, environ={"CLAUDE_ORCHESTRATOR_CLI_ROOT": str(self.cli_root)})
        self.assertEqual((legacy["status"], legacy["phase"]), ("running", "testing"))
        self.assertEqual(self.tree(), before)
        self.assertFalse((self.job / "report.json").exists())


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
