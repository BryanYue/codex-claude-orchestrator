"""Project-local, explainable executor preferences.

Routing only selects a preferred executor.  It never starts Claude, grants a
permission, changes a model, or decides that a task is ready to run.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import tempfile
from typing import Any

import workflow


SCHEMA_VERSION = 1
ROUTING_RELATIVE = Path(".agents/codex-claude/routing.json")
TASK_KINDS = {"clarification", "small_change", "implementation", "review", "architecture", "document_review", "research", "data_analysis"}
EXECUTORS = {"codex", "claude", "claude_workflow"}
DEFAULT_POLICY = {
    "schema_version": SCHEMA_VERSION,
    "mode": "auto",
    "weights": {"codex": 50, "claude": 50},
}
_BONUSES = {
    "clarification": {"codex": 20, "claude": 0},
    "small_change": {"codex": 20, "claude": 0},
    "implementation": {"codex": 0, "claude": 20},
    "review": {"codex": 0, "claude": 20},
    "architecture": {"codex": 20, "claude": 5},
    "document_review": {"codex": 0, "claude": 20},
    "research": {"codex": 20, "claude": 0},
    "data_analysis": {"codex": 20, "claude": 0},
}
PRESETS = {
    "balanced": {"mode": "auto", "codex_weight": 50, "claude_weight": 50},
    "manual": {"mode": "manual"},
    "claude_preferred": {"mode": "auto", "codex_weight": 40, "claude_weight": 70},
}


class RoutingError(ValueError):
    """The local preference file cannot be read or changed safely."""


def _copy_default() -> dict[str, Any]:
    return {"schema_version": 1, "mode": "auto", "weights": {"codex": 50, "claude": 50}}


def _validate_policy(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {"schema_version", "mode", "weights"}:
        raise RoutingError("routing configuration has an incompatible schema")
    if value["schema_version"] != SCHEMA_VERSION:
        raise RoutingError("routing configuration has an unsupported schema version")
    if value["mode"] not in {"auto", "manual"}:
        raise RoutingError("routing mode must be 'auto' or 'manual'")
    weights = value["weights"]
    if not isinstance(weights, dict) or set(weights) != {"codex", "claude"}:
        raise RoutingError("routing weights must contain exactly codex and claude")
    for name, weight in weights.items():
        if isinstance(weight, bool) or not isinstance(weight, int) or not 0 <= weight <= 100:
            raise RoutingError(f"routing weight for {name} must be an integer from 0 to 100")
    if weights["codex"] == 0 and weights["claude"] == 0:
        raise RoutingError("routing weights may not both be zero")
    return {
        "schema_version": SCHEMA_VERSION,
        "mode": value["mode"],
        "weights": {"codex": weights["codex"], "claude": weights["claude"]},
    }


def _safe_routing_path(root: Path) -> Path:
    current = root
    for part in ROUTING_RELATIVE.parts:
        current = current / part
        if current.is_symlink():
            raise RoutingError(f"symbolic links are not allowed in routing paths: {current}")
    return root / ROUTING_RELATIVE


def _read_policy(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise RoutingError(f"routing configuration must be a regular file: {path}")
    try:
        raw = path.read_text(encoding="utf-8")
        value = json.loads(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RoutingError(f"routing configuration is malformed: {path}") from error
    return _validate_policy(value)


def _atomic_write_preserving_permissions(path: Path, data: bytes) -> None:
    if not path.parent.is_dir() or path.parent.is_symlink():
        raise RoutingError(f"routing directory is unavailable or unsafe: {path.parent}")
    old_mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else None
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if old_mode is not None:
            os.chmod(temporary, old_mode)
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _context(cwd: str) -> tuple[dict[str, Any], Path | None]:
    current = workflow.context(cwd)
    root_text = current.get("project_root")
    if not isinstance(root_text, str) or not root_text:
        return current, None
    return current, Path(root_text)


def _require_exact_ready_root(cwd: str) -> tuple[dict[str, Any], Path]:
    current, root = _context(cwd)
    if not current.get("ready") or root is None:
        detail = "; ".join(str(item) for item in current.get("issues", [])) or "workflow adoption is not ready"
        raise RoutingError(f"routing can only be set in a ready adopted project: {detail}")
    try:
        candidate = workflow._safe_input_directory(cwd)
    except workflow.WorkflowError as error:
        raise RoutingError(str(error)) from error
    if not candidate.is_dir() or candidate != root:
        raise RoutingError(f"cwd must be the explicit adopted project root: {root}")
    return current, root


def get_policy(cwd: str) -> dict[str, Any]:
    """Return a project policy without treating a failed workflow check as unadopted."""
    current, root = _context(cwd)
    result = {
        "operation_type": "routing",
        "claude_started": False,
        "summary": "",
        "requested_cwd": current.get("requested_cwd"),
        "resolved_cwd": current.get("resolved_cwd"),
        "project_root": str(root) if root else None,
        "check_status": current.get("check_status"),
        "check_error": current.get("check_error"),
        "adoption_status": current.get("adoption_status"),
        "adopted": current.get("adopted"),
        "ready": bool(current.get("ready")),
        "policy_path": str(root / ROUTING_RELATIVE) if root else None,
        "policy": _copy_default(),
        "source": "default",
        "issues": list(current.get("issues", [])),
    }
    if current.get("check_status") == "check_failed":
        result["source"] = "unavailable"
        result["summary"] = "未能完成项目协作路由检查；尚未启动 Claude。"
        return result
    if not current.get("ready") or root is None:
        result["summary"] = "已完成路由读取；尚未启动 Claude。"
        return result
    path = _safe_routing_path(root)
    if not path.exists() and not path.is_symlink():
        return result
    result["policy"] = _read_policy(path)
    result["source"] = "project"
    result["summary"] = "已完成项目路由读取；尚未启动 Claude。"
    return result


def set_policy(cwd: str, mode: str | None = None, codex_weight: int | None = None,
               claude_weight: int | None = None, preset: str | None = None) -> dict[str, Any]:
    """Persist an explicit preference for a ready, explicitly named project root."""
    _, root = _require_exact_ready_root(cwd)
    if preset is not None and preset not in PRESETS:
        raise RoutingError("preset must be balanced, manual or claude_preferred")
    if preset and any(v is not None for v in (mode, codex_weight, claude_weight)):
        raise RoutingError("use either a preset or explicit preference fields")
    old = get_policy(cwd)["policy"]
    requested = PRESETS.get(preset, {})
    mode = requested.get("mode", mode)
    mode = old["mode"] if mode is None else mode
    codex_weight = requested.get("codex_weight", codex_weight)
    claude_weight = requested.get("claude_weight", claude_weight)
    policy = _validate_policy({
        "schema_version": SCHEMA_VERSION,
        "mode": mode,
        "weights": {"codex": old["weights"]["codex"] if codex_weight is None else codex_weight,
                    "claude": old["weights"]["claude"] if claude_weight is None else claude_weight},
    })
    path = _safe_routing_path(root)
    if path.exists() or path.is_symlink():
        # Refuse malformed/incompatible state instead of silently overwriting it.
        _read_policy(path)
    data = (json.dumps(policy, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    try:
        _atomic_write_preserving_permissions(path, data)
    except OSError as error:
        raise RoutingError(f"routing policy was not written; inspect project state before retrying: {error}") from error
    return get_policy(str(root))


def _scores(policy: dict[str, Any], task_kind: str) -> dict[str, int]:
    weights = policy["weights"]
    return {
        executor: 0 if weights[executor] == 0 else weights[executor] + _BONUSES[task_kind][executor]
        for executor in ("codex", "claude")
    }


def select(cwd: str, task_kind: str, explicit_executor: str | None = None,
           features: dict[str, Any] | None = None) -> dict[str, Any]:
    """Select a preference only; execution, authorization, and readiness stay external."""
    if task_kind not in TASK_KINDS:
        raise RoutingError(f"unsupported task_kind: {task_kind}")
    if explicit_executor is not None and explicit_executor not in EXECUTORS:
        raise RoutingError("explicit_executor must be codex, claude, or claude_workflow")
    if not isinstance(cwd, str) or not cwd or not Path(cwd).expanduser().is_absolute():
        raise RoutingError("cwd must be an absolute directory")
    if not isinstance(features, (dict, type(None))):
        raise RoutingError("features must be an object")
    flags = dict(features or {})
    booleans = {"scope_defined", "independent", "context_in_codex", "requires_external_tools", "claude_available", "latency_sensitive"}
    if set(flags) - booleans - {"required_tools", "review_mode"}:
        raise RoutingError("unknown task feature")
    if any(not isinstance(flags[k], bool) for k in booleans & flags.keys()):
        raise RoutingError("task feature flags must be booleans")
    required = flags.get("required_tools", [])
    if not isinstance(required, list) or not all(isinstance(x, str) for x in required):
        raise RoutingError("required_tools must be a list of tool names")
    if "review_mode" in flags and (not isinstance(flags["review_mode"], str) or flags["review_mode"] not in {"strict", "isolated"}):
        raise RoutingError("review_mode must be strict or isolated")
    state = get_policy(cwd)
    policy = state["policy"]
    scores = _scores(policy, task_kind)
    if state["check_status"] == "check_failed":
        reasons = ["项目协作配置检查失败：" + "; ".join(state["issues"])]
        return {
            "operation_type": "routing",
            "claude_started": False,
            "summary": "未能完成项目协作路由检查；尚未启动 Claude。",
            "requested_cwd": state["requested_cwd"],
            "resolved_cwd": state["resolved_cwd"],
            "project_root": state["project_root"],
            "check_status": state["check_status"],
            "check_error": state["check_error"],
            "adoption_status": state["adoption_status"],
            "adopted": state["adopted"],
            "executor": None,
            "source": "unavailable",
            "scores": scores,
            "rationale": reasons[0],
            "policy": policy,
            "workspace_kind": "unknown",
            "features": flags,
            "factors": [],
            "claude_eligible": False,
            "blocking_reasons": reasons,
            "dispatch_status": "blocked",
            "execution_authorized": False,
            "needs_workflow_name": False,
        }
    workspace_kind = "git" if state["project_root"] else "artifacts"
    issues, factors = [], []
    if workspace_kind == "git" and state["issues"]:
        issues.append("项目协作配置需要核对：" + "; ".join(state["issues"]))
    if workspace_kind == "artifacts" and task_kind not in {"review", "document_review"}:
        issues.append("普通资料文件夹目前只支持明确文件清单的只读审校")
    if workspace_kind == "artifacts" and explicit_executor == "claude_workflow":
        issues.append("保存 Workflow 目前要求 Git 工作区")
    review_mode = flags.get("review_mode", "isolated" if workspace_kind == "git" else "strict")
    isolated_review = workspace_kind == "git" and task_kind in {"review", "document_review"} and review_mode == "isolated"
    if workspace_kind == "artifacts" and review_mode == "isolated":
        issues.append("普通资料文件夹目前只支持 strict 审校")
    allowed = {"Read", "Glob", "Grep"}
    if explicit_executor == "claude_workflow":
        allowed.add("Workflow")
        if task_kind not in {"review", "document_review"}:
            issues.append("保存 Workflow 目前仅支持显式只读审查")
    if task_kind in {"implementation", "small_change"} and workspace_kind == "git":
        allowed |= {"Edit", "Write"}
    # Isolated reviews expose the CLI's default built-ins, including tools added
    # by newer CLI releases. External MCP remains disabled at dispatch.
    missing_tools = {name for name in required if name.startswith("mcp__")} if isolated_review else set(required) - allowed
    if flags.get("requires_external_tools") or missing_tools:
        issues.append("任务需要当前 Claude 执行席未提供的工具，应由 Codex 使用对应能力处理")
    if flags.get("claude_available") is False:
        issues.append("当前 Claude 环境不可用，先完成环境检查中的下一步")
    if flags.get("scope_defined") is False:
        issues.append("目标或文件范围尚未明确，先由 Codex 补齐")
    for name, executor, bonus, explanation in (
        ("independent", "claude", 20, "该任务可独立完成，有交叉检查或并行收益"),
        ("context_in_codex", "codex", 40, "关键上下文已在 Codex，继续处理可减少交接"),
        ("latency_sensitive", "codex", 35, "需要快速反馈，优先减少额外执行启动"),
    ):
        if flags.get(name):
            if policy["weights"][executor] > 0:
                scores[executor] += bonus
            factors.append(explanation)
    if explicit_executor is not None:
        executor = "claude" if explicit_executor == "claude_workflow" and isolated_review else explicit_executor
        source = "manual_override"
        rationale = "本次按你的明确指定选择执行者，项目默认偏好保持不变。"
    elif policy["mode"] == "manual":
        executor = "codex"
        source = "policy"
        rationale = "项目使用仅手动模式，本次由 Codex 处理。"
    else:
        executor = "claude" if not issues and scores["claude"] > scores["codex"] else "codex"
        if executor == "codex" and policy["weights"]["codex"] == 0:
            executor = None
            issues.append("没有可用的自动执行者；请补齐 Claude 条件或手动指定执行者")
        source = "policy" if state["source"] == "project" else "default"
        rationale = issues[0] if issues else "；".join(factors) if factors else "根据任务类型和项目偏好选择" + (" Claude" if executor == "claude" else " Codex")
    result = {
        "operation_type": "routing",
        "claude_started": False,
        "summary": "已完成执行者路由选择；尚未启动 Claude。",
        "requested_cwd": state["requested_cwd"],
        "resolved_cwd": state["resolved_cwd"],
        "project_root": state["project_root"],
        "check_status": state["check_status"],
        "check_error": state["check_error"],
        "adoption_status": state["adoption_status"],
        "adopted": state["adopted"],
        "executor": executor,
        "source": source,
        "scores": scores,
        "rationale": rationale,
        "policy": policy,
        "workspace_kind": workspace_kind,
        "features": flags,
        "factors": factors,
        "claude_eligible": not issues,
        "blocking_reasons": issues,
        "dispatch_status": "blocked" if executor is None or executor.startswith("claude") and issues else "preflight_required" if executor.startswith("claude") else "selected",
        "execution_authorized": False,
        "needs_workflow_name": executor == "claude_workflow",
    }
    if task_kind in {"review", "document_review"}:
        result["suggested_packet"] = {"role": "review", "review_mode": review_mode}
    if explicit_executor == "claude_workflow" and isolated_review:
        result["suggested_workflow"] = {"tool": "Workflow", "selection": "provider", "inventory_binding_required": False}
        result["deprecated_role"] = "workflow_review"
    elif executor == "claude_workflow":
        result["deprecated_role"] = "workflow_review"
    if issues:
        result["summary"] = "路由已完成，但存在派单阻断条件；尚未启动 Claude。"
    return result
