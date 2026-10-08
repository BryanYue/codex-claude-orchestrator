"""Validate and normalize the 1.0 task packet Codex sends to claude_start.

The packet carries the user's words verbatim, the frozen inputs, and the
coordinator's additions labelled by origin.  Validation checks types, paths and
encodability only; it never rewrites a requirement.
"""
from __future__ import annotations

import math
import os
from pathlib import Path, PurePosixPath
import re
from typing import Any

KINDS = ("implement", "analyze")
PROFILES = ("copy", "readonly")
ORIGINS = ("user", "doc", "coordinator")
SOURCES = ("human", "relayed")
EFFORTS = ("low", "medium", "high", "xhigh", "max")
RUN_ID = re.compile(r"run-[A-Za-z0-9_-]{8,64}")
TIMEOUT_RANGE = (1, 14400)
MAX_MESSAGES = 50
MAX_TEXT = 200_000
FIELDS = frozenset({"task_id", "kind", "cwd", "user_messages", "no_user_words_reason", "inputs", "brief",
                    "constraints", "done_when", "focus", "write_hint", "protected", "verify", "profile", "web",
                    "model", "effort", "timeout_seconds", "max_budget_usd", "continue_from"})
RENAMED = {"role": "kind", "objective": "brief", "user_request": "user_messages", "requirement_sources": "inputs",
           "acceptance": "done_when", "owned_files": "write_hint", "protected_files": "protected",
           "review_mode": "profile", "review_scope": "focus", "budget": "max_budget_usd", "revision": None,
           "workspace_kind": "profile", "input_files": "inputs", "baseline_commit": None, "correction": None,
           "workflow": None, "workflow_requirement": None, "protocol_binding": None}


class PacketError(ValueError):
    pass


def _text(value: Any, label: str, *, blank: bool = False, limit: int = MAX_TEXT) -> str:
    if not isinstance(value, str):
        raise PacketError(f"{label} must be a string")
    if not blank and not value.strip():
        raise PacketError(f"{label} must not be blank")
    if "\x00" in value:
        raise PacketError(f"{label} must not contain NUL")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise PacketError(f"{label} contains an unpaired surrogate; send the original text as UTF-8") from exc
    if len(value) > limit:
        raise PacketError(f"{label} exceeds {limit} characters")
    return value


