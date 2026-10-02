import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
import named_workflow  # noqa: E402
import bridge

BRIDGE = SCRIPTS / "bridge.py"


class NamedWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.enterContext(mock.patch.dict(os.environ, {"CLAUDE_ORCHESTRATOR_CLI_ROOT": str(self.root / "isolated-cli-store")}))
        self.repo = self.root / "repo"; self.repo.mkdir()
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        subprocess.run(["git", "-C", str(self.repo), "config", "user.email", "test@example.invalid"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "config", "user.name", "Test"], check=True)
        (self.repo / "base.txt").write_text("base\n")
        subprocess.run(["git", "-C", str(self.repo), "add", "base.txt"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-qm", "base"], check=True)
        self.config = self.root / "claude-config"; self.config.mkdir()
        self.previous_config = os.environ.get("CLAUDE_CONFIG_DIR")
        os.environ["CLAUDE_CONFIG_DIR"] = str(self.config)
        self.workflow = self.write_workflow(self.repo / ".claude" / "workflows" / "workflow_review.js")

    def tearDown(self):
        if self.previous_config is None:
            os.environ.pop("CLAUDE_CONFIG_DIR", None)
        else:
            os.environ["CLAUDE_CONFIG_DIR"] = self.previous_config
        self.tmp.cleanup()

    def write_workflow(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("export const meta = {\n  name: 'workflow_review',\n  description: 'Review safely',\n}\nreturn []\n")
        return path

    def binding(self):
        return {"name": "workflow_review", "path": str(self.workflow.resolve()),
                "sha256": hashlib.sha256(self.workflow.read_bytes()).hexdigest()}

    def test_inventory_uses_project_script_and_hash(self):
        found = named_workflow.inventory(self.repo)
        self.assertEqual(len(found), 1)
        self.assertEqual({key: found[0][key] for key in ("name", "path", "sha256")}, self.binding())
        self.assertEqual(named_workflow.validate(self.binding(), self.repo), self.binding())

    def test_inventory_rejects_duplicate_project_name_and_script_symlink(self):
        parent = self.repo / "parent"; child = parent / "child"; child.mkdir(parents=True)
        subprocess.run(["git", "init", "-q", str(parent)], check=True)
        subprocess.run(["git", "-C", str(parent), "config", "user.email", "test@example.invalid"], check=True)
        subprocess.run(["git", "-C", str(parent), "config", "user.name", "Test"], check=True)
        (parent / "base").write_text("base")
        subprocess.run(["git", "-C", str(parent), "add", "base"], check=True)
        subprocess.run(["git", "-C", str(parent), "commit", "-qm", "base"], check=True)
        self.write_workflow(parent / ".claude" / "workflows" / "one.js")
        self.write_workflow(child / ".claude" / "workflows" / "two.js")
        with self.assertRaisesRegex(named_workflow.NamedWorkflowError, "ambiguous"):
            named_workflow.inventory(child)
        (child / ".claude" / "workflows" / "two.js").unlink()
        (child / ".claude" / "workflows" / "two.js").symlink_to(self.workflow)
        with self.assertRaisesRegex(named_workflow.NamedWorkflowError, "symbolic"):
            named_workflow.inventory(child)

    def test_inventory_reads_metadata_without_ever_executing_the_script(self):
        marker = self.root / "script-executed"
        script = self.repo / ".claude" / "workflows" / "side_effect.js"
        script.write_text("export const meta = { name: 'side_effect', description: 'writes when run' }\n"
                          f"require('fs').writeFileSync({json.dumps(str(marker))}, 'ran')\n"
                          "export default async function run() { return [] }\n")
        found = {entry["name"] for entry in named_workflow.inventory(self.repo)}
        self.assertEqual(found, {"workflow_review", "side_effect"})
        self.assertFalse(marker.exists())


class MetaLiteralTests(unittest.TestCase):
    def test_initializer_end_rejects_expression_continuations_without_parsing_body(self):
        literal = "export const meta = {name: 'literal'}"
        for tail in (" && call()", "\n|| call()", "(call())", ".other", "\n[0]", "\n`tagged`", "\ninstanceof Other"):
            with self.subTest(tail=tail), self.assertRaises(named_workflow.NamedWorkflowError):
                named_workflow.parse_meta(literal + tail)
        for tail in ("", "; call()", "\ncall()", " /* comment\n */ const x = 1", "\n++counter"):
            with self.subTest(tail=tail):
                self.assertEqual(named_workflow.parse_meta(literal + tail)["name"], "literal")
        with self.assertRaises(named_workflow.NamedWorkflowError):
            named_workflow.parse_meta(literal + " " * named_workflow.META_LIMIT + "&& call()")

    def identity(self, source):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "script.js"
            path.write_text(source, encoding="utf-8")
            return named_workflow._script_identity(path)

    def test_single_and_multiline_literals_with_comments_and_quoted_braces(self):
        self.assertEqual(self.identity("export const meta = { name: 'one-line', description: 'x' }; export default 1")[0],
                         "one-line")
        source = """// leading comment
/* block comment with name: 'block-decoy' */
export const meta = {
  // name: 'commented-decoy',
  description: "uses { braces }, 'quotes', // and /* markers */",
  "name": 'real_name', /* trailing */
  phases: [{ title: 'a}b', agents: 2, weights: [1.5, -2, 3e2] }],
  enabled: true, extra: null, long: `template without substitution`,
  escaped: 'it\\'s \\u0041 \\x42',
};
const other = { name: 'body_decoy' }
"""
        name, digest = self.identity(source)
        self.assertEqual(name, "real_name")
        self.assertEqual(digest, hashlib.sha256(source.encode()).hexdigest())
        self.assertEqual(named_workflow.parse_meta(source)["escaped"], "it's A B")

    def test_decoys_ambiguity_and_executable_metadata_are_rejected(self):
        rejected = {
            "nested_decoy": "export const meta = { description: 'x', options: { name: 'nested' } }",
            "body_decoy": "export const meta = { description: 'x' }\nconst decoy = { name: 'body' }",
            "duplicate": "export const meta = { name: 'first', name: 'second' }",
            "duplicate_quoted": "export const meta = { name: 'first', 'na\\u006de': 'second' }",
            "computed": "export const meta = { ['name']: 'computed' }",
            "spread": "export const meta = { ...base, name: 'spread' }",
            "shorthand": "const name = 'x'\nexport const meta = { name }",
            "shorthand_first": "export const meta = { name }",
            "call": "export const meta = { name: makeName() }",
            "reference": "export const meta = { name: other }",
            "template_substitution": "export const meta = { name: `x${suffix}` }",
            "method": "export const meta = { name: 'm', run() { return 1 } }",
            "not_first": "const x = 1\nexport const meta = { name: 'late' }",
            "unsafe_name": "export const meta = { name: '../escape' }",
            "empty_name": "export const meta = { name: '' }",
            "numeric_name": "export const meta = { name: 5 }",
            "unterminated": "export const meta = { name: 'open', description: 'never closed'",
            "hex_number": "export const meta = { name: 'hex', count: 0x10 }",
        }
        for label, source in rejected.items():
            with self.subTest(case=label), self.assertRaises(named_workflow.NamedWorkflowError):
                self.identity(source)


class BridgeWorkflowTests(NamedWorkflowTests):
    def setUp(self):
        super().setUp()
        self.requirement = self.root / "requirements.md"; self.requirement.write_text("review only\n")
        self.fake = self.root / "fake_claude.py"
        self.fake.write_text("""#!/usr/bin/env python3
import json, os, subprocess, sys, time
sys.stdout.reconfigure(line_buffering=True)
args=sys.argv[1:]
if args == ['--version']: print('2.1.276 (Claude Code)'); raise SystemExit(0)
if args == ['--help']:
 print('-p --model --effort --output-format --verbose --json-schema --session-id --resume --permission-mode --tools --allowedTools --disallowedTools --settings --strict-mcp-config --mcp-config --disable-slash-commands --no-session-persistence'); raise SystemExit(0)
if args == ['auth','status','--json']: print(json.dumps({'loggedIn':True})); raise SystemExit(0)
sys.stdin.buffer.read()
def option(name): return args[args.index(name)+1] if name in args else None
session=option('--session-id')
if os.environ.get('FAKE_ENV_OUT'):
 open(os.environ['FAKE_ENV_OUT'],'w').write(json.dumps({'ceiling':os.environ.get('CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS'),'tmpdir':os.environ.get('CLAUDE_CODE_TMPDIR')}))
def result(status='completed', summary='done', **extra):
 return json.dumps({'type':'result','subtype':'success','session_id':session,**extra,'structured_output':{'status':status,'summary':summary,'evidence':[],'checks':[],'unresolved':[]}})
REPORT=os.environ.get('FAKE_REPORT') or ('Full workflow report\\n' + 'finding: src/module.py:42 verified\\n' * 1500 + 'END')
def notice(write=True, text=None, with_file=True):
 # Mirrors CLI 2.1.287: the task output is a public JSON envelope under CLAUDE_CODE_TMPDIR.
 folder=os.path.join(os.environ['CLAUDE_CODE_TMPDIR'], 'claude-%d' % os.getuid(), '-encoded-repo', session, 'tasks')
 os.makedirs(folder, exist_ok=True)
 path=os.path.join(folder, 'task-1.output')
 if write:
  open(path,'w').write(json.dumps({'summary':'workflow done','agentCount':1,'logs':[],'result':REPORT if text is None else text,'workflowProgress':[],'totalTokens':5,'totalToolCalls':2}))
 event={'type':'system','subtype':'task_notification','session_id':session,'tool_use_id':'toolu-workflow','task_id':'task-1','status':'completed'}
 if with_file: event['output_file']=path
 return json.dumps(event)
legacy=json.dumps({'type':'user','session_id':session,'message':{'content':'<task-notification>\\n<tool-use-id>toolu-workflow</tool-use-id>\\n<status>completed</status>\\n</task-notification>'}})
started=json.dumps({'type':'system','subtype':'task_started','session_id':session,'tool_use_id':'toolu-workflow','task_id':'task-1','task_type':'local_workflow'})
print(json.dumps({'type':'system','subtype':'init','session_id':session,'model':'test-model'}))
mode=os.environ.get('FAKE_WORKFLOW')
if mode:
 settings=json.load(open(option('--settings')))
 hook=settings['hooks']['PreToolUse'][0]['hooks'][0]['command']
 guarded=subprocess.run(['/bin/sh','-c',hook],input=json.dumps({'hook_event_name':'PreToolUse','tool_name':'Workflow','tool_input':{'name':'workflow_review'},'tool_use_id':'toolu-workflow'}),text=True,capture_output=True)
 if guarded.returncode != 0 or guarded.stdout.strip(): raise SystemExit(23)
 print(json.dumps({'type':'assistant','session_id':session,'message':{'content':[{'type':'tool_use','id':'toolu-workflow','name':'Workflow','input':{'name':'workflow_review'}}]}}))
 print(json.dumps({'type':'user','session_id':session,'message':{'content':[{'type':'tool_result','tool_use_id':'toolu-workflow','is_error':False,'content':'Workflow launched'}]}}))
 print(started)
 if mode == 'child_denial':
  # Like the real CLI, the child's call and its denial never reach the parent stream.
  child=subprocess.run(['/bin/sh','-c',hook],input=json.dumps({'hook_event_name':'PreToolUse','tool_name':'Glob','tool_input':{'path':'/outside-review-scope','pattern':'*'},'tool_use_id':'toolu-child-denied'}),text=True,capture_output=True)
  if json.loads(child.stdout or '{}').get('hookSpecificOutput',{}).get('permissionDecision') != 'deny': raise SystemExit(24)
 if mode == 'premature':
  print(result()); print(notice()); raise SystemExit(0)
 if mode == 'interim_then_final':
  print(result('blocked', 'workflow still running')); print(notice()); print(result()); raise SystemExit(0)
 if mode in ('hang', 'cancel'):
  if mode == 'cancel':
   open(os.path.join(os.path.dirname(option('--settings')), 'cancel.json'), 'w').write('{}')
  time.sleep(30)
 if mode == 'cancel_after_completion':
  print(notice()); open(os.path.join(os.path.dirname(option('--settings')), 'cancel.json'), 'w').write('{}'); time.sleep(30)
 if mode == 'legacy': print(legacy)
 elif mode == 'missing_output': print(notice(write=False))
 elif mode == 'blank_output': print(notice(text='   '))
 elif mode == 'no_output_file': print(notice(with_file=False))
 else: print(notice())
 if mode == 'child_result_after':
  print(result()); print(result(summary='child overwrote parent', parent_tool_use_id='toolu-child')); raise SystemExit(0)
print(result())
""")
        self.fake.chmod(0o755)
        self.previous_bin = os.environ.get("CLAUDE_BIN")
        os.environ["CLAUDE_BIN"] = str(self.fake)

    def tearDown(self):
        if self.previous_bin is None:
            os.environ.pop("CLAUDE_BIN", None)
        else:
            os.environ["CLAUDE_BIN"] = self.previous_bin
        os.environ.pop("FAKE_WORKFLOW", None)
        super().tearDown()

    def packet(self):
        return {"task_id":"workflow-1", "revision":1, "role":"workflow_review", "cwd":str(self.repo),
                "objective":"run the saved review", "requirement_sources":[str(self.requirement)], "constraints":["read only"],
                "acceptance":["structured"], "owned_files":[], "protected_files":["protected.txt"],
                "model":"test-model", "effort":"low", "workflow":self.binding()}

    def invoke(self, packet, name, extra=()):
        packet_path = self.root / f"{name}.json"; packet_path.write_text(json.dumps(packet))
        run = self.root / name
        got = subprocess.run([sys.executable, str(BRIDGE), "run", "--packet", str(packet_path), "--run-dir", str(run),
                              "--timeout", "3", *extra], text=True, capture_output=True)
        return got, run

    def test_bound_workflow_enables_only_exact_permission_and_records_tool_result(self):
        os.environ["FAKE_WORKFLOW"] = "success"
        got, run = self.invoke(self.packet(), "workflow-success")
        self.assertEqual(got.returncode, 0, got.stderr)
        command = json.loads((run / "command.json").read_text())["argv"]
        self.assertEqual(command[command.index("--tools") + 1], "Read,Glob,Grep,Workflow")
        self.assertEqual(command[command.index("--allowedTools") + 1], "Read,Glob,Grep,Workflow(workflow_review)")
        self.assertNotIn("Workflow", command[command.index("--disallowedTools") + 1])
        self.assertNotIn("--disable-slash-commands", command)
        self.assertEqual(json.loads((run / "settings.json").read_text())["hooks"]["PreToolUse"][0]["matcher"], "*")
        result = json.loads((run / "result.json").read_text())
        self.assertTrue(result["workflow_tool_use_observed"])
        self.assertTrue(result["workflow_tool_result_success"])
        self.assertTrue(result["workflow_completion_observed"])
        self.assertEqual(result["permission_denials"], [])
        self.assertIsNone(result["hook_denial_error"])
        self.assertEqual(json.loads((run / "receipt.json").read_text())["status"], "reported")
        delivery = result["workflow_delivery"]
        self.assertEqual((delivery["status"], delivery["reason_codes"]), ("delivered", []))
        invocation = delivery["invocations"][0]
        self.assertEqual((invocation["tool_use_id"], invocation["task_id"]), ("toolu-workflow", "task-1"))
        self.assertEqual(invocation["acknowledgement"]["state"], "succeeded")
        self.assertEqual(invocation["terminal"]["source"], "system_task_notification")
        report = (run / invocation["collection"]["report"]["path"]).read_text(encoding="utf-8")
        self.assertTrue(report.startswith("Full workflow report") and report.endswith("END"))
        self.assertGreater(len(report), len(result["structured"]["summary"]) * 100, "the full report is not the parent summary")
        self.assertEqual(invocation["collection"]["binding"]["workflow"], {key: self.binding()[key] for key in ("name", "path", "sha256")})
        self.assertEqual(delivery["temp_root"]["cleanup"], "removed")
        self.assertFalse(Path(delivery["temp_root"]["path"]).exists())

    def test_child_hook_denial_absent_from_parent_stream_fails_the_run(self):
        os.environ["FAKE_WORKFLOW"] = "child_denial"
        got, run = self.invoke(self.packet(), "workflow-child-denial")
        self.assertEqual(got.returncode, 1, got.stderr)
        result = json.loads((run / "result.json").read_text())
        # Everything the parent stream shows is valid; only the hook log has the denial.
        self.assertTrue(result["workflow_final_after_completion"])
        self.assertEqual(result["hook_guard_coverage"]["status"], "complete")
        self.assertEqual(result["provider_subtype"], "success")
        denials = [item for item in result["permission_denials"] if item.get("source") == "bridge_pretooluse_hook"]
        self.assertEqual(len(denials), 1)
        self.assertEqual(denials[0]["tool_use_id"], "toolu-child-denied")
        self.assertEqual(denials[0]["tool_name"], "Glob")
        self.assertIn("outside cwd", denials[0]["reason"])
        self.assertFalse(denials[0]["in_provider_stream"])
        self.assertFalse(denials[0]["in_provider_denials"])
        self.assertIn("denied 1 tool use", result["hook_denial_error"])
        self.assertEqual(result["structured"]["summary"], "done")
        self.assertEqual(result["report_evidence"]["state"], "structured")
        self.assertFalse(result["report_evidence"]["accepted"])
        self.assertEqual(result["report_evidence"]["run_status"], "failed")
        self.assertEqual(json.loads((run / "receipt.json").read_text())["status"], "failed")
        self.assertIn("tool_use_id=toolu-child-denied", got.stdout)

    def test_early_structured_result_followed_only_by_completion_is_not_reported(self):
        os.environ["FAKE_WORKFLOW"] = "premature"
        got, run = self.invoke(self.packet(), "workflow-premature")
        self.assertEqual(got.returncode, 0, got.stderr)
        result = json.loads((run / "result.json").read_text())
        self.assertTrue(result["workflow_tool_result_success"])
        self.assertTrue(result["workflow_completion_observed"])
        self.assertFalse(result["workflow_final_after_completion"])
        self.assertIn("preceded the Workflow completion", result["workflow_error"])
        self.assertEqual(json.loads((run / "receipt.json").read_text())["status"], "blocked")

    def test_interim_result_then_completion_then_final_parent_result_is_reported(self):
        os.environ["FAKE_WORKFLOW"] = "interim_then_final"
        got, run = self.invoke(self.packet(), "workflow-interim")
        self.assertEqual(got.returncode, 0, got.stderr)
        result = json.loads((run / "result.json").read_text())
        self.assertTrue(result["workflow_final_after_completion"])
        self.assertEqual(result["workflow_interim_result_count"], 1)
        self.assertEqual(result["structured"]["summary"], "done")
        self.assertIsNone(result["workflow_error"])
        self.assertEqual(json.loads((run / "receipt.json").read_text())["status"], "reported")

    def test_background_wait_ceiling_is_task_scoped_and_bounded_by_the_run_timeout(self):
        observed = self.root / "child-env.json"
        with mock.patch.dict(os.environ, {"FAKE_WORKFLOW": "success", "FAKE_ENV_OUT": str(observed),
                                          bridge.PRINT_BG_WAIT_CEILING: "0"}):
            got, run = self.invoke(self.packet(), "workflow-ceiling")
            self.assertEqual(got.returncode, 0, got.stderr)
            child_env = json.loads(observed.read_text())
            self.assertEqual(child_env["ceiling"], "3000")
            self.assertEqual(json.loads((run / "result.json").read_text())["workflow_background_wait_ceiling_ms"], 3000)
            self.assertEqual(os.environ[bridge.PRINT_BG_WAIT_CEILING], "0")
            self.assertEqual(child_env["tmpdir"], json.loads((run / "workflow-temp-root.json").read_text())["path"])
            self.assertFalse(Path(child_env["tmpdir"]).exists(), "the run-owned provider root is removed after capture")

            os.environ.pop("FAKE_WORKFLOW")
            review = {**self.packet(), "task_id": "plain-review", "role": "review"}
            review.pop("workflow")
            got, run = self.invoke(review, "plain-review-ceiling")
            self.assertEqual(got.returncode, 0, got.stderr)
            child_env = json.loads(observed.read_text())
            self.assertEqual(child_env["ceiling"], "0", "only workflow_review is changed")
            self.assertEqual(child_env["tmpdir"], os.environ.get("CLAUDE_CODE_TMPDIR"))
            result = json.loads((run / "result.json").read_text())
            self.assertIsNone(result["workflow_delivery"], "ordinary roles never acquire a Workflow artifact requirement")
            self.assertFalse((run / "workflow-temp-root.json").exists())
        self.assertEqual(bridge.print_background_wait_ceiling_ms(3600), 3_600_000)
        self.assertEqual(bridge.print_background_wait_ceiling_ms(0), 1000)

    def test_pending_workflow_still_obeys_the_outer_deadline_and_cancellation(self):
        for mode, expected in (("hang", "timeout"), ("cancel", "cancelled")):
            with self.subTest(mode=mode), mock.patch.dict(os.environ, {"FAKE_WORKFLOW": mode}):
                got, run = self.invoke(self.packet(), f"workflow-{mode}")
                self.assertEqual(json.loads((run / "receipt.json").read_text())["status"], expected, got.stderr)
                group = json.loads((run / "child.json").read_text())["process_group"]
                with self.assertRaises(ProcessLookupError):
                    os.killpg(group, 0)

    def test_claim_without_workflow_tool_is_blocked_not_reported(self):
        got, run = self.invoke(self.packet(), "workflow-missing")
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertEqual(json.loads((run / "receipt.json").read_text())["status"], "blocked")
        result = json.loads((run / "result.json").read_text())
        self.assertFalse(result["workflow_tool_use_observed"])
        self.assertFalse(result["workflow_tool_result_success"])
        self.assertFalse(result["workflow_completion_observed"])
        self.assertIn("not observed", result["workflow_error"])

    def test_hook_rejects_other_workflow_shapes_and_resume(self):
        packet = self.packet(); packet_path = self.root / "hook-packet.json"; packet_path.write_text(json.dumps(packet))
        exact = subprocess.run([sys.executable, str(BRIDGE), "hook", "--packet", str(packet_path), "--cwd", str(self.repo)],
                               input=json.dumps({"tool_name":"Workflow", "tool_input":{"name":"workflow_review"}, "tool_use_id":"workflow-exact"}), text=True, capture_output=True)
        self.assertEqual(exact.stdout, "")
        for input_ in ({"name":"other"}, {"script":"export const meta = {}"}, {"name":"workflow_review", "args": []}):
            denied = subprocess.run([sys.executable, str(BRIDGE), "hook", "--packet", str(packet_path), "--cwd", str(self.repo)],
                                    input=json.dumps({"tool_name":"Workflow", "tool_input":input_, "tool_use_id":"workflow-denied"}), text=True, capture_output=True)
            self.assertEqual(json.loads(denied.stdout)["hookSpecificOutput"]["permissionDecision"], "deny")
        got, _ = self.invoke(packet, "workflow-resume", ("--resume-from", str(self.root / "missing")))
        self.assertEqual(got.returncode, 2)
        self.assertIn("does not support resume", got.stderr)

    def test_bound_args_must_match_exactly(self):
        packet = self.packet(); packet["workflow"]["args"] = {"paths": ["base.txt"], "strict": True}
        packet_path = self.root / "args-packet.json"; packet_path.write_text(json.dumps(packet))
        exact = subprocess.run([sys.executable, str(BRIDGE), "hook", "--packet", str(packet_path), "--cwd", str(self.repo)],
                               input=json.dumps({"tool_name":"Workflow", "tool_input":{"name":"workflow_review", "args":{"strict":True, "paths":["base.txt"]}}, "tool_use_id":"workflow-args-exact"}), text=True, capture_output=True)
        self.assertEqual(exact.stdout, "")
        altered = subprocess.run([sys.executable, str(BRIDGE), "hook", "--packet", str(packet_path), "--cwd", str(self.repo)],
                                 input=json.dumps({"tool_name":"Workflow", "tool_input":{"name":"workflow_review", "args":{"paths":["other.txt"], "strict":True}}, "tool_use_id":"workflow-args-altered"}), text=True, capture_output=True)
        self.assertEqual(json.loads(altered.stdout)["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_output_only_tool_is_allowed_but_other_tools_remain_denied(self):
        packet_path = self.root / "output-packet.json"; packet_path.write_text(json.dumps(self.packet()))
        def hook(name, input_):
            return subprocess.run([sys.executable, str(BRIDGE), "hook", "--packet", str(packet_path), "--cwd", str(self.repo)],
                                  input=json.dumps({"tool_name":name, "tool_input":input_, "tool_use_id":"output-" + name}), text=True, capture_output=True)
        value = {"status":"completed", "summary":"output", "evidence":[], "checks":[], "unresolved":[]}
        self.assertEqual(hook("StructuredOutput", value).stdout, "")
        # A workflow child can declare a schema unrelated to the parent's final
        # report.  The bridge validates the parent's terminal result separately.
        self.assertEqual(hook("StructuredOutput", {"status":"accepted", "child_evidence":{"count":2}}).stdout, "")
        for name, input_ in [("Bash", {"command":"pwd"}), ("Write", {"file_path":"base.txt", "content":"bad"})]:
            self.assertEqual(json.loads(hook(name, input_).stdout)["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_short_report_and_missing_or_invalid_transport_are_distinguished(self):
        cases = {
            "short": ("reported", None, []),
            "missing_output": ("blocked", "workflow_evidence", ["output_missing", "workflow_report_not_collected"]),
            "blank_output": ("blocked", "workflow_evidence", ["output_result_blank", "workflow_report_not_collected"]),
            "no_output_file": ("blocked", "workflow_evidence", ["workflow_report_transport_missing"]),
            "legacy": ("blocked", "workflow_evidence", ["workflow_report_transport_missing"]),
        }
        for mode, (status, blocked_by, codes) in cases.items():
            env = {"FAKE_WORKFLOW": mode, "FAKE_REPORT": "No findings." if mode == "short" else ""}
            with self.subTest(mode=mode), mock.patch.dict(os.environ, env):
                got, run = self.invoke({**self.packet(), "task_id": "transport-" + mode}, f"workflow-{mode}")
                self.assertEqual(got.returncode, 0, got.stderr)
                receipt = json.loads((run / "receipt.json").read_text())
                result = json.loads((run / "result.json").read_text())
                self.assertEqual((receipt["status"], receipt["blocked_by"], result["blocked_by"]), (status, blocked_by, blocked_by))
                self.assertEqual(result["workflow_delivery"]["reason_codes"], codes)
                self.assertTrue(result["workflow_completion_observed"])
                self.assertEqual(result["structured"]["summary"], "done", "the parent's report stays as evidence")
                if status == "blocked":
                    self.assertIn("full report was not collected", result["workflow_error"])
                    self.assertEqual(result["workflow_error_codes"], codes)
                else:
                    collection = result["workflow_delivery"]["invocations"][0]["collection"]
                    self.assertEqual((run / collection["report"]["path"]).read_text(), "No findings.")

    def test_parent_report_stays_final_after_a_child_result(self):
        os.environ["FAKE_WORKFLOW"] = "child_result_after"
        got, run = self.invoke(self.packet(), "workflow-child-result")
        self.assertEqual(got.returncode, 0, got.stderr)
        result = json.loads((run / "result.json").read_text())
        self.assertEqual(result["structured"]["summary"], "done")
        self.assertIsNone(result["session_error"])
        self.assertEqual(result["result_selection"]["non_parent_result_events"], 1)
        self.assertEqual(json.loads((run / "receipt.json").read_text())["status"], "reported")

    def test_cancellation_after_completion_keeps_the_captured_artifact_as_evidence(self):
        os.environ["FAKE_WORKFLOW"] = "cancel_after_completion"
        got, run = self.invoke(self.packet(), "workflow-cancel-after")
        result = json.loads((run / "result.json").read_text())
        self.assertEqual(json.loads((run / "receipt.json").read_text())["status"], "cancelled", got.stderr)
        collection = result["workflow_delivery"]["invocations"][0]["collection"]
        self.assertEqual(collection["status"], "collected")
        self.assertTrue((run / collection["report"]["path"]).is_file())

    def hook_decision(self, packet_path, name, input_, tool_use_id="hook-check"):
        got = subprocess.run([sys.executable, str(BRIDGE), "hook", "--packet", str(packet_path), "--cwd", str(self.repo)],
                             input=json.dumps({"tool_name": name, "tool_input": input_, "tool_use_id": tool_use_id}),
                             text=True, capture_output=True)
        if not got.stdout.strip():
            return "allow", ""
        output = json.loads(got.stdout)["hookSpecificOutput"]
        return output["permissionDecision"], output["permissionDecisionReason"]

    def test_unrelated_inventory_change_never_flips_an_allowed_file_hook(self):
        packet_path = self.root / "frozen-packet.json"; packet_path.write_text(json.dumps(self.packet()))
        read = ("Read", {"file_path": "base.txt"})
        workflow = ("Workflow", {"name": "workflow_review"})
        self.assertEqual(self.hook_decision(packet_path, *read)[0], "allow")
        unrelated = self.config / "workflows" / "unrelated.js"
        unrelated.parent.mkdir(parents=True, exist_ok=True)
        unrelated.write_text("console.log('no metadata here')\n")
        self.assertEqual(self.hook_decision(packet_path, *read)[0], "allow", "an unrelated script must not deny a file hook")
        decision, reason = self.hook_decision(packet_path, *workflow)
        self.assertEqual(decision, "deny", "the exact Workflow invocation still re-runs effective resolution")
        self.assertIn("saved workflow identity check failed", reason)
        unrelated.unlink()
        self.assertEqual(self.hook_decision(packet_path, *workflow)[0], "allow")

        original = self.workflow.read_text()
        self.workflow.write_text(original + "// tampered\n")
        decision, reason = self.hook_decision(packet_path, *read)
        self.assertEqual(decision, "deny")
        self.assertIn("changed since dispatch", reason)
        self.workflow.write_text(original)
        copy = self.root / "copy.js"; copy.write_text(original)
        self.workflow.unlink(); self.workflow.symlink_to(copy)
        self.assertEqual(self.hook_decision(packet_path, *read)[0], "deny", "a symlinked replacement is a source path change")
        self.workflow.unlink(); self.workflow.write_text(original)
        self.assertEqual(self.hook_decision(packet_path, *read)[0], "allow")

    def test_newly_shadowing_project_script_is_denied_only_at_the_workflow_invocation(self):
        personal = self.config / "workflows" / "personal_review.js"
        personal.parent.mkdir(parents=True, exist_ok=True)
        personal.write_text("export const meta = { name: 'personal_review', description: 'personal' }\n")
        binding = {"name": "personal_review", "path": str(personal.resolve()),
                   "sha256": hashlib.sha256(personal.read_bytes()).hexdigest()}
        packet_path = self.root / "personal-packet.json"
        packet_path.write_text(json.dumps({**self.packet(), "workflow": binding}))
        workflow = ("Workflow", {"name": "personal_review"})
        self.assertEqual(self.hook_decision(packet_path, *workflow)[0], "allow")
        shadow = self.repo / ".claude" / "workflows" / "shadow.js"
        shadow.write_text("export const meta = { name: 'personal_review', description: 'project shadow' }\n")
        self.assertEqual(self.hook_decision(packet_path, *workflow)[0], "deny")
        self.assertEqual(self.hook_decision(packet_path, "Read", {"file_path": "base.txt"})[0], "allow")

    def test_scope_contract_names_exact_files_and_the_exact_workflow_input(self):
        external = self.root / "external-notes.md"; external.write_text("notes\n")
        packet = {**self.packet(), "requirement_sources": [str(self.requirement), str(external)]}
        packet["workflow"] = {**self.binding(), "args": {"paths": ["base.txt"], "strict": True}}
        validated = bridge.validate_packet(packet)
        scope = bridge.scope_contract(validated)
        self.assertEqual(scope["read"]["external_exact_files"], [str(self.requirement.resolve()), str(external.resolve())])
        self.assertNotIn(self.binding()["path"], scope["read"]["external_exact_files"], "the project script is inside cwd")
        self.assertEqual(scope["read"]["inside_cwd"]["root"], str(self.repo.resolve()))
        self.assertEqual(scope["workflow_invocation"]["input"],
                         {"name": "workflow_review", "args": {"paths": ["base.txt"], "strict": True}})
        self.assertEqual(scope["write"], {"mode": "none"})
        self.assertEqual(json.loads(bridge.prompt(validated))["scope"], scope)
        packet_path = self.root / "scope-packet.json"; packet_path.write_text(json.dumps(validated))
        self.assertEqual(self.hook_decision(packet_path, "Read", {"file_path": str(external.resolve())})[0], "allow")
        self.assertEqual(self.hook_decision(packet_path, "Grep", {"pattern": "notes", "path": str(external.resolve())})[0], "allow")
        decision, reason = self.hook_decision(packet_path, "Grep", {"pattern": "notes", "path": str(self.root.resolve())})
        self.assertEqual(decision, "deny", "the parent directory of an exact source is never granted")
        self.assertIn("outside cwd", reason)
        self.assertIn(str(external.resolve()), reason)
        self.assertIn("one exact file", reason)


class WorkflowStreamTests(unittest.TestCase):
    def parse(self, notification, *, actual_args=None, expected_args=None, second_launch=False):
        expected = {"name":"check"}
        actual = {"name":"check"}
        if expected_args is not None: expected["args"] = expected_args
        if actual_args is not None: actual["args"] = actual_args
        events = [
            {"type":"system", "subtype":"init", "session_id":"s", "model":"test"},
            {"type":"assistant", "message":{"content":[{"type":"tool_use", "id":"w1", "name":"Workflow", "input":actual}]}},
            {"type":"user", "message":{"content":[{"type":"tool_result", "tool_use_id":"w1", "is_error":False}]}},
            notification,
        ]
        if second_launch:
            events.append({"type":"assistant", "message":{"content":[{"type":"tool_use", "id":"w2", "name":"Workflow", "input":actual}]}})
        events.append({"type":"result", "subtype":"success", "session_id":"s"})
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/"stream.jsonl"; path.write_text("\n".join(json.dumps(e) for e in events))
            return bridge.parse_stream(path, "s", expected)[1]

    def test_current_cli_system_notification_requires_same_successful_call(self):
        event={"type":"system", "subtype":"task_notification", "session_id":"s", "tool_use_id":"w1", "status":"completed"}
        self.assertTrue(self.parse(event)["workflow_completion_observed"])
        for change in ({"tool_use_id":"other"}, {"status":"failed"}, {"session_id":"foreign"}, {"type":"assistant"}):
            self.assertFalse(self.parse({**event, **change})["workflow_completion_observed"])
        self.assertFalse(self.parse(event, second_launch=True)["workflow_completion_observed"])
        self.assertTrue(self.parse(event, actual_args={"a":1}, expected_args={"a":1})["workflow_completion_observed"])
        self.assertFalse(self.parse(event, actual_args={"a":2}, expected_args={"a":1})["workflow_completion_observed"])

    def test_assistant_claim_cannot_supply_completion(self):
        event={"type":"assistant", "message":{"content":"<task-notification><tool-use-id>w1</tool-use-id><status>completed</status></task-notification>"}}
        self.assertFalse(self.parse(event)["workflow_completion_observed"])


class WorkflowResultOrderTests(unittest.TestCase):
    LAUNCH = [
        {"type":"system", "subtype":"init", "session_id":"s", "model":"test"},
        {"type":"assistant", "session_id":"s", "message":{"content":[{"type":"tool_use", "id":"w1", "name":"Workflow", "input":{"name":"check"}}]}},
        {"type":"user", "session_id":"s", "message":{"content":[{"type":"tool_result", "tool_use_id":"w1", "is_error":False}]}},
    ]
    DONE = {"type":"system", "subtype":"task_notification", "session_id":"s", "tool_use_id":"w1", "task_id":"t1", "status":"completed"}

    @staticmethod
    def result(summary):
        return {"type":"result", "subtype":"success", "session_id":"s",
                "structured_output":{"status":"completed", "summary":summary, "evidence":[], "checks":[], "unresolved":[]}}

    def parse(self, *events):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "stream.jsonl"
            path.write_text("\n".join(json.dumps(e) for e in [*self.LAUNCH, *events]))
            return bridge.parse_stream(path, "s", {"name":"check"})

    def test_premature_result_followed_merely_by_completion_is_not_final(self):
        final, meta = self.parse(self.result("interim"), self.DONE)
        self.assertEqual(final["structured_output"]["summary"], "interim")
        self.assertTrue(meta["workflow_completion_observed"])
        self.assertFalse(meta["workflow_final_after_completion"])
        self.assertEqual(meta["workflow_interim_result_count"], 1)

    def test_parent_result_after_the_matching_completion_is_final(self):
        started = {"type":"system", "subtype":"task_started", "session_id":"s", "tool_use_id":"w1", "task_id":"t1"}
        final, meta = self.parse(started, self.result("interim"), self.DONE, self.result("report"))
        self.assertEqual(final["structured_output"]["summary"], "report")
        self.assertTrue(meta["workflow_final_after_completion"])
        self.assertEqual(meta["workflow_interim_result_count"], 1)

    def test_wrong_task_tool_or_session_completion_never_precedes_a_final_result(self):
        started = {"type":"system", "subtype":"task_started", "session_id":"s", "tool_use_id":"w1", "task_id":"t1"}
        text = "<task-notification>\n<task-id>{task}</task-id>\n<tool-use-id>{tool}</tool-use-id>\n<status>completed</status>\n</task-notification>"
        def notice(task="t1", tool="w1", **fields):
            return {"type":"user", "session_id":"s", "message":{"content":text.format(task=task, tool=tool)}, **fields}
        cases = {
            "wrong_task_system": (started, {**self.DONE, "task_id":"t2"}),
            "wrong_task_text": (started, notice(task="t2")),
            "wrong_tool_system": ({**self.DONE, "tool_use_id":"read-1"},),
            "wrong_tool_text": (notice(tool="read-1"),),
            "wrong_session_system": ({**self.DONE, "session_id":"foreign"},),
            "wrong_session_text": (notice(session_id="foreign"),),
            "workflow_agent_text": (notice(parent_tool_use_id="w1"),),
            "failed": ({**self.DONE, "status":"failed"},),
        }
        for name, events in cases.items():
            with self.subTest(case=name):
                _, meta = self.parse(*events, self.result("report"))
                self.assertFalse(meta["workflow_completion_observed"])
                self.assertFalse(meta["workflow_final_after_completion"])
        _, meta = self.parse(started, notice(), self.result("report"))
        self.assertTrue(meta["workflow_final_after_completion"], "the matching parent text notification still counts")

    def test_later_task_started_under_the_same_call_does_not_rebind_the_workflow_task(self):
        first = {"type":"system", "subtype":"task_started", "session_id":"s", "tool_use_id":"w1", "task_id":"t1"}
        nested = {**first, "task_id":"agent-7"}
        _, meta = self.parse(first, nested, {**self.DONE, "task_id":"agent-7"}, self.result("report"))
        self.assertFalse(meta["workflow_completion_observed"])
        _, meta = self.parse(first, nested, self.DONE, self.result("report"))
        self.assertTrue(meta["workflow_final_after_completion"])

    def test_completion_before_the_launch_acknowledgement_does_not_count(self):
        events = [self.LAUNCH[0], self.LAUNCH[1],
                  {"type":"user", "session_id":"s", "message":{"content":"<task-notification><tool-use-id>w1</tool-use-id><status>completed</status></task-notification>"}},
                  self.LAUNCH[2], self.result("report")]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "stream.jsonl"
            path.write_text("\n".join(json.dumps(e) for e in events))
            _, meta = bridge.parse_stream(path, "s", {"name":"check"})
        self.assertFalse(meta["workflow_completion_observed"])
        self.assertFalse(meta["workflow_final_after_completion"])
        invocation = meta["workflow_invocations"][0]
        self.assertEqual((invocation["notifications_before_acknowledgement"], invocation["terminal"]), (1, None))

    @staticmethod
    def child_result(summary="child overwrote parent", **fields):
        return {**WorkflowResultOrderTests.result(summary), "parent_tool_use_id": "toolu-child", **fields}

    def parse_plain(self, *events):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "stream.jsonl"
            path.write_text("\n".join(json.dumps(e) for e in [self.LAUNCH[0], *events]))
            return bridge.parse_stream(path, "s")

    def test_only_a_parent_result_in_the_expected_session_is_final(self):
        final, meta = self.parse_plain(self.result("parent report"), self.child_result())
        self.assertEqual(final["structured_output"]["summary"], "parent report")
        self.assertIsNone(meta.get("session_error"))
        self.assertEqual((meta["result_selection"]["parent_result_events"],
                          meta["result_selection"]["non_parent_result_events"]), (1, 1))
        final, meta = self.parse_plain(self.result("parent report"), {**self.result("foreign"), "session_id": "foreign"})
        self.assertEqual(final["structured_output"]["summary"], "parent report")
        self.assertIn("must both equal", meta["session_error"], "a wrong-session event is still audited")
        self.assertEqual(meta["result_selection"]["unexpected_session_result_events"], 1)
        final, meta = self.parse_plain(self.child_result())
        self.assertIsNone(final, "a child result alone is never the parent's report")
        self.assertIn("must both equal", meta["session_error"])
        final, meta = self.parse_plain({"type":"system", "subtype":"error", "session_id":"s", "is_error":True,
                                        "structured_output":{"summary":"not a result event"}})
        self.assertIsNone(final)

    def test_workflow_report_stays_final_when_child_events_follow(self):
        final, meta = self.parse(self.DONE, self.result("report"), self.child_result(),
                                 {"type":"assistant", "session_id":"s", "parent_tool_use_id":"w1", "message":{"content":[]}})
        self.assertEqual(final["structured_output"]["summary"], "report")
        self.assertTrue(meta["workflow_final_after_completion"])
        self.assertEqual(meta["workflow_interim_result_count"], 0)

    def text_notice(self, text):
        return {"type":"user", "session_id":"s", "message":{"content":[{"type":"text", "text":text}]}}

    def test_legacy_headers_are_read_only_from_the_leading_block_before_any_body(self):
        rejected = {
            "status_only_in_body": "<task-notification>\n<tool-use-id>w1</tool-use-id>\n<summary>done</summary>\n"
                                   "<result>quoted <status>completed</status></result>\n</task-notification>",
            "fields_split_across_blocks": "<task-notification><tool-use-id>w1</tool-use-id></task-notification>"
                                          "<task-notification><status>completed</status></task-notification>",
            "embedded_in_report_body": "Report text first <task-notification><tool-use-id>w1</tool-use-id>"
                                       "<status>completed</status></task-notification>",
            "repeated_header": "<task-notification><tool-use-id>w1</tool-use-id><status>failed</status>"
                               "<status>completed</status></task-notification>",
        }
        for label, text in rejected.items():
            with self.subTest(case=label):
                _, meta = self.parse(self.text_notice(text), self.result("report"))
                self.assertFalse(meta["workflow_completion_observed"])
        accepted = ("<task-notification>\n<task-id>t1</task-id>\n<tool-use-id>w1</tool-use-id>\n<status>completed</status>\n"
                    "<summary>Workflow finished</summary>\n<result><status>failed</status></result>\n</task-notification>")
        _, meta = self.parse(self.text_notice(accepted), self.result("report"))
        self.assertTrue(meta["workflow_final_after_completion"], "headers before the body still count")
        self.assertEqual(meta["workflow_invocations"][0]["terminal"]["source"], "legacy_text_notification")
        self.assertIsNone(meta["workflow_invocations"][0]["output_reference"], "legacy text never supplies a transport")

    def test_structured_events_outrank_legacy_text_and_conflicts_are_not_completion(self):
        legacy = self.text_notice("<task-notification><tool-use-id>w1</tool-use-id><status>completed</status></task-notification>")
        _, meta = self.parse({**self.DONE, "status":"failed"}, legacy, self.result("report"))
        self.assertFalse(meta["workflow_completion_observed"])
        self.assertEqual(meta["workflow_invocations"][0]["terminal"]["status"], "failed")
        done = {**self.DONE, "output_file":"/tmp/a.output"}
        _, meta = self.parse(done, done, self.result("report"))
        self.assertTrue(meta["workflow_final_after_completion"], "an identical duplicate completion is harmless")
        self.assertEqual(meta["workflow_invocations"][0]["terminal"]["count"], 2)
        _, meta = self.parse(done, {**done, "output_file":"/tmp/b.output"}, self.result("report"))
        self.assertFalse(meta["workflow_completion_observed"])
        self.assertTrue(meta["workflow_invocations"][0]["terminal"]["conflict"])
        _, meta = self.parse(self.result("report"), done)
        self.assertFalse(meta["workflow_final_after_completion"], "a late completion cannot precede the final result")

    def test_a_non_workflow_task_type_never_binds_the_call(self):
        agent = {"type":"system", "subtype":"task_started", "session_id":"s", "tool_use_id":"w1", "task_id":"agent-1",
                 "task_type":"local_agent"}
        workflow = {**agent, "task_id":"t1", "task_type":"local_workflow"}
        _, meta = self.parse(agent, workflow, self.DONE, self.result("report"))
        self.assertEqual(meta["workflow_invocations"][0]["task_id"], "t1")
        self.assertTrue(meta["workflow_final_after_completion"])


if __name__ == "__main__":
    unittest.main()
