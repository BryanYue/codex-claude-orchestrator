"""Bounded Git observations without user-configured executable callbacks."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess


# Resolve before launching an untrusted child, never from its later PATH/config.
GIT = "/usr/bin/git" if Path("/usr/bin/git").is_file() else shutil.which("git") or "git"


def run(cwd: Path, *args: str, **kwargs) -> subprocess.CompletedProcess:
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    environment.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, GIT_TERMINAL_PROMPT="0", LC_ALL="C")
    command = [GIT, "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false",
               "-c", "core.untrackedCache=false", "-c", "init.templateDir=", "-C", str(cwd), *args]
    kwargs.setdefault("capture_output", True)
    kwargs.setdefault("timeout", 30)
    return subprocess.run(command, env=environment, **kwargs)
