"""Codex-facing tools using the official MCP Python SDK (no custom protocol)."""
from __future__ import annotations

import argparse
import asyncio
from contextlib import suppress
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import sys
import time
from functools import wraps

from mcp.server import MCPServer
from mcp.types import ToolAnnotations
from mcp.server.mcpserver.exceptions import ToolError

ROOT = Path(__file__).resolve().parents[1]
BRIDGE_SCRIPTS = ROOT / "skills/codex-claude-orchestrator/scripts"
sys.path.insert(0, str(BRIDGE_SCRIPTS))
from runtime import Runtime
import bridge
import named_workflow
from viewer import Viewer, read_artifact
import workflow
import routing
import diagnostics
import cli_store
import cli_validation
import cli_updates
import model_catalog
from executable_locator import discover_system_claude, discover_external_claude, external_mode_requested
STATE_ROOT = Path(os.environ.get("CLAUDE_ORCHESTRATOR_STATE_DIR", str(Path.home() / ".codex/claude-orchestrator"))).expanduser().resolve()
runtime = None
viewer = None


async def maintain_supported_cli():
    reason = "startup"
    while True:
        try:
            await asyncio.to_thread(cli_updates.start, reason=reason)
        except (OSError, RuntimeError, ValueError):
            # Maintenance evidence explains failures; it must not take down
            # supervision of an already running Claude task.
            pass
        reason = "periodic"
        await asyncio.sleep(300)


@asynccontextmanager
async def lifespan(_):
    global runtime, viewer
    runtime = Runtime(STATE_ROOT)
    viewer = Viewer(runtime).start()
    maintenance = asyncio.create_task(maintain_supported_cli())
    try:
        yield {"runtime": runtime, "viewer": viewer}
    finally:
        maintenance.cancel()
        with suppress(asyncio.CancelledError):
            await maintenance
        await asyncio.to_thread(runtime.close)
        await asyncio.to_thread(viewer.close)


mcp = MCPServer(
    "claude-orchestrator", version="0.4.7", lifespan=lifespan,
    instructions="Use the codex-claude-orchestrator skill for natural task requests in adopted projects; users do not need tool commands. Read claude_workflow_context to recover the project workflow. A native Codex subagent can supervise independent Claude execution while the parent prepares verification; the parent owns scope and acceptance. Start with a versioned packet and file scope; keep run_id. Use claude_wait for incremental updates and claude_details for a compact live view. Use claude_environment for local preflight and claude_recovery/claude_reconcile for unknown-state recovery. Managed CLI maintenance follows its persisted bundled/latest channel. Official latest with auto_qualify explicitly enabled may run one bounded quota-consuming qualification per candidate and contract; status queries never start it. Use claude_models to discover CLI-advertised selectors and resolved models without a model prompt; forward official aliases for latest-in-family requests and preserve explicit model IDs. When cli_maintenance.notice_pending is true, explain its message, reason, current/target version and task impact to the user, then acknowledge that notice_id with claude_cli_update. Do not repeat ordinary up-to-date notices. Use claude_cli_status for progress and separate installation/login/compatibility facts; Explicit validate and opted-in auto_qualify consume provider quota; never imply model metadata is an actual successful call. Manual rollback pauses automatic maintenance until the user enables it again. Artifacts are explicit-file read-only reviews in non-Git directories. Only claude_decide after independent verification. Confirm the prior writer stopped before redirects. Project adoption does not approve a plan or deploy OS protection. Do not change login or bypass permissions.",
)
READ = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True)


def expected_errors(fn):
    @wraps(fn)
    async def wrapped(*args, **kwargs):
        try:
            return await fn(*args, **kwargs)
        except (ValueError, RuntimeError) as exc:
            raise ToolError(str(exc)) from exc
    return wrapped


def require_runtime():
    if runtime is None:
        raise RuntimeError("Server is not ready")
    return runtime


def checked(result, operation, summary):
    return {**result, "operation_type": operation, "claude_started": False, "summary": summary}


