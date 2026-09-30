"""The retired maintenance module only reports the user's local CLI."""
from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import cli_updates  # noqa: E402


def tree_bytes(root: Path) -> dict[str, bytes]:
    return {str(path.relative_to(root)): path.read_bytes() for path in sorted(root.rglob("*")) if path.is_file()}


class LocalCliStatusTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.store = self.base / "cli-store"
        self.cli = self.base / "bin" / "claude"
        self.cli.parent.mkdir()
        self.cli.write_text("#!/bin/sh\nexit 0\n")
        self.cli.chmod(0o755)
        self.env = {"HOME": str(self.base), "PATH": "/usr/bin:/bin", "CLAUDE_BIN": str(self.cli),
                    "CLAUDE_ORCHESTRATOR_CLI_ROOT": str(self.store),
                    "CLAUDE_ORCHESTRATOR_SETTINGS_PATH": str(self.base / "settings.json")}

    def tearDown(self):
        self.tmp.cleanup()

    def write_legacy_state(self) -> None:
        (self.store / "versions" / "darwin-arm64-2.1.278-deadbeefdeadbeefdead").mkdir(parents=True)
        (self.store / "selection.json").write_text(json.dumps({
            "schema_version": 1, "generation": 3, "active": "darwin-arm64-2.1.278-deadbeefdeadbeefdead",
            "previous": None, "history": ["darwin-arm64-2.1.278-deadbeefdeadbeefdead"], "mode": "managed"}))
        (self.store / "update-policy.json").write_text(json.dumps({
            "schema_version": 1, "generation": 2, "mode": "automatic", "reason": "default", "updated_at": 1.0}))
        (self.store / "updates").mkdir()
        (self.store / "updates" / "state.json").write_text(json.dumps({
            "schema_version": 1, "state": "failed", "resumable": True, "notice_id": "cli-update-old"}))

    def test_status_names_the_local_cli_without_subprocess_or_network(self):
        with patch.object(subprocess, "run", side_effect=AssertionError("status must not run a CLI")), \
             patch.object(subprocess, "Popen", side_effect=AssertionError("status must not spawn a worker")):
            status = cli_updates.status(self.env)
        self.assertEqual(status["policy"], "user_local_cli")
        self.assertEqual(status["version_management"], "retired")
        self.assertEqual(status["state"], "local_cli")
        self.assertEqual(status["local_cli"]["path"], str(self.cli))
        self.assertEqual(status["local_cli"]["source"], "CLAUDE_BIN")
        self.assertFalse(status["notice_pending"])
        self.assertFalse(status["legacy_managed_state"]["present"])
        self.assertIn("不再下载", status["message"])

    def test_missing_local_cli_reports_the_discovery_action(self):
        environment = {**self.env}
        environment.pop("CLAUDE_BIN")
        status = cli_updates.status(environment)
        self.assertEqual(status["state"], "cli_missing")
        self.assertIsNone(status["local_cli"]["path"])
        self.assertIn("CLAUDE_BIN", status["next_action"])

    def test_legacy_managed_state_is_reported_as_history_and_left_unchanged(self):
        self.write_legacy_state()
        before = tree_bytes(self.store)
        status = cli_updates.status(self.env)
        self.assertEqual(status["local_cli"]["path"], str(self.cli))
        legacy = status["legacy_managed_state"]
        self.assertTrue(legacy["present"])
        self.assertTrue(legacy["ignored_for_dispatch"])
        self.assertFalse(legacy["files_deleted"])
        self.assertEqual(legacy["selection"]["mode"], "managed")
        self.assertEqual(tree_bytes(self.store), before, "reading history must not rewrite or delete any file")
        self.assertNotIn("resumable", json.dumps(status))

    def test_running_worker_from_an_older_plugin_is_observed_not_joined(self):
        worker = self.store / "updates" / "worker.lock"
        worker.parent.mkdir(parents=True)
        with worker.open("a+") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            self.assertTrue(cli_updates.status(self.env)["legacy_managed_state"]["maintenance_worker_running"])
            self.assertEqual(cli_updates.status(self.env)["legacy_managed_state"]["maintenance_worker_lock"], "held")
        self.assertFalse(cli_updates.status(self.env)["legacy_managed_state"]["maintenance_worker_running"])
        self.assertEqual(cli_updates.status(self.env)["legacy_managed_state"]["maintenance_worker_lock"], "free")

    def test_overlapping_status_observers_do_not_report_each_other_as_an_old_worker(self):
        worker = self.store / "updates" / "worker.lock"
        worker.parent.mkdir(parents=True)
        worker.write_text("")
        # Another status reader in the middle of its own probe holds the shared lock.
        with worker.open("r") as observer:
            fcntl.flock(observer.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)
            legacy = cli_updates.status(self.env)["legacy_managed_state"]
            self.assertIs(legacy["maintenance_worker_running"], False)
            self.assertEqual(legacy["maintenance_worker_lock"], "free")
            with worker.open("r") as writer:
                with self.assertRaises(BlockingIOError, msg="the fixture reader must really hold its lock"):
                    fcntl.flock(writer.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_absent_lock_is_not_created_by_observation(self):
        legacy = cli_updates.status(self.env)["legacy_managed_state"]
        self.assertIs(legacy["maintenance_worker_running"], False)
        self.assertEqual(legacy["maintenance_worker_lock"], "absent")
        self.assertFalse(self.store.exists(), "observation must not create the store or the lock")

    def test_unreadable_lock_is_unknown_not_stopped(self):
        worker = self.store / "updates" / "worker.lock"
        worker.parent.mkdir(parents=True)
        target = self.base / "elsewhere.lock"; target.write_text("")
        worker.symlink_to(target)
        legacy = cli_updates.status(self.env)["legacy_managed_state"]
        self.assertIsNone(legacy["maintenance_worker_running"], "a lock that cannot be observed is not a stopped worker")
        self.assertEqual(legacy["maintenance_worker_lock"], "unreadable")
        if os.geteuid() == 0:
            return
        worker.unlink(); worker.write_text(""); worker.chmod(0)
        before = worker.stat()
        legacy = cli_updates.status(self.env)["legacy_managed_state"]
        self.assertIsNone(legacy["maintenance_worker_running"])
        self.assertEqual(legacy["maintenance_worker_lock"], "unreadable")
        after = worker.stat()
        self.assertEqual((after.st_mode, after.st_mtime_ns, after.st_size), (before.st_mode, before.st_mtime_ns, before.st_size))

    def test_retired_actions_change_nothing(self):
        self.write_legacy_state()
        before = tree_bytes(self.store)
        for action in ("prepare", "validate", "activate", "rollback", "refresh", "policy", "acknowledge"):
            with self.subTest(action=action):
                result = cli_updates.retired_action(action, self.env)
                self.assertTrue(result["retired"])
                self.assertFalse(result["changed"])
                self.assertFalse(result["claude_started"])
        self.assertEqual(tree_bytes(self.store), before)

    def test_no_download_switch_or_qualification_entry_remains(self):
        for name in ("start", "set_policy", "activate_explicit", "rollback_explicit", "acknowledge", "_run_worker"):
            self.assertFalse(hasattr(cli_updates, name), name)
        source = (ROOT / "scripts" / "cli_updates.py").read_text()
        for forbidden in ("official_releases", "acquire_official", "cli_validation", "Popen", "urlopen"):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
