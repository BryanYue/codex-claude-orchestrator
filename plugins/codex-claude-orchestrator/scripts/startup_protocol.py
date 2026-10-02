"""Bridge startup handoff shared by Runtime and the bridge process it spawns.

Runtime persists a ``runtime_pre_spawn`` lifecycle record before it starts a
bridge.  A bridge implementing this protocol takes that record over only when
its nonce, run binding, protocol version and code identity all match, and it
must do so before it can publish a launch intent.  A bridge without this
protocol rejects the extra ``--startup-nonce`` argument while parsing, so it
can neither take the record over nor dispatch Claude.

The record says nothing about a bridge that may still be alive; callers must
still prove the bridge process group stopped and hold the lane exclusively.
"""
from __future__ import annotations

import hashlib
import os
import time
from pathlib import Path
from typing import Any

import identity_manifest

STARTUP_PROTOCOL_VERSION = 1
PRE_SPAWN_PHASE = "runtime_pre_spawn"
NONCE_ARGUMENT = "--startup-nonce"

# Current installation declaration, retained for callers copying the code set.
CODE_FILES = identity_manifest.paths(Path(__file__).resolve().parents[1], "startup")
_LOADED: dict[str, dict[str, Any]] = {}


class StartupError(RuntimeError):
    pass


def plugin_root(bridge_script: Path) -> Path:
    return Path(bridge_script).parents[3]


def code_identity(root: Path) -> dict[str, Any]:
    """Hash the executable modules by content; paths and inodes are not part of it."""
    root = Path(root)
    files: dict[str, str | None] = {}
    errors: list[str] = []
    try:
        names = identity_manifest.paths(root, "startup")
    except (OSError, ValueError) as exc:
        names = ()
        errors.append(f"{identity_manifest.MANIFEST_FILE}: {type(exc).__name__}: {exc}")
    for relative in names:
        try:
            files[relative] = hashlib.sha256((root / relative).read_bytes()).hexdigest()
        except OSError as exc:
            files[relative] = None
            errors.append(f"{relative}: {type(exc).__name__}")
    value = None if errors else identity_manifest.digest(files, "startup")

    identity: dict[str, Any] = {"protocol_version": STARTUP_PROTOCOL_VERSION, "algorithm": "sha256",
                                "value": value, "files": files, "root": str(root)}
    if errors:
        identity["error"] = "; ".join(errors)
    return identity


def loaded_identity(root: Path) -> dict[str, Any]:
    """Return the identity first observed for root in this process.

    Python keeps no source bytes after import, so the hash taken at import time
    is the only witness of the code this process actually loaded.  Call it from
    module import (Runtime) or first thing in the entry point (bridge run).
    """
    key = str(root)
    if key not in _LOADED:
        _LOADED[key] = code_identity(root)
    return _LOADED[key]


def mismatch(loaded: Any, current: Any) -> str | None:
    """Explain why two identities are not interchangeable; None when they are."""
    if not isinstance(loaded, dict) or not isinstance(loaded.get("value"), str):
        detail = loaded.get("error") if isinstance(loaded, dict) else None
        return "loaded plugin code identity is unavailable" + (f" ({detail})" if detail else "")
    if not isinstance(current, dict) or not isinstance(current.get("value"), str):
        detail = current.get("error") if isinstance(current, dict) else None
        return "plugin code on disk cannot be read" + (f" ({detail})" if detail else "")
    if loaded.get("protocol_version") != current.get("protocol_version"):
        return "bridge startup protocol version differs from the loaded code"
    if loaded["value"] != current["value"]:
        old, new = loaded.get("files") or {}, current.get("files") or {}
        changed = sorted(name for name in set(old) | set(new) if old.get(name) != new.get(name))
        return "plugin code on disk differs from the loaded code: " + (", ".join(changed[:6]) or "identity value")
    return None


def require_unchanged(loaded: Any, root: Path) -> dict[str, Any]:
    current = code_identity(root)
    problem = mismatch(loaded, current)
    if problem:
        raise StartupError(problem + "; restart or reconnect the plugin MCP server before dispatching")
    return current


def pre_spawn_record(*, run_id: str, task_id: str, revision: int, cwd: str, packet_sha256: str,
                     cli_descriptor_sha256: str | None, lane_identity: str, nonce: str,
                     code_value: str) -> dict[str, Any]:
    # child_started is None, not False: a reader that predates this phase must
    # treat the record as unconfirmed rather than as pre-dispatch proof.
    now = time.time()
    return {"schema_version": 1, "phase": PRE_SPAWN_PHASE, "author": "runtime", "status": "starting",
            "child_started": None, "terminal": False, "run_id": run_id, "task_id": task_id,
            "revision": revision, "cwd": cwd, "packet_sha256": packet_sha256,
            "cli_descriptor_sha256": cli_descriptor_sha256, "lane_identity": lane_identity,
            "startup_nonce": nonce, "startup_protocol_version": STARTUP_PROTOCOL_VERSION,
            "code_identity": code_value, "recorded_at": now, "updated_at": now}


