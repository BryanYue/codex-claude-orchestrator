"""Nonblocking duplex stream-json transport owned by one supervised child."""
from __future__ import annotations

import json
import os
import selectors
from typing import Any, Callable

from events import append as append_activity
from stream_parser import tool_steps


class ProcessStream:
    """Keep input delivery and output draining on the same bounded event loop.

    The first payload is the brief.  With ``keep_open`` the child's stdin stays
    open afterwards so later lines (answers, updates, control requests) can be
    written; ``close_input`` ends the conversation.  A frame's ``on_written``
    callback runs with True once its last byte was accepted by the pipe, or with
    False and the number of its bytes written when the input closed first.
    """

    def __init__(self, proc: Any, stream: Any, payload: memoryview, run_dir: Any, workflow: bool, root: str | None = None,
                 *, keep_open: bool = False, on_event: Callable[[dict[str, Any]], None] | None = None,
                 on_payload_written: Callable[[bool, int], None] | None = None):
        self.proc, self.stream, self.payload = proc, stream, payload
        self.run_dir, self.workflow = run_dir, workflow
        # Claude may report the resolved path (/private/var/...) of a cwd given as /var/...
        self.roots = tuple(dict.fromkeys((root, os.path.realpath(root)))) if root else ()
        self.keep_open, self.on_event = keep_open, on_event
        self.pending = b""
        self.outgoing = bytearray(payload)
        self.sent_total = 0
        # (end offset in the whole input stream, frame length, callback) for frames not yet fully written.
        self.frames: list[tuple[int, int, Callable[[bool, int], None]]] = []
        if on_payload_written is not None:
            self.frames.append((len(payload), len(payload), on_payload_written))
        self.eof = False
        self.delivery: dict[str, Any] = {"state": "sending", "bytes": len(payload), "written_bytes": 0}
        self.input_state = "open"
        self.closing = False
        self.selector = selectors.DefaultSelector()
        assert proc.stdin and proc.stdout
        os.set_blocking(proc.stdout.fileno(), False)
        os.set_blocking(proc.stdin.fileno(), False)
        self.selector.register(proc.stdout, selectors.EVENT_READ)
        self.selector.register(proc.stdin, selectors.EVENT_WRITE)
        self._writing = True

    def close_input(self, outcome: str, error: BaseException | None = None) -> None:
        if self.delivery["state"] == "sending":
            self.delivery["state"] = outcome
            if error is not None:
                self.delivery["error"] = f"{type(error).__name__}: {error}"
        if self.input_state == "closed":
            return
        self.input_state = "closed"
        if self._writing:
            try:
                self.selector.unregister(self.proc.stdin)
            except (KeyError, ValueError):
                pass
            self._writing = False
        try:
            self.proc.stdin.close()
        except OSError:
            pass
        frames, self.frames = self.frames, []
        for end, length, callback in frames:
            callback(False, max(0, length - (end - self.sent_total)))

    def finish_input(self) -> None:
        """Close stdin once everything queued has been written."""
        self.closing = True
        if not self.outgoing:
            self.close_input("complete")

    def send(self, line: bytes, on_written: Callable[[bool, int], None] | None = None) -> bool:
        """Queue one JSON line; False when the input is already closed.  Queued is not written."""
        if self.input_state == "closed" or self.closing:
            return False
        frame = line if line.endswith(b"\n") else line + b"\n"
        self.outgoing += frame
        if on_written is not None:
            self.frames.append((self.sent_total + len(self.outgoing), len(frame), on_written))
        if not self._writing:
            self.selector.register(self.proc.stdin, selectors.EVENT_WRITE)
            self._writing = True
        return True

    @property
    def flushed(self) -> bool:
        return not self.outgoing

    def _send(self) -> None:
        if self.input_state == "closed":
            return
        try:
            written = os.write(self.proc.stdin.fileno(), bytes(self.outgoing[:65536]))
        except BlockingIOError:
            return
        except OSError as exc:
            self.close_input("incomplete", exc)
            return
        del self.outgoing[:written]
        self.sent_total += written
        while self.frames and self.frames[0][0] <= self.sent_total:
            _, length, callback = self.frames.pop(0)
            callback(True, length)
        if self.delivery["state"] == "sending":
            self.delivery["written_bytes"] = min(len(self.payload), self.delivery["written_bytes"] + written)
            if self.delivery["written_bytes"] >= len(self.payload):
                self.delivery["state"] = "complete"
        if not self.outgoing:
            if self.closing or not self.keep_open:
                self.close_input("complete")
            else:
                self.selector.unregister(self.proc.stdin)
                self._writing = False

    def _record_line(self, line: bytes) -> None:
        decoded = line.decode("utf-8", "replace")
        self.stream.write(decoded + "\n")
        self.stream.flush()
        try:
            item = json.loads(decoded)
        except json.JSONDecodeError:
            return
        if not isinstance(item, dict):
            return
        if self.on_event is not None:
            self.on_event(item)
        if item.get("type") == "assistant":
            for step in tool_steps(item, self.roots):
                append_activity(self.run_dir, "tool", step["tool"], tool=step["tool"], file=step.get("file"),
                                text=step.get("text"), tool_use_id=step.get("tool_use_id"))
            return
        if item.get("type") not in {"system", "result"}:
            return
        subtype = item.get("subtype")
        if self.workflow and subtype in {"task_started", "task_progress", "task_notification"}:
            append_activity(self.run_dir, "workflow", str(item.get("description") or item.get("summary") or "workflow activity"),
                            status=str(item.get("status") or subtype))
            for progress in item.get("workflow_progress", []):
                if isinstance(progress, dict) and progress.get("type") == "workflow_agent":
                    append_activity(self.run_dir, "workflow_agent", str(progress.get("label") or "workflow agent"),
                                    tool=progress.get("lastToolName"), status=progress.get("state"), text=progress.get("phaseTitle"))
        else:
            append_activity(self.run_dir, "provider", "provider lifecycle event", status=str(subtype or item.get("type")))

    def drain(self, wait: float) -> None:
        for key, _ in self.selector.select(wait):
            if key.fileobj is self.proc.stdin:
                self._send()
                continue
            if self.eof:
                continue
            chunk = os.read(self.proc.stdout.fileno(), 65536)
            if not chunk:
                self.eof = True
                self.selector.unregister(self.proc.stdout)
                continue
            self.pending += chunk
            while b"\n" in self.pending:
                line, self.pending = self.pending.split(b"\n", 1)
                self._record_line(line)

    def finish(self) -> None:
        if self.pending:
            self.stream.write(self.pending.decode("utf-8", "replace"))
            self.stream.flush()
            self.pending = b""
        self.selector.close()
        self.proc.stdout.close()
