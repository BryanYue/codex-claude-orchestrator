"""Verify the single-open-per-task policy is consistent across Skill, server presentation and the supervisor reference."""
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
SKILL_MD = ROOT / "skills/codex-claude-orchestrator/SKILL.md"
SUPERVISOR_MD = ROOT / "skills/codex-claude-orchestrator/references/supervisor.md"
SERVER_PY = ROOT / "scripts/server.py"
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "skills/codex-claude-orchestrator/scripts"))


class SkillTextPolicyTests(unittest.TestCase):
    def test_open_bookkeeping_replaces_unconditional_first_open(self):
        text = SKILL_MD.read_text(encoding="utf-8")
        self.assertIn("not_requested", text)
        self.assertIn("按打开请求记账", text)
        self.assertIn("queued", text)
        self.assertNotIn("同时用 `open_in_codex` 的 browser target 打开一次。读取打开结果：成功才说已打开", text)

    def test_subsequent_starts_only_give_deep_link(self):
        text = SKILL_MD.read_text(encoding="utf-8")
        self.assertIn("同任务后续 start/details 只给可点击链接和一句状态，不再自动调用 `open_in_codex`", text)

    def test_explicit_reopen_gets_a_new_link_without_new_run(self):
        text = SKILL_MD.read_text(encoding="utf-8")
        self.assertIn("这不新增 Claude run", text)

    def test_record_before_request_and_unknown_context_use_link(self):
        text = SKILL_MD.read_text(encoding="utf-8")
        self.assertIn("在发出 `open_in_codex` 调用前先记为 `requested`", text)
        self.assertIn("无法确认是否请求过时只给链接", text)
        self.assertIn("明确失败改为 `failed`、不自动重试", text)

    def test_native_supervisor_section_fetches_its_own_details_link(self):
        text = SKILL_MD.read_text(encoding="utf-8")
        section = text.split("## 原生 Codex 监督席", 1)[1].split("## 调用 Claude", 1)[0]
        self.assertIn("claude_details(run_id)", section)
        self.assertIn("不直接复用监督席回传、可能短命的 details_url", section)
        self.assertIn("原生监督席永不调用 `open_in_codex`", SKILL_MD.read_text(encoding="utf-8"))


class SupervisorReferencePolicyTests(unittest.TestCase):
    def test_supervisor_never_calls_open_in_codex(self):
        text = SUPERVISOR_MD.read_text(encoding="utf-8")
        self.assertIn("不自行调用 `open_in_codex`", text)
        self.assertIn("不直接复用该 details_url", text)
        self.assertIn("监督席不负责操作主窗口", text)

    def test_supervisor_details_url_is_reference_only(self):
        text = SUPERVISOR_MD.read_text(encoding="utf-8")
        self.assertIn("details_url 与 run_dir，仅供参考", text)


class ServerSourcePolicyTests(unittest.TestCase):
    def test_claude_details_docstring_defers_to_task_bookkeeping(self):
        text = SERVER_PY.read_text(encoding="utf-8")
        self.assertIn("has not yet requested opening it", text)
        self.assertIn("queued counts as requested", text)
        self.assertNotIn("Always give the user a clickable link and open it once with open_in_codex.", text)


class PresentationInstructionBehaviorTests(unittest.IsolatedAsyncioTestCase):
    async def test_decorate_presentation_instruction_matches_request_once_policy(self):
        import server
        with patch.object(server, "viewer", Mock(url=lambda *_: "http://127.0.0.1:1/#token=fixture&run=r1")), \
             patch.object(server, "maintenance_status", lambda: {"state": "up_to_date"}):
            result = await server.decorate({"run_id": "r1", "status": "starting"})
        instruction = result["presentation"]["instruction"]
        self.assertIn("能确认本 Codex 任务尚未请求打开工作台", instruction)
        self.assertIn("先记 requested 再请求打开一次", instruction)
        self.assertIn("已请求（含 queued）、失败或上下文未知只提供可点击链接", instruction)
        self.assertNotIn("并尝试打开一次", instruction)
        self.assertEqual(result["presentation"]["host_open_state"], "unobserved")

    async def test_presentation_instruction_when_details_url_unavailable_never_asks_for_redispatch(self):
        import server
        broken_viewer = Mock()
        broken_viewer.url.side_effect = RuntimeError("viewer unavailable")
        with patch.object(server, "viewer", broken_viewer), \
             patch.object(server, "maintenance_status", lambda: {"state": "up_to_date"}):
            result = await server.decorate({"run_id": "r1", "status": "starting"})
        self.assertIsNone(result["details_url"])
        self.assertEqual(result["presentation"]["state"], "unavailable")
        self.assertIn("不能为显示重新派单", result["presentation"]["instruction"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
