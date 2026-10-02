"""Focused regressions for bridge identity, lifecycle, blocked state, and cost evidence."""
import argparse
import importlib.util
import json
import os
import signal
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SCRIPTS = Path(__file__).resolve().parents[1]
BRIDGE = SCRIPTS / "bridge.py"


def load_bridge_module():
    scripts = str(SCRIPTS)
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    spec = importlib.util.spec_from_file_location("bridge_stability_unit", BRIDGE)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class BridgeStabilityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.enterContext(mock.patch.dict(os.environ, {"CLAUDE_ORCHESTRATOR_CLI_ROOT": str(self.root / "isolated-cli-store")}))
        self.repo = self.root / "repo"
        self.repo.mkdir()
        for args in (("init", "-q"), ("config", "user.email", "fixture@example.invalid"),
                     ("config", "user.name", "Fixture")):
            subprocess.run(["git", "-C", str(self.repo), *args], check=True)
        (self.repo / "base.txt").write_text("base\n")
        subprocess.run(["git", "-C", str(self.repo), "add", "base.txt"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-qm", "fixture"], check=True)
        self.requirement = self.root / "requirements.md"
        self.requirement.write_text("return evidence\n")
        self.fake = self.root / "fake_claude.py"
        self.fake.write_text("""#!/usr/bin/env python3
import json, math, os, sys
args=sys.argv[1:]
if args == ['--version']:
    print('2.1.276 (Claude Code)'); raise SystemExit(0)
if args == ['--help']:
    flags='-p --model --effort --output-format --verbose --json-schema --session-id --resume --permission-mode --tools --allowedTools --disallowedTools --settings --strict-mcp-config --mcp-config --disable-slash-commands --no-session-persistence --max-turns --max-budget-usd'.split()
    print(' '.join(flag for flag in flags if flag != os.environ.get('STABILITY_OMIT_FLAG'))); raise SystemExit(0)
if args == ['auth','status','--json']:
    print(json.dumps({'loggedIn':True})); raise SystemExit(0)
sys.stdin.buffer.read()
session=args[args.index('--session-id')+1]
print(json.dumps({'type':'system','subtype':'init','session_id':session,'model':'test-model'}))
cost=os.environ.get('STABILITY_COST')
if cost == 'nan': cost=float('nan')
elif cost is not None: cost=float(cost)
result={'type':'result','subtype':'success','session_id':session,
        'structured_output':{'status':os.environ.get('STABILITY_STATUS','completed'),'summary':'fixture summary','evidence':['base.txt'],'checks':['fixture'], 'unresolved':['need more detail']}}
if cost is not None: result['total_cost_usd']=cost
print(json.dumps(result))
""")
        self.fake.chmod(0o755)
        self.old_bin = os.environ.get("CLAUDE_BIN")
        os.environ["CLAUDE_BIN"] = str(self.fake)
        for name in ("STABILITY_OMIT_FLAG", "STABILITY_COST", "STABILITY_STATUS", "CODEX_BRIDGE_LIFECYCLE_FILE"):
            os.environ.pop(name, None)

    def tearDown(self):
        if self.old_bin is None:
            os.environ.pop("CLAUDE_BIN", None)
        else:
            os.environ["CLAUDE_BIN"] = self.old_bin
        for name in ("STABILITY_OMIT_FLAG", "STABILITY_COST", "STABILITY_STATUS", "CODEX_BRIDGE_LIFECYCLE_FILE"):
            os.environ.pop(name, None)
        self.tmp.cleanup()

    def packet(self, role="implement", owned=None, protected=None):
        return {"task_id":"stability", "revision":1, "role":role, "cwd":str(self.repo), "objective":"check fixture",
                "requirement_sources":[str(self.requirement)], "constraints":["read only"], "acceptance":["structured"],
                "owned_files":["owned.txt"] if owned is None and role == "implement" else (owned or []),
                "protected_files":["protected.txt"] if protected is None else protected,
                "model":"test-model", "effort":"low"}

    def invoke(self, packet, name, *, lifecycle=None):
        packet_path = self.root / f"{name}.json"
        packet_path.write_text(json.dumps(packet))
        run = self.root / name
        env = dict(os.environ)
        if lifecycle is not None:
            env["CODEX_BRIDGE_LIFECYCLE_FILE"] = str(lifecycle)
        result = subprocess.run([sys.executable, str(BRIDGE), "run", "--packet", str(packet_path), "--run-dir", str(run), "--timeout", "3"],
                                text=True, capture_output=True, env=env)
        return result, run

    def test_shutdown_signals_at_preflight_launch_and_collection_keep_cleanup_evidence(self):
        module = load_bridge_module()
        original_popen = subprocess.Popen
        original_check = module.check_environment
        original_cleanup = module.terminate_group
        original_scope = module.scope_violations
        lane = module.lane_identity(self.repo)
        for phase in ("preflight", "launch", "collection", "unconfirmed"):
            with self.subTest(phase=phase):
                run = self.root / ("shutdown-" + phase)
                packet_path = self.root / ("shutdown-" + phase + ".json")
                packet_path.write_text(json.dumps(self.packet()))
                args = argparse.Namespace(packet=str(packet_path), run_dir=str(run), timeout=3, resume_from=None)
                children = []
                saved = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGHUP)}

                def checked(*a, **kw):
                    answer = original_check(*a, **kw)
                    if phase == "preflight":
                        signal.raise_signal(signal.SIGTERM)
                    return answer

                def popen(command, *a, **kw):
                    proc = original_popen(command, *a, **kw)
                    if "--json-schema" in command:
                        children.append(proc)
                        if phase in {"launch", "unconfirmed"}:
                            # A live child exists but _run has not assigned proc.
                            signal.raise_signal(signal.SIGTERM)
                    return proc

                def cleanup(proc):
                    signal.raise_signal(signal.SIGHUP)
                    signal.raise_signal(signal.SIGTERM)
                    error = original_cleanup(proc)
                    self.assertIsNone(error)
                    return "injected unconfirmed group stop" if phase == "unconfirmed" else None

                def scope(*a, **kw):
                    answer = original_scope(*a, **kw)
                    if phase == "collection":
                        signal.raise_signal(signal.SIGTERM)
                    return answer

                try:
                    with mock.patch.object(module, "check_environment", side_effect=checked), \
                         mock.patch.object(module.subprocess, "Popen", new=popen), \
                         mock.patch.object(module, "terminate_group", side_effect=cleanup), \
                         mock.patch.object(module, "scope_violations", side_effect=scope):
                        module.run(args)
                    expected = "unknown" if phase == "unconfirmed" else "cancelled"
                    self.assertEqual(json.loads((run / "receipt.json").read_text())["status"], expected)
                    self.assertEqual(json.loads((run / "state.json").read_text())["status"], expected)
                    self.assertEqual(bool(children), phase != "preflight")
                    for proc in children:
                        self.assertIsNotNone(proc.poll())
                        self.assertTrue(module.process_group_stopped(proc.pid))
                    self.assertEqual(bool(module.unknown_markers(lane)), phase == "unconfirmed")
                    for sig, handler in saved.items():
                        self.assertEqual(signal.getsignal(sig), handler)
                finally:
                    for proc in children:
                        if proc.poll() is None:
                            original_cleanup(proc)
                    for marker, value in module.unknown_markers(lane):
                        if value.get("run_id") == run.name:
                            marker.unlink(missing_ok=True)

    def test_initialization_write_failure_keeps_packet_and_failed_receipt(self):
        module = load_bridge_module()
        packet_path = self.root / "initialization.json"
        packet_path.write_text(json.dumps(self.packet()))
        run = self.root / "initialization-run"
        args = argparse.Namespace(packet=str(packet_path), run_dir=str(run), timeout=3, resume_from=None)
        original_dump = module.dump

        def dump(path, value):
            if path.name == "cli-selection.json":
                raise PermissionError("injected selection write failure")
            return original_dump(path, value)

        with mock.patch.object(module, "dump", side_effect=dump):
            self.assertEqual(module.run(args), 1)
        self.assertTrue((run / "packet.json").is_file())
        self.assertFalse((run / "child.json").exists())
        self.assertEqual(json.loads((run / "state.json").read_text())["status"], "failed")
        self.assertEqual(json.loads((run / "receipt.json").read_text())["status"], "failed")
        self.assertIn("injected selection write failure", (run / "error.json").read_text())

    def test_protected_aliases_and_reserved_control_directories_are_rejected(self):
        aliases = [("case", ["PrOtEcTeD.TxT"]),
                   ("unicode", ["cafe\u0301.txt"])]
        (self.repo / "protected.txt").write_text("protected\n")
        (self.repo / "caf\u00e9.txt").write_text("protected\n")
        for name, owned in aliases:
            protected = ["protected.txt"] if name == "case" else ["caf\u00e9.txt"]
            got, _ = self.invoke(self.packet(owned=owned, protected=protected), f"alias-{name}")
            self.assertEqual(got.returncode, 2, got.stderr)
            self.assertRegex(got.stderr, r"(overlaps protected_files|aliases)")
        (self.repo / "hardlink-protected.txt").write_text("protected\n")
        os.link(self.repo / "hardlink-protected.txt", self.repo / "hardlink-owned.txt")
        got, _ = self.invoke(self.packet(owned=["hardlink-owned.txt"], protected=["hardlink-protected.txt"]), "alias-hardlink")
        self.assertEqual(got.returncode, 2, got.stderr)
        self.assertIn("aliases", got.stderr)
        for name, owned in (("git", [".GiT/config"]), ("claude", [".ClAuDe/config.json"]), ("codex", [".CoDeX/config.json"])):
            got, _ = self.invoke(self.packet(owned=owned), f"reserved-{name}")
            self.assertEqual(got.returncode, 2, got.stderr)
            self.assertIn("unsafe owned_files", got.stderr)

    def test_pre_dispatch_sidecar_is_terminal_failed_without_child(self):
        lifecycle = self.root / "lifecycle-pre-dispatch.json"
        invalid = {"task_id":"bad", "revision":1, "cwd":str(self.repo)}
        got, run = self.invoke(invalid, "invalid-pre-dispatch", lifecycle=lifecycle)
        self.assertEqual(got.returncode, 2)
        self.assertFalse(run.exists())
        sidecar = json.loads(lifecycle.read_text())
        self.assertTrue(sidecar["terminal"])
        self.assertEqual(sidecar["status"], "failed")
        self.assertEqual(sidecar["phase"], "pre_dispatch")
        self.assertFalse(sidecar["child_started"])
        self.assertIn("BridgeError", sidecar["reason"])

    def test_launch_intent_interrupt_is_indeterminate_not_pre_dispatch_failure(self):
        module = load_bridge_module()
        packet_path = self.root / "launch-intent.json"
        packet_path.write_text(json.dumps(self.packet()))
        lifecycle = self.root / "lifecycle-launch-intent.json"
        args = argparse.Namespace(packet=str(packet_path), run_dir=str(self.root / "launch-intent-run"), timeout=3, resume_from=None)
        previous = os.environ.get("CODEX_BRIDGE_LIFECYCLE_FILE")
        os.environ["CODEX_BRIDGE_LIFECYCLE_FILE"] = str(lifecycle)
        def interrupt_after_launch_intent(actual_args):
            module.lifecycle_update(actual_args, phase="launch_intent", child_started=None)
            raise KeyboardInterrupt()
        try:
            with mock.patch.object(module, "_run", side_effect=interrupt_after_launch_intent):
                with self.assertRaises(KeyboardInterrupt):
                    module.run(args)
        finally:
            if previous is None:
                os.environ.pop("CODEX_BRIDGE_LIFECYCLE_FILE", None)
            else:
                os.environ["CODEX_BRIDGE_LIFECYCLE_FILE"] = previous
        sidecar = json.loads(lifecycle.read_text())
        self.assertTrue(sidecar["terminal"])
        self.assertEqual(sidecar["status"], "unknown")
        self.assertEqual(sidecar["phase"], "terminal")
        self.assertIsNone(sidecar["child_started"])
        self.assertIn("KeyboardInterrupt", sidecar["reason"])

    def test_launch_intent_marker_precedes_claude_and_is_released_only_when_no_child_can_remain(self):
        module = load_bridge_module()
        lane = module.lane_identity(self.repo)
        self.addCleanup(lambda: [path.unlink(missing_ok=True) for path, _ in module.unknown_markers(lane)])
        original_popen = subprocess.Popen
        # (outcome at the Claude Popen, receipt status, whether the lane claim may be released)
        cases = (("reported", "reported", True),
                 ("exec_failure", "failed", True),
                 ("interrupted_during_launch", "cancelled", False))
        for outcome, receipt_status, released in cases:
            with self.subTest(outcome=outcome):
                run_dir = self.root / f"intent-{outcome}"
                lifecycle = self.root / f"intent-{outcome}-lifecycle.json"
                packet_path = self.root / f"intent-{outcome}.json"
                packet_path.write_text(json.dumps({**self.packet(), "task_id": "intent-" + outcome}))
                at_launch = {}

                def popen(command, *args, **kwargs):
                    if isinstance(command, list) and "--json-schema" in command:
                        at_launch["marker_paths"] = module.unknown_markers(lane)
                        at_launch["markers"] = [value for _, value in at_launch["marker_paths"]]
                        at_launch["lifecycle"] = json.loads(lifecycle.read_text())
                        if outcome == "exec_failure":
                            raise FileNotFoundError(2, "fixture exec failure")
                        if outcome == "interrupted_during_launch":
                            raise KeyboardInterrupt()
                    return original_popen(command, *args, **kwargs)

                args = argparse.Namespace(packet=str(packet_path), run_dir=str(run_dir), timeout=3, resume_from=None)
                with mock.patch.dict(os.environ, {"CODEX_BRIDGE_LIFECYCLE_FILE": str(lifecycle)}), \
                        mock.patch.object(module.subprocess, "Popen", new=popen):
                    module.run(args)

                # The claim is durable before any child can exist, and this
                # run's own checks have already passed so it cannot self-reject.
                self.assertEqual([value["run_id"] for value in at_launch["markers"]], [run_dir.name] * 2)
                self.assertEqual({path.parent for path, _ in at_launch["marker_paths"]}, set(module.unknown_marker_roots()))
                claim = at_launch["markers"][0]
                self.assertEqual((claim["lane_identity"], claim["cwd"]), (lane, str(self.repo.resolve())))
                self.assertEqual(claim["launch_intent"]["run_dir"], str(run_dir))
                self.assertEqual(claim["launch_intent"]["lifecycle_file"], str(lifecycle))
                self.assertEqual((at_launch["lifecycle"]["phase"], at_launch["lifecycle"]["child_started"]),
                                 ("launch_intent", None))
                self.assertEqual(json.loads((run_dir / "receipt.json").read_text())["status"], receipt_status)
                self.assertTrue(json.loads(lifecycle.read_text())["terminal"])
                remaining_paths = module.unknown_markers(lane)
                remaining = [value for _, value in remaining_paths]
                if released:
                    self.assertEqual(remaining, [])
                else:
                    self.assertEqual([value["run_id"] for value in remaining], [run_dir.name] * 2)
                    self.assertEqual({path.parent for path, _ in remaining_paths}, set(module.unknown_marker_roots()))
                    self.assertEqual(remaining[0]["launch_intent"], claim["launch_intent"])

        got, _ = self.invoke(self.packet(), "after-uncertain-launch")
        self.assertNotEqual(got.returncode, 0)
        self.assertIn("unknown prior supervised run", got.stderr)

    def test_preflight_and_executor_blocked_by_are_distinct(self):
        os.environ["STABILITY_OMIT_FLAG"] = "--verbose"
        got, run = self.invoke(self.packet(), "preflight-blocked")
        self.assertNotEqual(got.returncode, 0)
        preflight = json.loads((run / "receipt.json").read_text())
        self.assertEqual(preflight["status"], "blocked")
        self.assertEqual(preflight["blocked_by"], "preflight")
        self.assertEqual(preflight["reason"], "cli_incompatible")
        self.assertFalse((run / "command.json").exists())
        os.environ.pop("STABILITY_OMIT_FLAG")
        os.environ["STABILITY_STATUS"] = "blocked"
        got, run = self.invoke(self.packet(), "executor-blocked")
        self.assertEqual(got.returncode, 0, got.stderr)
        receipt = json.loads((run / "receipt.json").read_text())
        result = json.loads((run / "result.json").read_text())
        self.assertEqual(receipt["status"], "blocked")
        self.assertEqual(receipt["blocked_by"], "executor")
        self.assertEqual(result["blocked_by"], "executor")
        self.assertEqual(result["structured"]["summary"], "fixture summary")
        self.assertEqual(result["structured"]["unresolved"], ["need more detail"])

    def run_with_cancel_at(self, stage, *, early):
        """Run the bridge in process and record a cancellation as soon as ``stage`` returns."""
        module = load_bridge_module()
        name = f"cancel-{stage}-{'early' if early else 'run'}"
        run_dir = self.root / name
        packet_path = self.root / f"{name}.json"
        packet_path.write_text(json.dumps({**self.packet(role="review"), "task_id": name}))
        lifecycle = self.root / f"{name}-lifecycle.json"
        request = self.root / f"{name}-request.json"
        original = getattr(module, stage)
        launched = []

        def cancel_after(*args, **kwargs):
            value = original(*args, **kwargs)
            # The descriptor is verified once before run_dir exists; only the
            # final check after the guard self-test is the stage under test.
            if stage == "verify_cli_descriptor" and not (run_dir / "hook-guard-preflight.json").exists():
                return value
            if early:
                request.write_text("{}")
            else:
                module.dump(run_dir / "cancel.json", {"reason": f"fixture cancel during {stage}"})
            return value

        original_popen = subprocess.Popen

        def popen(command, *args, **kwargs):
            if isinstance(command, list) and "--json-schema" in command:
                launched.append(command)
            return original_popen(command, *args, **kwargs)

        environment = {"CODEX_BRIDGE_LIFECYCLE_FILE": str(lifecycle)}
        if early:
            environment["CODEX_BRIDGE_CANCEL_FILE"] = str(request)
        args = argparse.Namespace(packet=str(packet_path), run_dir=str(run_dir), timeout=3, resume_from=None)
        with mock.patch.dict(os.environ, environment), mock.patch.object(module, stage, side_effect=cancel_after), \
                mock.patch.object(module.subprocess, "Popen", new=popen):
            code = module.run(args)
        return module, code, run_dir, json.loads(lifecycle.read_text()), launched

    def test_cancellation_during_final_preflight_never_launches_claude(self):
        for stage in ("check_environment", "hook_policy_preflight", "verify_cli_descriptor"):
            for early in (False, True):
                with self.subTest(stage=stage, early=early):
                    module, code, run_dir, lifecycle, launched = self.run_with_cancel_at(stage, early=early)
                    self.assertEqual(code, 1)
                    self.assertEqual(launched, [], "no Claude child may be spawned after a cancellation was observed")
                    self.assertFalse((run_dir / "child.json").exists())
                    self.assertTrue((run_dir / "cancel.json").exists())
                    receipt = json.loads((run_dir / "receipt.json").read_text())
                    self.assertEqual(receipt["status"], "cancelled")
                    self.assertIn("before Claude dispatch", receipt["note"])
                    self.assertEqual((lifecycle["terminal"], lifecycle["phase"], lifecycle["child_started"], lifecycle["status"]),
                                     (True, "pre_dispatch", False, "cancelled"))
                    self.assertEqual(module.unknown_markers(module.lane_identity(self.repo)), [])

    def test_uncancelled_launch_still_reports_after_the_final_check(self):
        module = load_bridge_module()
        packet_path = self.root / "no-cancel.json"
        packet_path.write_text(json.dumps(self.packet(role="review")))
        lifecycle = self.root / "no-cancel-lifecycle.json"
        args = argparse.Namespace(packet=str(packet_path), run_dir=str(self.root / "no-cancel"), timeout=3, resume_from=None)
        with mock.patch.dict(os.environ, {"CODEX_BRIDGE_LIFECYCLE_FILE": str(lifecycle)}):
            self.assertEqual(module.run(args), 0)
        record = json.loads(lifecycle.read_text())
        self.assertEqual((record["phase"], record["child_started"], record["status"]), ("terminal", True, "reported"))
        self.assertEqual(json.loads((self.root / "no-cancel" / "result.json").read_text())["prompt_delivery"]["state"], "complete")

    def test_only_finite_nonnegative_provider_cost_is_transparent(self):
        for value, expected in (("0", 0.0), ("0.125", 0.125), ("-1", None), ("nan", None)):
            os.environ["STABILITY_COST"] = value
            got, run = self.invoke(self.packet(), "cost-" + value.replace(".", "_"))
            self.assertEqual(got.returncode, 0, got.stderr)
            result = json.loads((run / "result.json").read_text())
            self.assertEqual(result["total_cost_usd"], expected)


if __name__ == "__main__":
    unittest.main(verbosity=2)
