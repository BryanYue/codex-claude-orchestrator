"""One invariant across the combinations reviews kept probing.

For every pending operation (add, modify, delete), merge state of the round
that made it (not applied, applied and recorded, applied but only detected)
and the moment the original starts ignoring that operation's target (never,
before the operation, after its patch was produced), a continued round must
start, and after applying its delivery.patch the original's task files must
equal the copy's.  A file that is ignored before it is created is never part
of a delivery by design, so adding under an earlier ignore rule is excluded.
"""
from itertools import product
from pathlib import Path
import shlex
import subprocess
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness  # noqa: E402

GOOD = {"status": "completed", "summary": "done"}
MAC = sys.platform == "darwin" and Path("/usr/bin/sandbox-exec").is_file()
TASK_FILES = ("src/task.py", "src/extra.py")
OPERATIONS = {
    "add": ("src/extra.py", {"write": "src/extra.py", "text": "extra = 1\n"}),
    "modify": ("src/task.py", {"write": "src/task.py", "text": "task = 2\n"}),
    "delete": ("src/task.py", {"delete": "src/task.py"}),
}


def state(root: Path) -> dict[str, str | None]:
    return {name: (root / name).read_text() if (root / name).exists() else None for name in TASK_FILES}


@unittest.skipUnless(MAC, "the copy profile needs macOS sandbox-exec")
class DeliveryInvariantTests(unittest.TestCase):
    def apply(self, snapshot):
        changes = snapshot["outcome"]["changes"]
        if not changes["delivery_files"]:
            self.assertEqual((changes["check_hint"], changes["apply_hint"]), (None, None))
            return
        for command in (changes["check_hint"], changes["apply_hint"]):
            completed = subprocess.run(shlex.split(command), capture_output=True, text=True)
            self.assertEqual(completed.returncode, 0, (command, completed.stderr))

    def ignore(self, fx, target):
        (fx.source / ".gitignore").write_text(f"build/\n{target}\n")
        matched = subprocess.run(["git", "check-ignore", "-q", "--no-index", target], cwd=fx.source)
        self.assertEqual(matched.returncode, 0, f"the ignore rule does not match {target}")

    def test_original_equals_the_copy_after_applying_the_latest_delivery(self):
        cases = [(operation, merge, ignored)
                 for operation, merge, ignored in product(OPERATIONS, ("pending", "recorded", "detected"),
                                                          ("never", "before", "after"))
                 if not (operation == "add" and ignored == "before")]
        self.assertEqual(len(cases), 24)
        for operation, merge, ignored in cases:
            with self.subTest(operation=operation, merge=merge, ignored=ignored):
                fx = harness.Fixture(self)
                target, action = OPERATIONS[operation]
                one = fx.run({"write": "src/task.py", "text": "task = 1\n"}, {"deliver": True, "report": "r1", "result": GOOD})
                self.apply(one)
                fx.runtime.decide(one["run_id"], "accepted", "next_round", "合入", ["applied"], applied=True)
                if ignored == "before":
                    self.ignore(fx, target)
                two = fx.run(action, {"deliver": True, "report": "r2", "result": GOOD}, continue_from=one["run_id"])
                self.assertEqual(two["status"], "ok", two["outcome"]["note"])
                self.assertIn(target, [item["path"] for item in two["outcome"]["changes"]["delivery_files"]])
                if ignored == "after":
                    self.ignore(fx, target)
                if merge != "pending":
                    self.apply(two)
                    fx.runtime.decide(two["run_id"], "accepted", "next_round", "合入", ["applied"],
                                      applied=True if merge == "recorded" else None)
                three = fx.run({"deliver": True, "report": "r3", "result": GOOD}, continue_from=two["run_id"])
                self.assertEqual(three["status"], "ok", three["outcome"]["note"])
                copy = state(Path(three["lineage_root"]) / "copy")
                self.apply(three)
                self.assertEqual(state(fx.source), copy)
                self.doCleanups()


if __name__ == "__main__":
    unittest.main()
