#!/usr/bin/env python3
"""Small supervised bridge for the Claude CLI.

This is an invocation boundary, not an OS sandbox.  It deliberately has no
daemon, database, background retry, Codex handoff, or automatic workflow.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import selectors
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unicodedata
import uuid
from pathlib import Path
from typing import Any, Mapping

# Plugin-level discovery is shared with diagnostics, including desktop PATH.
PLUGIN_SCRIPTS = Path(__file__).resolve().parents[3] / "scripts"
if PLUGIN_SCRIPTS.is_dir():
    sys.path.insert(0, str(PLUGIN_SCRIPTS))
from executable_locator import locate_claude, cli_environment

from events import append as append_activity
from compatibility import REQUIRED_FLAGS, SUPPORTED_CLI_PROFILES, GROUPS, bridge_contract_id, required_groups, resolved_profile
from compatibility import DISPATCH_PROTOCOL_VERSION, LEGACY_DISPATCH_CONTRACTS
from workspace import WorkspaceError, artifact_snapshot, artifact_snapshot_difference, canonical_read_path, snapshot_digest, validate_artifact_lists

try:
    import fcntl
except ImportError:  # pragma: no cover - supported target is POSIX
    fcntl = None

WRITE_TOOLS = ("Edit", "Write")
READ_TOOLS = ("Read", "Glob", "Grep")
DISALLOWED = ("Bash", "Agent", "Task", "TeamCreate", "NotebookEdit", "Skill", "Workflow")
FINAL = {"completed", "blocked", "failed", "cancelled", "timeout", "unknown"}
class BridgeError(RuntimeError):
    pass


def activity(run_dir: Path, kind: str, summary: str, **fields: Any) -> None:
    event = append_activity(run_dir, kind, summary, **fields)
    # The terminal stream remains compact and deliberately omits any model text.
    print(f"activity[{event['seq']}] {event['kind']}: {event['summary']}", flush=True)


def dump(path: Path, value: Any) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def require_absolute(value: str, label: str) -> Path:
    p = Path(value)
    if not p.is_absolute():
        raise BridgeError(f"{label} must be absolute")
    return p


def unsafe_relative_path(value: str) -> bool:
    parts = Path(value).parts
    return not value or "\\" in value or Path(value).is_absolute() or ".." in parts or Path(value).as_posix() != value


def bad_owned_path(value: str) -> bool:
    parts = tuple(unicodedata.normalize("NFC", part).casefold() for part in Path(value).parts)
    return unsafe_relative_path(value) or ".git" in parts or any("oracle" in x.lower() for x in parts) or any(x in {".claude", ".codex"} for x in parts)


def path_identity(value: str) -> str:
    # APFS commonly folds case and Unicode decomposition. Be conservative on
    # case-sensitive volumes too: a portable packet must not alias protection.
    return unicodedata.normalize("NFC", value).casefold()


def aliases_protected(target: Path, protected: list[Path]) -> bool:
    for other in protected:
        if path_identity(str(target)) == path_identity(str(other)):
            return True
        try:
            if target.exists() and other.exists() and target.samefile(other):
                return True
        except OSError as exc:
            raise BridgeError(f"cannot establish protected file identity: {other}") from exc
    return False


def validate_packet(packet: Any) -> dict[str, Any]:
    if isinstance(packet, dict) and any(key in packet for key in ("cli_descriptor", "cli_selection", "cli_identity", "qualification")):
        raise BridgeError("CLI identity is selected by the supervisor, not by task packet fields")
    if not isinstance(packet, dict):
        raise BridgeError("packet must be an object")
    required = ("task_id", "revision", "role", "cwd", "objective", "requirement_sources",
                "constraints", "acceptance", "protected_files", "model", "effort")
    for key in required:
        if key not in packet:
            raise BridgeError(f"packet missing {key}")
    if not isinstance(packet["task_id"], str) or not packet["task_id"]:
        raise BridgeError("task_id must be a non-empty string")
    if not isinstance(packet["revision"], int) or isinstance(packet["revision"], bool) or packet["revision"] < 1:
        raise BridgeError("revision must be a positive integer")
    if packet["role"] not in ("review", "implement", "workflow_review"):
        raise BridgeError("role must be review, implement or workflow_review")
    raw_cwd = require_absolute(packet["cwd"], "cwd")
    workspace_kind = packet.get("workspace_kind", "git")
    if workspace_kind not in {"git", "artifacts"}:
        raise BridgeError("workspace_kind must be git or artifacts")
    if workspace_kind == "artifacts":
        try:
            # Validate the lexical root before resolve erases custom symlinks.
            cwd = canonical_read_path(str(raw_cwd), raw_cwd, "artifact workspace root")
        except (WorkspaceError, OSError) as exc:
            raise BridgeError(str(exc)) from exc
    else:
        cwd = raw_cwd.resolve()
    if not cwd.is_dir():
        raise BridgeError("cwd must be an existing directory")
    if not isinstance(packet["objective"], str) or not packet["objective"].strip():
        raise BridgeError("objective must be a non-empty string")
    for key in ("requirement_sources", "constraints", "acceptance", "protected_files"):
        if not isinstance(packet[key], list) or not all(isinstance(x, str) for x in packet[key]):
            raise BridgeError(f"{key} must be an array of strings")
    canonical_sources = []
    for source in packet["requirement_sources"]:
        p = require_absolute(source, "requirement source")
        if workspace_kind == "artifacts":
            # Keep the caller's lexical absolute path until workspace.py checks
            # every ancestor.  Resolving here would erase a custom symlink from
            # the evidence path before the artifacts policy can reject it.
            canonical_sources.append(str(p))
            continue
        if not p.is_file():
            raise BridgeError(f"requirement source is not a file: {source}")
        canonical_sources.append(str(p.resolve()))
    owned = packet.get("owned_files", [])
    if not isinstance(owned, list) or not all(isinstance(x, str) for x in owned):
        raise BridgeError("owned_files must be an array of strings")
    if packet["role"] == "implement" and not owned:
        raise BridgeError("implement packet requires owned_files")
    if packet["role"] in ("review", "workflow_review") and owned:
        raise BridgeError("review packet cannot own files")
    for key in ("owned_files", "protected_files"):
        for item in packet.get(key, []):
            if (key == "owned_files" and bad_owned_path(item)) or (key == "protected_files" and unsafe_relative_path(item)):
                raise BridgeError(f"unsafe {key} path: {item}")
    if len({path_identity(path) for path in owned}) != len(owned):
        raise BridgeError("owned_files contains case or Unicode aliases")
    if {path_identity(path) for path in owned} & {path_identity(path) for path in packet["protected_files"]}:
        raise BridgeError("owned_files overlaps protected_files")
    protected_targets = [cwd / path for path in packet["protected_files"]] + [Path(path) for path in canonical_sources]
    if any(aliases_protected(cwd / path, protected_targets) for path in owned):
        raise BridgeError("owned_files aliases a protected file or requirement source")
    for source in packet["requirement_sources"]:
        source_path = Path(source).resolve()
        try:
            relative_source = source_path.relative_to(cwd).as_posix()
        except ValueError:
            continue
        if path_identity(relative_source) in {path_identity(path) for path in owned}:
            raise BridgeError("requirement_sources cannot be owned_files")
    if (not isinstance(packet["model"], str) or not packet["model"].strip()
            or not isinstance(packet["effort"], str) or not packet["effort"].strip()):
        raise BridgeError("model and effort must be non-empty strings")
    budget = packet.get("budget", {})
    if not isinstance(budget, dict) or any(key not in {"max_turns", "max_budget_usd"} for key in budget):
        raise BridgeError("budget may contain only max_turns and max_budget_usd")
    if "max_turns" in budget and (not isinstance(budget["max_turns"], int) or isinstance(budget["max_turns"], bool) or budget["max_turns"] < 1):
        raise BridgeError("budget.max_turns must be a positive integer")
    if "max_budget_usd" in budget and (not isinstance(budget["max_budget_usd"], (int, float)) or isinstance(budget["max_budget_usd"], bool) or not math.isfinite(budget["max_budget_usd"]) or budget["max_budget_usd"] <= 0):
        raise BridgeError("budget.max_budget_usd must be a positive number")
    correction = packet.get("correction")
    if correction is not None:
        if not isinstance(correction, dict) or correction.get("kind") not in {"local", "structural", "direction", "infrastructure"}:
            raise BridgeError("invalid correction")
        if not isinstance(correction.get("finding_id"), str) or not isinstance(correction.get("reason"), str):
            raise BridgeError("correction needs finding_id and reason")
        if not isinstance(correction.get("attempt"), int) or correction["attempt"] < 1:
            raise BridgeError("correction attempt must be positive")
    baseline = packet.get("baseline_commit")
    if baseline is not None and (not isinstance(baseline, str) or len(baseline) != 40 or any(c not in "0123456789abcdef" for c in baseline)):
        raise BridgeError("baseline_commit must be a lowercase full 40-character SHA")
    packet = dict(packet)
    packet["cwd"] = str(cwd)
    packet["requirement_sources"] = canonical_sources
    packet["owned_files"] = list(owned)
    packet["protected_files"] = list(packet["protected_files"])
    packet["workspace_kind"] = workspace_kind
    packet["budget"] = budget
    if workspace_kind == "artifacts":
        if packet["role"] != "review":
            raise BridgeError("artifacts workspace supports review only; implement, resume and workflow are unsupported")
        git_probe = subprocess.run(["git", "-C", str(cwd), "rev-parse", "--is-inside-work-tree"],
                                   text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        if git_probe.returncode == 0 and git_probe.stdout.strip() == "true":
            raise BridgeError("artifacts workspace must be a non-Git directory; use workspace_kind=git for a repository or worktree")
        if packet.get("baseline_commit") is not None:
            raise BridgeError("artifacts workspace does not accept baseline_commit")
        if packet.get("protocol_binding") is not None:
            raise BridgeError("artifacts workspace does not support protocol_binding")
        try:
            inputs, sources = validate_artifact_lists(packet)
        except WorkspaceError as exc:
            raise BridgeError(str(exc)) from exc
        packet["input_files"] = inputs
        packet["requirement_sources"] = sources
    elif "input_files" in packet:
        # Preserve the old Git packet contract rather than silently making an
        # accidental file list look supervised.
        raise BridgeError("input_files is supported only for workspace_kind=artifacts")
    workflow = packet.get("workflow")
    if packet["role"] == "workflow_review":
        if packet.get("correction") is not None:
            raise BridgeError("workflow_review is fresh-only and cannot carry a correction")
        try:
            from named_workflow import NamedWorkflowError, validate as validate_named_workflow
            workflow = validate_named_workflow(workflow, cwd)
        except (NamedWorkflowError, OSError, subprocess.CalledProcessError) as exc:
            raise BridgeError(f"saved workflow identity check failed: {exc}") from exc
        packet["workflow"] = workflow
        # Saved scripts are protected exactly as requirement sources are: their
        # identity is checked before dispatch, after dispatch, and on any future
        # preflight.  The explicit packet hash is independently verified above.
        packet["requirement_sources"] = list(dict.fromkeys(packet["requirement_sources"] + [workflow["path"]]))
    elif workflow is not None:
        raise BridgeError("workflow is permitted only for workflow_review")
    if packet.get("protocol_binding") is not None:
        from protocol import validate_binding
        try:
            packet = validate_binding(packet)
        except (ValueError, OSError, subprocess.CalledProcessError) as exc:
            raise BridgeError(f"Protocol identity check failed: {exc}") from exc
        if any(aliases_protected(cwd / owned_path, [Path(source) for source in packet["requirement_sources"]]) for owned_path in owned):
            raise BridgeError("protocol/delta cannot be owned_files or alias an owned file")
    return packet


def git(cwd: Path, *args: str) -> str:
    p = subprocess.run(["git", "-C", str(cwd), *args], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if p.returncode:
        raise BridgeError(f"git {' '.join(args)} failed: {p.stderr.strip()}")
    return p.stdout


def git_head(cwd: Path) -> str | None:
    """Return HEAD when it exists, while still rejecting a non-Git cwd.

    A freshly initialized repository has a valid worktree but no commit yet.
    That condition is useful for a read-only supervised review and must remain
    distinguishable from an invalid repository; ``None`` is therefore part of
    the persisted snapshot rather than a fabricated SHA.
    """
    git(cwd, "rev-parse", "--is-inside-work-tree")
    p = subprocess.run(["git", "-C", str(cwd), "rev-parse", "--verify", "-q", "HEAD"],
                       text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if p.returncode == 1:
        return None
    if p.returncode:
        raise BridgeError(f"git rev-parse HEAD failed: {p.stderr.strip()}")
    return p.stdout.strip()


def git_status_entries(cwd: Path) -> list[tuple[str, str, str | None]]:
    p = subprocess.run(["git", "-C", str(cwd), "status", "--porcelain=v1", "-z", "-uall"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if p.returncode:
        raise BridgeError(f"git status failed: {p.stderr.decode(errors='replace').strip()}")
    fields = p.stdout.split(b"\0")
    entries: list[tuple[str, str, str | None]] = []
    i = 0
    while i < len(fields) - 1:
        field = fields[i]
        if len(field) < 3:
            raise BridgeError("malformed git porcelain status")
        xy = field[:2].decode("ascii")
        path = field[3:].decode("utf-8", "surrogateescape")
        original = None
        if "R" in xy or "C" in xy:
            i += 1
            if i >= len(fields) - 1:
                raise BridgeError("malformed git rename status")
            original = fields[i].decode("utf-8", "surrogateescape")
        entries.append((xy, path, original))
        i += 1
    return entries


def content_digest(path: Path) -> str:
    try:
        stat = path.lstat()
    except FileNotFoundError:
        return "missing"
    if path.is_symlink():
        return "symlink:" + hashlib.sha256(os.readlink(path).encode("utf-8", "surrogateescape")).hexdigest()
    if path.is_file():
        return sha256_file(path)
    return f"non-file:{stat.st_mode}:{stat.st_size}"


def git_snapshot(cwd: Path, packet: dict[str, Any]) -> dict[str, Any]:
    head = git_head(cwd)
    entries = git_status_entries(cwd)
    tracked_diff = git(cwd, "diff", "--no-ext-diff") + git(cwd, "diff", "--cached", "--no-ext-diff")
    hashes: dict[str, str] = {}
    for _, path, original in entries:
        hashes[path] = content_digest(cwd / path)
        if original is not None:
            hashes[original] = content_digest(cwd / original)
    guarded = {path: content_digest(cwd / path) for path in packet.get("owned_files", []) + packet.get("protected_files", [])}
    normalized = [{"xy": xy, "path": path, "original": original} for xy, path, original in entries]
    material = tracked_diff.encode() + json.dumps(normalized, ensure_ascii=False, sort_keys=True).encode() + json.dumps(hashes, ensure_ascii=False, sort_keys=True).encode()
    return {"head": head, "status_entries": normalized, "diff_hash": hashlib.sha256(material).hexdigest(), "dirty_content_hashes": hashes,
            "guarded_content_hashes": guarded, "ignored_files_not_enumerated": True}


def changed_paths(snapshot: dict[str, Any]) -> set[str]:
    paths: set[str] = set()
    for entry in snapshot["status_entries"]:
        paths.add(entry["path"])
        if entry.get("original") is not None:
            paths.add(entry["original"])
    return paths


def lane_lock_path(cwd: Path) -> Path:
    root = Path(tempfile.gettempdir()) / "codex-claude-cwd-locks"
    root.mkdir(mode=0o700, exist_ok=True)
    return root / (hashlib.sha256(str(cwd.resolve()).encode()).hexdigest() + ".lock")


def unknown_lane_marker(cwd: Path) -> Path:
    root = Path(tempfile.gettempdir()) / "codex-claude-cwd-unknown"
    root.mkdir(mode=0o700, exist_ok=True)
    return root / (hashlib.sha256(str(cwd.resolve()).encode()).hexdigest() + ".json")


def lock_file(cwd: Path, task_id: str):
    inherited = os.environ.get("CODEX_CLAUDE_LANE_FD")
    if inherited and inherited.isdigit():
        try:
            # Runtime already owns this cwd lock and intentionally passes a duplicate
            # into bridge so a Runtime crash cannot release it while bridge survives.
            expected = lane_lock_path(cwd).stat()
            received = os.fstat(int(inherited))
            if (received.st_dev, received.st_ino) != (expected.st_dev, expected.st_ino):
                raise BridgeError("inherited cwd lane lock does not match this cwd")
            handle = os.fdopen(os.dup(int(inherited)), "a+")
            if fcntl is not None:
                # A dup of Runtime's fd shares its lock; this also safely obtains
                # the lock for a direct, deliberately inherited invocation.
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return handle
        except OSError as exc:
            raise BridgeError(f"inherited cwd lane lock is unavailable: {exc}")
    f = lane_lock_path(cwd).open("a+")
    if fcntl is None:
        return f
    try:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        f.close()
        raise BridgeError("another active run already owns this cwd")
    return f


def state(run_dir: Path, status: str, **extra: Any) -> None:
    old = {}
    p = run_dir / "state.json"
    if p.exists():
        old = load(p)
    old.update({"status": status, "updated_at": time.time(), **extra})
    dump(p, old)


def pre_dispatch_cancelled(run_dir: Path, packet: dict[str, Any], note: str) -> int:
    state(run_dir, "cancelled", reason=note)
    dump(run_dir / "receipt.json", {"status": "cancelled", "task_id": packet["task_id"], "revision": packet["revision"],
                                     "note": "cancelled before Claude dispatch"})
    activity(run_dir, "cancelled", "cancelled before Claude dispatch", status="cancelled")
    return 1


def workspace_snapshot(packet: dict[str, Any]) -> dict[str, Any]:
    if packet["workspace_kind"] == "artifacts":
        try:
            return artifact_snapshot(packet)
        except WorkspaceError as exc:
            raise BridgeError(str(exc)) from exc
    snapshot = git_snapshot(Path(packet["cwd"]), packet)
    snapshot["workspace_digest"] = snapshot_digest(snapshot)
    return snapshot


def preflight(packet: dict[str, Any], resume: Path | None) -> tuple[dict[str, Any], dict[str, str]]:
    cwd = Path(packet["cwd"])
    before = workspace_snapshot(packet)
    if packet["workspace_kind"] == "git" and packet.get("baseline_commit") and before["head"] != packet["baseline_commit"]:
        raise BridgeError("git HEAD does not equal baseline_commit")
    requirements = {source: sha256_file(Path(source)) for source in packet["requirement_sources"]}
    if packet["workspace_kind"] == "git" and packet["role"] == "implement":
        if resume is None and before["status_entries"]:
            dirty_paths = [entry["path"] for entry in before["status_entries"]]
            raise BridgeError(f"first implement run requires a clean workspace; changed paths: {dirty_paths[:20]}")
        for owned in packet["owned_files"]:
            ignored = subprocess.run(["git", "-C", str(cwd), "check-ignore", "-q", "--", owned]).returncode == 0
            if ignored:
                raise BridgeError(f"owned file is ignored and cannot be supervised: {owned}")
    if resume is not None:
        if before != load(resume / "workspace_after.json"):
            raise BridgeError("resume workspace differs from prior run's after snapshot")
        if requirements != load(resume / "requirements.json"):
            raise BridgeError("resume requirement source paths or hashes changed")
    return before, requirements


def validate_resume(packet: dict[str, Any], previous: Path) -> str:
    prev_packet = validate_packet(load(previous / "packet.json"))
    prev_result = load(previous / "result.json")
    prev_state = load(previous / "state.json")
    frozen = ("task_id", "cwd", "role", "workspace_kind", "input_files", "model", "constraints", "acceptance", "owned_files", "protected_files", "baseline_commit", "protocol_binding", "project_workflow", "workflow", "budget")
    if any(packet.get(k) != prev_packet.get(k) for k in frozen):
        raise BridgeError("resume changed a frozen task contract field")
    if packet["revision"] <= prev_packet["revision"]:
        raise BridgeError("resume revision must strictly increase")
    if prev_state.get("status") != "completed" or prev_result.get("provider_subtype") != "success":
        raise BridgeError("resume requires a completed successful prior session")
    if (previous / "cancel.json").exists():
        raise BridgeError("resume is forbidden for a cancelled or invalidated prior session")
    correction = packet.get("correction")
    if not correction or correction["kind"] not in {"local", "infrastructure"}:
        raise BridgeError("only local or infrastructure correction may resume")
    sid = prev_result.get("actual_session_id")
    if not isinstance(sid, str) or not sid:
        raise BridgeError("prior run has no resumable session id")
    return sid


def _contained_path(raw: str, cwd: Path, sources: set[Path], allow_source: bool) -> tuple[Path, bool]:
    if not raw or "\x00" in raw or "$" in raw:
        raise BridgeError("empty, NUL, or variable-based paths are forbidden")
    value = Path(raw)
    if value.parts and value.parts[0].startswith("~"):
        raise BridgeError("tilde-based paths are forbidden")
    if ".." in value.parts:
        raise BridgeError("path traversal is forbidden")
    target = (value if value.is_absolute() else cwd / value)
    try:
        relative = target.relative_to(cwd)
        cursor = cwd
        for piece in relative.parts:
            cursor = cursor / piece
            if cursor.is_symlink():
                raise BridgeError("symlink path is forbidden")
        return target, True
    except ValueError:
        if allow_source and target in sources:
            if target.is_symlink():
                raise BridgeError("symlink requirement source is forbidden")
            return target, False
        raise BridgeError("path is outside cwd and requirement sources")


def _artifact_read_path(raw: str, cwd: Path, allowed: set[Path]) -> Path:
    value = Path(raw)
    if ".." in value.parts:
        raise BridgeError("path traversal is forbidden")
    try:
        canonical = canonical_read_path(raw, cwd)
    except WorkspaceError as exc:
        raise BridgeError(str(exc)) from exc
    if canonical not in allowed:
        raise BridgeError("artifact review may read only exact declared input_files and requirement_sources")
    return canonical


def _glob_base(pattern: str) -> str:
    first = min((i for i, char in enumerate(pattern) if char in "*?[{"), default=len(pattern))
    base = pattern[:first]
    if not base:
        return "."
    return base.rsplit("/", 1)[0] or ("/" if base.startswith("/") else ".")


def _glob_variants(pattern: str) -> list[str]:
    """Expand simple brace choices so every possible lexical base is checked.

    Claude Glob accepts brace choices.  Treat malformed/nested braces as
    ambiguous rather than trying to emulate a provider-specific glob parser.
    The bound keeps a generated pattern from turning guard validation into an
    unbounded expansion.
    """
    if not isinstance(pattern, str) or not pattern or "\x00" in pattern or "$" in pattern:
        raise BridgeError("Glob pattern must be a non-empty literal string")
    variants = [pattern]
    while any("{" in item or "}" in item for item in variants):
        expanded: list[str] = []
        for item in variants:
            left = item.find("{")
            right = item.find("}")
            if left < 0 or right < 0 or right < left or "{" in item[left + 1:right] or "}" in item[left + 1:right]:
                raise BridgeError("ambiguous Glob brace expression is forbidden")
            choices = item[left + 1:right].split(",")
            if len(choices) < 2 or any(not choice for choice in choices):
                raise BridgeError("ambiguous Glob brace expression is forbidden")
            expanded.extend(item[:left] + choice + item[right + 1:] for choice in choices)
            if len(expanded) > 64:
                raise BridgeError("Glob brace expansion is too large")
        variants = expanded
    if any(".." in Path(variant).parts for variant in variants):
        raise BridgeError("Glob pattern traversal is forbidden")
    return variants


def hook_reply_denied(reason: str) -> int:
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": reason}}))
    return 0


def hook_policy_preflight(cwd: Path, environment: Mapping[str, str],
                          managed_paths: list[Path] | None = None) -> dict[str, Any]:
    """Fail before provider dispatch when readable settings can disable this gate.

    The check never rewrites or suppresses existing settings/hooks.  Server or
    MDM policy that is not materialized as a readable file remains explicitly
    unconfirmed; the per-tool audit below must not be described as prevention.
    """
    disabled_env = [key for key in ("CLAUDE_CODE_SAFE_MODE", "CLAUDE_CODE_SIMPLE")
                    if environment.get(key) == "1"]
    if disabled_env:
        raise BridgeError("bridge hook guard is disabled by inherited environment: " + ", ".join(disabled_env))
    configured = environment.get("CLAUDE_CONFIG_DIR")
    home = environment.get("HOME")
    user_root = Path(configured).expanduser() if configured else (Path(home) / ".claude" if home else None)
    paths: list[tuple[str, Path]] = []
    if user_root is not None:
        paths.append(("user", user_root / "settings.json"))
    paths.extend((("project", cwd / ".claude" / "settings.json"),
                  ("project_local", cwd / ".claude" / "settings.local.json")))
    if managed_paths is None:
        managed_paths = [Path("/Library/Application Support/ClaudeCode/managed-settings.json"),
                         Path("/etc/claude-code/managed-settings.json")]
    paths.extend(("managed_file", path) for path in managed_paths)
    observed: list[dict[str, Any]] = []
    unsafe: list[str] = []
    for source, path in paths:
        if not path.exists():
            continue
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise BridgeError(f"cannot verify Claude hook policy in {source} settings: {type(exc).__name__}") from exc
        if not isinstance(value, dict):
            raise BridgeError(f"cannot verify Claude hook policy in {source} settings: root is not an object")
        flags = {key: value.get(key) for key in ("disableAllHooks", "allowManagedHooksOnly") if key in value}
        observed.append({"source": source, "path": str(path), "flags": flags})
        for key, flag in flags.items():
            if flag is True:
                unsafe.append(f"{source}:{key}=true")
            elif not isinstance(flag, bool):
                unsafe.append(f"{source}:{key} is not a boolean")
    if unsafe:
        raise BridgeError("bridge hook guard cannot be guaranteed by effective settings: " + "; ".join(unsafe))
    return {"status": "local_settings_checked", "settings": observed,
            "managed_policy_visibility": "local_files_only; server/MDM-only policy is not proven by this check"}


def hook_coverage(run_dir: Path, tool_uses: Mapping[str, str], *, guard_all_tools: bool = False) -> dict[str, Any]:
    audited: dict[str, str] = {}
    try:
        lines = (run_dir / "activity.jsonl").read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        lines = []
    for line in lines:
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if (isinstance(event, dict) and event.get("kind") == "tool" and event.get("status") == "allowed"
                and isinstance(event.get("tool_use_id"), str) and isinstance(event.get("tool"), str)):
            audited[event["tool_use_id"]] = event["tool"]
    # Ordinary runs install a path guard only for the file tools in their
    # matcher. StructuredOutput is the CLI's schema formatter and has no file
    # capability. Workflow uses matcher="*", so every observed tool (including
    # each child StructuredOutput) must have a corresponding hook event.
    guarded_names = set(READ_TOOLS + WRITE_TOOLS)
    expected = {tool_id: tool for tool_id, tool in tool_uses.items()
                if tool != "EndConversation" and (guard_all_tools or tool in guarded_names)}
    missing = sorted(tool_id for tool_id, tool in expected.items() if audited.get(tool_id) != tool)
    return {"status": "complete" if not missing else "incomplete", "expected_count": len(expected),
            "audited_count": sum(1 for tool_id, tool in expected.items() if audited.get(tool_id) == tool),
            "missing_tool_use_ids": missing}


def hook(packet_path: Path, cwd_arg: str) -> int:
    try:
        packet = validate_packet(load(packet_path))
        cwd = Path(cwd_arg).resolve()
        if cwd != Path(packet["cwd"]):
            raise BridgeError("hook cwd does not match packet")
        event = json.load(sys.stdin)
        name = event.get("tool_name") or event.get("toolName")
        input_ = event.get("tool_input") or event.get("toolInput") or {}
        tool_use_id = event.get("tool_use_id") or event.get("toolUseId")
        if not isinstance(tool_use_id, str) or not tool_use_id:
            raise BridgeError("PreToolUse event has no auditable tool_use_id")
        if name == "StructuredOutput" and packet["role"] == "workflow_review":
            # Workflow child agents may have their own output schema.  The
            # parent run's terminal structured result is still validated by
            # result_payload() after the provider process exits.
            append_activity(packet_path.parent, "tool", "workflow structured output permitted",
                            tool=name, status="allowed", tool_use_id=tool_use_id)
            return 0
        if name == "Workflow":
            # Claude Code's Workflow schema for a saved script accepts `name`
            # (and optionally args).  This adapter deliberately permits neither
            # inline scripts nor script paths: the packet binds a single
            # reviewed script identity and `Workflow(name)` is the CLI permission
            # grammar documented for headless saved workflows.
            expected = packet.get("workflow", {}).get("name")
            expected_input = {"name": expected}
            if "args" in packet.get("workflow", {}):
                expected_input["args"] = packet["workflow"]["args"]
            try:
                exact_input = json.dumps(input_, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")) == json.dumps(expected_input, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))
            except (TypeError, ValueError):
                exact_input = False
            if (packet.get("role") != "workflow_review" or not isinstance(input_, dict) or not exact_input):
                raise BridgeError("only the packet's exact saved Workflow(name) is permitted")
            append_activity(packet_path.parent, "tool", "saved workflow permitted", tool="Workflow", file=expected,
                            status="allowed", tool_use_id=tool_use_id)
            return 0
        raw = input_.get("file_path") or input_.get("path") or input_.get("query_path")
        sources = {Path(x).resolve() for x in packet["requirement_sources"]}
        artifact_allowed = sources | {Path(x).resolve() for x in packet.get("input_files", [])}
        if name in READ_TOOLS:
            if packet["workspace_kind"] == "artifacts":
                if not isinstance(raw, str) or not raw:
                    raise BridgeError("artifact read tool has no explicit file path")
                if name == "Glob" and any(mark in input_.get("pattern", "") for mark in "*?["):
                    raise BridgeError("artifact review does not permit wildcard discovery")
                _artifact_read_path(raw, cwd, artifact_allowed)
                append_activity(packet_path.parent, "tool", "artifact file permitted", tool=name, file=raw,
                                status="allowed", tool_use_id=tool_use_id)
                return 0
            # Glob has both path and pattern; validate both rather than trusting one.
            if name == "Glob":
                pattern = input_.get("pattern", "")
                explicit = raw if isinstance(raw, str) and raw else "."
                _contained_path(explicit, cwd, sources, True)
                checked: list[str] = []
                for variant in _glob_variants(pattern):
                    base = Path(_glob_base(variant))
                    candidate = str(base if base.is_absolute() else Path(explicit) / base)
                    _contained_path(candidate, cwd, sources, True)
                    checked.append(candidate)
                raw = checked[0]
            if name == "Grep" and not raw:
                raw = "."  # Claude Grep without a path searches its cwd.
            if not isinstance(raw, str) or not raw:
                raise BridgeError("read tool has no path")
            _contained_path(raw, cwd, sources, True)
            append_activity(packet_path.parent, "tool", "tool path permitted", tool=name, file=raw,
                            status="allowed", tool_use_id=tool_use_id)
            return 0
        if name not in WRITE_TOOLS:
            raise BridgeError("tool is not permitted by bridge packet")
        if not isinstance(raw, str) or not raw:
            raise BridgeError("write tool has no file path")
        target, inside = _contained_path(raw, cwd, sources, False)
        if not inside:  # defensive; write caller passed allow_source=False
            raise BridgeError("write path is outside cwd")
        relative = target.relative_to(cwd)
        rel = relative.as_posix()
        if aliases_protected(target, [cwd / path for path in packet["protected_files"]] + list(sources)):
            raise BridgeError("write path aliases a protected file or requirement source")
        if bad_owned_path(rel) or rel in set(packet["protected_files"]) or rel not in set(packet.get("owned_files", [])):
            raise BridgeError("write path is not an exact owned non-protected file")
        append_activity(packet_path.parent, "tool", "tool path permitted", tool=name, file=rel,
                        status="allowed", tool_use_id=tool_use_id)
        return 0
    except Exception as exc:
        append_activity(packet_path.parent, "tool", "tool path denied", tool=str(locals().get("name") or "unknown"),
                        status="denied", text=str(exc), tool_use_id=str(locals().get("tool_use_id") or "missing"))
        return hook_reply_denied(str(exc))


def prompt(packet: dict[str, Any]) -> str:
    # Corrections stay in the user prompt: resumed Claude sessions retain old system snapshots.
    instruction = "Return only the requested structured result. Do not claim acceptance."
    if packet["role"] == "workflow_review":
        instruction = (
            f"Run only the saved Workflow named /{packet['workflow']['name']} through the Workflow tool, "
            "then wait for its report before returning the structured result. Do not use an inline script, "
            "a scriptPath, any other Workflow, or a normal review as a substitute. Do not claim that the "
            "Workflow ran unless the tool call completed successfully. Do not claim acceptance."
        )
    evidence_rules = (
        "Ground findings in inspected source and distinguish confirmed facts from hypotheses. "
        "A search or Glob returning no matches is not proof a file is absent (hidden or ignored files may be omitted). "
        "Verify the exact path with permitted tools, or mark its existence unconfirmed; never read secrets to prove existence. "
        "For documented commands, trace actual defaults, generated artifact names and their consumers before asserting consistency. "
        "Do not claim checks were executed when you only inspected code."
    )
    return json.dumps({"packet": packet, "instruction": instruction, "evidence_rules": evidence_rules}, ensure_ascii=False)


def _stream_content_blocks(obj: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract only message content blocks, never tool definitions in init data."""
    message = obj.get("message")
    content = message.get("content") if isinstance(message, dict) else obj.get("content")
    if not isinstance(content, list):
        return []
    return [block for block in content if isinstance(block, dict)]


