import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

BRIDGE = Path(__file__).resolve().parents[1] / "bridge.py"
PLUGIN_ROOT = BRIDGE.parents[3]

FAKE_CLAUDE = r"""#!/usr/bin/env python3
import json, os, subprocess, sys, time
mode = os.environ.get('FAKE_MODE', 'normal')
args = sys.argv[1:]
if args == ['--version']: print('2.1.276 (Claude Code)'); sys.exit(0)
if args == ['--help']:
    print('-p --model --effort --output-format --verbose --json-schema --session-id --resume --permission-mode --tools --allowedTools --disallowedTools --settings --strict-mcp-config --mcp-config --disable-slash-commands --no-session-persistence --max-turns --max-budget-usd')
    sys.exit(0)
if args == ['auth', 'status', '--json']:
    if mode == 'logged_out': print(json.dumps({'loggedIn': False})); sys.exit(1)
    print(json.dumps({'loggedIn': True, 'authMethod': 'claude.ai', 'apiProvider': 'firstParty'})); sys.exit(0)

sys.stdin.buffer.read()
def option(name): return args[args.index(name) + 1] if name in args else None
session = option('--resume') or option('--session-id')

def emit(value): print(json.dumps(value), flush=True)

def use(tool_id, tool, tool_input, hook_id=None, hook_tool=None, run_hook=True):
    emit({'type': 'assistant', 'session_id': session, 'message': {'model': 'test-model', 'content': [
        {'type': 'tool_use', 'id': tool_id, 'name': tool, 'input': tool_input}]}})
    if run_hook:
        settings = json.load(open(option('--settings')))
        command = settings['hooks']['PreToolUse'][0]['hooks'][0]['command']
        subprocess.run(command, shell=True, text=True, capture_output=True, input=json.dumps({
            'hook_event_name': 'PreToolUse', 'tool_name': hook_tool or tool, 'tool_input': tool_input,
            'tool_use_id': hook_id or tool_id}))

report = {'status': 'completed', 'summary': 'SENTINEL_REPORT', 'evidence': ['e'], 'checks': ['c'], 'unresolved': []}
emit({'type': 'system', 'subtype': 'init', 'session_id': session, 'model': 'test-model'})
success = {'type': 'result', 'subtype': 'success', 'session_id': session, 'usage': {'input_tokens': 3}, 'structured_output': report}
failure = {'type': 'result', 'subtype': 'error_during_execution', 'is_error': True, 'session_id': session}

if mode == 'allowed':
    use('r1', 'Read', {'file_path': 'base.txt'}); emit(success)
elif mode == 'denied':
    use('r1', 'Read', {'file_path': '/etc/hosts'})
    emit({**success, 'permission_denials': [{'tool_name': 'Read', 'tool_use_id': 'r1', 'tool_input': {'file_path': '/etc/hosts'}}]})
elif mode == 'denied_hook_only':
    use('r1', 'Read', {'file_path': '/etc/hosts'}); emit(success)
elif mode == 'other_id':
    use('r1', 'Read', {'file_path': 'base.txt'}, hook_id='some-other-id'); emit(success)
elif mode == 'other_tool':
    use('r1', 'Read', {'file_path': 'base.txt'}, hook_tool='Grep'); emit(success)
elif mode == 'no_hook':
    use('r1', 'Read', {'file_path': 'base.txt'}, run_hook=False); emit(success)
elif mode == 'valid_report_failed':
    emit({**failure, 'structured_output': report}); sys.exit(3)
elif mode == 'invalid_report_failed':
    emit({**failure, 'structured_output': {'status': 'completed', 'summary': 5}}); sys.exit(3)
elif mode == 'invalid_report_success':
    emit({**success, 'structured_output': {'status': 'completed', 'summary': 5}})
elif mode == 'missing_report_success':
    emit({key: value for key, value in success.items() if key != 'structured_output'})
elif mode == 'error_no_report':
    emit(failure); sys.exit(7)
elif mode == 'report_then_hang':
    emit(success); time.sleep(30)
elif mode == 'bulky_denial':
    emit({**success, 'permission_denials': [{'tool_name': 'Write', 'tool_use_id': 'w1',
          'tool_input': {'file_path': 'x.txt', 'content': 'A' * 5000}}]})
elif mode == 'string_denial':
    emit({**success, 'permission_denials': ['no']})
elif mode == 'blank_summary':
    emit({**success, 'structured_output': {**report, 'summary': ' \n\t '}})
elif mode == 'blank_item':
    emit({**success, 'structured_output': {**report, 'evidence': ['e', '   ']}})
elif mode == 'no_findings':
    emit({**success, 'structured_output': {'status': 'completed', 'summary': 'No findings.', 'evidence': [], 'checks': [], 'unresolved': []}})
elif mode in ('formatter_rejected', 'formatter_rejected_invalid_final'):
    raw = '{broken formatter input}'
    emit({'type': 'assistant', 'session_id': session, 'parent_tool_use_id': None,
          'message': {'role': 'assistant', 'content': [{'type': 'tool_use', 'id': 'invalid-formatter',
             'name': 'StructuredOutput', 'caller': {'type': 'direct'},
             'input': {'__unparsedToolInput': {'raw': raw, 'len': len(raw.encode('utf-8'))}}}]}})
    emit({'type': 'user', 'session_id': session, 'parent_tool_use_id': None,
          'message': {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': 'invalid-formatter',
             'is_error': True, 'content': 'Rejected malformed formatter input'}]}})
    emit({**success, 'structured_output': {**report, 'summary': 5}} if mode.endswith('invalid_final') else success)
elif mode == 'external_exact':
    use('r1', 'Read', {'file_path': os.environ['FAKE_EXTERNAL']})
    use('g1', 'Grep', {'pattern': 'evidence', 'path': os.environ['FAKE_EXTERNAL']}); emit(success)
elif mode == 'parent_search':
    use('g1', 'Grep', {'pattern': 'evidence', 'path': os.path.dirname(os.environ['FAKE_EXTERNAL'])}); emit(success)
else:
    emit(success)
"""


