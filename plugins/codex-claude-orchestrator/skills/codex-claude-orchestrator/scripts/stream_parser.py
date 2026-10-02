"""Provider stream parsing and per-invocation evidence; no process or workspace writes."""
from __future__ import annotations

import hashlib
import json
import os
import stat
import re
from pathlib import Path
from typing import Any, Mapping

def workflow_input(workflow: Mapping[str, Any]) -> dict[str, Any]:
    return {key: workflow[key] for key in ("name", "args") if key in workflow}


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


DENIAL_TEXT_LIMIT = 500
DENIAL_TEXT_KEYS = ("tool_name", "toolName", "tool", "tool_use_id", "toolUseId", "reason", "message",
                    "permissionDecisionReason", "decision_reason")
DENIAL_INPUT_KEYS = ("file_path", "path", "query_path", "pattern", "name", "command")


def denial_entries(event: dict[str, Any], depth: int = 0) -> list[Any]:
    """Return only the denial entries a stream event carries.

    The event itself (a result with its report and usage, or a wrapper that
    nests another event's list) is never stored; each entry keeps its tool,
    id, reason and the path-like parts of its input, bounded in length.
    """
    raw = event.get("permission_denials")
    if isinstance(raw, list) and (raw or event.get("subtype") != "permission_denials"):
        items = raw
    elif raw:
        items = [raw]
    elif event.get("subtype") == "permission_denials":
        items = [{**event, "permission_denials": None}]
    else:
        return []
    entries: list[Any] = []
    for item in items:
        if isinstance(item, dict) and item.get("permission_denials") and depth < 3:
            entries.extend(denial_entries(item, depth + 1))
        elif isinstance(item, dict):
            entry: dict[str, Any] = {key: item[key][:DENIAL_TEXT_LIMIT] for key in DENIAL_TEXT_KEYS
                                     if isinstance(item.get(key), str)}
            tool_input = item.get("tool_input") if isinstance(item.get("tool_input"), dict) else {}
            kept = {key: tool_input[key][:DENIAL_TEXT_LIMIT] for key in DENIAL_INPUT_KEYS if isinstance(tool_input.get(key), str)}
            if kept:
                entry["tool_input"] = kept
            if item.get("subtype") == "permission_denials":
                entry["subtype"] = "permission_denials"
            entries.append(entry or {"unrecognized_shape": sorted(str(key) for key in item)[:10]})
        else:
            entries.append(str(item)[:DENIAL_TEXT_LIMIT])
    return entries


NOTIFICATION_HEADERS = frozenset({"task-id", "tool-use-id", "output-file", "status"})
_NOTIFICATION_ELEMENT = re.compile(r"\s*<([a-z][a-z-]*)>([^<]*)</\1>")
WORKFLOW_TASK_FAILURES = frozenset({"failed", "error", "killed", "stopped", "cancelled", "timeout"})
WORKFLOW_TERMINAL_SOURCES = ("system_task_notification", "legacy_text_notification")


def notification_headers(text: str) -> dict[str, str] | None:
    """Read the header elements of a text that is itself one task notification.

    Only a text that begins with ``<task-notification>`` counts, and only its
    leading header elements are read; the first other element (a summary or
    report body) ends them.  Tags quoted later, including a notification
    embedded in a report body, are never read, so fields are never combined
    across notifications.  A repeated header makes the notification ambiguous.
    """
    text = text.lstrip()
    if not text.startswith("<task-notification>"):
        return None
    position = len("<task-notification>")
    headers: dict[str, str] = {}
    while True:
        element = _NOTIFICATION_ELEMENT.match(text, position)
        if element is None or element.group(1) not in NOTIFICATION_HEADERS:
            return headers
        if element.group(1) in headers:
            return None
        headers[element.group(1)] = element.group(2).strip()
        position = element.end()


