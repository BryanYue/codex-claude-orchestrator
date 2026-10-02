"""Read, cancel and reconcile qualification jobs from earlier plugin versions.

New qualification and managed CLI selection are retired. This module launches
no worker or probe, creates no qualification receipt and never changes CLI
selection. Read-only status leaves records untouched; explicit legacy status
can finish bookkeeping only after positively confirming process cleanup.
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import secrets
import time
from pathlib import Path
from typing import Any, Callable, Mapping

import cli_store

SCHEMA_VERSION = 1
GROUPS = ("core", "read_only", "write", "resume", "workflow")
FINAL_JOB_STATUS = {"completed", "cancelled"}
TERMINAL_RECEIPTS = {"reported", "blocked", "failed", "cancelled", "timeout"}
JOB_ID = re.compile(r"^qualification-[A-Za-z0-9_-]{16,64}$")


def _root(environ: Mapping[str, str] | None = None) -> Path:
    return cli_store.store_root(environ=environ)


def _jobs_root(environ: Mapping[str, str] | None = None) -> Path:
    return _root(environ) / "jobs"


def _job_dir(job_id: str, environ: Mapping[str, str] | None = None) -> Path:
    if not isinstance(job_id, str) or not JOB_ID.fullmatch(job_id):
        raise ValueError("invalid qualification job id")
    return _jobs_root(environ) / job_id


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(6)}.tmp")
    try:
        with temporary.open("xb") as handle:
            handle.write(_json_bytes(value))
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(0o600)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"qualification state is unavailable: {path.name}") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"qualification state is malformed: {path.name}")
    return value


def _with_state(job_dir: Path, mutate: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
    lock_path = job_dir / "state.lock"
    lock_path.touch(mode=0o600, exist_ok=True)
    with lock_path.open("a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        state = _load_json(job_dir / "job.json")
        mutate(state)
        state["updated_at"] = time.time()
        _atomic_json(job_dir / "job.json", state)
        return state


def _event(job_dir: Path, phase: str, message: str, **fields: Any) -> dict[str, Any]:
    entry = {"at": time.time(), "phase": phase, "message": message, **fields}
    lock_path = job_dir / "events.lock"
    lock_path.touch(mode=0o600, exist_ok=True)
    with lock_path.open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        with (job_dir / "events.jsonl").open("a", encoding="utf-8") as output:
            output.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")
            output.flush()
            os.fsync(output.fileno())
    return entry


def _events(job_dir: Path) -> list[dict[str, Any]]:
    try:
        lines = (job_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    result = []
    for line in lines:
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if isinstance(item, dict):
            result.append(item)
    return result


def _matrix_default(reason: str = "not_requested") -> dict[str, dict[str, Any]]:
    return {name: {"status": "inconclusive", "reason": reason, "evidence": []} for name in GROUPS}


def _write_terminal_report(job_dir: Path, state: dict[str, Any], *, status_: str, phase: str,
                           outcome: str, matrix: dict[str, Any], reason: str,
                           results: Mapping[str, dict[str, Any]] | None = None,
                           cleanup_status: str | None = None,
                           active_scenario: dict[str, Any] | None = None) -> Path:
    current = _load_json(job_dir / "job.json")
    active = active_scenario if active_scenario is not None else current.get("active_scenario")
    cleanup: dict[str, Any] = {"status": cleanup_status or "not_applicable", "active_scenario": active}
    if isinstance(active, dict):
        lifecycle = _safe_json(Path(active.get("lifecycle_path", "")))
        run_dir = Path(active.get("run_dir", ""))
        child = _safe_json(run_dir / "child.json")
        cleanup.update(
            lifecycle={key: lifecycle.get(key) for key in
                       ("run_id", "task_id", "revision", "phase", "status", "terminal", "child_started", "bridge_pid")},
            receipt_status=_safe_json(run_dir / "receipt.json").get("status"),
            bridge_state=_presence(lifecycle.get("bridge_pid") or active.get("bridge_pid"), group=False),
            child_state=_presence(child.get("process_group")) if child else "not_started_or_unrecorded",
        )
    report = {
        "schema_version": SCHEMA_VERSION, "status": status_, "phase": phase,
        "job_id": state["job_id"], "identity_id": state["identity_id"],
        "identity_sha256": state.get("identity_sha256"),
        "contract_id": state.get("contract_id"), "bridge_contract_id": state.get("contract_id"),
        "suite_revision": state.get("suite_revision"), "groups": state.get("groups", []),
        "matrix": matrix, "outcome": outcome, "reason": reason,
        **coverage(state.get("groups", []), matrix),
        "scenarios": dict(results or {}), "cleanup": cleanup, "finished_at": time.time(),
        "note": "qualification did not create compatibility evidence",
    }
    path = job_dir / "report.json"
    _atomic_json(path, report)
    return path


def coverage(groups: list[str], matrix: dict[str, Any]) -> dict[str, Any]:
    statuses = [matrix.get(name, {}).get("status") for name in groups]
    outcome = ("pass" if statuses and all(value == "pass" for value in statuses)
               else "fail" if "fail" in statuses else "inconclusive")
    return {"outcome_scope": "core_read_only", "requested_groups_outcome": outcome,
            "missing_groups": [name for name in groups if matrix.get(name, {}).get("status") != "pass"]}


def _public(state: dict[str, Any], job_dir: Path) -> dict[str, Any]:
    active = state.get("active_scenario")
    safe_active = None
    if isinstance(active, dict):
        safe_active = {key: active.get(key) for key in ("scenario", "group", "phase", "run_dir", "lifecycle_path")}
    return {
        "job_id": state["job_id"],
        "identity_id": state["identity_id"],
        "status": state["status"],
        "phase": state.get("phase"),
        "outcome": state.get("outcome"),
        "groups": list(state["groups"]),
        "matrix": state.get("matrix") or _matrix_default(),
        **coverage(state["groups"], state.get("matrix") or _matrix_default()),
        "activate_on_success": state["activate_on_success"],
        "activation": state.get("activation"),
        "active_scenario": safe_active,
        "report_path": str(job_dir / "report.json"),
        "cancel_requested": (job_dir / "cancel.request").is_file(),
        "created_at": state["created_at"],
        "updated_at": state["updated_at"],
        "events": _events(job_dir),
    }


def _lock_available(path: Path) -> bool:
    path.touch(mode=0o600, exist_ok=True)
    with path.open("a+") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return True


def _active_cleanup_state(job_dir: Path, state: dict[str, Any]) -> str:
    """Return stopped/running/unconfirmed without signalling a recorded PID."""
    active = state.get("active_scenario")
    if not isinstance(active, dict):
        return "stopped"
    lifecycle = _safe_json(Path(active.get("lifecycle_path", "")))
    receipt = _safe_json(Path(active.get("run_dir", "")) / "receipt.json")
    child = _safe_json(Path(active.get("run_dir", "")) / "child.json")
    if any(lifecycle.get(key) != active.get(key) for key in
           ("run_id", "task_id", "revision", "cwd", "packet_sha256", "cli_descriptor_sha256")):
        return "unconfirmed"
    bridge_state = _presence(lifecycle.get("bridge_pid") or active.get("bridge_pid"), group=False)
    if bridge_state != "stopped":
        return bridge_state
    if lifecycle.get("terminal") is not True or lifecycle.get("status") not in TERMINAL_RECEIPTS:
        return "unconfirmed"
    if lifecycle.get("child_started") is True:
        if (receipt.get("status") not in TERMINAL_RECEIPTS
                or receipt.get("task_id") != active.get("task_id")
                or receipt.get("revision") != active.get("revision")):
            return "unconfirmed"
        group = child.get("process_group")
        if _presence(group) != "stopped":
            return "unconfirmed"
    elif lifecycle.get("child_started") is not False:
        return "unconfirmed"
    return "stopped"


def _reconcile_lost_worker(job_dir: Path, state: dict[str, Any]) -> dict[str, Any]:
    cleanup = _active_cleanup_state(job_dir, state)
    if cleanup == "stopped":
        if state.get("phase") == "committing":
            recovered = _recover_committing(job_dir, state)
            if recovered is not None:
                return recovered
        prior_active = state.get("active_scenario")
        matrix = _matrix_default("worker_lost")
        def lost(value: dict[str, Any]) -> None:
            if value.get("status") not in FINAL_JOB_STATUS:
                value.update(status="completed", phase="worker_lost", outcome="inconclusive",
                             active_scenario=None,
                             failure={"kind": "worker_lost", "reason": "qualification worker ended without a final report"})
        state = _with_state(job_dir, lost)
        _write_terminal_report(job_dir, state, status_="completed", phase="worker_lost",
                               outcome="inconclusive", matrix=matrix,
                               reason="qualification worker ended without a final report",
                               cleanup_status="confirmed", active_scenario=prior_active)
        _event(job_dir, "worker_lost", "qualification worker ended without a final report")
        return state
    def needs_cleanup(value: dict[str, Any]) -> None:
        if value.get("status") not in FINAL_JOB_STATUS:
            value.update(status="needs_cleanup", phase="needs_cleanup", outcome="inconclusive",
                         failure={"kind": "worker_lost", "reason": "an orphaned qualification bridge has not confirmed cleanup"})
    state = _with_state(job_dir, needs_cleanup)
    if state.get("phase") == "needs_cleanup" and not any(
            event.get("phase") == "needs_cleanup" for event in _events(job_dir)):
        _event(job_dir, "needs_cleanup", "orphaned qualification bridge cleanup is not confirmed")
    return state


def status(job_id: str, environ: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Read persisted progress; never runs a probe or changes CLI selection."""
    job_dir = _job_dir(job_id, environ)
    state = _load_json(job_dir / "job.json")
    if state.get("status") not in FINAL_JOB_STATUS and _lock_available(job_dir / "worker.lock"):
        state = _reconcile_lost_worker(job_dir, state)
    return _public(state, job_dir)


