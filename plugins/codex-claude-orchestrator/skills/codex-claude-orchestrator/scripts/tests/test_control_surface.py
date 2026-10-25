"""Control store transactions and the Runtime surface Codex uses during a run: reply, wait, ownership, titles, workbench."""
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness  # noqa: E402
import cli_env  # noqa: E402
import control_store  # noqa: E402
from control_store import CommandConflict, ControlStore, StaleRevision, StoreError  # noqa: E402
from events import append  # noqa: E402
import process_control  # noqa: E402
import process_family  # noqa: E402
import bridge  # noqa: E402
import runtime as runtime_module  # noqa: E402

CHAT_A = {"chat": "chat-a"}
CHAT_B = {"chat": "chat-b"}


def seed(fx, run_id, task_id="T-1", context=CHAT_A, status="running", **fields):
    """A registered run whose bridge is this test process, so the Runtime treats it as live."""
    run_dir = fx.runtime.run_dir(run_id)
    run_dir.mkdir()
    record = {"run_id": run_id, "task_id": task_id, "kind": "implement", "profile": "copy", "round": 1,
              "cwd": str(fx.source), "title": task_id, "title_source": "task_id", "round_title": None, "status": status,
              "started_at": time.time(), "updated_at": time.time(), "ended_at": None, "decision": None,
              "run_dir": str(run_dir), "lineage_root": str(run_dir), "copy_removed": False,
              "bridge": process_control.identity(os.getpid()), **fields}
    fx.runtime._update(lambda data: data["runs"].__setitem__(run_id, record))
    resolved = runtime_module.resolve_context(context)
    fx.runtime.store.register_run(run_id, task_id, resolved, "bridge-worker", True)
    return run_dir