def _stream_texts(obj: dict[str, Any]) -> list[str]:
    """Return notification text only long enough to validate its stable tags."""
    values: list[Any] = [obj.get("content")]
    message = obj.get("message")
    if isinstance(message, dict):
        values.append(message.get("content"))
    texts: list[str] = []
    for value in values:
        if isinstance(value, str):
            texts.append(value)
        elif isinstance(value, list):
            texts.extend(item.get("text") for item in value if isinstance(item, dict) and isinstance(item.get("text"), str))
    return texts


def parse_stream(path: Path, expected_session: str, expected_workflow: dict[str, Any] | str | None = None) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    final = None
    metadata: dict[str, Any] = {"permission_denials": [], "actual_models": [], "actual_model_sources": [],
                                "guarded_tool_uses": {}, "provider_response_observed": False}
    expected_input = ({"name": expected_workflow} if isinstance(expected_workflow, str) else {key: expected_workflow[key] for key in ("name", "args") if key in expected_workflow}) if expected_workflow is not None else None
    workflow_uses: set[str] = set()
    workflow_results: set[str] = set()
    workflow_completed: set[str] = set()
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            event_type = obj.get("type")
            event_session = obj.get("session_id") or obj.get("sessionId")
            if event_session:
                if event_session != expected_session:
                    metadata.setdefault("session_mismatches", []).append(event_session)
            if obj.get("model"):
                metadata["reported_model"] = obj["model"]
                if obj.get("type") == "system" and obj.get("subtype") in {"init", "system_init"}:
                    metadata["system_init_model"] = obj["model"]
                    metadata["initialized_model"] = obj["model"]
                    metadata["system_init_session_id"] = event_session
            if event_type == "assistant":
                metadata["provider_response_observed"] = True
                message = obj.get("message")
                assistant_model = message.get("model") if isinstance(message, dict) else None
                if not isinstance(assistant_model, str):
                    assistant_model = obj.get("model") if isinstance(obj.get("model"), str) else None
                if assistant_model and assistant_model not in metadata["actual_models"]:
                    metadata["actual_models"].append(assistant_model)
                if assistant_model and "assistant_message" not in metadata["actual_model_sources"]:
                    metadata["actual_model_sources"].append("assistant_message")
            if event_type == "result":
                metadata["provider_response_observed"] = True
                model_usage = obj.get("modelUsage")
                if isinstance(model_usage, dict):
                    usage_model_observed = False
                    for model_name in model_usage:
                        if isinstance(model_name, str) and model_name:
                            usage_model_observed = True
                            if model_name not in metadata["actual_models"]:
                                metadata["actual_models"].append(model_name)
                    if usage_model_observed and "result_model_usage" not in metadata["actual_model_sources"]:
                        metadata["actual_model_sources"].append("result_model_usage")
            if "usage" in obj:
                metadata["usage"] = obj["usage"]
            if obj.get("subtype") == "permission_denials" or obj.get("permission_denials"):
                metadata["permission_denials"].append(obj)
            for block in _stream_content_blocks(obj):
                if block.get("type") == "tool_use" and isinstance(block.get("id"), str) and isinstance(block.get("name"), str):
                    metadata["guarded_tool_uses"][block["id"]] = block["name"]
            if expected_workflow is not None:
                for block in _stream_content_blocks(obj):
                    if block.get("type") == "tool_use" and block.get("name") == "Workflow":
                        input_ = block.get("input")
                        tool_id = block.get("id")
                        if isinstance(tool_id, str) and isinstance(input_, dict) and input_ == expected_input:
                            workflow_uses.add(tool_id)
                    elif block.get("type") == "tool_result":
                        tool_id = block.get("tool_use_id")
                        if isinstance(tool_id, str) and tool_id in workflow_uses and block.get("is_error") is False:
                            workflow_results.add(tool_id)
                if (obj.get("type") == "system" and obj.get("subtype") == "task_notification"
                        and event_session == expected_session and obj.get("status") == "completed"
                        and obj.get("tool_use_id") in workflow_results):
                    workflow_completed.add(obj["tool_use_id"])
                for text in (_stream_texts(obj) if obj.get("type") == "user" else []):
                    if "<task-notification>" not in text:
                        continue
                    tool_match = re.search(r"<tool-use-id>([^<]+)</tool-use-id>", text)
                    status_match = re.search(r"<status>([^<]+)</status>", text)
                    if (tool_match and status_match and tool_match.group(1) in workflow_uses
                            and status_match.group(1) == "completed"):
                        workflow_completed.add(tool_match.group(1))
            if obj.get("type") == "result" or "subtype" in obj and ("structured_output" in obj or obj.get("is_error")):
                final = obj
                metadata["final_session_id"] = event_session
    init_session = metadata.get("system_init_session_id")
    final_session = metadata.get("final_session_id")
    if init_session == expected_session:
        metadata["actual_session_id"] = init_session
    if metadata.get("session_mismatches") or init_session != expected_session or final_session != expected_session or init_session != final_session:
        metadata["session_error"] = "system/init session and final result must both equal the expected session"
    if expected_workflow is not None:
        metadata["workflow_name"] = expected_input["name"]
        metadata["workflow_tool_use_observed"] = bool(workflow_uses)
        metadata["workflow_tool_result_success"] = bool(workflow_uses) and workflow_results == workflow_uses
        metadata["workflow_tool_use_count"] = len(workflow_uses)
        metadata["workflow_tool_result_success_count"] = len(workflow_results)
        metadata["workflow_completion_observed"] = bool(workflow_uses) and workflow_completed == workflow_uses
        metadata["workflow_completion_count"] = len(workflow_completed)
    metadata["actual_model_source"] = "+".join(metadata["actual_model_sources"]) or None
    return final, metadata


