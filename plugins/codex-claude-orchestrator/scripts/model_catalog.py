"""Read the selected CLI's advertised models without sending a user message.

Use the initialize control exchange used by Anthropic's Agent SDK. Model aliases
and provider-specific resolution remain owned by Claude, never a local version
table. This is metadata evidence, not a successful model invocation.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import selectors
import subprocess
import time
import uuid

from executable_locator import cli_environment, locate_claude


def _exchange(proc: subprocess.Popen, request: dict, timeout: float) -> bytes:
    """Bound both streams while waiting, including descendants retaining pipes."""
    proc.stdin.write((json.dumps(request) + "\n").encode())
    proc.stdin.close()
    output = bytearray()
    total = 0
    deadline = time.monotonic() + timeout
    with selectors.DefaultSelector() as streams:
        for stream in (proc.stdout, proc.stderr):
            streams.register(stream, selectors.EVENT_READ)
        while streams.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(proc.args, timeout)
            for key, _ in streams.select(min(remaining, .1)):
                chunk = os.read(key.fileobj.fileno(), 65536)
                if not chunk:
                    streams.unregister(key.fileobj)
                    continue
                total += len(chunk)
                if total > 2_000_000:
                    raise ValueError("CLI model catalog response exceeded the limit")
                if key.fileobj is proc.stdout:
                    output.extend(chunk)
    proc.wait(timeout=max(.001, deadline - time.monotonic()))
    return bytes(output)


def _stop(proc: subprocess.Popen) -> str:
    """A signal request is not proof that the process group has stopped."""
    try:
        for sig in (signal.SIGTERM, signal.SIGKILL):
            proc.poll()  # Reap the direct child before checking its group.
            try:
                os.killpg(proc.pid, 0)
            except ProcessLookupError:
                return "confirmed"
            os.killpg(proc.pid, sig)
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                proc.poll()
                try:
                    os.killpg(proc.pid, 0)
                except ProcessLookupError:
                    return "confirmed"
                time.sleep(.02)
    except OSError:
        return "unconfirmed"
    return "unconfirmed"


def _models(response: object) -> list[dict]:
    if not isinstance(response, list) or not response or len(response) > 200:
        raise ValueError("CLI did not provide a usable model catalog")
    models = []
    seen = set()
    for item in response:
        if not isinstance(item, dict):
            raise ValueError("CLI model catalog entry is malformed")
        value = item.get("value")
        if not isinstance(value, str) or not value.strip() or len(value) > 300 or value in seen:
            raise ValueError("CLI model catalog selector is malformed or duplicated")
        seen.add(value)
        model = {"value": value}
        # The initialize response also contains account information. Export
        # only this explicit model metadata allowlist, never the whole response.
        for key in ("resolvedModel", "displayName", "description"):
            if isinstance(item.get(key), str):
                model[key] = item[key][:600]
        for key in ("supportsEffort", "supportsAdaptiveThinking", "supportsFastMode", "supportsAutoMode"):
            if type(item.get(key)) is bool:
                model[key] = item[key]
        levels = item.get("supportedEffortLevels")
        if isinstance(levels, list) and all(isinstance(level, str) for level in levels):
            model["supportedEffortLevels"] = [level[:40] for level in levels[:20]]
        models.append(model)
    return models


def collect(cwd: str, *, timeout: float = 20) -> dict:
    folder = Path(cwd)
    if not folder.is_absolute() or not folder.is_dir():
        raise ValueError("cwd must be an existing absolute directory")
    decision = locate_claude()
    result = {"source": "claude_cli_initialize", "checked_at": time.time(),
              "cli": {"path": decision.get("path"), "source": decision.get("source")},
              "status": "unavailable", "models": [], "model_request_sent": False,
              "remote_invocation_verified": False,
              "note": "CLI-advertised metadata for the local Claude CLI and its configuration. Aliases follow Claude/provider configuration; only a real run proves access and the actual model."}
    if not decision.get("path"):
        return {**result, "reason": "cli_unavailable"}
    request_id = "model-catalog-" + uuid.uuid4().hex
    request = {"type": "control_request", "request_id": request_id,
               "request": {"subtype": "initialize"}}
    command = [decision["path"], "-p", "--input-format", "stream-json", "--output-format", "stream-json",
               "--verbose", "--no-session-persistence", "--tools", "", "--strict-mcp-config",
               "--mcp-config", '{"mcpServers":{}}', "--disable-slash-commands"]
    proc = None
    try:
        proc = subprocess.Popen(command, cwd=folder, env=cli_environment(decision),
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                start_new_session=True)
        stdout = _exchange(proc, request, timeout)
        if proc.returncode:
            result.update(reason="cli_initialize_failed", exit_code=proc.returncode)
            return result
        for line in stdout.splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not isinstance(event, dict) or event.get("type") != "control_response":
                continue
            response = event.get("response")
            if not isinstance(response, dict) or response.get("request_id") != request_id:
                continue
            payload = response.get("response")
            if response.get("subtype") != "success" or not isinstance(payload, dict):
                result["reason"] = "cli_initialize_rejected"
                return result
            result.update(status="advertised", models=_models(payload.get("models")))
            return result
        result["reason"] = "model_catalog_missing"
        return result
    except subprocess.TimeoutExpired:
        result["reason"] = "model_catalog_timeout"
        return result
    except (OSError, ValueError):
        # Do not leak raw CLI errors: they can contain account or proxy details.
        result["reason"] = "model_catalog_unavailable"
        return result
    finally:
        if proc is not None:
            result["cleanup_status"] = _stop(proc)
            if result["cleanup_status"] != "confirmed":
                result.update(status="unavailable", models=[],
                              cleanup_reason="process_group_stop_unconfirmed",
                              next_action="Check the model catalog CLI process before retrying.")
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                try:
                    stream.close()
                except OSError:
                    pass
