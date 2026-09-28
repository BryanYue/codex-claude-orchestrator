"""Explicitly tested Claude CLI invocation profiles.

Flags printed by ``claude --help`` establish syntax only.  A version is placed
here only after the supervised fixture suite has exercised the bridge
semantics; production callers receive no switch for injecting another profile.
"""
from __future__ import annotations

from typing import Any, Mapping
from pathlib import Path
import hashlib
import json


REQUIRED_FLAGS = frozenset({
    "-p", "--model", "--effort", "--output-format", "--json-schema", "--session-id",
    "--permission-mode", "--tools", "--allowedTools", "--disallowedTools", "--settings",
    "--strict-mcp-config", "--mcp-config", "--disable-slash-commands",
})

# This profile has existing real supervised receipt evidence, with the current
# isolated regression suite guarding the bridge contract.  A fake CLI alone is
# never sufficient to add a released version. New native builds can instead
# acquire identity-bound local evidence from the isolated qualification suite.
SUPPORTED_CLI_PROFILES: Mapping[str, Mapping[str, Any]] = {
    "2.1.276": {
        "tested": True,
        "capabilities": {
            "max_turns": "--max-turns",
            "max_budget_usd": "--max-budget-usd",
        },
    },
    # Real MCP fixtures: review/resume/fresh, artifacts, named workflow,
    # deny hook and cancellation after provider init.  This CLI removed
    # --max-turns; never infer its availability from a neighboring version.
    # 2.1.278: real MCP review/resume/fresh/artifacts and named Workflow;
    # child Read hook denial, ordinary denial, and post-init cancellation.
    "2.1.278": {
        "tested": True,
        "capabilities": {"max_budget_usd": "--max-budget-usd"},
    },
    "2.1.277": {
        "tested": True,
        "capabilities": {
            "max_budget_usd": "--max-budget-usd",
        },
    },
}

# Historical released profiles preserve their existing task support. New local
# identities gain only the groups proved by the current qualification suite.
GROUPS = frozenset({"core", "read_only", "write", "resume", "workflow"})
for _profile in SUPPORTED_CLI_PROFILES.values():
    _profile.setdefault("groups", sorted(GROUPS))
    _profile.setdefault("source", "bundled_profile")


# Bump only when the persisted dispatch/session protocol becomes incompatible.
# Source hashes still bind qualification evidence to the current implementation.
DISPATCH_PROTOCOL_VERSION = 1
LEGACY_DISPATCH_CONTRACTS = frozenset({
    "bb38e17696e5a05d000615b086191f20e9c5c34f8fea3600913e31f10a3e56b1",  # 0.4.2
    "a8c6eac1eaf4700efa4dc799f7c32f9495e95b29ad93441a4d8eaf040cfb3955",  # 0.4.3
})


def bridge_contract_id() -> str:
    scripts = Path(__file__).resolve().parent
    plugin = scripts.parents[2]
    names = [scripts / name for name in ("bridge.py", "runtime.py", "workspace.py", "events.py", "named_workflow.py", "compatibility.py")]
    names += [plugin / "scripts" / name for name in ("cli_validation.py", "cli_store.py")]
    contents = [(str(path.relative_to(plugin)), hashlib.sha256(path.read_bytes()).hexdigest()) for path in names if path.is_file()]
    return hashlib.sha256(json.dumps(contents, sort_keys=True).encode()).hexdigest()


def required_groups(packet: Mapping[str, Any], resume: bool = False) -> list[str]:
    groups = {"core", "write" if packet.get("role") == "implement" else "read_only"}
    if packet.get("role") == "workflow_review":
        groups.add("workflow")
    if resume:
        groups.add("resume")
    return sorted(groups)


def resolved_profile(version: str, selection: Mapping[str, Any],
                     profiles: Mapping[str, Mapping[str, Any]] | None = None) -> Mapping[str, Any] | None:
    if profiles is not None:
        return profiles.get(version)
    selected = selection.get("identity")
    if not isinstance(selected, dict):
        return profile_for(version)
    from cli_store import identity, qualification
    actual = identity(selected["id"])
    if actual["sha256"] != selected.get("sha256") or actual["path"] != selection.get("path") or actual["version"] != version:
        return None
    proof = qualification(actual["id"], bridge_contract_id())
    if not proof:
        return None
    passed = sorted(name for name, value in proof.get("matrix", {}).items()
                    if name in GROUPS and isinstance(value, dict) and value.get("status") == "pass")
    bundled = profile_for(version)
    return {"tested": True, "source": proof.get("source", "local_qualification"), "groups": passed,
            "capabilities": dict((bundled or {}).get("capabilities", {})),
            "evidence_path": proof.get("evidence_path")}


def profile_for(version: str, profiles: Mapping[str, Mapping[str, Any]] | None = None) -> Mapping[str, Any] | None:
    """Find a profile, with injection reserved for isolated unit fixtures."""
    return (SUPPORTED_CLI_PROFILES if profiles is None else profiles).get(version)
