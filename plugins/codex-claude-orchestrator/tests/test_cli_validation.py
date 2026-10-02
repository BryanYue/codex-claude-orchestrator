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

    def test_committing_crash_recovers_bound_report_without_overwrite_or_activation(self):
        job_dir, state, report = self.make_commit_fixture("recover000000000")
        state["phase"] = "committing"
        validation._atomic_json(job_dir / "job.json", state)
        validation._atomic_json(job_dir / "report.json", report)
        before = (job_dir / "report.json").read_bytes()
        recovered = validation.status(report["job_id"], environ=self.env)
        self.assertEqual(recovered["status"], "completed")
        self.assertEqual(recovered["outcome"], "pass")
        self.assertEqual((job_dir / "report.json").read_bytes(), before)
        self.assertEqual(self.tree(self.root / "versions"), {})
        self.assertEqual(recovered["activation"]["status"], "not_activated")

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

    def test_status_marks_a_lost_worker_inconclusive_without_touching_selection(self):
        job_id = "qualification-cccccccccccccccc"
        job_dir = self.root / "jobs" / job_id; job_dir.mkdir(parents=True)
        now = time.time()
        state = {"job_id": job_id, "identity_id": "candidate-id", "status": "running", "phase": "probe",
                 "nonce": "n", "groups": ["core", "read_only"], "activate_on_success": True,
                 "matrix": validation._matrix_default(), "created_at": now, "updated_at": now}
        validation._atomic_json(job_dir / "job.json", state)
        result = validation.status(job_id, environ=self.env)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["outcome"], "inconclusive")
        self.assertEqual(result["phase"], "worker_lost")
        self.assertEqual((self.root / "selection.json").read_bytes(), self.selection_bytes)
        report = validation._load_json(job_dir / "report.json")
        self.assertEqual(report["status"], "completed")
        self.assertEqual(report["matrix"], validation._matrix_default("worker_lost"))
        self.assertEqual(report["cleanup"]["status"], "confirmed")
        self.assertEqual(report["outcome_scope"], "core_read_only")
        self.assertEqual(report["requested_groups_outcome"], "inconclusive")
        self.assertEqual(report["missing_groups"], ["core", "read_only"])

    def test_orphan_bridge_stays_cancellable_and_nonterminal_until_cleanup(self):
        job_id = "qualification-orphan0000000000"
        job_dir = self.root / "jobs" / job_id; job_dir.mkdir(parents=True)
        run_dir = job_dir / "runs" / "git_positive"
        lifecycle = job_dir / "lifecycles" / "git_positive.json"
        external = job_dir / "cancel" / "git_positive.json"
        lifecycle.parent.mkdir(); run_dir.mkdir(parents=True)
        now = time.time()
        state = {"job_id": job_id, "identity_id": "candidate-id", "status": "running", "phase": "probe",
                 "nonce": "n", "groups": ["core", "read_only"], "activate_on_success": True,
                 "matrix": validation._matrix_default(), "created_at": now, "updated_at": now,
                 "active_scenario": {"scenario": "git_positive", "run_dir": str(run_dir),
                                     "external_cancel": str(external), "lifecycle_path": str(lifecycle),
                                     "bridge_pid": 999, "phase": "running", "run_id": "git_positive",
                                     "task_id": "qualification-review", "revision": 1,
                                     "cwd": str(job_dir / "fixtures" / "git"), "packet_sha256": "p" * 64,
                                     "cli_descriptor_sha256": "d" * 64}}
        validation._atomic_json(job_dir / "job.json", state)
        binding = {key: state["active_scenario"][key] for key in
                   ("run_id", "task_id", "revision", "cwd", "packet_sha256", "cli_descriptor_sha256")}
        validation._atomic_json(lifecycle, {**binding, "bridge_pid": 999, "terminal": False, "child_started": None})
        with patch.object(validation, "_presence", return_value="running"):
            observed = validation.status(job_id, environ=self.env)
            self.assertEqual(observed["status"], "needs_cleanup")
            cancelled = validation.cancel(job_id, environ=self.env)
            self.assertEqual(cancelled["status"], "cancel_requested")
            self.assertTrue(external.is_file())
            self.assertTrue((run_dir / "cancel.json").is_file())

        validation._atomic_json(lifecycle, {**binding, "bridge_pid": 999, "terminal": True,
                                             "status": "cancelled", "child_started": False})
        validation._atomic_json(run_dir / "receipt.json", {"status": "cancelled"})
        with patch.object(validation, "_presence", return_value="unconfirmed"):
            still_blocked = validation.status(job_id, environ=self.env)
        self.assertEqual(still_blocked["status"], "needs_cleanup")
        self.assertEqual(still_blocked["active_scenario"]["scenario"], "git_positive")
        self.assertNotIn("nonce", still_blocked["active_scenario"])
        with patch.object(validation, "_presence", return_value="stopped"):
            final = validation.status(job_id, environ=self.env)
        self.assertEqual(final["status"], "completed")
        self.assertEqual(final["outcome"], "inconclusive")
        report = validation._load_json(job_dir / "report.json")
        self.assertEqual(report["cleanup"]["status"], "confirmed")
        self.assertEqual(report["cleanup"]["active_scenario"]["scenario"], "git_positive")

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

    def test_held_worker_lock_keeps_the_record_active(self):
        job_dir, state, _ = self.make_commit_fixture()
        with (job_dir / "worker.lock").open("a+") as holder:
            fcntl.flock(holder.fileno(), fcntl.LOCK_EX)
            before = self.tree()
            result = validation.status(state["job_id"], self.env)
            self.assertEqual(result["status"], "running")
            self.assertEqual(self.tree(), before)
            self.assertTrue(validation.cancel(state["job_id"], self.env)["cancel_accepted"])

    def test_committing_without_bound_report_does_not_gain_pass_or_activate(self):
        job_dir, state, report = self.make_commit_fixture()
        state["phase"] = "committing"
        report["identity_sha256"] = "different"
        validation._atomic_json(job_dir / "job.json", state)
        validation._atomic_json(job_dir / "report.json", report)
        result = validation.status(state["job_id"], self.env)
        self.assertEqual(result["outcome"], "inconclusive")
        self.assertEqual(result["phase"], "worker_lost")
        self.assertEqual((self.root / "selection.json").read_bytes(), self.selection_bytes)

    def test_unconfirmed_child_cleanup_cannot_finish_even_with_stopped_bridge(self):
        job_dir, state, _ = self.make_commit_fixture()
        run_dir = job_dir / "runs" / "scenario"
        run_dir.mkdir(parents=True)
        lifecycle = job_dir / "lifecycle.json"
        binding = {"run_id": "scenario", "task_id": "probe", "revision": 1, "cwd": str(job_dir),
                   "packet_sha256": "p", "cli_descriptor_sha256": "d"}
        state["active_scenario"] = {**binding, "scenario": "scenario", "run_dir": str(run_dir),
                                    "lifecycle_path": str(lifecycle), "bridge_pid": 999}
        validation._atomic_json(job_dir / "job.json", state)
        validation._atomic_json(lifecycle, {**binding, "bridge_pid": 999, "terminal": True,
                                           "status": "cancelled", "child_started": True})
        validation._atomic_json(run_dir / "receipt.json", {"task_id": "probe", "revision": 1,
                                                           "status": "cancelled"})
        validation._atomic_json(run_dir / "child.json", {"process_group": 1000})
        with patch.object(validation, "_presence", side_effect=lambda pid, group=True: "unconfirmed" if group else "stopped"):
            result = validation.status(state["job_id"], self.env)
        self.assertEqual(result["status"], "needs_cleanup")
        self.assertEqual(result["active_scenario"]["scenario"], "scenario")
        self.assertFalse((job_dir / "report.json").exists())


if __name__ == "__main__":
    unittest.main()
