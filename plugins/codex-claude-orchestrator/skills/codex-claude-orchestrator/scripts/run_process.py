"""Nonblocking prompt/output transport owned by one supervised child."""
from __future__ import annotations

import json
import os
import selectors
from typing import Any

from events import append as append_activity


class ProcessStream:
    """Keep input delivery and output draining on the same bounded event loop."""

    def __init__(self, proc, stream, payload: memoryview, run_dir, workflow: bool):
        self.proc, self.stream, self.payload = proc, stream, payload
        self.run_dir, self.workflow = run_dir, workflow
        self.pending = b""
        self.eof = False
        self.delivery: dict[str, Any] = {"state": "sending", "bytes": len(payload), "written_bytes": 0}
        self.selector = selectors.DefaultSelector()
        assert proc.stdin and proc.stdout
        os.set_blocking(proc.stdout.fileno(), False)
        os.set_blocking(proc.stdin.fileno(), False)
        self.selector.register(proc.stdout, selectors.EVENT_READ)
        self.selector.register(proc.stdin, selectors.EVENT_WRITE)

    def close_input(self, outcome: str, error: BaseException | None = None) -> None:
        if self.delivery["state"] != "sending":
            return
        self.delivery["state"] = outcome
        if error is not None:
            self.delivery["error"] = f"{type(error).__name__}: {error}"
        try:
            self.selector.unregister(self.proc.stdin)
        except (KeyError, ValueError):
            pass
        try:
            self.proc.stdin.close()
        except OSError:
            pass

    def _send(self) -> None:
        if self.delivery["state"] != "sending":
            return
        written = self.delivery["written_bytes"]
        try:
            written += os.write(self.proc.stdin.fileno(), self.payload[written:written + 65536])
        except BlockingIOError:
            return
        except OSError as exc:
            self.close_input("incomplete", exc)
            return
        self.delivery["written_bytes"] = written
        if written >= len(self.payload):
            self.close_input("complete")

    def _record_line(self, line: bytes) -> None:
        decoded = line.decode("utf-8", "replace")
        self.stream.write(decoded + "\n")
        self.stream.flush()
        try:
            item = json.loads(decoded)
        except json.JSONDecodeError:
            return
        if not isinstance(item, dict) or item.get("type") not in {"system", "result"}:
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
