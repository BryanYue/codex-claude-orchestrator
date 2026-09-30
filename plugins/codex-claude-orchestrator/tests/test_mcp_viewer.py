"""Exercise the actual stdio MCP boundary and the loopback access boundary."""
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch, Mock
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from urllib.parse import urlsplit, parse_qs

from mcp import ClientSession
from mcp.client.stdio import stdio_client, StdioServerParameters

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import viewer
from viewer import Viewer, read_artifact


class CompactReportTests(unittest.TestCase):
    def test_failed_preview_labels_the_preserved_report_unaccepted(self):
        import server
        for status in ("failed", "cancelled", "timeout", "blocked", "unknown"):
            with self.subTest(status=status):
                value = server.compact_snapshot({"status": status, "result": {"structured": {"summary": "done"}}})
                self.assertIn(status, value["result_preview"]["summary"])
                self.assertIn("未验收", value["result_preview"]["summary"])
                self.assertFalse(value["result_preview"]["report_evidence"]["accepted"])
        value = server.compact_snapshot({"status": "reported", "result": {"structured": {"summary": "done"}}})
        self.assertEqual(value["result_preview"]["summary"], "done")


class ViewerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name)
        folder = self.folder
        class Records:
            def snapshot(self, run_id):
                if run_id != "run-fixture":
                    raise ValueError("Unknown run")
                return {"run_id": run_id, "run_dir": str(folder), "status": "reported"}
            def list_runs(self, limit=None, offset=0):
                values = [self.snapshot("run-fixture")]
                return values[offset:] if limit is None else values[offset:offset + limit]
            def events(self, run_id, after=0, limit=200):
                self.snapshot(run_id)
                return {"events": [], "next_cursor": after, "has_more": False}
        self.records = Records()
        self.view = Viewer(self.records).start()
        self.base = self.view.url().split("#")[0]
        self.headers = {"Authorization": "Bearer " + self.view.token}

    def tearDown(self):
        self.view.close()
        self.temp.cleanup()

    def test_capability_origin_host_and_read_only_boundary(self):
        with urlopen(self.base) as response:
            self.assertIn("text/html", response.headers["Content-Type"])
            self.assertIn("frame-ancestors 'none'", response.headers["Content-Security-Policy"])
            self.assertNotIn(self.view.token.encode(), response.read())
        for headers, expected in [({}, 401), ({**self.headers, "Origin": "https://evil.invalid"}, 403),
                                  ({**self.headers, "Host": "evil.invalid"}, 403)]:
            with self.assertRaises(HTTPError) as error:
                urlopen(Request(self.base + "api/runs", headers=headers))
            self.assertEqual(error.exception.code, expected)
        with urlopen(Request(self.base + "api/runs", headers=self.headers)) as response:
            self.assertEqual(json.load(response)["runs"][0]["run_id"], "run-fixture")
        with self.assertRaises(HTTPError) as error:
            urlopen(Request(self.base + "api/cancel", headers=self.headers, method="POST", data=b"{}"))
        self.assertEqual(error.exception.code, 405)

    def test_non_ascii_authorization_is_a_401_not_a_handler_crash(self):
        errors = []
        self.view.http.handle_error = lambda *args: errors.append(args)
        valid = "Bearer " + self.view.token
        cases = [({"Authorization": "Bearer töken"}, 401), ({"Authorization": valid + "é"}, 401),
                 ({"Authorization": "ÿ" + valid[1:]}, 401), ({}, 401), ({"Authorization": "Bearer wrong"}, 401),
                 ({"Authorization": "Bearer töken", "Host": "evil.invalid"}, 403),
                 ({"Authorization": "Bearer töken", "Origin": "https://evil.invalid"}, 403),
                 ({"Authorization": valid, "Origin": "https://evil.invalid"}, 403)]
        for headers, expected in cases:
            with self.subTest(headers=headers):
                with self.assertRaises(HTTPError) as error:
                    urlopen(Request(self.base + "api/runs", headers=headers), timeout=5)
                self.assertEqual(error.exception.code, expected)
                self.assertIn("error", json.load(error.exception))
        with urlopen(Request(self.base + "api/runs", headers={"Authorization": valid}), timeout=5) as response:
            self.assertEqual(json.load(response)["runs"][0]["run_id"], "run-fixture")
        self.assertEqual(errors, [], "no request may reach the server error handler")

    def test_unreadable_legacy_worker_lock_stays_unknown_through_the_endpoint(self):
        import cli_updates
        store = self.folder / "store"
        (store / "updates").mkdir(parents=True)
        (self.folder / "target.lock").write_text("")
        (store / "updates" / "worker.lock").symlink_to(self.folder / "target.lock")
        cli = self.folder / "claude"; cli.write_text("#!/bin/sh\nexit 0\n"); cli.chmod(0o755)
        env = {"CLAUDE_BIN": str(cli), "CLAUDE_ORCHESTRATOR_CLI_ROOT": str(store),
               "CLAUDE_ORCHESTRATOR_SETTINGS_PATH": str(self.folder / "settings.json")}
        with patch.object(viewer, "cli_updates", cli_updates), patch.dict(os.environ, env):
            with urlopen(Request(self.base + "api/cli-maintenance", headers=self.headers), timeout=5) as response:
                unknown = json.load(response)["legacy_managed_state"]
            (store / "updates" / "worker.lock").unlink()
            with urlopen(Request(self.base + "api/cli-maintenance", headers=self.headers), timeout=5) as response:
                absent = json.load(response)["legacy_managed_state"]
        self.assertIsNone(unknown["maintenance_worker_running"], "unknown must not be published as a stopped worker")
        self.assertEqual(unknown["maintenance_worker_lock"], "unreadable")
        self.assertIs(absent["maintenance_worker_running"], False)
        self.assertEqual(absent["maintenance_worker_lock"], "absent")

    def test_artifact_whitelist_partial_json_and_symlink(self):
        (self.folder / "result.json").write_text('{"incomplete":')
        self.assertFalse(read_artifact(self.records, "run-fixture", "result")["available"])
        with self.assertRaises(ValueError):
            read_artifact(self.records, "run-fixture", "../../secret")
        decisions = [{"decision": "returned", "reason": "missing evidence"}, {"decision": "accepted", "reason": "evidence checked"}]
        (self.folder / "decision-history.json").write_text(json.dumps(decisions))
        self.assertEqual(read_artifact(self.records, "run-fixture", "decision_history")["content"], decisions)
        (self.folder / "packet.json").symlink_to(self.folder / "result.json")
        with self.assertRaises(ValueError):
            read_artifact(self.records, "run-fixture", "packet")

    def test_cli_maintenance_endpoint_is_read_only_and_omits_private_fields(self):
        calls = []

        class Updates:
            @staticmethod
            def status():
                calls.append("status")
                return {
                    "policy": "user_local_cli", "version_management": "retired", "state": "local_cli",
                    "message": "使用本机 Claude CLI", "reason": "user_local_cli", "next_action": None,
                    "notice_id": None, "notice_pending": False, "current_version": None,
                    "local_cli": {"path": "/fixture/claude", "source": "CLAUDE_BIN", "candidate": "must-not-leak"},
                    "private_token": "must-not-leak",
                    "legacy_managed_state": {"present": True, "root": "must-not-leak",
                                             "versions": [{"id": "must-not-leak"}], "maintenance_worker_running": False},
                }

        with patch.object(viewer, "cli_updates", Updates):
            request = Request(self.base + "api/cli-maintenance", headers=self.headers)
            with urlopen(request) as response:
                payload = json.load(response)
        self.assertEqual(calls, ["status"])
        self.assertEqual(payload["policy"], "user_local_cli")
        self.assertEqual(payload["local_cli"], {"path": "/fixture/claude", "source": "CLAUDE_BIN"})
        self.assertEqual(payload["legacy_managed_state"],
                         {"present": True, "ignored_for_dispatch": True, "retained_versions": 1,
                          "maintenance_worker_running": False})
        self.assertNotIn("private_token", payload)
        self.assertNotIn("must-not-leak", json.dumps(payload))

    def test_artifact_reports_display_truncation_and_writing_state(self):
        (self.folder / "result.json").write_text(json.dumps({"summary": "x" * 400_000}))
        truncated = read_artifact(self.records, "run-fixture", "result")
        self.assertTrue(truncated["available"])
        self.assertTrue(truncated["truncated"])
        self.assertEqual(truncated["state"], "available")
        self.assertGreater(truncated["size_bytes"], truncated["display_limit_bytes"])
        (self.folder / "packet.json").write_text('{"partial":')
        writing = read_artifact(self.records, "run-fixture", "packet")
        self.assertEqual((writing["available"], writing["state"]), (False, "writing"))
        self.assertEqual(read_artifact(self.records, "run-fixture", "receipt")["state"], "missing")


