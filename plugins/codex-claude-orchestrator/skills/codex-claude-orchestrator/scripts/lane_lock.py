"""Persistent lane coordination, with locks retained for older installations.

The durable inode survives temporary-directory cleanup. Keeping the legacy
lock as well coordinates with versions that only know the temporary path.
An old process holding an already-unlinked legacy inode cannot be retroactively
protected; both descriptors must be inherited by every new bridge process.
"""
from __future__ import annotations

import fcntl
import hashlib
import os
import tempfile
from pathlib import Path
from typing import TextIO


def lane_key(identity: str) -> str:
    return hashlib.sha256(identity.encode()).hexdigest()


def coordination_root() -> Path:
    override = os.environ.get("CODEX_CLAUDE_COORDINATION_ROOT")
    root = Path(override).expanduser() if override else Path.home() / ".codex" / "claude-orchestrator-locks"
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    return root


def legacy_lock_path(identity: str) -> Path:
    root = Path(tempfile.gettempdir()) / "codex-claude-cwd-locks"
    root.mkdir(mode=0o700, exist_ok=True)
    return root / (lane_key(identity) + ".lock")


def durable_lock_path(identity: str) -> Path:
    return coordination_root() / (lane_key(identity) + ".lock")


def marker_roots() -> tuple[Path, Path]:
    durable = coordination_root() / "unknown"
    legacy = Path(tempfile.gettempdir()) / "codex-claude-cwd-unknown"
    for root in (durable, legacy):
        root.mkdir(mode=0o700, exist_ok=True)
    return durable, legacy


class LaneLock:
    """Own both open file descriptions; closing a duplicate never unlocks peers."""

    def __init__(self, durable: TextIO, legacy: TextIO) -> None:
        self.durable = durable
        self.legacy = legacy

    @property
    def closed(self) -> bool:
        return self.durable.closed and self.legacy.closed

    def fileno(self) -> int:
        # Preserve the legacy descriptor API for older launchers and callers.
        return self.legacy.fileno()

    def filenos(self) -> tuple[int, int]:
        return self.durable.fileno(), self.legacy.fileno()

    def inherited_environment(self) -> dict[str, str]:
        return {"CODEX_CLAUDE_DURABLE_LANE_FD": str(self.durable.fileno()),
                "CODEX_CLAUDE_LANE_FD": str(self.legacy.fileno())}

    def close(self) -> None:
        self.legacy.close()
        self.durable.close()


def _open_locked(path: Path, inherited: str | None) -> TextIO:
    if inherited is not None:
        if not inherited.isdigit():
            raise OSError("inherited lane descriptor is not a nonnegative integer")
        expected, received = path.stat(), os.fstat(int(inherited))
        if (received.st_dev, received.st_ino) != (expected.st_dev, expected.st_ino):
            raise OSError("inherited lane lock does not match this cwd's worktree lane")
        handle = os.fdopen(os.dup(int(inherited)), "a+")
    else:
        handle = path.open("a+")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BaseException:
        handle.close()
        raise
    return handle


def acquire(identity: str, *, inherit: bool = False) -> LaneLock:
    # Every installation acquires in this order to avoid inter-version deadlocks.
    durable = _open_locked(durable_lock_path(identity),
                           os.environ.get("CODEX_CLAUDE_DURABLE_LANE_FD") if inherit else None)
    try:
        legacy = _open_locked(legacy_lock_path(identity),
                              os.environ.get("CODEX_CLAUDE_LANE_FD") if inherit else None)
    except BaseException:
        durable.close()
        raise
    return LaneLock(durable, legacy)
