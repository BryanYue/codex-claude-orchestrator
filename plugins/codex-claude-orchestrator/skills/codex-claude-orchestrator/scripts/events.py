"""Append-only, non-sensitive run activity with a rebuildable cursor index."""
from __future__ import annotations

import fcntl
import json
import os
import secrets
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any


EVENTS_NAME = "activity.jsonl"
INDEX_NAME = "activity.index.json"
INDEX_SCHEMA = 2
CHECKPOINT_INTERVAL = 128
_READ_CACHE: OrderedDict = OrderedDict()


def meaningful(event: dict[str, Any]) -> bool:
    # Provider counters stay in the full log, but are not user milestones.
    return event.get("kind") != "provider"


def _clean(value: Any, limit: int = 480) -> str:
    text = str(value).replace("\x00", " ").replace("\r", " ").replace("\n", " ").strip()
    return text[:limit] + ("…" if len(text) > limit else "")


def _events_path(run_dir: Path) -> Path:
    return run_dir / EVENTS_NAME


def _index_path(run_dir: Path) -> Path:
    return run_dir / INDEX_NAME


def _empty_index() -> dict[str, Any]:
    return {"schema": INDEX_SCHEMA, "offset": 0, "count": 0, "last_seq": 0,
            "last_event": None, "last_meaningful_event": None, "checkpoints": []}


def _valid_last_event(value: Any) -> bool:
    """Check the minimal durable event shape stored in an index snapshot."""
    return (isinstance(value, dict)
            and isinstance(value.get("seq"), int) and value["seq"] >= 1
            and isinstance(value.get("received_at"), (int, float))
            and not isinstance(value.get("received_at"), bool)
            and isinstance(value.get("kind"), str)
            and isinstance(value.get("summary"), str))


def _valid_index(value: Any, size: int) -> bool:
    if not isinstance(value, dict) or value.get("schema") != INDEX_SCHEMA:
        return False
    if not all(isinstance(value.get(key), int) and value[key] >= 0
               for key in ("offset", "count", "last_seq")):
        return False
    if value["offset"] > size or not isinstance(value.get("checkpoints"), list):
        return False
    if "last_event" not in value:
        return False
    last_event = value["last_event"]
    if "last_meaningful_event" not in value:
        return False
    milestone = value["last_meaningful_event"]
    if milestone is not None and (not _valid_last_event(milestone) or not meaningful(milestone) or milestone["seq"] > value["last_seq"]):
        return False
    if value["count"] == 0:
        if value["last_seq"] != 0 or last_event is not None:
            return False
    elif not _valid_last_event(last_event) or last_event["seq"] != value["last_seq"]:
        return False
    previous = -1
    for checkpoint in value["checkpoints"]:
        if (not isinstance(checkpoint, list) or len(checkpoint) != 2
                or not all(isinstance(item, int) and item >= 0 for item in checkpoint)
                or checkpoint[0] <= previous or checkpoint[1] > value["offset"]):
            return False
        previous = checkpoint[0]
    return True