class MCPTests(unittest.IsolatedAsyncioTestCase):
    async def test_slow_maintenance_read_does_not_block_cancel_dispatch(self):
        import server
        import threading
        entered, release, cancelled = threading.Event(), threading.Event(), threading.Event()
        def slow_status():
            entered.set()
            release.wait(3)
            return {"state": "up_to_date"}
        rt = Mock()
        rt.snapshot.return_value = {"run_id": "one", "status": "running"}
        def cancel(*_):
            cancelled.set()
            return {"run_id": "one", "status": "cancelled"}
        rt.cancel.side_effect = cancel
        with patch.object(server, "runtime", rt), patch.object(server, "maintenance_status", slow_status), \
             patch.object(server, "viewer", Mock(url=lambda *_: "http://127.0.0.1/fixture")):
            status = asyncio.create_task(server.claude_status("one"))
            stop = None
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 1))
                stop = asyncio.create_task(server.claude_cancel("one", "user requested stop"))
                self.assertTrue(await asyncio.to_thread(cancelled.wait, 1), "maintenance blocked cancellation")
                self.assertFalse(release.is_set())
            finally:
                release.set()
                await status
                if stop:
                    await stop

    async def test_inspection_alias_and_display_failure_never_duplicate_dispatch(self):
        import server
        from runtime import Runtime
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            repo = root / "repo"; repo.mkdir()
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            alias = root / "alias"; alias.symlink_to(repo, target_is_directory=True)
            rt = Mock(spec=Runtime)
            rt.start.return_value = {"run_id": "specific-run", "status": "starting", "claude_started": False}
            maintenance = {"policy": "user_local_cli"}
            broken_view = Mock(); broken_view.url.side_effect = RuntimeError("viewer unavailable")
            with patch.object(server, "runtime", rt), patch.object(server, "viewer", broken_view), \
                 patch.object(server.cli_updates, "status", return_value=maintenance):
                inspected = await server.claude_workflow_context(str(alias))
                self.assertEqual(inspected["check_status"], "checked")
                self.assertEqual(inspected["resolved_cwd"], str(repo))
                self.assertFalse(inspected["claude_started"])
                rt.start.assert_not_called()
                created = await server.claude_start({"cwd": str(alias)})
                self.assertEqual(rt.start.call_count, 1)
                self.assertEqual(rt.start.call_args.args[0]["cwd"], str(repo))
                self.assertTrue(created["task_created"])
                self.assertFalse(created["claude_started"])
                self.assertIsNone(created["details_url"])
                self.assertEqual(created["run_id"], "specific-run")
                self.assertEqual(created["presentation"]["state"], "unavailable")
                rt.snapshot.return_value = rt.start.return_value
                broken_view.url.side_effect = None
                broken_view.url.return_value = "http://127.0.0.1:1234/#token=fixture&run=specific-run"
                restored = await server.claude_details("specific-run", compact=True)
                self.assertEqual(restored["presentation"]["state"], "available")
                self.assertEqual(restored["presentation"]["host_open_state"], "unobserved")
                self.assertEqual(restored["run_id"], "specific-run")
                self.assertEqual(rt.start.call_count, 1)
                broken = await server.claude_workflow_context(str(root / "missing"))
                self.assertIsNone(broken["adopted"])
                self.assertEqual(broken["check_status"], "check_failed")
                with self.assertRaises(Exception):
                    await server.claude_start({"cwd": str(root / "missing")})
                self.assertEqual(rt.start.call_count, 1)

    async def test_adopted_start_rejects_malformed_requirement_sources_before_runtime(self):
        import server
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            repo = root / "repo"; repo.mkdir()
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            adopted = await server.claude_workflow_enable(str(repo))
            self.assertTrue(adopted["ready"])
            spec = root / "spec.md"; spec.write_text("spec\n")
            rt = Mock()
            rt.start.return_value = {"run_id": "adopted-run", "status": "starting", "claude_started": False}
            with patch.object(server, "runtime", rt), \
                 patch.object(server, "viewer", Mock(url=lambda *_: "http://127.0.0.1/fixture")), \
                 patch.object(server.cli_updates, "status", return_value={"policy": "user_local_cli"}):
                for sources in (None, str(spec), {"path": str(spec)}, [str(spec), 3]):
                    with self.subTest(requirement_sources=sources):
                        with self.assertRaisesRegex(server.ToolError, "requirement_sources must be an array of strings"):
                            await server.claude_start({"cwd": str(repo), "requirement_sources": sources})
                rt.start.assert_not_called()
                await server.claude_start({"cwd": str(repo), "requirement_sources": [str(spec), adopted["protocol_path"], str(spec)]})
                sent = rt.start.call_args.args[0]
                self.assertEqual(sent["requirement_sources"], [str(spec), adopted["protocol_path"], adopted["config_path"]])
                self.assertEqual(sent["project_workflow"]["protocol_sha256"], adopted["protocol_sha256"])
                await server.claude_start({"cwd": str(repo)})
                self.assertEqual(rt.start.call_args.args[0]["requirement_sources"], [adopted["config_path"], adopted["protocol_path"]])
            self.assertEqual(rt.start.call_count, 2)

    async def test_dispatch_uses_local_cli_and_ignores_legacy_managed_state(self):
        import server
        from runtime import Runtime
        from bridge import create_cli_descriptor
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            cli = folder / "claude"
            cli.write_text("#!/bin/sh\nexit 0\n")
            cli.chmod(0o755)
            store = folder / "store"; store.mkdir()
            (store / "selection.json").write_text(json.dumps({
                "schema_version": 1, "generation": 1, "active": None, "previous": None, "history": [], "mode": "managed"}))
            (store / "update-policy.json").write_text(json.dumps({
                "schema_version": 1, "generation": 1, "mode": "automatic", "reason": "default", "updated_at": 1.0}))
            env = {"CLAUDE_BIN": str(cli), "CLAUDE_ORCHESTRATOR_CLI_ROOT": str(store),
                   "CLAUDE_ORCHESTRATOR_SETTINGS_PATH": str(folder / "settings.json")}
            packet = {"workspace_kind": "artifacts", "role": "review"}
            observed = []
            def dispatch(value, **kwargs):
                observed.append(create_cli_descriptor(value))
                return {"run_id": "fixture", "status": "starting"}
            fake_runtime = Mock(spec=Runtime)
            fake_runtime.start.side_effect = dispatch
            with patch.dict(os.environ, env), patch.object(server, "runtime", fake_runtime), \
                 patch.object(server, "viewer", Mock(url=lambda *args: "http://127.0.0.1/fixture")), \
                 patch.object(server.cli_validation, "start", side_effect=AssertionError("dispatch must not qualify")):
                created = await server.claude_start(packet)
                self.assertEqual(created["run_id"], "fixture")
                self.assertEqual(observed[0]["selection"]["path"], str(cli))
                self.assertEqual(observed[0]["selection"]["source"], "CLAUDE_BIN")
                self.assertNotIn("identity_id", observed[0])
                self.assertEqual(created["cli_maintenance"]["policy"], "user_local_cli")
                self.assertTrue(created["cli_maintenance"]["legacy_managed_state"]["present"])
                await server.claude_start(packet, resume_run_id="old-pinned-run")
                self.assertEqual(fake_runtime.start.call_args.kwargs["resume_run_id"], "old-pinned-run")
            self.assertEqual(sorted(path.name for path in store.iterdir()), ["selection.json", "update-policy.json"],
                             "dispatch must not create maintenance state, downloads or retained copies")

    async def test_cli_status_is_local_and_every_update_action_is_side_effect_free(self):
        import diagnostics
        import server
        import cli_store
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"; repo.mkdir()
            local = root / "claude"; local.write_text("#!/bin/sh\nexit 0\n"); local.chmod(0o755)
            environment = {
                "ready": True, "status": "local_checks_passed",
                "cli": {"path": str(local), "source": "CLAUDE_BIN", "version": "2.1.284"},
                "installation": {"installed": True, "version": "2.1.284"},
                "auth": {"status": "reported_logged_in"}, "probe": {"status": "not_requested"},
                "compatibility": {"status": "advertised", "behavior_verified": False, "missing_flags": []},
            }
            with patch.object(diagnostics, "codex_cli", return_value=None), \
                 patch.object(diagnostics.bridge, "check_environment", return_value=environment), \
                 patch.object(diagnostics, "locate_claude", return_value={"path": str(local), "source": "CLAUDE_BIN"}), \
                 patch.object(diagnostics.shutil, "which", return_value="/fixture/uv"):
                report = diagnostics.collect(str(repo))
            self.assertEqual(report["cli_management"]["active"]["path"], str(local))
            self.assertEqual(report["cli_management"]["version_management"], "retired")
            self.assertEqual(report["claude"]["version_policy"], "diagnostic_only")
            self.assertNotIn("system", report["cli_management"])
            forbidden = AssertionError("retired version management must not run")
            with patch.object(cli_store, "prepare", side_effect=forbidden), \
                 patch.object(cli_store, "capture", side_effect=forbidden), \
                 patch.object(cli_store, "activate_explicit", side_effect=forbidden), \
                 patch.object(cli_store, "rollback_explicit", side_effect=forbidden), \
                 patch.object(cli_store, "set_update_policy", side_effect=forbidden), \
                 patch.object(server.cli_validation, "start", side_effect=forbidden):
                for action in ("prepare", "validate", "activate", "rollback", "refresh", "policy", "acknowledge"):
                    with self.subTest(action=action):
                        result = await server.claude_cli_update(action=action, policy="automatic", channel="latest",
                                                                auto_qualify=True, identity_id="legacy", notice_id="n")
                        self.assertTrue(result["retired"])
                        self.assertFalse(result["changed"])
            with self.assertRaises(Exception):
                await server.claude_cli_update(action="cancel")
            with self.assertRaisesRegex(Exception, "identity_id is retired"):
                await server.claude_models(str(repo), identity_id="darwin-arm64-2.1.278-deadbeef")

    async def test_decorated_start_survives_local_status_failure_and_keeps_run_id(self):
        import server
        rt = Mock()
        rt.start.return_value = {"run_id": "created-run", "status": "starting", "claude_started": False}
        with tempfile.TemporaryDirectory() as tmp:
            packet = {"workspace_kind": "artifacts", "cwd": tmp}
            with patch.object(server, "runtime", rt), \
                 patch.object(server, "viewer", Mock(url=lambda *_: "http://127.0.0.1/fixture")), \
                 patch.object(server.cli_updates, "status", side_effect=subprocess.TimeoutExpired(["sysctl"], 5)):
                created = await server.claude_start(packet)
        self.assertEqual(rt.start.call_count, 1)
        self.assertEqual(created["run_id"], "created-run")
        self.assertEqual(created["cli_maintenance"]["state"], "unavailable")
        self.assertIn("TimeoutExpired", created["cli_maintenance"]["error"])

    async def test_actual_stdio_tools_doctor_run_events_result_and_decision(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"; repo.mkdir()
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            requirement = root / "requirements.md"; requirement.write_text("Read-only fixture.\n")
            fake = root / "claude-fixture"
            fake.write_text('''#!/usr/bin/env python3
import json,sys,time,os,subprocess
from pathlib import Path
a=sys.argv[1:]
if a==['--version']: print('2.1.276'); raise SystemExit
if a==['--help']: print('-p --model --effort --output-format --verbose --json-schema --session-id --resume --permission-mode --tools --allowedTools --disallowedTools --settings --strict-mcp-config --mcp-config --disable-slash-commands --no-session-persistence'); raise SystemExit
if a==['auth','status','--json']: print(json.dumps({'loggedIn':True,'authMethod':'fixture'})); raise SystemExit
if '--input-format' in a:
 r=json.loads(sys.stdin.read())
 print(json.dumps({'type':'control_response','response':{'request_id':r['request_id'],'subtype':'success','response':{'models':[{'value':'future-family','resolvedModel':'claude-future-99'}],'account':{'secret':'PRIVATE_ACCOUNT'}}}})); raise SystemExit
s=a[a.index('--session-id')+1]
settings_path=Path(a[a.index('--settings')+1])
packet=json.loads((settings_path.parent/'packet.json').read_text())
target=packet['requirement_sources'][0]
tool_input={'file_path':target}
hook_event={'hook_event_name':'PreToolUse','session_id':s,'cwd':os.getcwd(),'tool_name':'Read','tool_input':tool_input,'tool_use_id':'read-1'}
settings=json.loads(settings_path.read_text())
hook_command=settings['hooks']['PreToolUse'][0]['hooks'][0]['command']
guard=subprocess.run(hook_command,shell=True,input=json.dumps(hook_event),text=True,capture_output=True)
assert guard.returncode==0,guard.stderr+guard.stdout
if guard.stdout.strip():
 assert json.loads(guard.stdout).get('hookSpecificOutput',{}).get('permissionDecision')!='deny',guard.stdout
print(json.dumps({'type':'system','subtype':'init','session_id':s,'model':'fixture-init-model'}),flush=True)
print(json.dumps({'type':'assistant','message':{'model':'fixture-model','content':[{'type':'thinking','thinking':'PRIVATE_THINKING_SENTINEL'},{'type':'tool_use','name':'Read','input':tool_input,'id':'read-1'}]}}),flush=True)
print(json.dumps({'type':'assistant','message':{'model':'fixture-model','content':[{'type':'tool_use','name':'StructuredOutput','input':{'status':'completed','summary':'fixture result','evidence':['fixture'],'checks':[],'unresolved':[]},'id':'format-1'}]}}),flush=True)
time.sleep(.4)
print(json.dumps({'type':'result','subtype':'success','session_id':s,'structured_output':{'status':'completed','summary':'fixture result','evidence':['fixture'], 'checks':[], 'unresolved':[]}}),flush=True)
''')
            fake.chmod(0o755)
            config = json.loads((ROOT / ".mcp.json").read_text())["mcpServers"]["claude-orchestrator"]
            launch_cwd = (ROOT / config["cwd"]).resolve()
            params = StdioServerParameters(command=str(launch_cwd / config["command"]), args=config["args"], cwd=launch_cwd,
                env={**os.environ, "CLAUDE_BIN": str(fake), "CLAUDE_ORCHESTRATOR_STATE_DIR": str(root / "state"),
                     "CLAUDE_ORCHESTRATOR_CLI_ROOT": str(root / "cli-store"),
                     "UV_CACHE_DIR": str(root / "uv-cache"),
                     "CLAUDE_ORCHESTRATOR_ENV_DIR": sys.prefix})
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as client:
                    await client.initialize()
                    listed = (await client.list_tools()).tools
                    names = {tool.name for tool in listed}
                    for tool in listed:
                        if tool.name in {"claude_environment", "claude_diagnostics", "claude_recovery", "claude_cli_status"}:
                            self.assertTrue(tool.annotations.read_only_hint)
                    self.assertEqual(names, {"claude_content_status", "claude_content_check", "claude_content_read", "claude_content_review", "claude_content_switch", "claude_models", "claude_environment", "claude_diagnostics", "claude_recovery", "claude_reconcile", "claude_saved_workflows", "claude_route", "claude_routing_policy", "claude_routing_set", "claude_workflow_context", "claude_workflow_enable", "claude_workflow_disable", "claude_doctor", "claude_cli_status", "claude_cli_update", "claude_start", "claude_status", "claude_runs", "claude_wait", "claude_events", "claude_details", "claude_result", "claude_cancel", "claude_decide"})
                    async def call(name, args):
                        result = await client.call_tool(name, args)
                        self.assertFalse(result.is_error, str(result.content))
                        return result.structured_content or json.loads(result.content[0].text)
                    doctor = await call("claude_environment", {"cwd": str(repo)})
                    self.assertTrue(doctor["ready"])
                    self.assertEqual(doctor["cli_maintenance"]["policy"], "user_local_cli")
                    models = await call("claude_models", {"cwd": str(repo)})
                    self.assertEqual(models["models"][0]["resolvedModel"], "claude-future-99")
                    self.assertNotIn("PRIVATE_ACCOUNT", json.dumps(models))
                    self.assertFalse(models["model_request_sent"])
                    retired_identity = await client.call_tool("claude_models", {"cwd": str(repo), "identity_id": "darwin-arm64-2.1.278-deadbeef"})
                    self.assertTrue(retired_identity.is_error)
                    for update in ({"action": "policy", "policy": "automatic", "channel": "latest", "auto_qualify": True},
                                   {"action": "refresh"}, {"action": "acknowledge"},
                                   {"action": "prepare", "candidate_path": "relative-claude"}, {"action": "validate"}):
                        retired = await call("claude_cli_update", update)
                        self.assertTrue(retired["retired"])
                        self.assertFalse(retired["changed"])
                    self.assertFalse((root / "cli-store").exists(), "retired update actions must not create CLI state")
                    cli_status = await call("claude_cli_status", {"cwd": str(repo)})
                    self.assertEqual(cli_status["cli_management"]["active"]["path"], str(fake))
                    self.assertTrue(cli_status["cli_management"]["active"]["dispatch_ready"])
                    self.assertEqual(cli_status["cli_management"]["dimensions"]["recent_real_provider_call"]["status"], "not_recorded")
                    invalid_update = await client.call_tool("claude_cli_update", {"action": "unknown"})
                    self.assertTrue(invalid_update.is_error)
                    missing_cancel = await client.call_tool("claude_cli_update", {"action": "cancel"})
                    self.assertTrue(missing_cancel.is_error)
                    context = await call("claude_workflow_context", {"cwd": str(repo)})
                    self.assertFalse(context["adopted"])
                    enabled = await call("claude_workflow_enable", {"cwd": str(repo)})
                    self.assertTrue(enabled["ready"])
                    await call("claude_routing_set", {"cwd":str(repo),"mode":"manual"})
                    self.assertEqual((await call("claude_route", {"cwd":str(repo),"task_kind":"review"}))["executor"], "codex")
                    self.assertEqual((await call("claude_route", {"cwd":str(repo),"task_kind":"review","explicit_executor":"claude"}))["executor"], "claude")
                    packet = {"task_id":"mcp-fixture", "revision":1, "role":"review", "cwd":str(repo), "objective":"Read-only fixture",
                        "requirement_sources":[str(requirement)], "constraints":["No edits"], "acceptance":["Return evidence"],
                        "owned_files":[], "protected_files":[], "model":"sonnet", "effort":"low"}
                    start = await call("claude_start", {"packet": packet})
                    self.assertEqual(start["cli_maintenance"]["policy"], "user_local_cli")
                    run_id = start["run_id"]
                    cursor = 0; all_events = []; final = None
                    for _ in range(20):
                        result = await call("claude_wait", {"run_id":run_id, "after":cursor, "timeout_seconds":2})
                        all_events.extend(result["events"])
                        self.assertGreaterEqual(result["next_cursor"], cursor)
                        cursor = result["next_cursor"]
                        final = result["snapshot"]
                        if final["status"] in {"reported", "failed", "blocked", "unknown"}:
                            break
                    self.assertEqual(final["status"], "reported", final)
                    self.assertEqual(final["requested_model"], "sonnet")
                    self.assertEqual(final["initialized_model"], "fixture-init-model")
                    self.assertEqual(final["actual_models"], ["fixture-model"])
                    self.assertEqual(final["actual_model"], "fixture-model")
                    self.assertEqual(final["actual_model_source"], "assistant_message")
                    self.assertTrue(final["provider_response_observed"])
                    self.assertTrue(all_events)
                    self.assertNotIn("PRIVATE_THINKING_SENTINEL", json.dumps(all_events))
                    self.assertTrue((await call("claude_result", {"run_id":run_id}))["available"])
                    after_run_cli = await call("claude_cli_status", {"cwd": str(repo)})
                    recent_call = after_run_cli["cli_management"]["dimensions"]["recent_real_provider_call"]
                    self.assertEqual(recent_call["status"], "recorded")
                    self.assertNotIn("run_id", recent_call)
                    self.assertNotIn("session_id", recent_call)
                    self.assertEqual(recent_call["cli_version"], "2.1.276")
                    missing_job = await call("claude_cli_status", {"cwd": str(repo), "job_id": "qualification-aaaaaaaaaaaaaaaa"})
                    self.assertTrue(missing_job["cli_management"]["active"]["dispatch_ready"])
                    self.assertEqual(missing_job["cli_management"]["validation"]["status"], "unavailable")
                    recorded_packet = await call("claude_result", {"run_id":run_id, "artifact":"packet"})
                    self.assertIn("project_workflow", recorded_packet["content"])
                    details = await call("claude_details", {"run_id":run_id})
                    link = urlsplit(details["details_url"])
                    token = parse_qs(link.fragment)["token"][0]
                    req = Request(f"{link.scheme}://{link.netloc}/api/snapshot?run_id={run_id}", headers={"Authorization": "Bearer " + token})
                    with urlopen(req) as response:
                        self.assertEqual(json.load(response)["status"], "reported")
                    accepted = await call("claude_decide", {"run_id":run_id,"decision":"accepted","reason":"Test verified fixture outputs","evidence":["stdio fixture assertions"]})
                    self.assertEqual(accepted["decision"]["decision"], "accepted")
                    self.assertTrue((await call("claude_result", {"run_id":run_id,"artifact":"decision"}))["available"])
                    compact = await call("claude_status", {"run_id":run_id,"compact":True})
                    self.assertNotIn("result", compact)
                    self.assertNotIn("decision_history", compact)
                    self.assertTrue(compact["result_preview"]["available"])
                    self.assertIn("result", await call("claude_status", {"run_id":run_id}))
                    returned = await call("claude_decide", {"run_id":run_id,"decision":"returned","reason":"fixture correction independently checked","evidence":["stdio fixture assertions"],"resolution":"completed_by_codex","completion_summary":"Coordinator completed fixture checks","compact":True})
                    self.assertEqual(returned["decision"]["decision"], "returned")
                    self.assertEqual(returned["decision"]["resolution"], "completed_by_codex")
                    self.assertNotIn("result", returned)
                    self.assertIn("Codex 已补齐", returned["user_summary"])
                    delta = await call("claude_wait", {"run_id":run_id,"after":0,"timeout_seconds":0,"compact":True})
                    raw = await call("claude_events", {"run_id":run_id,"after":0,"limit":100})
                    self.assertEqual(delta["next_cursor"], raw["next_cursor"])
                    self.assertEqual(len(delta["events"])+delta["provider_events_omitted"], len(raw["events"]))
                    self.assertNotIn("result", delta["snapshot"])
                    page = await call("claude_runs", {"limit": 1, "offset": 0})
                    self.assertEqual(len(page["runs"]), 1)
                    self.assertFalse(page["has_more"])
                    self.assertEqual((await call("claude_runs", {"offset":1}))["runs"], [])
                    documents = root / "documents"; documents.mkdir()
                    doc = documents / "draft.md"; doc.write_text("The fixture draft.\n")
                    artifact = {**packet, "task_id":"artifact-mcp", "cwd":str(documents),
                                "workspace_kind":"artifacts", "input_files":[str(doc)]}
                    wrong_kind = {key: value for key, value in artifact.items() if key not in {"workspace_kind", "input_files"}}
                    rejected_kind = await client.call_tool("claude_start", {"packet": wrong_kind})
                    self.assertTrue(rejected_kind.is_error)
                    self.assertIn("workspace_kind=artifacts", str(rejected_kind.content))
                    self.assertNotIn("Project workflow needs reconciliation", str(rejected_kind.content))
                    started_doc = await call("claude_start", {"packet":artifact})
                    doc_cursor = 0
                    for _ in range(20):
                        update = await call("claude_wait", {"run_id":started_doc["run_id"], "after":doc_cursor, "timeout_seconds":2})
                        doc_cursor = update["next_cursor"]
                        if update["snapshot"]["status"] in {"reported","failed","blocked","unknown"}: break
                    self.assertEqual(update["snapshot"]["status"], "reported", update)
                    manifest = await call("claude_result", {"run_id":started_doc["run_id"], "artifact":"workspace_after"})
                    self.assertTrue(manifest["available"])
                    page = await call("claude_runs", {"limit":1})
                    self.assertTrue(page["has_more"])
                    self.assertEqual(page["next_offset"], 1)
                    protocol = Path(enabled["protocol_path"])
                    protocol.write_text(protocol.read_text() + "\nchanged without reconciliation\n")
                    rejected = await client.call_tool("claude_start", {"packet":{**packet,"revision":2}})
                    self.assertTrue(rejected.is_error)
                    protocol.write_text(protocol.read_text().removesuffix("\nchanged without reconciliation\n"))
                    disabled = await call("claude_workflow_disable", {"cwd":str(repo)})
                    self.assertFalse(disabled["adopted"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
