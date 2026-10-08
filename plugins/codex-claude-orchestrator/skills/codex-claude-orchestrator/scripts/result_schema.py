"""The result header Claude returns, and Codex's per-item decisions on it.

The full answer lives in report.md; the header carries what Codex needs to
triage: the claimed status, self-assessed acceptance, checks actually run,
gaps, disputes and questions only the user can decide.  ``items`` is an
optional appendix for findings or proposals.
"""
from __future__ import annotations

from typing import Any

STATUSES = ("completed", "partial", "blocked")
SELF_CHECKS = ("met", "not_met", "unverified")
CONFIDENCE = ("verified", "inferred")
DISPOSITIONS = ("accepted", "downgraded", "rejected")


def _strings() -> dict[str, Any]:
    return {"type": "array", "items": {"type": "string"}}


RESULT_PROPERTIES: dict[str, Any] = {
    "status": {"type": "string", "enum": list(STATUSES)},
    "summary": {"type": "string"},
    "acceptance": {"type": "array", "items": {
        "type": "object", "required": ["criterion", "self_check"], "additionalProperties": False,
        "properties": {"criterion": {"type": "string"}, "self_check": {"type": "string", "enum": list(SELF_CHECKS)},
                       "evidence": {"type": "string"}}}},
    "tests_run": {"type": "array", "items": {
        "type": "object", "required": ["command", "outcome"], "additionalProperties": False,
        "properties": {"command": {"type": "string"}, "exit_code": {"type": ["integer", "null"]},
                       "outcome": {"type": "string"}}}},
    "not_verified": _strings(),
    "disputes": _strings(),
    "questions": _strings(),
    "items": {"type": "array", "items": {
        "type": "object", "required": ["id", "title", "confidence"], "additionalProperties": False,
        "properties": {"id": {"type": "string"}, "title": {"type": "string"}, "severity": {"type": "string"},
                       "confidence": {"type": "string", "enum": list(CONFIDENCE)}, "evidence": {"type": "string"},
                       "location": {"type": "string"}}}},
}


def json_schema(*, with_report: bool) -> dict[str, Any]:
    """The --json-schema for the readonly profile, which has no file tools to write report.md."""
    properties = dict(RESULT_PROPERTIES)
    required = ["status", "summary"]
    if with_report:
        properties["report_markdown"] = {"type": "string"}
        required.append("report_markdown")
    return {"type": "object", "required": required, "additionalProperties": False, "properties": properties}


def _check(value: Any, schema: dict[str, Any], where: str, errors: list[str]) -> None:
    kind = schema.get("type")
    kinds: list[str] = [str(name) for name in kind] if isinstance(kind, list) else [str(kind)]
    matches = {"object": isinstance(value, dict), "array": isinstance(value, list), "null": value is None,
               "string": isinstance(value, str),
               "integer": isinstance(value, int) and not isinstance(value, bool)}
    if not any(matches.get(name, False) for name in kinds):
        errors.append(f"{where} must be {' or '.join(kinds)}")
        return
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{where} must be one of {', '.join(schema['enum'])}")
    if isinstance(value, dict):
        for key in schema.get("required", []):
            if key not in value:
                errors.append(f"{where}.{key} is required")
        properties = schema.get("properties", {})
        for key, item in value.items():
            if key in properties:
                _check(item, properties[key], f"{where}.{key}", errors)
            elif schema.get("additionalProperties") is False:
                errors.append(f"{where}.{key} is not a known field")
    if isinstance(value, list) and "items" in schema:
        for index, item in enumerate(value):
            _check(item, schema["items"], f"{where}[{index}]", errors)


def validate(value: Any, *, with_report: bool = False) -> list[str]:
    """Return problems with a result header; an empty list means it is well-formed."""
    errors: list[str] = []
    _check(value, json_schema(with_report=with_report), "result", errors)
    if isinstance(value, dict) and isinstance(value.get("items"), list):
        ids = [item["id"] for item in value["items"] if isinstance(item, dict) and isinstance(item.get("id"), str)]
        if len(ids) != len(set(ids)):
            errors.append("result.items ids must be unique")
    return errors[:50]


def item_decisions(items: list[dict[str, Any]], decisions: Any) -> list[dict[str, str]]:
    """Require exactly one decision per reported item; downgrades and rejections need a reason."""
    ids: list[str] = [item["id"] for item in items if isinstance(item, dict) and isinstance(item.get("id"), str)]
    if not ids:
        if decisions:
            raise ValueError("the report has no items to decide")
        return []
    if not isinstance(decisions, list):
        raise ValueError("item_decisions must cover every reported item: " + ", ".join(ids))
    normalized: list[dict[str, str]] = []
    for decision in decisions:
        if not isinstance(decision, dict) or set(decision) - {"id", "disposition", "reason"}:
            raise ValueError("each item decision is {id, disposition, reason}")
        disposition = decision.get("disposition")
        reason = decision.get("reason")
        if decision.get("id") not in ids or disposition not in DISPOSITIONS:
            raise ValueError("item decisions use reported ids and disposition accepted, downgraded or rejected")
        if disposition != "accepted" and (not isinstance(reason, str) or not reason.strip()):
            raise ValueError("downgraded and rejected items need a reason")
        entry = {"id": str(decision["id"]), "disposition": str(disposition)}
        if isinstance(reason, str) and reason.strip():
            entry["reason"] = reason.strip()
        normalized.append(entry)
    decided = [entry["id"] for entry in normalized]
    if sorted(decided) != sorted(ids):
        missing = sorted(set(ids) - set(decided))
        raise ValueError("item_decisions must decide each reported item exactly once"
                         + (": missing " + ", ".join(missing) if missing else ""))
    return normalized
