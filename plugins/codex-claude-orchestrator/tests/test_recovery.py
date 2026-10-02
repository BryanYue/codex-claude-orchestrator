import hashlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
from pathlib import Path
from unittest.mock import patch


SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "codex-claude-orchestrator" / "scripts"
sys.path.insert(0, str(SCRIPTS))
from runtime import Runtime  # noqa: E402
import bridge  # noqa: E402


class UnknownRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.enterContext(mock.patch.dict(os.environ, {"CLAUDE_ORCHESTRATOR_CLI_ROOT": str(self.root / "isolated-cli-store"),
                                                        "CLAUDE_CONFIG_DIR": str(self.root / "claude-config")}))
        self.repo = self.root / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        subprocess.run(["git", "-C", str(self.repo), "config", "user.email", "test@example.invalid"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "config", "user.name", "Test"], check=True)
        (self.repo / "base.txt").write_text("base\n")
        subprocess.run(["git", "-C", str(self.repo), "add", "base.txt"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-qm", "base"], check=True)
        self.requirement = self.root / "requirements.md"
        self.requirement.write_text("requirement\n")
        self.fake = self.root / "fake.py"
        self.fake.write_text("""#!/usr/bin/env python3
import json, sys
a=sys.argv[1:]
if a == ['--version']: print('2.1.276'); raise SystemExit
if a == ['--help']: print('-p --model --effort --output-format --verbose --json-schema --session-id --resume --permission-mode --tools --allowedTools --disallowedTools --settings --strict-mcp-config --mcp-config --disable-slash-commands --no-session-persistence'); raise SystemExit
if a == ['auth','status','--json']: print(json.dumps({'loggedIn':True})); raise SystemExit
s=a[a.index('--session-id')+1] if '--session-id' in a else a[a.index('--resume')+1]
print(json.dumps({'type':'system','subtype':'init','session_id':s,'model':'fixture'}), flush=True)
sys.stdin.buffer.read()
print(json.dumps({'type':'result','subtype':'success','session_id':s,'structured_output':{'status':'completed','summary':'done','evidence':[], 'checks':[], 'unresolved':[]}}), flush=True)
""")
        self.fake.chmod(0o755)
        self.old_bin = os.environ.get("CLAUDE_BIN")
        os.environ["CLAUDE_BIN"] = str(self.fake)
        self.runtime = Runtime(self.root / "state")
        self.run_id = "run-recovery_123"
        self.run = self.runtime.runs_root / self.run_id
        self.run.mkdir()
        self.packet = {"task_id": "recover", "revision": 1, "role": "review", "workspace_kind": "git", "cwd": str(self.repo),
                       "objective": "review", "requirement_sources": [str(self.requirement)],
                       "constraints": ["bounded"], "acceptance": ["structured"], "owned_files": [],
                       "protected_files": ["protected.txt"], "model": "fixture", "effort": "low"}
        (self.run / "packet.json").write_text(json.dumps(self.packet))
        # Deliberately impossible identifiers prove absence without touching a real process.
        (self.run / "state.json").write_text(json.dumps({"status": "unknown", "pid": 99999991}))
        (self.run / "child.json").write_text(json.dumps({"pid": 99999992, "process_group": 99999992}))
        record = {"run_id": self.run_id, "task_id": "recover", "revision": 1, "cwd": str(self.repo),
                  "run_dir": str(self.run), "status": "unknown", "phase": "unknown",
                  "started_at": time.time(), "updated_at": time.time(), "bridge_process_group": 99999991}
        self.runtime.registry_path.write_text(json.dumps({"runs": {self.run_id: record}}))
        self.marker = self.runtime._unknown_marker(str(self.repo))
        self.runtime._mark_unknown_lane(str(self.repo), self.run_id, "fixture unknown")

    def tearDown(self):
        self.runtime.close()
        if self.old_bin is None:
            os.environ.pop("CLAUDE_BIN", None)
        else:
            os.environ["CLAUDE_BIN"] = self.old_bin
        self.marker.unlink(missing_ok=True)
        for path, _ in bridge.unknown_markers(bridge.lane_identity(self.repo)):
            path.unlink(missing_ok=True)
        self.temp.cleanup()

    def test_reconcile_requires_fresh_workspace_evidence_and_preserves_unknown_receipt(self):
        inspection = self.runtime.inspect_recovery(self.run_id)
        self.assertTrue(inspection["eligible"], inspection)
        self.assertTrue(self.marker.exists())
        with self.assertRaises(RuntimeError):
            self.runtime.reconcile(self.run_id, "checked", ["fixture evidence"], "0" * 64)
        self.assertTrue(self.marker.exists())
        recovered = self.runtime.reconcile(self.run_id, "checked bridge and child groups", ["fixture process and workspace check"],
                                           inspection["workspace"]["digest"])
        self.assertEqual(recovered["status"], "unknown")
        self.assertEqual(recovered["reconciliation"]["outcome"], "confirmed_stopped")
        self.assertFalse(self.marker.exists())
        self.assertTrue((self.run / "reconciliation.json").is_file())
        self.assertEqual(self.runtime.snapshot(self.run_id)["status"], "unknown")
        # Reconciliation removes only the prior-run admission block. It starts
        # a distinct higher revision and never resumes or rewrites the unknown.
        fresh = self.runtime.start({**self.packet, "revision": 2})
        page = self.runtime.wait(fresh["run_id"], timeout=5)
        while page["snapshot"]["status"] not in {"reported", "failed", "blocked", "cancelled", "timeout", "unknown"}:
            page = self.runtime.wait(fresh["run_id"], after=page["next_cursor"], timeout=5)
        result = page["snapshot"]
        self.assertEqual(result["status"], "reported")
        self.assertEqual(self.runtime.snapshot(self.run_id)["status"], "unknown")

    def test_non_utf8_dirty_repository_can_be_observed_and_reconciled(self):
        (self.repo / "base.txt").write_bytes("中文旧内容\n".encode("gbk"))
        subprocess.run(["git", "-C", str(self.repo), "commit", "-qam", "GBK baseline"], check=True)
        (self.repo / "base.txt").write_bytes("中文新内容\n".encode("gbk"))
        inspection = self.runtime.inspect_recovery(self.run_id)
        self.assertTrue(inspection["eligible"], inspection)
        self.assertEqual(inspection["workspace"]["state"], "observed")
        result = self.runtime.reconcile(self.run_id, "processes stopped", ["GBK workspace inspected"],
                                        inspection["workspace"]["digest"])
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["reconciliation"]["outcome"], "confirmed_stopped")
        self.assertFalse(self.marker.exists())

    def test_reconcile_refuses_missing_process_identity(self):
        (self.run / "child.json").write_text(json.dumps({}))
        inspection = self.runtime.inspect_recovery(self.run_id)
        self.assertFalse(inspection["eligible"])
        self.assertIn("launch is unconfirmed", "; ".join(inspection["blocking_reasons"]))
        with self.assertRaises(RuntimeError):
            self.runtime.reconcile(self.run_id, "checked", ["fixture evidence"], inspection["workspace"].get("digest"))
        self.assertTrue(self.marker.exists())

    def test_unknown_snapshot_without_adoptable_terminal_does_not_take_the_lane(self):
        with patch.object(self.runtime, "_lane_lock", side_effect=AssertionError("snapshot contended for cwd lane")):
            snapshot = self.runtime.snapshot(self.run_id)
        self.assertEqual(snapshot["status"], "unknown")
        self.assertTrue(self.marker.exists())

    def test_reconcile_retries_only_marker_cleanup_after_unlink_failure(self):
        inspection = self.runtime.inspect_recovery(self.run_id)
        digest = inspection["workspace"]["digest"]
        original_unlink = Path.unlink
        failed = False

        def fail_marker_once(path, *args, **kwargs):
            nonlocal failed
            if path == self.marker and not failed:
                failed = True
                raise OSError("fixture marker unlink failure")
            return original_unlink(path, *args, **kwargs)

        with patch.object(Path, "unlink", fail_marker_once):
            with self.assertRaisesRegex(OSError, "unlink failure"):
                self.runtime.reconcile(self.run_id, "checked", ["fixture evidence"], digest)

        persisted = self.runtime.snapshot(self.run_id)
        self.assertEqual(persisted["reconciliation"]["outcome"], "confirmed_stopped")
        self.assertTrue(self.marker.exists())

        retried = self.runtime.reconcile(self.run_id, "different retry text", ["not accepted as a new fact"], digest)
        self.assertEqual(retried["reconciliation"], persisted["reconciliation"])
        self.assertFalse(self.marker.exists())
        fresh = self.runtime.start({**self.packet, "revision": 2})
        page = self.runtime.wait(fresh["run_id"], timeout=5)
        while page["snapshot"]["status"] not in {"reported", "failed", "blocked", "cancelled", "timeout", "unknown"}:
            page = self.runtime.wait(fresh["run_id"], after=page["next_cursor"], timeout=5)
        self.assertEqual(page["snapshot"]["status"], "reported")

    def test_reconcile_never_deletes_another_runs_marker(self):
        inspection = self.runtime.inspect_recovery(self.run_id)
        digest = inspection["workspace"]["digest"]
        self.runtime.reconcile(self.run_id, "checked", ["fixture evidence"], digest)
        other_run_id = "run-other_456"
        self.runtime._mark_unknown_lane(str(self.repo), other_run_id, "other unknown run")

        with self.assertRaisesRegex(RuntimeError, "belongs to another"):
            self.runtime.reconcile(self.run_id, "retry", ["fixture evidence"], digest)

        self.assertEqual(json.loads(self.marker.read_text())["run_id"], other_run_id)

    def test_inspection_and_reconciliation_share_one_runtime_guard(self):
        inspection = self.runtime.inspect_recovery(self.run_id)
        digest = inspection["workspace"]["digest"]
        inspect_entered = threading.Event()
        inspect_release = threading.Event()
        reconcile_started = threading.Event()
        results = {}
        original = self.runtime._recovery_details

        def gated_details(*args, **kwargs):
            if threading.current_thread().name == "fixture-inspect":
                inspect_entered.set()
                inspect_release.wait(5)
            return original(*args, **kwargs)

        def inspect():
            try:
                results["inspection"] = self.runtime.inspect_recovery(self.run_id)
            except BaseException as exc:
                results["inspect_error"] = exc

        def reconcile():
            reconcile_started.set()
            try:
                results["reconciled"] = self.runtime.reconcile(
                    self.run_id, "guarded fixture check", ["process and workspace evidence"], digest)
            except BaseException as exc:
                results["reconcile_error"] = exc

        with patch.object(self.runtime, "_recovery_details", side_effect=gated_details):
            inspect_thread = threading.Thread(target=inspect, name="fixture-inspect")
            reconcile_thread = threading.Thread(target=reconcile, name="fixture-reconcile")
            inspect_thread.start()
            self.assertTrue(inspect_entered.wait(2))
            reconcile_thread.start()
            self.assertTrue(reconcile_started.wait(2))
            time.sleep(.05)
            self.assertTrue(reconcile_thread.is_alive(), "reconcile must wait for same-Runtime inspection")
            inspect_release.set()
            inspect_thread.join(5); reconcile_thread.join(5)
        self.assertNotIn("inspect_error", results)
        self.assertNotIn("reconcile_error", results)
        self.assertTrue(results["inspection"]["eligible"])
        self.assertEqual(results["reconciled"]["reconciliation"]["outcome"], "confirmed_stopped")

    def _write_bound_record(self, run_id, status="starting", include_sha=True):
        packet = bridge.validate_packet(dict(self.packet))
        packet_path = self.runtime.packets_root / f"{run_id}.json"
        packet_path.write_text(json.dumps(packet, ensure_ascii=False, indent=2) + "\n")
        packet_sha = hashlib.sha256(packet_path.read_bytes()).hexdigest()
        record = {"run_id": run_id, "task_id": packet["task_id"], "revision": packet["revision"],
                  "cwd": packet["cwd"], "run_dir": str(self.runtime.runs_root / run_id),
                  "status": status, "phase": status, "started_at": time.time(), "updated_at": time.time(),
                  "bridge_process_group": 99999991,
                  "lifecycle_file": str(self.runtime._lifecycle_path(run_id))}
        if include_sha:
            record["packet_sha256"] = packet_sha
        self.runtime.registry_path.write_text(json.dumps({"runs": {run_id: record}}))
        return packet_sha

    def test_restart_adopts_bound_pre_dispatch_failure_without_child_inference(self):
        run_id = "run-preflight_123"
        packet_sha = self._write_bound_record(run_id)
        lifecycle = {"schema_version": 1, "run_id": run_id, "task_id": self.packet["task_id"],
                     "revision": self.packet["revision"], "cwd": str(self.repo.resolve()), "packet_sha256": packet_sha,
                     "bridge_pid": 99999991, "phase": "pre_dispatch", "status": "failed",
                     "child_started": False, "terminal": True, "reason": "fixture validation failure"}
        path = self.runtime._lifecycle_path(run_id); path.parent.mkdir(parents=True); path.write_text(json.dumps(lifecycle))
        # _write_bound_record replaced the registry, so setUp's run no longer
        # exists; drop both fixture-owned mirrors. Recovery must not delete
        # an unrelated run's legacy mirror merely because it shares this lane.
        lane = bridge.lane_identity(self.repo)
        prior = [(marker, value) for marker, value in bridge.unknown_markers(lane)
                 if isinstance(value, dict) and value.get("run_id") == self.run_id]
        self.assertEqual({marker.parent for marker, _ in prior}, set(bridge.unknown_marker_roots()))
        for marker, _ in prior:
            marker.unlink()
        self.runtime._mark_unknown_lane(str(self.repo), run_id, "fixture stale marker")

        restarted = Runtime(self.root / "state")
        self.addCleanup(restarted.close)
        recovered = restarted.snapshot(run_id)
        self.assertEqual(recovered["status"], "failed")
        self.assertEqual(recovered["terminal_evidence"], "bridge_lifecycle")
        self.assertFalse(restarted._unknown_marker(str(self.repo)).exists())
        self.assertEqual(bridge.unknown_markers(bridge.lane_identity(self.repo)), [])
        fresh = restarted.start({**self.packet, "revision": 2})
        page = restarted.wait(fresh["run_id"], timeout=5)
        while page["snapshot"]["status"] not in {"reported", "blocked", "failed", "cancelled", "timeout", "unknown"}:
            page = restarted.wait(fresh["run_id"], after=page["next_cursor"], timeout=5)
        self.assertEqual(page["snapshot"]["status"], "reported")

    def test_bound_pre_dispatch_without_terminal_receipt_is_recoverable_but_launch_intent_is_not(self):
        run_id = "run-pre_dispatch_gap"
        packet_sha = self._write_bound_record(run_id, status="unknown")
        run_dir = self.runtime.runs_root / run_id; run_dir.mkdir()
        (run_dir / "packet.json").write_bytes((self.runtime.packets_root / f"{run_id}.json").read_bytes())
        lifecycle = {"schema_version":1, "run_id":run_id, "task_id":self.packet["task_id"],
                     "revision":self.packet["revision"], "cwd":str(self.repo.resolve()), "packet_sha256":packet_sha,
                     "bridge_pid":99999991, "phase":"pre_dispatch", "status":"starting",
                     "child_started":False, "terminal":False}
        path = self.runtime._lifecycle_path(run_id); path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(lifecycle))
        self.runtime._mark_unknown_lane(str(self.repo), run_id, "fixture pre-dispatch gap")

        restarted = Runtime(self.root / "state")
        self.addCleanup(restarted.close)
        detail = restarted.inspect_recovery(run_id)
        self.assertTrue(detail["eligible"], detail)
        self.assertIn("proves no Claude child", detail["child"]["reason"])

        lifecycle.update(phase="launch_intent", child_started=None)
        path.write_text(json.dumps(lifecycle))
        detail = restarted.inspect_recovery(run_id)
        self.assertFalse(detail["eligible"])
        self.assertIn("launch is unconfirmed", "; ".join(detail["blocking_reasons"]))

    def test_restart_adopts_only_bound_and_stopped_child_terminal_receipt(self):
        run_id = "run-orphan_123"
        packet_sha = self._write_bound_record(run_id)
        lifecycle = {"schema_version": 1, "run_id": run_id, "task_id": self.packet["task_id"],
                     "revision": self.packet["revision"], "cwd": str(self.repo.resolve()), "packet_sha256": packet_sha,
                     "bridge_pid": 99999991, "phase": "terminal", "status": "reported",
                     "child_started": True, "child_pid": 99999992, "child_process_group": 99999992,
                     "terminal": True, "reason": "reported is not accepted"}
        path = self.runtime._lifecycle_path(run_id); path.parent.mkdir(parents=True); path.write_text(json.dumps(lifecycle))

        restarted = Runtime(self.root / "state")
        self.addCleanup(restarted.close)
        recovered = restarted.snapshot(run_id)
        self.assertEqual(recovered["status"], "reported")
        self.assertEqual(recovered["terminal_evidence"], "bridge_lifecycle")

        # A terminal-looking sidecar with the wrong packet binding is not
        # allowed to revive a genuinely uncertain run.
        run_id_2 = "run-orphan_456"
        packet_sha_2 = self._write_bound_record(run_id_2)
        bad = dict(lifecycle, run_id=run_id_2, packet_sha256="0" * 64)
        path_2 = self.runtime._lifecycle_path(run_id_2); path_2.write_text(json.dumps(bad))
        uncertain = Runtime(self.root / "state")
        self.addCleanup(uncertain.close)
        self.assertEqual(uncertain.snapshot(run_id_2)["status"], "unknown")
        self.assertNotEqual(packet_sha_2, bad["packet_sha256"])

        # A crash after launch intent but before child metadata is the Popen
        # ambiguity window.  Even exact packet binding cannot turn it into a
        # pre-dispatch failure or reported success.
        run_id_3 = "run-launchgap_789"
        packet_sha_3 = self._write_bound_record(run_id_3)
        launch_gap = dict(lifecycle, run_id=run_id_3, packet_sha256=packet_sha_3,
                          phase="launch_intent", status="starting", child_started=None, terminal=False)
        path_3 = self.runtime._lifecycle_path(run_id_3); path_3.write_text(json.dumps(launch_gap))
        ambiguous = Runtime(self.root / "state")
        self.addCleanup(ambiguous.close)
        self.assertEqual(ambiguous.snapshot(run_id_3)["status"], "unknown")

        run_id_4 = "run-false_reported_012"
        packet_sha_4 = self._write_bound_record(run_id_4)
        impossible = dict(lifecycle, run_id=run_id_4, packet_sha256=packet_sha_4,
                          phase="pre_dispatch", status="reported", child_started=False, terminal=True)
        path_4 = self.runtime._lifecycle_path(run_id_4); path_4.write_text(json.dumps(impossible))
        rejected = Runtime(self.root / "state")
        self.addCleanup(rejected.close)
        self.assertEqual(rejected.snapshot(run_id_4)["status"], "unknown")

    def test_frozen_0_4_registry_uses_matching_dual_packet_and_full_semantics(self):
        run_id = "run-legacy_040"
        self._write_bound_record(run_id, include_sha=False)
        run_dir = self.runtime.runs_root / run_id; run_dir.mkdir()
        packet_bytes = (self.runtime.packets_root / f"{run_id}.json").read_bytes()
        (run_dir / "packet.json").write_bytes(packet_bytes)
        (run_dir / "child.json").write_text(json.dumps({"pid": 99999992, "process_group": 99999992}))
        (run_dir / "receipt.json").write_text(json.dumps({"status": "reported", "task_id": self.packet["task_id"],
                                                          "revision": self.packet["revision"]}))

        restarted = Runtime(self.root / "state")
        self.addCleanup(restarted.close)
        recovered = restarted.snapshot(run_id)
        self.assertEqual(recovered["status"], "reported")
        self.assertEqual(recovered["terminal_evidence"], "bound_run_receipt")

        # Complete packet correspondence is part of the legacy trust boundary;
        # formatting may differ because bridge.dump canonicalizes its copy.
        run_id_2 = "run-legacy_bad"
        self._write_bound_record(run_id_2, include_sha=False)
        run_dir_2 = self.runtime.runs_root / run_id_2; run_dir_2.mkdir()
        bad_packet = dict(self.packet, objective="tampered after dispatch")
        (run_dir_2 / "packet.json").write_text(json.dumps(bad_packet, ensure_ascii=False, indent=2) + "\n")
        (run_dir_2 / "child.json").write_text(json.dumps({"pid": 99999992, "process_group": 99999992}))
        (run_dir_2 / "receipt.json").write_text(json.dumps({"status": "reported", "task_id": self.packet["task_id"],
                                                            "revision": self.packet["revision"]}))
        uncertain = Runtime(self.root / "state")
        self.addCleanup(uncertain.close)
        self.assertEqual(uncertain.snapshot(run_id_2)["status"], "unknown")


if __name__ == "__main__":  # pragma: no cover
    unittest.main(verbosity=2)
