"""Regressions derived from the independent review of 0.7.0."""
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest import mock
from types import SimpleNamespace

import test_bridge as fixtures

sys.path.insert(0, str(fixtures.BRIDGE.parent))
import bridge
import review_workspace
import viewer
from result_schema import result_payload, finding_decisions


class ReviewRegressionTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.BridgeTests(methodName="runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.repo = self.fixture.repo
        self.root = self.fixture.root

    def test_parent_git_never_executes_poisoned_global_or_local_fsmonitor(self):
        marker = self.root / "callback-ran"
        callback = self.root / "monitor.sh"
        callback.write_text('#!/bin/sh\nprintf bad >> "' + str(marker) + '"\n')
        callback.chmod(0o755)
        home = self.root / "fake-home"
        home.mkdir()
        (home / ".gitconfig").write_text('[core]\nfsmonitor = ' + str(callback) + '\n')
        subprocess.run(["git", "-C", str(self.repo), "config", "core.fsmonitor", str(callback)], check=True)
        with mock.patch.dict(os.environ, {"HOME": str(home), "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "core.fsmonitor", "GIT_CONFIG_VALUE_0": str(callback)}):
            bridge.git_snapshot(self.repo, self.fixture.packet())
            bridge.plugin_identity.git_source(self.repo)
        self.assertFalse(marker.exists(), "trusted observer executed a repository/global callback")

    def test_byte_filename_snapshot_and_json_roundtrip(self):
        raw_name = os.fsdecode(b"file-\xff.txt")
        with mock.patch.object(bridge, "git_status_entries", return_value=[("??", raw_name, None)]):
            snapshot = bridge.git_snapshot(self.repo, self.fixture.packet())
        path = self.root / "snapshot.json"
        bridge.dump(path, snapshot)
        self.assertEqual(bridge.load(path), snapshot)
        self.assertEqual(bridge.load(path)["status_entries"][0]["path"], raw_name)

    def test_invalid_full_report_is_preserved_with_digest_and_diagnostic(self):
        run = self.root / "report-run"
        run.mkdir()
        metadata = review_workspace.prepare_review_workspace(self.repo, run)
        report = Path(metadata["artifact_directory"]) / "result.json"
        report.parent.mkdir(exist_ok=True)
        data = b'{"status":"completed","summary":"original malformed report","findings":[]}'
        report.write_bytes(data)
        captured = bridge.capture_review_report(run, {"review_report_path": str(report)}, {})
        self.assertEqual(captured["status"], "invalid")
        self.assertIn("evidence", captured["reason"])
        self.assertEqual((run / "review-report.json").read_bytes(), data)
        self.assertEqual(captured["sha256"], hashlib.sha256(data).hexdigest())
        runtime = SimpleNamespace(snapshot=lambda _: {"run_dir": str(run), "result": {"review_report": captured}})
        page = viewer.read_artifact(runtime, "fixture", "review_report")
        self.assertTrue(page["available"])
        self.assertEqual(page["validation_status"], "invalid")
        self.assertIn("original malformed report", page["content"])

    def test_new_finding_fields_validate_and_legacy_decisions_fail_cleanly(self):
        finding = {"id": "F1", "category": "defect", "confidence": "probable", "summary": "check this", "evidence": [],
                   "severity": "P2", "location": "a.py:1", "suggestion": "reproduce"}
        report = {"status": "completed", "summary": "review", "evidence": [], "checks": [], "unresolved": [], "findings": [finding]}
        self.assertEqual(result_payload({"structured_output": report})["findings"], [finding])
        for key, value in (("severity", "P99"), ("location", ""), ("suggestion", [])):
            with self.subTest(key=key), self.assertRaises(bridge.BridgeError):
                result_payload({"structured_output": {**report, "findings": [{**finding, key: value}]}})
        for legacy in (['old free text'], [{"summary": "no id"}], None):
            with self.subTest(legacy=legacy), self.assertRaisesRegex(ValueError, "stable IDs"):
                finding_decisions(legacy, [])


if __name__ == "__main__":
    unittest.main()
