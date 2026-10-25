#!/usr/bin/env python3
"""Host acceptance for 1.1 interactive runs with the real Claude CLI and the real macOS write boundary.

Run on the host (not inside another sandbox); it makes several small paid Claude requests:
  NO_PROXY=localhost,127.0.0.1 uv run --project plugins/codex-claude-orchestrator --frozen \\
      python tools/host_acceptance.py --model haiku --out /tmp/cco-acceptance.json
Every scenario uses a throwaway Git fixture and state root under a new temporary directory and
records facts; model behaviour can vary, so a scenario reports what happened rather than retrying.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "plugins/codex-claude-orchestrator/skills/codex-claude-orchestrator/scripts"
sys.path.insert(0, str(SCRIPTS))
import cli_env  # noqa: E402
from runtime import Runtime  # noqa: E402


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-c", "core.hooksPath=/dev/null", *args], cwd=cwd, check=True,
                          capture_output=True, text=True).stdout


def fixture(base: Path) -> Path:
    source = base / "source"
    source.mkdir()
    git(source, "init", "-q")
    git(source, "config", "user.email", "acceptance@example.invalid")
    git(source, "config", "user.name", "Acceptance")
    (source / "README.md").write_text("# fixture\n")
    git(source, "add", ".")
    git(source, "commit", "-qm", "base")
    return source


def packet(source: Path, task_id: str, text: str, chat: str, model: str, **extra: Any) -> dict[str, Any]:
    return {"task_id": task_id, "kind": "implement", "cwd": str(source), "model": model, "effort": "low",
            "timeout_seconds": 600, "user_messages": [{"text": text, "source": "human"}],
            "title": task_id.replace("-", " ").lower(), "context": {"chat": chat}, **extra}


def drive(runtime: Runtime, run_id: str, chat: str, on_pending: Callable[[dict[str, Any]], None],
          on_activity: Callable[[dict[str, Any]], None] | None = None, limit: float = 600) -> dict[str, Any]:
    cursor, deadline, answered = 0, time.monotonic() + limit, set()
    while time.monotonic() < deadline:
        step = runtime.wait(run_id, after=cursor, timeout=20, context={"chat": chat})
        cursor = step["next_cursor"]
        for item in step["pending"]:
            if item["interaction_id"] not in answered:
                answered.add(item["interaction_id"])
                on_pending(item)
        if on_activity:
            on_activity(step)
        if step["reason"] == "terminal":
            return step["snapshot"]
    raise TimeoutError(f"{run_id} did not end")


def receipt_states(runtime: Runtime, run_id: str, ids: list[str]) -> dict[str, Any]:
    return {command: (runtime.store.command(run_id, command) or {}).get("delivery") for command in ids}


def question_update(runtime: Runtime, source: Path, model: str) -> dict[str, Any]:
    text = ("Do these steps in order. 1) Use the AskUserQuestion tool to ask me exactly one question: 'Which color?' "
            "with options red and blue. 2) Write the chosen answer text into color.txt. 3) Wait for any further "
            "instructions that arrive while you work, follow them, then reply DONE.")
    started = runtime.start(packet(source, "ACCEPT-QUESTION", text, "accept-a", model, round_title="question and update"))
    run_id, sent = started["run_id"], []

    def answer(item: dict[str, Any]) -> None:
        runtime.reply(run_id, "answer-1", "answer", "blue", interaction_id=item["interaction_id"], origin="human",
                      context={"chat": "accept-a"})
        sent.append("answer-1")
        runtime.reply(run_id, "update-1", "update", "Also create b.txt containing update-seen before replying DONE.",
                      context={"chat": "accept-a"})
        sent.append("update-1")
    snapshot = drive(runtime, run_id, "accept-a", answer)
    delivery = (Path(snapshot["run_dir"]) / "delivery.patch")
    return {"run_id": run_id, "outcome": (snapshot.get("outcome") or {}).get("run_outcome"),
            "title": snapshot.get("title"), "round_title": snapshot.get("round_title"),
            "interactions": [(item["kind"], item["status"], item.get("answered_by")) for item in snapshot["interactions"]],
            "receipts": receipt_states(runtime, run_id, sent),
            "patch_has_color": "color.txt" in delivery.read_text() if delivery.is_file() else False,
            "patch_has_update_file": "b.txt" in delivery.read_text() if delivery.is_file() else False,
            "original_untouched": git(source, "status", "--porcelain") == "",
            "warnings": [warning["code"] for warning in (snapshot.get("outcome") or {}).get("warnings") or []]}


def background_hold(runtime: Runtime, source: Path, model: str) -> dict[str, Any]:
    text = ("Do these steps in order. 1) With the Bash tool and run_in_background=true, run: "
            "`while true; do date +%s.%N >> beat.txt; sleep 0.2; done`. 2) Immediately use AskUserQuestion to ask "
            "'Proceed?' with options yes and no. 3) After the answer, stop the background command, write the answer into "
            "proceed.txt and reply DONE.")
    started = runtime.start(packet(source, "ACCEPT-HOLD", text, "accept-b", model))
    run_id, facts = started["run_id"], {}
    copy_beat = Path(started["lineage_root"]) / "copy" / "beat.txt"

    def answer(item: dict[str, Any]) -> None:
        time.sleep(2)
        size = copy_beat.stat().st_size if copy_beat.exists() else None
        time.sleep(3)
        facts.update(state_while_pending=runtime.store.run(run_id)["state"],
                     beat_grew_while_pending=(copy_beat.stat().st_size if copy_beat.exists() else None) != size,
                     hold=(runtime.store.interaction(item["interaction_id"]) or {}).get("hold"))
        runtime.reply(run_id, "answer-hold", "answer", "yes", interaction_id=item["interaction_id"],
                      context={"chat": "accept-b"})
    snapshot = drive(runtime, run_id, "accept-b", answer)
    execution = json.loads((Path(snapshot["run_dir"]) / "execution.json").read_text())
    return {"run_id": run_id, "outcome": (snapshot.get("outcome") or {}).get("run_outcome"), **facts,
            "legs": execution.get("legs"), "receipts": receipt_states(runtime, run_id, ["answer-hold"])}


def agent_stop(runtime: Runtime, source: Path, model: str) -> dict[str, Any]:
    text = ("Do these in order. 1) Use the Agent tool with run_in_background=true and a general-purpose subagent whose task is: "
            "'Run the Bash command `sleep 60` in the foreground, and only after it finishes write finished into agent.txt'. "
            "2) Without waiting for it, use AskUserQuestion to ask 'Proceed?' with options yes and no. 3) After the answer, write "
            "the answer into proceed.txt and reply DONE without waiting for the agent.")
    started = runtime.start(packet(source, "ACCEPT-AGENT", text, "accept-d", model))
    run_id, facts = started["run_id"], {}

    def answer(item: dict[str, Any]) -> None:
        time.sleep(4)
        facts["hold"] = (runtime.store.interaction(item["interaction_id"]) or {}).get("hold")
        runtime.reply(run_id, "answer-agent", "answer", "yes", interaction_id=item["interaction_id"], context={"chat": "accept-d"})
    snapshot = drive(runtime, run_id, "accept-d", answer)
    execution = json.loads((Path(snapshot["run_dir"]) / "execution.json").read_text())
    return {"run_id": run_id, "outcome": (snapshot.get("outcome") or {}).get("run_outcome"), **facts,
            "legs": execution.get("legs"), "receipts": receipt_states(runtime, run_id, ["answer-agent"])}


def leftover_shell(runtime: Runtime, source: Path, model: str) -> dict[str, Any]:
    text = "Use the Bash tool with run_in_background=true to run exactly: `sleep 900`. Do not wait for it. Then reply DONE."
    began = time.monotonic()
    started = runtime.start(packet(source, "ACCEPT-SHELL", text, "accept-e", model))
    snapshot = drive(runtime, started["run_id"], "accept-e", lambda item: None)
    outcome = snapshot.get("outcome") or {}
    return {"run_id": started["run_id"], "outcome": outcome.get("run_outcome"), "seconds": round(time.monotonic() - began, 1),
            "warnings": [warning["code"] for warning in outcome.get("warnings") or []]}


def readonly_boundary(runtime: Runtime, base: Path, model: str) -> dict[str, Any]:
    plain = base / "plain"
    plain.mkdir()
    (plain / "notes.md").write_text("keep me\n")
    text = ("This is a read-only analysis. As a boundary check, try once to run with Bash: `echo x >> notes.md; "
            "echo y > created.txt` and report the exact error. Then answer: what does notes.md contain?")
    started = runtime.start({**packet(plain, "ACCEPT-READONLY", text, "accept-c", model), "kind": "analyze"})
    snapshot = drive(runtime, started["run_id"], "accept-c", lambda item: None)
    command = json.loads((Path(snapshot["run_dir"]) / "command.json").read_text())["argv"]
    return {"run_id": started["run_id"], "profile": snapshot["profile"],
            "outcome": (snapshot.get("outcome") or {}).get("run_outcome"),
            "source_unchanged": (plain / "notes.md").read_text() == "keep me\n" and not (plain / "created.txt").exists(),
            "sandboxed": command[0] == "/usr/bin/sandbox-exec", "tools": command[command.index("--tools") + 1],
            "permission_mode": command[command.index("--permission-mode") + 1]}


def same_task_two_chats(runtime: Runtime, source: Path, model: str) -> dict[str, Any]:
    text = "Reply with the single word DONE without using tools."
    first = runtime.start(packet(source, "SAME-NAME", text, "chat-one", model))
    second = runtime.start(packet(source, "SAME-NAME", text, "chat-two", model))
    try:
        runtime.reply(first["run_id"], "cross", "update", "hello", context={"chat": "chat-two"})
        cross = "accepted"
    except RuntimeError as exc:
        cross = f"refused: {exc}"
    ends = [drive(runtime, item["run_id"], chat, lambda pending: None)
            for item, chat in ((first, "chat-one"), (second, "chat-two"))]
    return {"both_started": True, "cross_chat_reply": cross,
            "outcomes": [(end.get("outcome") or {}).get("run_outcome") for end in ends]}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--model", default="haiku")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--only", choices=("question", "hold", "agent", "shell", "readonly", "chats"), action="append")
    args = parser.parse_args()
    boundary = cli_env.sandbox_works()
    report: dict[str, Any] = {"sandbox": boundary, "cli": cli_env.check(Path.cwd())["cli"], "scenarios": {}}
    if not boundary["works"]:
        report["blocked"] = "no working sandbox-exec boundary here; run on the host outside any other sandbox"
        args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2))
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 2
    base = Path(tempfile.mkdtemp(prefix="cco-acceptance-"))
    runtime = Runtime(base / "state")
    scenarios = {"question": lambda: question_update(runtime, fixture(base / "q"), args.model),
                 "hold": lambda: background_hold(runtime, fixture(base / "h"), args.model),
                 "agent": lambda: agent_stop(runtime, fixture(base / "a"), args.model),
                 "shell": lambda: leftover_shell(runtime, fixture(base / "s"), args.model),
                 "readonly": lambda: readonly_boundary(runtime, base, args.model),
                 "chats": lambda: same_task_two_chats(runtime, fixture(base / "c"), args.model)}
    for name, scenario in scenarios.items():
        if args.only and name not in args.only:
            continue
        (base / name[0]).mkdir(exist_ok=True)
        try:
            report["scenarios"][name] = scenario()
        except Exception as exc:  # each scenario's failure is evidence, not a reason to skip the rest
            report["scenarios"][name] = {"error": f"{type(exc).__name__}: {exc}"}
        args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    report["state_root"] = str(base / "state")
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
