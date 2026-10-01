"""Bridge startup failures before run_dir exists, and their recovery boundaries."""
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from unittest import mock
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "codex-claude-orchestrator" / "scripts"
BRIDGE = SCRIPTS / "bridge.py"
sys.path.insert(0, str(SCRIPTS))
from runtime import Runtime  # noqa: E402
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
print(json.dumps({'type':'result','subtype':'success','session_id':s,'structured_output':{'status':'completed','summary':'done','evidence':[],'checks':[],'unresolved':[]}}), flush=True)
"""
TERMINAL = {"reported", "failed", "blocked", "cancelled", "timeout", "unknown"}
# Runs an entry point after deleting the directory the process is standing in.
DELETED_CWD_SHIM = ("import os,runpy,sys; d=sys.argv[1]; os.chdir(d); os.rmdir(d); "
                    "sys.path[:]=[p for p in sys.path if p]; sys.path.insert(0, os.path.dirname(sys.argv[2])); "
                    "sys.argv=sys.argv[2:]; runpy.run_path(sys.argv[0], run_name='__main__')")
EARLY_DEATH = "import sys; sys.stderr.write('fixture interpreter death before bridge import\\n'); sys.exit(1)"


def git(*args):
    subprocess.run(["git", *args], check=True, capture_output=True)


class StartupRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.task_log = self.root / "claude-task.log"
        self.enterContext(mock.patch.dict(os.environ, {
            "CLAUDE_ORCHESTRATOR_CLI_ROOT": str(self.root / "isolated-cli-store"),
            "CLAUDE_CONFIG_DIR": str(self.root / "claude-config"),
            "STARTUP_TASK_LOG": str(self.task_log)}))
        os.environ.pop("CODEX_CLAUDE_LANE_FD", None)
        self.repo = self.root / "repo"
        (self.repo / "pkg").mkdir(parents=True)
        (self.repo / "base.txt").write_text("base\n")
        (self.repo / "pkg" / "keep.txt").write_text("keep\n")
        git("init", "-q", str(self.repo))
        git("-C", str(self.repo), "config", "user.email", "t@example.invalid")
        git("-C", str(self.repo), "config", "user.name", "T")
        git("-C", str(self.repo), "add", ".")
        git("-C", str(self.repo), "commit", "-qm", "base")
        self.requirement = self.root / "spec.md"; self.requirement.write_text("spec\n")
        self.fake = self.root / "claude"; self.fake.write_text(FAKE_CLI); self.fake.chmod(0o755)
        self.enterContext(mock.patch.dict(os.environ, {"CLAUDE_BIN": str(self.fake)}))
        self.lane = bridge.lane_identity(self.repo)
        self.addCleanup(self.remove_lane_markers)
        self.runtime = Runtime(self.root / "state")
        self.addCleanup(self.runtime.close)

    def remove_lane_markers(self):
        for path, _ in bridge.unknown_markers(self.lane):
            path.unlink(missing_ok=True)

    def packet(self, revision=1, cwd=None, task="startup"):
        return {"task_id": task, "revision": revision, "role": "review", "cwd": str(cwd or self.repo),
                "objective": "fixture", "requirement_sources": [str(self.requirement)], "constraints": [],
                "acceptance": [], "owned_files": [], "protected_files": [], "model": "fixture", "effort": "low"}

    def finish(self, run_id, runtime=None):
        runtime = runtime or self.runtime
        page = runtime.wait(run_id, timeout=8)
        while page["snapshot"]["status"] not in TERMINAL:
            page = runtime.wait(run_id, after=page["next_cursor"], timeout=5)
        deadline = time.monotonic() + 5
        while run_id in runtime._workers and time.monotonic() < deadline:
            time.sleep(.02)
        return runtime.snapshot(run_id)

    def start_with_bridge(self, code, **packet_args):
        """Start through Runtime but execute code in place of the bridge entry point."""
        original = subprocess.Popen
        captured = {}

        def popen(command, *args, **kwargs):
            if isinstance(command, list) and startup_protocol.NONCE_ARGUMENT in command:
                captured.update(command=list(command), kwargs=kwargs)
                command = [sys.executable, "-c", code, *command[1:]]
            return original(command, *args, **kwargs)

        with patch.object(subprocess, "Popen", new=popen):
            run_id = self.runtime.start(self.packet(**packet_args))["run_id"]
        return run_id, captured

    def early_death(self, **packet_args):
        run_id, _ = self.start_with_bridge(EARLY_DEATH, **packet_args)
        final = self.finish(run_id)
        self.assertEqual(final["status"], "unknown", final)
        return run_id

    def record(self, run_id):
        return self.runtime._registry()["runs"][run_id]

    def lifecycle(self, run_id):
        return json.loads(self.runtime._lifecycle_path(run_id).read_text())

    def test_bridge_starts_in_state_root_while_claude_keeps_the_packet_cwd(self):
        original = subprocess.Popen
        captured = {}

        def popen(command, *args, **kwargs):
            if isinstance(command, list) and startup_protocol.NONCE_ARGUMENT in command:
                captured.update(kwargs)
            return original(command, *args, **kwargs)

        with patch.object(subprocess, "Popen", new=popen):
            run_id = self.runtime.start(self.packet(cwd=self.repo / "pkg"))["run_id"]
        self.assertEqual(self.finish(run_id)["status"], "reported")
        self.assertEqual(captured["cwd"], str(self.runtime.state_root))
        self.assertEqual(self.task_log.read_text().splitlines(), [str((self.repo / "pkg").resolve())])
        lifecycle = self.lifecycle(run_id)
        self.assertEqual(lifecycle["startup_nonce"], self.record(run_id)["startup_nonce"])
        self.assertEqual(lifecycle["taken_over_from"], startup_protocol.PRE_SPAWN_PHASE)
        self.assertEqual(lifecycle["phase"], "terminal")

    def test_runtime_whose_process_cwd_was_deleted_still_dispatches(self):
        doomed = self.root / "doomed-mcp-cwd"; doomed.mkdir()
        state = self.root / "deleted-cwd-state"
        owner = textwrap.dedent(f"""
            import json, os, sys, time
            sys.path.insert(0, {str(SCRIPTS)!r})
            from runtime import Runtime
            doomed = {str(doomed)!r}
            os.chdir(doomed); os.rmdir(doomed)
            sys.path[:] = [p for p in sys.path if p]
            try:
                os.getcwd(); cwd_state = "present"
            except OSError:
                cwd_state = "deleted"
            runtime = Runtime({str(state)!r})
            run = runtime.start({self.packet(task="deleted-mcp-cwd")!r}, timeout=30)
            deadline = time.monotonic() + 30
            snap = runtime.snapshot(run["run_id"])
            while snap["status"] not in {sorted(TERMINAL)!r} and time.monotonic() < deadline:
                time.sleep(.05); snap = runtime.snapshot(run["run_id"])
            print(json.dumps({{"cwd_state": cwd_state, "status": snap["status"], "summary": snap.get("summary")}}), flush=True)
            runtime.close()
        """)
        got = subprocess.run([sys.executable, "-c", owner], capture_output=True, text=True, timeout=90)
        self.assertEqual(got.returncode, 0, got.stderr)
        outcome = json.loads(got.stdout.strip().splitlines()[-1])
        self.assertEqual(outcome["cwd_state"], "deleted")
        self.assertEqual(outcome["status"], "reported", outcome)

    def test_bridge_entry_points_do_not_read_a_deleted_parent_cwd(self):
        def in_deleted_cwd(*argv):
            doomed = Path(tempfile.mkdtemp(dir=self.root))
            return subprocess.run([sys.executable, "-c", DELETED_CWD_SHIM, str(doomed), str(BRIDGE), *argv],
                                  capture_output=True, text=True, timeout=60)

        status = in_deleted_cwd("status", "--run-dir", str(self.root / "missing-run"))
        self.assertEqual(status.returncode, 2, status.stderr)
        self.assertIn("run-dir has no bridge state", status.stderr)
        self.assertNotIn("Traceback", status.stderr)

        packet_path = self.root / "standalone.json"
        packet_path.write_text(json.dumps(self.packet(task="standalone-deleted-cwd")))
        run_dir = self.root / "standalone-run"
        run = in_deleted_cwd("run", "--packet", str(packet_path), "--run-dir", str(run_dir), "--timeout", "20")
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(json.loads((run_dir / "receipt.json").read_text())["status"], "reported")

        implicit = in_deleted_cwd("doctor")
        self.assertEqual(implicit.returncode, 2, implicit.stderr)
        self.assertIn("current directory is unavailable", implicit.stderr)
        self.assertIn("--cwd", implicit.stderr)
        self.assertNotIn("Traceback", implicit.stderr)

        explicit = in_deleted_cwd("doctor", "--cwd", str(self.repo))
        self.assertNotIn("bridge error", explicit.stderr)
        self.assertEqual(json.loads(explicit.stdout)["cwd"], str(self.repo.resolve()))

        default = subprocess.run([sys.executable, str(BRIDGE), "doctor"], cwd=self.repo,
                                 capture_output=True, text=True, timeout=60)
        self.assertNotIn("bridge error", default.stderr)
        self.assertEqual(json.loads(default.stdout)["cwd"], str(self.repo.resolve()))
        self.assertEqual(default.returncode, explicit.returncode)

    def test_bridge_death_before_run_dir_is_recoverable_without_fabricated_run_evidence(self):
        run_id = self.early_death()
        run_dir = self.runtime.runs_root / run_id
        self.assertFalse(os.path.lexists(run_dir))
        self.assertEqual(self.lifecycle(run_id)["phase"], startup_protocol.PRE_SPAWN_PHASE)
        self.assertFalse(self.task_log.exists(), "no Claude task may run when the bridge never started")
        self.assertEqual([value["run_id"] for _, value in bridge.unknown_markers(self.lane)], [run_id])
        self.assertEqual(self.record(run_id)["bridge_logs"]["stderr"]["state"], "present")
        with self.assertRaisesRegex(RuntimeError, "unknown"):
            self.runtime.start(self.packet(revision=2))

        detail = self.runtime.inspect_recovery(run_id)
        self.assertTrue(detail["eligible"], detail)
        self.assertFalse(detail["run_dir_present"])
        self.assertIn("pre-spawn", detail["child"]["reason"])
        self.assertEqual(detail["workspace"]["packet_source"], "dispatched_packet")
        recovered = self.runtime.reconcile(run_id, "bridge died before import", ["bridge group absent"],
                                           detail["workspace"]["digest"])
        self.assertEqual(recovered["status"], "unknown")
        reconciliation = recovered["reconciliation"]
        self.assertEqual(reconciliation["outcome"], "confirmed_stopped")
        self.assertFalse(reconciliation["run_dir_present"])
        receipt = Path(reconciliation["receipt_file"])
        self.assertEqual(receipt, self.runtime.state_root / "recovery-receipts" / f"{run_id}.json")
        self.assertEqual(json.loads(receipt.read_text()), reconciliation)
        self.assertEqual(reconciliation["lifecycle_sha256"],
                         hashlib.sha256(self.runtime._lifecycle_path(run_id).read_bytes()).hexdigest())
        self.assertFalse(os.path.lexists(run_dir), "reconciliation must not create an execution run_dir")
        self.assertEqual(bridge.unknown_markers(self.lane), [])

        fresh = self.runtime.start(self.packet(revision=2))
        self.assertEqual(self.finish(fresh["run_id"])["status"], "reported")
        self.assertEqual(self.runtime.snapshot(run_id)["status"], "unknown")

    def test_marker_unlink_failure_retries_only_cleanup_and_keeps_run_dir_absent(self):
        run_id = self.early_death()
        digest = self.runtime.inspect_recovery(run_id)["workspace"]["digest"]
        marker = bridge.unknown_markers(self.lane)[0][0]
        original_unlink = Path.unlink
        failed = []

        def fail_once(path, *args, **kwargs):
            if path == marker and not failed:
                failed.append(path)
                raise OSError("fixture marker unlink failure")
            return original_unlink(path, *args, **kwargs)

        with patch.object(Path, "unlink", fail_once):
            with self.assertRaisesRegex(OSError, "unlink failure"):
                self.runtime.reconcile(run_id, "checked", ["fixture evidence"], digest)
        persisted = self.record(run_id)["reconciliation"]
        receipt = Path(persisted["receipt_file"])
        receipt_bytes = receipt.read_bytes()
        self.assertTrue(marker.exists())
        self.assertFalse(os.path.lexists(self.runtime.runs_root / run_id))
        with self.assertRaisesRegex(RuntimeError, "unknown"):
            self.runtime.start(self.packet(revision=2))

        retried = self.runtime.reconcile(run_id, "different retry text", ["not a new fact"], digest)
        self.assertEqual(retried["reconciliation"], persisted)
        self.assertEqual(receipt.read_bytes(), receipt_bytes)
        self.assertFalse(marker.exists())
        self.assertFalse(os.path.lexists(self.runtime.runs_root / run_id))
        fresh = self.runtime.start(self.packet(revision=2))
        self.assertEqual(self.finish(fresh["run_id"])["status"], "reported")

    def test_reconciliation_write_failure_releases_nothing(self):
        run_id = self.early_death()
        digest = self.runtime.inspect_recovery(run_id)["workspace"]["digest"]
        receipt = self.runtime._recovery_receipt_path(run_id)
        original_dump = bridge.dump

        def fail_receipt(path, value):
            if Path(path) == receipt:
                raise OSError("fixture receipt write failure")
            return original_dump(path, value)

        with patch.object(bridge, "dump", fail_receipt):
            with self.assertRaisesRegex(OSError, "receipt write failure"):
                self.runtime.reconcile(run_id, "checked", ["fixture evidence"], digest)
        self.assertIsNone(self.record(run_id).get("reconciliation"))
        self.assertEqual([value["run_id"] for _, value in bridge.unknown_markers(self.lane)], [run_id])
        self.assertFalse(receipt.exists())
        with self.assertRaisesRegex(RuntimeError, "unknown"):
            self.runtime.start(self.packet(revision=2))
        recovered = self.runtime.reconcile(run_id, "checked again", ["fixture evidence"], digest)
        self.assertEqual(recovered["reconciliation"]["outcome"], "confirmed_stopped")
        self.assertEqual(bridge.unknown_markers(self.lane), [])

    def test_workspace_change_after_inspection_invalidates_the_digest(self):
        run_id = self.early_death()
        stale = self.runtime.inspect_recovery(run_id)["workspace"]["digest"]
        (self.repo / "base.txt").write_text("changed after inspection\n")
        with self.assertRaisesRegex(RuntimeError, "workspace changed"):
            self.runtime.reconcile(run_id, "stale", ["fixture"], stale)
        self.assertIsNone(self.record(run_id).get("reconciliation"))
        self.assertEqual(len(bridge.unknown_markers(self.lane)), 1)
        fresh = self.runtime.inspect_recovery(run_id)
        self.assertTrue(fresh["eligible"], fresh)
        self.assertNotEqual(fresh["workspace"]["digest"], stale)

    def test_existing_run_dir_without_packet_copy_keeps_the_original_refusal(self):
        run_id = self.early_death()
        (self.runtime.runs_root / run_id).mkdir()
        detail = self.runtime.inspect_recovery(run_id)
        self.assertFalse(detail["eligible"])
        self.assertTrue(detail["run_dir_present"])
        self.assertEqual(detail["workspace"]["reason"], "packet.json is missing or malformed")
        # A protocol bridge creates run_dir only after taking the record over.
        self.assertEqual(detail["child"]["state"], "unconfirmed")
        with self.assertRaises(RuntimeError):
            self.runtime.reconcile(run_id, "unbound", ["fixture"], "0" * 64)
        self.assertEqual(len(bridge.unknown_markers(self.lane)), 1)

    def test_tampered_or_missing_startup_bindings_stay_unconfirmed(self):
        run_id = self.early_death()
        self.assertTrue(self.runtime.inspect_recovery(run_id)["eligible"])
        lifecycle_path = self.runtime._lifecycle_path(run_id)
        packet_path = self.runtime._packet_path(run_id)
        cli_path = self.runtime.packets_root / f"{run_id}.cli.json"
        originals = {path: path.read_bytes() for path in (lifecycle_path, packet_path, cli_path, self.runtime.registry_path)}

        def rewrite_lifecycle(**changes):
            value = json.loads(originals[lifecycle_path]); value.update(changes)
            lifecycle_path.write_text(json.dumps(value))

        def edit_record(mutate):
            data = json.loads(originals[self.runtime.registry_path]); mutate(data["runs"][run_id])
            self.runtime.registry_path.write_text(json.dumps(data))

        cases = {
            "lifecycle nonce": (lambda: rewrite_lifecycle(startup_nonce="f" * 32), "child"),
            "lifecycle code identity": (lambda: rewrite_lifecycle(code_identity="0" * 64), "child"),
            "lifecycle packet binding": (lambda: rewrite_lifecycle(packet_sha256="0" * 64), "child"),
            "lifecycle CLI binding": (lambda: rewrite_lifecycle(cli_descriptor_sha256="0" * 64), "child"),
            "lifecycle protocol version": (lambda: rewrite_lifecycle(startup_protocol_version=99), "child"),
            "lifecycle lane": (lambda: rewrite_lifecycle(lane_identity=str(self.root)), "child"),
            "lifecycle corrupt": (lambda: lifecycle_path.write_text("{not json"), "child"),
            "lifecycle missing": (lambda: lifecycle_path.unlink(), "child"),
            "registry nonce removed": (lambda: edit_record(lambda rec: rec.pop("startup_nonce")), "child"),
            "registry nonce invalid": (lambda: edit_record(lambda rec: rec.update(startup_nonce=None)), "child"),
            "outer packet changed": (lambda: packet_path.write_text(originals[packet_path].decode() + " "), "workspace"),
            "CLI descriptor changed": (lambda: cli_path.write_text(originals[cli_path].decode() + " "), "workspace"),
            "bridge identity never registered": (lambda: edit_record(lambda rec: [rec.pop(key, None) for key in
                                                 ("bridge_pid", "bridge_process_group", "bridge_identity")]), "bridge"),
            "bridge group alive": (lambda: edit_record(lambda rec: rec.update(bridge_process_group=os.getpgrp(),
                                                                              bridge_identity=None)), "bridge"),
        }
        for name, (tamper, gate) in cases.items():
            with self.subTest(name):
                tamper()
                try:
                    detail = self.runtime.inspect_recovery(run_id)
                    self.assertFalse(detail["eligible"], detail)
                    if gate == "workspace":
                        self.assertEqual(detail["workspace"]["state"], "unconfirmed")
                    else:
                        self.assertNotEqual(detail[gate]["state"], "stopped")
                    with self.assertRaises(RuntimeError):
                        self.runtime.reconcile(run_id, name, ["fixture"], detail["workspace"].get("digest") or "0" * 64)
                finally:
                    for path, value in originals.items():
                        path.write_bytes(value)
                self.assertIsNone(self.record(run_id).get("reconciliation"))
                self.assertEqual(len(bridge.unknown_markers(self.lane)), 1)
        self.assertTrue(self.runtime.inspect_recovery(run_id)["eligible"])

    def test_bridge_written_phases_after_takeover_keep_launch_uncertainty(self):
        run_id = self.early_death()
        lifecycle_path = self.runtime._lifecycle_path(run_id)
        pre_spawn = json.loads(lifecycle_path.read_text())
        record = self.record(run_id)
        taken_over = {"schema_version": 1, "run_id": run_id, "task_id": record["task_id"], "revision": record["revision"],
                      "cwd": record["cwd"], "packet_sha256": record["packet_sha256"],
                      "cli_descriptor_sha256": record["cli_descriptor_sha256"], "bridge_pid": 99999991,
                      "startup_nonce": record["startup_nonce"], "startup_protocol_version": 1,
                      "code_identity": pre_spawn["code_identity"], "lane_identity": record["lane_identity"],
                      "taken_over_from": startup_protocol.PRE_SPAWN_PHASE, "terminal": False, "status": "starting"}
        cases = (
            # Bridge took over and was killed before mkdir: bound pre-dispatch proof.
            ("bound pre_dispatch", "pre_dispatch", False, taken_over["startup_nonce"], True),
            # Launch intent persisted, or Popen returned before child identity was written.
            ("launch_intent", "launch_intent", None, taken_over["startup_nonce"], False),
            # The same pre-dispatch shape from a bridge that did not carry this run's nonce.
            ("pre_dispatch without nonce", "pre_dispatch", False, None, False),
            ("pre_dispatch with foreign nonce", "pre_dispatch", False, "e" * 32, False),
        )
        for name, phase, started, nonce, eligible in cases:
            with self.subTest(name):
                value = {**taken_over, "phase": phase, "child_started": started, "startup_nonce": nonce}
                if nonce is None:
                    del value["startup_nonce"]
                lifecycle_path.write_text(json.dumps(value))
                detail = self.runtime.inspect_recovery(run_id)
                self.assertEqual(detail["eligible"], eligible, detail)
                if not eligible:
                    self.assertIn("launch is unconfirmed", "; ".join(detail["blocking_reasons"]))
        lifecycle_path.write_text(json.dumps(pre_spawn))
        self.assertTrue(self.runtime.inspect_recovery(run_id)["eligible"])

    def test_takeover_retains_all_bindings_even_when_child_file_reports_stopped(self):
        run_id = self.early_death()
        path = self.runtime._lifecycle_path(run_id)
        original = self.lifecycle(run_id)
        taken = {**original, "phase": "pre_dispatch", "child_started": False}
        run_dir = self.runtime.runs_root / run_id
        run_dir.mkdir()
        (run_dir / "packet.json").write_bytes(self.runtime._packet_path(run_id).read_bytes())
        (run_dir / "cli-selection.json").write_bytes((self.runtime.packets_root / f"{run_id}.cli.json").read_bytes())
        path.write_text(json.dumps(taken))
        self.assertTrue(self.runtime.inspect_recovery(run_id)["eligible"])
        for child_file in (False, True):
            if child_file:
                (run_dir / "child.json").write_text(json.dumps({"pid": 99999992, "process_group": 99999992}))
            for key, value in (("startup_nonce", "0" * 32), ("code_identity", "0" * 64),
                               ("lane_identity", "/wrong/lane"), ("startup_protocol_version", 999)):
                with self.subTest(child_file=child_file, key=key):
                    path.write_text(json.dumps({**taken, key: value}))
                    detail = self.runtime.inspect_recovery(run_id)
                    self.assertFalse(detail["eligible"], detail)
                    path.write_text(json.dumps({**taken, key: value, "terminal": True, "status": "failed"}))
                    self.assertIsNone(self.runtime._trusted_terminal(self.record(run_id)))
        path.write_text(json.dumps(taken))
        self.runtime._update(lambda data: data["runs"][run_id].pop("startup_nonce"))
        self.assertFalse(self.runtime.inspect_recovery(run_id)["eligible"])
        self.assertIsNone(self.runtime._trusted_terminal(self.record(run_id)))

    def test_existing_execution_packet_and_cli_copy_must_match_in_full(self):
        run_id = self.early_death()
        path = self.runtime._lifecycle_path(run_id)
        taken = {**self.lifecycle(run_id), "phase": "pre_dispatch", "child_started": False}
        path.write_text(json.dumps(taken))
        run_dir = self.runtime.runs_root / run_id
        run_dir.mkdir()
        packet_path = run_dir / "packet.json"
        packet_bytes = self.runtime._packet_path(run_id).read_bytes()
        packet_path.write_bytes(packet_bytes)
        cli_path = run_dir / "cli-selection.json"
        cli_bytes = (self.runtime.packets_root / f"{run_id}.cli.json").read_bytes()
        cli_path.write_bytes(cli_bytes)
        self.assertTrue(self.runtime.inspect_recovery(run_id)["eligible"])
        changed = json.loads(packet_bytes)
        changed["objective"] = "different dispatched input"
        packet_path.write_text(json.dumps(changed))
        self.assertFalse(self.runtime.inspect_recovery(run_id)["eligible"])
        packet_path.write_bytes(packet_bytes)
        cli_path.write_text("{}")
        self.assertFalse(self.runtime.inspect_recovery(run_id)["eligible"])
        cli_path.write_bytes(cli_bytes)
        self.assertTrue(self.runtime.inspect_recovery(run_id)["eligible"])

    def test_receipt_survives_registry_commit_failure_without_rewriting_facts(self):
        run_id = self.early_death()
        digest = self.runtime.inspect_recovery(run_id)["workspace"]["digest"]
        receipt = self.runtime._recovery_receipt_path(run_id)
        replace = os.replace

        def fail_registry(source, destination):
            if Path(destination) == self.runtime.registry_path:
                raise OSError("fixture registry commit failure")
            return replace(source, destination)

        with patch.object(os, "replace", fail_registry):
            with self.assertRaisesRegex(OSError, "registry commit failure"):
                self.runtime.reconcile(run_id, "first fact", ["first evidence"], digest)
        original = receipt.read_bytes()
        self.assertIsNone(self.record(run_id).get("reconciliation"))
        self.assertEqual(len(bridge.unknown_markers(self.lane)), 1)
        # Corrupt or mismatched pending audit records cannot be replaced to pass.
        for corruption in ("{bad", json.dumps({**json.loads(original), "packet_sha256": "0" * 64})):
            with self.subTest(corruption=corruption):
                receipt.write_text(corruption)
                with self.assertRaisesRegex(RuntimeError, "existing recovery receipt"):
                    self.runtime.reconcile(run_id, "retry", ["retry evidence"], digest)
                self.assertEqual(receipt.read_text(), corruption)
                self.assertEqual(len(bridge.unknown_markers(self.lane)), 1)
        receipt.write_bytes(original)
        recovered = self.runtime.reconcile(run_id, "different retry", ["different evidence"], digest)
        self.assertEqual(receipt.read_bytes(), original)
        self.assertEqual(recovered["reconciliation"]["reason"], "first fact")
        self.assertEqual(recovered["reconciliation"]["evidence"], ["first evidence"])
        self.assertEqual(bridge.unknown_markers(self.lane), [])
        self.assertFalse((self.runtime.runs_root / run_id).exists())


    def test_startup_unknown_in_a_subdirectory_blocks_its_whole_worktree(self):
        run_id = self.early_death(cwd=self.repo / "pkg")
        for cwd in (self.repo, self.repo / "pkg"):
            with self.subTest(cwd=str(cwd)), self.assertRaisesRegex(RuntimeError, "unknown"):
                self.runtime.start(self.packet(cwd=cwd, task="other-" + cwd.name))
        other_state = Runtime(self.root / "other-state")
        self.addCleanup(other_state.close)
        with self.assertRaisesRegex(RuntimeError, "unknown"):
            other_state.start(self.packet(task="other-state"))
        detail = self.runtime.inspect_recovery(run_id)
        self.runtime.reconcile(run_id, "bridge died before import", ["fixture"], detail["workspace"]["digest"])
        fresh = other_state.start(self.packet(task="after-reconcile"))
        self.assertEqual(self.finish(fresh["run_id"], other_state)["status"], "reported")


if __name__ == "__main__":  # pragma: no cover
    unittest.main(verbosity=2)
