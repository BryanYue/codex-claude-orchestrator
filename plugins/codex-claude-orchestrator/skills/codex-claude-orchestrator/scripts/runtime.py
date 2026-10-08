"""Run registry and bridge supervision for the MCP server; meant for asyncio.to_thread callers.

Each run is a bridge process in its own session.  Nothing here locks the
user's worktree: copy-profile runs write only their own copy and readonly runs
write nothing.  A run is active until its outcome.json exists.  After an MCP
restart a living bridge is simply observed; a bridge that died without an
outcome is settled as ``lost`` once its Claude process group is stopped.
"""
from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import threading
import time
import traceback
from typing import Any, Callable

import bridge
import cli_env
import copy_workspace
from events import append, latest, latest_meaningful, read, statistics
import packet as packet_schema
import process_control
from process_family import Family
import result_schema
from shared_io import dump_json as dump
import stream_parser
import trusted_git

VERDICTS = ("accepted", "accepted_with_corrections", "rejected")
NEXT_STEPS = ("done", "next_round", "codex_finishes")
STOPPED_STATES = {"confirmed", "not_launched", "stopped"}


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _title(packet: dict[str, Any]) -> str:
    text = packet["user_messages"][0]["text"] if packet["user_messages"] else (packet["brief"] or packet["task_id"])
    line = next((part.strip() for part in str(text).splitlines() if part.strip()), str(packet["task_id"]))
    return line[:120]


