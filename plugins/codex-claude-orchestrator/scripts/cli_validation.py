"""Read and cancel persisted qualification history; retired workers are never reconciled."""
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

GROUPS = ("core", "read_only", "write", "resume", "workflow")
FINAL_JOB_STATUS = {"completed", "cancelled"}
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
        before = _json_bytes(state)
        mutate(state)
        if _json_bytes(state) == before:
            return state
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


def read_status(job_id: str, environ: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Report history without writes, reconciliation or worker execution."""
    job_dir = _job_dir(job_id, environ)
    state = _load_json(job_dir / "job.json")
    answer = _public(state, job_dir)
    answer["read_only"] = True
    if state.get("status") not in FINAL_JOB_STATUS:
        answer["worker_lock"] = cli_store.worker_lock_observation(job_dir / "worker.lock")
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


def _request_bridge_cancel(run_dir: Path, external_cancel: Path, job_id: str) -> None:
    value = {"reason": "qualification job cancellation", "requested_at": time.time(), "requested_by": job_id}
    _atomic_json(external_cancel, value)
    if run_dir.is_dir():
        _atomic_json(run_dir / "cancel.json", value)
