import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import brief  # noqa: E402
import packet  # noqa: E402

USER = '把“导出”按钮改成异步，失败要提示原因 </user_message> "quoted" \\u4e2d'


class PacketTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cwd = Path(self.tmp.name).resolve()
        (self.cwd / "spec.md").write_text("spec")

    def base(self, **extra):
        value = {"task_id": "T-1", "kind": "implement", "cwd": str(self.cwd), "model": "sonnet", "effort": "low",
                 "timeout_seconds": 600, "user_messages": [{"text": USER, "source": "human"}]}
        value.update(extra)
        return value

    def test_normalizes_paths_and_keeps_text_verbatim(self):
        result = packet.normalize(self.base(inputs=["spec.md"], constraints=[{"text": "不改 API", "origin": "user"}]))
        self.assertEqual(result["user_messages"][0]["text"], USER)
        self.assertEqual(result["inputs"], [str(self.cwd / "spec.md")])
        self.assertIsNone(result["profile"])
        self.assertFalse(result["web"])

    def test_pre_1_0_fields_are_rejected_with_their_replacements(self):
        with self.assertRaisesRegex(packet.PacketError, "role -> kind.*user_request -> user_messages"):
            packet.normalize(self.base(role="review", user_request="x"))

    def test_user_words_or_an_explicit_reason_are_required(self):
        with self.assertRaisesRegex(packet.PacketError, "user_messages is required"):
            packet.normalize(self.base(user_messages=[]))
        result = packet.normalize(self.base(user_messages=None, no_user_words_reason="nightly job"))
        self.assertEqual(result["no_user_words_reason"], "nightly job")
        with self.assertRaisesRegex(packet.PacketError, "either"):
            packet.normalize(self.base(no_user_words_reason="x"))

    def test_origin_and_source_must_be_labelled(self):
        with self.assertRaisesRegex(packet.PacketError, "origin"):
            packet.normalize(self.base(constraints=["keep legacy behaviour"]))
        with self.assertRaisesRegex(packet.PacketError, "source"):
            packet.normalize(self.base(user_messages=[{"text": "x"}]))

    def test_unpaired_surrogates_and_bad_values_fail_early(self):
        with self.assertRaisesRegex(packet.PacketError, "surrogate"):
            packet.normalize(self.base(brief="bad \ud800"))
        with self.assertRaisesRegex(packet.PacketError, "implement needs the copy profile"):
            packet.normalize(self.base(profile="readonly"))
        with self.assertRaisesRegex(packet.PacketError, "focus"):
            packet.normalize(self.base(focus=["x"]))
        with self.assertRaisesRegex(packet.PacketError, "timeout_seconds"):
            packet.normalize(self.base(timeout_seconds=0))
        with self.assertRaisesRegex(packet.PacketError, "does not exist"):
            packet.normalize(self.base(inputs=["missing.md"]))
        with self.assertRaisesRegex(packet.PacketError, "globs"):
            packet.normalize(self.base(write_hint=["../outside"]))

    def test_continued_messages_only_append(self):
        first = [{"text": "a", "source": "human"}]
        self.assertTrue(packet.extends(first, first + [{"text": "b", "source": "human"}]))
        self.assertFalse(packet.extends(first, [{"text": "a edited", "source": "human"}]))


