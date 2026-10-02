import hashlib
import json
import os
import shutil
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
import workflow_delivery as wd  # noqa: E402

SESSION = "0f8e3c1a-5b7d-4e2f-9a6c-1d2b3c4d5e6f"
TASK = "w1a2b3c4"
LONG_REPORT = ("## 发现\n" + "审查条目：依据 src/module.py:42 的实际实现。\n" * 600 + "END-OF-REPORT")
SHORT_REPORT = "No findings."


def envelope(result, **extra):
    value = {"summary": "workflow finished", "agentCount": 2, "logs": ["agent 1 done"], "result": result,
             "workflowProgress": [], "totalTokens": 1234, "totalToolCalls": 7, **extra}
    return json.dumps(value, ensure_ascii=False).encode("utf-8")


def replace_directory(path: str) -> None:
    """Put a different directory at ``path`` while the original still exists, so its inode differs."""
    os.rename(path, path + ".replaced")
    os.mkdir(path)


class DeliveryFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.run_dir = Path(self.tmp.name) / "run-fixture"
        self.run_dir.mkdir()
        self.root = wd.create_root()
        self.addCleanup(lambda: [shutil.rmtree(self.root["path"] + suffix, ignore_errors=True) for suffix in ("", ".replaced")])
        self.tasks = Path(self.root["path"]) / f"claude-{os.getuid()}" / "-private-tmp-repo" / SESSION / "tasks"
        self.tasks.mkdir(parents=True)
        self.binding = {"run_id": "run-fixture", "session_id": SESSION, "tool_use_id": "toolu-1", "task_id": TASK}

    def tearDown(self):
        self.tmp.cleanup()

    def output(self, content: bytes, task=TASK) -> Path:
        path = self.tasks / f"{task}.output"
        path.write_bytes(content)
        return path

    def capture(self, output_file, *, task=TASK, session=SESSION, index=0):
        return wd.capture(self.root, session_id=session, task_id=task, output_file=str(output_file),
                          run_dir=self.run_dir, index=index, binding=self.binding)