def compact_snapshot(snapshot):
    """Opt-in transport projection; full evidence remains available by artifact."""
    result = {key: value for key, value in snapshot.items() if key not in {"result", "decision_history"}}
    report = snapshot.get("result") or {}
    structured = report.get("structured") or report.get("structured_output") or {}
    summary = structured.get("summary") if isinstance(structured, dict) else None
    result["result_preview"] = {"available": bool(report), "summary": str(summary or "")[:600],
                                "truncated": len(str(summary or "")) > 600}
    result["decision_history_count"] = len(snapshot.get("decision_history") or [])
    result["response_detail"] = "compact"
    result["evidence_access"] = "claude_result(run_id, artifact='result'|'decision_history'|'receipt'|'workspace_before'|'workspace_after'); compact=False for full status"
    return result


async def decorate(snapshot, operation="execution_status", compact=False):
    started = snapshot.get("claude_started")
    execution = "Claude 已启动" if started is True else "Claude 尚未启动" if started is False else "Claude 是否已启动尚待执行证据确认"
    result = {**(compact_snapshot(snapshot) if compact else snapshot), "operation_type": operation, "task_created": bool(snapshot.get("run_id")),
              "user_summary": f"执行记录 {snapshot.get('run_id', '')}：{execution}；状态 {snapshot.get('status', 'unknown')}。结果需由 Codex 核验。",
              "cli_maintenance": await asyncio.to_thread(maintenance_status)}
    decision = snapshot.get("decision") or {}
    if isinstance(decision, dict) and decision.get("decision") in {"accepted", "returned"}:
        outcome = ("Claude 原报告未采纳，Codex 已补齐并记录完成" if decision.get("resolution") == "completed_by_codex" else
                   "Codex 已记录验收通过" if decision["decision"] == "accepted" else "报告已退回，等待修正")
        result["user_summary"] = f"执行记录 {snapshot.get('run_id', '')}：{outcome}。"
    if snapshot.get("superseded_by"):
        result["user_summary"] += " 本轮已有后续轮，仅作历史证据。"
    try:
        result["details_url"] = viewer.url(snapshot.get("run_id", ""))
    except (AttributeError, OSError, RuntimeError, ValueError):
        # Display failure must never turn a successful dispatch into a retry.
        result["details_url"] = None
        result["details_message"] = "详情入口暂不可用；继续按此 run_id 查询进度，勿重新派单。"
    result["presentation"] = {
        "run_id": snapshot.get("run_id"),
        "state": "available" if result["details_url"] else "unavailable",
        "link_label": "查看 Claude 执行详情",
        "host_open_state": "unobserved",
        "instruction": ("向用户提供可点击的 details_url，并尝试打开一次。宿主返回 queued 只表示排队，不能说已显示。"
                        if result["details_url"] else "继续查询原 run_id；连接恢复后用 claude_details 取得新链接，不能为显示重新派单。"),
    }
    return result


def maintenance_status():
    try:
        return cli_updates.status()
    except (OSError, RuntimeError, ValueError) as exc:
        return {"state": "unavailable", "message": "暂时无法读取 Claude 版本维护状态。", "error": str(exc)}


@mcp.tool(title="检查协作配置（不启动 Claude）", annotations=READ)
@expected_errors
async def claude_workflow_context(cwd: str) -> dict:
    """Read project adoption, protocol identity and issues before ordinary adopted-project tasks. No Claude request or project writes. This is not proof a plan/oracle was approved."""
    result = await asyncio.to_thread(workflow.context, cwd)
    result["cli_maintenance"] = await asyncio.to_thread(maintenance_status)
    return result


@mcp.tool(annotations=WRITE)
@expected_errors
async def claude_workflow_enable(cwd: str, protocol_path: str | None = None) -> dict:
    """One-time adoption when the user asks this Git project to use the workflow. Preserve existing AGENTS.md; add a bounded project entry and portable reference protocol. Uses project protocol if present, else bundled v2.9. Does not approve a plan, alter global configuration, log in, or run Claude."""
    return await asyncio.to_thread(workflow.enable, cwd, protocol_path)


@mcp.tool(annotations=WRITE)
@expected_errors
async def claude_workflow_disable(cwd: str) -> dict:
    """Disable project auto-adoption when requested, removing only an unmodified managed AGENTS block. Keep evidence. Does not cancel existing runs: their owner must cancel/wait first."""
    return await asyncio.to_thread(workflow.disable, cwd)


@mcp.tool(annotations=READ)
@expected_errors
async def claude_routing_policy(cwd: str) -> dict:
    """Read project auto/manual executor preference weights. Weights express task preference, not random allocation or measured model quality."""
    return await asyncio.to_thread(routing.get_policy, cwd)


