"""A deterministic stand-in for the Claude CLI used by end-to-end tests.

FAKE_SCRIPT is a JSON list of actions executed in the working directory;
FAKE_PROBES names a directory outside every protected path where write
probes record whether the OS allowed them.  With --input-format stream-json
the fake speaks the CLI's duplex protocol as observed on CLI 2.1.310: one
init per turn, can_use_tool control requests answered by the host,
--replay-user-messages echoes, interrupt control requests ending the turn
with an error result (and a control_cancel_request for a request still
waiting), session_state_changed running/requires_action/idle when
CLAUDE_CODE_SDK_READS_SESSION_STATE=1, stop_task answered with a terminal
task_updated and task_notification, and task_started carrying task_type.
"""
import functools
import json
import os
from pathlib import Path
import re
import select
import subprocess
import sys
import time
import types

FLAGS = ("-p --model --effort --input-format --output-format --verbose --replay-user-messages --json-schema "
         "--session-id --resume --permission-mode --tools --allowedTools --disallowedTools --strict-mcp-config "
         "--mcp-config --disable-slash-commands --max-budget-usd --no-session-persistence")
DEFAULT_TOOLS = "AskUserQuestion,Agent,Bash,Edit,Read,Write,Glob,Grep,Workflow,WebFetch"


def session_state(emit, state):
    if os.environ.get("CLAUDE_CODE_SDK_READS_SESSION_STATE") == "1":
        emit({"type": "system", "subtype": "session_state_changed", "state": state})


class Channel:
    """Line-oriented host input; user messages that arrive early wait until the fake 'reads' them."""

    def __init__(self, duplex, replay, emit):
        self.duplex, self.replay, self.emit = duplex, replay, emit
        self.stopped = []
        self.responses = {}
        self.buffer = b""
        self.queued = []
        self.interrupted = False
        self.closed = not duplex

    def _line(self, timeout=None):
        """One JSON line from stdin; None on EOF or when *timeout* passes first (like the CLI, input is never ignored)."""
        deadline = None if timeout is None else time.monotonic() + timeout
        while b"\n" not in self.buffer:
            if self.closed:
                return None
            wait = None if deadline is None else max(0.0, deadline - time.monotonic())
            if not select.select([0], [], [], wait)[0]:
                return None
            chunk = os.read(0, 65536)
            if not chunk:
                self.closed = True
                return None
            self.buffer += chunk
        raw, self.buffer = self.buffer.split(b"\n", 1)
        return json.loads(raw)

    def serve(self, seconds):
        """Keep answering host control requests while the fake 'works' for *seconds*."""
        deadline = time.monotonic() + seconds
        while not self.closed and time.monotonic() < deadline:
            item = self._line(timeout=max(0.0, deadline - time.monotonic()))
            if item is not None:
                self._handle(item)

    def _handle(self, item):
        if item.get("type") == "control_request" and item.get("request", {}).get("subtype") == "interrupt":
            self.interrupted = True
            self.emit({"type": "control_response", "response": {"subtype": "success", "request_id": item["request_id"],
                                                               "response": {"still_queued": []}}})
            return None
        if item.get("type") == "control_request" and item.get("request", {}).get("subtype") == "stop_task":
            task_id = item["request"].get("task_id")
            if os.environ.get("FAKE_STOP_TASK_FAIL") == "1":
                self.emit({"type": "control_response", "response": {"subtype": "error", "request_id": item["request_id"],
                                                                   "error": "fixture refuses stop_task"}})
                return None
            self.stopped.append(task_id)
            self.emit({"type": "system", "subtype": "task_updated", "task_id": task_id, "patch": {"status": "killed"}})
            self.emit({"type": "system", "subtype": "task_notification", "task_id": task_id, "status": "stopped"})
            self.emit({"type": "control_response", "response": {"subtype": "success", "request_id": item["request_id"],
                                                               "response": {}}})
            return None
        if item.get("type") == "user":
            self.queued.append(item)
            return None
        return item

    def wait_interrupt(self, timeout):
        deadline = time.monotonic() + timeout
        while not self.interrupted and not self.closed and time.monotonic() < deadline:
            item = self._line()
            if item is not None:
                self._handle(item)

    def control_response(self, request_id):
        while request_id not in self.responses and not self.closed:
            if self.interrupted:
                # The CLI abandons a request that an interrupt overtook.
                self.emit({"type": "control_cancel_request", "request_id": request_id})
                return {"cancelled": True}
            item = self._line()
            if item is None:
                break
            item = self._handle(item)
            if item and item.get("type") == "control_response":
                self.responses[item["response"].get("request_id")] = item["response"]
        return self.responses.pop(request_id, None)

    def user_message(self, timeout=None):
        deadline = None if timeout is None else time.monotonic() + timeout
        while not self.queued and not self.closed and (deadline is None or time.monotonic() < deadline):
            item = self._line(timeout=None if deadline is None else max(0.0, deadline - time.monotonic()))
            if item is not None:
                self._handle(item)
        if not self.queued:
            return None
        item = self.queued.pop(0)
        if self.replay:
            self.emit({**item, "isReplay": True, "uuid": "replay-" + str(time.monotonic())})
        content = item["message"]["content"]
        return content if isinstance(content, str) else json.dumps(content)


