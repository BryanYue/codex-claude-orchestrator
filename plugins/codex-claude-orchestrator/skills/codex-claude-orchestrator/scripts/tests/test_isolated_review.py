"""Exercise rich review through the real Bridge with a deterministic local CLI."""
import json
import os
import sys
import time
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
    emit({'type':'user','content':[{'type':'tool_result','tool_use_id':ident,'is_error':False,'content':'fixture tool completed'}]})
emit({'type':'system','subtype':'init','model':'fixture-model'})
use('Bash','bash-1',{'command':'test writable copy and protected source'})
source=pathlib.Path(packet['requirement_sources'][0])
source_cwd=pathlib.Path(os.environ['SOURCE_REPO'])
protected=[source_cwd/'base.txt',source,source_cwd/'.git'/'config',pathlib.Path(option('--settings')).parent/'receipt.json']
if option('--resume'):
    link=json.loads((pathlib.Path(option('--settings')).parent/'review-workspace-link.json').read_text())
    protected += [pathlib.Path(folder)/name for folder in link['prior_run_dirs'] for name in ('result.json','packet.json','review-report.json','receipt.json')]
protected += [pathlib.Path(option('--settings')).parent/name for name in ('stream.jsonl','review-report.json','diff.patch')]
lifecycle=os.environ.get('CODEX_BRIDGE_LIFECYCLE_FILE')
if lifecycle:
    state_root=pathlib.Path(lifecycle).parent.parent
    protected += [state_root/'registry.json', state_root/'packets'/'forged.json', state_root/'recovery-receipts'/'forged.json']
    for parent in ('recovery-receipts',):
        try: (state_root/parent).mkdir(exist_ok=True)
        except PermissionError: pass
    for target in protected[-3:]:
        try: target.write_text('{"decision":"accepted"}')
        except (PermissionError,FileNotFoundError): pass
        else: raise AssertionError('Runtime control write allowed: '+str(target))
    protected=protected[:-3]
for target in protected:
    try: target.write_text('CORRUPTED')
    except PermissionError: pass
    else: raise AssertionError('write protection failed: '+str(target))
pathlib.Path('base.txt').write_text('reviewer edit in copy')
if option('--resume'):
    assert pathlib.Path('retained.txt').read_text()=='previous copy'
else: pathlib.Path('retained.txt').write_text('previous copy')
if mode in ('workflow','incomplete_workflow'):
    use('Workflow','workflow-1',{'scriptPath':'/generated/inline-review.js'})
    emit({'type':'system','subtype':'task_started','tool_use_id':'workflow-1','task_id':'task-w','task_type':'local_workflow'})
    if mode=='workflow': emit({'type':'system','subtype':'task_notification','tool_use_id':'workflow-1','task_id':'task-w','status':'completed'})
full={'status':'completed','summary':'complete review','evidence':['base.txt:1'],'checks':['source writes denied; copy write succeeded'],
      'unresolved':[],'coverage':['correctness','architecture'],'findings':[{'id':'F1','category':'design','confidence':'code_confirmed','summary':'Inspect the responsibility boundary','evidence':['base.txt:1']}]}
if mode=='fifo': os.mkfifo(report)
elif mode!='missing_report': report.write_text(json.dumps(full))
parent={**full,'summary':'parent projection','findings':[]}
emit({'type':'result','subtype':'success','is_error':False,'structured_output':parent})
''')
        self.enterContext(mock.patch.dict(os.environ, {"SOURCE_REPO": str(self.fixture.repo.resolve())}))

    def run_review(self, mode="normal", name="isolated", resume=None):
        packet = {**self.fixture.packet(revision=json.loads((resume / "packet.json").read_text())["revision"] + 1 if resume else 1), "user_request": "原话：整体审查，保留所有发现。", "review_mode": "isolated"}
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
        self.assertIn("Agent", argv[argv.index("--tools") + 1])
        self.assertIn("--strict-mcp-config", argv)

    def test_runtime_state_cannot_be_forged_by_review_tools(self):
        sys.path.insert(0, str(fixtures.BRIDGE.parent))
        import runtime
        coordinator = runtime.Runtime(self.root / "state")
        try:
            packet = {**self.fixture.packet(), "user_request": "原话：完整审查", "review_mode": "isolated"}
            created = coordinator.start(packet, timeout=15)
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                snapshot = coordinator.snapshot(created["run_id"])
                if snapshot["status"] not in {"starting", "running", "cancelling"}:
                    break
                time.sleep(.05)
            self.assertEqual(snapshot["status"], "reported", snapshot)
            self.assertFalse(snapshot.get("decision"))
            self.assertEqual(snapshot["result"]["structured"]["findings"][0]["id"], "F1")
            self.assertFalse((coordinator.packets_root / "forged.json").exists())
        finally:
            coordinator.close()

    def test_workflow_completion_required_but_saved_identity_not_required(self):
        for mode, expected in (("workflow", "reported"), ("incomplete_workflow", "blocked")):
            with self.subTest(mode=mode):
                _, run, result = self.run_review(mode, mode)
                self.assertEqual(json.loads((run / "receipt.json").read_text())["status"], expected)
                self.assertEqual(result["workflow_tool_use_count"], 1)
                self.assertEqual(result["workflow_completion_observed"], expected == "reported")

    def test_missing_and_fifo_reports_block_without_hanging(self):
        for mode in ("missing_report", "fifo"):
            with self.subTest(mode=mode):
                _, run, result = self.run_review(mode, mode)
                self.assertEqual(json.loads((run / "receipt.json").read_text())["status"], "blocked")
                self.assertEqual(result["review_report"]["status"], "not_collected")

    def test_missing_hook_remains_failure_even_with_a_valid_report(self):
        _, run, result = self.run_review("missing_hook")
        self.assertEqual(json.loads((run / "receipt.json").read_text())["status"], "failed")
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
