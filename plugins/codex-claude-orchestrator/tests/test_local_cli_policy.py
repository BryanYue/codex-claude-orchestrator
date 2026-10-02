"""User-local Claude CLI policy across every production entry point."""
from __future__ import annotations

import inspect
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import test_runtime as fixtures

ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT.parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import model_catalog  # noqa: E402
import cli_store  # noqa: E402
import cli_validation  # noqa: E402

BRIDGE = ROOT / "skills/codex-claude-orchestrator/scripts/bridge.py"
PRODUCTION_ENTRIES = [
    ROOT / "scripts/server.py", ROOT / "scripts/viewer.py", ROOT / "scripts/install.py",
    ROOT / "scripts/executable_locator.py", ROOT / "scripts/model_catalog.py", ROOT / "scripts/diagnostics.py",
    ROOT / "scripts/cli_updates.py", ROOT / "scripts/launch.sh", REPO / "Install.command",
    ROOT / "skills/codex-claude-orchestrator/scripts/bridge.py",
    ROOT / "skills/codex-claude-orchestrator/scripts/runtime.py",
    ROOT / "skills/codex-claude-orchestrator/scripts/compatibility.py",
]
RETIRED_CALLS = ("cli_store.prepare", "cli_store.capture", "acquire_official", "activate_explicit",
                 "rollback_explicit", "activate_automatic", "cli_validation.start", "official_releases",
                 "set_update_policy", "set_external_mode", "discover_latest", "DISABLE_AUTOUPDATER",
                 "validate_probe_descriptor", "cli_store.select")


class EntryPointTests(unittest.TestCase):
    def test_no_production_entry_can_download_capture_qualify_or_switch(self):
        for path in PRODUCTION_ENTRIES:
            text = path.read_text(encoding="utf-8")
            for call in RETIRED_CALLS:
                with self.subTest(entry=path.name, call=call):
                    self.assertNotIn(call, text)

    def test_retired_implementation_cannot_be_invoked_directly(self):
        for name in ("prepare", "capture", "identity", "record_qualification", "select", "dispatch_metadata",
                     "activate", "activate_explicit", "activate_automatic", "rollback", "rollback_explicit",
                     "set_update_policy", "acquire_official_release", "acquire_official_target"):
            with self.subTest(module="cli_store", api=name):
                self.assertFalse(hasattr(cli_store, name))
        for name in ("start", "_worker", "validate_probe_descriptor", "_run_scenario", "_finalize_success"):
            with self.subTest(module="cli_validation", api=name):
                self.assertFalse(hasattr(cli_validation, name))
        self.assertFalse((ROOT / "scripts/official_releases.py").exists())
        for module in (cli_store, cli_validation):
            imported = inspect.getsource(module)
            self.assertNotIn("import subprocess", imported)
            self.assertNotIn("urllib", imported)

    def test_current_local_version_has_no_hard_coded_admission(self):
        for path in [*PRODUCTION_ENTRIES, ROOT / "scripts/cli_store.py", ROOT / "scripts/cli_validation.py"]:
            with self.subTest(entry=path.name):
                self.assertNotIn("2.1.284", path.read_text(encoding="utf-8"))

    def test_model_catalog_cannot_address_a_retained_identity(self):
        self.assertNotIn("identity_id", inspect.signature(model_catalog.collect).parameters)


class LocalDispatchTests(unittest.TestCase):
    setUp = fixtures.RuntimeTests.setUp
    tearDown = fixtures.RuntimeTests.tearDown
    packet = fixtures.RuntimeTests.packet
    finish = fixtures.RuntimeTests.finish

    def test_legacy_managed_selection_and_private_copy_are_never_executed(self):
        store = Path(os.environ["CLAUDE_ORCHESTRATOR_CLI_ROOT"])
        marker = self.root / "private-copy-ran"
        private = store / "versions" / "darwin-arm64-2.1.278-deadbeefdeadbeefdead"
        private.mkdir(parents=True)
        (private / "claude").write_text(f"#!/bin/sh\ntouch {marker}\nexit 0\n")
        (private / "claude").chmod(0o700)
        (store / "selection.json").write_text(json.dumps({
            "schema_version": 1, "generation": 9, "active": private.name, "previous": None,
            "history": [private.name], "mode": "managed"}))
        before = sorted(str(path.relative_to(store)) for path in store.rglob("*"))
        final = self.finish(self.runtime.start(self.packet())["run_id"])["snapshot"]
        self.assertEqual(final["status"], "reported")
        pinned = json.loads((Path(final["run_dir"]) / "cli-selection.json").read_text())
        self.assertEqual(pinned["selection"]["path"], str(self.fake))
        self.assertEqual(pinned["selection"]["source"], "CLAUDE_BIN")
        self.assertFalse(marker.exists(), "the retained private copy must not run")
        self.assertEqual(sorted(str(path.relative_to(store)) for path in store.rglob("*")), before,
                         "dispatch must neither delete nor extend retained history")


