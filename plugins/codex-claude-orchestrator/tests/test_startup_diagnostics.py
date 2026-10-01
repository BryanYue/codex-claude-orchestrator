"""Bridge startup readiness is reported apart from Claude CLI installation and login."""
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock
from unittest.mock import Mock, patch

PLUGIN = Path(__file__).resolve().parents[1]
SCRIPTS = PLUGIN / "skills" / "codex-claude-orchestrator" / "scripts"
sys.path.insert(0, str(PLUGIN / "scripts"))
sys.path.insert(0, str(SCRIPTS))
import runtime as runtime_module  # noqa: E402
from runtime import Runtime  # noqa: E402
import bridge  # noqa: E402
import diagnostics  # noqa: E402
import startup_protocol  # noqa: E402

FAKE_CLI = """#!/usr/bin/env python3
import json, sys
a=sys.argv[1:]
if a == ['--version']: print('2.1.276'); raise SystemExit
if a == ['--help']: print('-p --model --effort --output-format --verbose --json-schema --session-id --resume --permission-mode --tools --allowedTools --disallowedTools --settings --strict-mcp-config --mcp-config --disable-slash-commands --no-session-persistence'); raise SystemExit
if a == ['auth','status','--json']: print(json.dumps({'loggedIn':True})); raise SystemExit
raise SystemExit(3)
"""
TERMINAL = {"reported", "failed", "blocked", "cancelled", "timeout", "unknown"}


def git(*args):
    subprocess.run(["git", *args], check=True, capture_output=True)


class StartupReadinessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.enterContext(mock.patch.dict(os.environ, {
            "CLAUDE_ORCHESTRATOR_CLI_ROOT": str(self.root / "isolated-cli-store"),
            "CLAUDE_CONFIG_DIR": str(self.root / "claude-config")}))
        os.environ.pop("CODEX_CLAUDE_LANE_FD", None)
        self.runtime = Runtime(self.root / "state")
        self.addCleanup(self.runtime.close)

    def test_runtime_readiness_reports_identity_and_spawn_directory(self):
        readiness = self.runtime.startup_readiness()
        self.assertTrue(readiness["ready"], readiness)
        self.assertTrue(readiness["code_identity"]["matches"])
        self.assertEqual(readiness["code_identity"]["loaded"], readiness["code_identity"]["disk"])
        self.assertEqual(readiness["spawn_cwd"], {"path": str(self.runtime.state_root), "state": "available"})
        self.assertEqual(readiness["bridge_script"]["path"], str(Path(bridge.__file__).resolve()))
        self.assertIn("login", readiness["scope"])

    def test_loaded_code_mismatch_makes_startup_not_ready(self):
        stale = {**runtime_module._LOADED_CODE, "value": "0" * 64,
                 "files": {**runtime_module._LOADED_CODE["files"], "scripts/startup_protocol.py": "0" * 64}}
        with patch.object(runtime_module, "_LOADED_CODE", stale):
            readiness = self.runtime.startup_readiness()
        self.assertFalse(readiness["ready"])
        self.assertFalse(readiness["code_identity"]["matches"])
        self.assertIn("scripts/startup_protocol.py", readiness["code_identity"]["reason"])

    def test_deleted_process_cwd_is_visible_but_does_not_block_startup(self):
        doomed = self.root / "doomed"; doomed.mkdir()
        probe = ("import json, os, sys; from pathlib import Path; sys.path.insert(0, sys.argv[1]); "
                 "import startup_protocol as s; os.chdir(sys.argv[2]); os.rmdir(sys.argv[2]); "
                 "bridge_script = Path(sys.argv[3]); loaded = s.loaded_identity(s.plugin_root(bridge_script)); "
                 "print(json.dumps(s.startup_readiness(loaded, bridge_script, Path(sys.argv[4]))))")
        got = subprocess.run([sys.executable, "-c", probe, str(PLUGIN / "scripts"), str(doomed),
                              str(SCRIPTS / "bridge.py"), str(self.runtime.state_root)],
                             capture_output=True, text=True, timeout=30)
        self.assertEqual(got.returncode, 0, got.stderr)
        readiness = json.loads(got.stdout)
        self.assertEqual(readiness["process_cwd"]["state"], "unavailable")
        self.assertTrue(readiness["ready"], readiness)

    def test_diagnostics_keep_startup_and_cli_readiness_apart(self):
        env = {"ready": False, "status": "not_authenticated",
               "auth": {"status": "not_logged_in", "email": "private@example.test", "token": "PRIVATE"},
               "cli": {"version": "test"}, "raw": "PRIVATE"}
        with patch.object(diagnostics, "codex_cli", return_value=None), \
                patch.object(diagnostics.bridge, "check_environment", return_value=env), \
                patch.object(diagnostics.shutil, "which", return_value=None):
            supplied = diagnostics.collect(str(self.root), bridge_startup=self.runtime.startup_readiness())
            fallback = diagnostics.collect(str(self.root))
        self.assertFalse(supplied["claude"]["ready"])
        self.assertTrue(supplied["bridge_startup"]["ready"])
        self.assertEqual(supplied["bridge_startup"]["spawn_cwd"]["path"], str(self.runtime.state_root))
        self.assertEqual(fallback["bridge_startup"]["runtime_state"], "not_supplied")
        self.assertTrue(fallback["bridge_startup"]["code_identity"]["matches"])
        self.assertFalse(supplied["ready"])
        encoded = json.dumps([supplied, fallback])
        self.assertNotIn("PRIVATE", encoded)
        self.assertNotIn("private@example", encoded)


