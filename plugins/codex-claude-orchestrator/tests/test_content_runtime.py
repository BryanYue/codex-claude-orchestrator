"""Runtime pinning of reviewed coordination content: fresh pins, resume freezing, tamper refusal, MCP tools."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

import test_runtime as fixtures
from test_content_store import Fetch, write_source

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import content_store  # noqa: E402
from content_store import ContentStore  # noqa: E402

CORRECTION = {"finding_id": "f-1", "kind": "local", "reason": "fix", "attempt": 1}


class ContentRuntimeTests(unittest.TestCase):
    tearDown = fixtures.RuntimeTests.tearDown
    packet = fixtures.RuntimeTests.packet
    finish = fixtures.RuntimeTests.finish

    def setUp(self):
        fixtures.RuntimeTests.setUp(self)
        self.bundled = self.root / "bundled"
        self.bundled_digest = write_source(self.bundled, {"guide.md": "# bundled\n", "protocol.md": "protocol\n"}, version="0.1")
        self.runtime.content = ContentStore(self.runtime.state_root / "content", fetch=Fetch({}), bundled_dir=self.bundled)

    def approve(self, name, guide, version):
        directory = self.root / name
        digest = write_source(directory, {"guide.md": guide, "protocol.md": "protocol\n"}, version=version)
        self.assertEqual(self.runtime.content.check(local_dir=str(directory))["status"], "pending_review")
        self.runtime.content.review(digest, "approve", "no permission or acceptance change", [f"{name}/guide.md"])
        return digest

    def run_to_end(self, packet, resume_run_id=None):
        final = self.finish(self.runtime.start(packet, resume_run_id=resume_run_id, expected_content_digest=(self.runtime._registry()["runs"][resume_run_id]["content_binding"]["digest"] if resume_run_id else self.runtime.content.read()["digest"]))["run_id"])["snapshot"]
        self.assertEqual(final["status"], "reported", final.get("summary"))
        return final, Path(final["run_dir"])

    def test_fresh_run_pins_content_as_run_evidence_outside_the_packet(self):
        final, run_dir = self.run_to_end(self.packet())
        self.assertEqual(final["content_binding"]["mode"], "bundled")
        self.assertEqual(final["content_binding"]["digest"], self.bundled_digest)
        binding = json.loads((run_dir / "content-binding.json").read_text())
        content_store.verify_binding(binding)
        pinned = self.runtime.packets_root / f"{final['run_id']}.content.json"
        self.assertEqual(self.runtime._file_sha256(pinned), final["content_binding_sha256"])
        self.assertEqual(json.loads(pinned.read_text()), binding)
        self.assertEqual(json.loads((run_dir / "receipt.json").read_text())["content_binding"]["digest"], self.bundled_digest)
        result = json.loads((run_dir / "result.json").read_text())
        self.assertEqual(result["content_binding"]["digest"], self.bundled_digest)
        self.assertNotIn("content_binding", json.loads((run_dir / "packet.json").read_text()),
                         "the guide identity is Codex coordination evidence, not a Claude instruction field")
        # The fixture CLI reports a partial modelUsage entry: known values stay, missing ones stay unknown.
        self.assertEqual(result["model_usage"], {"provider-model": {"inputTokens": 1}})
        self.assertEqual(result["usage_summary"], {"input_tokens": 1})
        cli = result["usage_report"]["cli_session"]
        self.assertEqual(cli["models"][0]["tokens"]["input_tokens"], 1)
        self.assertIsNone(cli["models"][0]["tokens"]["output_tokens"])
        self.assertIsNone(cli["total_cost_usd"])
        self.assertFalse(cli["complete"])
        self.assertEqual(result["usage_report"]["result_events"], 1)

    def test_resume_keeps_the_original_pin_after_activation_and_disable(self):
        v2 = self.approve("v2", "# guide v2\n", "2.0")
        first, first_dir = self.run_to_end(self.packet(1))
        self.assertEqual((first["content_binding"]["mode"], first["content_binding"]["digest"]), ("active", v2))
        v3 = self.approve("v3", "# guide v3\n", "3.0")
        self.runtime.content.switch("disable", "stop dynamic content for new tasks")
        resumed_packet = {**self.packet(2), "correction": CORRECTION}
        resumed, resumed_dir = self.run_to_end(resumed_packet, resume_run_id=first["run_id"])
        self.assertEqual(resumed["content_binding"]["digest"], v2)
        self.assertEqual(json.loads((resumed_dir / "content-binding.json").read_text()),
                         json.loads((first_dir / "content-binding.json").read_text()))
        self.assertTrue(json.loads((resumed_dir / "result.json").read_text())["usage_report"]["cli_session"]["may_include_prior_turns"])
        fresh, _ = self.run_to_end(self.packet(3))
        self.assertEqual((fresh["content_binding"]["mode"], fresh["content_binding"]["digest"]), ("bundled", self.bundled_digest))
        self.runtime.content.switch("rollback", "re-enable v3", v3)
        newest, _ = self.run_to_end(self.packet(4))
        self.assertEqual(newest["content_binding"]["digest"], v3)

    def test_dispatch_refuses_changed_or_unread_dynamic_guide(self):
        v2 = self.approve("v2", "# guide v2\n", "2.0")
        read = self.runtime.content.read()
        v3 = self.approve("v3", "# guide v3\n", "3.0")
        self.assertEqual(self.runtime.content.read(digest=v2)["text"], "# guide v2\n")
        for expected in (None, read["digest"]):
            with self.subTest(expected=expected), self.assertRaises(RuntimeError):
                self.runtime.start(self.packet(), expected_content_digest=expected)
            self.assertEqual(self.runtime._registry().get("runs", {}), {})
            self.assertEqual(list(self.runtime.runs_root.iterdir()), [])
        final, _ = self.run_to_end(self.packet())
        self.assertEqual(final["content_binding"]["digest"], v3)
        self.runtime.content.switch("disable", "fixture")
        with self.assertRaises(RuntimeError):
            self.runtime.start(self.packet(2), expected_content_digest=v3)
        with self.assertRaises(RuntimeError):
            self.runtime.start({**self.packet(2), "correction": CORRECTION}, resume_run_id=final["run_id"],
                               expected_content_digest=self.bundled_digest)

    def test_nul_cli_arguments_are_rejected_without_a_run_or_lane_marker(self):
        from unittest.mock import patch
        import bridge
        for field in ("model", "effort"):
            packet = {**self.packet(), field: "invalid\x00value"}
            with patch.object(bridge, "create_cli_descriptor") as descriptor:
                with self.assertRaisesRegex(RuntimeError, "NUL"):
                    self.runtime.start(packet)
                descriptor.assert_not_called()
            self.assertEqual(self.runtime._registry().get("runs", {}), {})
            self.assertEqual(list(self.runtime.runs_root.iterdir()), [])
            self.assertEqual(bridge.unknown_markers(bridge.lane_identity(self.repo)), [])

    def test_client_cannot_supply_a_reviewed_binding(self):
        forged = {"schema_version": 1, "digest": "0" * 64, "mode": "active", "review": {"decision": "approved"}}
        with self.assertRaises(RuntimeError):
            self.runtime.start({**self.packet(), "content_binding": forged})
        self.assertEqual(self.runtime._registry().get("runs", {}), {})

    def test_tampered_active_snapshot_refuses_dispatch_before_any_run_directory(self):
        v2 = self.approve("v2", "# guide v2\n", "2.0")
        (self.runtime.content.snapshots / v2 / "guide.md").write_text("# edited cache\n")
        with self.assertRaises(RuntimeError) as raised:
            self.runtime.start(self.packet(), expected_content_digest=v2)
        self.assertIn("failed verification", str(raised.exception))
        self.assertEqual(list(self.runtime.runs_root.iterdir()), [])
        self.assertEqual(self.runtime._registry().get("runs", {}), {})

    def test_tampered_prior_binding_refuses_resume(self):
        self.approve("v2", "# guide v2\n", "2.0")
        first, first_dir = self.run_to_end(self.packet(1))
        record = first_dir / "content-binding.json"
        original = record.read_text()
        record.write_text(json.dumps({**json.loads(original), "content_version": "9.9"}))
        with self.assertRaises(RuntimeError):
            self.runtime.start({**self.packet(2), "correction": CORRECTION}, resume_run_id=first["run_id"])
        record.write_text(original)
        (self.runtime.content.snapshots / first["content_binding"]["digest"] / "protocol.md").write_text("changed\n")
        with self.assertRaises(RuntimeError):
            self.runtime.start({**self.packet(2), "correction": CORRECTION}, resume_run_id=first["run_id"])


class ContentMcpStdioTests(unittest.IsolatedAsyncioTestCase):
    async def test_actual_stdio_content_tools(self):
        try:
            from mcp import ClientSession
            from mcp.client.stdio import stdio_client, StdioServerParameters
        except ImportError:
            self.skipTest("MCP SDK is required for the stdio boundary test")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            source = root / "source"
            digest = write_source(source, {"guide.md": "# stdio guide\n", "protocol.md": "protocol\n"}, version="5.0")
            config = json.loads((ROOT / ".mcp.json").read_text())["mcpServers"]["claude-orchestrator"]
            launch_cwd = (ROOT / config["cwd"]).resolve()
            params = StdioServerParameters(command=str(launch_cwd / config["command"]), args=config["args"], cwd=launch_cwd,
                env={**os.environ, "CLAUDE_ORCHESTRATOR_STATE_DIR": str(root / "state"),
                     "CLAUDE_ORCHESTRATOR_CLI_ROOT": str(root / "cli-store"), "UV_CACHE_DIR": str(root / "uv-cache"),
                     "CLAUDE_ORCHESTRATOR_ENV_DIR": sys.prefix})
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as client:
                    await client.initialize()
                    tools = {tool.name: tool for tool in (await client.list_tools()).tools}
                    for name in ("claude_content_status", "claude_content_read"):
                        self.assertTrue(tools[name].annotations.read_only_hint, name)
                    for name in ("claude_content_check", "claude_content_review", "claude_content_switch"):
                        self.assertFalse(tools[name].annotations.read_only_hint, name)

                    async def call(name, args):
                        result = await client.call_tool(name, args)
                        self.assertFalse(result.is_error, str(result.content))
                        return result.structured_content or json.loads(result.content[0].text)

                    status = await call("claude_content_status", {})
                    self.assertEqual(status["effective"]["mode"], "bundled")
                    self.assertEqual(status["source"]["repository"], "BryanYue/codex-claude-orchestrator")
                    checked = await call("claude_content_check", {"local_dir": str(source)})
                    self.assertEqual((checked["status"], checked["digest"]), ("pending_review", digest))
                    self.assertEqual((await call("claude_content_read", {"digest": digest}))["trust"], "untrusted_candidate")
                    wrong = await client.call_tool("claude_content_review", {"digest": "f" * 64, "decision": "approve",
                                                                             "reason": "x", "evidence": ["y"]})
                    self.assertTrue(wrong.is_error)
                    reviewed = await call("claude_content_review", {"digest": digest, "decision": "approve",
                                                                    "reason": "fixture reviewed", "evidence": ["source/guide.md"]})
                    self.assertEqual(reviewed["status"], "activated")
                    self.assertEqual((await call("claude_content_read", {}))["text"], "# stdio guide\n")
                    disabled = await call("claude_content_switch", {"action": "disable", "reason": "fixture"})
                    self.assertEqual(disabled["effective"]["mode"], "bundled")
                    enabled = await call("claude_content_switch", {"action": "rollback", "reason": "fixture", "digest": digest})
                    self.assertEqual(enabled["effective"]["digest"], digest)
            self.assertTrue((root / "state" / "content" / "snapshots" / digest).is_dir())


if __name__ == "__main__":
    unittest.main(verbosity=2)
