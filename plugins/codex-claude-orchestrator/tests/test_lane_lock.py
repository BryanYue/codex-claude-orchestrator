"""Lane ownership survives Runtime death and temporary coordination cleanup."""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "codex-claude-orchestrator" / "scripts"
sys.path.insert(0, str(SCRIPTS))
import bridge  # noqa: E402
import lane_lock  # noqa: E402
from runtime import Runtime  # noqa: E402


class DurableLaneTests(unittest.TestCase):
    def setUp(self):
        temporary = self.enterContext(tempfile.TemporaryDirectory())
        self.root = Path(temporary)
        self.enterContext(mock.patch.dict(os.environ, {
            "TMPDIR": str(self.root), "CODEX_CLAUDE_COORDINATION_ROOT": str(self.root / "durable")}))
        self.enterContext(mock.patch.object(tempfile, "tempdir", temporary))
        os.environ.pop("CODEX_CLAUDE_LANE_FD", None)
        os.environ.pop("CODEX_CLAUDE_DURABLE_LANE_FD", None)
        self.identity = str((self.root / "workspace").resolve())

    def assertLocked(self, path):
        with path.open("a+") as handle:
            with self.assertRaises(BlockingIOError):
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_legacy_holder_blocks_new_admission_and_releases_partial_durable_lock(self):
        with bridge.lane_lock_path(self.identity).open("a+") as old:
            fcntl.flock(old.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(bridge.BridgeError):
                bridge.lock_file(self.identity, "new")
            with lane_lock.durable_lock_path(self.identity).open("a+") as durable:
                fcntl.flock(durable.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_legacy_only_inheritance_gains_durable_protection(self):
        with bridge.lane_lock_path(self.identity).open("a+") as old:
            fcntl.flock(old.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            with mock.patch.dict(os.environ, {"CODEX_CLAUDE_LANE_FD": str(old.fileno())}):
                handle = bridge.lock_file(self.identity, "old-launcher")
            try:
                self.assertLocked(lane_lock.durable_lock_path(self.identity))
            finally:
                handle.close()
            self.assertLocked(bridge.lane_lock_path(self.identity))

    def test_removing_temporary_lock_tree_cannot_admit_another_new_run(self):
        handle = lane_lock.acquire(self.identity)
        try:
            shutil.rmtree(bridge.lane_lock_path(self.identity).parent)
            with self.assertRaises(bridge.BridgeError):
                bridge.lock_file(self.identity, "second")
            self.assertLocked(lane_lock.durable_lock_path(self.identity))
        finally:
            handle.close()

    def test_inherited_bridge_retains_both_locks_after_runtime_crash(self):
        release = self.root / "release"
        child = """import os,sys,time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import bridge
held=bridge.lock_file(sys.argv[2], 'child')
print(os.getpid(), flush=True)
deadline=time.monotonic()+15
while not Path(sys.argv[3]).exists() and time.monotonic()<deadline: time.sleep(.01)
held.close()
"""
        parent = """import os,subprocess,sys
sys.path.insert(0, sys.argv[1])
from runtime import Runtime
held=object.__new__(Runtime)._lane_lock(sys.argv[2])
env=dict(os.environ);env.update(held.inherited_environment())
child=subprocess.Popen([sys.executable,'-c',sys.argv[4],*sys.argv[1:4]],env=env,pass_fds=held.filenos(),stdout=subprocess.PIPE,text=True)
print(child.stdout.readline().strip(), flush=True)
os._exit(0)
"""
        proc = subprocess.Popen([sys.executable, "-c", parent, str(SCRIPTS), self.identity, str(release), child],
                                env=dict(os.environ), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            child_pid_line = proc.stdout.readline().strip()
            if not child_pid_line.isdigit():
                self.fail(proc.stderr.read())
            child_pid = int(child_pid_line)
            self.addCleanup(lambda: release.touch())
            def stop_child():
                try:
                    os.kill(child_pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
            self.addCleanup(stop_child)
            self.assertEqual(proc.wait(timeout=5), 0)
            self.assertLocked(lane_lock.durable_lock_path(self.identity))
            self.assertLocked(bridge.lane_lock_path(self.identity))
            with self.assertRaises(bridge.BridgeError):
                bridge.lock_file(self.identity, "contender")
        finally:
            release.touch()
            if proc.poll() is None:
                proc.kill()
            proc.communicate(timeout=5)

    def test_wrong_lane_inherited_descriptor_fails_closed(self):
        handle = lane_lock.acquire(self.identity)
        try:
            with mock.patch.dict(os.environ, handle.inherited_environment()):
                with self.assertRaises(bridge.BridgeError):
                    bridge.lock_file(str(self.root / "other"), "other")
        finally:
            handle.close()

    def test_legacy_marker_remains_visible_to_runtime_without_migration_writes(self):
        durable, legacy = bridge.unknown_marker_roots()
        marker = legacy / (bridge._lane_key(self.identity) + ".json")
        marker.write_text(json.dumps({"run_id": "old", "lane_identity": self.identity}))
        self.assertEqual([path for path, _ in bridge.unknown_markers(self.identity)], [marker])
        self.assertEqual(Runtime._marker_values()[0]["run_id"], "old")
        self.assertFalse(list(durable.glob("*.json")))

    def test_publication_preserves_foreign_primary_in_both_roots(self):
        foreign = json.dumps({"run_id": "foreign", "lane_identity": self.identity})
        for root in bridge.unknown_marker_roots():
            (root / (bridge._lane_key(self.identity) + ".json")).write_text(foreign)
        published = bridge.publish_unknown_marker(self.identity, {"run_id": "mine"})
        self.assertEqual(published.parent, bridge.unknown_marker_root())
        markers = bridge.unknown_markers(self.identity)
        self.assertEqual(len(markers), 4)
        self.assertEqual(sorted(value["run_id"] for _, value in markers), ["foreign", "foreign", "mine", "mine"])
        for root in bridge.unknown_marker_roots():
            self.assertEqual((root / (bridge._lane_key(self.identity) + ".json")).read_text(), foreign)

    def test_unknown_evidence_survives_legacy_directory_cleanup(self):
        bridge.publish_unknown_marker(self.identity, {"run_id": "mine"})
        shutil.rmtree(bridge.unknown_marker_roots()[1])
        markers = bridge.unknown_markers(self.identity)
        self.assertEqual(len(markers), 1)
        self.assertEqual(markers[0][1]["run_id"], "mine")

    def test_launch_intent_cleanup_removes_both_nonce_matches_but_preserves_foreign(self):
        run_dir = self.root / "run"
        run_dir.mkdir()
        (run_dir / "receipt.json").write_text(json.dumps({"status": "reported"}))
        bridge.publish_unknown_marker(self.identity, {"run_id": "foreign", "launch_intent": {"nonce": "foreign"}})
        path = bridge.publish_unknown_marker(self.identity, {"run_id": "run", "launch_intent": {"nonce": "mine"}})
        args = argparse.Namespace(run_dir=str(run_dir), _launch_intent={"path": path, "nonce": "mine", "child": "stopped"})
        bridge.release_launch_intent(args)
        self.assertEqual([value["run_id"] for _, value in bridge.unknown_markers(self.identity)], ["foreign", "foreign"])


if __name__ == "__main__":
    unittest.main()
