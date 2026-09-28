import json
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import routing  # noqa: E402
import workflow  # noqa: E402


class RoutingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.repo = self.base / "repo"; self.repo.mkdir()
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        self.protocol = self.base / "protocol.md"
        self.protocol.write_text("# Fixture protocol\n")

    def tearDown(self):
        self.temp.cleanup()

    def adopt(self):
        workflow.enable(str(self.repo), str(self.protocol))

    @property
    def routing_path(self):
        return self.repo / routing.ROUTING_RELATIVE

    def test_unadopted_project_returns_unwritten_default(self):
        result = routing.get_policy(str(self.repo))
        self.assertFalse(result["ready"])
        self.assertEqual(result["source"], "default")
        self.assertEqual((result["operation_type"], result["check_status"], result["adoption_status"], result["adopted"]),
                         ("routing", "checked", "unadopted", False))
        self.assertFalse(result["claude_started"])
        self.assertIn("尚未启动 Claude", result["summary"])
        self.assertEqual(result["policy"], routing.DEFAULT_POLICY)
        self.assertFalse(self.routing_path.exists())
        with self.assertRaisesRegex(routing.RoutingError, "ready adopted"):
            routing.set_policy(str(self.repo))

    def test_routing_alias_uses_canonical_identity_and_check_failure_is_blocked(self):
        self.adopt()
        alias = self.base / "repo-alias"
        alias.symlink_to(self.repo, target_is_directory=True)
        selected = routing.select(str(alias), "review")
        self.assertEqual((selected["operation_type"], selected["requested_cwd"], selected["resolved_cwd"]),
                         ("routing", str(alias), str(self.repo.resolve())))
        self.assertEqual(selected["workspace_kind"], "git")
        self.assertFalse(selected["claude_started"])
        self.assertEqual(selected["check_status"], "checked")
        with self.assertRaisesRegex(routing.RoutingError, "symbolic"):
            routing.set_policy(str(alias))

        failed = routing.select(str(self.base / "missing"), "review")
        self.assertEqual((failed["check_status"], failed["check_error"], failed["adoption_status"], failed["adopted"]),
                         ("check_failed", "invalid_path", "unknown", None))
        self.assertEqual((failed["source"], failed["executor"], failed["dispatch_status"]),
                         ("unavailable", None, "blocked"))
        self.assertEqual(failed["workspace_kind"], "unknown")
        self.assertFalse(failed["claude_started"])
        self.assertIn("尚未启动 Claude", failed["summary"])

    def test_auto_task_fit_preferences_are_explainable(self):
        self.adopt()
        routing.set_policy(str(self.repo), codex_weight=50, claude_weight=50)
        expected = {
            "clarification": "codex",
            "small_change": "codex",
            "implementation": "claude",
            "review": "claude",
            "architecture": "codex",
        }
        for kind, executor in expected.items():
            selected = routing.select(str(self.repo), kind)
            self.assertEqual(selected["executor"], executor)
            self.assertEqual(selected["source"], "policy")
            self.assertFalse(selected["execution_authorized"])
            self.assertTrue(selected["rationale"])
            self.assertFalse(selected["needs_workflow_name"])

    def test_manual_explicit_and_zero_weight_behavior(self):
        self.adopt()
        routing.set_policy(str(self.repo), mode="manual", codex_weight=0, claude_weight=100)
        manual = routing.select(str(self.repo), "implementation")
        self.assertEqual((manual["executor"], manual["source"]), ("codex", "policy"))
        overridden = routing.select(str(self.repo), "clarification", "claude_workflow")
        self.assertEqual(overridden["executor"], "claude_workflow")
        self.assertEqual(overridden["source"], "manual_override")
        self.assertTrue(overridden["needs_workflow_name"])
        routing.set_policy(str(self.repo), mode="auto", codex_weight=0, claude_weight=1)
        auto = routing.select(str(self.repo), "clarification")
        self.assertEqual(auto["scores"]["codex"], 0)
        self.assertEqual(auto["executor"], "claude")
        direct = routing.select(str(self.repo), "small_change", "codex")
        self.assertEqual(direct["executor"], "codex")
        with self.assertRaisesRegex(routing.RoutingError, "both be zero"):
            routing.set_policy(str(self.repo), codex_weight=0, claude_weight=0)

    def test_unavailable_claude_does_not_select_excluded_codex(self):
        self.adopt()
        routing.set_policy(str(self.repo), mode="auto", codex_weight=0, claude_weight=100)
        selected = routing.select(str(self.repo), "review", features={"claude_available":False})
        self.assertIsNone(selected["executor"])
        self.assertEqual(selected["dispatch_status"], "blocked")
        explicit = routing.select(str(self.repo), "review", "codex", {"claude_available":False})
        self.assertEqual(explicit["executor"], "codex")
        self.assertEqual(explicit["dispatch_status"], "selected")
        with self.assertRaises(routing.RoutingError):
            routing.set_policy(str(self.repo), mode="")

    def test_named_workflow_capability_is_read_only_and_explicit(self):
        yes = routing.select(str(self.repo), "review", "claude_workflow", {"required_tools":["Workflow","Read"]})
        self.assertEqual(yes["dispatch_status"], "preflight_required")
        no = routing.select(str(self.repo), "implementation", "claude_workflow")
        self.assertEqual(no["dispatch_status"], "blocked")

    def test_existing_permissions_are_preserved(self):
        self.adopt()
        routing.set_policy(str(self.repo))
        self.routing_path.chmod(0o640)
        routing.set_policy(str(self.repo), codex_weight=40, claude_weight=60)
        self.assertEqual(stat.S_IMODE(self.routing_path.stat().st_mode), 0o640)
        self.assertEqual(json.loads(self.routing_path.read_text())["weights"], {"claude": 60, "codex": 40})

    def test_features_change_same_task_kind_without_ignoring_explicit_request(self):
        self.adopt()
        cases = [
            ({"scope_defined": True, "independent": True}, "claude", True),
            ({"context_in_codex": True}, "codex", True),
            ({"latency_sensitive": True}, "codex", True),
            ({"requires_external_tools": True}, "codex", False),
            ({"required_tools": ["WebSearch"]}, "codex", False),
            ({"required_tools": ["Read"]}, "claude", True),
            ({"claude_available": False}, "codex", False),
            ({"scope_defined": False}, "codex", False),
        ]
        for features, executor, eligible in cases:
            with self.subTest(features=features):
                result = routing.select(str(self.repo), "review", features=features)
                self.assertEqual(result["executor"], executor)
                self.assertEqual(result["claude_eligible"], eligible)
        forced = routing.select(str(self.repo), "review", "claude", {"required_tools": ["WebSearch"]})
        self.assertEqual(forced["executor"], "claude")
        self.assertEqual(forced["dispatch_status"], "blocked")
        self.assertTrue(forced["blocking_reasons"])
        self.assertFalse(self.routing_path.exists())

    def test_preset_and_partial_update_preserve_unrequested_weights(self):
        self.adopt()
        routing.set_policy(str(self.repo), codex_weight=43, claude_weight=71)
        manual = routing.set_policy(str(self.repo), preset="manual")["policy"]
        self.assertEqual(manual, {"schema_version": 1, "mode": "manual", "weights": {"codex": 43, "claude": 71}})
        partial = routing.set_policy(str(self.repo), claude_weight=80)["policy"]
        self.assertEqual(partial["mode"], "manual")
        self.assertEqual(partial["weights"], {"codex": 43, "claude": 80})
        preferred = routing.set_policy(str(self.repo), preset="claude_preferred")["policy"]
        self.assertEqual(preferred["mode"], "auto")
        self.assertGreater(preferred["weights"]["claude"], preferred["weights"]["codex"])
        with self.assertRaises(routing.RoutingError):
            routing.set_policy(str(self.repo), mode="auto", preset="manual")

    def test_artifact_workspace_and_invalid_features(self):
        for kind, allowed in (("document_review", True), ("review", True), ("implementation", False), ("data_analysis", False)):
            result = routing.select(str(self.base), kind, "claude")
            self.assertEqual(result["workspace_kind"], "artifacts")
            self.assertEqual(result["claude_eligible"], allowed)
        for features in ({"scope_defined": "yes"}, {"required_tools": "Read"}, {"unrecognized": True}, [1]):
            with self.assertRaises(routing.RoutingError):
                routing.select(str(self.repo), "review", features=features)

    def test_malformed_symlink_and_non_root_set_are_rejected(self):
        self.adopt()
        self.routing_path.write_text("not json")
        with self.assertRaisesRegex(routing.RoutingError, "malformed"):
            routing.get_policy(str(self.repo))
        self.routing_path.unlink()
        other = self.base / "other.json"; other.write_text("{}")
        self.routing_path.symlink_to(other)
        with self.assertRaisesRegex(routing.RoutingError, "symbolic"):
            routing.get_policy(str(self.repo))
        self.routing_path.unlink()
        nested = self.repo / "nested"; nested.mkdir()
        with self.assertRaisesRegex(routing.RoutingError, "explicit adopted project root"):
            routing.set_policy(str(nested))


if __name__ == "__main__":
    unittest.main(verbosity=2)
