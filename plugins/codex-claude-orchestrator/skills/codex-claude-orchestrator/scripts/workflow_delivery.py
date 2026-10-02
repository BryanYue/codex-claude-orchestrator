"""Saved-Workflow report transport for ``workflow_review`` runs.

Claude Code (observed in CLI 2.1.287) reports a background Workflow's end as a
structured ``system/task_notification`` whose ``output_file`` is a public
task-output JSON envelope (summary, logs, result, ...), written below
``CLAUDE_CODE_TMPDIR``.  The bridge points that variable, for its own Claude
child only, at a private root it created and snapshots the bound file once the
child's process group is proven stopped.

Only the observed layout ``<root>/claude-<uid>/<cwd encoding>/<session>/tasks/
<task>.output`` is accepted; anything else is an explicit unsupported or
unavailable state, never a guess at another provider path.  Captured bytes
prove provenance and complete transport, not that the report is correct.
"""
from __future__ import annotations

import hashlib
import errno
import json
import os
import re
import shutil
import stat
import tempfile
from pathlib import Path
from typing import Any

TMPDIR_ENV = "CLAUDE_CODE_TMPDIR"
ADAPTER = "claude_code_task_output_file_v1"
OUTPUT_DIR = "workflow-output"
MAX_OUTPUT_BYTES = 8 * 1024 * 1024
DEFAULT_PAGE_BYTES = 64 * 1024
MAX_PAGE_BYTES = 256 * 1024
# One UTF-8 character is at most four bytes, so every page makes progress.
MIN_PAGE_BYTES = 4
PARTS = {"report": "report.txt", "envelope": "envelope.json"}
_TASK_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_REPORT_ACCESS = "claude_result(run_id, artifact='workflow_report', index=N, offset=0), then follow next_offset_bytes"


class DeliveryError(ValueError):
    """A request for a captured Workflow artifact is malformed."""


def create_root() -> dict[str, Any]:
    """Create the private temporary root that only this run's Claude child receives."""
    path = Path(tempfile.mkdtemp(prefix="ccw-")).resolve(strict=True)
    info = os.lstat(path)
    return {"env": TMPDIR_ENV, "path": str(path), "device": info.st_dev, "inode": info.st_ino}


def _intact_root(root: Any) -> Path | None:
    if not isinstance(root, dict) or not isinstance(root.get("path"), str):
        return None
    path = Path(root["path"])
    try:
        info = os.lstat(path)
    except OSError:
        return None
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or (info.st_dev, info.st_ino) != (root.get("device"), root.get("inode"))):
        return None
    return path


def remove_root(root: Any) -> str:
    """Remove the directory this run created, never a path that has since replaced it."""
    path = _intact_root(root)
    if path is None:
        return "kept: the root is missing or is no longer the directory this run created"
    try:
        shutil.rmtree(path)
    except OSError as exc:
        return f"kept: {type(exc).__name__}"
    return "removed"


def artifact_names(index: int) -> dict[str, str]:
    return {part: f"{OUTPUT_DIR}/{index}.{suffix}" for part, suffix in PARTS.items()}


def _failure(status: str, code: str, detail: str, **extra: Any) -> dict[str, Any]:
    return {"status": status, "reason_code": code, "detail": detail, **extra}


def _same_file(left: os.stat_result, right: os.stat_result) -> bool:
    fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns", "st_mode", "st_uid", "st_nlink")
    return all(getattr(left, field) == getattr(right, field) for field in fields)


def _directory_fd(path: Path, *, parent_fd: int | None = None) -> int:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
    if os.fstat(fd).st_uid != os.getuid():
        os.close(fd)
        raise OSError(errno.EPERM, "artifact directory belongs to another user")
    return fd


