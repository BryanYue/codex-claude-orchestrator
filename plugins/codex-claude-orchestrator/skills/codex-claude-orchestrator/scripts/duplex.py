"""The live conversation between the bridge and one Claude CLI stream-json session.

Claude raises serious questions with its native AskUserQuestion tool; the CLI
sends them, and any permission escalation, as ``can_use_tool`` control
requests and waits for the host.  Each one becomes a durable pending
interaction.  Nonblocking notes and disputes are typed JSON lines Claude
appends to its notes file; they are acknowledged as soon as they are read.
Codex's answers, updates and interrupts arrive as queued commands in the
control store and are written to the CLI's stdin here.

A pending blocking question must really stop execution, not merely ask
Claude to wait.  The CLI itself is blocked on the control request; every
other process of the run, background shell commands included, is SIGSTOPped
until every blocking question is answered (``family_hold``).  Background
agents and workflows run inside the CLI process, so they are stopped with the
CLI's native ``stop_task`` control request and Claude is told so with the
answer.  Only when the CLI does not confirm that stop does the bridge stop the
whole execution and resume the session once every pending question has an
answer (``stop_for_answer``).

Input closes after a result once the CLI reports ``session_state_changed:
idle``, nothing Codex sent is unwritten or unechoed, and no blocking question is
pending.  CLI 2.1.310 holds ``idle`` back while a background agent or workflow
is still running and reports it only after their follow-up turns, so a fresh
idle is authoritative even when a task's own terminal event never arrived.
Without session states, input closes once the turn's result arrived and no
tracked background task is in flight.
Background shells (Bash ``run_in_background``, Monitor) differ: CLI 2.1.310
reports ``idle`` while they still run, and their end wakes one more turn that
consumes their output.  Idle with a shell still running is therefore not the
end of the session; input stays open until every tracked shell has ended and a
fresh idle follows.  Only Claude's structured ``{"type": "final"}`` notes line
says shells still running are leftovers (a server, a watcher): after it, a
fresh idle stops them with ``stop_task`` and closes input.  Any message Codex
writes afterwards voids that line.  Cancellation and the deadline stop the run
regardless.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import stat
import time
from typing import Any

from control_store import ControlStore
from events import append as activity
from process_family import Family, Identity, signal_identity

TASK_DONE = frozenset({"completed", "failed", "error", "killed", "stopped", "cancelled", "timeout"})
# Task types that keep the session producing turns (upstream SDK DEFERRING_TASK_TYPES).
DEFERRING_TASK_TYPES = frozenset({"local_agent", "local_workflow"})
SHELL_TASK_TYPES = frozenset({"local_bash"})
NOTE_TYPES = ("note", "dispute")
FINAL_TYPE = "final"
NOTE_LIMIT = 4000
NOTES_READ_LIMIT = 1024 * 1024
STOP_TASK_CONFIRM_SECONDS = 15.0
ORIGIN_LABELS = {"coordinator": "Codex 协调者", "human": "用户原话"}
SUMMARY_KEYS = ("file_path", "notebook_path", "path", "description", "url", "pattern")


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _short(value: Any, limit: int = 300) -> str:
    text = " ".join(str(value).split())
    return text[:limit] + ("…" if len(text) > limit else "")


def questions_of(request: dict[str, Any]) -> list[str]:
    raw = request.get("questions")
    return [str(item.get("question")) for item in raw if isinstance(item, dict) and item.get("question")] if isinstance(raw, list) else []


def answer_entries(interaction: dict[str, Any], payload: dict[str, Any]) -> list[tuple[str, str]]:
    """One (question or decision, answer) pair per item Claude asked; shared by live and resumed delivery."""
    request = _dict(interaction.get("request"))
    text = str(payload.get("text") or "")
    if interaction.get("kind") == "permission":
        decision = "allow" if payload.get("decision") == "allow" else "deny"
        return [(f"{request.get('tool_name')} {request.get('summary') or ''}".strip(), decision + (f"：{text}" if text else ""))]
    answers = _dict(payload.get("answers"))
    return [(question, str(answers.get(question) or text)) for question in questions_of(request)] or [("", text)]


def update_text(command_id: str, origin: str, text: str) -> str:
    return (f"【Codex 补充 · {ORIGIN_LABELS[origin]} · {command_id}】\n{text}\n\n"
            "（任务进行中的新信息。结合当前进度继续，不需要回复确认；与原任务冲突时写进 disputes。）")


def interrupt_text(command_id: str, origin: str, text: str) -> str:
    head = f"【Codex 打断 · {ORIGIN_LABELS[origin]} · {command_id}】\n"
    if text.strip():
        return head + f"{text}\n\n（Codex 打断了上一步并给出新的方向。停下被打断的工作，按这段内容继续；与原任务冲突时写进 disputes。）"
    return head + "停下当前这一步：不要继续被打断的工作，也不要开始新的工作；简要说明当前状态后结束这一轮。"


def resumed_text(answered: list[dict[str, Any]], messages: list[dict[str, Any]] | None = None,
                 withdrawn: list[dict[str, Any]] | None = None, identity: str | None = None) -> str:
    """The resumed session's first message: answers, questions an interrupt withdrew, then Codex's messages in order."""
    messages, withdrawn = messages or [], withdrawn or []
    lines: list[str] = []
    if answered:
        lines.append("这是对你此前用 AskUserQuestion 提出的问题（以及授权请求）的正式回答。")
    for interaction in answered:
        origin = ORIGIN_LABELS.get(str(interaction.get("answered_by")), "Codex 协调者")
        kind = "授权请求" if interaction.get("kind") == "permission" else "问题"
        lines.append(f"\n{kind} {interaction['interaction_id']}（回答来自{origin}）：")
        for asked, answer in answer_entries(interaction, _dict(interaction.get("answer"))):
            lines.append(f"- 你问：{asked}\n  回答：{answer}" if interaction.get("kind") != "permission"
                         else f"- 请求：{asked}\n  决定：{answer}")
    if withdrawn:
        lines.append("\n下面的问题已被 Codex 的打断撤回，不会再有回答：")
        for interaction in withdrawn:
            request = _dict(interaction.get("request"))
            asked = "；".join(questions_of(request)) or f"{request.get('tool_name') or ''} {request.get('summary') or ''}".strip()
            lines.append(f"- {interaction['interaction_id']}：{asked}")
    if messages:
        lines.append("\n下面是 Codex 发来、还不能确认已进入会话的消息，按发送顺序排列。停止前已送出的那些你可能已经看到过，看到过就忽略重复：")
        for command in messages:
            text = str(_dict(command.get("payload")).get("text") or "")
            origin = str(command.get("origin"))
            lines.append("\n" + (update_text(command["command_id"], origin, text) if command.get("kind") == "update"
                                 else interrupt_text(command["command_id"], origin, text)))
    mark = f"\n\n（这条恢复消息的编号：{identity}）" if identity else ""
    if not lines:
        return ("编排插件为暂停后台工作停止了上一段执行；等待的问题已被撤回或已处理。先检查工作目录和后台任务的当前状态，再继续原任务。"
                + mark)
    lines.append("\n说明：等待回答期间，编排插件主动中断并停止了上一段执行，以暂停后台工作。会话里随后出现的"
                 "“用户不想继续这次工具调用、STOP”之类的提示和后台任务被停止的通知，都是这次暂停造成的，"
                 + ("哪些问题被撤回以上文为准。按上面的内容处理" if withdrawn else "不代表用户拒绝或撤回了问题。按上面的回答继续")
                 + "；被停止的后台工作需要的话重新启动，先检查工作目录的当前状态。")
    return "\n".join(lines) + mark


class Conversation:
    """Owns the control-plane side of one run; the bridge owns processes and deadlines."""

    def __init__(self, run_dir: Path, store: ControlStore, run_id: str, *, interactive: bool, notes_path: Path | None):
        self.run_dir, self.store, self.run_id = run_dir, store, run_id
        self.interactive, self.notes_path = interactive, notes_path
        self.transport: Any = None
        self.cli_pid: int | None = None
        self.family: Family | None = None
        self.capability_error: str | None = None
        self.inits = 0
        self.results = 0
        self.session_id: str | None = None
        self.session_state: str | None = None
        self.idle_stale = False
        self.awaiting_result = True
        self.tasks: dict[str, tuple[str, str]] = {}
        self.tool_names: dict[str, str] = {}
        self.requests: dict[str, dict[str, Any]] = {}
        self.by_request: dict[str, str] = {}
        self.sending: set[str] = set()
        self.in_flight: dict[str, str] = {}
        self.update_texts: dict[str, str] = {}
        self.resume_texts: dict[str, list[str]] = {}
        # Commands a resumed first message carries; they are never also delivered on their own.
        self.carried: set[str] = set()
        self.withdrawn: list[dict[str, Any]] = []
        self.answer_tools: dict[str, str] = {}
        self.interrupts: dict[str, str] = {}
        self.stop_tasks: dict[str, str] = {}
        self.shell_stops: dict[str, str] = {}
        # Shells the close stopped, kept until their terminal event whether the CLI's ack came first or not.
        self.stopping_shells: set[str] = set()
        self.shells_stopped: list[str] = []
        # Claude's structured final line names the latest Codex message it has read ("after": the command id in that
        # message's header, or the run id before any).  Only a final naming the latest message Codex wrote counts, so
        # the check does not depend on when the host happens to observe the CLI taking that message in.
        self.finalized = False
        self.latest_input = run_id
        self.latest_command: str | None = None
        self.bare_interrupts: set[str] = set()
        self.background_wait_reported = False
        self.stop_deadline = 0.0
        self.stop_requested = False
        self.notes_offset = 0
        self.notes_identity: tuple[int, int] | None = None
        self.notes_problem: str | None = None
        self.held: dict[int, Identity] = {}
        self.holding: set[str] = set()
        self.input_closed = False
        self.session_ready = False
        self.next_hold_scan = 0.0
        # Descendants present when the session initialized (MCP servers, hooks) only answer the blocked CLI.
        self.infrastructure: set[int] = set()

    def attach(self, transport: Any, cli_pid: int, family: Family) -> None:
        self.transport, self.cli_pid, self.family = transport, cli_pid, family
        self.awaiting_result, self.input_closed, self.stop_requested = True, False, False
        # Background work of a stopped execution died with it; the resumed session reports its own.
        self.tasks.clear()
        self.session_state = None
        self.idle_stale = False
        self.infrastructure = set()
        self.session_ready = False
        self.background_wait_reported = False
        self.latest_command = None
        self._input_taken()

    def deferring_tasks(self) -> list[str]:
        return [task_id for task_id, (task_type, _) in self.tasks.items() if task_type not in SHELL_TASK_TYPES]

    def _input_taken(self) -> None:
        """New Codex input voids a final line read so far; only a final naming that input counts again."""
        self.finalized = False

    def _send(self, value: dict[str, Any], on_written: Any = None) -> bool:
        return self.transport is not None and bool(
            self.transport.send(json.dumps(value, ensure_ascii=True).encode("utf-8"), on_written))

    def _respond(self, request_id: str, response: dict[str, Any] | None = None, error: str | None = None,
                 on_written: Any = None) -> bool:
        body = ({"subtype": "error", "request_id": request_id, "error": error} if error is not None
                else {"subtype": "success", "request_id": request_id, "response": response or {}})
        return self._send({"type": "control_response", "response": body}, on_written)

    def on_event(self, event: dict[str, Any]) -> None:
        kind, subtype = event.get("type"), event.get("subtype")
        if kind == "system" and subtype == "init":
            self._init(event)
        elif kind == "system" and subtype == "session_state_changed":
            self.session_state = str(event.get("state"))
            self.idle_stale = False
        elif kind == "control_request":
            self._control_request(event)
        elif kind == "control_cancel_request":
            self._cancelled(str(event.get("request_id")))
        elif kind == "control_response":
            self._control_response(_dict(event.get("response")))
        elif kind == "user":
            self._user(event)
        elif kind == "assistant":
            for block in _dict(event.get("message")).get("content") or []:
                if isinstance(block, dict) and block.get("type") == "tool_use" and isinstance(block.get("id"), str):
                    self.tool_names[block["id"]] = str(block.get("name"))
        elif kind == "system" and subtype == "task_started" and event.get("task_id"):
            task_type = event.get("task_type")
            if not isinstance(task_type, str):
                tool = self.tool_names.get(str(event.get("tool_use_id")))
                task_type = "local_bash" if tool == "Bash" else "unknown"
            self.tasks[str(event["task_id"])] = (task_type, str(event.get("description") or ""))
            if self.holding and task_type not in SHELL_TASK_TYPES:
                # Started alongside the question (same assistant message): it must not run while Claude waits.
                self._stop_deferring(sorted(self.holding)[0])
        elif kind == "system" and subtype == "task_notification" and event.get("status") in TASK_DONE:
            self._task_ended(str(event.get("task_id")))
        elif kind == "system" and subtype == "task_updated" and _dict(event.get("patch")).get("status") in TASK_DONE:
            # A terminal patch can be the only completion signal; no task_notification may follow.
            self._task_ended(str(event.get("task_id")))
        elif kind == "result" and event.get("parent_tool_use_id") is None:
            self.results += 1
            # A message written before this result but not yet replayed was not part of this turn: the CLI has
            # not taken it as input, so this result does not answer for it.  A replayed one was taken in.
            self.awaiting_result = bool(self.update_texts)
            activity(self.run_dir, "turn", "Claude finished a turn", status=str(subtype or "result"))

    def _task_ended(self, task_id: str) -> None:
        ended = self.tasks.pop(task_id, None)
        if task_id in self.stopping_shells:
            # Stopped by the close below: no follow-up turn is owed for a shell Claude declared a leftover.
            self.stopping_shells.discard(task_id)
            return
        if ended is not None and self.session_state == "idle":
            # Any background task ending can start one more turn; only the CLI's next idle says the session is done.
            self.idle_stale = True
        elif ended is not None and self.session_state is None and not self.awaiting_result:
            # Without session states only the follow-up turn's result says that turn is done.
            self.awaiting_result = True

    def _init(self, event: dict[str, Any]) -> None:
        self.inits += 1
        if not self.session_ready and self.family is not None:
            self.session_ready = True
            self.family.observe()
            self.infrastructure = set(self.family.members())
        if isinstance(event.get("session_id"), str):
            self.session_id = event["session_id"]
        if self.inits > 1:
            # Each turn of a duplex session starts with its own init; its result ends it.
            self.awaiting_result = True
            return
        tools = event.get("tools")
        problems = []
        if not isinstance(tools, list) or not tools:
            problems.append("the CLI initialized without native tools")
        if event.get("permissionMode") != "auto":
            problems.append(f"permissionMode is {event.get('permissionMode')!r}, not 'auto'")
        if self.interactive and (not isinstance(tools, list) or "AskUserQuestion" not in tools):
            problems.append("AskUserQuestion is not available, so Claude cannot ask Codex during the run")
        if problems:
            self.capability_error = "; ".join(problems)

    def _control_request(self, event: dict[str, Any]) -> None:
        request_id = str(event.get("request_id") or "")
        request = _dict(event.get("request"))
        if request.get("subtype") != "can_use_tool":
            self._respond(request_id, error=f"control request {request.get('subtype')!r} is not supported by the orchestrator host")
            activity(self.run_dir, "provider", "unsupported control request answered with an error",
                     status=str(request.get("subtype")))
            return
        tool = str(request.get("tool_name") or "")
        tool_input = _dict(request.get("input"))
        question = tool == "AskUserQuestion"
        if self.input_closed:
            # Input already ended (a background task woke a new turn after the last result); no answer can be written.
            activity(self.run_dir, "question" if question else "permission",
                     "arrived after Claude's input was closed; it cannot be answered in this run", tool=tool, status="unanswerable")
            return
        if not self.interactive:
            message = ("This run has no live Codex channel. Record the question in result.json questions and continue with "
                       "work that does not depend on it." if question else
                       "This run has no live Codex channel, so this permission escalation cannot be granted.")
            self._respond(request_id, {"behavior": "deny", "message": message})
            activity(self.run_dir, "question" if question else "permission", "declined: the run is not interactive", tool=tool)
            return
        if question:
            body = {"questions": tool_input.get("questions") if isinstance(tool_input.get("questions"), list) else []}
        else:
            target = next((tool_input[key] for key in SUMMARY_KEYS if isinstance(tool_input.get(key), str)), "")
            body = {"tool_name": tool, "summary": _short(target), "input": tool_input}
        record = self.store.open_interaction(self.run_id, "question" if question else "permission", blocking=True,
                                             request=body, request_id=request_id, tool_use_id=event.get("tool_use_id")
                                             or request.get("tool_use_id"))
        self.requests[record["interaction_id"]] = {"request_id": request_id, "tool": tool, "input": tool_input,
                                                   "tool_use_id": request.get("tool_use_id"), "record": record}
        self.by_request[request_id] = record["interaction_id"]
        first = "；".join(questions_of(body)) if question else f"{tool} {body.get('summary', '')}"
        activity(self.run_dir, "question" if question else "permission",
                 "Claude asked Codex" if question else "Claude asked for permission", text=_short(first, 200),
                 status="pending")
        self._begin_hold(record["interaction_id"])

    def _cancelled(self, request_id: str) -> None:
        interaction_id = self.by_request.pop(request_id, None)
        if interaction_id is None:
            return
        self.requests.pop(interaction_id, None)
        if self.stop_requested:
            # The execution is being stopped for this answer; the question stays pending for the resumed session.
            return
        self.store.cancel(interaction_id, "the CLI withdrew the request")
        activity(self.run_dir, "question", "the CLI withdrew a pending request (for example after an interrupt)",
                 status="cancelled")
        self._maybe_release("cancelled")

    def _control_response(self, response: dict[str, Any]) -> None:
        request_id = str(response.get("request_id"))
        if request_id in self.shell_stops:
            task_id = self.shell_stops.pop(request_id)
            if response.get("subtype") != "success":
                # The CLI keeps the shell; input still closes and the run deadline bounds the CLI's wait.
                self.tasks.pop(task_id, None)
                activity(self.run_dir, "turn", "the CLI refused to stop a background command", status="stop_refused", text=task_id)
            return
        if request_id in self.stop_tasks:
            task_id = self.stop_tasks.pop(request_id)
            if response.get("subtype") != "success":
                self.stop_requested = True
                activity(self.run_dir, "hold", "the CLI refused to stop a background task; stopping the execution",
                         status="stop_task_refused", text=task_id)
            return
        command_id = self.interrupts.pop(request_id, None)
        if command_id and request_id in self.bare_interrupts and command_id == self.latest_command:
            if response.get("subtype") == "success":
                # Only the latest bare interrupt can end the current work.
                self.finalized = True
            else:
                # This control request has no user-message header: Claude never saw its command id.
                activity(self.run_dir, "interrupt", "the CLI refused the interrupt; the previous input remains current",
                         status="refused", text=_short(response.get("error") or "interrupt refused"))
        self.bare_interrupts.discard(request_id)
        if command_id and command_id not in self.update_texts.values():
            self.in_flight.pop(command_id, None)
            self.store.mark_delivery(self.run_id, command_id, "echoed",
                                     "the CLI acknowledged the interrupt request; this does not prove its processes stopped")

    def expect_resume(self, text: str, command_ids: list[str]) -> Any:
        """The resumed session's first message carries these commands: written when it is, echoed when replayed."""
        self.resume_texts[text] = command_ids
        self.carried.update(command_ids)

        def done(complete: bool, written: int) -> None:
            for command_id in command_ids:
                if complete:
                    self.store.redeliver(self.run_id, command_id, "written", "sent in the resumed session's first message")
                elif written:
                    self.store.mark_delivery(self.run_id, command_id, "failed",
                                             f"the resumed session's input closed after {written} byte(s)")
        return done

    def _user(self, event: dict[str, Any]) -> None:
        content = _dict(event.get("message")).get("content")
        if event.get("isReplay") and isinstance(content, str) and content in self.resume_texts:
            for command_id in self.resume_texts.pop(content):
                self.store.redeliver(self.run_id, command_id, "echoed", "the CLI replayed the resumed session's first message")
        if event.get("isReplay") and isinstance(content, str) and content in self.update_texts:
            command_id = self.update_texts.pop(content)
            self.in_flight.pop(command_id, None)
            self.store.mark_delivery(self.run_id, command_id, "echoed", "the CLI replayed the message into the session")
            # The CLI took the message as input: a turn for it is underway until its result, even if idle was last seen.
            self.awaiting_result = True
            self.idle_stale = self.idle_stale or self.session_state == "idle"
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    answered = self.answer_tools.pop(str(block.get("tool_use_id")), None)
                    if answered:
                        self.in_flight.pop(answered, None)
                        self.store.mark_delivery(self.run_id, answered, "echoed",
                                                 "the CLI returned the answer as the tool result")

    def _begin_hold(self, interaction_id: str) -> None:
        self.holding.add(interaction_id)
        held = self._hold_members()
        self.store.update_hold(interaction_id, {"mode": "family_hold", "cli_blocked_on_request": True,
                                                "held_processes": held, "stopped_tasks": [],
                                                "background_shells": len(self.tasks) - len(self.deferring_tasks()),
                                                "started_at": time.time()})
        activity(self.run_dir, "hold", "execution paused until Codex answers", status="family_hold")
        self._stop_deferring(interaction_id)

    def _stop_deferring(self, interaction_id: str) -> None:
        """Ask the CLI to stop every in-process background agent or workflow; record what was asked."""
        stopped = []
        for task_id in self.deferring_tasks():
            request_id = f"stop-{task_id}-{interaction_id}"
            if task_id not in self.stop_tasks.values() and self._send(
                    {"type": "control_request", "request_id": request_id, "request": {"subtype": "stop_task", "task_id": task_id}}):
                self.stop_tasks[request_id] = task_id
                stopped.append(f"{self.tasks[task_id][0]}: {self.tasks[task_id][1]}")
        if not stopped:
            return
        self.stop_deadline = time.monotonic() + STOP_TASK_CONFIRM_SECONDS
        record = self.store.interaction(interaction_id) or {}
        hold = dict(record.get("hold") or {})
        hold["stopped_tasks"] = list(hold.get("stopped_tasks") or []) + stopped
        self.store.update_hold(interaction_id, hold)
        activity(self.run_dir, "hold", "stopping background agents and workflows with the CLI's stop_task",
                 status="stop_task", text="; ".join(stopped)[:400])

    def _hold_members(self) -> int:
        if self.family is None:
            return len(self.held)
        self.family.observe()
        for pid, identity in self.family.members().items():
            if pid != self.cli_pid and pid not in self.infrastructure and pid not in self.held \
                    and signal_identity(identity, signal.SIGSTOP):
                self.held[pid] = identity
        return len(self.held)

    def release(self, reason: str) -> None:
        if not self.held and not self.holding:
            return
        for identity in self.held.values():
            signal_identity(identity, signal.SIGCONT)
        self._end_hold(reason, released_processes=len(self.held))
        activity(self.run_dir, "released", "execution resumed", status=reason)

    def discard_held(self, reason: str) -> None:
        """The execution is being stopped: held processes are killed while still stopped, never resumed first.

        Resuming them before the CLI's group is gone would let background work write again for as long as the
        CLI takes to exit; SIGKILL reaches a stopped process.  The family's own reclamation still follows.
        """
        if not self.held and not self.holding:
            return
        killed = sum(1 for identity in self.held.values() if signal_identity(identity, signal.SIGKILL))
        self._end_hold(reason, released_processes=0, killed_processes=killed)
        activity(self.run_dir, "hold", "held background processes were stopped without resuming them", status=reason)

    def _end_hold(self, reason: str, **counts: int) -> None:
        self.held.clear()
        for interaction_id in sorted(self.holding):
            record = self.store.interaction(interaction_id)
            hold = dict((record or {}).get("hold") or {})
            hold.update(released_at=time.time(), release_reason=reason, **counts)
            self.store.update_hold(interaction_id, hold)
        self.holding.clear()

    def _maybe_release(self, reason: str) -> None:
        if self.holding and not self.store.pending_blocking(self.run_id):
            self.release(reason)

    def pump(self) -> None:
        """Read new notes, keep a hold complete, and write queued commands while a process is attached."""
        self._read_notes()
        if self.holding and time.monotonic() >= self.next_hold_scan:
            self._hold_members()
            self.next_hold_scan = time.monotonic() + .5
        if self.stop_tasks and time.monotonic() > self.stop_deadline:
            # The CLI never confirmed stopping in-process background work; only stopping the execution is certain.
            self.stop_requested = True
        if self.stop_requested and not self.store.pending_blocking(self.run_id):
            # Every question was answered on the live session first: nothing is waiting, so nothing needs stopping.
            self.stop_requested = False
            self.stop_tasks.clear()
        if self.transport is None or self.input_closed:
            return
        for command in self.store.queued(self.run_id):
            if command["command_id"] not in self.sending | self.carried and command["command_id"] not in self.in_flight:
                self._deliver(command)
        self._maybe_release("answered")
        self._maybe_close()

    def _written(self, command_id: str, *, echo: bool) -> Any:
        def done(complete: bool, written: int) -> None:
            self.sending.discard(command_id)
            if complete:
                if echo:
                    self.in_flight[command_id] = "written"
                self.store.mark_delivery(self.run_id, command_id, "written")
            elif written:
                self.store.mark_delivery(self.run_id, command_id, "failed",
                                         f"the CLI input closed after {written} byte(s) of this command were written")
        return done

    def _deliver(self, command: dict[str, Any]) -> None:
        command_id, payload, origin = command["command_id"], command["payload"], command["origin"]
        if command["kind"] == "answer":
            self._read_notes_before_answer()
        # New input reopens the work Claude declared finished; only a final naming the newest message counts again.
        self._input_taken()
        self.latest_command = command_id
        if command["kind"] == "update" or (command["kind"] == "interrupt" and str(payload.get("text") or "").strip()):
            self.latest_input = command_id
        if command["kind"] == "update":
            text = update_text(command_id, origin, payload["text"])
            self.sending.add(command_id)
            if self._send({"type": "user", "message": {"role": "user", "content": text}}, self._written(command_id, echo=True)):
                self.update_texts[text] = command_id
                self.awaiting_result = True
                activity(self.run_dir, "update", "Codex sent an update", status=origin, text=str(payload["text"]))
            else:
                self.sending.discard(command_id)
            return
        if command["kind"] == "interrupt":
            request_id = "interrupt-" + command_id
            raw_follow = str(payload.get("text") or "")
            follow = raw_follow if raw_follow.strip() else ""
            frames = [{"type": "control_request", "request_id": request_id, "request": {"subtype": "interrupt"}}]
            if not follow:
                self.bare_interrupts.add(request_id)
            if follow:
                # The redirect is the interrupted session's next message; its replay is the delivery evidence.
                text = interrupt_text(command_id, origin, follow)
                frames.append({"type": "user", "message": {"role": "user", "content": text}})
                self.update_texts[text] = command_id
                self.awaiting_result = True
            self.sending.add(command_id)
            self.interrupts[request_id] = command_id
            ok = self._send(frames[0], None if follow else self._written(command_id, echo=True))
            if ok and follow:
                ok = self._send(frames[1], self._written(command_id, echo=True))
            if ok:
                activity(self.run_dir, "interrupt", "Codex asked Claude to stop the current step", status=origin,
                         text=follow or None)
            else:
                self.sending.discard(command_id)
                if follow:
                    # Never sent whole: no replay can come, so it must not keep a turn open.
                    self.update_texts.pop(text, None)
            return
        self._deliver_answer(command, command_id, payload, origin)

    def _deliver_answer(self, command: dict[str, Any], command_id: str, payload: dict[str, Any], origin: str) -> None:
        interaction_id = command["interaction_id"]
        live = self.requests.get(interaction_id)
        if live is None:
            # No process waits for it (stopped execution); the bridge delivers it when the session resumes.
            return
        label = f"[{ORIGIN_LABELS[origin]}] "
        entries = answer_entries(live["record"], payload)
        if live["tool"] == "AskUserQuestion":
            updated = dict(live["input"])
            updated["answers"] = {question: label + answer for question, answer in entries if question}
            response = {"behavior": "allow", "updatedInput": updated}
        elif payload.get("decision") == "allow":
            response = {"behavior": "allow", "updatedInput": live["input"]}
        else:
            response = {"behavior": "deny", "message": label + str(payload.get("text") or "Codex declined this request")}
        self.sending.add(command_id)
        if self._respond(live["request_id"], response, on_written=self._written(command_id, echo=bool(live.get("tool_use_id")))):
            self.requests.pop(interaction_id, None)
            self.by_request.pop(live["request_id"], None)
            if live.get("tool_use_id"):
                self.answer_tools[str(live["tool_use_id"])] = command_id
            activity(self.run_dir, "answer", "Codex answered Claude", status=origin)
        else:
            self.sending.discard(command_id)

    def _maybe_close(self) -> None:
        if self.results < 1 or self.sending or self.in_flight or self.requests or self.interrupts or self.stop_tasks:
            return
        if not self.transport.flushed:
            return
        if self.session_state is None:
            if self.awaiting_result or self.deferring_tasks() or (self.tasks and not self.finalized):
                return
        elif self.session_state != "idle" or self.idle_stale:
            return
        for task_id in self.deferring_tasks():
            # The CLI reported idle, so it has no in-process work left; this task's terminal event was never seen.
            self.tasks.pop(task_id, None)
        if self.store.pending_blocking(self.run_id):
            return
        if self.shell_stops:
            return
        if self.tasks and not self.finalized:
            # A background shell still running: CLI idle does not wait for it, but its end wakes the turn that
            # reads its output.  Stopping it here would drop the result Claude is waiting for.
            if not self.background_wait_reported:
                activity(self.run_dir, "turn", "Claude is idle while background commands are still running; waiting for their results",
                         status="waiting_background")
                self.background_wait_reported = True
            return
        shells = [task_id for task_id in self.tasks if task_id not in self.stopping_shells]
        if shells:
            for task_id in shells:
                request_id = f"stop-shell-{task_id}"
                if self._send({"type": "control_request", "request_id": request_id,
                               "request": {"subtype": "stop_task", "task_id": task_id}}):
                    self.shell_stops[request_id] = task_id
                    self.stopping_shells.add(task_id)
                    self.shells_stopped.append(self.tasks[task_id][1] or task_id)
            activity(self.run_dir, "turn", "stopping background commands Claude declared leftovers before closing its input",
                     status="stop_task", text="; ".join(self.shells_stopped[-len(shells):])[:400])
            return
        if self.store.set_state(self.run_id, "closing", expect=("running",)):
            self.input_closed = True
            self.transport.finish_input()
            activity(self.run_dir, "turn", "no further input is pending; closing Claude's input", status="input_closed")

    def _notes_chunk(self) -> bytes:
        """Read new bytes of the notes file without blocking; only a regular file is ever read."""
        assert self.notes_path is not None
        try:
            fd = os.open(self.notes_path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
        except FileNotFoundError:
            return b""
        except OSError as exc:
            self._note_problem(f"notes file cannot be opened ({type(exc).__name__}); notes are ignored until it can")
            return b""
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                self._note_problem("notes path is not a regular file; notes are ignored until it is")
                return b""
            identity = (info.st_dev, info.st_ino)
            if identity != self.notes_identity or info.st_size < self.notes_offset:
                # Replaced or truncated: read the new content from its start.
                self.notes_identity, self.notes_offset = identity, 0
            os.lseek(fd, self.notes_offset, os.SEEK_SET)
            return os.read(fd, NOTES_READ_LIMIT)
        finally:
            os.close(fd)

    def _note_problem(self, problem: str) -> None:
        if problem != self.notes_problem:
            self.notes_problem = problem
            activity(self.run_dir, "note", problem, status="invalid")

    def _read_notes(self) -> None:
        if self.notes_path is None:
            return
        data = self._notes_chunk()
        complete = data[:data.rfind(b"\n") + 1]
        self.notes_offset += len(complete)
        for raw in complete.splitlines():
            if raw.strip():
                self._note(raw)

    def _read_notes_before_answer(self) -> None:
        """Read the existing backlog before unblocking the CLI; an old final must not survive in a later chunk."""
        if self.notes_path is None:
            return
        try:
            info = self.notes_path.stat(follow_symlinks=False)
        except OSError:
            return
        if not stat.S_ISREG(info.st_mode):
            return
        # The CLI is blocked on its control request and its other processes are held.  Bound this drain to the
        # bytes already present, retaining the per-read memory limit and stopping on a partial line or read error.
        while self.notes_offset < info.st_size:
            previous = self.notes_offset
            self._read_notes()
            if self.notes_offset <= previous:
                break

    def _note(self, raw: bytes) -> None:
        try:
            note = json.loads(raw.decode("utf-8"))
            text = note.get("text") if isinstance(note, dict) else None
            if isinstance(text, str):
                text.encode("utf-8")
        except (ValueError, UnicodeError):
            activity(self.run_dir, "note", "ignored a notes line that is not valid UTF-8 JSON", status="invalid")
            return
        if isinstance(note, dict) and note.get("type") == FINAL_TYPE:
            if note.get("after") != self.latest_input:
                activity(self.run_dir, "note", "ignored a final line that does not name Codex's latest message",
                         status="stale", text=_short(note.get("after"), 80))
                return
            self.finalized = True
            activity(self.run_dir, "note", "Claude declared its work final; background commands still running are leftovers",
                     status="final")
            return
        if not isinstance(note, dict) or note.get("type") not in NOTE_TYPES or not isinstance(text, str) or not text.strip():
            activity(self.run_dir, "note", "ignored a notes line without type note|dispute and text", status="invalid")
            return
        scope = note.get("scope") if isinstance(note.get("scope"), str) else None
        self.store.open_interaction(self.run_id, note["type"], blocking=False,
                                    request={"text": text[:NOTE_LIMIT], "scope": scope[:300] if scope else None})
        activity(self.run_dir, note["type"], "Claude left a note" if note["type"] == "note" else "Claude raised a dispute",
                 text=_short(text, 200), status="acknowledged")

    def detach(self) -> None:
        """The process is gone: nothing waits for these control responses any more; durable questions stay pending.

        Messages written but never replayed stay in the store as written; a resumed session carries them again.
        """
        self.discard_held("process_stopped")
        self.transport = None
        self.update_texts.clear()
        self.requests.clear()
        self.by_request.clear()
        self.interrupts.clear()
        self.stop_tasks.clear()
        self.in_flight.clear()
        self.sending.clear()
