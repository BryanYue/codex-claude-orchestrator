"""Verify the isolated test-runner process boundary without running suites."""
from __future__ import annotations

import os
from pathlib import Path
import sys
import tempfile
import unittest
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import run_tests  # noqa: E402


class TestRunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "plugin"
        self.root.mkdir()
        for relative in run_tests.SUITE_DIRECTORIES.values():
            (self.root / relative).mkdir(parents=True)

    def tearDown(self):
        self.temp.cleanup()

    def test_all_uses_one_child_only_tmpdir_and_removes_it_after_both_children(self):
        observed = []
        parent_tmp = os.environ.get("TMPDIR")
        parent_coordination = os.environ.get("CODEX_CLAUDE_COORDINATION_ROOT")

        def execute(command, *, cwd, env, check):
            observed.append((command, cwd, dict(env), check))
            self.assertTrue(Path(env["TMPDIR"]).is_dir())
            self.assertEqual(cwd, self.root.resolve())
            self.assertFalse(check)
            return SimpleNamespace(returncode=0)

        status = run_tests.run_suites(self.root, "all", execute=execute)
        self.assertEqual(status, 0)
        self.assertEqual(len(observed), 2)
        child_tmp = Path(observed[0][2]["TMPDIR"])
        self.assertEqual(observed[1][2]["TMPDIR"], str(child_tmp))
        self.assertEqual(observed[0][2]["CODEX_CLAUDE_COORDINATION_ROOT"], str(child_tmp / "durable-coordination"))
        self.assertEqual(observed[1][2]["CODEX_CLAUDE_COORDINATION_ROOT"], str(child_tmp / "durable-coordination"))
        self.assertFalse(child_tmp.exists())
        self.assertEqual(os.environ.get("TMPDIR"), parent_tmp)
        self.assertEqual(os.environ.get("CODEX_CLAUDE_COORDINATION_ROOT"), parent_coordination)
        self.assertEqual(observed[0][0][0], sys.executable)

    def test_failure_still_runs_remaining_suite_and_cleans_owned_directory(self):
        observed = []

        def execute(command, *, cwd, env, check):
            observed.append((command, env["TMPDIR"]))
            return SimpleNamespace(returncode=23 if len(observed) == 1 else 0)

        status = run_tests.run_suites(self.root, "all", execute=execute)
        self.assertEqual(status, 23)
        self.assertEqual(len(observed), 2)
        self.assertFalse(Path(observed[0][1]).exists())

    def test_single_suite_keeps_unittest_discovery_contract(self):
        commands = []

        def execute(command, *, cwd, env, check):
            commands.append(command)
            return SimpleNamespace(returncode=0)

        self.assertEqual(run_tests.run_suites(self.root, "bridge", execute=execute), 0)
        self.assertEqual(commands, [[sys.executable, "-m", "unittest", "discover", "-s",
                                    str(self.root.resolve() / run_tests.SUITE_DIRECTORIES["bridge"])]] )


if __name__ == "__main__":
    unittest.main()