class CaptureTests(DeliveryFixture):
    def test_structured_results_keep_exact_envelope_and_label_paged_json_projection(self):
        for index, value in enumerate(({"status": "reviewed", "findings": [], "observed": "中文😀"},
                                       [{"finding": "行😀" * 40}], {}, [])):
            with self.subTest(value=value):
                raw = envelope(value)
                collected = self.capture(self.output(raw, task=f"struct{index}"), task=f"struct{index}", index=index)
                self.assertEqual(collected["status"], "collected", collected)
                self.assertEqual((self.run_dir / collected["envelope"]["path"]).read_bytes(), raw)
                delivery = {"invocations": [{} for _ in range(index)] + [{"collection": collected}]}
                compact = wd.summary(delivery)["reports"][index]
                self.assertEqual(compact["representation"], "json_value")
                self.assertEqual(compact["value_type"], "object" if isinstance(value, dict) else "array")
                joined, offset = "", 0
                while True:
                    page = wd.read_page(self.run_dir, delivery, index=index, offset=offset, limit=16)
                    self.assertTrue(page["available"], page)
                    self.assertEqual(page["representation"], "json_value")
                    joined += page["content"]
                    if page["end_of_artifact"]:
                        break
                    offset = page["next_offset_bytes"]
                stored = (self.run_dir / collected["report"]["path"]).read_bytes()
                self.assertEqual(json.loads(stored), value)
                self.assertEqual(joined.encode(), stored)
                self.assertEqual(collected["report"]["sha256"], hashlib.sha256(stored).hexdigest())

    def test_nonfinite_structured_result_is_not_a_report(self):
        failed = self.capture(self.output(envelope({"value": float("nan")})))
        self.assertEqual(failed["reason_code"], "output_result_invalid_json")

    def test_decoder_recursion_error_keeps_invalid_collection_and_source_evidence(self):
        # Decoder limits vary across Python builds; inject its failure rather than
        # assuming sys.getrecursionlimit also bounds the C decoder.
        raw = envelope({"nested": []})
        output = self.output(raw)
        with mock.patch.object(wd.json, "loads", side_effect=RecursionError("decoder nesting limit")):
            failed = self.capture(output)
        self.assertEqual(failed["status"], "invalid")
        self.assertEqual(failed["reason_code"], "output_json_too_deep")
        self.assertEqual(failed["source"]["sha256"], hashlib.sha256(raw).hexdigest())
        self.assertEqual(failed["source"]["size_bytes"], len(raw))

    def test_ancestor_swap_between_inspection_and_open_is_rejected(self):
        path = self.output(envelope(SHORT_REPORT))
        original = wd._read_bounded
        def swap(*args, **kwargs):
            moved = Path(self.tmp.name) / "moved-tasks"
            self.tasks.rename(moved)
            self.tasks.symlink_to(moved, target_is_directory=True)
            return original(*args, **kwargs)
        with mock.patch.object(wd, "_read_bounded", side_effect=swap):
            self.assertNotEqual(self.capture(path)["status"], "collected")

    def test_provider_anchor_replaced_before_open_is_rejected(self):
        path = self.output(envelope(SHORT_REPORT))
        original = wd._read_bounded
        def swap(*args, **kwargs):
            replace_directory(self.root["path"])
            path.parent.mkdir(parents=True)
            path.write_bytes(envelope(SHORT_REPORT))
            return original(*args, **kwargs)
        with mock.patch.object(wd, "_read_bounded", side_effect=swap):
            self.assertNotEqual(self.capture(path)["status"], "collected")

    def test_snapshot_destination_symlink_never_writes_outside_the_run(self):
        path = self.output(envelope(SHORT_REPORT))
        outside = Path(self.tmp.name) / "outside"
        outside.mkdir()
        (self.run_dir / wd.OUTPUT_DIR).symlink_to(outside, target_is_directory=True)
        self.assertNotEqual(self.capture(path)["status"], "collected")
        self.assertEqual(list(outside.iterdir()), [])

    def test_long_and_short_reports_are_captured_whole_with_provenance(self):
        for index, report in enumerate((LONG_REPORT, SHORT_REPORT)):
            with self.subTest(size=len(report)):
                data = envelope(report)
                path = self.output(data, task=f"{TASK}{index}")
                collected = self.capture(path, task=f"{TASK}{index}", index=index)
                self.assertEqual(collected["status"], "collected", collected)
                self.assertIsNone(collected["reason_code"])
                stored_envelope = (self.run_dir / collected["envelope"]["path"]).read_bytes()
                stored_report = (self.run_dir / collected["report"]["path"]).read_bytes()
                self.assertEqual(stored_envelope, data)
                self.assertEqual(stored_report.decode("utf-8"), report)
                self.assertEqual(collected["source"]["sha256"], hashlib.sha256(data).hexdigest())
                self.assertEqual(collected["report"]["sha256"], hashlib.sha256(report.encode()).hexdigest())
                self.assertEqual((collected["report"]["size_bytes"], collected["report"]["characters"]),
                                 (len(report.encode()), len(report)))
                self.assertEqual(collected["binding"], self.binding)
                self.assertIn("result", collected["envelope"]["top_level_keys"])
                self.assertEqual(stat.S_IMODE((self.run_dir / collected["report"]["path"]).stat().st_mode), 0o400)
        self.assertGreater(len(LONG_REPORT.encode()), 20_000, "the long fixture must exceed a parent summary")

    def test_blank_null_missing_or_invalid_output_is_not_a_delivery(self):
        cases = {
            "output_result_blank": envelope("  \n\t"),
            "output_result_missing": envelope(None),
            "output_not_json": b"{not json",
            "output_not_utf8": b"\xff\xfe{}",
            "output_shape_invalid": b"[1, 2]",
            "output_result_invalid_text": b'{"result": "\\ud800 lone surrogate"}',
        }
        for index, (code, content) in enumerate(cases.items()):
            with self.subTest(code=code):
                path = self.output(content, task=f"{TASK}{index}")
                failed = self.capture(path, task=f"{TASK}{index}", index=index)
                self.assertNotEqual(failed["status"], "collected")
                self.assertEqual(failed["reason_code"], code)
                self.assertFalse((self.run_dir / wd.artifact_names(index)["report"]).exists())
        without_key = json.dumps({"summary": "done"}).encode()
        failed = self.capture(self.output(without_key, task="nokey"), task="nokey", index=9)
        self.assertEqual(failed["reason_code"], "output_result_missing")

    def test_wrong_session_task_or_location_is_unsupported_not_guessed(self):
        path = self.output(envelope(SHORT_REPORT))
        self.assertEqual(self.capture(path, session="other-session")["reason_code"], "output_layout_unsupported")
        self.assertEqual(self.capture(path, task="othertask")["reason_code"], "output_layout_unsupported")
        outside = Path(self.tmp.name) / f"{TASK}.output"
        outside.write_bytes(envelope(SHORT_REPORT))
        self.assertEqual(self.capture(outside)["reason_code"], "output_path_outside_root")
        detour = str(self.tasks / ".." / "tasks" / f"{TASK}.output")
        self.assertEqual(self.capture(detour)["reason_code"], "output_path_unsupported")
        missing_reference = wd.capture(self.root, session_id=SESSION, task_id=TASK, output_file=None,
                                       run_dir=self.run_dir, index=0, binding=self.binding)
        self.assertEqual(missing_reference["reason_code"], "output_reference_missing")
        self.assertEqual(self.capture(path, task="../escape")["reason_code"], "task_binding_invalid")
        self.assertEqual(self.capture(path, task=None)["reason_code"], "task_binding_invalid")
        self.assertFalse((self.run_dir / wd.OUTPUT_DIR).exists(), "nothing is snapshotted for a refused reference")

    def test_symlink_hardlink_oversize_deleted_and_replaced_root_are_refused(self):
        real = Path(self.tmp.name) / "real.output"
        real.write_bytes(envelope(SHORT_REPORT))
        (self.tasks / f"{TASK}.output").symlink_to(real)
        self.assertEqual(self.capture(self.tasks / f"{TASK}.output")["reason_code"], "output_symlink")
        (self.tasks / f"{TASK}.output").unlink()

        path = self.output(envelope(SHORT_REPORT))
        alias = Path(self.root["path"]) / "alias"
        os.link(path, alias)
        self.assertEqual(self.capture(path)["reason_code"], "output_hardlink_alias")
        alias.unlink()

        with mock.patch.object(wd, "MAX_OUTPUT_BYTES", 16):
            self.assertEqual(self.capture(path)["reason_code"], "output_oversize")

        path.unlink()
        self.assertEqual(self.capture(path)["reason_code"], "output_missing")

        session_dir = self.tasks.parent
        moved = Path(self.tmp.name) / "moved-session"
        shutil.move(str(session_dir), str(moved))
        session_dir.symlink_to(moved)
        (moved / "tasks" / f"{TASK}.output").write_bytes(envelope(SHORT_REPORT))
        self.assertEqual(self.capture(path)["reason_code"], "output_path_unsafe")

        replace_directory(self.root["path"])
        self.assertEqual(self.capture(path)["reason_code"], "temp_root_unavailable")

    def test_file_changed_during_read_is_unstable(self):
        path = self.output(envelope(SHORT_REPORT))
        original = wd._read_bounded

        def grow(*args, **kwargs):
            data, before, after = original(*args, **kwargs)
            with path.open("ab") as handle:
                handle.write(b" ")
            return data, before, after

        with mock.patch.object(wd, "_read_bounded", side_effect=grow):
            self.assertEqual(self.capture(path)["reason_code"], "output_unstable")

    def test_remove_root_never_deletes_a_replacement(self):
        replace_directory(self.root["path"])
        keep = Path(self.root["path"]) / "foreign.txt"
        keep.write_text("not ours")
        self.assertTrue(wd.remove_root(self.root).startswith("kept"))
        self.assertTrue(keep.exists())
        own = wd.create_root()
        self.addCleanup(lambda: shutil.rmtree(own["path"], ignore_errors=True))
        (Path(own["path"]) / f"claude-{os.getuid()}").mkdir()
        self.assertEqual(wd.remove_root(own), "removed")
        self.assertFalse(Path(own["path"]).exists())