class ServerStartupReadinessTests(unittest.IsolatedAsyncioTestCase):
    async def test_environment_tool_adds_startup_readiness_without_changing_cli_ready(self):
        import server
        rt = Mock()
        rt.startup_readiness.return_value = {"ready": False, "reason": "fixture mismatch"}
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(server, "runtime", rt), \
                patch.object(server.bridge, "check_environment", return_value={"ready": True, "status": "ready"}), \
                patch.object(server, "maintenance_status", return_value={"state": "fixture"}):
            result = await server.claude_environment(tmp)
        self.assertTrue(result["ready"])
        self.assertEqual(result["bridge_startup"], {"ready": False, "reason": "fixture mismatch"})


class RecoveryLogEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.fake = self.root / "claude"; self.fake.write_text(FAKE_CLI); self.fake.chmod(0o755)
        self.enterContext(mock.patch.dict(os.environ, {
            "CLAUDE_ORCHESTRATOR_CLI_ROOT": str(self.root / "isolated-cli-store"),
            "CLAUDE_CONFIG_DIR": str(self.root / "claude-config"), "CLAUDE_BIN": str(self.fake)}))
        os.environ.pop("CODEX_CLAUDE_LANE_FD", None)
        self.repo = self.root / "repo"; self.repo.mkdir()
        (self.repo / "base.txt").write_text("base\n")
        git("init", "-q", str(self.repo))
        git("-C", str(self.repo), "config", "user.email", "t@example.invalid")
        git("-C", str(self.repo), "config", "user.name", "T")
        git("-C", str(self.repo), "add", ".")
        git("-C", str(self.repo), "commit", "-qm", "base")
        self.requirement = self.root / "spec.md"; self.requirement.write_text("spec\n")
        self.lane = bridge.lane_identity(self.repo)
        self.addCleanup(lambda: [path.unlink(missing_ok=True) for path, _ in bridge.unknown_markers(self.lane)])
        self.runtime = Runtime(self.root / "state")
        self.addCleanup(self.runtime.close)

    def test_absent_run_dir_logs_are_located_and_hashed_without_their_text(self):
        original = subprocess.Popen
        code = "import sys; sys.stderr.write('TASK_TEXT_SENTINEL ' * 40); sys.exit(1)"

        def popen(command, *args, **kwargs):
            if isinstance(command, list) and startup_protocol.NONCE_ARGUMENT in command:
                command = [sys.executable, "-c", code, *command[1:]]
            return original(command, *args, **kwargs)

        packet = {"task_id": "logs", "revision": 1, "role": "review", "cwd": str(self.repo), "objective": "TASK_TEXT_SENTINEL",
                  "requirement_sources": [str(self.requirement)], "constraints": [], "acceptance": [],
                  "owned_files": [], "protected_files": [], "model": "fixture", "effort": "low"}
        with patch.object(subprocess, "Popen", new=popen):
            run_id = self.runtime.start(packet)["run_id"]
        deadline = time.monotonic() + 10
        while (run_id in self.runtime._workers or self.runtime.snapshot(run_id)["status"] not in TERMINAL) \
                and time.monotonic() < deadline:
            time.sleep(.05)
        stderr_log = self.runtime.logs_root / f"{run_id}.stderr.log"
        expected = {"path": str(stderr_log), "state": "present", "size": stderr_log.stat().st_size,
                    "sha256": hashlib.sha256(stderr_log.read_bytes()).hexdigest()}
        self.assertEqual(self.runtime._registry()["runs"][run_id]["bridge_logs"]["stderr"], expected)
        detail = self.runtime.inspect_recovery(run_id)
        self.assertEqual(detail["bridge_logs"]["stderr"], expected)
        self.assertNotIn("TASK_TEXT_SENTINEL", json.dumps(detail))


if __name__ == "__main__":  # pragma: no cover
    unittest.main(verbosity=2)
