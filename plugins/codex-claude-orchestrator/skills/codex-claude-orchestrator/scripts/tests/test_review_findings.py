"""Regressions for the defects found in the Codex review of the 1.0 rewrite."""
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness  # noqa: E402
import bridge  # noqa: E402
import copy_workspace  # noqa: E402
import packet as packet_schema  # noqa: E402
import result_schema  # noqa: E402
import runtime  # noqa: E402

GOOD = {"status": "completed", "summary": "done"}
MAC = sys.platform == "darwin" and Path("/usr/bin/sandbox-exec").is_file()


@unittest.skipUnless(MAC, "the copy profile needs macOS sandbox-exec")
class ReviewFindingTests(unittest.TestCase):
    def setUp(self):
        self.fx = harness.Fixture(self)

    def test_wrongly_typed_result_fields_still_end_the_run(self):
        bad = {**GOOD, "questions": 1, "disputes": {"x": 1}, "items": [{"id": [], "title": "x", "confidence": "verified"}]}
        snapshot = self.fx.run({"deliver": True, "report": "report survives", "result": bad})
        outcome = snapshot["outcome"]
        self.assertEqual((snapshot["status"], outcome["run_outcome"]), ("ok", "ok"))
        self.assertIn("result_malformed", self.fx.warnings(snapshot))
        self.assertNotIn("questions_for_user", self.fx.warnings(snapshot))
        self.assertEqual((self.fx.run_dir(snapshot) / "report.md").read_text(), "report survives")
        self.assertEqual(json.loads((self.fx.run_dir(snapshot) / "result.json").read_text()), bad)
        self.assertEqual(runtime.Runtime(self.fx.state).snapshot(snapshot["run_id"])["status"], "ok")

    def test_an_assembly_failure_still_writes_an_outcome(self):
        fx = self.fx
        first = fx.run({"deliver": True, "report": "r", "result": GOOD})
        run_dir = fx.run_dir(first)
        (run_dir / "outcome.json").unlink()
        with mock.patch.object(bridge, "_assemble", side_effect=RuntimeError("boom")):
            outcome = bridge.finalize(run_dir, "ok")
        self.assertEqual(outcome["run_outcome"], "ok")
        self.assertEqual([warning["code"] for warning in outcome["warnings"]], ["finalize_error"])
        self.assertTrue((run_dir / "outcome.json").is_file())

    def test_cleanup_claims_the_copy_before_a_continuation_can_start(self):
        fx = self.fx
        one = fx.run({"deliver": True, "report": "r1", "result": GOOD})
        fx.runtime.decide(one["run_id"], "accepted", "done", "checked", ["read patch"])
        entered, release, errors = threading.Event(), threading.Event(), []
        real_remove = copy_workspace.discard_copy

        def slow_remove(path):
            entered.set()
            release.wait(30)
            return real_remove(path)

        def cleanup():
            try:
                fx.runtime.cleanup(one["run_id"])
            except Exception as exc:
                errors.append(exc)
        with mock.patch.object(copy_workspace, "discard_copy", side_effect=slow_remove):
            thread = threading.Thread(target=cleanup)
            thread.start()
            self.assertTrue(entered.wait(10))
            with self.assertRaisesRegex(ValueError, "removed"):
                fx.runtime.start(fx.packet(continue_from=one["run_id"]))
            release.set()
            thread.join(10)
        self.assertEqual(errors, [])
        self.assertFalse((Path(one["lineage_root"]) / "copy").exists())
        self.assertEqual([r["run_id"] for r in fx.runtime.list_runs(task_id="T-1")], [one["run_id"]])

    def test_an_interrupted_cleanup_is_taken_over_only_after_its_cleaner_exits(self):
        fx = self.fx
        child = r"""
import sys, time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import copy_workspace, runtime
real = copy_workspace.discard_copy
def interrupted(path):
    if sys.argv[5] == "after_delete":
        real(path)
    Path(sys.argv[4]).write_text("ready")
    time.sleep(60)
copy_workspace.discard_copy = interrupted
runtime.Runtime(Path(sys.argv[2])).cleanup(sys.argv[3])
"""
        for phase in ("before_delete", "after_delete"):
            with self.subTest(phase=phase):
                first = fx.run({"write": "saved.txt", "text": "x"}, {"deliver": True, "report": "r", "result": GOOD}, task_id=phase)
                fx.runtime.decide(first["run_id"], "accepted", "done", "checked", ["read patch"])
                ready = fx.root / f"{phase}-ready"
                proc = subprocess.Popen([sys.executable, "-c", child, str(Path(runtime.__file__).parent), str(fx.state),
                                         first["run_id"], str(ready), phase], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                self.addCleanup(lambda proc=proc: proc.poll() is None and proc.kill())
                deadline = time.monotonic() + 20
                while not ready.exists() and proc.poll() is None and time.monotonic() < deadline:
                    time.sleep(.02)
                self.assertTrue(ready.exists(), proc.stderr.read().decode() if proc.poll() is not None else "")
                with self.assertRaisesRegex(RuntimeError, "another live cleanup"):
                    runtime.Runtime(fx.state).cleanup(first["run_id"])
                proc.kill()
                proc.communicate(timeout=10)
                restarted = runtime.Runtime(fx.state)
                with self.assertRaisesRegex(ValueError, "removed"):
                    restarted.start(fx.packet(task_id=phase, continue_from=first["run_id"]))
                outcome = restarted.cleanup(first["run_id"])
                self.assertEqual((outcome["state"], outcome["took_over"]), ("removed", True))
                self.assertFalse((Path(first["lineage_root"]) / "copy").exists())
                self.assertEqual(restarted.snapshot(first["run_id"])["copy_state"], "removed")
                self.assertEqual(restarted.cleanup(first["run_id"])["state"], "already_removed")

    def test_a_failed_deletion_keeps_the_copy_unusable_until_cleanup_finishes(self):
        fx = self.fx
        first = fx.run({"deliver": True, "report": "r", "result": GOOD})
        fx.runtime.decide(first["run_id"], "accepted", "done", "checked", ["read patch"])
        copy_root = Path(first["lineage_root"]) / "copy"

        def partial(path):
            (copy_root / "src" / "app.py").unlink()
            raise OSError("disk went away")
        with mock.patch.object(copy_workspace, "discard_copy", side_effect=partial):
            with self.assertRaisesRegex(OSError, "disk went away"):
                fx.runtime.cleanup(first["run_id"])
        snapshot = fx.runtime.snapshot(first["run_id"])
        self.assertEqual((snapshot["copy_state"], snapshot["copy_removed"], snapshot.get("copy_cleaner")), ("removing", True, None))
        with self.assertRaisesRegex(ValueError, "removed"):
            fx.runtime.start(fx.packet(continue_from=first["run_id"]))
        self.assertEqual(fx.runtime.cleanup(first["run_id"])["state"], "removed")
        self.assertFalse(copy_root.exists())

    def test_a_running_continuation_blocks_cleanup(self):
        fx = self.fx
        one = fx.run({"deliver": True, "report": "r1", "result": GOOD})
        fx.runtime.decide(one["run_id"], "accepted", "next_round", "more", ["read patch"])
        fx.script({"sleep": 30})
        two = fx.runtime.start(fx.packet(continue_from=one["run_id"]))["run_id"]
        with self.assertRaisesRegex(RuntimeError, "continues this copy"):
            fx.runtime.cleanup(one["run_id"])
        with self.assertRaisesRegex(RuntimeError, "may still be active"):
            fx.runtime.cleanup(two)
        fx.runtime.cancel(two, "test cleanup")
        fx.wait(two)
        self.assertTrue((Path(one["lineage_root"]) / "copy").is_dir())

    def test_only_the_latest_round_can_be_continued(self):
        fx = self.fx
        one = fx.run({"write": "one.txt", "text": "1"}, {"deliver": True, "report": "r1", "result": GOOD})
        words = fx.packet()["user_messages"] + [{"text": "第二轮新约束：必须保留 CSV 接口", "source": "human"}]
        two = fx.run({"write": "two.txt", "text": "2"}, {"deliver": True, "report": "r2", "result": GOOD},
                     continue_from=one["run_id"], user_messages=words)
        with self.assertRaisesRegex(ValueError, f"latest round of this task, {two['run_id']}"):
            fx.runtime.start(fx.packet(continue_from=one["run_id"]))
        three = fx.run({"deliver": True, "report": "r3", "result": GOOD}, continue_from=two["run_id"], user_messages=words)
        self.assertEqual(three["round"], 3)

    def test_every_protected_file_must_be_confirmed_not_only_the_displayed_ones(self):
        fx = self.fx
        actions = [{"write": f"migrations/{index:03}.sql", "text": "x\n"} for index in range(51)]
        snapshot = fx.run(*actions, {"deliver": True, "report": "r", "result": GOOD}, protected=["migrations/"])
        warning = fx.warnings(snapshot)["protected_touched"]
        self.assertEqual((warning["count"], len(warning["paths"])), (51, 50))
        with self.assertRaisesRegex(ValueError, "migrations/050.sql"):
            fx.runtime.decide(snapshot["run_id"], "accepted", "done", "checked", ["read patch"],
                              protected_confirmed=warning["paths"])
        every = [item["path"] for item in snapshot["outcome"]["changes"]["files"]]
        decided = fx.runtime.decide(snapshot["run_id"], "accepted", "done", "checked", ["read patch"], protected_confirmed=every)
        self.assertEqual(decided["decision"]["verdict"], "accepted")

    def test_suggested_commands_apply_over_uncommitted_original_edits(self):
        fx = self.fx
        (fx.source / "src" / "app.py").write_text("print('user uncommitted')\n")
        snapshot = fx.run({"write": "src/app.py", "text": "print('delegated change')\n"},
                          {"deliver": True, "report": "r", "result": GOOD})
        changes = snapshot["outcome"]["changes"]
        self.assertNotIn("--3way", changes["apply_hint"])
        for command in (changes["check_hint"], changes["apply_hint"]):
            completed = subprocess.run(shlex.split(command), capture_output=True, text=True)
            self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual((fx.source / "src" / "app.py").read_text(), "print('delegated change')\n")

    def test_implement_never_falls_back_to_readonly_without_a_sandbox(self):
        with mock.patch.object(runtime.cli_env, "sandbox_available", return_value=False):
            with self.assertRaisesRegex(ValueError, "implement tasks need macOS sandbox-exec"):
                self.fx.runtime._profile(packet_schema.normalize(self.fx.packet()))
            analyze = packet_schema.normalize(self.fx.packet(kind="analyze"))
            self.assertEqual(self.fx.runtime._profile(analyze), "readonly")


class ResultSchemaRobustnessTests(unittest.TestCase):
    def test_validation_never_raises_on_unhashable_or_odd_values(self):
        for value in ({**GOOD, "items": [{"id": [], "title": "x", "confidence": "verified"}, {"id": {}}]},
                      {**GOOD, "status": ["completed"]}, [], "text", None):
            self.assertIsInstance(result_schema.validate(value), list)


if __name__ == "__main__":
    os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    unittest.main()
