"""Shared fixture: a Git source repository, a fake Claude CLI and an isolated state root."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from typing import Any
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
from runtime import Runtime  # noqa: E402


def git(cwd: Path, *args: str) -> str:
    result = subprocess.run(["git", "-c", "core.hooksPath=/dev/null", *args], cwd=cwd, capture_output=True, text=True)
    if result.returncode:
        raise AssertionError(result.stderr)
    return result.stdout


class Fixture:
    def __init__(self, test: Any):
        self.tmp = tempfile.TemporaryDirectory(prefix="orchestrator 引用 ")
        test.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.source = self.root / "source"
        self.source.mkdir()
        git(self.source, "init", "-q")
        git(self.source, "config", "user.email", "test@example.invalid")
        git(self.source, "config", "user.name", "Fixture")
        (self.source / "src").mkdir()
        (self.source / "src" / "app.py").write_text("print('v1')\n")
        (self.source / "old.txt").write_text("remove me\n")
        (self.source / ".gitignore").write_text("build/\n")
        git(self.source, "add", ".")
        git(self.source, "commit", "-qm", "base")
        self.spec = self.root / "docs" / "spec.md"
        self.spec.parent.mkdir()
        self.spec.write_text("# 规格\n导出必须异步。\n")
        self.state = self.root / "state"
        self.probes = self.root / "probes"
        self.config = self.root / "claude-config"
        self.config.mkdir()
        (self.config / "CLAUDE.md").write_text("默认中文回复\n")
        (self.config / "settings.json").write_text(json.dumps({"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "gate.sh"}]}]}}))
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        self.cli = bin_dir / "claude"
        self.cli.write_text(f"#!{sys.executable}\n" + (Path(__file__).parent / "fake_claude.py").read_text())
        self.cli.chmod(0o755)
        self.environ = {"CLAUDE_BIN": str(self.cli), "CLAUDE_CONFIG_DIR": str(self.config),
                        "CLAUDE_ORCHESTRATOR_SETTINGS_PATH": str(self.root / "plugin-settings.json"),
                        "FAKE_PROBES": str(self.probes), "FAKE_SCRIPT": "[]"}
        patcher = mock.patch.dict(os.environ, self.environ)
        patcher.start()
        test.addCleanup(patcher.stop)
        self.runtime = Runtime(self.state)
        test.addCleanup(self.runtime.close)

    def packet(self, **changes: Any) -> dict[str, Any]:
        value = {"task_id": "T-1", "kind": "implement", "cwd": str(self.source), "model": "sonnet", "effort": "low",
                 "timeout_seconds": 60, "user_messages": [{"text": "把导出改成异步，失败要提示原因", "source": "human"}],
                 "inputs": [str(self.spec)], "done_when": [{"text": "测试通过", "origin": "user"}]}
        value.update(changes)
        return value

    def script(self, *actions: Any) -> None:
        os.environ["FAKE_SCRIPT"] = json.dumps(list(actions), ensure_ascii=False)

    def wait(self, run_id: str, timeout: float = 60) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            snapshot = self.runtime.snapshot(run_id)
            if snapshot["status"] != "running":
                return snapshot
            time.sleep(.1)
        raise AssertionError(f"{run_id} did not finish: {self.runtime.snapshot(run_id)}")

    def run(self, *actions: Any, **packet_changes: Any) -> dict[str, Any]:
        self.script(*actions)
        return self.wait(self.runtime.start(self.packet(**packet_changes))["run_id"])

    def run_dir(self, snapshot: dict[str, Any]) -> Path:
        return Path(snapshot["run_dir"])

    def warnings(self, snapshot: dict[str, Any]) -> dict[str, dict[str, Any]]:
        return {warning["code"]: warning for warning in snapshot["outcome"]["warnings"]}

    def probes_seen(self) -> dict[str, str]:
        if not self.probes.is_dir():
            return {}
        return {value["target"]: value["outcome"] for value in
                (json.loads(path.read_text()) for path in sorted(self.probes.glob("*.json")))}