class PageTests(DeliveryFixture):
    def test_readback_rejects_ancestor_swap_and_hardlink_alias(self):
        delivery = self.delivery()
        path = self.run_dir / wd.artifact_names(0)["report"]
        alias = self.run_dir / "alias.txt"
        os.link(path, alias)
        self.assertFalse(wd.read_page(self.run_dir, delivery)["available"])
        alias.unlink()
        folder = self.run_dir / wd.OUTPUT_DIR
        moved = Path(self.tmp.name) / "moved-artifacts"
        folder.rename(moved)
        folder.symlink_to(moved, target_is_directory=True)
        self.assertFalse(wd.read_page(self.run_dir, delivery)["available"])

    def delivery(self, report=LONG_REPORT):
        collection = self.capture(self.output(envelope(report)))
        self.assertEqual(collection["status"], "collected")
        return {"status": "delivered", "reason_codes": [],
                "invocations": [{"index": 0, "tool_use_id": "toolu-1", "task_id": TASK, "collection": collection}]}

    def pages(self, delivery, part="report", limit=1000, max_pages=None):
        text, offset, seen = "", 0, []
        while offset is not None and (max_pages is None or len(seen) < max_pages):
            page = wd.read_page(self.run_dir, delivery, index=0, part=part, offset=offset, limit=limit)
            self.assertTrue(page["available"], page)
            self.assertLessEqual(page["page_bytes"], limit)
            self.assertEqual(page["offset_bytes"], offset)
            seen.append(page)
            text += page["content"]
            offset = page["next_offset_bytes"]
        return text, seen

    def test_multi_page_reconstruction_is_exact_and_coherent(self):
        delivery = self.delivery()
        text, pages = self.pages(delivery, limit=1000)
        self.assertEqual(text, LONG_REPORT)
        self.assertGreater(len(pages), 10)
        self.assertEqual({page["total_bytes"] for page in pages}, {len(LONG_REPORT.encode())})
        self.assertEqual({page["sha256"] for page in pages}, {hashlib.sha256(LONG_REPORT.encode()).hexdigest()})
        self.assertTrue(pages[-1]["end_of_artifact"])
        self.assertFalse(any(page["end_of_artifact"] for page in pages[:-1]))
        envelope_text, _ = self.pages(delivery, part="envelope", limit=4096)
        self.assertEqual(json.loads(envelope_text)["result"], LONG_REPORT)
        prefix, small = self.pages(delivery, limit=wd.MIN_PAGE_BYTES, max_pages=40)
        self.assertTrue(LONG_REPORT.startswith(prefix))
        self.assertTrue(all(page["page_bytes"] > 0 for page in small), "a minimum page always holds one character")

    def test_bad_requests_and_tamper_or_deletion_serve_nothing(self):
        delivery = self.delivery()
        with self.assertRaises(wd.DeliveryError):
            wd.read_page(self.run_dir, delivery, offset=len("## 发".encode()) - 1)
        for limit in (0, 3, wd.MAX_PAGE_BYTES + 1):
            with self.assertRaises(wd.DeliveryError):
                wd.read_page(self.run_dir, delivery, limit=limit)
        with self.assertRaises(wd.DeliveryError):
            wd.read_page(self.run_dir, delivery, offset=10 ** 9)
        with self.assertRaises(wd.DeliveryError):
            wd.read_page(self.run_dir, delivery, part="transcript")
        self.assertEqual(wd.read_page(self.run_dir, delivery, index=3)["state"], "no_such_invocation")
        self.assertEqual(wd.read_page(self.run_dir, None)["state"], "not_recorded")
        failed = {"invocations": [{"index": 0, "collection": {"status": "invalid", "reason_code": "output_result_blank"}}]}
        self.assertEqual(wd.read_page(self.run_dir, failed)["state"], "not_collected")
        redirected = json.loads(json.dumps(delivery))
        redirected["invocations"][0]["collection"]["report"]["path"] = "../outside.txt"
        self.assertEqual(wd.read_page(self.run_dir, redirected)["state"], "record_invalid")
        stored = self.run_dir / wd.artifact_names(0)["report"]
        stored.chmod(0o600)
        stored.write_text(LONG_REPORT.replace("END-OF-REPORT", "TAMPERED-ENDING"), encoding="utf-8")
        tampered = wd.read_page(self.run_dir, delivery)
        self.assertEqual((tampered["available"], tampered["state"], tampered["content"]), (False, "integrity_mismatch", None))
        stored.unlink()
        self.assertEqual(wd.read_page(self.run_dir, delivery)["state"], "missing")


