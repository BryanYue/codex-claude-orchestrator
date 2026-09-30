#!/usr/bin/env python3
"""Run plugin regressions with a throwaway coordination TMPDIR.

Production Runtime and bridge processes retain their legacy coordination paths.
This entrypoint changes only the environment passed to fresh test subprocesses,
so test-created lane locks and unknown markers cannot accumulate in the user's
normal TMPDIR.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from typing import Callable, Sequence


ROOT = Path(__file__).resolve().parents[1]
SUITE_DIRECTORIES = {
    "plugin": Path("tests"),
    "bridge": Path("skills/codex-claude-orchestrator/scripts/tests"),
}


def selected_suites(suite: str) -> tuple[str, ...]:
    if suite == "all":
        return tuple(SUITE_DIRECTORIES)
    if suite in SUITE_DIRECTORIES:
        return (suite,)
    raise ValueError(f"unknown suite: {suite}")


def command_for(root: Path, suite: str) -> list[str]:
    directory = root / SUITE_DIRECTORIES[suite]
    return [sys.executable, "-m", "unittest", "discover", "-s", str(directory)]


def run_suites(root: Path, suite: str, *, execute: Callable[..., subprocess.CompletedProcess] = subprocess.run,
               temporary_directory: Callable[..., str] = tempfile.mkdtemp) -> int:
    """Run selected suites, preserving output and waiting for every child.

    ``TMPDIR`` is copied into the child environment only.  The runner does not
    mutate ``os.environ`` and never deletes a production coordination directory.
    """
    root = root.resolve()
    isolated_tmp = Path(temporary_directory(prefix="codex-claude-test-coordination-"))
    first_failure = 0
    try:
        environment = dict(os.environ)
        environment["TMPDIR"] = str(isolated_tmp)
        environment["CLAUDE_ORCHESTRATOR_CLI_ROOT"] = str(isolated_tmp / "cli-store")
        for name in selected_suites(suite):
            try:
                completed = execute(command_for(root, name), cwd=root, env=environment, check=False)
            except OSError as error:
                print(f"Unable to start {name} regression suite: {error}", file=sys.stderr)
                if not first_failure:
                    first_failure = 127
                continue
            if completed.returncode and not first_failure:
                first_failure = completed.returncode
        return first_failure
    finally:
        # Every subprocess above is synchronously awaited by subprocess.run
        # before the only owned directory is removed.
        shutil.rmtree(isolated_tmp, ignore_errors=False)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run Codex-Claude plugin tests with an isolated TMPDIR")
    parser.add_argument("--suite", choices=("plugin", "bridge", "all"), default="all")
    args = parser.parse_args(argv)
    return run_suites(ROOT, args.suite)


if __name__ == "__main__":
    raise SystemExit(main())
