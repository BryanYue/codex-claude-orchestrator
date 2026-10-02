"""Cleanup must use the current disposition and serialize it with copy disposal."""
import fcntl
import json
from pathlib import Path
import shutil
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

PLUGIN = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(PLUGIN / "scripts"), str(PLUGIN / "skills/codex-claude-orchestrator/scripts")]
import process_family
import runtime


class ReviewCleanupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.instance = runtime.Runtime.__new__(runtime.Runtime)
        self.instance._guard = threading.RLock()
        self.instance.registry_path = self.root / "registry.json"
        self.instance.registry_lock_path = self.root / "registry.lock"
        self.instance.runs_root = self.root / "runs"
        self.run_id = "run-cleanup-fixture"
        self.run = self.instance.runs_root / self.run_id
        self.copy = self.run / "review-workspace"
        self.copy.mkdir(parents=True)
        (self.copy / "review-evidence.txt").write_text("retained evidence")
        self.record = {"run_id": self.run_id, "status": "reported", "cwd": str(self.root),
                       "lane_identity": str(self.root), "decision": {"decision": "accepted"}}
        runtime.bridge.dump(self.instance.registry_path, {"runs": {self.run_id: self.record}})
        runtime.bridge.dump(self.run / "child.json", {"family_marker": "fixture-marker"})
        # The snapshot is frozen before the simulated concurrent decision at lane acquisition.
        self.instance.snapshot = Mock(return_value=json.loads(json.dumps(self.record)))
        self.instance._trusted_terminal = Mock(return_value={"status": "reported"})
        self.instance._release_lane = Mock()
        self.instance._lane_lock = Mock(return_value=object())
        self.enterContext(patch.object(runtime.bridge, "load_execution_workspace", return_value={
            "metadata_path": str(self.run / "identity.json"), "workspace_root": str(self.copy)}))
        self.enterContext(patch.object(process_family.Family, "observe", return_value={"state": "observed", "live_count": 0}))

    def test_stale_accepted_snapshot_cannot_remove_a_returned_or_superseded_copy(self):
        for change in ({"decision": {"decision": "returned", "resolution": "revision_requested"}},
                       {"superseded_by": "run-newer-fixture"}):
            with self.subTest(change=change):
                runtime.bridge.dump(self.instance.registry_path, {"runs": {self.run_id: self.record}})

                def concurrent_change(lane):
                    current = self.instance._registry()
                    current["runs"][self.run_id].update(change)
                    runtime.bridge.dump(self.instance.registry_path, current)
                    return object()

                self.instance._lane_lock.side_effect = concurrent_change
                with patch.object(runtime.bridge.review_workspace, "cleanup_review_workspace") as remove:
                    with self.assertRaisesRegex(RuntimeError, "returned/unknown copies are retained"):
                        self.instance.cleanup_review(self.run_id)
                    remove.assert_not_called()
                self.assertEqual((self.copy / "review-evidence.txt").read_text(), "retained evidence")
                self.assertFalse((self.run / "review-cleanup.json").exists())
                current = self.instance._registry()["runs"][self.run_id]
                for key, value in change.items():
                    self.assertEqual(current[key], value)
        self.assertEqual(self.instance._release_lane.call_count, 2)

    def test_registry_lock_is_held_through_actual_copy_disposal_and_outcome(self):
        def remove(*args, **kwargs):
            with self.instance.registry_lock_path.open("a+") as contender:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(contender.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(kwargs["status"], "accepted")
            shutil.rmtree(self.copy)
            return True

        with patch.object(runtime.bridge.review_workspace, "cleanup_review_workspace", side_effect=remove):
            outcome = self.instance.cleanup_review(self.run_id)
        self.assertEqual(outcome["state"], "removed")
        self.assertFalse(self.copy.exists())
        self.assertEqual(self.instance._registry()["runs"][self.run_id]["review_cleanup"], outcome)
        self.assertEqual(json.loads((self.run / "review-cleanup.json").read_text()), outcome)
        self.instance._release_lane.assert_called_once()


if __name__ == "__main__":
    unittest.main()
