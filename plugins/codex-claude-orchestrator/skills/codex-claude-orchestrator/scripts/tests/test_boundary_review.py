"""Boundary regressions for the review findings that must fail closed."""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SCRIPTS = Path(__file__).resolve().parents[1]
BRIDGE = SCRIPTS / "bridge.py"
MANAGED_SETTINGS = {
    Path("/Library/Application Support/ClaudeCode/managed-settings.json"),
    Path("/etc/claude-code/managed-settings.json"),
}


def load_bridge_module():
    scripts = str(SCRIPTS)
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    spec = importlib.util.spec_from_file_location("bridge_boundary_review", BRIDGE)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class BoundaryReviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.home = self.root / "home"
        self.config = self.root / "claude-config"
        self.repo = self.root / "repo"
        self.repo.mkdir()
        (self.repo / "src").mkdir()
        (self.repo / "src" / "keep.py").write_text("pass\n", encoding="utf-8")
        (self.repo / "src" / "notes.md").write_text("notes\n", encoding="utf-8")
        for args in (("init", "-q"), ("config", "user.email", "fixture@example.invalid"),
                     ("config", "user.name", "Fixture")):
            subprocess.run(["git", "-C", str(self.repo), *args], check=True)
        subprocess.run(["git", "-C", str(self.repo), "add", "src"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-qm", "fixture"], check=True)
        self.requirement = self.root / "requirements.md"
        self.requirement.write_text("inspect only the fixture\n", encoding="utf-8")
        self.packet_path = self.root / "packet.json"
        self.packet_path.write_text(json.dumps({
            "task_id": "boundary-review", "revision": 1, "role": "review", "cwd": str(self.repo),
            "objective": "review fixture", "requirement_sources": [str(self.requirement)],
            "constraints": ["no network"], "acceptance": ["structured result"],
            "owned_files": [], "protected_files": [], "model": "fixture-model", "effort": "low",
        }), encoding="utf-8")
        self.bridge = load_bridge_module()

    def tearDown(self):
        self.tmp.cleanup()

    @property
    def user_settings(self):
        return self.config / "settings.json"

    @property
    def project_settings(self):
        return self.repo / ".claude" / "settings.json"

    def policy_environment(self, **extra):
        environment = {"HOME": str(self.home), "CLAUDE_CONFIG_DIR": str(self.config)}
        environment.update(extra)
        return environment

    def preflight(self, **extra):
        """Run against only fixture settings, never host managed-settings files."""
        original_exists = Path.exists
        original_read_text = Path.read_text
        allowed_read_paths = {self.user_settings, self.project_settings,
                              self.repo / ".claude" / "settings.local.json"}

        def fixture_exists(path):
            if path in MANAGED_SETTINGS:
                return False
            return original_exists(path)

        def fixture_read_text(path, *args, **kwargs):
            if path not in allowed_read_paths:
                raise AssertionError(f"unexpected settings read outside fixture: {path}")
            return original_read_text(path, *args, **kwargs)

        with mock.patch.object(self.bridge.Path, "exists", fixture_exists), \
             mock.patch.object(self.bridge.Path, "read_text", fixture_read_text):
            return self.bridge.hook_policy_preflight(self.repo, self.policy_environment(**extra))

    def hook(self, event):
        env = {"HOME": str(self.home), "CLAUDE_CONFIG_DIR": str(self.config),
               "PATH": os.environ.get("PATH", "")}
        return subprocess.run(
            [sys.executable, str(BRIDGE), "hook", "--packet", str(self.packet_path), "--cwd", str(self.repo)],
            input=json.dumps(event), text=True, capture_output=True, env=env, timeout=10,
        )

    def assert_hook_denied(self, event):
        result = self.hook(event)
        self.assertEqual(result.returncode, 0, result.stderr)
        reply = json.loads(result.stdout)
        self.assertEqual(reply["hookSpecificOutput"]["permissionDecision"], "deny")
        return reply["hookSpecificOutput"]["permissionDecisionReason"]

    def test_glob_braces_are_expanded_and_hook_rejects_lexical_escape_forms(self):
        self.assertEqual(self.bridge._glob_variants("src/**/*.{py,md}"),
                         ["src/**/*.py", "src/**/*.md"])
        for index, pattern in enumerate(("~/x", "$HOME/x", "{../escape,src}/**")):
            with self.subTest(pattern=pattern):
                self.assert_hook_denied({"tool_name": "Glob", "tool_input": {"path": ".", "pattern": pattern},
                                         "tool_use_id": f"escaped-glob-{index}"})
        allowed = self.hook({"tool_name": "Glob", "tool_input": {"path": ".", "pattern": "src/**/*.{py,md}"},
                             "tool_use_id": "allowed-glob"})
        self.assertEqual(allowed.returncode, 0, allowed.stderr)
        self.assertEqual(allowed.stdout, "")

    def test_hook_requires_an_auditable_tool_use_id(self):
        reason = self.assert_hook_denied({"tool_name": "Read", "tool_input": {"file_path": "src/keep.py"}})
        self.assertIn("auditable tool_use_id", reason)

    def test_hook_policy_preflight_blocks_safe_mode_and_simple_mode(self):
        for variable in ("CLAUDE_CODE_SAFE_MODE", "CLAUDE_CODE_SIMPLE"):
            with self.subTest(variable=variable):
                with self.assertRaisesRegex(self.bridge.BridgeError, variable):
                    self.preflight(**{variable: "1"})

    def test_hook_policy_preflight_fails_closed_for_user_and_project_disable_flags(self):
        for settings_path, source in ((self.user_settings, "user"), (self.project_settings, "project")):
            settings_path.parent.mkdir(parents=True, exist_ok=True)
            for flag in ("disableAllHooks", "allowManagedHooksOnly"):
                with self.subTest(source=source, flag=flag):
                    settings_path.write_text(json.dumps({flag: True}), encoding="utf-8")
                    with self.assertRaisesRegex(self.bridge.BridgeError, f"{source}:{flag}=true"):
                        self.preflight()
            settings_path.unlink()

    def test_hook_policy_preflight_fails_closed_for_malformed_or_non_boolean_settings(self):
        self.user_settings.parent.mkdir(parents=True, exist_ok=True)
        self.user_settings.write_text("{ malformed", encoding="utf-8")
        with self.assertRaisesRegex(self.bridge.BridgeError, "cannot verify Claude hook policy"):
            self.preflight()
        self.user_settings.write_text(json.dumps({"disableAllHooks": "false"}), encoding="utf-8")
        with self.assertRaisesRegex(self.bridge.BridgeError, "not a boolean"):
            self.preflight()

    def test_hook_policy_preflight_records_only_local_file_visibility_when_clear(self):
        result = self.preflight()
        self.assertEqual(result["status"], "local_settings_checked")
        self.assertEqual(result["settings"], [])
        self.assertIn("local_files_only", result["managed_policy_visibility"])

    def test_parse_stream_keeps_init_separate_and_reports_multiple_actual_models_in_order(self):
        session = "boundary-session"
        stream = self.root / "stream.jsonl"
        events = [
            {"type": "system", "subtype": "init", "session_id": session, "model": "init-only"},
            {"type": "assistant", "session_id": session, "message": {"model": "assistant-model", "content": []}},
            {"type": "assistant", "session_id": session, "message": {"model": "assistant-model", "content": []}},
            {"type": "result", "subtype": "success", "session_id": session,
             "modelUsage": {"result-model": {}, "assistant-model": {}}},
        ]
        stream.write_text("\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8")

        final, metadata = self.bridge.parse_stream(stream, session)

        self.assertEqual(final, events[-1])
        self.assertEqual(metadata["initialized_model"], "init-only")
        self.assertEqual(metadata["actual_models"], ["assistant-model", "result-model"])
        self.assertEqual(metadata["actual_model_sources"], ["assistant_message", "result_model_usage"])
        self.assertEqual(metadata["actual_model_source"], "assistant_message+result_model_usage")
        self.assertNotIn("init-only", metadata["actual_models"])
        self.assertNotIn("actual_model", metadata)


if __name__ == "__main__":
    unittest.main()