def load_bridge_module():
    scripts = str(BRIDGE.parent)
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    spec = importlib.util.spec_from_file_location("bridge_run_evidence_unit", BRIDGE)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class CoverageAndDenialUnitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.run = Path(self.tmp.name) / "run"
        self.run.mkdir()
        self.bridge = load_bridge_module()

    def tearDown(self):
        self.tmp.cleanup()

    def record(self, status, tool, tool_use_id):
        self.bridge.append_activity(self.run, "tool", "hook decision", tool=tool, status=status, tool_use_id=tool_use_id)

    def test_long_paths_keep_scope_rule_and_action_in_bounded_denial(self):
        parent = Path("/") / ("long" * 60)
        source = parent / ("file" * 60 + ".md")
        reason = self.bridge._outside_scope_message(parent, {source}, True)
        self.bridge.append_activity(self.run, "tool", "tool path denied", text=reason,
                                    tool="Grep", status="denied", tool_use_id="g1")
        denial = self.bridge.hook_denials(self.run, {"g1": "Grep"}, [])[0]
        self.assertIn("never searchable", denial["reason"])
        self.assertIn("Use Read", denial["reason"])
        self.assertIn("one exact file", denial["reason"])

    def test_allowed_and_denied_matching_events_are_both_audited_and_reported_separately(self):
        self.record("allowed", "Read", "r1")
        self.record("denied", "Glob", "g1")
        coverage = self.bridge.hook_coverage(self.run, {"r1": "Read", "g1": "Glob"})
        self.assertEqual(coverage["status"], "complete")
        self.assertEqual((coverage["expected_count"], coverage["audited_count"]), (2, 2))
        self.assertEqual((coverage["allowed_count"], coverage["denied_count"]), (1, 1))
        self.assertEqual((coverage["allowed_tool_use_ids"], coverage["denied_tool_use_ids"]), (["r1"], ["g1"]))
        self.assertEqual(coverage["missing_tool_use_ids"], [])

    def test_wrong_tool_name_unknown_status_and_other_ids_are_not_audit_evidence(self):
        self.record("denied", "Grep", "r1")
        self.record("pending", "Read", "r2")
        self.record("error", "Read", "r3")
        self.record("allowed", "Read", "unrelated")
        uses = {"r1": "Read", "r2": "Read", "r3": "Read", "r4": "Read"}
        coverage = self.bridge.hook_coverage(self.run, uses)
        self.assertEqual(coverage["status"], "incomplete")
        self.assertEqual(coverage["missing_tool_use_ids"], ["r1", "r2", "r3", "r4"])
        self.assertEqual((coverage["audited_count"], coverage["allowed_count"], coverage["denied_count"]), (0, 0, 0))

    def test_denied_wins_when_one_id_has_both_decisions(self):
        self.record("allowed", "Read", "r1")
        self.record("denied", "Read", "r1")
        coverage = self.bridge.hook_coverage(self.run, {"r1": "Read"})
        self.assertEqual((coverage["allowed_count"], coverage["denied_count"]), (0, 1))

    def test_workflow_children_need_a_decision_for_every_tool_and_denials_count(self):
        self.record("denied", "StructuredOutput", "s1")
        uses = {"s1": "StructuredOutput", "s2": "StructuredOutput"}
        ordinary = self.bridge.hook_coverage(self.run, uses)
        self.assertEqual((ordinary["status"], ordinary["expected_count"]), ("complete", 0))
        workflow = self.bridge.hook_coverage(self.run, uses, guard_all_tools=True)
        self.assertEqual((workflow["denied_tool_use_ids"], workflow["missing_tool_use_ids"]), (["s1"], ["s2"]))

    def formatter_events(self):
        # Redacted native CLI shape: invalid bare evidence value, exact raw
        # length, parent assistant wrapper followed by its parent user error.
        session = "formatter-parent"
        raw = '{"status":"blocked","evidence": Evidence is still pending}'
        def turn(kind, block):
            return {"type": kind, "session_id": session, "parent_tool_use_id": None,
                    "message": {"role": kind, "content": [block]}}
        report = {"status": "completed", "summary": "final report", "evidence": [], "checks": [], "unresolved": []}
        return [
            {"type": "system", "subtype": "init", "session_id": session, "model": "test-model"},
            turn("assistant", {"type": "tool_use", "id": "workflow-1", "name": "Workflow", "input": {"name": "fixture"}}),
            turn("user", {"type": "tool_result", "tool_use_id": "workflow-1", "is_error": False, "content": "acknowledged"}),
            turn("assistant", {"type": "tool_use", "id": "formatter-bad", "name": "StructuredOutput", "caller": {"type": "direct"},
                               "input": {"__unparsedToolInput": {"raw": raw, "len": len(raw.encode("utf-8"))}}}),
            turn("user", {"type": "tool_result", "tool_use_id": "formatter-bad", "is_error": True, "content": "formatter rejected invalid input"}),
            turn("assistant", {"type": "tool_use", "id": "formatter-good", "name": "StructuredOutput", "input": report}),
            turn("user", {"type": "tool_result", "tool_use_id": "formatter-good", "is_error": False, "content": "formatted"}),
            {"type": "result", "subtype": "success", "session_id": session, "parent_tool_use_id": None, "structured_output": report},
        ]

    def parse_formatter_events(self, events):
        stream = self.run / "formatter-stream.jsonl"
        stream.write_text("\n".join(json.dumps(item) for item in events) + "\n")
        return self.bridge.parse_stream(stream, "formatter-parent")

    def formatter_coverage(self, meta):
        return self.bridge.hook_coverage(self.run, meta["guarded_tool_uses"], guard_all_tools=True,
                                         rejected_formatter_tool_uses=meta["rejected_formatter_tool_uses"])

    def test_native_unparsed_formatter_rejection_is_separate_from_hook_coverage(self):
        self.record("allowed", "Workflow", "workflow-1")
        self.record("allowed", "StructuredOutput", "formatter-good")
        final, meta = self.parse_formatter_events(self.formatter_events())
        original = self.bridge.hook_coverage(self.run, meta["guarded_tool_uses"], guard_all_tools=True)
        self.assertEqual((original["expected_count"], original["audited_count"]), (3, 2))
        self.assertEqual(original["missing_tool_use_ids"], ["formatter-bad"])
        coverage = self.formatter_coverage(meta)
        self.assertEqual((coverage["status"], coverage["expected_count"], coverage["audited_count"]), ("complete", 2, 2))
        self.assertEqual(coverage["pre_execution_rejected_formatter_tool_use_ids"], ["formatter-bad"])
        proof = meta["rejected_formatter_tool_uses"]["formatter-bad"]
        self.assertEqual((proof["tool_use_stream_line"], proof["tool_result_stream_line"]), (4, 5))
        self.assertNotIn("raw", proof)
        self.assertRegex(proof["input_sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(self.bridge.result_payload(final)["status"], "completed")

    def test_formatter_exclusion_requires_complete_invalid_input_and_unique_parent_error(self):
        self.record("allowed", "Workflow", "workflow-1")
        self.record("allowed", "StructuredOutput", "formatter-good")
        def use(events): return events[3]["message"]["content"][0]
        def error(events): return events[4]["message"]["content"][0]
        def wrapper(events): return use(events)["input"]["__unparsedToolInput"]
        def set_raw(events, raw):
            use(events)["input"] = {"__unparsedToolInput": {"raw": raw, "len": len(raw.encode("utf-8"))}}
        mutations = [
            ("Read", lambda e: use(e).update(name="Read")),
            ("Workflow", lambda e: use(e).update(name="Workflow")),
            ("unknown tool", lambda e: use(e).update(name="UnknownTool")),
            ("ordinary formatter input", lambda e: use(e).update(input={"status": "completed"})),
            ("extra input field", lambda e: use(e)["input"].update(extra=True)),
            ("extra wrapper field", lambda e: wrapper(e).update(extra=True)),
            ("missing raw", lambda e: wrapper(e).pop("raw")),
            ("missing length", lambda e: wrapper(e).pop("len")),
            ("nonstring raw", lambda e: wrapper(e).update(raw=123)),
            ("wrong length", lambda e: wrapper(e).update(len=1)),
            ("boolean length", lambda e: wrapper(e).update(len=True)),
            ("float length", lambda e: wrapper(e).update(len=57.0)),
            ("valid object raw", lambda e: set_raw(e, '{"status":"completed"}')),
            ("valid scalar raw", lambda e: set_raw(e, 'null')),
            ("no error result", lambda e: e.pop(4)),
            ("successful result", lambda e: error(e).update(is_error=False)),
            ("missing is_error", lambda e: error(e).pop("is_error")),
            ("string is_error", lambda e: error(e).update(is_error="true")),
            ("empty error content", lambda e: error(e).update(content="")),
            ("wrong result id", lambda e: error(e).update(tool_use_id="another-id")),
            ("wrong use session", lambda e: e[3].update(session_id="other-session")),
            ("wrong error session", lambda e: e[4].update(session_id="other-session")),
            ("missing use session", lambda e: e[3].pop("session_id")),
            ("missing error session", lambda e: e[4].pop("session_id")),
            ("conflicting session alias", lambda e: e[4].update(sessionId="other-session")),
            ("child use", lambda e: e[3].update(parent_tool_use_id="workflow-1")),
            ("child error", lambda e: e[4].update(parent_tool_use_id="workflow-1")),
            ("missing use parent identity", lambda e: e[3].pop("parent_tool_use_id")),
            ("missing error parent identity", lambda e: e[4].pop("parent_tool_use_id")),
            ("child caller", lambda e: use(e).update(caller={"type": "agent"})),
            ("nonassistant use", lambda e: e[3].update(type="user")),
            ("nonuser result", lambda e: e[4].update(type="assistant")),
            ("result before use", lambda e: e.insert(3, e.pop(4))),
            ("duplicate use", lambda e: e.insert(5, json.loads(json.dumps(e[3])))),
            ("duplicate result", lambda e: e.insert(5, json.loads(json.dumps(e[4])))),
            ("malformed duplicate use", lambda e: e.insert(5, {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "formatter-bad"}]}})),
            ("provider denial", lambda e: e[-1].update(permission_denials=[{"tool_name": "StructuredOutput", "tool_use_id": "formatter-bad"}])),
        ]
        for name, mutate in mutations:
            with self.subTest(case=name):
                events = self.formatter_events()
                mutate(events)
                _, meta = self.parse_formatter_events(events)
                self.assertEqual(meta["rejected_formatter_tool_uses"], {})
                coverage = self.formatter_coverage(meta)
                self.assertEqual(coverage["missing_tool_use_ids"], ["formatter-bad"])
                self.assertEqual(coverage["pre_execution_rejected_formatter_count"], 0)

    def test_existing_hook_evidence_and_denials_are_never_formatter_exemptions(self):
        _, meta = self.parse_formatter_events(self.formatter_events())
        for status in ("allowed", "denied"):
            with self.subTest(status=status):
                self.record(status, "StructuredOutput", "formatter-bad")
                coverage = self.formatter_coverage(meta)
                self.assertEqual(coverage["pre_execution_rejected_formatter_tool_use_ids"], [])
                self.assertEqual(coverage["expected_count"], 3)
                self.assertIn("formatter-bad", coverage[status + "_tool_use_ids"])
        denials = self.bridge.hook_denials(self.run, meta["guarded_tool_uses"], [])
        self.assertEqual([item["tool_use_id"] for item in denials], ["formatter-bad"])

    def test_rejected_formatter_evidence_does_not_validate_the_final_result(self):
        events = self.formatter_events()
        events[-1]["structured_output"]["summary"] = 5
        final, meta = self.parse_formatter_events(events)
        self.assertIn("formatter-bad", meta["rejected_formatter_tool_uses"])
        with self.assertRaises(self.bridge.BridgeError):
            self.bridge.result_payload(final)

    def test_any_same_id_hook_record_prevents_formatter_exclusion(self):
        _, meta = self.parse_formatter_events(self.formatter_events())
        self.record("denied", "Read", "formatter-bad")
        coverage = self.formatter_coverage(meta)
        self.assertEqual(coverage["pre_execution_rejected_formatter_count"], 0)
        self.assertIn("formatter-bad", coverage["missing_tool_use_ids"])
        self.assertEqual(self.bridge.hook_denials(self.run, meta["guarded_tool_uses"], [])[0]["tool_name"], "Read")

    def test_denial_entries_keep_only_the_actual_denials(self):
        event = {"type": "result", "subtype": "success", "usage": {"input_tokens": 9},
                 "structured_output": {"summary": "SENTINEL_REPORT"}, "result": "SENTINEL_TEXT",
                 "permission_denials": [{"tool_name": "Write", "tool_use_id": "w1", "session_id": "s",
                                         "tool_input": {"file_path": "x.txt", "content": "A" * 5000, "nested": {"a": 1}}},
                                        "plain", {"odd": True}]}
        entries = self.bridge.denial_entries(event)
        self.assertEqual(entries[0], {"tool_name": "Write", "tool_use_id": "w1", "tool_input": {"file_path": "x.txt"}})
        self.assertEqual(entries[1], "plain")
        self.assertEqual(entries[2], {"unrecognized_shape": ["odd"]})
        encoded = json.dumps(entries)
        for leaked in ("SENTINEL", "input_tokens", "AAAA"):
            self.assertNotIn(leaked, encoded)
        self.assertEqual(self.bridge.denial_entries({"type": "result", "permission_denials": []}), [])
        self.assertEqual(self.bridge.denial_entries({"type": "result", "subtype": "success"}), [])

    def test_denial_entries_accept_the_earlier_wrapper_and_subtype_shapes(self):
        wrapped = {"type": "result", "permission_denials": [
            {"type": "result", "permission_denials": [{"tool_name": "Read", "tool_use_id": "t1",
                                                        "tool_input": {"file_path": "/outside"}}]}]}
        self.assertEqual(self.bridge.denial_entries(wrapped),
                         [{"tool_name": "Read", "tool_use_id": "t1", "tool_input": {"file_path": "/outside"}}])
        bare = self.bridge.denial_entries({"type": "system", "subtype": "permission_denials", "message": "blocked",
                                           "session_id": "s", "usage": {"input_tokens": 1}})
        self.assertEqual(bare, [{"message": "blocked", "subtype": "permission_denials"}])
        single = self.bridge.denial_entries({"permission_denials": {"tool_name": "Bash", "tool_use_id": "b1"}})
        self.assertEqual(single, [{"tool_name": "Bash", "tool_use_id": "b1"}])

    def test_explicit_denial_with_empty_list_survives_a_later_success(self):
        denied = {"type": "system", "subtype": "permission_denials", "permission_denials": [],
                  "session_id": "s", "message": "blocked"}
        stream = self.run / "stream.jsonl"
        stream.write_text(json.dumps(denied) + "\n" + json.dumps({"type": "result", "subtype": "success",
                          "session_id": "s", "structured_output": {"status": "completed"}}) + "\n")
        _, meta = self.bridge.parse_stream(stream, "s")
        self.assertEqual(meta["permission_denials"], [{"message": "blocked", "subtype": "permission_denials"}])

    def test_parse_stream_stores_no_result_event_inside_permission_denials(self):
        stream = self.run / "stream.jsonl"
        stream.write_text("\n".join(json.dumps(item) for item in (
            {"type": "system", "subtype": "init", "session_id": "s", "model": "m"},
            {"type": "result", "subtype": "success", "session_id": "s", "usage": {"input_tokens": 2},
             "structured_output": {"summary": "SENTINEL_REPORT"},
             "permission_denials": [{"tool_name": "Read", "tool_use_id": "t1", "tool_input": {"file_path": "/x"}}]})) + "\n")
        final, meta = self.bridge.parse_stream(stream, "s")
        self.assertEqual(final["structured_output"]["summary"], "SENTINEL_REPORT")
        self.assertEqual(meta["permission_denials"], [{"tool_name": "Read", "tool_use_id": "t1", "tool_input": {"file_path": "/x"}}])

    def test_corrupt_frozen_identity_does_not_mask_the_run_outcome(self):
        (self.run / "plugin-identity.json").write_text("{broken")
        packet = {"task_id": "t", "revision": 1}
        self.assertEqual(self.bridge.pre_dispatch_cancelled(self.run, packet, "cancelled"), 1)
        receipt = json.loads((self.run / "receipt.json").read_text())
        self.assertEqual(receipt["status"], "cancelled")
        self.assertEqual(receipt["plugin_identity"]["status"], "unavailable")


class RunEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.enterContext(mock.patch.dict(os.environ, {
            "CLAUDE_ORCHESTRATOR_CLI_ROOT": str(self.root / "isolated-cli-store"),
            "CLAUDE_CONFIG_DIR": str(self.root / "claude-config"),
            "FAKE_MODE": "normal",
        }))
        self.repo = self.root / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        subprocess.run(["git", "-C", str(self.repo), "config", "user.email", "test@example.invalid"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "config", "user.name", "Test"], check=True)
        (self.repo / "base.txt").write_text("base\n")
        subprocess.run(["git", "-C", str(self.repo), "add", "base.txt"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-qm", "base"], check=True)
        self.repo_head = subprocess.run(["git", "-C", str(self.repo), "rev-parse", "HEAD"], check=True,
                                        capture_output=True, text=True).stdout.strip()
        self.requirement = self.root / "requirements.md"
        self.requirement.write_text("must return evidence\n")
        fake = self.root / "fake_claude.py"
        fake.write_text(FAKE_CLAUDE)
        fake.chmod(0o755)
        os.environ["CLAUDE_BIN"] = str(fake)

    def tearDown(self):
        os.environ.pop("CLAUDE_BIN", None)
        self.tmp.cleanup()

    def packet(self):
        return {"task_id": "task-1", "revision": 1, "role": "review", "cwd": str(self.repo), "objective": "check it",
                "requirement_sources": [str(self.requirement)], "constraints": ["no network"], "acceptance": ["structured"],
                "owned_files": [], "protected_files": ["protected.txt"], "model": "test-model", "effort": "low"}

    def run_mode(self, mode, name=None, timeout="5"):
        os.environ["FAKE_MODE"] = mode
        name = name or mode
        packet_path = self.root / (name + ".json")
        packet_path.write_text(json.dumps(self.packet()))
        run = self.root / name
        got = subprocess.run([sys.executable, str(BRIDGE), "run", "--packet", str(packet_path), "--run-dir", str(run),
                              "--timeout", timeout], text=True, capture_output=True)
        result = json.loads((run / "result.json").read_text()) if (run / "result.json").exists() else None
        return got, run, json.loads((run / "receipt.json").read_text()), result

    def test_allowed_tool_use_is_audited_and_the_run_is_reported(self):
        got, run, receipt, result = self.run_mode("allowed")
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertEqual(receipt["status"], "reported")
        coverage = result["hook_guard_coverage"]
        self.assertEqual((coverage["status"], coverage["allowed_count"], coverage["denied_count"]), ("complete", 1, 0))
        self.assertEqual(result["permission_denials"], [])
        self.assertEqual(result["report_evidence"]["state"], "structured")
        self.assertIs(result["report_evidence"]["accepted"], False)

    def test_actual_run_persists_formatter_rejection_proof_and_keeps_final_validation(self):
        for mode in ("formatter_rejected", "formatter_rejected_invalid_final"):
            with self.subTest(mode=mode):
                got, run, receipt, result = self.run_mode(mode)
                proof = result["rejected_formatter_tool_uses"]["invalid-formatter"]
                child = json.loads((run / "child.json").read_text())
                self.assertEqual(proof["session_id"], child["expected_session_id"])
                self.assertEqual(proof["input_bytes"], len(b"{broken formatter input}"))
                self.assertRegex(proof["input_sha256"], r"^[0-9a-f]{64}$")
                self.assertLess(proof["tool_use_stream_line"], proof["tool_result_stream_line"])
                self.assertNotIn("raw", proof)
                self.assertEqual(result["hook_guard_coverage"]["pre_execution_rejected_formatter_count"], 0)
                # Ordinary runs do not guard the formatter; the proof is still
                # durable and cannot make an invalid final report valid.
                if mode.endswith("invalid_final"):
                    self.assertNotEqual(got.returncode, 0)
                    self.assertEqual(receipt["status"], "failed")
                    self.assertEqual(result["report_evidence"]["state"], "validation_error")
                else:
                    self.assertEqual(got.returncode, 0)
                    self.assertEqual(receipt["status"], "reported")

    def test_denied_tool_use_is_audited_but_still_fails_the_run(self):
        for mode in ("denied", "denied_hook_only"):
            with self.subTest(mode=mode):
                got, run, receipt, result = self.run_mode(mode)
                self.assertNotEqual(got.returncode, 0)
                self.assertEqual(receipt["status"], "failed")
                coverage = result["hook_guard_coverage"]
                self.assertEqual((coverage["status"], coverage["allowed_count"], coverage["denied_count"],
                                  coverage["missing_tool_use_ids"]), ("complete", 0, 1, []))
                self.assertIsNone(result["hook_guard_error"])
                self.assertIn("PreToolUse guard denied", result["hook_denial_error"])
                sources = [item.get("source") for item in result["permission_denials"] if isinstance(item, dict)]
                self.assertIn("bridge_pretooluse_hook", sources)

    def test_decision_for_another_id_or_tool_leaves_the_tool_use_unaudited_and_fails(self):
        for mode in ("other_id", "other_tool", "no_hook"):
            with self.subTest(mode=mode):
                got, run, receipt, result = self.run_mode(mode)
                self.assertEqual(receipt["status"], "failed")
                coverage = result["hook_guard_coverage"]
                self.assertEqual((coverage["status"], coverage["missing_tool_use_ids"]), ("incomplete", ["r1"]))
                self.assertEqual(coverage["allowed_count"], 0)
                self.assertIn("lack matching PreToolUse guard evidence", result["hook_guard_error"])

    def test_denial_records_only_the_denied_entries(self):
        got, run, receipt, result = self.run_mode("bulky_denial")
        self.assertEqual(receipt["status"], "failed")
        self.assertEqual(result["permission_denials"],
                         [{"tool_name": "Write", "tool_use_id": "w1", "tool_input": {"file_path": "x.txt"}}])
        encoded = json.dumps(result["permission_denials"])
        for leaked in ("SENTINEL_REPORT", "input_tokens", "structured_output", "AAAA"):
            self.assertNotIn(leaked, encoded)
        stream = (run / "stream.jsonl").read_text()
        self.assertIn("SENTINEL_REPORT", stream)
        self.assertIn("AAAAAAAA", stream)
        self.assertEqual(result["structured"]["summary"], "SENTINEL_REPORT")

    def test_string_denial_shape_still_fails_the_run(self):
        got, run, receipt, result = self.run_mode("string_denial")
        self.assertEqual(receipt["status"], "failed")
        self.assertEqual(result["permission_denials"], ["no"])

    def test_failed_run_with_a_valid_report_keeps_it_as_unaccepted_evidence(self):
        got, run, receipt, result = self.run_mode("valid_report_failed")
        self.assertNotEqual(got.returncode, 0)
        self.assertEqual(receipt["status"], "failed")
        self.assertEqual(json.loads((run / "state.json").read_text())["status"], "failed")
        self.assertEqual(result["structured"]["summary"], "SENTINEL_REPORT")
        self.assertIsNone(result["result_validation_error"])
        self.assertEqual(result["report_evidence"], {
            "state": "structured", "claimed_status": "completed", "run_status": "failed", "accepted": False,
            "note": result["report_evidence"]["note"]})
        self.assertEqual(result["provider_subtype"], "error_during_execution")

    def test_failed_run_with_an_invalid_report_keeps_the_validation_error(self):
        got, run, receipt, result = self.run_mode("invalid_report_failed")
        self.assertEqual(receipt["status"], "failed")
        self.assertIsNone(result["structured"])
        self.assertIn("invalid status or summary", result["result_validation_error"])
        self.assertEqual((result["report_evidence"]["state"], result["report_evidence"]["run_status"],
                          result["report_evidence"]["claimed_status"]), ("validation_error", "failed", None))

    def test_successful_provider_result_with_a_bad_or_missing_report_still_fails(self):
        got, run, receipt, result = self.run_mode("invalid_report_success")
        self.assertEqual(receipt["status"], "failed")
        self.assertIn("invalid status or summary", result["result_validation_error"])
        got, run, receipt, result = self.run_mode("missing_report_success")
        self.assertEqual(receipt["status"], "failed")
        self.assertEqual(result["result_validation_error"], "provider result lacks structured_output object")
        self.assertEqual(result["report_evidence"]["state"], "absent")

    def test_blank_summary_or_item_is_a_schema_failure_but_a_short_report_is_valid(self):
        for mode, message in (("blank_summary", "summary is blank"), ("blank_item", "evidence contains a blank item")):
            with self.subTest(mode=mode):
                got, run, receipt, result = self.run_mode(mode)
                self.assertEqual(receipt["status"], "failed")
                self.assertIn(message, result["result_validation_error"])
                self.assertEqual(result["report_evidence"]["state"], "validation_error")
        got, run, receipt, result = self.run_mode("no_findings")
        self.assertEqual(receipt["status"], "reported", got.stderr)
        self.assertEqual(result["structured"]["summary"], "No findings.")
        schema = json.loads(json.loads((run / "command.json").read_text())["argv"][
            json.loads((run / "command.json").read_text())["argv"].index("--json-schema") + 1])
        self.assertEqual(schema["properties"]["summary"]["pattern"], r"\S")
        self.assertEqual(schema["properties"]["evidence"]["items"]["pattern"], r"\S")

    def test_exact_external_file_is_readable_but_its_parent_directory_search_fails_the_run(self):
        with mock.patch.dict(os.environ, {"FAKE_EXTERNAL": str(self.requirement.resolve())}):
            got, run, receipt, result = self.run_mode("external_exact")
            self.assertEqual(receipt["status"], "reported", got.stderr)
            self.assertEqual(result["hook_guard_coverage"]["allowed_count"], 2)
            got, run, receipt, result = self.run_mode("parent_search")
        self.assertEqual(receipt["status"], "failed")
        denial = next(item for item in result["permission_denials"] if item.get("source") == "bridge_pretooluse_hook")
        self.assertIn("outside cwd", denial["reason"])
        self.assertIn(str(self.requirement.resolve()), denial["reason"])
        self.assertIn("never searchable", denial["reason"])

    def test_failure_without_any_report_is_absent_and_not_a_validation_error(self):
        got, run, receipt, result = self.run_mode("error_no_report")
        self.assertEqual(receipt["status"], "failed")
        self.assertIsNone(result["structured"])
        self.assertIsNone(result["result_validation_error"])
        self.assertEqual(result["report_evidence"]["state"], "absent")

    def test_timeout_after_a_report_is_not_turned_into_success(self):
        got, run, receipt, result = self.run_mode("report_then_hang", timeout="2")
        self.assertNotEqual(got.returncode, 0)
        self.assertEqual(receipt["status"], "timeout")
        self.assertEqual(json.loads((run / "state.json").read_text())["status"], "timeout")
        self.assertEqual(result["structured"]["summary"], "SENTINEL_REPORT")
        self.assertEqual((result["report_evidence"]["state"], result["report_evidence"]["run_status"],
                          result["report_evidence"]["accepted"]), ("structured", "timeout", False))

    def assert_frozen_identity(self, run, receipt, result=None):
        frozen = json.loads((run / "plugin-identity.json").read_text())
        self.assertEqual(receipt["plugin_identity"], frozen)
        if result is not None:
            self.assertEqual(result["plugin_identity"], frozen)
        return frozen

    def test_normal_receipt_and_result_carry_the_frozen_plugin_identity(self):
        module = load_bridge_module()
        got, run, receipt, result = self.run_mode("allowed")
        frozen = self.assert_frozen_identity(run, receipt, result)
        plugin_manifest = json.loads((PLUGIN_ROOT / ".codex-plugin" / "plugin.json").read_text())
        self.assertEqual(frozen["plugin_version"], plugin_manifest["version"])
        self.assertEqual(frozen["plugin_root"], str(PLUGIN_ROOT.resolve()))
        self.assertEqual(frozen["bridge_contract_id"], module.bridge_contract_id())
        self.assertRegex(frozen["code_digest"]["value"], r"^[0-9a-f]{64}$")
        self.assertIn(frozen["source"]["state"], {"clean", "dirty", "unknown"})
        self.assertIn(frozen["source"]["kind"], {"git", "release_manifest", "unknown"})
        # The reviewed repository's HEAD must never stand in for the plugin's own revision.
        self.assertNotEqual(frozen["source"].get("revision"), self.repo_head)

    def test_preflight_blocked_receipt_carries_the_identity_frozen_at_round_start(self):
        got, run, receipt, result = self.run_mode("logged_out")
        self.assertEqual(receipt["status"], "blocked")
        self.assertFalse((run / "command.json").exists())
        self.assert_frozen_identity(run, receipt)

    def test_provider_failure_receipt_carries_the_same_identity(self):
        got, run, receipt, result = self.run_mode("denied")
        self.assertEqual(receipt["status"], "failed")
        self.assert_frozen_identity(run, receipt, result)

    def test_bridge_failure_receipt_carries_the_identity(self):
        config = self.root / "claude-config"
        config.mkdir()
        (config / "settings.json").write_text(json.dumps({"disableAllHooks": True}))
        got, run, receipt, result = self.run_mode("allowed", name="hooks-disabled")
        self.assertEqual(receipt["status"], "failed")
        self.assertIn("bridge failure", receipt["note"])
        self.assertTrue((run / "error.json").exists())
        self.assert_frozen_identity(run, receipt)

    def test_early_cancel_receipt_carries_the_identity(self):
        marker = self.root / "cancel-marker"
        marker.write_text("cancel")
        with mock.patch.dict(os.environ, {"CODEX_BRIDGE_CANCEL_FILE": str(marker)}):
            got, run, receipt, result = self.run_mode("allowed", name="early-cancel")
        self.assertEqual(receipt["status"], "cancelled")
        self.assert_frozen_identity(run, receipt)


if __name__ == "__main__":
    unittest.main()
