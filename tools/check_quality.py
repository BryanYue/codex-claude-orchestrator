#!/usr/bin/env python3
"""Run pinned lint and focused type checks; enforce baseline complexity ceilings and report structural metrics.

From the repository root:
  uv run --project plugins/codex-claude-orchestrator --group dev --frozen python tools/check_quality.py
Use --metrics-only for a standard-library-only structural snapshot.
"""
from __future__ import annotations

import argparse
import ast
from pathlib import Path
import subprocess
import sys
from typing import Sequence

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugins/codex-claude-orchestrator"
CONFIG = PLUGIN / "pyproject.toml"


def structural_metrics() -> dict[str, list[tuple[int, str]]]:
    files = sorted((PLUGIN / "scripts").glob("*.py")) + sorted(
        (PLUGIN / "skills/codex-claude-orchestrator/scripts").glob("*.py"))
    modules: list[tuple[int, str]] = []
    functions: list[tuple[int, str]] = []
    for path in files:
        source = path.read_bytes()
        relative = path.relative_to(ROOT).as_posix()
        modules.append((len(source.splitlines()), relative))
        tree = ast.parse(source, filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                functions.append(((node.end_lineno or node.lineno) - node.lineno + 1,
                                  f"{relative}:{node.lineno} {node.name}"))
    return {"modules": sorted(modules, reverse=True), "functions": sorted(functions, reverse=True)}


def print_metrics() -> None:
    metrics = structural_metrics()
    print("Structural snapshot (line counts informational; Ruff enforces baseline complexity/branch/statement ceilings):", flush=True)
    for kind in ("modules", "functions"):
        print(f"Largest {kind}:", flush=True)
        for lines, source in metrics[kind][:5]:
            print(f"  {lines:5d} lines  {source}", flush=True)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--metrics-only", action="store_true", help="Report source structure without third-party tools")
    args = parser.parse_args(argv)
    print_metrics()
    if args.metrics_only:
        return 0
    commands = [
        ([sys.executable, "-m", "ruff", "check", "--config", str(CONFIG), str(PLUGIN), str(ROOT / "tools")], ROOT),
        ([sys.executable, "-m", "mypy", "--config-file", str(CONFIG)], PLUGIN),
    ]
    first_failure = 0
    for command, working_directory in commands:
        completed = subprocess.run(command, cwd=working_directory, check=False)
        if completed.returncode and not first_failure:
            first_failure = completed.returncode
    return first_failure


if __name__ == "__main__":
    raise SystemExit(main())