def _open_beneath(root: Path, path: Path, flags: int, *, mode: int = 0o600,
                  root_identity: tuple[int, int] | None = None) -> int:
    """Walk untrusted descendants relative to open directories, never through symlinks.

    The root comes from Runtime or create_root; system aliases above that
    trusted anchor (such as macOS /var) are not artifact path components.
    """
    parts = path.relative_to(root).parts
    if not parts or any(part in {".", ".."} for part in parts):
        raise OSError(errno.EINVAL, "invalid artifact path")
    directory = _directory_fd(root)
    try:
        info = os.fstat(directory)
        if root_identity is not None and (info.st_dev, info.st_ino) != root_identity:
            raise OSError(errno.ESTALE, "artifact root was replaced")
        for part in parts[:-1]:
            child = _directory_fd(Path(part), parent_fd=directory)
            os.close(directory)
            directory = child
        return os.open(parts[-1], flags | os.O_NOFOLLOW | os.O_NONBLOCK, mode, dir_fd=directory)
    finally:
        os.close(directory)


def _read_bounded(path: Path, limit: int, *, root: Path,
                  root_identity: tuple[int, int] | None = None) -> tuple[bytes, os.stat_result, os.stat_result]:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    fd = _open_beneath(root, path, flags, root_identity=root_identity)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid() or before.st_nlink != 1:
            raise OSError(errno.EPERM, "artifact is not an owned regular file without aliases")
        if before.st_size > limit:
            raise OSError(errno.EFBIG, "artifact exceeds the bounded read size")
        chunks: list[bytes] = []
        total = 0
        while total <= limit:
            chunk = os.read(fd, min(1024 * 1024, limit + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        after = os.fstat(fd)
    finally:
        os.close(fd)
    return b"".join(chunks), before, after


def _write_once(path: Path, data: bytes, *, root: Path, root_identity: tuple[int, int]) -> None:
    fd = _open_beneath(root, path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, root_identity=root_identity)
    try:
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view):]
        os.fsync(fd)
        os.fchmod(fd, 0o400)
    finally:
        os.close(fd)


