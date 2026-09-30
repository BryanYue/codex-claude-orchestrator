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
def option(name): return args[args.index(name)+1] if name in args else None
session=option('--session-id')
if os.environ.get('FAKE_ENV_OUT'):
 open(os.environ['FAKE_ENV_OUT'],'w').write(json.dumps({'ceiling':os.environ.get('CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS')}))
def result(status='completed', summary='done'):
 return json.dumps({'type':'result','subtype':'success','session_id':session,'structured_output':{'status':status,'summary':summary,'evidence':[],'checks':[],'unresolved':[]}})
notice=json.dumps({'type':'user','session_id':session,'message':{'content':'<task-notification>\\n<tool-use-id>toolu-workflow</tool-use-id>\\n<status>completed</status>\\n</task-notification>'}})
print(json.dumps({'type':'system','subtype':'init','session_id':session,'model':'test-model'}))
mode=os.environ.get('FAKE_WORKFLOW')
if mode:
 settings=json.load(open(option('--settings')))
 hook=settings['hooks']['PreToolUse'][0]['hooks'][0]['command']
 guarded=subprocess.run(['/bin/sh','-c',hook],input=json.dumps({'hook_event_name':'PreToolUse','tool_name':'Workflow','tool_input':{'name':'workflow_review'},'tool_use_id':'toolu-workflow'}),text=True,capture_output=True)
 if guarded.returncode != 0 or guarded.stdout.strip(): raise SystemExit(23)
 print(json.dumps({'type':'assistant','session_id':session,'message':{'content':[{'type':'tool_use','id':'toolu-workflow','name':'Workflow','input':{'name':'workflow_review'}}]}}))
 print(json.dumps({'type':'user','session_id':session,'message':{'content':[{'type':'tool_result','tool_use_id':'toolu-workflow','is_error':False,'content':'Workflow launched'}]}}))
 if mode == 'child_denial':
  # Like the real CLI, the child's call and its denial never reach the parent stream.
  child=subprocess.run(['/bin/sh','-c',hook],input=json.dumps({'hook_event_name':'PreToolUse','tool_name':'Glob','tool_input':{'path':'/outside-review-scope','pattern':'*'},'tool_use_id':'toolu-child-denied'}),text=True,capture_output=True)
  if json.loads(child.stdout or '{}').get('hookSpecificOutput',{}).get('permissionDecision') != 'deny': raise SystemExit(24)
 if mode == 'premature':
  print(result()); print(notice); raise SystemExit(0)
 if mode == 'interim_then_final':
  print(result('blocked', 'workflow still running')); print(notice); print(result()); raise SystemExit(0)
 if mode in ('hang', 'cancel'):
  if mode == 'cancel':
   open(os.path.join(os.path.dirname(option('--settings')), 'cancel.json'), 'w').write('{}')
  time.sleep(30)
 print(notice)
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
            self.assertEqual(json.loads(observed.read_text())["ceiling"], "3000")
            self.assertEqual(json.loads((run / "result.json").read_text())["workflow_background_wait_ceiling_ms"], 3000)
            self.assertEqual(os.environ[bridge.PRINT_BG_WAIT_CEILING], "0")

            os.environ.pop("FAKE_WORKFLOW")
            review = {**self.packet(), "task_id": "plain-review", "role": "review"}
            review.pop("workflow")
            got, run = self.invoke(review, "plain-review-ceiling")
            self.assertEqual(got.returncode, 0, got.stderr)
            self.assertEqual(json.loads(observed.read_text())["ceiling"], "0", "only workflow_review is changed")
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


if __name__ == "__main__":
    unittest.main()
