"""The public MCP contract, exercised through the real Runtime with a fake Claude CLI."""
import asyncio
import hashlib
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "skills/codex-claude-orchestrator/scripts/tests"))
import harness  # noqa: E402
import server  # noqa: E402
from mcp.server.mcpserver.exceptions import ToolError  # noqa: E402
from viewer import Viewer, read_artifact  # noqa: E402

TOOLS = ["claude_environment", "claude_models", "claude_start", "claude_status", "claude_wait", "claude_result",
         "claude_cancel", "claude_decide", "claude_runs", "claude_cleanup"]
REPORT = "## 需求对照\n" + "- 导出已改为异步，失败时提示原因。\n" * 400


@unittest.skipUnless(harness.Path("/usr/bin/sandbox-exec").is_file(), "copy profile needs macOS sandbox-exec")
class ServerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.fx = harness.Fixture(self)
        self.viewer = Viewer(self.fx.runtime).start()
        self.addCleanup(self.viewer.close)
        self.enterContext(patch.object(server, "runtime", self.fx.runtime))
        self.enterContext(patch.object(server, "viewer", self.viewer))

    async def finished(self, run_id):
        while True:
            answer = await server.claude_wait(run_id, timeout_seconds=5)
            if answer["snapshot"]["status"] != "running":
                return answer["snapshot"]

    async def test_tool_surface_is_ten_tools(self):
        self.assertEqual([tool.name for tool in await server.mcp.list_tools()], TOOLS)

    async def test_legacy_packets_fail_with_the_new_field_names(self):
        with self.assertRaisesRegex(ToolError, "objective -> brief.*role -> kind"):
            await server.claude_start({"task_id": "x", "role": "review", "objective": "o", "cwd": str(self.fx.source)})

    async def test_start_wait_result_decide_through_the_tools(self):
        files = [{"write": f"src/m{index}.py", "text": "x\n"} for index in range(25)]
        self.fx.script(*files, {"deliver": True, "report": REPORT, "result": {"status": "completed", "summary": "完成"}})
        started = await server.claude_start(self.fx.packet())
        self.assertIn("运行中", started["user_summary"])
        self.assertTrue(started["details_url"].startswith("http://127.0.0.1:"))
        snapshot = await self.finished(started["run_id"])
        changes = snapshot["outcome"]["changes"]
        self.assertEqual((changes["file_count"], len(changes["files"]), changes["files_truncated"]), (25, 20, True))
        self.assertNotIn("usage", snapshot["outcome"])
        full = await server.claude_status(started["run_id"], compact=False)
        self.assertEqual(len(full["outcome"]["changes"]["files"]), 25)
        pages, offset = [], 0
        while True:
            page = await server.claude_result(started["run_id"], "report", offset=offset, limit=1001)
            pages.append(page["content"])
            offset = page["next_offset"]
            if page["end_of_artifact"]:
                break
        self.assertGreater(len(pages), 3)
        self.assertEqual("".join(pages), REPORT)
        self.assertEqual(page["sha256"], hashlib.sha256(REPORT.encode()).hexdigest())
        result = await server.claude_result(started["run_id"], "result")
        self.assertEqual(result["content"]["summary"], "完成")
        brief = await server.claude_result(started["run_id"], "brief")
        self.assertIn("把导出改成异步，失败要提示原因", brief["content"])
        decided = await server.claude_decide(started["run_id"], "accepted", "done", "复核通过", ["git apply --check", "pytest"])
        self.assertIn("Codex 裁决 accepted → done", decided["user_summary"])
        listed = await server.claude_runs(task_id="T-1")
        self.assertEqual([row["run_id"] for row in listed["runs"]], [started["run_id"]])
        with self.assertRaisesRegex(ToolError, "unknown artifact"):
            await server.claude_result(started["run_id"], "../registry")

    async def test_viewer_requires_its_token_and_host(self):
        self.fx.script({"deliver": True, "report": "r", "result": {"status": "completed", "summary": "s"}})
        run_id = (await server.claude_start(self.fx.packet()))["run_id"]
        await self.finished(run_id)
        url = self.viewer.url(run_id)
        base, token = url.split("/#token=")[0], url.split("#token=")[1].split("&")[0]

        def get(path, **headers):
            return json.loads(urlopen(Request(base + path, headers=headers), timeout=5).read())
        with self.assertRaises(HTTPError) as denied:
            await asyncio.to_thread(get, "/api/runs")
        self.assertEqual(denied.exception.code, 401)
        rows = await asyncio.to_thread(get, "/api/runs", Authorization="Bearer " + token)
        self.assertEqual(rows["runs"][0]["run_id"], run_id)
        artifact = await asyncio.to_thread(get, f"/api/artifact?run_id={run_id}&name=outcome", Authorization="Bearer " + token)
        self.assertEqual(artifact["content"]["run_outcome"], "ok")
        self.assertEqual(read_artifact(self.fx.runtime, run_id, "patch")["available"], True)


if __name__ == "__main__":
    unittest.main()
