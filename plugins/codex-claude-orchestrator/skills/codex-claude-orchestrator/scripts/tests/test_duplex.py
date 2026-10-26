"""The live conversation with a real child process speaking the CLI's stream-json duplex protocol.

These run bridge.supervise() in-process against fake_claude.py without the OS
write boundary, so they also run where nested sandbox-exec is refused; the
boundary itself is covered by the copy/readonly end-to-end suites.
"""
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
import bridge  # noqa: E402
import duplex  # noqa: E402
from control_store import ControlStore  # noqa: E402


LOST_ANSWER_CHILD = """
import json, os, sys
from pathlib import Path
capture = Path(os.environ["LOST_CAPTURE"]); resumed = "--resume" in sys.argv
def emit(e): print(json.dumps({"session_id": "review-session", **e}), flush=True)
def read():
    line = sys.stdin.readline()
    return json.loads(line) if line else None
first = read(); emit({**first, "isReplay": True})
emit({"type": "system", "subtype": "session_state_changed", "state": "running"})
emit({"type": "system", "subtype": "init", "permissionMode": "auto", "tools": ["AskUserQuestion", "Bash", "Agent"]})
if resumed:
    (capture / "resume.json").write_text(json.dumps(first))
    emit({"type": "result", "subtype": "success", "is_error": False})
    emit({"type": "system", "subtype": "session_state_changed", "state": "idle"})
    while read() is not None: pass
else:
    emit({"type": "system", "subtype": "task_started", "task_id": "agent-1", "task_type": "local_agent", "description": "agent"})
    for i in (1, 2):
        emit({"type": "control_request", "request_id": f"req-{i}", "request": {"subtype": "can_use_tool",
              "tool_name": "AskUserQuestion", "tool_use_id": f"tool-{i}", "input": {"questions": [{"question": f"Q{i}"}]}}})
    stop_id = None; got = False
    while True:
        item = read()
        if item is None: break
        if item["type"] == "control_request" and item["request"]["subtype"] == "stop_task": stop_id = item["request_id"]
        if item["type"] == "control_response" and item["response"]["request_id"] == "req-1":
            # The answer reached the CLI, but no tool_result echo is emitted before the execution is stopped.
            (capture / "live-answer.json").write_text(json.dumps(item)); got = True
        if got and stop_id:
            emit({"type": "control_response", "response": {"subtype": "error", "request_id": stop_id, "error": "cannot stop"}})
            got = False
"""


HELD_MESSAGES_CHILD = """
import json, os, sys
from pathlib import Path
capture = Path(os.environ["LOST_CAPTURE"]); resumed = "--resume" in sys.argv
expect = int(os.environ.get("HELD_EXPECT", "1"))
def emit(e): print(json.dumps({"session_id": "held-session", **e}), flush=True)
def read():
    line = sys.stdin.readline()
    return json.loads(line) if line else None
first = read(); emit({**first, "isReplay": True})
emit({"type": "system", "subtype": "session_state_changed", "state": "running"})
emit({"type": "system", "subtype": "init", "permissionMode": "auto", "tools": ["AskUserQuestion", "Bash", "Agent"]})
if resumed:
    (capture / "resume.json").write_text(json.dumps(first))
    emit({"type": "result", "subtype": "success", "is_error": False})
    emit({"type": "system", "subtype": "session_state_changed", "state": "idle"})
    while True:
        item = read()
        if item is None: break
        with (capture / "resumed-extra.jsonl").open("a") as out: out.write(json.dumps(item) + "\\n")
else:
    emit({"type": "system", "subtype": "task_started", "task_id": "agent-1", "task_type": "local_agent", "description": "agent"})
    emit({"type": "control_request", "request_id": "req-1", "request": {"subtype": "can_use_tool",
          "tool_name": "AskUserQuestion", "tool_use_id": "tool-1", "input": {"questions": [{"question": "Q1"}]}}})
    stop_id = None; seen = 0; refused = False
    while True:
        item = read()
        if item is None: break
        if item["type"] == "control_request" and item["request"]["subtype"] == "stop_task": stop_id = item["request_id"]
        if item["type"] == "user":
            seen += 1
            # Blocked on the question: only a message marked ECHO-ME is taken in; the rest wait unreplayed in the input.
            if "ECHO-ME" in item["message"]["content"]: emit({**item, "isReplay": True})
        if seen >= expect and stop_id and not refused:
            emit({"type": "control_response", "response": {"subtype": "error", "request_id": stop_id, "error": "cannot stop"}})
            refused = True
"""

PARTIAL_ANSWER_CHILD = """
import json, os, sys, time
from pathlib import Path
capture = Path(os.environ["LOST_CAPTURE"]); resumed = "--resume" in sys.argv
def emit(e): print(json.dumps({"session_id": "partial-session", **e}), flush=True)
def read():
    line = sys.stdin.readline()
    return json.loads(line) if line else None
first = read(); emit({**first, "isReplay": True})
emit({"type": "system", "subtype": "session_state_changed", "state": "running"})
emit({"type": "system", "subtype": "init", "permissionMode": "auto", "tools": ["AskUserQuestion", "Bash", "Agent"]})
if resumed:
    (capture / "resume.json").write_text(json.dumps(first))
    emit({"type": "result", "subtype": "success", "is_error": False})
    emit({"type": "system", "subtype": "session_state_changed", "state": "idle"})
    while read() is not None: pass
else:
    emit({"type": "system", "subtype": "task_started", "task_id": "agent-1", "task_type": "local_agent", "description": "agent"})
    for i in (1, 2):
        emit({"type": "control_request", "request_id": f"req-{i}", "request": {"subtype": "can_use_tool",
              "tool_name": "AskUserQuestion", "tool_use_id": f"tool-{i}", "input": {"questions": [{"question": f"Q{i}"}]}}})
    stop = read()
    # The CLI stops reading its input: a large answer now fills the pipe only in part.
    while not (capture / "go").exists(): time.sleep(.05)
    emit({"type": "control_response", "response": {"subtype": "error", "request_id": stop["request_id"], "error": "cannot stop"}})
    time.sleep(30)
"""

HEARTBEAT_CHILD = """
import json, os, signal, subprocess, sys, time
def emit(e): print(json.dumps({"session_id": "beat-session", **e}), flush=True)
def read():
    line = sys.stdin.readline()
    return json.loads(line) if line else None
def slow_exit(*_):
    time.sleep(1.5)
    os._exit(0)
signal.signal(signal.SIGTERM, slow_exit)
first = read(); emit({**first, "isReplay": True})
emit({"type": "system", "subtype": "session_state_changed", "state": "running"})
emit({"type": "system", "subtype": "init", "permissionMode": "auto", "tools": ["AskUserQuestion", "Bash"]})
time.sleep(.5)
beat = ("import signal, sys, time\\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\\n"
        "with open(sys.argv[1], 'a') as out:\\n    while True:\\n        out.write(repr(time.time()) + '\\\\n'); out.flush(); time.sleep(.05)\\n")
subprocess.Popen([sys.executable, "-c", beat, os.environ["BEAT_FILE"]], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
time.sleep(.4)
emit({"type": "control_request", "request_id": "req-1", "request": {"subtype": "can_use_tool",
      "tool_name": "AskUserQuestion", "tool_use_id": "tool-1", "input": {"questions": [{"question": "Wait?"}]}}})
while read() is not None: pass
time.sleep(30)
"""

TURN_CHILD = """
import json, os, sys, time
mode = os.environ["TURN_MODE"]; updates = int(os.environ.get("TURN_UPDATES", "1"))
def emit(e): print(json.dumps({"session_id": "turn-session", **e}), flush=True)
def read():
    line = sys.stdin.readline()
    return json.loads(line) if line else None
first = read(); emit({**first, "isReplay": True})
emit({"type": "system", "subtype": "session_state_changed", "state": "running"})
emit({"type": "system", "subtype": "init", "permissionMode": "auto", "tools": ["AskUserQuestion", "Bash"]})
emit({"type": "assistant", "message": {"content": [{"type": "text", "text": "working"}]}})
for _ in range(updates):
    item = read()
    if mode != "unechoed_exit":
        # As CLI 2.1.310 does: the message is replayed and absorbed into the running turn.
        emit({**item, "isReplay": True})
if mode == "echoed_exit":
    sys.exit(9)
emit({"type": "result", "subtype": "success", "is_error": False, "result": "turn done"})
if mode in ("unechoed_exit", "echoed_result_exit"):
    time.sleep(.3)
    sys.exit(9)
emit({"type": "system", "subtype": "session_state_changed", "state": "idle"})
while read() is not None: pass
"""


class DuplexTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="duplex ")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.run_dir = self.root / "state" / "v1" / "runs" / "run-duplextest01"
        self.run_dir.mkdir(parents=True)
        self.work = self.root / "work"
        self.work.mkdir()
        self.notes = self.root / "outbox" / "notes.jsonl"
        self.notes.parent.mkdir()
        self.probes = self.root / "probes"
        self.store = ControlStore(self.root / "state" / "v2" / "control.sqlite3")
        self.store.register_run(self.run_dir.name, "T-1", {"namespace": "n", "chat": "c"}, "bridge-worker", True)
        self.cli = [sys.executable, str(Path(__file__).parent / "fake_claude.py")]
        # The bridge asks the CLI for session_state_changed events; the fixture emits them as CLI 2.1.310 does.
        patcher = mock.patch.dict(os.environ, {"FAKE_PROBES": str(self.probes), "FAKE_SCRIPT": "[]",
                                               "CLAUDE_CODE_SDK_READS_SESSION_STATE": "1"})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.sessions = []

    def command_for(self, session):
        self.sessions.append(session)
        flag = "--resume" if session[0] == "resume" else "--session-id"
        return [*self.cli, "-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
                "--replay-user-messages", "--permission-mode", "auto", "--permission-prompt-tool", "stdio",
                "--tools", "default", flag, session[1]]

    def start(self, *actions, interactive=True, timeout=40, resume_script=None, env=None):
        os.environ["FAKE_SCRIPT"] = json.dumps(list(actions), ensure_ascii=False)
        if resume_script is not None:
            os.environ["FAKE_RESUME_SCRIPT"] = json.dumps(list(resume_script), ensure_ascii=False)
        os.environ.update(env or {})
        self.conversation = duplex.Conversation(self.run_dir, self.store, self.run_dir.name, interactive=interactive,
                                                notes_path=self.notes)
        payload = bridge.user_line(f"任务书。笔记写到 `{self.notes}`。")
        self.facts = {}

        def target():
            self.facts.update(bridge.supervise(self.run_dir, self.command_for, str(self.work), dict(os.environ), payload,
                                               timeout, "stream.jsonl", ("session", "11111111-2222-3333-4444-555555555555"),
                                               self.conversation, self.store, self.run_dir.name))
        self.thread = threading.Thread(target=target, daemon=True)
        self.thread.start()

    def finish(self, timeout=40):
        self.thread.join(timeout)
        self.assertFalse(self.thread.is_alive(), "the supervised session did not end")
        return self.facts

    def until(self, predicate, timeout=20, message="condition"):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = predicate()
            if value:
                return value
            time.sleep(.05)
        self.fail(f"timed out waiting for {message}")

    def pending(self):
        return self.until(lambda: self.store.interactions(self.run_dir.name, status="pending"), message="a pending interaction")

    def probe(self, name):
        path = self.probes / name
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def test_a_question_waits_for_codex_and_the_answer_reaches_the_tool(self):
        self.start({"ask": "Which color?", "options": ["red", "blue"]}, {"sleep": .2})
        question = self.pending()[0]
        self.assertEqual((question["kind"], question["blocking"]), ("question", True))
        self.assertEqual(question["request"]["questions"][0]["question"], "Which color?")
        time.sleep(.5)
        self.assertEqual(self.probe("answers.jsonl"), [], "the CLI must still be waiting for the host")
        receipt = self.store.submit(self.run_dir.name, "cmd-1", "answer", {"text": "blue"}, "coordinator",
                                    interaction_id=question["interaction_id"])
        self.assertEqual(receipt["delivery"]["state"], "queued")
        facts = self.finish()
        answered = self.probe("answers.jsonl")[0]["response"]["response"]
        self.assertEqual(answered["behavior"], "allow")
        self.assertEqual(answered["updatedInput"]["answers"], {"Which color?": "[Codex 协调者] blue"})
        self.assertEqual(self.store.command(self.run_dir.name, "cmd-1")["delivery"]["state"], "echoed")
        self.assertEqual(self.store.interaction(question["interaction_id"])["status"], "answered")
        self.assertEqual(facts["exit_code"], 0)
        self.assertEqual(self.store.run(self.run_dir.name)["state"], "closing")

    def test_human_words_are_labelled_as_the_users(self):
        self.start({"ask": "Ship it?"})
        question = self.pending()[0]
        self.store.submit(self.run_dir.name, "cmd-h", "answer", {"text": "先别发布"}, "human",
                          interaction_id=question["interaction_id"])
        self.finish()
        self.assertEqual(self.probe("answers.jsonl")[0]["response"]["response"]["updatedInput"]["answers"],
                         {"Ship it?": "[用户原话] 先别发布"})

    def test_a_pending_question_holds_the_rest_of_the_process_family(self):
        beat = self.root / "beat.txt"
        self.start({"spawn": str(beat)}, {"sleep": .4}, {"ask": "Continue?"}, {"sleep": .3})
        question = self.pending()[0]
        self.until(lambda: (self.store.interaction(question["interaction_id"]) or {}).get("hold"), message="hold record")
        time.sleep(.3)
        held_size = beat.stat().st_size
        time.sleep(.8)
        self.assertEqual(beat.stat().st_size, held_size, "a held background process kept writing")
        hold = self.store.interaction(question["interaction_id"])["hold"]
        self.assertEqual(hold["mode"], "family_hold")
        self.assertGreaterEqual(hold["held_processes"], 1)
        self.store.submit(self.run_dir.name, "cmd-2", "answer", {"text": "yes"}, "coordinator",
                          interaction_id=question["interaction_id"])
        self.until(lambda: beat.stat().st_size > held_size, message="the held process to resume")
        self.finish()
        released = self.store.interaction(question["interaction_id"])["hold"]
        self.assertEqual(released["release_reason"], "answered")

    def test_a_background_workflow_is_stopped_natively_and_the_answer_reaches_the_waiting_tool(self):
        self.start({"bg_task": "wf-1", "type": "local_workflow"}, {"ask": "Which schema?"}, {"sleep": .3},
                   resume_script=[{"capture_brief": str(self.root / "never.txt")}])
        question = self.pending()[0]
        hold = self.until(lambda: ((self.store.interaction(question["interaction_id"]) or {}).get("hold") or {}).get("stopped_tasks"),
                          message="the workflow stop")
        self.assertEqual(hold, ["local_workflow: background"])
        self.until(lambda: "wf-1" not in self.conversation.tasks, message="the CLI to confirm the stop")
        self.store.submit(self.run_dir.name, "cmd-3", "answer", {"text": "use v2"}, "human",
                          interaction_id=question["interaction_id"])
        facts = self.finish()
        self.assertEqual([leg["reason"] for leg in facts["legs"]], ["exited"])
        self.assertEqual(len(self.sessions), 1, "the native stop keeps the session; nothing is resumed")
        self.assertEqual(self.probe("answers.jsonl")[0]["response"]["response"]["updatedInput"]["answers"],
                         {"Which schema?": "[用户原话] use v2"})
        self.assertEqual(self.store.command(self.run_dir.name, "cmd-3")["delivery"]["state"], "echoed")

    def test_an_agent_started_while_a_question_waits_is_stopped_natively(self):
        self.start({"ask_many": ["Proceed?"], "then_agent": "late-agent"}, {"sleep": .3})
        question = self.pending()[0]
        hold = self.until(lambda: ((self.store.interaction(question["interaction_id"]) or {}).get("hold") or {}).get("stopped_tasks"),
                          message="the late agent stop")
        self.assertEqual(hold, ["local_agent: started with the question"])
        self.store.submit(self.run_dir.name, "late-1", "answer", {"text": "yes"}, "coordinator",
                          interaction_id=question["interaction_id"])
        facts = self.finish()
        self.assertEqual([leg["reason"] for leg in facts["legs"]], ["exited"])
        self.assertEqual(len(self.sessions), 1)

    def test_an_unconfirmed_native_stop_stops_the_execution_and_resumes_with_the_answer(self):
        capture = self.root / "resumed-brief.txt"
        self.start({"bg_task": "wf-1", "type": "local_agent"}, {"ask": "Which schema?"}, {"sleep": 5},
                   resume_script=[{"capture_brief": str(capture)}], env={"FAKE_STOP_TASK_FAIL": "1"})
        question = self.pending()[0]
        self.until(lambda: self.store.run(self.run_dir.name)["state"] == "held", message="the run to be held")
        hold = self.store.interaction(question["interaction_id"])["hold"]
        self.assertEqual((hold["mode"], hold["stop_confirmed"]), ("stop_for_answer", True))
        self.assertEqual(len(self.sessions), 1, "nothing may run while the answer is pending")
        self.store.submit(self.run_dir.name, "cmd-4", "answer", {"text": "use v2"}, "human",
                          interaction_id=question["interaction_id"])
        facts = self.finish()
        self.assertEqual([leg["reason"] for leg in facts["legs"]], ["stop_for_answer", "exited"])
        self.assertEqual(facts["legs"][0]["family"], "stopped")
        self.assertEqual(self.sessions[1], ("resume", "11111111-2222-3333-4444-555555555555"))
        resumed = capture.read_text()
        self.assertIn("回答来自用户原话", resumed)
        self.assertIn("你问：Which schema?\n  回答：use v2", resumed)
        self.assertIn("不代表用户拒绝或撤回了问题", resumed)
        self.assertEqual(self.store.command(self.run_dir.name, "cmd-4")["delivery"]["state"], "echoed")

    def test_a_stopped_execution_resumes_only_after_every_pending_question_has_its_answer(self):
        capture = self.root / "resumed-many.txt"
        self.start({"bg_task": "ag-1", "type": "local_agent"}, {"ask_many": ["QUESTION-1", "QUESTION-2"]}, {"sleep": 5},
                   resume_script=[{"capture_brief": str(capture)}], env={"FAKE_STOP_TASK_FAIL": "1"})
        self.until(lambda: len(self.store.pending_blocking(self.run_dir.name)) == 2, message="two pending questions")
        self.until(lambda: self.store.run(self.run_dir.name)["state"] == "held", message="the run to be held")
        first, second = self.store.interactions(self.run_dir.name, status="pending")
        self.store.submit(self.run_dir.name, "only-second", "answer", {"text": "",
                          "answers": {"QUESTION-2": "SECOND-EXACT"}}, "coordinator", interaction_id=second["interaction_id"])
        time.sleep(1.5)
        self.assertEqual(len(self.sessions), 1, "one unanswered question must keep the execution stopped")
        self.assertEqual(self.store.run(self.run_dir.name)["state"], "held")
        self.store.submit(self.run_dir.name, "then-first", "answer", {"text": "FIRST-TEXT"}, "human",
                          interaction_id=first["interaction_id"])
        facts = self.finish()
        self.assertEqual([leg["reason"] for leg in facts["legs"]], ["stop_for_answer", "exited"])
        resumed = capture.read_text()
        self.assertIn("你问：QUESTION-1\n  回答：FIRST-TEXT", resumed)
        self.assertIn("你问：QUESTION-2\n  回答：SECOND-EXACT", resumed)
        self.assertEqual({self.store.command(self.run_dir.name, c)["delivery"]["state"] for c in ("only-second", "then-first")},
                         {"echoed"})
        self.assertEqual(self.store.pending_blocking(self.run_dir.name), [])

    def test_parallel_questions_on_a_live_session_release_only_after_both_answers(self):
        beat = self.root / "beat-many.txt"
        self.start({"spawn": str(beat)}, {"sleep": .3}, {"ask_many": ["Q-A", "Q-B"]})
        self.until(lambda: len(self.store.pending_blocking(self.run_dir.name)) == 2, message="two pending questions")
        first, second = self.store.interactions(self.run_dir.name, status="pending")
        self.store.submit(self.run_dir.name, "a-1", "answer", {"text": "one"}, "coordinator", interaction_id=second["interaction_id"])
        time.sleep(.8)
        size = beat.stat().st_size
        time.sleep(.6)
        self.assertEqual(beat.stat().st_size, size, "the hold must last while another question is pending")
        self.store.submit(self.run_dir.name, "a-2", "answer", {"text": "two"}, "coordinator", interaction_id=first["interaction_id"])
        self.until(lambda: beat.stat().st_size > size, message="release after the last answer")
        self.finish()
        answers = [row["response"]["response"]["updatedInput"]["answers"] for row in self.probe("answers.jsonl")]
        self.assertEqual(answers, [{"Q-A": "[Codex 协调者] two"}, {"Q-B": "[Codex 协调者] one"}])

    def test_a_resumed_permission_answer_keeps_its_decision(self):
        capture = self.root / "resumed-permission.txt"
        self.start({"bg_task": "ag-2", "type": "local_agent"}, {"permission": "Bash", "input": {"command": "rm -rf dist"}},
                   {"sleep": 5}, resume_script=[{"capture_brief": str(capture)}], env={"FAKE_STOP_TASK_FAIL": "1"})
        request = self.pending()[0]
        self.until(lambda: self.store.run(self.run_dir.name)["state"] == "held", message="the run to be held")
        self.store.submit(self.run_dir.name, "perm-r", "answer", {"text": "超出授权", "decision": "deny"}, "coordinator",
                          interaction_id=request["interaction_id"])
        self.finish()
        resumed = capture.read_text()
        self.assertIn("授权请求", resumed)
        self.assertIn("决定：deny：超出授权", resumed)

    def test_a_resume_that_cannot_start_leaves_the_answer_unwritten(self):
        self.start({"bg_task": "ag-3", "type": "local_agent"}, {"ask": "Go?"}, {"sleep": 5}, env={"FAKE_STOP_TASK_FAIL": "1"})
        question = self.pending()[0]
        self.until(lambda: self.store.run(self.run_dir.name)["state"] == "held", message="the run to be held")
        self.cli = [str(self.root / "missing-claude")]
        self.store.submit(self.run_dir.name, "never-sent", "answer", {"text": "yes"}, "coordinator",
                          interaction_id=question["interaction_id"])
        facts = self.finish()
        self.assertEqual([leg["reason"] for leg in facts["legs"]], ["stop_for_answer", "launch_failed"])
        self.assertIn("launch_error", facts)
        self.assertEqual(self.store.command(self.run_dir.name, "never-sent")["delivery"]["state"], "queued")

    def test_a_background_shell_command_is_suspended_in_place_not_stopped(self):
        beat = self.root / "beat3.txt"
        self.start({"spawn": str(beat)}, {"bg_task": "sh-1", "tool": "Bash"}, {"sleep": .3}, {"ask": "Go on?"}, {"bg_done": "sh-1"})
        question = self.pending()[0]
        self.until(lambda: (self.store.interaction(question["interaction_id"]) or {}).get("hold"), message="hold record")
        time.sleep(.3)
        size = beat.stat().st_size
        time.sleep(.8)
        self.assertEqual(beat.stat().st_size, size)
        self.assertEqual(self.store.interaction(question["interaction_id"])["hold"]["mode"], "family_hold")
        self.store.submit(self.run_dir.name, "cmd-sh", "answer", {"text": "yes"}, "coordinator",
                          interaction_id=question["interaction_id"])
        facts = self.finish()
        self.assertEqual([leg["reason"] for leg in facts["legs"]], ["exited"])
        self.assertEqual(len(self.sessions), 1)

    def test_an_unanswered_stopped_question_ends_at_the_deadline_without_resuming(self):
        self.start({"bg_task": "wf-1"}, {"ask": "Which schema?"}, {"sleep": 5}, timeout=3, env={"FAKE_STOP_TASK_FAIL": "1"})
        facts = self.finish()
        self.assertTrue(facts["timed_out"])
        self.assertEqual(len(self.sessions), 1)

    def test_an_update_is_written_mid_run_and_its_replay_is_recorded(self):
        self.start({"wait_message": "received.jsonl"})
        self.until(lambda: self.conversation.transport is not None, message="the session to attach")
        self.store.submit(self.run_dir.name, "upd-1", "update", {"text": "也要处理空列表"}, "coordinator")
        self.finish()
        content = self.probe("received.jsonl")[0]["content"]
        self.assertIn("【Codex 补充 · Codex 协调者 · upd-1】", content)
        self.assertIn("也要处理空列表", content)
        self.assertEqual(self.store.command(self.run_dir.name, "upd-1")["delivery"]["state"], "echoed")

    def test_interrupt_ends_the_turn_and_the_redirect_starts_the_next_one(self):
        self.start({"wait_interrupt": True})
        self.until(lambda: self.conversation.transport is not None, message="the session to attach")
        redirect = "\n    停下，改查日志\n\n"
        self.store.submit(self.run_dir.name, "int-1", "interrupt", {"text": redirect}, "coordinator")
        facts = self.finish()
        self.assertEqual(self.probe("interrupts.jsonl"), [{"interrupted": True}])
        self.assertIn(redirect, self.probe("turns.jsonl")[0]["content"])
        # Live and resumed redirects carry the same interrupt label, so Claude does not read the CLI's generic
        # interruption as a human cancelling the run.
        self.assertTrue(self.probe("turns.jsonl")[0]["content"].startswith("【Codex 打断 · Codex 协调者 · int-1】\n"))
        parsed = bridge.stream_parser.parse_stream(self.run_dir / "stream.jsonl")
        self.assertEqual([turn["subtype"] for turn in parsed["turns"]], ["error_during_execution", "success"])
        self.assertEqual(parsed["inits"], 2)
        self.assertEqual(parsed["final"]["subtype"], "success")
        self.assertEqual(self.store.command(self.run_dir.name, "int-1")["delivery"]["state"], "echoed")
        self.assertEqual(facts["exit_code"], 0)

    def test_notes_are_acknowledged_at_once_and_do_not_block(self):
        self.start({"note": {"type": "dispute", "text": "约束 2 与原话冲突", "scope": "constraints"}},
                   {"note": {"type": "question", "text": "untyped"}}, {"sleep": .6})
        facts = self.finish()
        items = self.store.interactions(self.run_dir.name)
        self.assertEqual([(item["kind"], item["blocking"], item["status"]) for item in items],
                         [("dispute", False, "acknowledged")])
        self.assertEqual(items[0]["request"], {"text": "约束 2 与原话冲突", "scope": "constraints"})
        self.assertEqual(facts["exit_code"], 0)

    def test_a_permission_escalation_is_decided_by_codex(self):
        self.start({"permission": "Bash", "input": {"command": "rm -rf build", "description": "clean build"}})
        request = self.pending()[0]
        self.assertEqual((request["kind"], request["request"]["tool_name"]), ("permission", "Bash"))
        self.store.submit(self.run_dir.name, "perm-1", "answer", {"text": "超出任务授权", "decision": "deny"},
                          "coordinator", interaction_id=request["interaction_id"])
        self.finish()
        response = self.probe("answers.jsonl")[0]["response"]["response"]
        self.assertEqual(response, {"behavior": "deny", "message": "[Codex 协调者] 超出任务授权"})

    def test_without_a_live_channel_questions_are_declined_not_left_waiting(self):
        self.start({"ask": "Which color?"}, interactive=False)
        self.finish()
        response = self.probe("answers.jsonl")[0]["response"]["response"]
        self.assertEqual(response["behavior"], "deny")
        self.assertEqual(self.store.interactions(self.run_dir.name), [])

    def test_a_session_without_the_question_tool_is_stopped_as_a_capability_error(self):
        self.start({"sleep": 5}, env={"FAKE_TOOLS": "Read,Bash"})
        facts = self.finish()
        self.assertIn("AskUserQuestion is not available", facts["capability_error"])
        self.assertEqual(facts["legs"][0]["reason"], "capability")

    def test_a_session_not_in_auto_mode_is_a_capability_error(self):
        self.start({"sleep": 5}, env={"FAKE_PERMISSION_MODE": "dontAsk"})
        facts = self.finish()
        self.assertIn("not 'auto'", facts["capability_error"])

    def test_input_stays_open_while_background_work_can_still_wake_a_turn(self):
        self.start({"bg_task": "bg-1"}, {"bg_later": "bg-1", "delay": 1.5})
        facts = self.finish()
        self.assertEqual(facts["exit_code"], 0)
        events = [json.loads(line) for line in (self.run_dir / "activity.jsonl").read_text().splitlines()]
        turns = [event["summary"] for event in events if event["kind"] == "turn"]
        self.assertEqual(turns, ["Claude finished a turn", "Claude finished a turn",
                                 "no further input is pending; closing Claude's input"])

    def test_a_background_shell_still_running_at_idle_keeps_input_open_for_its_follow_up_turn(self):
        # BG_IDLE_EARLY_CLOSE: CLI 2.1.310 reports idle while a Bash/Monitor task runs; its end wakes the turn
        # that reads its output.  The old close stopped the shell at that idle and the follow-up turn never came.
        self.start({"bg_task": "sh-2", "type": "local_bash"}, {"ask": "Proceed?"},
                   {"bg_later": "sh-2", "delay": 2}, timeout=60)
        question = self.pending()[0]
        self.store.submit(self.run_dir.name, "cmd-go", "answer", {"text": "yes"}, "coordinator",
                          interaction_id=question["interaction_id"])
        facts = self.finish(timeout=30)
        self.assertEqual(facts["legs"][0]["reason"], "exited")
        self.assertNotIn("shells_stopped", facts)
        events = [json.loads(line) for line in (self.run_dir / "activity.jsonl").read_text().splitlines()]
        turns = [event["summary"] for event in events if event["kind"] == "turn"
                 and event.get("status") != "waiting_background"]
        self.assertEqual(turns, ["Claude finished a turn", "Claude finished a turn",
                                 "no further input is pending; closing Claude's input"])
        self.assertFalse(any(event.get("status") == "stop_task" and event["kind"] == "turn" for event in events))

    def test_idle_seen_before_a_shell_ends_is_stale_until_the_next_idle(self):
        conversation = duplex.Conversation(self.run_dir, self.store, self.run_dir.name, interactive=True, notes_path=None)
        conversation.on_event({"type": "system", "subtype": "task_started", "task_id": "sh", "task_type": "local_bash"})
        conversation.on_event({"type": "system", "subtype": "session_state_changed", "state": "idle"})
        conversation.on_event({"type": "system", "subtype": "task_notification", "task_id": "sh", "status": "completed"})
        self.assertEqual(conversation.tasks, {})
        self.assertTrue(conversation.idle_stale, "the shell's end wakes one more turn; the earlier idle is not final")
        conversation.on_event({"type": "system", "subtype": "session_state_changed", "state": "running"})
        conversation.on_event({"type": "system", "subtype": "session_state_changed", "state": "idle"})
        self.assertFalse(conversation.idle_stale)

    def test_a_shell_ending_inside_a_running_turn_does_not_wait_for_another_idle(self):
        conversation = duplex.Conversation(self.run_dir, self.store, self.run_dir.name, interactive=True, notes_path=None)
        conversation.on_event({"type": "system", "subtype": "session_state_changed", "state": "running"})
        conversation.on_event({"type": "system", "subtype": "task_started", "task_id": "sh", "task_type": "local_bash"})
        conversation.on_event({"type": "system", "subtype": "task_updated", "task_id": "sh", "patch": {"status": "killed"}})
        conversation.on_event({"type": "system", "subtype": "session_state_changed", "state": "idle"})
        self.assertEqual(conversation.tasks, {})
        self.assertFalse(conversation.idle_stale)

    def test_a_shell_that_never_ends_holds_input_until_the_deadline(self):
        started = time.monotonic()
        self.start({"bg_task": "sh-3", "type": "local_bash"}, {"bg_later": "sh-3", "delay": 30}, timeout=3)
        facts = self.finish(timeout=30)
        self.assertGreaterEqual(time.monotonic() - started, 2.5)
        self.assertTrue(facts["timed_out"])

    def test_a_final_line_lets_a_leftover_service_be_stopped_without_waiting(self):
        started = time.monotonic()
        self.start({"bg_task": "svc", "type": "local_bash"}, {"note": {"type": "final", "after": "run-duplextest01"}}, {"sleep": .5},
                   {"bg_later": "svc", "delay": 30, "turn": False}, timeout=60)
        self.until(lambda: self.store.run(self.run_dir.name)["state"] == "closing", timeout=10, message="input to close")
        self.assertLess(time.monotonic() - started, 10, "a declared leftover must not keep the run open")
        facts = self.finish(timeout=20)
        self.assertEqual(facts["legs"][0]["reason"], "exited")
        self.assertEqual(facts["shells_stopped"], ["background"])
        self.assertEqual(self.store.interactions(self.run_dir.name), [], "final is a signal, not a note for Codex")

    def test_an_update_after_final_voids_it(self):
        conversation = duplex.Conversation(self.run_dir, self.store, self.run_dir.name, interactive=True, notes_path=None)
        conversation.transport = type("T", (), {"send": lambda self, data, cb=None: True, "flushed": True})()
        conversation._note(b'{"type": "final", "after": "run-duplextest01"}')
        self.assertTrue(conversation.finalized)
        conversation._deliver({"command_id": "cmd-u", "kind": "update", "payload": {"text": "one more"},
                               "origin": "coordinator"})
        self.assertFalse(conversation.finalized)

    def live(self):
        """A Conversation on a recording transport; frames it writes are kept in sent."""
        conversation = duplex.Conversation(self.run_dir, self.store, self.run_dir.name, interactive=True,
                                           notes_path=self.notes)
        sent, closed = [], []

        class Transport:
            flushed = True

            def send(self, data, on_written=None):
                sent.append(json.loads(data))
                if on_written:
                    on_written(True, len(data))
                return True

            def finish_input(self):
                closed.append(True)

        transport = Transport()
        conversation.transport = transport
        conversation.results = 1
        conversation.awaiting_result = False
        return conversation, sent, closed

    def append_note(self, value):
        with self.notes.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(value) + "\n")

    def shell_stops_sent(self, sent):
        return [frame["request"]["task_id"] for frame in sent
                if frame.get("type") == "control_request" and frame["request"].get("subtype") == "stop_task"]

    def test_without_session_states_a_final_line_still_stops_a_leftover_shell(self):
        conversation, sent, closed = self.live()
        conversation.on_event({"type": "system", "subtype": "task_started", "task_id": "svc", "task_type": "local_bash"})
        conversation.pump()
        self.assertEqual(self.shell_stops_sent(sent), [], "a running shell is waited for without a final line")
        self.append_note({"type": "final", "after": self.run_dir.name})
        conversation.pump()
        self.assertEqual(self.shell_stops_sent(sent), ["svc"])

    def test_without_session_states_a_shell_ending_after_the_result_waits_for_the_follow_up_result(self):
        conversation, sent, closed = self.live()
        conversation.on_event({"type": "system", "subtype": "task_started", "task_id": "sh", "task_type": "local_bash"})
        conversation.on_event({"type": "system", "subtype": "task_notification", "task_id": "sh", "status": "completed"})
        conversation.pump()
        self.assertEqual(closed, [], "the shell's follow-up turn has not produced its result yet")
        conversation.on_event({"type": "result", "subtype": "success"})
        conversation.pump()
        self.assertEqual(closed, [True])

    def update_and_replay(self, conversation, sent, command_id, *, replay=True):
        self.store.submit(self.run_dir.name, command_id, "update", {"text": "one more"}, "coordinator")
        conversation.pump()
        update = [frame for frame in sent if frame.get("type") == "user"][-1]
        return {"type": "user", "isReplay": True, "message": {"content": update["message"]["content"]}}

    def test_a_final_naming_an_older_message_is_stale_even_if_read_late(self):
        conversation, sent, closed = self.live()
        conversation.session_state = "idle"
        conversation.on_event({"type": "system", "subtype": "task_started", "task_id": "svc", "task_type": "local_bash"})
        replay = self.update_and_replay(conversation, sent, "upd-1")
        self.append_note({"type": "final", "after": self.run_dir.name})  # old turn, before it saw upd-1
        conversation.on_event(replay)
        conversation.on_event({"type": "result", "subtype": "success"})
        conversation.on_event({"type": "system", "subtype": "session_state_changed", "state": "idle"})
        conversation.pump()
        self.assertFalse(conversation.finalized)
        self.assertEqual(self.shell_stops_sent(sent), [])
        self.append_note({"type": "final", "after": "upd-1"})
        conversation.pump()
        self.assertEqual(self.shell_stops_sent(sent), ["svc"])

    def test_a_final_for_the_new_message_counts_even_before_the_host_sees_its_replay(self):
        # R1 counterexample A: the CLI replayed the update and Claude wrote its final before the host drained the
        # replay; a notes-size floor taken at replay time rejected this legitimate final.
        conversation, sent, closed = self.live()
        conversation.session_state = "idle"
        conversation.on_event({"type": "system", "subtype": "task_started", "task_id": "svc", "task_type": "local_bash"})
        replay = self.update_and_replay(conversation, sent, "upd-2")
        self.append_note({"type": "final", "after": "upd-2"})
        conversation.pump()
        conversation.on_event(replay)
        conversation.on_event({"type": "result", "subtype": "success"})
        conversation.on_event({"type": "system", "subtype": "session_state_changed", "state": "idle"})
        conversation.pump()
        self.assertTrue(conversation.finalized)
        self.assertEqual(self.shell_stops_sent(sent), ["svc"])

    def test_a_late_bare_interrupt_ack_does_not_end_a_newer_update(self):
        # R1 counterexample B.
        conversation, sent, closed = self.live()
        conversation.session_state = "idle"
        conversation.on_event({"type": "system", "subtype": "task_started", "task_id": "mon", "task_type": "local_bash"})
        self.store.submit(self.run_dir.name, "int-old", "interrupt", {"text": ""}, "coordinator")
        conversation.pump()
        request_id = next(frame["request_id"] for frame in sent if frame["request"].get("subtype") == "interrupt")
        replay = self.update_and_replay(conversation, sent, "upd-3")
        conversation.on_event(replay)
        conversation.on_event({"type": "control_response", "response": {"subtype": "success", "request_id": request_id}})
        conversation.on_event({"type": "result", "subtype": "success"})
        conversation.on_event({"type": "system", "subtype": "session_state_changed", "state": "idle"})
        conversation.pump()
        self.assertFalse(conversation.finalized)
        self.assertEqual(self.shell_stops_sent(sent), [])

    def test_an_unread_final_from_the_previous_leg_is_stale_after_the_resumed_message(self):
        conversation, sent, closed = self.live()
        self.append_note({"type": "final", "after": self.run_dir.name})  # written by the stopped leg, not yet read
        text = duplex.resumed_text([], [], [], "resume-x-1")
        self.assertTrue(text.endswith("（这条恢复消息的编号：resume-x-1）"))
        conversation.latest_input = "resume-x-1"  # what the bridge sets with the resumed first message
        conversation.attach(conversation.transport, 0, None)
        conversation.pump()
        self.assertFalse(conversation.finalized)
        self.append_note({"type": "final", "after": "resume-x-1"})
        conversation.pump()
        self.assertTrue(conversation.finalized)

    def test_a_refused_bare_interrupt_keeps_the_input_id_claude_already_knows(self):
        conversation, sent, closed = self.live()
        conversation.session_state = "idle"
        conversation.on_event({"type": "system", "subtype": "task_started", "task_id": "svc", "task_type": "local_bash"})
        self.store.submit(self.run_dir.name, "int-refused", "interrupt", {"text": ""}, "coordinator")
        conversation.pump()
        request_id = next(frame["request_id"] for frame in sent if frame.get("request", {}).get("subtype") == "interrupt")
        conversation.on_event({"type": "control_response", "response": {
            "subtype": "error", "request_id": request_id, "error": "cannot interrupt"}})
        self.assertEqual(conversation.latest_input, self.run_dir.name)
        self.assertFalse(conversation.finalized)
        self.append_note({"type": "final", "after": self.run_dir.name})
        conversation.pump()
        self.assertTrue(conversation.finalized)
        self.assertEqual(self.shell_stops_sent(sent), ["svc"])

    def test_waiting_for_background_results_is_reported_without_repeating_every_pump(self):
        conversation, sent, closed = self.live()
        conversation.session_state = "idle"
        conversation.on_event({"type": "system", "subtype": "task_started", "task_id": "mon", "task_type": "local_bash"})
        for _ in range(3):
            conversation.pump()
        activity_path = self.run_dir / "activity.jsonl"
        records = [json.loads(line) for line in activity_path.read_text().splitlines()]
        self.assertEqual(sum(item.get("status") == "waiting_background" for item in records), 1)
        self.assertEqual(self.shell_stops_sent(sent), [])
        self.assertFalse(closed)

    def test_two_refused_bare_interrupts_never_replace_the_visible_input_id(self):
        conversation, sent, closed = self.live()
        for command_id in ("stop-a", "stop-b"):
            self.store.submit(self.run_dir.name, command_id, "interrupt", {"text": ""}, "coordinator")
            conversation.pump()
        requests = [frame["request_id"] for frame in sent if frame.get("request", {}).get("subtype") == "interrupt"]
        for request_id in requests:
            conversation.on_event({"type": "control_response", "response": {
                "subtype": "error", "request_id": request_id, "error": "cannot interrupt"}})
        self.assertEqual(conversation.latest_input, self.run_dir.name)
        self.append_note({"type": "final", "after": self.run_dir.name})
        conversation.pump()
        self.assertTrue(conversation.finalized)

    def test_a_bare_interrupt_ack_after_an_answer_does_not_finalize_that_answer(self):
        conversation, sent, closed = self.live()
        conversation.on_event({"type": "control_request", "request_id": "q1", "request": {
            "subtype": "can_use_tool", "tool_name": "AskUserQuestion", "tool_use_id": "tu1",
            "input": {"questions": [{"question": "Go?"}]}}})
        self.store.submit(self.run_dir.name, "old-stop", "interrupt", {"text": ""}, "coordinator")
        conversation.pump()
        request_id = next(frame["request_id"] for frame in sent if frame.get("request", {}).get("subtype") == "interrupt")
        question = self.pending()[0]
        self.store.submit(self.run_dir.name, "new-answer", "answer", {"text": "yes"}, "coordinator",
                          interaction_id=question["interaction_id"])
        conversation.pump()
        conversation.on_event({"type": "control_response", "response": {"subtype": "success", "request_id": request_id}})
        self.assertFalse(conversation.finalized)

    def test_a_final_written_before_a_question_is_voided_by_the_answer(self):
        # Pump reads notes before it delivers queued answers, so a final already on disk is read and then voided.
        conversation, sent, closed = self.live()
        conversation.on_event({"type": "control_request", "request_id": "q1", "request": {
            "subtype": "can_use_tool", "tool_name": "AskUserQuestion", "tool_use_id": "tu1",
            "input": {"questions": [{"question": "Go?"}]}}})
        self.append_note({"type": "final", "after": self.run_dir.name})
        question = self.pending()[0]
        self.store.submit(self.run_dir.name, "ans-1", "answer", {"text": "yes"}, "coordinator",
                          interaction_id=question["interaction_id"])
        conversation.pump()
        self.assertFalse(conversation.finalized)

    def test_an_answer_voids_a_final_beyond_the_first_notes_chunk(self):
        conversation, sent, closed = self.live()
        conversation.session_state = "idle"
        conversation.on_event({"type": "system", "subtype": "task_started", "task_id": "mon", "task_type": "local_bash"})
        conversation.on_event({"type": "control_request", "request_id": "q1", "request": {
            "subtype": "can_use_tool", "tool_name": "AskUserQuestion", "tool_use_id": "tu1",
            "input": {"questions": [{"question": "Go?"}]}}})
        line = json.dumps({"type": "note", "text": "x" * 4000}) + "\n"
        backlog = line * (duplex.NOTES_READ_LIMIT // len(line) + 1)
        self.notes.write_text(backlog + json.dumps({"type": "final", "after": self.run_dir.name}) + "\n")
        question = self.pending()[0]
        self.store.submit(self.run_dir.name, "ans-backlog", "answer", {"text": "yes"}, "coordinator",
                          interaction_id=question["interaction_id"])
        conversation.pump()
        conversation.on_event({"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "tu1", "content": "yes"}]}})
        conversation.on_event({"type": "result", "subtype": "success"})
        conversation.on_event({"type": "system", "subtype": "session_state_changed", "state": "idle"})
        conversation.pump()
        self.assertFalse(conversation.finalized)
        self.assertEqual(self.shell_stops_sent(sent), [])
        self.assertFalse(closed)
        self.append_note({"type": "final", "after": self.run_dir.name})
        conversation.pump()
        self.assertEqual(self.shell_stops_sent(sent), ["mon"], "a new final after the answer still permits cleanup")

    def test_a_final_line_without_the_message_id_is_ignored(self):
        conversation, sent, closed = self.live()
        self.append_note({"type": "final"})
        conversation.pump()
        self.assertFalse(conversation.finalized)

    def test_a_new_leg_does_not_inherit_the_previous_final_line(self):
        conversation, sent, closed = self.live()
        self.append_note({"type": "final", "after": self.run_dir.name})
        conversation.pump()
        self.assertTrue(conversation.finalized)
        conversation.attach(conversation.transport, 0, None)
        self.assertFalse(conversation.finalized)

    def test_a_bare_interrupt_while_waiting_for_a_shell_stops_it_and_closes(self):
        conversation, sent, closed = self.live()
        conversation.session_state = "idle"
        conversation.on_event({"type": "system", "subtype": "task_started", "task_id": "mon", "task_type": "local_bash"})
        self.store.submit(self.run_dir.name, "int-bare", "interrupt", {"text": ""}, "coordinator")
        conversation.pump()
        request_id = next(frame["request_id"] for frame in sent if frame["request"].get("subtype") == "interrupt")
        conversation.on_event({"type": "control_response", "response": {"subtype": "success", "request_id": request_id}})
        conversation.on_event({"type": "result", "subtype": "error_during_execution"})
        conversation.on_event({"type": "system", "subtype": "session_state_changed", "state": "idle"})
        conversation.pump()
        self.assertEqual(self.shell_stops_sent(sent), ["mon"])

    def test_a_declared_leftover_stop_never_waits_for_a_follow_up_turn_in_either_order(self):
        for ack_first in (True, False):
            with self.subTest(ack_first=ack_first):
                conversation, sent, closed = self.live()
                conversation.session_state = "idle"
                conversation.on_event({"type": "system", "subtype": "task_started", "task_id": "svc", "task_type": "local_bash"})
                conversation.finalized = True
                conversation.pump()
                request_id = next(frame["request_id"] for frame in sent if frame["request"].get("subtype") == "stop_task")
                ack = {"type": "control_response", "response": {"subtype": "success", "request_id": request_id}}
                terminal = {"type": "system", "subtype": "task_notification", "task_id": "svc", "status": "stopped"}
                for event in ((ack, terminal) if ack_first else (terminal, ack)):
                    conversation.on_event(event)
                self.assertFalse(conversation.idle_stale)
                self.store.set_state(self.run_dir.name, "running", expect=("closing",))
                conversation.pump()
                self.assertEqual(closed, [True])
                self.assertEqual(self.shell_stops_sent(sent), ["svc"], "the shell is stopped once")

    def test_a_question_after_input_closed_creates_no_hold(self):
        conversation = duplex.Conversation(self.run_dir, self.store, self.run_dir.name, interactive=True, notes_path=None)
        conversation.input_closed = True
        conversation.on_event({"type": "control_request", "request_id": "late",
                               "request": {"subtype": "can_use_tool", "tool_name": "AskUserQuestion",
                                           "input": {"questions": [{"question": "late?"}]}}})
        self.assertEqual(self.store.interactions(self.run_dir.name), [])
        self.assertEqual(conversation.holding, set())
        self.assertFalse(conversation.stop_requested)

    def test_cancellation_while_a_question_waits_stops_and_releases(self):
        beat = self.root / "beat2.txt"
        self.start({"spawn": str(beat)}, {"sleep": .3}, {"ask": "Wait?"})
        self.pending()
        (self.run_dir / "cancel.json").write_text("{}")
        facts = self.finish()
        self.assertTrue(facts["cancelled"])
        self.assertEqual(facts["family"]["state"], "stopped")

    def test_a_terminal_task_update_without_notification_ends_the_wait(self):
        self.start({"bg_task": "ag-4", "type": "local_agent"}, {"bg_later": "ag-4", "delay": 1, "signal": "updated_only"})
        facts = self.finish()
        self.assertEqual(facts["legs"][0]["reason"], "exited")
        turns = [json.loads(line)["summary"] for line in (self.run_dir / "activity.jsonl").read_text().splitlines()
                 if json.loads(line)["kind"] == "turn"]
        self.assertEqual(turns[-1], "no further input is pending; closing Claude's input")
        self.assertEqual(len(turns), 3)

    def test_without_session_states_input_closes_after_the_turn_result(self):
        os.environ.pop("CLAUDE_CODE_SDK_READS_SESSION_STATE")
        self.start({"sleep": .2})
        facts = self.finish()
        self.assertEqual(facts["legs"][0]["reason"], "exited")
        self.assertIsNone(self.conversation.session_state)

    def test_an_interrupt_withdraws_a_pending_question_and_releases_the_hold(self):
        beat = self.root / "beat-int.txt"
        self.start({"spawn": str(beat)}, {"sleep": .3}, {"ask": "Which color?"})
        question = self.pending()[0]
        self.store.submit(self.run_dir.name, "int-q", "interrupt", {"text": "别问了，改查日志"}, "coordinator")
        self.until(lambda: self.store.interaction(question["interaction_id"])["status"] == "cancelled", message="withdrawal")
        facts = self.finish()
        self.assertEqual(facts["legs"][0]["reason"], "exited")
        self.assertIn("改查日志", self.probe("turns.jsonl")[0]["content"])
        self.assertEqual(self.store.interaction(question["interaction_id"])["hold"]["release_reason"], "cancelled")
        self.assertEqual(self.store.pending_blocking(self.run_dir.name), [])

    def test_a_note_with_an_unpaired_surrogate_is_ignored_without_stopping_supervision(self):
        self.notes.write_bytes(b'{"type":"note","text":"\\ud800"}\n{"type":"note","text":"valid"}\n')
        self.start({"sleep": .8})
        facts = self.finish()
        self.assertEqual(facts["legs"][0]["reason"], "exited")
        self.assertEqual([item["request"]["text"] for item in self.store.interactions(self.run_dir.name)], ["valid"])

    def test_a_supervisor_failure_still_stops_the_cli_and_its_background_processes(self):
        beat = self.root / "beat-fail.txt"
        self.start({"spawn": str(beat)}, {"sleep": 30})
        self.until(lambda: beat.exists() and beat.stat().st_size > 0, message="the background writer")
        original = self.conversation.pump

        def failing() -> None:
            raise RuntimeError("injected store failure")
        self.conversation.pump = failing
        facts = self.finish(timeout=30)
        self.conversation.pump = original
        self.assertEqual(facts["legs"][0]["reason"], "supervisor_error")
        self.assertIn("injected store failure", facts["supervisor_error"])
        self.assertEqual(facts["family"]["state"], "stopped")
        size = beat.stat().st_size
        time.sleep(.4)
        self.assertEqual(beat.stat().st_size, size, "the background writer must be stopped")

    def test_a_notes_fifo_cannot_block_cancellation(self):
        os.mkfifo(self.notes)
        beat = self.root / "beat-fifo.txt"
        started = time.monotonic()
        self.start({"spawn": str(beat)}, {"sleep": 30}, timeout=60)
        self.until(lambda: beat.exists() and beat.stat().st_size > 0, message="the background writer")
        (self.run_dir / "cancel.json").write_text("{}")
        facts = self.finish(timeout=15)
        self.assertTrue(facts["cancelled"])
        self.assertLess(time.monotonic() - started, 20)
        notes = [json.loads(line) for line in (self.run_dir / "activity.jsonl").read_text().splitlines()
                 if json.loads(line)["kind"] == "note"]
        self.assertEqual(notes[0]["summary"], "notes path is not a regular file; notes are ignored until it is")

    def test_idle_after_a_workflow_whose_completion_never_arrived_still_ends_the_run(self):
        # Host failure H2: result success and idle arrived, but a tracked Workflow kept input open forever.
        started = time.monotonic()
        self.start({"workflow": "pending"}, timeout=40)
        facts = self.finish(timeout=20)
        self.assertEqual(facts["legs"][0]["reason"], "exited")
        self.assertLess(time.monotonic() - started, 15)
        parsed = bridge.stream_parser.parse_stream(self.run_dir / "stream.jsonl")
        self.assertEqual([item["status"] for item in parsed["workflows"].values()], ["launched"])

    def lost_answer_child(self):
        child = self.root / "lost_answer_child.py"
        child.write_text(LOST_ANSWER_CHILD)
        self.cli = [sys.executable, str(child)]
        os.environ["LOST_CAPTURE"] = str(self.root)

    def test_a_written_but_unechoed_answer_is_carried_into_the_resumed_session(self):
        self.lost_answer_child()
        self.start(timeout=20)
        self.until(lambda: len(self.store.pending_blocking(self.run_dir.name)) == 2, message="two questions")
        first, second = self.store.interactions(self.run_dir.name, status="pending")
        self.store.submit(self.run_dir.name, "answer-one", "answer", {"text": "FIRST-REAL-ANSWER"}, "human",
                          interaction_id=first["interaction_id"])
        self.until(lambda: self.store.run(self.run_dir.name)["state"] == "held", message="the fallback stop")
        self.assertEqual(self.store.command(self.run_dir.name, "answer-one")["delivery"]["state"], "written")
        self.store.submit(self.run_dir.name, "answer-two", "answer", {"text": "SECOND-REAL-ANSWER"}, "human",
                          interaction_id=second["interaction_id"])
        facts = self.finish(timeout=20)
        self.assertEqual([leg["reason"] for leg in facts["legs"]], ["stop_for_answer", "exited"])
        resumed = json.loads((self.root / "resume.json").read_text())["message"]["content"]
        self.assertIn("FIRST-REAL-ANSWER", resumed)
        self.assertIn("SECOND-REAL-ANSWER", resumed)
        one = self.store.command(self.run_dir.name, "answer-one")["delivery"]
        self.assertEqual(one["state"], "echoed")
        self.assertIsNotNone(one["written_at"], "the original transport evidence is kept")

    def test_an_echoed_answer_is_not_sent_again_on_resume(self):
        capture = self.root / "resumed-once.txt"
        self.start({"ask": "First?"}, {"bg_task": "ag-9", "type": "local_agent"}, {"ask": "Second?"}, {"sleep": 5},
                   resume_script=[{"capture_brief": str(capture)}], env={"FAKE_STOP_TASK_FAIL": "1"})
        first = self.pending()[0]
        self.store.submit(self.run_dir.name, "e-1", "answer", {"text": "ANSWERED-LIVE"}, "coordinator",
                          interaction_id=first["interaction_id"])
        self.until(lambda: self.store.command(self.run_dir.name, "e-1")["delivery"]["state"] == "echoed", message="echo")
        second = self.until(lambda: [i for i in self.store.interactions(self.run_dir.name, status="pending")], message="Q2")[0]
        self.until(lambda: self.store.run(self.run_dir.name)["state"] == "held", message="the fallback stop")
        self.store.submit(self.run_dir.name, "e-2", "answer", {"text": "ANSWERED-HELD"}, "coordinator",
                          interaction_id=second["interaction_id"])
        self.finish()
        resumed = capture.read_text()
        self.assertIn("ANSWERED-HELD", resumed)
        self.assertNotIn("ANSWERED-LIVE", resumed)

    def test_a_cancel_before_the_last_answer_starts_no_new_execution(self):
        self.lost_answer_child()
        self.start(timeout=20)
        self.until(lambda: len(self.store.pending_blocking(self.run_dir.name)) == 2, message="two questions")
        first, second = self.store.interactions(self.run_dir.name, status="pending")
        self.store.submit(self.run_dir.name, "c-1", "answer", {"text": "one"}, "human", interaction_id=first["interaction_id"])
        self.until(lambda: self.store.run(self.run_dir.name)["state"] == "held", message="the fallback stop")
        (self.run_dir / "cancel.json").write_text("{}")
        self.store.submit(self.run_dir.name, "c-2", "answer", {"text": "two"}, "human", interaction_id=second["interaction_id"])
        facts = self.finish(timeout=20)
        self.assertTrue(facts["cancelled"])
        self.assertEqual(len(self.sessions), 1, "no resumed execution may start after cancellation")
        self.assertFalse((self.root / "resume.json").exists())

    def test_answers_and_a_cancellation_seen_together_keep_the_run_held(self):
        question = self.store.open_interaction(self.run_dir.name, "question", blocking=True,
                                               request={"questions": [{"question": "Q"}]})
        self.store.submit(self.run_dir.name, "late", "answer", {"text": "a"}, "human", interaction_id=question["interaction_id"])
        self.store.set_state(self.run_dir.name, "held")
        (self.run_dir / "cancel.json").write_text("{}")
        conversation = duplex.Conversation(self.run_dir, self.store, self.run_dir.name, interactive=True, notes_path=None)
        self.assertEqual(bridge._await_answers(self.run_dir, self.store, self.run_dir.name, time.monotonic() + 30, conversation),
                         ("cancelled", []))
        (self.run_dir / "cancel.json").unlink()
        self.assertEqual(bridge._await_answers(self.run_dir, self.store, self.run_dir.name, time.monotonic() - 1, conversation),
                         ("timed_out", []))
        self.assertEqual(self.store.run(self.run_dir.name)["state"], "held")

    def test_a_later_turn_that_dies_without_its_result_is_an_unfinished_turn(self):
        self.start({"sleep": .8}, env={"FAKE_CRASH_NEXT_TURN": "7"})
        self.until(lambda: self.conversation.transport is not None, message="the session")
        # Sent during the first turn; the CLI takes it as the next turn after the first result.
        self.store.submit(self.run_dir.name, "more-work", "update", {"text": "second requested action"}, "human")
        facts = self.finish()
        self.assertEqual(facts["exit_code"], 7)
        self.assertTrue(facts["unfinished_turn"])
        parsed = bridge.stream_parser.parse_stream(self.run_dir / "stream.jsonl")
        self.assertEqual((parsed["final"]["subtype"], parsed["inits"], len(parsed["turns"])), ("success", 2, 1))

    def test_ordinary_multi_turn_and_interrupted_turns_are_finished(self):
        self.start({"wait_message": "m.jsonl"})
        self.until(lambda: self.conversation.transport is not None, message="the session")
        self.store.submit(self.run_dir.name, "u-ok", "update", {"text": "next"}, "coordinator")
        facts = self.finish()
        self.assertFalse(facts["unfinished_turn"])


    def child(self, source, **env):
        child = self.root / "scripted_child.py"
        child.write_text(source)
        self.cli = [sys.executable, str(child)]
        os.environ.update(LOST_CAPTURE=str(self.root), **env)

    def test_messages_written_before_a_stop_for_answer_are_carried_into_the_resumed_session(self):
        self.child(HELD_MESSAGES_CHILD, HELD_EXPECT="3")
        self.start(timeout=20)
        question = self.pending()[0]
        self.store.submit(self.run_dir.name, "upd-echo", "update", {"text": "ECHO-ME early"}, "coordinator")
        self.until(lambda: self.store.command(self.run_dir.name, "upd-echo")["delivery"]["state"] == "echoed", message="echo")
        self.store.submit(self.run_dir.name, "upd-a", "update", {"text": "UPDATE-A"}, "coordinator")
        human_update = "\n    UPDATE-B\n    keep_code_indentation()\n\n"
        self.store.submit(self.run_dir.name, "upd-b", "update", {"text": human_update}, "human")
        self.until(lambda: self.store.run(self.run_dir.name)["state"] == "held", message="the fallback stop")
        first_write = self.store.command(self.run_dir.name, "upd-a")["delivery"]
        self.assertEqual((first_write["state"], first_write["echoed_at"]), ("written", None))
        self.store.submit(self.run_dir.name, "upd-c", "update", {"text": "UPDATE-C-WHILE-HELD"}, "coordinator")
        self.store.submit(self.run_dir.name, "ans-1", "answer", {"text": "THE-ANSWER"}, "coordinator",
                          interaction_id=question["interaction_id"])
        facts = self.finish(timeout=20)
        self.assertEqual([leg["reason"] for leg in facts["legs"]], ["stop_for_answer", "exited"])
        resumed = json.loads((self.root / "resume.json").read_text())["message"]["content"]
        self.assertIn("THE-ANSWER", resumed)
        blocks = ["【Codex 补充 · Codex 协调者 · upd-a】\nUPDATE-A", "【Codex 补充 · 用户原话 · upd-b】\n" + human_update,
                  "【Codex 补充 · Codex 协调者 · upd-c】\nUPDATE-C-WHILE-HELD"]
        self.assertEqual([resumed.find(block) for block in blocks], sorted(resumed.find(block) for block in blocks))
        self.assertTrue(all(block in resumed for block in blocks), resumed)
        self.assertNotIn("ECHO-ME", resumed, "an echoed message is already in the session")
        extra = self.root / "resumed-extra.jsonl"
        self.assertFalse(extra.exists() and "UPDATE-C" in extra.read_text(), "a carried message is not sent twice")
        after = self.store.command(self.run_dir.name, "upd-a")["delivery"]
        self.assertEqual(after["state"], "echoed")
        self.assertEqual(after["written_at"], first_write["written_at"], "the first transport evidence is kept")
        self.assertEqual(self.store.command(self.run_dir.name, "upd-c")["delivery"]["state"], "echoed")
        self.assertFalse(self.store.queued(self.run_dir.name))

    def test_an_answer_only_partly_written_before_the_stop_is_carried_into_the_resumed_session(self):
        self.child(PARTIAL_ANSWER_CHILD)
        self.start(timeout=30)
        self.until(lambda: len(self.store.pending_blocking(self.run_dir.name)) == 2, message="two questions")
        first, second = self.store.interactions(self.run_dir.name, status="pending")
        big = "ANSWER-ONE-START " + "x" * 400_000 + " ANSWER-ONE-END"
        self.store.submit(self.run_dir.name, "big-1", "answer", {"text": big}, "human", interaction_id=first["interaction_id"])
        time.sleep(.8)
        (self.root / "go").write_text("")
        self.until(lambda: self.store.run(self.run_dir.name)["state"] == "held", message="the fallback stop")
        partial = self.until(lambda: (lambda d: d if d["state"] != "queued" else None)(
            self.store.command(self.run_dir.name, "big-1")["delivery"]), message="the partial write evidence")
        self.assertEqual(partial["state"], "failed", "a frame cut short is never written")
        self.assertIn("byte(s)", partial["note"])
        self.store.submit(self.run_dir.name, "ans-2", "answer", {"text": "SECOND"}, "coordinator",
                          interaction_id=second["interaction_id"])
        facts = self.finish(timeout=30)
        self.assertEqual([leg["reason"] for leg in facts["legs"]], ["stop_for_answer", "exited"])
        resumed = json.loads((self.root / "resume.json").read_text())["message"]["content"]
        self.assertIn("ANSWER-ONE-END", resumed)
        self.assertIn("SECOND", resumed)
        after = self.store.command(self.run_dir.name, "big-1")["delivery"]
        self.assertEqual(after["state"], "echoed")
        self.assertIn("byte(s)", after["note"], "the partial attempt stays on record")
        self.assertEqual(self.store.interaction(first["interaction_id"])["status"], "answered")

    def test_stopping_a_held_execution_never_lets_its_background_work_run_again(self):
        beat = self.root / "beat-stop.txt"
        self.child(HEARTBEAT_CHILD, BEAT_FILE=str(beat))
        self.start(timeout=30)
        question = self.pending()[0]
        self.until(lambda: (self.store.interaction(question["interaction_id"]) or {}).get("hold"), message="hold record")
        time.sleep(.4)
        held_size = beat.stat().st_size
        time.sleep(.5)
        self.assertEqual(beat.stat().st_size, held_size, "the background writer is held")
        stop_requested = time.time()
        (self.run_dir / "cancel.json").write_text("{}")
        facts = self.finish(timeout=30)
        self.assertTrue(facts["cancelled"])
        self.assertEqual(facts["family"]["state"], "stopped")
        during_stop = [line for line in beat.read_text().splitlines() if float(line) >= stop_requested]
        self.assertEqual(during_stop, [], "a held writer ran again while the CLI was being stopped")
        hold = self.store.interaction(question["interaction_id"])["hold"]
        self.assertEqual((hold["release_reason"], hold["released_processes"]), ("stopping", 0))
        self.assertGreaterEqual(hold["killed_processes"], 1)

    def held_for_interrupt(self, capture):
        self.start({"bg_task": "wf-1", "type": "local_agent"}, {"ask": "Which schema?"}, {"sleep": 5},
                   resume_script=[{"capture_brief": str(capture)}], env={"FAKE_STOP_TASK_FAIL": "1"}, timeout=30)
        question = self.pending()[0]
        self.until(lambda: self.store.run(self.run_dir.name)["state"] == "held", message="the run to be held")
        return question

    def test_an_interrupt_while_held_withdraws_the_question_and_redirects_the_resumed_session(self):
        capture = self.root / "resumed-interrupt.txt"
        question = self.held_for_interrupt(capture)
        self.store.submit(self.run_dir.name, "upd-held", "update", {"text": "等待期间补充"}, "human")
        self.store.submit(self.run_dir.name, "int-held", "interrupt", {"text": "别等了，改查日志"}, "coordinator")
        self.until(lambda: self.store.interaction(question["interaction_id"])["status"] == "cancelled",
                   timeout=5, message="the question to be withdrawn")
        facts = self.finish(timeout=30)
        self.assertEqual([leg["reason"] for leg in facts["legs"]], ["stop_for_answer", "exited"])
        resumed = capture.read_text()
        self.assertIn("【Codex 打断 · Codex 协调者 · int-held】\n别等了，改查日志", resumed)
        self.assertIn(f"- {question['interaction_id']}：Which schema?", resumed)
        self.assertNotIn("回答来自", resumed, "an interrupt is not an answer")
        self.assertNotIn("不代表用户拒绝或撤回了问题", resumed)
        withdrawn = self.store.interaction(question["interaction_id"])
        self.assertIsNone(withdrawn["answer_command"])
        self.assertIn("int-held", withdrawn["answer"]["reason"])
        self.assertEqual(self.store.command(self.run_dir.name, "int-held")["delivery"]["state"], "echoed")
        self.assertFalse(facts["cancelled"])
        self.assertEqual(self.probe("interrupts.jsonl"), [], "the resumed session carries it; it is not interrupted again")
        events = [json.loads(line) for line in (self.run_dir / "activity.jsonl").read_text().splitlines()]
        messages = [(item["kind"], item["status"], item.get("text")) for item in events if item["kind"] in {"update", "interrupt"}]
        self.assertEqual(messages, [("update", "human", "等待期间补充"), ("interrupt", "coordinator", "别等了，改查日志")])

    def test_whitespace_only_interrupt_is_a_stop_in_the_resumed_message(self):
        content = duplex.resumed_text([], [{"kind": "interrupt", "command_id": "int-ws", "origin": "human",
                                           "payload": {"text": " \n\t "}}])
        self.assertIn("停下当前这一步", content)
        self.assertNotIn("并给出新的方向", content)

    def test_an_interrupt_without_text_while_held_stops_the_step_in_the_resumed_session(self):
        capture = self.root / "resumed-stop.txt"
        question = self.held_for_interrupt(capture)
        self.store.submit(self.run_dir.name, "int-bare", "interrupt", {}, "human")
        facts = self.finish(timeout=30)
        self.assertEqual([leg["reason"] for leg in facts["legs"]], ["stop_for_answer", "exited"])
        self.assertEqual(self.store.interaction(question["interaction_id"])["status"], "cancelled")
        resumed = capture.read_text()
        self.assertIn("【Codex 打断 · 用户原话 · int-bare】\n停下当前这一步", resumed)
        self.assertEqual(self.store.pending_blocking(self.run_dir.name), [])

    def test_a_cancel_wins_over_an_interrupt_while_held(self):
        self.held_for_interrupt(self.root / "never.txt")
        (self.run_dir / "cancel.json").write_text("{}")
        self.store.submit(self.run_dir.name, "int-late", "interrupt", {"text": "改向"}, "coordinator")
        facts = self.finish(timeout=30)
        self.assertTrue(facts["cancelled"])
        self.assertEqual(len(self.sessions), 1, "no resumed execution may start after cancellation")
        self.assertFalse(self.store.command(self.run_dir.name, "int-late")["delivery"]["state"] in {"written", "echoed"})

    def turn_child(self, mode, updates=1):
        self.child(TURN_CHILD, TURN_MODE=mode, TURN_UPDATES=str(updates))
        self.start(timeout=20)
        self.until(lambda: self.conversation.inits >= 1, message="init")
        for index in range(updates):
            self.store.submit(self.run_dir.name, f"turn-upd-{index}", "update", {"text": f"MID-TURN-{index}"}, "coordinator")
        return self.finish(timeout=20)

    def test_a_result_before_an_update_is_taken_in_does_not_finish_it(self):
        facts = self.turn_child("unechoed_exit")
        self.assertEqual(facts["exit_code"], 9)
        self.assertTrue(facts["unfinished_turn"], "the earlier turn's success must not stand for the unread update")
        self.assertEqual(self.store.command(self.run_dir.name, "turn-upd-0")["delivery"]["state"], "written")

    def test_an_update_replayed_but_never_followed_by_a_result_is_unfinished(self):
        facts = self.turn_child("echoed_exit")
        self.assertEqual(facts["exit_code"], 9)
        self.assertTrue(facts["unfinished_turn"])

    def test_an_abnormal_exit_after_absorbing_an_update_and_reporting_success_is_crashed(self):
        facts = self.turn_child("echoed_result_exit")
        self.assertEqual(facts["exit_code"], 9)
        self.assertFalse(facts["unfinished_turn"], "the query did return a result; the process then failed")
        self.assertEqual(self.store.command(self.run_dir.name, "turn-upd-0")["delivery"]["state"], "echoed")
        result = ExecuteOutcomeTests().run_execute(facts)
        self.assertEqual(result["outcome"], "crashed", "a successful result cannot mask an abnormal process exit")
        self.assertIn("9", result["note"])

    def test_updates_absorbed_into_the_running_turn_finish_with_its_result(self):
        for updates in (1, 2):
            with self.subTest(updates=updates):
                self.setUp()
                facts = self.turn_child("absorbed", updates)
                self.assertEqual(facts["exit_code"], 0)
                self.assertFalse(facts["unfinished_turn"])
                self.assertEqual({self.store.command(self.run_dir.name, f"turn-upd-{i}")["delivery"]["state"]
                                  for i in range(updates)}, {"echoed"})

class ExecuteOutcomeTests(unittest.TestCase):
    """execute() must not report an earlier turn's success for a later turn that died."""

    def run_execute(self, facts):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run-execute0001"
            run_dir.mkdir()
            (run_dir / "packet.json").write_text(json.dumps({"cwd": tmp, "max_budget_usd": None, "timeout_seconds": 30,
                                                             "inputs": [], "interactive": True}))
            (run_dir / "plan.json").write_text(json.dumps({"state_root": str(Path(tmp) / "state")}))
            (run_dir / "stream.jsonl").write_text(json.dumps({"type": "system", "subtype": "init"}) + "\n" + json.dumps(
                {"type": "result", "subtype": "success", "is_error": False}) + "\n")
            seen = {}

            def finalize(folder, outcome, note=None):
                seen.update(outcome=outcome, note=note)
                return {"run_outcome": outcome}
            environment = {"ready": True, "cli": {"path": "/bin/true", "version": "x", "budget_flag": True}}
            with mock.patch.multiple(bridge, finalize=finalize, build_command=lambda *a: ["claude"],
                                     prepare_workspace=lambda *a: {"profile": "readonly",
                                     "execution_cwd": tmp, "source_root": tmp}, freeze_inputs=lambda *a: ([], []),
                                     supervise=lambda *a, **k: {"legs": [{}], "exit_code": facts.get("exit_code"), **facts}), \
                    mock.patch.object(bridge.cli_env, "check", return_value=environment), \
                    mock.patch.object(bridge.instruction_layer, "scan", return_value={}), \
                    mock.patch.object(bridge.brief_renderer, "render", return_value="brief"):
                bridge.execute(run_dir)
            return seen

    def test_an_unfinished_later_turn_is_crashed_and_a_finished_one_is_ok(self):
        crashed = self.run_execute({"unfinished_turn": True, "exit_code": 7})
        self.assertEqual(crashed["outcome"], "crashed")
        self.assertIn("never produced its result", crashed["note"])
        self.assertEqual(self.run_execute({"unfinished_turn": False, "exit_code": 0})["outcome"], "ok")


class LaunchOutcomeTests(unittest.TestCase):
    """execute() with real CLI processes; only the OS write boundary is left out of the command (a test double)."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="launch ")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.state = self.root / "state"
        self.run_dir = self.state / "v1" / "runs" / "run-launchtest01"
        self.run_dir.mkdir(parents=True)
        self.work = self.root / "work"
        self.work.mkdir()
        ControlStore(self.state / "v2" / "control.sqlite3").register_run(self.run_dir.name, "T-1", None, "bridge-worker", True)
        patcher = mock.patch.dict(os.environ, {"FAKE_PROBES": str(self.root / "probes"), "FAKE_SCRIPT": "[]"})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.sessions = []
        self.seen = {}

    def fake_command(self, session):
        self.sessions.append(session)
        flag = "--resume" if session[0] == "resume" else "--session-id"
        return [sys.executable, str(Path(__file__).parent / "fake_claude.py"), "-p", "--input-format", "stream-json",
                "--output-format", "stream-json", "--verbose", "--replay-user-messages", "--permission-mode", "auto",
                "--permission-prompt-tool", "stdio", "--tools", "default", flag, session[1]]

    def execute(self, plan, build=None, protected=None, **patches):
        (self.run_dir / "packet.json").write_text(json.dumps({"cwd": str(self.work), "kind": "analyze", "max_budget_usd": None,
                                                              "timeout_seconds": 30, "inputs": [], "interactive": True,
                                                              "model": "sonnet", "effort": "low"}))
        (self.run_dir / "plan.json").write_text(json.dumps({"state_root": str(self.state), **plan}))

        def finalize(folder, outcome, note=None):
            self.seen.update(outcome=outcome, note=note)
            return {"run_outcome": outcome}
        workspace = {"profile": "readonly", "execution_cwd": str(self.work), "source_root": str(self.work),
                     "protected_source": [str(protected or self.work)], "channel": str(self.run_dir / "channel")}
        if build is not None:
            patches["build_command"] = lambda packet, context, cli, run_dir, session: build(session)
        environment = {"ready": True, "cli": {"path": sys.executable, "version": "x", "budget_flag": True}}
        with mock.patch.multiple(bridge, finalize=finalize, prepare_workspace=lambda *a: dict(workspace),
                                 freeze_inputs=lambda *a: ([], []), locate_claude=lambda: None,
                                 cli_environment=lambda *a: dict(os.environ), **patches), \
                mock.patch.object(bridge.cli_env, "check", return_value=environment), \
                mock.patch.object(bridge.instruction_layer, "scan", return_value={}), \
                mock.patch.object(bridge.brief_renderer, "render", return_value="brief"):
            bridge.execute(self.run_dir)
        return self.seen, json.loads((self.run_dir / "execution.json").read_text())

    def test_a_resume_that_cannot_be_launched_reports_the_os_error_and_tries_no_fresh_session(self):
        missing = str(self.root / "missing-claude")
        seen, execution = self.execute({"resume_session": "old-session"},
                                       build=lambda session: [missing] if session[0] == "resume" else self.fake_command(session))
        self.assertEqual(seen["outcome"], "not_started", seen)
        self.assertIn("FileNotFoundError", seen["note"])
        self.assertIn("resume session old-session", seen["note"])
        self.assertIn("No fresh session was tried", seen["note"])
        self.assertEqual(self.sessions, [], "a launch error is not a stale session")
        self.assertEqual([leg["reason"] for leg in execution["legs"]], ["launch_failed"])
        self.assertNotIn("resume_fallback", execution)
        self.assertFalse(execution["launched"])

    def test_a_resumed_session_the_cli_rejects_still_falls_back_to_a_fresh_one(self):
        os.environ["FAKE_RESUME_FAIL"] = "1"
        seen, execution = self.execute({"resume_session": "old-session"}, build=self.fake_command)
        self.assertEqual(seen["outcome"], "ok", seen)
        self.assertIn("did not start", execution["resume_fallback"])
        self.assertEqual([session[0] for session in self.sessions][-2:], ["resume", "session"])

    def test_a_readonly_directory_that_holds_the_state_is_not_started_with_the_layout_reason(self):
        seen, execution = self.execute({}, protected=self.root)
        self.assertEqual(seen["outcome"], "not_started", seen)
        self.assertIn(f"contains the orchestrator state directory {self.state}", seen["note"])
        self.assertIn("CLAUDE_ORCHESTRATOR_STATE_DIR", seen["note"])
        self.assertIn("Claude was not launched", seen["note"])
        self.assertFalse(execution["launched"])
        self.assertFalse((self.run_dir / "child.json").exists())

    def test_a_readonly_directory_beside_the_state_passes_the_layout_check(self):
        calls = []

        def supervise(*args, **kwargs):
            calls.append(args[1](args[7]))
            return {"legs": [{"reason": "exited"}], "exit_code": 0, "cancelled": False, "timed_out": False}
        seen, _ = self.execute({}, supervise=supervise)
        self.assertEqual(len(calls), 1, seen)
        self.assertEqual(calls[0][0], "/usr/bin/sandbox-exec", "the real readonly boundary is built")

class TransportTests(unittest.TestCase):
    def test_a_frame_is_written_only_after_its_last_byte_reaches_the_pipe(self):
        import subprocess
        from run_process import ProcessStream
        with tempfile.TemporaryDirectory() as tmp:
            proc = subprocess.Popen([sys.executable, "-c", "import sys, time; time.sleep(30)"], stdin=subprocess.PIPE,
                                    stdout=subprocess.PIPE)
            self.addCleanup(proc.kill)
            with open(Path(tmp) / "s.jsonl", "w") as stream:
                transport = ProcessStream(proc, stream, memoryview(b""), Path(tmp), False, keep_open=True)
                seen = []
                transport.send(b"x" * 10, lambda ok, n: seen.append(("first", ok, n)))
                self.assertEqual(seen, [], "queued in memory is not written")
                transport.drain(.2)
                self.assertEqual(seen, [("first", True, 11)])
                transport.send(b"y" * 400_000, lambda ok, n: seen.append(("big", ok, n)))
                transport.drain(.2)
                transport.close_input("incomplete")
                self.assertEqual(seen[1][:2], ("big", False))
                self.assertGreater(seen[1][2], 0, "a partly written frame reports the bytes that left")
                transport.send(b"z", lambda ok, n: seen.append(("late", ok, n)))
                self.assertEqual(len(seen), 2, "a closed input accepts nothing")
                transport.finish()


if __name__ == "__main__":
    unittest.main()