class EvaluateTests(unittest.TestCase):
    def test_bounded_preview_does_not_truncate_collected_count(self):
        delivery = {"invocations": [{"index": i, "collection": {"status": "collected"}} for i in range(20)]}
        summary = wd.summary(delivery)
        self.assertEqual(summary["collected_count"], 20)
        self.assertEqual(len(summary["reports"]), 16)
        self.assertTrue(summary["reports_truncated"])

    @staticmethod
    def invocation(ack="succeeded", terminal=None, collection=None, before_ack=0):
        return {"acknowledgement": {"state": ack}, "terminal": terminal, "collection": collection or {},
                "notifications_before_acknowledgement": before_ack}

    def test_reason_codes_distinguish_each_missing_step(self):
        done = {"status": "completed", "source": "system_task_notification", "conflict": False}
        collected = {"status": "collected"}
        cases = [
            ([], "workflow_invocation_missing"),
            ([self.invocation(ack="missing")], "workflow_acknowledgement_missing"),
            ([self.invocation(ack="failed")], "workflow_acknowledgement_failed"),
            ([self.invocation(before_ack=1)], "workflow_notification_before_acknowledgement"),
            ([self.invocation()], "workflow_terminal_pending"),
            ([self.invocation(terminal={**done, "status": "failed"})], "workflow_terminal_failed"),
            ([self.invocation(terminal={**done, "conflict": True})], "workflow_terminal_conflict"),
            ([self.invocation(terminal={**done, "source": "legacy_text_notification"})], "workflow_report_transport_missing"),
            ([self.invocation(terminal=done, collection={"status": "unavailable", "reason_code": "output_reference_missing"})],
             "workflow_report_transport_missing"),
            ([self.invocation(terminal=done, collection={"status": "invalid", "reason_code": "output_symlink"})],
             "workflow_report_not_collected"),
        ]
        for invocations, code in cases:
            with self.subTest(code=code):
                verdict = wd.evaluate(invocations, final_after_completion=True, final_result_observed=True)
                self.assertEqual(verdict["status"], "not_delivered")
                self.assertIn(code, verdict["reason_codes"])
        symlink = wd.evaluate([self.invocation(terminal=done, collection={"status": "invalid", "reason_code": "output_symlink"})],
                              final_after_completion=True, final_result_observed=True)
        self.assertIn("output_symlink", symlink["reason_codes"])
        ok = [self.invocation(terminal=done, collection=collected)]
        self.assertEqual(wd.evaluate(ok, final_after_completion=True, final_result_observed=True),
                         {"status": "delivered", "reason_codes": []})
        self.assertEqual(wd.evaluate(ok, final_after_completion=False, final_result_observed=True)["reason_codes"],
                         ["workflow_final_before_completion"])
        self.assertEqual(wd.evaluate(ok, final_after_completion=False, final_result_observed=False)["reason_codes"],
                         ["workflow_final_result_missing"])
        two = wd.evaluate([*ok, self.invocation()], final_after_completion=False, final_result_observed=True)
        self.assertEqual(two["reason_codes"], ["workflow_terminal_pending"], "every genuine invocation must deliver")

    def test_summary_is_bounded_and_points_to_paged_access(self):
        self.assertIsNone(wd.summary(None))
        record = {"status": "delivered", "reason_codes": [],
                  "invocations": [{"index": 0, "tool_use_id": "t", "task_id": "k",
                                   "collection": {"status": "collected",
                                                  "report": {"size_bytes": 10, "characters": 10, "sha256": "a" * 64}}}]}
        value = wd.summary(record)
        self.assertEqual((value["invocation_count"], value["collected_count"]), (1, 1))
        self.assertEqual(value["reports"][0]["total_bytes"], 10)
        self.assertIn("workflow_report", value["access"])
        self.assertFalse(any("content" in item for item in value["reports"]))


if __name__ == "__main__":
    unittest.main()
