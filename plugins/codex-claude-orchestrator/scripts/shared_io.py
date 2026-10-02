"""Small filesystem primitives with explicit, shared serialization semantics."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any
import uuid


def dump_json(path: Path, value: Any) -> None:
    """Replace a JSON record atomically; preserve the previous record on failure."""
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=True, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def read_regular(path: Path, limit: int) -> bytes:
    """Bounded nonblocking read; symlinks and FIFOs never reach a blocking read."""
    import stat
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
    except OSError as exc:
        raise ValueError(f"cannot open {path.name} as a regular non-symlink file: {exc.strerror or exc}") from exc
    with os.fdopen(fd, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise ValueError(f"{path.name} is not a regular file")
        data = handle.read(limit + 1)
    if len(data) > limit:
        raise ValueError(f"{path.name} exceeds its byte limit")
    return data
