"""Plugin identity frozen at the start of one bridge run.

The plugin root is resolved from this file, never from the reviewed
repository's cwd, so a run records the code that actually executed.  Three
independent facts are kept apart: the release version, the source revision
(Git HEAD or a distribution manifest) with its dirty state, and a digest over
the plugin files that are on disk now.  A revision alone never stands in for
the code digest.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import stat
import time
import uuid
import trusted_git
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
FILE_NAME = "plugin-identity.json"
PLUGIN_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_MANIFEST_RELATIVE = ".codex-plugin/plugin.json"
RELEASE_MANIFEST_NAME = "RELEASE-MANIFEST.json"
DIRTY_SAMPLE_LIMIT = 20

# tools/build_distribution.py packages the same set; its drift test compares the two digests.
OMIT_DIR_NAMES = frozenset({".venv", "__pycache__", ".uv-cache", ".git", "dist",
                            ".pytest_cache", "coverage", ".mypy_cache", ".ruff_cache"})
ALLOWED_SUFFIXES = frozenset({".md", ".json", ".py", ".lock", ".toml", ".yaml", ".yml", ".sh", ".html", ".command", ".png", ".js"})
ALLOWED_BARE_NAMES = frozenset({".gitignore"})
CODE_DIGEST_SCOPE = ("files under the plugin root with distributable suffixes, taken from the working tree now "
                     "(path + file-byte SHA-256; excludes caches, .venv, dist and .git; ignored files with those "
                     "suffixes are included; not the reviewed repository)")
_SHA1 = re.compile(r"[0-9a-f]{40}")


def counted_file(relative_parts: tuple[str, ...]) -> bool:
    name = relative_parts[-1]
    if any(part in OMIT_DIR_NAMES for part in relative_parts[:-1]) or name.endswith(".pyc"):
        return False
    return Path(name).suffix.lower() in ALLOWED_SUFFIXES or name in ALLOWED_BARE_NAMES


def digest_entries(entries: list[tuple[str, str]]) -> str:
    ordered = sorted([path, digest] for path, digest in entries)
    return hashlib.sha256(json.dumps(ordered, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def code_digest(root: Path) -> dict[str, Any]:
    entries: list[tuple[str, str]] = []
    for directory, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(name for name in dirnames if name not in OMIT_DIR_NAMES)
        for name in sorted(filenames):
            path = Path(directory) / name
            relative = path.relative_to(root)
            if not counted_file(relative.parts):
                continue
            if path.is_symlink():
                content = "symlink:" + hashlib.sha256(os.readlink(path).encode("utf-8", "surrogateescape")).hexdigest()
            else:
                # Diagnostic hashing must never wait on a FIFO/device, including
                # a file swapped between traversal and open.
                fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
                with os.fdopen(fd, "rb") as stream:
                    if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                        raise OSError("non-regular plugin file")
                    content = hashlib.file_digest(stream, "sha256").hexdigest()
            entries.append((relative.as_posix(), content))
    return {"algorithm": "sha256", "value": digest_entries(entries), "file_count": len(entries),
            "scope": CODE_DIGEST_SCOPE}


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    return trusted_git.run(root, *args, timeout=20)


def _status_paths(raw: bytes) -> list[str]:
    fields = raw.split(b"\0")
    paths: list[str] = []
    index = 0
    while index < len(fields):
        field = fields[index]
        index += 1
        if len(field) < 4:
            continue
        code = field[:2].decode("ascii", "replace")
        paths.append(field[3:].decode("utf-8", "replace"))
        if "R" in code or "C" in code:
            index += 1  # the rename/copy origin is a separate NUL field
    return paths


def git_source(root: Path) -> dict[str, Any] | None:
    """Describe root as a Git checkout, or None when it is not tracked by one.

    A Git directory that merely contains root (an installed copy under an
    unrelated repository) is not this plugin's checkout: the plugin manifest
    itself must be tracked.
    """
    def unavailable(reason: str) -> dict[str, Any]:
        return {"kind": "unknown", "revision": None, "state": "unknown", "error": reason}
    try:
        probe = _git(root, "rev-parse", "--is-inside-work-tree")
        if probe.returncode:
            if b"not a git repository" in probe.stderr.lower():
                return None
            return unavailable(f"git rev-parse failed (exit {probe.returncode})")
        tracked = _git(root, "ls-files", "--error-unmatch", "--", PLUGIN_MANIFEST_RELATIVE)
        if tracked.returncode:
            if tracked.returncode == 1 and b"did not match any file(s) known to git" in tracked.stderr:
                return None
            return unavailable(f"git ls-files failed (exit {tracked.returncode})")
    except (OSError, subprocess.TimeoutExpired) as exc:
        return unavailable(f"git probe unavailable: {type(exc).__name__}")
    source: dict[str, Any] = {"kind": "git", "revision": None, "revision_source": "git_head", "state": "unknown",
                              "dirty_scope": "plugin_root"}
    try:
        head = _git(root, "rev-parse", "--verify", "-q", "HEAD")
        if head.returncode == 0:
            source["revision"] = head.stdout.decode().strip()
        status = _git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all", "--", ".")
    except (OSError, subprocess.TimeoutExpired) as exc:
        source["error"] = f"git status unavailable: {type(exc).__name__}"
        return source
    if status.returncode:
        source["error"] = "git status failed: " + status.stderr.decode("utf-8", "replace").strip()[:200]
        return source
    paths = _status_paths(status.stdout)
    source["state"] = "dirty" if paths else "clean"
    source["dirty_count"] = len(paths)
    source["dirty_paths"] = paths[:DIRTY_SAMPLE_LIMIT]
    source["dirty_paths_truncated"] = len(paths) > DIRTY_SAMPLE_LIMIT
    return source


def release_manifest_source(root: Path) -> dict[str, Any] | None:
    """Read the distribution manifest as provenance only.

    The installed files are not compared with it, so the dirty state stays
    unknown and no claim is made that the code equals what was built.
    """
    if root.parent.name != "plugins":
        return None
    path = root.parent.parent / RELEASE_MANIFEST_NAME
    if not path.is_file():
        return None
    source: dict[str, Any] = {"kind": "release_manifest", "revision": None, "revision_source": "release_manifest",
                              "state": "unknown", "provenance_only": True, "verification": "not_performed"}
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        source["error"] = f"release manifest unreadable: {type(exc).__name__}"
        return source
    if not isinstance(manifest, dict):
        source["error"] = "release manifest is not an object"
        return source
    commit = manifest.get("source_commit")
    if isinstance(commit, str) and _SHA1.fullmatch(commit):
        source["revision"] = commit
    for key in ("plugin_version", "contract_digest", "plugin_code_digest"):
        if isinstance(manifest.get(key), str):
            source["manifest_" + key] = manifest[key]
    return source


def plugin_version(root: Path) -> tuple[str | None, str | None, str | None]:
    try:
        manifest = json.loads((root / PLUGIN_MANIFEST_RELATIVE).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return None, None, f"plugin manifest unreadable: {type(exc).__name__}"
    if not isinstance(manifest, dict):
        return None, None, "plugin manifest is not an object"
    name, version = manifest.get("name"), manifest.get("version")
    return (name if isinstance(name, str) else None, version if isinstance(version, str) else None,
            None if isinstance(version, str) else "plugin manifest has no version string")


def collect(root: Path | None = None, contract_id: str | None = None) -> dict[str, Any]:
    root = (PLUGIN_ROOT if root is None else Path(root)).resolve()
    name, version, version_error = plugin_version(root)
    identity: dict[str, Any] = {"schema_version": SCHEMA_VERSION, "frozen_at": time.time(), "plugin_root": str(root),
                                "plugin_name": name, "plugin_version": version, "bridge_contract_id": contract_id}
    if version_error:
        identity["plugin_version_error"] = version_error
    try:
        source = git_source(root)
        if source is None or source.get("kind") == "unknown":
            release = release_manifest_source(root)
            if release is not None:
                if source and source.get("error"):
                    release["git_probe_error"] = source["error"]
                source = release
    except Exception as exc:  # identity must never stop a run
        source = {"kind": "unknown", "error": f"source lookup failed: {type(exc).__name__}"}
    identity["source"] = source or {"kind": "unknown", "revision": None, "state": "unknown",
                                    "note": "no verified Git source or release manifest is available; nothing is inferred"}
    try:
        identity["code_digest"] = code_digest(root)
    except OSError as exc:
        identity["code_digest"] = {"algorithm": "sha256", "value": None, "scope": CODE_DIGEST_SCOPE,
                                   "error": f"code digest unavailable: {type(exc).__name__}"}
    return identity


def load_frozen(run_dir: Path) -> dict[str, Any] | None:
    """Return the run's frozen identity; legacy runs and unreadable files give None."""
    try:
        value = json.loads((Path(run_dir) / FILE_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) and value.get("schema_version") == SCHEMA_VERSION else None


def freeze(run_dir: Path, contract_id: str | None = None, root: Path | None = None) -> dict[str, Any]:
    """Collect once and persist without replacing an identity frozen earlier."""
    run_dir = Path(run_dir)
    existing = load_frozen(run_dir)
    if existing is not None:
        return existing
    try:
        identity = collect(root, contract_id)
    except Exception as exc:  # identity must never stop a run
        identity = {"schema_version": SCHEMA_VERSION, "frozen_at": time.time(), "bridge_contract_id": contract_id,
                    "source": {"kind": "unknown", "revision": None, "state": "unknown"},
                    "collect_error": f"{type(exc).__name__}: {exc}"}
    target = run_dir / FILE_NAME
    temporary = run_dir / f".{FILE_NAME}.{uuid.uuid4().hex}.tmp"
    try:
        temporary.write_text(json.dumps(identity, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.link(temporary, target)
    except FileExistsError:
        return load_frozen(run_dir) or identity
    except OSError as exc:
        identity["freeze_error"] = f"identity file not persisted: {type(exc).__name__}"
    finally:
        temporary.unlink(missing_ok=True)
    return identity


def receipt_fields(run_dir: Path) -> dict[str, Any]:
    """The field every receipt and result carries; legacy or unreadable files stay explicit."""
    identity = load_frozen(run_dir)
    if identity is None:
        identity = {"status": "unavailable", "reason": f"{FILE_NAME} is missing or unreadable"}
    return {"plugin_identity": identity}
