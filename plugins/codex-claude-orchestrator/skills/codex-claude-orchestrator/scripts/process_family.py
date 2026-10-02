"""Bounded supervision of marker-visible descendants, including detached sessions.

This is supplementary process supervision, not OS containment. A descendant
that deliberately removes its inherited marker before observation can evade it.
Darwin signals have no atomic PID/birth binding; identity is rechecked immediately
before each signal. Unrelated processes are never selected by ancestry alone.
"""
from __future__ import annotations

import ctypes
from dataclasses import dataclass
from functools import lru_cache
import os
from pathlib import Path
import secrets
import signal
import sys
import time
from typing import Any


ENVIRONMENT_KEY = "CODEX_CLAUDE_PROCESS_FAMILY"
LIMITATION = ("Covers marker-visible and previously observed process identities; deliberate marker removal before "
              "first observation and inaccessible unidentified processes are outside this evidence. Darwin PID "
              "identity is rechecked before signals but the OS exposes no atomic PID/birth signal binding.")


class InspectionError(RuntimeError):
    """The process table cannot supply required ownership evidence."""


@dataclass(frozen=True)
class Identity:
    pid: int
    birth: tuple[int, int]
    uid: int
    zombie: bool = False


class _BsdInfo(ctypes.Structure):
    _fields_ = [(name, ctypes.c_uint32) for name in (
        "flags", "status", "xstatus", "pid", "ppid", "uid", "gid", "ruid", "rgid", "svuid", "svgid", "reserved")]
    _fields_ += [("comm", ctypes.c_char * 16), ("name", ctypes.c_char * 32)]
    _fields_ += [(name, ctypes.c_uint32) for name in ("nfiles", "pgid", "jobc", "tdev", "tpgid", "nice")]
    _fields_ += [("start_seconds", ctypes.c_uint64), ("start_microseconds", ctypes.c_uint64)]


@lru_cache(maxsize=1)
def _darwin_libraries():
    proc = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
    proc.proc_pidinfo.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int]
    proc.proc_pidinfo.restype = ctypes.c_int
    proc.proc_listallpids.argtypes = [ctypes.c_void_p, ctypes.c_int]
    proc.proc_listallpids.restype = ctypes.c_int
    libc = ctypes.CDLL(None, use_errno=True)
    libc.sysctl.argtypes = [ctypes.POINTER(ctypes.c_int), ctypes.c_uint, ctypes.c_void_p,
                           ctypes.POINTER(ctypes.c_size_t), ctypes.c_void_p, ctypes.c_size_t]
    libc.sysctl.restype = ctypes.c_int
    return proc, libc


def _identity(pid: int) -> Identity | None:
    if sys.platform == "darwin":
        info = _BsdInfo()
        proc, _ = _darwin_libraries()
        count = proc.proc_pidinfo(pid, 3, 0, ctypes.byref(info), ctypes.sizeof(info))
        if count != ctypes.sizeof(info):
            if ctypes.get_errno() in {2, 3}:
                return None
            raise InspectionError("identity_unavailable")
        return Identity(pid, (info.start_seconds, info.start_microseconds), info.uid, info.status == 5)
    if sys.platform.startswith("linux"):
        try:
            folder = Path("/proc") / str(pid)
            raw = (folder / "stat").read_text()
            fields = raw[raw.rfind(")") + 2:].split()
            return Identity(pid, (int(fields[19]), 0), folder.stat().st_uid, fields[0] == "Z")
        except FileNotFoundError:
            return None
        except (OSError, ValueError, IndexError) as exc:
            raise InspectionError("identity_unavailable") from exc
    raise InspectionError("platform_unsupported")


def _pids() -> list[int]:
    if sys.platform == "darwin":
        proc, _ = _darwin_libraries()
        count = proc.proc_listallpids(None, 0)
        if not 0 < count <= 100_000:
            raise InspectionError("process_table_unavailable")
        buffer = (ctypes.c_int * (count + 1024))()
        found = proc.proc_listallpids(buffer, ctypes.sizeof(buffer))
        if not 0 < found < len(buffer):
            raise InspectionError("process_table_unavailable")
        return [pid for pid in buffer[:found] if pid > 0]
    if sys.platform.startswith("linux"):
        try:
            return [int(entry.name) for entry in Path("/proc").iterdir() if entry.name.isdecimal()]
        except OSError as exc:
            raise InspectionError("process_table_unavailable") from exc
    raise InspectionError("platform_unsupported")