class Runtime:
    """Thread-safe owner of bridge processes started by this MCP server."""

    def __init__(self, state_root: Path):
        self.state_root = Path(state_root).expanduser().resolve()
        self.root = self.state_root / "v1"
        self.runs_root = self.root / "runs"
        self.runs_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.registry_path = self.root / "registry.json"
        self.lock_path = self.root / "registry.lock"
        self._guard = threading.RLock()
        self._workers: dict[str, subprocess.Popen[bytes]] = {}
        for run_id, record in self._registry()["runs"].items():
            if record.get("status") == "running":
                try:
                    self._observe_unowned(run_id)
                except Exception:
                    # One damaged run must not keep the server from starting.
                    traceback.print_exc(file=sys.stderr)

    def _registry(self) -> dict[str, Any]:
        value = _read_json(self.registry_path)
        return value if isinstance(value, dict) and isinstance(value.get("runs"), dict) else {"runs": {}}

    def _update(self, mutate: Callable[[dict[str, Any]], Any]) -> Any:
        with self.lock_path.open("a+") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            data = self._registry()
            result = mutate(data)
            dump(self.registry_path, data)
            return result

    def _record(self, run_id: str) -> dict[str, Any]:
        self.run_dir(run_id)
        record = self._registry()["runs"].get(run_id)
        if not isinstance(record, dict):
            raise ValueError("run_id was not found")
        return record

    def run_dir(self, run_id: str) -> Path:
        if not isinstance(run_id, str) or not packet_schema.RUN_ID.fullmatch(run_id):
            raise ValueError("invalid run_id")
        return self.runs_root / run_id

    def _profile(self, packet: dict[str, Any]) -> str:
        cwd = Path(packet["cwd"])
        probe = trusted_git.run(cwd, "rev-parse", "--is-inside-work-tree", text=True)
        is_git = probe.returncode == 0 and probe.stdout.strip() == "true"
        sandbox = cli_env.sandbox_available()
        if packet["kind"] == "implement" and not is_git:
            raise ValueError("implement tasks need a Git repository: they run in an independent copy and return a patch")
        if packet["kind"] == "implement" and not sandbox:
            raise ValueError("implement tasks need macOS sandbox-exec on this host: they only run in a protected copy")
        profile = packet["profile"] or ("copy" if is_git and sandbox else "readonly")
        if profile == "copy" and not is_git:
            raise ValueError("the copy profile needs a Git worktree; use profile=readonly for a plain directory")
        if profile == "copy" and not sandbox:
            raise ValueError("the copy profile needs macOS sandbox-exec on this host"
                             + ("" if packet["kind"] == "implement" else "; use profile=readonly"))
        return profile

    def start(self, raw: Any) -> dict[str, Any]:
        try:
            packet = packet_schema.normalize(raw)
        except packet_schema.PacketError as exc:
            raise ValueError(str(exc)) from exc
        profile = self._profile(packet)
        run_id = "run-" + secrets.token_urlsafe(12).replace("-", "_")
        run_dir = self.run_dir(run_id)
        with self._guard:
            plan = self._admit(packet, profile, run_id, run_dir)
            run_dir.mkdir(mode=0o700)
            dump(run_dir / "packet.json", packet)
            dump(run_dir / "plan.json", plan)
            record = {"run_id": run_id, "task_id": packet["task_id"], "kind": packet["kind"], "profile": profile,
                      "round": plan["round"], "cwd": packet["cwd"], "title": _title(packet), "model": packet["model"],
                      "effort": packet["effort"], "timeout_seconds": packet["timeout_seconds"],
                      "continue_from": packet["continue_from"], "lineage_root": plan["lineage_root"],
                      "run_dir": str(run_dir), "status": "running", "started_at": time.time(),
                      "updated_at": time.time(), "ended_at": None, "decision": None, "copy_removed": False}

            def register(data: dict[str, Any]) -> None:
                # Decided under the registry lock, so cleanup() and another server see a consistent lineage.
                active = [r["run_id"] for r in data["runs"].values()
                          if r.get("task_id") == packet["task_id"] and r.get("status") == "running"]
                if active:
                    raise RuntimeError(f"task {packet['task_id']!r} already has an active run {active[0]}; "
                                       "wait for it or cancel it first")
                if packet["continue_from"]:
                    lineage = [r for r in data["runs"].values() if r.get("lineage_root") == plan["lineage_root"]]
                    latest = max(lineage, key=lambda r: r.get("started_at") or 0)
                    if latest["run_id"] != packet["continue_from"]:
                        raise ValueError(f"continue from the latest round of this task, {latest['run_id']}; "
                                         f"{packet['continue_from']} was already continued")
                    if any(r.get("copy_removed") for r in lineage):
                        raise ValueError("the copy of that task lineage was removed; start a new run without continue_from")
                data["runs"][run_id] = record
            try:
                self._update(register)
            except BaseException:
                shutil.rmtree(run_dir, ignore_errors=True)
                raise
            log = (run_dir / "bridge.log").open("ab")
            try:
                proc = subprocess.Popen([sys.executable, str(Path(bridge.__file__).resolve()), "run", "--run-dir", str(run_dir)],
                                        cwd=str(self.state_root), stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                        start_new_session=True, env=dict(os.environ))
            except OSError as exc:
                log.close()
                bridge.finalize(run_dir, "not_started", f"the bridge could not start: {exc}")
                self._settle(run_id)
                raise RuntimeError(f"the bridge could not start: {exc}") from exc
            log.close()
            identity = process_control.identity(proc.pid) or {"pid": proc.pid, "birth": None}
            dump(run_dir / "bridge.json", identity)
            self._update(lambda data: data["runs"][run_id].update(bridge=identity))
            self._workers[run_id] = proc
            threading.Thread(target=self._watch, args=(run_id, proc), daemon=True).start()
        return self.snapshot(run_id)

    def _admit(self, packet: dict[str, Any], profile: str, run_id: str, run_dir: Path) -> dict[str, Any]:
        plan: dict[str, Any] = {"run_id": run_id, "profile": profile, "round": 1, "lineage_root": str(run_dir),
                                "state_root": str(self.state_root), "plugin_version": bridge.plugin_version(),
                                "continued": None, "resume_session": None}
        runs = self._registry()["runs"]
        if not packet["continue_from"]:
            return plan
        prior = runs.get(packet["continue_from"])
        if not isinstance(prior, dict):
            raise ValueError("continue_from run was not found")
        for key, label in (("task_id", "task_id"), ("kind", "kind"), ("profile", "profile")):
            if prior.get(key) != (packet[key] if key != "profile" else profile):
                raise ValueError(f"continue_from must keep the same {label} ({prior.get(key)!r})")
        if prior.get("status") == "running":
            raise RuntimeError("continue_from run is still active; wait for it or cancel it first")
        if not self._stopped(prior):
            raise RuntimeError("the Claude process of the continue_from run may still be running")
        if profile == "copy" and prior.get("copy_removed"):
            raise ValueError("the copy of that task lineage was removed; start a new run without continue_from")
        prior_dir = Path(prior["run_dir"])
        prior_packet = _read_json(prior_dir / "packet.json") or {}
        if not packet_schema.extends(prior_packet.get("user_messages") or [], packet["user_messages"]):
            raise ValueError("continue_from: user_messages must repeat every earlier message verbatim and only append new ones")
        if profile == "readonly" and prior_packet.get("cwd") != packet["cwd"]:
            raise ValueError("continue_from must keep the same cwd")
        outcome = _read_json(prior_dir / "outcome.json") or {}
        lineage_root = prior.get("lineage_root") or str(prior_dir)
        decided = sorted((r for r in runs.values() if r.get("lineage_root") == lineage_root and r.get("decision")),
                         key=lambda r: r.get("started_at") or 0)
        decisions = [{"run_id": r["run_id"], "round": r.get("round"), "decision": r["decision"],
                      "file": str(Path(r["run_dir"]) / "decision.json")} for r in decided]
        if profile == "copy":
            plan.update(self._carry(runs, lineage_root))
        plan.update(round=int(prior.get("round") or 1) + 1, lineage_root=lineage_root,
                    continued={"run_id": prior["run_id"], "message_count": len(prior_packet.get("user_messages") or []),
                               "report": str(prior_dir / "report.md") if (prior_dir / "report.md").is_file() else None,
                               "decisions": decisions},
                    resume_session=outcome.get("session_id") if outcome.get("run_outcome") != "not_started" else None)
        return plan

    def _watch(self, run_id: str, proc: subprocess.Popen[bytes]) -> None:
        try:
            proc.wait()
        finally:
            try:
                self._settle(run_id)
            finally:
                with self._guard:
                    self._workers.pop(run_id, None)

    def _observe_unowned(self, run_id: str) -> None:
        record = self._registry()["runs"].get(run_id) or {}
        if record.get("status") != "running" or run_id in self._workers:
            return
        if (self.run_dir(run_id) / "outcome.json").is_file():
            self._settle(run_id)
            return
        bridge_identity = record.get("bridge") or _read_json(self.run_dir(run_id) / "bridge.json")
        if bridge_identity is None:
            # Registered moments ago by another server that has not recorded its bridge yet.
            if time.time() - float(record.get("started_at") or 0) < 30:
                return
        elif process_control.presence(bridge_identity) != "stopped":
            return
        self._settle(run_id)

    def _settle(self, run_id: str) -> None:
        """Record a terminal outcome; a bridge that died without one is finalized as lost."""
        run_dir = self.run_dir(run_id)
        if not run_dir.is_dir():
            return
        with (run_dir / ".settle.lock").open("a+") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            outcome = _read_json(run_dir / "outcome.json")
            if not isinstance(outcome, dict):
                try:
                    outcome = self._finalize_lost(run_dir)
                except Exception as exc:
                    traceback.print_exc(file=sys.stderr)
                    outcome = {"run_outcome": "lost", "process_stopped": "unknown", "warnings": [],
                               "note": f"the run could not be settled: {type(exc).__name__}: {exc}"}
        summary = outcome.get("summary") or outcome.get("note") or outcome["run_outcome"]

        def settle(data: dict[str, Any]) -> None:
            record = data["runs"].get(run_id)
            if record is None or record.get("status") != "running":
                return
            record.update(status=outcome["run_outcome"], ended_at=outcome.get("ended_at") or time.time(),
                          updated_at=time.time(), summary=str(summary)[:600], claimed_status=outcome.get("claimed_status"),
                          process_stopped=outcome.get("process_stopped"),
                          warnings=[warning["code"] for warning in outcome.get("warnings") or []])
        self._update(settle)

    @staticmethod
    def _finalize_lost(run_dir: Path) -> dict[str, Any]:
        execution = _read_json(run_dir / "execution.json") or {}
        child = _read_json(run_dir / "child.json")
        stop = "not_launched"
        if isinstance(child, dict):
            stop = process_control.terminate_recorded_group(child.get("identity"))
            marker = child.get("family_marker")
            if marker and Family(marker=marker).finish().get("state") != "stopped":
                stop = "unknown"
        execution.update(lost_stop=stop, ended_at=time.time())
        dump(run_dir / "execution.json", execution)
        return bridge.finalize(run_dir, "lost", "the bridge process ended without recording an outcome")

    def _stopped(self, record: dict[str, Any]) -> bool:
        if record.get("process_stopped") in STOPPED_STATES:
            return True
        child = _read_json(Path(record["run_dir"]) / "child.json")
        if not isinstance(child, dict):
            return True
        if process_control.presence(child.get("identity")) == "running":
            return False
        observed = Family(marker=child["family_marker"]).observe() if child.get("family_marker") else {"live_count": 0}
        return observed.get("state") != "unknown" and not observed.get("live_count")

    def snapshot(self, run_id: str) -> dict[str, Any]:
        if run_id not in self._workers:
            self._observe_unowned(run_id)
        record = dict(self._record(run_id))
        run_dir = self.run_dir(run_id)
        record["active"] = record["status"] == "running"
        record["elapsed_seconds"] = round(max(0.0, (record.get("ended_at") or time.time()) - record["started_at"]), 1)
        count, last = statistics(run_dir) if (run_dir / "activity.jsonl").exists() else (0, None)
        record.update(events_count=count, last_event=last,
                      last_meaningful_event=latest_meaningful(run_dir) if count else None)
        record["outcome"] = _read_json(run_dir / "outcome.json")
        if record["active"]:
            stream = run_dir / "stream.jsonl"
            try:
                lines = stream.read_text(encoding="utf-8", errors="replace").splitlines() if stream.stat().st_size < 4_000_000 else []
            except OSError:
                lines = []
            record["live"] = stream_parser.live_identity(lines)
        history = _read_json(run_dir / "decision-history.json")
        record["decision_history_count"] = len(history) if isinstance(history, list) else 0
        return record

    def list_runs(self, limit: int = 50, offset: int = 0, task_id: str | None = None) -> list[dict[str, Any]]:
        runs = [dict(record) for record in self._registry()["runs"].values()
                if task_id is None or record.get("task_id") == task_id]
        for record in runs:
            if record.get("status") == "running" and record["run_id"] not in self._workers:
                self._observe_unowned(record["run_id"])
        current = self._registry()["runs"]
        ordered = sorted((dict(current.get(r["run_id"], r)) for r in runs), key=lambda r: r.get("started_at") or 0, reverse=True)
        return ordered[offset:offset + limit]

    def events(self, run_id: str, after: int = 0, limit: int = 100) -> dict[str, Any]:
        page, cursor, more = read(self.run_dir(run_id), after, limit)
        return {"events": page, "next_cursor": cursor, "has_more": more}

    def wait(self, run_id: str, after: int = 0, timeout: float = 25) -> dict[str, Any]:
        if not 0 <= timeout <= 60:
            raise ValueError("timeout must be 0..60 seconds")
        self._record(run_id)
        deadline = time.monotonic() + timeout
        pause = .1
        while True:
            if latest(self.run_dir(run_id)) > after:
                break
            if self.snapshot(run_id)["status"] != "running" or time.monotonic() >= deadline:
                break
            time.sleep(min(pause, max(0.0, deadline - time.monotonic())))
            pause = min(.75, pause * 2)
        answer = self.events(run_id, after, 100)
        answer["snapshot"] = self.snapshot(run_id)
        return answer

    def cancel(self, run_id: str, reason: str) -> dict[str, Any]:
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("a cancellation reason is required")
        record = self._record(run_id)
        if record["status"] != "running":
            raise RuntimeError(f"run already ended ({record['status']})")
        run_dir = self.run_dir(run_id)
        if not (run_dir / "cancel.json").exists():
            dump(run_dir / "cancel.json", {"reason": reason, "requested_at": time.time()})
            append(run_dir, "cancelling", "cancellation requested", status="cancelling", text=reason)
        return self.snapshot(run_id)

    def decide(self, run_id: str, verdict: str, next_step: str, note: str, evidence: list[str],
               item_decisions: list[dict[str, Any]] | None = None,
               protected_confirmed: list[str] | None = None, applied: bool | None = None) -> dict[str, Any]:
        if verdict not in VERDICTS:
            raise ValueError("verdict must be accepted, accepted_with_corrections or rejected")
        if next_step not in NEXT_STEPS:
            raise ValueError("next must be done, next_round or codex_finishes")
        if not isinstance(note, str) or not note.strip():
            raise ValueError("note must explain the verdict")
        if not isinstance(evidence, list) or not evidence or not all(isinstance(item, str) and item.strip() for item in evidence):
            raise ValueError("evidence must list what Codex actually checked")
        record = self._record(run_id)
        if record["status"] == "running":
            raise RuntimeError("the run is still active; wait for it or cancel it before deciding")
        if not self._stopped(record):
            raise RuntimeError("the Claude process of this run may still be running; it cannot be decided yet")
        run_dir = self.run_dir(run_id)
        outcome = _read_json(run_dir / "outcome.json") or {}
        result = _read_json(run_dir / "result.json")
        raw_items = result.get("items") if isinstance(result, dict) else None
        items: list[dict[str, Any]] = [item for item in raw_items if isinstance(item, dict)] if isinstance(raw_items, list) else []
        decisions = result_schema.item_decisions(items, item_decisions)
        raw_changes = outcome.get("changes")
        changes: dict[str, Any] = raw_changes if isinstance(raw_changes, dict) else {}
        touched = [item["path"] for item in changes.get("files") or []
                   if isinstance(item, dict) and item.get("class") == "protected"]
        confirmed = [path for path in protected_confirmed or [] if isinstance(path, str)]
        missing = sorted(set(touched) - set(confirmed))
        if missing:
            raise ValueError("protected paths changed by the patch must be confirmed in protected_confirmed: " + ", ".join(missing))
        if applied is None:
            # Omitted: a revised note or new evidence must not undo a merge recorded earlier.
            applied = bool((record.get("decision") or {}).get("applied"))
        elif not isinstance(applied, bool):
            raise ValueError("applied must be true or false")
        elif applied:
            self._check_delivery(record, changes)
        value = {"verdict": verdict, "next": next_step, "note": note.strip(), "evidence": evidence,
                 "item_decisions": decisions, "protected_confirmed": confirmed, "applied": applied,
                 "recorded_at": time.time()}
        history = _read_json(run_dir / "decision-history.json")
        history = history if isinstance(history, list) else []
        history.append(value)
        dump(run_dir / "decision.json", value)
        dump(run_dir / "decision-history.json", history)
        self._update(lambda data: data["runs"][run_id].update(decision=value, updated_at=time.time()))
        append(run_dir, "decision", f"Codex verdict: {verdict}; next: {next_step}", status=verdict)
        return self.snapshot(run_id)

    @staticmethod
    def _carry(runs: dict[str, Any], lineage_root: str) -> dict[str, Any]:
        """Pick the work a continued round must start from, from immutable per-run artifacts.

        The latest run with a delivery.patch holds everything not yet applied,
        unless Codex recorded that patch as applied.  Every path any delivery of
        this task touched is brought into later copies while the original has
        it, even when ignored: the list says what to take from the original
        before replay, so a pending deletion still finds its file.
        """
        lineage = sorted((r for r in runs.values() if r.get("lineage_root") == lineage_root),
                         key=lambda r: r.get("started_at") or 0)
        latest: dict[str, Any] | None = None
        task_paths: set[str] = set()
        for record in lineage:
            folder = Path(record["run_dir"])
            changes = (_read_json(folder / "outcome.json") or {}).get("changes")
            if isinstance(changes, dict) and (folder / "delivery.patch").is_file():
                latest = record
                task_paths.update(item["path"] for item in changes.get("delivery_files") or [])
            elif (folder / "child.json").is_file() and (folder / "context.json").is_file():
                # Claude worked in a copy whose changes were never measured; continuing would drop them.
                raise RuntimeError(f"{record['run_id']} ran in its copy but its changes could not be recorded; "
                                   f"inspect {Path(lineage_root) / 'copy'} and start a new task")
        carry = None
        if latest is not None and not (latest.get("decision") or {}).get("applied"):
            carry = {"from_run": latest["run_id"], "patch": str(Path(latest["run_dir"]) / "delivery.patch")}
        return {"carry": carry, "task_paths": sorted(task_paths)}

    def _check_delivery(self, record: dict[str, Any], changes: dict[str, Any]) -> None:
        """applied=true is recorded on the decision; only the newest delivery can have been applied."""
        if record.get("profile") != "copy" or not changes.get("end_tree"):
            raise ValueError("applied=true needs a copy-profile run that produced a delivery patch")
        later = [r for r in self._registry()["runs"].values() if r.get("lineage_root") == record.get("lineage_root")
                 and (r.get("started_at") or 0) > (record.get("started_at") or 0)]
        if any(r.get("status") == "running" or ((_read_json(Path(r["run_dir"]) / "outcome.json") or {}).get("changes"))
               for r in later):
            raise ValueError("only the latest round that changed the copy can be recorded as applied")

    def cleanup(self, run_id: str) -> dict[str, Any]:
        """Remove a lineage's copy after its latest run is decided and every run in it has stopped.

        The claim records this process as the cleaner.  A later call takes over a
        removal whose cleaner has exited or released it, and finishes it; once
        deletion has begun the copy never becomes usable again.
        """
        record = self._record(run_id)
        if record.get("profile") != "copy":
            raise ValueError("only copy-profile runs have a copy to remove")
        lineage = str(record["lineage_root"])
        me = process_control.identity(os.getpid())

        def claim(data: dict[str, Any]) -> str:
            # Checked and claimed under the registry lock that start() also takes for a continuation.
            runs = [r for r in data["runs"].values() if r.get("lineage_root") == lineage]
            if any(r.get("copy_state") == "removed" for r in runs):
                return "already_removed"
            removing = [r for r in runs if r.get("copy_state") == "removing"]
            if removing:
                cleaner = removing[0].get("copy_cleaner")
                if cleaner is not None and process_control.presence(cleaner) != "stopped":
                    raise RuntimeError("this copy is being removed by another live cleanup; try again when it ends")
                for r in runs:
                    r.update(copy_state="removing", copy_removed=True, copy_cleaner=me)
                return "takeover"
            latest = max(runs, key=lambda r: r.get("started_at") or 0)
            if latest["run_id"] != run_id:
                raise RuntimeError(f"{latest['run_id']} continues this copy; clean up from the latest run")
            if any(r.get("status") == "running" or not self._stopped(r) for r in runs):
                raise RuntimeError("a run of this copy may still be active")
            if not data["runs"][run_id].get("decision"):
                raise RuntimeError("decide the run before removing its copy")
            for r in runs:
                r.update(copy_state="removing", copy_removed=True, copy_cleaner=me)
            return "claimed"

        def mark(**fields: Any) -> None:
            self._update(lambda data: [r.update(fields) for r in data["runs"].values() if r.get("lineage_root") == lineage])

        state = self._update(claim)
        if state == "already_removed":
            return {"state": "already_removed", "lineage_root": lineage}
        if state == "claimed":
            try:
                copy_workspace.load_copy(Path(lineage))
            except BaseException:
                # Nothing was deleted yet, so the copy stays usable.
                mark(copy_state=None, copy_removed=False, copy_cleaner=None)
                raise
        try:
            copy_workspace.discard_copy(Path(lineage))
        except BaseException:
            # Possibly half deleted: keep it unusable and let the next cleanup take over.
            mark(copy_cleaner=None)
            raise
        outcome = {"state": "removed", "lineage_root": lineage, "removed_at": time.time(), "took_over": state == "takeover",
                   "kept": ["patches", "reports", "brief", "stream", "decisions"]}
        dump(Path(lineage) / "cleanup.json", outcome)
        mark(copy_state="removed", copy_cleaner=None)
        return outcome

    def close(self) -> None:
        """Leave bridges running: a later server observes them by their recorded identity."""
        with self._guard:
            self._workers.clear()