@mcp.tool(annotations=WRITE)
@expected_errors
async def claude_routing_set(cwd: str, mode: str | None = None, codex_weight: int | None = None, claude_weight: int | None = None, preset: str | None = None) -> dict:
    """Change only specified project preferences. Use preset balanced/manual/claude_preferred OR individual fields. Omitted fields retain current values; 0 excludes automatic selection. Requires project adoption."""
    return await asyncio.to_thread(routing.set_policy, cwd, mode, codex_weight, claude_weight, preset)


@mcp.tool(title="选择任务执行者（不启动 Claude）", annotations=READ)
@expected_errors
async def claude_route(cwd: str, task_kind: str, explicit_executor: str | None = None, features: dict | None = None) -> dict:
    """Select using preference plus concrete features: scope_defined, independent, context_in_codex, requires_external_tools, claude_available, latency_sensitive (booleans), required_tools (names). Kinds include review, implementation, small_change, architecture, clarification, document_review, research, data_analysis. Explicit choice is preserved even if dispatch_status=blocked; never silently substitute or grant permission. Artifacts support read-only reviews only; Workflow remains explicit."""
    return await asyncio.to_thread(routing.select, cwd, task_kind, explicit_executor, features)


@mcp.tool(title="检查 Claude 环境（不发模型请求）", annotations=READ)
@expected_errors
async def claude_environment(cwd: str) -> dict:
    """Read local Claude installation, compatibility profile and masked login status; no model request. Prefer this for routine preflight. Use claude_doctor verify=True only when actual remote credential verification is needed."""
    path = Path(cwd)
    if not path.is_absolute() or not path.is_dir():
        raise ValueError("cwd must be an existing absolute directory")
    result = await asyncio.to_thread(bridge.check_environment, path, verify=False)
    result["cli_maintenance"] = await asyncio.to_thread(maintenance_status)
    return checked(result, "environment_check", "本次仅检查本地安装、登录配置与兼容能力，未启动 Claude 任务；" +
                   ("本地预检通过，远端调用另行核实。" if result.get("ready") else "环境尚未就绪，请查看具体状态与下一步。"))


@mcp.tool(annotations=READ)
@expected_errors
async def claude_diagnostics(cwd: str) -> dict:
    """Read a shareable local readiness summary: plugin/host/Claude versions, uv and authentication state, next steps. No account identity, credentials, prompts, task artifacts, telemetry or paid model request."""
    result = await asyncio.to_thread(diagnostics.collect, cwd, recent_runs=await asyncio.to_thread(require_runtime().list_runs, limit=50))
    return checked(result, "diagnostics", "本次仅生成环境诊断，未启动 Claude；历史调用记录不代表本次执行。")


@mcp.tool(title="读取 Claude 模型目录（不发模型请求）", annotations=READ)
@expected_errors
async def claude_models(cwd: str, identity_id: str | None = None) -> dict:
    """Discover model selectors, resolved model IDs and effort options from the selected Claude CLI's initialize metadata. Sends no user prompt or model generation request. Omit identity_id for current managed selection; use a retained identity to inspect a candidate or a capability fallback. Metadata is CLI-advertised availability, not verified account access. Pass official aliases unchanged for latest-in-family selection; respect explicit model IDs and provider overrides. Never infer the actual execution model from this catalog."""
    result = await asyncio.to_thread(model_catalog.collect, cwd, identity_id=identity_id)
    return {**result, "operation_type": "model_catalog",
            "user_summary": "本次启动所选 CLI 读取初始化模型目录，未发送用户提示词或模型生成请求；目录不证明账号实际调用成功。"}


@mcp.tool(annotations=READ)
@expected_errors
async def claude_cli_status(cwd: str, job_id: str | None = None) -> dict:
    """Read system CLI discovery, the immutable active execution selection, retained candidates and an optional validation job. Installation, login, latest real provider call and compatibility are reported independently; this performs no model request and a candidate never makes a healthy active CLI unavailable."""
    path = Path(cwd)
    if not path.is_absolute() or not path.is_dir():
        raise ValueError("cwd must be an existing absolute directory")
    recent_runs = await asyncio.to_thread(require_runtime().list_runs, limit=50)
    result = await asyncio.to_thread(diagnostics.collect, cwd, recent_runs=recent_runs, job_id=job_id)
    return checked(result, "cli_status", "本次仅查询执行版本和维护状态，未启动 Claude 任务。")


