"""Codex-facing MCP tools (official MCP Python SDK)."""
from __future__ import annotations

import argparse
import asyncio
from contextlib import asynccontextmanager
from functools import wraps
import json
import os
from pathlib import Path
import sys
import time
from typing import Any

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/codex-claude-orchestrator/scripts"))
import bridge  # noqa: E402
import cli_env  # noqa: E402
import model_catalog  # noqa: E402
from runtime import QUIET_KINDS, Runtime, resolve_context  # noqa: E402
from viewer import DEFAULT_PAGE_BYTES, Viewer, read_artifact  # noqa: E402

STATE_ROOT = Path(os.environ.get("CLAUDE_ORCHESTRATOR_STATE_DIR", str(Path.home() / ".codex/claude-orchestrator"))).expanduser().resolve()
runtime: Runtime | None = None
viewer: Viewer | None = None
COMPACT_FILES = 20


@asynccontextmanager
async def lifespan(_: Any):
    global runtime, viewer
    runtime = Runtime(STATE_ROOT)
    try:
        viewer = Viewer(runtime).start()
    except BaseException:
        runtime.close()
        raise
    try:
        yield {"runtime": runtime, "viewer": viewer}
    finally:
        try:
            await asyncio.to_thread(runtime.close)
        finally:
            await asyncio.to_thread(viewer.close)


mcp = MCPServer(
    "claude-orchestrator", version="1.1.1", lifespan=lifespan,
    instructions=("Delegate implement or analyze tasks to the local Claude CLI. Read the bundled "
                  "codex-claude-orchestrator skill first. Copy the user's words verbatim into user_messages and label "
                  "everything Codex adds with origin=coordinator. Pass the same context={chat} on every call of one "
                  "conversation. While a run is active, claude_wait returns Claude's questions and notes; answer with "
                  "claude_reply. Results are facts plus warnings; Codex verifies and records its verdict with claude_decide."),
)
READ = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True)


def expected_errors(fn: Any) -> Any:
    @wraps(fn)
    async def wrapped(*args: Any, **kwargs: Any) -> Any:
        try:
            return await fn(*args, **kwargs)
        except (ValueError, RuntimeError) as exc:
            raise ToolError(str(exc)) from exc
    return wrapped


def current() -> Runtime:
    if runtime is None:
        raise RuntimeError("Server is not ready")
    return runtime


