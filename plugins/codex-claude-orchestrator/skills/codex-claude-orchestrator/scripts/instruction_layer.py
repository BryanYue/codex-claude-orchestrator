"""Make the host instruction layer visible: which CLAUDE.md, rules and hooks a delegated session loads.

The delegated Claude session runs with the user's normal setting sources, so
user and project instructions apply on top of the brief.  This module only
lists and hashes them; it never changes or suppresses any of them.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Mapping

MAX_FILES = 200
MAX_HASH_BYTES = 2 * 1024 * 1024
MANAGED_SETTINGS = (Path("/Library/Application Support/ClaudeCode/managed-settings.json"),
                    Path("/etc/claude-code/managed-settings.json"))


def _config_root(environ: Mapping[str, str]) -> Path:
    configured = environ.get("CLAUDE_CONFIG_DIR")
    if configured:
        return Path(configured).expanduser()
    return Path(environ.get("HOME") or str(Path.home())) / ".claude"


def _file_entry(path: Path, scope: str) -> dict[str, Any] | None:
    try:
        if path.is_symlink() and not path.exists():
            return None
        if not path.is_file():
            return None
        size = path.stat().st_size
        digest = None
        if size <= MAX_HASH_BYTES:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return {"path": str(path), "scope": scope, "readable": False}
    return {"path": str(path), "scope": scope, "bytes": size, "sha256": digest}


def _rules(directory: Path) -> list[Path]:
    try:
        return sorted(path for path in directory.rglob("*.md") if path.is_file())
    except OSError:
        return []


def _hooks(path: Path, source: str) -> list[dict[str, Any]]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    hooks = value.get("hooks") if isinstance(value, dict) else None
    found: list[dict[str, Any]] = []
    if isinstance(value, dict) and value.get("disableAllHooks") is True:
        found.append({"source": source, "path": str(path), "event": "*", "note": "disableAllHooks=true"})
    if not isinstance(hooks, dict):
        return found
    for event, groups in hooks.items():
        for group in groups if isinstance(groups, list) else []:
            if not isinstance(group, dict):
                continue
            for hook in group.get("hooks") or []:
                if isinstance(hook, dict):
                    command = str(hook.get("command") or hook.get("type") or "")
                    found.append({"source": source, "path": str(path), "event": str(event),
                                  "matcher": group.get("matcher"), "command": command[:200]})
    return found


def scan(execution_cwd: Path, environ: Mapping[str, str] | None = None) -> dict[str, Any]:
    env = os.environ if environ is None else environ
    root = _config_root(env)
    candidates: list[tuple[Path, str]] = [(root / "CLAUDE.md", "user")]
    candidates += [(path, "user_rules") for path in _rules(root / "rules")]
    settings: list[tuple[Path, str]] = [(root / "settings.json", "user")]
    cwd = Path(execution_cwd)
    for directory in [cwd, *cwd.parents]:
        scope = "project" if directory == cwd else "ancestor"
        candidates += [(directory / "CLAUDE.md", scope), (directory / "CLAUDE.local.md", scope + "_local"),
                       (directory / ".claude" / "CLAUDE.md", scope)]
        if directory == cwd:
            candidates += [(path, "project_rules") for path in _rules(directory / ".claude" / "rules")]
            settings += [(directory / ".claude" / "settings.json", "project"),
                         (directory / ".claude" / "settings.local.json", "project_local")]
    settings += [(path, "managed") for path in MANAGED_SETTINGS]
    files: list[dict[str, Any]] = []
    seen: set[str] = set()
    for path, scope in candidates:
        try:
            key = str(path.resolve())
        except OSError:
            key = str(path)
        if key in seen:
            continue
        seen.add(key)
        entry = _file_entry(path, scope)
        if entry is not None:
            files.append(entry)
        if len(files) >= MAX_FILES:
            break
    hooks = [hook for path, source in settings if path.is_file() for hook in _hooks(path, source)]
    return {"schema": 1, "execution_cwd": str(cwd), "config_root": str(root), "files": files, "hooks": hooks,
            "note": ("Instruction files and hooks the delegated session is expected to load with its default setting "
                     "sources; plugins and auto-memory are reported from the session's init event.")}
