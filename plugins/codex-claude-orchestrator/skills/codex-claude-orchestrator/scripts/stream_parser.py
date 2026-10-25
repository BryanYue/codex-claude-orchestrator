"""Facts from the Claude CLI stream-json output; no process or workspace access.

Only a parent ``type=result`` event is the final result.  Everything else is
observation for warnings: models, usage, permission denials, which files were
read, and whether background Workflows that were launched also finished, plus
the public tool steps shown as run activity.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Iterable, Sequence

DENIAL_TEXT_LIMIT = 500
WORKFLOW_DONE = frozenset({"completed", "failed", "error", "killed", "stopped", "cancelled", "timeout"})
READ_TOOLS = frozenset({"Read", "NotebookRead"})
STEP_TARGET_KEYS = ("file_path", "notebook_path", "path")
# Commands, URLs, search text and file contents can carry secrets, so a step
# only names the tool, its target path and a label the model wrote for people.
STEP_LABEL_KEYS = {"Bash": "description", "Agent": "description", "Task": "description", "Skill": "skill",
                   "Workflow": "name", "Glob": "pattern"}


def _blocks(event: dict[str, Any]) -> list[dict[str, Any]]:
    message = event.get("message")
    content = message.get("content") if isinstance(message, dict) else event.get("content")
    return [block for block in content if isinstance(block, dict)] if isinstance(content, list) else []


def _denials(event: dict[str, Any]) -> list[dict[str, Any]]:
    raw = event.get("permission_denials")
    entries = []
    for item in raw if isinstance(raw, list) else []:
        if isinstance(item, dict):
            raw_input = item.get("tool_input")
            tool_input: dict[str, Any] = raw_input if isinstance(raw_input, dict) else {}
            entry = {key: str(item[key])[:DENIAL_TEXT_LIMIT] for key in ("tool_name", "tool_use_id") if key in item}
            target = next((tool_input[key] for key in ("file_path", "path", "command", "url", "pattern")
                           if isinstance(tool_input.get(key), str)), None)
            if target:
                entry["target"] = target[:DENIAL_TEXT_LIMIT]
            entries.append(entry)
    return entries


def parse_lines(lines: Iterable[str], expected_session: str | None = None) -> dict[str, Any]:
    facts: dict[str, Any] = {
        "init": None, "session_id": None, "final": None, "result_events": 0, "models": [],
        "provider_response_observed": False, "denials": [], "tool_counts": {}, "read_paths": [],
        "workflows": {}, "session_mismatch": False, "inits": 0, "turns": [],
    }
    reads: set[str] = set()
    workflow_tools: set[str] = set()
    workflow_tasks: dict[str, str] = {}
    for line in lines:
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        kind = event.get("type")
        session = event.get("session_id")
        if kind == "system" and event.get("subtype") == "init":
            # A duplex session emits one init per turn; the first describes the session.
            facts["inits"] += 1
            if facts["init"] is None:
                facts["init"] = {key: event.get(key) for key in ("model", "cwd", "tools", "plugins", "memory_paths",
                                                                 "claude_code_version", "permissionMode", "skills")}
                facts["session_id"] = session
        if expected_session and isinstance(session, str) and session != expected_session:
            facts["session_mismatch"] = True
        if kind == "assistant":
            facts["provider_response_observed"] = True
            message = event.get("message")
            model = message.get("model") if isinstance(message, dict) else None
            if isinstance(model, str) and model and model not in facts["models"]:
                facts["models"].append(model)
        for block in _blocks(event):
            if block.get("type") != "tool_use" or not isinstance(block.get("name"), str):
                continue
            name = block["name"]
            facts["tool_counts"][name] = facts["tool_counts"].get(name, 0) + 1
            raw_input = block.get("input")
            tool_input: dict[str, Any] = raw_input if isinstance(raw_input, dict) else {}
            if name in READ_TOOLS and isinstance(tool_input.get("file_path"), str):
                reads.add(tool_input["file_path"])
            if name == "Workflow" and isinstance(block.get("id"), str) and event.get("parent_tool_use_id") is None:
                workflow_tools.add(block["id"])
                facts["workflows"].setdefault(block["id"], {"name": tool_input.get("name") or tool_input.get("scriptPath"),
                                                            "task_id": None, "status": "launched"})
        if kind == "system" and event.get("subtype") == "task_updated" and isinstance(event.get("task_id"), str):
            # A terminal patch may be a Workflow's only completion signal; it names the task, not the tool call.
            status = (event.get("patch") or {}).get("status") if isinstance(event.get("patch"), dict) else None
            tool_use_id = workflow_tasks.get(event["task_id"])
            if tool_use_id is not None and status in WORKFLOW_DONE:
                facts["workflows"][tool_use_id]["status"] = status
        if kind == "system" and event.get("tool_use_id") in workflow_tools:
            record = facts["workflows"][event["tool_use_id"]]
            if event.get("subtype") == "task_started" and record["task_id"] is None:
                record["task_id"] = event.get("task_id")
                if isinstance(record["task_id"], str):
                    workflow_tasks[record["task_id"]] = event["tool_use_id"]
            elif (event.get("subtype") == "task_notification"
                  and (record["task_id"] is None or event.get("task_id") == record["task_id"])
                  and event.get("status") in WORKFLOW_DONE):
                record["status"] = event["status"]
        if kind == "result":
            facts["provider_response_observed"] = True
            facts["result_events"] += 1
            if event.get("parent_tool_use_id") is None:
                facts["final"] = event
                facts["turns"].append({key: event.get(key) for key in ("subtype", "is_error", "stop_reason", "terminal_reason")})
            usage = event.get("modelUsage")
            for model in usage if isinstance(usage, dict) else {}:
                if isinstance(model, str) and model and model not in facts["models"]:
                    facts["models"].append(model)
    # Each result event repeats the session's cumulative denials; the final one is complete.
    facts["denials"] = _denials(facts["final"]) if facts["final"] else []
    facts["read_paths"] = sorted(reads)
    return facts


def _under(path: str, roots: Sequence[str]) -> str:
    for root in roots if os.path.isabs(path) else ():
        relative = os.path.relpath(path, root)
        if relative != ".." and not relative.startswith(".." + os.sep):
            return relative
    return path


def tool_steps(event: dict[str, Any], roots: Sequence[str] = ()) -> list[dict[str, str]]:
    """Public steps of the main session's tool calls in one assistant event."""
    if event.get("type") != "assistant" or event.get("parent_tool_use_id") is not None:
        return []
    steps = []
    for block in _blocks(event):
        if block.get("type") != "tool_use" or not isinstance(block.get("name"), str):
            continue
        raw_input = block.get("input")
        tool_input: dict[str, Any] = raw_input if isinstance(raw_input, dict) else {}
        step = {"tool": block["name"]}
        target = next((tool_input[key] for key in STEP_TARGET_KEYS if isinstance(tool_input.get(key), str)), None)
        if target:
            step["file"] = _under(target, roots)
        label = tool_input.get(STEP_LABEL_KEYS.get(block["name"], ""))
        if isinstance(label, str) and label.strip():
            step["text"] = label
        if isinstance(block.get("id"), str):
            step["tool_use_id"] = block["id"]
        steps.append(step)
    return steps


def parse_stream(path: Path, expected_session: str | None = None) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        text = ""
    return parse_lines(text.splitlines(), expected_session)


def live_identity(lines: Iterable[str]) -> dict[str, Any]:
    """The few identity fields a progress view may show while the run is active."""
    facts = parse_lines(lines)
    init = facts["init"] or {}
    return {"session_id": facts["session_id"], "initialized_model": init.get("model"), "models": facts["models"],
            "provider_response_observed": facts["provider_response_observed"]}
