"""Read-only workspace manifests for supervised non-Git artifact reviews.

The artifact lane deliberately names every file it may read.  It does not
discover a directory tree, follow symlinks, or guess that an Office/binary
format is text.  Git snapshots remain implemented by :mod:`bridge` because
their status/diff semantics are materially different.
"""
from __future__ import annotations

import hashlib
import json
import os
import codecs
from pathlib import Path
from typing import Any


TEXT_SUFFIXES = frozenset({".txt", ".md", ".csv", ".json"})
FINDER_METADATA_NAME = ".DS_Store"


class WorkspaceError(RuntimeError):
    pass


def snapshot_digest(snapshot: dict[str, Any]) -> str:
    """Return one canonical digest suitable for recovery reconciliation."""
    # Finder metadata is retained for auditability in artifact manifests but is
    # intentionally not workspace evidence: it must not alter reconciliation.
    material = dict(snapshot)
    material.pop("ignored_directory_entries", None)
    canonical = json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8", "surrogateescape")).hexdigest()


def artifact_snapshot_difference(before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    """Describe content or directory-structure changes without treating mtime as data.

    Declared files are hash-bound.  Other workspace files are deliberately not
    read, so their guard covers only path addition/removal and entry-type
    changes.  Finder's exact ``.DS_Store`` metadata name is excluded and is
    retained in each snapshot for auditability.
    """
    if before.get("kind") != "artifacts" or after.get("kind") != "artifacts":
        return ["artifact manifest kind changed"]
    changes: list[str] = []
    def entry_key(entry: dict[str, Any]) -> tuple[str, str]:
        return str(entry.get("scope")), str(entry.get("path"))
    old_declared = {entry_key(entry): entry for entry in before.get("entries", []) if isinstance(entry, dict)}
    new_declared = {entry_key(entry): entry for entry in after.get("entries", []) if isinstance(entry, dict)}
    for key in sorted(set(old_declared) | set(new_declared)):
        old, new = old_declared.get(key), new_declared.get(key)
        display = f"{key[0]}:{key[1]}"
        if old is None:
            changes.append(f"declared file added: {display}")
        elif new is None:
            changes.append(f"declared file removed: {display}")
        elif old.get("sha256") != new.get("sha256"):
            changes.append(f"declared content changed: {display}")
        elif old.get("roles") != new.get("roles"):
            changes.append(f"declared file role changed: {display}")
    old_directory = {entry["path"]: entry["type"] for entry in before.get("directory_guard_entries", [])
                     if isinstance(entry, dict) and isinstance(entry.get("path"), str) and isinstance(entry.get("type"), str)}
    new_directory = {entry["path"]: entry["type"] for entry in after.get("directory_guard_entries", [])
                     if isinstance(entry, dict) and isinstance(entry.get("path"), str) and isinstance(entry.get("type"), str)}
    for path in sorted(set(old_directory) | set(new_directory)):
        old, new = old_directory.get(path), new_directory.get(path)
        if old is None:
            changes.append(f"directory entry added: {path} ({new})")
        elif new is None:
            changes.append(f"directory entry removed: {path} ({old})")
        elif old != new:
            changes.append(f"directory entry type changed: {path} ({old} -> {new})")
    return changes


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _normalise_system_alias(path: Path) -> Path:
    """Normalize macOS's stable /var and /tmp aliases before ancestor checks."""
    path = Path(os.path.normpath(str(path)))
    for alias in (Path("/var"), Path("/tmp")):
        try:
            suffix = path.relative_to(alias)
        except ValueError:
            continue
        return alias.resolve(strict=True) / suffix
    return path


def _reject_symlink_ancestors(path: Path, label: str) -> Path:
    """Reject a user-controlled symlink at any component of an absolute path."""
    if not path.is_absolute():
        raise WorkspaceError(f"{label} must be absolute after workspace resolution")
    if ".." in path.parts:
        raise WorkspaceError(f"{label} path traversal is not supported")
    normalized = _normalise_system_alias(path)
    cursor = Path(normalized.anchor)
    for part in normalized.parts[1:]:
        cursor = cursor / part
        try:
            if cursor.is_symlink():
                raise WorkspaceError(f"{label} symlink ancestor is not supported: {cursor}")
        except OSError as exc:
            raise WorkspaceError(f"{label} is not readable: {path}") from exc
    return normalized


def _validate_utf8_text(path: Path, label: str) -> None:
    decoder = codecs.getincrementaldecoder("utf-8")("strict")
    try:
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                if b"\0" in block:
                    raise WorkspaceError(f"{label} contains NUL bytes and is not supported text: {path}")
                decoder.decode(block)
            decoder.decode(b"", final=True)
    except UnicodeDecodeError as exc:
        raise WorkspaceError(f"{label} is not valid UTF-8 text: {path}") from exc


def _regular_text_file(raw: str, label: str) -> Path:
    path = _reject_symlink_ancestors(Path(raw), label)
    try:
        stat = path.stat()
    except OSError as exc:
        raise WorkspaceError(f"{label} is not readable: {raw}") from exc
    if not os.path.isfile(path):
        raise WorkspaceError(f"{label} must be a regular file: {raw}")
    if path.suffix.lower() not in TEXT_SUFFIXES:
        supported = ", ".join(sorted(TEXT_SUFFIXES))
        raise WorkspaceError(f"{label} type is unsupported ({path.suffix or 'no extension'}); supported text types: {supported}")
    if stat.st_size < 0:  # pragma: no cover - defensive, normal stat cannot do this
        raise WorkspaceError(f"{label} has invalid size: {raw}")
    _validate_utf8_text(path, label)
    return path.resolve(strict=True)


def canonical_artifact_file(raw: str, root: Path, label: str, *, must_be_inside: bool) -> Path:
    """Validate one declared file and return its canonical target.

    Sources may be an exact external file (for example a separately stored
    requirement); review inputs must be inside the selected artifact root.
    """
    if not isinstance(raw, str) or not raw:
        raise WorkspaceError(f"{label} must be a non-empty path string")
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = root / candidate
    target = _regular_text_file(str(candidate), label)
    if must_be_inside:
        try:
            target.relative_to(root)
        except ValueError as exc:
            raise WorkspaceError(f"{label} is outside artifact workspace: {raw}") from exc
    return target


def canonical_read_path(raw: str, root: Path, label: str = "artifact read path") -> Path:
    """Canonicalize a hook path without opening undeclared file contents."""
    if not isinstance(raw, str) or not raw:
        raise WorkspaceError(f"{label} must be a non-empty path string")
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = root / candidate
    return _reject_symlink_ancestors(candidate, label).resolve(strict=True)


def validate_artifact_lists(packet: dict[str, Any]) -> tuple[list[str], list[str]]:
    root = Path(packet["cwd"]).resolve(strict=True)
    inputs = packet.get("input_files")
    sources = packet.get("requirement_sources")
    if not isinstance(inputs, list) or not inputs or not all(isinstance(value, str) for value in inputs):
        raise WorkspaceError("artifacts workspace requires a non-empty input_files array of strings")
    if not isinstance(sources, list) or not sources or not all(isinstance(value, str) for value in sources):
        raise WorkspaceError("artifacts workspace requires a non-empty requirement_sources array of strings")
    canonical_inputs = [str(canonical_artifact_file(value, root, "input file", must_be_inside=True)) for value in inputs]
    canonical_sources = [str(canonical_artifact_file(value, root, "requirement source", must_be_inside=False)) for value in sources]
    if len(set(canonical_inputs)) != len(canonical_inputs):
        raise WorkspaceError("input_files must not contain duplicate canonical files")
    if len(set(canonical_sources)) != len(canonical_sources):
        raise WorkspaceError("requirement_sources must not contain duplicate canonical files")
    return canonical_inputs, canonical_sources


def artifact_snapshot(packet: dict[str, Any]) -> dict[str, Any]:
    """Return a stable manifest for all declared inputs and sources."""
    root = Path(packet["cwd"]).resolve(strict=True)
    declared: dict[str, set[str]] = {}
    for role, raw_paths in (("input_file", packet["input_files"]), ("requirement_source", packet["requirement_sources"])):
        for raw in raw_paths:
            path = canonical_artifact_file(raw, root, role.replace("_", " "), must_be_inside=role == "input_file")
            declared.setdefault(str(path), set()).add(role)
    entries: list[dict[str, Any]] = []
    for canonical, roles in sorted(declared.items()):
        path = Path(canonical)
        try:
            relative = path.relative_to(root).as_posix()
            location = {"path": relative, "scope": "workspace"}
        except ValueError:
            location = {"path": canonical, "scope": "external_requirement_source"}
        entries.append({**location, "roles": sorted(roles), "size": path.stat().st_size, "sha256": sha256_file(path)})
    material = "\n".join(f"{entry['scope']}\0{entry['path']}\0{entry['size']}\0{entry['sha256']}\0{','.join(entry['roles'])}" for entry in entries)
    # Protect the selected directory from creation/deletion/rename while not
    # reading undisclosed file contents.  Declared inputs get the stronger
    # content hash above; this guard catches an attempted side-effect beside
    # them without treating every ordinary file as model-readable input.
    directory_entries: list[dict[str, str]] = []
    ignored_directory_entries: list[str] = []
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root).as_posix()
        if path.name == FINDER_METADATA_NAME:
            # Finder metadata has no review meaning.  Keep an explicit audit
            # record, but do not turn it into a workspace side-effect signal.
            ignored_directory_entries.append(rel)
            continue
        path.lstat()
        if path.is_symlink():
            kind = "symlink"
        elif path.is_dir():
            kind = "directory"
        elif path.is_file():
            kind = "file"
        else:
            kind = "other"
        directory_entries.append({"path": rel, "type": kind})
    directory_material = "\n".join(f"{entry['path']}\0{entry['type']}" for entry in directory_entries)
    snapshot = {"kind": "artifacts", "root": str(root), "entries": entries,
            "manifest_sha256": hashlib.sha256(material.encode("utf-8")).hexdigest(),
            "directory_guard_sha256": hashlib.sha256(directory_material.encode("utf-8")).hexdigest(),
            "directory_guard_entries": directory_entries,
            "ignored_directory_entries": ignored_directory_entries,
            "read_scope": "exact_declared_files_only"}
    snapshot["workspace_digest"] = snapshot_digest(snapshot)
    return snapshot
