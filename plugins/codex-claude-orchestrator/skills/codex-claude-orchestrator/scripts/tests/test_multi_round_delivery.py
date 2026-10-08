"""Multi-round flows: which code the next round starts from, which decisions it inherits, which patch is delivered."""
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness  # noqa: E402

GOOD = {"status": "completed", "summary": "done"}
MAC = sys.platform == "darwin" and Path("/usr/bin/sandbox-exec").is_file()


def run(command, cwd=None):
    return subprocess.run(command, cwd=cwd, capture_output=True, text=True)


@unittest.skipUnless(MAC, "the copy profile needs macOS sandbox-exec")
class MultiRoundDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.fx = harness.Fixture(self)

    def apply(self, snapshot):
        changes = snapshot["outcome"]["changes"]
        if not changes["delivery_files"]:
            self.assertEqual((changes["check_hint"], changes["apply_hint"]), (None, None))
            return
        for command in (changes["check_hint"], changes["apply_hint"]):
            completed = run(shlex.split(command))
            self.assertEqual(completed.returncode, 0, (command, completed.stderr))

    def words(self, *extra):
        return self.fx.packet()["user_messages"] + [{"text": text, "source": "human"} for text in extra]

    def copy(self, snapshot):
        return Path(snapshot["lineage_root"]) / "copy"

    def delivered(self, snapshot):
        return sorted(item["path"] for item in snapshot["outcome"]["changes"]["delivery_files"])

    def test_codex_corrections_become_the_next_rounds_base(self):
        fx = self.fx
        one = fx.run({"write": "src/value.py", "text": "def value():\n    return 'claude_v1'\n"},
                     {"deliver": True, "report": "r1", "result": GOOD})
        self.apply(one)
        (fx.source / "src" / "value.py").write_text("def value():\n    return {'text': 'codex_corrected'}\n")
        fx.runtime.decide(one["run_id"], "accepted_with_corrections", "next_round", "value 改成返回 dict",
                          ["applied delivery.patch", "corrected src/value.py"], applied=True)
        two = fx.run({"capture_brief": str(fx.root / "brief2.md")},
                     {"write": "src/export.py", "text": "from src.value import value\n\ndef export():\n    return value()['text']\n"},
                     {"deliver": True, "report": "r2", "result": GOOD},
                     continue_from=one["run_id"], user_messages=self.words("再加一个导出接口"))
        self.assertIn("codex_corrected", (self.copy(two) / "src" / "value.py").read_text())
        self.assertIn("工作目录按原仓库的当前状态重新建立", (fx.root / "brief2.md").read_text())
        self.assertEqual(self.delivered(two), ["src/export.py"])
        self.apply(two)
        check = run([sys.executable, "-c", "from src.export import export; print(export())"], cwd=fx.source)
        self.assertEqual((check.returncode, check.stdout.strip()), (0, "codex_corrected"), check.stderr)

    def test_rejected_then_corrected_work_is_delivered_once_from_the_last_round(self):
        fx = self.fx
        one = fx.run({"write": "src/new_feature.py", "text": "def feature():\n    return 1\n"},
                     {"deliver": True, "report": "r1", "result": GOOD})
        fx.runtime.decide(one["run_id"], "rejected", "next_round", "返回值不对", ["read changes.patch"])
        two = fx.run({"write": "src/new_feature.py", "text": "def feature():\n    return 2\n"},
                     {"deliver": True, "report": "r2", "result": GOOD},
                     continue_from=one["run_id"], user_messages=self.words("返回 2"))
        self.assertEqual([item["status"] for item in two["outcome"]["changes"]["files"]], ["M"])
        self.assertEqual(two["outcome"]["changes"]["delivery_files"], [{"path": "src/new_feature.py", "status": "A"}])
        self.assertEqual(two["outcome"]["carried"], {"from_run": one["run_id"], "mode": "replayed", "files": 1})
        self.apply(two)
        self.assertEqual((fx.source / "src" / "new_feature.py").read_text(), "def feature():\n    return 2\n")

    def test_a_first_round_that_never_started_does_not_lose_later_work(self):
        fx = self.fx
        os.environ["FAKE_LOGGED_IN"] = "0"
        try:
            one = fx.run()
        finally:
            os.environ.pop("FAKE_LOGGED_IN")
        self.assertEqual(one["status"], "not_started")
        two = fx.run({"write": "src/a.py", "text": "a\n"}, {"deliver": True, "report": "r2", "result": GOOD},
                     continue_from=one["run_id"])
        three = fx.run({"write": "src/b.py", "text": "b\n"}, {"deliver": True, "report": "r3", "result": GOOD},
                       continue_from=two["run_id"])
        self.assertEqual([item["path"] for item in three["outcome"]["changes"]["files"]], ["src/b.py"])
        self.assertEqual(self.delivered(three), ["src/a.py", "src/b.py"])
        self.apply(three)

    def test_item_decisions_reach_the_next_brief_with_their_authority(self):
        fx = self.fx
        items = [{"id": "F1", "title": "导出没有超时处理", "confidence": "inferred"},
                 {"id": "F2", "title": "缺少失败提示", "confidence": "verified"}]
        one = fx.run({"deliver": True, "report": "r1", "result": {**GOOD, "items": items}}, kind="analyze")
        fx.runtime.decide(one["run_id"], "accepted_with_corrections", "next_round", "按逐项裁决继续分析", ["read report"],
                          item_decisions=[{"id": "F1", "disposition": "rejected", "reason": "误报：export() 已有 30 秒超时"},
                                          {"id": "F2", "disposition": "accepted"}])
        fx.run({"capture_brief": str(fx.root / "brief2.md")}, {"deliver": True, "report": "r2", "result": GOOD},
               kind="analyze", continue_from=one["run_id"])
        brief = (fx.root / "brief2.md").read_text()
        self.assertIn("F1 不成立：误报：export() 已有 30 秒超时", brief)
        self.assertIn("F2 成立", brief)
        self.assertIn(str(fx.run_dir(one) / "decision.json"), brief)
        self.assertIn("效力高于这些轮次的报告", brief)

    def test_applied_work_without_a_declaration_is_detected_but_corrections_need_one(self):
        fx = self.fx
        one = fx.run({"write": "src/app.py", "text": "print('v2')\n"}, {"deliver": True, "report": "r1", "result": GOOD})
        self.apply(one)
        fx.runtime.decide(one["run_id"], "accepted", "next_round", "继续", ["applied"])
        two = fx.run({"write": "src/more.py", "text": "x\n"}, {"deliver": True, "report": "r2", "result": GOOD},
                     continue_from=one["run_id"])
        self.assertIn("carried_already_in_original", fx.warnings(two))
        self.assertEqual(self.delivered(two), ["src/more.py"])
        self.apply(two)
        (fx.source / "src" / "more.py").write_text("x corrected\n")
        fx.runtime.decide(two["run_id"], "accepted_with_corrections", "next_round", "改了 more.py", ["applied, corrected"])
        three = fx.run(continue_from=two["run_id"])
        self.assertEqual(three["status"], "not_started")
        self.assertIn("applied=true", three["outcome"]["note"])
        fx.runtime.decide(two["run_id"], "accepted_with_corrections", "next_round", "改了 more.py", ["applied, corrected"],
                          applied=True)
        four = fx.run({"deliver": True, "report": "r4", "result": GOOD}, continue_from=three["run_id"])
        self.assertEqual(four["status"], "ok")
        self.assertEqual((self.copy(four) / "src" / "more.py").read_text(), "x corrected\n")
        self.assertEqual(self.delivered(four), [])

    def test_unrelated_original_edits_are_picked_up_and_undelivered_work_replayed(self):
        fx = self.fx
        one = fx.run({"write": "src/feature.py", "text": "f\n"}, {"deliver": True, "report": "r1", "result": GOOD})
        fx.runtime.decide(one["run_id"], "accepted", "next_round", "继续", ["read patch"])
        (fx.source / "README.md").write_text("user edit between rounds\n")
        two = fx.run({"deliver": True, "report": "r2", "result": GOOD}, continue_from=one["run_id"])
        self.assertEqual((self.copy(two) / "README.md").read_text(), "user edit between rounds\n")
        self.assertEqual((self.copy(two) / "src" / "feature.py").read_text(), "f\n")
        self.assertEqual(self.delivered(two), ["src/feature.py"])
        self.apply(two)

    def test_any_leftover_copy_state_is_rebuilt_on_the_next_round(self):
        fx = self.fx
        one = fx.run({"write": "src/feature.py", "text": "def feature():\n    return 42\n"},
                     {"deliver": True, "report": "r1", "result": GOOD})
        lineage = Path(one["lineage_root"])
        damages = {
            "copy_and_identity_gone": lambda: (shutil.rmtree(lineage / "copy"), (lineage / "copy.json").unlink()),
            "identity_gone": lambda: (lineage / "copy.json").unlink(),
            "copy_half_deleted": lambda: shutil.rmtree(lineage / "copy" / "src"),
        }
        previous = one
        for name, damage in damages.items():
            with self.subTest(name):
                damage()
                current = fx.run({"deliver": True, "report": name, "result": GOOD}, continue_from=previous["run_id"])
                self.assertEqual(current["status"], "ok", current["outcome"]["note"])
                self.assertEqual((self.copy(current) / "src" / "feature.py").read_text(), "def feature():\n    return 42\n")
                self.assertEqual(self.delivered(current), ["src/feature.py"])
                previous = current
        self.apply(previous)

    def test_a_round_that_cannot_build_its_copy_does_not_lose_earlier_work(self):
        fx = self.fx
        one = fx.run({"write": "src/feature.py", "text": "def feature():\n    return 42\n"},
                     {"deliver": True, "report": "r1", "result": GOOD})
        outside = fx.root / "outside.py"
        outside.write_text("x\n")
        link = fx.source / "external-link"
        link.symlink_to(outside)
        two = fx.run(continue_from=one["run_id"])
        self.assertEqual(two["status"], "not_started")
        link.unlink()
        three = fx.run({"write": "src/next.py", "text": "from .feature import feature\n"},
                       {"deliver": True, "report": "r3", "result": GOOD}, continue_from=two["run_id"])
        self.assertEqual(self.delivered(three), ["src/feature.py", "src/next.py"])
        self.apply(three)
        check = run([sys.executable, "-c", "from src.feature import feature; print(feature())"], cwd=fx.source)
        self.assertEqual(check.stdout.strip(), "42", check.stderr)

    def test_applied_work_corrected_back_to_the_old_state_reaches_the_copy(self):
        fx = self.fx
        (fx.source / "src" / "value.py").write_text("def value():\n    return 'v1'\n")
        harness.git(fx.source, "add", "src/value.py")
        harness.git(fx.source, "commit", "-qm", "chore:value")
        one = fx.run({"write": "src/value.py", "text": "def value():\n    return {'value': 'v1'}\n"},
                     {"deliver": True, "report": "r1", "result": GOOD})
        self.apply(one)
        (fx.source / "src" / "value.py").write_text("def value():\n    return 'v1'\n")
        fx.runtime.decide(one["run_id"], "accepted_with_corrections", "next_round", "改回字符串返回值", ["applied, reverted"],
                          applied=True)
        two = fx.run({"deliver": True, "report": "r2", "result": GOOD}, continue_from=one["run_id"])
        self.assertEqual((self.copy(two) / "src" / "value.py").read_text(), "def value():\n    return 'v1'\n")
        self.assertEqual(self.delivered(two), [])

    def test_ignored_task_files_stay_in_delivery_and_in_later_copies(self):
        fx = self.fx
        one = fx.run({"write": "src/feature.py", "text": "def feature():\n    return 42\n"},
                     {"deliver": True, "report": "r1", "result": GOOD})
        (fx.source / ".gitignore").write_text("build/\nsrc/feature.py\n")
        two = fx.run({"write": "src/next.py", "text": "from .feature import feature\n"},
                     {"deliver": True, "report": "r2", "result": GOOD}, continue_from=one["run_id"])
        self.assertEqual(fx.warnings(two)["undelivered_files_now_ignored"]["paths"], ["src/feature.py"])
        self.assertEqual(self.delivered(two), ["src/feature.py", "src/next.py"])
        self.apply(two)
        fx.runtime.decide(two["run_id"], "accepted", "next_round", "ignored 文件也要交付", ["applied"], applied=True)
        three = fx.run({"deliver": True, "report": "r3", "result": GOOD}, continue_from=two["run_id"])
        self.assertEqual((self.copy(three) / "src" / "feature.py").read_text(), "def feature():\n    return 42\n")
        check = run([sys.executable, "-c", "from src.next import feature; print(feature())"], cwd=self.copy(three))
        self.assertEqual(check.stdout.strip(), "42", check.stderr)
        self.assertEqual(self.delivered(three), [])

    def test_ignored_task_files_reach_later_copies_when_the_merge_is_detected_not_declared(self):
        fx = self.fx
        one = fx.run({"write": "src/feature.py", "text": "def feature():\n    return 42\n"},
                     {"deliver": True, "report": "r1", "result": GOOD})
        (fx.source / ".gitignore").write_text("build/\nsrc/feature.py\n")
        two = fx.run({"write": "src/next.py", "text": "from .feature import feature\n"},
                     {"deliver": True, "report": "r2", "result": GOOD}, continue_from=one["run_id"])
        self.apply(two)
        fx.runtime.decide(two["run_id"], "accepted", "next_round", "已合入但没有声明", ["applied"])
        three = fx.run({"deliver": True, "report": "r3", "result": GOOD}, continue_from=two["run_id"])
        self.assertIn("carried_already_in_original", fx.warnings(three))
        four = fx.run({"deliver": True, "report": "r4", "result": GOOD}, continue_from=three["run_id"])
        for current in (three, four):
            check = run([sys.executable, "-c", "from src.next import feature; print(feature())"], cwd=self.copy(current))
            self.assertEqual(check.stdout.strip(), "42", check.stderr)

    def test_deciding_again_without_applied_keeps_the_recorded_merge(self):
        fx = self.fx
        one = fx.run({"write": "src/value.py", "text": "def value():\n    return 'v1'\n"},
                     {"deliver": True, "report": "r1", "result": GOOD})
        self.apply(one)
        (fx.source / "src" / "value.py").write_text("def value():\n    return 'corrected'\n")
        fx.runtime.decide(one["run_id"], "accepted_with_corrections", "next_round", "已合入并修正", ["applied"], applied=True)
        decided = fx.runtime.decide(one["run_id"], "accepted_with_corrections", "next_round", "补充：测试也通过",
                                    ["applied", "pytest"])
        self.assertTrue(decided["decision"]["applied"])
        two = fx.run({"deliver": True, "report": "r2", "result": GOOD}, continue_from=one["run_id"])
        self.assertEqual(two["status"], "ok", two["outcome"]["note"])
        self.assertEqual((self.copy(two) / "src" / "value.py").read_text(), "def value():\n    return 'corrected'\n")
        withdrawn = fx.runtime.decide(one["run_id"], "accepted_with_corrections", "next_round", "撤回合入", ["reverted"],
                                      applied=False)
        self.assertFalse(withdrawn["decision"]["applied"])

    def test_report_files_never_enter_delivery_when_the_outbox_name_collides(self):
        fx = self.fx
        (fx.source / ".codex-run").mkdir()
        (fx.source / ".codex-run" / "config.txt").write_text("project file\n")
        harness.git(fx.source, "add", ".codex-run/config.txt")
        harness.git(fx.source, "commit", "-qm", "chore:project dir")
        one = fx.run({"write": "src/feature.py", "text": "f\n"}, {"deliver": True, "report": "r1", "result": GOOD})
        two = fx.run({"write": "src/next.py", "text": "n\n"}, {"deliver": True, "report": "r2", "result": GOOD},
                     continue_from=one["run_id"])
        self.assertEqual(self.delivered(two), ["src/feature.py", "src/next.py"])
        self.assertEqual((fx.run_dir(two) / "report.md").read_text(), "r2")

    def test_a_retargeted_symlink_in_the_original_reaches_the_copy(self):
        fx = self.fx
        for name in ("a", "b", "c"):
            (fx.source / "src" / f"{name}.py").write_text(f"value = '{name}'\n")
        link = fx.source / "src" / "current.py"
        link.symlink_to("a.py")
        harness.git(fx.source, "add", "src")
        harness.git(fx.source, "commit", "-qm", "chore:link")
        link.unlink()
        link.symlink_to("b.py")
        one = fx.run({"deliver": True, "report": "r1", "result": GOOD})
        link.unlink()
        link.symlink_to("c.py")
        two = fx.run({"deliver": True, "report": "r2", "result": GOOD}, continue_from=one["run_id"])
        self.assertEqual(os.readlink(self.copy(two) / "src" / "current.py"), "c.py")

    def test_only_the_latest_round_that_changed_the_copy_can_be_recorded_as_applied(self):
        fx = self.fx
        one = fx.run({"write": "src/a.py", "text": "a\n"}, {"deliver": True, "report": "r1", "result": GOOD})
        fx.run({"deliver": True, "report": "r2", "result": GOOD}, continue_from=one["run_id"])
        with self.assertRaisesRegex(ValueError, "latest round that changed the copy"):
            fx.runtime.decide(one["run_id"], "accepted", "done", "ok", ["x"], applied=True)


if __name__ == "__main__":
    unittest.main()