def seed_legacy(fx, run_id, task_id="T-1", **fields):
    """An ended 1.0.x record: registry only, no control-store row and no title_source."""
    run_dir = fx.runtime.run_dir(run_id)
    run_dir.mkdir()
    record = {"run_id": run_id, "task_id": task_id, "kind": "implement", "profile": "copy", "round": 1, "cwd": str(fx.source),
              "title": "把导出改成异步，失败要提示原因", "status": "ok", "started_at": time.time(), "updated_at": time.time(),
              "ended_at": time.time(), "decision": None, "continue_from": None, "run_dir": str(run_dir),
              "lineage_root": str(run_dir), "copy_removed": False, "process_stopped": "confirmed", **fields}
    fx.runtime._update(lambda data: data["runs"].__setitem__(run_id, record))
    return run_dir


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.fx = harness.Fixture(self)
        self.store = ControlStore(self.fx.root / "store" / "control.sqlite3")
        self.store.register_run("run-store0001", "T", {"namespace": "n", "chat": "a"}, "bridge-worker", True)
        self.question = self.store.open_interaction("run-store0001", "question", blocking=True, request={"questions": []})

    def test_the_rollback_journal_is_used_and_verified(self):
        journal = self.store.journal()
        self.assertEqual((journal["journal_mode"], journal["wal"]), ("delete", False))
        self.assertEqual(journal["sqlite_version"], sqlite3.sqlite_version)

    def test_the_same_command_is_idempotent_and_a_different_payload_conflicts(self):
        first = self.store.submit("run-store0001", "c1", "update", {"text": "x"}, "coordinator")
        again = self.store.submit("run-store0001", "c1", "update", {"text": "x"}, "coordinator")
        self.assertEqual((first["duplicate"], again["duplicate"], again["revision"]), (False, True, first["revision"]))
        with self.assertRaises(CommandConflict):
            self.store.submit("run-store0001", "c1", "update", {"text": "y"}, "coordinator")
        with self.assertRaises(CommandConflict):
            self.store.submit("run-store0001", "c1", "update", {"text": "x"}, "human")
        self.assertEqual(len(self.store.queued("run-store0001")), 1)

    def test_a_stale_revision_and_a_second_answer_are_refused(self):
        revision = self.store.run("run-store0001")["revision"]
        with self.assertRaises(StaleRevision):
            self.store.submit("run-store0001", "c2", "update", {"text": "x"}, "coordinator", expected_revision=revision - 1)
        self.store.submit("run-store0001", "a1", "answer", {"text": "yes"}, "coordinator",
                          interaction_id=self.question["interaction_id"], expected_revision=revision)
        with self.assertRaisesRegex(StaleRevision, "already answered by command a1"):
            self.store.submit("run-store0001", "a2", "answer", {"text": "no"}, "human",
                              interaction_id=self.question["interaction_id"])
        stored = self.store.interaction(self.question["interaction_id"])
        self.assertEqual((stored["answer"], stored["answered_by"]), ({"text": "yes"}, "coordinator"))

    def test_concurrent_answers_have_exactly_one_winner(self):
        outcomes = []

        def answer(index):
            try:
                ControlStore(self.store.path).submit("run-store0001", f"race-{index}", "answer", {"text": str(index)},
                                                     "coordinator", interaction_id=self.question["interaction_id"])
                outcomes.append("won")
            except StaleRevision:
                outcomes.append("refused")
        threads = [threading.Thread(target=answer, args=(index,)) for index in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sorted(outcomes), ["refused"] * 7 + ["won"])
        self.assertEqual(len(self.store.queued("run-store0001")), 1)

    def test_another_chat_cannot_command_or_read_the_run(self):
        with self.assertRaisesRegex(StoreError, "another Codex chat"):
            self.store.submit("run-store0001", "c3", "update", {"text": "x"}, "coordinator",
                              context={"namespace": "n", "chat": "b"})
        with self.assertRaisesRegex(StoreError, "another Codex chat"):
            self.store.check_context("run-store0001", {"namespace": "other", "chat": "a"})
        self.assertIsNotNone(self.store.check_context("run-store0001", {"namespace": "n", "chat": "a"}))

    def test_input_cannot_close_over_a_queued_command_and_the_end_abandons_what_is_open(self):
        self.store.submit("run-store0001", "c4", "update", {"text": "late"}, "coordinator")
        self.assertFalse(self.store.set_state("run-store0001", "closing", expect=("running",)))
        self.store.mark_delivery("run-store0001", "c4", "written")
        self.assertTrue(self.store.set_state("run-store0001", "closing", expect=("running",)))
        with self.assertRaisesRegex(StoreError, "no longer accepts input"):
            self.store.submit("run-store0001", "c5", "update", {"text": "too late"}, "coordinator")
        self.store.set_state("run-store0001", "ended")
        self.assertEqual(self.store.interaction(self.question["interaction_id"])["status"], "abandoned")

    def test_notes_are_acknowledged_in_the_same_transaction(self):
        note = self.store.open_interaction("run-store0001", "note", blocking=False, request={"text": "fyi"})
        self.assertEqual((note["status"], note["blocking"]), ("acknowledged", False))
        with self.assertRaisesRegex(StoreError, "interaction_id was not found|already acknowledged"):
            self.store.submit("run-store0001", "c6", "answer", {"text": "x"}, "coordinator",
                              interaction_id=note["interaction_id"])

    def test_one_automatic_open_per_chat_and_only_an_explicit_reopen_after_it(self):
        first = self.store.reserve_opening("n", "a")
        self.assertEqual(first["action"], "open_in_codex")
        self.assertEqual(self.store.reserve_opening("n", "a")["action"], "link_only")
        self.store.record_opening("n", "a", "unknown")
        self.assertEqual(self.store.reserve_opening("n", "a")["action"], "link_only")
        self.assertEqual(self.store.reserve_opening("n", "b")["action"], "open_in_codex")
        self.assertEqual(self.store.reserve_opening("m", "a")["action"], "open_in_codex")
        self.assertEqual(self.store.reserve_opening("n", "a", reopen=True)["action"], "open_in_codex")
        with self.assertRaises(StoreError):
            self.store.record_opening("n", "never", "queued")


