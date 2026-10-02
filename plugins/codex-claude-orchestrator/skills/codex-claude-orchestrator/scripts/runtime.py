"""In-process registry for supervised bridge runs; intended for asyncio.to_thread callers."""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import secrets
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import bridge
from events import append, latest, latest_meaningful, read, statistics
import content_store  # bridge placed the plugin scripts directory on sys.path
import startup_protocol
from result_schema import finding_decisions as validate_finding_decisions
from stream_parser import provider_identity

# Must be taken while this module is being imported; see loaded_identity().
_LOADED_CODE = startup_protocol.loaded_identity(startup_protocol.plugin_root(Path(bridge.__file__).resolve()))


ACTIVE = {"starting", "running", "executing", "collecting", "cancelling"}
FINAL = {"reported", "blocked", "failed", "cancelled", "timeout", "unknown"}
SETTLED = FINAL - {"unknown"}
PHASES = {"preflight", "starting", "executing", "collecting", "reported", "blocked", "failed", "cancelled", "timeout", "unknown", "cancelling"}
_NO_REGISTRY_CHANGE = object()
_LOCAL_LANES: dict[str, Any] = {}
_LOCAL_LANES_GUARD = threading.RLock()


def workspace_changes(before: Any, after: Any) -> dict[str, Any]:
    """Compare recorded observations, never assign authorship from Git dirtiness."""
    result = {"status": "unknown", "changed_files": None,
              "preexisting_dirty_files": None, "worktree_dirty_files": None,
              "coverage": "snapshot evidence unavailable",
              "attribution": "observed between snapshots; not proof of which actor changed a file"}
    if not isinstance(before, dict) or not isinstance(after, dict):
        return result
    if before.get("kind") == "artifacts" and after.get("kind") == "artifacts":
        result.update(status="observed", changes=bridge.artifact_snapshot_difference(before, after),
                      coverage="declared file contents and directory structure; other file contents not inspected")
        return result
    required = ("head", "status_entries", "diff_hash", "dirty_content_hashes", "guarded_content_hashes")
    if not all(all(key in snap for key in required) for snap in (before, after)):
        return result
    def paths(snap):
        return {entry[key] for entry in snap["status_entries"] for key in ("path", "original") if entry.get(key)}
    old_paths, new_paths = paths(before), paths(after)
    result.update(preexisting_dirty_files=sorted(old_paths), worktree_dirty_files=sorted(new_paths),
                  coverage="Git status, dirty and declared file hashes; ignored files not enumerated; intermediate edits not observed")
    old = {**before["dirty_content_hashes"], **before["guarded_content_hashes"]}
    new = {**after["dirty_content_hashes"], **after["guarded_content_hashes"]}
    changed = old_paths ^ new_paths
    changed.update(path for path in old.keys() | new.keys() if old.get(path) != new.get(path))
    statuses = lambda snap: {entry["path"]: (entry["xy"], entry.get("original")) for entry in snap["status_entries"]}
    old_status, new_status = statuses(before), statuses(after)
    changed.update(path for path in old_status.keys() | new_status.keys() if old_status.get(path) != new_status.get(path))
    result["observed_changed_files"] = sorted(changed)
    # A HEAD change or an otherwise unlocated index-only diff cannot be mapped
    # to a complete path list using these snapshots. Never turn it into zero.
    if before["head"] != after["head"] or (before["diff_hash"] != after["diff_hash"] and not changed):
        result["coverage"] += "; HEAD/index change cannot be fully attributed to paths"
        return result
    result.update(status="observed", changed_files=sorted(changed))
    return result


