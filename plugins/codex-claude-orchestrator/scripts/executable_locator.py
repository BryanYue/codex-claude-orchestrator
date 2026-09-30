"""Deterministic Claude CLI discovery shared by diagnostics and the bridge.

Every dispatch uses the Claude CLI the user installed or explicitly configured:
``CLAUDE_BIN``, the plugin ``claude_bin`` setting, or ``claude`` on this MCP
process PATH.  A retained plugin-managed private copy is historical evidence
only and is never selected.  The MCP process intentionally does not source a
login shell, inspect shell aliases, or search version-manager directories.  A
user whose Claude Code was installed through nvm, fnm, or Volta can set an
absolute ``CLAUDE_BIN`` in the environment that starts Codex.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import tempfile
from typing import Any


NVM_ACTION = (
    "Claude Code was not found in this MCP process PATH. If it was installed "
    "through nvm, fnm, or Volta, set CLAUDE_BIN to that executable's absolute "
    "path in the environment that starts Codex, then restart Codex. This "
    "plugin does not source shell initialization files or select an nvm version."
)

ZIP_PERSISTENCE_HINT = (
    " Only a ZIP distribution, whose root contains FILE-SHA256.json, can "
    "instead persist it with its Install.command --configure-claude-bin "
    "/absolute/path/to/claude; a Git marketplace checkout uses CLAUDE_BIN."
)

RETIRED_SELECTION_SOURCES =frozenset({"managed_native", "qualification"})


def stale_setting_action(path: Path) -> str:
    """Recovery for a saved claude_bin that no longer names an executable."""
    return (
        "The plugin settings claude_bin path does not name an executable file visible to this MCP process. "
        f"It is read from {path}; while it is set, claude on PATH is not used. To recover, set CLAUDE_BIN "
        "to a working absolute path in the environment that starts Codex, then restart Codex (CLAUDE_BIN "
        "takes precedence over this setting), or change only the claude_bin value in that file to a working "
        "absolute path. Only a ZIP distribution, whose root contains FILE-SHA256.json, can instead rewrite "
        "that settings file with its Install.command --configure-claude-bin /absolute/path/to/claude; a Git "
        "marketplace checkout uses CLAUDE_BIN."
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
    resolve its sibling ``node``.  It does not source a shell profile, scan
    version-manager directories, or change the user's CLI update settings, and
    it changes only the supervised child environment.
    """
    result = dict(os.environ if environ is None else environ)
    executable = decision.get("path")
    if isinstance(executable, str) and executable.startswith("/"):
        parent = str(Path(executable).parent)
        current = result.get("PATH", "")
        result["PATH"] = parent if not current else parent + os.pathsep + current
        result["CLAUDE_ORCHESTRATOR_CLAUDE_BIN_DIR"] = parent
    return result


def discover_external_claude(environ: dict[str, str] | None = None) -> dict[str, Any]:
    """Discover the user's CLI: CLAUDE_BIN, then plugin settings, then PATH.

    This deliberately retains lexical nvm/npm paths and never consults the
    private retained store.
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
        location = settings_path(env)
        return {
            "path": resolved,
            "source": "plugin_settings" if persisted else None,
            "configured": bool(persisted),
            "candidate": persisted,
            "settings_path": str(location),
            "action": error or (None if resolved else stale_setting_action(location)),
        }
    resolved = _executable_path("claude", path=path, environ=env)
    return {
        "path": resolved,
        "source": "PATH" if resolved else None,
        "configured": False,
        "candidate": "claude",
        "settings_path": str(settings_path(env)),
        "action": None if resolved else NVM_ACTION + ZIP_PERSISTENCE_HINT,
    }


def locate_claude(environ: dict[str, str] | None = None) -> dict[str, Any]:
    """Return the user's local Claude CLI for a new dispatch or local check.

    A persisted managed selection from an earlier plugin version is ignored:
    it can neither choose a private copy nor block the local CLI.
    """
    return discover_external_claude(environ)