def result_payload(provider: dict[str, Any]) -> dict[str, Any]:
    value = provider.get("structured_output")
    if not isinstance(value, dict):
        raise BridgeError("provider result lacks structured_output object")
    if value.get("status") not in {"completed", "blocked"} or not isinstance(value.get("summary"), str):
        raise BridgeError("structured result has invalid status or summary")
    for key in ("evidence", "checks", "unresolved"):
        if not isinstance(value.get(key), list) or not all(isinstance(x, str) for x in value[key]):
            raise BridgeError(f"structured result {key} must be an array of strings")
    return value


def process_group_absent(group_id: int) -> bool:
    """Confirm absence via a readable full process list; denied/partial is unknown."""
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


def _process_rows() -> list[dict[str, Any]] | None:
    """Return a complete, minimally parsed process table or None when unsure."""
    try:
        result = subprocess.run(["ps", "-axo", "pid=,pgid=,lstart=,command="], text=True,
                                capture_output=True, timeout=3)
        if result.returncode != 0:
            return None
        rows: list[dict[str, Any]] = []
        for line in result.stdout.splitlines():
            parts = line.strip().split(None, 7)
            if len(parts) < 7:
                return None
            command = parts[7] if len(parts) == 8 else ""
            rows.append({"pid": int(parts[0]), "process_group": int(parts[1]),
                         "start_time": " ".join(parts[2:7]), "command": command})
        if not any(row["pid"] == os.getpid() for row in rows):
            return None
        return rows
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