@mcp.tool(annotations=WRITE)
@expected_errors
async def claude_cli_update(action: str, candidate_path: str | None = None, identity_id: str | None = None,
                            job_id: str | None = None, model: str = "sonnet", groups: list[str] | None = None,
                            activate_on_success: bool = True, policy: str | None = None,
                            notice_id: str | None = None, channel: str | None = None,
                            auto_qualify: bool | None = None) -> dict:
    """Manage retained Claude CLI releases. policy automatic/manual resumes/pauses maintenance; optional channel bundled/latest chooses the installed release list or official latest discovery, and auto_qualify opts into bounded quota-consuming qualification of each new identity/contract once. Legacy settings retain bundled/no-auto-qualification. refresh starts background discovery/acquisition according to policy. Unknown candidates never activate without qualification; missing capability groups use eligible retained versions. acknowledge requires notice_id after explaining cause and impact. validate explicitly spends quota. activate/rollback pause automatic maintenance. Existing runs/resumes retain their CLI identity."""
    if action not in {"prepare", "validate", "activate", "rollback", "cancel", "refresh", "policy", "acknowledge"}:
        raise ValueError("action must be prepare, validate, activate, rollback, cancel, refresh, policy or acknowledge")
    if not isinstance(model, str) or not model.strip():
        raise ValueError("model must be a non-empty string")
    if not isinstance(activate_on_success, bool):
        raise ValueError("activate_on_success must be a boolean")
    if channel is not None and channel not in {"bundled", "latest"}:
        raise ValueError("channel must be bundled or latest")
    if auto_qualify is not None and type(auto_qualify) is not bool:
        raise ValueError("auto_qualify must be boolean")
    if action != "policy" and (channel is not None or auto_qualify is not None):
        raise ValueError("channel and auto_qualify apply only to policy")
    if groups is not None and (not isinstance(groups, list) or not all(isinstance(group, str) for group in groups)):
        raise ValueError("groups must be an array of strings when provided")
    if candidate_path is not None and (not isinstance(candidate_path, str) or not candidate_path
                                       or not Path(candidate_path).is_absolute()):
        raise ValueError("candidate_path must be a non-empty absolute executable path when provided")
    if identity_id is not None and (not isinstance(identity_id, str) or not identity_id):
        raise ValueError("identity_id must be a non-empty string when provided")
    if job_id is not None and (not isinstance(job_id, str) or not job_id):
        raise ValueError("job_id must be a non-empty string when provided")

    if action in {"refresh", "policy", "acknowledge"}:
        if any(value is not None for value in (candidate_path, identity_id, job_id, groups)):
            raise ValueError("maintenance actions do not accept candidate or validation arguments")
        if action == "refresh":
            if policy is not None or notice_id is not None:
                raise ValueError("refresh accepts no policy or notice_id; enable automatic policy separately")
            return await asyncio.to_thread(cli_updates.start, reason="user_refresh", force=True)
        if action == "policy":
            if policy not in {"automatic", "manual"} or notice_id is not None:
                raise ValueError("policy requires automatic or manual, without notice_id")
            options = {key: value for key, value in (("channel", channel), ("auto_qualify", auto_qualify)) if value is not None}
            result = await asyncio.to_thread(cli_updates.set_policy, policy, reason="user_request", **options)
            if policy == "automatic":
                result = await asyncio.to_thread(cli_updates.start, reason="policy_enabled", force=True)
            return result
        if policy is not None or not isinstance(notice_id, str) or not notice_id.strip():
            raise ValueError("acknowledge requires a non-empty notice_id only")
        return await asyncio.to_thread(cli_updates.acknowledge, notice_id)
    if policy is not None or notice_id is not None:
        raise ValueError("policy and notice_id apply only to their maintenance actions")

    if action == "prepare":
        if identity_id is not None or job_id is not None:
            raise ValueError("prepare accepts candidate_path only")
        return await asyncio.to_thread(cli_store.prepare, candidate_path)
    if action == "validate":
        if job_id is not None:
            raise ValueError("validate starts a new job; omit job_id")
        if identity_id is not None and candidate_path is not None:
            raise ValueError("validate accepts identity_id or candidate_path, not both")
        if identity_id is None:
            path = candidate_path
            if path is None:
                external = await asyncio.to_thread(discover_system_claude)
                path = external.get("path") if isinstance(external, dict) else None
            if not isinstance(path, str) or not path:
                raise RuntimeError("No external Claude executable is available to capture as a validation candidate")
            captured = await asyncio.to_thread(cli_store.capture, path)
            identity_id = captured.get("id")
        if not isinstance(identity_id, str) or not identity_id:
            raise RuntimeError("Candidate capture did not return an immutable identity")
        return await asyncio.to_thread(cli_validation.start, identity_id, model=model, groups=groups,
                                       activate_on_success=activate_on_success)
    if action == "activate":
        if not identity_id:
            raise ValueError("activate requires identity_id from claude_cli_status or prepare")
        if candidate_path is not None or job_id is not None:
            raise ValueError("activate accepts identity_id only")
        selection = await asyncio.to_thread(cli_store.get_selection)
        result = await asyncio.to_thread(cli_updates.activate_explicit, identity_id, expected_generation=selection["generation"])
        result["cli_maintenance"] = await asyncio.to_thread(maintenance_status)
        return result
    if action == "rollback":
        if candidate_path is not None or identity_id is not None or job_id is not None:
            raise ValueError("rollback does not accept candidate_path, identity_id or job_id")
        selection = await asyncio.to_thread(cli_store.get_selection)
        if not selection.get("previous"):
            raise RuntimeError("no retained previous CLI identity is available for rollback")
        result = await asyncio.to_thread(cli_updates.rollback_explicit, expected_generation=selection["generation"])
        result["cli_maintenance"] = await asyncio.to_thread(maintenance_status)
        return result
    if candidate_path is not None or identity_id is not None or not job_id:
        raise ValueError("cancel requires job_id only")
    return await asyncio.to_thread(cli_validation.cancel, job_id)


