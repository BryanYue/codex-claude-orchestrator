"""REMOTE-F1-ISOLATION: one Git worktree is one execution lane for every cwd.

Root/subdirectory/sibling dispatches of the same worktree share lane locks,
active/unknown admission and recovery identity; distinct linked worktrees and
artifact roots stay independent.  No live model is used.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "codex-claude-orchestrator" / "scripts"
sys.path.insert(0, str(SCRIPTS))
from runtime import Runtime, _LOCAL_LANES  # noqa: E402
import bridge  # noqa: E402

BRIDGE = SCRIPTS / "bridge.py"
FAKE_CLI = """#!/usr/bin/env python3
import json, os, sys, time
a=sys.argv[1:]
if a == ['--version']: print('2.1.276'); raise SystemExit
if a == ['--help']: print('-p --model --effort --output-format --verbose --json-schema --session-id --resume --permission-mode --tools --allowedTools --disallowedTools --settings --strict-mcp-config --mcp-config --disable-slash-commands --no-session-persistence'); raise SystemExit
if a == ['auth','status','--json']: print(json.dumps({'loggedIn':True})); raise SystemExit
s=a[a.index('--session-id')+1] if '--session-id' in a else a[a.index('--resume')+1]
print(json.dumps({'type':'system','subtype':'init','session_id':s,'model':'fixture'}), flush=True)
if os.environ.get('ISOLATION_FAKE_SLEEP'):
    if os.environ.get('ISOLATION_FAKE_READY'):
        with open(os.environ['ISOLATION_FAKE_READY'], 'w') as ready: ready.write('ready')
    time.sleep(float(os.environ['ISOLATION_FAKE_SLEEP']))