def capture_process_identity(pid: int, expected_session_id: str | None = None) -> dict[str, Any] | None:
    rows = _process_rows()
    if rows is None:
        return None
    row = next((item for item in rows if item["pid"] == pid), None)
    if row is None:
        return None
    return {"pid": pid, "process_group": row["process_group"], "start_time": row["start_time"],
            "command_sha256": hashlib.sha256(row["command"].encode("utf-8", "replace")).hexdigest(),
            "expected_session_id": expected_session_id, "observed_at": time.time()}


def process_identity_presence(identity: Any, process_group: Any, label: str) -> dict[str, str]:
    """Check a recorded process identity without treating a reused PID as ours."""
    if isinstance(process_group, bool) or not isinstance(process_group, int) or process_group <= 0:
        return {"state": "unconfirmed", "reason": f"{label} identity is missing or invalid"}
    # ESRCH is kernel evidence that no member of this process group exists.  It
    # is both stronger and cheaper than a process-list search, and remains
    # usable in restricted hosts where `ps` itself cannot be executed.
    try:
        os.killpg(process_group, 0)
    except ProcessLookupError:
        return {"state": "stopped", "reason": f"{label} is absent"}
    except OSError as exc:
        # Permission and transient host errors are not absence evidence.  Keep
        # old records unknown instead of trusting a potentially restricted
        # process list to disprove the kernel's ambiguous result.
        return {"state": "unconfirmed", "reason": f"cannot inspect {label}: {type(exc).__name__}: {exc}"}
    rows = _process_rows()
    if rows is None:
        return {"state": "unconfirmed", "reason": f"cannot inspect {label}"}
    members = [row for row in rows if row["process_group"] == process_group]
    if not members:
        return {"state": "unconfirmed", "reason": f"{label} was live at the kernel check but absent from the process table; retry inspection"}
    if not isinstance(identity, dict):
        return {"state": "unconfirmed", "reason": f"{label} is present but its durable identity is unavailable"}
    pid = identity.get("pid")
    if (identity.get("process_group") != process_group or isinstance(pid, bool) or not isinstance(pid, int)
            or pid <= 0 or not isinstance(identity.get("start_time"), str)
            or not isinstance(identity.get("command_sha256"), str)):
        return {"state": "unconfirmed", "reason": f"{label} is present but its durable identity is invalid"}
    leader = next((row for row in members if row["pid"] == pid), None)
    if leader is not None:
        command_sha = hashlib.sha256(leader["command"].encode("utf-8", "replace")).hexdigest()
        if leader["start_time"] == identity.get("start_time") and command_sha == identity.get("command_sha256"):
            return {"state": "running", "reason": f"{label} with the recorded identity is still present"}
    session = identity.get("expected_session_id")
    if isinstance(session, str) and session and any(session in row["command"] for row in members):
        return {"state": "running", "reason": f"{label} session is still present in its process group"}
    if leader is not None:
        if leader["start_time"] == identity.get("start_time"):
            return {"state": "unconfirmed", "reason": f"{label} kept its PID/start time but its command identity changed"}
        return {"state": "stopped", "reason": f"recorded {label} identity is absent; its PID/PGID was reused"}
    return {"state": "unconfirmed", "reason": f"{label} leader ended but same-group descendants or a reused group remain"}


def process_group_stopped(group_id: int) -> bool:
    """Prove a process group is absent without treating inspection denial as absence."""
    try:
        os.killpg(group_id, 0)
    except ProcessLookupError:
        return True
    except OSError:
        return process_group_absent(group_id)
    return False


