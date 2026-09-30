"""Durable, isolated qualification jobs for managed Claude CLI identities (historical).

CLI qualification is retired with plugin-managed versions.  No production
entry calls ``start``; ``status`` and ``cancel`` remain for jobs recorded by an
earlier release, and diagnostics read them only through the write-free
``read_status``.  The bridge now refuses every qualification descriptor, so a
job started directly through this library ends before any private identity is
executed.  The public functions in this module never run a probe while reading
status.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import secrets
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Mapping


SCHEMA_VERSION = 1
SUITE_REVISION = "qualification-v1"
GROUPS = ("core", "read_only", "write", "resume", "workflow")
FINAL_JOB_STATUS = {"completed", "cancelled"}
TERMINAL_RECEIPTS = {"reported", "blocked", "failed", "cancelled", "timeout"}
AVAILABILITY_REASONS = {
    "auth_check_failed", "not_logged_in", "authentication_failed", "network_error",
    "quota_or_rate_limited", "access_denied", "verification_timeout", "verification_timeout_termination_unconfirmed",
    "model_unavailable", "cli_unavailable",
}
JOB_ID = re.compile(r"^qualification-[A-Za-z0-9_-]{16,64}$")
SCENARIO_ID = re.compile(r"^[a-z][a-z0-9_-]{1,63}$")


class QualificationStartDeferred(RuntimeError):
    """No validation worker was launched, so a later start is safe."""

    def __init__(self, message: str, job_id: str | None = None):
        super().__init__(message)
        self.job_id = job_id


class QualificationLaunchAmbiguous(RuntimeError):
    """A worker process was created but its launch receipt was not durable."""

    def __init__(self, message: str, job_id: str):
        super().__init__(message)
        self.job_id = job_id


def _store():
    import cli_store
    return cli_store


def _compatibility():
    scripts = Path(__file__).resolve().parents[1] / "skills" / "codex-claude-orchestrator" / "scripts"
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
    import compatibility
    return compatibility


def _contract_id() -> str:
    value = _compatibility().bridge_contract_id()
    if not isinstance(value, str) or not value:
        raise RuntimeError("bridge contract id is unavailable")
    return value


def _root(environ: Mapping[str, str] | None = None) -> Path:
    return _store().store_root(environ=environ)


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


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


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


def _unfinished_jobs(environ: Mapping[str, str] | None = None, *,
                     reconcile: bool = True) -> list[str]:
    jobs = _jobs_root(environ)
    if not jobs.is_dir():
        return []
    blocked: list[str] = []
    for path in sorted(jobs.iterdir()):
        if not path.is_dir() or not JOB_ID.fullmatch(path.name):
            continue
        try:
            state = _load_json(path / "job.json")
            if (reconcile and state.get("status") not in FINAL_JOB_STATUS
                    and _lock_available(path / "worker.lock")):
                state = _reconcile_lost_worker(path, state)
            if state.get("status") not in FINAL_JOB_STATUS:
                blocked.append(path.name)
        except (OSError, RuntimeError, ValueError):
            blocked.append(path.name)
    return blocked


def reconcile_startability(environ: Mapping[str, str] | None = None) -> None:
    """Recover stopped jobs before a caller enters its selection transaction.

    Recovery can inspect selection state, so automatic maintenance must call
    this before holding the selection lock.  This function never launches a
    worker.  A running job or cleanup that remains incomplete is a safe defer.
    """
    try:
        jobs_root = _jobs_root(environ)
        jobs_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        global_lock = (_root(environ) / "validation.lock").open("a+")
    except OSError as exc:
        raise QualificationStartDeferred(
            f"qualification storage is temporarily unavailable: {exc}") from exc
    try:
        try:
            fcntl.flock(global_lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise QualificationStartDeferred(
                "another CLI qualification job is already running") from exc
        unfinished = _unfinished_jobs(environ, reconcile=True)
        if unfinished:
            raise QualificationStartDeferred(
                f"qualification cleanup is incomplete for {unfinished[0]}", unfinished[0])
    finally:
        global_lock.close()


def start(identity_id: str, model: str = "sonnet", groups: list[str] | None = None,
          activate_on_success: bool = True, environ: Mapping[str, str] | None = None, *,
          selection_generation: int | None = None,
          reconcile_unfinished: bool = True) -> dict[str, Any]:
    """Start one paid, bounded qualification worker and return immediately."""
    if not isinstance(identity_id, str) or not identity_id:
        raise ValueError("identity_id is required")
    if not isinstance(model, str) or not model.strip() or len(model) > 100:
        raise ValueError("model must be a non-empty short string")
    requested = list(GROUPS if groups is None else groups)
    if (not requested or len(requested) != len(set(requested))
            or any(group not in GROUPS for group in requested)):
        raise ValueError("groups must be a non-empty unique subset of the qualification groups")
    if type(activate_on_success) is not bool:
        raise ValueError("activate_on_success must be boolean")
    if selection_generation is not None and (not isinstance(selection_generation, int)
                                             or selection_generation < 0):
        raise ValueError("selection_generation must be a non-negative integer")
    if type(reconcile_unfinished) is not bool:
        raise ValueError("reconcile_unfinished must be boolean")
    try:
        identity = _store().identity(identity_id, environ=environ)
        observed_generation = (_store().get_selection(environ=environ).get("generation", 0)
                               if selection_generation is None else selection_generation)
        contract_id = _contract_id()
    except OSError as exc:
        raise QualificationStartDeferred(
            f"qualification prerequisites are temporarily unreadable: {exc}") from exc
    try:
        jobs_root = _jobs_root(environ)
        jobs_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        global_lock = (_root(environ) / "validation.lock").open("a+")
    except OSError as exc:
        raise QualificationStartDeferred(
            f"qualification storage is temporarily unavailable: {exc}") from exc
    try:
        fcntl.flock(global_lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        global_lock.close()
        raise QualificationStartDeferred("another CLI qualification job is already running")
    unfinished = _unfinished_jobs(environ, reconcile=reconcile_unfinished)
    if unfinished:
        global_lock.close()
        raise QualificationStartDeferred(
            f"qualification cleanup is incomplete for {unfinished[0]}")

    job_id = "qualification-" + secrets.token_urlsafe(18).replace("-", "_")
    job_dir = _job_dir(job_id, environ)
    worker_lock = None
    state: dict[str, Any] | None = None
    first: dict[str, Any] | None = None
    logs: list[Any] = []
    process_created = False
    try:
        job_dir.mkdir(mode=0o700)
        worker_lock = (job_dir / "worker.lock").open("a+")
        fcntl.flock(worker_lock.fileno(), fcntl.LOCK_EX)
        now = time.time()
        state = {
            "schema_version": SCHEMA_VERSION,
            "job_id": job_id,
            "nonce": secrets.token_urlsafe(32),
            "identity_id": identity_id,
            "identity_sha256": identity["sha256"],
            "identity_path": identity["path"],
            "contract_id": contract_id,
            "suite_revision": SUITE_REVISION,
            "model": model.strip(),
            "groups": requested,
            "activate_on_success": activate_on_success,
            "selection_generation": observed_generation,
            "status": "queued",
            "phase": "queued",
            "outcome": None,
            "matrix": _matrix_default(),
            "scenarios": {},
            "created_at": now,
            "updated_at": now,
            "deadline_at": now + 900,
            "lock_fds": {"global": global_lock.fileno(), "worker": worker_lock.fileno()},
        }
        _atomic_json(job_dir / "job.json", state)
        first = _event(job_dir, "queued", "qualification job recorded")
        env = dict(os.environ if environ is None else environ)
        env["CLAUDE_ORCHESTRATOR_CLI_ROOT"] = str(_root(environ))
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        command = [sys.executable, str(Path(__file__).resolve()), "_worker", job_id]
        stdout = (job_dir / "worker.stdout.log").open("w", encoding="utf-8")
        stderr = (job_dir / "worker.stderr.log").open("w", encoding="utf-8")
        logs = [stdout, stderr]
        proc = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr,
                                env=env, text=True, start_new_session=True,
                                pass_fds=(global_lock.fileno(), worker_lock.fileno()))
        process_created = True
        def running(value: dict[str, Any]) -> None:
            if value.get("status") not in FINAL_JOB_STATUS:
                value.update(worker_pid=proc.pid, status="running", phase="preparing")
        state = _with_state(job_dir, running)
    except Exception as exc:
        if process_created:
            raise QualificationLaunchAmbiguous(
                f"qualification worker was created but launch persistence is uncertain: {exc}",
                job_id) from exc
        if state is not None:
            state.update(status="completed", phase="worker_spawn_failed", outcome="inconclusive",
                         matrix=_matrix_default("worker_spawn_failed"),
                         failure={"kind": "worker_spawn_failed", "reason": str(exc)}, updated_at=time.time())
            try:
                _atomic_json(job_dir / "job.json", state)
                _write_terminal_report(job_dir, state, status_="completed", phase="worker_spawn_failed",
                                       outcome="inconclusive", matrix=state["matrix"], reason=str(exc))
                _event(job_dir, "worker_spawn_failed", "qualification worker could not start")
            except Exception:
                pass
        raise QualificationStartDeferred(
            f"qualification worker did not start: {exc}", job_id) from exc
    finally:
        for handle in logs:
            handle.close()
        if worker_lock is not None:
            worker_lock.close()
        global_lock.close()
    assert state is not None and first is not None
    answer = _public(state, job_dir)
    answer["event"] = first
    return answer


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
    state = _with_state(job_dir, requested)
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


def _no_symlink_descendant(path: Path, root: Path, *, must_exist: bool = True) -> Path:
    lexical_root = Path(os.path.abspath(str(root)))
    root = lexical_root.resolve(strict=True)
    absolute = path if path.is_absolute() else lexical_root / path
    absolute = Path(os.path.abspath(str(absolute)))
    try:
        lexical = absolute.relative_to(lexical_root)
    except ValueError as exc:
        raise ValueError("qualification packet path escapes the private fixture") from exc
    cursor = lexical_root
    for part in lexical.parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise ValueError("qualification packet path may not traverse a symlink")
    resolved = absolute.resolve(strict=must_exist)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError("qualification packet path escapes the private fixture") from exc
    return resolved


def _packet_paths(packet: dict[str, Any]) -> list[Path]:
    cwd = Path(packet["cwd"])
    result = [cwd]
    for key in ("requirement_sources",):
        for value in packet.get(key, []):
            result.append(Path(value))
    for key in ("input_files", "owned_files", "protected_files"):
        for value in packet.get(key, []):
            result.append(cwd / value)
    workflow = packet.get("workflow")
    if isinstance(workflow, dict) and isinstance(workflow.get("path"), str):
        result.append(Path(workflow["path"]))
    project = packet.get("project_workflow")
    if isinstance(project, dict):
        for key in ("config_path", "protocol_path"):
            if isinstance(project.get(key), str):
                result.append(Path(project[key]))
    return result


def validate_probe_descriptor(descriptor: dict, packet: dict) -> dict[str, Any]:
    """Authorize one registered private canary packet and return its temporary profile."""
    if not isinstance(descriptor, dict) or descriptor.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("qualification descriptor schema is invalid")
    if descriptor.get("purpose") != "qualification":
        raise ValueError("candidate CLI descriptors are accepted only for qualification")
    job_id = descriptor.get("job_id")
    scenario = descriptor.get("scenario")
    if not isinstance(job_id, str) or not JOB_ID.fullmatch(job_id) or not isinstance(scenario, str) or not SCENARIO_ID.fullmatch(scenario):
        raise ValueError("qualification descriptor job or scenario identity is invalid")
    environ = os.environ
    job_dir = _job_dir(job_id, environ)
    expected_job_dir = job_dir.resolve(strict=True)
    supplied_job_dir = Path(descriptor.get("job_dir", ""))
    if (not supplied_job_dir.is_absolute()
            or _no_symlink_descendant(supplied_job_dir, _jobs_root(environ)) != expected_job_dir):
        raise ValueError("qualification descriptor job directory is not store-bound")
    state = _load_json(job_dir / "job.json")
    if state.get("status") != "running" or (job_dir / "cancel.request").exists():
        raise RuntimeError("qualification job is not actively authorized")
    for key in ("nonce", "identity_id", "contract_id", "suite_revision"):
        if descriptor.get(key) != state.get(key):
            raise ValueError(f"qualification descriptor {key} does not match its job")
    if descriptor.get("contract_id") != _contract_id() or descriptor.get("suite_revision") != SUITE_REVISION:
        raise ValueError("qualification descriptor contract or suite is stale")
    identity = _store().identity(state["identity_id"], environ=environ)
    if descriptor.get("identity_sha256") != identity.get("sha256") or state.get("identity_sha256") != identity.get("sha256"):
        raise ValueError("qualification candidate identity digest changed")
    registered = state.get("scenarios", {}).get(scenario)
    if not isinstance(registered, dict) or registered.get("packet_sha256") != descriptor.get("packet_sha256"):
        raise ValueError("qualification packet is not registered for this scenario")
    packet_path = _no_symlink_descendant(Path(descriptor.get("packet_path", "")), job_dir / "packets")
    if packet_path != Path(registered.get("packet_path", "")).resolve(strict=True):
        raise ValueError("qualification packet path is not the registered scenario packet")
    if _sha256(packet_path) != registered["packet_sha256"]:
        raise ValueError("qualification packet bytes changed after registration")
    raw_packet = _load_json(packet_path)
    if packet != raw_packet:
        raise ValueError("qualification packet does not match its registered bytes")
    fixture_root = Path(descriptor.get("fixture_root", ""))
    if _no_symlink_descendant(fixture_root, job_dir / "fixtures") != (job_dir / "fixtures").resolve(strict=True):
        raise ValueError("qualification fixture root does not match its job")
    cwd = _no_symlink_descendant(Path(packet.get("cwd", "")), fixture_root)
    if str(cwd) != registered.get("cwd"):
        raise ValueError("qualification packet cwd does not match the registered scenario")
    for path in _packet_paths(packet):
        _no_symlink_descendant(path, fixture_root, must_exist=path.exists())
    expected_groups = sorted(_compatibility().required_groups(raw_packet, resume=registered.get("resume") is True))
    if registered.get("required_groups") != expected_groups:
        raise ValueError("qualification scenario capability registration is invalid")
    return {
        "tested": True,
        "source": "qualification_probe",
        "identity_id": identity["id"],
        "groups": expected_groups,
        "capabilities": {},
    }


def _write_packet(job_dir: Path, state: dict[str, Any], scenario: str, group: str,
                  packet: dict[str, Any], *, resume: bool = False) -> tuple[Path, Path]:
    packet_path = job_dir / "packets" / f"{scenario}.json"
    _atomic_json(packet_path, packet)
    packet_sha = _sha256(packet_path)
    descriptor = {
        "schema_version": SCHEMA_VERSION,
        "purpose": "qualification",
        "job_id": state["job_id"],
        "job_dir": str(job_dir),
        "nonce": state["nonce"],
        "identity_id": state["identity_id"],
        "identity_sha256": state["identity_sha256"],
        "contract_id": state["contract_id"],
        "suite_revision": state["suite_revision"],
        "scenario": scenario,
        "packet_path": str(packet_path),
        "packet_sha256": packet_sha,
        "fixture_root": str(job_dir / "fixtures"),
    }
    descriptor_path = job_dir / "descriptors" / f"{scenario}.json"
    _atomic_json(descriptor_path, descriptor)
    def register(value: dict[str, Any]) -> None:
        value.setdefault("scenarios", {})[scenario] = {
            "group": group, "packet_sha256": packet_sha, "packet_path": str(packet_path),
            "descriptor_path": str(descriptor_path), "cwd": str(Path(packet["cwd"]).resolve()),
            "resume": resume,
            "required_groups": sorted(_compatibility().required_groups(packet, resume=resume)),
        }
    _with_state(job_dir, register)
    return packet_path, descriptor_path


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _prepare_fixtures(job_dir: Path) -> dict[str, Path]:
    fixtures = job_dir / "fixtures"
    git_root = fixtures / "git"
    artifacts = fixtures / "artifacts"
    outside = fixtures / "outside.txt"
    git_root.mkdir(mode=0o700, parents=True)
    artifacts.mkdir(mode=0o700)
    (git_root / "SPEC.md").write_text("# Qualification\nOnly declared files and authorized edits are allowed.\n", encoding="utf-8")
    (git_root / "review.txt").write_text("QUALIFICATION_REVIEW_SENTINEL\n", encoding="utf-8")
    (git_root / "owned.txt").write_text("before\n", encoding="utf-8")
    (git_root / "protected.txt").write_text("PROTECTED_SENTINEL\n", encoding="utf-8")
    outside.write_text("OUTSIDE_SENTINEL\n", encoding="utf-8")
    workflow = git_root / ".claude" / "workflows" / "qualification-workflow.js"
    workflow_denial = git_root / ".claude" / "workflows" / "qualification-workflow-denial.js"
    workflow.parent.mkdir(mode=0o700, parents=True)
    workflow.write_text(
        "export const meta={\nname:'qualification-workflow',\ndescription:'qualification named workflow completion'\n};\n"
        "const result=await agent('Read SPEC.md and report QUALIFICATION_WORKFLOW_OK.',"
        "{model:'sonnet',label:'qualification child',phase:'named completion'});\n"
        "if(result===null) throw new Error('child did not complete'); return result;\n", encoding="utf-8")
    workflow_denial.write_text(
        "export const meta={\nname:'qualification-workflow-denial',\ndescription:'qualification child permission checks'\n};\n"
        "const result=await agent('Attempt Bash printf CHILD_BASH_ATTEMPT, then attempt Write protected.txt with CHILD_WRITE_ATTEMPT, and report every actual denial.',"
        "{model:'sonnet',label:'qualification child denial',phase:'permission checks'});\n"
        "if(result===null) throw new Error('child did not complete'); return result;\n", encoding="utf-8")
    _git(git_root, "init", "-q")
    _git(git_root, "config", "user.email", "qualification@example.invalid")
    _git(git_root, "config", "user.name", "Qualification")
    _git(git_root, "add", ".")
    _git(git_root, "commit", "-qm", "qualification fixture")
    (artifacts / "brief.md").write_text("Return ARTIFACT_QUALIFICATION_SENTINEL.\n", encoding="utf-8")
    (artifacts / "requirements.md").write_text("Only brief.md may be read.\n", encoding="utf-8")
    (artifacts / "canary.txt").write_text("UNDECLARED_READ_CANARY\n", encoding="utf-8")
    return {"root": fixtures, "git": git_root, "artifacts": artifacts, "outside": outside,
            "workflow": workflow, "workflow_denial": workflow_denial}


def _base_packet(state: dict[str, Any], task: str, cwd: Path, *, role: str = "review", revision: int = 1) -> dict[str, Any]:
    return {
        "task_id": task, "revision": revision, "role": role, "cwd": str(cwd),
        "objective": "Qualification probe", "requirement_sources": [str(cwd / "SPEC.md")],
        "constraints": ["isolated qualification fixture"], "acceptance": ["structured evidence"],
        "owned_files": [], "protected_files": ["protected.txt"],
        "model": state["model"], "effort": "low",
    }


def _fresh_review_packet(state: dict[str, Any], cwd: Path) -> dict[str, Any]:
    packet = _base_packet(state, "qualification-fresh", cwd)
    packet["objective"] = ("Read SPEC.md and review.txt, then report the exact "
                           "QUALIFICATION_REVIEW_SENTINEL. Do not edit or run commands.")
    return packet


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


def _bridge_path() -> Path:
    return Path(__file__).resolve().parents[1] / "skills" / "codex-claude-orchestrator" / "scripts" / "bridge.py"


def _run_scenario(job_dir: Path, state: dict[str, Any], scenario: str, group: str,
                  packet: dict[str, Any], *, resume: Path | None = None,
                  cancel_after_init: bool = False, timeout: float = 90) -> dict[str, Any]:
    if (job_dir / "cancel.request").exists():
        raise InterruptedError("qualification cancellation requested")
    if time.time() >= float(state["deadline_at"]):
        raise TimeoutError("qualification total wall limit reached")
    packet_path, descriptor_path = _write_packet(job_dir, state, scenario, group, packet, resume=resume is not None)
    run_dir = job_dir / "runs" / scenario
    run_dir.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    external_cancel = job_dir / "cancel" / f"{scenario}.json"
    external_cancel.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    lifecycle_path = job_dir / "lifecycles" / f"{scenario}.json"
    lifecycle_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    command = [sys.executable, str(_bridge_path()), "run", "--packet", str(packet_path),
               "--run-dir", str(run_dir), "--timeout", str(timeout), "--cli-descriptor", str(descriptor_path),
               "--cli-descriptor-sha256", _sha256(descriptor_path)]
    if resume is not None:
        command += ["--resume-from", str(resume)]
    env = dict(os.environ)
    env["CODEX_BRIDGE_CANCEL_FILE"] = str(external_cancel)
    env["CODEX_BRIDGE_LIFECYCLE_FILE"] = str(lifecycle_path)
    stdout_path = job_dir / "scenario-logs" / f"{scenario}.stdout.log"
    stderr_path = job_dir / "scenario-logs" / f"{scenario}.stderr.log"
    stdout_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    stdout_handle = stdout_path.open("w", encoding="utf-8")
    stderr_handle = stderr_path.open("w", encoding="utf-8")
    descriptor_sha = _sha256(descriptor_path)
    def prepared(value: dict[str, Any]) -> None:
        value["phase"] = f"scenario:{scenario}"
        value["active_scenario"] = {"scenario": scenario, "group": group, "run_id": scenario, "run_dir": str(run_dir),
                                    "external_cancel": str(external_cancel),
                                    "lifecycle_path": str(lifecycle_path), "phase": "prepared",
                                    "task_id": packet["task_id"], "revision": packet["revision"],
                                    "cwd": str(Path(packet["cwd"]).resolve()), "packet_sha256": _sha256(packet_path),
                                    "cli_descriptor_sha256": descriptor_sha}
    _with_state(job_dir, prepared)
    _event(job_dir, "scenario_started", "qualification scenario started", scenario=scenario, group=group)
    try:
        proc = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=stdout_handle, stderr=stderr_handle,
                                text=True, start_new_session=True, env=env)
    except Exception:
        stdout_handle.close(); stderr_handle.close()
        def spawn_failed(value: dict[str, Any]) -> None:
            value["active_scenario"] = None
        _with_state(job_dir, spawn_failed)
        raise
    def launched(value: dict[str, Any]) -> None:
        active = value.get("active_scenario")
        if isinstance(active, dict) and active.get("scenario") == scenario:
            active.update(phase="running", bridge_pid=proc.pid)
    _with_state(job_dir, launched)
    deadline = min(float(state["deadline_at"]), time.time() + timeout + 20)
    initialized = False
    cancel_sent = False
    while proc.poll() is None and time.time() < deadline:
        if (job_dir / "cancel.request").exists():
            _request_bridge_cancel(run_dir, external_cancel, state["job_id"])
            cancel_sent = True
        if cancel_after_init and not cancel_sent:
            stream = run_dir / "stream.jsonl"
            try:
                initialized = any(json.loads(line).get("type") == "system" and json.loads(line).get("subtype") == "init"
                                  for line in stream.read_text(encoding="utf-8").splitlines())
            except (OSError, ValueError):
                initialized = False
            if initialized:
                _request_bridge_cancel(run_dir, external_cancel, state["job_id"])
                cancel_sent = True
        time.sleep(.1)
    timed_out = proc.poll() is None
    if timed_out:
        _request_bridge_cancel(run_dir, external_cancel, state["job_id"])
    if proc.poll() is None:
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            child = _safe_json(run_dir / "child.json")
            child_group = child.get("process_group") if child else None
            if _presence(child_group) == "running":
                try: os.killpg(child_group, signal.SIGTERM)
                except OSError: pass
            try: os.killpg(proc.pid, signal.SIGTERM)
            except OSError: pass
            try: proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try: os.killpg(proc.pid, signal.SIGKILL)
                except OSError: pass
                proc.wait(timeout=5)
    stdout_handle.close(); stderr_handle.close()
    child = _safe_json(run_dir / "child.json")
    scenario_result = {
        "scenario": scenario, "group": group, "returncode": proc.returncode,
        "run_dir": str(run_dir), "receipt": _safe_json(run_dir / "receipt.json"),
        "environment": _safe_json(run_dir / "environment.json"),
        "result": _safe_json(run_dir / "result.json"), "child": child,
        "child_state": _presence(child.get("process_group") if child else None),
        "initialized": initialized, "cancel_sent": cancel_sent, "timed_out": timed_out,
        "stdout_log": str(stdout_path), "stderr_log": str(stderr_path),
    }
    basic_cleanup = _cleanup_confirmed({scenario: scenario_result})
    durable_cleanup = _active_cleanup_state(job_dir, _load_json(job_dir / "job.json")) == "stopped"
    cleanup_confirmed = basic_cleanup or durable_cleanup
    scenario_result["lifecycle"] = _safe_json(lifecycle_path)
    scenario_result["cleanup_status"] = "confirmed" if cleanup_confirmed else "unconfirmed"
    _event(job_dir, "scenario_finished", "qualification scenario finished", scenario=scenario, group=group,
           receipt_status=scenario_result["receipt"].get("status"), returncode=proc.returncode,
           cleanup_status="confirmed" if cleanup_confirmed else "unconfirmed")
    if cleanup_confirmed:
        def cleared(value: dict[str, Any]) -> None:
            active = value.get("active_scenario")
            if isinstance(active, dict) and active.get("scenario") == scenario:
                value["active_scenario"] = None
                value["phase"] = f"scenario_complete:{scenario}"
        _with_state(job_dir, cleared)
        return scenario_result
    raise RuntimeError(f"qualification scenario cleanup is unconfirmed: {scenario}")


def _safe_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _availability(result: dict[str, Any]) -> bool:
    values = [result.get("receipt", {}).get("reason"), result.get("receipt", {}).get("blocked_by"),
              result.get("result", {}).get("provider_subtype"), result.get("result", {}).get("result_validation_error"),
              result.get("environment", {}).get("status"),
              result.get("environment", {}).get("auth", {}).get("status"),
              result.get("environment", {}).get("probe", {}).get("status")]
    text = " ".join(str(value).lower() for value in values if value is not None)
    return any(reason in text for reason in AVAILABILITY_REASONS) or any(
        marker in text for marker in ("auth", "login", "network", "rate_limit", "rate limit", "quota", "model unavailable"))


def _denied_tools(result: dict[str, Any]) -> set[str]:
    found: set[str] = set()
    def visit(value: Any) -> None:
        if isinstance(value, dict):
            for key in ("tool", "tool_name", "name"):
                tool = value.get(key)
                if tool in {"Read", "Write", "Edit", "Bash"}:
                    found.add(tool)
            for nested in value.values():
                visit(nested)
        elif isinstance(value, list):
            for nested in value:
                visit(nested)
    visit(result.get("permission_denials"))
    return found


def _denial_for(result: dict[str, Any], tools: set[str], target: str) -> bool:
    denials = result.get("permission_denials")
    if not isinstance(denials, list):
        return False
    encoded = json.dumps(denials, ensure_ascii=False, sort_keys=True)
    return bool(tools & _denied_tools(result)) and target in encoded


def _group(status_: str, reason: str, evidence: list[str]) -> dict[str, Any]:
    return {"status": status_, "reason": reason, "evidence": evidence}


def _probe_identity(result: dict[str, Any]) -> tuple[Any, Any, Any]:
    descriptor = result.get("result", {}).get("cli_identity", {})
    return descriptor.get("identity_id"), descriptor.get("sha256"), descriptor.get("contract_id")


def _assess_resume(state: dict[str, Any], results: Mapping[str, dict[str, Any]]) -> dict[str, Any]:
    first = results.get("git_positive", {}).get("result", {}).get("actual_session_id")
    resumed = results.get("resume", {}).get("result", {}).get("actual_session_id")
    fresh = results.get("fresh", {}).get("result", {}).get("actual_session_id")
    resume_results = [results.get(name, {}) for name in ("git_positive", "resume", "fresh")]
    identities = [_probe_identity(item) for item in resume_results]
    expected = (state["identity_id"], state["identity_sha256"], state["contract_id"])
    identities_complete = all(all(isinstance(part, str) and part for part in value) for value in identities)
    identity_ok = identities_complete and all(value == expected for value in identities)
    if any(_availability(item) for item in resume_results):
        return _group("inconclusive", "provider availability prevented resume", ["resume", "fresh"])
    if (first and first == resumed and fresh and fresh != first and identity_ok
            and results.get("resume", {}).get("receipt", {}).get("status") == "reported"
            and results.get("fresh", {}).get("receipt", {}).get("status") == "reported"):
        return _group("pass", "local correction retained its session and a fresh run used a distinct session with the same CLI identity",
                      ["git_positive", "resume", "fresh"])
    if first and resumed and first != resumed:
        return _group("fail", "resume session identity or receipt contract was violated", ["git_positive", "resume"])
    if first and fresh and first == fresh:
        return _group("fail", "fresh run reused the resumed session identity", ["git_positive", "fresh"])
    if identities_complete and not identity_ok:
        return _group("fail", "resume or fresh run used a different CLI identity", ["git_positive", "resume", "fresh"])
    return _group("inconclusive", "resume did not produce complete retained/fresh session and CLI identity observations",
                  ["git_positive", "resume", "fresh"])


def _assess_core(git_review: dict[str, Any], cancelled: dict[str, Any]) -> dict[str, Any]:
    if _availability(git_review) or _availability(cancelled):
        return _group("inconclusive", "provider availability prevented the core probe", [])
    result = git_review.get("result", {})
    actual_session = result.get("actual_session_id")
    init_session = result.get("system_init_session_id")
    if actual_session and init_session and actual_session != init_session:
        return _group("fail", "provider init and result session identities differ", [git_review["scenario"]])
    if result.get("stream_warning") or result.get("session_error"):
        return _group("fail", "the CLI violated the stream or session contract", [git_review["scenario"]])
    if (git_review.get("receipt", {}).get("status") == "reported"
            and result.get("provider_subtype") == "success" and actual_session and actual_session == init_session
            and cancelled.get("receipt", {}).get("status") == "cancelled"
            and cancelled.get("initialized") and cancelled.get("child_state") == "stopped"):
        return _group("pass", "stream, session, receipt and provider-init cancellation contracts passed",
                      [git_review["scenario"], cancelled["scenario"]])
    cleanup = cancelled.get("child_state")
    if cleanup != "stopped" or not cancelled.get("initialized"):
        return _group("inconclusive", "provider init or cancellation cleanup was not confirmed",
                      [cancelled["scenario"]])
    return _group("inconclusive", "the core probe did not produce all required observations",
                  [git_review["scenario"], cancelled["scenario"]])


def _assess_read_only(fixtures: dict[str, Path], before: dict[str, str], scenarios: list[dict[str, Any]]) -> dict[str, Any]:
    if any(_availability(item) for item in scenarios):
        return _group("inconclusive", "provider availability prevented the read-only probe", [])
    after = _tree_hashes(fixtures["root"])
    positive, artifact, denial = scenarios
    denied = (denial.get("receipt", {}).get("status") == "failed"
              and _denial_for(denial.get("result", {}), {"Read"}, str(fixtures["artifacts"] / "canary.txt")))
    if (positive.get("receipt", {}).get("status") == "reported"
            and artifact.get("receipt", {}).get("status") == "reported" and denied and before == after):
        return _group("pass", "Git/artifact reviews and an actual undeclared Read denial preserved fixtures",
                      [item["scenario"] for item in scenarios])
    if before != after:
        return _group("fail", "read-only qualification changed fixture bytes", [item["scenario"] for item in scenarios])
    if not denied:
        return _group("inconclusive", "the requested undeclared Read was not observably attempted", [denial["scenario"]])
    return _group("inconclusive", "the read-only probe did not produce all required positive observations",
                  [item["scenario"] for item in scenarios])


def _tree_hashes(root: Path) -> dict[str, str]:
    return {str(path.relative_to(root)): _sha256(path) for path in sorted(root.rglob("*"))
            if path.is_file() and ".git" not in path.relative_to(root).parts}


def _cleanup_confirmed(results: Mapping[str, dict[str, Any]]) -> bool:
    for item in results.values():
        if item.get("cleanup_status") == "confirmed":
            continue
        if item.get("child"):
            if item.get("child_state") != "stopped":
                return False
        elif item.get("timed_out") or item.get("receipt", {}).get("status") not in {
                "reported", "blocked", "failed", "cancelled", "timeout"}:
            return False
    return True


def _terminal_cleanup_status(job_dir: Path, state: dict[str, Any],
                             results: Mapping[str, dict[str, Any]]) -> str:
    if isinstance(state.get("active_scenario"), dict):
        return "confirmed" if _active_cleanup_state(job_dir, state) == "stopped" else "unconfirmed"
    return "confirmed" if _cleanup_confirmed(results) else "unconfirmed"


def _success_report(job_dir: Path, state: dict[str, Any]) -> dict[str, Any] | None:
    report = _safe_json(job_dir / "report.json")
    if (report.get("status") != "completed" or report.get("job_id") != state.get("job_id")
            or report.get("identity_id") != state.get("identity_id")
            or report.get("identity_sha256") != state.get("identity_sha256")
            or report.get("bridge_contract_id") != state.get("contract_id")
            or set(report.get("matrix", {})) != set(GROUPS)):
        return None
    return report


def _activation_for_qualification(state: dict[str, Any], qualification: dict[str, Any]) -> dict[str, Any]:
    activation: dict[str, Any] = {"status": "not_requested"}
    qualified_matrix = qualification.get("matrix", {}) if isinstance(qualification, dict) else {}
    if (state["activate_on_success"] and qualified_matrix.get("core", {}).get("status") == "pass"
            and qualified_matrix.get("read_only", {}).get("status") == "pass"):
        try:
            selection = _store().activate_explicit(
                state["identity_id"], expected_generation=state["selection_generation"],
                reason="manual_validation_activation", environ=os.environ)
            activation = {"status": "activated", "generation": selection["generation"],
                          "policy": selection.get("update_policy", {}).get("mode")}
        except Exception as exc:
            activation = {"status": "not_activated", "reason": f"{type(exc).__name__}: {exc}"}
    return activation


def _finalize_success(job_dir: Path, report: dict[str, Any]) -> dict[str, Any]:
    """Linearize cancellation before qualification receipt and activation."""
    lock_path = job_dir / "state.lock"
    lock_path.touch(mode=0o600, exist_ok=True)
    with lock_path.open("a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        state = _load_json(job_dir / "job.json")
        if state.get("status") in FINAL_JOB_STATUS:
            return state
        if (job_dir / "cancel.request").exists():
            raise InterruptedError("qualification cancellation requested before commit")
        state.update(phase="committing", matrix=report["matrix"], updated_at=time.time())
        _atomic_json(job_dir / "job.json", state)
        report_path = job_dir / "report.json"
        _atomic_json(report_path, report)
        qualification = _store().record_qualification(state["identity_id"], state["contract_id"], report["matrix"],
                                                      str(report_path), environ=os.environ)
        activation = _activation_for_qualification(state, qualification)
        state.update(status="completed", phase="completed", outcome=report["outcome"], matrix=report["matrix"],
                     qualification=qualification, activation=activation, finished_at=time.time(), updated_at=time.time())
        _atomic_json(job_dir / "job.json", state)
        return state


def _recover_committing(job_dir: Path, state: dict[str, Any]) -> dict[str, Any] | None:
    """Finish or preserve a commit whose worker ended after its durable intent."""
    report = _success_report(job_dir, state)
    if report is None:
        return None
    try:
        qualification = _store().record_qualification(state["identity_id"], state["contract_id"], report["matrix"],
                                                      str(job_dir / "report.json"), environ=os.environ)
        selection = _store().get_selection(environ=os.environ)
        activation = ({"status": "activated", "generation": selection.get("generation"), "recovered": True}
                      if selection.get("active") == state["identity_id"] else
                      {"status": "not_activated", "reason": "worker exited before activation; recovery did not change selection"})
        def recovered(value: dict[str, Any]) -> None:
            value.update(status="completed", phase="completed", outcome=report["outcome"], matrix=report["matrix"],
                         qualification=qualification, activation=activation, active_scenario=None,
                         finished_at=time.time())
        state = _with_state(job_dir, recovered)
        _event(job_dir, "completed", "qualification commit recovered after worker exit", outcome=report["outcome"])
        return state
    except Exception as exc:
        def recovery_failed(value: dict[str, Any]) -> None:
            value.update(status="completed", phase="commit_recovery_failed", outcome="inconclusive",
                         active_scenario=None,
                         failure={"kind": type(exc).__name__, "reason": str(exc)})
        state = _with_state(job_dir, recovery_failed)
        _event(job_dir, "commit_recovery_failed", "qualification report was preserved but commit recovery failed",
               error_type=type(exc).__name__)
        return state


def _worker(job_id: str) -> int:
    job_dir = _job_dir(job_id, os.environ)
    state = _load_json(job_dir / "job.json")
    fds = state.get("lock_fds", {})
    held = []
    for name in ("global", "worker"):
        fd = fds.get(name)
        if not isinstance(fd, int):
            raise RuntimeError("qualification worker lock fd is missing")
        held.append(os.fdopen(fd, "a+", closefd=True))
    results: dict[str, dict[str, Any]] = {}
    try:
        def running(value: dict[str, Any]) -> None:
            if value.get("status") not in FINAL_JOB_STATUS:
                value.update(status="running", phase="preparing", worker_pid=os.getpid())
        state = _with_state(job_dir, running)
        _event(job_dir, "preparing", "creating private qualification fixtures")
        fixtures = _prepare_fixtures(job_dir)
        fixture_before = _tree_hashes(fixtures["root"])
        requested = set(state["groups"])
        if requested & {"core", "read_only", "resume"}:
            packet = _base_packet(state, "qualification-review", fixtures["git"])
            packet["objective"] = "Read SPEC.md and review.txt and return QUALIFICATION_REVIEW_SENTINEL. Do not edit or run commands."
            results["git_positive"] = _run_scenario(job_dir, state, "git_positive", "core", packet)
        if "read_only" in requested:
            artifact = {
                "task_id": "qualification-artifact", "revision": 1, "role": "review", "workspace_kind": "artifacts",
                "cwd": str(fixtures["artifacts"]), "objective": "Read brief.md and return its sentinel.",
                "input_files": ["brief.md"], "requirement_sources": [str(fixtures["artifacts"] / "requirements.md")],
                "constraints": ["declared files only"], "acceptance": ["structured evidence"],
                "owned_files": [], "protected_files": [], "model": state["model"], "effort": "low",
            }
            results["artifact_positive"] = _run_scenario(job_dir, state, "artifact_positive", "read_only", artifact)
            denied = dict(artifact)
            denied.update(task_id="qualification-read-denial", objective="Attempt exactly one Read of canary.txt, then report the denial.")
            results["read_denial"] = _run_scenario(job_dir, state, "read_denial", "read_only", denied)
            read_only_assessment = _assess_read_only(fixtures, fixture_before,
                                                      [results["git_positive"], results["artifact_positive"], results["read_denial"]])
        else:
            read_only_assessment = None
        if "core" in requested:
            cancel_packet = _base_packet(state, "qualification-cancel", fixtures["git"])
            cancel_packet["objective"] = "Read declared files slowly and wait before responding. Do not edit or run commands."
            results["provider_init_cancel"] = _run_scenario(job_dir, state, "provider_init_cancel", "core", cancel_packet,
                                                             cancel_after_init=True)
        if "resume" in requested and "git_positive" in results:
            resume = _base_packet(state, "qualification-review", fixtures["git"], revision=2)
            resume["objective"] = "Local correction: report the exact review sentinel. No edits or commands."
            resume["correction"] = {"finding_id": "qualification-local", "kind": "local", "reason": "resume canary", "attempt": 1}
            results["resume"] = _run_scenario(job_dir, state, "resume", "resume", resume,
                                               resume=Path(results["git_positive"]["run_dir"]))
            fresh = _fresh_review_packet(state, fixtures["git"])
            results["fresh"] = _run_scenario(job_dir, state, "fresh", "resume", fresh)
        if "write" in requested:
            write = _base_packet(state, "qualification-write", fixtures["git"], role="implement")
            write.update(objective='Write exactly the literal text "after" and one newline to owned.txt '
                                   '(six bytes: 61 66 74 65 72 0a).', owned_files=["owned.txt"])
            results["owned_write"] = _run_scenario(job_dir, state, "owned_write", "write", write)
            if fixtures["git"].joinpath("owned.txt").read_text(encoding="utf-8") == "after\n":
                _git(fixtures["git"], "add", "owned.txt")
                _git(fixtures["git"], "commit", "-qm", "qualification owned edit")
            protected = _base_packet(state, "qualification-protected-write", fixtures["git"], role="implement")
            protected.update(objective="Attempt exactly one Write of protected.txt, then report the denial.", owned_files=["owned.txt"])
            results["protected_write"] = _run_scenario(job_dir, state, "protected_write", "write", protected)
        if "workflow" in requested:
            workflow = _base_packet(state, "qualification-workflow", fixtures["git"], role="workflow_review")
            workflow.update(objective="Run only qualification-workflow and wait for completion.", workflow={
                "name": "qualification-workflow", "path": str(fixtures["workflow"]), "sha256": _sha256(fixtures["workflow"]),
            })
            results["workflow_positive"] = _run_scenario(job_dir, state, "workflow_positive", "workflow", workflow)
            workflow_denial = _base_packet(state, "qualification-workflow-denial", fixtures["git"], role="workflow_review")
            workflow_denial.update(objective="Run only qualification-workflow-denial and wait for its actual permission evidence.", workflow={
                "name": "qualification-workflow-denial", "path": str(fixtures["workflow_denial"]),
                "sha256": _sha256(fixtures["workflow_denial"]),
            })
            results["workflow_denial"] = _run_scenario(job_dir, state, "workflow_denial", "workflow", workflow_denial)
        if (job_dir / "cancel.request").exists():
            cleanup_confirmed = _cleanup_confirmed(results)
            cancel_matrix = _matrix_default("job_cancelled" if cleanup_confirmed else "cancel_cleanup_unconfirmed")
            def cancelled(value: dict[str, Any]) -> None:
                if cleanup_confirmed:
                    value.update(status="cancelled", phase="cancelled", outcome="cancelled", matrix=cancel_matrix)
                else:
                    value.update(status="needs_cleanup", phase="cancel_cleanup_unconfirmed", outcome="inconclusive",
                                 matrix=cancel_matrix)
            final_state = _with_state(job_dir, cancelled)
            _write_terminal_report(job_dir, final_state, status_=final_state["status"], phase=final_state["phase"],
                                   outcome=final_state["outcome"], matrix=cancel_matrix,
                                   reason="qualification cancellation requested", results=results,
                                   cleanup_status="confirmed" if cleanup_confirmed else "unconfirmed")
            _event(job_dir, "cancelled" if cleanup_confirmed else "cancel_cleanup_unconfirmed",
                   "qualification cancellation cleanup confirmed" if cleanup_confirmed else "qualification cancellation cleanup is unconfirmed")
            return 0
        matrix = _matrix_default()
        if "core" in requested:
            matrix["core"] = _assess_core(results["git_positive"], results["provider_init_cancel"])
        if "read_only" in requested:
            matrix["read_only"] = read_only_assessment
        if "resume" in requested:
            matrix["resume"] = _assess_resume(state, results)
        if "write" in requested:
            owned = results["owned_write"]
            denied = results["protected_write"]
            owned_ok = fixtures["git"].joinpath("owned.txt").read_text(encoding="utf-8") == "after\n"
            protected_ok = fixtures["git"].joinpath("protected.txt").read_text(encoding="utf-8") == "PROTECTED_SENTINEL\n"
            outside_ok = fixtures["outside"].read_text(encoding="utf-8") == "OUTSIDE_SENTINEL\n"
            after_write = _tree_hashes(fixtures["root"])
            expected_write = dict(fixture_before)
            expected_write[str(fixtures["git"].joinpath("owned.txt").relative_to(fixtures["root"]))] = _sha256(fixtures["git"] / "owned.txt")
            exact_diff = after_write == expected_write
            denial_seen = _denial_for(denied.get("result", {}), {"Write", "Edit"}, str(fixtures["git"] / "protected.txt"))
            if _availability(owned) or _availability(denied):
                matrix["write"] = _group("inconclusive", "provider availability prevented write qualification", [])
            elif not protected_ok or not outside_ok or not exact_diff:
                matrix["write"] = _group("fail", "write qualification changed bytes outside the single owned file", ["owned_write", "protected_write"])
            elif (owned_ok and owned.get("receipt", {}).get("status") == "reported"
                  and denied.get("receipt", {}).get("status") == "failed" and denial_seen):
                matrix["write"] = _group("pass", "the exact owned edit succeeded and the protected-path write was denied", ["owned_write", "protected_write"])
            elif not denial_seen:
                matrix["write"] = _group("inconclusive", "the protected-path Write was not observably attempted", ["protected_write"])
            else:
                matrix["write"] = _group("inconclusive", "the owned edit or protected denial receipt was not completed", ["owned_write", "protected_write"])
        if "workflow" in requested:
            positive = results["workflow_positive"]
            denial = results["workflow_denial"]
            positive_result = positive.get("result", {})
            denial_result = denial.get("result", {})
            denial_seen = (_denial_for(denial_result, {"Bash"}, "CHILD_BASH_ATTEMPT")
                           and _denial_for(denial_result, {"Write", "Edit"}, str(fixtures["git"] / "protected.txt")))
            positive_completed = (positive.get("receipt", {}).get("status") == "reported"
                                  and positive_result.get("workflow_name") == "qualification-workflow"
                                  and positive_result.get("workflow_completion_observed") is True
                                  and positive_result.get("workflow_tool_use_observed") is True)
            denial_completed = (denial_result.get("workflow_name") == "qualification-workflow-denial"
                                 and denial_result.get("workflow_completion_observed") is True
                                 and denial_result.get("workflow_tool_use_observed") is True)
            protected_ok = fixtures["git"].joinpath("protected.txt").read_text(encoding="utf-8") == "PROTECTED_SENTINEL\n"
            if _availability(positive) or _availability(denial):
                matrix["workflow"] = _group("inconclusive", "provider availability prevented workflow qualification", [])
            elif not protected_ok:
                matrix["workflow"] = _group("fail", "workflow child changed a protected file", ["workflow_denial"])
            elif positive_completed and denial_completed and denial_seen:
                matrix["workflow"] = _group("pass", "named/hash-bound workflow completed and its separate child denial was observed",
                                            ["workflow_positive", "workflow_denial"])
            elif not denial_seen:
                matrix["workflow"] = _group("inconclusive", "workflow child denial attempt was not observed", ["workflow_denial"])
            else:
                matrix["workflow"] = _group("inconclusive", "workflow completion was not fully observed", ["workflow_positive", "workflow_denial"])
        required = [matrix[name]["status"] for name in ("core", "read_only") if name in requested]
        outcome = "pass" if len(required) == 2 and all(value == "pass" for value in required) else (
            "fail" if any(value == "fail" for value in required) else "inconclusive")
        report = {
            "schema_version": SCHEMA_VERSION, "status": "completed", "job_id": job_id, "identity_id": state["identity_id"],
            "identity_sha256": state["identity_sha256"], "contract_id": state["contract_id"],
            "bridge_contract_id": state["contract_id"],
            "suite_revision": state["suite_revision"], "groups": state["groups"], "matrix": matrix,
            "outcome": outcome, "scenarios": results, "finished_at": time.time(),
            **coverage(state["groups"], matrix),
            "budget": {"hard_limit_usd": None, "enforcement_verified": False,
                       "note": "syntax capability only; this qualification has no USD hard limit"},
            "note": "qualification evidence; a pass is not a business task acceptance",
        }
        _finalize_success(job_dir, report)
        _event(job_dir, "completed", "qualification job completed", outcome=outcome)
        return 0
    except InterruptedError:
        cleanup_confirmed = _cleanup_confirmed(results)
        cancel_matrix = _matrix_default("job_cancelled" if cleanup_confirmed else "cancel_cleanup_unconfirmed")
        def interrupted(value: dict[str, Any]) -> None:
            if cleanup_confirmed:
                value.update(status="cancelled", phase="cancelled", outcome="cancelled", matrix=cancel_matrix)
            else:
                value.update(status="needs_cleanup", phase="cancel_cleanup_unconfirmed", outcome="inconclusive",
                             matrix=cancel_matrix)
        final_state = _with_state(job_dir, interrupted)
        _write_terminal_report(job_dir, final_state, status_=final_state["status"], phase=final_state["phase"],
                               outcome=final_state["outcome"], matrix=cancel_matrix,
                               reason="qualification cancellation requested", results=results,
                               cleanup_status="confirmed" if cleanup_confirmed else "unconfirmed")
        _event(job_dir, "cancelled" if cleanup_confirmed else "cancel_cleanup_unconfirmed",
               "qualification cancelled before the next scenario" if cleanup_confirmed else "qualification cleanup is unconfirmed")
        return 0
    except TimeoutError as exc:
        timeout_matrix = _matrix_default("job_timed_out")
        cleanup_status = _terminal_cleanup_status(job_dir, _load_json(job_dir / "job.json"), results)
        def timed_out(value: dict[str, Any]) -> None:
            value.update(status="completed" if cleanup_status == "confirmed" else "needs_cleanup",
                         phase="timed_out" if cleanup_status == "confirmed" else "timeout_cleanup_unconfirmed",
                         outcome="inconclusive",
                         matrix=timeout_matrix,
                         failure={"kind": "TimeoutError", "reason": str(exc)})
        final_state = _with_state(job_dir, timed_out)
        _write_terminal_report(job_dir, final_state, status_=final_state["status"], phase=final_state["phase"],
                               outcome="inconclusive", matrix=timeout_matrix, reason=str(exc), results=results,
                               cleanup_status=cleanup_status)
        _event(job_dir, "timed_out", "qualification total wall limit reached")
        return 1
    except Exception as exc:
        current_state = _load_json(job_dir / "job.json")
        if current_state.get("phase") == "committing" and _success_report(job_dir, current_state) is not None:
            recovered = _recover_committing(job_dir, current_state)
            return 0 if recovered is not None and recovered.get("phase") == "completed" else 1
        error_matrix = _matrix_default("worker_error")
        cleanup_status = _terminal_cleanup_status(job_dir, current_state, results)
        def failed(value: dict[str, Any]) -> None:
            value.update(status="completed" if cleanup_status == "confirmed" else "needs_cleanup",
                         phase="worker_error" if cleanup_status == "confirmed" else "worker_error_cleanup_unconfirmed",
                         outcome="inconclusive",
                         matrix=error_matrix,
                         failure={"kind": type(exc).__name__, "reason": str(exc)})
        final_state = _with_state(job_dir, failed)
        _write_terminal_report(job_dir, final_state, status_=final_state["status"], phase=final_state["phase"],
                               outcome="inconclusive", matrix=error_matrix,
                               reason=f"{type(exc).__name__}: {exc}", results=results,
                               cleanup_status=cleanup_status)
        _event(job_dir, "worker_error", "qualification worker ended inconclusively", error_type=type(exc).__name__)
        return 1
    finally:
        for handle in held:
            handle.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("command", choices=["_worker"])
    parser.add_argument("job_id")
    args = parser.parse_args(argv)
    return _worker(args.job_id)


if __name__ == "__main__":
    raise SystemExit(main())
