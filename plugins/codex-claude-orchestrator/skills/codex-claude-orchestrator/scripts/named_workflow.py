"""Inventory and bind saved Claude Workflow scripts without executing them."""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any


class NamedWorkflowError(ValueError):
    """The requested saved workflow cannot be safely identified."""


NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
META_START = re.compile(r"^\s*export\s+const\s+meta\s*=\s*\{")
META_NAME = re.compile(r"(?:^|[,\n])\s*name\s*:\s*(['\"])([A-Za-z0-9][A-Za-z0-9_-]{0,63})\1\s*(?=,|\n|})")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _git_root(cwd: Path) -> Path:
    try:
        root = subprocess.run(
            ["git", "-C", str(cwd), "rev-parse", "--show-toplevel"],
            capture_output=True, check=True, text=True,
        ).stdout.strip()
    except subprocess.CalledProcessError as exc:
        raise NamedWorkflowError("saved workflow inventory requires a Git working tree") from exc
    return Path(root).resolve()


def _no_symlink(path: Path, label: str) -> None:
    """Refuse a symlink at every project workflow path component."""
    if path.is_symlink():
        raise NamedWorkflowError(f"saved workflow {label} cannot be a symbolic link: {path}")


def _project_workflow_dirs(cwd: Path) -> list[Path]:
    root = _git_root(cwd)
    try:
        relative = cwd.resolve().relative_to(root)
    except ValueError as exc:  # pragma: no cover - git root should contain cwd
        raise NamedWorkflowError("Git root does not contain cwd") from exc
    directories: list[Path] = []
    current = cwd.resolve()
    while True:
        claude = current / ".claude"
        if claude.exists() or claude.is_symlink():
            _no_symlink(claude, "project directory")
            workflows = claude / "workflows"
            if workflows.exists() or workflows.is_symlink():
                _no_symlink(workflows, "project directory")
                if not workflows.is_dir():
                    raise NamedWorkflowError(f"saved workflow directory is not a directory: {workflows}")
                directories.append(workflows)
        if current == root:
            break
        current = current.parent
    return directories


def _personal_workflow_dir() -> Path:
    configured = os.environ.get("CLAUDE_CONFIG_DIR")
    base = Path(configured).expanduser() if configured else Path.home() / ".claude"
    return base / "workflows"


def _meta_name(path: Path) -> str:
    """Read only a script's literal meta declaration; never evaluate JavaScript."""
    source = path.read_text(encoding="utf-8")
    if not META_START.match(source):
        raise NamedWorkflowError(f"saved workflow lacks a literal export const meta block: {path}")
    # The saved-script contract requires a pure literal meta block.  Accepting
    # only an early literal `name` prevents this inventory from interpreting JS.
    header = source[: min(len(source), 16_384)]
    match = META_NAME.search(header)
    if not match:
        raise NamedWorkflowError(f"saved workflow meta.name is missing or unsafe: {path}")
    return match.group(2)


def _entries(directory: Path, scope: str) -> list[dict[str, str]]:
    if not directory.exists() and not directory.is_symlink():
        return []
    _no_symlink(directory, f"{scope} directory")
    if not directory.is_dir():
        raise NamedWorkflowError(f"saved workflow directory is not a directory: {directory}")
    result: list[dict[str, str]] = []
    for candidate in sorted(directory.iterdir(), key=lambda value: value.name):
        if candidate.suffix != ".js":
            continue
        _no_symlink(candidate, f"{scope} script")
        if not candidate.is_file():
            raise NamedWorkflowError(f"saved workflow script is not a regular file: {candidate}")
        result.append({"name": _meta_name(candidate), "path": str(candidate.resolve()),
                       "sha256": sha256_file(candidate), "scope": scope})
    return result


def inventory(cwd: str | Path) -> list[dict[str, str]]:
    """Return effective saved workflows as name/path/hash, with no execution.

    Project directories are checked nearest-to-root.  A name present in any
    project directory is deliberately rejected when it appears more than once:
    the CLI has a precedence rule, but a permission rule only names the workflow
    and this adapter must not turn a path/hash packet into an implicit choice.
    A single project name shadows a same-name personal workflow, matching Claude
    Code's documented project precedence.
    """
    base = Path(cwd).resolve()
    if not base.is_dir():
        raise NamedWorkflowError("workflow cwd must be an existing directory")
    project: list[dict[str, str]] = []
    for directory in _project_workflow_dirs(base):
        project.extend(_entries(directory, "project"))
    names: dict[str, list[dict[str, str]]] = {}
    for entry in project:
        names.setdefault(entry["name"], []).append(entry)
    duplicate_projects = sorted(name for name, entries in names.items() if len(entries) > 1)
    if duplicate_projects:
        raise NamedWorkflowError("ambiguous duplicate project workflow name(s): " + ", ".join(duplicate_projects))
    personal = _entries(_personal_workflow_dir(), "personal")
    effective = list(project)
    project_names = set(names)
    for entry in personal:
        if entry["name"] not in project_names:
            effective.append(entry)
    names = {}
    for entry in effective:
        names.setdefault(entry["name"], []).append(entry)
    duplicates = sorted(name for name, entries in names.items() if len(entries) > 1)
    if duplicates:
        raise NamedWorkflowError("ambiguous duplicate saved workflow name(s): " + ", ".join(duplicates))
    return sorted(effective, key=lambda entry: (entry["name"], entry["path"]))


def _json_value(value: Any) -> bool:
    try:
        json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError):
        return False
    return True


def validate(packet_workflow: Any, cwd: str | Path) -> dict[str, Any]:
    """Require an exact name/path/sha packet identity that inventory can find."""
    if not isinstance(packet_workflow, dict) or set(packet_workflow) not in ({"name", "path", "sha256"}, {"name", "path", "sha256", "args"}):
        raise NamedWorkflowError("workflow must contain name, path, sha256 and optional args")
    name = packet_workflow.get("name")
    path = packet_workflow.get("path")
    digest = packet_workflow.get("sha256")
    if not isinstance(name, str) or not NAME.fullmatch(name):
        raise NamedWorkflowError("workflow name is unsafe")
    if not isinstance(path, str) or not Path(path).is_absolute():
        raise NamedWorkflowError("workflow path must be absolute")
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise NamedWorkflowError("workflow sha256 must be a lowercase SHA-256")
    requested = {"name": name, "path": str(Path(path).resolve()), "sha256": digest}
    found = [{key: entry[key] for key in requested} for entry in inventory(cwd)]
    if requested not in found:
        raise NamedWorkflowError("workflow name/path/sha256 is not an effective saved workflow")
    if "args" in packet_workflow:
        if not _json_value(packet_workflow["args"]):
            raise NamedWorkflowError("workflow args must be a JSON value")
        requested["args"] = packet_workflow["args"]
    return requested