def _workflow_input_evidence(value: dict[str, Any]) -> dict[str, Any]:
    """Preserve invocation identity without copying script contents or argument secrets.

    A path digest observes bytes at parsing time, not proof of the bytes executed.
    Relative paths cannot be resolved without an independently observed working directory.
    """
    evidence: dict[str, Any] = {"name": value.get("name") if isinstance(value.get("name"), str) else None,
                                "script_path": value.get("scriptPath") if isinstance(value.get("scriptPath"), str) else None}
    if "args" in value:
        material = json.dumps(value["args"], sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
        evidence["args_sha256"] = hashlib.sha256(material).hexdigest()
    inline = value.get("script")
    if isinstance(inline, str):
        evidence.update(script_sha256=hashlib.sha256(inline.encode()).hexdigest(), script_digest_source="inline_tool_input")
    elif evidence["script_path"]:
        path = Path(evidence["script_path"])
        evidence["script_sha256"] = None
        if not path.is_absolute():
            evidence["script_digest_unavailable"] = "relative_path_without_execution_cwd"
            return evidence
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
            with os.fdopen(descriptor, "rb") as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_size > 2 * 1024 * 1024:
                    evidence["script_digest_unavailable"] = "not_a_regular_bounded_script"
                else:
                    content = stream.read(2 * 1024 * 1024 + 1)
                    if len(content) > 2 * 1024 * 1024:
                        evidence["script_digest_unavailable"] = "script_exceeds_size_limit"
                    else:
                        evidence.update(script_sha256=hashlib.sha256(content).hexdigest(), script_digest_source="file_at_parse_time")
        except OSError as exc:
            evidence["script_digest_unavailable"] = type(exc).__name__
    return evidence


def _new_invocation(tool_use_id: str, line: int, tool_input: dict[str, Any]) -> dict[str, Any]:
    return {"tool_use_id": tool_use_id, "tool_use_line": line, "input": _workflow_input_evidence(tool_input),
            "acknowledgement": {"state": "missing", "stream_line": None},
            "task_id": None, "task_started_line": None, "notifications_before_acknowledgement": 0,
            "unbound_notifications": 0, "terminals": {source: [] for source in WORKFLOW_TERMINAL_SOURCES}}


def _note_workflow_terminal(record: dict[str, Any], source: str, task_id: Any, status: Any, output_file: Any,
                            line: int, diagnostics: dict[str, int]) -> None:
    if record["acknowledgement"]["state"] == "missing":
        record["notifications_before_acknowledgement"] += 1
        return
    if record["acknowledgement"]["state"] != "succeeded":
        return
    bound = record["task_id"]
    # The Workflow's task is the first one started for its tool call; a
    # notification for another task (such as a nested agent) is not its end.
    if bound is not None and task_id != bound and not (source == "legacy_text_notification" and task_id is None):
        record["unbound_notifications"] += 1
        return
    if status != "completed" and status not in WORKFLOW_TASK_FAILURES:
        diagnostics["unrecognized_notification_statuses"] += 1
        return
    record["terminals"][source].append({"status": status, "stream_line": line,
                                        "task_id": task_id if isinstance(task_id, str) else None,
                                        "output_file": output_file if isinstance(output_file, str) else None})


def _workflow_invocation_evidence(index: int, record: dict[str, Any]) -> dict[str, Any]:
    """One per-invocation fact from the stream; structured system events outrank legacy text."""
    source = next((name for name in WORKFLOW_TERMINAL_SOURCES if record["terminals"][name]), None)
    terminals = record["terminals"][source] if source else []
    first = terminals[0] if terminals else None
    terminal = None
    if first is not None:
        conflict = any((item["status"], item["output_file"]) != (first["status"], first["output_file"])
                       for item in terminals[1:])
        terminal = {"status": first["status"], "source": source, "stream_line": first["stream_line"],
                    "count": len(terminals), "conflict": conflict}
    return {"index": index, "tool_use_id": record["tool_use_id"], "tool_use_line": record["tool_use_line"],
            "acknowledgement": record["acknowledgement"], "input": record["input"],
            "task_id": record["task_id"] or (first["task_id"] if first else None),
            "task_started_line": record["task_started_line"], "terminal": terminal,
            "output_reference": ({"output_file": first["output_file"], "source": source}
                                 if first is not None and first["output_file"] is not None else None),
            "notifications_before_acknowledgement": record["notifications_before_acknowledgement"],
            "unbound_notifications": record["unbound_notifications"]}


def _completed(invocation: dict[str, Any]) -> bool:
    terminal = invocation.get("terminal")
    return isinstance(terminal, dict) and terminal.get("status") == "completed" and not terminal.get("conflict")


class _StreamObservations:
    """Transient evidence counters and bindings for one provider stream."""
    def __init__(self, expected_session: str, expected_workflow: dict[str, Any] | str | None):
        self.expected_session: str = expected_session
        self.expected_workflow: dict[str, Any] | str | None = expected_workflow
        self.final: dict[str, Any] | None = None
        self.metadata: dict[str, Any] = {"permission_denials": [], "actual_models": [], "actual_model_sources": [],
                                    "guarded_tool_uses": {}, "provider_response_observed": False,
                                    "rejected_formatter_tool_uses": {}}
        self.expected_input: dict[str, Any] | None = ({"name": self.expected_workflow} if isinstance(self.expected_workflow, str) else workflow_input(self.expected_workflow)) if self.expected_workflow is not None else None
        self.invocations: dict[str, dict[str, Any]] = {}
        self.parent_result_lines: list[int] = []
        self.final_line: int | None = None
        self.diagnostics: dict[str, int] = {"non_parent_result_events": 0, "unexpected_session_result_events": 0,
                       "other_workflow_tool_uses": 0, "unrecognized_notification_statuses": 0}
        self.tool_use_counts: dict[str, int] = {}
        self.tool_result_counts: dict[str, int] = {}
        self.formatter_candidates: dict[str, dict[str, Any]] = {}
        self.formatter_errors: dict[str, int] = {}



def _invalid_formatter_input(block: dict[str, Any]) -> dict[str, Any] | None:
    value = block.get("input")
    if not isinstance(value, dict) or set(value) != {"__unparsedToolInput"}:
        return None
    wrapper = value["__unparsedToolInput"]
    if not isinstance(wrapper, dict) or set(wrapper) != {"raw", "len"}:
        return None
    raw, length = wrapper["raw"], wrapper["len"]
    if not isinstance(raw, str) or isinstance(length, bool) or not isinstance(length, int) or length <= 0:
        return None
    try:
        encoded = raw.encode("utf-8")
        if len(encoded) != length:
            return None
        json.loads(raw)
    except json.JSONDecodeError:
        return {"input_bytes": length, "input_sha256": hashlib.sha256(encoded).hexdigest()}
    except (ValueError, UnicodeError, RecursionError):
        return None
    return None


def parse_stream(path: Path, expected_session: str, expected_workflow: dict[str, Any] | str | None = None) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Parse the provider stream; only a parent ``type=result`` event in the expected session is final."""
    ctx = _StreamObservations(expected_session, expected_workflow)
    for line_number, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), start=1):
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            event_type = obj.get("type")
            event_session = obj.get("session_id") or obj.get("sessionId")
            parent_event = obj.get("parent_tool_use_id") is None
            _observe_provider_identity(ctx, obj, event_type, event_session)
            _observe_guarded_tools(ctx, obj, event_type, event_session, line_number)
            _observe_workflow_events(ctx, obj, event_type, event_session, parent_event, line_number)
            _select_parent_result(ctx, obj, event_type, event_session, parent_event, line_number)
    return _finalize_stream(ctx)


def _observe_provider_identity(ctx: _StreamObservations, obj: dict[str, Any], event_type: Any, event_session: Any) -> None:
    """Observe provider/session/model/usage facts without selecting a result."""
    if event_session:
        if event_session != ctx.expected_session:
            ctx.metadata.setdefault("session_mismatches", []).append(event_session)
    if obj.get("model"):
        ctx.metadata["reported_model"] = obj["model"]
        if obj.get("type") == "system" and obj.get("subtype") in {"init", "system_init"}:
            ctx.metadata["system_init_model"] = obj["model"]
            ctx.metadata["initialized_model"] = obj["model"]
            ctx.metadata["system_init_session_id"] = event_session
    if event_type == "assistant":
        ctx.metadata["provider_response_observed"] = True
        message = obj.get("message")
        assistant_model = message.get("model") if isinstance(message, dict) else None
        if not isinstance(assistant_model, str):
            assistant_model = obj.get("model") if isinstance(obj.get("model"), str) else None
        if assistant_model and assistant_model not in ctx.metadata["actual_models"]:
            ctx.metadata["actual_models"].append(assistant_model)
        if assistant_model and "assistant_message" not in ctx.metadata["actual_model_sources"]:
            ctx.metadata["actual_model_sources"].append("assistant_message")
    if event_type == "result":
        ctx.metadata["provider_response_observed"] = True
        ctx.metadata["result_event_count"] = ctx.metadata.get("result_event_count", 0) + 1
        model_usage = obj.get("modelUsage")
        if isinstance(model_usage, dict):
            usage_model_observed = False
            for model_name in model_usage:
                if isinstance(model_name, str) and model_name:
                    usage_model_observed = True
                    if model_name not in ctx.metadata["actual_models"]:
                        ctx.metadata["actual_models"].append(model_name)
            if usage_model_observed and "result_model_usage" not in ctx.metadata["actual_model_sources"]:
                ctx.metadata["actual_model_sources"].append("result_model_usage")
    if "usage" in obj:
        ctx.metadata["usage"] = obj["usage"]
    ctx.metadata["permission_denials"].extend(denial_entries(obj))


def _observe_guarded_tools(ctx: _StreamObservations, obj: dict[str, Any], event_type: Any, event_session: Any, line_number: int) -> None:
    """Count tool IDs and retain only proven rejected-before-execution formatter candidates."""
    # The observed CLI wrapper and matching error result establish a
    # formatter never reached execution. Missing identity or duplicate
    # IDs/results cannot establish this, and no error text is matched.
    formatter_parent = (obj.get("parent_tool_use_id", True) is None
                        and event_session == ctx.expected_session
                        and all(obj.get(key, ctx.expected_session) == ctx.expected_session
                                for key in ("session_id", "sessionId")))
    for block in _stream_content_blocks(obj):
        if block.get("type") == "tool_use" and isinstance(block.get("id"), str):
            tool_id = block["id"]
            ctx.tool_use_counts[tool_id] = ctx.tool_use_counts.get(tool_id, 0) + 1
            if isinstance(block.get("name"), str):
                ctx.metadata["guarded_tool_uses"][tool_id] = block["name"]
            if (tool_id.strip() and event_type == "assistant" and formatter_parent and block.get("name") == "StructuredOutput"
                    and block.get("caller", {"type": "direct"}) == {"type": "direct"}):
                proof = _invalid_formatter_input(block)
                if proof is not None:
                    ctx.formatter_candidates[tool_id] = {**proof, "state": "rejected_before_execution",
                                                     "tool_name": "StructuredOutput", "session_id": ctx.expected_session,
                                                     "tool_use_stream_line": line_number}
        elif block.get("type") == "tool_result" and isinstance(block.get("tool_use_id"), str):
            tool_id = block["tool_use_id"]
            ctx.tool_result_counts[tool_id] = ctx.tool_result_counts.get(tool_id, 0) + 1
            if (event_type == "user" and formatter_parent and block.get("is_error") is True
                    and isinstance(block.get("content"), str) and block["content"].strip()):
                ctx.formatter_errors[tool_id] = line_number


def _observe_workflow_events(ctx: _StreamObservations, obj: dict[str, Any], event_type: Any, event_session: Any, parent_event: bool, line_number: int) -> None:
    """Bind parent Workflow calls, acknowledgements, task IDs, and terminal notifications."""
    if ctx.expected_workflow is not None:
        # Only the parent's own turns launch and acknowledge the call; a
        # Workflow agent's messages carry a parent_tool_use_id.
        if parent_event and event_session in (None, ctx.expected_session):
            for block in _stream_content_blocks(obj):
                if block.get("type") == "tool_use" and block.get("name") == "Workflow":
                    tool_id = block.get("id")
                    if isinstance(tool_id, str) and isinstance(block.get("input"), dict) and (ctx.expected_workflow == "*" or block["input"] == ctx.expected_input):
                        ctx.invocations.setdefault(tool_id, _new_invocation(tool_id, line_number, block["input"]))
                    else:
                        ctx.diagnostics["other_workflow_tool_uses"] += 1
                elif block.get("type") == "tool_result" and block.get("tool_use_id") in ctx.invocations:
                    record = ctx.invocations[block["tool_use_id"]]
                    if record["acknowledgement"]["state"] == "missing":
                        record["acknowledgement"] = {"state": "succeeded" if block.get("is_error") is False else "failed",
                                                     "stream_line": line_number}
        if event_type == "system" and event_session == ctx.expected_session and obj.get("tool_use_id") in ctx.invocations:
            record = ctx.invocations[obj["tool_use_id"]]
            if obj.get("subtype") == "task_started":
                if (record["task_id"] is None and isinstance(obj.get("task_id"), str)
                        and obj.get("task_type", "local_workflow") == "local_workflow"):
                    record["task_id"], record["task_started_line"] = obj["task_id"], line_number
            elif obj.get("subtype") == "task_notification":
                _note_workflow_terminal(record, "system_task_notification", obj.get("task_id"), obj.get("status"),
                                        obj.get("output_file"), line_number, ctx.diagnostics)
        if event_type == "user" and event_session == ctx.expected_session and parent_event:
            for text in _stream_texts(obj):
                headers = notification_headers(text)
                if headers and headers.get("tool-use-id") in ctx.invocations:
                    _note_workflow_terminal(ctx.invocations[headers["tool-use-id"]], "legacy_text_notification",
                                            headers.get("task-id"), headers.get("status"), None,
                                            line_number, ctx.diagnostics)


def _select_parent_result(ctx: _StreamObservations, obj: dict[str, Any], event_type: Any, event_session: Any, parent_event: bool, line_number: int) -> None:
    """Select only parent results in the expected session and retain mismatch counters."""
    if event_type == "result":
        if parent_event and event_session == ctx.expected_session:
            ctx.final = obj
            ctx.final_line = line_number
            ctx.parent_result_lines.append(line_number)
            ctx.metadata["final_session_id"] = event_session
        elif not parent_event:
            ctx.diagnostics["non_parent_result_events"] += 1
        else:
            ctx.diagnostics["unexpected_session_result_events"] += 1


def _finalize_stream(ctx: _StreamObservations) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Reconcile formatter/session evidence after every event, then summarize Workflow completion."""
    if isinstance(ctx.final, dict) and "usage" in ctx.final:
        ctx.metadata["usage"] = ctx.final["usage"]
    provider_denied_ids = {item.get("tool_use_id") or item.get("toolUseId")
                          for item in ctx.metadata["permission_denials"] if isinstance(item, dict)
                          and isinstance(item.get("tool_use_id") or item.get("toolUseId"), str)}
    ctx.metadata["rejected_formatter_tool_uses"] = {
        tool_id: {**proof, "tool_result_stream_line": ctx.formatter_errors[tool_id]}
        for tool_id, proof in ctx.formatter_candidates.items()
        if ctx.tool_use_counts.get(tool_id) == 1 and ctx.tool_result_counts.get(tool_id) == 1
        and ctx.formatter_errors.get(tool_id, 0) > proof["tool_use_stream_line"]
        and tool_id not in provider_denied_ids}
    init_session = ctx.metadata.get("system_init_session_id")
    final_session = ctx.metadata.get("final_session_id")
    if init_session == ctx.expected_session:
        ctx.metadata["actual_session_id"] = init_session
    if ctx.metadata.get("session_mismatches") or init_session != ctx.expected_session or final_session != ctx.expected_session or init_session != final_session:
        ctx.metadata["session_error"] = "system/init session and final result must both equal the expected session"
    ctx.metadata["result_selection"] = {"rule": "last parent type=result event in the expected session",
                                    "final_stream_line": ctx.final_line, "parent_result_events": len(ctx.parent_result_lines),
                                    "non_parent_result_events": ctx.diagnostics["non_parent_result_events"],
                                    "unexpected_session_result_events": ctx.diagnostics["unexpected_session_result_events"]}
    _summarize_workflow(ctx)
    ctx.metadata["actual_model_source"] = "+".join(ctx.metadata["actual_model_sources"]) or None
    return ctx.final, ctx.metadata


def _summarize_workflow(ctx: _StreamObservations) -> None:
    """Keep completion counts and final-after-completion ordering per bound invocation."""
    if ctx.expected_workflow is not None:
        evidence = [_workflow_invocation_evidence(index, record) for index, record in enumerate(ctx.invocations.values())]
        completed = bool(evidence) and all(_completed(item) for item in evidence)
        last_completion = max((item["terminal"]["stream_line"] for item in evidence if _completed(item)), default=0)
        after_completion = [line for line in ctx.parent_result_lines if completed and line > last_completion]
        names = list(dict.fromkeys(item["input"]["name"] for item in evidence if item["input"]["name"]))
        ctx.metadata["workflow_name"] = (names[0] if len(names) == 1 else None) if ctx.expected_workflow == "*" else ctx.expected_input["name"]
        ctx.metadata["workflow_names"] = names
        ctx.metadata["workflow_invocations"] = evidence
        ctx.metadata["workflow_stream_diagnostics"] = {"other_workflow_tool_uses": ctx.diagnostics["other_workflow_tool_uses"],
                                                   "unrecognized_notification_statuses": ctx.diagnostics["unrecognized_notification_statuses"]}
        ctx.metadata["workflow_tool_use_observed"] = bool(evidence)
        ctx.metadata["workflow_tool_result_success"] = bool(evidence) and all(item["acknowledgement"]["state"] == "succeeded" for item in evidence)
        ctx.metadata["workflow_tool_use_count"] = len(evidence)
        ctx.metadata["workflow_tool_result_success_count"] = sum(item["acknowledgement"]["state"] == "succeeded" for item in evidence)
        ctx.metadata["workflow_completion_observed"] = completed
        ctx.metadata["workflow_completion_count"] = sum(_completed(item) for item in evidence)
        # A result emitted while a launched Workflow is still pending (the
        # launch acknowledgement turn) is interim; only a later parent result
        # can be the Workflow report.
        ctx.metadata["workflow_final_after_completion"] = ctx.final_line is not None and ctx.final_line in after_completion
        ctx.metadata["workflow_interim_result_count"] = len(ctx.parent_result_lines) - len(after_completion)


def provider_identity(lines) -> dict[str, Any]:
    """Project live identity using the same event rules as terminal parsing."""
    ctx = _StreamObservations("", None)
    session = None
    for line in lines:
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        kind = event.get("type")
        _observe_provider_identity(ctx, event, kind, event.get("session_id"))
        if kind == "result" or (kind == "system" and event.get("subtype") in {"init", "system_init"}):
            if isinstance(event.get("session_id"), str):
                session = event["session_id"]
    return {"session_id": session, "initialized_model": ctx.metadata.get("initialized_model"),
            "actual_models": ctx.metadata["actual_models"],
            "actual_model_source": "+".join(ctx.metadata["actual_model_sources"]) or None,
            "provider_response_observed": ctx.metadata.get("provider_response_observed", False)}
