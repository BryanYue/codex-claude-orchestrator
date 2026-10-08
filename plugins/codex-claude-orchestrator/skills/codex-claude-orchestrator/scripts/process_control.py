"""Process-group termination and identity-checked liveness.

A recorded identity is ``{pid, birth}``; a PID whose birth differs is a reused
PID, not the recorded process.  Failure to inspect is never treated as proof
that a process stopped.
"""
from __future__ import annotations

import os
import signal
import subprocess
import time
from typing import Any

import process_family

TERMINATION_GRACE_SECONDS = 3


def identity(pid: int) -> dict[str, Any] | None:
    try:
        found = process_family._identity(pid)
    except process_family.InspectionError:
        return None
    if found is None:
        return None
    return {"pid": pid, "birth": list(found.birth)}


def presence(recorded: Any) -> str:
    """Return running, stopped or unknown for a recorded identity."""
    if (not isinstance(recorded, dict) or not isinstance(recorded.get("pid"), int) or recorded["pid"] <= 0
            or not recorded.get("birth")):
        return "unknown"
    try:
        found = process_family._identity(recorded["pid"])
    except process_family.InspectionError:
        return "unknown"
    if found is None or found.zombie or list(found.birth) != list(recorded.get("birth") or []):
        return "stopped"
    return "running"


def group_absent(group_id: int) -> bool:
    """Confirm absence via a readable full process list; denied or partial output is not absence."""
    try:
        result = subprocess.run(["ps", "-axo", "pid=,pgid="], text=True, capture_output=True, timeout=3)
        if result.returncode != 0:
            return False
        rows = [tuple(map(int, line.split())) for line in result.stdout.splitlines() if line.strip()]
        if any(len(row) != 2 for row in rows) or not any(pid == os.getpid() for pid, _ in rows):
            return False
        return not any(group == group_id for _, group in rows)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return False


def group_stopped(group_id: int) -> bool:
    try:
        os.killpg(group_id, 0)
    except ProcessLookupError:
        return True
    except OSError:
        return group_absent(group_id)
    return False


def wait_group_absent(group_id: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while True:
        if group_stopped(group_id):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(.05)


def terminate_group(proc: subprocess.Popen[Any]) -> str | None:
    """Stop a child's whole process group; return a diagnostic when the stop cannot be confirmed."""
    # Reap an exited direct child first: on macOS an unreaped leader can make
    # killpg() report EPERM for an otherwise empty group.
    if proc.poll() is not None and group_stopped(proc.pid):
        return None
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return None
    except OSError:
        proc.poll()
    deadline = time.monotonic() + TERMINATION_GRACE_SECONDS
    while time.monotonic() < deadline:
        proc.poll()
        if group_stopped(proc.pid):
            return None
        time.sleep(.05)
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        return None
    except OSError as exc:
        return f"SIGKILL process-group request failed: {type(exc).__name__}: {exc}"
    try:
        proc.wait(timeout=TERMINATION_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        return "direct child did not terminate before the cleanup deadline"
    return None if wait_group_absent(proc.pid, TERMINATION_GRACE_SECONDS) else "process group remained after SIGKILL"


def terminate_recorded_group(recorded: Any) -> str:
    """Stop a group by its recorded leader identity when this process is not its parent."""
    state = presence(recorded)
    if state == "unknown":
        return state
    group = recorded["pid"]
    if state == "stopped":
        # Leftover members cannot be told apart from a reused group id here;
        # the caller's process-family marker covers descendants.
        return "stopped" if group_stopped(group) else "unknown"
    for sig, wait in ((signal.SIGTERM, TERMINATION_GRACE_SECONDS), (signal.SIGKILL, TERMINATION_GRACE_SECONDS)):
        if presence(recorded) != "running":
            break
        try:
            os.killpg(group, sig)
        except ProcessLookupError:
            break
        except OSError:
            return "unknown"
        wait_group_absent(group, wait)
    return "stopped" if group_stopped(group) else "unknown"
