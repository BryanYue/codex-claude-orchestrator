"""Project-local adoption state for the Codex/Claude orchestration plugin.

This module deliberately owns only ``.agents/codex-claude`` inside an explicit
Git project.  It does not install a plugin, invoke Claude, or alter any global
Codex configuration.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile
from typing import Any


SCHEMA_VERSION = 1
ROUTING = "codex-coordinates-claude-supervised"
CONFIG_RELATIVE = Path(".agents/codex-claude/workflow.json")
PROTOCOL_RELATIVE = Path(".agents/codex-claude/protocol.md")
PLUGIN_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PROTOCOL = PLUGIN_ROOT / "skills/codex-claude-orchestrator/references/orchestration-protocol.md"

BEGIN = "<!-- codex-claude-orchestrator:begin -->"
END = "<!-- codex-claude-orchestrator:end -->"
AGENTS_BLOCK = (
    "\n<!-- codex-claude-orchestrator:begin -->\n"
    "## Codex-Claude orchestration\n\n"
    "本项目的多步实施、已确认计划、继续执行与纠正工作使用 `$codex-claude-orchestrator`。"
    "主 Codex 对目标和验收负责；合适时可派原生 Codex 监督子代理管理 Claude。"
    "开始前先读取项目 workflow context 与原任务 state。日常简单任务按需直接完成，无需用户再次点名此插件。\n"
    "本采用仅提供工作流和参考协议，不批准任何 delta、基线或 oracle。\n"
    "<!-- codex-claude-orchestrator:end -->\n"
).encode("utf-8")


class WorkflowError(ValueError):
    """A safe adoption operation cannot proceed without manual coordination."""


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _title(data: bytes) -> str:
    match = re.search(r"(?m)^#\s+(.+?)\s*$", data.decode("utf-8", "replace"))
    return match.group(1).strip() if match else "Codex-Claude orchestration protocol"


def _has_parent_reference(value: str) -> bool:
    return any(part == ".." for part in Path(value).parts)


def _input_path(cwd: str) -> Path:
    if not isinstance(cwd, str) or not cwd:
        raise WorkflowError("cwd must be a non-empty directory path")
    if _has_parent_reference(cwd):
        raise WorkflowError("cwd may not contain '..' path traversal")
    candidate = Path(cwd).expanduser()
    if not candidate.is_absolute():
        candidate = (Path.cwd() / candidate)
    return candidate


def _contains_symbolic_link(candidate: Path) -> bool:
    """Reject an explicitly supplied project-entry alias for write operations.

    System temporary paths commonly contain a platform-owned symlink component
    (for example ``/var`` on macOS), so only the supplied entry itself is the
    user-controlled alias checked here.  Protected project paths are still
    checked component by component in ``_safe_project_path``.
    """
    return candidate.is_symlink()


def _safe_input_directory(cwd: str) -> Path:
    candidate = _input_path(cwd)
    if _contains_symbolic_link(candidate):
        raise WorkflowError("cwd may not be a symbolic link")
    try:
        candidate = candidate.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise WorkflowError(f"cwd does not exist: {cwd}") from error
    if not candidate.is_dir():
        raise WorkflowError("cwd must be a directory")
    return candidate


def _inspection_input_directory(cwd: str) -> tuple[Path, Path]:
    """Resolve a read-only project entry while retaining the requested alias."""
    requested = _input_path(cwd)
    try:
        resolved = requested.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise WorkflowError(f"cwd does not exist: {cwd}") from error
    if not resolved.is_dir():
        raise WorkflowError("cwd must be a directory")
    return requested, resolved


def _is_git_root(path: Path) -> bool:
    dot_git = path / ".git"
    return dot_git.exists() and not dot_git.is_symlink() and (dot_git.is_dir() or dot_git.is_file())


def _find_git_root(start: Path) -> Path | None:
    for candidate in (start, *start.parents):
        if _is_git_root(candidate):
            return candidate
    return None


def _find_binding_root(start: Path) -> Path | None:
    """Return the nearest repository; a nested repository is an ownership boundary."""
    for candidate in (start, *start.parents):
        if _is_git_root(candidate):
            return candidate
    return None


def _forbidden_root(root: Path) -> bool:
    home = Path.home().resolve()
    return root in {Path("/").resolve(), home, home / ".codex", Path("/root"), Path("/root/.codex")}


def _project_root_from_directory(start: Path, *, exact: bool) -> Path:
    git_root = _find_git_root(start)
    root = git_root if exact else (_find_binding_root(start) or git_root)
    if root is None:
        raise WorkflowError("cwd is not inside an existing Git project")
    if _forbidden_root(root):
        raise WorkflowError("home, filesystem root, and global .codex directories cannot be adopted")
    if exact and start != root:
        raise WorkflowError(f"cwd must be the explicit Git project root; actual root is {root}")
    return root


def _require_project_root(cwd: str, *, exact: bool) -> Path:
    return _project_root_from_directory(_safe_input_directory(cwd), exact=exact)


def _safe_relative(value: Any) -> bool:
    if not isinstance(value, str) or not value or Path(value).is_absolute() or _has_parent_reference(value):
        return False
    return True


def _safe_project_path(root: Path, relative: Path) -> Path:
    if not _safe_relative(str(relative)):
        raise WorkflowError("project-relative path is unsafe")
    path = root / relative
    # The fixed path is intentionally checked component by component before any write.
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise WorkflowError(f"symbolic links are not allowed in workflow paths: {current}")
    return path


def _read_regular(path: Path, label: str) -> bytes:
    if path.is_symlink() or not path.exists() or not path.is_file():
        raise WorkflowError(f"{label} must be an existing regular file: {path}")
    try:
        return path.read_bytes()
    except OSError as error:
        raise WorkflowError(f"cannot read {label}: {path}") from error


def _protocol_source(root: Path, protocol_path: str | None) -> tuple[Path, bytes]:
    if protocol_path is None:
        project_protocol = _safe_project_path(root, Path("docs/internal/orchestration-protocol.md"))
        if project_protocol.exists() or project_protocol.is_symlink():
            source = project_protocol
        else:
            source = DEFAULT_PROTOCOL
    else:
        if not isinstance(protocol_path, str) or not protocol_path or _has_parent_reference(protocol_path):
            raise WorkflowError("protocol_path may not be empty or contain '..' path traversal")
        raw = Path(protocol_path).expanduser()
        source = raw if raw.is_absolute() else _safe_project_path(root, raw)
    # A custom source is input only.  Its bytes are copied into the project path.
    return source, _read_regular(source, "protocol source")


def _load_config(path: Path) -> dict[str, Any]:
    data = _read_regular(path, "workflow configuration")
    try:
        config = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise WorkflowError(f"workflow configuration is malformed: {path}") from error
    if not isinstance(config, dict):
        raise WorkflowError("workflow configuration must be an object")
    expected = {"schema_version", "enabled", "routing", "protocol_path", "protocol_sha256", "protocol_title"}
    if set(config) != expected:
        raise WorkflowError("workflow configuration has an incompatible schema")
    if config["schema_version"] != SCHEMA_VERSION or config["routing"] != ROUTING:
        raise WorkflowError("workflow configuration has an unsupported schema version or routing")
    if not isinstance(config["enabled"], bool):
        raise WorkflowError("workflow configuration enabled must be boolean")
    if config["protocol_path"] != str(PROTOCOL_RELATIVE) or not _safe_relative(config["protocol_path"]):
        raise WorkflowError("workflow configuration protocol_path is incompatible or unsafe")
    if not isinstance(config["protocol_sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", config["protocol_sha256"]):
        raise WorkflowError("workflow configuration protocol_sha256 is invalid")
    if not isinstance(config["protocol_title"], str) or not config["protocol_title"].strip():
        raise WorkflowError("workflow configuration protocol_title is invalid")
    return config


def _config_bytes(protocol: bytes, enabled: bool) -> bytes:
    value = {
        "schema_version": SCHEMA_VERSION,
        "enabled": enabled,
        "routing": ROUTING,
        "protocol_path": str(PROTOCOL_RELATIVE),
        "protocol_sha256": _sha256(protocol),
        "protocol_title": _title(protocol),
    }
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _agents_state(path: Path) -> tuple[str, bytes]:
    if path.exists() or path.is_symlink():
        if path.is_symlink() or not path.is_file():
            raise WorkflowError(f"AGENTS.md must be a regular file when present: {path}")
        data = path.read_bytes()
    else:
        data = b""
    begins, ends = data.count(BEGIN.encode()), data.count(END.encode())
    if begins == 0 and ends == 0:
        return "absent", data
    if begins != 1 or ends != 1:
        raise WorkflowError("AGENTS.md has malformed or duplicate orchestration markers")
    start = data.find(BEGIN.encode())
    finish = data.find(END.encode(), start) + len(END.encode())
    # The leading and trailing newlines are part of the exact block, allowing
    # disable() to return every non-plugin byte unchanged.
    block_start = start - 1 if start > 0 and data[start - 1:start] == b"\n" else start
    block_end = finish + 1 if data[finish:finish + 1] == b"\n" else finish
    if data[block_start:block_end] != AGENTS_BLOCK:
        raise WorkflowError("orchestration block in AGENTS.md was modified; coordinate removal manually")
    return "exact", data


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _result_base(root: Path | None, *, requested_cwd: Path | None = None,
                 resolved_cwd: Path | None = None, check_status: str = "checked") -> dict[str, Any]:
    return {
        "operation_type": "workflow_check",
        "claude_started": False,
        "summary": "",
        "requested_cwd": str(requested_cwd) if requested_cwd else None,
        "resolved_cwd": str(resolved_cwd) if resolved_cwd else None,
        "check_status": check_status,
        "check_error": None,
        "adoption_status": "unknown",
        "adopted": None,
        "disabled": False,
        "ready": False,
        "project_root": str(root) if root else None,
        "config_path": str(root / CONFIG_RELATIVE) if root else None,
        "protocol_path": str(root / PROTOCOL_RELATIVE) if root else None,
        "protocol_sha256": None,
        "protocol_sha256_actual": None,
        "protocol_sha256_matches": False,
        "protocol_title": None,
        "block_present": False,
        "issues": [],
    }


def _check_error_code(error: WorkflowError) -> str:
    message = str(error).lower()
    if ("workflow configuration" in message or "malformed" in message
            or "incompatible schema" in message or "unsupported schema" in message):
        return "corrupt_config"
    if "does not exist" in message or "must be a directory" in message:
        return "invalid_path"
    if "symbolic" in message or "unsafe" in message or "traversal" in message:
        return "unsafe_path"
    return "inspection_error"


def _finish_summary(result: dict[str, Any]) -> dict[str, Any]:
    # Inspection explains the next valid operation; it never performs adoption.
    status = result["adoption_status"]
    target = result.get("project_root")
    can_enable = result["check_status"] == "checked" and status == "unadopted" and bool(target) and not result["issues"]
    reason = ("inspection_failed" if result["check_status"] != "checked" else
              "not_git_project" if not target else "explicitly_disabled" if status == "disabled" else
              "configuration_incomplete" if result["issues"] or status == "unknown" else
              "already_adopted" if status == "adopted" else "ready_to_enable")
    result["adoption_guidance"] = {"can_enable": can_enable, "target_cwd": target,
        "reason": reason, "next_action": {
            "inspection_failed": "先解决检查错误，再检查原目录；不要当成未采用。",
            "not_git_project": "当前目录不是可绑定的 Git 项目。进入本次任务的实际 Git 仓库后再检查；不要在此调用 enable。",
            "explicitly_disabled": "保留项目明确停用设置；只有用户明确重新启用才调用 enable。",
            "configuration_incomplete": "先核对已有配置及项目约定，不自动覆盖或重新启用。",
            "already_adopted": "沿用项目现有协作约定。",
            "ready_to_enable": "已获项目采用授权时，在 target_cwd 启用；未获授权时只报告状态。",
        }[reason]}
    if result["check_status"] == "check_failed":
        result["summary"] = "未能检查项目协作配置；尚未启动 Claude。"
    elif result["adoption_status"] == "unadopted":
        result["summary"] = ("已检查：当前目录不是可绑定的 Git 项目，需在实际仓库完成绑定；尚未启动 Claude。" if not target else
                             "已检查：项目尚未采用协作流程；尚未启动 Claude。")
    elif result["adoption_status"] == "disabled":
        result["summary"] = "已检查：项目协作流程已停用；尚未启动 Claude。"
    elif result["adoption_status"] == "adopted" and result["ready"]:
        result["summary"] = "已检查：项目协作流程已采用且就绪；尚未启动 Claude。"
    elif result["adoption_status"] == "adopted":
        result["summary"] = "已检查：项目已采用协作流程，但当前未就绪；尚未启动 Claude。"
    else:
        result["summary"] = "已检查：项目协作采用状态不完整，尚无法确认；尚未启动 Claude。"
    return result


def context(cwd: str) -> dict[str, Any]:
    """Inspect an existing project without creating or repairing anything."""
    requested: Path | None = None
    try:
        requested = _input_path(cwd)
        _, resolved = _inspection_input_directory(cwd)
    except WorkflowError as error:
        result = _result_base(None, requested_cwd=requested, check_status="check_failed")
        result["check_error"] = _check_error_code(error)
        result["issues"].append(str(error))
        return _finish_summary(result)
    root = _find_binding_root(resolved) or _find_git_root(resolved)
    if root is None:
        result = _result_base(None, requested_cwd=requested, resolved_cwd=resolved)
        result["adoption_status"] = "unadopted"
        result["adopted"] = False
        return _finish_summary(result)
    if _forbidden_root(root):
        error = WorkflowError("home, filesystem root, and global .codex directories cannot be adopted")
        result = _result_base(None, requested_cwd=requested, resolved_cwd=resolved, check_status="check_failed")
        result["check_error"] = _check_error_code(error)
        result["issues"].append(str(error))
        return _finish_summary(result)
    result = _result_base(root, requested_cwd=requested, resolved_cwd=resolved)
    try:
        config_path = _safe_project_path(root, CONFIG_RELATIVE)
        protocol_path = _safe_project_path(root, PROTOCOL_RELATIVE)
        agents_path = root / "AGENTS.md"
        if not config_path.exists() and not config_path.is_symlink():
            block_state, _ = _agents_state(agents_path)
            if block_state == "exact" or protocol_path.exists() or protocol_path.is_symlink():
                result["issues"].append("workflow configuration is missing from a partial adoption")
                result["adoption_status"] = "unknown"
            else:
                result["adoption_status"] = "unadopted"
                result["adopted"] = False
            return _finish_summary(result)
        config = _load_config(config_path)
        result["protocol_sha256"] = config["protocol_sha256"]
        result["protocol_title"] = config["protocol_title"]
        if not config["enabled"]:
            result["disabled"] = True
            result["adoption_status"] = "disabled"
            result["adopted"] = False
            return _finish_summary(result)
        if not protocol_path.exists() and not protocol_path.is_symlink():
            result["issues"].append("protocol copy is missing")
        else:
            actual = _sha256(_read_regular(protocol_path, "protocol copy"))
            result["protocol_sha256_actual"] = actual
            result["protocol_sha256_matches"] = actual == config["protocol_sha256"]
            if not result["protocol_sha256_matches"]:
                result["issues"].append("protocol copy sha256 differs from workflow configuration")
        state, _ = _agents_state(agents_path)
        result["block_present"] = state == "exact"
        if not result["block_present"]:
            result["issues"].append("orchestration block is missing from AGENTS.md")
        result["adoption_status"] = "adopted"
        result["adopted"] = True
        result["ready"] = bool(result["protocol_sha256_matches"] and result["block_present"])
    except WorkflowError as error:
        result["check_status"] = "check_failed"
        result["check_error"] = _check_error_code(error)
        result["adoption_status"] = "unknown"
        result["adopted"] = None
        result["ready"] = False
        result["issues"].append(str(error))
    return _finish_summary(result)


def enable(cwd: str, protocol_path: str | None = None) -> dict[str, Any]:
    """Adopt exactly one Git project after all safety checks have passed."""
    root = _require_project_root(cwd, exact=True)
    config_path = _safe_project_path(root, CONFIG_RELATIVE)
    target_protocol = _safe_project_path(root, PROTOCOL_RELATIVE)
    agents_path = root / "AGENTS.md"
    source, protocol = _protocol_source(root, protocol_path)
    state, agents = _agents_state(agents_path)

    config_exists = config_path.exists() or config_path.is_symlink()
    target_exists = target_protocol.exists() or target_protocol.is_symlink()
    if config_exists:
        config = _load_config(config_path)
        if not target_exists:
            raise WorkflowError("existing workflow configuration has no protocol copy; refusing to replace it")
        current_protocol = _read_regular(target_protocol, "protocol copy")
        if _sha256(current_protocol) != config["protocol_sha256"]:
            raise WorkflowError("existing protocol copy differs from workflow configuration; coordinate repair manually")
        if _sha256(protocol) != config["protocol_sha256"]:
            raise WorkflowError("a workflow is already configured with a different protocol; refusing to overwrite it")
    elif target_exists:
        raise WorkflowError("a protocol copy already exists without workflow configuration; refusing to overwrite it")

    # All reads and marker checks happen before the first replacement.  File
    # replacement is individually atomic; a cross-file transaction is not
    # available, and AGENTS.md is intentionally the last enable step.
    try:
        if not config_exists:
            _atomic_write(target_protocol, protocol)
        _atomic_write(config_path, _config_bytes(protocol, True))
        if state == "absent":
            _atomic_write(agents_path, agents + AGENTS_BLOCK)
    except OSError as error:
        raise WorkflowError(f"workflow enable did not finish; inspect project state before retrying: {error}") from error
    result = context(str(root))
    if not result["ready"]:
        raise WorkflowError("workflow enable did not produce a ready project; inspect project state before retrying")
    return result


def disable(cwd: str) -> dict[str, Any]:
    """Disable a valid adoption and remove only the exact, untouched block."""
    root = _require_project_root(cwd, exact=True)
    config_path = _safe_project_path(root, CONFIG_RELATIVE)
    if not (config_path.exists() or config_path.is_symlink()):
        raise WorkflowError("workflow configuration is missing")
    config = _load_config(config_path)
    agents_path = root / "AGENTS.md"
    state, agents = _agents_state(agents_path)
    protocol_path = _safe_project_path(root, PROTOCOL_RELATIVE)
    # Preserve the recorded protocol metadata.  disable never deletes it.
    if protocol_path.exists() or protocol_path.is_symlink():
        protocol = _read_regular(protocol_path, "protocol copy")
    else:
        # A missing protocol is already a broken adoption, but disabling it is
        # safe when the configuration itself remains structurally valid.
        protocol = b""
    if protocol and _sha256(protocol) != config["protocol_sha256"]:
        # Do not rewrite metadata over a drifted protocol; only flip enabled.
        config_data = dict(config)
        config_data["enabled"] = False
        config_bytes = (json.dumps(config_data, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    else:
        config_bytes = _config_bytes(protocol, False) if protocol else (json.dumps({**config, "enabled": False}, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    try:
        _atomic_write(config_path, config_bytes)
        if state == "exact":
            _atomic_write(agents_path, agents.replace(AGENTS_BLOCK, b"", 1))
    except OSError as error:
        raise WorkflowError(f"workflow disable did not finish; inspect project state before retrying: {error}") from error
    return context(str(root))


def _main() -> int:
    parser = argparse.ArgumentParser(description="Inspect or adopt a project-local Codex-Claude workflow")
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("inspect", "enable", "disable"):
        item = commands.add_parser(command)
        item.add_argument("--cwd", required=True)
        if command == "enable":
            item.add_argument("--protocol-path")
    args = parser.parse_args()
    try:
        if args.command == "inspect":
            result = context(args.cwd)
        elif args.command == "enable":
            result = enable(args.cwd, args.protocol_path)
        else:
            result = disable(args.cwd)
    except WorkflowError as error:
        print(json.dumps({"ready": False, "error": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
