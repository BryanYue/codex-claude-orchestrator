"""Bridge helpers that admit a local CLI and verify each run's actual behavior."""
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
import bridge  # noqa: E402
import compatibility  # noqa: E402


class LocalCapabilityHelperTests(unittest.TestCase):
    def test_flag_matching_is_exact(self):
        help_text = "-p, --print  --tools <tools>  --permission-mode <mode>  --resume-session"
        self.assertTrue(compatibility.flag_advertised(help_text, "-p"))
        self.assertTrue(compatibility.flag_advertised(help_text, "--tools"))
        self.assertFalse(compatibility.flag_advertised(help_text, "--resume"))
        self.assertFalse(compatibility.flag_advertised("--print", "-p"))
        self.assertFalse(compatibility.flag_advertised("--allowedTools", "--tools"))

    def test_resume_group_adds_only_the_resume_flag(self):
        self.assertEqual(compatibility.required_flags({"core", "read_only"}), set(compatibility.REQUIRED_FLAGS))
        self.assertEqual(compatibility.required_flags({"core", "resume"}) - set(compatibility.REQUIRED_FLAGS), {"--resume"})

    def test_version_profiles_are_history_not_admission(self):
        source = (SCRIPTS / "bridge.py").read_text(encoding="utf-8")
        self.assertNotIn("SUPPORTED_CLI_PROFILES", source)
        self.assertNotIn("profile_for", source)

    def test_git_root_paths_are_converted_to_cwd_coordinates(self):
        self.assertEqual(bridge.cwd_relative_status_path("src/a.txt", ""), "src/a.txt")
        self.assertEqual(bridge.cwd_relative_status_path("package/a.txt", "package/"), "a.txt")
        self.assertEqual(bridge.cwd_relative_status_path("other/b.txt", "package/"), "../other/b.txt")
        self.assertEqual(bridge.cwd_relative_status_path("root.txt", "a/b/"), "../../root.txt")
        self.assertTrue(bridge.bad_owned_path(bridge.cwd_relative_status_path("root.txt", "a/")))

    def test_nested_cwd_with_leading_space_keeps_owned_scope(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            child = repo / " space"
            child.mkdir(parents=True)
            for args in (("init", "-q"), ("config", "user.email", "fixture@example.invalid"), ("config", "user.name", "Fixture")):
                subprocess.run(["git", "-C", str(repo), *args], check=True)
            (child / "owned.txt").write_text("base\n")
            (repo / "outside.txt").write_text("base\n")
            subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-qm", "base"], check=True)
            packet = {"owned_files": ["owned.txt"], "protected_files": []}
            before = bridge.git_snapshot(child, packet)
            self.assertEqual(before["git_prefix"], " space/")

            (child / "owned.txt").write_text("changed\n")
            after = bridge.git_snapshot(child, packet)
            self.assertEqual(bridge.changed_paths(after), {"owned.txt"})
            self.assertEqual(bridge.scope_violations(packet, before, after), [])

            (repo / "outside.txt").write_text("changed\n")
            after = bridge.git_snapshot(child, packet)
            self.assertEqual(bridge.scope_violations(packet, before, after), ["../outside.txt"])

    def test_unexpected_tools_are_named_against_the_task_tool_set(self):
        uses = {"1": "Read", "2": "StructuredOutput", "3": "Bash", "4": "EndConversation", "5": "Write"}
        self.assertEqual(bridge.unexpected_tool_uses(uses, bridge.READ_TOOLS), ["Bash", "Write"])
        self.assertEqual(bridge.unexpected_tool_uses(uses, bridge.READ_TOOLS + bridge.WRITE_TOOLS), ["Bash"])

    def test_probe_diagnostics_keep_failure_class_without_secrets(self):
        timeout = bridge.probe_diagnostic(None, "", "", "timeout")
        self.assertEqual(timeout, {"exit_code": None, "error_kind": "timeout"})
        signalled = bridge.probe_diagnostic(-9, "", "", None)
        self.assertEqual((signalled["error_kind"], signalled["signal"]), ("signal", 9))
        failed = bridge.probe_diagnostic(3, "", "boom sk-ant-abcdef token=abc123 user@example.com " + "x" * 600, None)
        self.assertEqual(failed["error_kind"], "nonzero_exit")
        self.assertIn("boom", failed["stderr_excerpt"])
        for secret in ("sk-ant-abcdef", "abc123", "user@example.com"):
            self.assertNotIn(secret, failed["stderr_excerpt"])
        self.assertLessEqual(len(failed["stderr_excerpt"]), 401)


if __name__ == "__main__":
    unittest.main()