TOOL_FAKE = """#!/usr/bin/env python3
import json, os, sys
a = sys.argv[1:]
if a == ['--version']: print('9.9.999'); raise SystemExit
if a == ['--help']: print('-p --model --effort --output-format --verbose --json-schema --session-id --resume --permission-mode --tools --allowedTools --disallowedTools --settings --strict-mcp-config --mcp-config --disable-slash-commands --no-session-persistence'); raise SystemExit
if a == ['auth', 'status', '--json']: print(json.dumps({'loggedIn': True})); raise SystemExit
session = a[a.index('--session-id') + 1]
sys.stdin.buffer.read()
tool = os.environ.get('FAKE_TOOL', 'StructuredOutput')
print(json.dumps({'type': 'system', 'subtype': 'init', 'session_id': session, 'model': 'fixture'}))
print(json.dumps({'type': 'assistant', 'session_id': session, 'message': {'model': 'fixture', 'content': [
    {'type': 'tool_use', 'id': 'tool-1', 'name': tool, 'input': {'command': 'true'}}]}}))
print(json.dumps({'type': 'result', 'subtype': 'success', 'session_id': session,
                  'structured_output': {'status': 'completed', 'summary': 'done', 'evidence': [], 'checks': [], 'unresolved': []}}))
"""


class BridgeGuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"; self.repo.mkdir()
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        self.requirement = self.root / "spec.md"; self.requirement.write_text("spec\n")
        self.fake = self.root / "claude"; self.fake.write_text(TOOL_FAKE); self.fake.chmod(0o755)
        self.env = {**os.environ, "CLAUDE_BIN": str(self.fake), "CLAUDE_CONFIG_DIR": str(self.root / "claude-config"),
                    "CLAUDE_ORCHESTRATOR_CLI_ROOT": str(self.root / "store")}

    def tearDown(self):
        self.tmp.cleanup()

    def invoke(self, name: str, extra: list[str] | None = None, tool: str = "StructuredOutput"):
        packet = {"task_id": name, "revision": 1, "role": "review", "cwd": str(self.repo), "objective": "fixture",
                  "requirement_sources": [str(self.requirement)], "constraints": [], "acceptance": [],
                  "owned_files": [], "protected_files": [], "model": "fixture", "effort": "low"}
        packet_path = self.root / f"{name}.json"; packet_path.write_text(json.dumps(packet))
        run_dir = self.root / name
        completed = subprocess.run([sys.executable, str(BRIDGE), "run", "--packet", str(packet_path), "--run-dir",
                                    str(run_dir), "--timeout", "10", *(extra or [])],
                                   env={**self.env, "FAKE_TOOL": tool}, capture_output=True, text=True)
        return completed, run_dir

    def test_schema_formatter_tool_is_permitted(self):
        completed, run = self.invoke("formatter")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads((run / "receipt.json").read_text())["status"], "reported")

    def test_tool_outside_the_task_tool_set_fails_even_without_a_hook_denial(self):
        completed, run = self.invoke("bash", tool="Bash")
        self.assertNotEqual(completed.returncode, 0)
        result = json.loads((run / "result.json").read_text())
        self.assertEqual(json.loads((run / "receipt.json").read_text())["status"], "failed")
        self.assertIn("Bash", result["tool_policy_error"])
        self.assertEqual(result["hook_guard_coverage"]["status"], "complete")

    def test_qualification_descriptor_is_refused_before_any_run_directory(self):
        # invoke() writes its packet to <name>.json, so the descriptor needs a distinct name.
        descriptor = self.root / "qualification-descriptor.json"
        descriptor.write_text(json.dumps({"schema_version": 1, "purpose": "qualification", "identity_id": "candidate",
                                          "job_id": "qualification-aaaaaaaaaaaaaaaa", "scenario": "git_review"}))
        completed, run = self.invoke("qualification", ["--cli-descriptor", str(descriptor)])
        self.assertEqual(completed.returncode, 2)
        self.assertIn("qualification descriptors are retired", completed.stderr)
        self.assertFalse(run.exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
