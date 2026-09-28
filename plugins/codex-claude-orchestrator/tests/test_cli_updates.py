from __future__ import annotations

import fcntl
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import cli_store  # noqa: E402
import cli_updates  # noqa: E402
import cli_validation  # noqa: E402
import official_releases  # noqa: E402


class CliUpdatesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.store = self.base / "cli"
        self.env = {"HOME": str(self.base / "home"), "PATH": "/usr/bin:/bin",
                    "CLAUDE_ORCHESTRATOR_CLI_ROOT": str(self.store)}
        (self.base / "home").mkdir()
        self.native = patch.object(cli_store, "_native_check", return_value=None)
        self.native.start()
        self.contract = patch.object(cli_store, "_contract_id", return_value="bridge-contract-test")
        self.contract.start()

    def tearDown(self):
        self.contract.stop()
        self.native.stop()
        self.tmp.cleanup()

    def candidate(self, name: str, version: str) -> Path:
        path = self.base / name
        path.write_text(f"#!/bin/sh\nif [ \"$1\" = \"--version\" ]; then echo '{version} (Claude Code)'; exit 0; fi\nexit 2\n")
        path.chmod(0o755)
        return path

    def descriptor(self, name: str, version: str) -> dict:
        return cli_store.capture(str(self.candidate(name, version)), self.env)

    def target(self, descriptor: dict) -> dict:
        return {"version": descriptor["version"], "platform": "darwin-arm64",
                "sha256": descriptor["sha256"], "size": Path(descriptor["path"]).stat().st_size,
                "url": "https://example.invalid/claude", "source": "bundled",
                "scope": "installed_plugin_release"}

    def remote_target(self, descriptor: dict) -> dict:
        return {"version": descriptor["version"], "platform": "darwin-arm64",
                "sha256": descriptor["sha256"], "size": Path(descriptor["path"]).stat().st_size,
                "url": f"{cli_store.OFFICIAL_RELEASES}/{descriptor['version']}/darwin-arm64/claude",
                "source": "official", "scope": "official_latest",
                "manifest_url": f"{cli_store.OFFICIAL_RELEASES}/{descriptor['version']}/manifest.json",
                "manifest_sha256": "a" * 64,
                "signing_key_fingerprint": official_releases.RELEASE_KEY_FINGERPRINT,
                "discovered_at": time.time()}

    def activate(self, descriptor: dict) -> None:
        cli_store.activate(descriptor["id"], cli_store.get_selection(self.env)["generation"], self.env)

    def automatic_state(self) -> dict:
        policy = cli_store.set_update_policy(
            "automatic", "test_auto_qualification", self.env,
            channel="latest", auto_qualify=True)
        return {"auto_qualify": True,
                "selection_generation": cli_store.get_selection(self.env)["generation"],
                "policy_generation": policy["generation"]}

    def test_invalid_or_future_cache_time_cannot_suppress_latest_discovery(self):
        target = self.remote_target(self.descriptor("clock-candidate", "2.1.280"))
        for timestamp in (float("nan"), float("inf"), -1, time.time() + 86400, True):
            with self.subTest(timestamp=timestamp):
                cli_updates._atomic_json(cli_updates._latest_cache_path(self.env),
                                         {"schema_version": 1, "target": target, "checked_at": timestamp})
                cached = cli_updates._read_latest_cache(self.env)
                self.assertEqual(cached["checked_at"], 0)
                self.assertEqual(cached["target"], target)
                self.assertTrue(cli_updates._discovery_due({"channel": "latest"}, cached))

    def inline_worker(self, acquired: dict):
        class Process:
            pid = 4242

        def launch(command, **_kwargs):
            with patch.object(cli_store, "acquire_official_release", return_value=acquired):
                cli_updates._run_worker(command[3], self.env)
            return Process()
        return launch

    def test_default_current_target_is_quiet_and_environment_can_pause_network_work(self):
        current = self.descriptor("current", "2.1.278")
        self.activate(current)
        with patch.object(cli_store, "official_release_target", return_value=self.target(current)), \
             patch.object(cli_updates.subprocess, "Popen") as popen:
            summary = cli_updates.start(reason="startup", environ=self.env)
        self.assertEqual(summary["policy"], "automatic")
        self.assertEqual(summary["state"], "up_to_date")
        self.assertFalse(summary["notice_pending"])
        self.assertEqual(summary["channel"]["source"], "bundled")
        self.assertEqual(summary["channel"]["scope"], "installed_plugin_release")
        self.assertEqual(summary["channel"]["mode"], "bundled")
        popen.assert_not_called()

        manual_env = {**self.env, "CLAUDE_ORCHESTRATOR_UPDATE_POLICY": "manual"}
        with patch.object(cli_store, "official_release_target", return_value=self.target(current)), \
             patch.object(cli_updates.subprocess, "Popen") as popen:
            paused = cli_updates.start(reason="startup", environ=manual_env)
        self.assertEqual(paused["policy"], "manual")
        self.assertEqual(paused["policy_source"], "environment_override")
        popen.assert_not_called()

        cli_store.set_update_policy("manual", "manual_rollback", self.env)
        automatic_env = {**self.env, "CLAUDE_ORCHESTRATOR_UPDATE_POLICY": "automatic"}
        with patch.object(cli_store, "official_release_target", return_value=self.target(current)), \
             patch.object(cli_updates.subprocess, "Popen") as popen:
            held = cli_updates.start(reason="startup", environ=automatic_env)
        self.assertEqual(held["policy"], "manual")
        self.assertEqual(held["policy_source"], "persistent_manual_hold")
        self.assertEqual(held["reason"], "manual_rollback")
        popen.assert_not_called()

    def test_status_uses_lightweight_metadata_and_unqualified_higher_is_not_called_verified(self):
        current = self.descriptor("current", "2.1.278")
        self.activate(current)
        with patch.object(cli_store, "official_release_target", return_value=self.target(current)), \
             patch.object(cli_store, "identity", side_effect=AssertionError("full validation during status")), \
             patch.object(cli_store, "_sha256", side_effect=AssertionError("binary hash during status")), \
             patch.object(cli_store, "_native_check", side_effect=AssertionError("codesign during status")):
            summary = cli_updates.status(self.env)
        self.assertEqual(summary["state"], "up_to_date")
        self.assertTrue(summary["current_qualified"])

        higher = self.descriptor("higher", "9.9.999")
        selection = cli_store.get_selection(self.env)
        raw = {**selection, "generation": selection["generation"] + 1,
               "active": higher["id"], "previous": current["id"],
               "history": [current["id"], higher["id"]]}
        cli_store._atomic_json(self.store / "selection.json", raw)
        with patch.object(cli_store, "official_release_target", return_value=self.target(current)):
            unqualified = cli_updates.status(self.env)
        self.assertFalse(unqualified["current_qualified"])
        self.assertEqual(unqualified["state"], "available")
        self.assertNotIn("验证", unqualified["message"])
        self.assertEqual(unqualified["effective_dispatch_version"], "2.1.278")
        self.assertEqual(unqualified["effective_dispatch_reason"], "previous")
        self.assertEqual(unqualified["effective_dispatch_groups"], ["core", "read_only"])
        self.assertTrue(unqualified["active_unqualified_for_contract"])
        self.assertIn("基础只读任务的派单预览暂由 2.1.278", unqualified["dispatch_message"])

    def test_paid_launch_authorization_is_linearized_with_manual_policy(self):
        current = self.descriptor("authorization-current", "2.1.278")
        candidate = self.descriptor("authorization-candidate", "2.1.281")
        self.activate(current)
        policy = cli_store.set_update_policy(
            "automatic", "latest", self.env, channel="latest", auto_qualify=True)
        state = {"auto_qualify": True,
                 "selection_generation": cli_store.get_selection(self.env)["generation"],
                 "policy_generation": policy["generation"]}
        original = cli_store.qualification

        def pause_after_receipt(identity_id, contract_id, environ=None):
            receipt = original(identity_id, contract_id, environ)
            cli_store.set_update_policy("manual", "user_pause", self.env, auto_qualify=False)
            return receipt

        with patch.object(cli_store, "qualification", side_effect=pause_after_receipt), \
             patch.object(cli_validation, "start") as no_paid:
            action, proof = cli_updates._qualification_action(candidate, state, self.env)
        self.assertEqual(action, "superseded")
        self.assertEqual(proof["state"], "superseded")
        self.assertFalse(proof["attempt_final"])
        no_paid.assert_not_called()
        self.assertFalse(cli_updates._attempt_path(
            candidate["id"], "bridge-contract-test", self.env).exists())

    def test_unstarted_qualification_is_deferred_but_ambiguous_launch_is_final(self):
        current = self.descriptor("defer-current", "2.1.278")
        candidate = self.descriptor("defer-candidate", "2.1.281")
        self.activate(current)
        policy = cli_store.set_update_policy(
            "automatic", "latest", self.env, channel="latest", auto_qualify=True)
        state = {"auto_qualify": True,
                 "selection_generation": cli_store.get_selection(self.env)["generation"],
                 "policy_generation": policy["generation"]}
        lock = (self.store / "validation.lock").open("a+")
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            action, deferred = cli_updates._qualification_action(candidate, state, self.env)
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
            lock.close()
        self.assertEqual(action, "deferred")
        self.assertFalse(deferred["attempt_final"])

        with patch.object(cli_validation.subprocess, "Popen", return_value=type("P", (), {"pid": 42})()):
            action, pending = cli_updates._qualification_action(candidate, state, self.env)
        self.assertEqual(action, "pending")
        self.assertIn("job_id", pending)

        other = self.descriptor("ambiguous-candidate", "2.1.282")
        ambiguous_job = "qualification-ambiguous000000"
        with patch.object(cli_validation, "start", side_effect=
                          cli_validation.QualificationLaunchAmbiguous("receipt write failed", ambiguous_job)) as start:
            action, terminal = cli_updates._qualification_action(other, state, self.env)
        self.assertEqual(action, "terminal")
        self.assertTrue(terminal["attempt_final"])
        self.assertEqual(terminal["job_id"], ambiguous_job)
        with patch.object(cli_validation, "start") as no_retry:
            action, repeated = cli_updates._qualification_action(other, state, self.env)
        self.assertEqual(action, "terminal")
        no_retry.assert_not_called()
        start.assert_called_once()

    def test_committing_job_is_recovered_before_launch_authorization(self):
        candidate = self.descriptor("recovered-candidate", "2.1.281")
        state = self.automatic_state()
        job_id = "qualification-recoverlaunch0000"
        job_dir = self.store / "jobs" / job_id
        job_dir.mkdir(parents=True)
        matrix = {group: {"status": "pass", "reason": "fixture", "evidence": ["fixture"]}
                  for group in cli_validation.GROUPS}
        now = time.time()
        cli_validation._atomic_json(job_dir / "job.json", {
            "schema_version": 1, "job_id": job_id, "nonce": "fixture",
            "identity_id": candidate["id"], "identity_sha256": candidate["sha256"],
            "identity_path": candidate["path"], "contract_id": "bridge-contract-test",
            "suite_revision": cli_validation.SUITE_REVISION, "model": "sonnet",
            "groups": list(cli_validation.GROUPS), "activate_on_success": True,
            "selection_generation": state["selection_generation"], "status": "running",
            "phase": "committing", "outcome": None, "matrix": matrix, "scenarios": {},
            "active_scenario": None, "created_at": now, "updated_at": now,
            "deadline_at": now + 60,
        })
        cli_validation._atomic_json(job_dir / "report.json", {
            "schema_version": 1, "status": "completed", "job_id": job_id,
            "identity_id": candidate["id"], "identity_sha256": candidate["sha256"],
            "contract_id": "bridge-contract-test", "bridge_contract_id": "bridge-contract-test",
            "suite_revision": cli_validation.SUITE_REVISION,
            "groups": list(cli_validation.GROUPS), "matrix": matrix,
            "outcome": "pass", "scenarios": {}, "finished_at": now,
        })

        with patch.dict(__import__("os").environ, self.env, clear=True), \
             patch.object(cli_validation, "start") as no_paid:
            action, proof = cli_updates._qualification_action(candidate, state, self.env)

        self.assertEqual(action, "eligible")
        self.assertEqual(proof["state"], "qualified")
        self.assertEqual(cli_validation._load_json(job_dir / "job.json")["status"], "completed")
        self.assertFalse(cli_updates._attempt_path(
            candidate["id"], "bridge-contract-test", self.env).exists())
        no_paid.assert_not_called()

    def test_acknowledged_failure_notice_stays_acknowledged_when_cause_is_unchanged(self):
        base = {"schema_version": 1, "state": "acquiring", "target_version": "2.1.281",
                "target_sha256": "a" * 64,
                "progress": {"phase": "starting", "bytes_received": 0, "total_bytes": 10}}
        first = cli_updates._failure_state(base, "maintenance_failed", "GPG is required")
        acknowledged = {**first, "notice_acknowledged_at": 10.0}
        retry = {**base, "prior_notice": {
            "notice_id": acknowledged["notice_id"],
            "notice_fingerprint": acknowledged["notice_fingerprint"],
            "notice_acknowledged_at": acknowledged["notice_acknowledged_at"]}}
        same = cli_updates._failure_state(retry, "maintenance_failed", "GPG is required")
        changed = cli_updates._failure_state(retry, "maintenance_failed", "signature mismatch")
        self.assertEqual(same["notice_id"], first["notice_id"])
        self.assertEqual(same["notice_acknowledged_at"], 10.0)
        self.assertNotEqual(changed["notice_id"], first["notice_id"])
        self.assertIsNone(changed["notice_acknowledged_at"])

    def test_periodic_retry_preserves_ack_for_same_discovery_failure(self):
        current = self.descriptor("notice-current", "2.1.278")
        self.activate(current)
        cli_store.set_update_policy(
            "automatic", "latest", self.env, channel="latest", auto_qualify=True)

        class Process:
            pid = 4242
        def launch(command, **_kwargs):
            cli_updates._run_worker(command[3], self.env)
            return Process()

        with patch.object(cli_updates.subprocess, "Popen", side_effect=launch), \
             patch.object(official_releases, "discover_latest",
                          side_effect=RuntimeError("GPG is required")):
            first = cli_updates.start(reason="periodic", environ=self.env)
            acknowledged = cli_updates.acknowledge(first["notice_id"], self.env)
            with patch.object(cli_updates.time, "time", return_value=time.time() + 3601):
                second = cli_updates.start(reason="periodic", environ=self.env)
        self.assertFalse(acknowledged["notice_pending"])
        self.assertEqual(second["notice_id"], first["notice_id"])
        self.assertFalse(second["notice_pending"])
        self.assertEqual(second["error"], first["error"])

    def test_status_reads_terminal_qualification_job_without_calling_it_running(self):
        current = self.descriptor("status-job-current", "2.1.278")
        candidate = self.descriptor("status-job-candidate", "2.1.281")
        self.activate(current)
        target = self.remote_target(candidate)
        policy = cli_store.set_update_policy(
            "automatic", "latest", self.env, channel="latest", auto_qualify=True)
        cli_updates._write_latest_cache(target, self.env)
        selection = cli_store.get_selection(self.env)
        cli_updates._with_state(self.env, lambda _old: {
            "schema_version": 1, "run_id": "maintenance-status-job", "state": "available",
            "reason": "qualification_pending", "message": "running", "next_action": None,
            "notice_id": None, "notice_acknowledged_at": None,
            "target": target, "target_version": target["version"], "target_sha256": target["sha256"],
            "selection_generation": selection["generation"], "policy_generation": policy["generation"],
            "qualification": {"state": "pending", "job_id": "qualification-status000000",
                              "identity_id": candidate["id"], "contract_id": "bridge-contract-test"},
            "progress": {"phase": "qualification", "bytes_received": target["size"],
                         "total_bytes": target["size"], "percent": 100}})
        with patch.object(cli_validation, "status", return_value={"status": "completed", "outcome": "pass"}):
            summary = cli_updates.status(self.env)
        self.assertEqual(summary["reason"], "qualification_result_pending_maintenance")
        self.assertEqual(summary["progress"]["phase"], "qualification_complete")
        self.assertNotIn("正在", summary["message"])

    def test_real_start_entry_promotes_supported_target_and_explains_impact(self):
        lower = self.descriptor("lower", "2.1.276")
        target_descriptor = self.descriptor("target", "2.1.278")
        self.activate(lower)
        acquired = {**target_descriptor, "acquisition": "retained", "release": self.target(target_descriptor)}
        with patch.object(cli_store, "official_release_target", return_value=self.target(target_descriptor)), \
             patch.object(cli_updates.subprocess, "Popen", side_effect=self.inline_worker(acquired)):
            result = cli_updates.start(reason="startup", environ=self.env)
        self.assertEqual(result["state"], "switched")
        self.assertEqual(result["current_version"], "2.1.278")
        self.assertTrue(result["notice_pending"])
        self.assertIn("已启动任务继续使用各自固定的 CLI 身份", result["message"])
        self.assertEqual(cli_store.get_selection(self.env)["previous"], lower["id"])

        acknowledged = cli_updates.acknowledge(result["notice_id"], self.env)
        self.assertFalse(acknowledged["notice_pending"])
        with self.assertRaisesRegex(ValueError, "not current"):
            cli_updates.acknowledge("different-notice", self.env)

    def test_manual_policy_wins_final_activation_and_retains_acquired_binary(self):
        lower = self.descriptor("lower", "2.1.276")
        target_descriptor = self.descriptor("target", "2.1.278")
        self.activate(lower)
        policy = cli_store.get_update_policy(self.env)
        selection = cli_store.get_selection(self.env)
        run_id = "maintenance-policy-race"
        cli_updates._with_state(self.env, lambda _old: {
            "schema_version": 1, "run_id": run_id, "state": "acquiring", "reason": "test",
            "message": "running", "next_action": None, "notice_id": None,
            "target_version": "2.1.278", "target_sha256": target_descriptor["sha256"],
            "selection_generation": selection["generation"], "policy_generation": policy["generation"],
            "started_at": time.time(), "updated_at": time.time(),
            "progress": {"phase": "starting", "bytes_received": 0, "total_bytes": 10, "percent": 0}})
        acquired = {**target_descriptor, "acquisition": "retained", "release": self.target(target_descriptor)}
        cli_store.set_update_policy("manual", "manual_rollback", self.env)
        with patch.object(cli_store, "acquire_official_release", return_value=acquired):
            cli_updates._run_worker(run_id, self.env)
        raw = cli_updates._read_state(self.env)
        self.assertEqual(raw["state"], "superseded")
        self.assertEqual(cli_store.get_selection(self.env)["active"], lower["id"])
        self.assertEqual(cli_updates.status(self.env)["policy"], "manual")

    def test_explicit_update_api_switches_or_rolls_back_with_manual_policy(self):
        lower = self.descriptor("lower-explicit", "2.1.276")
        target = self.descriptor("target-explicit", "2.1.278")
        self.activate(lower)
        generation = cli_store.get_selection(self.env)["generation"]
        switched = cli_updates.activate_explicit(target["id"], generation, self.env)
        self.assertEqual(switched["active"], target["id"])
        self.assertEqual(switched["update_policy"]["reason"], "manual_activation")
        rolled = cli_updates.rollback_explicit(switched["generation"], self.env)
        self.assertEqual(rolled["active"], lower["id"])
        self.assertEqual(rolled["update_policy"]["reason"], "manual_rollback")

    def test_cross_version_explicit_switch_conflict_is_visible_and_acknowledgeable(self):
        notice_id = "cli-switch-conflict-123"
        cli_store._ensure_root(self.env)
        cli_store._atomic_json(self.store / f"{notice_id}.json", {
            "schema_version": 1, "state": "conflict", "notice_id": notice_id,
            "notice_acknowledged_at": None, "diagnostic": "legacy writer preserved"})
        current = cli_updates.status(self.env)
        self.assertEqual(current["state"], "failed")
        self.assertEqual(current["reason"], "explicit_switch_conflict")
        self.assertTrue(current["notice_pending"])
        self.assertIn("较新的磁盘值", current["message"])
        acknowledged = cli_updates.acknowledge(notice_id, self.env)
        self.assertFalse(acknowledged["notice_pending"])
        self.assertNotEqual(acknowledged["reason"], "explicit_switch_conflict")

    def test_failed_download_and_interrupted_worker_preserve_active_and_can_retry(self):
        lower = self.descriptor("lower", "2.1.276")
        target_descriptor = self.descriptor("target", "2.1.278")
        self.activate(lower)

        class Process:
            pid = 3131
        def failing_launch(command, **_kwargs):
            with patch.object(cli_store, "acquire_official_release", side_effect=RuntimeError("digest mismatch")):
                cli_updates._run_worker(command[3], self.env)
            return Process()
        with patch.object(cli_store, "official_release_target", return_value=self.target(target_descriptor)), \
             patch.object(cli_updates.subprocess, "Popen", side_effect=failing_launch):
            failed = cli_updates.start(reason="startup", environ=self.env)
        self.assertEqual(failed["state"], "failed")
        self.assertIn("digest mismatch", failed["error"])
        self.assertIn("摘要、大小", failed["message"])
        self.assertEqual(cli_store.get_selection(self.env)["active"], lower["id"])

        run_id = "maintenance-interrupted"
        selection = cli_store.get_selection(self.env)
        policy = cli_store.get_update_policy(self.env)
        cli_updates._with_state(self.env, lambda _old: {
            "schema_version": 1, "run_id": run_id, "state": "acquiring", "reason": "retry",
            "message": "正在后台获取审计版本；现有 active 版本继续服务，已启动任务不切换。",
            "next_action": None, "notice_id": None,
            "target_version": "2.1.278", "target_sha256": target_descriptor["sha256"],
            "selection_generation": selection["generation"], "policy_generation": policy["generation"],
            "started_at": time.time(), "updated_at": time.time(),
            "progress": {"phase": "downloading", "bytes_received": 1, "total_bytes": 10, "percent": 10}})
        update_lock = self.store / "updates" / "worker.lock"
        with update_lock.open("a+") as held:
            fcntl.flock(held.fileno(), fcntl.LOCK_EX)
            with patch.object(cli_store, "official_release_target", return_value=self.target(target_descriptor)):
                acquiring = cli_updates.status(self.env)
            self.assertEqual(acquiring["state"], "acquiring")
            self.assertEqual(acquiring["progress"]["phase"], "downloading")
            self.assertIn("现有 active 版本", acquiring["message"])
        with patch.object(cli_store, "official_release_target", return_value=self.target(target_descriptor)):
            interrupted = cli_updates.status(self.env)
        self.assertEqual(interrupted["state"], "interrupted")
        self.assertTrue(interrupted["notice_pending"])
        acknowledged = cli_updates.acknowledge(interrupted["notice_id"], self.env)
        self.assertFalse(acknowledged["notice_pending"])
        acquired = {**target_descriptor, "acquisition": "retained", "release": self.target(target_descriptor)}
        with patch.object(cli_store, "official_release_target", return_value=self.target(target_descriptor)), \
             patch.object(cli_updates.subprocess, "Popen", side_effect=self.inline_worker(acquired)):
            retried = cli_updates.start(reason="periodic", force=True, environ=self.env)
        self.assertEqual(retried["state"], "switched")
        self.assertEqual(cli_store.get_selection(self.env)["active"], target_descriptor["id"])

    def test_worker_uses_idle_and_one_hour_attempt_bounds_and_failed_progress_is_resumable(self):
        lower = self.descriptor("lower-bounds", "2.1.276")
        target_descriptor = self.descriptor("target-bounds", "2.1.278")
        self.activate(lower)
        selection = cli_store.get_selection(self.env)
        policy = cli_store.get_update_policy(self.env)
        run_id = "maintenance-bounds"
        cli_updates._with_state(self.env, lambda _old: {
            "schema_version": 1, "run_id": run_id, "state": "acquiring", "reason": "test",
            "message": "running", "next_action": None, "notice_id": None,
            "target_version": "2.1.278", "target_sha256": target_descriptor["sha256"],
            "selection_generation": selection["generation"], "policy_generation": policy["generation"],
            "started_at": time.time(), "updated_at": time.time(),
            "progress": {"phase": "downloading", "bytes_received": 10,
                         "total_bytes": 100, "percent": 10}})
        observed = {}

        def acquire(version, environ, **kwargs):
            observed.update(version=version, environ=environ, **kwargs)
            raise RuntimeError("network timed out")

        with patch.object(cli_store, "acquire_official_release", side_effect=acquire):
            cli_updates._run_worker(run_id, self.env)
        self.assertEqual(observed["idle_timeout_seconds"], 120)
        self.assertEqual(observed["attempt_timeout_seconds"], 3600)
        failed = cli_updates._read_state(self.env)
        self.assertEqual(failed["state"], "failed")
        self.assertEqual(failed["progress"]["phase"], "paused")
        self.assertEqual(failed["progress"]["bytes_received"], 10)
        self.assertTrue(failed["resumable"])
        self.assertEqual(cli_store.get_selection(self.env)["active"], lower["id"])

    def test_old_ack_cannot_acknowledge_a_new_notice_and_spawn_failure_releases_lock(self):
        target_descriptor = self.descriptor("target", "2.1.278")
        first = {"schema_version": 1, "state": "failed", "reason": "first", "message": "first",
                 "next_action": "retry", "notice_id": "notice-first", "notice_acknowledged_at": None,
                 "progress": {"phase": "complete", "bytes_received": 0, "total_bytes": 1, "percent": 0}}
        cli_updates._with_state(self.env, lambda _old: first)
        second = {**first, "notice_id": "notice-second", "reason": "second"}
        cli_updates._with_state(self.env, lambda _old: second)
        with self.assertRaisesRegex(ValueError, "not current"):
            cli_updates.acknowledge("notice-first", self.env)

        cli_store.set_update_policy("manual", "manual_rollback", self.env)
        with patch.object(cli_store, "official_release_target", return_value=self.target(target_descriptor)):
            paused = cli_updates.status(self.env)
        self.assertEqual(paused["state"], "manual")
        self.assertIsNone(paused["notice_id"])
        self.assertFalse(paused["notice_pending"])
        cli_store.set_update_policy("automatic", "user_request", self.env)

        lower = self.descriptor("lower", "2.1.276")
        self.activate(lower)
        target = self.target(target_descriptor)
        original_with_state = cli_updates._with_state
        calls = 0
        def fail_first_write(environ, mutate):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise OSError("state write failed")
            return original_with_state(environ, mutate)
        with patch.object(cli_store, "official_release_target", return_value=target), \
             patch.object(cli_updates, "_with_state", side_effect=fail_first_write):
            failed = cli_updates.start(reason="startup", force=True, environ=self.env)
        self.assertEqual(failed["state"], "failed")
        lock_path = self.store / "updates" / "worker.lock"
        with lock_path.open("a+") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def test_post_activation_worker_loss_recovers_switch_notice_without_claiming_no_switch(self):
        lower = self.descriptor("lower", "2.1.276")
        target_descriptor = self.descriptor("target", "2.1.278")
        self.activate(lower)
        selection = cli_store.get_selection(self.env)
        policy = cli_store.get_update_policy(self.env)
        run_id = "maintenance-post-activation-loss"
        cli_updates._with_state(self.env, lambda _old: {
            "schema_version": 1, "run_id": run_id, "state": "acquiring", "reason": "startup",
            "message": "running", "next_action": None, "notice_id": None,
            "target_version": "2.1.278", "target_sha256": target_descriptor["sha256"],
            "selection_generation": selection["generation"], "policy_generation": policy["generation"],
            "started_at": time.time(), "updated_at": time.time(),
            "progress": {"phase": "downloading", "bytes_received": 10, "total_bytes": 10, "percent": 100}})
        cli_store.activate(target_descriptor["id"], selection["generation"], self.env)
        with patch.object(cli_store, "official_release_target", return_value=self.target(target_descriptor)):
            recovered = cli_updates.status(self.env)
        self.assertEqual(recovered["state"], "switched")
        self.assertEqual(recovered["reason"], "target_selected_before_worker_exit")
        self.assertIn("已经成为 active", recovered["message"])
        self.assertTrue(recovered["notice_pending"])
        with patch.object(cli_store, "official_release_target", return_value=self.target(target_descriptor)):
            acknowledged = cli_updates.acknowledge(recovered["notice_id"], self.env)
        self.assertFalse(acknowledged["notice_pending"])

    def test_concurrent_start_coalesces_and_verified_newer_active_is_not_downgraded(self):
        lower = self.descriptor("lower", "2.1.276")
        target_descriptor = self.descriptor("target", "2.1.278")
        self.activate(lower)
        root = self.store / "updates"
        root.mkdir(parents=True)
        with (root / "worker.lock").open("a+") as held:
            fcntl.flock(held.fileno(), fcntl.LOCK_EX)
            with patch.object(cli_store, "official_release_target", return_value=self.target(target_descriptor)), \
                 patch.object(cli_updates.subprocess, "Popen") as popen:
                coalesced = cli_updates.start(reason="startup", environ=self.env)
            self.assertEqual(coalesced["state"], "available")
            popen.assert_not_called()

        higher = self.descriptor("higher", "2.1.279")
        # Unknown versions require real local qualification before activation.
        matrix = {name: {"status": "pass" if name in {"core", "read_only"} else "inconclusive",
                         "reason": "fixture", "evidence": ["report.json"] if name in {"core", "read_only"} else []}
                  for name in ("core", "read_only", "write", "resume", "workflow")}
        report = self.store / "jobs" / "higher" / "report.json"
        report.parent.mkdir(parents=True)
        report.write_text(__import__("json").dumps({"status": "completed", "job_id": "higher",
                                                   "identity_id": higher["id"], "identity_sha256": higher["sha256"],
                                                   "bridge_contract_id": "bridge-contract-test", "matrix": matrix}))
        cli_store.record_qualification(higher["id"], "bridge-contract-test", matrix, str(report), self.env)
        self.activate(higher)
        with patch.object(cli_store, "official_release_target", return_value=self.target(target_descriptor)), \
             patch.object(cli_updates.subprocess, "Popen") as popen:
            kept = cli_updates.start(reason="periodic", environ=self.env)
        self.assertEqual(kept["state"], "up_to_date")
        self.assertEqual(kept["reason"], "current_newer_than_target")
        self.assertEqual(cli_store.get_selection(self.env)["active"], higher["id"])
        popen.assert_not_called()

    def test_latest_discovery_qualifies_once_then_promotes_current_contract_proof(self):
        lower = self.descriptor("latest-lower", "2.1.278")
        candidate = self.descriptor("latest-candidate", "2.1.280")
        self.activate(lower)
        target = self.remote_target(candidate)
        cli_store.set_update_policy("automatic", "user_requested_official_latest", self.env,
                                    channel="latest", auto_qualify=True)
        acquired = {**candidate, "acquisition": "downloaded", "release": target}

        class Process:
            pid = 4242
        def launch(command, **_kwargs):
            with patch.object(cli_store, "acquire_official_target", return_value=acquired):
                cli_updates._run_worker(command[3], self.env)
            return Process()

        with patch.object(official_releases, "discover_latest", return_value=target), \
             patch.object(cli_updates.subprocess, "Popen", side_effect=launch), \
             patch.object(cli_validation, "start", return_value={"job_id": "qualification-latest"}) as paid:
            pending = cli_updates.start(reason="startup", environ=self.env)
        self.assertEqual(pending["reason"], "qualification_pending")
        self.assertEqual(pending["qualification"]["job_id"], "qualification-latest")
        self.assertEqual(cli_store.get_selection(self.env)["active"], lower["id"])
        paid.assert_called_once_with(candidate["id"], model="sonnet", groups=None,
                                     activate_on_success=False, environ=self.env,
                                     selection_generation=1, reconcile_unfinished=False)

        matrix = {group: {"status": "pass" if group in {"core", "read_only"} else "inconclusive",
                          "reason": "fixture", "evidence": ["report.json"] if group in {"core", "read_only"} else []}
                  for group in ("core", "read_only", "write", "resume", "workflow")}
        report = self.store / "jobs" / "qualification-latest" / "report.json"
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text(__import__("json").dumps(
            {"status": "completed", "job_id": "qualification-latest", "identity_id": candidate["id"],
             "identity_sha256": candidate["sha256"], "bridge_contract_id": "bridge-contract-test",
             "matrix": matrix}))
        cli_store.record_qualification(candidate["id"], "bridge-contract-test", matrix, str(report), self.env)

        with patch.object(cli_updates.subprocess, "Popen", side_effect=launch), \
             patch.object(cli_validation, "start") as no_second_paid:
            switched = cli_updates.start(reason="periodic", environ=self.env)
        self.assertEqual(switched["state"], "switched")
        self.assertEqual(cli_store.get_selection(self.env)["active"], candidate["id"])
        self.assertEqual(switched["missing_groups"], ["write", "resume", "workflow"])
        self.assertEqual({item["group"] for item in switched["qualification"]["fallback_evidence"]},
                         {"write", "resume", "workflow"})
        no_second_paid.assert_not_called()

    def test_unchanged_required_candidate_is_not_reacquired_each_tick(self):
        lower = self.descriptor("required-lower", "2.1.278")
        candidate = self.descriptor("required-candidate", "2.1.280")
        self.activate(lower)
        target = self.remote_target(candidate)
        cli_store.set_update_policy("automatic", "latest", self.env,
                                    channel="latest", auto_qualify=False)
        cli_updates._write_latest_cache(target, self.env)
        acquired = {**candidate, "acquisition": "retained", "release": target}

        class Process:
            pid = 4242
        def launch(command, **_kwargs):
            with patch.object(cli_store, "acquire_official_target", return_value=acquired):
                cli_updates._run_worker(command[3], self.env)
            return Process()

        with patch.object(cli_updates.subprocess, "Popen", side_effect=launch):
            first = cli_updates.start(reason="periodic", environ=self.env)
        self.assertEqual(first["reason"], "qualification_required")
        with patch.object(cli_updates.subprocess, "Popen") as no_worker, \
             patch.object(cli_store, "acquire_official_target") as no_acquire:
            second = cli_updates.start(reason="periodic", environ=self.env)
        self.assertEqual(second["reason"], "qualification_required")
        no_worker.assert_not_called()
        no_acquire.assert_not_called()

        matrix = {group: {"status": "pass" if group in {"core", "read_only"} else "inconclusive",
                          "reason": "manual evidence", "evidence": ["report.json"]
                          if group in {"core", "read_only"} else []}
                  for group in ("core", "read_only", "write", "resume", "workflow")}
        report = self.store / "jobs" / "manual-no-activate" / "report.json"
        report.parent.mkdir(parents=True)
        report.write_text(__import__("json").dumps({
            "status": "completed", "job_id": "manual-no-activate",
            "identity_id": candidate["id"], "identity_sha256": candidate["sha256"],
            "bridge_contract_id": "bridge-contract-test", "matrix": matrix}))
        cli_store.record_qualification(candidate["id"], "bridge-contract-test", matrix,
                                       str(report), self.env)
        with patch.object(cli_updates.subprocess, "Popen", side_effect=launch):
            switched = cli_updates.start(reason="periodic", environ=self.env)
        self.assertEqual(switched["state"], "switched")
        self.assertEqual(cli_store.get_selection(self.env)["active"], candidate["id"])

    def test_failed_or_inconclusive_identity_never_repeats_paid_qualification(self):
        candidate = self.descriptor("failed-candidate", "2.1.280")
        state = self.automatic_state()
        with patch.object(cli_validation, "start", return_value={"job_id": "qualification-failed"}) as paid:
            action, first = cli_updates._qualification_action(candidate, state, self.env)
        self.assertEqual(action, "pending")
        self.assertEqual(first["job_id"], "qualification-failed")
        paid.assert_called_once()
        with patch.object(cli_validation, "status",
                          return_value={"status": "completed", "outcome": "inconclusive"}):
            action, terminal = cli_updates._qualification_action(candidate, state, self.env)
        self.assertEqual(action, "terminal")
        self.assertTrue(terminal["attempt_final"])
        with patch.object(cli_validation, "start") as no_retry, \
             patch.object(cli_validation, "status") as no_poll:
            action, repeated = cli_updates._qualification_action(candidate, state, self.env)
        self.assertEqual(action, "terminal")
        self.assertTrue(repeated["attempt_final"])
        no_retry.assert_not_called()
        no_poll.assert_not_called()

    def test_preexisting_failed_proof_is_preferred_without_new_paid_request(self):
        candidate = self.descriptor("preexisting-failed", "2.1.280")
        matrix = {group: {"status": "fail" if group == "core" else "inconclusive",
                          "reason": "prior result", "evidence": []}
                  for group in ("core", "read_only", "write", "resume", "workflow")}
        report = self.store / "jobs" / "prior-failed" / "report.json"
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text(__import__("json").dumps(
            {"status": "completed", "job_id": "prior-failed", "identity_id": candidate["id"],
             "identity_sha256": candidate["sha256"], "bridge_contract_id": "bridge-contract-test",
             "matrix": matrix}))
        cli_store.record_qualification(candidate["id"], "bridge-contract-test", matrix, str(report), self.env)
        with patch.object(cli_validation, "start") as no_paid:
            action, result = cli_updates._qualification_action(
                candidate, {"auto_qualify": True}, self.env)
        self.assertEqual(action, "terminal")
        self.assertEqual(result["state"], "fail")
        self.assertTrue(result["attempt_final"])
        no_paid.assert_not_called()

    def test_partial_candidate_requires_retained_fallback_for_every_missing_group(self):
        candidate = self.descriptor("partial-no-fallback", "2.1.280")
        evidence, missing = cli_updates._fallback_evidence(
            ["write", "resume", "workflow"], candidate["id"],
            "bridge-contract-test", self.env)
        self.assertEqual(evidence, [])
        self.assertEqual(missing, ["write", "resume", "workflow"])

    def test_stale_latest_response_cannot_replace_newer_signed_cache(self):
        lower = self.descriptor("stale-lower", "2.1.278")
        newer = self.descriptor("stale-newer", "2.1.280")
        stale = self.descriptor("stale-response", "2.1.279")
        self.activate(lower)
        newer_target, stale_target = self.remote_target(newer), self.remote_target(stale)
        cli_store.set_update_policy("automatic", "latest", self.env,
                                    channel="latest", auto_qualify=False)
        cli_updates._write_latest_cache(newer_target, self.env)
        acquired = {**newer, "acquisition": "retained", "release": newer_target}

        class Process:
            pid = 4242
        def launch(command, **_kwargs):
            with patch.object(cli_store, "acquire_official_target", return_value=acquired):
                cli_updates._run_worker(command[3], self.env)
            return Process()
        with patch.object(official_releases, "discover_latest", return_value=stale_target), \
             patch.object(cli_updates.subprocess, "Popen", side_effect=launch):
            result = cli_updates.start(reason="refresh", force=True, environ=self.env)
        self.assertEqual(result["target_version"], "2.1.280")
        self.assertEqual(cli_updates._read_latest_cache(self.env)["target"]["version"], "2.1.280")
        self.assertEqual(result["reason"], "qualification_required")

    def test_latest_discovery_failure_preserves_active_and_never_starts_qualification(self):
        current = self.descriptor("discovery-current", "2.1.278")
        self.activate(current)
        cli_store.set_update_policy("automatic", "latest", self.env,
                                    channel="latest", auto_qualify=True)

        class Process:
            pid = 4242
        def launch(command, **_kwargs):
            cli_updates._run_worker(command[3], self.env)
            return Process()
        with patch.object(official_releases, "discover_latest",
                          side_effect=RuntimeError("manifest signature verification failed")), \
             patch.object(cli_updates.subprocess, "Popen", side_effect=launch), \
             patch.object(cli_validation, "start") as no_paid:
            failed = cli_updates.start(reason="startup", environ=self.env)
        self.assertEqual(failed["state"], "failed")
        self.assertIn("signature verification failed", failed["error"])
        self.assertEqual(cli_store.get_selection(self.env)["active"], current["id"])
        no_paid.assert_not_called()

    def test_stale_cached_target_does_not_hide_fresh_discovery_failure(self):
        current = self.descriptor("stale-failure-current", "2.1.278")
        self.activate(current)
        target = self.remote_target(current)
        cli_store.set_update_policy("automatic", "latest", self.env,
                                    channel="latest", auto_qualify=True)
        cli_updates._atomic_json(cli_updates._latest_cache_path(self.env), {
            "schema_version": 1, "target": target, "checked_at": time.time() - 86400})

        class Process:
            pid = 4242
        def launch(command, **_kwargs):
            cli_updates._run_worker(command[3], self.env)
            return Process()
        with patch.object(official_releases, "discover_latest",
                          side_effect=RuntimeError("GPG is required")), \
             patch.object(cli_updates.subprocess, "Popen", side_effect=launch):
            summary = cli_updates.start(reason="periodic", environ=self.env)
        self.assertEqual(summary["state"], "failed")
        self.assertEqual(summary["reason"], "maintenance_failed")
        self.assertEqual(summary["channel"]["discovery_state"], "failed_stale")
        self.assertEqual(summary["channel"]["last_discovery_error"], "GPG is required")
        self.assertTrue(summary["current_qualified"])


if __name__ == "__main__":
    unittest.main()