class RuntimeSurfaceTests(unittest.TestCase):
    def setUp(self):
        self.fx = harness.Fixture(self)
        self.runtime = self.fx.runtime

    def question(self, run_id):
        return self.runtime.store.open_interaction(run_id, "question", blocking=True,
                                                   request={"questions": [{"question": "哪个方案？"}]})

    def test_reply_returns_a_durable_receipt_and_replays_are_idempotent(self):
        seed(self.fx, "run-surface001")
        asked = self.question("run-surface001")
        first = self.runtime.reply("run-surface001", "r1", "answer", "方案 B", interaction_id=asked["interaction_id"],
                                   origin="human", context=CHAT_A)
        self.assertEqual((first["receipt"]["delivery"]["state"], first["receipt"]["origin"]), ("queued", "human"))
        self.assertEqual(first["pending"], [])
        again = self.runtime.reply("run-surface001", "r1", "answer", "方案 B", interaction_id=asked["interaction_id"],
                                   origin="human", context=CHAT_A)
        self.assertTrue(again["receipt"]["duplicate"])
        with self.assertRaisesRegex(RuntimeError, "different payload"):
            self.runtime.reply("run-surface001", "r1", "answer", "方案 C", interaction_id=asked["interaction_id"],
                               origin="human", context=CHAT_A)

    def test_two_chats_with_the_same_task_name_are_kept_apart(self):
        seed(self.fx, "run-surface002", "SAME", CHAT_A)
        seed(self.fx, "run-surface003", "SAME", CHAT_B)
        self.assertEqual([row["run_id"] for row in self.runtime.list_runs(context=CHAT_A)], ["run-surface002"])
        self.assertEqual([row["run_id"] for row in self.runtime.list_runs(context=CHAT_B)], ["run-surface003"])
        with self.assertRaisesRegex(RuntimeError, "another Codex chat"):
            self.runtime.reply("run-surface002", "x1", "update", "hi", context=CHAT_B)
        with self.assertRaisesRegex(RuntimeError, "another Codex chat"):
            self.runtime.cancel("run-surface002", "stop", CHAT_B)
        with self.assertRaisesRegex(RuntimeError, "another Codex chat"):
            self.runtime.snapshot("run-surface003", CHAT_A)
        self.assertEqual(self.runtime.snapshot("run-surface003", CHAT_B)["context"], {"namespace": "default", "chat": "chat-b"})

    def test_a_chat_lists_the_chat_less_earlier_rounds_of_its_own_lineages_only(self):
        old = seed_legacy(self.fx, "run-lineage0001", "OLD")
        seed(self.fx, "run-lineage0002", "OLD", CHAT_A, status="ok", round=2, continue_from="run-lineage0001",
             lineage_root=str(old), title="OLD")
        bare = seed(self.fx, "run-lineage0003", "LATE", context=None, status="ok")
        seed(self.fx, "run-lineage0004", "LATE", CHAT_A, status="ok", round=2, continue_from="run-lineage0003",
             lineage_root=str(bare))
        seed_legacy(self.fx, "run-lineage0005", "OLD")
        seed(self.fx, "run-lineage0006", "OLD", CHAT_B, status="ok")
        seed(self.fx, "run-lineage0007", "LATE", context=None, status="ok")
        before = self.runtime._registry()["runs"]["run-lineage0001"]

        def listed(context, **kwargs):
            return {row["run_id"]: row["context"] for row in self.runtime.list_runs(context=context, **kwargs)}
        self.assertEqual(listed(CHAT_A), {"run-lineage0001": None, "run-lineage0002": {"namespace": "default", "chat": "chat-a"},
                                          "run-lineage0003": None, "run-lineage0004": {"namespace": "default", "chat": "chat-a"}})
        self.assertEqual(listed(CHAT_B), {"run-lineage0006": {"namespace": "default", "chat": "chat-b"}})
        self.assertEqual(set(listed(CHAT_A, task_id="OLD")), {"run-lineage0001", "run-lineage0002"})
        roots = {row["run_id"]: row["lineage_root"] for row in self.runtime.list_runs(context=CHAT_A)}
        self.assertEqual(roots["run-lineage0001"], roots["run-lineage0002"], "the workbench groups rounds by this key")
        self.assertEqual(self.runtime.snapshot("run-lineage0001")["lineage_root"], roots["run-lineage0002"])
        self.assertEqual(len(listed(None)), 7)
        self.assertIsNone(self.runtime.store.run("run-lineage0001"), "no owner row is written for the 1.0 round")
        self.assertIsNone(self.runtime.store.run("run-lineage0003")["chat"])
        self.assertEqual(self.runtime._registry()["runs"]["run-lineage0001"], before)
        self.runtime.authorize("run-lineage0001", CHAT_B)

    def test_an_omitted_context_never_reaches_a_chat_owned_run(self):
        seed(self.fx, "run-owned00001")
        seed(self.fx, "run-unowned001", "OTHER", context=None)
        for call in (lambda: self.runtime.reply("run-owned00001", "o1", "update", "x"),
                     lambda: self.runtime.cancel("run-owned00001", "stop"),
                     lambda: self.runtime.wait("run-owned00001", timeout=0),
                     lambda: self.runtime.authorize("run-owned00001", None)):
            with self.assertRaisesRegex(RuntimeError, "belongs to a Codex chat"):
                call()
        self.runtime.authorize("run-owned00001", CHAT_A)
        self.runtime.authorize("run-unowned001", None)
        self.runtime.authorize("run-unowned001", CHAT_B)
        self.assertEqual(self.runtime.snapshot("run-owned00001")["run_id"], "run-owned00001", "the read-only workbench still reads")

    def test_admission_is_per_chat_for_identical_task_ids(self):
        seed(self.fx, "run-surface004", "SAME", CHAT_A)
        with mock.patch.object(cli_env, "sandbox_works", return_value={"works": True, "reason": None}):
            with self.assertRaisesRegex(RuntimeError, "already has an active run run-surface004"):
                self.runtime.start(self.fx.packet(task_id="SAME", context=CHAT_A))
            other = self.runtime.start(self.fx.packet(task_id="SAME", context=CHAT_B, title="另一个对话"))
        self.assertEqual((other["task_id"], other["context"]["chat"], other["title"]), ("SAME", "chat-b", "另一个对话"))
        self.fx.wait(other["run_id"])

    def test_without_a_working_write_boundary_no_profile_starts(self):
        with mock.patch.object(cli_env, "sandbox_works", return_value={"works": False, "reason": "refused"}):
            for profile in ("copy", "readonly"):
                with self.assertRaisesRegex(ValueError, f"the {profile} profile needs a working macOS sandbox-exec"):
                    self.runtime.start(self.fx.packet(kind="analyze", profile=profile))
        self.assertEqual(self.runtime.list_runs(), [])

    def test_a_permission_answer_needs_a_decision_and_an_ended_run_takes_no_input(self):
        seed(self.fx, "run-surface005")
        asked = self.runtime.store.open_interaction("run-surface005", "permission", blocking=True,
                                                    request={"tool_name": "Bash", "summary": "rm"})
        with self.assertRaisesRegex(ValueError, "decision=allow or decision=deny"):
            self.runtime.reply("run-surface005", "p1", "answer", "no", interaction_id=asked["interaction_id"], context=CHAT_A)
        self.runtime.reply("run-surface005", "p1", "answer", "no", interaction_id=asked["interaction_id"], decision="deny",
                           context=CHAT_A)
        seed(self.fx, "run-surface006", status="ok")
        with self.assertRaisesRegex(RuntimeError, "already ended"):
            self.runtime.reply("run-surface006", "u1", "update", "more", context=CHAT_A)

    def test_hidden_activity_does_not_end_a_wait_but_a_question_does(self):
        run_dir = seed(self.fx, "run-surface007")
        for _ in range(5):
            append(run_dir, "provider", "provider lifecycle event")
            append(run_dir, "tool", "Read", tool="Read")
        started = time.monotonic()
        quiet = self.runtime.wait("run-surface007", after=0, timeout=1.2, context=CHAT_A)
        self.assertGreaterEqual(time.monotonic() - started, 1.1)
        self.assertEqual((quiet["reason"], quiet["next_cursor"]), ("timeout", 10))

        def ask():
            time.sleep(.4)
            self.question("run-surface007")
            append(run_dir, "question", "Claude asked Codex", text="哪个方案？", status="pending")
        threading.Thread(target=ask, daemon=True).start()
        started = time.monotonic()
        woke = self.runtime.wait("run-surface007", after=quiet["next_cursor"], timeout=10, context=CHAT_A)
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(woke["reason"], "interaction")
        self.assertEqual(woke["pending"][0]["request"]["questions"][0]["question"], "哪个方案？")

    def test_titles_follow_explicit_then_lineage_then_task_id(self):
        packet = {"task_id": "EXPORT-ASYNC", "title": None, "user_messages": [{"text": "很长的原话" * 50}]}
        self.assertEqual(runtime_module._title(packet, None), ("EXPORT-ASYNC", "task_id"))
        self.assertEqual(runtime_module._title({**packet, "title": "导出改异步"}, None), ("导出改异步", "explicit"))
        self.assertEqual(runtime_module._title(packet, {"title": "导出改异步", "title_source": "explicit"}),
                         ("导出改异步", "lineage"))
        self.assertEqual(runtime_module._title(packet, {"title": "很长的原话", "title_source": None}), ("EXPORT-ASYNC", "task_id"))

    def test_packet_titles_are_single_bounded_lines(self):
        normalized = runtime_module.packet_schema.normalize(self.fx.packet(title="  导出\n改成\t异步  " + "很" * 80,
                                                                           round_title="补测试" * 40))
        self.assertNotIn("\n", normalized["title"])
        self.assertEqual(len(normalized["title"]), 60)
        self.assertTrue(normalized["title"].endswith("…"))
        self.assertEqual(len(normalized["round_title"]), 80)
        self.assertEqual(normalized["user_messages"], self.fx.packet()["user_messages"])
        with self.assertRaisesRegex(ValueError, "context must be"):
            runtime_module.packet_schema.normalize(self.fx.packet(context={"thread": "x"}))
        with self.assertRaisesRegex(ValueError, "context.chat"):
            runtime_module.packet_schema.normalize(self.fx.packet(context={"chat": "has space"}))

    def test_the_workbench_opens_once_per_chat_and_never_without_one(self):
        self.assertEqual(self.runtime.workbench(None, reserve=True)["action"], "link_only")
        self.assertEqual(self.runtime.workbench(CHAT_A, reserve=True)["action"], "open_in_codex")
        self.assertEqual(self.runtime.workbench(CHAT_A, reserve=True)["action"], "link_only")
        self.assertEqual(self.runtime.workbench(CHAT_A, reserve=False)["action"], "link_only")
        self.assertEqual(self.runtime.workbench(CHAT_A, reserve=False, outcome="queued")["state"], "queued")
        restarted = runtime_module.Runtime(self.fx.state)
        self.assertEqual(restarted.workbench(CHAT_A, reserve=True)["action"], "link_only")
        self.assertEqual(restarted.workbench(CHAT_A, reserve=True, reopen=True)["action"], "open_in_codex")

    def test_concurrent_decisions_keep_every_history_entry(self):
        run_dir = seed(self.fx, "run-decide0001", status="ok", process_stopped="confirmed")
        other = runtime_module.Runtime(self.fx.state)
        read = runtime_module._read_json
        entered = threading.Event()

        def slow_history(path):
            value = read(path)
            if path.name == "decision-history.json" and threading.current_thread().name.startswith("decider"):
                # Widen the read-modify-write window; serialized deciders cannot both be inside it.
                entered.set()
                time.sleep(.4)
            return value
        errors = []

        def decide(rt, note):
            try:
                rt.decide("run-decide0001", "accepted", "done", note, [note], context=CHAT_A)
            except Exception as exc:  # noqa: BLE001
                errors.append(repr(exc))
        with mock.patch.object(runtime_module, "_read_json", side_effect=slow_history):
            threads = [threading.Thread(target=decide, args=(rt, note), name="decider-" + note)
                       for rt, note in ((self.runtime, "one"), (other, "two"))]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
        history = read(run_dir / "decision-history.json")
        self.assertEqual(errors, [])
        self.assertEqual(sorted(entry["note"] for entry in history), ["one", "two"])
        self.assertEqual(read(run_dir / "decision.json"), history[-1])
        self.assertEqual(self.runtime._record("run-decide0001")["decision"], history[-1])

    def test_a_decision_without_applied_never_reverts_a_concurrent_applied_true(self):
        run_dir = seed(self.fx, "run-applied0001", status="ok", process_stopped="confirmed")
        runtime_module.dump(run_dir / "outcome.json", {"changes": {"end_tree": "fixture", "delivery_files": [{"path": "src/app.py"}]}})
        (run_dir / "delivery.patch").write_text("fixture patch")
        other = runtime_module.Runtime(self.fx.state)
        read = runtime_module._read_json
        stale, done, errors = threading.Event(), threading.Event(), []

        def intercept(path):
            value = read(path)
            if path.name == "outcome.json" and threading.current_thread().name == "stale-note":
                stale.set()
                done.wait(5)
            return value

        def late_note():
            try:
                other.decide("run-applied0001", "accepted", "next_round", "additional evidence", ["extra"], context=CHAT_A)
            except Exception as exc:  # noqa: BLE001
                errors.append(repr(exc))
        with mock.patch.object(runtime_module, "_read_json", side_effect=intercept):
            thread = threading.Thread(target=late_note, name="stale-note")
            thread.start()
            self.assertTrue(stale.wait(5))
            self.runtime.decide("run-applied0001", "accepted", "next_round", "applied delivery", ["merge checked"], applied=True,
                                context=CHAT_A)
            done.set()
            thread.join()
        record = self.runtime._record("run-applied0001")
        self.assertEqual(errors, [])
        self.assertTrue(record["decision"]["applied"], "an omitted applied must keep the recorded merge")
        self.assertEqual([entry["applied"] for entry in read(run_dir / "decision-history.json")], [True, True])
        self.assertIsNone(self.runtime._carry(self.runtime._registry()["runs"], record["lineage_root"])["carry"])

    def test_a_lost_acknowledgment_is_recovered_after_the_run_ended(self):
        seed(self.fx, "run-retry00001")
        first = self.runtime.reply("run-retry00001", "update-1", "update", "new input", context=CHAT_A)["receipt"]
        self.runtime.store.mark_delivery("run-retry00001", "update-1", "written")
        self.runtime.store.mark_delivery("run-retry00001", "update-1", "echoed")
        self.runtime.store.set_state("run-retry00001", "ended")
        self.runtime._update(lambda data: data["runs"]["run-retry00001"].update(status="ok"))
        again = self.runtime.reply("run-retry00001", "update-1", "update", "new input", context=CHAT_A)["receipt"]
        self.assertEqual((again["duplicate"], again["revision"], again["delivery"]["state"]), (True, first["revision"], "echoed"))
        with self.assertRaisesRegex(RuntimeError, "different payload"):
            self.runtime.reply("run-retry00001", "update-1", "update", "changed input", context=CHAT_A)
        with self.assertRaisesRegex(RuntimeError, "another Codex chat"):
            self.runtime.reply("run-retry00001", "update-1", "update", "new input", context=CHAT_B)
        with self.assertRaisesRegex(RuntimeError, "already ended"):
            self.runtime.reply("run-retry00001", "update-2", "update", "new input", context=CHAT_A)

    def test_delivery_evidence_only_moves_forward(self):
        seed(self.fx, "run-forward001")
        self.runtime.reply("run-forward001", "u1", "update", "x", context=CHAT_A)
        store = self.runtime.store
        self.assertTrue(store.mark_delivery("run-forward001", "u1", "written"))
        self.assertTrue(store.mark_delivery("run-forward001", "u1", "echoed"))
        self.assertFalse(store.mark_delivery("run-forward001", "u1", "written"))
        self.assertEqual(store.command("run-forward001", "u1")["delivery"]["state"], "echoed")

    def test_answers_must_cover_every_question_and_keep_their_shape(self):
        seed(self.fx, "run-answers001")
        asked = self.runtime.store.open_interaction("run-answers001", "question", blocking=True,
                                                    request={"questions": [{"question": "A?"}, {"question": "B?"}]})
        ix = asked["interaction_id"]
        with self.assertRaisesRegex(ValueError, "every question needs an answer"):
            self.runtime.reply("run-answers001", "x1", "answer", answers={"A?": "a"}, interaction_id=ix, context=CHAT_A)
        with self.assertRaisesRegex(ValueError, "did not ask"):
            self.runtime.reply("run-answers001", "x2", "answer", answers={"A?": "a", "B?": "b", "C?": "c"}, interaction_id=ix,
                               context=CHAT_A)
        with self.assertRaisesRegex(ValueError, "unpaired surrogate"):
            self.runtime.reply("run-answers001", "x3", "answer", "\ud800", interaction_id=ix, context=CHAT_A)
        with self.assertRaisesRegex(ValueError, "decision belongs to a permission"):
            self.runtime.reply("run-answers001", "x4", "answer", "a", interaction_id=ix, decision="allow", context=CHAT_A)
        receipt = self.runtime.reply("run-answers001", "x5", "answer", answers={"A?": "a", "B?": "b"}, interaction_id=ix,
                                     context=CHAT_A)
        self.assertEqual(self.runtime.store.interaction(ix)["answer"], {"text": "", "answers": {"A?": "a", "B?": "b"}})
        self.assertFalse(receipt["receipt"]["duplicate"])
        entries = runtime_module.duplex.answer_entries(self.runtime.store.interaction(ix), {"text": "", "answers": {"A?": "a", "B?": "b"}})
        self.assertEqual(entries, [("A?", "a"), ("B?", "b")])

    def test_a_continuation_admitted_while_a_decision_lands_uses_the_registry_it_was_published_with(self):
        # Two Runtimes: A plans a continuation and pauses; B records applied=true; A publishes. Formerly A's plan still
        # carried the applied patch while the registry said nothing remained to carry.
        run_dir = seed(self.fx, "run-carryrace01", status="ok", process_stopped="confirmed")
        packet = self.fx.packet(context=CHAT_A, continue_from="run-carryrace01")
        runtime_module.dump(run_dir / "packet.json", packet)
        runtime_module.dump(run_dir / "outcome.json", {"run_outcome": "ok", "changes": {
            "end_tree": "fixture-end-tree", "delivery_files": [{"path": "src/app.py"}]}})
        (run_dir / "delivery.patch").write_text("fixture patch")
        other = runtime_module.Runtime(self.fx.state)
        planned, proceed, errors = threading.Event(), threading.Event(), []
        admit = self.runtime._admit

        def first_plan_pauses(*args):
            value = admit(*args)
            if not planned.is_set():
                planned.set()
                if not proceed.wait(5):
                    raise RuntimeError("fixture deadline")
            return value

        def start():
            try:
                self.runtime.start(packet)
            except Exception as exc:  # noqa: BLE001
                errors.append(str(exc))
        with mock.patch.object(self.runtime, "_admit", side_effect=first_plan_pauses), \
                mock.patch.object(self.runtime, "_profile", return_value="copy"), \
                mock.patch.object(runtime_module.subprocess, "Popen", side_effect=OSError("fixture stops after admission")), \
                mock.patch.object(runtime_module.bridge, "finalize",
                                  side_effect=lambda folder, outcome, note: {"run_outcome": outcome, "warnings": []}):
            thread = threading.Thread(target=start)
            thread.start()
            self.assertTrue(planned.wait(5))
            other.decide("run-carryrace01", "accepted", "next_round", "applied while planned", ["merged"], applied=True,
                         context=CHAT_A)
            proceed.set()
            thread.join(5)
        records = self.runtime._registry()["runs"]
        new = [record for record in records.values() if record["run_id"] != "run-carryrace01"]
        self.assertEqual(len(new), 1)
        plan = runtime_module._read_json(Path(new[0]["run_dir"]) / "plan.json")
        self.assertIsNone(plan["carry"], "the published plan must not carry a delivery recorded as applied")
        self.assertTrue(plan["continued"]["decisions"][0]["decision"]["applied"])
        self.assertIsNone(self.runtime._carry(records, str(run_dir))["carry"])
        self.assertIn("fixture stops after admission", errors[0])

    def test_a_lost_worker_never_leaves_an_observed_markerless_writer_running(self):
        run_dir = self.fx.root / "lost-run"
        run_dir.mkdir()
        leaf_script, beat = self.fx.root / "leaf.py", self.fx.root / "beat.txt"
        leaf_script.write_text("import pathlib, sys, time\nend = time.monotonic() + 30\nwhile time.monotonic() < end:\n"
                               "    pathlib.Path(sys.argv[1]).open('a').write('x')\n    time.sleep(.04)\n")
        root_script = self.fx.root / "cli.py"
        root_script.write_text("import json, os, pathlib, subprocess, sys, time\nenv = dict(os.environ)\n"
                               f"env.pop({bridge.FAMILY_KEY!r}, None)\n"
                               "leaf = subprocess.Popen([sys.executable, sys.argv[1], sys.argv[2]], env=env, start_new_session=True)\n"
                               "pathlib.Path(sys.argv[3]).write_text(json.dumps({'pid': leaf.pid}))\ntime.sleep(30)\n")
        family = process_family.Family()
        root = subprocess.Popen([sys.executable, str(root_script), str(leaf_script), str(beat), str(run_dir / "leaf.json")],
                                env={**os.environ, **family.environment}, start_new_session=True)
        self.addCleanup(lambda: root.poll() is None and root.kill())
        deadline = time.monotonic() + 10
        while not (run_dir / "leaf.json").exists() and time.monotonic() < deadline:
            time.sleep(.02)
        leaf = process_control.identity(json.loads((run_dir / "leaf.json").read_text())["pid"])
        self.addCleanup(lambda: process_control.presence(leaf) == "running" and os.kill(leaf["pid"], 9))
        family.adopt_descendants_of(root.pid)
        family.observe()
        bridge.persist_family(run_dir, family)
        runtime_module.dump(run_dir / "child.json", {"pid": root.pid, "identity": process_control.identity(root.pid),
                                                     "family_marker": family.environment[bridge.FAMILY_KEY]})
        record = {"run_dir": str(run_dir), "process_stopped": "unknown"}
        root.kill()
        root.wait()
        self.assertEqual(process_control.presence(leaf), "running")
        self.assertFalse(self.runtime._stopped(record), "a known writer is still running")
        with mock.patch.object(runtime_module.bridge, "finalize",
                               side_effect=lambda folder, outcome, note: runtime_module._read_json(folder / "execution.json")):
            recovered = runtime_module.Runtime._finalize_lost(run_dir)
        self.assertEqual(recovered["lost_stop"], "stopped")
        self.assertEqual(process_control.presence(leaf), "stopped")
        size = beat.stat().st_size
        time.sleep(.25)
        self.assertEqual(beat.stat().st_size, size)
        self.assertTrue(self.runtime._stopped(record))

    def test_a_lost_worker_with_a_live_root_adopts_its_descendants_before_stopping_it(self):
        run_dir = self.fx.root / "lost-live"
        run_dir.mkdir()
        root_script = self.fx.root / "cli-live.py"
        root_script.write_text("import json, os, pathlib, subprocess, sys, time\nenv = dict(os.environ)\n"
                               f"env.pop({bridge.FAMILY_KEY!r}, None)\n"
                               "leaf = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'], env=env,"
                               " start_new_session=True)\n"
                               "pathlib.Path(sys.argv[1]).write_text(json.dumps({'pid': leaf.pid}))\ntime.sleep(30)\n")
        marker = process_family.Family()
        root = subprocess.Popen([sys.executable, str(root_script), str(run_dir / "leaf.json")],
                                env={**os.environ, **marker.environment}, start_new_session=True)
        self.addCleanup(lambda: root.poll() is None and root.kill())
        deadline = time.monotonic() + 10
        while not (run_dir / "leaf.json").exists() and time.monotonic() < deadline:
            time.sleep(.02)
        leaf = process_control.identity(json.loads((run_dir / "leaf.json").read_text())["pid"])
        self.addCleanup(lambda: process_control.presence(leaf) == "running" and os.kill(leaf["pid"], 9))
        # The worker died before it ever observed the leaf: nothing was persisted for it.
        runtime_module.dump(run_dir / "child.json", {"pid": root.pid, "identity": process_control.identity(root.pid),
                                                     "family_marker": marker.environment[bridge.FAMILY_KEY]})
        threading.Thread(target=root.wait, daemon=True).start()
        with mock.patch.object(runtime_module.bridge, "finalize",
                               side_effect=lambda folder, outcome, note: runtime_module._read_json(folder / "execution.json")):
            recovered = runtime_module.Runtime._finalize_lost(run_dir)
        self.assertEqual(recovered["lost_stop"], "stopped")
        self.assertEqual(process_control.presence(leaf), "stopped")

    def test_a_v1_record_without_control_rows_stays_readable(self):
        seed(self.fx, "run-legacy0001", status="ok")
        with control_store.ControlStore(self.runtime.store.path)._write() as db:
            db.execute("DELETE FROM runs WHERE run_id='run-legacy0001'")
        snapshot = self.runtime.snapshot("run-legacy0001", CHAT_A)
        self.assertEqual((snapshot["interactive"], snapshot["interactions"], snapshot["context"]), (False, [], None))
        with self.assertRaisesRegex(RuntimeError, "already ended"):
            self.runtime.reply("run-legacy0001", "u1", "update", "more")


if __name__ == "__main__":
    unittest.main()
