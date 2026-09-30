"""Local CLI admission uses exact advertised flags, never a version allowlist.

Help output establishes syntax only; these fixtures check that the report says
so and that a missing flag or budget control is reported by name.
"""
import sys
import unittest
from unittest.mock import patch
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
import bridge  # noqa: E402


class CapabilityFixtureTests(unittest.TestCase):
    def report(self, version, help_text, groups=None, verify=False):
        def execute(command, *args, **kwargs):
            if command[1:] == ["--version"]:
                return 0, version, "", None
            if command[1:] == ["--help"]:
                return 0, help_text, "", None
            if command[1:] == ["auth", "status", "--json"]:
                return 0, '{"loggedIn":true}', "", None
            self.fail("local check made an unexpected provider request")
        with patch.object(bridge, "check_command", side_effect=execute):
            return bridge.check_environment(Path.cwd(), verify=verify, required_groups=groups,
                                            selection={"path": "/fixture/claude", "source": "fixture"})

    def test_each_task_flag_absent_from_help_blocks_before_dispatch(self):
        self.assertIn("--verbose", bridge.REQUIRED_FLAGS)
        for absent in sorted(bridge.REQUIRED_FLAGS):
            with self.subTest(absent=absent):
                report = self.report("9.9.999", " ".join(flag for flag in bridge.REQUIRED_FLAGS if flag != absent))
                self.assertFalse(report["ready"])
                self.assertEqual(report["status"], "cli_incompatible")
                self.assertEqual(report["cli"]["missing_flags"], [absent])
                self.assertEqual(report["readiness"]["login"]["status"], "reported_logged_in")

    def test_session_persistence_flag_is_required_only_for_verify(self):
        flags = " ".join(bridge.REQUIRED_FLAGS)
        self.assertNotIn("--no-session-persistence", bridge.required_flags({"core", "read_only"}))
        ordinary = self.report("9.9.999", flags)
        self.assertTrue(ordinary["ready"], ordinary)
        verify = self.report("9.9.999", flags, verify=True)
        self.assertFalse(verify["ready"])
        self.assertEqual(verify["status"], "cli_incompatible")
        self.assertEqual(verify["cli"]["missing_flags"], ["--no-session-persistence"])
        self.assertEqual(verify["probe"]["status"], "not_requested")

    def test_unlisted_version_is_admitted_on_advertised_syntax_without_claiming_behavior(self):
        for version in ("2.1.284 (Claude Code)", "9.9.999", "unversioned build"):
            with self.subTest(version=version):
                report = self.report(version, " ".join(bridge.REQUIRED_FLAGS) + " --resume")
                self.assertTrue(report["ready"], report)
                self.assertEqual(report["status"], "local_checks_passed")
                self.assertEqual(report["compatibility"]["status"], "advertised")
                self.assertEqual(report["compatibility"]["evidence"], "cli_help_syntax")
                self.assertFalse(report["compatibility"]["behavior_verified"])
                self.assertEqual(report["cli"]["version_policy"], "diagnostic_only")
                self.assertNotIn("profile", report["cli"])
        self.assertEqual(self.report("unversioned build", " ".join(bridge.REQUIRED_FLAGS))["cli"]["version"], "unrecognized")

    def test_absent_optional_resume_flag_does_not_block_fresh_review(self):
        flags = " ".join(bridge.REQUIRED_FLAGS)
        fresh = self.report("2.1.278", flags)
        resume = self.report("2.1.278", flags, ["core", "read_only", "resume"])
        self.assertTrue(fresh["ready"])
        self.assertNotIn("resume", fresh["compatibility"]["available_groups"])
        self.assertFalse(resume["ready"])
        self.assertEqual(resume["status"], "cli_incompatible")
        self.assertEqual(resume["cli"]["missing_flags"], ["--resume"])
        self.assertIn("--resume", resume["action"])
        self.assertEqual(resume["readiness"]["login"]["status"], "reported_logged_in")

    def test_flags_match_exact_tokens_not_substrings(self):
        flags = [flag for flag in bridge.REQUIRED_FLAGS if flag not in {"-p", "--tools"}]
        help_text = " ".join(flags) + " --print --toolset --tools-list"
        report = self.report("2.1.290", help_text)
        self.assertFalse(report["ready"])
        self.assertEqual(report["cli"]["missing_flags"], ["--tools", "-p"])
        self.assertEqual(report["compatibility"]["status"], "unavailable")

    def test_budget_controls_come_only_from_advertised_flags(self):
        flags = " ".join(bridge.REQUIRED_FLAGS)
        without = self.report("2.1.276", flags)
        self.assertEqual(without["cli"]["capabilities"], {})
        self.assertEqual(without["cli"]["unavailable_capabilities"], ["max_budget_usd", "max_turns"])
        with_budget = self.report("9.9.999", flags + " --max-turns --max-budget-usd")
        self.assertEqual(with_budget["cli"]["capabilities"]["max_turns"]["flag"], "--max-turns")
        self.assertEqual(with_budget["cli"]["capabilities"]["max_turns"]["evidence"], "cli_help_syntax")
        self.assertFalse(with_budget["cli"]["capabilities"]["max_turns"]["limit_exhaustion_verified"])


if __name__ == "__main__":
    unittest.main()
