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
META_PREFIX = re.compile(r"export\s+const\s+meta\s*=")
META_LIMIT = 65_536
META_DEPTH = 16
_IDENTIFIER = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]*")
_NUMBER = re.compile(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?")
_LITERAL_WORDS = {"true": True, "false": False, "null": None}
_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f", "v": "\v", "0": "\0",
            "'": "'", '"': '"', "\\": "\\", "`": "`"}


class _MetaParser:
    """Read the leading ``export const meta = {...}`` object literal without evaluating JavaScript.

    Only static data is accepted: strings (template literals without
    substitutions), numbers, true/false/null, and nested object/array literals.
    Computed or shorthand keys, spreads, methods, calls, references and
    duplicate keys are rejected as ambiguous or executable.  Only the end of
    the initializer is checked after the closing brace; the body is not parsed.
    """

    def __init__(self, source: str):
        self.source = source[:META_LIMIT]
        self.truncated = len(source) > META_LIMIT
        self.position = 0

    def fail(self, reason: str) -> NamedWorkflowError:
        return NamedWorkflowError(f"saved workflow meta is not a pure literal ({reason})")

    def skip(self) -> None:
        while self.position < len(self.source):
            char = self.source[self.position]
            if char.isspace():
                self.position += 1
            elif self.source.startswith("//", self.position):
                end = self.source.find("\n", self.position)
                self.position = len(self.source) if end < 0 else end + 1
            elif self.source.startswith("/*", self.position):
                end = self.source.find("*/", self.position + 2)
                if end < 0:
                    raise self.fail("unterminated comment")
                self.position = end + 2
            else:
                return

    def peek(self) -> str:
        self.skip()
        if self.position >= len(self.source):
            raise self.fail(f"no closing brace within {META_LIMIT} characters")
        return self.source[self.position]

    def expect(self, char: str) -> None:
        if self.peek() != char:
            raise self.fail(f"expected {char!r}")
        self.position += 1

    def string(self) -> str:
        quote = self.source[self.position]
        self.position += 1
        value: list[str] = []
        while self.position < len(self.source):
            char = self.source[self.position]
            self.position += 1
            if char == quote:
                return "".join(value)
            if quote == "`" and char == "$" and self.source.startswith("{", self.position):
                raise self.fail("template substitution")
            if char in "\r\n" and quote != "`":
                raise self.fail("line break in string")
            if char != "\\":
                value.append(char)
                continue
            if self.position >= len(self.source):
                break
            escape = self.source[self.position]
            self.position += 1
            if escape in "\r\n":
                if escape == "\r" and self.source.startswith("\n", self.position):
                    self.position += 1
            elif escape in _ESCAPES:
                value.append(_ESCAPES[escape])
            elif escape == "x" and re.fullmatch(r"[0-9A-Fa-f]{2}", self.source[self.position:self.position + 2]):
                value.append(chr(int(self.source[self.position:self.position + 2], 16)))
                self.position += 2
            elif escape == "u" and re.fullmatch(r"[0-9A-Fa-f]{4}", self.source[self.position:self.position + 4]):
                value.append(chr(int(self.source[self.position:self.position + 4], 16)))
                self.position += 4
            elif escape == "u" and self.source.startswith("{", self.position):
                end = self.source.find("}", self.position)
                digits = self.source[self.position + 1:end] if end > 0 else ""
                if not re.fullmatch(r"[0-9A-Fa-f]{1,6}", digits) or int(digits, 16) > 0x10FFFF:
                    raise self.fail("invalid unicode escape")
                value.append(chr(int(digits, 16)))
                self.position = end + 1
            elif escape in "xu" or escape.isdigit():
                raise self.fail("invalid escape")
            else:
                value.append(escape)
        raise self.fail("unterminated string")

    def key(self) -> str:
        char = self.peek()
        if char in "'\"":
            return self.string()
        if char == "[":
            raise self.fail("computed key")
        if self.source.startswith("...", self.position):
            raise self.fail("spread")
        number = _NUMBER.match(self.source, self.position)
        if number and not number.group().startswith("-"):
            self.position = number.end()
            return number.group()
        identifier = _IDENTIFIER.match(self.source, self.position)
        if not identifier:
            raise self.fail("invalid key")
        self.position = identifier.end()
        return identifier.group()

    def value(self, depth: int) -> Any:
        if depth > META_DEPTH:
            raise self.fail("nesting too deep")
        char = self.peek()
        if char == "{":
            return self.object(depth + 1)
        if char == "[":
            return self.array(depth + 1)
        if char in "'\"`":
            return self.string()
        number = _NUMBER.match(self.source, self.position)
        if number:
            self.position = number.end()
            return float(number.group()) if any(c in number.group() for c in ".eE") else int(number.group())
        identifier = _IDENTIFIER.match(self.source, self.position)
        if identifier and identifier.group() in _LITERAL_WORDS:
            self.position = identifier.end()
            return _LITERAL_WORDS[identifier.group()]
        raise self.fail("non-literal value")

    def object(self, depth: int) -> dict[str, Any]:
        self.expect("{")
        result: dict[str, Any] = {}
        while self.peek() != "}":
            name = self.key()
            if self.peek() != ":":
                raise self.fail("shorthand property or method")
            self.position += 1
            if name in result:
                raise self.fail(f"duplicate key {name!r}")
            result[name] = self.value(depth)
            if self.peek() == ",":
                self.position += 1
            elif self.peek() != "}":
                raise self.fail("expected ',' or '}'")
        self.position += 1
        return result

    def array(self, depth: int) -> list[Any]:
        self.expect("[")
        result: list[Any] = []
        while self.peek() != "]":
            if self.source.startswith("...", self.position):
                raise self.fail("spread")
            result.append(self.value(depth))
            if self.peek() == ",":
                self.position += 1
            elif self.peek() != "]":
                raise self.fail("expected ',' or ']'")
        self.position += 1
        return result

    def meta(self) -> dict[str, Any]:
        self.skip()
        prefix = META_PREFIX.match(self.source, self.position)
        if not prefix:
            raise NamedWorkflowError("saved workflow lacks a literal export const meta block")
        self.position = prefix.end()
        result = self.object(1)
        end = self.position
        self.skip()
        if self.position == len(self.source):
            if self.truncated:
                raise self.fail("initializer end exceeds metadata limit")
            return result
        tail = self.source[self.position:]
        if tail.startswith(";"):
            return result
        # ASI allows a new statement on the next line, but a call, member
        # access or operator still continues this initializer across that line.
        newline = any(c in self.source[end:self.position] for c in "\r\n\u2028\u2029")
        continuation = (tail[0] in "([.`?,*/%<>=&|^"
                        or tail.startswith(("!=", "+", "-"))
                        or re.match(r"(?:in|instanceof)\b", tail))
        if newline and tail.startswith(("++", "--")):
            continuation = False
        if not newline or continuation:
            raise self.fail("expression continues after the object literal")
        return result


