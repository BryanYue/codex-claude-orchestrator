"""Exercise rich review through the real Bridge with a deterministic local CLI."""
import json
import os
import signal
import sys
import time
import threading
import subprocess
from pathlib import Path
import unittest
from unittest import mock

import test_bridge as fixtures


@unittest.skipUnless(sys.platform == "darwin", "OS source-write boundary currently supports macOS")
class IsolatedReviewTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.BridgeTests(methodName="runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.root = self.fixture.root
        self.enterContext(mock.patch.dict(os.environ, {"ISOLATED_MODE": "normal"}))
        self.fixture.fake.write_text(f"#!{sys.executable}\n" + r'''
import json, os, pathlib, subprocess, sys
args=sys.argv[1:]
if args==['--version']: print('2.1.287 (fixture)'); sys.exit()
if args==['--help']:
    print('-p --model --effort --output-format --verbose --json-schema --session-id --resume --permission-mode --tools --allowedTools --disallowedTools --settings --strict-mcp-config --mcp-config --disable-slash-commands --no-session-persistence --max-turns --max-budget-usd'); sys.exit()
if args==['auth','status','--json']:
    print(json.dumps({'loggedIn':True,'authMethod':'claude.ai','apiProvider':'firstParty'})); sys.exit()
def option(name): return args[args.index(name)+1] if name in args else None
session=option('--resume') or option('--session-id')
mode=os.environ.get('ISOLATED_MODE','normal')
prompt=json.load(sys.stdin); packet=prompt['packet']; report=pathlib.Path(packet['review_report_path'])
settings=json.loads(pathlib.Path(option('--settings')).read_text()); hook=settings['hooks']['PreToolUse'][0]['hooks'][0]['command']
def emit(value): print(json.dumps({'session_id':session,'parent_tool_use_id':None,**value}),flush=True)
def use(name,ident,input_):
    emit({'type':'assistant','message':{'model':'fixture-model','content':[{'type':'tool_use','id':ident,'name':name,'input':input_}]}})
    if mode!='missing_hook':
        checked=subprocess.run(['/bin/sh','-c',hook],input=json.dumps({'tool_name':name,'tool_input':input_,'tool_use_id':ident}),text=True,capture_output=True)
        assert checked.returncode==0,(checked.stdout,checked.stderr)
    emit({'type':'user','content':[{'type':'tool_result','tool_use_id':ident,'is_error':ident=='workflow-failed','content':'fixture tool completed'}]})
emit({'type':'system','subtype':'init','model':'fixture-model'})
use('Bash','bash-1',{'command':'test writable copy and protected source'})
source=pathlib.Path(packet['requirement_sources'][0])
source_cwd=pathlib.Path(os.environ['SOURCE_REPO'])
protected=[source_cwd/'base.txt',source,source_cwd/'.git'/'config',pathlib.Path(option('--settings')).parent/'receipt.json']
if option('--resume'):
    link=json.loads((pathlib.Path(option('--settings')).parent/'review-workspace-link.json').read_text())
    protected += [pathlib.Path(folder)/name for folder in link['prior_run_dirs'] for name in ('result.json','packet.json','review-report.json','receipt.json')]
protected += [pathlib.Path(option('--settings')).parent/name for name in ('stream.jsonl','review-report.json','diff.patch')]
assert not any(name.startswith('CODEX_BRIDGE_') for name in os.environ), 'bridge control environment leaked'
state_root=os.environ.get('SOURCE_STATE_ROOT')
if state_root:
    state_root=pathlib.Path(state_root)
    protected += [state_root/'registry.json', state_root/'packets'/'forged.json', state_root/'recovery-receipts'/'forged.json', state_root/'runs'/'other-run'/'result.json', state_root/'venvs'/'forged.py', state_root/'bridge-logs'/'forged.log']
    for parent in ('recovery-receipts',):
        try: (state_root/parent).mkdir(exist_ok=True)
        except PermissionError: pass
    for target in protected[-6:]:
        try: target.write_text('{"decision":"accepted"}')
        except (PermissionError,FileNotFoundError): pass
        else: raise AssertionError('Runtime control write allowed: '+str(target))
    protected=protected[:-6]
    lifecycle=state_root/'bridge-receipts'/(pathlib.Path(option('--settings')).parent.name+'.json')
    assert lifecycle.is_file(), 'Runtime lifecycle fixture missing'
    try:
        with lifecycle.open('r+b'): pass
    except PermissionError: pass
    else: raise AssertionError('lifecycle write access allowed')
for target in json.loads(os.environ.get('SOURCE_GUARD_TARGETS','[]')):
    # Opening for update probes permission without corrupting executing code if a guard regresses.
    try:
        with pathlib.Path(target).open('r+b'): pass
    except PermissionError: pass
    else: raise AssertionError('executing code or lane control write access allowed: '+target)
for target in protected:
    try: target.write_text('CORRUPTED')
    except PermissionError: pass
    else: raise AssertionError('write protection failed: '+str(target))
pathlib.Path('base.txt').write_text('reviewer edit in copy')
if option('--resume'):
    assert pathlib.Path('retained.txt').read_text()=='previous copy'
else: pathlib.Path('retained.txt').write_text('previous copy')
if mode in ('workflow','incomplete_workflow','workflow_retry'):
    if mode=='workflow_retry': use('Workflow','workflow-failed',{'scriptPath':'/generated/bad-review.js'})
    use('Workflow','workflow-1',{'scriptPath':'/generated/inline-review.js'})
    emit({'type':'system','subtype':'task_started','tool_use_id':'workflow-1','task_id':'task-w','task_type':'local_workflow'})
    if mode in ('workflow','workflow_retry'): emit({'type':'system','subtype':'task_notification','tool_use_id':'workflow-1','task_id':'task-w','status':'completed'})
if mode=='external_source_change':
    import time
    pathlib.Path(os.environ['SOURCE_CHANGE_READY']).write_text('ready')
    deadline=time.monotonic()+5
    while not pathlib.Path(os.environ['SOURCE_CHANGE_DONE']).exists() and time.monotonic()<deadline: time.sleep(.01)
if mode=='signal_wait':
    import time
    pathlib.Path(os.environ['SOURCE_SIGNAL_READY']).write_text('ready')
    time.sleep(30)
full={'status':'completed','summary':'complete review','evidence':['base.txt:1'],'checks':['source writes denied; copy write succeeded'],
      'unresolved':[],'coverage':['correctness','architecture'],'findings':[{'id':'F1','category':'design','confidence':'code_confirmed','summary':'Inspect the responsibility boundary','evidence':['base.txt:1']}]}
if mode=='fifo': os.mkfifo(report)
elif mode!='missing_report': report.write_text(json.dumps(full))
parent={**full,'summary':'parent projection','findings':[]}
emit({'type':'result','subtype':'success','is_error':False,'structured_output':parent})
''')
        self.enterContext(mock.patch.dict(os.environ, {"SOURCE_REPO": str(self.fixture.repo.resolve())}))

    def run_review(self, mode="normal", name="isolated", resume=None, extra=None):
        packet = {**self.fixture.packet(revision=json.loads((resume / "packet.json").read_text())["revision"] + 1 if resume else 1), "user_request": "原话：整体审查，保留所有发现。", "review_mode": "isolated"}
        packet.update(extra or {})
        if resume:
            packet["correction"] = {"kind": "local", "finding_id": "F1", "reason": "verify source again", "attempt": 1}
        with mock.patch.dict(os.environ, {"ISOLATED_MODE": mode}):
            got, run = self.fixture.invoke(packet, name, resume, timeout="10")
        error = (run / "error.json").read_text() if (run / "error.json").exists() else ""
        self.assertTrue((run / "result.json").is_file(), got.stdout + got.stderr + error)
        return got, run, json.loads((run / "result.json").read_text())

    def test_copy_source_protection_and_full_report_are_end_to_end(self):
        got, run, result = self.run_review()
        self.assertEqual(got.returncode, 0, got.stdout + got.stderr)
        self.assertEqual(json.loads((run / "receipt.json").read_text())["status"], "reported")
        self.assertEqual((self.fixture.repo / "base.txt").read_text(), "base\n")
        self.assertEqual(result["structured"]["findings"][0]["id"], "F1")
        self.assertEqual(result["parent_structured"]["findings"], [])
        self.assertEqual(result["hook_guard_coverage"]["status"], "complete")
        self.assertEqual(result["review_report"]["status"], "delivered")
        self.assertEqual(json.loads((run / "workspace_before.json").read_text()), json.loads((run / "workspace_after.json").read_text()))
        argv = json.loads((run / "command.json").read_text())["argv"]
        self.assertEqual("default", argv[argv.index("--tools") + 1])
        for tool in ("Bash", "Agent", "Workflow", "Skill", "WebFetch", "WebSearch", "Write"):
            self.assertIn(tool, argv[argv.index("--allowedTools") + 1].split(","))
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "dontAsk")
        self.assertNotIn("--disallowedTools", argv)
        self.assertIn("reviewer edit in copy", (run / "review-copy.diff.patch").read_text())
        self.assertIn("--strict-mcp-config", argv)

    def test_runtime_state_cannot_be_forged_by_review_tools(self):
        sys.path.insert(0, str(fixtures.BRIDGE.parent))
        import runtime
        import lane_lock
        coordinator = runtime.Runtime(self.root / "state")
        try:
            packet = {**self.fixture.packet(), "user_request": "原话：完整审查", "review_mode": "isolated"}
            lane = runtime.bridge.lane_identity(self.fixture.repo)
            controls = [lane_lock.durable_lock_path(lane), lane_lock.legacy_lock_path(lane)]
            for root in lane_lock.marker_roots():
                marker = root / ("write-probe-" + self.root.name)
                marker.write_text("probe")
                self.addCleanup(marker.unlink, missing_ok=True)
                controls.append(marker)
            with mock.patch.dict(os.environ, {"SOURCE_STATE_ROOT": str(coordinator.state_root),
                 "SOURCE_GUARD_TARGETS": json.dumps([str(path) for path in [fixtures.BRIDGE,
                     fixtures.BRIDGE.parents[3] / "scripts/shared_io.py", Path(sys.executable).resolve(), *controls]])}):
                created = coordinator.start(packet)
                self.assertEqual(created["timeout_seconds"], 3600)
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                snapshot = coordinator.snapshot(created["run_id"])
                if snapshot["status"] not in runtime.ACTIVE:
                    break
                time.sleep(.05)
            self.assertEqual(snapshot["status"], "reported", snapshot)
            self.assertFalse(snapshot.get("decision"))
            self.assertEqual(snapshot["result"]["structured"]["findings"][0]["id"], "F1")
            self.assertFalse((coordinator.packets_root / "forged.json").exists())
            deadline = time.monotonic() + 3
            while created["run_id"] in coordinator._workers and time.monotonic() < deadline:
                time.sleep(.02)
            with self.assertRaisesRegex(RuntimeError, "accepted"):
                coordinator.cleanup_review(created["run_id"])
            run_dir = Path(snapshot["run_dir"])
            report_bytes = (run_dir / "review-report.json").read_bytes()
            coordinator.record_decision(created["run_id"], "accepted", "fixture verified", ["source protection and captured report"],
                                        finding_decisions=[{"finding_id": "F1", "disposition": "accepted"}])
            removed = coordinator.cleanup_review(created["run_id"])
            self.assertEqual(removed["state"], "removed")
            self.assertFalse(Path(removed["workspace_root"]).exists())
            self.assertEqual((run_dir / "review-report.json").read_bytes(), report_bytes)
            self.assertEqual(coordinator.cleanup_review(created["run_id"]), removed)
        finally:
            coordinator.close()

    def test_sigterm_reaps_provider_under_actual_os_wrapper(self):
        sys.path.insert(0, str(fixtures.BRIDGE.parent))
        import bridge
        packet = {**self.fixture.packet(), "user_request": "test isolated shutdown", "review_mode": "isolated"}
        packet_path = self.root / "signal-packet.json"
        packet_path.write_text(json.dumps(packet))
        run = self.root / "signal-run"
        ready = self.root / "signal-ready"
        child = None
        with mock.patch.dict(os.environ, {"ISOLATED_MODE": "signal_wait", "SOURCE_SIGNAL_READY": str(ready)}):
            proc = subprocess.Popen([sys.executable, str(fixtures.BRIDGE), "run", "--packet", str(packet_path),
                                     "--run-dir", str(run), "--timeout", "30"],
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            deadline = time.monotonic() + 10
            while not ready.exists() and proc.poll() is None and time.monotonic() < deadline:
                time.sleep(.02)
            self.assertTrue(ready.exists(), "provider never reached protected execution")
            child = json.loads((run / "child.json").read_text())["pid"]
            self.assertEqual(json.loads((run / "command.json").read_text())["argv"][0], "/usr/bin/sandbox-exec")
            proc.send_signal(signal.SIGTERM)
            out, err = proc.communicate(timeout=15)
            self.assertEqual(proc.returncode, 1, out + err)
            self.assertEqual(json.loads((run / "receipt.json").read_text())["status"], "cancelled")
            self.assertEqual(json.loads((run / "process-family.json").read_text())["state"], "stopped")
            with self.assertRaises(ProcessLookupError):
                os.killpg(child, 0)
            self.assertFalse(bridge.unknown_markers(bridge.lane_identity(self.fixture.repo)))
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.communicate(timeout=5)
            if child is not None:
                try:
                    os.killpg(child, signal.SIGKILL)
                except ProcessLookupError:
                    pass

    def test_workflow_attempts_are_audited_without_discarding_delivered_report(self):
        for mode, expected in (("workflow", "reported"), ("incomplete_workflow", "reported")):
            with self.subTest(mode=mode):
                _, run, result = self.run_review(mode, mode)
                self.assertEqual(json.loads((run / "receipt.json").read_text())["status"], expected)
                self.assertEqual(result["workflow_tool_use_count"], 1)
                self.assertEqual(result["workflow_completion_observed"], mode == "workflow")
                if mode == "incomplete_workflow":
                    self.assertIn("not observed", result["workflow_warning"])

    def test_failed_workflow_retry_keeps_both_attempts_and_report(self):
        _, run, result = self.run_review("workflow_retry")
        self.assertEqual(json.loads((run / "receipt.json").read_text())["status"], "reported")
        self.assertEqual(result["workflow_tool_use_count"], 2)
        self.assertIn("failed", result["workflow_warning"])
        self.assertEqual(result["review_report"]["status"], "delivered")

    def test_source_mutation_by_another_actor_still_fails_after_valid_report(self):
        ready, done = self.root / "ready", self.root / "done"
        def change_source():
            deadline = time.monotonic() + 8
            while not ready.exists() and time.monotonic() < deadline:
                time.sleep(.01)
            if ready.exists():
                (self.fixture.repo / "base.txt").write_text("concurrent source edit")
                done.write_text("changed")
        worker = threading.Thread(target=change_source)
        worker.start()
        try:
            with mock.patch.dict(os.environ, {"SOURCE_CHANGE_READY": str(ready), "SOURCE_CHANGE_DONE": str(done)}):
                _, run, result = self.run_review("external_source_change")
            self.assertEqual(json.loads((run / "receipt.json").read_text())["status"], "failed")
            self.assertEqual(result["review_report"]["status"], "delivered")
            self.assertIn("workspace", result["invariant_error"])
        finally:
            worker.join(10)

    def test_subdirectory_and_gbk_input_roundtrip(self):
        nested = self.fixture.repo / "nested"
        nested.mkdir()
        gbk = nested / "sample.txt"
        gbk.write_bytes("原文".encode("gbk"))
        subprocess.run(["git", "-C", str(self.fixture.repo), "add", "nested"], check=True)
        subprocess.run(["git", "-C", str(self.fixture.repo), "commit", "-qm", "nested GBK"], check=True)
        gbk.write_bytes("新文本".encode("gbk"))
        got, _, result = self.run_review(extra={"cwd": str(nested)})
        self.assertEqual(got.returncode, 0)
        self.assertEqual((Path(result["review_workspace"]["cwd"]) / "sample.txt").read_bytes(), gbk.read_bytes())

    def test_missing_and_fifo_reports_block_without_hanging(self):
        for mode in ("missing_report", "fifo"):
            with self.subTest(mode=mode):
                _, run, result = self.run_review(mode, mode)
                self.assertEqual(json.loads((run / "receipt.json").read_text())["status"], "blocked")
                self.assertEqual(result["review_report"]["status"], "not_collected")

    def test_missing_isolated_hook_is_audit_warning_with_valid_report(self):
        _, run, result = self.run_review("missing_hook")
        self.assertEqual(json.loads((run / "receipt.json").read_text())["status"], "reported")
        self.assertIn("incomplete", result["hook_audit_warning"])
        self.assertEqual(result["review_report"]["status"], "delivered")
        self.assertFalse(result["report_evidence"]["accepted"])

    def test_resume_reuses_copy_and_preserves_original_snapshots(self):
        _, previous, before = self.run_review()
        got, run, after = self.run_review(name="resumed", resume=previous)
        self.assertEqual(got.returncode, 0, got.stdout + got.stderr)
        self.assertEqual(before["review_workspace"]["cwd"], after["review_workspace"]["cwd"])
        self.assertTrue((run / "review-workspace-link.json").is_file())
        original_evidence = (previous / "result.json").read_bytes()
        got, last, final = self.run_review(name="resumed-again", resume=run)
        self.assertEqual(got.returncode, 0, got.stdout + got.stderr)
        self.assertEqual((previous / "result.json").read_bytes(), original_evidence)
        self.assertEqual(final["review_workspace"]["cwd"], before["review_workspace"]["cwd"])
        self.assertEqual((self.fixture.repo / "base.txt").read_text(), "base\n")


if __name__ == "__main__":
    unittest.main()