@mcp.tool(annotations=WRITE)
@expected_errors
async def claude_doctor(cwd: str, verify: bool = False, model: str = "sonnet") -> dict:
    """Check CLI installation and masked login state. verify=True makes one minimal paid/quota-consuming no-tool model request. Never installs or logs in."""
    path = Path(cwd)
    if not path.is_absolute() or not path.is_dir():
        raise ValueError("cwd must be an existing absolute directory")
    result = await asyncio.to_thread(bridge.check_environment, path, verify=verify, model=model, timeout=60)
    result.update(operation_type="connectivity_probe" if verify else "environment_check", claude_task_started=False,
                  summary="本次为在线连接验证，不创建业务执行任务。" if verify else "本次仅检查本地环境，未启动 Claude。")
    return result


@mcp.tool(annotations=READ)
@expected_errors
async def claude_saved_workflows(cwd: str) -> dict:
    """List effective saved Claude Workflows with name/path/sha256 for an explicit Workflow request. Never executes scripts or imports historical session scripts. Supported dispatch is read-only role=workflow_review, fresh only, with exact workflow identity and optional JSON args; this is not a full Dynamic Workflow Review implementation."""
    return {"workflows": await asyncio.to_thread(named_workflow.inventory, cwd), "supported_role": "workflow_review", "resume_supported": False}


