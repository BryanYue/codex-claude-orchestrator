#!/usr/bin/env python3
"""Run one delegated Claude task under supervision and record what happened.

The bridge freezes the inputs, prepares the workspace, renders the brief,
launches the local Claude CLI in its own process group and enforces the
deadline and cancellation.  ``outcome.json`` is written last.  The bridge
reports facts and warnings; whether the work is accepted is Codex's decision.
"""
from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import signal
import subprocess
import sys
import time
import traceback
from typing import Any, Callable
import uuid

PLUGIN_ROOT = Path(__file__).resolve().parents[3]
if str(PLUGIN_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT / "scripts"))
from shared_io import dump_json as dump, load_json as load, read_regular
import trusted_git
from executable_locator import cli_environment, locate_claude

import brief as brief_renderer
import cli_env
from control_store import ControlStore
import copy_workspace
import duplex
from events import append as append_activity
import instruction_layer
import process_control
from process_family import Family, ENVIRONMENT_KEY as FAMILY_KEY
import result_schema
from run_process import ProcessStream
import stream_parser
import usage as usage_report

OUTCOMES = ("ok", "timeout", "cancelled", "crashed", "not_started", "lost")
PRINT_BG_WAIT_CEILING = "CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS"
INPUT_FILE_LIMIT = 2000
INPUT_BYTE_LIMIT = 64 * 1024 * 1024
REPORT_LIMIT = 8 * 1024 * 1024
RESULT_LIMIT = 1024 * 1024
SUMMARY_LIMIT = 600


def plugin_version() -> str:
    try:
        return str(load(PLUGIN_ROOT / ".codex-plugin" / "plugin.json")["version"])
    except (OSError, ValueError, KeyError):
        return "unknown"


def activity(run_dir: Path, kind: str, summary: str, **fields: Any) -> None:
    append_activity(run_dir, kind, summary, **fields)


def _read_json(path: Path) -> Any:
    try:
        return load(path)
    except (OSError, ValueError):
        return None


def cancel_requested(run_dir: Path) -> bool:
    return (run_dir / "cancel.json").exists()


def control_store(state_root: str | Path) -> ControlStore:
    return ControlStore(Path(state_root) / "v2" / "control.sqlite3")


def user_line(text: str) -> bytes:
    return json.dumps({"type": "user", "message": {"role": "user", "content": text}}, ensure_ascii=False).encode("utf-8") + b"\n"


def freeze_inputs(inputs: list[str], destination: Path) -> tuple[list[dict[str, Any]], list[str]]:
    """Copy each input byte for byte; symlinks, sensitive names and overflow are listed, never copied."""
    destination.mkdir(mode=0o700)
    entries: list[dict[str, Any]] = []
    skipped: list[str] = []
    budget = {"files": INPUT_FILE_LIMIT, "bytes": INPUT_BYTE_LIMIT}

    def take(source: Path, target: Path) -> dict[str, Any] | None:
        size = source.stat().st_size
        if budget["files"] < 1 or size > budget["bytes"]:
            skipped.append(f"{source} (input size limit)")
            return None
        budget["files"] -= 1
        budget["bytes"] -= size
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target, follow_symlinks=False)
        target.chmod(0o444)
        return {"bytes": size, "sha256": hashlib.sha256(target.read_bytes()).hexdigest()}

    for index, raw in enumerate(inputs, start=1):
        source = Path(raw)
        target = destination / f"{index:02d}-{source.name or 'input'}"
        if copy_workspace.sensitive_path(source.name):
            skipped.append(f"{source} (sensitive name)")
            continue
        if source.is_file():
            copied = take(source, target)
            if copied:
                entries.append({"original": str(source), "copy": str(target), "kind": "file", **copied})
            continue
        files = 0
        total = 0
        for directory, dirs, names in os.walk(source, followlinks=False):
            relative_dir = Path(directory).relative_to(source)
            for name in sorted(dirs):
                if copy_workspace.sensitive_path(name) or name == ".git" or (Path(directory) / name).is_symlink():
                    skipped.append(str(Path(directory) / name))
            dirs[:] = sorted(name for name in dirs if not copy_workspace.sensitive_path(name) and name != ".git"
                             and not (Path(directory) / name).is_symlink())
            for name in sorted(names):
                path = Path(directory) / name
                if copy_workspace.sensitive_path(name) or path.is_symlink() or not path.is_file():
                    skipped.append(str(path))
                    continue
                copied = take(path, target / relative_dir / name)
                if copied:
                    files += 1
                    total += copied["bytes"]
        entries.append({"original": str(source), "copy": str(target), "kind": "directory", "files": files, "bytes": total})
    return entries, skipped


def worktree_state(root: Path) -> dict[str, Any]:
    """HEAD plus every non-ignored changed path with its content, link target and exec bit.

    A continuation compares this to decide whether its copy must be rebuilt from the original.
    """
    head = trusted_git.run(root, "rev-parse", "--verify", "-q", "HEAD", text=True).stdout.strip() or None
    status = trusted_git.run(root, "status", "--porcelain=v1", "-z", "-uall")
    entries: dict[str, list[str]] = {}
    fields = status.stdout.split(b"\0") if status.returncode == 0 else []
    index = 0
    while index < len(fields) - 1:
        field = fields[index]
        if len(field) < 4:
            break
        name = field[3:].decode("utf-8", "surrogateescape")
        if b"R" in field[:2] or b"C" in field[:2]:
            index += 1
        path = root / name
        try:
            if path.is_symlink():
                digest = "link:" + os.readlink(path)
            elif path.is_file():
                digest = hashlib.sha256(path.read_bytes()).hexdigest() + (":x" if os.access(path, os.X_OK) else "")
            else:
                digest = "missing" if not os.path.lexists(path) else "other"
        except OSError:
            digest = "unreadable"
        entries[name] = [field[:2].decode("ascii", "replace"), digest]
        index += 1
    return {"head": head, "entries": entries, "status_ok": status.returncode == 0}


