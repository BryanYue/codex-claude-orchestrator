"""A deterministic stand-in for the Claude CLI used by end-to-end tests.

FAKE_SCRIPT is a JSON list of actions executed in the working directory;
FAKE_PROBES names a directory outside every protected path where write
probes record whether the OS allowed them.
"""
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

FLAGS = ("-p --model --effort --output-format --verbose --json-schema --session-id --resume --permission-mode "
         "--tools --allowedTools --disallowedTools --strict-mcp-config --mcp-config --disable-slash-commands "
         "--max-budget-usd --no-session-persistence")


def main() -> int:
    args = sys.argv[1:]
    if args == ["--version"]:
        print("2.1.292 (Claude Code fixture)")
        return 0
    if args == ["--help"]:
        print(os.environ.get("FAKE_HELP", FLAGS))
        return 0
    if args[:3] == ["auth", "status", "--json"]:
        print(json.dumps({"loggedIn": os.environ.get("FAKE_LOGGED_IN", "1") == "1", "authMethod": "claude.ai",
                          "email": "someone@example.test"}))
        return 0

    def option(name):
        return args[args.index(name) + 1] if name in args else None

    session = option("--resume") or option("--session-id")
    if option("--resume") and os.environ.get("FAKE_RESUME_FAIL") == "1":
        print("No conversation found with session ID", file=sys.stderr)
        return 1
    brief = sys.stdin.buffer.read()
    text = brief.decode("utf-8")
    probes = Path(os.environ.get("FAKE_PROBES", "/nonexistent"))

    def emit(event):
        print(json.dumps({"session_id": session, **event}, ensure_ascii=False), flush=True)

    def tool(name, value, ident):
        emit({"type": "assistant", "parent_tool_use_id": None,
              "message": {"model": "fixture-model", "content": [{"type": "tool_use", "id": ident, "name": name, "input": value}]}})
        emit({"type": "user", "parent_tool_use_id": None,
              "message": {"content": [{"type": "tool_result", "tool_use_id": ident, "content": "ok"}]}})

    emit({"type": "system", "subtype": "init", "model": "fixture-model", "cwd": os.getcwd(),
          "tools": ["Read", "Bash"], "plugins": [{"name": "fixture-plugin"}], "memory_paths": {}})
    outboxes = re.findall(r"`([^`]+)/report\.md`", text)
    outbox = outboxes[-1] if outboxes else None
    structured = None
    denials = []
    result = True
    exit_code = 0
    counter = 0
    for action in json.loads(os.environ.get("FAKE_SCRIPT", "[]")):
        counter += 1
        kind = next(iter(action))
        if kind == "write":
            path = Path(action["write"])
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(action.get("text", ""), encoding="utf-8")
        elif kind == "binary":
            Path(action["binary"]).write_bytes(bytes.fromhex(action["hex"]))
        elif kind == "delete":
            Path(action["delete"]).unlink()
        elif kind == "attempt_write":
            target = Path(action["attempt_write"])
            try:
                with target.open("a") as handle:
                    handle.write("tampered")
                outcome = "allowed"
            except OSError as exc:
                outcome = type(exc).__name__
            probes.mkdir(parents=True, exist_ok=True)
            (probes / f"{counter}.json").write_text(json.dumps({"target": str(target), "outcome": outcome}))
        elif kind == "capture_brief":
            Path(action["capture_brief"]).write_bytes(brief)
        elif kind == "read_inputs":
            for ident, path in enumerate(re.findall(r"- `([^`]+)`（原路径", text)):
                tool("Read", {"file_path": path}, f"read-{ident}")
        elif kind == "tool":
            tool(action["tool"], action.get("input", {}), f"tool-{counter}")
        elif kind == "workflow":
            ident = f"workflow-{counter}"
            tool("Workflow", {"name": "codex-analyze", "args": {}}, ident)
            emit({"type": "system", "subtype": "task_started", "tool_use_id": ident, "task_id": f"task-{counter}"})
            if action["workflow"] != "pending":
                emit({"type": "system", "subtype": "task_notification", "tool_use_id": ident,
                      "task_id": f"task-{counter}", "status": action["workflow"]})
        elif kind == "deliver":
            assert outbox, "brief names no outbox"
            folder = Path(outbox)
            if "report" in action:
                (folder / "report.md").write_text(action["report"], encoding="utf-8")
            if "result" in action:
                value = action["result"]
                (folder / "result.json").write_text(value if isinstance(value, str) else json.dumps(value, ensure_ascii=False),
                                                    encoding="utf-8")
        elif kind == "structured":
            structured = action["structured"]
        elif kind == "denial":
            denials.append({"tool_name": action["denial"], "tool_use_id": f"denied-{counter}",
                            "tool_input": {"url": "https://example.test"}})
        elif kind == "sleep":
            time.sleep(action["sleep"])
        elif kind == "touch":
            Path(action["touch"]).write_text("ready")
        elif kind == "wait_for":
            deadline = time.monotonic() + 15
            while not Path(action["wait_for"]).exists() and time.monotonic() < deadline:
                time.sleep(.02)
        elif kind == "background":
            subprocess.Popen([sys.executable, "-c", f"import time; time.sleep({action['background']})"],
                             start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        elif kind == "no_result":
            result = False
            exit_code = action["no_result"]
    if result:
        event = {"type": "result", "subtype": "success", "is_error": False, "parent_tool_use_id": None,
                 "result": "Delivered as requested.", "total_cost_usd": 0.0123, "permission_denials": denials,
                 "usage": {"input_tokens": 10, "output_tokens": 20, "cache_read_input_tokens": 0,
                           "cache_creation_input_tokens": 0},
                 "modelUsage": {"fixture-model": {"inputTokens": 10, "outputTokens": 20, "cacheReadInputTokens": 0,
                                                  "cacheCreationInputTokens": 0, "costUSD": 0.0123}}}
        if structured is not None:
            event["structured_output"] = structured
        emit(event)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