sys.stdin.buffer.read()
print(json.dumps({'type':'result','subtype':'success','session_id':s,'structured_output':{'status':'completed','summary':'done','evidence':[],'checks':[],'unresolved':[]}}), flush=True)
"""
TERMINAL = {"reported", "failed", "blocked", "cancelled", "timeout", "unknown"}


def git(*args: str) -> None:
    subprocess.run(["git", *args], check=True, capture_output=True)


class WorktreeLaneTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.enterContext(mock.patch.dict(os.environ, {
            "CLAUDE_ORCHESTRATOR_CLI_ROOT": str(self.root / "isolated-cli-store"),
            "CLAUDE_CONFIG_DIR": str(self.root / "claude-config")}))
        os.environ.pop("CODEX_CLAUDE_LANE_FD", None)
        os.environ.pop("ISOLATION_FAKE_SLEEP", None)
        self.repo = self.root / "repo"
        for name in ("pkg", "sib"):
            (self.repo / name).mkdir(parents=True)
            (self.repo / name / "keep.txt").write_text("keep\n")
        git("init", "-q", str(self.repo))
        git("-C", str(self.repo), "config", "user.email", "t@example.invalid")
        git("-C", str(self.repo), "config", "user.name", "T")
        git("-C", str(self.repo), "add", ".")
        git("-C", str(self.repo), "commit", "-qm", "base")
        self.child = self.repo / "pkg"
        self.sibling = self.repo / "sib"
        self.linked = self.root / "linked"
        git("-C", str(self.repo), "worktree", "add", "-q", "-b", "linked", str(self.linked))
        self.requirement = self.root / "spec.md"; self.requirement.write_text("spec\n")
        self.fake = self.root / "claude"; self.fake.write_text(FAKE_CLI); self.fake.chmod(0o755)
        os.environ["CLAUDE_BIN"] = str(self.fake)
        self.lane = bridge.lane_identity(self.repo)
        self.linked_lane = bridge.lane_identity(self.linked)
        self.addCleanup(self.remove_lane_markers)
        self.runtime = Runtime(self.root / "state")
        self.addCleanup(self.runtime.close)

    def remove_lane_markers(self):
        for lane in (self.lane, self.linked_lane):
            for path, _ in bridge.unknown_markers(lane):
                path.unlink(missing_ok=True)

    def assert_marker_owners(self, expected):
        markers = bridge.unknown_markers(self.lane)
        roots = set(bridge.unknown_marker_roots())
        self.assertEqual(sorted(value["run_id"] for _, value in markers), sorted(expected * len(roots)))
        for owner in expected:
            self.assertEqual({path.parent for path, value in markers if value["run_id"] == owner}, roots)
        return markers

    def packet(self, cwd: Path, task: str, revision: int = 1) -> dict:
        return {"task_id": task, "revision": revision, "role": "review", "cwd": str(cwd), "objective": "fixture",
                "requirement_sources": [str(self.requirement)], "constraints": [], "acceptance": [],
                "owned_files": [], "protected_files": [], "model": "fixture", "effort": "low"}

    def finish(self, runtime: Runtime, run_id: str) -> dict:
        page = runtime.wait(run_id, timeout=8)
        while page["snapshot"]["status"] not in TERMINAL:
            page = runtime.wait(run_id, after=page["next_cursor"], timeout=5)
        deadline = time.monotonic() + 5
        while run_id in runtime._workers and time.monotonic() < deadline:
            time.sleep(.02)
        return page["snapshot"]

    def unknown_fixture(self, runtime: Runtime, cwd: Path, run_id: str, *, lane_identity: bool = True) -> None:
        """Register an unknown run whose impossible process ids are provably absent."""
        run = runtime.runs_root / run_id
        run.mkdir()
        packet = {**self.packet(cwd, "recover-" + run_id), "workspace_kind": "git", "cwd": str(cwd.resolve())}
        (run / "packet.json").write_text(json.dumps(packet))
        (run / "state.json").write_text(json.dumps({"status": "unknown", "pid": 99999991}))
        (run / "child.json").write_text(json.dumps({"pid": 99999992, "process_group": 99999992}))
        record = {"run_id": run_id, "task_id": packet["task_id"], "revision": 1, "cwd": packet["cwd"],
                  "run_dir": str(run), "status": "unknown", "phase": "unknown", "started_at": time.time(),
                  "updated_at": time.time(), "bridge_process_group": 99999991}
        if lane_identity:
            record["lane_identity"] = bridge.lane_identity(cwd)
        runtime._update(lambda data: data.setdefault("runs", {}).__setitem__(run_id, record))
        runtime._mark_unknown_lane(packet["cwd"], run_id, "fixture unknown")

    def reconcile(self, runtime: Runtime, run_id: str) -> dict:
        inspection = runtime.inspect_recovery(run_id)
        self.assertTrue(inspection["eligible"], inspection)
        return runtime.reconcile(run_id, "fixture groups absent", ["impossible fixture pids"],
                                 inspection["workspace"]["digest"])

    @staticmethod
    def wait_until(ready, message: str, timeout: float = 20):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = ready()
            if value:
                return value
            time.sleep(.03)
        raise AssertionError(message)

    @classmethod
    def read_json_when(cls, path: Path, key: str) -> dict:
        def ready():
            try:
                value = json.loads(path.read_text())
            except (OSError, ValueError):
                return None
            return value if isinstance(value, dict) and isinstance(value.get(key), int) else None
        return cls.wait_until(ready, f"fixture did not publish {path}")

    def test_double_crash_launch_intent_blocks_other_state_roots_until_reconciled(self):
        # WF-R7: Runtime A and its bridge are killed after child.json exists;
        # the Claude-shaped child lives on in its own process group.
        state_a = self.root / "state-a"
        child_ready = self.root / "child-ready"
        owner_code = textwrap.dedent(f"""
            import json, sys, time
            sys.path.insert(0, {str(SCRIPTS)!r})
            from runtime import Runtime
            run = Runtime({str(state_a)!r}).start({self.packet(self.child, 'double-crash')!r}, timeout=120)
            print(json.dumps(run), flush=True)
            time.sleep(120)
        """)
        owner = subprocess.Popen([sys.executable, "-u", "-c", owner_code], stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, text=True, env=dict(
                                     os.environ, ISOLATION_FAKE_SLEEP="120", ISOLATION_FAKE_READY=str(child_ready)))
        groups: list[int] = []

        def stop_fixture_processes():
            for group in groups:
                try:
                    os.killpg(group, signal.SIGKILL)
                except OSError:
                    pass
            if owner.poll() is None:
                owner.kill()
            owner.wait(timeout=5); owner.stdout.close(); owner.stderr.close()
        self.addCleanup(stop_fixture_processes)

        line = owner.stdout.readline()
        self.assertTrue(line, "Runtime A did not return its start snapshot")
        run_id = json.loads(line)["run_id"]
        run_dir = state_a / "runs" / run_id
        child_group = self.read_json_when(run_dir / "child.json", "process_group")["process_group"]
        groups.append(child_group)
        bridge_pid = self.read_json_when(run_dir / "state.json", "pid")["pid"]
        groups.append(bridge_pid)
        self.assertNotIn(owner.pid, (child_group, bridge_pid))
        self.assertNotEqual(child_group, bridge_pid)

        # child.json proves spawn, not that the fixture passed its initial
        # stdout write; closing that pipe first can kill it with BrokenPipe.
        self.wait_until(child_ready.exists, "child did not enter the crash-survival fixture")
        owner.kill(); owner.wait(timeout=5)
        os.kill(bridge_pid, signal.SIGKILL)
        self.wait_until(lambda: bridge.process_group_stopped(bridge_pid), "killed bridge group did not disappear")
        self.assertFalse(bridge.process_group_stopped(child_group), "the child must outlive both crashes")

        markers = self.assert_marker_owners([run_id])
        marker = markers[0][1]
        self.assertEqual((marker["lane_identity"], marker["cwd"]), (self.lane, str(self.child.resolve())))
        self.assertEqual(marker["launch_intent"]["run_dir"], str(state_a.resolve() / "runs" / run_id))
        self.assertEqual(marker["launch_intent"]["lifecycle_file"],
                         str(state_a.resolve() / "bridge-receipts" / f"{run_id}.json"))

        state_b = Runtime(self.root / "state-b")
        self.addCleanup(state_b.close)
        for cwd in (self.repo, self.sibling, self.child):
            with self.subTest(cwd=str(cwd)), self.assertRaisesRegex(RuntimeError, "unknown prior"):
                state_b.start(self.packet(cwd, "after-double-crash"))
        self.assertEqual(state_b._registry().get("runs", {}), {})
        packet_path = self.root / "standalone.json"
        packet_path.write_text(json.dumps(self.packet(self.repo, "standalone")))
        got = subprocess.run([sys.executable, str(BRIDGE), "run", "--packet", str(packet_path),
                              "--run-dir", str(self.root / "standalone-run"), "--timeout", "5"],
                             text=True, capture_output=True)
        self.assertNotEqual(got.returncode, 0)
        self.assertIn("unknown prior supervised run", got.stderr)
        self.assertFalse((self.root / "standalone-run" / "child.json").exists())
        linked = state_b.start(self.packet(self.linked, "linked-free"))
        self.assertEqual(self.finish(state_b, linked["run_id"])["status"], "reported")

        # PID evidence alone never clears the marker: the original state must
        # prove both groups stopped through inspect/reconcile.
        restarted = Runtime(state_a)
        self.addCleanup(restarted.close)
        self.assertEqual(restarted._registry()["runs"][run_id]["status"], "unknown")
        live = restarted.inspect_recovery(run_id)
        self.assertFalse(live["eligible"], live)
        self.assertNotEqual(live["child"]["state"], "stopped")
        escalated = self.assert_marker_owners([run_id])
        self.assertEqual(escalated[0][1]["state_root"], str(state_a.resolve()))
        with self.assertRaisesRegex(RuntimeError, "unknown prior"):
            state_b.start(self.packet(self.repo, "while-child-lives"))

        os.killpg(child_group, signal.SIGKILL)
        self.wait_until(lambda: bridge.process_group_stopped(child_group), "killed child group did not disappear")
        inspection = restarted.inspect_recovery(run_id)
        self.assertTrue(inspection["eligible"], inspection)
        restarted.reconcile(run_id, "fixture bridge and child groups killed", ["fixture process groups absent"],
                            inspection["workspace"]["digest"])
        self.assertEqual(bridge.unknown_markers(self.lane), [])
        after = state_b.start(self.packet(self.repo, "after-reconcile"))
        self.assertEqual(self.finish(state_b, after["run_id"])["status"], "reported")
        self.assertEqual(bridge.unknown_markers(self.lane), [], "a confirmed normal run releases its launch intent")

    def test_root_subdirectory_and_sibling_share_one_lane_but_linked_worktree_does_not(self):
        self.assertEqual(bridge.lane_identity(self.child), self.lane)
        self.assertEqual(bridge.lane_identity(self.sibling), self.lane)
        self.assertNotEqual(self.linked_lane, self.lane)
        # The original reproduction: one Runtime obtained both locks.
        held = self.runtime._lane_lock(bridge.lane_identity(self.repo))
        try:
            with self.assertRaisesRegex(RuntimeError, "already owns"):
                self.runtime._lane_lock(bridge.lane_identity(self.child))
            other = self.runtime._lane_lock(self.linked_lane)
            self.runtime._release_lane(self.linked_lane, other)
        finally:
            self.runtime._release_lane(self.lane, held)

    def test_child_run_blocks_root_and_sibling_starts_until_its_watcher_releases(self):
        os.environ["ISOLATION_FAKE_SLEEP"] = "2"
        child = self.runtime.start(self.packet(self.child, "child-task"))
        self.assertEqual(self.runtime._registry()["runs"][child["run_id"]]["lane_identity"], self.lane)
        other_state = Runtime(self.root / "other-state")
        self.addCleanup(other_state.close)
        for runtime, cwd in ((self.runtime, self.repo), (self.runtime, self.sibling), (other_state, self.repo)):
            with self.subTest(cwd=str(cwd), state=str(runtime.state_root)), \
                 self.assertRaisesRegex(RuntimeError, "already owns"):
                runtime.start(self.packet(cwd, "overlap"))
        linked = other_state.start(self.packet(self.linked, "linked-task"))
        os.environ.pop("ISOLATION_FAKE_SLEEP")
        self.assertEqual(self.finish(self.runtime, child["run_id"])["status"], "reported")
        self.assertEqual(self.finish(other_state, linked["run_id"])["status"], "reported")
        self.assertNotIn(self.lane, _LOCAL_LANES)
        root = self.runtime.start(self.packet(self.repo, "root-after"))
        self.assertEqual(self.finish(self.runtime, root["run_id"])["status"], "reported")

    def test_lane_held_by_another_process_blocks_every_cwd_of_the_worktree(self):
        holder = subprocess.Popen([sys.executable, "-c", textwrap.dedent(f"""
            import sys
            sys.path.insert(0, {str(SCRIPTS)!r})
            import bridge
            from runtime import Runtime
            rt = Runtime({str(self.root / 'holder-state')!r})
            rt._lane_lock(bridge.lane_identity(__import__('pathlib').Path({str(self.child)!r})))
            print('held', flush=True)
            sys.stdin.read()
        """)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, env=dict(os.environ))
        try:
            self.assertEqual(holder.stdout.readline().strip(), "held")
            for cwd in (self.repo, self.sibling):
                with self.subTest(cwd=str(cwd)), self.assertRaisesRegex(RuntimeError, "already owns"):
                    self.runtime.start(self.packet(cwd, "cross-process"))
            self.assertEqual(self.runtime._registry().get("runs", {}), {})
        finally:
            holder.stdin.close(); holder.wait(timeout=5); holder.stdout.close()
        run = self.runtime.start(self.packet(self.repo, "after-holder"))
        self.assertEqual(self.finish(self.runtime, run["run_id"])["status"], "reported")

    def test_inherited_and_standalone_bridge_locks_agree_on_the_worktree_lane(self):
        held = self.runtime._lane_lock(self.lane)
        linked = bridge.lock_file(self.linked_lane, "linked fixture")
        linked.close()
        try:
            with self.assertRaisesRegex(bridge.BridgeError, "active run"):
                bridge.lock_file(bridge.lane_identity(self.child), "standalone")
            with mock.patch.dict(os.environ, held.inherited_environment()):
                inherited = bridge.lock_file(bridge.lane_identity(self.child), "inherited")
                inherited.close()
                with self.assertRaisesRegex(bridge.BridgeError, "does not match"):
                    bridge.lock_file(self.linked_lane, "wrong worktree")
            packet_path = self.root / "standalone.json"
            packet_path.write_text(json.dumps(self.packet(self.sibling, "standalone")))
            run_dir = self.root / "standalone-run"
            got = subprocess.run([sys.executable, str(BRIDGE), "run", "--packet", str(packet_path),
                                  "--run-dir", str(run_dir), "--timeout", "5"], text=True, capture_output=True)
            self.assertNotEqual(got.returncode, 0)
            self.assertIn("active run", (run_dir / "error.json").read_text())
            self.assertFalse((run_dir / "child.json").exists())
        finally:
            self.runtime._release_lane(self.lane, held)

    def test_child_unknown_blocks_parent_and_sibling_until_that_run_is_reconciled(self):
        self.unknown_fixture(self.runtime, self.child, "run-child_unknown")
        other_state = Runtime(self.root / "other-state")
        self.addCleanup(other_state.close)
        for runtime, cwd in ((self.runtime, self.repo), (self.runtime, self.sibling), (other_state, self.repo)):
            with self.subTest(cwd=str(cwd), state=str(runtime.state_root)), \
                 self.assertRaisesRegex(RuntimeError, "unknown"):
                runtime.start(self.packet(cwd, "blocked"))
        packet_path = self.root / "standalone.json"
        packet_path.write_text(json.dumps(self.packet(self.repo, "standalone")))
        got = subprocess.run([sys.executable, str(BRIDGE), "run", "--packet", str(packet_path),
                              "--run-dir", str(self.root / "standalone-run"), "--timeout", "5"],
                             text=True, capture_output=True)
        self.assertNotEqual(got.returncode, 0)
        self.assertIn("unknown prior supervised run", got.stderr)
        linked = other_state.start(self.packet(self.linked, "linked-free"))
        self.assertEqual(self.finish(other_state, linked["run_id"])["status"], "reported")

        self.reconcile(self.runtime, "run-child_unknown")
        self.assertEqual(bridge.unknown_markers(self.lane), [])
        root = self.runtime.start(self.packet(self.repo, "after-reconcile"))
        self.assertEqual(self.finish(self.runtime, root["run_id"])["status"], "reported")

    def test_root_unknown_blocks_child_dispatch(self):
        self.unknown_fixture(self.runtime, self.repo, "run-root_unknown")
        with self.assertRaisesRegex(RuntimeError, "unknown"):
            self.runtime.start(self.packet(self.child, "child-blocked"))
        self.reconcile(self.runtime, "run-root_unknown")
        child = self.runtime.start(self.packet(self.child, "child-after"))
        self.assertEqual(self.finish(self.runtime, child["run_id"])["status"], "reported")

    def test_reconcile_clears_only_its_own_marker_in_a_shared_lane(self):
        self.unknown_fixture(self.runtime, self.child, "run-child_one")
        self.unknown_fixture(self.runtime, self.sibling, "run-sibling_two")
        self.assert_marker_owners(["run-child_one", "run-sibling_two"])
        self.reconcile(self.runtime, "run-child_one")
        self.assert_marker_owners(["run-sibling_two"])
        with self.assertRaisesRegex(RuntimeError, "unknown"):
            self.runtime.start(self.packet(self.repo, "still-blocked"))
        self.reconcile(self.runtime, "run-sibling_two")
        self.assertEqual(bridge.unknown_markers(self.lane), [])

    def test_foreign_marker_is_never_cleared_by_another_runs_recovery(self):
        self.unknown_fixture(self.runtime, self.child, "run-child_one")
        self.reconcile(self.runtime, "run-child_one")
        bridge.publish_unknown_marker(self.lane, {"cwd": str(self.sibling.resolve()), "run_id": "run-foreign",
                                                  "reason": "foreign", "recorded_at": time.time()})
        digest = self.runtime.snapshot("run-child_one")["reconciliation"]["workspace_digest"]
        with self.assertRaisesRegex(RuntimeError, "belongs to another"):
            self.runtime.reconcile("run-child_one", "retry", ["fixture"], digest)
        self.assert_marker_owners(["run-foreign"])

    def test_legacy_exact_cwd_marker_from_subdirectory_still_blocks_its_worktree(self):
        legacy_key = hashlib.sha256(str(self.child.resolve()).encode()).hexdigest()
        legacy = bridge.unknown_marker_root() / f"{legacy_key}.json"
        legacy.write_text(json.dumps({"cwd": str(self.child.resolve()), "run_id": "run-legacy", "reason": "0.x"}))
        self.addCleanup(legacy.unlink, missing_ok=True)
        for cwd in (self.repo, self.sibling):
            with self.subTest(cwd=str(cwd)), self.assertRaisesRegex(RuntimeError, "unknown"):
                self.runtime.start(self.packet(cwd, "legacy-blocked"))
        linked = self.runtime.start(self.packet(self.linked, "linked-free"))
        self.assertEqual(self.finish(self.runtime, linked["run_id"])["status"], "reported")

    def test_legacy_marker_for_a_deleted_subdirectory_stays_blocking(self):
        gone = self.lane + "/removed-package"
        legacy = bridge.unknown_marker_root() / (hashlib.sha256(gone.encode()).hexdigest() + ".json")
        legacy.write_text(json.dumps({"cwd": gone, "run_id": "run-legacy_gone"}))
        self.addCleanup(legacy.unlink, missing_ok=True)
        with self.assertRaisesRegex(RuntimeError, "unknown"):
            self.runtime.start(self.packet(self.repo, "gone-blocked"))
        self.assertEqual(bridge.unknown_markers(self.linked_lane), [])

    def test_restart_of_legacy_record_with_deleted_cwd_blocks_the_worktree_across_state_roots(self):
        run_id = "run-legacy_deleted"
        state_a = self.root / "state-a"; state_a.mkdir()
        # A pre-identity record keeps the caller's lexical cwd; /var aliases
        # must still be attributed once the directory is gone.
        record = {"run_id": run_id, "task_id": "old", "revision": 1, "cwd": str(self.child),
                  "status": "running", "phase": "executing", "started_at": time.time(), "updated_at": time.time()}
        (state_a / "registry.json").write_text(json.dumps({"runs": {run_id: record}}))
        shutil.rmtree(self.child)

        restarted = Runtime(state_a)
        self.addCleanup(restarted.close)
        self.assertEqual(restarted._registry()["runs"][run_id]["status"], "unknown")
        markers = self.assert_marker_owners([run_id])
        self.assertIsNone(markers[0][1]["lane_identity"], "an unresolved cwd must not claim a worktree lane")
        self.assertEqual(bridge.unknown_markers(self.linked_lane), [])

        state_b = Runtime(self.root / "state-b")
        self.addCleanup(state_b.close)
        for runtime, cwd in ((state_b, self.repo), (state_b, self.sibling), (restarted, self.repo)):
            with self.subTest(cwd=str(cwd), state=str(runtime.state_root)), \
                 self.assertRaisesRegex(RuntimeError, "unknown"):
                runtime.start(self.packet(cwd, "deleted-legacy-blocked"))
        packet_path = self.root / "standalone.json"
        packet_path.write_text(json.dumps(self.packet(self.repo, "standalone")))
        got = subprocess.run([sys.executable, str(BRIDGE), "run", "--packet", str(packet_path),
                              "--run-dir", str(self.root / "standalone-run"), "--timeout", "5"],
                             text=True, capture_output=True)
        self.assertNotEqual(got.returncode, 0)
        self.assertIn("unknown prior supervised run", got.stderr)
        linked = state_b.start(self.packet(self.linked, "linked-free"))
        self.assertEqual(self.finish(state_b, linked["run_id"])["status"], "reported")

    def test_legacy_unknown_record_without_marker_blocks_the_worktree(self):
        self.unknown_fixture(self.runtime, self.child, "run-legacy_record", lane_identity=False)
        for path, _ in bridge.unknown_markers(self.lane):
            path.unlink()
        with self.assertRaisesRegex(RuntimeError, "unknown"):
            self.runtime.start(self.packet(self.repo, "legacy-record-blocked"))
        self.reconcile(self.runtime, "run-legacy_record")
        root = self.runtime.start(self.packet(self.repo, "legacy-record-after"))
        self.assertEqual(self.finish(self.runtime, root["run_id"])["status"], "reported")

    def test_pre_upgrade_active_subdirectory_run_holding_its_old_lock_is_not_relabelled(self):
        run_id = "run-legacy_active"
        state = self.root / "legacy-state"; state.mkdir()
        record = {"run_id": run_id, "task_id": "old", "revision": 1, "cwd": str(self.child.resolve()),
                  "status": "running", "phase": "executing", "started_at": time.time(), "updated_at": time.time()}
        (state / "registry.json").write_text(json.dumps({"runs": {run_id: record}}))
        # An older release locked sha256(exact cwd); that key is still what
        # lane_lock_path yields for the recorded subdirectory string.
        old_lock = bridge.lane_lock_path(str(self.child.resolve())).open("a+")
        fcntl.flock(old_lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            legacy_runtime = Runtime(state)
            self.addCleanup(legacy_runtime.close)
            self.assertEqual(legacy_runtime._registry()["runs"][run_id]["status"], "running")
            with self.assertRaisesRegex(RuntimeError, "recorded before worktree lanes"):
                legacy_runtime.start(self.packet(self.repo, "new-root"))
            self.assertEqual(legacy_runtime._registry()["runs"][run_id]["status"], "running")
            self.assertNotIn(self.lane, _LOCAL_LANES)
        finally:
            old_lock.close()
        legacy_runtime.snapshot(run_id)
        self.assertEqual(legacy_runtime._registry()["runs"][run_id]["status"], "unknown")
        with self.assertRaisesRegex(RuntimeError, "unknown"):
            legacy_runtime.start(self.packet(self.repo, "new-root"))

    def test_worktree_root_observation_reproduces_the_cwd_snapshot_coordinates(self):
        (self.child / "keep.txt").write_text("child edit\n")
        (self.sibling / "keep.txt").write_text("sibling edit\n")
        (self.child / "new.txt").write_text("untracked\n")
        packet = {**self.packet(self.child.resolve(), "coordinates"), "workspace_kind": "git",
                  "protected_files": ["keep.txt", "absent.txt"]}
        at_cwd = bridge.workspace_snapshot(packet)
        at_root = bridge.workspace_snapshot(packet, worktree_root=Path(self.lane))
        self.assertEqual(at_root, at_cwd)
        self.assertEqual(at_root["git_prefix"], "pkg/")
        self.assertIn("../sib/keep.txt", at_root["dirty_content_hashes"])

    def test_deleted_child_cwd_unknown_is_inspected_at_its_worktree_root_then_reconciled(self):
        run_id = "run-deleted_child"
        self.unknown_fixture(self.runtime, self.child, run_id)
        packet_bytes = (self.runtime.runs_root / run_id / "packet.json").read_bytes()
        (self.runtime.packets_root / f"{run_id}.json").write_bytes(packet_bytes)
        packet_sha = hashlib.sha256(packet_bytes).hexdigest()
        self.runtime._update(lambda data: data["runs"][run_id].__setitem__("packet_sha256", packet_sha))
        recorded_cwd = self.runtime._registry()["runs"][run_id]["cwd"]
        shutil.rmtree(self.child)
        for cwd in (self.repo, self.sibling):
            with self.subTest(cwd=str(cwd)), self.assertRaisesRegex(RuntimeError, "unknown"):
                self.runtime.start(self.packet(cwd, "still-blocked"))

        inspection = self.runtime.inspect_recovery(run_id)
        self.assertTrue(inspection["eligible"], inspection)
        workspace = inspection["workspace"]
        self.assertEqual((workspace["observed_at"], workspace["observed_root"], workspace["recorded_cwd"]),
                         ("established_worktree_root", self.lane, recorded_cwd))
        self.assertEqual(self.runtime.reconcile(run_id, "child dir deleted; groups absent", ["fixture pids"],
                                                workspace["digest"])["reconciliation"]["workspace_digest"],
                         workspace["digest"])
        self.assertEqual((self.runtime.runs_root / run_id / "packet.json").read_bytes(), packet_bytes)
        record = self.runtime._registry()["runs"][run_id]
        self.assertEqual((record["cwd"], record["status"]), (recorded_cwd, "unknown"))
        self.assertEqual(bridge.unknown_markers(self.lane), [])
        for cwd, task in ((self.sibling, "sibling-after"), (self.repo, "root-after")):
            run = self.runtime.start(self.packet(cwd, task))
            self.assertEqual(self.finish(self.runtime, run["run_id"])["status"], "reported")

    def test_deleted_cwd_recovery_keeps_digest_marker_and_process_gates(self):
        run_id = "run-deleted_gates"
        self.unknown_fixture(self.runtime, self.child, run_id)
        shutil.rmtree(self.child)
        inspection = self.runtime.inspect_recovery(run_id)
        self.assertTrue(inspection["eligible"], inspection)
        (self.sibling / "keep.txt").write_text("changed after inspection\n")
        with self.assertRaisesRegex(RuntimeError, "workspace changed since inspection"):
            self.runtime.reconcile(run_id, "stale", ["fixture"], inspection["workspace"]["digest"])

        for path, _ in bridge.unknown_markers(self.lane):
            path.unlink()
        bridge.publish_unknown_marker(self.lane, {"cwd": str(self.sibling.resolve()), "run_id": "run-foreign",
                                                  "reason": "foreign", "recorded_at": time.time()})
        fresh = self.runtime.inspect_recovery(run_id)
        with self.assertRaisesRegex(RuntimeError, "belongs to another"):
            self.runtime.reconcile(run_id, "foreign", ["fixture"], fresh["workspace"]["digest"])
        self.assert_marker_owners(["run-foreign"])
        self.assertIsNone(self.runtime._registry()["runs"][run_id].get("reconciliation"))

        (self.runtime.runs_root / run_id / "child.json").write_text(
            json.dumps({"pid": os.getpid(), "process_group": os.getpgrp()}))
        live = self.runtime.inspect_recovery(run_id)
        self.assertFalse(live["eligible"])
        self.assertEqual(live["workspace"]["state"], "observed", "only the process gate may block here")
        self.assertNotEqual(live["child"]["state"], "stopped")
        with self.assertRaisesRegex(RuntimeError, "cannot be reconciled"):
            self.runtime.reconcile(run_id, "live", ["fixture"], live["workspace"]["digest"])

    def test_deleted_cwd_without_a_verifiable_lane_binding_stays_unobservable(self):
        inner = self.child / "inner"; inner.mkdir()
        elsewhere = self.root / "elsewhere"; elsewhere.mkdir()

        def restore():
            if self.child.is_symlink():
                self.child.unlink()
            self.child.mkdir(exist_ok=True)
            shutil.rmtree(self.child / ".git", ignore_errors=True)
            if inner.exists() and not inner.is_dir():
                inner.unlink()
            inner.mkdir(exist_ok=True)

        def legacy(run_id):
            self.unknown_fixture(self.runtime, inner, run_id, lane_identity=False)
            shutil.rmtree(inner)

        def wrong_lane(run_id):
            self.unknown_fixture(self.runtime, inner, run_id)
            self.runtime._update(lambda data: data["runs"][run_id].__setitem__("lane_identity", self.linked_lane))
            shutil.rmtree(inner)

        def rebound_nested(run_id):
            self.unknown_fixture(self.runtime, inner, run_id)
            shutil.rmtree(inner); git("init", "-q", str(self.child))

        def symlinked_ancestor(run_id):
            self.unknown_fixture(self.runtime, inner, run_id)
            shutil.rmtree(self.child); self.child.symlink_to(elsewhere, target_is_directory=True)

        def replaced_by_file(run_id):
            self.unknown_fixture(self.runtime, inner, run_id)
            shutil.rmtree(inner); inner.write_text("not a directory\n")

        def changed_packet(run_id):
            self.unknown_fixture(self.runtime, inner, run_id)
            self.runtime._update(lambda data: data["runs"][run_id].__setitem__("packet_sha256", "0" * 64))
            shutil.rmtree(inner)

        def missing_packet(run_id):
            self.unknown_fixture(self.runtime, inner, run_id)
            (self.runtime.runs_root / run_id / "packet.json").unlink()
            shutil.rmtree(inner)

        cases = {"legacy": (legacy, "predates worktree lanes"),
                 "wrong_lane": (wrong_lane, "outside its established worktree lane"),
                 "rebound": (rebound_nested, "different worktree"),
                 "symlink": (symlinked_ancestor, "symlink"),
                 "file": (replaced_by_file, "non-directory"),
                 "packet_binding": (changed_packet, "packet binding cannot be verified"),
                 "missing_packet": (missing_packet, "packet.json is missing or malformed")}
        for index, (name, (arrange, reason)) in enumerate(cases.items()):
            with self.subTest(case=name):
                run_id = f"run-unbound_{index}"
                restore()
                arrange(run_id)
                packet_path = self.runtime.runs_root / run_id / "packet.json"
                before = packet_path.read_bytes() if packet_path.exists() else None
                inspection = self.runtime.inspect_recovery(run_id)
                self.assertFalse(inspection["eligible"], inspection)
                self.assertEqual(inspection["workspace"]["state"], "unconfirmed")
                self.assertIn(reason, inspection["workspace"]["reason"])
                self.assertNotIn("observed_root", inspection["workspace"])
                self.assertEqual(packet_path.read_bytes() if packet_path.exists() else None, before)
                with self.assertRaisesRegex(RuntimeError, "cannot be reconciled"):
                    self.runtime.reconcile(run_id, "unbound", ["fixture"], "0" * 64)
        with self.assertRaisesRegex(RuntimeError, "unknown"):
            self.runtime.start(self.packet(self.repo, "unbound-still-blocked"))

    def test_known_lane_record_is_observed_at_an_unchanged_or_restored_real_cwd(self):
        self.unknown_fixture(self.runtime, self.child, "run-unchanged_cwd")
        unchanged = self.runtime.inspect_recovery("run-unchanged_cwd")
        self.assertTrue(unchanged["eligible"], unchanged)
        self.assertNotIn("observed_root", unchanged["workspace"])
        self.reconcile(self.runtime, "run-unchanged_cwd")

        self.unknown_fixture(self.runtime, self.child, "run-restored_cwd")
        shutil.rmtree(self.child)
        self.child.mkdir(); (self.child / "keep.txt").write_text("keep\n")
        restored = self.runtime.inspect_recovery("run-restored_cwd")
        self.assertTrue(restored["eligible"], restored)
        self.assertNotIn("observed_root", restored["workspace"])
        self.reconcile(self.runtime, "run-restored_cwd")
        root = self.runtime.start(self.packet(self.repo, "after-restored"))
        self.assertEqual(self.finish(self.runtime, root["run_id"])["status"], "reported")

    def test_known_lane_record_refuses_a_rebound_or_symlinked_existing_cwd(self):
        other = self.root / "other-repo"; other.mkdir()
        git("init", "-q", str(other))
        (self.linked / "inner").mkdir()
        inner = self.child / "inner"
        cases = [
            ("cwd_symlink_to_linked_worktree", self.child,
             lambda: self.child.symlink_to(self.linked, target_is_directory=True), "symlink"),
            ("cwd_symlink_within_same_worktree", self.child,
             lambda: self.child.symlink_to(self.sibling, target_is_directory=True), "symlink"),
            ("cwd_symlink_to_other_repository", self.child,
             lambda: self.child.symlink_to(other, target_is_directory=True), "symlink"),
            ("ancestor_symlink_to_linked_worktree", inner,
             lambda: self.child.symlink_to(self.linked, target_is_directory=True), "resolves elsewhere"),
            ("nested_repository", self.child,
             lambda: (self.child.mkdir(), git("init", "-q", str(self.child))), "different worktree"),
            # Keep last: a linked worktree registered at the old cwd path is not restored.
            ("nested_linked_worktree", self.child,
             lambda: git("-C", str(self.repo), "worktree", "add", "-q", "-b", "nested", str(self.child)),
             "different worktree"),
        ]
        for index, (name, cwd, rebind, reason) in enumerate(cases):
            with self.subTest(case=name):
                if self.child.is_symlink():
                    self.child.unlink()
                elif self.child.exists():
                    shutil.rmtree(self.child)
                self.child.mkdir(); (self.child / "keep.txt").write_text("keep\n")
                cwd.mkdir(exist_ok=True)
                run_id = f"run-rebound_{index}"
                self.unknown_fixture(self.runtime, cwd, run_id)
                shutil.rmtree(self.child)
                rebind()
                self.assertTrue(os.path.isdir(cwd), "the recorded cwd path exists again, but as another workspace")
                inspection = self.runtime.inspect_recovery(run_id)
                self.assertFalse(inspection["eligible"], inspection)
                self.assertEqual(inspection["workspace"]["state"], "unconfirmed")
                self.assertNotIn("digest", inspection["workspace"])
                self.assertIn(reason, inspection["workspace"]["reason"])
                with self.assertRaisesRegex(RuntimeError, "cannot be reconciled"):
                    self.runtime.reconcile(run_id, "rebound", ["fixture"], "0" * 64)
                self.assertIsNone(self.runtime._registry()["runs"][run_id].get("reconciliation"))
                self.assertIn(run_id, [value["run_id"] for _, value in bridge.unknown_markers(self.lane)])
        with self.assertRaisesRegex(RuntimeError, "unknown"):
            self.runtime.start(self.packet(self.repo, "rebound-still-blocked"))

    def test_root_lane_keeps_the_pre_identity_key_and_whitespace_names(self):
        self.assertEqual(bridge.lane_lock_path(self.lane).name,
                         hashlib.sha256(str(self.repo.resolve()).encode()).hexdigest() + ".lock")
        spaced = self.root / " spaced repo "
        (spaced / "inner ").mkdir(parents=True)
        git("init", "-q", str(spaced))
        self.assertEqual(bridge.lane_identity(spaced / "inner "), str(spaced.resolve()))

    def test_artifact_roots_keep_exact_independent_lanes(self):
        docs = self.root / "docs"
        (docs / "nested").mkdir(parents=True)
        self.assertEqual(bridge.lane_identity(docs, "artifacts"), str(docs.resolve()))
        self.assertNotEqual(bridge.lane_identity(docs / "nested", "artifacts"), bridge.lane_identity(docs, "artifacts"))
        with self.assertRaises(bridge.BridgeError):
            bridge.lane_identity(docs, "git")


class SubdirectorySnapshotTests(unittest.TestCase):
    def test_untracked_child_content_change_is_hashed_at_the_real_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"; child = repo / "sub"; child.mkdir(parents=True)
            git("init", "-q", str(repo))
            (child / "untracked.txt").write_text("before\n")
            packet = {"owned_files": [], "protected_files": []}
            before = bridge.git_snapshot(child.resolve(), packet)
            (child / "untracked.txt").write_text("after\n")
            after = bridge.git_snapshot(child.resolve(), packet)
            self.assertNotEqual(before["dirty_content_hashes"]["untracked.txt"], "missing")
            self.assertNotEqual(before["dirty_content_hashes"], after["dirty_content_hashes"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main(verbosity=2)