def _strings(value: Any, label: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise PacketError(f"{label} must be an array of strings")
    return [_text(item, f"{label}[{index}]") for index, item in enumerate(value)]


def _labelled(value: Any, label: str) -> list[dict[str, str]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise PacketError(f"{label} must be an array of {{text, origin}} objects")
    items = []
    for index, item in enumerate(value):
        if not isinstance(item, dict) or set(item) - {"text", "origin"}:
            raise PacketError(f"{label}[{index}] must be {{text, origin}}; origin is one of {', '.join(ORIGINS)}")
        origin = item.get("origin")
        if origin not in ORIGINS:
            raise PacketError(f"{label}[{index}].origin must be one of {', '.join(ORIGINS)}; "
                              "use coordinator for anything Codex added")
        items.append({"text": _text(item.get("text"), f"{label}[{index}].text"), "origin": origin})
    return items


def _messages(value: Any) -> list[dict[str, str]]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > MAX_MESSAGES:
        raise PacketError(f"user_messages must be an array of at most {MAX_MESSAGES} {{text, source, at?}} objects")
    messages = []
    for index, item in enumerate(value):
        if not isinstance(item, dict) or set(item) - {"text", "source", "at"}:
            raise PacketError(f"user_messages[{index}] must be {{text, source, at?}}")
        if item.get("source") not in SOURCES:
            raise PacketError(f"user_messages[{index}].source must be human (the user's own words) or relayed "
                              "(words passed on by another agent)")
        message = {"text": _text(item.get("text"), f"user_messages[{index}].text"), "source": item["source"]}
        if "at" in item:
            message["at"] = _text(item["at"], f"user_messages[{index}].at", limit=100)
        messages.append(message)
    return messages


def _globs(value: Any, label: str) -> list[str]:
    patterns = _strings(value, label)
    for pattern in patterns:
        path = PurePosixPath(pattern)
        if path.is_absolute() or ".." in path.parts or "\\" in pattern:
            raise PacketError(f"{label} entries are repository-relative POSIX globs without '..': {pattern}")
    return patterns


def _directory(value: Any) -> Path:
    raw = _text(value, "cwd", limit=4096)
    path = Path(os.path.expanduser(raw))
    if not path.is_absolute():
        raise PacketError("cwd must be an absolute directory (or start with ~)")
    path = path.resolve()
    if not path.is_dir():
        raise PacketError(f"cwd is not an existing directory: {raw}")
    return path


def _inputs(value: Any, cwd: Path) -> list[str]:
    paths = []
    for index, raw in enumerate(_strings(value, "inputs")):
        candidate = Path(os.path.expanduser(raw))
        path = (candidate if candidate.is_absolute() else cwd / candidate)
        if not os.path.lexists(path):
            raise PacketError(f"inputs[{index}] does not exist: {raw}")
        if path.is_symlink():
            raise PacketError(f"inputs[{index}] is a symlink; pass the target path: {raw}")
        if not (path.is_file() or path.is_dir()):
            raise PacketError(f"inputs[{index}] must be a regular file or directory: {raw}")
        resolved = str(path.resolve())
        if resolved not in paths:
            paths.append(resolved)
    return paths


def normalize(raw: Any) -> dict[str, Any]:
    """Return a complete normalized packet; profile stays None when the caller must choose the default."""
    if not isinstance(raw, dict):
        raise PacketError("packet must be an object")
    legacy = sorted(key for key in raw if key in RENAMED)
    if legacy:
        hints = [f"{key} -> {RENAMED[key]}" if RENAMED[key] else f"{key} (removed)" for key in legacy]
        raise PacketError("packet uses pre-1.0 fields: " + "; ".join(hints))
    unknown = sorted(set(raw) - FIELDS)
    if unknown:
        raise PacketError("unknown packet fields: " + ", ".join(unknown))
    for key in ("task_id", "kind", "cwd", "model", "effort", "timeout_seconds"):
        if key not in raw:
            raise PacketError(f"packet missing {key}")
    task_id = _text(raw["task_id"], "task_id", limit=120)
    if any(ord(character) < 32 for character in task_id):
        raise PacketError("task_id must not contain control characters")
    kind = raw["kind"]
    if kind not in KINDS:
        raise PacketError("kind must be implement or analyze (analyze covers review, planning, research and questions)")
    cwd = _directory(raw["cwd"])
    messages = _messages(raw.get("user_messages"))
    reason = raw.get("no_user_words_reason")
    if messages and reason is not None:
        raise PacketError("send either user_messages or no_user_words_reason, not both")
    if not messages:
        if reason is None:
            raise PacketError("user_messages is required: copy the user's own words verbatim; "
                              "only when none are available, explain why in no_user_words_reason")
        reason = _text(reason, "no_user_words_reason", limit=2000)
    focus = _strings(raw.get("focus"), "focus")
    if focus and kind != "analyze":
        raise PacketError("focus is only for analyze tasks")
    profile = raw.get("profile")
    if profile is not None and profile not in PROFILES:
        raise PacketError("profile must be copy or readonly")
    if kind == "implement" and profile == "readonly":
        raise PacketError("implement needs the copy profile; readonly has no write tools")
    web = raw.get("web", False)
    if not isinstance(web, bool):
        raise PacketError("web must be a boolean")
    model = _text(raw["model"], "model", limit=200)
    effort = raw["effort"]
    if effort not in EFFORTS:
        raise PacketError("effort must be one of " + ", ".join(EFFORTS))
    timeout = raw["timeout_seconds"]
    if (isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout)
            or not TIMEOUT_RANGE[0] <= timeout <= TIMEOUT_RANGE[1]):
        raise PacketError(f"timeout_seconds must be between {TIMEOUT_RANGE[0]} and {TIMEOUT_RANGE[1]}")
    budget = raw.get("max_budget_usd")
    if budget is not None and (isinstance(budget, bool) or not isinstance(budget, (int, float))
                               or not math.isfinite(budget) or budget <= 0):
        raise PacketError("max_budget_usd must be a positive number")
    continue_from = raw.get("continue_from")
    if continue_from is not None and (not isinstance(continue_from, str) or not RUN_ID.fullmatch(continue_from)):
        raise PacketError("continue_from must be a run_id")
    brief = raw.get("brief")
    return {
        "task_id": task_id, "kind": kind, "cwd": str(cwd),
        "user_messages": messages, "no_user_words_reason": reason if not messages else None,
        "inputs": _inputs(raw.get("inputs"), cwd),
        "brief": _text(brief, "brief") if brief is not None else None,
        "constraints": _labelled(raw.get("constraints"), "constraints"),
        "done_when": _labelled(raw.get("done_when"), "done_when"),
        "focus": focus, "write_hint": _globs(raw.get("write_hint"), "write_hint"),
        "protected": _globs(raw.get("protected"), "protected"), "verify": _strings(raw.get("verify"), "verify"),
        "profile": profile, "web": web, "model": model, "effort": effort, "timeout_seconds": float(timeout),
        "max_budget_usd": float(budget) if budget is not None else None, "continue_from": continue_from,
    }


def extends(previous: list[dict[str, str]], current: list[dict[str, str]]) -> bool:
    """A continued round may only append user messages; earlier words stay verbatim."""
    return len(current) >= len(previous) and current[:len(previous)] == previous
