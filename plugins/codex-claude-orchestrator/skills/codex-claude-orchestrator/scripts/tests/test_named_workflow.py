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
import json, os, subprocess, sys
args=sys.argv[1:]
if args == ['--version']: print('2.1.276 (Claude Code)'); raise SystemExit(0)
if args == ['--help']:
 print('-p --model --effort --output-format --json-schema --session-id --resume --permission-mode --tools --allowedTools --disallowedTools --settings --strict-mcp-config --mcp-config --disable-slash-commands'); raise SystemExit(0)
if args == ['auth','status','--json']: print(json.dumps({'loggedIn':True})); raise SystemExit(0)
def option(name): return args[args.index(name)+1] if name in args else None
session=option('--session-id')
print(json.dumps({'type':'system','subtype':'init','session_id':session,'model':'test-model'}))
if os.environ.get('FAKE_WORKFLOW') == 'success':
 settings=json.load(open(option('--settings')))
 hook=settings['hooks']['PreToolUse'][0]['hooks'][0]['command']
 guarded=subprocess.run(['/bin/sh','-c',hook],input=json.dumps({'hook_event_name':'PreToolUse','tool_name':'Workflow','tool_input':{'name':'workflow_review'},'tool_use_id':'toolu-workflow'}),text=True,capture_output=True)
 if guarded.returncode != 0 or guarded.stdout.strip(): raise SystemExit(23)
 print(json.dumps({'type':'assistant','session_id':session,'message':{'content':[{'type':'tool_use','id':'toolu-workflow','name':'Workflow','input':{'name':'workflow_review'}}]}}))
 print(json.dumps({'type':'user','session_id':session,'message':{'content':[{'type':'tool_result','tool_use_id':'toolu-workflow','is_error':False,'content':'Workflow launched'}]}}))
 print(json.dumps({'type':'user','session_id':session,'message':{'content':'<task-notification>\\n<tool-use-id>toolu-workflow</tool-use-id>\\n<status>completed</status>\\n</task-notification>'}}))
print(json.dumps({'type':'result','subtype':'success','session_id':session,'structured_output':{'status':'completed','summary':'done','evidence':[],'checks':[],'unresolved':[]}}))
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
        self.assertEqual(json.loads((run / "receipt.json").read_text())["status"], "reported")

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


if __name__ == "__main__":
    unittest.main()
