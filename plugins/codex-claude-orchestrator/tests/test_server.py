"""Check the public tool contract without making a provider request."""
import inspect
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import server  # noqa: E402


class ServerContractTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name) / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        subprocess.run(["git", "-C", str(self.repo), "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                        "commit", "--allow-empty", "-qm", "fixture"], check=True)
        self.spec = Path(self.temp.name) / "requirements.md"
        self.spec.write_text("Review fixture requirements\n")
        self.runtime = Mock()
        self.runtime.start.side_effect = self.dispatch
        self.enterContext(patch.object(server, "runtime", self.runtime))
        self.enterContext(patch.object(server, "viewer", Mock(url=lambda *_: "http://127.0.0.1/fixture")))

    def packet(self, **extra):
        return {"task_id": "server-fixture", "revision": 1, "role": "review", "cwd": str(self.repo),
                "objective": "Review the fixture", "requirement_sources": [str(self.spec)],
                "constraints": ["bounded"], "acceptance": ["complete report"], "owned_files": [],
                "protected_files": [], "model": "sonnet", "effort": "low", **extra}

    def dispatch(self, packet, **_):
        # Use the real validator to expose the mode/provenance the run records.
        normalized = server.bridge.validate_packet(packet)
        return {"run_id": "run-fixture", "status": "starting", "claude_started": False,
                **{key: normalized[key] for key in ("role", "review_mode", "review_scope", "request_provenance")}}

    async def test_fresh_original_request_selects_isolated_without_mutating_caller(self):
        packet = self.packet(user_request="Review this fixture and run its tests")
        created = await server.claude_start(packet)
        self.assertEqual(created["review_mode"], "isolated")
        self.assertEqual(created["request_provenance"], "user_request")
        self.assertNotIn("warnings", created)
        self.assertNotIn("review_mode", packet)
        self.assertIsNone(self.runtime.start.call_args.kwargs["timeout"])

    async def test_legacy_review_warns_in_compact_dispatch_response(self):
        created = await server.claude_start(self.packet(), compact=True)
        self.assertEqual(created["review_mode"], "strict")
        self.assertEqual(created["request_provenance"], "legacy_unspecified")
        self.assertIn("未提供 user_request", created["user_summary"])
        self.assertIn("strict", created["warnings"][0])

    async def test_resume_omitted_mode_is_left_for_runtime_frozen_inheritance(self):
        packet = self.packet(user_request="Review this fixture", revision=2)
        await server.claude_start(packet, resume_run_id="run-prior")
        sent = self.runtime.start.call_args.args[0]
        self.assertNotIn("review_mode", sent)
        self.assertEqual(self.runtime.start.call_args.kwargs["resume_run_id"], "run-prior")

    async def test_explicit_deadline_survives_and_invalid_deadline_cannot_dispatch(self):
        await server.claude_start(self.packet(user_request="Review", review_mode="strict"), timeout_seconds=42)
        self.assertEqual(self.runtime.start.call_args.kwargs["timeout"], 42)
        self.runtime.start.reset_mock()
        for deadline in (True, False, 0, 14401, float("nan"), float("inf"), "300"):
            with self.subTest(deadline=deadline), self.assertRaisesRegex(server.ToolError, "timeout_seconds"):
                await server.claude_start(self.packet(), timeout_seconds=deadline)
        self.runtime.start.assert_not_called()

    async def test_retired_update_signature_and_cancel_history(self):
        self.assertEqual(set(inspect.signature(server.claude_cli_update).parameters), {"action", "job_id"})
        with patch.object(server.cli_validation, "cancel", return_value={"cancel_requested": True}) as cancel:
            value = await server.claude_cli_update("cancel", job_id="historical-job")
            self.assertTrue(value["cancel_requested"])
            cancel.assert_called_once_with("historical-job")
        with self.assertRaisesRegex(server.ToolError, "job_id only"):
            await server.claude_cli_update("cancel")
        with self.assertRaises(TypeError):
            await server.claude_cli_update("prepare", candidate_path="unused")

    async def test_saved_workflow_inventory_marks_only_legacy_role_deprecated(self):
        with patch.object(server.named_workflow, "inventory", return_value=[]) as inventory:
            result = await server.claude_saved_workflows(str(self.repo))
        inventory.assert_called_once_with(str(self.repo))
        self.assertEqual(result["supported_role"], "review")
        self.assertEqual(result["suggested_review_mode"], "isolated")
        self.assertTrue(result["legacy_role_deprecated"])
        self.assertFalse(result["legacy_resume_supported"])


if __name__ == "__main__":
    unittest.main()