def wait_process_group_absent(group_id: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while True:
        if process_group_stopped(group_id):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(.05)


def terminate_group(proc: subprocess.Popen[str]) -> str | None:
    """Request group termination and return a diagnostic if its outcome is unknown.

    A failed signal is not evidence that the child stopped.  Callers must turn a
    returned diagnostic into an ``unknown`` terminal state and leave the cwd
    marker in place for Runtime or standalone reconciliation.
    """
    # Reap an already exited direct child before signalling its process group.
    # On macOS an unreaped session leader remains visible as a zombie and
    # killpg() can return EPERM for that otherwise empty group.  That is a
    # cleanup race, not evidence of a live descendant.  If the group still has
    # members after reaping, the ordinary group termination path remains in
    # force.
    if proc.poll() is not None and process_group_stopped(proc.pid):
        return None
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return None
    except OSError as exc:
        proc.poll()
        if process_group_absent(proc.pid):
            return None
        return f"SIGTERM process-group request failed: {type(exc).__name__}: {exc}"
    # A parent can exit while descendants in its group ignore SIGTERM.
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        proc.poll()
        try:
            os.killpg(proc.pid, 0)
        except ProcessLookupError:
            return
        except OSError as exc:
            # Some macOS host permission checks race with group disappearance.
            # A failed signal check alone never establishes that it has stopped.
            try:
                proc.wait(timeout=max(.01, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                return f"process-group liveness check failed: {type(exc).__name__}: {exc}"
            if process_group_absent(proc.pid):
                return None
            return f"process-group liveness check failed: {type(exc).__name__}: {exc}"
        time.sleep(.05)
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        return None
    except OSError as exc:
        return f"SIGKILL process-group request failed: {type(exc).__name__}: {exc}"
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        return "direct child did not terminate before cleanup deadline"
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if process_group_stopped(proc.pid):
            return None
        time.sleep(.05)
    return "process group remained present after SIGKILL"


def record_unconfirmed_cleanup(run_dir: Path, packet: dict[str, Any], reason: str, cleanup_error: str,
                               original_error: BaseException | None = None) -> int:
    """Persist a fail-closed terminal state when process cleanup cannot be proved."""
    diagnostic = {"reason": reason, "cleanup_status": "unconfirmed", "cleanup_error": cleanup_error}
    if original_error is not None:
        diagnostic["original_error"] = f"{type(original_error).__name__}: {original_error}"
    dump(run_dir / "error.json", diagnostic)
    marker = unknown_lane_marker(Path(packet["cwd"]))
    dump(marker, {"cwd": packet["cwd"], "run_id": run_dir.name, "reason": reason,
                  "cleanup_status": "unconfirmed", "recorded_at": time.time()})
    state(run_dir, "unknown", **diagnostic)
    dump(run_dir / "receipt.json", {"status": "unknown", "task_id": packet["task_id"], "revision": packet["revision"],
                                     "cleanup_status": "unconfirmed",
                                     "note": "process cleanup could not be confirmed; reconcile process state before reusing this cwd."})
    activity(run_dir, "unknown", "process cleanup unconfirmed; reconciliation required", status="unknown",
             text=cleanup_error)
    print(f"bridge cleanup unconfirmed: {cleanup_error}", file=sys.stderr, flush=True)
    return 1


def check_command(command: list[str], cwd: Path, timeout: float, input_text: str | None = None, env: dict[str, str] | None = None) -> tuple[int | None, str, str, str | None]:
    """Keep diagnostics in memory; callers expose only selected non-secret fields."""
    proc = None
    try:
        proc = subprocess.Popen(command, cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, start_new_session=True, env=env)
        stdout, stderr = proc.communicate(input=input_text, timeout=timeout)
        return proc.returncode, stdout, stderr, None
    except subprocess.TimeoutExpired:
        cleanup_error = terminate_group(proc)
        if cleanup_error:
            return None, "", "", "timeout_termination_unconfirmed"
        return None, "", "", "timeout"
    except OSError:
        return None, "", "", "execution_error"
    except KeyboardInterrupt:
        if proc is not None:
            # check_command has no run directory in which to leave a recovery
            # marker.  Preserve the interrupt for its owning command instead.
            terminate_group(proc)
        raise
    finally:
        if proc is not None:
            for pipe in (proc.stdin, proc.stdout, proc.stderr):
                if pipe is not None:
                    pipe.close()


def failure_category(text: str) -> str:
    """Classify failures without reflecting provider messages or credential values."""
    lower = text.lower()
    if any(s in lower for s in ("authentication_error", "invalid api key", "invalid_api_key", "token expired",
                                "token has expired", "not logged in", "please run /login")) or re.search(r"\b401\b", lower):
        return "authentication_failed"
    if any(s in lower for s in ("rate_limit", "rate limit", "usage limit", "credit balance", "insufficient credit")) or re.search(r"\b429\b", lower):
        return "quota_or_rate_limited"
    if "permission_denied" in lower or "permission_error" in lower or re.search(r"\b403\b", lower):
        return "access_denied"
    if any(s in lower for s in ("enotfound", "econn", "connection error", "network", "dns", "tls", "ssl")):
        return "network_error"
    return "verification_failed"


def auth_metadata(data: dict[str, Any]) -> dict[str, Any]:
    # Never echo arbitrary auth JSON, token sources, organization IDs, or raw errors.
    safe: dict[str, Any] = {}
    for source, destination in (("authMethod", "auth_method"), ("apiProvider", "provider"), ("subscriptionType", "subscription_type")):
        value = data.get(source)
        if isinstance(value, str) and re.fullmatch(r"[A-Za-z][A-Za-z0-9._-]{0,39}", value):
            safe[destination] = value
    email = data.get("email")
    if isinstance(email, str) and re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
        local, domain = email.rsplit("@", 1)
        safe["account_masked"] = local[:1] + "***@" + domain
    return safe


def create_cli_descriptor(packet: dict, resume: Path | None = None) -> dict:
    """Pin selection once. Resume never follows a newly activated executable."""
    if resume is not None:
        previous = resume / "cli-selection.json"
        if not previous.is_file():
            raise BridgeError("resume_cli_identity_unavailable: legacy run has no fixed CLI identity; start a fresh run")
        descriptor = load(previous)
        if descriptor.get("purpose") != "dispatch":
            raise BridgeError("resume_cli_identity_unavailable: prior selection is not a business dispatch")
        protocol = descriptor.get("dispatch_protocol_version")
        if protocol is None and descriptor.get("schema_version") == 1 and descriptor.get("contract_id") in LEGACY_DISPATCH_CONTRACTS:
            protocol = 1
        if type(protocol) is not int or protocol != DISPATCH_PROTOCOL_VERSION:
            raise BridgeError("resume_protocol_incompatible: prior dispatch protocol is not supported; inspect the old result and start fresh")
        # Rebind implementation evidence, never the executable or session. The
        # ordinary preflight must still verify current qualification + resume.
        descriptor = {**descriptor, "dispatch_protocol_version": protocol,
                      "contract_id_at_start": descriptor.get("contract_id_at_start", descriptor.get("contract_id")),
                      "contract_id": bridge_contract_id(), "contract_id_now": bridge_contract_id()}
        verify_cli_descriptor(descriptor, packet)
        return descriptor
    budget = packet.get("budget") if isinstance(packet.get("budget"), dict) else {}
    capabilities = [name for name in ("max_turns", "max_budget_usd") if budget.get(name) is not None]
    selection = locate_claude(required_groups=required_groups(packet), required_capabilities=capabilities)
    path = selection.get("path")
    descriptor = {"schema_version": 1, "purpose": "dispatch", "contract_id": bridge_contract_id(),
                  "dispatch_protocol_version": DISPATCH_PROTOCOL_VERSION,
                  "contract_id_at_start": bridge_contract_id(), "contract_id_now": bridge_contract_id(),
                  "selection": selection, "created_at": time.time()}
    if path:
        target = Path(path)
        descriptor.update(sha256=sha256_file(target), resolved_path=str(target.resolve()))
        if isinstance(selection.get("identity"), dict):
            descriptor["identity_id"] = selection["identity"]["id"]
            descriptor["version"] = selection["identity"]["version"]
    return descriptor


def verify_cli_descriptor(descriptor: dict, packet: dict | None = None) -> tuple[dict, Mapping | None]:
    """Validate an internal descriptor; no user packet field can enable a probe."""
    if not isinstance(descriptor, dict):
        raise BridgeError("invalid CLI identity descriptor")
    if descriptor.get("purpose") == "qualification":
        if packet is None:
            raise BridgeError("qualification descriptor requires a registered fixture packet")
        from cli_validation import validate_probe_descriptor
        profile = validate_probe_descriptor(descriptor, packet)
        from cli_store import identity
        selected = identity(descriptor["identity_id"])
        return {"path": selected["path"], "source": "qualification", "identity": selected}, {selected["version"]: profile}
    if descriptor.get("purpose") != "dispatch" or descriptor.get("contract_id") != bridge_contract_id():
        raise BridgeError("CLI descriptor contract changed; prepare a fresh dispatch with current compatibility evidence")
    if (type(descriptor.get("schema_version")) is not int or descriptor["schema_version"] != 1
            or type(descriptor.get("dispatch_protocol_version")) is not int
            or descriptor["dispatch_protocol_version"] != DISPATCH_PROTOCOL_VERSION):
        raise BridgeError("dispatch_protocol_incompatible: prepare a dispatch with a supported protocol")
    selection = descriptor.get("selection")
    if not isinstance(selection, dict):
        raise BridgeError("invalid pinned CLI selection")
    path = selection.get("path")
    if path:
        actual = Path(path)
        if (not actual.is_file() or not os.access(actual, os.X_OK)
                or str(actual.resolve()) != descriptor.get("resolved_path")
                or sha256_file(actual) != descriptor.get("sha256")):
            raise BridgeError("cli_identity_changed: selected executable is missing or changed; no fallback was used")
        if isinstance(selection.get("identity"), dict):
            from cli_store import identity
            managed = identity(selection["identity"]["id"])
            if managed["path"] != path or managed["sha256"] != descriptor.get("sha256"):
                raise BridgeError("managed CLI identity changed")
    return selection, None


def check_environment(cwd: Path, verify: bool = False, model: str = "sonnet", timeout: float = 60,
                      profiles: Mapping[str, Mapping[str, Any]] | None = None,
                      required_groups: list[str] | None = None, selection: dict | None = None) -> dict[str, Any]:
    needed_groups = set(required_groups or ["core", "read_only"])
    if not needed_groups.issubset(GROUPS):
        raise BridgeError("unknown compatibility capability group")
    if selection is None:
        selection = locate_claude(required_groups=sorted(needed_groups))
    binary = selection.get("candidate") or "claude"
    resolved = selection["path"]
    child_env = cli_environment(selection)
    report: dict[str, Any] = {"checked_at": time.time(), "cwd": str(cwd), "ready": False,
                              "cli": {"path": resolved, "installed": bool(resolved), "source": selection["source"],
                                      "identity": selection.get("identity"), "selection_reason": selection.get("selection_reason")},
                              "auth": {"status": "not_checked", "credential_validity": "not_verified"},
                              "probe": {"status": "not_requested", "requested_model": model}}
    def fail(status_: str, action: str) -> dict[str, Any]:
        report.update(status=status_, action=action)
        report["installation"] = {"installed": report["cli"]["installed"], "version": report["cli"].get("version")}
        report["readiness"] = {"installation": report["installation"], "login": report["auth"],
                               "invocation": report["probe"], "compatibility": report.get("compatibility", {"status": "not_checked"})}
        return report
    if not resolved:
        if selection.get("source") == "managed_native" and selection.get("identity"):
            report["cli"]["installed"] = True
            report["compatibility"] = {"status": "unverified", "required_groups": sorted(needed_groups),
                                       "selection_available": False}
            return fail("cli_capability_unverified", selection.get("action") or "所需能力尚未验证；请选择合格的保留版本或验证候选。")
        status_ = "cli_not_executable" if "/" in binary and Path(binary).exists() else "cli_not_found"
        return fail(status_, selection.get("action") or "Install/repair Claude Code: https://code.claude.com/docs/en/setup")
    code, out, _, error = check_command([resolved, "--version"], cwd, min(timeout, 10), env=child_env)
    if error or code != 0:
        return fail("cli_unavailable", "Claude CLI could not run; check installation and host execution permissions.")
    version = re.search(r"\b\d+\.\d+\.\d+(?:[-+][\w.]+)?\b", out)
    report["cli"]["version"] = version.group() if version else "unrecognized"
    code, out, _, error = check_command([resolved, "--help"], cwd, min(timeout, 10), env=child_env)
    if error or code != 0:
        return fail("cli_unavailable", "Claude CLI help failed; check installation and host permissions.")
    required_flags = set(REQUIRED_FLAGS)
    if "resume" in needed_groups:
        required_flags.add("--resume")
    missing = sorted(flag for flag in required_flags if flag not in out)
    report["cli"]["missing_flags"] = missing
    profile = resolved_profile(report["cli"]["version"], selection, profiles)
    available_groups = set(profile.get("groups", GROUPS)) if profile else set()
    if "--resume" not in out:
        available_groups.discard("resume")
    missing_groups = sorted(needed_groups - available_groups)
    report["cli"]["profile"] = {"status": "tested" if profile else "unverified",
                                "source": (profile or {}).get("source", "test_fixture" if profiles is not None else None),
                                "tested_versions": sorted((SUPPORTED_CLI_PROFILES if profiles is None else profiles).keys())}
    report["compatibility"] = {"status": "unverified" if not profile else "unavailable" if missing or missing_groups else "verified",
                               "required_groups": sorted(needed_groups), "available_groups": sorted(available_groups),
                               "missing_groups": missing_groups, "missing_flags": missing,
                               "source": report["cli"]["profile"]["source"], "contract_id": bridge_contract_id()}
    advertised = (profile or {}).get("capabilities", {})
    capabilities = {name: flag for name, flag in advertised.items() if isinstance(flag, str) and flag in out}
    report["cli"]["capabilities"] = {name: {"flag": flag, "enforcement": "provider_option", "limit_exhaustion_verified": False} for name, flag in capabilities.items()}
    report["cli"]["unavailable_capabilities"] = sorted(name for name in advertised if name not in capabilities)
    # Local login is orthogonal to compatibility: an unqualified candidate can
    # still have a perfectly valid configured account. Never report it as logout.
    code, out, err, error = check_command([resolved, "auth", "status", "--json"], cwd, min(timeout, 15), env=child_env)
    if error:
        report["auth"]["status"] = "check_timeout" if error.startswith("timeout") else "check_" + error
        if error == "timeout_termination_unconfirmed":
            report["auth"]["cleanup_status"] = "unconfirmed"
        return fail("auth_check_failed", "Authentication status could not be read; check host/keychain permissions or CLI availability, without clearing credentials.")
    try:
        data = json.loads(out)
    except (ValueError, TypeError):
        data = None
    if not isinstance(data, dict) or type(data.get("loggedIn")) is not bool:
        report["auth"]["status"] = "unknown"
        return fail("auth_check_failed", "Claude auth status did not return a recognized JSON status; inspect CLI auth support.")
    report["auth"].update(auth_metadata(data), logged_in=data["loggedIn"])
    if not data["loggedIn"] and code in (0, 1):
        report["auth"]["status"] = "not_logged_in"
        return fail("not_logged_in", "Run claude auth login in your terminal for an account login, or configure your intended API/provider authentication; then rerun the check.")
    if code != 0:
        report["auth"]["status"] = "unknown"
        return fail("auth_check_failed", "Authentication status returned an inconsistent exit code; do not treat it as authenticated.")
    report["auth"]["status"] = "reported_logged_in"
    report["installation"] = {"installed": True, "version": report["cli"]["version"]}
    report["readiness"] = {"installation": report["installation"], "login": report["auth"],
                           "invocation": report["probe"], "compatibility": report["compatibility"]}
    if missing:
        return fail("cli_incompatible", "Required CLI options are unavailable for this task; the login result is independent. Select a retained compatible execution version.")
    if not profile:
        return fail("cli_profile_unverified", "This executable has not passed the bridge compatibility suite. Keep the managed working version; use claude_cli_update to prepare and validate this candidate. This does not mean the login is invalid.")
    if missing_groups:
        return fail("cli_capability_unverified", "This task needs unverified compatibility groups: " + ", ".join(missing_groups) + ". Use a retained eligible version or validate the candidate for these groups.")

    if verify:
        # A single normal CLI request exercises its effective auth, with no agent tools.
        command = [resolved, "-p", "--model", model, "--effort", "low", "--output-format", "json",
                   "--tools", "", "--disallowedTools", ",".join(READ_TOOLS + WRITE_TOOLS + DISALLOWED),
                   "--permission-mode", "dontAsk", "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
                   "--disable-slash-commands", "--no-session-persistence"]
        if "max_turns" in capabilities:
            command[1:1] = [capabilities["max_turns"], "1"]
        code, out, err, error = check_command(command, cwd, timeout, "Authentication connectivity check only. Reply exactly AUTH_CHECK_OK. Do not use tools.", env=child_env)
        report["probe"]["checked_at"] = time.time()
        if error:
            category = "timeout" if error.startswith("timeout") else error
            report["probe"]["status"] = category
            if error == "timeout_termination_unconfirmed":
                report["probe"]["cleanup_status"] = "unconfirmed"
                return fail("verification_timeout", "Probe timed out and host permissions prevented termination confirmation; inspect its process before retrying. Credential validity remains unknown.")
            return fail("verification_" + category, "Online verification could not finish; this does not establish that credentials are invalid.")
        try:
            provider = json.loads(out)
        except (ValueError, TypeError):
            provider = None
        if (isinstance(provider, dict) and code == 0 and provider.get("type") == "result"
                and provider.get("subtype") == "success" and not provider.get("is_error")
                and not provider.get("permission_denials") and isinstance(provider.get("result"), str)):
            report["probe"]["status"] = "verified"
            report["auth"]["credential_validity"] = "verified_for_request"
            report["ready"] = True
            report["status"] = "verified"
            return report
        category = failure_category(out + "\n" + err)
        report["probe"]["status"] = category
        if category == "authentication_failed":
            report["auth"]["credential_validity"] = "rejected"
            return fail(category, "Claude rejected authentication. Reauthenticate the intended account/provider, then rerun verification.")
        return fail(category, "Check the reported availability category; network, quota, model or access failures do not by themselves mean the login expired.")
    report.update(ready=True, status="local_checks_passed",
                  note="CLI reports authentication configured. Use doctor --verify to check an actual request; this check does not prove remote validity, quota or session persistence.")
    return report


def lifecycle_update(args: argparse.Namespace, **fields: Any) -> None:
    record = getattr(args, "_lifecycle", None)
    if record is None:
        return
    record.update(fields, updated_at=time.time())
    dump(args._lifecycle_path, record)


def run(args: argparse.Namespace) -> int:
    """Keep launch evidence outside run_dir, including failures before mkdir.

    Missing child metadata is never proof of no dispatch: launch_intent is
    persisted before Popen and remains indeterminate across a hard crash.
    """
    sidecar = os.environ.get("CODEX_BRIDGE_LIFECYCLE_FILE")
    if sidecar:
        packet_path = require_absolute(args.packet, "packet")
        raw = load(packet_path)
        args._lifecycle_path = require_absolute(sidecar, "lifecycle file")
        args._lifecycle_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        args._lifecycle = {"schema_version": 1, "run_id": Path(args.run_dir).name,
                           "task_id": raw.get("task_id"), "revision": raw.get("revision"),
                           "cwd": str(Path(raw.get("cwd", "")).resolve()),
                           "packet_sha256": sha256_file(packet_path), "bridge_pid": os.getpid(),
                           "cli_descriptor_sha256": getattr(args, "cli_descriptor_sha256", None),
                           "phase": "pre_dispatch", "status": "starting", "child_started": False,
                           "terminal": False}
        args._lifecycle["bridge_identity"] = capture_process_identity(os.getpid())
        lifecycle_update(args)
    try:
        descriptor_path = getattr(args, "cli_descriptor", None)
        if descriptor_path:
            descriptor_file = require_absolute(descriptor_path, "cli-descriptor")
            expected = getattr(args, "cli_descriptor_sha256", None)
            if expected and sha256_file(descriptor_file) != expected:
                raise BridgeError("CLI descriptor bytes changed before bridge start")
            args._cli_descriptor = load(descriptor_file)
            args._cli_descriptor_sha256 = sha256_file(descriptor_file)
        lifecycle_update(args, cli_descriptor_sha256=getattr(args, "_cli_descriptor_sha256", None))
        code = _run(args)
    except (Exception, KeyboardInterrupt) as exc:
        record = getattr(args, "_lifecycle", {})
        not_launched = record.get("child_started") is False and record.get("phase") == "pre_dispatch"
        lifecycle_update(args, terminal=True, status="failed" if not_launched else "unknown",
                         phase="pre_dispatch" if not_launched else "terminal",
                         reason=f"{type(exc).__name__}: {exc}")
        raise
    descriptor = getattr(args, "_cli_descriptor", None)
    receipt_path = Path(args.run_dir) / "receipt.json"
    if descriptor and receipt_path.is_file():
        receipt_value = load(receipt_path)
        receipt_value["cli_identity"] = {"id": descriptor.get("identity_id"), "sha256": descriptor.get("sha256") or descriptor.get("identity_sha256"),
                                         "contract_id": descriptor.get("contract_id"), "purpose": descriptor.get("purpose"),
                                         "contract_id_at_start": descriptor.get("contract_id_at_start", descriptor.get("contract_id")),
                                         "contract_id_now": descriptor.get("contract_id"),
                                         "dispatch_protocol_version": descriptor.get("dispatch_protocol_version")}
        dump(receipt_path, receipt_value)
    record = getattr(args, "_lifecycle", {})
    if record:
        receipt_path = Path(args.run_dir) / "receipt.json"
        receipt = load(receipt_path) if receipt_path.is_file() else {}
        status_ = receipt.get("status", "unknown")
        lifecycle_update(args, terminal=True, status=status_,
                         phase="pre_dispatch" if record.get("child_started") is False else "terminal",
                         blocked_by=receipt.get("blocked_by"), reason=receipt.get("reason") or receipt.get("note"))
    return code


def _run(args: argparse.Namespace) -> int:
    packet_path = require_absolute(args.packet, "packet")
    run_dir = require_absolute(args.run_dir, "run-dir")
    if run_dir.exists():
        raise BridgeError("run-dir already exists; refusing duplicate run")
    raw_packet = load(packet_path)
    packet = validate_packet(raw_packet)
    resume = require_absolute(args.resume_from, "resume-from") if args.resume_from else None
    if resume is not None:
        if packet["workspace_kind"] == "artifacts":
            raise BridgeError("artifacts workspace does not support resume; start a fresh read-only review")
        if packet["role"] == "workflow_review":
            raise BridgeError("workflow_review does not support resume; start a fresh hash-bound run")
        if not resume.is_dir():
            raise BridgeError("resume-from must be an existing run directory")
        resume_session = validate_resume(packet, resume)
    else:
        resume_session = None
    descriptor = getattr(args, "_cli_descriptor", None)
    if descriptor is None:
        descriptor = create_cli_descriptor(packet, resume)
        args._cli_descriptor = descriptor
    selection, probe_profiles = verify_cli_descriptor(descriptor, raw_packet)
    run_dir.mkdir(mode=0o700)  # atomic creation is the duplicate-run guard
    dump(run_dir / "cli-selection.json", descriptor)
    activity(run_dir, "preflight", "run directory created", status="preflight")
    if unknown_lane_marker(Path(packet["cwd"])).exists():
        raise BridgeError("cwd has an unknown prior supervised run; reconcile it manually before dispatch")
    early_cancel = os.environ.get("CODEX_BRIDGE_CANCEL_FILE")
    if early_cancel and Path(early_cancel).is_file():
        dump(run_dir / "cancel.json", {"reason": "runtime cancelled before bridge dispatch", "requested_at": time.time()})
        return pre_dispatch_cancelled(run_dir, packet, "cancelled before bridge dispatch")
    lock = None
    proc: subprocess.Popen[str] | None = None
    stream = err = selector = None
    try:
        lock = lock_file(Path(packet["cwd"]), packet["task_id"])
        dump(run_dir / "packet.json", packet)
        before, requirements = preflight(packet, resume)
        dump(run_dir / "workspace_before.json", before)
        if packet["workspace_kind"] == "git":
            dump(run_dir / "git_before.json", before)
        dump(run_dir / "requirements.json", requirements)
        environment = check_environment(Path(packet["cwd"]), model=packet["model"], profiles=probe_profiles,
                                        required_groups=required_groups(packet, resume is not None), selection=selection)
        environment["cli_descriptor"] = {"identity_id": descriptor.get("identity_id"), "sha256": descriptor.get("sha256") or descriptor.get("identity_sha256"),
                                           "contract_id": descriptor.get("contract_id"), "purpose": descriptor.get("purpose"),
                                           "contract_id_at_start": descriptor.get("contract_id_at_start", descriptor.get("contract_id")),
                                           "contract_id_now": descriptor.get("contract_id"),
                                           "dispatch_protocol_version": descriptor.get("dispatch_protocol_version")}
        dump(run_dir / "environment.json", environment)
        if not environment["ready"]:
            state(run_dir, "blocked", reason=environment["status"])
            dump(run_dir / "receipt.json", {"status": "blocked", "blocked_by": "preflight", "reason": environment["status"],
                                             "task_id": packet["task_id"], "revision": packet["revision"],
                                             "note": "Claude task was not launched. Resolve the preflight issue, then use a new run-dir."})
            print(f"bridge blocked before dispatch: {environment['status']}", flush=True)
            activity(run_dir, "blocked", "environment preflight blocked dispatch", status="blocked")
            return 1
        capabilities = environment["cli"].get("capabilities", {})
        unsupported_budget = sorted(key for key in packet["budget"] if key not in capabilities)
        if unsupported_budget:
            state(run_dir, "blocked", reason="budget_capability_unavailable")
            dump(run_dir / "receipt.json", {"status": "blocked", "blocked_by": "preflight", "reason": "budget_capability_unavailable",
                                             "task_id": packet["task_id"], "revision": packet["revision"],
                                             "note": f"CLI profile does not advertise executable budget controls: {unsupported_budget}. Wall timeout remains enforced by bridge."})
            activity(run_dir, "blocked", "requested budget control unavailable", status="blocked")
            return 1
        # Runtime cancellation can arrive while an authentication/CLI preflight
        # is running.  Check both marker locations again immediately before the
        # only Claude Popen in this function.
        if (run_dir / "cancel.json").is_file() or (early_cancel and Path(early_cancel).is_file()):
            if not (run_dir / "cancel.json").exists():
                dump(run_dir / "cancel.json", {"reason": "runtime cancelled during bridge preflight", "requested_at": time.time()})
            return pre_dispatch_cancelled(run_dir, packet, "cancelled during bridge preflight")
        state(run_dir, "running", pid=os.getpid())
        activity(run_dir, "starting", "dispatching supervised Claude process", status="starting")
        bridge = Path(__file__).resolve()
        hook_argv = [sys.executable, str(bridge), "hook", "--packet", str(run_dir / "packet.json"), "--cwd", packet["cwd"]]
        # Claude treats hook exit 1/import failures as non-blocking.  Force
        # every launcher failure to exit 2, which PreToolUse treats as a block.
        hook_cmd = " ".join(shlex.quote(item) for item in hook_argv) + " || exit 2"
        matcher = "*" if packet["role"] == "workflow_review" else "Read|Glob|Grep|Edit|Write"
        settings = {"hooks": {"PreToolUse": [{"matcher": matcher, "hooks": [{"type": "command", "command": hook_cmd}]}]}}
        dump(run_dir / "settings.json", settings)
        dump(run_dir / "mcp.json", {"mcpServers": {}})
        schema = {"type": "object", "required": ["status", "summary", "evidence", "checks", "unresolved"],
                  "properties": {"status": {"enum": ["completed", "blocked"]}, "summary": {"type": "string"},
                  "evidence": {"type": "array", "items": {"type": "string"}}, "checks": {"type": "array", "items": {"type": "string"}},
                  "unresolved": {"type": "array", "items": {"type": "string"}}}}
        session = str(uuid.uuid4())
        tools = READ_TOOLS + (WRITE_TOOLS if packet["role"] == "implement" else ())
        allowed_tools = tools
        disallowed_tools = DISALLOWED
        if packet["role"] == "workflow_review":
            tools = READ_TOOLS + ("Workflow",)
            allowed_tools = READ_TOOLS + (f"Workflow({packet['workflow']['name']})",)
            disallowed_tools = tuple(tool for tool in DISALLOWED if tool != "Workflow") + WRITE_TOOLS
        command = [environment["cli"]["path"], "-p", "--model", packet["model"], "--effort", packet["effort"],
                   "--output-format", "stream-json", "--verbose", "--json-schema", json.dumps(schema), "--permission-mode", "dontAsk",
                   "--tools", ",".join(tools), "--allowedTools", ",".join(allowed_tools), "--disallowedTools", ",".join(disallowed_tools),
                   "--settings", str(run_dir / "settings.json"), "--strict-mcp-config", "--mcp-config", str(run_dir / "mcp.json")]
        for key, value in packet["budget"].items():
            command += [capabilities[key]["flag"], str(value)]
        if packet["role"] != "workflow_review":
            command.append("--disable-slash-commands")
        command += (["--resume", resume_session] if resume_session else ["--session-id", session])
        dump(run_dir / "command.json", {"argv": command, "initial_session_id": session, "resume_session_id": resume_session,
                                        "cli_descriptor": environment["cli_descriptor"]})
        child_env = cli_environment(environment["cli"])
        policy_evidence = hook_policy_preflight(Path(packet["cwd"]), child_env)
        self_test_id = "bridge-self-test-" + uuid.uuid4().hex
        if packet["workspace_kind"] == "artifacts":
            declared_reads = packet["requirement_sources"] + packet.get("input_files", [])
            if not declared_reads:
                raise BridgeError("artifact guard self-test requires a declared readable file")
            self_test_path = declared_reads[0]
        else:
            self_test_path = "."
        self_test = subprocess.run(["/bin/sh", "-c", hook_cmd], cwd=packet["cwd"], env=child_env,
                                   input=json.dumps({"hook_event_name": "PreToolUse", "tool_name": "Read",
                                                     "tool_input": {"file_path": self_test_path}, "tool_use_id": self_test_id}),
                                   text=True, capture_output=True, timeout=10)
        if self_test.returncode != 0 or self_test.stdout.strip():
            raise BridgeError("bridge hook guard self-test failed before provider dispatch")
        policy_evidence.update(self_test="passed", self_test_tool_use_id=self_test_id,
                               limitation="does not prove server/MDM-only policy or later CLI hook invocation")
        dump(run_dir / "hook-guard-preflight.json", policy_evidence)
        print(f"bridge started task={packet['task_id']} run_dir={run_dir}", flush=True)
        stream = (run_dir / "stream.jsonl").open("w", encoding="utf-8")
        err = (run_dir / "stderr").open("w", encoding="utf-8")
        verify_cli_descriptor(descriptor, raw_packet)
        if packet["role"] == "workflow_review":
            prior_cap = child_env.get("CLAUDE_CODE_WORKFLOW_MAX_CONCURRENT_AGENTS", "2")
            child_env["CLAUDE_CODE_WORKFLOW_MAX_CONCURRENT_AGENTS"] = "1" if prior_cap == "1" else "2"
        lifecycle_update(args, phase="launch_intent", child_started=None)
        try:
            proc = subprocess.Popen(command, cwd=packet["cwd"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=err,
                                    text=True, start_new_session=True, env=child_env)
        except OSError as exc:
            # Popen reports exec failure only after reaping its failed child.
            lifecycle_update(args, phase="pre_dispatch", child_started=False, reason=f"Claude launch failed: {exc}")
            raise
        # Popen succeeded.  Stage this fact in memory before touching stdin so
        # a synchronous BrokenPipe is still recorded as a launched child by the
        # terminal lifecycle update.  Keep the persisted sidecar at
        # launch_intent until prompt delivery succeeds: a hard crash in this
        # small window remains deliberately unknown.
        lifecycle = getattr(args, "_lifecycle", None)
        if lifecycle is not None:
            lifecycle.update(phase="executing", child_started=True, child_pid=proc.pid,
                             child_process_group=proc.pid, child_identity=None)
        assert proc.stdin and proc.stdout
        # Send the task before the full-process-table identity observation.
        # A fast CLI can otherwise exit while ps runs, turning prompt delivery
        # into BrokenPipe and leaving an unreaped zombie for exception cleanup.
        proc.stdin.write(prompt(packet)); proc.stdin.close()
        child_identity = capture_process_identity(proc.pid, resume_session or session)
        lifecycle_update(args, phase="executing", child_started=True, child_pid=proc.pid, child_process_group=proc.pid,
                         child_identity=child_identity)
        dump(run_dir / "child.json", {"pid": proc.pid, "process_group": proc.pid, "started_at": time.time(),
                                       "expected_session_id": resume_session or session, "identity": child_identity})
        activity(run_dir, "executing", "Claude child started", status="executing")
        selector = selectors.DefaultSelector()
        os.set_blocking(proc.stdout.fileno(), False)
        selector.register(proc.stdout, selectors.EVENT_READ)
        pending = b""
        stream_eof = False
        def drain(wait: float) -> None:
            nonlocal pending, stream_eof
            for _, _ in selector.select(wait):
                chunk = os.read(proc.stdout.fileno(), 65536)
                if not chunk:
                    stream_eof = True
                    selector.unregister(proc.stdout)
                    return
                pending += chunk
                while b"\n" in pending:
                    line, pending = pending.split(b"\n", 1)
                    decoded = line.decode("utf-8", "replace")
                    stream.write(decoded + "\n"); stream.flush()
                    try:
                        item = json.loads(decoded)
                    except json.JSONDecodeError:
                        item = None
                    if isinstance(item, dict) and item.get("type") in {"system", "result"}:
                        subtype = item.get("subtype")
                        if packet["role"] == "workflow_review" and subtype in {"task_started", "task_progress", "task_notification"}:
                            append_activity(run_dir, "workflow", str(item.get("description") or item.get("summary") or "workflow activity"),
                                            status=str(item.get("status") or subtype))
                            for progress in item.get("workflow_progress", []):
                                if isinstance(progress, dict) and progress.get("type") == "workflow_agent":
                                    append_activity(run_dir, "workflow_agent", str(progress.get("label") or "workflow agent"),
                                                    tool=progress.get("lastToolName"), status=progress.get("state"),
                                                    text=progress.get("phaseTitle"))
                        else:
                            append_activity(run_dir, "provider", "provider lifecycle event", status=str(subtype or item.get("type")))
        deadline = time.monotonic() + args.timeout
        cancelled = timed_out = interrupted = False
        cleanup_error: str | None = None
        try:
            while proc.poll() is None:
                drain(.15)
                if (run_dir / "cancel.json").exists():
                    cancelled = True; cleanup_error = terminate_group(proc); break
                if time.monotonic() > deadline:
                    timed_out = True; cleanup_error = terminate_group(proc); break
        except KeyboardInterrupt:
            cancelled = True
            interrupted = True
            cleanup_error = terminate_group(proc)
        if cleanup_error:
            reason = "KeyboardInterrupt" if interrupted else ("cancelled run" if cancelled else "timed out run")
            return record_unconfirmed_cleanup(run_dir, packet, reason, cleanup_error)
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired as exc:
            cleanup_error = terminate_group(proc)
            return record_unconfirmed_cleanup(run_dir, packet, "post-stop wait", cleanup_error or
                                              "direct child remained running after stop request", exc)
        residual_process_group = not wait_process_group_absent(proc.pid, .75)
        if residual_process_group:
            cleanup_error = terminate_group(proc)
            if cleanup_error:
                return record_unconfirmed_cleanup(run_dir, packet, "normal child exit left a live process group", cleanup_error)
        activity(run_dir, "collecting", "collecting final provider output", status="collecting")
        eof_deadline = time.monotonic() + 3
        while not stream_eof and time.monotonic() < eof_deadline:
            drain(min(.1, max(.01, eof_deadline - time.monotonic())))
        if pending:
            stream.write(pending.decode("utf-8", "replace")); stream.flush()
        selector.close(); proc.stdout.close()
        meta_stream_warning = None if stream_eof else "stdout did not reach EOF before collection deadline"
        if not stream_eof:
            cleanup_error = terminate_group(proc)
            if cleanup_error:
                return record_unconfirmed_cleanup(run_dir, packet, "stdout collection cleanup", cleanup_error)
        stream.close(); err.close()
        expected_session = resume_session or session
        provider, meta = parse_stream(run_dir / "stream.jsonl", expected_session,
                                      packet["workflow"] if packet["role"] == "workflow_review" else None)
        coverage = hook_coverage(run_dir, meta.get("guarded_tool_uses", {}),
                                 guard_all_tools=packet["role"] == "workflow_review")
        after = workspace_snapshot(packet); dump(run_dir / "workspace_after.json", after)
        if packet["workspace_kind"] == "git":
            dump(run_dir / "git_after.json", after)
            if before["head"] is None:
                diff_text = git(Path(packet["cwd"]), "diff", "--no-ext-diff") + git(Path(packet["cwd"]), "diff", "--cached", "--no-ext-diff")
                diff_header = "# Workspace diff has no HEAD baseline; untracked paths are listed in git_after.json.\n"
            else:
                diff_text = git(Path(packet["cwd"]), "diff", "--no-ext-diff", "HEAD")
                diff_header = "# Workspace diff relative to HEAD; this may include changes that predated this run.\n"
            (run_dir / "diff.patch").write_text(diff_header + diff_text, encoding="utf-8")
        requirements_after = {source: sha256_file(Path(source)) for source in packet["requirement_sources"]}
        final_status = "failed"; structured: dict[str, Any] | None = None
        subtype = provider.get("subtype") if provider else None
        if cancelled or (run_dir / "cancel.json").exists():
            final_status = "cancelled"
        elif timed_out:
            final_status = "timeout"
        elif provider is None or meta_stream_warning:
            final_status = "failed"
        elif residual_process_group:
            meta["process_group_error"] = "direct Claude child exited while same-group descendants remained; descendants were stopped"
            final_status = "failed"
        elif coverage["status"] != "complete":
            meta["hook_guard_error"] = "one or more provider tool uses lack matching PreToolUse guard evidence"
            final_status = "failed"
        elif meta.get("session_error") or meta["permission_denials"] or subtype == "permission_denials" or provider.get("is_error") or subtype != "success" or proc.returncode != 0:
            final_status = "failed"
        else:
            try:
                structured = result_payload(provider)
                final_status = structured["status"]
            except Exception as exc:
                meta["result_validation_error"] = str(exc)
                final_status = "failed"
        if packet["role"] == "workflow_review" and not (meta.get("workflow_tool_result_success") and meta.get("workflow_completion_observed")):
            # The first tool result only confirms that a background workflow was
            # launched.  A completed task-notification tied to that same tool ID
            # is required before Claude's later text can count as a report.
            meta["workflow_error"] = "expected named Workflow launch and completed task-notification were not observed"
            if final_status == "completed":
                final_status = "blocked"
        scope_error = None
        invariant_error = None
        if requirements_after != requirements:
            invariant_error = "requirement source changed during run"
            final_status = "failed"
        if packet["workspace_kind"] == "git" and after["head"] != before["head"]:
            invariant_error = "git HEAD changed during run"
            final_status = "failed"
        if packet["role"] in ("review", "workflow_review"):
            if packet["workspace_kind"] == "artifacts":
                differences = artifact_snapshot_difference(before, after)
                if differences:
                    invariant_error = "artifact workspace changed: " + "; ".join(differences)
                    final_status = "failed"
            elif after != before:
                invariant_error = "review run changed workspace state"
                final_status = "failed"
        if packet["role"] == "implement":
            changed = changed_paths(after)
            changed.update(p for p in packet["protected_files"]
                           if before["guarded_content_hashes"].get(p) != after["guarded_content_hashes"].get(p))
            forbidden = sorted(p for p in changed if p not in set(packet["owned_files"]) or p in set(packet["protected_files"]) or bad_owned_path(p))
            if forbidden:
                scope_error = f"out-of-scope or protected changes: {forbidden}"
                final_status = "failed"
        usage = meta.get("usage")
        usage_summary = {key: usage[key] for key in ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
                         if isinstance(usage, dict) and isinstance(usage.get(key), int)}
        cost = provider.get("total_cost_usd") if provider else None
        cost = cost if isinstance(cost, (float, int)) and not isinstance(cost, bool) and math.isfinite(cost) and cost >= 0 else None
        result = {"workspace_kind": packet["workspace_kind"], "workspace_manifest": {"before": "workspace_before.json", "after": "workspace_after.json"},
                  "cli_identity": environment["cli_descriptor"],
                  "total_cost_usd": cost, "blocked_by": "executor" if final_status == "blocked" else None,
                  "budget": packet["budget"], "budget_enforcement": "provider_enforced" if packet["budget"] else "not_requested",
                  "provider_subtype": subtype, "provider_exit_code": proc.returncode, "requested_model": packet["model"],
                  "initialized_model": meta.get("initialized_model"), "cli_resolved_model": meta.get("initialized_model"),
                  "actual_models": meta.get("actual_models", []), "provider_models": meta.get("actual_models", []),
                  "actual_model": meta.get("actual_models", [None])[0] if len(meta.get("actual_models", [])) == 1 else None,
                  "actual_model_source": meta.get("actual_model_source"),
                  "provider_response_observed": meta.get("provider_response_observed", False),
                  "system_init_model": meta.get("system_init_model"), "reported_model": meta.get("reported_model"),
                  "actual_session_id": meta.get("actual_session_id"),
                  "usage": usage, "usage_summary": usage_summary, "permission_denials": meta["permission_denials"], "structured": structured,
                  "scope_error": scope_error, "invariant_error": invariant_error, "stream_warning": meta_stream_warning,
                  "result_validation_error": meta.get("result_validation_error"), "session_error": meta.get("session_error"),
                  "hook_guard_preflight": policy_evidence, "hook_guard_coverage": coverage,
                  "hook_guard_error": meta.get("hook_guard_error"),
                  "process_group_error": meta.get("process_group_error"),
                  "system_init_session_id": meta.get("system_init_session_id"), "final_session_id": meta.get("final_session_id"),
                  "workflow_name": meta.get("workflow_name"), "workflow_tool_use_observed": meta.get("workflow_tool_use_observed"),
                  "workflow_tool_result_success": meta.get("workflow_tool_result_success"),
                  "workflow_tool_use_count": meta.get("workflow_tool_use_count"),
                  "workflow_tool_result_success_count": meta.get("workflow_tool_result_success_count"),
                  "workflow_completion_observed": meta.get("workflow_completion_observed"),
                  "workflow_completion_count": meta.get("workflow_completion_count"),
                  "workflow_error": meta.get("workflow_error")}
        # A marker arriving after process exit but before final persistence wins.
        if (run_dir / "cancel.json").exists() and final_status in {"completed", "blocked"}:
            final_status = "cancelled"
        dump(run_dir / "result.json", result)
        receipt_status = "reported" if final_status == "completed" else ("blocked" if final_status == "blocked" else final_status)
        dump(run_dir / "receipt.json", {"status": receipt_status, "task_id": packet["task_id"], "revision": packet["revision"],
                                          "blocked_by": "executor" if receipt_status == "blocked" else None,
                                          "note": "reported is not accepted; Codex must verify evidence and workspace state."})
        state(run_dir, final_status, provider_exit_code=proc.returncode)
        phase = "reported" if final_status == "completed" else final_status
        activity(run_dir, phase, "provider result recorded", status=phase)
        print(f"bridge finished status={final_status} provider_subtype={subtype}", flush=True)
        return 0 if final_status in {"completed", "blocked"} else 1
    except KeyboardInterrupt:
        if proc is not None:
            cleanup_error = terminate_group(proc)
            if cleanup_error:
                return record_unconfirmed_cleanup(run_dir, packet, "KeyboardInterrupt", cleanup_error)
        state(run_dir, "cancelled", reason="KeyboardInterrupt")
        dump(run_dir / "receipt.json", {"status": "cancelled", "note": "interrupted; inspect process state manually."})
        activity(run_dir, "cancelled", "bridge interrupted", status="cancelled")
        return 130
    except Exception as exc:
        if proc is not None:
            cleanup_error = terminate_group(proc)
            if cleanup_error:
                return record_unconfirmed_cleanup(run_dir, packet, "bridge exception cleanup", cleanup_error, exc)
        dump(run_dir / "error.json", {"error": str(exc)})
        state(run_dir, "failed", error=str(exc))
        dump(run_dir / "receipt.json", {"status": "failed", "note": "bridge failure; inspect stderr/error.json."})
        activity(run_dir, "failed", "bridge failure recorded", status="failed")
        print(f"bridge failed: {exc}", file=sys.stderr, flush=True)
        return 1
    finally:
        # Exception and unconfirmed-cleanup paths can return before collection.
        # Release this bridge's handles without implying that the child stopped.
        if selector is not None:
            selector.close()
        for handle in (stream, err, proc.stdin if proc else None, proc.stdout if proc else None):
            if handle is not None and not handle.closed:
                try:
                    handle.close()
                except OSError:
                    pass
        if lock is not None:
            lock.close()


def status(run_dir: Path) -> int:
    if not run_dir.is_dir() or not (run_dir / "state.json").exists():
        raise BridgeError("run-dir has no bridge state")
    s = load(run_dir / "state.json")
    if s.get("status") == "running":
        s["status"] = "unknown"
        s["note"] = "bridge may have died; do not retry automatically; inspect process and artifacts manually."
    print(json.dumps(s, ensure_ascii=False))
    return 0


def cancel(run_dir: Path, reason: str) -> int:
    if not run_dir.is_dir() or not (run_dir / "state.json").exists():
        raise BridgeError("run-dir has no bridge state")
    current = load(run_dir / "state.json")
    if current.get("status") in FINAL:
        raise BridgeError("run is already final; cancellation marker was not written")
    dump(run_dir / "cancel.json", {"reason": reason, "requested_at": time.time()})
    print(f"cancel marker written: {run_dir}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Supervised Claude CLI bridge; invocation constraints are not an OS sandbox.")
    sub = parser.add_subparsers(dest="command", required=True)
    d = sub.add_parser("doctor", help="check CLI installation/login; optionally verify one no-tool request")
    d.add_argument("--cwd", default=str(Path.cwd()))
    d.add_argument("--verify", action="store_true", help="make one minimal request to verify effective authentication")
    d.add_argument("--model", default="sonnet")
    d.add_argument("--timeout", type=float, default=60)
    r = sub.add_parser("run", help="start one supervised run")
    r.add_argument("--packet", required=True); r.add_argument("--run-dir", required=True); r.add_argument("--resume-from")
    r.add_argument("--timeout", type=float, required=True)
    r.add_argument("--cli-descriptor", help=argparse.SUPPRESS)
    r.add_argument("--cli-descriptor-sha256", help=argparse.SUPPRESS)
    s = sub.add_parser("status"); s.add_argument("--run-dir", required=True)
    c = sub.add_parser("cancel"); c.add_argument("--run-dir", required=True); c.add_argument("--reason", required=True)
    h = sub.add_parser("hook"); h.add_argument("--packet", required=True); h.add_argument("--cwd", required=True)
    args = parser.parse_args()
    try:
        if args.command == "doctor":
            cwd = require_absolute(args.cwd, "cwd").resolve()
            if not cwd.is_dir() or args.timeout <= 0:
                raise BridgeError("doctor requires an existing cwd and a positive timeout")
            report = check_environment(cwd, args.verify, args.model, args.timeout)
            print(json.dumps(report, ensure_ascii=False))
            return 0 if report["ready"] else 1
        if args.command == "run": return run(args)
        if args.command == "status": return status(require_absolute(args.run_dir, "run-dir"))
        if args.command == "cancel": return cancel(require_absolute(args.run_dir, "run-dir"), args.reason)
        return hook(require_absolute(args.packet, "packet"), args.cwd)
    except BridgeError as exc:
        print(f"bridge error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