INTERACTIVE = frozenset({"ask", "ask_many", "permission", "wait_message", "bg_task", "bg_done", "bg_later", "spawn",
                         "note", "wait_interrupt"})


def interactive(kind, action, counter, io):
    """Duplex-only actions; returns "unanswered" or "interrupted" when the scripted turn must stop."""
    if kind in {"ask", "permission"}:
        ident = f"{kind}-{counter}"
        if kind == "ask":
            name = "AskUserQuestion"
            value = {"questions": [{"question": action["ask"], "header": "Q", "multiSelect": False,
                                    "options": [{"label": label, "description": ""} for label in action.get("options", [])]}]}
        else:
            name, value = action["permission"], action.get("input", {})
        io.emit({"type": "assistant", "parent_tool_use_id": None,
                 "message": {"model": "fixture-model", "content": [{"type": "tool_use", "id": ident, "name": name, "input": value}]}})
        request_id = f"req-{counter}"
        session_state(io.emit, "requires_action")
        io.emit({"type": "control_request", "request_id": request_id,
                 "request": {"subtype": "can_use_tool", "tool_name": name, "display_name": name, "input": value,
                             "tool_use_id": ident, "requires_user_interaction": kind == "ask"}})
        response = io.channel.control_response(request_id)
        io.record(action.get("record", "answers.jsonl"), {"request_id": request_id, "response": response})
        if response is None:
            return "unanswered"
        if response.get("cancelled"):
            io.result_event("error_during_execution", True)
            return "interrupted"
        session_state(io.emit, "running")
        io.emit({"type": "user", "parent_tool_use_id": None,
                 "message": {"content": [{"type": "tool_result", "tool_use_id": ident, "content": json.dumps(response)}]}})
    elif kind == "ask_many":
        # Parallel questions of one assistant message: every request is outstanding before any answer.
        requests = []
        for index, text in enumerate(action["ask_many"]):
            ident, request_id = f"many-{counter}-{index}", f"req-{counter}-{index}"
            value = {"questions": [{"question": text, "header": "Q", "multiSelect": False, "options": []}]}
            io.emit({"type": "assistant", "parent_tool_use_id": None, "message": {"model": "fixture-model", "content": [
                {"type": "tool_use", "id": ident, "name": "AskUserQuestion", "input": value}]}})
            session_state(io.emit, "requires_action")
            io.emit({"type": "control_request", "request_id": request_id, "request": {
                "subtype": "can_use_tool", "tool_name": "AskUserQuestion", "input": value, "tool_use_id": ident}})
            requests.append((ident, request_id))
        if action.get("then_agent"):
            # Real CLI 2.1.310 order: a background agent of the same message can report task_started after the question.
            io.emit({"type": "system", "subtype": "task_started", "task_id": action["then_agent"], "task_type": "local_agent",
                     "description": "started with the question"})
        for ident, request_id in requests:
            response = io.channel.control_response(request_id)
            io.record("answers.jsonl", {"request_id": request_id, "response": response})
            if response is None:
                return "unanswered"
            if response.get("cancelled"):
                io.result_event("error_during_execution", True)
                return "interrupted"
            io.emit({"type": "user", "parent_tool_use_id": None,
                     "message": {"content": [{"type": "tool_result", "tool_use_id": ident, "content": json.dumps(response)}]}})
        session_state(io.emit, "running")
    elif kind == "wait_message":
        io.record(action["wait_message"], {"content": io.channel.user_message(timeout=action.get("timeout", 20))})
    elif kind == "bg_task":
        # The tool that started the task decides whether a hold can suspend it (Bash) or the run must stop (Workflow).
        tool = action.get("tool", "Workflow")
        io.emit({"type": "assistant", "parent_tool_use_id": None,
                 "message": {"model": "fixture-model", "content": [{"type": "tool_use", "id": "bg-" + action["bg_task"],
                                                                    "name": tool, "input": {}}]}})
        started = {"type": "system", "subtype": "task_started", "task_id": action["bg_task"], "description": "background",
                   "tool_use_id": "bg-" + action["bg_task"]}
        if action.get("type"):
            started["task_type"] = action["type"]
        if action.get("type", "local_bash" if tool == "Bash" else "local_workflow") != "local_bash":
            # CLI 2.1.310 reports idle only after its background agents and workflows end.
            io.agents.add(action["bg_task"])
        io.emit(started)
    elif kind == "bg_later":
        io.deferred.append(action)
    elif kind == "bg_done":
        io.agents.discard(action["bg_done"])
        io.emit({"type": "system", "subtype": "task_notification", "task_id": action["bg_done"], "status": "completed"})
    elif kind == "spawn":
        # A child in the same process group that keeps writing: proves whether a hold stopped it.
        subprocess.Popen([sys.executable, "-c", "import sys, time\nwhile True:\n    open(sys.argv[1], 'a').write(f'{time.time()}\\n')\n"
                          "    time.sleep(.05)", action["spawn"]], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    elif kind == "note":
        assert io.notes, "brief names no notes file"
        with open(io.notes[-1], "a", encoding="utf-8") as handle:
            handle.write(json.dumps(action["note"], ensure_ascii=False) + "\n")
    elif kind == "wait_interrupt":
        io.channel.wait_interrupt(action.get("timeout", 20))
        io.record("interrupts.jsonl", {"interrupted": io.channel.interrupted})
        if io.channel.interrupted:
            io.result_event("error_during_execution", True)
            return "interrupted"
    return None


FILE_ACTIONS = frozenset({"write", "binary", "delete", "attempt_write", "sleep", "touch", "wait_for", "background"})


def file_action(kind, action, counter, probes):
    if kind == "write":
        path = Path(action["write"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(action.get("text", ""), encoding="utf-8")
    elif kind == "binary":
        Path(action["binary"]).write_bytes(bytes.fromhex(action["hex"]))
    elif kind == "delete":
        Path(action["delete"]).unlink()
    elif kind == "attempt_write":
        target = Path(action["attempt_write"])
        try:
            with target.open("a") as handle:
                handle.write("tampered")
            outcome = "allowed"
        except OSError as exc:
            outcome = type(exc).__name__
        probes.mkdir(parents=True, exist_ok=True)
        (probes / f"{counter}.json").write_text(json.dumps({"target": str(target), "outcome": outcome}))
    elif kind == "sleep":
        time.sleep(action["sleep"])
    elif kind == "touch":
        Path(action["touch"]).write_text("ready")
    elif kind == "wait_for":
        deadline = time.monotonic() + 15
        while not Path(action["wait_for"]).exists() and time.monotonic() < deadline:
            time.sleep(.02)
    elif kind == "background":
        subprocess.Popen([sys.executable, "-c", f"import time; time.sleep({action['background']})"],
                         start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def init_event(emit, mode):
    tools = os.environ.get("FAKE_TOOLS", DEFAULT_TOOLS)
    emit({"type": "system", "subtype": "init", "model": "fixture-model", "cwd": os.getcwd(),
          "tools": [name for name in tools.split(",") if name], "plugins": [{"name": "fixture-plugin"}],
          "memory_paths": {}, "permissionMode": os.environ.get("FAKE_PERMISSION_MODE", mode)})


def final_event(emit, subtype="success", error=False, extra=None):
    event = {"type": "result", "subtype": subtype, "is_error": error, "parent_tool_use_id": None,
             "result": None if error else "Delivered as requested.", "total_cost_usd": 0.0123,
             "permission_denials": [], "stop_reason": "tool_use" if error else "end_turn",
             "terminal_reason": "aborted_tools" if error else "completed",
             "usage": {"input_tokens": 10, "output_tokens": 20, "cache_read_input_tokens": 0,
                       "cache_creation_input_tokens": 0},
             "modelUsage": {"fixture-model": {"inputTokens": 10, "outputTokens": 20, "cacheReadInputTokens": 0,
                                              "cacheCreationInputTokens": 0, "costUSD": 0.0123}}}
    event.update(extra or {})
    emit(event)


def meta(args):
    """Answer --version, --help and auth status; None for a real session."""
    if args == ["--version"]:
        return "2.1.310 (Claude Code fixture)"
    if args == ["--help"]:
        return os.environ.get("FAKE_HELP", FLAGS)
    if args[:3] == ["auth", "status", "--json"]:
        return json.dumps({"loggedIn": os.environ.get("FAKE_LOGGED_IN", "1") == "1", "authMethod": "claude.ai",
                           "email": "someone@example.test"})
    return None


def later_turns(channel, record, init, result_event, io):
    """Every new user message is one more turn of the same process, until stdin closes.

    A background task that finishes after the turn's result wakes one more turn, as the CLI does.
    """
    for action in io.deferred:
        channel.serve(action.get("delay", 1)) if channel.duplex else time.sleep(action.get("delay", 1))
        if action["bg_later"] in channel.stopped:
            continue
        status = action.get("status", "completed")
        io.emit({"type": "system", "subtype": "task_updated", "task_id": action["bg_later"],
                 "patch": {"status": "killed" if status == "stopped" else status}})
        if action.get("signal") != "updated_only":
            io.emit({"type": "system", "subtype": "task_notification", "task_id": action["bg_later"], "status": status})
        io.agents.discard(action["bg_later"])
        if action.get("turn", True):
            session_state(io.emit, "running")
            init()
            result_event()
        if not io.agents - set(channel.stopped):
            session_state(io.emit, "idle")
    while channel.duplex and not channel.closed:
        message = channel.user_message()
        if message is None:
            break
        record("turns.jsonl", {"content": message})
        session_state(io.emit, "running")
        init()
        if os.environ.get("FAKE_CRASH_NEXT_TURN"):
            # The later turn starts and the process dies before its result.
            sys.exit(int(os.environ["FAKE_CRASH_NEXT_TURN"]))
        result_event()
        session_state(io.emit, "idle")


def main() -> int:
    args = sys.argv[1:]
    answer = meta(args)
    if answer is not None:
        print(answer)
        return 0

    def option(name):
        return args[args.index(name) + 1] if name in args else None

    session = option("--resume") or option("--session-id")
    if option("--resume") and os.environ.get("FAKE_RESUME_FAIL") == "1":
        print("No conversation found with session ID", file=sys.stderr)
        return 1
    probes = Path(os.environ.get("FAKE_PROBES", "/nonexistent"))
    duplex = option("--input-format") == "stream-json"

    def emit(event):
        print(json.dumps({"session_id": session, **event}, ensure_ascii=False), flush=True)

    channel = Channel(duplex, "--replay-user-messages" in args, emit)
    if duplex:
        text = channel.user_message() or ""
        brief = text.encode("utf-8")
    else:
        brief = sys.stdin.buffer.read()
        text = brief.decode("utf-8")

    def record(name, value):
        probes.mkdir(parents=True, exist_ok=True)
        with (probes / name).open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(value, ensure_ascii=False) + "\n")

    def tool(name, value, ident):
        emit({"type": "assistant", "parent_tool_use_id": None,
              "message": {"model": "fixture-model", "content": [{"type": "tool_use", "id": ident, "name": name, "input": value}]}})
        emit({"type": "user", "parent_tool_use_id": None,
              "message": {"content": [{"type": "tool_result", "tool_use_id": ident, "content": "ok"}]}})

    init = functools.partial(init_event, emit, option("--permission-mode"))
    result_event = functools.partial(final_event, emit)

    session_state(emit, "running")
    init()
    # A real first turn spends model time between init and its first tool process; MCP servers start before init.
    time.sleep(float(os.environ.get("FAKE_FIRST_TURN_DELAY", ".3")))
    io = types.SimpleNamespace(channel=channel, emit=emit, record=record, result_event=result_event,
                               notes=re.findall(r"`([^`]+notes\.jsonl)`", text), deferred=[], agents=set())
    outboxes = re.findall(r"`([^`]+)/report\.md`", text)
    outbox = outboxes[-1] if outboxes else None
    structured = None
    denials = []
    result = True
    exit_code = 0
    counter = 0
    script = os.environ.get("FAKE_RESUME_SCRIPT") if option("--resume") and os.environ.get("FAKE_RESUME_SCRIPT") \
        else os.environ.get("FAKE_SCRIPT", "[]")
    for action in json.loads(script):
        counter += 1
        kind = next(iter(action))
        if kind in FILE_ACTIONS:
            file_action(kind, action, counter, probes)
        elif kind == "capture_brief":
            Path(action["capture_brief"]).write_bytes(brief)
        elif kind == "read_inputs":
            for ident, path in enumerate(re.findall(r"- `([^`]+)`（原路径", text)):
                tool("Read", {"file_path": path}, f"read-{ident}")
        elif kind == "tool":
            value = {key: item.replace("{cwd}", os.getcwd()) if isinstance(item, str) else item
                     for key, item in action.get("input", {}).items()}
            tool(action["tool"], value, f"tool-{counter}")
        elif kind == "workflow":
            ident = f"workflow-{counter}"
            tool("Workflow", {"name": "codex-analyze", "args": {}}, ident)
            emit({"type": "system", "subtype": "task_started", "tool_use_id": ident, "task_id": f"task-{counter}"})
            if action["workflow"] != "pending":
                emit({"type": "system", "subtype": "task_notification", "tool_use_id": ident,
                      "task_id": f"task-{counter}", "status": action["workflow"]})
        elif kind in INTERACTIVE:
            stop = interactive(kind, action, counter, io)
            if stop:
                result, exit_code = False, (3 if stop == "unanswered" else 0)
                break
        elif kind == "deliver":
            assert outbox, "brief names no outbox"
            folder = Path(outbox)
            if "report" in action:
                (folder / "report.md").write_text(action["report"], encoding="utf-8")
            if "result" in action:
                value = action["result"]
                (folder / "result.json").write_text(value if isinstance(value, str) else json.dumps(value, ensure_ascii=False),
                                                    encoding="utf-8")
        elif kind == "structured":
            structured = action["structured"]
        elif kind == "denial":
            denials.append({"tool_name": action["denial"], "tool_use_id": f"denied-{counter}",
                            "tool_input": {"url": "https://example.test"}})
        elif kind == "no_result":
            result = False
            exit_code = action["no_result"]
    if result:
        extra = {"permission_denials": denials}
        if structured is not None:
            extra["structured_output"] = structured
        result_event(extra=extra)
    if exit_code:
        return exit_code
    if not io.agents - set(channel.stopped):
        session_state(emit, "idle")
    later_turns(channel, record, init, result_event, io)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
