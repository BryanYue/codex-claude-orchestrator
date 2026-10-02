"""Workflow provenance observed from stream inputs, without a model request."""
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import stream_parser


class WorkflowInputEvidenceTests(unittest.TestCase):
    def parse(self, inputs, expected="*"):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "stream.jsonl"
            events = [{"type": "assistant", "session_id": "session", "message": {"content": [
                {"type": "tool_use", "name": "Workflow", "id": f"w{index}", "input": value}]}}
                for index, value in enumerate(inputs)]
            path.write_text("\n".join(json.dumps(event) for event in events))
            return stream_parser.parse_stream(path, "session", expected)[1]

    def test_live_and_terminal_identity_share_system_init_and_model_rules(self):
        events = [{"type": "system", "subtype": "system_init", "model": "initialized", "session_id": "session"},
                  {"type": "assistant", "model": "fallback-model", "message": {"content": []}},
                  {"type": "result", "session_id": "session", "modelUsage": {"usage-model": {}}}]
        lines = [json.dumps(item) for item in events]
        live = stream_parser.provider_identity(lines)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "stream.jsonl"
            path.write_text("\n".join(lines))
            _, final = stream_parser.parse_stream(path, "session")
        self.assertEqual(live["session_id"], "session")
        for key in ("initialized_model", "actual_models", "actual_model_source", "provider_response_observed"):
            self.assertEqual(live[key], final[key])
        self.assertEqual(live["initialized_model"], "initialized")

    def test_wildcard_records_actual_name_and_inline_digest(self):
        script = "return {value: 3};"
        metadata = self.parse([{"name": "actual-review", "script": script, "args": {"token": "private"}}])
        self.assertEqual(metadata["workflow_name"], "actual-review")
        entry = metadata["workflow_invocations"][0]["input"]
        self.assertEqual(entry["script_sha256"], hashlib.sha256(script.encode()).hexdigest())
        self.assertEqual(entry["script_digest_source"], "inline_tool_input")
        self.assertNotIn("private", json.dumps(entry))
        self.assertNotIn("script", entry)

    def test_absolute_script_hash_is_explicitly_a_parse_time_observation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "script.js"
            path.write_bytes(b"return 7;")
            metadata = self.parse([{"scriptPath": str(path)}])
            self.assertIsNone(metadata["workflow_name"])
            entry = metadata["workflow_invocations"][0]["input"]
            self.assertEqual(entry["script_path"], str(path))
            self.assertEqual(entry["script_sha256"], hashlib.sha256(path.read_bytes()).hexdigest())
            self.assertEqual(entry["script_digest_source"], "file_at_parse_time")

    def test_relative_or_missing_path_remains_observed_without_invented_digest(self):
        metadata = self.parse([{"scriptPath": "relative.js"}, {"scriptPath": "/missing/review.js"}])
        entries = [row["input"] for row in metadata["workflow_invocations"]]
        self.assertIsNone(entries[0]["script_sha256"])
        self.assertEqual(entries[0]["script_digest_unavailable"], "relative_path_without_execution_cwd")
        self.assertIsNone(entries[1]["script_sha256"])
        self.assertEqual(entries[1]["script_digest_unavailable"], "FileNotFoundError")

    def test_symlink_fifo_and_oversized_script_are_not_read(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "target.js"
            target.write_text("return 1")
            link = root / "link.js"
            link.symlink_to(target)
            fifo = root / "fifo.js"
            os.mkfifo(fifo)
            large = root / "large.js"
            with large.open("wb") as stream:
                stream.truncate(2 * 1024 * 1024 + 1)
            metadata = self.parse([{"scriptPath": str(path)} for path in (link, fifo, large)])
            for row in metadata["workflow_invocations"]:
                self.assertIsNone(row["input"]["script_sha256"])
                self.assertIn("script_digest_unavailable", row["input"])

    def test_multiple_names_do_not_collapse_to_wildcard_or_one_name(self):
        metadata = self.parse([{"name": "one"}, {"name": "two"}])
        self.assertIsNone(metadata["workflow_name"])
        self.assertEqual(metadata["workflow_names"], ["one", "two"])

    def test_strict_named_binding_still_rejects_script_substitution(self):
        metadata = self.parse([{"name": "expected", "script": "return 1"}], "expected")
        self.assertFalse(metadata["workflow_tool_use_observed"])
        self.assertEqual(metadata["workflow_stream_diagnostics"]["other_workflow_tool_uses"], 1)


if __name__ == "__main__":
    unittest.main()
