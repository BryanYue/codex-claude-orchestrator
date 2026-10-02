"""Loaded-versus-executed code identity and the Runtime/bridge startup handoff."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock
from unittest.mock import patch

PLUGIN = Path(__file__).resolve().parents[1]
SCRIPTS = PLUGIN / "skills" / "codex-claude-orchestrator" / "scripts"
BRIDGE = SCRIPTS / "bridge.py"
sys.path.insert(0, str(SCRIPTS))
import runtime as runtime_module  # noqa: E402
from runtime import Runtime, _LOCAL_LANES  # noqa: E402
import bridge  # noqa: E402
import startup_protocol  # noqa: E402

FAKE_CLI = """#!/usr/bin/env python3
import json, os, sys
a=sys.argv[1:]
if a == ['--version']: print('2.1.276'); raise SystemExit
if a == ['--help']: print('-p --model --effort --output-format --verbose --json-schema --session-id --resume --permission-mode --tools --allowedTools --disallowedTools --settings --strict-mcp-config --mcp-config --disable-slash-commands --no-session-persistence'); raise SystemExit
if a == ['auth','status','--json']: print(json.dumps({'loggedIn':True})); raise SystemExit
if os.environ.get('STARTUP_TASK_LOG'):
    with open(os.environ['STARTUP_TASK_LOG'], 'a') as log: log.write(os.getcwd() + '\\n')
s=a[a.index('--session-id')+1] if '--session-id' in a else a[a.index('--resume')+1]
print(json.dumps({'type':'system','subtype':'init','session_id':s,'model':'fixture'}), flush=True)
sys.stdin.buffer.read()
print(json.dumps({'type':'result','subtype':'success','session_id':s,'structured_output':{'status':'completed','summary':'done','evidence':[],'checks':[],'unresolved':[]}}), flush=True)
"""
TERMINAL = {"reported", "failed", "blocked", "cancelled", "timeout", "unknown"}
# Executes the real bridge after seeding what it believes it loaded or later
# reads from disk; argv[1] selects the fault, argv[2:] is the bridge command.
HANDOFF_SHIM = """
import runpy, sys
from pathlib import Path
fault, bridge_path = sys.argv[1], sys.argv[2]
sys.argv = sys.argv[2:]
here = Path(bridge_path).resolve()
sys.path.insert(0, str(here.parents[3] / "scripts")); sys.path.insert(0, str(here.parent))
import startup_protocol
root = startup_protocol.plugin_root(here)
real = startup_protocol.code_identity(root)
if fault == "loaded_differs":
    startup_protocol._LOADED[str(root)] = {**real, "value": "0" * 64}
elif fault == "disk_changes_after_takeover":
    startup_protocol._LOADED[str(root)] = real
    original = startup_protocol.code_identity
    startup_protocol.code_identity = lambda r: {**original(r), "value": "1" * 64}
