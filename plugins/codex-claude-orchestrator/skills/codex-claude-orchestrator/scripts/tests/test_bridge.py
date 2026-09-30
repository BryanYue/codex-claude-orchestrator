import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
import importlib.util
from pathlib import Path
from unittest import mock

BRIDGE = Path(__file__).resolve().parents[1] / "bridge.py"


def load_bridge_module():
    scripts = str(BRIDGE.parent)
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    spec = importlib.util.spec_from_file_location("bridge_cleanup_unit", BRIDGE)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.enterContext(mock.patch.dict(os.environ, {
            "CLAUDE_ORCHESTRATOR_CLI_ROOT": str(self.root / "isolated-cli-store"),
            "CLAUDE_CONFIG_DIR": str(self.root / "claude-config"),
        }))
        self.repo = self.root / "repo"; self.repo.mkdir()
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        subprocess.run(["git", "-C", str(self.repo), "config", "user.email", "test@example.invalid"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "config", "user.name", "Test"], check=True)
        (self.repo / "base.txt").write_text("base\n")
        subprocess.run(["git", "-C", str(self.repo), "add", "base.txt"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-qm", "base"], check=True)
        self.requirement = self.root / "requirements.md"; self.requirement.write_text("must return evidence\n")
        self.fake = self.root / "fake_claude.py"
        self.fake.write_text("""#!/usr/bin/env python3
import json, os, pathlib, subprocess, sys, time
mode=os.environ.get('FAKE_MODE','normal')
args=sys.argv[1:]
if os.environ.get('FAKE_CALLS_PATH'):
    with open(os.environ['FAKE_CALLS_PATH'],'a') as log: log.write(json.dumps(args)+'\\n')
if args == ['--version']: print(os.environ.get('FAKE_VERSION', '2.1.276') + ' (Claude Code)'); sys.exit(0)
if args == ['--help']:
    flags='-p --model --effort --output-format --verbose --json-schema --session-id --resume --permission-mode --tools --allowedTools --disallowedTools --settings --strict-mcp-config --mcp-config --disable-slash-commands --no-session-persistence --max-turns --max-budget-usd'.split()
    print(' '.join(flag for flag in flags if flag != os.environ.get('FAKE_OMIT_FLAG'))); sys.exit(0)
if args == ['auth','status','--json']:
    if mode == 'auth_malformed': print('SECRET_SENTINEL invalid json'); sys.exit(1)
    if mode == 'auth_timeout': time.sleep(4)
    if mode == 'auth_inconsistent': print(json.dumps({'loggedIn':True})); sys.exit(1)
    if mode == 'logged_out': print(json.dumps({'loggedIn':False,'secret':'SECRET_SENTINEL'})); sys.exit(1)
    auth={'loggedIn':True,'authMethod':'claude.ai','apiProvider':'firstParty','email':'tester@example.invalid','accessToken':'SECRET_SENTINEL'}
    if mode == 'api_key': auth={'loggedIn':True,'authMethod':'api_key','apiProvider':'firstParty','apiKey':'SECRET_SENTINEL'}
    print(json.dumps(auth)); sys.exit(0)
if '--no-session-persistence' in args:
    if mode == 'probe_timeout': time.sleep(4)
    errors={'probe_auth':'401 authentication_error SECRET_SENTINEL', 'probe_quota':'429 rate_limit_error SECRET_SENTINEL', 'probe_network':'connection error SECRET_SENTINEL', 'probe_access':'403 permission_error SECRET_SENTINEL'}
    if mode in errors: print(json.dumps({'type':'result','subtype':'error_during_execution','is_error':True,'result':errors[mode]})); sys.exit(1)
    print(json.dumps({'type':'result','subtype':'success','is_error':False,'result':'AUTH_CHECK_OK'})); sys.exit(0)
def option(name): return args[args.index(name)+1] if name in args else None
session=option('--resume') or option('--session-id')
if mode == 'exit_immediately': sys.exit(17)
if mode == 'wrong_session': session='wrong-session'
print(json.dumps({'type':'system','subtype':'init','session_id':session,'model':'test-model'}))
if mode == 'sleep': time.sleep(4)
if mode == 'childstdout': subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(.2)'])
if mode == 'stubbornstdout':
    child=subprocess.Popen([sys.executable, '-c', 'import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); print("child ready",flush=True); time.sleep(30)'])
    pathlib.Path(os.environ['CHILD_PID_FILE']).write_text(str(child.pid))
    time.sleep(.2)
if mode == 'touch': pathlib.Path('outside.txt').write_text('outside')
if mode == 'protectedignored': pathlib.Path('protected.txt').write_text('tampered')
if mode == 'touchdir': pathlib.Path('nested').mkdir(); pathlib.Path('nested/out.txt').write_text('outside')
if mode == 'malformed': print('{bad')
elif mode == 'error': print(json.dumps({'type':'result','subtype':'error','is_error':True,'session_id':session})); sys.exit(7)
elif mode == 'denial': print(json.dumps({'type':'result','subtype':'success','permission_denials':['no'],'session_id':session,'structured_output':{'status':'completed','summary':'x','evidence':[],'checks':[],'unresolved':[]}}))
else: print(json.dumps({'type':'result','subtype':'success','session_id':session,'usage':{'input_tokens':3},'structured_output':{'status':'completed','summary':'done','evidence':['e'], 'checks':['c'], 'unresolved':[]}}))
""")
        self.fake.chmod(0o755)
        self.old_bin, self.old_mode = os.environ.get("CLAUDE_BIN"), os.environ.get("FAKE_MODE")
        os.environ["CLAUDE_BIN"] = str(self.fake)
        os.environ["FAKE_MODE"] = "normal"

    def tearDown(self):
        os.environ.pop('FAKE_CALLS_PATH', None)
        os.environ.pop('FAKE_VERSION', None)
        os.environ.pop('FAKE_OMIT_FLAG', None)
        if self.old_bin is None: os.environ.pop("CLAUDE_BIN", None)
        else: os.environ["CLAUDE_BIN"] = self.old_bin
        if self.old_mode is None: os.environ.pop("FAKE_MODE", None)
        else: os.environ["FAKE_MODE"] = self.old_mode
        self.tmp.cleanup()

    def packet(self, role="review", revision=1, correction=None):
        data = {"task_id":"task-1", "revision":revision, "role":role, "cwd":str(self.repo), "objective":"check it",
                "requirement_sources":[str(self.requirement)], "constraints":["no network"], "acceptance":["structured"],
                "owned_files":["owned.txt"] if role == "implement" else [], "protected_files":["protected.txt"], "model":"test-model", "effort":"low"}
        if correction: data["correction"] = correction
        return data

    def invoke(self, packet, name="run", resume=None, timeout="3"):
        p = self.root / (name + ".json"); p.write_text(json.dumps(packet))
        run = self.root / name
        cmd = [sys.executable, str(BRIDGE), "run", "--packet", str(p), "--run-dir", str(run), "--timeout", timeout]
        if resume: cmd += ["--resume-from", str(resume)]
        got = subprocess.run(cmd, text=True, capture_output=True)
        return got, run

    def doctor(self, *extra):
        got = subprocess.run([sys.executable, str(BRIDGE), 'doctor', '--cwd', str(self.repo), *extra],
                             text=True, capture_output=True, timeout=10)
        self.assertNotIn('SECRET_SENTINEL', got.stdout + got.stderr)
        return got, json.loads(got.stdout)

    def test_doctor_reports_missing_cli_and_dispatch_is_blocked(self):
        os.environ['CLAUDE_BIN'] = str(self.root / 'not-installed')
        got, report = self.doctor()
        self.assertNotEqual(got.returncode, 0)
        self.assertEqual(report['status'], 'cli_not_found')
        got, run = self.invoke(self.packet(), 'no-cli')
        self.assertNotEqual(got.returncode, 0)
        self.assertEqual(json.loads((run/'state.json').read_text())['status'], 'blocked')
        self.assertFalse((run/'command.json').exists())

    def test_model_alias_and_explicit_future_id_are_forwarded_without_version_mapping(self):
        calls = self.root / "calls.jsonl"
        os.environ['FAKE_CALLS_PATH'] = str(calls)
        for index, model in enumerate(['opus', 'future-family', 'claude-future-99']):
            packet = self.packet(); packet['model'] = model
            got, run = self.invoke(packet, 'model-' + str(index))
            self.assertEqual(got.returncode, 0, got.stderr)
            command = [json.loads(line) for line in calls.read_text().splitlines() if '--model' in line][-1]
            self.assertEqual(command[command.index('--model') + 1], model)
            result = json.loads((run / 'result.json').read_text())
            self.assertEqual(result['requested_model'], model)
            self.assertEqual(result['system_init_model'], 'test-model')
        module = load_bridge_module()
        for value in ['', '   ']:
            packet = self.packet(); packet['model'] = value
            with self.assertRaisesRegex(module.BridgeError, 'non-empty'):
                module.validate_packet(packet)

    def test_doctor_local_login_does_not_claim_remote_validity_or_leak_credentials(self):
        got, report = self.doctor()
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertEqual(report['auth']['credential_validity'], 'not_verified')
        self.assertEqual(report['auth']['account_masked'], 't***@example.invalid')
        self.assertNotIn('tester@', got.stdout)
        os.environ['FAKE_MODE'] = 'api_key'
        got, report = self.doctor()
        self.assertEqual(got.returncode, 0)
        self.assertEqual(report['auth']['auth_method'], 'api_key')

    def test_unlisted_local_cli_version_runs_when_flags_and_login_pass(self):
        os.environ['FAKE_VERSION'] = '2.1.284'
        got, report = self.doctor()
        self.assertEqual(got.returncode, 0, got.stdout + got.stderr)
        self.assertEqual(report['status'], 'local_checks_passed')
        self.assertEqual(report['cli']['version'], '2.1.284')
        self.assertEqual(report['cli']['missing_flags'], [])
        self.assertFalse(report['compatibility']['behavior_verified'])
        got, run = self.invoke(self.packet(), 'unlisted-version')
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertEqual(json.loads((run / 'receipt.json').read_text())['status'], 'reported')
        self.assertEqual(json.loads((run / 'environment.json').read_text())['cli_descriptor']['version'], '2.1.284')

    def test_logged_out_stops_before_any_task_request(self):
        os.environ['FAKE_MODE'] = 'logged_out'
        calls = self.root / 'calls.jsonl'
        os.environ['FAKE_CALLS_PATH'] = str(calls)
        got, report = self.doctor('--verify')
        self.assertEqual(report['status'], 'not_logged_in')
        self.assertNotEqual(got.returncode, 0)
        got, run = self.invoke(self.packet(), 'logged-out')
        self.assertNotEqual(got.returncode, 0)
        self.assertFalse(any('-p' in json.loads(line) for line in calls.read_text().splitlines()))
        self.assertEqual(json.loads((run/'receipt.json').read_text())['status'], 'blocked')
        self.assertNotIn('SECRET_SENTINEL', (run/'environment.json').read_text())

    def test_unreadable_or_inconsistent_auth_status_is_not_logged_out(self):
        for mode in ('auth_malformed', 'auth_inconsistent', 'auth_timeout'):
            os.environ['FAKE_MODE'] = mode
            got, report = self.doctor('--timeout', '.3')
            self.assertNotEqual(got.returncode, 0)
            self.assertEqual(report['status'], 'auth_check_failed')

    def test_online_verification_checks_a_real_request_without_tools(self):
        calls = self.root / 'calls.jsonl'
        os.environ['FAKE_CALLS_PATH'] = str(calls)
        got, report = self.doctor('--verify', '--model', 'test-model')
        self.assertEqual(got.returncode, 0)
        self.assertEqual(report['auth']['credential_validity'], 'verified_for_request')
        invocation = [json.loads(line) for line in calls.read_text().splitlines() if '-p' in json.loads(line)][0]
        self.assertEqual(invocation[invocation.index('--tools') + 1], '')
        self.assertIn('--no-session-persistence', invocation)

    def test_every_option_in_the_task_command_is_checked_by_preflight(self):
        module = load_bridge_module()
        got, run = self.invoke(self.packet(), 'command-audit')
        self.assertEqual(got.returncode, 0, got.stderr)
        argv = json.loads((run / 'command.json').read_text())['argv'][1:]
        options = {token for token in argv if token.startswith('-')}
        self.assertIn('--verbose', options)
        self.assertEqual(options - module.required_flags({'core', 'read_only'}), set())

    def test_absent_verbose_flag_blocks_task_before_any_provider_request(self):
        os.environ['FAKE_OMIT_FLAG'] = '--verbose'
        calls = self.root / 'calls.jsonl'
        os.environ['FAKE_CALLS_PATH'] = str(calls)
        got, report = self.doctor()
        self.assertNotEqual(got.returncode, 0)
        self.assertEqual(report['status'], 'cli_incompatible')
        self.assertEqual(report['cli']['missing_flags'], ['--verbose'])
        got, run = self.invoke(self.packet(), 'no-verbose')
        self.assertNotEqual(got.returncode, 0)
        receipt = json.loads((run / 'receipt.json').read_text())
        self.assertEqual((receipt['status'], receipt['blocked_by'], receipt['reason']), ('blocked', 'preflight', 'cli_incompatible'))
        self.assertFalse((run / 'command.json').exists())
        self.assertFalse(any('-p' in json.loads(line) for line in calls.read_text().splitlines()))

    def test_session_persistence_flag_gates_only_doctor_verify(self):
        os.environ['FAKE_OMIT_FLAG'] = '--no-session-persistence'
        calls = self.root / 'calls.jsonl'
        os.environ['FAKE_CALLS_PATH'] = str(calls)
        got, report = self.doctor('--verify', '--model', 'test-model')
        self.assertNotEqual(got.returncode, 0)
        self.assertEqual(report['status'], 'cli_incompatible')
        self.assertEqual(report['cli']['missing_flags'], ['--no-session-persistence'])
        self.assertFalse(any('-p' in json.loads(line) for line in calls.read_text().splitlines()))
        got, report = self.doctor()
        self.assertEqual(got.returncode, 0, got.stdout + got.stderr)
        self.assertEqual(report['status'], 'local_checks_passed')
        got, run = self.invoke(self.packet(), 'no-session-persistence-flag')
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertEqual(json.loads((run / 'receipt.json').read_text())['status'], 'reported')

    def test_online_auth_rejection_is_distinct_from_quota_network_and_access(self):
        expected = {'probe_auth':'authentication_failed', 'probe_quota':'quota_or_rate_limited',
                    'probe_network':'network_error', 'probe_access':'access_denied', 'probe_timeout':'verification_timeout'}
        for mode, category in expected.items():
            os.environ['FAKE_MODE'] = mode
            got, report = self.doctor('--verify', '--timeout', '.3')
            self.assertNotEqual(got.returncode, 0)
            self.assertEqual(report['status'], category)
            validity = 'rejected' if mode == 'probe_auth' else 'not_verified'
            self.assertEqual(report['auth']['credential_validity'], validity)

    def test_normal_records_reported_result_and_stream(self):
        got, run = self.invoke(self.packet())
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertEqual(json.loads((run / "receipt.json").read_text())["status"], "reported")
        result = json.loads((run / "result.json").read_text())
        self.assertEqual(result["provider_subtype"], "success")
        self.assertEqual(result["actual_session_id"], json.loads((run / "command.json").read_text())["initial_session_id"])
        self.assertEqual(result["system_init_model"], "test-model")
        self.assertTrue((run / "stream.jsonl").read_text())
        command = json.loads((run / "command.json").read_text())["argv"]
        self.assertIn("Workflow", command[command.index("--disallowedTools") + 1])

    def test_budget_is_passed_only_when_help_advertises_it_and_is_reported_as_provider_enforced(self):
        packet = self.packet(); packet['budget'] = {'max_turns': 3, 'max_budget_usd': 1.25}
        got, run = self.invoke(packet, 'budget')
        self.assertEqual(got.returncode, 0, got.stderr)
        command = json.loads((run / 'command.json').read_text())['argv']
        self.assertEqual(command[command.index('--max-turns') + 1], '3')
        self.assertEqual(command[command.index('--max-budget-usd') + 1], '1.25')
        result = json.loads((run / 'result.json').read_text())
        self.assertEqual(result['budget_enforcement'], 'provider_enforced')
        self.assertEqual(result['usage_summary'], {'input_tokens': 3})

    def test_error_exit_malformed_and_permission_denial_fail(self):
        for mode in ("error", "malformed", "denial"):
            os.environ["FAKE_MODE"] = mode
            got, run = self.invoke(self.packet(), mode)
            self.assertNotEqual(got.returncode, 0, mode)
            self.assertEqual(json.loads((run / "receipt.json").read_text())["status"], "failed")

    def test_duplicate_new_run_dir_is_rejected(self):
        got, run = self.invoke(self.packet(), "duplicate")
        self.assertEqual(got.returncode, 0)
        p = self.root / "again.json"; p.write_text(json.dumps(self.packet()))
        again = subprocess.run([sys.executable, str(BRIDGE), "run", "--packet", str(p), "--run-dir", str(run), "--timeout", "1"], text=True, capture_output=True)
        self.assertEqual(again.returncode, 2)
        self.assertIn("already exists", again.stderr)

    def test_explicit_resume_requires_matching_completed_session(self):
        first, previous = self.invoke(self.packet(), "first")
        self.assertEqual(first.returncode, 0, first.stderr)
        correction = {"finding_id":"f-1", "kind":"local", "reason":"fix", "attempt":1}
        second, current = self.invoke(self.packet(revision=2, correction=correction), "second", previous)
        self.assertEqual(second.returncode, 0, second.stderr)
        command = json.loads((current / "command.json").read_text())["argv"]
        self.assertIn("--resume", command)
        self.assertEqual(command[command.index("--resume") + 1], json.loads((previous / "result.json").read_text())["actual_session_id"])

    def test_resume_freezes_contract_and_review_snapshot(self):
        first, previous = self.invoke(self.packet(), "freeze-first")
        self.assertEqual(first.returncode, 0, first.stderr)
        correction = {"finding_id":"f-1", "kind":"local", "reason":"fix", "attempt":1}
        changed = self.packet(revision=2, correction=correction); changed["acceptance"] = ["different"]
        contract, _ = self.invoke(changed, "freeze-contract", previous)
        self.assertEqual(contract.returncode, 2)
        self.assertIn("frozen", contract.stderr)
        self.requirement.write_text("changed source")
        source, _ = self.invoke(self.packet(revision=2, correction=correction), "freeze-source", previous)
        self.assertEqual(source.returncode, 1)
        self.assertIn("requirement source", (self.root / "freeze-source" / "error.json").read_text())
        self.requirement.write_text("must return evidence\n")
        (self.repo / "external.txt").write_text("external")
        workspace, _ = self.invoke(self.packet(revision=2, correction=correction), "freeze-workspace", previous)
        self.assertEqual(workspace.returncode, 1)
        self.assertIn("differs", (self.root / "freeze-workspace" / "error.json").read_text())

    def test_wrong_provider_session_fails_and_is_not_resumable(self):
        os.environ["FAKE_MODE"] = "wrong_session"
        got, run = self.invoke(self.packet(), "wrong-session")
        self.assertNotEqual(got.returncode, 0)
        self.assertIn("must both equal", json.loads((run / "result.json").read_text())["session_error"])

    def test_cancel_late_success_is_conservatively_cancelled(self):
        os.environ["FAKE_MODE"] = "sleep"
        p = self.root / "cancel.json.packet"; p.write_text(json.dumps(self.packet()))
        run = self.root / "cancel-run"
        child = subprocess.Popen([sys.executable, str(BRIDGE), "run", "--packet", str(p), "--run-dir", str(run), "--timeout", "8"], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        for _ in range(40):
            if (run / "state.json").exists(): break
            time.sleep(.05)
        marked = subprocess.run([sys.executable, str(BRIDGE), "cancel", "--run-dir", str(run), "--reason", "stop"], text=True, capture_output=True)
        self.assertEqual(marked.returncode, 0, marked.stderr)
        self.assertNotEqual(child.wait(timeout=5), 0)
        child.stdout.close(); child.stderr.close()
        receipt = json.loads((run / "receipt.json").read_text())
        self.assertEqual(receipt["status"], "cancelled")

    def test_liveness_permission_error_requires_independent_absence_evidence(self):
        bridge = load_bridge_module()
        for absent in (False, True):
            proc = mock.Mock(pid=987654); proc.poll.return_value = None
            with self.subTest(absent=absent), mock.patch.object(bridge.os, "killpg", side_effect=[None, PermissionError(1, "denied")]), mock.patch.object(bridge, "process_group_absent", return_value=absent):
                diagnostic = bridge.terminate_group(proc)
            if absent:
                self.assertIsNone(diagnostic)
            else:
                self.assertIn("liveness check failed", diagnostic)

    def test_fast_exit_is_reaped_without_unknown_cleanup_marker(self):
        os.environ["FAKE_MODE"] = "exit_immediately"
        got, run = self.invoke(self.packet(), "fast-exit")
        self.assertNotEqual(got.returncode, 0)
        self.assertEqual(json.loads((run / "receipt.json").read_text())["status"], "failed")
        self.assertEqual(json.loads((run / "state.json").read_text())["status"], "failed")
        self.assertFalse(load_bridge_module().unknown_lane_marker(self.repo).exists())

    def test_terminate_group_reaps_exited_direct_child_before_signalling(self):
        bridge = load_bridge_module()
        proc = mock.Mock(pid=987654); proc.poll.return_value = 17
        with mock.patch.object(bridge, "process_group_stopped", return_value=True) as stopped, \
             mock.patch.object(bridge.os, "killpg") as killpg:
            self.assertIsNone(bridge.terminate_group(proc))
        stopped.assert_called_once_with(proc.pid)
        killpg.assert_not_called()

    def test_process_list_absence_rejects_incomplete_or_failed_inspection(self):
        bridge = load_bridge_module()
        own = os.getpid()
        cases = [(0, f"{own} 1\n8 8\n", True), (0, f"{own} 1\n8 987654\n", False),
                 (1, "", False), (0, "", False), (0, f"{own} 1\nmalformed\n", False)]
        for code, output, expected in cases:
            with self.subTest(output=output, code=code), mock.patch.object(bridge.subprocess, "run", return_value=mock.Mock(returncode=code, stdout=output)):
                self.assertEqual(bridge.process_group_absent(987654), expected)

    def test_process_identity_distinguishes_pid_reuse_and_bound_descendants(self):
        bridge = load_bridge_module()
        identity = {"pid": 42, "process_group":42, "start_time": "Mon Sep 23 10:00:00 2026",
                    "command_sha256": bridge.hashlib.sha256(b"bound command").hexdigest(),
                    "expected_session_id": "session-bound"}
        with mock.patch.object(bridge.os, "killpg", return_value=None):
            with mock.patch.object(bridge, "_process_rows", return_value=[
                    {"pid":42, "process_group":42, "start_time":"Mon Sep 23 10:01:00 2026", "command":"reused"}]):
                result = bridge.process_identity_presence(identity, 42, "fixture group")
                self.assertEqual(result["state"], "stopped")
                self.assertIn("reused", result["reason"])
            with mock.patch.object(bridge, "_process_rows", return_value=[
                    {"pid":42, "process_group":42, "start_time":"Mon Sep 23 10:00:00 2026", "command":"exec changed"}]):
                result = bridge.process_identity_presence(identity, 42, "fixture group")
                self.assertEqual(result["state"], "unconfirmed")
                self.assertIn("command identity changed", result["reason"])
            with mock.patch.object(bridge, "_process_rows", return_value=[
                    {"pid":99, "process_group":42, "start_time":"Mon Sep 23 10:02:00 2026",
                     "command":"provider --session-id session-bound"}]):
                self.assertEqual(bridge.process_identity_presence(identity, 42, "fixture group")["state"], "running")
            with mock.patch.object(bridge, "_process_rows", return_value=[
                    {"pid":99, "process_group":42, "start_time":"Mon Sep 23 10:02:00 2026", "command":"unrelated"}]):
                self.assertEqual(bridge.process_identity_presence(identity, 42, "fixture group")["state"], "unconfirmed")

    def test_hook_coverage_matches_the_installed_matcher(self):
        bridge = load_bridge_module()
        run = self.root / "coverage"; run.mkdir()
        bridge.append_activity(run, "tool", "read permitted", tool="Read", status="allowed", tool_use_id="read-1")
        uses = {"read-1":"Read", "format-1":"StructuredOutput"}
        ordinary = bridge.hook_coverage(run, uses)
        self.assertEqual(ordinary["status"], "complete")
        self.assertEqual(ordinary["expected_count"], 1)
        workflow = bridge.hook_coverage(run, uses, guard_all_tools=True)
        self.assertEqual(workflow["status"], "incomplete")
        self.assertEqual(workflow["missing_tool_use_ids"], ["format-1"])

    def test_hook_denials_come_from_activity_and_are_correlated_with_the_provider(self):
        bridge = load_bridge_module()
        run = self.root / "denials"; run.mkdir()
        bridge.append_activity(run, "tool", "read permitted", tool="Read", status="allowed", tool_use_id="read-1")
        self.assertEqual(bridge.hook_denials(run, {"read-1":"Read"}, []), [])
        bridge.append_activity(run, "tool", "tool path denied", tool="Read", status="denied", text="outside", tool_use_id="read-2")
        bridge.append_activity(run, "tool", "tool path denied", tool="Glob", status="denied", text="outside", tool_use_id="child-1")
        provider = [{"type":"result", "permission_denials":[{"tool_name":"Read", "tool_use_id":"read-2"}]}]
        denials = bridge.hook_denials(run, {"read-1":"Read", "read-2":"Read"}, provider)
        self.assertEqual([(d["tool_use_id"], d["tool_name"], d["reason"], d["in_provider_stream"], d["in_provider_denials"])
                          for d in denials],
                         [("read-2", "Read", "outside", True, True), ("child-1", "Glob", "outside", False, False)])
        self.assertTrue(all(d["source"] == "bridge_pretooluse_hook" and isinstance(d["activity_seq"], int) for d in denials))

    def test_permission_denied_cleanup_records_unknown_receipt_and_cwd_marker(self):
        bridge = load_bridge_module()
        class Proc:
            pid = 987654
            @staticmethod
            def poll(): return None
        with mock.patch.object(bridge.os, "killpg", side_effect=PermissionError(1, "Operation not permitted")), \
             mock.patch.object(bridge, "process_group_absent", return_value=False):
            diagnostic = bridge.terminate_group(Proc())
        self.assertIn("PermissionError", diagnostic)
        run = self.root / "unconfirmed-cleanup"
        run.mkdir()
        self.assertEqual(bridge.record_unconfirmed_cleanup(run, self.packet(), "cancelled run", diagnostic), 1)
        state = json.loads((run / "state.json").read_text())
        receipt = json.loads((run / "receipt.json").read_text())
        error = json.loads((run / "error.json").read_text())
        marker = bridge.unknown_lane_marker(self.repo)
        self.addCleanup(marker.unlink, missing_ok=True)
        self.assertEqual(state["status"], "unknown")
        self.assertEqual(state["cleanup_status"], "unconfirmed")
        self.assertEqual(receipt["status"], "unknown")
        self.assertEqual(receipt["cleanup_status"], "unconfirmed")
        self.assertIn("SIGTERM process-group request failed", error["cleanup_error"])
        self.assertEqual(json.loads(marker.read_text())["run_id"], run.name)

    def test_cleanup_failure_preserves_original_exception_and_keyboard_interrupt_is_unknown(self):
        bridge = load_bridge_module()
        original_run = self.root / "exception-unconfirmed"
        original_run.mkdir()
        self.assertEqual(bridge.record_unconfirmed_cleanup(
            original_run, self.packet(), "bridge exception cleanup",
            "SIGTERM process-group request failed: PermissionError: [Errno 1] Operation not permitted",
            RuntimeError("preflight invariant failed"),
        ), 1)
        original_error = json.loads((original_run / "error.json").read_text())
        self.assertEqual(original_error["original_error"], "RuntimeError: preflight invariant failed")
        run = self.root / "keyboard-interrupt-unconfirmed"
        run.mkdir()
        self.assertEqual(bridge.record_unconfirmed_cleanup(
            run, self.packet(), "KeyboardInterrupt",
            "SIGKILL process-group request failed: PermissionError: [Errno 1] Operation not permitted",
            KeyboardInterrupt(),
        ), 1)
        marker = bridge.unknown_lane_marker(self.repo)
        lane = bridge.lane_identity(self.repo)
        self.addCleanup(lambda: [path.unlink(missing_ok=True) for path, _ in bridge.unknown_markers(lane)])
        # The second unknown run must not replace the first run's evidence.
        self.assertEqual({value["run_id"] for _, value in bridge.unknown_markers(lane)}, {original_run.name, run.name})
        state = json.loads((run / "state.json").read_text())
        receipt = json.loads((run / "receipt.json").read_text())
        error = json.loads((run / "error.json").read_text())
        self.assertEqual(state["status"], "unknown")
        self.assertEqual(receipt["status"], "unknown")
        self.assertEqual(state["reason"], "KeyboardInterrupt")
        self.assertEqual(error["original_error"], "KeyboardInterrupt: ")
        self.assertEqual(json.loads(marker.read_text())["cleanup_status"], "unconfirmed")

    def test_actual_run_records_cleanup_failure_for_timeout_and_exception_paths(self):
        from argparse import Namespace
        from contextlib import ExitStack
        bridge = load_bridge_module()
        original_cleanup = bridge.terminate_group
        os.environ["FAKE_MODE"] = "sleep"
        packet_path = self.root / "cleanup-packet.json"
        packet_path.write_text(json.dumps(self.packet()))
        def cleanup_and_report_error(proc):
            # Stop the real fixture before simulating an unconfirmed check.
            actual = original_cleanup(proc)
            self.assertIsNone(actual, "This regression requires normal host process permissions")
            return "injected PermissionError during process-group confirmation"
        for kind, failure in (("timeout", None), ("exception", RuntimeError("injected collection failure")), ("interrupt", KeyboardInterrupt())):
            run = self.root / ("actual-cleanup-" + kind)
            with self.subTest(kind=kind), ExitStack() as stack:
                stack.enter_context(mock.patch.object(bridge, "terminate_group", side_effect=cleanup_and_report_error))
                if failure is not None:
                    stack.enter_context(mock.patch.object(bridge.selectors, "DefaultSelector", side_effect=failure))
                code = bridge.run(Namespace(packet=str(packet_path), run_dir=str(run), resume_from=None, timeout=.05))
            receipt = json.loads((run / "receipt.json").read_text())
            self.assertNotEqual(code, 0)
            self.assertEqual(receipt["status"], "unknown")
            self.assertEqual(receipt["cleanup_status"], "unconfirmed")
            marker = bridge.unknown_lane_marker(self.repo)
            self.assertEqual(json.loads(marker.read_text())["run_id"], run.name)
            if kind == "exception":
                self.assertIn("injected collection failure", json.loads((run/"error.json").read_text())["original_error"])
            marker.unlink()

    def test_timeout_is_recorded(self):
        os.environ["FAKE_MODE"] = "sleep"
        got, run = self.invoke(self.packet(), "timeout", timeout=".1")
        self.assertNotEqual(got.returncode, 0)
        receipt = json.loads((run / "receipt.json").read_text())
        self.assertEqual(receipt["status"], "timeout")

    def test_hook_rejects_traversal_and_allows_exact_owned_file(self):
        packet = self.packet("implement")
        p = self.root / "hook-packet.json"; p.write_text(json.dumps(packet))
        for target, decision in (("../escape.txt", "deny"), ("protected.txt", "deny"), ("owned.txt", "allow")):
            event = json.dumps({"tool_name":"Write", "tool_input":{"file_path":target}, "tool_use_id":"write-" + target})
            got = subprocess.run([sys.executable, str(BRIDGE), "hook", "--packet", str(p), "--cwd", str(self.repo)], input=event, text=True, capture_output=True)
            if decision == "allow":
                self.assertEqual(got.stdout, "")
            else:
                self.assertEqual(json.loads(got.stdout)["hookSpecificOutput"]["permissionDecision"], decision)
        packet["owned_files"].append("linked.txt")
        p.write_text(json.dumps(packet))
        (self.repo / "linked.txt").symlink_to(self.root / "elsewhere.txt")
        linked = subprocess.run([sys.executable, str(BRIDGE), "hook", "--packet", str(p), "--cwd", str(self.repo)], input=json.dumps({"tool_name":"Write", "tool_input":{"file_path":"linked.txt"}, "tool_use_id":"write-linked"}), text=True, capture_output=True)
        self.assertEqual(json.loads(linked.stdout)["hookSpecificOutput"]["permissionDecision"], "deny")
        glob = subprocess.run([sys.executable, str(BRIDGE), "hook", "--packet", str(p), "--cwd", str(self.repo)], input=json.dumps({"tool_name":"Glob", "tool_input":{"path":".", "pattern":"../*.txt"}, "tool_use_id":"glob-traversal"}), text=True, capture_output=True)
        self.assertEqual(json.loads(glob.stdout)["hookSpecificOutput"]["permissionDecision"], "deny")
        for index, pattern in enumerate(("src/*/../../../outside/**", "**/{../escape,src}/**")):
            with self.subTest(pattern=pattern):
                escaped = subprocess.run([sys.executable, str(BRIDGE), "hook", "--packet", str(p), "--cwd", str(self.repo)],
                                         input=json.dumps({"tool_name":"Glob", "tool_input":{"path":".", "pattern":pattern},
                                                           "tool_use_id":f"glob-late-traversal-{index}"}), text=True, capture_output=True)
                self.assertEqual(json.loads(escaped.stdout)["hookSpecificOutput"]["permissionDecision"], "deny")
        external = subprocess.run([sys.executable, str(BRIDGE), "hook", "--packet", str(p), "--cwd", str(self.repo)], input=json.dumps({"tool_name":"Glob", "tool_input":{"path":str(self.root), "pattern":"*.txt"}, "tool_use_id":"glob-external"}), text=True, capture_output=True)
        self.assertEqual(json.loads(external.stdout)["hookSpecificOutput"]["permissionDecision"], "deny")
        grep = subprocess.run([sys.executable, str(BRIDGE), "hook", "--packet", str(p), "--cwd", str(self.repo)], input=json.dumps({"tool_name":"Grep", "tool_input":{"pattern":"needle"}, "tool_use_id":"grep-default"}), text=True, capture_output=True)
        self.assertEqual(grep.stdout, "")

    def test_packet_rejects_noncanonical_owned_and_protected_paths(self):
        packet = self.packet("implement"); packet["owned_files"] = ["./owned.txt"]
        got, _ = self.invoke(packet, "dot-owned")
        self.assertEqual(got.returncode, 2)
        packet = self.packet("implement"); packet["protected_files"] = ["./protected.txt"]
        got, _ = self.invoke(packet, "dot-protected")
        self.assertEqual(got.returncode, 2)

    def test_active_lane_lock_rejects_second_writer_in_another_run_dir(self):
        os.environ["FAKE_MODE"] = "sleep"
        p = self.root / "lane.packet"; p.write_text(json.dumps(self.packet("implement")))
        first_dir = self.root / "lane-first"
        first = subprocess.Popen([sys.executable, str(BRIDGE), "run", "--packet", str(p), "--run-dir", str(first_dir), "--timeout", "8"], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        for _ in range(40):
            if (first_dir / "state.json").exists(): break
            time.sleep(.05)
        second, second_dir = self.invoke(self.packet("implement"), "lane-second", timeout="1")
        self.assertNotEqual(second.returncode, 0)
        self.assertIn("active run", (second_dir / "error.json").read_text())
        subprocess.run([sys.executable, str(BRIDGE), "cancel", "--run-dir", str(first_dir), "--reason", "finish test"], check=True, capture_output=True)
        first.wait(timeout=5); first.stdout.close(); first.stderr.close()

    def test_implement_out_of_scope_change_fails_without_reverting(self):
        os.environ["FAKE_MODE"] = "touch"
        got, run = self.invoke(self.packet("implement"), "scope")
        self.assertNotEqual(got.returncode, 0)
        self.assertTrue((self.repo / "outside.txt").exists())
        self.assertIn("out-of-scope", json.loads((run / "result.json").read_text())["scope_error"])

    def test_untracked_directory_is_listed_and_out_of_scope(self):
        os.environ["FAKE_MODE"] = "touchdir"
        got, run = self.invoke(self.packet("implement"), "scope-directory")
        self.assertNotEqual(got.returncode, 0)
        paths = {entry["path"] for entry in json.loads((run / "git_after.json").read_text())["status_entries"]}
        self.assertIn("nested/out.txt", paths)

    def test_descendant_inheriting_stdout_is_bounded_and_collects_result(self):
        os.environ["FAKE_MODE"] = "childstdout"
        started = time.monotonic()
        got, _ = self.invoke(self.packet(), "childstdout")
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertLess(time.monotonic() - started, 2)

    def test_ignored_protected_content_change_fails(self):
        (self.repo / '.gitignore').write_text('protected.txt\n')
        subprocess.run(['git', '-C', str(self.repo), 'add', '.gitignore'], check=True)
        subprocess.run(['git', '-C', str(self.repo), 'commit', '-qm', 'ignore fixture'], check=True)
        (self.repo / 'protected.txt').write_text('original')
        os.environ['FAKE_MODE'] = 'protectedignored'
        got, run = self.invoke(self.packet('implement'), 'protected-ignored')
        self.assertNotEqual(got.returncode, 0)
        self.assertIn('protected.txt', json.loads((run / 'result.json').read_text())['scope_error'])

    def test_stubborn_descendant_is_stopped_after_parent_exits(self):
        os.environ['FAKE_MODE'] = 'stubbornstdout'
        pidfile = self.root / 'child.pid'
        os.environ['CHILD_PID_FILE'] = str(pidfile)
        started = time.monotonic()
        try:
            got, run = self.invoke(self.packet(), 'stubborn-stdout', timeout='10')
            self.assertNotEqual(got.returncode, 0)
            self.assertLess(time.monotonic() - started, 10)
            self.assertTrue(pidfile.read_text().isdigit())
            child = json.loads((run / 'child.json').read_text())
            self.assertTrue(load_bridge_module().process_group_stopped(child['process_group']))
            self.assertIn('same-group descendants remained', json.loads((run / 'result.json').read_text())['process_group_error'])
        finally:
            os.environ.pop('CHILD_PID_FILE', None)


if __name__ == "__main__":
    unittest.main(verbosity=2)
