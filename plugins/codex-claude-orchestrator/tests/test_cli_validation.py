"""Historical qualification remains readable and cancellable after worker retirement."""
import fcntl
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import cli_validation as validation

class ValidationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "cli"
        self.root.mkdir()
        self.env = {"CLAUDE_ORCHESTRATOR_CLI_ROOT": str(self.root)}
        self.selection_bytes = b'{"active":"old-id","mode":"managed"}\n'
        (self.root / "selection.json").write_bytes(self.selection_bytes)

    def tearDown(self):
        self.temp.cleanup()

    def tree(self, root=None):
        root = self.root if root is None else root
        return {str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*") if path.is_file()}

    def test_cancel_unknown_job_has_domain_error_without_creating_state(self):
        with self.assertRaisesRegex(ValueError, "Unknown qualification job"):
            validation.cancel("qualification-1234567890123456", self.env)
        self.assertFalse((self.root / "jobs").exists())

    def test_cancel_disappearing_job_has_domain_error(self):
        job_dir, *_ = self.make_commit_fixture()
        with patch.object(validation, "_with_state", side_effect=FileNotFoundError("removed after check")):
            with self.assertRaisesRegex(ValueError, "no longer available"):
                validation.cancel(job_dir.name, self.env)

    def test_partial_qualification_never_advertises_full_requested_coverage(self):
        matrix = {name: {"status": "pass"} for name in validation.GROUPS}
        matrix["workflow"] = {"status": "inconclusive"}
        result = validation.coverage(list(validation.GROUPS), matrix)
        self.assertEqual(result["outcome_scope"], "core_read_only")
        self.assertEqual(result["requested_groups_outcome"], "inconclusive")
        self.assertEqual(result["missing_groups"], ["workflow"])
        self.assertEqual(validation.coverage(["core", "read_only"], matrix)["requested_groups_outcome"], "pass")
        matrix["workflow"] = {"status": "fail"}
        self.assertEqual(validation.coverage(list(validation.GROUPS), matrix)["requested_groups_outcome"], "fail")

    def make_commit_fixture(self, suffix="commit0000000000"):
        job_id = f"qualification-{suffix}"
        job_dir = self.root / "jobs" / job_id; job_dir.mkdir(parents=True)
        now = time.time()
        matrix = validation._matrix_default()
        matrix["core"] = {"status": "pass", "reason": "core passed", "evidence": ["core"]}
        matrix["read_only"] = {"status": "pass", "reason": "read passed", "evidence": ["read"]}
        state = {"schema_version": 1, "job_id": job_id, "nonce": "nonce", "identity_id": "candidate-id",
                 "identity_sha256": "a" * 64, "identity_path": str(self.root / "versions/candidate-id/claude"),
                 "contract_id": "contract-1", "suite_revision": "qualification-v1",
                 "model": "sonnet", "groups": ["core", "read_only"], "activate_on_success": True,
                 "selection_generation": 7, "status": "running", "phase": "scenario_complete:last",
                 "outcome": None, "matrix": validation._matrix_default(), "scenarios": {},
                 "active_scenario": None, "created_at": now, "updated_at": now, "deadline_at": now + 60}
        report = {"schema_version": 1, "status": "completed", "job_id": job_id,
                  "identity_id": "candidate-id", "identity_sha256": "a" * 64,
                  "contract_id": "contract-1", "bridge_contract_id": "contract-1",
                  "suite_revision": "qualification-v1", "groups": ["core", "read_only"],
                  "matrix": matrix, "outcome": "pass", "scenarios": {}, "finished_at": now}
        validation._atomic_json(job_dir / "job.json", state)
        return job_dir, state, report

    def test_cancel_is_durable_and_never_signals_recorded_pid(self):
        job_id = "qualification-bbbbbbbbbbbbbbbb"
        job_dir = self.root / "jobs" / job_id; job_dir.mkdir(parents=True)
        now = time.time()
        state = {"job_id": job_id, "identity_id": "candidate-id", "status": "running", "phase": "probe",
                 "nonce": "n", "groups": ["core"], "activate_on_success": True,
                 "matrix": validation._matrix_default(), "created_at": now, "updated_at": now,
                 "worker_pid": 12345}
        validation._atomic_json(job_dir / "job.json", state)
        worker_lock = (job_dir / "worker.lock").open("a+")
        fcntl.flock(worker_lock.fileno(), fcntl.LOCK_EX)
        self.addCleanup(worker_lock.close)
        with patch.object(validation.os, "kill") as kill, patch.object(validation.os, "killpg") as killpg:
            result = validation.cancel(job_id, environ=self.env)
        self.assertEqual(result["status"], "cancel_requested")
        self.assertTrue((job_dir / "cancel.request").is_file())
        kill.assert_not_called(); killpg.assert_not_called()

    def test_read_status_preserves_nonterminal_history_and_every_file(self):
        job_dir, state, report = self.make_commit_fixture()
        state["phase"] = "committing"
        validation._atomic_json(job_dir / "job.json", state)
        validation._atomic_json(job_dir / "report.json", report)
        before = self.tree()
        result = validation.read_status(state["job_id"], self.env)
        self.assertEqual(result["status"], "running")
        self.assertEqual(result["phase"], "committing")
        self.assertEqual(result["worker_lock"], "absent")
        self.assertTrue(result["read_only"])
        self.assertEqual(self.tree(), before)

    def test_cancel_preserves_terminal_and_committing_records(self):
        for status, phase in (("completed", "completed"), ("cancelled", "cancelled"), ("running", "committing")):
            with self.subTest(status=status, phase=phase):
                job_dir, state, _ = self.make_commit_fixture(status + "000000000000")
                state.update(status=status, phase=phase)
                validation._atomic_json(job_dir / "job.json", state)
                before = (job_dir / "job.json").read_bytes()
                result = validation.cancel(state["job_id"], self.env)
                self.assertFalse(result["cancel_accepted"])
                self.assertIn("already committing or terminal", result["note"])
                self.assertEqual((job_dir / "job.json").read_bytes(), before)
                self.assertFalse((job_dir / "cancel.request").exists())
                self.assertFalse((job_dir / "events.jsonl").exists())

if __name__ == "__main__":
    unittest.main()