def _worker_lock_observation(path: Path) -> str:
    """Probe an existing lock through a read-only descriptor; never create it."""
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        return "absent"
    except OSError:
        return "unreadable"
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            return "held"
        except OSError:
            return "unreadable"
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        return "free"
    finally:
        os.close(descriptor)


def read_status(job_id: str, environ: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Report recorded history exactly as stored, for diagnostics and MCP status.

    Unlike ``status``, this never creates a lock file, reconciles a lost
    worker, recovers a committing job, writes a report/event or touches CLI
    selection.  A nonterminal record stays nonterminal here; only an explicit
    ``cancel`` (or legacy ``status``) may change retired validation history.
    """
    job_dir = _job_dir(job_id, environ)
    state = _load_json(job_dir / "job.json")
    answer = _public(state, job_dir)
    answer["read_only"] = True
    if state.get("status") not in FINAL_JOB_STATUS:
        answer["worker_lock"] = _worker_lock_observation(job_dir / "worker.lock")
        answer["reconciliation"] = "not_performed"
        answer["note"] = ("recorded state is shown unchanged; a stopped worker is not reconciled by a "
                          "read-only status query")
    return answer


def cancel(job_id: str, environ: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Persist cancellation for the owning worker; never signal a recorded PID."""
    job_dir = _job_dir(job_id, environ)
    if not (job_dir / "job.json").is_file():
        raise ValueError(f"Unknown qualification job: {job_id}")
    accepted = False
    request: dict[str, Any] = {}
    def requested(value: dict[str, Any]) -> None:
        nonlocal accepted, request
        if value.get("status") in FINAL_JOB_STATUS or value.get("phase") == "committing":
            return
        request = {"job_id": job_id, "requested_at": time.time(), "nonce": value["nonce"]}
        _atomic_json(job_dir / "cancel.request", request)
        value.update(status="cancel_requested", phase="cancelling", cancel_requested_at=request["requested_at"])
        accepted = True
    try:
        state = _with_state(job_dir, requested)
    except FileNotFoundError as exc:
        # The job can disappear between the existence check and lock creation.
        raise ValueError(f"Qualification job is no longer available: {job_id}") from exc
    if not accepted:
        answer = _public(state, job_dir)
        answer.update(cancel_accepted=False,
                      note="validation is already committing or terminal; inspect its completion")
        return answer
    active = state.get("active_scenario")
    if isinstance(active, dict):
        _request_bridge_cancel(Path(active["run_dir"]), Path(active["external_cancel"]), job_id)
    _event(job_dir, "cancelling", "qualification cancellation requested")
    answer = _public(state, job_dir)
    answer["cancel_accepted"] = True
    return answer


def _presence(pid: Any, *, group: bool = True) -> str:
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return "unconfirmed"
    try:
        (os.killpg if group else os.kill)(pid, 0)
    except ProcessLookupError:
        return "stopped"
    except (PermissionError, OSError):
        return "unconfirmed"
    return "running"


def _request_bridge_cancel(run_dir: Path, external_cancel: Path, job_id: str) -> None:
    value = {"reason": "qualification job cancellation", "requested_at": time.time(), "requested_by": job_id}
    _atomic_json(external_cancel, value)
    if run_dir.is_dir():
        _atomic_json(run_dir / "cancel.json", value)


def _safe_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _success_report(job_dir: Path, state: dict[str, Any]) -> dict[str, Any] | None:
    report = _safe_json(job_dir / "report.json")
    if (report.get("status") != "completed" or report.get("job_id") != state.get("job_id")
            or report.get("identity_id") != state.get("identity_id")
            or report.get("identity_sha256") != state.get("identity_sha256")
            or report.get("bridge_contract_id") != state.get("contract_id")
            or set(report.get("matrix", {})) != set(GROUPS)):
        return None
    return report


def _recover_committing(job_dir: Path, state: dict[str, Any]) -> dict[str, Any] | None:
    """Preserve a completed historical report without renewing retired authority."""
    report = _success_report(job_dir, state)
    if report is None:
        return None
    def recovered(value: dict[str, Any]) -> None:
        if value.get("status") in FINAL_JOB_STATUS or value.get("phase") != "committing":
            return
        value.update(status="completed", phase="completed", outcome=report["outcome"], matrix=report["matrix"],
                     active_scenario=None, finished_at=time.time())
        if value.get("activation") is None:
            value["activation"] = {"status": "not_activated",
                                   "reason": "managed CLI activation is retired; historical report preserved"}
    recovered_state = _with_state(job_dir, recovered)
    _event(job_dir, "completed", "historical qualification report preserved; no receipt or activation created",
           outcome=report["outcome"])
    return recovered_state
