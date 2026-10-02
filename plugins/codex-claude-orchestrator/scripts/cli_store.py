"""Read-only access to retired plugin-managed Claude CLI history.

New dispatch uses the user's local CLI. Historical selections, private copies
and receipts stay on disk for inspection; this module cannot acquire, qualify,
activate, execute, rewrite or delete them.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
from typing import Any

SCHEMA_VERSION = 1
_IDENTITY_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,191}$")
_SELECTION_NAME = "selection.json"


def _environment(environ: dict[str, str] | None) -> dict[str, str]:
    return dict(os.environ if environ is None else environ)


def _expand(value: str, environ: dict[str, str]) -> Path:
    if value == "~" or value.startswith("~/"):
        return Path(environ.get("HOME") or str(Path.home())) / value[2:]
    return Path(value)


def store_root(environ: dict[str, str] | None = None) -> Path:
    """Return the plugin-owned store root without creating it."""
    env = _environment(environ)
    explicit = env.get("CLAUDE_ORCHESTRATOR_CLI_ROOT", "").strip()
    if explicit:
        return Path(os.path.abspath(str(_expand(explicit, env))))
    codex_home = _expand(env.get("CODEX_HOME", "~/.codex"), env)
    return Path(os.path.abspath(str(codex_home / "claude-orchestrator/cli")))


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as error:
        raise RuntimeError(f"invalid CLI store JSON: {path}") from error
    if not isinstance(data, dict):
        raise RuntimeError(f"invalid CLI store object: {path}")
    return data


def _descriptor(metadata: dict[str, Any], executable: Path) -> dict[str, Any]:
    keys = ("id", "version", "sha256", "platform", "machine", "source", "created_at")
    if any(key not in metadata for key in keys):
        raise RuntimeError("CLI identity metadata is incomplete")
    return {"id": metadata["id"], "path": str(executable), "version": metadata["version"],
            "sha256": metadata["sha256"], "platform": metadata["platform"],
            "machine": metadata["machine"], "source": metadata["source"],
            "created_at": metadata["created_at"]}


def _identity_path(identity_id: str, environ: dict[str, str] | None) -> tuple[Path, Path]:
    if not isinstance(identity_id, str) or not _IDENTITY_ID.fullmatch(identity_id):
        raise ValueError("invalid CLI identity id")
    base = store_root(environ) / "versions" / identity_id
    return base, base / "identity.json"


def identity_metadata(identity_id: str, environ: dict[str, str] | None = None) -> dict[str, Any]:
    """Read immutable identity metadata without hashing/codesigning the binary.

    This is historical metadata only; it grants no execution or selection authority.
    """
    base, metadata_path = _identity_path(identity_id, environ)
    if base.is_symlink() or metadata_path.is_symlink():
        raise RuntimeError("CLI store identity cannot be a symbolic link")
    metadata = _read_json(metadata_path)
    if metadata is None or metadata.get("schema_version") != SCHEMA_VERSION or metadata.get("id") != identity_id:
        raise RuntimeError("CLI identity metadata does not bind its path")
    executable = base / "claude"
    if executable.is_symlink() or not executable.is_file() or not os.access(executable, os.X_OK):
        raise RuntimeError("managed CLI executable is missing or is not executable")
    descriptor = _descriptor(metadata, executable)
    expected_id = _identity_id_for(descriptor["platform"], descriptor["machine"], descriptor["version"], descriptor["sha256"])
    if descriptor["id"] != expected_id:
        raise RuntimeError("managed CLI identity id does not match its metadata")
    return descriptor


def _identity_id_for(platform_name: str, machine: str, version: str, digest: str) -> str:
    safe_version = re.sub(r"[^A-Za-z0-9._-]", "-", version)
    safe_platform = re.sub(r"[^A-Za-z0-9._-]", "-", platform_name)
    safe_machine = re.sub(r"[^A-Za-z0-9._-]", "-", machine)
    return f"{safe_platform}-{safe_machine}-{safe_version}-{digest[:20]}"


def _recorded_size(identity_id: str, environ: dict[str, str] | None) -> dict[str, Any]:
    """Report the identity.json size only when it matches the retained file."""
    base, metadata_path = _identity_path(identity_id, environ)
    try:
        metadata = _read_json(metadata_path) or {}
        actual = (base / "claude").stat().st_size
    except (OSError, RuntimeError):
        return {"size": None, "size_status": "unreadable"}
    recorded = metadata.get("size")
    if type(recorded) is not int or recorded < 0:
        return {"size": None, "size_status": "not_recorded"}
    if recorded != actual:
        return {"size": None, "size_status": "mismatch"}
    return {"size": recorded, "size_status": "recorded_matches_file"}


def legacy_records(environ: dict[str, str] | None = None) -> dict[str, Any]:
    """Summarize retained managed-CLI history without locks, writes or hashing.

    Nothing here is executable authority: the dispatch path never reads it.
    Files are left in place because they are the user's historical evidence.
    """
    root_path = store_root(environ)
    result: dict[str, Any] = {"root": str(root_path), "present": root_path.is_dir(),
                              "ignored_for_dispatch": True, "files_deleted": False,
                              "selection": None, "versions": []}
    if not result["present"]:
        result["storage_summary"] = {"retained_bytes": 0, "unmeasured_versions": 0}
        return result
    try:
        selection = _read_json(root_path / _SELECTION_NAME)
    except RuntimeError as error:
        result["selection_error"] = str(error)
    else:
        if selection is not None:
            result["selection"] = {key: selection.get(key) for key in ("mode", "active", "previous", "generation")}
    versions_root = root_path / "versions"
    if versions_root.is_dir() and not versions_root.is_symlink():
        for entry in sorted(versions_root.iterdir(), key=lambda item: item.name):
            if not entry.is_dir() or entry.is_symlink() or not _IDENTITY_ID.fullmatch(entry.name):
                continue
            try:
                metadata = identity_metadata(entry.name, environ)
            except (ValueError, RuntimeError, OSError) as error:
                result["versions"].append({"id": entry.name, "metadata": "unreadable", "error": str(error),
                                           "size": None, "size_status": "unreadable"})
                continue
            result["versions"].append({"id": metadata["id"], "version": metadata["version"],
                                       "source": metadata["source"], "created_at": metadata["created_at"],
                                       **_recorded_size(entry.name, environ)})
    sizes = [item["size"] for item in result["versions"] if type(item.get("size")) is int]
    result["storage_summary"] = {"retained_bytes": sum(sizes),
                                 "unmeasured_versions": len(result["versions"]) - len(sizes),
                                 "excludes": ["download_partials", "run_evidence"],
                                 "automatic_cleanup": False}
    return result
