"""Bundled coordination guidance and immutable historical task snapshots.

New tasks use only the guidance shipped with the installed plugin. Historical
approved snapshots remain readable and verifiable; activation state is neither
consulted for dispatch nor rewritten. No network content update is supported.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
from shared_io import read_regular
import re
import secrets
import shutil
import stat
import sys
import threading
import time
from typing import Any, Iterator

SCHEMA = "codex-claude-orchestrator.content/v1"
CONTENT_API = 1
# Legacy requirement labels remain understood for pinned snapshots. They do
# not expose or reactivate the retired update methods.
CAPABILITIES = frozenset({"check", "read", "review", "disable", "rollback", "task_pin", "workflow_report_v1", "workflow_report_json_v1"})
BUNDLED_DIR = Path(__file__).resolve().parents[1] / "skills/codex-claude-orchestrator/references"
MANIFEST_NAME = "manifest.json"
MAX_REVIEW_BYTES = 256 * 1024
MAX_MANIFEST_BYTES = 64 * 1024
MAX_FILES = 32
MAX_FILE_BYTES = 256 * 1024
MAX_TOTAL_BYTES = 1024 * 1024
HISTORY_LIMIT = 500
MANIFEST_KEYS = frozenset({"schema", "content_version", "entry", "protocol_reference", "requires", "files"})
FILE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}\.md")
DIGEST = re.compile(r"[0-9a-f]{64}")
VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,63}")
TRUSTED_DECISIONS = frozenset({"approved", "bundled"})
_THREAD_LOCK = threading.Lock()

class ContentError(RuntimeError):
    pass

def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def parse_manifest(data: bytes) -> dict[str, Any]:
    """Validate the exact manifest shape; unknown keys (for example `approved`) are refused."""
    if len(data) > MAX_MANIFEST_BYTES:
        raise ContentError("manifest exceeds its byte limit")
    try:
        value = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ContentError("manifest is not UTF-8 JSON") from exc
    if not isinstance(value, dict) or set(value) != MANIFEST_KEYS:
        raise ContentError("manifest fields must be exactly: " + ", ".join(sorted(MANIFEST_KEYS)))
    if value["schema"] != SCHEMA:
        raise ContentError(f"unsupported manifest schema: {value['schema']!r}")
    version = value["content_version"]
    if not isinstance(version, str) or not VERSION.fullmatch(version):
        raise ContentError("content_version must be a short version label")
    entries = value["files"]
    if not isinstance(entries, list) or not 1 <= len(entries) <= MAX_FILES:
        raise ContentError(f"files must list 1..{MAX_FILES} Markdown files")
    files: dict[str, str] = {}
    folded: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {"path", "sha256"}:
            raise ContentError("each file entry must contain exactly path and sha256")
        path, digest = entry["path"], entry["sha256"]
        if not isinstance(path, str) or not FILE_NAME.fullmatch(path):
            raise ContentError(f"file path must be a flat .md name: {path!r}")
        if not isinstance(digest, str) or not DIGEST.fullmatch(digest):
            raise ContentError(f"file sha256 must be lowercase hex: {path}")
        if path.casefold() in folded:
            raise ContentError(f"duplicate file path: {path}")
        folded.add(path.casefold())
        files[path] = digest
    for key in ("entry", "protocol_reference"):
        if not isinstance(value[key], str) or value[key] not in files:
            raise ContentError(f"{key} must name a declared file")
    requires = value["requires"]
    if not isinstance(requires, dict) or set(requires) != {"content_api", "capabilities"}:
        raise ContentError("requires must contain exactly content_api and capabilities")
    api = requires["content_api"]
    if type(api) is not int or api < 1:
        raise ContentError("requires.content_api must be a positive integer")
    capabilities = requires["capabilities"]
    if (not isinstance(capabilities, list) or not all(isinstance(item, str) and item for item in capabilities)
            or len(set(capabilities)) != len(capabilities)):
        raise ContentError("requires.capabilities must be unique names")
    return {"content_version": version, "entry": value["entry"], "protocol_reference": value["protocol_reference"],
            "requires": {"content_api": api, "capabilities": list(capabilities)}, "files": files}


def compatibility_issue(manifest: dict[str, Any]) -> str | None:
    requires = manifest["requires"]
    if requires["content_api"] > CONTENT_API:
        return f"content requires interface {requires['content_api']}; this plugin provides {CONTENT_API}"
    missing = sorted(set(requires["capabilities"]) - CAPABILITIES)
    if missing:
        return "content requires unavailable capabilities: " + ", ".join(missing)
    return None


def _text(path: str, data: bytes) -> str:
    if len(data) > MAX_FILE_BYTES:
        raise ContentError(f"{path} exceeds the per-file byte limit")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ContentError(f"{path} is not UTF-8 text") from exc
    if "\x00" in text:
        raise ContentError(f"{path} contains NUL bytes")
    return text


def verify_material(manifest_bytes: bytes, files: dict[str, bytes]) -> dict[str, Any]:
    manifest = parse_manifest(manifest_bytes)
    if set(files) != set(manifest["files"]):
        raise ContentError("material does not match the declared file list")
    total = len(manifest_bytes)
    for path, expected in manifest["files"].items():
        data = files[path]
        _text(path, data)
        total += len(data)
        if _sha256(data) != expected:
            raise ContentError(f"{path} does not match its declared sha256")
    if total > MAX_TOTAL_BYTES:
        raise ContentError("content exceeds the total byte limit")
    return manifest


def _read_regular(path: Path, limit: int) -> bytes:
    try:
        return read_regular(path, limit)
    except ValueError as exc:
        raise ContentError(str(exc)) from exc


def read_directory(directory: Path) -> tuple[bytes, dict[str, bytes]]:
    """Read only the manifest and its declared files; the directory is never modified."""
    try:
        info = directory.lstat()
    except OSError as exc:
        raise ContentError(f"content directory is unavailable: {directory}") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise ContentError(f"content directory must be a real directory: {directory}")
    manifest_bytes = _read_regular(directory / MANIFEST_NAME, MAX_MANIFEST_BYTES)
    manifest = parse_manifest(manifest_bytes)
    return manifest_bytes, {path: _read_regular(directory / path, MAX_FILE_BYTES) for path in manifest["files"]}


def _write_new_file(path: Path, data: bytes) -> None:
    """Create an immutable record; an existing file is never replaced."""
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(6)}.tmp")
    temporary.write_bytes(data)
    try:
        os.link(temporary, path)
    except FileExistsError as exc:
        raise ContentError(f"{path.name} already exists and is immutable") from exc
    finally:
        temporary.unlink(missing_ok=True)


def _dump_json(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _check_digest(value: Any) -> str:
    if not isinstance(value, str) or not DIGEST.fullmatch(value):
        raise ContentError("digest must be a lowercase SHA-256 content identity")
    return value


class ContentStore:
    """Only immutable snapshots/reviews are written under the Runtime content root."""

    def __init__(self, root: Path, bundled_dir: Path | None = None):
        self.root = Path(root).expanduser().absolute()
        self.bundled_dir = Path(bundled_dir) if bundled_dir is not None else BUNDLED_DIR
        self.snapshots = self.root / "snapshots"
        self.reviews = self.root / "reviews"

    @contextmanager
    def _locked(self) -> Iterator[None]:
        for directory in (self.root, self.snapshots, self.reviews):
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        with _THREAD_LOCK, (self.root / "lock").open("a+") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _review(self, digest: str) -> tuple[dict[str, Any] | None, str | None]:
        path = self.reviews / f"{digest}.json"
        try:
            data = _read_regular(path, MAX_REVIEW_BYTES)
        except ContentError:
            if os.path.lexists(path):
                raise
            return None, None
        try:
            value = json.loads(data)
        except ValueError as exc:
            raise ContentError(f"review record for {digest[:12]} is malformed") from exc
        if not isinstance(value, dict) or value.get("digest") != digest or value.get("decision") not in {"approved", "rejected", "bundled"}:
            raise ContentError(f"review record for {digest[:12]} is malformed")
        return value, _sha256(data)


    @staticmethod
    def _verify_dir(directory: Path, digest: str, extra: frozenset[str] = frozenset()) -> tuple[dict[str, Any], bytes, dict[str, bytes]]:
        """Re-hash a stored copy; any tampering, symlink or undeclared entry is refused."""
        try:
            info = directory.lstat()
        except OSError as exc:
            raise ContentError(f"stored content {digest[:12]} is missing") from exc
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise ContentError(f"stored content {digest[:12]} is not a real directory")
        manifest_bytes = _read_regular(directory / MANIFEST_NAME, MAX_MANIFEST_BYTES)
        if _sha256(manifest_bytes) != digest:
            raise ContentError(f"stored content {digest[:12]} manifest was modified")
        manifest = parse_manifest(manifest_bytes)
        entries = set(os.listdir(directory))
        allowed = {MANIFEST_NAME, *manifest["files"], *extra}
        if not entries <= allowed or not {MANIFEST_NAME, *manifest["files"]} <= entries:
            raise ContentError(f"stored content {digest[:12]} has missing or undeclared entries")
        files = {path: _read_regular(directory / path, MAX_FILE_BYTES) for path in manifest["files"]}
        try:
            verify_material(manifest_bytes, files)
        except ContentError as exc:
            raise ContentError(f"stored content {digest[:12]} failed verification: {exc}") from exc
        return manifest, manifest_bytes, files


    def _materialize(self, parent: Path, digest: str, manifest_bytes: bytes, files: dict[str, bytes]) -> Path:
        target = parent / digest
        if os.path.lexists(target):
            self._verify_dir(target, digest)
            return target
        temporary = parent / f".tmp-{digest[:12]}-{secrets.token_hex(6)}"
        temporary.mkdir(mode=0o700)
        try:
            for name, data in {MANIFEST_NAME: manifest_bytes, **files}.items():
                with (temporary / name).open("xb") as handle:
                    handle.write(data)
            os.rename(temporary, target)
        except OSError:
            shutil.rmtree(temporary, ignore_errors=True)
            if not os.path.lexists(target):
                raise
        self._verify_dir(target, digest)
        return target

    def _bundled(self) -> tuple[str, dict[str, Any], bytes, dict[str, bytes]]:
        manifest_bytes, files = read_directory(self.bundled_dir)
        manifest = verify_material(manifest_bytes, files)
        issue = compatibility_issue(manifest)
        if issue:
            raise ContentError(f"bundled content is incompatible: {issue}")
        return _sha256(manifest_bytes), manifest, manifest_bytes, files


    def _ensure_bundled_snapshot(self) -> tuple[str, dict[str, Any]]:
        digest, manifest, manifest_bytes, files = self._bundled()
        review, _ = self._review(digest)
        if review is None:
            record = {"schema_version": 1, "digest": digest, "decision": "bundled", "recorded_at": time.time(),
                      "content_version": manifest["content_version"],
                      "source": {"kind": "bundled", "directory": str(self.bundled_dir)},
                      "reason": "shipped inside the installed plugin; trusted as part of the installation"}
            try:
                _write_new_file(self.reviews / f"{digest}.json", _dump_json(record))
            except ContentError:
                pass  # a concurrent writer created the same immutable record
            review, _ = self._review(digest)
        if review is None or review["decision"] not in TRUSTED_DECISIONS:
            raise ContentError("bundled content identity has a conflicting review record")
        self._materialize(self.snapshots, digest, manifest_bytes, files)
        return digest, manifest


    def _binding(self, digest: str, mode: str, fallback_reason: str | None = None) -> dict[str, Any]:
        review, review_sha = self._review(digest)
        if review is None or review["decision"] not in TRUSTED_DECISIONS:
            raise ContentError(f"content {digest[:12]} has no approved review; it cannot be used for a task")
        manifest, _, _ = self._verify_dir(self.snapshots / digest, digest)
        issue = compatibility_issue(manifest)
        if issue:
            raise ContentError(issue)
        return {"schema_version": 1, "digest": digest, "mode": mode, "fallback_reason": fallback_reason,
                "origin": (review.get("source") or {}).get("kind"), "source": review.get("source"),
                "content_version": manifest["content_version"], "entry": manifest["entry"],
                "protocol_reference": manifest["protocol_reference"], "files": manifest["files"],
                "snapshot_dir": str(self.snapshots / digest),
                "review": {"decision": review["decision"], "recorded_at": review.get("recorded_at"),
                           "record_sha256": review_sha},
                "pinned_at": time.time()}


    def pin(self, expected_digest: str | None = None, *, require_read: bool = False) -> dict[str, Any]:
        """Pin shipped guidance for a fresh task, irrespective of legacy activation state."""
        with self._locked():
            digest, _ = self._ensure_bundled_snapshot()
            if expected_digest is not None and _check_digest(expected_digest) != digest:
                raise ContentError("bundled coordination content changed since it was read; read the guide again")
            return self._binding(digest, "bundled")

    def check(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        raise ContentError("online coordination content updates are retired; guidance ships with the plugin")

    def review(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        raise ContentError("coordination content activation is retired; historical reviews are immutable evidence")

    def switch(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        raise ContentError("coordination content switching is retired; new tasks always use bundled guidance")

    def read(self, path: str | None = None, digest: str | None = None) -> dict[str, Any]:
        """Read shipped guidance by default, or an explicitly requested historical snapshot."""
        if digest is None:
            digest, manifest, _, files = self._bundled()
            trust = "bundled"
        else:
            digest = _check_digest(digest)
            if os.path.lexists(self.snapshots / digest):
                review, _ = self._review(digest)
                if review is None or review["decision"] not in TRUSTED_DECISIONS:
                    raise ContentError("historical content has no trusted immutable review")
                manifest, _, files = self._verify_dir(self.snapshots / digest, digest)
                trust = "reviewed" if review["decision"] == "approved" else "bundled"
            else:
                bundled_digest, manifest, _, files = self._bundled()
                if digest != bundled_digest:
                    raise ContentError("no stored content has this digest")
                trust = "bundled"
        name = path if path is not None else manifest["entry"]
        if not isinstance(name, str) or name not in manifest["files"]:
            raise ContentError(f"{name!r} is not declared by this content; declared: {', '.join(manifest['files'])}")
        return {"digest": digest, "path": name, "sha256": manifest["files"][name], "trust": trust,
                "content_version": manifest["content_version"], "entry": manifest["entry"],
                "protocol_reference": manifest["protocol_reference"], "files": sorted(manifest["files"]),
                "text": files[name].decode("utf-8"),
                "note": "Guidance for Codex; user requirements, approved protocols and task acceptance remain authoritative."}

    def status(self) -> dict[str, Any]:
        try:
            digest, manifest, _, _ = self._bundled()
            effective = {"mode": "bundled", "digest": digest, "content_version": manifest["content_version"],
                         "origin": "bundled", "verified": True}
        except ContentError as exc:
            effective = {"mode": "bundled", "digest": None, "verified": False, "error": str(exc)}
        known = []
        for record_path in sorted(self.reviews.glob("*.json"))[-HISTORY_LIMIT:]:
            digest = record_path.stem
            if not DIGEST.fullmatch(digest):
                continue
            try:
                review, _ = self._review(digest)
            except ContentError as exc:
                known.append({"digest": digest, "decision": "unreadable", "error": str(exc)})
                continue
            if review is not None:
                known.append({"digest": digest, "decision": review["decision"], "content_version": review.get("content_version"),
                              "source": review.get("source"), "recorded_at": review.get("recorded_at")})
        return {"content_root": str(self.root), "source": {"kind": "bundled", "bundled_dir": str(self.bundled_dir)},
                "effective": effective, "online_updates": "retired", "reviewed_content": known,
                "limits": {"files": MAX_FILES, "file_bytes": MAX_FILE_BYTES, "total_bytes": MAX_TOTAL_BYTES},
                "claude_started": False, "note": "New tasks use shipped guidance; explicitly pinned historical snapshots remain readable."}

def binding_summary(binding: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(binding, dict):
        return None
    source = binding.get("source") if isinstance(binding.get("source"), dict) else {}
    return {"digest": binding.get("digest"), "mode": binding.get("mode"), "origin": binding.get("origin"),
            "content_version": binding.get("content_version"), "fallback_reason": binding.get("fallback_reason"),
            "source_commit": source.get("commit"), "review_decision": (binding.get("review") or {}).get("decision")}


def verify_binding(binding: Any) -> dict[str, Any]:
    """Re-verify a pinned task identity against its immutable snapshot and review record."""
    if not isinstance(binding, dict) or binding.get("schema_version") != 1:
        raise ContentError("content binding is malformed")
    digest = _check_digest(binding.get("digest"))
    snapshot = binding.get("snapshot_dir")
    if not isinstance(snapshot, str) or not Path(snapshot).is_absolute():
        raise ContentError("content binding has no absolute snapshot path")
    snapshot_dir = Path(snapshot)
    if snapshot_dir.name != digest or snapshot_dir.parent.name != "snapshots":
        raise ContentError("content binding snapshot path does not match its digest")
    store = ContentStore(snapshot_dir.parent.parent)
    review, review_sha = store._review(digest)
    expected_review = binding.get("review") if isinstance(binding.get("review"), dict) else {}
    if review is None or review["decision"] not in TRUSTED_DECISIONS or review_sha != expected_review.get("record_sha256"):
        raise ContentError("content binding review record is missing or changed")
    manifest, _, _ = store._verify_dir(snapshot_dir, digest)
    issue = compatibility_issue(manifest)
    if issue:
        raise ContentError(issue)
    if (manifest["files"] != binding.get("files") or manifest["entry"] != binding.get("entry")
            or manifest["protocol_reference"] != binding.get("protocol_reference")
            or manifest["content_version"] != binding.get("content_version")):
        raise ContentError("content binding does not match its snapshot manifest")
    return binding_summary(binding) or {}


def write_manifest(directory: Path) -> str:
    """Refresh declared file hashes of a source directory's manifest; returns the new digest."""
    path = directory / MANIFEST_NAME
    raw = _read_regular(path, MAX_MANIFEST_BYTES)
    parse_manifest(raw)
    value = json.loads(raw)
    for entry in value["files"]:
        entry["sha256"] = _sha256(_read_regular(directory / entry["path"], MAX_FILE_BYTES))
    data = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    verify_material(data, {entry["path"]: _read_regular(directory / entry["path"], MAX_FILE_BYTES) for entry in value["files"]})
    path.write_bytes(data)
    return _sha256(data)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Maintain or verify a coordination content source directory.")
    parser.add_argument("command", choices=("verify", "write-manifest"))
    parser.add_argument("--dir", type=Path, default=BUNDLED_DIR)
    args = parser.parse_args(argv)
    try:
        if args.command == "write-manifest":
            digest = write_manifest(args.dir)
        else:
            manifest_bytes, files = read_directory(args.dir)
            verify_material(manifest_bytes, files)
            digest = _sha256(manifest_bytes)
    except (ContentError, OSError, ValueError, KeyError) as exc:
        print(f"content error: {exc}", file=sys.stderr)
        return 1
    print(json.dumps({"directory": str(args.dir), "digest": digest}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
