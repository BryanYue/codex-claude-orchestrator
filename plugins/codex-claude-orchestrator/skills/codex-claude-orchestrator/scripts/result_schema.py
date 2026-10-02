"""Structured review reports and explicit coordinator finding decisions."""
from __future__ import annotations
from typing import Any
from bridge_errors import BridgeError

NONBLANK = r"\S"
RESULT_SCHEMA: dict[str, Any] = {"type": "object", "required": ["status", "summary", "evidence", "checks", "unresolved"],
                 "properties": {"status": {"enum": ["completed", "blocked"]},
                                "summary": {"type": "string", "pattern": NONBLANK},
                                "evidence": {"type": "array", "items": {"type": "string", "pattern": NONBLANK}},
                                "checks": {"type": "array", "items": {"type": "string", "pattern": NONBLANK}},
                                "unresolved": {"type": "array", "items": {"type": "string", "pattern": NONBLANK}}}}


def result_payload(provider: dict[str, Any]) -> dict[str, Any]:
    """Validate the structured report's schema: a non-blank summary and no blank list items.

    This is shape, not adequacy: a short no-findings report is valid, and
    whether its content is correct remains Codex's acceptance decision.
    """
    value = provider.get("structured_output")
    if not isinstance(value, dict):
        raise BridgeError("provider result lacks structured_output object")
    if not isinstance(value.get("status"), str) or value["status"] not in {"completed", "blocked"} or not isinstance(value.get("summary"), str):
        raise BridgeError("structured result has invalid status or summary")
    if not value["summary"].strip():
        raise BridgeError("structured result summary is blank")
    for key in ("evidence", "checks", "unresolved"):
        if not isinstance(value.get(key), list) or not all(isinstance(x, str) for x in value[key]):
            raise BridgeError(f"structured result {key} must be an array of strings")
        if not all(x.strip() for x in value[key]):
            raise BridgeError(f"structured result {key} contains a blank item")
    validate_findings(value)
    return value



REVIEW_SCOPES = {"defects", "quality", "full"}
CATEGORIES = {"defect", "design", "maintainability", "test", "documentation"}
CONFIDENCES = {"reproduced", "code_confirmed", "probable", "subjective"}
FINDING_SCHEMA: dict[str, Any] = {
    "type": "object", "required": ["id", "category", "confidence", "summary", "evidence"],
    "properties": {
        "id": {"type": "string", "pattern": NONBLANK},
        "category": {"enum": sorted(CATEGORIES)}, "confidence": {"enum": sorted(CONFIDENCES)},
        "summary": {"type": "string", "pattern": NONBLANK},
        "evidence": {"type": "array", "items": {"type": "string", "pattern": NONBLANK}},
    },
}
FINDING_SCHEMA["allOf"] = [{"if": {"properties": {"confidence": {"enum": ["reproduced", "code_confirmed"]}}},
                             "then": {"properties": {"evidence": {"minItems": 1}}}}]
RESULT_SCHEMA["properties"]["findings"] = {"type": "array", "items": FINDING_SCHEMA}
RESULT_SCHEMA["properties"]["coverage"] = {"type": "array", "items": {"type": "string", "pattern": NONBLANK}}


def validate_findings(value: dict[str, Any]) -> None:
    if "coverage" in value and (not isinstance(value["coverage"], list)
            or not all(isinstance(x, str) and x.strip() for x in value["coverage"])):
        raise BridgeError("coverage must be an array of nonblank strings")
    if "findings" not in value:
        return
    findings = value["findings"]
    if not isinstance(findings, list):
        raise BridgeError("findings must be an array")
    seen = set()
    for finding in findings:
        if not isinstance(finding, dict):
            raise BridgeError("finding must be an object")
        for key in ("id", "summary"):
            if not isinstance(finding.get(key), str) or not finding[key].strip():
                raise BridgeError(f"finding {key} must be nonblank")
        if finding["id"] in seen:
            raise BridgeError("finding IDs must be unique")
        seen.add(finding["id"])
        if (not isinstance(finding.get("category"), str) or finding["category"] not in CATEGORIES
                or not isinstance(finding.get("confidence"), str) or finding["confidence"] not in CONFIDENCES):
            raise BridgeError("finding has an invalid category or confidence")
        evidence = finding.get("evidence")
        if not isinstance(evidence, list) or not all(isinstance(x, str) and x.strip() for x in evidence):
            raise BridgeError("finding evidence must be an array of nonblank strings")
        if finding["confidence"] in {"reproduced", "code_confirmed"} and not evidence:
            raise BridgeError("confirmed finding requires evidence")


def finding_decisions(findings: list[dict[str, Any]], decisions: Any) -> list[dict[str, Any]]:
    """Keep every original finding and require reasons for changed conclusions."""
    if not isinstance(decisions, list):
        raise ValueError("finding_decisions must be an array")
    known = {item["id"] for item in findings}
    seen = set()
    for item in decisions:
        if not isinstance(item, dict) or not isinstance(item.get("finding_id"), str):
            raise ValueError("finding decision requires finding_id")
        fid = item["finding_id"]
        if fid not in known or fid in seen:
            raise ValueError("finding decision must reference a unique reported finding")
        seen.add(fid)
        if not isinstance(item.get("disposition"), str) or item["disposition"] not in {"accepted", "downgraded", "rejected"}:
            raise ValueError("invalid finding disposition")
        if item["disposition"] != "accepted" and (not isinstance(item.get("reason"), str) or not item["reason"].strip()):
            raise ValueError("downgraded/rejected findings require a reason")
    if seen != known:
        raise ValueError("record a decision for every reported finding; none may be silently discarded")
    return decisions