@mcp.tool(title="创建 Claude 执行任务", annotations=WRITE)
@expected_errors
async def claude_start(packet: dict, timeout_seconds: float = 300, resume_run_id: str | None = None, compact: bool = False) -> dict:
    """Start one authorized Claude review, file-scoped edit, or explicitly requested saved read-only workflow_review. packet requires task_id, revision, role, cwd, objective, requirement_sources, constraints, acceptance, owned_files, protected_files, model, effort. workflow_review also requires inventory-bound workflow={name,path,sha256,args?} and forbids resume. Optional budget sets max_turns/max_budget_usd. workspace_kind defaults git; artifacts requires a non-Git cwd, role=review, explicit input_files and requirement_sources, no owned files or resume/protocol/workflow. For Git ordinary roles, resume_run_id is an exact successful prior run for a local correction. Fresh redirects use a higher revision. timeout_seconds defaults to 300 (5 minutes), is an execution limit distinct from wait timeout, and can be set explicitly up to 14400; choose a limit appropriate to the scoped task. Returns promptly; wait and verify the result."""
    if not 1 <= timeout_seconds <= 14400:
        raise ValueError("timeout_seconds must be between 1 and 14400")
    if not isinstance(packet, dict):
        raise ValueError("packet must be an object")
    try:
        maintenance = await asyncio.to_thread(cli_updates.start, reason="dispatch")
    except (OSError, RuntimeError, ValueError):
        # Maintenance cannot make an otherwise qualified pinned run unavailable.
        maintenance = await asyncio.to_thread(maintenance_status)
    selection = await asyncio.to_thread(cli_store.get_selection)
    if (resume_run_id is None and maintenance.get("policy") == "automatic" and not selection.get("active")
            and selection.get("mode") != "external" and not external_mode_requested()):
        external = await asyncio.to_thread(discover_external_claude)
        if not external.get("path"):
            raise RuntimeError(str(maintenance.get("message") or "正在准备受支持的 Claude 执行版本。")
                               + " 请用 claude_cli_status 查看准备进度；本次尚未派单。")
    if packet.get("workspace_kind", "git") == "git":
        cwd_value = packet.get("cwd")
        if not isinstance(cwd_value, str) or not Path(cwd_value).is_absolute() or not Path(cwd_value).is_dir():
            raise ValueError("cwd must be an existing absolute directory")
        try:
            await asyncio.to_thread(bridge.git_head, Path(cwd_value))
        except bridge.BridgeError as exc:
            raise ValueError("workspace_kind=git requires a Git working tree. For a plain document directory, use workspace_kind=artifacts, role=review, and explicit input_files and requirement_sources.") from exc
        context = await asyncio.to_thread(workflow.context, packet.get("cwd", ""))
        if context.get("issues"):
            raise ValueError("Project workflow needs reconciliation: " + "; ".join(context["issues"]))
        if context.get("check_status") == "check_failed":
            raise ValueError(context.get("summary") or "Project workflow inspection failed; Claude was not started")
        packet = dict(packet)
        packet["cwd"] = context.get("resolved_cwd") or str(Path(cwd_value).resolve())
        if context.get("adopted"):
            if not context.get("ready"):
                raise ValueError("Adopted project workflow is not ready")
            packet = dict(packet)
            packet["project_workflow"] = {key: context[key] for key in ("config_path", "protocol_path", "protocol_sha256")}
            packet["requirement_sources"] = list(dict.fromkeys(packet.get("requirement_sources", []) + [context["config_path"], context["protocol_path"]]))
    return await decorate(await asyncio.to_thread(require_runtime().start, packet, timeout=timeout_seconds, resume_run_id=resume_run_id), "dispatch", compact=compact)


@mcp.tool(title="查询 Claude 执行状态", annotations=READ)
@expected_errors
async def claude_status(run_id: str, compact: bool = False) -> dict:
    """Read execution state, identity and coordinator decision. Use compact=True for routine progress; full result/history remain available through claude_result or compact=False."""
    return await decorate(await asyncio.to_thread(require_runtime().snapshot, run_id), compact=compact)


@mcp.tool(annotations=READ)
@expected_errors
async def claude_runs(limit: int = 50, offset: int = 0) -> dict:
    """List persisted run summaries. Use a specific run_id for details; do not resume 'latest'."""
    if not 1 <= limit <= 200 or offset < 0:
        raise ValueError("limit must be 1..200 and offset non-negative")
    snapshots = await asyncio.to_thread(require_runtime().list_runs, limit=limit + 1, offset=offset)
    runs = [{k: v for k, v in row.items() if k not in {"result", "receipt"}} for row in snapshots]
    return {"runs": runs[:limit], "next_offset": offset + min(limit, len(runs)), "has_more": len(runs) > limit, "details_url": viewer.url()}


@mcp.tool(title="等待 Claude 公开活动", annotations=READ)
@expected_errors
async def claude_wait(run_id: str, after: int = 0, timeout_seconds: float = 25, compact: bool = False) -> dict:
    """Wait up to 25 seconds for new events or completion, returning only the requested delta and next_cursor. Reuse next_cursor; do not busy-poll. Ordinary tool results provide progress even on hosts that ignore MCP progress notifications."""
    if not 0 <= timeout_seconds <= 25 or after < 0:
        raise ValueError("Invalid cursor or wait timeout")
    result = await asyncio.to_thread(require_runtime().wait, run_id, after=after, timeout=timeout_seconds)
    if compact:
        result["snapshot"] = compact_snapshot(result["snapshot"])
        public_events = result.get("events", [])
        result["events"] = [event for event in public_events if event.get("kind") != "provider"]
        result["provider_events_omitted"] = len(public_events) - len(result["events"])
    display = await decorate(result["snapshot"], "execution_progress")
    result.update({key: display[key] for key in ("details_url", "presentation", "cli_maintenance", "operation_type", "user_summary")})
    if display.get("details_message"):
        result["details_message"] = display["details_message"]
    return result