def state_changes(before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    changed = sorted(path for path in set(before["entries"]) | set(after["entries"])
                     if before["entries"].get(path) != after["entries"].get(path))
    return (["HEAD"] if before["head"] != after["head"] else []) + changed


def matches(path: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatchcase(path, pattern) or path.startswith(pattern.rstrip("/") + "/") for pattern in patterns)


def source_root_of(cwd: str) -> Path:
    top = trusted_git.run(Path(cwd), "rev-parse", "--show-toplevel", text=True)
    if top.returncode:
        raise copy_workspace.CopyWorkspaceError(f"not a Git worktree: {top.stderr.strip()}")
    return Path(top.stdout.rstrip("\n")).resolve()


def readonly_sources(cwd: str) -> list[str]:
    """What a readonly run must not write: the worktree (or the plain directory) and its Git metadata."""
    top = trusted_git.run(Path(cwd), "rev-parse", "--path-format=absolute", "--show-toplevel", "--git-dir",
                          "--git-common-dir", text=True)
    if top.returncode:
        return [str(Path(cwd).resolve())]
    return sorted({str(Path(line).resolve()) for line in top.stdout.splitlines() if line.strip()} | {str(Path(cwd).resolve())})


def prepare_workspace(packet: dict[str, Any], plan: dict[str, Any], run_dir: Path) -> dict[str, Any]:
    """Build this round's copy from scratch: the original's current files plus work not yet delivered.

    The copy is disposable and rebuilt every round, so nothing about it has to
    survive a crash; what carries over between rounds is the immutable
    delivery.patch of an earlier run, chosen by Runtime.
    """
    if plan["profile"] == "readonly":
        channel = run_dir / "channel"
        channel.mkdir(mode=0o700)
        return {"profile": "readonly", "execution_cwd": packet["cwd"], "source_root": packet["cwd"],
                "protected_source": readonly_sources(packet["cwd"]), "channel": str(channel),
                "notes": str(channel / "notes.jsonl")}
    lineage = Path(plan["lineage_root"])
    source = source_root_of(packet["cwd"])
    original = worktree_state(source)
    carry = plan.get("carry") or {}
    carried = Path(carry["patch"]).read_bytes() if carry.get("patch") else b""
    (run_dir / "carried.patch").write_bytes(carried)
    mode = "none"
    if carried.strip():
        fits, problem = copy_workspace.patch_applies(source, carried)
        if fits:
            mode = "replayed"
        elif copy_workspace.patch_applies(source, carried, reverse=True)[0]:
            mode = "already_in_original"
        else:
            raise copy_workspace.CopyWorkspaceError(
                f"the undelivered work of {carry['from_run']} no longer applies to the original: {problem[:800]}. "
                "If its delivery.patch was applied to the original and then corrected, record that with "
                "claude_decide(..., applied=true) and continue again; otherwise reconcile the files first")
    if os.path.lexists(lineage / copy_workspace.COPY_NAME):
        copy_workspace.discard_copy(lineage)
    (lineage / copy_workspace.METADATA_NAME).unlink(missing_ok=True)
    metadata = copy_workspace.prepare_copy(packet["cwd"], lineage, plan.get("task_paths") or [])
    store, index = run_dir / "baseline.git", run_dir / "baseline.index"
    base_tree = copy_workspace.record_tree(store, metadata, index, track=metadata["included_task_files"])
    carried_paths: list[str] = []
    if mode == "replayed":
        copy_workspace.apply_patch(metadata, carried)
        carried_paths = copy_workspace.patch_paths(Path(metadata["workspace_root"]), carried)
    start_tree = copy_workspace.record_tree(store, metadata, index, track=carried_paths)
    outbox = Path(metadata["outbox"]) / plan["run_id"]
    outbox.mkdir()
    workflow = copy_workspace.install_workflow_template(metadata) if packet["kind"] == "analyze" else None
    return {"profile": "copy", "execution_cwd": metadata["cwd"], "source_root": metadata["source_root"],
            "copy": metadata, "outbox": str(outbox), "notes": str(outbox / "notes.jsonl"), "workflow": workflow, "store": str(store), "index": str(index),
            "base_tree": base_tree, "start_tree": start_tree, "original_before": original,
            "carried": {"from_run": carry.get("from_run"), "mode": mode, "files": len(carried_paths),
                        "now_ignored": copy_workspace.ignored_paths(metadata, carried_paths)}}


def boundary_problem(context: dict[str, Any], plan: dict[str, Any], exc: Exception) -> str:
    """Why the OS write boundary cannot be built for this layout, and what the user can change; nothing was launched."""
    state_root = Path(plan["state_root"]).resolve()
    if context.get("profile") == "readonly":
        for raw in context.get("protected_source") or []:
            source = Path(raw).resolve()
            if state_root == source or source in state_root.parents:
                return (f"the readonly directory {source} contains the orchestrator state directory {state_root}, where "
                        "this run must write its own records, so the readonly write boundary cannot protect the directory "
                        f"without blocking the run. Analyze a narrower directory that does not contain {state_root}, or start "
                        f"the plugin with CLAUDE_ORCHESTRATOR_STATE_DIR outside {source}. Claude was not launched ({exc})")
    return f"the OS write boundary for this run could not be built: {exc}. Claude was not launched"


def build_command(packet: dict[str, Any], context: dict[str, Any], cli_path: str, run_dir: Path,
                  session: tuple[str, str]) -> list[str]:
    """Native tool set and permission mode; isolation comes from the OS write boundary, not a tool list."""
    command = [cli_path, "-p", "--model", packet["model"], "--effort", packet["effort"],
               "--input-format", "stream-json", "--output-format", "stream-json", "--verbose", "--replay-user-messages",
               "--permission-mode", "auto", "--permission-prompt-tool", "stdio", "--tools", "default"]
    if context["profile"] != "copy":
        command += ["--json-schema", json.dumps(result_schema.json_schema(with_report=True), ensure_ascii=False)]
    if packet["max_budget_usd"] is not None:
        command += ["--max-budget-usd", repr(packet["max_budget_usd"])]
    mode, value = session
    command += ["--resume", value] if mode == "resume" else ["--session-id", value]
    state_root = Path(context["state_root"])
    protected = [state_root, PLUGIN_ROOT, Path(sys.prefix).resolve(), Path(sys.base_prefix).resolve(),
                 Path(sys.executable).resolve(), Path(cli_path).resolve()]
    writable = [(run_dir / "stderr").resolve()]
    wrapped: list[str]
    if context["profile"] == "copy":
        wrapped = copy_workspace.protected_command(
            command, context["copy"], extra_protected=protected,
            writable_exceptions=[Path(context["copy"]["workspace_root"]), *writable])
    else:
        wrapped = copy_workspace.readonly_command(command, source_paths=[Path(item) for item in context["protected_source"]],
                                                  extra_protected=protected,
                                                  writable_exceptions=[*writable, Path(context["channel"]).resolve()])
    return wrapped


FAMILY_NAME = "family.json"


def persist_family(run_dir: Path, family: Family, previous: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Record the run-owned process identities seen so far; worker-loss recovery stops them by identity."""
    identities: list[dict[str, Any]] = family.identities()
    if identities != previous:
        dump(run_dir / FAMILY_NAME, {"marker": family.environment[FAMILY_KEY], "identities": identities})
    return identities


def _stop_leg(proc: subprocess.Popen[bytes], family: Family, transport: ProcessStream | None,
              conversation: duplex.Conversation, facts: dict[str, Any]) -> dict[str, Any]:
    """Reclaim one CLI process family; each step runs even when an earlier one fails."""
    errors: list[str] = []

    def step(label: str, action: Callable[[], Any]) -> Any:
        try:
            return action()
        except Exception as exc:  # noqa: BLE001 - reclamation must reach the later steps
            errors.append(f"{label}: {type(exc).__name__}: {exc}")
            return None
    # Held processes die while still stopped; resuming them first would let them write while the CLI exits.
    step("discard held", lambda: conversation.discard_held("stopping"))
    stop_error = step("terminate", lambda: process_control.terminate_group(proc))
    if stop_error:
        facts["cleanup_error"] = stop_error
    if transport is not None:
        step("close input", lambda: transport.close_input("incomplete"))
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        facts["cleanup_error"] = step("terminate again", lambda: process_control.terminate_group(proc)) \
            or "direct child still running after stop"
    if not step("group check", lambda: process_control.wait_group_absent(proc.pid, .75)):
        facts["residual_group"] = True
        facts["cleanup_error"] = step("terminate group", lambda: process_control.terminate_group(proc)) or facts["cleanup_error"]
    observed = step("observe", family.observe) or {}
    facts["residual_descendants"] = (facts.get("residual_descendants") or 0) + int(observed.get("live_count") or 0)
    family_evidence = step("finish family", family.finish) or {"state": "unknown"}
    if transport is not None:
        eof_deadline = time.monotonic() + 3
        while not transport.eof and time.monotonic() < eof_deadline:
            failures = len(errors)
            step("drain", lambda: transport.drain(.1))
            if len(errors) > failures:
                break
        step("finish stream", transport.finish)
    step("detach", conversation.detach)
    if errors:
        facts.setdefault("cleanup_errors", []).extend(errors)
    return family_evidence


def _leg(run_dir: Path, command: list[str], cwd: str, env: dict[str, str], payload: bytes, deadline: float,
         stream_name: str, conversation: duplex.Conversation, facts: dict[str, Any],
         payload_written: Callable[[bool, int], None] | None = None) -> str:
    """Run one CLI process until it exits, the run is cancelled or timed out, or it must stop for an answer.

    Every path after the process starts, including an exception in event handling, notes or the store, ends with
    the process family reclaimed and the stream closed.
    """
    family = Family()
    env = {**env, **family.environment}
    with (run_dir / stream_name).open("a", encoding="utf-8") as stream, (run_dir / "stderr").open("a", encoding="utf-8") as err:
        try:
            proc = subprocess.Popen(command, cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=err,
                                    start_new_session=True, env=env)
        except OSError as exc:
            facts["launch_error"] = f"{type(exc).__name__}: {exc}"[:600]
            facts["legs"].append({"reason": "launch_failed", "exit_code": None, "family": "not_launched", "pid": None})
            return "launch_failed"
        reason = "exited"
        transport: ProcessStream | None = None
        try:
            family.adopt_descendants_of(proc.pid)
            dump(run_dir / "child.json", {"pid": proc.pid, "process_group": proc.pid, "identity": process_control.identity(proc.pid),
                                          "family_marker": env[FAMILY_KEY], "started_at": time.time(), "stream": stream_name,
                                          "leg": len(facts["legs"]) + 1})
            activity(run_dir, "executing", "Claude started" if not facts["legs"] else "Claude resumed", status="executing")
            transport = ProcessStream(proc, stream, memoryview(payload), run_dir, True, cwd, keep_open=True,
                                      on_event=conversation.on_event, on_payload_written=payload_written)
            conversation.attach(transport, proc.pid, family)
            next_observation = 0.0
            persisted: list[dict[str, Any]] | None = None
            while proc.poll() is None:
                if time.monotonic() >= next_observation:
                    family.observe()
                    persisted = persist_family(run_dir, family, persisted)
                    next_observation = time.monotonic() + .5
                transport.drain(.15)
                conversation.pump()
                if conversation.capability_error:
                    reason = "capability"
                elif conversation.stop_requested:
                    reason = "stop_for_answer"
                elif cancel_requested(run_dir):
                    reason = "cancelled"
                elif time.monotonic() > deadline:
                    reason = "timed_out"
                if reason != "exited":
                    # No interrupt first for stop_for_answer: the CLI would withdraw the very questions being held.
                    break
        except KeyboardInterrupt:
            reason = "cancelled"
        except Exception as exc:
            traceback.print_exc()
            reason = "supervisor_error"
            facts["supervisor_error"] = f"{type(exc).__name__}: {exc}"[:600]
        finally:
            family_evidence = _stop_leg(proc, family, transport, conversation, facts)
            facts.update(exit_code=proc.returncode, prompt_delivery=transport.delivery if transport else None,
                         family=family_evidence, stdout_eof=transport.eof if transport else False)
            facts["legs"].append({"reason": reason, "exit_code": proc.returncode, "family": family_evidence.get("state"),
                                  "pid": proc.pid})
            if conversation.shells_stopped:
                facts["shells_stopped"] = list(conversation.shells_stopped)
            if family_evidence.get("state") != "stopped" and not facts["cleanup_error"]:
                facts["cleanup_error"] = "detached descendants could not be confirmed stopped"
    return reason


def _await_answers(run_dir: Path, store: ControlStore, run_id: str, deadline: float,
                   conversation: duplex.Conversation) -> tuple[str, list[dict[str, Any]]]:
    """Execution is stopped; wait until every blocking question has an answer, a cancellation or the deadline.

    An interrupt sent meanwhile withdraws the questions it follows, as the live CLI would; the resumed
    session then carries the interrupt and its redirect.
    """
    while True:
        # Cancellation and the deadline win over answers that arrive at the same time: the run stays held.
        if cancel_requested(run_dir):
            return "cancelled", []
        if time.monotonic() > deadline:
            return "timed_out", []
        conversation.pump()
        withdrawn = store.withdraw_for_interrupt(run_id)
        if withdrawn:
            conversation.withdrawn.extend(withdrawn)
            activity(run_dir, "question", "Codex's interrupt withdrew the question(s) the stopped execution was waiting on",
                     status="cancelled", text="; ".join(item["interaction_id"] for item in withdrawn))
        answered = store.resume_answers(run_id)
        if answered is not None:
            return "answered", answered
        time.sleep(.2)


def supervise(run_dir: Path, command_for: Callable[[tuple[str, str]], list[str]], cwd: str, env: dict[str, str],
              payload: bytes, timeout: float, stream_name: str, session: tuple[str, str],
              conversation: duplex.Conversation, store: ControlStore, run_id: str) -> dict[str, Any]:
    """Run the Claude session to its end, stopping and resuming it when a serious question needs that."""
    deadline = time.monotonic() + timeout
    facts: dict[str, Any] = {"timed_out": False, "cancelled": False, "cleanup_error": None, "legs": [], "holds": []}
    written: Callable[[bool, int], None] | None = None
    while True:
        if facts["legs"]:
            # A resumed leg starts only if nothing ended the run while the answers were being collected.
            if cancel_requested(run_dir):
                facts["cancelled"] = True
                return facts
            if time.monotonic() > deadline:
                facts["timed_out"] = True
                return facts
        reason = _leg(run_dir, command_for(session), cwd, env, payload, deadline, stream_name, conversation, facts, written)
        facts["unfinished_turn"] = bool(conversation.awaiting_result and conversation.inits)
        facts["timed_out"] = facts["timed_out"] or reason == "timed_out"
        facts["cancelled"] = facts["cancelled"] or reason == "cancelled"
        if reason == "capability":
            facts["capability_error"] = conversation.capability_error
        if reason != "stop_for_answer":
            return facts
        pending = store.pending_blocking(run_id)
        stopped = not facts["cleanup_error"] and facts["family"].get("state") == "stopped"
        for interaction_id in pending:
            hold = dict((store.interaction(interaction_id) or {}).get("hold") or {})
            hold.update(mode="stop_for_answer", stopped_at=time.time(), stop_confirmed=stopped, session_id=conversation.session_id)
            store.update_hold(interaction_id, hold)
        facts["holds"].append({"interaction_ids": pending, "stop_confirmed": stopped})
        if not stopped or not conversation.session_id:
            # An execution that may still be writing is never joined by a resumed one.
            for interaction_id in pending:
                store.expire(interaction_id, "the stopped execution could not be confirmed stopped or has no session")
            facts["hold_error"] = "execution could not be confirmed stopped for a serious question; it was not resumed"
            return facts
        store.set_state(run_id, "held", expect=("running",))
        activity(run_dir, "hold", "execution stopped; waiting for every pending answer", status="stopped")
        waited, answered = _await_answers(run_dir, store, run_id, deadline, conversation)
        if waited != "answered":
            facts["timed_out"] = facts["timed_out"] or waited == "timed_out"
            facts["cancelled"] = facts["cancelled"] or waited == "cancelled"
            return facts
        messages = store.resume_messages(run_id)
        for message in messages:
            activity(run_dir, message["kind"], "Codex's message queued in the resumed session's first input",
                     status=message["origin"], text=str(message["payload"].get("text") or ""))
        text = duplex.resumed_text(answered, messages, conversation.withdrawn)
        conversation.withdrawn = []
        payload = user_line(text)
        written = conversation.expect_resume(text, [str(item["answer_command"]) for item in answered]
                                             + [str(item["command_id"]) for item in messages])
        session = ("resume", conversation.session_id)
        activity(run_dir, "resumed", "resuming the session with Codex's answers", status="resumed")


def execute(run_dir: Path) -> int:
    packet = load(run_dir / "packet.json")
    plan = load(run_dir / "plan.json")
    execution: dict[str, Any] = {"started_at": time.time(), "launched": False}

    def finish(outcome: str, note: str | None = None) -> int:
        execution.update(ended_at=time.time(), note=note)
        dump(run_dir / "execution.json", execution)
        result = finalize(run_dir, outcome, note)
        return 0 if result["run_outcome"] in {"ok", "cancelled"} else 1

    try:
        activity(run_dir, "preflight", "checking the local Claude CLI", status="preflight")
        if cancel_requested(run_dir):
            return finish("cancelled", "cancelled before launch")
        environment = cli_env.check(Path(packet["cwd"]))
        dump(run_dir / "environment.json", environment)
        if not environment["ready"]:
            return finish("not_started", f"Claude CLI is not ready ({environment['status']}): {environment['action']}")
        if packet["max_budget_usd"] is not None and not environment["cli"]["budget_flag"]:
            return finish("not_started", "max_budget_usd was requested but the local Claude CLI has no --max-budget-usd")
        activity(run_dir, "preflight", "preparing the workspace", status="preflight")
        try:
            context = prepare_workspace(packet, plan, run_dir)
        except copy_workspace.CopyWorkspaceError as exc:
            hint = "; analyze tasks can use profile=readonly" if packet["kind"] == "analyze" and "undelivered" not in str(exc) else ""
            return finish("not_started", f"copy preparation failed: {exc}{hint}")
        context["state_root"] = plan["state_root"]
        context["inputs"], context["inputs_skipped"] = freeze_inputs(packet["inputs"], run_dir / "inputs")
        dump(run_dir / "inputs.json", {"inputs": context["inputs"], "skipped": context["inputs_skipped"]})
        layer = instruction_layer.scan(Path(context["execution_cwd"]))
        dump(run_dir / "instruction-layer.json", layer)
        context.update(deadline=time.time() + packet["timeout_seconds"], brief_path=str(run_dir / "brief.md"),
                       cli={"path": environment["cli"]["path"], "version": environment["cli"]["version"]})
        brief_bytes = brief_renderer.render(packet, {**plan, **context, "instruction_layer": layer}).encode("utf-8")
        (run_dir / "brief.md").write_bytes(brief_bytes)
        context["brief_sha256"] = hashlib.sha256(brief_bytes).hexdigest()
        dump(run_dir / "context.json", context)
        env = {key: value for key, value in cli_environment(locate_claude()).items() if not key.startswith("CODEX_CLAUDE_")}
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        # Lets the bridge close input on the CLI's own idle state, as the SDK does.
        env["CLAUDE_CODE_SDK_READS_SESSION_STATE"] = "1"
        # claude -p otherwise stops waiting for background work after its own idle ceiling.
        env[PRINT_BG_WAIT_CEILING] = str(max(1000, int(packet["timeout_seconds"] * 1000)))
        store = control_store(plan["state_root"])
        conversation = duplex.Conversation(run_dir, store, run_dir.name, interactive=packet.get("interactive", True),
                                           notes_path=Path(context["notes"]) if context.get("notes") else None)
        resume = plan.get("resume_session")
        attempts = [("resume", resume)] if resume else []
        attempts.append(("session", str(uuid.uuid4())))
        payload = user_line(brief_bytes.decode("utf-8"))

        def command_for(session: tuple[str, str]) -> list[str]:
            command = build_command(packet, context, environment["cli"]["path"], run_dir, session)
            dump(run_dir / "command.json", {"argv": command, "cwd": context["execution_cwd"], "session": list(session)})
            return command

        try:
            build_command(packet, context, environment["cli"]["path"], run_dir, attempts[0])
        except copy_workspace.CopyWorkspaceError as exc:
            return finish("not_started", boundary_problem(context, plan, exc))
        for position, session in enumerate(attempts):
            if cancel_requested(run_dir):
                return finish("cancelled", "cancelled before launch")
            remaining = max(1.0, context["deadline"] - time.time())
            execution["launched"] = True
            stream_name = "stream.jsonl" if position == len(attempts) - 1 else "stream-resume-attempt.jsonl"
            facts = supervise(run_dir, command_for, context["execution_cwd"], env, payload, remaining, stream_name, session,
                              conversation, store, run_dir.name)
            execution.update(facts, session=list(session))
            if session[0] == "resume":
                attempt = stream_parser.parse_stream(run_dir / stream_name)
                # Only a CLI that ran and rejected the session is a stale session; a launch error would repeat.
                ran = not facts.get("launch_error")
                if ran and attempt["init"] is None and not (facts["cancelled"] or facts["timed_out"]) \
                        and facts.get("exit_code") != 0:
                    execution["resume_fallback"] = f"--resume {session[1]} did not start (exit {facts.get('exit_code')}); a fresh session was used"
                    continue
                if not ran:
                    execution["resume_launch_failed"] = f"--resume {session[1]} could not be launched; no fresh session was tried"
                (run_dir / stream_name).rename(run_dir / "stream.jsonl")
            break
        if execution.get("capability_error"):
            return finish("not_started", "the Claude CLI did not start in native auto mode with a question channel: "
                          + execution["capability_error"] + ". Update Claude Code; the plugin does not fall back to "
                          "dontAsk or a reduced tool set")
        if execution.get("cancelled"):
            return finish("cancelled", "cancelled by request")
        if execution.get("timed_out"):
            return finish("timeout", f"stopped at the {packet['timeout_seconds']:g}s deadline")
        if execution.get("launch_error"):
            legs = execution.get("legs") or []
            if all(leg.get("reason") == "launch_failed" for leg in legs):
                # Nothing ran: the true outcome is that the run never started, not that Claude crashed.
                execution["launched"] = False
                resuming = (execution.get("session") or [None])[0] == "resume"
                return finish("not_started", "the Claude CLI could not be started"
                              + (f" to resume session {execution['session'][1]}" if resuming else "") + ": "
                              + execution["launch_error"] + (". No fresh session was tried: a launch error is not a "
                                                             "rejected session" if resuming else ""))
            return finish("crashed", "the session could not be resumed after the answers: " + execution["launch_error"])
        if execution.get("supervisor_error"):
            return finish("crashed", "the supervisor failed and stopped Claude: " + execution["supervisor_error"])
        if execution.get("hold_error"):
            return finish("crashed", execution["hold_error"])
        facts = stream_parser.parse_stream(run_dir / "stream.jsonl")
        if facts["final"] is None:
            return finish("crashed", f"Claude exited (code {execution.get('exit_code')}) without a final result")
        if execution.get("unfinished_turn"):
            # An earlier turn's result stays as history; it does not answer for a turn that started after it.
            return finish("crashed", f"Claude exited (code {execution.get('exit_code')}) during a later turn that never "
                          "produced its result; the earlier result is kept only as history")
        if execution.get("exit_code") != 0:
            return finish("crashed", f"Claude exited abnormally (code {execution.get('exit_code')}) after reporting a result; "
                          "the reported result is kept as evidence, not as a normal process exit")
        return finish("ok")
    except Exception as exc:
        traceback.print_exc()
        execution["error"] = f"{type(exc).__name__}: {exc}"
        return finish("crashed" if execution["launched"] else "not_started", execution["error"][:600])


def _collect(source: Path, target: Path, limit: int) -> bytes | None:
    try:
        data: bytes = read_regular(source, limit)
    except (ValueError, OSError):
        return None
    target.write_bytes(data)
    return data


def _deliverables(run_dir: Path, context: dict[str, Any], facts: dict[str, Any],
                  warnings: list[dict[str, Any]]) -> tuple[Any, bytes | None]:
    """Copy the report and result header into run state; return (result, report bytes)."""
    final = facts["final"] or {}
    if isinstance(final.get("result"), str) and final["result"].strip():
        (run_dir / "final-message.md").write_text(final["result"], encoding="utf-8")
    result: Any = None
    report: bytes | None = None
    raw_result: bytes | None = None
    if context.get("profile") == "copy" and context.get("outbox"):
        outbox = Path(context["outbox"])
        report = _collect(outbox / "report.md", run_dir / "report.md", REPORT_LIMIT)
        raw_result = _collect(outbox / "result.json", run_dir / "result.json", RESULT_LIMIT)
    elif isinstance(final.get("structured_output"), dict):
        structured = dict(final["structured_output"])
        markdown = structured.pop("report_markdown", None)
        if isinstance(markdown, str):
            report = markdown.encode("utf-8")
            (run_dir / "report.md").write_bytes(report)
        raw_result = json.dumps(structured, ensure_ascii=False, indent=2).encode("utf-8")
        (run_dir / "result.json").write_bytes(raw_result)
    if report is None:
        warnings.append({"code": "report_missing", "detail": "Claude did not deliver report.md; final-message.md holds its last reply if any"})
    if raw_result is None:
        warnings.append({"code": "result_missing", "detail": "Claude did not deliver result.json"})
        return None, report
    try:
        result = json.loads(raw_result)
    except ValueError as exc:
        warnings.append({"code": "result_malformed", "detail": f"result.json is not JSON: {exc}"})
        return None, report
    problems = result_schema.validate(result)
    if problems:
        warnings.append({"code": "result_malformed", "detail": "; ".join(problems[:10])})
    return result, report


def _changes(run_dir: Path, packet: dict[str, Any], context: dict[str, Any],
             warnings: list[dict[str, Any]]) -> dict[str, Any] | None:
    if context.get("profile") != "copy" or not context.get("start_tree"):
        return None
    metadata = context["copy"]
    store, index = Path(context["store"]), Path(context["index"])
    try:
        end_tree = copy_workspace.record_tree(store, metadata, index)
        patch = copy_workspace.tree_patch(store, metadata, index, context["start_tree"], end_tree)
        files = copy_workspace.tree_changes(store, metadata, index, context["start_tree"], end_tree)
        (run_dir / "changes.patch").write_bytes(patch)
        base = context.get("base_tree") or context["start_tree"]
        (run_dir / "delivery.patch").write_bytes(copy_workspace.tree_patch(store, metadata, index, base, end_tree))
        delivery_files = [{"path": item["path"], "status": item["status"]}
                          for item in copy_workspace.tree_changes(store, metadata, index, base, end_tree)]
    except (copy_workspace.CopyWorkspaceError, OSError) as exc:
        warnings.append({"code": "patch_unavailable", "detail": f"changes could not be computed: {exc}"})
        return None
    protected = [item["path"] for item in files if matches(item["path"], packet["protected"])]
    outside = [item["path"] for item in files if packet["write_hint"] and not matches(item["path"], packet["write_hint"])]
    repo, patch = shlex.quote(metadata["source_root"]), shlex.quote(str(run_dir / "delivery.patch"))
    for item in files:
        item["class"] = "protected" if item["path"] in protected else "outside_hint" if item["path"] in outside else "in_scope"
    if protected:
        warnings.append({"code": "protected_touched", "count": len(protected), "paths": protected[:50],
                         "detail": f"the patch changes {len(protected)} protected path(s); claude_decide must confirm each "
                                   "(the full list is changes.files with class=protected)"})
    if outside:
        warnings.append({"code": "outside_hint", "count": len(outside), "paths": outside[:50],
                         "detail": f"the patch changes {len(outside)} file(s) outside write_hint"})
    return {"patch": "changes.patch", "delivery_patch": "delivery.patch", "base_tree": base,
            "delivery_files": delivery_files, "delivery_file_count": len(delivery_files), "start_tree": context["start_tree"],
            "end_tree": end_tree, "files": files, "file_count": len(files),
            "insertions": sum(item["insertions"] or 0 for item in files),
            "deletions": sum(item["deletions"] or 0 for item in files),
            "copy_root": metadata["workspace_root"], "source_root": metadata["source_root"],
            "base_head": metadata["source_head"],
            # delivery.patch holds every change not yet recorded as applied, measured from the original's
            # working tree (uncommitted edits included), so it applies to that tree; --3way would demand an index match.
            # git apply rejects an empty patch, so there is no command when nothing is pending.
            "check_hint": f"git -C {repo} apply --check {patch}" if delivery_files else None,
            "apply_hint": f"git -C {repo} apply {patch}" if delivery_files else None}


def _stream_warnings(packet: dict[str, Any], context: dict[str, Any], facts: dict[str, Any],
                     execution: dict[str, Any], warnings: list[dict[str, Any]]) -> None:
    if facts["denials"]:
        names = sorted({entry.get("tool_name", "?") for entry in facts["denials"]})
        warnings.append({"code": "tool_denied", "detail": f"{len(facts['denials'])} tool call(s) were denied: {', '.join(names)}",
                         "items": facts["denials"][:20]})
    pending = [record for record in facts["workflows"].values() if record["status"] == "launched"]
    failed = [record for record in facts["workflows"].values() if record["status"] not in {"launched", "completed"}]
    if pending or failed:
        warnings.append({"code": "workflow_incomplete",
                         "detail": f"{len(pending)} Workflow(s) never reported completion and {len(failed)} ended as failed/stopped"})
    unread = [entry["copy"] for entry in context.get("inputs") or []
              if entry["kind"] == "file" and entry["copy"] not in facts["read_paths"]]
    if unread and facts["init"] is not None:
        warnings.append({"code": "source_not_read", "paths": unread[:50],
                         "detail": "these frozen inputs were not opened with Read in the main session (subagent reads may be missing)"})
    final = facts["final"] or {}
    if final and (final.get("is_error") or final.get("subtype") not in (None, "success")):
        warnings.append({"code": "provider_error", "detail": f"the CLI result ended with subtype={final.get('subtype')!r}"})
    if execution.get("residual_group") or execution.get("residual_descendants"):
        warnings.append({"code": "residual_processes_stopped",
                         "detail": "descendant processes were still running when Claude exited and were stopped"})
    if execution.get("cleanup_error"):
        warnings.append({"code": "process_stop_unconfirmed", "detail": execution["cleanup_error"]})
    carried = context.get("carried") or {}
    if carried.get("mode") == "already_in_original":
        warnings.append({"code": "carried_already_in_original",
                         "detail": f"the undelivered work of {carried.get('from_run')} is already in the original; "
                                   "record applied=true when you apply a delivery.patch"})
    if carried.get("now_ignored"):
        warnings.append({"code": "undelivered_files_now_ignored", "paths": carried["now_ignored"][:50],
                         "detail": "the original's ignore rules now match these undelivered files; they stay in "
                                   "delivery.patch, but check whether they should still be delivered"})
    if execution.get("resume_fallback"):
        warnings.append({"code": "resume_fallback", "detail": execution["resume_fallback"]})
    if execution.get("shells_stopped"):
        warnings.append({"code": "background_commands_stopped", "items": execution["shells_stopped"][:20],
                         "detail": "Claude finished while background commands were still running; they were stopped "
                                   "with the CLI's stop_task so the run could end"})
    turns = facts.get("turns") or []
    if final and len(turns) > 1 and any(turn.get("is_error") for turn in turns[:-1]):
        warnings.append({"code": "turn_interrupted", "detail": f"{sum(bool(t.get('is_error')) for t in turns[:-1])} earlier "
                         "turn(s) ended with an error or interrupt; the last turn's result is the final one"})
    skipped = list(context.get("inputs_skipped") or []) + list((context.get("copy") or {}).get("skipped_untracked") or [])
    if skipped:
        warnings.append({"code": "sensitive_inputs_skipped", "paths": skipped[:50],
                         "detail": "these paths were not copied for Claude (sensitive name, symlink or size limit)"})


def _interaction_facts(plan: dict[str, Any], run_id: str, warnings: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not plan.get("state_root"):
        return None
    summary = control_store(plan["state_root"]).summary(run_id)
    if summary["revision"] is None:
        return None
    items = summary["interactions"]
    abandoned = [item["interaction_id"] for item in items if item["status"] == "abandoned"]
    if abandoned:
        warnings.append({"code": "questions_unanswered", "items": abandoned[:20],
                         "detail": "Claude's blocking question(s) were still waiting for Codex when the run ended"})
    counts: dict[str, int] = {}
    for item in items:
        counts[item["kind"]] = counts.get(item["kind"], 0) + 1
    return {"revision": summary["revision"], "counts": counts, "abandoned": abandoned,
            "holds": [item["hold"] for item in items if item.get("hold")][:20]}


def _result_warnings(result: Any, warnings: list[dict[str, Any]]) -> None:
    if not isinstance(result, dict):
        return
    for key, code, detail in (("questions", "questions_for_user", "Claude asks questions only the user can decide; relay them verbatim"),
                              ("disputes", "disputes_present", "Claude reports conflicts with the user's words, inputs or constraints"),
                              ("not_verified", "not_verified_present", "Claude lists work it could not verify")):
        raw = result.get(key)
        values = [value for value in raw if isinstance(value, str) and value.strip()] if isinstance(raw, list) else []
        if values:
            warnings.append({"code": code, "detail": detail, "items": values[:20]})


def finalize(run_dir: Path, run_outcome: str, note: str | None = None) -> dict[str, Any]:
    """Write outcome.json from what is on disk; idempotent once written, and always written."""
    existing = _read_json(run_dir / "outcome.json")
    if isinstance(existing, dict):
        return existing
    if run_outcome not in OUTCOMES:
        raise ValueError(f"unknown run outcome {run_outcome}")
    state_root = (_read_json(run_dir / "plan.json") or {}).get("state_root")
    if state_root:
        try:
            # Pending questions are abandoned and undelivered commands fail with the run.
            control_store(state_root).set_state(run_dir.name, "lost" if run_outcome == "lost" else "ended")
        except Exception:
            traceback.print_exc()
    try:
        outcome = _assemble(run_dir, run_outcome, note)
    except Exception as exc:
        # Whatever Claude delivered stays on disk as evidence; the run must still end.
        traceback.print_exc()
        packet = _read_json(run_dir / "packet.json") or {}
        plan = _read_json(run_dir / "plan.json") or {}
        outcome = {"schema": "codex-claude-orchestrator.outcome/1", "run_id": run_dir.name,
                   "task_id": packet.get("task_id"), "kind": packet.get("kind"), "profile": plan.get("profile"),
                   "round": plan.get("round"), "run_outcome": run_outcome, "note": note, "process_stopped": "unknown",
                   "claimed_status": None, "summary": None, "report": None, "result": None, "items": 0, "changes": None,
                   "brief": None, "instruction_layer": None, "session_id": None, "models": [], "usage": None,
                   "cost_usd": None, "duration_seconds": None, "cli": None, "plugin_version": plugin_version(),
                   "warnings": [{"code": "finalize_error",
                                 "detail": f"the outcome could not be fully assembled ({type(exc).__name__}: {exc}); "
                                           "read the raw artifacts"}],
                   "ended_at": time.time()}
    dump(run_dir / "outcome.json", outcome)
    activity(run_dir, "finished", f"run ended: {run_outcome}", status=run_outcome)
    return outcome


def _assemble(run_dir: Path, run_outcome: str, note: str | None) -> dict[str, Any]:
    packet = load(run_dir / "packet.json")
    plan = _read_json(run_dir / "plan.json") or {}
    context = _read_json(run_dir / "context.json") or {}
    execution = _read_json(run_dir / "execution.json") or {}
    child = _read_json(run_dir / "child.json")
    facts = stream_parser.parse_stream(run_dir / "stream.jsonl")
    warnings: list[dict[str, Any]] = []
    result, report = _deliverables(run_dir, context, facts, warnings) if execution.get("launched") or child else (None, None)
    if run_outcome == "not_started":
        warnings = []
    # A copy prepared for a round that then did not start (a CLI capability failure at init, for one) still holds
    # the carried work and anything written before the stop; measuring it keeps the lineage continuable.
    changes = _changes(run_dir, packet, context, warnings)
    if context.get("original_before") and run_outcome != "not_started":
        try:
            changed = state_changes(context["original_before"], worktree_state(Path(context["source_root"])))
        except OSError:
            changed = []
        if changed:
            warnings.append({"code": "original_changed_during_run", "paths": changed[:50],
                             "detail": "the original worktree changed while Claude worked in the copy; check that the patch still applies"})
    if run_outcome != "not_started":
        _stream_warnings(packet, context, facts, execution, warnings)
        _result_warnings(result, warnings)
    try:
        interactions = _interaction_facts(plan, run_dir.name, warnings)
    except Exception as exc:
        interactions = None
        warnings.append({"code": "interactions_unavailable", "detail": f"{type(exc).__name__}: {exc}"})
    stopped = "unknown"
    if child is None:
        stopped = "not_launched"
    elif not execution.get("cleanup_error") and execution.get("exit_code") is not None:
        stopped = "confirmed"
    elif run_outcome == "lost":
        stopped = execution.get("lost_stop", "unknown")
    final = facts["final"] or {}
    summary = result.get("summary") if isinstance(result, dict) and isinstance(result.get("summary"), str) else None
    layer = _read_json(run_dir / "instruction-layer.json") or {}
    outcome = {
        "schema": "codex-claude-orchestrator.outcome/1", "run_id": run_dir.name, "task_id": packet["task_id"],
        "kind": packet["kind"], "profile": plan.get("profile"), "round": plan.get("round"),
        "run_outcome": run_outcome, "note": note, "process_stopped": stopped,
        "claimed_status": result.get("status") if isinstance(result, dict) and result.get("status") in result_schema.STATUSES else None,
        "summary": summary[:SUMMARY_LIMIT] if summary else None,
        "report": ({"path": "report.md", "bytes": len(report), "sha256": hashlib.sha256(report).hexdigest()}
                   if report is not None else None),
        "result": {"path": "result.json", "valid": not any(w["code"] == "result_malformed" for w in warnings)}
                  if (run_dir / "result.json").is_file() else None,
        "items": len(result.get("items") or []) if isinstance(result, dict) and isinstance(result.get("items"), list) else 0,
        "changes": changes, "carried": {key: (context.get("carried") or {}).get(key) for key in ("from_run", "mode", "files")}
        if context.get("carried") else None,
        "brief": {"path": "brief.md", "sha256": context["brief_sha256"]} if context.get("brief_sha256") else None,
        "instruction_layer": {"path": "instruction-layer.json", "files": len(layer.get("files") or []),
                              "hooks": len(layer.get("hooks") or []),
                              "plugins": [item.get("name") for item in ((facts["init"] or {}).get("plugins") or [])
                                          if isinstance(item, dict)]} if layer else None,
        "warnings": warnings,
        "session_id": facts["session_id"], "models": facts["models"], "interactions": interactions,
        "turns": len(facts.get("turns") or []), "legs": len(execution.get("legs") or []) or None,
        "usage": usage_report.usage_report(final, result_events=facts["result_events"],
                                           resumed=(execution.get("session") or [None])[0] == "resume") if final else None,
        "cost_usd": usage_report.cost_amount(final.get("total_cost_usd")) if final else None,
        "duration_seconds": round(max(0.0, (execution.get("ended_at") or time.time()) - (execution.get("started_at") or time.time())), 1),
        "cli": context.get("cli"), "plugin_version": plan.get("plugin_version") or plugin_version(),
        "ended_at": time.time(),
    }
    return outcome


def main() -> int:
    parser = argparse.ArgumentParser(description="Supervised Claude CLI bridge")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="execute one prepared run directory")
    run.add_argument("--run-dir", required=True)
    doctor = sub.add_parser("doctor", help="check the local Claude CLI; --verify makes one small paid request")
    doctor.add_argument("--cwd", default=None)
    doctor.add_argument("--verify", action="store_true")
    doctor.add_argument("--model", default="sonnet")
    args = parser.parse_args()
    if args.command == "run":
        run_dir = Path(args.run_dir).resolve()

        def stop(signum: int, _frame: Any) -> None:
            # Route shutdown through cancellation so the Claude group is stopped and evidence is kept.
            if not cancel_requested(run_dir):
                dump(run_dir / "cancel.json", {"reason": f"bridge received {signal.Signals(signum).name}",
                                               "requested_at": time.time()})

        for signum in (signal.SIGTERM, signal.SIGHUP):
            signal.signal(signum, stop)
        return execute(run_dir)
    if args.command == "doctor":
        report = cli_env.check(Path(args.cwd or os.getcwd()).resolve(), verify=args.verify, model=args.model)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["ready"] else 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