def _environment(pid: int) -> list[bytes]:
    if sys.platform == "darwin":
        _, libc = _darwin_libraries()
        argmax = ctypes.c_int()
        argmax_size = ctypes.c_size_t(ctypes.sizeof(argmax))
        argmax_mib = (ctypes.c_int * 2)(1, 8)  # CTL_KERN, KERN_ARGMAX
        if libc.sysctl(argmax_mib, 2, ctypes.byref(argmax), ctypes.byref(argmax_size), None, 0) != 0 or not 0 < argmax.value <= 2 * 1024 * 1024:
            raise InspectionError("environment_unavailable")
        mib = (ctypes.c_int * 3)(1, 49, pid)  # CTL_KERN, KERN_PROCARGS2
        data = ctypes.create_string_buffer(argmax.value)
        size = ctypes.c_size_t(len(data))
        if libc.sysctl(mib, 3, data, ctypes.byref(size), None, 0) != 0:
            if ctypes.get_errno() in {2, 3}:
                return []
            raise InspectionError("environment_unavailable")
        raw = data.raw[:size.value]
        if len(raw) < 4:
            raise InspectionError("environment_unavailable")
        argc = int.from_bytes(raw[:4], sys.byteorder, signed=True)
        cursor = raw.find(b"\0", 4)
        if cursor < 0 or argc < 0:
            raise InspectionError("environment_unavailable")
        while cursor < len(raw) and raw[cursor] == 0:
            cursor += 1
        for _ in range(argc):
            cursor = raw.find(b"\0", cursor)
            if cursor < 0:
                raise InspectionError("environment_unavailable")
            cursor += 1
        return raw[cursor:].split(b"\0")
    if sys.platform.startswith("linux"):
        try:
            return (Path("/proc") / str(pid) / "environ").read_bytes().split(b"\0")
        except FileNotFoundError:
            return []
        except OSError as exc:
            raise InspectionError("environment_unavailable") from exc
    raise InspectionError("platform_unsupported")


class Family:
    """One run's inherited marker and identity-checked stopping evidence."""
    def __init__(self, marker: str | None = None):
        self._marker = secrets.token_hex(24) if marker is None else marker
        if not isinstance(self._marker, str) or not self._marker or "\0" in self._marker:
            raise ValueError("invalid process family marker")
        self.environment = {ENVIRONMENT_KEY: self._marker}
        self._entry = f"{ENVIRONMENT_KEY}={self._marker}".encode()
        self._tracked: dict[int, Identity] = {}
        self._live: dict[int, Identity] = {}
        self._baseline: set[Identity] = set()
        self._initial_blockers: list[str] = []
        # Reopening a persisted marker must inspect existing processes: they
        # can be the very detached writers a later cleanup needs to reconcile.
        if marker is not None:
            return
        try:
            for pid in _pids():
                try:
                    identity = _identity(pid)
                    if identity is not None:
                        self._baseline.add(identity)
                except InspectionError:
                    continue
        except InspectionError as exc:
            self._initial_blockers.append(str(exc))

    def observe(self) -> dict[str, Any]:
        blockers = list(self._initial_blockers)
        inaccessible = 0
        live: dict[int, Identity] = {}
        try:
            pids = _pids()
        except InspectionError as exc:
            self._live = dict(self._tracked)
            return self._result("unknown", blockers + [str(exc)], inaccessible)
        for pid in pids:
            try:
                identity = _identity(pid)
                if identity is None or identity.zombie or identity.uid != os.getuid():
                    continue
                previous = self._tracked.get(pid)
                if previous is not None and previous.birth == identity.birth:
                    live[pid] = identity
                    continue
                if identity in self._baseline:
                    continue
                if self._entry in _environment(pid):
                    # Reading environment and identity is not atomic: recheck
                    # the birth token before adopting this particular process.
                    current = _identity(pid)
                    if current == identity:
                        self._tracked[pid] = identity
                        live[pid] = identity
            except InspectionError:
                inaccessible += 1
                if pid in self._tracked:
                    blockers.append("tracked_identity_unavailable")
                    live[pid] = self._tracked[pid]
        self._live = live
        return self._result("unknown" if blockers else "observed", blockers, inaccessible)

    def _result(self, state: str, blockers: list[str], inaccessible: int) -> dict[str, Any]:
        return {"state": state, "tracked_count": len(self._tracked), "live_count": len(self._live),
                "remaining_pids": sorted(self._live), "blockers": sorted(set(blockers)),
                "inspection_unavailable_count": inaccessible, "limitation": LIMITATION}

    def _signal(self, sig: int) -> None:
        for pid, expected in list(self._live.items()):
            try:
                current = _identity(pid)
                if current is not None and current.birth == expected.birth and current.uid == expected.uid and not current.zombie:
                    os.kill(pid, sig)
            except (InspectionError, ProcessLookupError, PermissionError):
                # observe() supplies the final evidence; failure never means stopped.
                continue

    def finish(self, grace_seconds: float = .5) -> dict[str, Any]:
        if isinstance(grace_seconds, bool) or not isinstance(grace_seconds, (float, int)) or not 0 <= grace_seconds <= 3:
            raise ValueError("grace_seconds must be 0..3")
        result = self.observe()
        self._signal(signal.SIGTERM)
        deadline = time.monotonic() + grace_seconds
        while self._live and time.monotonic() < deadline:
            time.sleep(.05)
            result = self.observe()
        if self._live:
            self._signal(signal.SIGKILL)
            deadline = time.monotonic() + .5
            while self._live and time.monotonic() < deadline:
                time.sleep(.05)
                result = self.observe()
        if result["state"] != "unknown":
            result["state"] = "unknown" if self._live else "stopped"
        return result