class Runtime:
    """Thread-safe owner of bridge subprocesses; this is not a persistent daemon."""
    def __init__(self, state_root: Path):
        self.state_root = Path(state_root).expanduser().resolve()
        self.runs_root = self.state_root / "runs"
        self.packets_root = self.state_root / "packets"
        self.logs_root = self.state_root / "bridge-logs"
        for directory in (self.state_root, self.runs_root, self.packets_root, self.logs_root):
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.registry_path = self.state_root / "registry.json"
        self.registry_lock_path = self.state_root / "registry.lock"
        self.content = content_store.ContentStore(self.state_root / "content")
        self.owner_id = secrets.token_urlsafe(12)
        self._guard = threading.RLock()
        self._workers: dict[str, tuple[subprocess.Popen[str], Any]] = {}
        self._closing = False
        self._reconcile_incomplete()

    def _registry(self) -> dict[str, Any]:
        if not self.registry_path.exists():
            return {"runs": {}}
        return json.loads(self.registry_path.read_text(encoding="utf-8"))

    def _update(self, mutate):
        with self.registry_lock_path.open("a+") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            data = self._registry()
            result = mutate(data)
            if result is _NO_REGISTRY_CHANGE:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
                return None
            bridge.dump(self.registry_path, data)
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
            return result

    def _lane_lock(self, lane: str):
        lane = str(Path(lane).resolve())
        with _LOCAL_LANES_GUARD:
            if lane in _LOCAL_LANES:
                raise RuntimeError("another supervised run already owns this cwd's worktree lane")
            return self._open_lane_lock(lane)

    def _open_lane_lock(self, lane: str):
        from lane_lock import acquire
        try:
            handle = acquire(lane)
        except BlockingIOError:
            raise RuntimeError("another supervised run already owns this cwd's worktree lane")
        _LOCAL_LANES[lane] = handle
        return handle

    def _release_lane(self, lane: str, handle) -> None:
        # Match by handle: a watcher knows the run's cwd, which for a Git
        # subdirectory is not the lane key it holds.
        with _LOCAL_LANES_GUARD:
            for key, value in list(_LOCAL_LANES.items()):
                if value is handle:
                    _LOCAL_LANES.pop(key, None)
            handle.close()

    @staticmethod
    def _record_lane(record: dict[str, Any]) -> str:
        lane = record.get("lane_identity")
        if isinstance(lane, str) and lane:
            return lane
        try:
            return bridge.recorded_lane_identity(record.get("cwd"))
        except bridge.BridgeError as exc:
            raise RuntimeError(str(exc)) from exc

    @staticmethod
    def _legacy_lane_key(record: dict[str, Any]) -> str | None:
        """Exact-cwd lock key an older release held for a record without a lane identity."""
        if isinstance(record.get("lane_identity"), str) or not isinstance(record.get("cwd"), str):
            return None
        return str(Path(record["cwd"]).resolve())

    @staticmethod
    def _record_in_lane(record: dict[str, Any], lane: str) -> bool:
        recorded = record.get("lane_identity")
        if isinstance(recorded, str) and recorded:
            return recorded == lane
        cwd = record.get("cwd")
        if not isinstance(cwd, str) or not Path(cwd).is_absolute():
            return True  # unattributable evidence blocks rather than disappears
        return bridge.recorded_cwd_in_lane(cwd, lane)

    def _hold_record_lanes(self, record: dict[str, Any]) -> list[tuple[str, Any]]:
        """Hold the record's lane and, for a pre-identity record, the old exact-cwd key too.

        A free worktree lane alone cannot prove that an older release is not
        still holding its subdirectory lock.
        """
        keys = [self._record_lane(record)]
        legacy = self._legacy_lane_key(record)
        if legacy is not None and legacy != str(Path(keys[0]).resolve()):
            keys.append(legacy)
        held: list[tuple[str, Any]] = []
        try:
            for key in keys:
                held.append((key, self._lane_lock(key)))
        except BaseException:
            self._release_lanes(held)
            raise
        return held

    def _release_lanes(self, held: list[tuple[str, Any]]) -> None:
        for key, handle in reversed(held):
            self._release_lane(key, handle)

    def _run_lane(self, run_id: str, cwd: str) -> tuple[str, bool, str]:
        """Return (lane key, whether it is an established worktree lane, recorded cwd)."""
        record = self._registry().get("runs", {}).get(run_id)
        if isinstance(record, dict):
            lane = record.get("lane_identity")
            if isinstance(lane, str) and lane:
                return lane, True, record.get("cwd") if isinstance(record.get("cwd"), str) else cwd
            if isinstance(record.get("cwd"), str):
                cwd = record["cwd"]
        try:
            key, established = bridge.recorded_lane(cwd)
        except bridge.BridgeError as exc:
            raise RuntimeError(str(exc)) from exc
        return key, established, cwd

    def _unknown_marker(self, cwd: str) -> Path:
        return bridge.unknown_marker_path(bridge.recorded_lane_identity(str(Path(cwd).resolve())))

    def _mark_unknown_lane(self, cwd: str, run_id: str, reason: str) -> None:
        lane, established, recorded_cwd = self._run_lane(run_id, cwd)
        # Replaces this run's own launch-intent marker; the state root keeps
        # the evidence locatable from another Runtime's admission failure.
        bridge.publish_unknown_marker(lane, {"cwd": recorded_cwd, "run_id": run_id, "reason": reason,
                                             "state_root": str(self.state_root), "recorded_at": time.time()},
                                      established=established)

    def _own_unknown_markers(self, cwd: str, run_id: str) -> tuple[list[Path], list[Path]]:
        ours: list[Path] = []
        others: list[Path] = []
        record = self._registry().get("runs", {}).get(run_id, {})
        for path, value in bridge.unknown_markers(self._run_lane(run_id, cwd)[0]):
            owned = (isinstance(value, dict) and value.get("run_id") == run_id
                     and self._marker_from_this_runtime(record, value))
            (ours if owned else others).append(path)
        return ours, others

    def _require_matching_unknown_marker(self, cwd: str, run_id: str) -> list[Path]:
        """Return this run's markers, rejecting a lane whose only markers are foreign or malformed."""
        ours, others = self._own_unknown_markers(cwd, run_id)
        if others and not ours:
            raise RuntimeError("cwd unknown marker belongs to another or malformed recovery record; it was not cleared")
        return ours

    def _clear_matching_unknown_marker(self, cwd: str, run_id: str) -> None:
        """Clear only this run's admission markers; other runs' markers keep blocking the lane."""
        for marker in self._own_unknown_markers(cwd, run_id)[0]:
            marker.unlink(missing_ok=True)

    def _note_admission_cleanup(self, run_id: str, state: str, error: str | None = None) -> None:
        """Record a post-terminal cleanup fact; it never changes the run's execution status."""
        def note(data):
            rec = data.get("runs", {}).get(run_id)
            if not rec:
                return _NO_REGISTRY_CHANGE
            previous = rec.get("admission_cleanup") if isinstance(rec.get("admission_cleanup"), dict) else {}
            attempts = previous.get("attempts") if isinstance(previous.get("attempts"), int) else 0
            value = {"state": state, "attempts": attempts + 1, "attempted_at": time.time()}
            if error:
                value["error"] = error[:500]
            rec["admission_cleanup"] = value
        self._update(note)

    def _finish_admission_cleanup(self, run_id: str, cwd: str, *, record_success: bool = False) -> bool:
        """Clear a settled run's own lane markers, keeping any failure as a retryable fact.

        The execution outcome is already committed and stays as it is: an
        unlink failure neither turns it into unknown nor publishes another
        marker.  A marker that could not be removed keeps blocking the lane
        until admission or a restart retries this under the same proof.
        """
        try:
            self._clear_matching_unknown_marker(cwd, run_id)
        except Exception as exc:
            try:
                self._note_admission_cleanup(run_id, "pending", f"{type(exc).__name__}: {exc}")
            except Exception:
                pass
            return False
        try:
            current = self._registry().get("runs", {}).get(run_id, {})
            if record_success or isinstance(current.get("admission_cleanup"), dict):
                self._note_admission_cleanup(run_id, "completed")
        except Exception:
            pass
        return True

    def _retry_admission_cleanup(self, record: dict[str, Any]) -> bool:
        """Retry a settled run's own marker cleanup; the caller holds the run's lane exclusively.

        Only a bound, trusted terminal whose bridge and Claude process groups
        are proven stopped may clear its own markers.  Foreign or malformed
        markers are never touched and keep blocking.
        """
        run_id = record.get("run_id")
        evidence = self._trusted_terminal(record)
        if not evidence or evidence.get("status") != record.get("status"):
            self._note_admission_cleanup(run_id, "pending", "terminal binding, lifecycle or stopped-process proof is not currently available")
            return False
        return self._finish_admission_cleanup(run_id, record.get("cwd", ""), record_success=True)

    @staticmethod
    def _marker_values() -> list[Any]:
        values = []
        for path in sorted({path for root in bridge.unknown_marker_roots() for path in root.glob("*.json")}):
            try:
                values.append(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                continue
        return values

    def _marker_from_this_runtime(self, record: dict[str, Any], marker: dict[str, Any]) -> bool:
        intent = marker.get("launch_intent") if isinstance(marker.get("launch_intent"), dict) else {}
        # Missing legacy fields are allowed; every explicit owner must agree.
        return (("state_root" not in marker or marker["state_root"] == str(self.state_root))
                and ("run_dir" not in marker or marker["run_dir"] == record.get("run_dir"))
                and ("run_dir" not in intent or intent["run_dir"] == record.get("run_dir")))

    def _admission_cleanup_candidates(self, lane: str | None = None) -> list[dict[str, Any]]:
        """Settled runs with pending cleanup or a marker of their own, optionally limited to one lane."""
        runs = self._registry().get("runs", {})
        found: dict[str, dict[str, Any]] = {}
        for run_id, record in runs.items():
            cleanup = record.get("admission_cleanup")
            if (record.get("status") in SETTLED and isinstance(cleanup, dict) and cleanup.get("state") == "pending"
                    and (lane is None or self._record_in_lane(record, lane))):
                found[run_id] = record
        markers = [value for _, value in bridge.unknown_markers(lane)] if lane is not None else self._marker_values()
        for value in markers:
            if not isinstance(value, dict):
                continue
            record = runs.get(value.get("run_id"))
            if (isinstance(record, dict) and record.get("status") in SETTLED
                    and self._marker_from_this_runtime(record, value)):
                found[record["run_id"]] = record
        return list(found.values())

    def _retry_lane_admission_cleanup(self, lane: str) -> None:
        """Admission path: the caller already holds ``lane``."""
        for record in self._admission_cleanup_candidates(lane):
            legacy = self._legacy_lane_key(record)
            handle = None
            try:
                if legacy is not None and legacy != lane:
                    handle = self._lane_lock(legacy)
                self._retry_admission_cleanup(record)
            except Exception:
                # Whatever remains is reported by the marker check that follows.
                pass
            finally:
                if handle is not None:
                    self._release_lane(legacy, handle)

    @staticmethod
    def _has_reconciliation(record: dict[str, Any]) -> bool:
        reconciliation = record.get("reconciliation")
        return isinstance(reconciliation, dict) and reconciliation.get("outcome") == "confirmed_stopped"

    @classmethod
    def _unreconciled_unknown(cls, record: dict[str, Any]) -> bool:
        return record.get("status") == "unknown" and not cls._has_reconciliation(record)

    def _reconcile_incomplete(self) -> None:
        # A free lane proves that no bridge still owns the worktree.  Before
        # falling back to unknown, adopt a terminal receipt only when it is
        # bound to the exact run/packet and all recorded process groups are gone.
        data = self._registry()
        for run_id, record in data.get("runs", {}).items():
            if record.get("status") not in ACTIVE and not self._unreconciled_unknown(record):
                continue
            try:
                held = self._hold_record_lanes(record)
            except RuntimeError:
                if record.get("status") in ACTIVE:
                    self._refresh(run_id, allow_unowned_active=True)
                continue
            try:
                evidence = self._trusted_terminal(record)
                if evidence:
                    self._adopt_trusted_terminal(run_id, evidence)
                elif record.get("status") in ACTIVE:
                    self._mark_run_unknown(run_id, "runtime restarted without a confirmed owner; manual reconciliation required")
            finally:
                self._release_lanes(held)
        # A settled run whose own marker cleanup failed earlier is retried
        # here; a lane still held elsewhere is retried by its next admission.
        for record in self._admission_cleanup_candidates():
            try:
                held = self._hold_record_lanes(record)
            except RuntimeError:
                continue
            try:
                self._retry_admission_cleanup(record)
            except Exception:
                pass
            finally:
                self._release_lanes(held)

    def _packet_path(self, run_id: str) -> Path:
        return self.packets_root / f"{run_id}.json"

    def _lifecycle_path(self, run_id: str) -> Path:
        return self.state_root / "bridge-receipts" / f"{run_id}.json"

    @staticmethod
    def _file_sha256(path: Path) -> str | None:
        try:
            return hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            return None

    def _packet_binding(self, record: dict[str, Any], *, require_run_copy: bool) -> dict[str, Any] | None:
        run_id = record.get("run_id")
        if not isinstance(run_id, str):
            return None
        packet_path = self._packet_path(run_id)
        expected_sha = record.get("packet_sha256")
        try:
            packet_bytes = packet_path.read_bytes()
        except OSError:
            return None
        actual_sha = hashlib.sha256(packet_bytes).hexdigest()
        if expected_sha is not None and (not isinstance(expected_sha, str) or actual_sha != expected_sha):
            return None
        try:
            packet = json.loads(packet_bytes)
        except (ValueError, TypeError):
            return None
        if not isinstance(packet, dict):
            return None
        if (packet.get("task_id") != record.get("task_id") or packet.get("revision") != record.get("revision")
                or packet.get("cwd") != record.get("cwd")):
            return None
        descriptor_sha = record.get("cli_descriptor_sha256")
        if descriptor_sha:
            descriptor_path = self.packets_root / f"{run_id}.cli.json"
            if self._file_sha256(descriptor_path) != descriptor_sha:
                return None
            run_descriptor = self._run_dir(run_id) / "cli-selection.json"
            if require_run_copy and self._read_json(run_descriptor) != self._read_json(descriptor_path):
                return None
        if record.get("run_dir") != str(self._run_dir(run_id)):
            return None
        if record.get("model") is not None and packet.get("model") != record.get("model"):
            return None
        if require_run_copy:
            try:
                run_packet = json.loads((self._run_dir(run_id) / "packet.json").read_bytes())
                if run_packet != packet:
                    return None
            except (OSError, ValueError, TypeError):
                return None
        return packet

    def _trusted_terminal(self, record: dict[str, Any]) -> dict[str, Any] | None:
        """Return bound terminal evidence; process/lane absence is checked by the caller."""
        run_id = record.get("run_id")
        if not isinstance(run_id, str) or self._packet_binding(record, require_run_copy=False) is None:
            return None
        lifecycle = self._read_json(self._lifecycle_path(run_id))
        if isinstance(record.get("packet_sha256"), str) and isinstance(lifecycle, dict):
            bound = (lifecycle.get("schema_version") == 1 and lifecycle.get("run_id") == run_id
                     and lifecycle.get("task_id") == record.get("task_id")
                     and lifecycle.get("revision") == record.get("revision")
                     and lifecycle.get("cwd") == record.get("cwd")
                     and lifecycle.get("packet_sha256") == record.get("packet_sha256"))
            if record.get("cli_descriptor_sha256"):
                bound = bound and lifecycle.get("cli_descriptor_sha256") == record["cli_descriptor_sha256"]
            bound = bound and startup_protocol.nonce_bound(lifecycle, record)
            status = lifecycle.get("status")
            started = lifecycle.get("child_started")
            phase = lifecycle.get("phase")
            shape = ((started is False and phase == "pre_dispatch" and status != "reported")
                     or (started is True and phase == "terminal"))
            if bound and lifecycle.get("terminal") is True and status in FINAL and shape:
                bridge_pid = lifecycle.get("bridge_pid", record.get("bridge_process_group"))
                bridge_state = self._process_presence(bridge_pid, group=True, label="bridge process group",
                                                      identity=lifecycle.get("bridge_identity") or record.get("bridge_identity"))
                if bridge_state["state"] != "stopped":
                    return None
                if started:
                    child_group = lifecycle.get("child_process_group")
                    child_state = self._process_presence(child_group, group=True, label="Claude child process group",
                                                         identity=lifecycle.get("child_identity"))
                    if child_state["state"] != "stopped":
                        return None
                return {"status": status, "source": "bridge_lifecycle", "value": lifecycle}

        # Backward compatibility for 0.4.0 runs that completed after their
        # Runtime owner died.  A missing child.json is deliberately not enough:
        # Popen can succeed before child.json is persisted.
        if startup_protocol.has_startup_binding(record):
            return None
        legacy_packet = self._packet_binding(record, require_run_copy=True)
        if legacy_packet is None:
            return None
        try:
            normalized_legacy = bridge.validate_packet(dict(legacy_packet))
        except Exception:
            return None
        if (normalized_legacy.get("task_id") != record.get("task_id")
                or normalized_legacy.get("revision") != record.get("revision")
                or normalized_legacy.get("cwd") != record.get("cwd")):
            return None
        run_dir = self._run_dir(run_id)
        receipt = self._read_json(run_dir / "receipt.json")
        if not isinstance(receipt, dict) or receipt.get("status") not in FINAL:
            return None
        if receipt.get("task_id") != record.get("task_id") or receipt.get("revision") != record.get("revision"):
            return None
        bridge_state = self._process_presence(record.get("bridge_process_group"), group=True,
                                              label="recorded bridge process group", identity=record.get("bridge_identity"))
        child = self._read_json(run_dir / "child.json")
        child_group = child.get("process_group") if isinstance(child, dict) else None
        child_state = self._process_presence(child_group, group=True, label="recorded Claude child process group",
                                             identity=child.get("identity") if isinstance(child, dict) else None)
        if bridge_state["state"] == "stopped" and child_state["state"] == "stopped":
            return {"status": receipt["status"], "source": "bound_run_receipt", "value": receipt}
        return None

    def _mark_run_unknown(self, run_id: str, summary: str) -> None:
        def mark(data):
            rec = data.get("runs", {}).get(run_id)
            if not rec or rec.get("status") in FINAL:
                return _NO_REGISTRY_CHANGE
            rec.update(status="unknown", phase="unknown", updated_at=time.time(), ended_at=time.time(), summary=summary)
        self._update(mark)
        current = self._registry().get("runs", {}).get(run_id, {})
        if current.get("status") == "unknown":
            self._mark_unknown_lane(current.get("cwd", ""), run_id, summary)

    def _adopt_trusted_terminal(self, run_id: str, evidence: dict[str, Any]) -> None:
        self._refresh(run_id, trusted_terminal=evidence)
        current = self._registry().get("runs", {}).get(run_id, {})
        if current.get("status") == "unknown":
            self._mark_unknown_lane(current.get("cwd", ""), run_id, "bridge returned a trustworthy unknown terminal receipt")
            return
        self._finish_admission_cleanup(run_id, current.get("cwd", ""))

    def _sync_unowned(self, run_id: str) -> None:
        with self._guard:
            record = self._registry().get("runs", {}).get(run_id)
            if not record or (record.get("status") not in ACTIVE and not self._unreconciled_unknown(record)):
                return
            # A UI snapshot of an unresolved unknown run must not contend for
            # the CWD lane unless there is terminal evidence it can actually
            # adopt.  Reconciliation still reacquires the lane and rechecks all
            # evidence before changing any state.
            if self._unreconciled_unknown(record):
                candidate = self._trusted_terminal(record)
                if candidate is None or candidate.get("status") == "unknown":
                    return
            try:
                held = self._hold_record_lanes(record)
            except RuntimeError:
                if record.get("status") in ACTIVE:
                    self._refresh(run_id, allow_unowned_active=True)
                return
            try:
                current = self._registry().get("runs", {}).get(run_id)
                if not current or (current.get("status") not in ACTIVE and not self._unreconciled_unknown(current)):
                    return
                evidence = self._trusted_terminal(current)
                if evidence and (current.get("status") in ACTIVE or evidence.get("status") != "unknown"):
                    self._adopt_trusted_terminal(run_id, evidence)
                elif current.get("status") in ACTIVE:
                    self._mark_run_unknown(run_id, "bridge ownership ended without a trustworthy terminal receipt; manual reconciliation required")
            finally:
                self._release_lanes(held)

    def _run_dir(self, run_id: str) -> Path:
        if not isinstance(run_id, str) or not run_id.startswith("run-") or any(c not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-" for c in run_id[4:]):
            raise ValueError("invalid run_id")
        return self.runs_root / run_id

    def start(self, packet: dict, timeout: float | None = None, resume_run_id: str | None = None, expected_content_digest: str | None = None) -> dict:
        packet = dict(packet)
        if resume_run_id:
            previous_packet = self._read_json(self._run_dir(resume_run_id) / "packet.json") or {}
            for key in ("review_mode", "review_scope"):
                if key not in packet and key in previous_packet:
                    packet[key] = previous_packet[key]
        normalized = bridge.validate_packet(packet)
        if timeout is None:
            timeout = 3600 if normalized.get("review_mode") == "isolated" and normalized.get("review_scope") == "full" else 300
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 1 <= timeout <= 14400:
            raise ValueError("timeout must be 1..14400 seconds")
        lane_identity = bridge.lane_identity(Path(normalized["cwd"]), normalized["workspace_kind"])
        with self._guard:
            if self._closing:
                raise RuntimeError("runtime is closing; it will not dispatch a new Claude process")
            # Rejected before any run, packet, lifecycle or marker exists: this
            # process would otherwise pair its loaded modules with other bridge code.
            bridge_script = Path(bridge.__file__).resolve()
            code_identity = startup_protocol.require_unchanged(_LOADED_CODE, startup_protocol.plugin_root(bridge_script))
            lane = self._lane_lock(lane_identity)
            proc = None
            thread = None
            stdout = None
            stderr = None
            cancel_request = None
            run_id = None
            try:
                previous = self._admit_start(normalized, lane_identity, resume_run_id)
                content_binding = self._content_binding(previous if resume_run_id else None, expected_content_digest)
                cli_descriptor = bridge.create_cli_descriptor(normalized, Path(previous["run_dir"]) if resume_run_id else None)
                run_id = "run-" + secrets.token_urlsafe(12).replace("-", "_")
                run_dir = self._run_dir(run_id)
                command, lifecycle_path = self._prepare_start(
                    normalized, run_id, run_dir, lane_identity, previous, cli_descriptor,
                    content_binding, bridge_script, code_identity, timeout, resume_run_id)
                stdout =(self.logs_root / f"{run_id}.stdout.log").open("w", encoding="utf-8")
                stderr = (self.logs_root / f"{run_id}.stderr.log").open("w", encoding="utf-8")
                environment = dict(os.environ)
                environment.update(lane.inherited_environment())
                cancel_request = self.state_root / "cancel-requests" / f"{run_id}.json"
                environment["CODEX_BRIDGE_CANCEL_FILE"] = str(cancel_request)
                environment["CODEX_BRIDGE_LIFECYCLE_FILE"] = str(lifecycle_path)
                cancel_request.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                # The bridge must not inherit this server's cwd, which a plugin
                # reinstall can delete; Claude itself still runs in packet cwd.
                proc = subprocess.Popen(command, cwd=str(self.state_root), stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr,
                                        text=True, start_new_session=True, env=environment, pass_fds=lane.filenos())
                bridge_identity = bridge.capture_process_identity(proc.pid)
                self._update(lambda data: data["runs"][run_id].update(bridge_pid=proc.pid, bridge_process_group=proc.pid,
                                                                        bridge_identity=bridge_identity,
                                                                        updated_at=time.time()))
                self._workers[run_id] = (proc, lane)
                thread = threading.Thread(target=self._watch, args=(run_id, proc, lane, stdout, stderr), daemon=True)
                thread.start()
                return self.snapshot(run_id)
            except Exception as exc:
                if proc is not None and run_id is not None:
                    self._handle_started_failure(exc, normalized, run_id, run_dir, lane_identity,
                                                 lane, proc, thread, stdout, stderr, cancel_request)
                    raise
                if run_id is not None:
                    try:
                        for handle in (stdout, stderr):
                            if handle is not None and not handle.closed:
                                handle.close()
                        self._update(lambda data, detail=str(exc): data["runs"][run_id].update(status="failed", phase="failed", updated_at=time.time(),
                                                                              summary=f"bridge spawn failed before process start: {detail}"))
                    except Exception:
                        pass
                self._release_lane(lane_identity, lane)
                raise

    def _admit_start(self, normalized: dict[str, Any], lane_identity: str,
                     resume_run_id: str | None) -> dict[str, Any] | None:
        """Reconcile the exclusively held lane, then validate revision/resume admission."""
        # Holding the lane lets this Runtime safely settle an orphaned
        # prior record from any cwd of this worktree before deciding
        # whether a new dispatch is legal.
        for old in self._registry().get("runs", {}).values():
            if old.get("status") not in ACTIVE and not self._unreconciled_unknown(old):
                continue
            if not isinstance(old.get("cwd"), str) or not self._record_in_lane(old, lane_identity):
                continue
            legacy = self._legacy_lane_key(old)
            legacy_handle = None
            if legacy is not None and legacy != lane_identity:
                try:
                    legacy_handle = self._lane_lock(legacy)
                except RuntimeError as exc:
                    raise RuntimeError("a supervised run recorded before worktree lanes may still own "
                                       f"{old['cwd']}; wait for it or reconcile it before dispatch") from exc
            try:
                evidence = self._trusted_terminal(old)
                if evidence:
                    self._adopt_trusted_terminal(old["run_id"], evidence)
                elif old.get("status") in ACTIVE:
                    self._mark_run_unknown(old["run_id"], "prior bridge no longer owns the cwd and has no trustworthy terminal receipt")
            finally:
                if legacy_handle is not None:
                    self._release_lane(legacy, legacy_handle)
        self._retry_lane_admission_cleanup(lane_identity)
        if bridge.unknown_markers(lane_identity):
            raise RuntimeError("cwd worktree has an unknown prior supervised run; reconcile it manually before dispatch")
        registry = self._registry()
        same_task = [r for r in registry.get("runs", {}).values() if r.get("task_id") == normalized["task_id"] and r.get("cwd") == normalized["cwd"]]
        if any((self._unreconciled_unknown(r) or r.get("status") in ACTIVE) and self._record_in_lane(r, lane_identity)
               for r in registry.get("runs", {}).values()):
            raise RuntimeError("cwd worktree has an unknown or unsettled recorded run; reconcile it manually before dispatch")
        if any(r.get("revision") == normalized["revision"] for r in same_task):
            raise ValueError("task revision already exists for this cwd")
        if same_task and normalized["revision"] <= max(r["revision"] for r in same_task):
            raise ValueError("task revision must strictly increase for this cwd")
        previous = None
        if resume_run_id:
            previous = registry.get("runs", {}).get(resume_run_id)
            if not previous:
                raise ValueError("resume_run_id was not found")
            if (previous.get("status") != "reported" or previous.get("superseded_by") or previous.get("task_id") != normalized["task_id"]
                    or previous.get("cwd") != normalized["cwd"] or normalized["revision"] <= previous.get("revision", 0)):
                raise RuntimeError("resume_run_id must have a reported terminal receipt")
            bridge.validate_resume(normalized, Path(previous["run_dir"]))
        else:
            candidates = [r for r in same_task if r.get("revision", 0) < normalized["revision"]]
            previous = max(candidates, key=lambda r: r["revision"]) if candidates else None
        return previous

    def _prepare_start(self, normalized: dict[str, Any], run_id: str, run_dir: Path, lane_identity: str,
                       previous: dict[str, Any] | None, cli_descriptor: dict[str, Any],
                       content_binding: dict[str, Any] | None, bridge_script: Path,
                       code_identity: dict[str, Any], timeout: float, resume_run_id: str | None) -> tuple[list[str], Path]:
        """Persist immutable launch bindings and register intent before any bridge exists."""
        packet_path = self.packets_root / f"{run_id}.json"
        bridge.dump(packet_path, normalized)
        packet_sha256 = self._file_sha256(packet_path)
        cli_path = self.packets_root / f"{run_id}.cli.json"
        bridge.dump(cli_path, cli_descriptor)
        cli_sha256 = self._file_sha256(cli_path)
        content_path = content_sha256 = None
        if content_binding is not None:
            content_path = self.packets_root / f"{run_id}.content.json"
            bridge.dump(content_path, content_binding)
            content_sha256 = self._file_sha256(content_path)
        lifecycle_path = self._lifecycle_path(run_id)
        lifecycle_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        startup_nonce = secrets.token_hex(16)
        # Written once, before the bridge exists; only the bridge may
        # replace it, after verifying this binding.
        bridge.dump(lifecycle_path, startup_protocol.pre_spawn_record(
            run_id=run_id, task_id=normalized["task_id"], revision=normalized["revision"], cwd=normalized["cwd"],
            packet_sha256=packet_sha256, cli_descriptor_sha256=cli_sha256, lane_identity=lane_identity,
            nonce=startup_nonce, code_value=code_identity["value"]))
        record = {"run_id": run_id, "task_id": normalized["task_id"], "revision": normalized["revision"], "cwd": normalized["cwd"],
                  "lane_identity": lane_identity, "status": "starting", "phase": "starting", "started_at": time.time(), "updated_at": time.time(),
                  "last_activity_at": None, "model": normalized["model"], "session_id": None, "summary": "bridge starting",
                  "requested_model": normalized["model"], "effort": normalized["effort"],
                  "objective": normalized["objective"], "role": normalized["role"],
                  "review_mode": normalized.get("review_mode"), "review_scope": normalized.get("review_scope"),
                  "request_provenance": normalized.get("request_provenance"), "timeout_seconds": timeout,
                  "scope": {"cwd": normalized["cwd"], "owned_files": normalized["owned_files"],
                            "input_files": normalized.get("input_files", [])},
                  "decision": None, "previous_run_id": previous.get("run_id") if previous else None, "run_dir": str(run_dir),
                  "owner_id": self.owner_id, "packet_sha256": packet_sha256, "lifecycle_file": str(lifecycle_path),
                  "cli_descriptor_file": str(cli_path), "cli_descriptor_sha256": cli_sha256,
                  "cli_identity_id": cli_descriptor.get("identity_id"),
                  "content_binding": content_store.binding_summary(content_binding),
                  "content_binding_file": str(content_path) if content_path else None,
                  "content_binding_sha256": content_sha256,
                  "startup_nonce": startup_nonce,
                  "startup_protocol_version": startup_protocol.STARTUP_PROTOCOL_VERSION,
                  "bridge_code_identity": code_identity["value"], "bridge_script": str(bridge_script),
                  "bridge_spawn_cwd": str(self.state_root),
                  "changed_files": None, "workspace_changes": workspace_changes(None, None),
                  "environment": {"isolation": "isolated OS source-write protection pending dispatch" if normalized.get("review_mode") == "isolated" else "strict tool guards; no OS sandbox"}}
        record["events_count"] = 0
        def register(data):
            data.setdefault("runs", {})[run_id] = record
            for old in data["runs"].values():
                if old.get("task_id") == normalized["task_id"] and old.get("cwd") == normalized["cwd"] and old.get("revision", 0) < normalized["revision"]:
                    old.setdefault("superseded_by", run_id)
                    old["updated_at"] = time.time()
        self._update(register)
        command = [sys.executable, str(bridge_script), "run", "--packet", str(packet_path), "--run-dir", str(run_dir), "--timeout", str(timeout),
                   "--cli-descriptor", str(cli_path), "--cli-descriptor-sha256", cli_sha256,
                   startup_protocol.NONCE_ARGUMENT, startup_nonce]
        if resume_run_id:
            command += ["--resume-from", previous["run_dir"]]
        if content_path is not None:
            command += ["--content-binding", str(content_path), "--content-binding-sha256", content_sha256]
        return command, lifecycle_path

    def _handle_started_failure(self, exc: Exception, normalized: dict[str, Any], run_id: str,
                                run_dir: Path, lane_identity: str, lane, proc, thread,
                                stdout, stderr, cancel_request: Path | None) -> None:
        """Request durable cancellation after Popen; retain uncertainty and a reaping owner."""
        watcher_alive = thread is not None and thread.is_alive()
        # The bridge exists and inherited the CWD lane FD.  Do not
        # relabel this as a spawn failure or signal the process
        # tree: request cancellation through the durable file
        # protocol and leave an auditable unknown until the bridge
        # and any Claude child are confirmed stopped.
        request = {"reason": f"runtime supervision setup failed: {exc}",
                   "requested_at": time.time(), "requested_by": self.owner_id}
        try:
            if cancel_request is not None:
                bridge.dump(cancel_request, request)
        except Exception:
            pass
        try:
            if run_dir.is_dir():
                bridge.dump(run_dir / "cancel.json", request)
        except Exception:
            pass
        def mark_post_spawn_unknown(data):
            rec = data.get("runs", {}).get(run_id)
            if rec:
                rec.update(status="unknown", phase="unknown", updated_at=time.time(),
                           bridge_pid=proc.pid, bridge_process_group=proc.pid,
                           supervision_failure={"stage": "post_bridge_popen", "error": str(exc),
                                                "cancel_requested_at": request["requested_at"]},
                           summary="runtime lost supervision after bridge start; cancellation requested and recovery evidence is required")
        try:
            self._update(mark_post_spawn_unknown)
        except Exception:
            pass
        try:
            self._mark_unknown_lane(normalized["cwd"], run_id, "runtime supervision failed after bridge start")
        except Exception:
            pass
        if not watcher_alive:
            # Keep one owner for the Popen handle and lane when the
            # runtime can still create a recovery watcher.  It only
            # reaps and records the already-cancel-requested bridge;
            # it does not signal it.
            recovery_thread = threading.Thread(target=self._watch,
                                               args=(run_id, proc, lane, stdout, stderr), daemon=True)
            self._workers[run_id] = (proc, lane)
            try:
                recovery_thread.start()
                watcher_alive = True
            except Exception:
                self._workers.pop(run_id, None)
        if not watcher_alive:
            for handle in (stdout, stderr):
                if handle is not None and not handle.closed:
                    handle.close()
            self._release_lane(lane_identity, lane)

    def _content_binding(self, previous: dict[str, Any] | None, expected_digest: str | None = None) -> dict[str, Any] | None:
        """Fresh tasks pin the effective reviewed content; a resume keeps the prior run's pin."""
        try:
            if previous is None:
                return self.content.pin(expected_digest, require_read=True)
            path = Path(previous["run_dir"]) / "content-binding.json"
            expected = previous.get("content_binding_sha256")
            if not path.is_file():
                if expected:
                    raise RuntimeError("the prior run's pinned coordination content record is missing; start a fresh revision")
                if expected_digest is not None:
                    raise RuntimeError("legacy resumed run has no pinned coordination content; do not substitute current content")
                return None  # recorded before content pinning existed
            binding = self._read_json(path)
            if expected:
                pinned = self.packets_root / f"{previous.get('run_id')}.content.json"
                if self._file_sha256(pinned) != expected or self._read_json(pinned) != binding:
                    raise RuntimeError("the prior run's pinned coordination content record changed; start a fresh revision")
            content_store.verify_binding(binding)
            if expected_digest is not None and expected_digest != binding["digest"]:
                raise RuntimeError("resume must use the prior run's pinned coordination content")
            return binding
        except content_store.ContentError as exc:
            raise RuntimeError(str(exc)) from exc

    def _watch(self, run_id, proc, lane, stdout, stderr) -> None:
        packet = self._read_json(self._packet_path(run_id)) or {}
        cwd = packet.get("cwd") if isinstance(packet.get("cwd"), str) else ""
        terminated = False
        try:
            proc.wait()
            terminated = True
            stdout.close(); stderr.close()
            self._archive_bridge_logs(run_id)
            if not os.path.lexists(self._run_dir(run_id)):
                try:
                    logs = self._bridge_log_evidence(run_id)
                    self._update(lambda data: data["runs"][run_id].update(bridge_logs=logs)
                                 if run_id in data.get("runs", {}) else _NO_REGISTRY_CHANGE)
                except Exception:
                    pass
            current = self._registry().get("runs", {}).get(run_id, {})
            if isinstance(current.get("cwd"), str):
                cwd = current["cwd"]
            trusted_terminal = self._trusted_terminal(current) if current else None
            self._refresh(run_id, proc.returncode, owned=True, trusted_terminal=trusted_terminal)
            final = self._registry().get("runs", {}).get(run_id, {})
            if isinstance(final.get("cwd"), str):
                cwd = final["cwd"]
            if final.get("status") == "unknown":
                self._mark_unknown_lane(cwd, run_id, "bridge exited without a trustworthy terminal receipt")
            else:
                self._finish_admission_cleanup(run_id, cwd)
        except Exception as exc:
            if terminated and cwd:
                try:
                    self._mark_unknown_lane(cwd, run_id, f"runtime watcher failed after bridge exit: {exc}")
                except Exception:
                    pass
        finally:
            for handle in (stdout, stderr):
                try:
                    if handle is not None and not handle.closed:
                        handle.close()
                except Exception:
                    pass
            # Releasing this process's lane is safe only after wait() confirms
            # the bridge has exited.  The lifecycle/unknown evidence remains
            # available for a later Runtime to reconcile any Claude child.
            if terminated:
                try:
                    self._release_lane(cwd, lane)
                finally:
                    with self._guard:
                        self._workers.pop(run_id, None)

    def _archive_bridge_logs(self, run_id: str) -> None:
        run_dir = self._run_dir(run_id)
        if not run_dir.exists():
            return
        for source, name in ((self.logs_root / f"{run_id}.stdout.log", "runtime_bridge.stdout.log"),
                             (self.logs_root / f"{run_id}.stderr.log", "runtime_bridge.stderr.log")):
            try:
                os.replace(source, run_dir / name)
            except OSError:
                pass

    def _bridge_log_evidence(self, run_id: str) -> dict[str, Any]:
        """Locate and hash the bridge's own stdout/stderr without exposing their text."""
        run_dir = self._run_dir(run_id)
        evidence: dict[str, Any] = {}
        for stream in ("stdout", "stderr"):
            evidence[stream] = {"path": None, "state": "missing"}
            for path in (run_dir / f"runtime_bridge.{stream}.log", self.logs_root / f"{run_id}.{stream}.log"):
                try:
                    size = path.stat().st_size
                    digest = bridge.sha256_file(path)
                except OSError:
                    continue
                evidence[stream] = {"path": str(path), "state": "present", "size": size, "sha256": digest}
                break
        return evidence

    def _recovery_receipt_path(self, run_id: str) -> Path:
        return self.state_root / "recovery-receipts" / f"{self._run_dir(run_id).name}.json"

    def startup_readiness(self) -> dict[str, Any]:
        return startup_protocol.startup_readiness(_LOADED_CODE, Path(bridge.__file__).resolve(), spawn_cwd=self.state_root)

    def _refresh(self, run_id: str, exit_code: int | None = None, owned: bool = False,
                 allow_unowned_active: bool = False, trusted_terminal: dict[str, Any] | None = None) -> None:
        run_dir = self._run_dir(run_id)
        current = self._registry().get("runs", {}).get(run_id)
        if not current:
            return
        if allow_unowned_active and self._packet_binding(current, require_run_copy=run_dir.is_dir()) is None:
            return
        state = self._read_json(run_dir / "state.json") or {}
        receipt = self._read_json(run_dir / "receipt.json") or {}
        result = self._read_json(run_dir / "result.json") or {}
        event_count, last_event = statistics(run_dir)
        status = (trusted_terminal or {}).get("status") or receipt.get("status") or state.get("status") or current.get("status", "unknown")
        # A receipt can become visible just before the owned watcher archives
        # bridge logs and publishes its exit code.  Keep that local run active
        # until the watcher completes evidence finalization.  Unowned recovery
        # still adopts a bound terminal lifecycle receipt through
        # trusted_terminal after acquiring the now-free lane.
        local_terminal_pending = (owned and exit_code is None and run_id in self._workers
                                  and current.get("status") in ACTIVE and status in FINAL)
        if local_terminal_pending:
            status = "cancelling" if current.get("status") == "cancelling" else "collecting"
        unconfirmed_unowned_terminal = allow_unowned_active and not trusted_terminal and status in FINAL
        if unconfirmed_unowned_terminal:
            state_status = state.get("status")
            status = state_status if state_status in ACTIVE else current.get("status", "unknown")
        # Reaping the outer bridge proves only that wrapper exited.  A final
        # receipt is publishable by Runtime only after _trusted_terminal also
        # proves its binding and every recorded process group has stopped.
        if exit_code is not None and not trusted_terminal:
            status = "unknown"
        event_phase = last_event.get("status") if last_event else None
        # Provider stream values such as init/success are evidence, not public
        # Runtime phases.  Keep the lifecycle monotonic and meaningful while
        # the bridge is still collecting a final result/receipt.
        if event_phase in PHASES:
            phase = event_phase
        elif event_phase == "init":
            phase = "executing"
        elif event_phase == "success":
            phase = "collecting"
        else:
            phase = current.get("phase") if current.get("phase") in PHASES else status
        if local_terminal_pending:
            phase = status
        if unconfirmed_unowned_terminal and phase in FINAL:
            phase = current.get("phase") if current.get("phase") in ACTIVE else status
        if status in FINAL:
            phase = status
        structured_summary = ((result.get("structured") or {}).get("summary")
                              if isinstance(result.get("structured"), dict) else None)
        state_error = state.get("error")
        receipt_reason = receipt.get("reason")
        has_summary = bool(structured_summary or state_error or receipt_reason)
        summary = structured_summary or state_error or receipt_reason or "run state recorded"
        if structured_summary and status in {"failed", "cancelled", "timeout", "blocked", "unknown"}:
            summary = f"执行状态 {status}；报告已保留，未验收。"
            if state_error or receipt_reason:
                summary += " " + str(state_error or receipt_reason)
        if local_terminal_pending:
            summary = current.get("summary") or "bridge finalizing terminal evidence"
        if unconfirmed_unowned_terminal:
            summary = current.get("summary") or "run state recorded"
        if current.get("status") == "cancelling" and status in ACTIVE:
            summary = current.get("summary") or summary
        if trusted_terminal:
            terminal_value = trusted_terminal.get("value") if isinstance(trusted_terminal.get("value"), dict) else {}
            if not has_summary:
                summary = terminal_value.get("reason") or terminal_value.get("note") or summary
        if exit_code is not None and not trusted_terminal and status == "unknown":
            summary = "bridge exited without trustworthy stopped-process terminal evidence; inspect processes and workspace before retrying"
        before = self._read_json(run_dir / "workspace_before.json") or self._read_json(run_dir / "git_before.json") or {}
        after = self._read_json(run_dir / "workspace_after.json") or self._read_json(run_dir / "git_after.json") or {}
        changes = workspace_changes(before, after)
        environment = self._read_json(run_dir / "environment.json") or {}
        live_identity = self._live_provider_identity(run_dir)
        safe_environment = {"status": environment.get("status"), "cli_version": (environment.get("cli") or {}).get("version"),
                            "auth_status": (environment.get("auth") or {}).get("status"),
                            "cli_identity": environment.get("cli_descriptor"),
                            "isolation": "OS source/control-write protection; credentials/network not isolated" if result.get("review_workspace") else "strict tool guards; no OS sandbox"}
        def observation(rec):
            current_status = rec.get("status")
            if current_status in FINAL:
                if not (current_status == "unknown" and trusted_terminal and status in FINAL):
                    return None
            if status in ACTIVE and not (owned or allow_unowned_active):
                return None
            effective_status = status
            effective_phase = phase
            if current_status == "cancelling" and status in ACTIVE and status != "cancelling":
                effective_status = effective_phase = "cancelling"
            ended_at = rec.get("ended_at")
            if effective_status in FINAL and not ended_at:
                ended_at = time.time()
            updates = {"status": effective_status, "phase": effective_phase,
                       "last_event": last_event or rec.get("last_event"),
                       "last_activity_at": last_event["received_at"] if last_event else rec.get("last_activity_at"),
                       "session_id": result.get("actual_session_id") or live_identity.get("session_id") or rec.get("session_id"),
                       "initialized_model": result.get("initialized_model") or result.get("cli_resolved_model")
                                            or result.get("system_init_model") or live_identity.get("initialized_model"),
                       "cli_resolved_model": result.get("cli_resolved_model") or result.get("initialized_model")
                                             or result.get("system_init_model") or live_identity.get("initialized_model"),
                       "actual_models": (result.get("actual_models") if isinstance(result.get("actual_models"), list)
                                         else result.get("provider_models") if isinstance(result.get("provider_models"), list)
                                         else live_identity.get("actual_models", [])),
                       "actual_model_source": result.get("actual_model_source") or live_identity.get("actual_model_source"),
                       "provider_response_observed": bool(result.get("provider_response_observed")
                                                          or result.get("provider_subtype") or live_identity.get("provider_response_observed")),
                       "summary": summary, "events_count": event_count, "changed_files": changes["changed_files"], "workspace_changes": changes, "environment": safe_environment,
                       "bridge_exit_code": exit_code if exit_code is not None else rec.get("bridge_exit_code"), "ended_at": ended_at}
            updates["provider_models"] = list(updates["actual_models"])
            updates["actual_model"] = updates["actual_models"][0] if len(updates["actual_models"]) == 1 else None
            if trusted_terminal:
                updates["terminal_evidence"] = trusted_terminal.get("source")
            return updates

        initial_updates = observation(current)
        if initial_updates is None or all(current.get(key) == value for key, value in initial_updates.items()):
            return

        def mutate(data):
            rec = data["runs"].get(run_id)
            if not rec:
                return _NO_REGISTRY_CHANGE
            updates = observation(rec)
            if updates is None:
                return _NO_REGISTRY_CHANGE
            if all(rec.get(key) == value for key, value in updates.items()):
                return _NO_REGISTRY_CHANGE
            updates["updated_at"] = time.time()
            rec.update(updates)
        self._update(mutate)

    @staticmethod
    def _live_provider_identity(run_dir: Path) -> dict[str, Any]:
        """Extract only provider identity fields from an in-progress stream.

        This deliberately does not surface assistant text, tool input, thinking,
        or credentials through Runtime snapshots.
        """
        path = run_dir / "stream.jsonl"
        try:
            if path.stat().st_size > 2 * 1024 * 1024:
                return {"session_id": None, "initialized_model": None, "actual_models": [],
                        "actual_model_source": None, "provider_response_observed": False}
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return {"session_id": None, "initialized_model": None, "actual_models": [],
                    "actual_model_source": None, "provider_response_observed": False}
        return provider_identity(lines)

    @staticmethod
    def _read_json(path: Path):
        try: return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError): return None

    @staticmethod
    def _digest(value: Any) -> str:
        material = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8", "surrogateescape")
        return hashlib.sha256(material).hexdigest()

    @staticmethod
    def _process_presence(pid: Any, *, group: bool, label: str, identity: Any = None) -> dict[str, str]:
        if group:
            return bridge.process_identity_presence(identity, pid, label)
        if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
            return {"state": "unconfirmed", "reason": f"{label} identity is missing or invalid"}
        try:
            if group:
                os.killpg(pid, 0)
            else:
                os.kill(pid, 0)
        except ProcessLookupError:
            return {"state": "stopped", "reason": f"{label} is absent"}
        except PermissionError:
            return {"state": "unconfirmed", "reason": f"cannot inspect {label}"}
        except OSError as exc:
            return {"state": "unconfirmed", "reason": f"cannot inspect {label}: {exc}"}
        return {"state": "running", "reason": f"{label} is still present"}

    def _bound_lifecycle(self, record: dict[str, Any]) -> dict[str, Any] | None:
        run_id = record.get("run_id")
        if not isinstance(run_id, str):
            return None
        lifecycle = self._read_json(self._lifecycle_path(run_id))
        if not isinstance(lifecycle, dict) or lifecycle.get("schema_version") != 1:
            return None
        keys = ("run_id", "task_id", "revision", "cwd", "packet_sha256")
        if any(lifecycle.get(key) != record.get(key) for key in keys):
            return None
        if record.get("cli_descriptor_sha256") and lifecycle.get("cli_descriptor_sha256") != record.get("cli_descriptor_sha256"):
            return None
        if not startup_protocol.nonce_bound(lifecycle, record):
            return None
        return lifecycle

    def _recorded_cwd_observation_root(self, record: dict[str, Any], packet: dict[str, Any]) -> Path | None:
        """Return None to observe at the verified cwd, the verified root for a deleted cwd, or refuse.

        The persisted packet is never rewritten.  It must still be the one this
        record dispatched, because its cwd, owned and protected files define
        the coordinates the snapshot reproduces.  Records without a lane
        identity keep their earlier present-cwd behavior.
        """
        cwd = packet.get("cwd")
        present = isinstance(cwd, str) and os.path.lexists(cwd)
        lane = record.get("lane_identity")
        established = isinstance(lane, str) and bool(lane)
        if packet["workspace_kind"] != "git":
            if present:
                return None
            raise RuntimeError("recorded artifact root is missing; it cannot be observed from another location")
        if not established:
            if present:
                return None
            raise RuntimeError("recorded cwd is missing and the run predates worktree lanes; "
                               "recreate the directory to inspect it")
        if cwd != record.get("cwd") or packet.get("task_id") != record.get("task_id") \
                or packet.get("revision") != record.get("revision"):
            raise RuntimeError("packet.json does not match this run record; its workspace cannot be observed")
        expected_sha = record.get("packet_sha256")
        if not present and expected_sha is not None:
            dispatched_path = self._packet_path(record["run_id"])
            dispatched = self._read_json(dispatched_path)
            coordinates = ("task_id", "revision", "cwd", "owned_files", "protected_files")
            if (not isinstance(expected_sha, str) or self._file_sha256(dispatched_path) != expected_sha
                    or not isinstance(dispatched, dict)
                    or any(dispatched.get(key) != packet.get(key) for key in coordinates)
                    or dispatched.get("workspace_kind", "git") != packet["workspace_kind"]):
                raise RuntimeError("dispatched packet binding cannot be verified; a deleted cwd cannot be observed elsewhere")
        try:
            return bridge.recorded_cwd_observation_root(cwd, lane)
        except bridge.BridgeError as exc:
            raise RuntimeError(f"recorded cwd identity cannot be verified for its worktree lane: {exc}") from exc

    def _recovery_details(self, run_id: str, *, lane_available: bool | None = None) -> dict[str, Any]:
        record = self._registry().get("runs", {}).get(run_id)
        if not record:
            raise ValueError("run_id was not found")
        run_dir = self._run_dir(run_id)
        run_dir_present = os.path.lexists(run_dir)
        lifecycle = self._bound_lifecycle(record)
        protocol_bound = startup_protocol.has_startup_binding(record)
        detail: dict[str, Any] = {
            "run_id": run_id,
            "status": record.get("status"),
            "reconciliation": record.get("reconciliation"),
            "lane_available": lane_available,
            "bridge": self._process_presence(record.get("bridge_process_group"), group=True,
                                             label="recorded bridge process group", identity=record.get("bridge_identity")),
            "run_dir_present": run_dir_present,
            "lifecycle_phase": lifecycle.get("phase") if isinstance(lifecycle, dict) else None,
            "bridge_logs": self._bridge_log_evidence(run_id),
        }
        state = self._read_json(run_dir / "state.json") or {}
        # State is written by bridge before its sole Claude Popen.  Prefer it
        # when registry was persisted before Runtime learned the outer PID.
        bridge_pid = state.get("pid") if isinstance(state, dict) else None
        if isinstance(bridge_pid, int):
            detail["bridge"] = self._process_presence(bridge_pid, group=True, label="bridge process group",
                                                      identity=(lifecycle.get("bridge_identity") if isinstance(lifecycle, dict) else None)
                                                               or record.get("bridge_identity"))
        child = self._read_json(run_dir / "child.json") or {}
        child_group = child.get("process_group") if isinstance(child, dict) else None
        if isinstance(child_group, int):
            detail["child"] = self._process_presence(child_group, group=True, label="recorded Claude child process group",
                                                     identity=child.get("identity") if isinstance(child, dict) else None)
        elif (isinstance(lifecycle, dict) and lifecycle.get("phase") == "pre_dispatch"
              and lifecycle.get("child_started") is False):
            detail["child"] = {"state": "stopped", "reason": "bound pre-dispatch lifecycle proves no Claude child was launched"}
        elif isinstance(lifecycle, dict) and lifecycle.get("child_started") is True:
            detail["child"] = self._process_presence(lifecycle.get("child_process_group"), group=True,
                                                     label="lifecycle Claude child process group",
                                                     identity=lifecycle.get("child_identity"))
        elif not run_dir_present and startup_protocol.bound_pre_spawn(lifecycle, record):
            # A protocol bridge replaces this record before it can create the
            # run directory or publish a launch intent.
            detail["child"] = {"state": "stopped",
                               "reason": "bound runtime pre-spawn record proves the bridge never took over, so no Claude child was launched"}
        else:
            detail["child"] = {"state": "unconfirmed",
                               "reason": "Claude child launch is unconfirmed; no bound pre-dispatch proof or durable child identity exists"}
        packet = self._read_json(run_dir / "packet.json")
        dispatched_fallback = (not run_dir_present and isinstance(record.get("startup_nonce"), str)
                               and isinstance(record.get("packet_sha256"), str))
        if dispatched_fallback:
            # Only a never-created execution directory may substitute the
            # strictly bound outer packet; an existing one keeps its own copy.
            packet = self._packet_binding(record, require_run_copy=False)
        packet_mismatch = False
        if protocol_bound and run_dir_present and isinstance(packet, dict):
            # An existing execution copy must match the dispatched inputs in
            # full; matching only task/cwd coordinates is not enough.
            packet = self._packet_binding(record, require_run_copy=True)
            packet_mismatch = packet is None
        workspace: dict[str, Any] = {"state": "unconfirmed"}
        if not isinstance(packet, dict):
            workspace["reason"] = ("execution packet or CLI binding cannot be verified" if packet_mismatch else
                                   "dispatched packet binding cannot be verified" if dispatched_fallback
                                   else "packet.json is missing or malformed")
        else:
            try:
                # bridge owns both Git and declared-artifact snapshot semantics.
                # Old persisted packets predate workspace_kind, whose historical
                # meaning was Git, so preserve that recovery path explicitly.
                packet = dict(packet)
                packet.setdefault("workspace_kind", "git")
                observed_root = self._recorded_cwd_observation_root(record, packet)
                snapshot = bridge.workspace_snapshot(packet, worktree_root=observed_root)
                digest = snapshot.get("workspace_digest")
                workspace = {"state": "observed", "digest": digest if isinstance(digest, str) else self._digest(snapshot),
                             "kind": snapshot.get("kind", packet["workspace_kind"])}
                if observed_root is not None:
                    workspace.update(observed_at="established_worktree_root", observed_root=str(observed_root),
                                     recorded_cwd=packet["cwd"], recorded_cwd_present=False)
                if dispatched_fallback:
                    workspace["packet_source"] = "dispatched_packet"
                recorded = self._read_json(run_dir / "workspace_after.json")
                if not isinstance(recorded, dict):
                    recorded = self._read_json(run_dir / "git_after.json")
                if isinstance(recorded, dict):
                    recorded_digest = recorded.get("workspace_digest")
                    workspace["recorded_after_digest"] = recorded_digest if isinstance(recorded_digest, str) else self._digest(recorded)
                    workspace["matches_recorded_after"] = snapshot == recorded
            except Exception as exc:
                workspace["reason"] = f"cannot obtain current workspace evidence: {exc}"
        detail["workspace"] = workspace
        blocking: list[str] = []
        if record.get("status") != "unknown":
            blocking.append("run is not unknown")
        if self._has_reconciliation(record):
            blocking.append("unknown run already has a confirmed reconciliation")
        if protocol_bound and lifecycle is None:
            blocking.append("startup lifecycle binding is missing or invalid")
        if lane_available is not True:
            blocking.append("cwd lane is not exclusively available")
        if detail["bridge"]["state"] != "stopped":
            blocking.append(detail["bridge"]["reason"])
        if detail["child"]["state"] != "stopped":
            blocking.append(detail["child"]["reason"])
        if workspace.get("state") != "observed":
            blocking.append(str(workspace.get("reason") or "current workspace evidence is unavailable"))
        detail["blocking_reasons"] = blocking
        detail["eligible"] = not blocking
        cleanup = record.get("admission_cleanup")
        if isinstance(cleanup, dict):
            detail["admission_cleanup"] = cleanup
            if record.get("status") in SETTLED and cleanup.get("state") == "pending":
                detail["admission_cleanup_note"] = (
                    "the settled outcome is kept; this run's own lane-marker cleanup is retried at the next dispatch "
                    "for this worktree or a Runtime restart, under the same trusted-terminal and stopped-process proof")
        return detail

    def inspect_recovery(self, run_id: str) -> dict[str, Any]:
        """Inspect an unknown run without changing it or releasing any marker."""
        with self._guard:
            record = self._registry().get("runs", {}).get(run_id)
            if not record:
                raise ValueError("run_id was not found")
            try:
                held = self._hold_record_lanes(record)
            except RuntimeError:
                return self._recovery_details(run_id, lane_available=False)
            try:
                return self._recovery_details(run_id, lane_available=True)
            finally:
                self._release_lanes(held)

    def reconcile(self, run_id: str, reason: str, evidence: list[str], expected_workspace_digest: str | None = None) -> dict[str, Any]:
        """Record a manual unknown-run reconciliation; never revive the old run."""
        if not isinstance(reason, str) or not reason.strip() or not isinstance(evidence, list) or not evidence or not all(isinstance(item, str) and item.strip() for item in evidence):
            raise ValueError("reason and non-empty evidence strings are required")
        if (expected_workspace_digest is not None and (not isinstance(expected_workspace_digest, str)
                                                       or len(expected_workspace_digest) != 64
                                                       or any(char not in "0123456789abcdef" for char in expected_workspace_digest))):
            raise ValueError("expected_workspace_digest must be a lowercase SHA-256 when provided")
        with self._guard:
            record = self._registry().get("runs", {}).get(run_id)
            if not record:
                raise ValueError("run_id was not found")
            held = self._hold_record_lanes(record)
            try:
                detail = self._recovery_details(run_id, lane_available=True)
                actual_digest = detail["workspace"].get("digest")
                if expected_workspace_digest is None:
                    raise ValueError("expected_workspace_digest from inspect_recovery is required")
                current = self._registry().get("runs", {}).get(run_id)
                if not current:
                    raise ValueError("run_id was not found")
                reconciliation = current.get("reconciliation")
                if self._has_reconciliation(current):
                    # The reconciliation record is immutable.  A retry may
                    # only finish a failed marker unlink when it proves the
                    # same workspace fact and the two process groups remain
                    # absent while this CWD lane is exclusively held.
                    recorded_digest = reconciliation.get("workspace_digest") if isinstance(reconciliation, dict) else None
                    retry_blocking = [item for item in detail["blocking_reasons"]
                                      if item != "unknown run already has a confirmed reconciliation"]
                    if retry_blocking:
                        raise RuntimeError("unknown run cannot finish reconciliation cleanup: " + "; ".join(retry_blocking))
                    if not isinstance(recorded_digest, str) or expected_workspace_digest != recorded_digest:
                        raise RuntimeError("expected_workspace_digest does not match the recorded reconciliation")
                    if actual_digest != recorded_digest:
                        raise RuntimeError("workspace changed since recorded reconciliation; marker was not cleared")
                    if reconciliation.get("run_dir_present") is False:
                        if self._read_json(self._recovery_receipt_path(run_id)) != reconciliation:
                            raise RuntimeError("persisted recovery receipt does not match the recorded reconciliation")
                    self._require_matching_unknown_marker(current["cwd"], run_id)
                    self._clear_matching_unknown_marker(current["cwd"], run_id)
                    return self.snapshot(run_id)
                if not detail["eligible"]:
                    raise RuntimeError("unknown run cannot be reconciled: " + "; ".join(detail["blocking_reasons"]))
                if actual_digest != expected_workspace_digest:
                    raise RuntimeError("workspace changed since inspection; inspect again before reconciliation")
                self._require_matching_unknown_marker(current["cwd"], run_id)
                value = {"outcome": "confirmed_stopped", "reason": reason, "evidence": list(evidence),
                         "workspace_digest": actual_digest, "recorded_at": time.time(),
                         "note": "manual recovery fact; old unknown receipt remains terminal and is not accepted"}
                run_dir = self._run_dir(run_id)
                run_dir_present = detail.get("run_dir_present") is not False
                receipt_path = run_dir / "reconciliation.json"
                if not run_dir_present:
                    # Creating the execution directory here would fabricate
                    # run evidence and disable the outer-packet fallback that a
                    # marker-cleanup retry depends on.
                    receipt_path = self._recovery_receipt_path(run_id)
                    value.update(run_id=run_id, run_dir_present=False, receipt_file=str(receipt_path),
                                 packet_sha256=current.get("packet_sha256"), startup_nonce=current.get("startup_nonce"),
                                 lifecycle_phase=detail.get("lifecycle_phase"),
                                 lifecycle_sha256=self._file_sha256(self._lifecycle_path(run_id)),
                                 bridge_logs=detail.get("bridge_logs"))
                def persist(data):
                    current = data.get("runs", {}).get(run_id)
                    if not current or not self._unreconciled_unknown(current):
                        raise RuntimeError("unknown run is no longer eligible for reconciliation")
                    persisted = value
                    if not run_dir_present:
                        if os.path.lexists(run_dir):
                            raise RuntimeError("execution run directory appeared during reconciliation; inspect again")
                        receipt_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                        if os.path.lexists(receipt_path):
                            # A crash can commit this receipt but not registry.json.
                            # Adopt only that same fact; never replace it with a retry.
                            existing = self._read_json(receipt_path)
                            keys = ("run_id", "outcome", "run_dir_present", "receipt_file", "packet_sha256",
                                    "startup_nonce", "workspace_digest", "lifecycle_phase", "lifecycle_sha256")
                            if (not isinstance(existing, dict)
                                    or any(existing.get(key) != value.get(key) for key in keys)
                                    or not isinstance(existing.get("reason"), str) or not existing["reason"].strip()
                                    or not isinstance(existing.get("evidence"), list) or not existing["evidence"]
                                    or not all(isinstance(item, str) and item.strip() for item in existing["evidence"])
                                    or not isinstance(existing.get("recorded_at"), (int, float))):
                                raise RuntimeError("existing recovery receipt does not match this recovery fact; it was not overwritten")
                            persisted = existing
                        else:
                            bridge.dump(receipt_path, persisted)
                    else:
                        bridge.dump(receipt_path, persisted)
                    current["reconciliation"] = persisted
                    current["updated_at"] = time.time()
                    current["summary"] = "unknown run manually reconciled as stopped; a fresh higher revision may be dispatched"
                self._update(persist)
                # Re-read after persisting: an unlink failure leaves an
                # immutable recovery fact and can be retried only through the
                # guarded branch above, never by reviving this unknown run.
                self._clear_matching_unknown_marker(record["cwd"], run_id)
                if run_dir_present:
                    append(run_dir, "reconciliation", "unknown run manually confirmed stopped", status="unknown")
                return self.snapshot(run_id)
            finally:
                self._release_lanes(held)

    def snapshot(self, run_id: str) -> dict:
        self._run_dir(run_id)
        record = self._registry().get("runs", {}).get(run_id)
        if not record: raise ValueError("run_id was not found")
        # A different live Runtime holds the lane lock; never reinterpret its active
        # record as an orphan merely because this object lacks its Popen handle.
        if run_id in self._workers:
            self._refresh(run_id, owned=True)
        elif record.get("status") in ACTIVE or self._unreconciled_unknown(record):
            self._sync_unowned(run_id)
        # Terminal records are immutable summaries written by _watch.  Reading
        # them must not rewrite registry.json merely to redraw history/UI.
        record = self._registry()["runs"][run_id]
        copy = dict(record); copy["elapsed_seconds"] = max(0, (copy.get("ended_at") or time.time()) - copy["started_at"])
        # Execution visibility is evidence, not an inference from a run ID or
        # a successful preflight. A launch-intent crash stays indeterminate.
        lifecycle = self._read_json(self._lifecycle_path(run_id)) or {}
        bound = (all(lifecycle.get(key) == record.get(key) for key in
                     ("run_id", "task_id", "revision", "cwd", "packet_sha256", "cli_descriptor_sha256"))
                 and startup_protocol.nonce_bound(lifecycle, record)
                 and lifecycle.get("phase") != startup_protocol.PRE_SPAWN_PHASE)
        copy["claude_started"] = lifecycle.get("child_started") if bound else None
        if copy.get("session_id") or copy.get("provider_response_observed"):
            copy["claude_started"] = True
        copy["execution_evidence"] = ("provider_event" if copy.get("provider_response_observed")
                                      else "bridge_lifecycle" if bound else "unconfirmed")
        run_dir = self._run_dir(run_id)
        copy["workspace_changes"] = workspace_changes(None, None)
        copy["changed_files"] = None
        if run_dir.exists():
            # Terminal display may show newly appended public events (for
            # example a coordinator decision), but that observation must not
            # rewrite the durable run registry.
            event_count, last_event = statistics(run_dir)
            copy["events_count"] = event_count
            copy["last_meaningful_event"] = latest_meaningful(run_dir)
            if last_event:
                copy["last_activity_at"] = last_event.get("received_at", copy.get("last_activity_at"))
                copy["last_event"] = last_event
            copy["receipt"] = self._read_json(run_dir / "receipt.json")
            copy["result"] = self._read_json(run_dir / "result.json")
            copy["workspace_changes"] = workspace_changes(
                self._read_json(run_dir / "workspace_before.json") or self._read_json(run_dir / "git_before.json"),
                self._read_json(run_dir / "workspace_after.json") or self._read_json(run_dir / "git_after.json"))
            copy["changed_files"] = copy["workspace_changes"]["changed_files"]
        return copy

    def list_runs(self, limit: int | None = None, offset: int = 0) -> list[dict]:
        """Return immutable run summaries, optionally paged without refreshing history."""
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ValueError("offset must be a non-negative integer")
        if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int) or limit < 1):
            raise ValueError("limit must be a positive integer when provided")
        now = time.time()
        runs = []
        for record in self._registry().get("runs", {}).values():
            copy = dict(record)
            if "workspace_changes" not in copy:
                # Legacy registry paths meant ending dirtiness, not this run's
                # changes. Defer reconstruction to a specific snapshot request.
                copy["workspace_changes"] = workspace_changes(None, None)
                copy["changed_files"] = None
            copy["elapsed_seconds"] = max(0, (copy.get("ended_at") or now) - copy["started_at"])
            runs.append(copy)
        ordered = sorted(runs, key=lambda item: item["started_at"], reverse=True)
        return ordered[offset:] if limit is None else ordered[offset:offset + limit]

    def events(self, run_id: str, after: int = 0, limit: int = 100) -> dict:
        page, cursor, more = read(self._run_dir(run_id), after, limit)
        return {"events": page, "next_cursor": cursor, "has_more": more}

    def wait(self, run_id: str, after: int = 0, timeout: float = 25) -> dict:
        if timeout < 0 or timeout > 60: raise ValueError("timeout must be 0..60 seconds")
        deadline = time.monotonic() + timeout
        pause = .10
        while True:
            if latest(self._run_dir(run_id)) > after:
                answer = self.events(run_id, after, 100)
                answer["snapshot"] = self.snapshot(run_id)
                return answer
            record = self._registry().get("runs", {}).get(run_id)
            if not record:
                raise ValueError("run_id was not found")
            if record.get("status") in FINAL or time.monotonic() >= deadline:
                answer = self.events(run_id, after, 100)
                snap = self.snapshot(run_id)
                answer["snapshot"] = snap
                return answer
            time.sleep(min(pause, max(0, deadline - time.monotonic())))
            pause = min(.75, pause * 2)

    def cancel(self, run_id: str, reason: str) -> dict:
        if not isinstance(reason, str) or not reason.strip(): raise ValueError("reason is required")
        if run_id not in self._workers:
            self._sync_unowned(run_id)
        record = self._registry().get("runs", {}).get(run_id)
        if not record:
            raise ValueError("run_id was not found")
        if record.get("status") not in ACTIVE:
            raise RuntimeError("run is terminal or unknown; cancellation requires a live bridge")
        run_dir = self._run_dir(run_id)
        request = self.state_root / "cancel-requests" / f"{run_id}.json"
        request.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        bridge.dump(request, {"reason": reason, "requested_at": time.time(), "requested_by": self.owner_id})
        deadline = time.monotonic() + 3
        while not run_dir.exists() and time.monotonic() < deadline:
            time.sleep(.05)
        if not run_dir.exists():
            def mark_queued(data):
                rec = data.get("runs", {}).get(run_id)
                if not rec or rec.get("status") in FINAL:
                    return _NO_REGISTRY_CHANGE
                requested_at = time.time()
                rec.update(status="cancelling", phase="cancelling", updated_at=requested_at,
                           cancel_requested_by=self.owner_id, cancel_requested_at=requested_at,
                           summary="cancellation queued before bridge preflight")
            self._update(mark_queued)
            return self.snapshot(run_id)
        bridge.dump(run_dir / "cancel.json", {"reason": reason, "requested_at": time.time(), "requested_by": self.owner_id})
        append(run_dir, "cancelling", "cancellation marker recorded", status="cancelling")
        def mark_cancelling(data):
            rec = data.get("runs", {}).get(run_id)
            if rec:
                request_time = time.time()
                rec.update(updated_at=request_time, cancel_requested_by=self.owner_id, cancel_requested_at=request_time)
                if rec.get("status") not in FINAL:
                    rec.update(status="cancelling", phase="cancelling")
        self._update(mark_cancelling)
        return self.snapshot(run_id)

    def record_decision(self, run_id: str, decision: str, reason: str, evidence: list[str],
                        resolution: str | None = None, completion_summary: str | None = None,
                        finding_decisions: list[dict] | None = None) -> dict:
        if decision not in {"accepted", "returned"}: raise ValueError("decision must be accepted or returned")
        if not isinstance(reason, str) or not reason.strip() or not isinstance(evidence, list) or not evidence or not all(isinstance(x, str) and x.strip() for x in evidence):
            raise ValueError("reason and evidence strings are required")
        if resolution is not None and (decision != "returned" or resolution not in {"revision_requested", "completed_by_codex"}):
            raise ValueError("resolution is only valid for returned: revision_requested or completed_by_codex")
        if resolution == "completed_by_codex":
            if not isinstance(completion_summary, str) or not completion_summary.strip():
                raise ValueError("completed_by_codex requires the independently verified completion_summary")
        elif completion_summary is not None:
            raise ValueError("completion_summary requires completed_by_codex")
        value = {"decision": decision, "reason": reason, "evidence": evidence, "recorded_at": time.time(), "note": "coordinator record; not automatic acceptance"}
        if decision == "returned":
            value["resolution"] = resolution or "revision_requested"
        if completion_summary is not None:
            value["completion_summary"] = completion_summary.strip()
        run_dir = self._run_dir(run_id)
        def write_if_current(data):
            rec = data.get("runs", {}).get(run_id)
            if not rec or rec.get("status") != "reported" or rec.get("superseded_by"):
                raise RuntimeError("decision requires a non-superseded reported run")
            reported = self._read_json(run_dir / "result.json") or {}
            findings = (reported.get("structured") or {}).get("findings", [])
            if findings or finding_decisions is not None:
                try:
                    value["finding_decisions"] = validate_finding_decisions(findings, finding_decisions)
                except ValueError as exc:
                    if rec.get("review_mode") is not None or resolution != "completed_by_codex":
                        raise
                    value["legacy_findings_unparsed"] = findings
                    value["finding_decisions_unavailable"] = str(exc)
            history = list(rec.get("decision_history") or [])
            current = rec.get("decision")
            if isinstance(current, dict) and (not history or history[-1] != current):
                history.append(current)
            history.append(value)
            bridge.dump(run_dir / "decision.json", value)
            bridge.dump(run_dir / "decision-history.json", history)
            rec.update(decision=value, decision_history=history, updated_at=time.time())
        self._update(write_if_current)
        append(run_dir, "decision", "Codex completed the task; original report returned" if resolution == "completed_by_codex" else "coordinator decision recorded", status=decision)
        return self.snapshot(run_id)

    def cleanup_review(self, run_id: str) -> dict:
        """Dispose a reviewed copy under both lane and registry ownership."""
        run_dir = self._run_dir(run_id)
        record = self.snapshot(run_id)
        lane = record.get("lane_identity") or bridge.lane_identity(Path(record["cwd"]))
        outcome = {}
        def dispose_current(data):
            current = data.get("runs", {}).get(run_id, {})
            decision = current.get("decision") or {}
            disposition = "accepted" if decision.get("decision") == "accepted" else decision.get("resolution")
            if current.get("status") != "reported" or current.get("superseded_by") or disposition not in {"accepted", "completed_by_codex"}:
                raise RuntimeError("cleanup requires a non-superseded accepted or completed_by_codex review; returned/unknown copies are retained")
            previous = self._read_json(run_dir / "review-cleanup.json")
            if previous and previous.get("state") == "removed":
                outcome.update(previous)
                return _NO_REGISTRY_CHANGE
            if not self._trusted_terminal(current):
                raise RuntimeError("cleanup cannot confirm the bound bridge/provider stopped")
            workspace = bridge.load_execution_workspace(run_dir)
            from process_family import Family
            child = self._read_json(run_dir / "child.json") or {}
            marker = child.get("family_marker")
            if not marker:
                raise RuntimeError("legacy copy has no detached-process observation; retain it for manual disposal")
            observed = Family(marker=marker).observe()
            if observed.get("live_count") or observed.get("state") == "unknown":
                raise RuntimeError("cleanup found a live or unconfirmed detached review process")
            removed = bridge.review_workspace.cleanup_review_workspace(
                Path(workspace["metadata_path"]).parent, terminal=True, explicit=True, status=disposition)
            outcome.update(state="removed" if removed else "retained", workspace_root=workspace["workspace_root"],
                           recorded_at=time.time(), process_observation=observed,
                           retained=["original report", "copy patch/status", "source snapshots", "decision history"])
            bridge.dump(run_dir / "review-cleanup.json", outcome)
            current["review_cleanup"] = outcome
        with self._guard:
            handle = self._lane_lock(lane)
            try:
                # Re-read disposition under the cross-process registry lock and
                # hold it through deletion: a concurrent return cannot lose its copy.
                self._update(dispose_current)
            finally:
                self._release_lane(lane, handle)
        return outcome

    def close(self) -> None:
        self._closing = True
        for run_id in list(self._workers):
            try: self.cancel(run_id, "runtime closing")
            except (RuntimeError, ValueError): pass
        # Allow TERM grace, direct-child reap and group confirmation, followed
        # by output collection and durable terminal evidence from the bridge.
        deadline = time.monotonic() + 3 * bridge.TERMINATION_GRACE_SECONDS + 6
        while self._workers and time.monotonic() < deadline: time.sleep(.1)
        for run_id in list(self._workers):
            record = self._registry().get("runs", {}).get(run_id, {})
            def mark_unconfirmed(data, rid=run_id):
                rec = data.get("runs", {}).get(rid)
                if not rec or rec.get("status") in FINAL:
                    return _NO_REGISTRY_CHANGE
                rec.update(status="unknown", phase="unknown", updated_at=time.time(),
                           summary="close could not confirm bridge termination; do not retry automatically")
            self._update(mark_unconfirmed)
            current = self._registry().get("runs", {}).get(run_id, {})
            if current.get("status") == "unknown" and not self._has_reconciliation(current):
                self._mark_unknown_lane(record.get("cwd", ""), run_id, "runtime close could not confirm bridge termination")