@mcp.tool(annotations=READ)
@expected_errors
async def claude_events(run_id: str, after: int = 0, limit: int = 100) -> dict:
    """Read actual public execution activities by cursor. Excludes private thinking blocks and raw credential material."""
    if after < 0 or not 1 <= limit <= 200:
        raise ValueError("Invalid cursor or limit")
    return await asyncio.to_thread(require_runtime().events, run_id, after=after, limit=limit)


@mcp.tool(title="查看 Claude 执行详情", annotations=READ)
@expected_errors
async def claude_details(run_id: str, compact: bool = False) -> dict:
    """Return the same run's read-only live details URL. Always give the user a clickable link and open it once with open_in_codex. Its queued result means queued, not displayed. Unavailable details never require another dispatch; reconnect then request this run_id again."""
    return await decorate(await asyncio.to_thread(require_runtime().snapshot, run_id), compact=compact)


@mcp.tool(annotations=READ)
@expected_errors
async def claude_result(run_id: str, artifact: str = "result") -> dict:
    """Inspect recorded evidence: result, receipt, packet, diff, environment, decision, decision_history, git_before, git_after, workspace_before, workspace_after or reconciliation. Diff is the recorded working-tree comparison, not proof of accepted work."""
    return await asyncio.to_thread(read_artifact, require_runtime(), run_id, artifact)


@mcp.tool(annotations=WRITE)
@expected_errors
async def claude_cancel(run_id: str, reason: str) -> dict:
    """Request cancellation and inspect its confirmation state. A request alone is not proof the Claude writer stopped; wait for a terminal receipt before handing off files."""
    if not reason.strip():
        raise ValueError("A cancellation reason is required")
    return await decorate(await asyncio.to_thread(require_runtime().cancel, run_id, reason))


@mcp.tool(annotations=READ)
@expected_errors
async def claude_recovery(run_id: str) -> dict:
    """Inspect an unknown run's process ownership and current workspace without restarting it. Gives concrete blockers and a workspace digest for evidence-based reconciliation. Unknown never implies stopped."""
    return await asyncio.to_thread(require_runtime().inspect_recovery, run_id)


@mcp.tool(annotations=WRITE)
@expected_errors
async def claude_reconcile(run_id: str, reason: str, evidence: list[str], expected_workspace_digest: str) -> dict:
    """Record coordinator reconciliation after inspecting claude_recovery and actual workspace changes. Requires confirmed stopped process groups, free lane and matching inspected digest. Preserve unknown history; permit a new fresh revision only. Never accepts old work, resumes a session, kills an unowned process or auto-retries."""
    return await asyncio.to_thread(require_runtime().reconcile, run_id, reason, evidence, expected_workspace_digest)


@mcp.tool(annotations=WRITE)
@expected_errors
async def claude_decide(run_id: str, decision: str, reason: str, evidence: list[str],
                        resolution: str | None = None, completion_summary: str | None = None, compact: bool = False) -> dict:
    """Record independent Codex verification, not automatic validation. accepted accepts the Claude report. returned defaults to revision_requested; only after Codex actually completes and verifies the task use resolution='completed_by_codex' with completion_summary and evidence. The original report stays returned. compact=True omits repeated report/history bodies."""
    if not reason.strip() or not evidence or decision not in {"accepted", "returned"}:
        raise ValueError("Use accepted/returned with a reason and actual evidence references")
    snapshot = await asyncio.to_thread(require_runtime().record_decision, run_id, decision, reason, evidence, resolution, completion_summary)
    return await decorate(snapshot, "coordinator_decision", compact=compact)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-root", type=Path)
    parser.add_argument("--dashboard", action="store_true", help="Serve the persisted records read-only in a managed foreground process")
    args = parser.parse_args()
    global STATE_ROOT
    if args.state_root:
        STATE_ROOT = args.state_root.expanduser().resolve()
    if args.dashboard:
        rt = Runtime(STATE_ROOT)
        view = Viewer(rt).start()
        print(json.dumps({"details_url": view.url(), "state_root": str(STATE_ROOT)}), flush=True)
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        finally:
            view.close()
            rt.close()
    else:
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