runpy.run_path(bridge_path, run_name="__main__")
"""


def git(*args):
    subprocess.run(["git", *args], check=True, capture_output=True)


def copy_code(target: Path) -> Path:
    for relative in startup_protocol.CODE_FILES:
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(PLUGIN / relative, destination)
    return target


class CodeIdentityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_every_listed_module_exists_in_the_plugin(self):
        missing = [relative for relative in startup_protocol.CODE_FILES if not (PLUGIN / relative).is_file()]
        self.assertEqual(missing, [])
        self.assertIsInstance(startup_protocol.code_identity(PLUGIN)["value"], str)

    def test_identical_bytes_elsewhere_match_and_guides_are_not_code(self):
        installed = startup_protocol.code_identity(PLUGIN)
        reinstalled = copy_code(self.root / "reinstalled")
        guide = reinstalled / "skills" / "codex-claude-orchestrator" / "references" / "guide.md"
        guide.parent.mkdir(parents=True)
        guide.write_text("first guide\n")
        before = startup_protocol.code_identity(reinstalled)
        self.assertEqual(before["value"], installed["value"])
        self.assertIsNone(startup_protocol.mismatch(installed, before))
        guide.write_text("updated guide only\n")
        self.assertIsNone(startup_protocol.mismatch(installed, startup_protocol.code_identity(reinstalled)))

    def test_changed_or_missing_code_is_a_mismatch(self):
        installed = startup_protocol.code_identity(PLUGIN)
        mixed = copy_code(self.root / "mixed")
        target = mixed / "skills" / "codex-claude-orchestrator" / "scripts" / "bridge.py"
        target.write_bytes(target.read_bytes() + b"\n# replaced by a different release\n")
        problem = startup_protocol.mismatch(installed, startup_protocol.code_identity(mixed))
        self.assertIn("skills/codex-claude-orchestrator/scripts/bridge.py", problem)
        target.unlink()
        broken = startup_protocol.code_identity(mixed)
        self.assertIsNone(broken["value"])
        self.assertIn("cannot be read", startup_protocol.mismatch(installed, broken))
        self.assertIn("unavailable", startup_protocol.mismatch(broken, installed))
        with self.assertRaisesRegex(RuntimeError, "restart or reconnect"):
            startup_protocol.require_unchanged(installed, mixed)


class StartupHandoffTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.task_log = self.root / "claude-task.log"
        self.enterContext(mock.patch.dict(os.environ, {
            "CLAUDE_ORCHESTRATOR_CLI_ROOT": str(self.root / "isolated-cli-store"),
            "CLAUDE_CONFIG_DIR": str(self.root / "claude-config"),
            "STARTUP_TASK_LOG": str(self.task_log)}))
        for name in ("CODEX_CLAUDE_LANE_FD", "CODEX_BRIDGE_LIFECYCLE_FILE", "CODEX_BRIDGE_CANCEL_FILE"):
            os.environ.pop(name, None)
        self.repo = self.root / "repo"; self.repo.mkdir()
        (self.repo / "base.txt").write_text("base\n")
        git("init", "-q", str(self.repo))
        git("-C", str(self.repo), "config", "user.email", "t@example.invalid")
        git("-C", str(self.repo), "config", "user.name", "T")
        git("-C", str(self.repo), "add", ".")
        git("-C", str(self.repo), "commit", "-qm", "base")
        self.requirement = self.root / "spec.md"; self.requirement.write_text("spec\n")
        self.fake = self.root / "claude"; self.fake.write_text(FAKE_CLI); self.fake.chmod(0o755)
        self.enterContext(mock.patch.dict(os.environ, {"CLAUDE_BIN": str(self.fake)}))
        self.lane = bridge.lane_identity(self.repo)
        self.addCleanup(lambda: [path.unlink(missing_ok=True) for path, _ in bridge.unknown_markers(self.lane)])
        self.runtime = Runtime(self.root / "state")
        self.addCleanup(self.runtime.close)

    def packet(self, revision=1, task="identity"):
        return {"task_id": task, "revision": revision, "role": "review", "cwd": str(self.repo), "objective": "fixture",
                "requirement_sources": [str(self.requirement)], "constraints": [], "acceptance": [],
                "owned_files": [], "protected_files": [], "model": "fixture", "effort": "low"}

    def finish(self, run_id):
        page = self.runtime.wait(run_id, timeout=8)
        while page["snapshot"]["status"] not in TERMINAL:
            page = self.runtime.wait(run_id, after=page["next_cursor"], timeout=5)
        deadline = time.monotonic() + 5
        while run_id in self.runtime._workers and time.monotonic() < deadline:
            time.sleep(.02)
        return self.runtime.snapshot(run_id)

    def start_through(self, argv_prefix):
        original = subprocess.Popen

        def popen(command, *args, **kwargs):
            if isinstance(command, list) and startup_protocol.NONCE_ARGUMENT in command:
                command = [sys.executable, *argv_prefix, *command[1:]]
            return original(command, *args, **kwargs)

        with patch.object(subprocess, "Popen", new=popen):
            return self.runtime.start(self.packet())["run_id"]

    def lifecycle(self, run_id):
        return json.loads(self.runtime._lifecycle_path(run_id).read_text())

    def bridge_run(self, packet_path, run_dir, *extra, lifecycle=None):
        env = dict(os.environ)
        if lifecycle is not None:
            env["CODEX_BRIDGE_LIFECYCLE_FILE"] = str(lifecycle)
        return subprocess.run([sys.executable, str(BRIDGE), "run", "--packet", str(packet_path), "--run-dir", str(run_dir),
                               "--timeout", "10", *extra], env=env, capture_output=True, text=True, timeout=60)

    def test_mixed_code_is_rejected_before_any_run_state_exists(self):
        mixed = copy_code(self.root / "mixed")
        changed = mixed / "skills" / "codex-claude-orchestrator" / "scripts" / "runtime.py"
        changed.write_bytes(changed.read_bytes() + b"\n# a release loaded before the reinstall\n")
        with patch.object(runtime_module, "_LOADED_CODE", startup_protocol.code_identity(mixed)):
            with self.assertRaisesRegex(RuntimeError, "restart or reconnect"):
                self.runtime.start(self.packet())
        self.assertEqual(self.runtime._registry().get("runs", {}), {})
        self.assertEqual(list(self.runtime.packets_root.iterdir()), [])
        receipts = self.runtime.state_root / "bridge-receipts"
        self.assertFalse(receipts.exists() and any(receipts.iterdir()))
        self.assertEqual(bridge.unknown_markers(self.lane), [])
        self.assertNotIn(self.lane, _LOCAL_LANES)
        self.assertFalse(self.task_log.exists())

        identical = startup_protocol.code_identity(copy_code(self.root / "identical-reinstall"))
        with patch.object(runtime_module, "_LOADED_CODE", identical):
            run_id = self.runtime.start(self.packet())["run_id"]
        self.assertEqual(self.finish(run_id)["status"], "reported")
        self.assertEqual(self.runtime._registry()["runs"][run_id]["bridge_code_identity"], identical["value"])

    def test_child_that_loaded_different_code_never_takes_over_or_dispatches(self):
        run_id = self.start_through(["-c", HANDOFF_SHIM, "loaded_differs"])
        final = self.finish(run_id)
        self.assertEqual(final["status"], "unknown")
        self.assertEqual(self.lifecycle(run_id)["phase"], startup_protocol.PRE_SPAWN_PHASE)
        stderr = (self.runtime.logs_root / f"{run_id}.stderr.log").read_text()
        self.assertIn("bridge code differs from the code Runtime verified", stderr)
        self.assertFalse(self.task_log.exists())
        self.assertFalse(os.path.lexists(self.runtime.runs_root / run_id))
        detail = self.runtime.inspect_recovery(run_id)
        self.assertTrue(detail["eligible"], detail)

    def test_code_replaced_after_takeover_stops_before_launch_intent(self):
        run_id = self.start_through(["-c", HANDOFF_SHIM, "disk_changes_after_takeover"])
        final = self.finish(run_id)
        self.assertEqual(final["status"], "failed", final)
        self.assertEqual(final["terminal_evidence"], "bridge_lifecycle")
        lifecycle = self.lifecycle(run_id)
        self.assertEqual((lifecycle["phase"], lifecycle["child_started"]), ("pre_dispatch", False))
        self.assertEqual(lifecycle["startup_nonce"], self.runtime._registry()["runs"][run_id]["startup_nonce"])
        error = json.loads((self.runtime.runs_root / run_id / "error.json").read_text())
        self.assertIn("differs from the loaded code", error["error"])
        self.assertFalse((self.runtime.runs_root / run_id / "child.json").exists())
        self.assertFalse(self.task_log.exists())
        self.assertEqual(bridge.unknown_markers(self.lane), [])

    def test_handoff_refuses_unpaired_missing_malformed_and_replayed_records(self):
        packet_path = self.root / "standalone.json"
        packet_path.write_text(json.dumps(self.packet(task="standalone")))
        nonce = "a" * 32
        no_sidecar = self.bridge_run(packet_path, self.root / "no-sidecar", startup_protocol.NONCE_ARGUMENT, nonce)
        self.assertEqual(no_sidecar.returncode, 2, no_sidecar.stderr)
        self.assertIn("requires the Runtime lifecycle file", no_sidecar.stderr)
        self.assertFalse((self.root / "no-sidecar").exists())

        absent = self.root / "absent-lifecycle.json"
        missing = self.bridge_run(packet_path, self.root / "missing", startup_protocol.NONCE_ARGUMENT, nonce, lifecycle=absent)
        self.assertEqual(missing.returncode, 2, missing.stderr)
        self.assertIn("missing, corrupt or already taken over", missing.stderr)
        self.assertFalse(absent.exists())
        self.assertFalse((self.root / "missing").exists())

        run_id = self.runtime.start(self.packet())["run_id"]
        self.assertEqual(self.finish(run_id)["status"], "reported")
        record = self.runtime._registry()["runs"][run_id]
        lifecycle_path = self.runtime._lifecycle_path(run_id)
        settled = lifecycle_path.read_bytes()
        cli = ["--cli-descriptor", record["cli_descriptor_file"], "--cli-descriptor-sha256", record["cli_descriptor_sha256"]]
        for name, value, message in (("malformed", "not-hex", "malformed"),
                                     ("replayed", record["startup_nonce"], "already taken over")):
            with self.subTest(name):
                got = self.bridge_run(self.runtime._packet_path(run_id), self.root / f"{name}-run", *cli,
                                      startup_protocol.NONCE_ARGUMENT, value, lifecycle=lifecycle_path)
                self.assertEqual(got.returncode, 2, got.stderr)
                self.assertIn(message, got.stderr)
                self.assertEqual(lifecycle_path.read_bytes(), settled)
                self.assertFalse((self.root / f"{name}-run").exists())
        self.assertEqual(self.task_log.read_text().count("\n"), 1)

    def test_a_bridge_without_a_requested_protocol_argument_exits_before_the_lifecycle(self):
        # A pre-protocol bridge receives --startup-nonce exactly like this one
        # receives an argument it does not implement: argparse exits first.
        packet_path = self.root / "future.json"
        packet_path.write_text(json.dumps(self.packet(task="future")))
        lifecycle = self.root / "future-lifecycle.json"
        lifecycle.write_text(json.dumps({"phase": startup_protocol.PRE_SPAWN_PHASE, "author": "runtime"}))
        before = lifecycle.read_bytes()
        got = self.bridge_run(packet_path, self.root / "future-run", "--startup-nonce-v2", "a" * 32, lifecycle=lifecycle)
        self.assertEqual(got.returncode, 2, got.stderr)
        self.assertIn("unrecognized arguments", got.stderr)
        self.assertEqual(lifecycle.read_bytes(), before)
        self.assertFalse((self.root / "future-run").exists())

    def test_pre_spawn_record_is_unconfirmed_for_readers_without_the_protocol(self):
        run_id = self.start_through(["-c", "import sys; sys.exit(1)"])
        self.assertEqual(self.finish(run_id)["status"], "unknown")
        lifecycle = self.lifecycle(run_id)
        # Baseline 7dd38ca accepts lifecycle evidence only for phase
        # pre_dispatch with child_started False, child_started True, or a
        # terminal record; this phase matches none of them.
        self.assertIsNone(lifecycle["child_started"])
        self.assertIs(lifecycle["terminal"], False)
        self.assertNotIn(lifecycle["phase"], {"pre_dispatch", "terminal"})
        self.assertIsNone(self.runtime.snapshot(run_id)["claude_started"])

        def as_pre_protocol(data):
            for key in ("startup_nonce", "startup_protocol_version", "bridge_code_identity"):
                data["runs"][run_id].pop(key, None)
        self.runtime._update(as_pre_protocol)
        record = self.runtime._registry()["runs"][run_id]
        self.assertIsNone(self.runtime._trusted_terminal(record))
        detail = self.runtime.inspect_recovery(run_id)
        self.assertFalse(detail["eligible"])
        self.assertEqual(detail["child"]["state"], "unconfirmed")
        self.assertEqual(detail["workspace"]["reason"], "packet.json is missing or malformed")


if __name__ == "__main__":  # pragma: no cover
    unittest.main(verbosity=2)