def verify_handoff(record: Any, *, nonce: Any, run_id: str, packet_sha256: str,
                   cli_descriptor_sha256: str | None, code: dict[str, Any]) -> dict[str, Any]:
    """Validate the pre-spawn record this bridge is about to take over."""
    if not isinstance(nonce, str) or len(nonce) < 32 or any(c not in "0123456789abcdef" for c in nonce):
        raise StartupError("startup nonce is missing or malformed")
    if not isinstance(record, dict) or record.get("phase") != PRE_SPAWN_PHASE or record.get("author") != "runtime":
        raise StartupError("runtime pre-spawn record is missing, corrupt or already taken over")
    if record.get("startup_protocol_version") != STARTUP_PROTOCOL_VERSION:
        raise StartupError("runtime pre-spawn record uses an incompatible startup protocol")
    if record.get("startup_nonce") != nonce:
        raise StartupError("runtime pre-spawn record nonce does not match this bridge invocation")
    expected = {"run_id": run_id, "packet_sha256": packet_sha256, "cli_descriptor_sha256": cli_descriptor_sha256}
    wrong = sorted(key for key, value in expected.items() if record.get(key) != value)
    if wrong:
        raise StartupError("runtime pre-spawn record is bound to different inputs: " + ", ".join(wrong))
    if not isinstance(code.get("value"), str) or record.get("code_identity") != code["value"]:
        raise StartupError("bridge code differs from the code Runtime verified before spawn; Claude was not launched")
    return record


def bound_pre_spawn(lifecycle: Any, record: dict[str, Any]) -> bool:
    """Whether lifecycle is still exactly the pre-spawn record this run registered."""
    nonce = record.get("startup_nonce")
    code_value = record.get("bridge_code_identity")
    return (isinstance(lifecycle, dict) and lifecycle.get("phase") == PRE_SPAWN_PHASE
            and lifecycle.get("author") == "runtime" and lifecycle.get("terminal") is False
            and lifecycle.get("child_started") is None
            and lifecycle.get("startup_protocol_version") == STARTUP_PROTOCOL_VERSION
            and record.get("startup_protocol_version") == STARTUP_PROTOCOL_VERSION
            and isinstance(nonce, str) and lifecycle.get("startup_nonce") == nonce
            and isinstance(code_value, str) and lifecycle.get("code_identity") == code_value
            and isinstance(record.get("lane_identity"), str)
            and lifecycle.get("lane_identity") == record["lane_identity"])


def has_startup_binding(record: dict[str, Any]) -> bool:
    return any(key in record for key in ("startup_nonce", "startup_protocol_version", "bridge_code_identity"))


def nonce_bound(lifecycle: Any, record: dict[str, Any]) -> bool:
    """New records keep every startup binding after the bridge takes over."""
    if not has_startup_binding(record):
        return True
    nonce = record.get("startup_nonce")
    code = record.get("bridge_code_identity")
    return (isinstance(nonce, str) and len(nonce) >= 32 and all(c in "0123456789abcdef" for c in nonce)
            and isinstance(code, str) and len(code) == 64 and all(c in "0123456789abcdef" for c in code)
            and isinstance(lifecycle, dict) and lifecycle.get("startup_nonce") == nonce
            and record.get("startup_protocol_version") == STARTUP_PROTOCOL_VERSION
            and lifecycle.get("startup_protocol_version") == STARTUP_PROTOCOL_VERSION
            and lifecycle.get("code_identity") == code
            and isinstance(record.get("lane_identity"), str)
            and lifecycle.get("lane_identity") == record["lane_identity"])


def startup_readiness(loaded: Any, bridge_script: Path, spawn_cwd: Path | None = None) -> dict[str, Any]:
    """Report whether a bridge can be started; never includes task text or credentials."""
    bridge_script = Path(bridge_script)
    try:
        os.getcwd()
        process_cwd: dict[str, Any] = {"state": "available"}
    except OSError as exc:
        process_cwd = {"state": "unavailable", "reason": type(exc).__name__}
    process_cwd["note"] = "bridge processes start in the Runtime state directory and do not inherit this directory"
    if spawn_cwd is None:
        spawn: dict[str, Any] = {"state": "not_configured"}
    else:
        spawn = {"path": str(spawn_cwd), "state": "available" if Path(spawn_cwd).is_dir() else "unavailable"}
    current = code_identity(plugin_root(bridge_script))
    problem = mismatch(loaded, current)
    present = bridge_script.is_file()
    ready = problem is None and present and spawn["state"] != "unavailable"
    return {"ready": ready, "protocol_version": STARTUP_PROTOCOL_VERSION,
            "bridge_script": {"path": str(bridge_script), "present": present},
            "process_cwd": process_cwd, "spawn_cwd": spawn,
            "code_identity": {"loaded": loaded.get("value") if isinstance(loaded, dict) else None,
                              "disk": current.get("value"), "matches": problem is None, "reason": problem,
                              "scope": "executable bridge/Runtime modules by content; guides excluded"},
            "scope": "bridge process startability only; Claude CLI installation and login are reported separately"}
