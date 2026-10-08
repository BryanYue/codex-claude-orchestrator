"""Whole runs through Runtime, the real bridge and (on macOS) the real sandbox, with a fake Claude CLI."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness  # noqa: E402
from runtime import Runtime  # noqa: E402

RESULT = {"status": "completed", "summary": "导出已改为异步", "acceptance": [{"criterion": "测试通过", "self_check": "met",
                                                                         "evidence": "pytest 12 passed"}],
          "tests_run": [{"command": "pytest -q", "exit_code": 0, "outcome": "12 passed"}]}
MAC = sys.platform == "darwin" and Path("/usr/bin/sandbox-exec").is_file()


@unittest.skipUnless(MAC, "the copy profile needs macOS sandbox-exec")
class CopyProfileTests(unittest.TestCase):
    def setUp(self):
        self.fx = harness.Fixture(self)

    def test_implement_in_copy_returns_patch_report_and_keeps_original_untouched(self):
        fx = self.fx
        original_before = harness.git(fx.source, "status", "--porcelain")
        snapshot = fx.run(
            {"read_inputs": True},
            {"write": "src/app.py", "text": "print('v2')\n"},
            {"write": "src/export.py", "text": "async def export():\n    return '中文'\n"},
            {"binary": "src/blob.bin", "hex": "00ff10"},
            {"delete": "old.txt"},
            {"write": "build/out.o", "text": "ignored build output"},
            {"attempt_write": str(fx.source / "src" / "app.py")},
            {"attempt_write": str(fx.source / ".git" / "config")},
            {"attempt_write": str(fx.state / "v1" / "registry.json")},
            {"capture_brief": str(fx.root / "probes-brief.md")},
            {"deliver": True, "report": "## 需求对照\n- 异步导出：已做\n", "result": RESULT},
        )
        outcome = snapshot["outcome"]
        self.assertEqual((snapshot["status"], outcome["run_outcome"], outcome["claimed_status"]), ("ok", "ok", "completed"))
        self.assertEqual(outcome["process_stopped"], "confirmed")
        denied = fx.probes_seen()
        self.assertEqual(len(denied), 3)
        self.assertTrue(all(value == "PermissionError" for value in denied.values()), denied)
        self.assertEqual(original_before, harness.git(fx.source, "status", "--porcelain"))
        self.assertEqual((fx.source / "src" / "app.py").read_text(), "print('v1')\n")
        files = {item["path"]: item["status"] for item in outcome["changes"]["files"]}
        self.assertEqual(files, {"src/app.py": "M", "src/export.py": "A", "src/blob.bin": "A", "old.txt": "D"})
        run_dir = fx.run_dir(snapshot)
        check = subprocess.run(["git", "apply", "--check", str(run_dir / "changes.patch")], cwd=fx.source,
                               capture_output=True, text=True)
        self.assertEqual(check.returncode, 0, check.stderr)
        self.assertEqual((run_dir / "report.md").read_text(), "## 需求对照\n- 异步导出：已做\n")
        self.assertEqual(json.loads((run_dir / "result.json").read_text()), RESULT)
        self.assertEqual((fx.root / "probes-brief.md").read_bytes(), (run_dir / "brief.md").read_bytes())
        brief = (run_dir / "brief.md").read_text()
        self.assertEqual(brief.count("把导出改成异步，失败要提示原因"), 1)
        self.assertIn(str(fx.config / "CLAUDE.md"), brief)
        self.assertEqual((run_dir / "inputs" / "01-spec.md").read_text(), fx.spec.read_text())
        codes = {warning["code"] for warning in outcome["warnings"]}
        self.assertFalse(codes & {"report_missing", "result_missing", "result_malformed", "source_not_read"}, codes)
        command = json.loads((run_dir / "command.json").read_text())["argv"]
        self.assertEqual(command[0], "/usr/bin/sandbox-exec")
        self.assertIn("Bash", command[command.index("--allowedTools") + 1])
        self.assertIn("RemoteTrigger", command[command.index("--disallowedTools") + 1])
        self.assertNotIn("--json-schema", command)
        self.assertEqual(outcome["instruction_layer"]["hooks"], 1)
        self.assertEqual(outcome["cost_usd"], 0.0123)

    def test_warnings_report_facts_without_changing_the_outcome(self):
        fx = self.fx
        (fx.source / ".env").write_text("SECRET=1\n")
        snapshot = fx.run(
            {"write": "migrations/001.sql", "text": "drop table x;"},
            {"write": "README.md", "text": "notes"},
            {"workflow": "pending"},
            {"denial": "WebFetch"},
            {"deliver": True, "report": "r",
             "result": {**RESULT, "questions": ["要不要保留旧接口？"], "disputes": ["约束 X 与原话冲突"]}},
            write_hint=["src/"], protected=["migrations/"])
        warnings = fx.warnings(snapshot)
        self.assertEqual(snapshot["outcome"]["run_outcome"], "ok")
        self.assertEqual(warnings["protected_touched"]["paths"], ["migrations/001.sql"])
        self.assertEqual(sorted(warnings["outside_hint"]["paths"]), ["README.md", "migrations/001.sql"])
        self.assertEqual(warnings["questions_for_user"]["items"], ["要不要保留旧接口？"])
        self.assertIn("disputes_present", warnings)
        self.assertIn("workflow_incomplete", warnings)
        self.assertIn("WebFetch", warnings["tool_denied"]["detail"])
        self.assertIn(".env", warnings["sensitive_inputs_skipped"]["paths"])
        self.assertIn(str(fx.spec.parent / "spec.md").split("/")[-1], " ".join(warnings["source_not_read"]["paths"]))
        self.assertFalse((Path(snapshot["lineage_root"]) / "copy" / ".env").exists())

    def test_original_edits_during_the_run_are_reported_not_fatal(self):
        fx = self.fx
        ready, go = fx.root / "ready", fx.root / "go"

        def edit_original():
            deadline = time.monotonic() + 15
            while not ready.exists() and time.monotonic() < deadline:
                time.sleep(.02)
            (fx.source / "src" / "app.py").write_text("print('coordinator edit')\n")
            go.write_text("go")
        thread = threading.Thread(target=edit_original)
        thread.start()
        snapshot = fx.run({"touch": str(ready)}, {"wait_for": str(go)}, {"deliver": True, "report": "r", "result": RESULT})
        thread.join()
        self.assertEqual(snapshot["outcome"]["run_outcome"], "ok")
        self.assertEqual(fx.warnings(snapshot)["original_changed_during_run"]["paths"], ["src/app.py"])

    def test_timeout_keeps_what_was_already_written(self):
        snapshot = self.fx.run({"deliver": True, "report": "## 需求对照\n- 部分完成\n"}, {"write": "src/app.py", "text": "x"},
                               {"sleep": 30}, timeout_seconds=4)
        outcome = snapshot["outcome"]
        self.assertEqual(outcome["run_outcome"], "timeout")
        self.assertEqual(outcome["report"]["bytes"], len("## 需求对照\n- 部分完成\n".encode()))
        self.assertEqual([item["path"] for item in outcome["changes"]["files"]], ["src/app.py"])
        self.assertEqual(outcome["process_stopped"], "confirmed")

    def test_cancel_stops_claude_and_keeps_evidence(self):
        fx = self.fx
        fx.script({"write": "src/app.py", "text": "partial"}, {"touch": str(fx.root / "started")}, {"sleep": 30})
        run_id = fx.runtime.start(fx.packet())["run_id"]
        deadline = time.monotonic() + 20
        while not (fx.root / "started").exists() and time.monotonic() < deadline:
            time.sleep(.05)
        fx.runtime.cancel(run_id, "用户要求停止")
        snapshot = fx.wait(run_id)
        self.assertEqual(snapshot["outcome"]["run_outcome"], "cancelled")
        self.assertEqual(snapshot["outcome"]["changes"]["file_count"], 1)
        with self.assertRaisesRegex(RuntimeError, "already ended"):
            fx.runtime.cancel(run_id, "again")

    def test_residual_background_processes_are_stopped_and_reported(self):
        snapshot = self.fx.run({"background": 60}, {"deliver": True, "report": "r", "result": RESULT})
        self.assertIn("residual_processes_stopped", self.fx.warnings(snapshot))
        self.assertEqual(snapshot["outcome"]["process_stopped"], "confirmed")

    def test_crash_without_result_is_reported_with_partial_files(self):
        snapshot = self.fx.run({"write": "src/app.py", "text": "half"}, {"no_result": 3})
        outcome = snapshot["outcome"]
        self.assertEqual(outcome["run_outcome"], "crashed")
        self.assertIn("report_missing", self.fx.warnings(snapshot))
        self.assertEqual(outcome["changes"]["file_count"], 1)

    def test_not_logged_in_never_launches_claude(self):
        os.environ["FAKE_LOGGED_IN"] = "0"
        self.addCleanup(os.environ.pop, "FAKE_LOGGED_IN", None)
        snapshot = self.fx.run()
        outcome = snapshot["outcome"]
        self.assertEqual((outcome["run_outcome"], outcome["process_stopped"]), ("not_started", "not_launched"))
        self.assertIn("not_logged_in", outcome["note"])
        self.assertEqual(outcome["warnings"], [])

    def test_continue_from_reuses_the_copy_resumes_and_splits_patches(self):
        fx = self.fx
        first = fx.run({"write": "src/app.py", "text": "round one\n"}, {"deliver": True, "report": "r1", "result": RESULT})
        fx.runtime.decide(first["run_id"], "accepted_with_corrections", "next_round", "缺少 CSV 支持", ["读了 changes.patch"])
        messages = fx.packet()["user_messages"] + [{"text": "还要支持 CSV", "source": "human"}]
        second = fx.run({"capture_brief": str(fx.root / "brief2.md")}, {"write": "src/csv.py", "text": "csv\n"},
                        {"deliver": True, "report": "r2", "result": RESULT},
                        continue_from=first["run_id"], user_messages=messages)
        outcome = second["outcome"]
        self.assertEqual((second["round"], outcome["run_outcome"]), (2, "ok"))
        self.assertEqual([item["path"] for item in outcome["changes"]["files"]], ["src/csv.py"])
        delivery = (fx.run_dir(second) / "delivery.patch").read_text()
        self.assertIn("src/app.py", delivery)
        self.assertIn("src/csv.py", delivery)
        self.assertEqual(outcome["changes"]["delivery_file_count"], 2)
        brief = (fx.root / "brief2.md").read_text()
        self.assertIn('new_this_round="true"', brief)
        self.assertIn("缺少 CSV 支持", brief)
        command = json.loads((fx.run_dir(second) / "command.json").read_text())["argv"]
        self.assertEqual(command[command.index("--resume") + 1], first["outcome"]["session_id"])

    def test_resume_failure_falls_back_to_a_fresh_session(self):
        fx = self.fx
        first = fx.run({"deliver": True, "report": "r1", "result": RESULT})
        os.environ["FAKE_RESUME_FAIL"] = "1"
        self.addCleanup(os.environ.pop, "FAKE_RESUME_FAIL", None)
        second = fx.run({"deliver": True, "report": "r2", "result": RESULT}, continue_from=first["run_id"])
        self.assertEqual(second["outcome"]["run_outcome"], "ok")
        self.assertIn("resume_fallback", fx.warnings(second))
        self.assertNotEqual(second["outcome"]["session_id"], first["outcome"]["session_id"])

    def test_continue_requires_appended_words_and_a_stopped_run_of_the_same_task(self):
        fx = self.fx
        first = fx.run({"deliver": True, "report": "r", "result": RESULT})
        with self.assertRaisesRegex(ValueError, "verbatim"):
            fx.runtime.start(fx.packet(continue_from=first["run_id"], user_messages=[{"text": "改过的原话", "source": "human"}]))
        with self.assertRaisesRegex(ValueError, "task_id"):
            fx.runtime.start(fx.packet(task_id="T-2", continue_from=first["run_id"]))

    def test_one_active_run_per_task(self):
        fx = self.fx
        fx.script({"sleep": 30})
        run_id = fx.runtime.start(fx.packet())["run_id"]
        with self.assertRaisesRegex(RuntimeError, "already has an active run"):
            fx.runtime.start(fx.packet())
        fx.runtime.cancel(run_id, "test cleanup")
        fx.wait(run_id)

    def test_decide_requires_item_and_protected_confirmation_then_cleanup(self):
        fx = self.fx
        items = [{"id": "F1", "title": "导出未处理超时", "confidence": "verified"}]
        snapshot = fx.run({"write": "migrations/1.sql", "text": "x"},
                          {"deliver": True, "report": "r", "result": {**RESULT, "items": items}}, protected=["migrations/"])
        run_id = snapshot["run_id"]
        with self.assertRaisesRegex(RuntimeError, "decide the run"):
            fx.runtime.cleanup(run_id)
        with self.assertRaisesRegex(ValueError, "item_decisions"):
            fx.runtime.decide(run_id, "accepted", "done", "ok", ["ran tests"])
        with self.assertRaisesRegex(ValueError, "protected_confirmed"):
            fx.runtime.decide(run_id, "accepted", "done", "ok", ["ran tests"],
                              item_decisions=[{"id": "F1", "disposition": "accepted"}])
        decided = fx.runtime.decide(run_id, "accepted", "done", "ok", ["ran tests"],
                                    item_decisions=[{"id": "F1", "disposition": "accepted"}],
                                    protected_confirmed=["migrations/1.sql"])
        self.assertEqual((decided["decision"]["verdict"], decided["decision"]["next"]), ("accepted", "done"))
        fx.runtime.decide(run_id, "rejected", "codex_finishes", "复核后发现回归", ["pytest -q failed"],
                          item_decisions=[{"id": "F1", "disposition": "rejected", "reason": "不成立"}],
                          protected_confirmed=["migrations/1.sql"])
        self.assertEqual(fx.runtime.snapshot(run_id)["decision_history_count"], 2)
        copy_root = Path(snapshot["lineage_root"]) / "copy"
        self.assertTrue(copy_root.is_dir())
        self.assertEqual(fx.runtime.cleanup(run_id)["state"], "removed")
        self.assertFalse(copy_root.exists())
        self.assertTrue((fx.run_dir(snapshot) / "changes.patch").is_file())
        with self.assertRaisesRegex(ValueError, "removed"):
            fx.runtime.start(fx.packet(continue_from=run_id))

    def test_a_restarted_runtime_settles_a_dead_bridge_as_lost(self):
        fx = self.fx
        fx.script({"write": "src/app.py", "text": "partial"}, {"touch": str(fx.root / "started")}, {"sleep": 60})
        run_id = fx.runtime.start(fx.packet())["run_id"]
        deadline = time.monotonic() + 20
        while not (fx.root / "started").exists() and time.monotonic() < deadline:
            time.sleep(.05)
        run_dir = fx.runtime.run_dir(run_id)
        bridge_pid = json.loads((run_dir / "bridge.json").read_text())["pid"]
        child = json.loads((run_dir / "child.json").read_text())
        fx.runtime._workers.pop(run_id)
        os.kill(bridge_pid, signal.SIGKILL)
        time.sleep(.3)
        restarted = Runtime(fx.state)
        snapshot = restarted.snapshot(run_id)
        self.assertEqual(snapshot["status"], "lost")
        self.assertEqual(snapshot["outcome"]["process_stopped"], "stopped")
        with self.assertRaises(ProcessLookupError):
            os.killpg(child["pid"], 0)
        self.assertEqual(snapshot["outcome"]["changes"]["file_count"], 1)
        restarted.decide(run_id, "rejected", "next_round", "运行中断", ["read the partial patch"])


class ReadonlyProfileTests(unittest.TestCase):
    def setUp(self):
        self.fx = harness.Fixture(self)
        self.plain = self.fx.root / "plain-docs"
        self.plain.mkdir()
        (self.plain / "design.md").write_text("设计稿")

    def test_readonly_analysis_delivers_through_structured_output(self):
        fx = self.fx
        structured = {"status": "completed", "summary": "可行，但有两个风险", "report_markdown": "## 需求对照\n- 可行性：已回答\n",
                      "items": [{"id": "R1", "title": "风险", "confidence": "inferred"}]}
        snapshot = fx.run({"structured": structured}, kind="analyze", cwd=str(self.plain), inputs=[],
                          user_messages=[{"text": "这个设计可行吗？", "source": "human"}], web=True)
        outcome = snapshot["outcome"]
        run_dir = fx.run_dir(snapshot)
        self.assertEqual((snapshot["profile"], outcome["run_outcome"], outcome["claimed_status"]), ("readonly", "ok", "completed"))
        self.assertEqual((run_dir / "report.md").read_text(), "## 需求对照\n- 可行性：已回答\n")
        self.assertNotIn("report_markdown", json.loads((run_dir / "result.json").read_text()))
        self.assertIsNone(outcome["changes"])
        command = json.loads((run_dir / "command.json").read_text())["argv"]
        self.assertNotEqual(command[0], "/usr/bin/sandbox-exec")
        self.assertEqual(command[command.index("--tools") + 1], "Read,Glob,Grep,WebFetch,WebSearch")
        self.assertIn("--json-schema", command)
        self.assertEqual(outcome["items"], 1)

    def test_implement_needs_a_git_repository(self):
        with self.assertRaisesRegex(ValueError, "implement tasks need a Git repository"):
            self.fx.runtime.start(self.fx.packet(cwd=str(self.plain)))


if __name__ == "__main__":
    unittest.main()