def _load_index(run_dir: Path, size: int) -> dict[str, Any] | None:
    try:
        value = json.loads(_index_path(run_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if _valid_index(value, size) else None


def _write_index(run_dir: Path, value: dict[str, Any]) -> None:
    target = _index_path(run_dir)
    temporary = target.with_name(f".{target.name}.{secrets.token_hex(6)}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def _copy_index(value: dict[str, Any]) -> dict[str, Any]:
    return {"schema": INDEX_SCHEMA, "offset": value["offset"], "count": value["count"],
            "last_seq": value["last_seq"], "last_event": value["last_event"],
            "last_meaningful_event": value["last_meaningful_event"],
            "checkpoints": [list(item) for item in value["checkpoints"]]}


def _scan(handle, start: int, state: dict[str, Any]) -> tuple[int, bool]:
    """Advance *state* through complete lines and return (complete_end, partial_tail)."""
    handle.seek(start)
    raw = handle.read()
    cursor = start
    partial_tail = False
    for line in raw.splitlines(keepends=True):
        if not line.endswith(b"\n"):
            partial_tail = True
            break
        next_cursor = cursor + len(line)
        try:
            event = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            cursor = next_cursor
            continue
        if isinstance(event, dict) and isinstance(event.get("seq"), int) and event["seq"] >= 1:
            state["count"] += 1
            state["last_seq"] = max(state["last_seq"], event["seq"])
            state["last_event"] = event
            if meaningful(event):
                state["last_meaningful_event"] = event
            if not state["checkpoints"] or event["seq"] % CHECKPOINT_INTERVAL == 0:
                state["checkpoints"].append([event["seq"], cursor])
        cursor = next_cursor
    state["offset"] = cursor
    return cursor, partial_tail


def _state_for_handle(run_dir: Path, handle, *, repair_partial: bool) -> dict[str, Any]:
    handle.seek(0, os.SEEK_END)
    size = handle.tell()
    current = _load_index(run_dir, size)
    if current is None:
        current = _empty_index()
        complete_end, partial = _scan(handle, 0, current)
    else:
        current = _copy_index(current)
        complete_end, partial = _scan(handle, current["offset"], current)
    if partial and repair_partial:
        # A writer crash may leave an unterminated JSON line. Retain every
        # complete event and remove only bytes that can never form an event.
        handle.truncate(complete_end)
        handle.flush()
        os.fsync(handle.fileno())
    return current


def _checkpoint_offset(state: dict[str, Any], after: int) -> int:
    offset = 0
    for sequence, candidate in state["checkpoints"]:
        if sequence > after:
            break
        offset = candidate
    return offset


def append(run_dir: Path, kind: str, summary: str, *, tool: str | None = None,
           file: str | None = None, status: str | None = None, text: str | None = None,
           tool_use_id: str | None = None) -> dict[str, Any]:
    """Append one public activity event and atomically advance its index.

    JSONL remains authoritative. If its index is absent or stale after a crash,
    the next writer rebuilds it from complete records.
    """
    run_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = _events_path(run_dir)
    with path.open("a+b") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        state = _state_for_handle(run_dir, handle, repair_partial=True)
        sequence = state["last_seq"] + 1
        event: dict[str, Any] = {"seq": sequence, "received_at": time.time(),
                                 "kind": _clean(kind, 64), "summary": _clean(summary)}
        if tool:
            event["tool"] = _clean(tool, 80)
        if file:
            event["file"] = _clean(file, 1024)
        if status:
            event["status"] = _clean(status, 64)
        if text:
            event["text"] = _clean(text)
        if tool_use_id:
            event["tool_use_id"] = _clean(tool_use_id, 160)
        handle.seek(0, os.SEEK_END)
        start = handle.tell()
        handle.write((json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8"))
        handle.flush()
        os.fsync(handle.fileno())
        state["count"] += 1
        state["last_seq"] = sequence
        state["last_event"] = event
        if meaningful(event):
            state["last_meaningful_event"] = event
        if not state["checkpoints"] or sequence % CHECKPOINT_INTERVAL == 0:
            state["checkpoints"].append([sequence, start])
        state["offset"] = handle.tell()
        _write_index(run_dir, state)
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    return event


def _current_state(run_dir: Path) -> dict[str, Any] | None:
    path = _events_path(run_dir)
    if not path.exists():
        return None
    with path.open("rb") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_SH)
        stat = os.fstat(handle.fileno())
        try:
            index_stat = _index_path(run_dir).stat()
            index_key = (index_stat.st_ino, index_stat.st_size, index_stat.st_mtime_ns, index_stat.st_ctime_ns)
        except FileNotFoundError:
            index_key = None
        key = (str(path), stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns, index_key)
        # A viewer rebuilds old indexes in memory without changing evidence.
        cached = _READ_CACHE.get(key)
        if cached is not None:
            return _copy_index(cached)
        state = _state_for_handle(run_dir, handle, repair_partial=False)
        _READ_CACHE[key] = _copy_index(state)
        while len(_READ_CACHE) > 128:
            _READ_CACHE.popitem(last=False)
        return state


def latest(run_dir: Path) -> int:
    """Return the newest durable public sequence without paging the log."""
    state = _current_state(run_dir)
    return state["last_seq"] if state else 0


def read(run_dir: Path, after: int = 0, limit: int = 100) -> tuple[list[dict[str, Any]], int, bool]:
    if after < 0 or limit < 1 or limit > 1000:
        raise ValueError("after must be non-negative and limit must be 1..1000")
    path = _events_path(run_dir)
    if not path.exists():
        return [], after, False
    with path.open("rb") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_SH)
        state = _state_for_handle(run_dir, handle, repair_partial=False)
        if after >= state["last_seq"]:
            return [], after, False
        handle.seek(_checkpoint_offset(state, after))
        events: list[dict[str, Any]] = []
        more = False
        for raw in handle:
            # An unterminated final line is a writer-crash tail.  It might be
            # valid JSON already, but it was never committed as a JSONL event.
            if not raw.endswith(b"\n"):
                break
            try:
                event = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if not isinstance(event, dict) or not isinstance(event.get("seq"), int) or event["seq"] <= after:
                continue
            if len(events) == limit:
                more = True
                break
            events.append(event)
    cursor = events[-1]["seq"] if events else after
    return events, cursor, more


def count(run_dir: Path) -> int:
    state = _current_state(run_dir)
    return state["count"] if state else 0


def statistics(run_dir: Path) -> tuple[int, dict[str, Any] | None]:
    """Return total valid events and newest public event without pagination."""
    state = _current_state(run_dir)
    if not state:
        return 0, None
    return state["count"], state["last_event"]


def latest_meaningful(run_dir: Path) -> dict[str, Any] | None:
    state = _current_state(run_dir)
    return state["last_meaningful_event"] if state else None
