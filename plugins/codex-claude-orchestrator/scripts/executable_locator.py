"""Deterministic Claude CLI discovery shared by diagnostics and the bridge.

The MCP process intentionally does not source a login shell, inspect shell
aliases, or search version-manager directories.  Those actions would make the
executable selected for a supervised run implicit and hard to audit.  A user
whose Claude Code was installed through nvm, fnm, or Volta can set an absolute
``CLAUDE_BIN`` in the environment that starts Codex.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import tempfile
from typing import Any

import cli_store


NVM_ACTION = (
    "Claude Code was not found in this MCP process PATH. If it was installed "
    "through nvm, fnm, or Volta, set CLAUDE_BIN to that executable's absolute "
    "path in the environment that starts Codex, then restart Codex. This "
    "plugin does not source shell initialization files or select an nvm version."
)


def _expand_for_environment(value: str, environ: dict[str, str]) -> Path:
    """Expand ``~`` from the supplied environment, never ambient process HOME."""
    if value == "~" or value.startswith("~/"):
        home = environ.get("HOME") or str(Path.home())
        return Path(home) / value[2:]
    return Path(value)


def settings_path(environ: dict[str, str] | None = None) -> Path:
    """Return the plugin-owned persistent settings path, never a Claude setting."""
    env = os.environ if environ is None else environ
    configured = env.get("CLAUDE_ORCHESTRATOR_SETTINGS_PATH", "").strip()
    if configured:
        return _expand_for_environment(configured, env)
    codex_home = _expand_for_environment(env.get("CODEX_HOME", "~/.codex"), env)
    return codex_home / "claude-orchestrator/settings.json"


def _configured_cli(environ: dict[str, str]) -> tuple[str | None, str | None]:
    path = settings_path(environ)
    try:
        value = json.loads(path.read_text())
    except FileNotFoundError:
        return None, None
    except (OSError, ValueError):
        return None, "Claude Orchestrator settings could not be read as JSON; correct its claude_bin entry or remove the malformed plugin settings file."
    candidate = value.get("claude_bin") if isinstance(value, dict) else None
    if not isinstance(candidate, str) or not candidate.strip():
        return None, "Claude Orchestrator settings do not contain a usable claude_bin absolute path."
    candidate = candidate.strip()
    if not _expand_for_environment(candidate, environ).is_absolute():
        return None, "Claude Orchestrator settings claude_bin must be an absolute path."
    return candidate, None


def external_mode_requested(environ: dict[str, str] | None = None) -> bool:
    """Whether plugin settings contain the user's explicit external-mode choice."""
    env = os.environ if environ is None else environ
    try:
        value = json.loads(settings_path(env).read_text())
    except (FileNotFoundError, OSError, ValueError):
        return False
    return isinstance(value, dict) and value.get("execution_mode") == "external"


def configure_claude_bin(candidate: str, environ: dict[str, str] | None = None) -> Path:
    """Persist a user-supplied absolute executable path after verifying it locally."""
    env = os.environ if environ is None else environ
    path = _expand_for_environment(candidate, env)
    if not path.is_absolute() or not path.is_file() or not os.access(path, os.X_OK):
        raise ValueError("claude_bin must be an existing absolute executable path")
    destination = settings_path(env)
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix="settings-", dir=destination.parent)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            # Keep the user-selected bin path rather than following an npm/nvm
            # symlink into node_modules; cli_environment needs that bin parent.
            handle.write('{"schema_version":1,"execution_mode":"external","claude_bin":' + json.dumps(str(Path(os.path.abspath(str(path))))) + "}\n")
        temporary_path.chmod(0o600)
        temporary_path.replace(destination)
    finally:
        temporary_path.unlink(missing_ok=True)
    # Selecting a configured external path is an explicit user action.  It may
    # opt out of a retained managed snapshot, but a missing/broken path never
    # flips this mode implicitly.
    cli_store.set_external_mode(env)
    return destination


def _executable_path(candidate: str, *, path: str | None, environ: dict[str, str]) -> str | None:
    """Resolve an executable without treating aliases or shell functions as CLIs."""
    if not candidate:
        return None
    if "/" in candidate:
        # Preserve this lexical path. An nvm global `claude` can be a
        # bin-directory symlink into node_modules; resolving it would lose the
        # matching bin directory that contains its `node` sibling.
        location = Path(os.path.abspath(str(_expand_for_environment(candidate, environ))))
        return str(location) if location.is_file() and os.access(location, os.X_OK) else None
    return shutil.which(candidate, path=path)


