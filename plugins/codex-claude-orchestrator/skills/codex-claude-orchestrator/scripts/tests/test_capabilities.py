"""Isolated compatibility-profile fixtures.

These tests exercise adapter wiring only.  They never add a production CLI
profile, and therefore cannot turn a locally installed newer Claude Code into
an accepted bridge version.
"""
import os
import sys
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
import bridge  # noqa: E402
from compatibility import SUPPORTED_CLI_PROFILES  # noqa: E402


class CapabilityFixtureTests(unittest.TestCase):
    def report(self, version, help_text, groups=None):
        def execute(command, *args, **kwargs):
            if command[1:] == ["--version"]:
                return 0, version, "", None
            if command[1:] == ["--help"]:
                return 0, help_text, "", None
            if command[1:] == ["auth", "status", "--json"]:
                return 0, '{"loggedIn":true}', "", None
            self.fail("local check made an unexpected provider request")
        with patch.object(bridge, "check_command", side_effect=execute):
            return bridge.check_environment(Path.cwd(), required_groups=groups,
                                            selection={"path": "/fixture/claude", "source": "fixture"})

    def test_unknown_candidate_reports_login_separately_and_help_does_not_qualify(self):
        report = self.report("9.9.999", " ".join(bridge.REQUIRED_FLAGS) + " --resume")
        self.assertFalse(report["ready"])
        self.assertEqual(report["status"], "cli_profile_unverified")
        self.assertTrue(report["installation"]["installed"])
        self.assertEqual(report["readiness"]["login"]["status"], "reported_logged_in")
        self.assertEqual(report["readiness"]["invocation"]["status"], "not_requested")
        self.assertEqual(report["compatibility"]["status"], "unverified")

    def test_absent_optional_resume_flag_does_not_block_fresh_review(self):
        flags = " ".join(bridge.REQUIRED_FLAGS)
        fresh = self.report("2.1.278", flags)
        resume = self.report("2.1.278", flags, ["core", "read_only", "resume"])
        self.assertTrue(fresh["ready"])
        self.assertNotIn("resume", fresh["compatibility"]["available_groups"])
        self.assertFalse(resume["ready"])
        self.assertEqual(resume["cli"]["missing_flags"], ["--resume"])

    def test_temporary_profile_is_dependency_injected_not_product_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake = root / "claude"
            fake.write_text("""#!/usr/bin/env python3
import json, sys
if sys.argv[1:] == ['--version']: print('9.9.999 (fixture)')
elif sys.argv[1:] == ['--help']: print('-p --model --effort --output-format --json-schema --session-id --resume --permission-mode --tools --allowedTools --disallowedTools --settings --strict-mcp-config --mcp-config --disable-slash-commands --max-turns --max-budget-usd')
elif sys.argv[1:] == ['auth', 'status', '--json']: print(json.dumps({'loggedIn': True}))
else: raise SystemExit(2)
""")
            fake.chmod(0o755)
            self.enterContext(patch.dict(os.environ, {"CLAUDE_ORCHESTRATOR_CLI_ROOT": str(root / "isolated-cli-store")}))
            old = os.environ.get("CLAUDE_BIN")
            os.environ["CLAUDE_BIN"] = str(fake)
            try:
                fixture_profiles = dict(SUPPORTED_CLI_PROFILES)
                fixture_profiles["9.9.999"] = {"tested": True, "capabilities": {"max_turns": "--max-turns", "max_budget_usd": "--max-budget-usd"}}
                report = bridge.check_environment(root, profiles=fixture_profiles)
                self.assertTrue(report["ready"])
                self.assertEqual(report["cli"]["profile"]["status"], "tested")
                self.assertIn("max_turns", report["cli"]["capabilities"])
                self.assertNotIn("9.9.999", SUPPORTED_CLI_PROFILES)
            finally:
                if old is None:
                    os.environ.pop("CLAUDE_BIN", None)
                else:
                    os.environ["CLAUDE_BIN"] = old


if __name__ == "__main__":
    unittest.main()