def parse_meta(source: str) -> dict[str, Any]:
    return _MetaParser(source).meta()


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
        cwd.resolve().relative_to(root)
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


def _script_identity(path: Path) -> tuple[str, str]:
    """Return (meta.name, sha256) from one read of the script; never evaluate JavaScript."""
    data = path.read_bytes()
    try:
        meta = parse_meta(data.decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise NamedWorkflowError(f"saved workflow is not UTF-8 text: {path}") from exc
    except NamedWorkflowError as exc:
        raise NamedWorkflowError(f"{exc}: {path}") from exc
    name = meta.get("name")
    if not isinstance(name, str) or not NAME.fullmatch(name):
        raise NamedWorkflowError(f"saved workflow meta.name is missing or unsafe: {path}")
    return name, hashlib.sha256(data).hexdigest()


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
        name, digest = _script_identity(candidate)
        result.append({"name": name, "path": str(candidate.resolve()), "sha256": digest, "scope": scope})
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


def _requested(packet_workflow: Any) -> dict[str, Any]:
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
    requested: dict[str, Any] = {"name": name, "path": str(Path(path).resolve()), "sha256": digest}
    if "args" in packet_workflow:
        if not _json_value(packet_workflow["args"]):
            raise NamedWorkflowError("workflow args must be a JSON value")
        requested["args"] = packet_workflow["args"]
    return requested


def validate(packet_workflow: Any, cwd: str | Path) -> dict[str, Any]:
    """Require an exact name/path/sha packet identity that inventory can find."""
    requested = _requested(packet_workflow)
    identity = {key: requested[key] for key in ("name", "path", "sha256")}
    found = [{key: entry[key] for key in identity} for entry in inventory(cwd)]
    if identity not in found:
        raise NamedWorkflowError("workflow name/path/sha256 is not an effective saved workflow")
    return requested


def verify_bound(packet_workflow: Any, cwd: str | Path) -> dict[str, Any]:
    """Re-check an already dispatched script's identity without enumerating other scripts.

    Discovery and ambiguity are admission checks: ``validate`` runs at dispatch
    and again at the exact Workflow invocation.  An ordinary file-tool hook
    only needs the bound script to still be the same regular, non-symlinked
    file directly inside an effective workflow directory, with the same bytes
    and literal meta name, so an unrelated script appearing elsewhere cannot
    change whether an allowed Read stays allowed.
    """
    requested = _requested(packet_workflow)
    base = Path(cwd).resolve()
    if not base.is_dir():
        raise NamedWorkflowError("workflow cwd must be an existing directory")
    bound = Path(requested["path"])
    directories = _project_workflow_dirs(base)
    personal = _personal_workflow_dir()
    if personal.exists() or personal.is_symlink():
        _no_symlink(personal, "personal directory")
        directories.append(personal)
    directory = next((item for item in directories if item.resolve() == bound.parent), None)
    if directory is None:
        raise NamedWorkflowError("bound workflow is no longer in an effective saved workflow directory")
    lexical = directory / bound.name
    _no_symlink(lexical, "script")
    if not lexical.is_file() or lexical.resolve() != bound:
        raise NamedWorkflowError(f"bound workflow script is missing or not a regular file: {lexical}")
    name, digest = _script_identity(lexical)
    if name != requested["name"] or digest != requested["sha256"]:
        raise NamedWorkflowError("bound workflow script name or sha256 changed since dispatch")
    return requested
