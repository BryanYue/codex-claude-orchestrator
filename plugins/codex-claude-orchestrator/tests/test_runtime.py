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
from runtime import Runtime, _LOCAL_LANES, workspace_changes  # noqa: E402
import bridge
from events import append  # noqa: E402


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name); self.repo = self.root / "repo"; self.repo.mkdir()
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        subprocess.run(["git", "-C", str(self.repo), "config", "user.email", "test@example.invalid"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "config", "user.name", "Test"], check=True)
        (self.repo / "base.txt").write_text("base\n")
        subprocess.run(["git", "-C", str(self.repo), "add", "base.txt"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-qm", "base"], check=True)
        self.requirement = self.root / "requirements.md"; self.requirement.write_text("requirement\n")
        self.fake = self.root / "fake.py"
        self.fake.write_text("""#!/usr/bin/env python3
import json, os, sys, time
a=sys.argv[1:]
if a == ['--version']: print('2.1.276'); raise SystemExit
if a == ['--help']: print('-p --model --effort --output-format --json-schema --session-id --resume --permission-mode --tools --allowedTools --disallowedTools --settings --strict-mcp-config --mcp-config --disable-slash-commands'); raise SystemExit
if a == ['auth','status','--json']: print(json.dumps({'loggedIn':True,'authMethod':'test'})); raise SystemExit
def opt(x): return a[a.index(x)+1] if x in a else None
s=opt('--resume') or opt('--session-id')
print(json.dumps({'type':'system','subtype':'init','session_id':s,'model':'test-model'}), flush=True)
if os.environ.get('RUNTIME_FAKE_SLEEP'): time.sleep(3)
print(json.dumps({'type':'assistant','session_id':s,'message':{'model':'provider-model','content':[]}}), flush=True)
print(json.dumps({'type':'result','subtype':'success','session_id':s,'modelUsage':{'provider-model':{'inputTokens':1}},'usage':{'input_tokens':1},'structured_output':{'status':'completed','summary':'done','evidence':['fake'], 'checks':[], 'unresolved':[]}}), flush=True)
""")
        self.enterContext(mock.patch.dict(os.environ, {"CLAUDE_ORCHESTRATOR_CLI_ROOT": str(self.root / "isolated-cli-store"),
                                                        "CLAUDE_CONFIG_DIR": str(self.root / "claude-config")}))
        self.fake.chmod(0o755); self.old = os.environ.get("CLAUDE_BIN"); os.environ["CLAUDE_BIN"] = str(self.fake)
        self.runtime = Runtime(self.root / "runtime-state")

    def tearDown(self):
        self.runtime.close()
        if self.old is None: os.environ.pop("CLAUDE_BIN", None)
        else: os.environ["CLAUDE_BIN"] = self.old
        os.environ.pop("RUNTIME_FAKE_SLEEP", None); self.temp.cleanup()

    def packet(self, revision=1):
        return {"task_id":"runtime-task", "revision":revision, "role":"review", "cwd":str(self.repo), "objective":"review",
                "requirement_sources":[str(self.requirement)], "constraints":["bounded"], "acceptance":["structured"],
                "owned_files":[], "protected_files":["protected.txt"], "model":"test-model", "effort":"low"}

    def finish(self, run_id):
        result = self.runtime.wait(run_id, timeout=8)
        while result["snapshot"]["status"] not in {"reported", "failed", "blocked", "cancelled", "timeout", "unknown"}:
            result = self.runtime.wait(run_id, after=result["next_cursor"], timeout=2)
        return result

    def wait_recovered_terminal(self, run_id):
        deadline = time.monotonic() + 8
        snapshot = self.runtime.snapshot(run_id)
        while snapshot["status"] == "unknown" and time.monotonic() < deadline:
            time.sleep(.05)
            snapshot = self.runtime.snapshot(run_id)
        return snapshot

    def wait_for_child(self):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            for child in self.runtime.runs_root.glob("run-*/child.json"):
                if child.is_file():
                    return child
            time.sleep(.02)
        self.fail("fixture bridge did not start its Claude child")

    def test_start_events_snapshot_diff_and_coordinator_decision(self):
        start = self.runtime.start(self.packet())
        self.assertEqual(start["status"], "starting")
        final = self.finish(start["run_id"])["snapshot"]
        self.assertEqual(final["status"], "reported")
        self.assertEqual(final["summary"], "done")
        self.assertTrue(final["session_id"])
        self.assertEqual(final["requested_model"], "test-model")
        self.assertEqual(final["initialized_model"], "test-model")
        self.assertEqual(final["actual_models"], ["provider-model"])
        self.assertEqual(final["provider_models"], ["provider-model"])
        self.assertEqual(final["actual_model"], "provider-model")
        self.assertEqual(final["actual_model_source"], "assistant_message+result_model_usage")
        self.assertEqual(final["execution_evidence"], "provider_event")
        page = self.runtime.events(start["run_id"])
        self.assertGreaterEqual(len(page["events"]), 4)
        self.assertTrue((Path(final["run_dir"]) / "diff.patch").exists())
        self.assertTrue((Path(final["run_dir"]) / "runtime_bridge.stdout.log").exists())
        decision = self.runtime.record_decision(start["run_id"], "accepted", "coordinator checked", ["fake evidence"])
        self.assertEqual(decision["decision"]["decision"], "accepted")

    def test_returned_report_can_close_after_independent_codex_completion(self):
        run_id = self.runtime.start(self.packet())["run_id"]
        self.assertEqual(self.finish(run_id)["snapshot"]["status"], "reported")
        returned = self.runtime.record_decision(run_id, "returned", "wrong claim", ["source.txt:1"])
        self.assertEqual(returned["decision"]["resolution"], "revision_requested")
        for decision, resolution, summary in [("accepted", "completed_by_codex", "done"), ("returned", "completed_by_codex", ""), ("returned", None, "done")]:
            with self.assertRaises(ValueError):
                self.runtime.record_decision(run_id, decision, "reason", ["source.txt:1"], resolution, summary)
        completed = self.runtime.record_decision(run_id, "returned", "wrong claim", ["source.txt:1"], "completed_by_codex", "Codex checked the exact file and completed review")
        self.assertEqual(completed["decision"]["decision"], "returned")
        self.assertEqual(len(completed["decision_history"]), 2)
        self.assertEqual(completed["decision_history"][0], returned["decision"])
        restarted = Runtime(self.runtime.state_root)
        try:
            recovered = restarted.snapshot(run_id)
            self.assertEqual(recovered["decision"], completed["decision"])
            self.assertEqual(recovered["result"]["structured"]["summary"], "done")
        finally:
            restarted.close()

    def test_unchanged_dirty_files_and_legacy_projection_do_not_rewrite_registry(self):
        (self.repo / "base.txt").write_text("preexisting edit\n")
        run_id = self.runtime.start(self.packet())["run_id"]
        final = self.finish(run_id)["snapshot"]
        self.assertEqual(final["status"], "reported")
        self.assertEqual(final["changed_files"], [])
        self.assertEqual(final["workspace_changes"]["preexisting_dirty_files"], ["base.txt"])
        def legacy_record(data):
            data["runs"][run_id].pop("workspace_changes", None)
            data["runs"][run_id]["changed_files"] = ["base.txt"]
        self.runtime._update(legacy_record)
        original = self.runtime.registry_path.read_bytes()
        listed = self.runtime.list_runs()[0]
        self.assertIsNone(listed["changed_files"])
        self.assertEqual(listed["workspace_changes"]["status"], "unknown")
        for _ in range(2):
            self.assertEqual(self.runtime.snapshot(run_id)["changed_files"], [])
        self.assertEqual(self.runtime.registry_path.read_bytes(), original)

    def test_snapshot_delta_detects_same_status_content_edits_rename_and_restore(self):
        packet = self.packet()
        (self.repo / "base.txt").write_text("dirty before\n")
        before = bridge.git_snapshot(self.repo, packet)
        (self.repo / "base.txt").write_text("different dirty content\n")
        (self.repo / "new.txt").write_text("new\n")
        after = bridge.git_snapshot(self.repo, packet)
        self.assertEqual(workspace_changes(before, after)["changed_files"], ["base.txt", "new.txt"])
        subprocess.run(["git", "-C", str(self.repo), "restore", "base.txt"], check=True)
        self.assertIn("base.txt", workspace_changes(after, bridge.git_snapshot(self.repo, packet))["changed_files"])
        subprocess.run(["git", "-C", str(self.repo), "mv", "base.txt", "renamed.txt"], check=True)
        self.assertEqual(workspace_changes(before, bridge.git_snapshot(self.repo, packet))["changed_files"], ["base.txt", "new.txt", "renamed.txt"])
        self.assertIsNone(workspace_changes(before, None)["changed_files"])
        unlocated = {**before, "diff_hash": "different-index"}
        self.assertEqual(workspace_changes(before, unlocated)["status"], "unknown")

    def test_failed_bridge_launch_has_unknown_file_impact_without_run_directory(self):
        original_popen = subprocess.Popen
        def fail_bridge(command, *args, **kwargs):
            if "--packet" in command:
                raise OSError("fixture bridge launch failed")
            return original_popen(command, *args, **kwargs)
        with patch.object(subprocess, "Popen", new=fail_bridge):
            with self.assertRaisesRegex(OSError, "fixture bridge launch failed"):
                self.runtime.start(self.packet())
        record = self.runtime.list_runs()[0]
        self.assertEqual(record["status"], "failed")
        self.assertFalse(Path(record["run_dir"]).exists())
        self.assertIsNone(record["changed_files"])
        snapshot = self.runtime.snapshot(record["run_id"])
        self.assertIsNone(snapshot["changed_files"])
        self.assertEqual(snapshot["workspace_changes"]["status"], "unknown")

    def test_owned_terminal_waits_for_log_archive_before_publication(self):
        archive_entered = threading.Event()
        archive_release = threading.Event()
        original_archive = self.runtime._archive_bridge_logs

        def gated_archive(run_id):
            archive_entered.set()
            archive_release.wait(5)
            return original_archive(run_id)

        try:
            with patch.object(self.runtime, "_archive_bridge_logs", side_effect=gated_archive):
                start = self.runtime.start(self.packet())
                self.assertTrue(archive_entered.wait(5), "watcher did not reach the log archive gate")
                held_snapshot = self.runtime.snapshot(start["run_id"])
                held_wait = self.runtime.wait(start["run_id"], after=10**9, timeout=0)["snapshot"]
                self.assertEqual(held_snapshot["status"], "collecting")
                self.assertEqual(held_snapshot["phase"], "collecting")
                self.assertEqual(held_wait["status"], "collecting")
                self.assertFalse((Path(start["run_dir"]) / "runtime_bridge.stdout.log").exists())
                archive_release.set()
                final = self.finish(start["run_id"])["snapshot"]
        finally:
            archive_release.set()

        self.assertEqual(final["status"], "reported")
        self.assertTrue((Path(final["run_dir"]) / "runtime_bridge.stdout.log").exists())

    def test_outer_bridge_exit_cannot_publish_receipt_without_stopped_group_evidence(self):
        run_id = "run-untrusted_receipt"
        run_dir = self.runtime.runs_root / run_id; run_dir.mkdir()
        (run_dir / "state.json").write_text(json.dumps({"status":"completed"}))
        (run_dir / "receipt.json").write_text(json.dumps({"status":"reported", "task_id":"receipt", "revision":1}))
        record = {"run_id":run_id, "task_id":"receipt", "revision":1, "cwd":str(self.repo),
                  "run_dir":str(run_dir), "status":"running", "phase":"executing",
                  "started_at":time.time(), "updated_at":time.time()}
        self.runtime._update(lambda data: data.setdefault("runs", {}).__setitem__(run_id, record))

        self.runtime._refresh(run_id, exit_code=0, owned=True, trusted_terminal=None)

        final = self.runtime._registry()["runs"][run_id]
        self.assertEqual(final["status"], "unknown")
        self.assertIn("without trustworthy stopped-process terminal evidence", final["summary"])

    def test_cross_runtime_same_cwd_is_exclusive_and_cancel_is_marker_based(self):
        os.environ["RUNTIME_FAKE_SLEEP"] = "1"
        first = self.runtime.start(self.packet())
        second_runtime = Runtime(self.root / "second-state")
        self.addCleanup(second_runtime.close)
        with self.assertRaises(RuntimeError): second_runtime.start(self.packet(revision=2))
        cancelling = self.runtime.cancel(first["run_id"], "test stop")
        self.assertIn(cancelling["status"], {"cancelling", "cancelled"})
        self.assertEqual(self.finish(first["run_id"])["snapshot"]["status"], "cancelled")

    def test_restarted_runtime_can_request_file_based_cancel(self):
        os.environ["RUNTIME_FAKE_SLEEP"] = "3"
        first = self.runtime.start(self.packet())
        restarted = Runtime(self.root / "runtime-state")
        self.addCleanup(restarted.close)
        cancelling = restarted.cancel(first["run_id"], "replacement coordinator stop")
        self.assertIn(cancelling["status"], {"cancelling", "cancelled"})
        self.assertEqual(cancelling["cancel_requested_by"], restarted.owner_id)
        self.assertTrue((Path(first["run_dir"]) / "cancel.json").is_file())
        self.assertTrue((self.root / "runtime-state" / "cancel-requests" / f"{first['run_id']}.json").is_file())
        self.assertEqual(self.finish(first["run_id"])["snapshot"]["status"], "cancelled")

    def test_cancel_queue_cannot_overwrite_a_concurrent_terminal_record(self):
        run_id = "run-cancel_race"
        record = {"run_id":run_id, "task_id":"race", "revision":1, "cwd":str(self.repo),
                  "run_dir":str(self.runtime.runs_root / run_id), "status":"starting", "phase":"starting",
                  "started_at":time.time(), "updated_at":time.time()}
        self.runtime._update(lambda data: data.setdefault("runs", {}).__setitem__(run_id, record))
        original_update = self.runtime._update
        entered = False

        def terminal_before_cancel_mutation(mutate):
            nonlocal entered
            if not entered:
                entered = True
                original_update(lambda data: data["runs"][run_id].update(
                    status="reported", phase="reported", ended_at=time.time(), summary="terminal won"))
            return original_update(mutate)

        with patch.object(self.runtime, "_sync_unowned"), \
             patch.object(self.runtime, "_update", side_effect=terminal_before_cancel_mutation), \
             patch("runtime.time.monotonic", side_effect=[0.0, 4.0]):
            result = self.runtime.cancel(run_id, "fixture race")
        self.assertEqual(result["status"], "reported")
        self.assertEqual(self.runtime._registry()["runs"][run_id]["status"], "reported")

    def test_close_fallback_cannot_overwrite_a_concurrent_terminal_record(self):
        run_id = "run-close_race"
        record = {"run_id":run_id, "task_id":"race", "revision":1, "cwd":str(self.repo),
                  "run_dir":str(self.runtime.runs_root / run_id), "status":"running", "phase":"executing",
                  "started_at":time.time(), "updated_at":time.time()}
        self.runtime._update(lambda data: data.setdefault("runs", {}).__setitem__(run_id, record))
        self.runtime._workers[run_id] = (mock.Mock(), mock.Mock())
        original_update = self.runtime._update
        entered = False

        def terminal_before_close_mutation(mutate):
            nonlocal entered
            if not entered:
                entered = True
                original_update(lambda data: data["runs"][run_id].update(
                    status="reported", phase="reported", ended_at=time.time(), summary="terminal won"))
            return original_update(mutate)

        try:
            with patch.object(self.runtime, "cancel"), \
                 patch.object(self.runtime, "_update", side_effect=terminal_before_close_mutation), \
                 patch("runtime.time.monotonic", side_effect=[0.0, 9.0]):
                self.runtime.close()
            self.assertEqual(self.runtime._registry()["runs"][run_id]["status"], "reported")
            self.assertFalse(self.runtime._unknown_marker(str(self.repo)).exists())
        finally:
            self.runtime._workers.pop(run_id, None)
            self.runtime._closing = False

    def test_live_snapshot_revision_resume_and_decision_guards(self):
        os.environ["RUNTIME_FAKE_SLEEP"] = "1"
        first = self.runtime.start(self.packet())
        deadline = time.monotonic() + 2
        live = self.runtime.snapshot(first["run_id"])
        while live["events_count"] == 0 and time.monotonic() < deadline:
            time.sleep(.05); live = self.runtime.snapshot(first["run_id"])
        self.assertIn(live["phase"], {"preflight", "starting", "executing", "running", "collecting"})
        self.assertGreaterEqual(live["events_count"], 1)
        # The duplicate is rejected either by the immutable revision rule or,
        # while the first worker owns the lane, by the earlier CWD exclusion.
        with self.assertRaises((ValueError, RuntimeError)): self.runtime.start(self.packet())
        self.finish(first["run_id"])
        with self.assertRaises(ValueError): self.runtime.record_decision(first["run_id"], "accepted", "missing", [])
        correction = {"finding_id":"f-1", "kind":"local", "reason":"fix", "attempt":1}
        resumed_packet = self.packet(revision=2); resumed_packet["correction"] = correction
        resumed = self.runtime.start(resumed_packet, resume_run_id=first["run_id"])
        self.assertEqual(resumed["previous_run_id"], first["run_id"])
        self.finish(resumed["run_id"])
        with self.assertRaises(RuntimeError):
            self.runtime.record_decision(first["run_id"], "accepted", "obsolete", ["old evidence"])
        with self.assertRaises(RuntimeError):
            self.runtime.start({**resumed_packet, "revision":3}, resume_run_id=first["run_id"])

    def test_abrupt_wrapper_exit_remains_unknown_and_blocks_a_new_runtime(self):
        os.environ["RUNTIME_FAKE_SLEEP"] = "1"
        first = self.runtime.start(self.packet())
        worker = self.runtime._workers[first["run_id"]][0]
        worker.kill()
        worker.wait(timeout=3)
        deadline = time.monotonic() + 3
        while first["run_id"] in self.runtime._workers and time.monotonic() < deadline:
            time.sleep(.05)
        result = self.runtime.snapshot(first["run_id"])
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["phase"], "unknown")
        other = Runtime(self.root / "separate-registry")
        self.addCleanup(other.close)
        with self.assertRaises(RuntimeError):
            other.start(self.packet(revision=2))

    def test_restart_marks_unowned_active_record_unknown_only_when_lane_is_free(self):
        run_id = "run-stale_123"
        state = self.root / "restart-state"; state.mkdir()
        (state / "registry.json").write_text(json.dumps({"runs": {run_id: {"run_id":run_id,"task_id":"x","revision":1,"cwd":str(self.repo),"status":"running","phase":"executing","started_at":time.time(),"updated_at":time.time()}}}))
        refreshed = Runtime(state)
        self.addCleanup(refreshed.close)
        self.assertEqual(refreshed.snapshot(run_id)["status"], "unknown")
        with self.assertRaises(RuntimeError): refreshed.start(self.packet(revision=2))

    def test_event_count_is_not_capped_at_page_limit(self):
        start = self.runtime.start(self.packet())
        final = self.finish(start["run_id"])["snapshot"]
        run_dir = Path(final["run_dir"])
        for i in range(1005): last_event = append(run_dir, "test", f"event {i}")
        refreshed = self.runtime.snapshot(start["run_id"])
        self.assertGreaterEqual(refreshed["events_count"], 1009)
        self.assertEqual(refreshed["last_activity_at"], last_event["received_at"])
        page = self.runtime.events(start["run_id"], limit=100)
        self.assertEqual(len(page["events"]), 100)
        self.assertTrue(page["has_more"])

    def test_terminal_snapshot_and_history_read_do_not_rewrite_registry(self):
        start = self.runtime.start(self.packet())
        self.finish(start["run_id"])
        before = self.runtime.registry_path.read_bytes()
        self.runtime.snapshot(start["run_id"])
        self.runtime.list_runs()
        self.assertEqual(self.runtime.registry_path.read_bytes(), before)

    def test_stale_active_snapshot_cannot_overwrite_terminal_watch_result(self):
        os.environ["RUNTIME_FAKE_SLEEP"] = "1"
        start = self.runtime.start(self.packet())
        run_id = start["run_id"]
        run_dir = Path(start["run_dir"])
        deadline = time.monotonic() + 3
        while not (run_dir / "child.json").exists() and time.monotonic() < deadline:
            time.sleep(.02)
        self.assertFalse((run_dir / "receipt.json").exists())
        original = Runtime._live_provider_identity
        snapshot_thread = threading.get_ident()

        def interleave(path):
            if threading.get_ident() == snapshot_thread:
                deadline_ = time.monotonic() + 5
                while self.runtime._registry()["runs"][run_id]["status"] != "reported" and time.monotonic() < deadline_:
                    time.sleep(.02)
            return original(path)

        with patch.object(Runtime, "_live_provider_identity", staticmethod(interleave)):
            snapshot = self.runtime.snapshot(run_id)
        self.assertEqual(snapshot["status"], "reported")
        self.assertEqual(self.runtime._registry()["runs"][run_id]["status"], "reported")

    def test_unchanged_active_snapshot_does_not_enter_registry_update(self):
        os.environ["RUNTIME_FAKE_SLEEP"] = "3"
        start = self.runtime.start(self.packet())
        run_id = start["run_id"]
        deadline = time.monotonic() + 2
        snapshot = self.runtime.snapshot(run_id)
        while snapshot["phase"] != "executing" and time.monotonic() < deadline:
            time.sleep(.03)
            snapshot = self.runtime.snapshot(run_id)
        self.assertEqual(snapshot["phase"], "executing")
        time.sleep(.10)
        self.runtime.snapshot(run_id)  # persist the stable executing observation once
        original = self.runtime._update
        calls = []

        def counting(mutate):
            calls.append(1)
            return original(mutate)

        self.runtime._update = counting
        try:
            for _ in range(5):
                self.runtime.snapshot(run_id)
        finally:
            self.runtime._update = original
        self.assertEqual(calls, [])
        self.runtime.cancel(run_id, "test complete")
        self.finish(run_id)

    def test_decision_changes_preserve_complete_history(self):
        start = self.runtime.start(self.packet())
        self.finish(start["run_id"])
        first = self.runtime.record_decision(start["run_id"], "accepted", "first review", ["evidence one"])
        second = self.runtime.record_decision(start["run_id"], "returned", "new evidence", ["evidence two"])
        history = second["decision_history"]
        self.assertEqual([item["decision"] for item in history], ["accepted", "returned"])
        self.assertEqual(first["decision_history"][0]["reason"], "first review")
        persisted = json.loads((Path(start["run_dir"]) / "decision-history.json").read_text())
        self.assertEqual(persisted, history)

    def test_registry_failure_after_bridge_popen_becomes_recoverable_unknown(self):
        os.environ["RUNTIME_FAKE_SLEEP"] = "5"
        original_update = self.runtime._update
        original_watch = self.runtime._watch
        reap_gate = threading.Event()
        calls = 0

        def gated_watch(*args):
            reap_gate.wait(5)
            with patch.object(self.runtime, "_refresh", side_effect=OSError("fixture terminal refresh failure")):
                return original_watch(*args)

        def fail_bridge_identity_update(mutate):
            nonlocal calls
            calls += 1
            if calls == 2:
                self.wait_for_child()
                raise OSError("fixture registry write failure after bridge Popen")
            return original_update(mutate)

        with patch.object(self.runtime, "_watch", new=gated_watch), patch.object(
                self.runtime, "_update", side_effect=fail_bridge_identity_update):
            with self.assertRaisesRegex(OSError, "after bridge Popen"):
                self.runtime.start(self.packet())
        registry = self.runtime._registry()["runs"]
        self.assertEqual(len(registry), 1)
        run_id, record = next(iter(registry.items()))
        try:
            self.assertEqual(record["status"], "unknown")
            self.assertEqual(record["supervision_failure"]["stage"], "post_bridge_popen")
            self.assertIsInstance(record.get("bridge_pid"), int)
            self.assertEqual(record.get("bridge_process_group"), record.get("bridge_pid"))
            self.assertIn(run_id, self.runtime._workers)
            self.assertTrue(self.runtime._unknown_marker(str(self.repo)).is_file())
            self.assertTrue((self.root / "runtime-state" / "cancel-requests" / f"{run_id}.json").is_file())
            self.assertTrue((Path(record["run_dir"]) / "cancel.json").is_file())
            with patch.object(self.runtime, "_trusted_terminal", return_value=None):
                with self.assertRaises(RuntimeError):
                    self.runtime.start(self.packet(revision=2))
        finally:
            reap_gate.set()
        deadline = time.monotonic() + 8
        while run_id in self.runtime._workers and time.monotonic() < deadline:
            time.sleep(.05)
        recovered = self.wait_recovered_terminal(run_id)
        self.assertEqual(recovered["status"], "cancelled")
        self.assertNotIn(run_id, self.runtime._workers)
        lane = self.runtime._lane_lock(str(self.repo)); self.runtime._release_lane(str(self.repo), lane)
        fresh = self.runtime.start(self.packet(revision=2))
        self.assertEqual(self.finish(fresh["run_id"])["snapshot"]["status"], "reported")

    def test_thread_start_failure_after_bridge_popen_requests_cancel_and_does_not_leak_worker(self):
        os.environ["RUNTIME_FAKE_SLEEP"] = "5"
        original_watch = self.runtime._watch
        original_thread_start = threading.Thread.start
        reap_gate = threading.Event()
        start_calls = 0

        def gated_watch(*args):
            reap_gate.wait(5)
            return original_watch(*args)

        def fail_first_watcher_start(thread):
            nonlocal start_calls
            start_calls += 1
            if start_calls == 1:
                self.wait_for_child()
                raise RuntimeError("fixture watcher thread start failure")
            return original_thread_start(thread)

        with patch.object(self.runtime, "_watch", new=gated_watch), patch.object(
                threading.Thread, "start", new=fail_first_watcher_start):
            with self.assertRaisesRegex(RuntimeError, "watcher thread start failure"):
                self.runtime.start(self.packet())
        registry = self.runtime._registry()["runs"]
        self.assertEqual(len(registry), 1)
        run_id, record = next(iter(registry.items()))
        try:
            self.assertEqual(record["status"], "unknown")
            self.assertEqual(record["supervision_failure"]["stage"], "post_bridge_popen")
            self.assertIsInstance(record.get("bridge_pid"), int)
            self.assertIn(run_id, self.runtime._workers)
            self.assertTrue(self.runtime._unknown_marker(str(self.repo)).is_file())
            self.assertTrue((self.root / "runtime-state" / "cancel-requests" / f"{run_id}.json").is_file())
            self.assertTrue((Path(record["run_dir"]) / "cancel.json").is_file())
            with patch.object(self.runtime, "_trusted_terminal", return_value=None):
                with self.assertRaises(RuntimeError):
                    self.runtime.start(self.packet(revision=2))
        finally:
            reap_gate.set()
        recovered = self.wait_recovered_terminal(run_id)
        self.assertEqual(recovered["status"], "cancelled")
        self.assertNotIn(run_id, self.runtime._workers)
        child = json.loads((Path(record["run_dir"]) / "child.json").read_text())
        self.assertEqual(self.runtime._process_presence(child["process_group"], group=True, label="fixture child")["state"], "stopped")
        lane = self.runtime._lane_lock(str(self.repo)); self.runtime._release_lane(str(self.repo), lane)

    def test_both_watcher_starts_failing_leave_durable_unknown_without_local_handles(self):
        os.environ["RUNTIME_FAKE_SLEEP"] = "5"
        original_popen = subprocess.Popen
        processes = []

        def capture_popen(*args, **kwargs):
            proc = original_popen(*args, **kwargs)
            processes.append(proc)
            return proc

        def fail_watcher_start(_thread):
            if not processes:
                self.fail("fixture watcher start ran before bridge Popen")
            self.wait_for_child()
            raise RuntimeError("fixture system cannot create watcher thread")

        with patch.object(subprocess, "Popen", new=capture_popen), patch.object(
                threading.Thread, "start", new=fail_watcher_start):
            with self.assertRaisesRegex(RuntimeError, "cannot create watcher thread"):
                self.runtime.start(self.packet())

        registry = self.runtime._registry()["runs"]
        self.assertEqual(len(registry), 1)
        run_id, record = next(iter(registry.items()))
        self.assertEqual(record["status"], "unknown")
        self.assertEqual(record["supervision_failure"]["stage"], "post_bridge_popen")
        self.assertNotIn(run_id, self.runtime._workers)
        self.assertNotIn(str(self.repo.resolve()), _LOCAL_LANES)
        self.assertTrue(self.runtime._unknown_marker(str(self.repo)).is_file())
        self.assertTrue((self.root / "runtime-state" / "cancel-requests" / f"{run_id}.json").is_file())
        self.assertTrue((Path(record["run_dir"]) / "cancel.json").is_file())
        with self.assertRaises(RuntimeError):
            self.runtime.start(self.packet(revision=2))

        processes[0].wait(timeout=8)
        recovered = self.runtime.snapshot(run_id)
        self.assertEqual(recovered["status"], "cancelled")
        child = json.loads((Path(record["run_dir"]) / "child.json").read_text())
        self.assertEqual(self.runtime._process_presence(child["process_group"], group=True, label="fixture child")["state"], "stopped")
        lane = self.runtime._lane_lock(str(self.repo)); self.runtime._release_lane(str(self.repo), lane)


if __name__ == "__main__": unittest.main(verbosity=2)