def compact(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Shorter transport form; every artifact stays readable through claude_result."""
    result = dict(snapshot)
    outcome = dict(result.get("outcome") or {})
    if outcome:
        changes = outcome.get("changes")
        if isinstance(changes, dict) and len(changes.get("files") or []) > COMPACT_FILES:
            outcome["changes"] = {**changes, "files": changes["files"][:COMPACT_FILES], "files_truncated": True}
        outcome.pop("usage", None)
        outcome["warnings"] = [{**warning, "paths": (warning.get("paths") or [])[:10], "items": (warning.get("items") or [])[:10]}
                               for warning in outcome.get("warnings") or []]
        result["outcome"] = outcome
    result.pop("last_event", None)
    interactions = result.get("interactions")
    if isinstance(interactions, list):
        result["interactions"] = [item for item in interactions if item.get("status") == "pending"]
        result["interaction_count"] = len(interactions)
    return result


def user_summary(snapshot: dict[str, Any]) -> str:
    title = snapshot.get("title") or snapshot["task_id"]
    round_title = f"：{snapshot['round_title']}" if snapshot.get("round_title") else ""
    head = f"「{title}」第 {snapshot['round']} 轮{round_title}（{snapshot['kind']}/{snapshot['profile']}，{snapshot['run_id']}）"
    if snapshot["status"] == "running":
        pending = snapshot.get("pending_interactions") or 0
        return head + (f"：运行中，Claude 在等 Codex 回答（{pending} 个）。" if pending else "：运行中。")
    outcome = snapshot.get("outcome") or {}
    parts = [f"已结束：{snapshot['status']}"]
    if outcome.get("claimed_status"):
        parts.append(f"Claude 自称 {outcome['claimed_status']}")
    warnings = outcome.get("warnings") or []
    if warnings:
        parts.append(f"{len(warnings)} 条警告（{', '.join(sorted({w['code'] for w in warnings}))}）")
    decision = snapshot.get("decision")
    parts.append(f"Codex 裁决 {decision['verdict']} → {decision['next']}" if decision else "待 Codex 核验")
    return head + "：" + "；".join(parts) + "。"


async def workbench(context: dict[str, Any] | None, run_id: str = "", *, reserve: bool = False, reopen: bool = False,
                    outcome: str | None = None) -> dict[str, Any]:
    """The page link plus the plugin's open decision; only a new reservation may call open_in_codex."""
    decision = await asyncio.to_thread(current().workbench, context, reserve=reserve, reopen=reopen, outcome=outcome)
    try:
        url = viewer.url(run_id, current_context(context)) if viewer else None
    except RuntimeError:
        url = None
    return {"url": url, "open": decision}


def current_context(context: dict[str, Any] | None) -> dict[str, str] | None:
    return resolve_context(context) if isinstance(context, dict) else None


async def present(snapshot: dict[str, Any], *, brief: bool, context: dict[str, Any] | None = None,
                  reserve: bool = False) -> dict[str, Any]:
    result = compact(snapshot) if brief else dict(snapshot)
    result["user_summary"] = user_summary(snapshot)
    try:
        result["workbench"] = await workbench(context or snapshot.get("context"), snapshot["run_id"], reserve=reserve)
    except ValueError as exc:
        # A run recorded under another namespace stays readable; its page is just not opened from here.
        result["workbench"] = {"url": viewer.url(snapshot["run_id"]) if viewer else None,
                               "open": {"action": "link_only", "reason": str(exc)}}
    result["details_url"] = result["workbench"]["url"]
    return result


@mcp.tool(title="检查本机 Claude CLI", annotations=WRITE)
@expected_errors
async def claude_environment(cwd: str, verify: bool = False, model: str = "sonnet") -> dict:
    """Check the local Claude CLI, advertised flags, login state and whether the copy profile's macOS sandbox is available. verify=True makes one small paid request to confirm the login actually works; otherwise no model request is sent."""
    path = Path(os.path.expanduser(cwd))
    if not path.is_absolute() or not path.is_dir():
        raise ValueError("cwd must be an existing absolute directory")
    report = await asyncio.to_thread(cli_env.check, path, verify=verify, model=model)
    report["plugin_version"] = bridge.plugin_version()
    report["state_root"] = str(STATE_ROOT)
    return report


@mcp.tool(title="读取 Claude 模型目录", annotations=READ)
@expected_errors
async def claude_models(cwd: str) -> dict:
    """List the model selectors and effort options the local Claude CLI advertises. Sends no prompt; it does not prove account access."""
    return await asyncio.to_thread(model_catalog.collect, cwd)


@mcp.tool(title="派发 Claude 任务", annotations=WRITE)
@expected_errors
async def claude_start(packet: dict, compact: bool = True, open_workbench: bool = True) -> dict:
    """Start one delegated run and return immediately.

packet fields:
- task_id (string), kind: implement | analyze (analyze covers review, planning, research, questions)
- cwd: absolute directory. Git worktrees default to profile=copy (independent clone); other directories use profile=readonly. Both profiles use native full tools with auto permissions and protect original source from writes. implement requires copy.
- user_messages: [{text, source: human|relayed, at?}] — the user's own words, verbatim, every message still in force. Only when none exist send no_user_words_reason instead.
- inputs: [paths] of specs, plans, attachments (files or directories); they are frozen byte for byte at start.
- brief: Codex's own understanding (shown to Claude as possibly wrong).
- constraints / done_when: [{text, origin: user|doc|coordinator}] — label what Codex added as coordinator. Do not add compatibility or 'keep the old path' constraints the user did not ask for.
- focus: [strings] (analyze only), write_hint / protected: [repo-relative globs] used to classify the patch, verify: [suggested commands]
- web: false unless the user allowed network use; model, effort (low..max), timeout_seconds (required, 1..14400); max_budget_usd optional
- continue_from: the latest stopped run of the same task (same copy; user_messages must repeat earlier ones and append new ones).
- title (≤60 chars, one line): a short human title for the task; continuations keep it unless you send a new one. round_title (≤80): what this round changes. Display only; the user's words stay verbatim in user_messages.
- context: {chat, namespace?} — one stable key you keep for this Codex conversation; send it on every call. Without it the workbench is link-only.
- interactive (default true): Claude may ask blocking questions (answer with claude_reply) and leave nonblocking notes.
The answer's workbench.open.action is open_in_codex only for the chat's first request; otherwise show workbench.url as a link. A supervising subagent passes open_workbench=false."""
    snapshot = await asyncio.to_thread(current().start, packet)
    return await present(snapshot, brief=compact, context=packet.get("context") if isinstance(packet, dict) else None,
                         reserve=open_workbench)


@mcp.tool(title="查看 Claude 任务", annotations=READ)
@expected_errors
async def claude_status(run_id: str, compact: bool = True, context: dict | None = None) -> dict:
    """Return the run's state, outcome (run_outcome, claimed_status, warnings, changes), pending interactions and Codex decision, with the workbench link."""
    await asyncio.to_thread(current().authorize, run_id, context)
    return await present(await asyncio.to_thread(current().snapshot, run_id, context), brief=compact, context=context)


@mcp.tool(title="等待 Claude 进展", annotations=READ)
@expected_errors
async def claude_wait(run_id: str, after: int = 0, timeout_seconds: float = 25, compact: bool = True,
                      context: dict | None = None) -> dict:
    """Wait up to 25 seconds for Claude's questions or notes, other visible progress, or the end of the run; pass next_cursor back as after. reason is interaction | activity | terminal | timeout; pending lists blocking questions and permission requests to answer with claude_reply. Provider and tool counters never end a wait early."""
    if not 0 <= timeout_seconds <= 25 or after < 0:
        raise ValueError("timeout_seconds must be 0..25 and after non-negative")
    answer = await asyncio.to_thread(current().wait, run_id, after=after, timeout=timeout_seconds, context=context)
    if compact:
        answer["events"] = [event for event in answer["events"] if event.get("kind") not in QUIET_KINDS]
    answer["snapshot"] = await present(answer["snapshot"], brief=compact, context=context)
    return answer


@mcp.tool(title="回复 Claude", annotations=WRITE)
@expected_errors
async def claude_reply(run_id: str, command_id: str, kind: str, text: str | None = None, interaction_id: str | None = None,
                       decision: str | None = None, answers: dict | None = None, origin: str = "coordinator",
                       expected_revision: int | None = None, context: dict | None = None) -> dict:
    """Send Claude, while its run is active, an answer, update or interrupt. Returns a durable receipt at once; delivery.state moves queued → written → echoed (echo means the CLI received it, not that Claude acted on it).
- command_id: your own unique id; resending the same id with the same content returns the first receipt (also after the run ended, so a lost acknowledgment can be recovered); different content is refused.
- kind=answer: interaction_id from claude_wait pending; text answers every question, or answers maps each asked question text to its answer (every question needs one). A permission request needs decision=allow|deny, judged against the task's authorization. A question the CLI withdrew (after an interrupt) is cancelled and takes no answer.
- kind=update: new information mid-run without stopping Claude (it continues after its current step).
- kind=interrupt: stop the current step; text, if given, is sent right after as the redirect. Not proof that processes stopped; use claude_cancel to end the run.
- origin: coordinator for your own words, human only for the user's words copied verbatim.
- expected_revision: the revision you read; a stale one is refused so two answers cannot both win.
Answer only what the task's authorization covers; escalate core requirement or acceptance conflicts to the user and relay the user's words with origin=human."""
    return await asyncio.to_thread(current().reply, run_id, command_id, kind, text, interaction_id=interaction_id,
                                   decision=decision, answers=answers, origin=origin,
                                   expected_revision=expected_revision, context=context)


@mcp.tool(title="Claude 工作台", annotations=WRITE)
@expected_errors
async def claude_workbench(context: dict | None = None, reopen: bool = False, outcome: str | None = None) -> dict:
    """The chat's workbench link and open decision. Call open_in_codex only when open.action is open_in_codex. reopen=true only when the user asks to open the page again. outcome records what the host reported for the last open (queued | failed | unknown); no outcome ever triggers an automatic retry."""
    return await workbench(context, reserve=outcome is None, reopen=reopen, outcome=outcome)


@mcp.tool(title="读取运行产物", annotations=READ)
@expected_errors
async def claude_result(run_id: str, artifact: str = "report", offset: int = 0, limit: int = DEFAULT_PAGE_BYTES,
                        context: dict | None = None) -> dict:
    """Read one run artifact in UTF-8-safe byte pages (follow next_offset until end_of_artifact; check sha256). artifact: report, result, brief (exactly what Claude received), patch (this round), delivery_patch (everything not yet applied to the original), carried_patch, outcome, packet, plan, inputs, instruction_layer, final_message, decision, decision_history, execution, environment, command, stream, stderr, bridge_log."""
    await asyncio.to_thread(current().authorize, run_id, context)
    return await asyncio.to_thread(read_artifact, current(), run_id, artifact, offset=offset, limit=limit)


@mcp.tool(title="取消 Claude 任务", annotations=WRITE)
@expected_errors
async def claude_cancel(run_id: str, reason: str, context: dict | None = None) -> dict:
    """Ask an active run to stop. The bridge stops the Claude process group and still collects what was written; wait for the run to end before relying on it."""
    return await present(await asyncio.to_thread(current().cancel, run_id, reason, context), brief=True, context=context)


@mcp.tool(title="记录 Codex 裁决", annotations=WRITE)
@expected_errors
async def claude_decide(run_id: str, verdict: str, next: str, note: str, evidence: list[str],
                        item_decisions: list[dict] | None = None, protected_confirmed: list[str] | None = None,
                        applied: bool | None = None, compact: bool = True, context: dict | None = None) -> dict:
    """Record Codex's verification of an ended run (any outcome, once its process has stopped). verdict: accepted | accepted_with_corrections | rejected. next: done | next_round | codex_finishes. note says why; evidence lists what Codex actually checked. When result.json has items, item_decisions must decide each: {id, disposition: accepted|downgraded|rejected, reason}. Every protected path the round changed must be listed in protected_confirmed. Set applied=true after applying this (latest) round's delivery.patch to the original, even if you then corrected the code (omit applied to keep the value recorded earlier; false withdraws it): every round rebuilds its copy from the original and carries over only the latest delivery.patch not recorded as applied. The next round's brief includes these decisions. Deciding again appends to the history."""
    snapshot = await asyncio.to_thread(current().decide, run_id, verdict, next, note, evidence,
                                       item_decisions, protected_confirmed, applied, context)
    return await present(snapshot, brief=compact, context=context)


@mcp.tool(title="列出 Claude 任务", annotations=READ)
@expected_errors
async def claude_runs(limit: int = 20, offset: int = 0, task_id: str | None = None, context: dict | None = None) -> dict:
    """List recorded runs, newest first, optionally for one task_id; with context only this chat's runs. Use the run_id you started; never assume 'latest'."""
    if not 1 <= limit <= 200 or offset < 0:
        raise ValueError("limit must be 1..200 and offset non-negative")
    rows = await asyncio.to_thread(current().list_runs, limit + 1, offset, task_id, context)
    page = await workbench(context)
    return {"runs": rows[:limit], "next_offset": offset + min(limit, len(rows)), "has_more": len(rows) > limit,
            "workbench": page, "details_url": page["url"]}


@mcp.tool(title="删除任务副本", annotations=WRITE)
@expected_errors
async def claude_cleanup(run_id: str, context: dict | None = None) -> dict:
    """Delete the independent copy of a copy-profile task once its latest run is decided and every run of it has stopped. Patches, reports, briefs and decisions are kept; the task can no longer be continued."""
    await asyncio.to_thread(current().authorize, run_id, context)
    return await asyncio.to_thread(current().cleanup, run_id)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-root", type=Path)
    parser.add_argument("--dashboard", action="store_true", help="Serve the recorded runs read-only in the foreground")
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