def cli_environment(decision: dict[str, Any], environ: dict[str, str] | None = None) -> dict[str, str]:
    """Build the child environment for the one executable the locator selected.

    npm/nvm shims commonly have ``#!/usr/bin/env node``.  Supplying the parent
    directory of the already selected executable lets that exact Node install
    resolve its sibling ``node``.  It does not source a shell profile or scan
    version-manager directories, and it changes only the supervised child
    environment.
    """
    result = dict(os.environ if environ is None else environ)
    executable = decision.get("path")
    if isinstance(executable, str) and executable.startswith("/"):
        parent = str(Path(executable).parent)
        current = result.get("PATH", "")
        result["PATH"] = parent if not current else parent + os.pathsep + current
        result["CLAUDE_ORCHESTRATOR_CLAUDE_BIN_DIR"] = parent
    # This is deliberately scoped to an immutable private native object.  It
    # does not alter the user's global Claude configuration, launcher, or a
    # normal external/npm invocation.
    if decision.get("source") == "managed_native" or (decision.get("source") == "qualification" and isinstance(decision.get("identity"), dict)):
        result["DISABLE_AUTOUPDATER"] = "1"
    return result


def discover_external_claude(environ: dict[str, str] | None = None) -> dict[str, Any]:
    """Discover an explicitly external CLI using the legacy F8 precedence.

    This deliberately retains lexical nvm/npm paths and does not consult the
    private immutable store.  It is used only when no managed selection exists
    or when the user has explicitly selected external execution.
    """
    env = os.environ if environ is None else environ
    configured = env.get("CLAUDE_BIN", "").strip()
    path = env.get("PATH", "")
    if configured:
        resolved = _executable_path(configured, path=path, environ=env)
        return {
            "path": resolved,
            "source": "CLAUDE_BIN",
            "configured": True,
            "candidate": configured if "/" not in configured else str(_expand_for_environment(configured, env)),
            "action": None if resolved else "CLAUDE_BIN is set but does not name an executable file visible to this MCP process.",
        }
    persisted, error = _configured_cli(env)
    if persisted or error:
        resolved = _executable_path(persisted or "", path=path, environ=env)
        return {
            "path": resolved,
            "source": "plugin_settings" if persisted else None,
            "configured": bool(persisted),
            "candidate": persisted,
            "settings_path": str(settings_path(env)),
            "action": error or (None if resolved else "The plugin settings claude_bin path does not name an executable file visible to this MCP process."),
        }
    resolved = _executable_path("claude", path=path, environ=env)
    return {
        "path": resolved,
        "source": "PATH" if resolved else None,
        "configured": False,
        "candidate": "claude",
        "settings_path": str(settings_path(env)),
        "action": None if resolved else NVM_ACTION + " Persist it with Install.command --configure-claude-bin /absolute/path/to/claude.",
    }


def discover_system_claude(environ: dict[str, str] | None = None) -> dict[str, Any]:
    """Observe only the process override or actual PATH installation.

    Status and candidate-validation callers use this to see an updated native
    launcher even while a plugin settings pin or a managed active identity is
    retained for dispatch.  It intentionally ignores both plugin choices and
    never sources a shell profile.
    """
    env = os.environ if environ is None else environ
    configured = env.get("CLAUDE_BIN", "").strip()
    path = env.get("PATH", "")
    if configured:
        resolved = _executable_path(configured, path=path, environ=env)
        return {"path": resolved, "source": "CLAUDE_BIN", "configured": True,
                "candidate": configured if "/" not in configured else str(_expand_for_environment(configured, env)),
                "action": None if resolved else "CLAUDE_BIN is set but does not name an executable file visible to this MCP process."}
    resolved = _executable_path("claude", path=path, environ=env)
    return {"path": resolved, "source": "PATH" if resolved else None, "configured": False,
            "candidate": "claude", "action": None if resolved else NVM_ACTION}


def _bridge_contract_id() -> str:
    """Load compatibility lazily; locator remains usable by its standalone fixtures."""
    try:
        return cli_store._contract_id()
    except (AttributeError, ImportError, RuntimeError) as error:
        raise RuntimeError("managed Claude CLI cannot resolve the bridge compatibility contract") from error


def locate_claude(environ: dict[str, str] | None = None,
                  required_groups: list[str] | None = None,
                  required_capabilities: list[str] | None = None) -> dict[str, Any]:
    """Resolve a managed immutable object before considering mutable discovery.

    An existing managed active pointer is authoritative.  A missing required
    capability or an integrity failure remains a managed diagnostic instead of
    silently choosing a newer binary from ``CLAUDE_BIN`` or ``PATH``.
    """
    env = os.environ if environ is None else environ
    selection = cli_store.get_selection(env)
    if selection.get("mode") == "managed" and selection.get("active"):
        groups = required_groups or ["core", "read_only"]
        contract_id = _bridge_contract_id()
        managed = cli_store.select(groups, contract_id, env, required_capabilities=required_capabilities)
        if managed is None:
            return {"path": None, "source": "managed_native", "configured": True,
                    "candidate": selection["active"], "identity": {"id": selection["active"]},
                    "selection_generation": selection["generation"],
                    "action": "The active managed Claude CLI has no retained qualification for the requested compatibility groups; validate a candidate or roll back deliberately."}
        return {"path": managed["path"], "source": "managed_native", "configured": True,
                "candidate": managed["id"], "identity": {key: managed[key] for key in ("id", "path", "version", "sha256", "platform", "machine", "source", "created_at")},
                "selection_generation": managed["generation"], "selection_reason": managed["selection_reason"],
                "qualification": managed["qualification"], "action": None}
    return discover_external_claude(env)