def capture(root: Any, *, session_id: str, task_id: Any, output_file: Any, run_dir: Path, index: int,
            binding: dict[str, Any]) -> dict[str, Any]:
    """Snapshot the exact envelope and its text or structured JSON result."""
    if not isinstance(output_file, str) or not output_file or "\x00" in output_file:
        return _failure("unavailable", "output_reference_missing", "the task notification carried no output_file")
    if not isinstance(task_id, str) or not _TASK_ID.fullmatch(task_id):
        return _failure("invalid", "task_binding_invalid", "no safe task_id is bound to this Workflow call")
    base = _intact_root(root)
    if base is None:
        return _failure("unavailable", "temp_root_unavailable",
                        "the run-owned provider temporary root is missing or was replaced")
    if not os.path.isabs(output_file) or os.path.normpath(output_file) != output_file:
        return _failure("unsupported", "output_path_unsupported", "output_file is not an absolute normalized path")
    candidate = Path(output_file)
    try:
        relative = candidate.relative_to(base)
    except ValueError:
        return _failure("unsupported", "output_path_outside_root",
                        "output_file is outside the run-owned provider temporary root")
    parts = relative.parts
    if (len(parts) != 5 or parts[0] != f"claude-{os.getuid()}" or parts[1] in {".", ".."}
            or tuple(parts[2:]) != (session_id, "tasks", f"{task_id}.output")):
        return _failure("unsupported", "output_layout_unsupported",
                        "output_file is not <root>/claude-<uid>/<cwd>/<session>/tasks/<task>.output for this session and task")
    cursor = base
    for part in parts[:-1]:
        cursor = cursor / part
        try:
            info = os.lstat(cursor)
        except FileNotFoundError:
            return _failure("unavailable", "output_missing", "the bound output directory does not exist")
        except OSError as exc:
            return _failure("unavailable", "output_read_error", f"cannot inspect the output directory: {type(exc).__name__}")
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
            return _failure("invalid", "output_path_unsafe",
                            "an output path component is a symlink, not a directory, or owned by another user")
    try:
        info = os.lstat(candidate)
    except FileNotFoundError:
        return _failure("unavailable", "output_missing", "the bound output file does not exist")
    except OSError as exc:
        return _failure("unavailable", "output_read_error", f"cannot inspect the output file: {type(exc).__name__}")
    if stat.S_ISLNK(info.st_mode):
        return _failure("invalid", "output_symlink", "the bound output file is a symbolic link")
    if not stat.S_ISREG(info.st_mode):
        return _failure("invalid", "output_not_regular", "the bound output file is not a regular file")
    if info.st_uid != os.getuid():
        return _failure("invalid", "output_owner_mismatch", "the bound output file is owned by another user")
    if info.st_nlink != 1:
        return _failure("invalid", "output_hardlink_alias", "the bound output file has another hard link")
    if info.st_size > MAX_OUTPUT_BYTES:
        return _failure("invalid", "output_oversize", f"the bound output file exceeds {MAX_OUTPUT_BYTES} bytes")
    try:
        data, before, after = _read_bounded(candidate, MAX_OUTPUT_BYTES, root=base,
                                          root_identity=(root["device"], root["inode"]))
        settled = os.lstat(candidate)
    except OSError as exc:
        return _failure("unavailable", "output_read_error", f"cannot read the output file: {type(exc).__name__}")
    if len(data) > MAX_OUTPUT_BYTES:
        return _failure("invalid", "output_oversize", f"the bound output file exceeds {MAX_OUTPUT_BYTES} bytes")
    if not (_same_file(info, before) and _same_file(before, after) and _same_file(after, settled)
            and len(data) == before.st_size):
        return _failure("invalid", "output_unstable", "the bound output file changed while it was read")
    digest = hashlib.sha256(data).hexdigest()
    source = {"output_file": output_file, "relative_path": relative.as_posix(), "size_bytes": len(data), "sha256": digest}
    try:
        envelope = json.loads(data.decode("utf-8"))
    except UnicodeDecodeError:
        return _failure("invalid", "output_not_utf8", "the output file is not valid UTF-8", source=source)
    except RecursionError:
        return _failure("invalid", "output_json_too_deep", "the output JSON exceeds the decoder nesting limit", source=source)
    except ValueError:
        return _failure("invalid", "output_not_json", "the output file is not JSON", source=source)
    if not isinstance(envelope, dict):
        return _failure("invalid", "output_shape_invalid", "the output file is not a JSON object", source=source)
    report = envelope.get("result")
    value_type = "string" if isinstance(report, str) else "object" if isinstance(report, dict) else "array" if isinstance(report, list) else type(report).__name__
    representation = "exact_text"
    if isinstance(report, (dict, list)):
        # The envelope remains byte-exact; this is an explicitly labelled JSON
        # projection of a structured value, not a substring of its source bytes.
        try:
            report = json.dumps(report, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        except (ValueError, TypeError, RecursionError):
            return _failure("invalid", "output_result_invalid_json", "the structured result is not finite JSON", source=source)
        representation = "json_value"
    elif not isinstance(report, str):
        return _failure("invalid", "output_result_missing", "the output envelope has no text, object or array result",
                        source=source, result_type=value_type)
    if not report.strip():
        return _failure("invalid", "output_result_blank", "the output envelope result is blank", source=source)
    try:
        report_bytes = report.encode("utf-8")
    except UnicodeEncodeError:
        return _failure("invalid", "output_result_invalid_text", "the output envelope result is not encodable text",
                        source=source)
    names = artifact_names(index)
    try:
        directory = _directory_fd(run_dir)
        try:
            info = os.fstat(directory)
            run_identity = (info.st_dev, info.st_ino)
            try:
                os.mkdir(OUTPUT_DIR, mode=0o700, dir_fd=directory)
            except FileExistsError:
                pass  # The descriptor-relative opens below verify the existing directory.
        finally:
            os.close(directory)
        _write_once(run_dir / names["envelope"], data, root=run_dir, root_identity=run_identity)
        _write_once(run_dir / names["report"], report_bytes, root=run_dir, root_identity=run_identity)
    except OSError as exc:
        return _failure("unavailable", "snapshot_write_failed", f"cannot store the snapshot: {type(exc).__name__}",
                        source=source)
    return {"status": "collected", "reason_code": None,
            "detail": "complete output envelope and its explicitly represented result captured from the provider-bound file",
            "source": source,
            "envelope": {"path": names["envelope"], "size_bytes": len(data), "sha256": digest,
                         "top_level_keys": sorted(envelope)[:32]},
            "report": {"path": names["report"], "size_bytes": len(report_bytes), "characters": len(report),
                       "sha256": hashlib.sha256(report_bytes).hexdigest(), "encoding": "utf-8", "field": "result",
                       "representation": representation, "value_type": value_type},
            "binding": binding}


def invocation_reasons(invocation: dict[str, Any]) -> list[str]:
    """Stable reason codes for one bound Workflow call that has not delivered a report."""
    acknowledgement = (invocation.get("acknowledgement") or {}).get("state")
    if acknowledgement == "missing":
        return ["workflow_acknowledgement_missing"]
    if acknowledgement != "succeeded":
        return ["workflow_acknowledgement_failed"]
    terminal = invocation.get("terminal")
    if not isinstance(terminal, dict):
        if invocation.get("notifications_before_acknowledgement"):
            return ["workflow_notification_before_acknowledgement"]
        return ["workflow_terminal_pending"]
    if terminal.get("conflict"):
        return ["workflow_terminal_conflict"]
    if terminal.get("status") != "completed":
        return ["workflow_terminal_failed"]
    if terminal.get("source") != "system_task_notification":
        return ["workflow_report_transport_missing"]
    collection = invocation.get("collection") or {}
    if collection.get("status") == "collected":
        return []
    if collection.get("reason_code") == "output_reference_missing":
        return ["workflow_report_transport_missing"]
    return ["workflow_report_not_collected", str(collection.get("reason_code") or "collection_not_attempted")]


def evaluate(invocations: list[dict[str, Any]], *, final_after_completion: bool,
             final_result_observed: bool) -> dict[str, Any]:
    """Delivery holds only when every bound call completed, was captured, and the parent result followed."""
    reasons: list[str] = []
    if not invocations:
        reasons.append("workflow_invocation_missing")
    for invocation in invocations:
        reasons.extend(invocation_reasons(invocation))
    completed = bool(invocations) and all(isinstance(item.get("terminal"), dict) and item["terminal"].get("status") == "completed"
                                          and not item["terminal"].get("conflict") for item in invocations)
    if not final_result_observed:
        reasons.append("workflow_final_result_missing")
    elif completed and not final_after_completion:
        reasons.append("workflow_final_before_completion")
    unique = sorted(set(reasons))
    return {"status": "delivered" if not unique else "not_delivered", "reason_codes": unique}


def summary(delivery: Any) -> dict[str, Any] | None:
    """Bounded projection of a run's delivery record for status views; content stays paged."""
    if not isinstance(delivery, dict):
        return None
    invocations = [item for item in delivery.get("invocations") or [] if isinstance(item, dict)]
    reports = []
    for item in invocations[:16]:
        collection = item.get("collection") if isinstance(item.get("collection"), dict) else {}
        report = collection.get("report") if isinstance(collection.get("report"), dict) else {}
        reports.append({"index": item.get("index"), "tool_use_id": item.get("tool_use_id"), "task_id": item.get("task_id"),
                        "collection_status": collection.get("status"), "reason_code": collection.get("reason_code"),
                        "total_bytes": report.get("size_bytes"), "total_characters": report.get("characters"),
                        "sha256": report.get("sha256"),
                        "representation": report.get("representation", "exact_text"),
                        "value_type": report.get("value_type", "string")})
    return {"status": delivery.get("status"), "reason_codes": list(delivery.get("reason_codes") or []),
            "invocation_count": len(invocations),
            "collected_count": sum(1 for item in invocations if isinstance(item.get("collection"), dict)
                                   and item["collection"].get("status") == "collected"),
            "reports_truncated": len(invocations) > len(reports),
            "reports": reports, "access": _REPORT_ACCESS,
            "note": ("The full Workflow output is kept separately from the parent summary. Captured bytes prove "
                     "provenance and complete transport only; Codex still verifies the content.")}


def _int(label: str, value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise DeliveryError(f"{label} must be an integer")
    return value


def read_page(run_dir: Path, delivery: Any, *, index: int = 0, part: str = "report", offset: int = 0,
              limit: int = DEFAULT_PAGE_BYTES) -> dict[str, Any]:
    """Return one UTF-8-safe byte range of a captured artifact after re-verifying its whole hash.

    Offsets count bytes of the stored UTF-8 file.  A page never splits a
    character, so joining ``content`` from offset 0 along ``next_offset_bytes``
    reconstructs the captured text exactly.
    """
    if part not in PARTS:
        raise DeliveryError("part must be report or envelope")
    index, offset, limit = _int("index", index), _int("offset", offset), _int("limit", limit)
    if index < 0 or offset < 0:
        raise DeliveryError("index and offset must be non-negative")
    if not MIN_PAGE_BYTES <= limit <= MAX_PAGE_BYTES:
        raise DeliveryError(f"limit must be {MIN_PAGE_BYTES}..{MAX_PAGE_BYTES} bytes")
    answer: dict[str, Any] = {"name": f"workflow_{part}", "index": index, "part": part, "content": None}
    if not isinstance(delivery, dict):
        return {**answer, "available": False, "state": "not_recorded",
                "note": "No Workflow delivery record is available yet. Check the run's role, status and execution evidence; this does not establish whether Workflow ran."}
    invocations = [item for item in delivery.get("invocations") or [] if isinstance(item, dict)]
    answer["invocation_count"] = len(invocations)
    if index >= len(invocations):
        return {**answer, "available": False, "state": "no_such_invocation"}
    collection = invocations[index].get("collection") if isinstance(invocations[index].get("collection"), dict) else {}
    if collection.get("status") != "collected":
        return {**answer, "available": False, "state": "not_collected", "reason_code": collection.get("reason_code"),
                "note": "This Workflow call has no captured artifact; the run did not deliver its full report."}
    recorded = collection.get(part)
    expected_path = artifact_names(index)[part]
    if (not isinstance(recorded, dict) or recorded.get("path") != expected_path
            or not isinstance(recorded.get("size_bytes"), int) or not isinstance(recorded.get("sha256"), str)):
        return {**answer, "available": False, "state": "record_invalid"}
    try:
        folder = os.lstat(run_dir / OUTPUT_DIR)
        if stat.S_ISLNK(folder.st_mode) or not stat.S_ISDIR(folder.st_mode):
            return {**answer, "available": False, "state": "unreadable",
                    "note": "The artifact directory is not a plain directory inside the run record."}
        data, before, after = _read_bounded(run_dir / expected_path, MAX_OUTPUT_BYTES, root=run_dir)
    except FileNotFoundError:
        return {**answer, "available": False, "state": "missing", "note": "The captured artifact file is missing."}
    except OSError as exc:
        return {**answer, "available": False, "state": "unreadable", "note": f"{type(exc).__name__}"}
    if (not stat.S_ISREG(before.st_mode) or not _same_file(before, after) or len(data) != recorded["size_bytes"]
            or hashlib.sha256(data).hexdigest() != recorded["sha256"]):
        return {**answer, "available": False, "state": "integrity_mismatch",
                "note": "The stored artifact no longer matches the size and hash recorded at capture; nothing is served."}
    total = len(data)
    if offset > total:
        raise DeliveryError("offset is beyond the end of the artifact")
    if offset < total and data[offset] & 0xC0 == 0x80:
        raise DeliveryError("offset is not a UTF-8 character boundary; use a returned next_offset_bytes")
    end = min(total, offset + limit)
    while end < total and data[end] & 0xC0 == 0x80:
        end -= 1
    try:
        content = data[offset:end].decode("utf-8")
    except UnicodeDecodeError:
        return {**answer, "available": False, "state": "integrity_mismatch"}
    return {**answer, "available": True, "state": "available", "content": content, "encoding": "utf-8",
            "pagination": "utf8_byte_offsets", "offset_bytes": offset, "page_bytes": end - offset,
            "next_offset_bytes": end if end < total else None, "end_of_artifact": end == total,
            "total_bytes": total, "total_characters": recorded.get("characters"), "sha256": recorded["sha256"],
            "binding": collection.get("binding"),
            "representation": recorded.get("representation", "exact_text" if part == "report" else "exact_envelope"),
            "value_type": recorded.get("value_type"),
            "note": "Captured Workflow output; provenance and byte completeness are verified, semantic correctness is not."}
