import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import result_schema  # noqa: E402
import stream_parser  # noqa: E402


def lines(*events):
    return [json.dumps(event) for event in events]


class StreamParserTests(unittest.TestCase):
    def test_final_result_models_reads_and_denials(self):
        facts = stream_parser.parse_lines(lines(
            {"type": "system", "subtype": "init", "session_id": "s", "model": "opus", "tools": ["Read"]},
            {"type": "assistant", "session_id": "s", "message": {"model": "claude-x", "content": [
                {"type": "tool_use", "id": "a", "name": "Read", "input": {"file_path": "/in/spec.md"}}]}},
            {"type": "result", "subtype": "success", "session_id": "s", "permission_denials": [
                {"tool_name": "WebFetch", "tool_use_id": "d", "tool_input": {"url": "https://x.test"}}],
             "modelUsage": {"claude-y": {}}},
        ), "s")
        self.assertEqual(facts["session_id"], "s")
        self.assertEqual(facts["models"], ["claude-x", "claude-y"])
        self.assertEqual(facts["read_paths"], ["/in/spec.md"])
        self.assertEqual(facts["denials"], [{"tool_name": "WebFetch", "tool_use_id": "d", "target": "https://x.test"}])
        self.assertEqual(facts["final"]["subtype"], "success")
        self.assertFalse(facts["session_mismatch"])

    def test_workflow_completion_is_bound_to_its_tool_call(self):
        facts = stream_parser.parse_lines(lines(
            {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "w1", "name": "Workflow", "input": {"name": "x"}}]}},
            {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "w2", "name": "Workflow", "input": {"name": "y"}}]}},
            {"type": "system", "subtype": "task_started", "tool_use_id": "w1", "task_id": "t1"},
            {"type": "system", "subtype": "task_notification", "tool_use_id": "w1", "task_id": "other", "status": "completed"},
            {"type": "system", "subtype": "task_notification", "tool_use_id": "w1", "task_id": "t1", "status": "completed"},
        ))
        self.assertEqual(facts["workflows"]["w1"]["status"], "completed")
        self.assertEqual(facts["workflows"]["w2"]["status"], "launched")

    def test_garbage_and_subagent_results_are_ignored(self):
        facts = stream_parser.parse_lines(["not json", "[]",
                                           json.dumps({"type": "result", "parent_tool_use_id": "agent", "subtype": "success"})])
        self.assertIsNone(facts["final"])
        self.assertEqual(facts["result_events"], 1)


class ResultSchemaTests(unittest.TestCase):
    def test_valid_and_invalid_headers(self):
        good = {"status": "completed", "summary": "done", "acceptance": [{"criterion": "x", "self_check": "met"}],
                "tests_run": [{"command": "pytest", "exit_code": 0, "outcome": "12 passed"}], "questions": ["要保留旧接口吗？"],
                "items": [{"id": "F1", "title": "t", "confidence": "verified"}]}
        self.assertEqual(result_schema.validate(good), [])
        problems = result_schema.validate({"status": "done", "items": [{"id": "F1"}, {"id": "F1", "title": "t", "confidence": "x"}]})
        joined = "; ".join(problems)
        for expected in ("result.summary is required", "result.status must be one of", "unique", "confidence must be one of"):
            self.assertIn(expected, joined)

    def test_readonly_schema_requires_a_report(self):
        schema = result_schema.json_schema(with_report=True)
        self.assertIn("report_markdown", schema["required"])
        self.assertIn("report_markdown is required", "; ".join(result_schema.validate({"status": "completed", "summary": "s"},
                                                                                       with_report=True)))

    def test_item_decisions_cover_each_item_once(self):
        items = [{"id": "F1"}, {"id": "F2"}]
        decided = result_schema.item_decisions(items, [{"id": "F1", "disposition": "accepted"},
                                                       {"id": "F2", "disposition": "rejected", "reason": "not reproducible"}])
        self.assertEqual([entry["id"] for entry in decided], ["F1", "F2"])
        with self.assertRaisesRegex(ValueError, "missing F2"):
            result_schema.item_decisions(items, [{"id": "F1", "disposition": "accepted"}])
        with self.assertRaisesRegex(ValueError, "reason"):
            result_schema.item_decisions(items, [{"id": "F1", "disposition": "accepted"}, {"id": "F2", "disposition": "downgraded"}])
        self.assertEqual(result_schema.item_decisions([], None), [])


if __name__ == "__main__":
    unittest.main()