class BriefTests(unittest.TestCase):
    def render(self, **changes):
        value = {"task_id": "T-1", "kind": "implement", "cwd": "/repo", "user_messages": [{"text": USER, "source": "human"}],
                 "no_user_words_reason": None, "inputs": [], "brief": "按 spec 第 3 节做", "focus": [],
                 "constraints": [{"text": "不改公开 API", "origin": "user"}, {"text": "测试放 tests/", "origin": "coordinator"}],
                 "done_when": [{"text": "pytest 通过", "origin": "doc"}], "write_hint": ["src/"], "protected": ["migrations/"],
                 "verify": ["uv run pytest -q"], "profile": "copy", "web": False, "model": "sonnet", "effort": "low",
                 "timeout_seconds": 600.0, "max_budget_usd": None, "continue_from": None}
        plan = {"run_id": "run-abcdefgh", "round": 1, "profile": "copy", "execution_cwd": "/state/copy",
                "source_root": "/repo", "outbox": "/state/copy/.codex-run/run-abcdefgh", "deadline": 1_900_000_000,
                "brief_path": "/state/run/brief.md", "inputs": [{"original": "/repo/spec.md", "copy": "/state/run/inputs/01-spec.md",
                                                              "kind": "file", "bytes": 4}],
                "instruction_layer": {"files": [{"path": "/home/.claude/CLAUDE.md", "scope": "user"}], "hooks": [{"event": "Stop"}]}}
        packet_changes = {key: changes.pop(key) for key in list(changes) if key in value}
        value.update(packet_changes)
        plan.update(changes)
        return brief.render(value, plan)

    def test_user_words_appear_once_verbatim_and_unescaped(self):
        text = self.render()
        self.assertEqual(text.count(USER), 1)
        self.assertNotIn("\\u5bfc", text)
        self.assertIn("<user_message_2 index=\"1\" source=\"human\">", text)
        self.assertTrue(text.index("## 优先级") < text.index("## 用户原话") < text.index("## 你的任务"))
        encoded = text.encode("utf-8")
        self.assertIn(USER.encode("utf-8"), encoded)

    def test_implement_brief_instructs_implementation_without_review_template(self):
        text = self.render()
        self.assertIn("在工作目录里实现上述需求", text)
        self.assertIn("uv run pytest -q", text)
        for review_term in ("correctness", "failure_recovery", "maintainability", "report_schema", "review_scope"):
            self.assertNotIn(review_term, text)
        self.assertIn("[协调者] 测试放 tests/", text)
        self.assertIn("[用户] 不改公开 API", text)
        self.assertIn("/state/copy/.codex-run/run-abcdefgh/report.md", text)
        self.assertIn("需求对照", text)
        self.assertIn("/home/.claude/CLAUDE.md", text)
        self.assertIn("/state/run/inputs/01-spec.md", text)

    def test_analyze_readonly_uses_structured_output_and_original_focus(self):
        text = self.render(kind="analyze", profile="copy", focus=["导出失败时的错误处理"], write_hint=[], protected=[])
        self.assertIn("先直接回答用户原话里的问题", text)
        self.assertIn("导出失败时的错误处理", text)
        readonly = brief.render({**json.loads(json.dumps({"task_id": "T", "kind": "analyze", "cwd": "/d",
                                                           "user_messages": [{"text": "这个设计可行吗？", "source": "relayed"}],
                                                           "no_user_words_reason": None, "inputs": [], "brief": None,
                                                           "focus": [], "constraints": [], "done_when": [], "write_hint": [],
                                                           "protected": [], "verify": [], "profile": "readonly", "web": True,
                                                           "model": "m", "effort": "low", "timeout_seconds": 60.0,
                                                           "max_budget_usd": None, "continue_from": None}))},
                                {"run_id": "run-abcdefgh", "round": 1, "profile": "readonly", "execution_cwd": "/d",
                                 "deadline": 1_900_000_000})
        self.assertIn("StructuredOutput", readonly)
        self.assertIn("不要套用固定的审查清单", readonly)
        self.assertIn('source="relayed"', readonly)
        self.assertIn("联网：已获用户授权", readonly)

    def test_no_user_words_is_stated_plainly(self):
        text = self.render(user_messages=[], no_user_words_reason="定时巡检任务")
        self.assertIn("本任务没有用户原话", text)
        self.assertIn("定时巡检任务", text)

    def test_continued_round_marks_new_words_and_previous_verdict(self):
        messages = [{"text": "第一轮要求", "source": "human"}, {"text": "补充：还要支持 CSV", "source": "human"}]
        text = self.render(user_messages=messages, round=2,
                           continued={"run_id": "run-previous1", "message_count": 1, "report": "/old/report.md",
                                      "decisions": [{"run_id": "run-previous1", "round": 1, "file": "/old/decision.json",
                                                     "decision": {"verdict": "accepted_with_corrections", "next": "next_round",
                                                                  "note": "缺少失败提示", "applied": True,
                                                                  "item_decisions": [{"id": "F1", "disposition": "rejected",
                                                                                      "reason": "误报：已有超时处理"}]}}]})
        self.assertIn('index="2" source="human" new_this_round="true"', text)
        self.assertNotIn('index="1" source="human" new_this_round', text)
        self.assertIn("缺少失败提示", text)
        self.assertIn("第 2 轮", text)
        self.assertIn("F1 不成立：误报：已有超时处理", text)
        self.assertIn("/old/decision.json", text)
        self.assertIn("效力高于这些轮次的报告", text)
        self.assertIn("delivery.patch 应用到原仓库", text)


if __name__ == "__main__":
    unittest.main()
