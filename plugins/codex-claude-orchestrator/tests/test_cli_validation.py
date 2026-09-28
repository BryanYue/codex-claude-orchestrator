import fcntl
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import cli_validation as validation  # noqa: E402


class FakeStore:
    def __init__(self, root: Path):
        self.root = root
        self.identity_value = {
            "id": "candidate-id", "path": str(root / "versions/candidate-id/claude"),
            "version": "9.9.9", "sha256": "a" * 64, "platform": "darwin",
            "machine": "arm64", "source": "fixture", "created_at": 1.0,
        }
        self.selection = {"schema_version": 1, "generation": 7, "active": "old-id",
                          "previous": None, "history": ["old-id"], "mode": "managed"}
        self.recorded = []
        self.activated = []
        self.explicit_activated = []

    def store_root(self, environ=None):
        return self.root

    def identity(self, identity_id, environ=None):
        if identity_id != self.identity_value["id"]:
            raise ValueError("unknown identity")
        return dict(self.identity_value)

    def get_selection(self, environ=None):
        return dict(self.selection)

    def record_qualification(self, identity_id, contract_id, matrix, evidence_path, environ=None):
        self.recorded.append((identity_id, contract_id, matrix, evidence_path))
        return {"identity_id": identity_id, "bridge_contract_id": contract_id,
                "matrix": matrix, "evidence_path": evidence_path, "source": "local_qualification"}

    def activate(self, identity_id, expected_generation=None, environ=None):
        self.activated.append((identity_id, expected_generation))
        return {**self.selection, "generation": expected_generation + 1, "active": identity_id,
                "previous": self.selection["active"]}

    def activate_explicit(self, identity_id, expected_generation=None,
                          reason="manual_activation", environ=None):
        self.activated.append((identity_id, expected_generation))
        self.explicit_activated.append((identity_id, expected_generation, reason))
        return {**self.selection, "generation": expected_generation + 1, "active": identity_id,
                "previous": self.selection["active"],
                "update_policy": {"mode": "manual", "reason": reason}}


