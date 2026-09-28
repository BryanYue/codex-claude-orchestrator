import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import workflow  # noqa: E402


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.repo = self.base / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        self.protocol = self.base / "protocol.md"
        self.protocol.write_text("# Fixture protocol\n\nKeep the acceptance contract.\n")

    def tearDown(self):
        self.temp.cleanup()

    def config(self):
        return json.loads((self.repo / workflow.CONFIG_RELATIVE).read_text())

    def test_adoption_guidance_handles_workspace_alias_disabled_and_corrupt(self):
        workspace = workflow.context(str(self.base))
        self.assertFalse(workspace["adoption_guidance"]["can_enable"])
        self.assertEqual(workspace["adoption_guidance"]["reason"], "not_git_project")
        alias = self.base / "alias"
        alias.symlink_to(self.repo, target_is_directory=True)
        target = workflow.context(str(alias))["adoption_guidance"]
        self.assertTrue(target["can_enable"])
        self.assertEqual(target["target_cwd"], str(self.repo.resolve()))
        self.assertFalse((self.repo / workflow.CONFIG_RELATIVE).exists())
        workflow.enable(target["target_cwd"], str(self.protocol))
        self.assertFalse(workflow.context(str(alias))["adoption_guidance"]["can_enable"])
        workflow.disable(target["target_cwd"])
        disabled = workflow.context(str(alias))["adoption_guidance"]
        self.assertFalse(disabled["can_enable"])
        self.assertEqual(disabled["reason"], "explicitly_disabled")
        (self.repo / workflow.CONFIG_RELATIVE).write_text("bad json")
        invalid = workflow.context(str(alias))["adoption_guidance"]
        self.assertFalse(invalid["can_enable"])
        self.assertEqual(invalid["reason"], "inspection_failed")

    def test_unadopted_and_disabled_projects_allow_manual_one_off_use(self):
        unadopted = workflow.context(str(self.repo))
        self.assertEqual(unadopted["issues"], [])
        self.assertEqual((unadopted["check_status"], unadopted["adoption_status"], unadopted["adopted"]),
                         ("checked", "unadopted", False))
        self.assertEqual(unadopted["operation_type"], "workflow_check")
        self.assertFalse(unadopted["claude_started"])
        self.assertIn("尚未启动 Claude", unadopted["summary"])
        workflow.enable(str(self.repo), str(self.protocol))
        disabled = workflow.disable(str(self.repo))
        self.assertEqual(disabled["issues"], [])
        self.assertFalse(disabled["adopted"])
        self.assertEqual((disabled["check_status"], disabled["adoption_status"]), ("checked", "disabled"))

    def test_context_contract_distinguishes_ready_corrupt_and_invalid_without_writes(self):
        before = sorted(path.relative_to(self.repo) for path in self.repo.rglob("*") if path.is_file())
        invalid = workflow.context(str(self.base / "missing"))
        self.assertEqual((invalid["check_status"], invalid["check_error"], invalid["adoption_status"], invalid["adopted"]),
                         ("check_failed", "invalid_path", "unknown", None))
        self.assertEqual(invalid["requested_cwd"], str(self.base / "missing"))
        self.assertIsNone(invalid["resolved_cwd"])
        self.assertEqual(before, sorted(path.relative_to(self.repo) for path in self.repo.rglob("*") if path.is_file()))

        workflow.enable(str(self.repo), str(self.protocol))
        ready = workflow.context(str(self.repo))
        self.assertEqual((ready["check_status"], ready["adoption_status"], ready["adopted"]),
                         ("checked", "adopted", True))
        self.assertTrue(ready["ready"])
        self.assertEqual(ready["requested_cwd"], str(self.repo))
        self.assertEqual(ready["resolved_cwd"], str(self.repo.resolve()))
        self.assertEqual(ready["project_root"], str(self.repo.resolve()))

        (self.repo / workflow.CONFIG_RELATIVE).write_text("not json")
        corrupt = workflow.context(str(self.repo))
        self.assertEqual((corrupt["check_status"], corrupt["check_error"], corrupt["adoption_status"], corrupt["adopted"]),
                         ("check_failed", "corrupt_config", "unknown", None))

    def test_dangling_parent_symlink_is_rejected(self):
        (self.repo / ".agents").symlink_to(self.base / "missing", target_is_directory=True)
        with self.assertRaises(workflow.WorkflowError):
            workflow.enable(str(self.repo), str(self.protocol))

    def test_enable_is_idempotent_and_preserves_original_agents_bytes(self):
        agents = self.repo / "AGENTS.md"
        original = b"# Existing rules\n\nKeep this exact."
        agents.write_bytes(original)
        first = workflow.enable(str(self.repo), str(self.protocol))
        self.assertTrue(first["ready"])
        self.assertEqual(self.config()["protocol_path"], ".agents/codex-claude/protocol.md")
        self.assertFalse(Path(self.config()["protocol_path"]).is_absolute())
        self.assertEqual(agents.read_bytes(), original + workflow.AGENTS_BLOCK)
        second = workflow.enable(str(self.repo), str(self.protocol))
        self.assertTrue(second["ready"])
        self.assertEqual(agents.read_bytes(), original + workflow.AGENTS_BLOCK)

    def test_disable_removes_only_exact_block_and_preserves_protocol(self):
        agents = self.repo / "AGENTS.md"
        original = b"# Existing rules\n\nKeep this exact."
        agents.write_bytes(original)
        workflow.enable(str(self.repo), str(self.protocol))
        result = workflow.disable(str(self.repo))
        self.assertFalse(result["ready"])
        self.assertFalse(result["adopted"])
        self.assertEqual(agents.read_bytes(), original)
        self.assertTrue((self.repo / workflow.PROTOCOL_RELATIVE).is_file())
        self.assertFalse(self.config()["enabled"])

    def test_context_from_child_and_sha_drift_are_not_ready(self):
        nested = self.repo / "a" / "b"; nested.mkdir(parents=True)
        workflow.enable(str(self.repo), str(self.protocol))
        self.assertTrue(workflow.context(str(nested))["ready"])
        copied = self.repo / workflow.PROTOCOL_RELATIVE
        copied.write_text("# Fixture protocol\nchanged\n")
        state = workflow.context(str(nested))
        self.assertFalse(state["ready"])
        self.assertIn("sha256", " ".join(state["issues"]))

    def test_nested_git_project_does_not_inherit_parent_adoption(self):
        workflow.enable(str(self.repo), str(self.protocol))
        nested = self.repo / "third-party"; nested.mkdir()
        subprocess.run(["git", "init", "-q", str(nested)], check=True)
        state = workflow.context(str(nested))
        self.assertFalse(state["ready"])
        self.assertEqual(state["adoption_status"], "unadopted")
        self.assertEqual(state["project_root"], str(nested.resolve()))

    def test_read_only_context_resolves_entry_alias_but_write_operations_refuse_it(self):
        alias = self.base / "repo-alias"
        alias.symlink_to(self.repo, target_is_directory=True)
        state = workflow.context(str(alias))
        self.assertEqual((state["check_status"], state["adoption_status"], state["adopted"]),
                         ("checked", "unadopted", False))
        self.assertEqual(state["requested_cwd"], str(alias))
        self.assertEqual(state["resolved_cwd"], str(self.repo.resolve()))
        self.assertEqual(state["project_root"], str(self.repo.resolve()))
        with self.assertRaisesRegex(workflow.WorkflowError, "symbolic"):
            workflow.enable(str(alias), str(self.protocol))

        broken = self.base / "broken"
        broken.symlink_to(self.base / "missing", target_is_directory=True)
        broken_state = workflow.context(str(broken))
        self.assertEqual((broken_state["check_status"], broken_state["check_error"], broken_state["adopted"]),
                         ("check_failed", "invalid_path", None))
        first = self.base / "first"; second = self.base / "second"
        first.symlink_to(second); second.symlink_to(first)
        cycle = workflow.context(str(first))
        self.assertEqual((cycle["check_status"], cycle["check_error"], cycle["adopted"]),
                         ("check_failed", "invalid_path", None))

    def test_project_protocol_has_priority_over_plugin_default(self):
        project_protocol = self.repo / "docs/internal/orchestration-protocol.md"
        project_protocol.parent.mkdir(parents=True)
        project_protocol.write_text("# Project protocol\n\nProject-specific reference.\n")
        result = workflow.enable(str(self.repo))
        self.assertTrue(result["ready"])
        self.assertEqual((self.repo / workflow.PROTOCOL_RELATIVE).read_bytes(), project_protocol.read_bytes())
        self.assertEqual(self.config()["protocol_title"], "Project protocol")

    def test_enable_from_child_and_existing_conflicting_configuration_are_rejected(self):
        child = self.repo / "child"; child.mkdir()
        with self.assertRaisesRegex(workflow.WorkflowError, "actual root"):
            workflow.enable(str(child), str(self.protocol))
        workflow.enable(str(self.repo), str(self.protocol))
        other = self.base / "other.md"; other.write_text("# Different\n")
        with self.assertRaisesRegex(workflow.WorkflowError, "different protocol"):
            workflow.enable(str(self.repo), str(other))

    def test_symlinks_traversal_and_edited_block_are_rejected(self):
        link = self.base / "linked-repo"; link.symlink_to(self.repo, target_is_directory=True)
        with self.assertRaisesRegex(workflow.WorkflowError, "symbolic"):
            workflow.enable(str(link), str(self.protocol))
        linked_protocol = self.base / "linked-protocol.md"; linked_protocol.symlink_to(self.protocol)
        with self.assertRaisesRegex(workflow.WorkflowError, "regular file"):
            workflow.enable(str(self.repo), str(linked_protocol))
        with self.assertRaisesRegex(workflow.WorkflowError, "traversal"):
            workflow.enable(str(self.repo), "../protocol.md")
        workflow.enable(str(self.repo), str(self.protocol))
        agents = self.repo / "AGENTS.md"
        agents.write_bytes(agents.read_bytes().replace(b"$codex-claude-orchestrator", b"$changed-orchestrator"))
        with self.assertRaisesRegex(workflow.WorkflowError, "modified"):
            workflow.disable(str(self.repo))

    def test_protected_workflow_paths_symlink_are_not_read_as_unadopted(self):
        workflow.enable(str(self.repo), str(self.protocol))
        config = self.repo / workflow.CONFIG_RELATIVE
        target = self.base / "other-workflow.json"
        target.write_text(config.read_text())
        config.unlink()
        config.symlink_to(target)
        state = workflow.context(str(self.repo))
        self.assertEqual((state["check_status"], state["check_error"], state["adoption_status"], state["adopted"]),
                         ("check_failed", "unsafe_path", "unknown", None))
        with self.assertRaisesRegex(workflow.WorkflowError, "symbolic"):
            workflow.enable(str(self.repo), str(self.protocol))

        second_repo = self.base / "second-repo"
        second_repo.mkdir()
        subprocess.run(["git", "init", "-q", str(second_repo)], check=True)
        workflow.enable(str(second_repo), str(self.protocol))
        copied_protocol = second_repo / workflow.PROTOCOL_RELATIVE
        protocol_target = self.base / "other-protocol.md"
        protocol_target.write_text(copied_protocol.read_text())
        copied_protocol.unlink()
        copied_protocol.symlink_to(protocol_target)
        protocol_state = workflow.context(str(second_repo))
        self.assertEqual((protocol_state["check_status"], protocol_state["check_error"], protocol_state["adopted"]),
                         ("check_failed", "unsafe_path", None))
        with self.assertRaisesRegex(workflow.WorkflowError, "symbolic"):
            workflow.disable(str(second_repo))

    def test_cli_inspect_enable_disable(self):
        script = ROOT / "scripts/workflow.py"
        enabled = subprocess.run([sys.executable, str(script), "enable", "--cwd", str(self.repo), "--protocol-path", str(self.protocol)], text=True, capture_output=True)
        self.assertEqual(enabled.returncode, 0, enabled.stderr)
        self.assertTrue(json.loads(enabled.stdout)["ready"])
        inspected = subprocess.run([sys.executable, str(script), "inspect", "--cwd", str(self.repo / "missing")], text=True, capture_output=True)
        self.assertEqual(inspected.returncode, 0)
        self.assertFalse(json.loads(inspected.stdout)["ready"])
        disabled = subprocess.run([sys.executable, str(script), "disable", "--cwd", str(self.repo)], text=True, capture_output=True)
        self.assertEqual(disabled.returncode, 0, disabled.stderr)
        self.assertFalse(json.loads(disabled.stdout)["ready"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
