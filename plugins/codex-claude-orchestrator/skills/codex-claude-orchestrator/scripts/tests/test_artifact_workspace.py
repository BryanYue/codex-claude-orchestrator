import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1]
BRIDGE = SCRIPTS / "bridge.py"
sys.path.insert(0, str(SCRIPTS))
from workspace import artifact_snapshot, artifact_snapshot_difference


class ArtifactWorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.enterContext(mock.patch.dict(os.environ, {"CLAUDE_ORCHESTRATOR_CLI_ROOT": str(self.root / "isolated-cli-store"),
                                                        "CLAUDE_CONFIG_DIR": str(self.root / "claude-config")}))
        self.workspace = self.root / "materials"; self.workspace.mkdir()
        self.input = self.workspace / "brief.md"; self.input.write_text("review this\n")
        self.extra = self.workspace / "private.txt"; self.extra.write_text("not declared\n")
        self.requirement = self.root / "requirements.md"; self.requirement.write_text("cite the brief\n")
        self.fake = self.root / "fake_claude.py"
        self.fake.write_text("""#!/usr/bin/env python3
import json, os, pathlib, sys
args=sys.argv[1:]
if args == ['--version']: print('2.1.276 (Claude Code)'); raise SystemExit(0)
if args == ['--help']:
 print('-p --model --effort --output-format --verbose --json-schema --session-id --resume --permission-mode --tools --allowedTools --disallowedTools --settings --strict-mcp-config --mcp-config --disable-slash-commands --no-session-persistence --max-turns --max-budget-usd'); raise SystemExit(0)
if args == ['auth','status','--json']: print(json.dumps({'loggedIn':True})); raise SystemExit(0)
session=args[args.index('--session-id')+1]
print(json.dumps({'type':'system','subtype':'init','session_id':session,'model':'test-model'}))
if os.environ.get('ARTIFACT_FAKE_MODE') == 'touch_input': pathlib.Path('brief.md').write_text('changed')
if os.environ.get('ARTIFACT_FAKE_MODE') == 'new_file': pathlib.Path('side-effect.txt').write_text('changed')
if os.environ.get('ARTIFACT_FAKE_MODE') == 'touch_undeclared_mtime': os.utime('private.txt', None)
if os.environ.get('ARTIFACT_FAKE_MODE') == 'new_finder_metadata': pathlib.Path('.DS_Store').write_bytes(b'Finder metadata')
print(json.dumps({'type':'result','subtype':'success','session_id':session,'usage':{'input_tokens':4,'cache_read_input_tokens':9},'structured_output':{'status':'completed','summary':'done','evidence':['brief.md'], 'checks':['manifest'], 'unresolved':[]}}))
""")
        self.fake.chmod(0o755)
        self.old_bin = os.environ.get("CLAUDE_BIN")
        os.environ["CLAUDE_BIN"] = str(self.fake)
        os.environ.pop("ARTIFACT_FAKE_MODE", None)

    def tearDown(self):
        if self.old_bin is None:
            os.environ.pop("CLAUDE_BIN", None)
        else:
            os.environ["CLAUDE_BIN"] = self.old_bin
        os.environ.pop("ARTIFACT_FAKE_MODE", None)
        self.tmp.cleanup()

    def packet(self):
        return {"task_id":"artifact-review", "revision":1, "role":"review", "workspace_kind":"artifacts",
                "cwd":str(self.workspace), "objective":"check brief", "input_files":["brief.md"],
                "requirement_sources":[str(self.requirement)], "constraints":["read only"], "acceptance":["structured"],
                "owned_files":[], "protected_files":[], "model":"test-model", "effort":"low"}

    def invoke(self, packet, name):
        packet_path = self.root / f"{name}.json"; packet_path.write_text(json.dumps(packet))
        run = self.root / name
        result = subprocess.run([sys.executable, str(BRIDGE), "run", "--packet", str(packet_path), "--run-dir", str(run), "--timeout", "3"],
                                text=True, capture_output=True)
        return result, run

    def test_artifact_root_symlink_is_rejected_before_resolving_relative_input(self):
        link = self.root / "user-link"
        link.symlink_to(self.workspace, target_is_directory=True)
        packet = self.packet()
        packet["cwd"] = str(link)
        packet["input_files"] = ["brief.md"]
        got, run = self.invoke(packet, "root-symlink")
        self.assertEqual(got.returncode, 2)
        self.assertIn("artifact workspace root symlink ancestor", got.stderr)
        self.assertFalse(run.exists())
        self.assertEqual(self.input.read_text(), "review this\n")

    def test_explicit_text_inputs_produce_read_only_manifests(self):
        got, run = self.invoke(self.packet(), "ok")
        self.assertEqual(got.returncode, 0, got.stderr)
        before = json.loads((run / "workspace_before.json").read_text())
        after = json.loads((run / "workspace_after.json").read_text())
        self.assertEqual(before, after)
        self.assertEqual(before["read_scope"], "exact_declared_files_only")
        self.assertEqual({entry["path"] for entry in before["entries"]}, {"brief.md", str(self.requirement.resolve())})
        self.assertFalse((run / "git_before.json").exists())
        result = json.loads((run / "result.json").read_text())
        self.assertEqual(result["workspace_kind"], "artifacts")
        self.assertEqual(result["usage_summary"], {"input_tokens": 4, "cache_read_input_tokens": 9})

    def test_input_and_directory_side_effects_fail_without_reverting(self):
        for mode in ("touch_input", "new_file"):
            os.environ["ARTIFACT_FAKE_MODE"] = mode
            got, run = self.invoke(self.packet(), mode)
            self.assertNotEqual(got.returncode, 0)
            result = json.loads((run / "result.json").read_text())
            self.assertIn("workspace changed", result["invariant_error"])
            if mode == "touch_input":
                self.assertIn("declared content changed: workspace:brief.md", result["invariant_error"])
            else:
                self.assertIn("directory entry added: side-effect.txt (file)", result["invariant_error"])
            if mode == "touch_input":
                self.input.write_text("review this\n")
            else:
                (self.workspace / "side-effect.txt").unlink()

    def test_undeclared_mtime_and_finder_metadata_do_not_change_workspace_evidence(self):
        # Unread undeclared files remain structurally guarded, but their mtime
        # and size are not content or side-effect proof.  Finder's one exact
        # metadata name is also ignored, while being visible in the manifest.
        finder = self.workspace / ".DS_Store"; finder.write_bytes(b"Finder metadata")
        before = artifact_snapshot(self.packet())
        os.utime(self.extra, None)
        after_mtime = artifact_snapshot(self.packet())
        self.assertEqual(artifact_snapshot_difference(before, after_mtime), [])
        self.assertEqual(before["directory_guard_sha256"], after_mtime["directory_guard_sha256"])
        self.assertEqual(before["workspace_digest"], after_mtime["workspace_digest"])
        self.assertEqual(after_mtime["ignored_directory_entries"], [".DS_Store"])
        os.environ["ARTIFACT_FAKE_MODE"] = "touch_undeclared_mtime"
        got, run = self.invoke(self.packet(), "undeclared-mtime")
        self.assertEqual(got.returncode, 0, got.stderr)
        run_before = json.loads((run / "workspace_before.json").read_text())
        run_after = json.loads((run / "workspace_after.json").read_text())
        self.assertEqual(artifact_snapshot_difference(run_before, run_after), [])
        self.assertEqual(json.loads((run / "receipt.json").read_text())["status"], "reported")
        os.environ["ARTIFACT_FAKE_MODE"] = "new_finder_metadata"
        finder.unlink()
        got, run = self.invoke(self.packet(), "finder-metadata")
        self.assertEqual(got.returncode, 0, got.stderr)
        final = json.loads((run / "workspace_after.json").read_text())
        self.assertEqual(final["ignored_directory_entries"], [".DS_Store"])
        self.assertNotIn(".DS_Store", {entry["path"] for entry in final["directory_guard_entries"]})
        self.assertEqual(json.loads((run / "receipt.json").read_text())["status"], "reported")

    def test_unknown_binary_and_unsupported_lanes_are_rejected_before_dispatch(self):
        binary = self.workspace / "sheet.xlsx"; binary.write_bytes(b"PK\x03\x04")
        unsupported = self.packet(); unsupported["input_files"] = ["sheet.xlsx"]
        got, _ = self.invoke(unsupported, "binary")
        self.assertEqual(got.returncode, 2)
        self.assertIn("unsupported", got.stderr)
        for role in ("implement", "workflow_review"):
            packet = self.packet(); packet["role"] = role
            if role == "implement": packet["owned_files"] = ["out.md"]
            got, _ = self.invoke(packet, role)
            self.assertEqual(got.returncode, 2)
            self.assertIn("supports review only", got.stderr)

    def test_symlinked_declared_input_or_requirement_is_rejected(self):
        linked_input = self.workspace / "linked.md"; linked_input.symlink_to(self.input)
        packet = self.packet(); packet["input_files"] = ["linked.md"]
        got, _ = self.invoke(packet, "linked-input")
        self.assertEqual(got.returncode, 2)
        self.assertIn("symlink", got.stderr)
        linked_requirement = self.root / "linked-requirements.md"; linked_requirement.symlink_to(self.requirement)
        packet = self.packet(); packet["requirement_sources"] = [str(linked_requirement)]
        got, _ = self.invoke(packet, "linked-requirement")
        self.assertEqual(got.returncode, 2)
        self.assertIn("symlink", got.stderr)

    def test_symlink_ancestor_is_rejected_for_input_and_external_requirement(self):
        actual_input_dir = self.workspace / "actual"; actual_input_dir.mkdir()
        (actual_input_dir / "nested.md").write_text("nested\n")
        (self.workspace / "input-link").symlink_to(actual_input_dir, target_is_directory=True)
        packet = self.packet(); packet["input_files"] = ["input-link/nested.md"]
        got, _ = self.invoke(packet, "input-ancestor-link")
        self.assertEqual(got.returncode, 2)
        self.assertIn("symlink ancestor", got.stderr)
        external_actual = self.root / "external-actual"; external_actual.mkdir()
        (external_actual / "requirements.md").write_text("external\n")
        (self.root / "external-link").symlink_to(external_actual, target_is_directory=True)
        packet = self.packet(); packet["requirement_sources"] = [str(self.root / "external-link" / "requirements.md")]
        got, _ = self.invoke(packet, "requirement-ancestor-link")
        self.assertEqual(got.returncode, 2)
        self.assertIn("symlink ancestor", got.stderr)

    def test_nul_and_non_utf8_content_are_rejected_as_not_text(self):
        cases = {"nul.md": b"hello\0world", "invalid.txt": b"\xff\xfe"}
        for name, content in cases.items():
            (self.workspace / name).write_bytes(content)
            packet = self.packet(); packet["input_files"] = [name]
            got, _ = self.invoke(packet, name)
            self.assertEqual(got.returncode, 2)
            self.assertTrue("NUL" in got.stderr or "UTF-8" in got.stderr, got.stderr)

    def test_non_finite_budget_is_rejected_before_cli_dispatch(self):
        for name, value in (("nan", float("nan")), ("infinity", float("inf"))):
            packet = self.packet(); packet["budget"] = {"max_budget_usd": value}
            got, _ = self.invoke(packet, name)
            self.assertEqual(got.returncode, 2)
            self.assertIn("positive number", got.stderr)

    def test_artifacts_lane_cannot_bypass_a_git_worktree(self):
        subprocess.run(["git", "init", "-q", str(self.workspace)], check=True)
        got, _ = self.invoke(self.packet(), "git-artifacts")
        self.assertEqual(got.returncode, 2)
        self.assertIn("non-Git", got.stderr)

    def test_hook_allows_only_declared_artifact_files(self):
        packet_path = self.root / "hook.json"; packet_path.write_text(json.dumps(self.packet()))
        def hook(file_name):
            return subprocess.run([sys.executable, str(BRIDGE), "hook", "--packet", str(packet_path), "--cwd", str(self.workspace)],
                                  input=json.dumps({"tool_name":"Read", "tool_input":{"file_path":file_name},
                                                    "tool_use_id":"artifact-" + file_name.replace("/", "-")}),
                                  text=True, capture_output=True)
        self.assertEqual(hook("brief.md").stdout, "")
        denied = hook("private.txt")
        self.assertEqual(json.loads(denied.stdout)["hookSpecificOutput"]["permissionDecision"], "deny")
        alias = self.workspace / "alias"; alias.symlink_to(self.workspace, target_is_directory=True)
        through_alias = hook("alias/brief.md")
        self.assertEqual(json.loads(through_alias.stdout)["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_artifact_resume_and_protocol_binding_are_refused(self):
        packet = self.packet(); packet["protocol_binding"] = {"protocol_id":"not-used"}
        got, _ = self.invoke(packet, "binding")
        self.assertEqual(got.returncode, 2)
        self.assertIn("does not support protocol_binding", got.stderr)
        # A direct resume request reaches the explicit lane guard before a
        # previous-run identity is consulted.
        first, run = self.invoke(self.packet(), "first")
        self.assertEqual(first.returncode, 0, first.stderr)
        follow_up = self.packet(); follow_up["revision"] = 2
        packet_path = self.root / "resume.json"; packet_path.write_text(json.dumps(follow_up))
        resumed = subprocess.run([sys.executable, str(BRIDGE), "run", "--packet", str(packet_path), "--run-dir", str(self.root / "resume"),
                                  "--timeout", "3", "--resume-from", str(run)], text=True, capture_output=True)
        self.assertEqual(resumed.returncode, 2)
        self.assertIn("does not support resume", resumed.stderr)


if __name__ == "__main__":
    unittest.main()
