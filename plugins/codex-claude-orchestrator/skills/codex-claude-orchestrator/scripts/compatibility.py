"""Claude CLI invocation requirements for the supervised bridge.

Any local Claude CLI version may run a task when ``--help`` advertises the exact
flags that task needs and the local login check passes.  Help output proves only
advertised syntax.  Behavior is established per run by the hook self-test and
the post-run tool, session, result and workspace verification in ``bridge.py``.
The version string is recorded for diagnosis and never used as an allowlist.
"""
from __future__ import annotations

from typing import Any, Mapping
from pathlib import Path
import hashlib
import json
import re


REQUIRED_FLAGS = frozenset({
    "-p", "--model", "--effort", "--output-format", "--verbose", "--json-schema", "--session-id",
    "--permission-mode", "--tools", "--allowedTools", "--disallowedTools", "--settings",
    "--strict-mcp-config", "--mcp-config", "--disable-slash-commands",
})
RESUME_FLAG = "--resume"
# Only the doctor --verify probe passes this; ordinary tasks keep their session.
VERIFY_FLAGS = frozenset({"--no-session-persistence"})
BUDGET_FLAGS: Mapping[str, str] = {
    "max_turns": "--max-turns",
    "max_budget_usd": "--max-budget-usd",
}

# Historical record of versions exercised by earlier releases.  It is read only
# by the retained-store history code and never admits or rejects a local CLI.
SUPPORTED_CLI_PROFILES: Mapping[str, Mapping[str, Any]] = {
    "2.1.276": {
        "tested": True,
        "capabilities": {
            "max_turns": "--max-turns",
            "max_budget_usd": "--max-budget-usd",
        },
    },
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

GROUPS = frozenset({"core", "read_only", "write", "resume", "workflow"})
for _profile in SUPPORTED_CLI_PROFILES.values():
    _profile.setdefault("groups", sorted(GROUPS))
    _profile.setdefault("source", "bundled_profile")


# Bump only when the persisted dispatch/session protocol becomes incompatible.
DISPATCH_PROTOCOL_VERSION = 1
LEGACY_DISPATCH_CONTRACTS = frozenset({
    "bb38e17696e5a05d000615b086191f20e9c5c34f8fea3600913e31f10a3e56b1",  # 0.4.2
    "a8c6eac1eaf4700efa4dc799f7c32f9495e95b29ad93441a4d8eaf040cfb3955",  # 0.4.3
})


def bridge_contract_id() -> str:
    scripts = Path(__file__).resolve().parent
    plugin = scripts.parents[2]
    names = [scripts / name for name in ("bridge.py", "runtime.py", "workspace.py", "events.py", "named_workflow.py",
                                         "compatibility.py", "usage.py", "workflow_delivery.py")]
    names += [plugin / "scripts" / name for name in ("cli_validation.py", "cli_store.py", "content_store.py", "plugin_identity.py")]
    contents = [(str(path.relative_to(plugin)), hashlib.sha256(path.read_bytes()).hexdigest()) for path in names if path.is_file()]
    return hashlib.sha256(json.dumps(contents, sort_keys=True).encode()).hexdigest()


def required_groups(packet: Mapping[str, Any], resume: bool = False) -> list[str]:
    groups = {"core", "write" if packet.get("role") == "implement" else "read_only"}
    if packet.get("role") == "workflow_review":
        groups.add("workflow")
    if resume:
        groups.add("resume")
    return sorted(groups)


def flag_advertised(help_text: str, flag: str) -> bool:
    """Match one exact option token; ``-p`` must not match inside ``--print``."""
    return re.search(r"(?<![\w-])" + re.escape(flag) + r"(?![\w-])", help_text) is not None


def required_flags(groups: set[str] | frozenset[str], verify: bool = False) -> set[str]:
    flags = set(REQUIRED_FLAGS)
    if "resume" in groups:
        flags.add(RESUME_FLAG)
    if verify:
        flags |= VERIFY_FLAGS
    return flags


def profile_for(version: str, profiles: Mapping[str, Mapping[str, Any]] | None = None) -> Mapping[str, Any] | None:
    """Historical lookup used only by the retained-store records."""
    return (SUPPORTED_CLI_PROFILES if profiles is None else profiles).get(version)