class ValidationTests(unittest.TestCase):
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

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "cli"
        self.root.mkdir()
        self.env = {**os.environ, "CLAUDE_ORCHESTRATOR_CLI_ROOT": str(self.root)}
        self.store = FakeStore(self.root)
        self.fixture_index = 0
        self.patches = [patch.object(validation, "_store", return_value=self.store),
                        patch.object(validation, "_contract_id", return_value="contract-1")]
        for item in self.patches:
            item.start(); self.addCleanup(item.stop)

    def tearDown(self):
        self.temp.cleanup()

    def make_descriptor_fixture(self):
        self.fixture_index += 1
        job_id = f"qualification-{self.fixture_index:016d}"
        job_dir = self.root / "jobs" / job_id
        cwd = job_dir / "fixtures" / "git"
        packets = job_dir / "packets"
        cwd.mkdir(parents=True); packets.mkdir()
        requirement = cwd / "SPEC.md"; requirement.write_text("fixture\n")
        protected = cwd / "protected.txt"; protected.write_text("protected\n")
        packet = {"task_id": "probe", "revision": 1, "role": "review", "cwd": str(cwd),
                  "objective": "probe", "requirement_sources": [str(requirement)],
                  "constraints": ["private"], "acceptance": ["evidence"], "owned_files": [],
                  "protected_files": ["protected.txt"], "model": "sonnet", "effort": "low"}
        packet_path = packets / "git_positive.json"
        validation._atomic_json(packet_path, packet)
        packet_sha = validation._sha256(packet_path)
        state = {"schema_version": 1, "job_id": job_id, "nonce": "nonce-value",
                 "identity_id": "candidate-id", "identity_sha256": "a" * 64,
                 "contract_id": "contract-1", "suite_revision": validation.SUITE_REVISION,
                 "status": "running", "scenarios": {"git_positive": {
                     "group": "core", "packet_sha256": packet_sha, "packet_path": str(packet_path),
                     "cwd": str(cwd.resolve()), "resume": False,
                     "required_groups": ["core", "read_only"]}}}
        validation._atomic_json(job_dir / "job.json", state)
        descriptor = {"schema_version": 1, "purpose": "qualification", "job_id": job_id,
                      "job_dir": str(job_dir), "nonce": "nonce-value", "identity_id": "candidate-id",
                      "identity_sha256": "a" * 64, "contract_id": "contract-1",
                      "suite_revision": validation.SUITE_REVISION, "scenario": "git_positive",
                      "packet_path": str(packet_path), "packet_sha256": packet_sha,
                      "fixture_root": str(job_dir / "fixtures")}
        return job_dir, packet, packet_path, descriptor

    def make_commit_fixture(self, suffix="commit0000000000"):
        job_id = f"qualification-{suffix}"
        job_dir = self.root / "jobs" / job_id; job_dir.mkdir(parents=True)
        now = time.time()
        matrix = validation._matrix_default()
        matrix["core"] = {"status": "pass", "reason": "core passed", "evidence": ["core"]}
        matrix["read_only"] = {"status": "pass", "reason": "read passed", "evidence": ["read"]}
        state = {"schema_version": 1, "job_id": job_id, "nonce": "nonce", "identity_id": "candidate-id",
                 "identity_sha256": "a" * 64, "identity_path": self.store.identity_value["path"],
                 "contract_id": "contract-1", "suite_revision": validation.SUITE_REVISION,
                 "model": "sonnet", "groups": ["core", "read_only"], "activate_on_success": True,
                 "selection_generation": 7, "status": "running", "phase": "scenario_complete:last",
                 "outcome": None, "matrix": validation._matrix_default(), "scenarios": {},
                 "active_scenario": None, "created_at": now, "updated_at": now, "deadline_at": now + 60}
        report = {"schema_version": 1, "status": "completed", "job_id": job_id,
                  "identity_id": "candidate-id", "identity_sha256": "a" * 64,
                  "contract_id": "contract-1", "bridge_contract_id": "contract-1",
                  "suite_revision": validation.SUITE_REVISION, "groups": ["core", "read_only"],
                  "matrix": matrix, "outcome": "pass", "scenarios": {}, "finished_at": now}
        validation._atomic_json(job_dir / "job.json", state)
        return job_dir, state, report

    def test_descriptor_is_job_packet_and_fixture_bound(self):
        _, packet, _, descriptor = self.make_descriptor_fixture()
        with patch.dict(os.environ, self.env, clear=True):
            profile = validation.validate_probe_descriptor(descriptor, packet)
        self.assertTrue(profile["tested"])
        self.assertEqual(profile["identity_id"], "candidate-id")
        self.assertEqual(profile["groups"], ["core", "read_only"])
        self.assertEqual(profile["capabilities"], {})

        for field, value in (("purpose", "dispatch"), ("nonce", "wrong"),
                             ("identity_sha256", "b" * 64), ("contract_id", "old-contract")):
            changed = {**descriptor, field: value}
            with self.subTest(field=field), patch.dict(os.environ, self.env, clear=True), self.assertRaises((ValueError, RuntimeError)):
                validation.validate_probe_descriptor(changed, packet)

    def test_descriptor_rejects_mutated_packet_and_symlink_escape(self):
        job_dir, packet, packet_path, descriptor = self.make_descriptor_fixture()
        packet_path.write_text(packet_path.read_text() + " ")
        with patch.dict(os.environ, self.env, clear=True), self.assertRaisesRegex(ValueError, "bytes changed"):
            validation.validate_probe_descriptor(descriptor, packet)

        job_dir, packet, packet_path, descriptor = self.make_descriptor_fixture()
        outside = self.root / "business"; outside.mkdir(); (outside / "SPEC.md").write_text("business\n")
        link = job_dir / "fixtures" / "business-link"; link.symlink_to(outside, target_is_directory=True)
        packet["cwd"] = str(link); packet["requirement_sources"] = [str(link / "SPEC.md")]
        validation._atomic_json(packet_path, packet)
        digest = validation._sha256(packet_path)
        descriptor["packet_sha256"] = digest
        state = validation._load_json(job_dir / "job.json")
        state["scenarios"]["git_positive"].update(packet_sha256=digest, cwd=str(outside.resolve()))
        validation._atomic_json(job_dir / "job.json", state)
        with patch.dict(os.environ, self.env, clear=True), self.assertRaisesRegex(ValueError, "symlink"):
            validation.validate_probe_descriptor(descriptor, packet)

    def test_descriptor_uses_registered_resume_requirements_and_exact_packet(self):
        job_dir, packet, _, descriptor = self.make_descriptor_fixture()
        state = validation._load_json(job_dir / "job.json")
        state["scenarios"]["git_positive"].update(resume=True,
                                                        required_groups=["core", "read_only", "resume"])
        validation._atomic_json(job_dir / "job.json", state)
        with patch.dict(os.environ, self.env, clear=True):
            profile = validation.validate_probe_descriptor(descriptor, packet)
        self.assertEqual(profile["groups"], ["core", "read_only", "resume"])
        with patch.dict(os.environ, self.env, clear=True), self.assertRaisesRegex(ValueError, "registered bytes"):
            validation.validate_probe_descriptor(descriptor, {**packet, "unregistered": True})

    def test_scenario_binds_descriptor_digest_and_external_lifecycle(self):
        job_id = "qualification-eeeeeeeeeeeeeeee"
        job_dir = self.root / "jobs" / job_id
        cwd = job_dir / "fixtures" / "git"; cwd.mkdir(parents=True)
        (cwd / "SPEC.md").write_text("fixture\n")
        (cwd / "protected.txt").write_text("protected\n")
        now = time.time()
        state = {"schema_version": 1, "job_id": job_id, "nonce": "nonce", "identity_id": "candidate-id",
                 "identity_sha256": "a" * 64, "contract_id": "contract-1",
                 "suite_revision": validation.SUITE_REVISION, "status": "running", "model": "sonnet",
                 "scenarios": {}, "deadline_at": now + 60, "updated_at": now}
        validation._atomic_json(job_dir / "job.json", state)
        packet = validation._base_packet(state, "digest-probe", cwd)
        proc = Mock(pid=4321, returncode=0); proc.poll.return_value = 0
        with patch.object(validation.subprocess, "Popen", return_value=proc) as popen:
            with self.assertRaisesRegex(RuntimeError, "cleanup is unconfirmed"):
                validation._run_scenario(job_dir, state, "digest_probe", "core", packet)
        command = popen.call_args.args[0]
        descriptor = Path(command[command.index("--cli-descriptor") + 1])
        self.assertEqual(command[command.index("--cli-descriptor-sha256") + 1], validation._sha256(descriptor))
        self.assertEqual(popen.call_args.kwargs["env"]["CODEX_BRIDGE_LIFECYCLE_FILE"],
                         str(job_dir / "lifecycles" / "digest_probe.json"))
        active = validation._load_json(job_dir / "job.json")["active_scenario"]
        self.assertEqual(active["cli_descriptor_sha256"], validation._sha256(descriptor))
        events = validation._events(job_dir)
        self.assertEqual([event["phase"] for event in events], ["scenario_started", "scenario_finished"])
        self.assertEqual(events[-1]["cleanup_status"], "unconfirmed")

    def test_scenario_creates_runs_parent_before_real_subprocess(self):
        job_dir, state, _ = self.make_commit_fixture("subprocess0000000")
        cwd = job_dir / "fixtures" / "git"; cwd.mkdir(parents=True)
        (cwd / "SPEC.md").write_text("fixture\n")
        (cwd / "protected.txt").write_text("protected\n")
        packet = validation._base_packet(state, "path-probe", cwd)
        fake_bridge = job_dir / "fake_bridge.py"
        fake_bridge.write_text(
            "import json,os,sys\n"
            "from pathlib import Path\n"
            "a=sys.argv; run=Path(a[a.index('--run-dir')+1]); packet=json.load(open(a[a.index('--packet')+1]))\n"
            "run.mkdir()\n"
            "json.dump({'status':'reported','task_id':packet['task_id'],'revision':packet['revision']},open(run/'receipt.json','w'))\n"
            "json.dump({'provider_subtype':'success'},open(run/'result.json','w'))\n",
            encoding="utf-8")
        with patch.object(validation, "_bridge_path", return_value=fake_bridge):
            result = validation._run_scenario(job_dir, state, "real_subprocess", "core", packet, timeout=5)
        self.assertEqual(result["returncode"], 0)
        self.assertEqual(result["receipt"]["status"], "reported")
        self.assertTrue((job_dir / "runs" / "real_subprocess").is_dir())

    def test_bound_predispatch_failure_is_cleanup_confirmed_scenario_evidence(self):
        job_dir, state, _ = self.make_commit_fixture("predispatch000000")
        cwd = job_dir / "fixtures" / "git"; cwd.mkdir(parents=True)
        (cwd / "SPEC.md").write_text("fixture\n")
        (cwd / "protected.txt").write_text("protected\n")
        packet = validation._base_packet(state, "predispatch-probe", cwd)
        fake_bridge = job_dir / "fake_predispatch.py"
        fake_bridge.write_text(
            "import hashlib,json,os,sys\n"
            "from pathlib import Path\n"
            "a=sys.argv; pp=Path(a[a.index('--packet')+1]); packet=json.load(open(pp)); run=Path(a[a.index('--run-dir')+1])\n"
            "descriptor=Path(a[a.index('--cli-descriptor')+1]); life=Path(os.environ['CODEX_BRIDGE_LIFECYCLE_FILE'])\n"
            "value={'schema_version':1,'run_id':run.name,'task_id':packet['task_id'],'revision':packet['revision'],"
            "'cwd':str(Path(packet['cwd']).resolve()),'packet_sha256':hashlib.sha256(pp.read_bytes()).hexdigest(),"
            "'cli_descriptor_sha256':hashlib.sha256(descriptor.read_bytes()).hexdigest(),'bridge_pid':os.getpid(),"
            "'phase':'pre_dispatch','status':'failed','child_started':False,'terminal':True}\n"
            "life.write_text(json.dumps(value)); raise SystemExit(2)\n",
            encoding="utf-8")
        with patch.object(validation, "_bridge_path", return_value=fake_bridge):
            result = validation._run_scenario(job_dir, state, "predispatch_probe", "core", packet, timeout=5)
        self.assertEqual(result["returncode"], 2)
        self.assertEqual(result["receipt"], {})
        self.assertEqual(result["lifecycle"]["status"], "failed")
        self.assertEqual(result["cleanup_status"], "confirmed")
        self.assertIsNone(validation._load_json(job_dir / "job.json")["active_scenario"])

    def test_prepared_workflow_literals_are_inventory_safe_and_hash_bound(self):
        job_id = "qualification-workflowfixture0"
        job_dir = self.root / "jobs" / job_id; job_dir.mkdir(parents=True)
        fixtures = validation._prepare_fixtures(job_dir)
        validation._compatibility()  # installs the skill scripts import root
        import named_workflow
        found = {entry["name"]: entry for entry in named_workflow.inventory(fixtures["git"])}
        self.assertTrue({"qualification-workflow", "qualification-workflow-denial"}.issubset(found))
        for name, path in (("qualification-workflow", fixtures["workflow"]),
                           ("qualification-workflow-denial", fixtures["workflow_denial"])):
            expected = {"name": name, "path": str(path.resolve()), "sha256": validation._sha256(path)}
            self.assertEqual(named_workflow.validate(expected, fixtures["git"]), expected)
        import bridge
        state = {"model": "sonnet"}
        review = validation._base_packet(state, "review-fixture", fixtures["git"])
        implement = validation._base_packet(state, "write-fixture", fixtures["git"], role="implement")
        implement["owned_files"] = ["owned.txt"]
        artifact = {"task_id": "artifact-fixture", "revision": 1, "role": "review",
                    "workspace_kind": "artifacts", "cwd": str(fixtures["artifacts"]),
                    "objective": "Read the declared brief.", "input_files": ["brief.md"],
                    "requirement_sources": [str(fixtures["artifacts"] / "requirements.md")],
                    "constraints": ["declared files only"], "acceptance": ["structured evidence"],
                    "owned_files": [], "protected_files": [], "model": "sonnet", "effort": "low"}
        workflow = validation._base_packet(state, "workflow-fixture", fixtures["git"], role="workflow_review")
        workflow["workflow"] = {"name": "qualification-workflow", "path": str(fixtures["workflow"].resolve()),
                                "sha256": validation._sha256(fixtures["workflow"])}
        for packet in (review, implement, artifact, workflow):
            self.assertEqual(bridge.validate_packet(packet)["task_id"], packet["task_id"])

    def test_fresh_resume_control_packet_is_self_contained_and_preflight_valid(self):
        job_id = "qualification-freshfixture0000"
        job_dir = self.root / "jobs" / job_id; job_dir.mkdir(parents=True)
        fixtures = validation._prepare_fixtures(job_dir)
        validation._compatibility()
        import bridge
        packet = validation._fresh_review_packet({"model": "sonnet"}, fixtures["git"])
        self.assertIn("SPEC.md", packet["objective"])
        self.assertIn("review.txt", packet["objective"])
        self.assertIn("QUALIFICATION_REVIEW_SENTINEL", packet["objective"])
        normalized = bridge.validate_packet(packet)
        before, requirements = bridge.preflight(normalized, None)
        self.assertEqual(normalized["task_id"], "qualification-fresh")
        self.assertEqual(before["head"], subprocess.run(
            ["git", "-C", str(fixtures["git"]), "rev-parse", "HEAD"], check=True,
            capture_output=True, text=True).stdout.strip())
        self.assertEqual(set(requirements), {str((fixtures["git"] / "SPEC.md").resolve())})

    def test_cancel_and_commit_are_linearized_by_state_lock(self):
        job_dir, _, report = self.make_commit_fixture("linear0000000000")
        entered = threading.Event(); release = threading.Event(); errors = []
        original = self.store.record_qualification

        def blocking_record(*args, **kwargs):
            entered.set()
            if not release.wait(2):
                raise RuntimeError("test release timeout")
            return original(*args, **kwargs)

        def finish():
            try:
                validation._finalize_success(job_dir, report)
            except BaseException as exc:  # surfaced in the test thread
                errors.append(exc)

        with patch.object(self.store, "record_qualification", side_effect=blocking_record):
            commit_thread = threading.Thread(target=finish)
            commit_thread.start(); self.assertTrue(entered.wait(1))
            cancel_result = {}
            cancel_thread = threading.Thread(target=lambda: cancel_result.update(validation.cancel(
                report["job_id"], environ=self.env)))
            cancel_thread.start()
            release.set(); commit_thread.join(2); cancel_thread.join(2)
        self.assertEqual(errors, [])
        self.assertFalse(cancel_result["cancel_accepted"])
        self.assertIn("already committing or terminal", cancel_result["note"])
        self.assertFalse((job_dir / "cancel.request").exists())
        self.assertEqual(validation._load_json(job_dir / "job.json")["status"], "completed")

    def test_cancel_before_commit_prevents_receipt_and_activation(self):
        job_dir, _, report = self.make_commit_fixture("cancel0000000000")
        cancelled = validation.cancel(report["job_id"], environ=self.env)
        self.assertTrue(cancelled["cancel_accepted"])
        with self.assertRaises(InterruptedError):
            validation._finalize_success(job_dir, report)
        self.assertEqual(self.store.recorded, [])
        self.assertEqual(self.store.activated, [])

    def test_committing_crash_recovers_bound_report_without_overwrite_or_activation(self):
        job_dir, state, report = self.make_commit_fixture("recover000000000")
        state["phase"] = "committing"
        validation._atomic_json(job_dir / "job.json", state)
        validation._atomic_json(job_dir / "report.json", report)
        before = validation._sha256(job_dir / "report.json")
        recovered = validation.status(report["job_id"], environ=self.env)
        self.assertEqual(recovered["status"], "completed")
        self.assertEqual(recovered["outcome"], "pass")
        self.assertEqual(validation._sha256(job_dir / "report.json"), before)
        self.assertEqual(len(self.store.recorded), 1)
        self.assertEqual(self.store.activated, [])
        self.assertEqual(recovered["activation"]["status"], "not_activated")

    def test_denial_evidence_must_name_the_requested_target(self):
        result = {"permission_denials": [{"tool_name": "Write", "tool_input": {"file_path": "/tmp/other.txt"}}]}
        self.assertFalse(validation._denial_for(result, {"Write", "Edit"}, "/private/fixture/protected.txt"))
        result["permission_denials"][0]["tool_input"]["file_path"] = "/private/fixture/protected.txt"
        self.assertTrue(validation._denial_for(result, {"Write", "Edit"}, "/private/fixture/protected.txt"))

    def test_resume_uses_bridge_summary_sha_and_requires_complete_identity_for_mismatch(self):
        state = {"identity_id": "candidate-id", "identity_sha256": "a" * 64, "contract_id": "contract-1"}
        def scenario(session, sha="a" * 64):
            identity = {"identity_id": "candidate-id", "contract_id": "contract-1"}
            if sha is not None:
                identity["sha256"] = sha
            return {"receipt": {"status": "reported"}, "result": {
                "actual_session_id": session, "cli_identity": identity}}
        real_shape = {"git_positive": scenario("session-1"), "resume": scenario("session-1"),
                      "fresh": scenario("session-2")}
        self.assertEqual(validation._probe_identity(real_shape["git_positive"]),
                         ("candidate-id", "a" * 64, "contract-1"))
        self.assertEqual(validation._assess_resume(state, real_shape)["status"], "pass")
        different = {**real_shape, "fresh": scenario("session-2", "b" * 64)}
        self.assertEqual(validation._assess_resume(state, different)["status"], "fail")
        missing = {**real_shape, "fresh": scenario("session-2", None)}
        self.assertEqual(validation._assess_resume(state, missing)["status"], "inconclusive")

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
        self.assertEqual(self.store.selection["active"], "old-id")
        self.assertEqual(self.store.activated, [])
        report = validation._load_json(job_dir / "report.json")
        self.assertEqual(report["status"], "completed")
        self.assertEqual(report["matrix"], validation._matrix_default("worker_lost"))
        self.assertEqual(report["cleanup"]["status"], "confirmed")
        self.assertEqual(report["outcome_scope"], "core_read_only")
        self.assertEqual(report["requested_groups_outcome"], "inconclusive")
        self.assertEqual(report["missing_groups"], ["core", "read_only"])

    def test_orphan_bridge_stays_cancellable_and_blocks_a_new_job_until_cleanup(self):
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
            with self.assertRaisesRegex(RuntimeError, "cleanup is incomplete"):
                validation.start("candidate-id", groups=["core", "read_only"], environ=self.env)

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

    def test_start_holds_persistent_global_and_worker_locks_for_child(self):
        spawned = Mock(pid=2468)
        with patch.object(validation.subprocess, "Popen", return_value=spawned) as popen:
            result = validation.start("candidate-id", groups=["core", "read_only"], environ=self.env)
        self.assertEqual(result["status"], "running")
        call = popen.call_args
        self.assertEqual(call.args[0][1:3], [str(Path(validation.__file__).resolve()), "_worker"])
        self.assertEqual(len(call.kwargs["pass_fds"]), 2)
        self.assertEqual(call.kwargs["env"]["CLAUDE_ORCHESTRATOR_CLI_ROOT"], str(self.root))
        job = validation._load_json(Path(result["report_path"]).parent / "job.json")
        self.assertEqual(job["worker_pid"], 2468)
        self.assertEqual(job["selection_generation"], 7)

    def test_post_popen_state_failure_is_ambiguous_and_not_safe_to_retry(self):
        spawned = Mock(pid=2468)
        with patch.object(validation.subprocess, "Popen", return_value=spawned), \
             patch.object(validation, "_with_state", side_effect=OSError("state write failed")), \
             self.assertRaises(validation.QualificationLaunchAmbiguous) as raised:
            validation.start("candidate-id", groups=["core", "read_only"], environ=self.env)
        self.assertTrue(raised.exception.job_id.startswith("qualification-"))
        job = validation._load_json(self.root / "jobs" / raised.exception.job_id / "job.json")
        self.assertEqual(job["status"], "queued")

    def test_single_global_job_lock_rejects_parallel_start(self):
        lock = (self.root / "validation.lock").open("a+")
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.addCleanup(lock.close)
        with self.assertRaisesRegex(RuntimeError, "already running"):
            validation.start("candidate-id", groups=["core"], activate_on_success=False, environ=self.env)

    def test_partial_optional_inconclusive_keeps_core_pass_and_can_activate(self):
        job_id = "qualification-dddddddddddddddd"
        job_dir = self.root / "jobs" / job_id; job_dir.mkdir(parents=True)
        global_lock = (self.root / "validation.lock").open("a+")
        worker_lock = (job_dir / "worker.lock").open("a+")
        fcntl.flock(global_lock.fileno(), fcntl.LOCK_EX); fcntl.flock(worker_lock.fileno(), fcntl.LOCK_EX)
        global_fd, worker_fd = os.dup(global_lock.fileno()), os.dup(worker_lock.fileno())
        now = time.time()
        state = {"schema_version": 1, "job_id": job_id, "nonce": "nonce", "identity_id": "candidate-id",
                 "identity_sha256": "a" * 64, "identity_path": self.store.identity_value["path"],
                 "contract_id": "contract-1", "suite_revision": validation.SUITE_REVISION,
                 "model": "sonnet", "groups": list(validation.GROUPS), "activate_on_success": True,
                 "selection_generation": 7, "status": "running", "phase": "preparing", "outcome": None,
                 "matrix": validation._matrix_default(), "scenarios": {}, "created_at": now, "updated_at": now,
                 "deadline_at": now + 60, "lock_fds": {"global": global_fd, "worker": worker_fd}}
        validation._atomic_json(job_dir / "job.json", state)

        def fake_scenario(_job_dir, _state, scenario, group, packet, **_kwargs):
            base = {"scenario": scenario, "group": group, "returncode": 0, "run_dir": str(job_dir / "runs" / scenario),
                    "receipt": {"status": "reported"}, "result": {"provider_subtype": "success",
                    "actual_session_id": "session-1", "system_init_session_id": "session-1"},
                    "child": {"process_group": 999}, "child_state": "stopped", "initialized": True,
                    "cancel_sent": scenario == "provider_init_cancel", "timed_out": False}
            if scenario == "provider_init_cancel":
                base["receipt"] = {"status": "cancelled"}
            if scenario in {"read_denial", "protected_write"}:
                tool = "Read" if scenario == "read_denial" else "Write"
                target = str(Path(packet["cwd"]) / ("canary.txt" if scenario == "read_denial" else "protected.txt"))
                base["receipt"] = {"status": "failed"}
                base["result"]["permission_denials"] = [{"tool_name": tool, "tool_input": {"file_path": target}}]
            if scenario == "owned_write":
                Path(packet["cwd"]).joinpath("owned.txt").write_text("after\n")
            if scenario == "fresh":
                base["result"].update(actual_session_id="session-2", system_init_session_id="session-2")
            base["result"]["cli_identity"] = {"identity_id": "candidate-id", "sha256": "a" * 64,
                                                   "contract_id": "contract-1"}
            if scenario.startswith("workflow_"):
                base["result"].update(workflow_name=packet["workflow"]["name"],
                                      workflow_completion_observed=True, workflow_tool_use_observed=True)
                # No actual child denial: workflow is inconclusive, not a false pass.
            return base

        try:
            with patch.object(validation, "_run_scenario", side_effect=fake_scenario):
                code = validation._worker(job_id)
        finally:
            global_lock.close(); worker_lock.close()
        self.assertEqual(code, 0)
        final = validation._load_json(job_dir / "job.json")
        self.assertEqual(final["outcome"], "pass")
        self.assertEqual(final["matrix"]["core"]["status"], "pass")
        self.assertEqual(final["matrix"]["read_only"]["status"], "pass")
        self.assertEqual(final["matrix"]["workflow"]["status"], "inconclusive")
        self.assertEqual(self.store.activated, [("candidate-id", 7)])
        self.assertEqual(self.store.explicit_activated,
                         [("candidate-id", 7, "manual_validation_activation")])
        self.assertEqual(len(self.store.recorded), 1)
        self.assertTrue(Path(self.store.recorded[0][3]).is_file())

    def test_worker_error_publishes_inconclusive_report_without_qualification(self):
        job_id = "qualification-ffffffffffffffff"
        job_dir = self.root / "jobs" / job_id; job_dir.mkdir(parents=True)
        global_lock = (self.root / "validation.lock").open("a+")
        worker_lock = (job_dir / "worker.lock").open("a+")
        fcntl.flock(global_lock.fileno(), fcntl.LOCK_EX); fcntl.flock(worker_lock.fileno(), fcntl.LOCK_EX)
        global_fd, worker_fd = os.dup(global_lock.fileno()), os.dup(worker_lock.fileno())
        now = time.time()
        state = {"schema_version": 1, "job_id": job_id, "nonce": "nonce", "identity_id": "candidate-id",
                 "identity_sha256": "a" * 64, "identity_path": self.store.identity_value["path"],
                 "contract_id": "contract-1", "suite_revision": validation.SUITE_REVISION,
                 "model": "sonnet", "groups": ["core", "read_only"], "activate_on_success": True,
                 "selection_generation": 7, "status": "running", "phase": "preparing", "outcome": None,
                 "matrix": validation._matrix_default(), "scenarios": {}, "active_scenario": None,
                 "created_at": now, "updated_at": now, "deadline_at": now + 60,
                 "lock_fds": {"global": global_fd, "worker": worker_fd}}
        validation._atomic_json(job_dir / "job.json", state)
        try:
            with patch.object(validation, "_prepare_fixtures", side_effect=OSError("fixture unavailable")):
                code = validation._worker(job_id)
        finally:
            global_lock.close(); worker_lock.close()
        self.assertEqual(code, 1)
        final = validation._load_json(job_dir / "job.json")
        self.assertEqual(final["status"], "completed")
        self.assertEqual(final["phase"], "worker_error")
        report = validation._load_json(job_dir / "report.json")
        self.assertEqual(report["status"], "completed")
        self.assertEqual(report["outcome"], "inconclusive")
        self.assertEqual(report["cleanup"]["status"], "confirmed")
        self.assertEqual(self.store.recorded, [])
        self.assertEqual(self.store.activated, [])

    def test_cleanup_requires_positive_child_stop_or_terminal_predispatch_receipt(self):
        self.assertFalse(validation._cleanup_confirmed({"x": {"child": {"process_group": 12}, "child_state": "unconfirmed"}}))
        self.assertFalse(validation._cleanup_confirmed({"x": {"child": {}, "timed_out": True, "receipt": {}}}))
        self.assertTrue(validation._cleanup_confirmed({"x": {"child": {}, "timed_out": False,
                                                               "receipt": {"status": "blocked"}}}))


if __name__ == "__main__":
    unittest.main(verbosity=2)
