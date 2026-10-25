"""Transactional control records for interactive runs: ownership, commands, interactions, workbench openings.

Large logs and artifacts stay in run directories; this store keeps only the
facts that must change together.  It uses SQLite's rollback journal
(``journal_mode=DELETE``, ``synchronous=FULL``): the local SQLite predates the
WAL-reset fix, so WAL is never enabled and the chosen mode is verified when
the store opens.  Each mutation is one ``BEGIN IMMEDIATE`` transaction, so a
command, its receipt, the interaction it resolves, the revision bump and its
delivery row either all exist or none do.

Runs recorded before 1.1 have no rows here; they stay readable through the
v1 registry and are never adopted.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import re
import secrets
import sqlite3
import time
from typing import Any, Iterator

SCHEMA_VERSION = 1
CHAT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
NAMESPACE = CHAT
COMMAND_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
COMMAND_KINDS = ("answer", "update", "interrupt")
ORIGINS = ("coordinator", "human")
SURFACE = "codex"
INTERACTION_KINDS = ("question", "permission", "note", "dispute")
DELIVERY_STATES = ("queued", "written", "echoed", "failed")
# Delivery evidence only moves forward: a later echo is never overwritten by an earlier write checkpoint.
_FORWARD = {"written": ("queued",), "echoed": ("queued", "written"), "failed": ("queued", "written")}
OPEN_OUTCOMES = ("queued", "failed", "unknown")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS bindings (
  namespace TEXT NOT NULL, chat TEXT NOT NULL, source TEXT NOT NULL, created_at REAL NOT NULL,
  last_seen_at REAL NOT NULL, PRIMARY KEY (namespace, chat));
CREATE TABLE IF NOT EXISTS runs (
  run_id TEXT PRIMARY KEY, namespace TEXT, chat TEXT, task_id TEXT NOT NULL, owner TEXT NOT NULL,
  attempt INTEGER NOT NULL, interactive INTEGER NOT NULL, revision INTEGER NOT NULL, state TEXT NOT NULL,
  created_at REAL NOT NULL, updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS commands (
  command_id TEXT NOT NULL, run_id TEXT NOT NULL, kind TEXT NOT NULL, payload_sha256 TEXT NOT NULL,
  payload TEXT NOT NULL, origin TEXT NOT NULL, interaction_id TEXT, revision INTEGER NOT NULL,
  delivery TEXT NOT NULL, delivery_note TEXT, created_at REAL NOT NULL, written_at REAL, echoed_at REAL,
  sequence INTEGER PRIMARY KEY AUTOINCREMENT, UNIQUE (run_id, command_id));
CREATE TABLE IF NOT EXISTS interactions (
  interaction_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, kind TEXT NOT NULL, blocking INTEGER NOT NULL,
  status TEXT NOT NULL, request TEXT NOT NULL, request_id TEXT, tool_use_id TEXT, hold TEXT,
  answer TEXT, answered_by TEXT, answer_command TEXT, answer_revision INTEGER,
  created_at REAL NOT NULL, closed_at REAL);
CREATE TABLE IF NOT EXISTS openings (
  namespace TEXT NOT NULL, chat TEXT NOT NULL, surface TEXT NOT NULL, state TEXT NOT NULL, attempts INTEGER NOT NULL,
  reserved_at REAL NOT NULL, outcome_at REAL, PRIMARY KEY (namespace, chat, surface));
CREATE INDEX IF NOT EXISTS interactions_run ON interactions (run_id, status);
CREATE INDEX IF NOT EXISTS commands_delivery ON commands (run_id, delivery);
"""


class StoreError(RuntimeError):
    """A control request that the store refuses; the message is safe to show to Codex."""


class CommandConflict(StoreError):
    pass


class StaleRevision(StoreError):
    pass


def payload_digest(payload: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode()).hexdigest()


def _dumps(value: Any) -> str:
    # ASCII escapes keep text the CLI decoded into lone surrogates storable as UTF-8.
    return json.dumps(value, ensure_ascii=True)


def _loads(value: str | None) -> Any:
    return json.loads(value) if value else None


class ControlStore:
    """Open per call; every connection verifies the journal arrangement it relies on."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        db = self._connect()
        try:
            db.executescript(_SCHEMA)
            db.execute("INSERT OR IGNORE INTO meta VALUES ('schema', ?)", (str(SCHEMA_VERSION),))
            found = db.execute("SELECT value FROM meta WHERE key='schema'").fetchone()[0]
        finally:
            db.close()
        if found != str(SCHEMA_VERSION):
            raise StoreError(f"control store schema {found} is not supported by this plugin (expects {SCHEMA_VERSION})")

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        db.row_factory = sqlite3.Row
        mode = db.execute("PRAGMA journal_mode=DELETE").fetchone()[0]
        if str(mode).lower() != "delete":
            db.close()
            raise StoreError(f"the control store could not use the rollback journal (journal_mode={mode})")
        db.execute("PRAGMA synchronous=FULL")
        return db

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            try:
                yield db
            except BaseException:
                db.execute("ROLLBACK")
                raise
            db.execute("COMMIT")
        finally:
            db.close()

    @contextmanager
    def _read(self) -> Iterator[sqlite3.Connection]:
        db = self._connect()
        try:
            yield db
        finally:
            db.close()

    def journal(self) -> dict[str, Any]:
        with self._read() as db:
            return {"sqlite_version": sqlite3.sqlite_version, "journal_mode": db.execute("PRAGMA journal_mode").fetchone()[0],
                    "synchronous": db.execute("PRAGMA synchronous").fetchone()[0], "wal": False}

    def bind(self, namespace: str, chat: str, source: str) -> None:
        now = time.time()
        with self._write() as db:
            db.execute("INSERT INTO bindings VALUES (?, ?, ?, ?, ?) ON CONFLICT (namespace, chat) "
                       "DO UPDATE SET last_seen_at=excluded.last_seen_at", (namespace, chat, source, now, now))

    def register_run(self, run_id: str, task_id: str, context: dict[str, str] | None, owner: str, interactive: bool) -> None:
        now = time.time()
        with self._write() as db:
            db.execute("INSERT INTO runs VALUES (?, ?, ?, ?, ?, 1, ?, 0, 'running', ?, ?)",
                       (run_id, (context or {}).get("namespace"), (context or {}).get("chat"), task_id, owner,
                        int(interactive), now, now))

    def run(self, run_id: str) -> dict[str, Any] | None:
        with self._read() as db:
            row = db.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
        return dict(row) if row else None

    def check_context(self, run_id: str, context: dict[str, Any] | None) -> dict[str, Any] | None:
        """Refuse a caller whose stated context differs from the run's owner; v1 runs have no owner row."""
        row = self.run(run_id)
        if row is None or context is None:
            return row
        if row["chat"] is not None and (row["namespace"], row["chat"]) != (context.get("namespace"), context.get("chat")):
            raise StoreError("this run belongs to another Codex chat; use the run_id your own chat started")
        return row

    def set_state(self, run_id: str, state: str, *, expect: tuple[str, ...] = ()) -> bool:
        """Move a run to *state*; with *expect*, only from one of those states.  Returns whether it moved."""
        with self._write() as db:
            row = db.execute("SELECT state FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if row is None or (expect and row["state"] not in expect):
                return False
            if state == "closing" and db.execute("SELECT 1 FROM commands WHERE run_id=? AND delivery='queued'",
                                                 (run_id,)).fetchone():
                return False
            db.execute("UPDATE runs SET state=?, updated_at=? WHERE run_id=?", (state, time.time(), run_id))
            if state in {"ended", "lost"}:
                db.execute("UPDATE interactions SET status='abandoned', closed_at=? WHERE run_id=? AND status='pending'",
                           (time.time(), run_id))
                db.execute("UPDATE commands SET delivery='failed', delivery_note='the run ended before delivery' "
                           "WHERE run_id=? AND delivery='queued'", (run_id,))
            return True

    def submit(self, run_id: str, command_id: str, kind: str, payload: dict[str, Any], origin: str,
               interaction_id: str | None = None, expected_revision: int | None = None,
               context: dict[str, str] | None = None) -> dict[str, Any]:
        """Record one Codex command and its durable receipt atomically; replays return the first receipt."""
        if not isinstance(command_id, str) or not COMMAND_ID.fullmatch(command_id):
            raise StoreError("command_id must be 1-128 characters of letters, digits and . _ : -")
        if kind not in COMMAND_KINDS:
            raise StoreError("kind must be answer, update or interrupt")
        if origin not in ORIGINS:
            raise StoreError("origin must be coordinator (Codex's own words) or human (the user's words, verbatim)")
        digest = payload_digest({"kind": kind, "payload": payload, "origin": origin, "interaction_id": interaction_id})
        now = time.time()
        with self._write() as db:
            run = self._owned(db, run_id, context)
            replay = self._replay(db, run_id, command_id, digest)
            if replay is not None:
                return replay
            if run["state"] not in {"running", "held"}:
                raise StoreError(f"the run no longer accepts input ({run['state']}); continue it with continue_from instead")
            if expected_revision is not None and expected_revision != run["revision"]:
                raise StaleRevision(f"expected_revision {expected_revision} is stale; the run is at revision {run['revision']}. "
                                    "Read the run's interactions again before answering")
            revision = run["revision"] + 1
            if kind == "answer":
                if not interaction_id:
                    raise StoreError("an answer needs the interaction_id it answers")
                pending = db.execute("SELECT * FROM interactions WHERE interaction_id=? AND run_id=?",
                                     (interaction_id, run_id)).fetchone()
                if pending is None:
                    raise StoreError("interaction_id was not found for this run")
                if pending["status"] != "pending":
                    raise StaleRevision(f"interaction {interaction_id} is already {pending['status']}"
                                        + (f" by command {pending['answer_command']}" if pending["answer_command"] else ""))
                db.execute("UPDATE interactions SET status='answered', answer=?, answered_by=?, answer_command=?, "
                           "answer_revision=?, closed_at=? WHERE interaction_id=?",
                           (_dumps(payload), origin, command_id, revision, now, interaction_id))
            elif interaction_id:
                raise StoreError("only an answer refers to an interaction_id")
            db.execute("UPDATE runs SET revision=?, updated_at=? WHERE run_id=?", (revision, now, run_id))
            db.execute("INSERT INTO commands (command_id, run_id, kind, payload_sha256, payload, origin, interaction_id, "
                       "revision, delivery, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?)",
                       (command_id, run_id, kind, digest, _dumps(payload), origin,
                        interaction_id, revision, now))
            row = db.execute("SELECT * FROM commands WHERE run_id=? AND command_id=?", (run_id, command_id)).fetchone()
            return self._receipt(row, duplicate=False)

    @staticmethod
    def _owned(db: sqlite3.Connection, run_id: str, context: dict[str, Any] | None) -> sqlite3.Row:
        run: sqlite3.Row | None = db.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
        if run is None:
            raise StoreError("this run has no interactive channel (it was started before 1.1 or is not interactive)")
        if context is not None and run["chat"] is not None and \
                (run["namespace"], run["chat"]) != (context.get("namespace"), context.get("chat")):
            raise StoreError("this run belongs to another Codex chat; use the run_id your own chat started")
        return run

    def _replay(self, db: sqlite3.Connection, run_id: str, command_id: str, digest: str) -> dict[str, Any] | None:
        existing = db.execute("SELECT * FROM commands WHERE run_id=? AND command_id=?", (run_id, command_id)).fetchone()
        if existing is None:
            return None
        if existing["payload_sha256"] != digest:
            raise CommandConflict(f"command_id {command_id} was already used with a different payload; "
                                  "use a new command_id for a different command")
        return self._receipt(existing, duplicate=True)

    def replay(self, run_id: str, command_id: str, kind: str, payload: dict[str, Any], origin: str,
               interaction_id: str | None = None, context: dict[str, Any] | None = None) -> dict[str, Any] | None:
        """The first receipt of an identical earlier command, also after the run ended; None when there is none."""
        digest = payload_digest({"kind": kind, "payload": payload, "origin": origin, "interaction_id": interaction_id})
        with self._read() as db:
            self._owned(db, run_id, context)
            return self._replay(db, run_id, command_id, digest)

    @staticmethod
    def _receipt(row: sqlite3.Row, *, duplicate: bool) -> dict[str, Any]:
        return {"command_id": row["command_id"], "run_id": row["run_id"], "kind": row["kind"], "origin": row["origin"],
                "interaction_id": row["interaction_id"], "revision": row["revision"], "duplicate": duplicate,
                "delivery": {"state": row["delivery"], "note": row["delivery_note"], "recorded_at": row["created_at"],
                             "written_at": row["written_at"], "echoed_at": row["echoed_at"]}}

    def command(self, run_id: str, command_id: str) -> dict[str, Any] | None:
        with self._read() as db:
            row = db.execute("SELECT * FROM commands WHERE run_id=? AND command_id=?", (run_id, command_id)).fetchone()
        return self._receipt(row, duplicate=False) if row else None

    def queued(self, run_id: str) -> list[dict[str, Any]]:
        with self._read() as db:
            rows = db.execute("SELECT * FROM commands WHERE run_id=? AND delivery='queued' ORDER BY sequence", (run_id,)).fetchall()
        return [{**dict(row), "payload": _loads(row["payload"])} for row in rows]

    def mark_delivery(self, run_id: str, command_id: str, state: str, note: str | None = None) -> bool:
        """Advance a command's delivery evidence; returns False when it was already at or past *state*."""
        if state not in _FORWARD:
            raise ValueError(f"unknown delivery state {state}")
        column = {"written": "written_at", "echoed": "echoed_at"}.get(state)
        allowed = _FORWARD[state]
        marks = ",".join("?" for _ in allowed)
        with self._write() as db:
            if column:
                moved = db.execute(f"UPDATE commands SET delivery=?, delivery_note=COALESCE(?, delivery_note), {column}=? "
                                   f"WHERE run_id=? AND command_id=? AND delivery IN ({marks})",
                                   (state, note, time.time(), run_id, command_id, *allowed)).rowcount
            else:
                moved = db.execute(f"UPDATE commands SET delivery=?, delivery_note=? WHERE run_id=? AND command_id=? "
                                   f"AND delivery IN ({marks})", (state, note, run_id, command_id, *allowed)).rowcount
            return bool(moved)

    def redeliver(self, run_id: str, command_id: str, state: str, note: str) -> bool:
        """Advance a command carried again in a resumed session's first message.

        A failed attempt (only part of its frame left) may still be delivered this way; the earlier evidence
        stays in the note and the first written_at is kept, so a resend never poses as the first write.
        """
        if state not in ("written", "echoed"):
            raise ValueError(f"unknown redelivery state {state}")
        column = f"{state}_at"
        allowed = ("queued", "failed") if state == "written" else ("queued", "written", "failed")
        marks = ",".join("?" for _ in allowed)
        with self._write() as db:
            return bool(db.execute(
                f"UPDATE commands SET delivery=?, delivery_note=COALESCE(delivery_note || ' | ', '') || ?, "
                f"{column}=COALESCE({column}, ?) WHERE run_id=? AND command_id=? AND delivery IN ({marks})",
                (state, note, time.time(), run_id, command_id, *allowed)).rowcount)

    def open_interaction(self, run_id: str, kind: str, *, blocking: bool, request: dict[str, Any],
                         request_id: str | None = None, tool_use_id: str | None = None,
                         hold: dict[str, Any] | None = None) -> dict[str, Any]:
        """Record what Claude raised; nonblocking notes are acknowledged in the same transaction."""
        if kind not in INTERACTION_KINDS:
            raise ValueError(f"unknown interaction kind {kind}")
        interaction_id = "ix-" + secrets.token_hex(6)
        now = time.time()
        with self._write() as db:
            run = db.execute("SELECT revision FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if run is None:
                raise StoreError("unknown run")
            db.execute("INSERT INTO interactions (interaction_id, run_id, kind, blocking, status, request, request_id, "
                       "tool_use_id, hold, created_at, closed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                       (interaction_id, run_id, kind, int(blocking), "pending" if blocking else "acknowledged",
                        _dumps(request), request_id, tool_use_id,
                        _dumps(hold) if hold else None, now, None if blocking else now))
            db.execute("UPDATE runs SET revision=revision+1, updated_at=? WHERE run_id=?", (now, run_id))
            row = db.execute("SELECT * FROM interactions WHERE interaction_id=?", (interaction_id,)).fetchone()
        return self._interaction(row)

    def update_hold(self, interaction_id: str, hold: dict[str, Any]) -> None:
        with self._write() as db:
            db.execute("UPDATE interactions SET hold=? WHERE interaction_id=?", (_dumps(hold), interaction_id))

    def expire(self, interaction_id: str, reason: str) -> bool:
        with self._write() as db:
            moved = db.execute("UPDATE interactions SET status='expired', answer=?, closed_at=? "
                               "WHERE interaction_id=? AND status='pending'",
                               (_dumps({"reason": reason}), time.time(), interaction_id)).rowcount
            if moved:
                db.execute("UPDATE runs SET revision=revision+1 WHERE run_id=(SELECT run_id FROM interactions "
                           "WHERE interaction_id=?)", (interaction_id,))
            return bool(moved)

    def cancel(self, interaction_id: str, reason: str) -> dict[str, Any] | None:
        """The CLI abandoned this request (for example after an interrupt): close it and fail an undelivered answer."""
        now = time.time()
        with self._write() as db:
            row = db.execute("SELECT * FROM interactions WHERE interaction_id=?", (interaction_id,)).fetchone()
            if row is None or row["status"] not in {"pending", "answered"}:
                return None
            db.execute("UPDATE interactions SET status='cancelled', closed_at=? WHERE interaction_id=?", (now, interaction_id))
            if row["answer_command"]:
                db.execute("UPDATE commands SET delivery='failed', delivery_note=? WHERE run_id=? AND command_id=? "
                           "AND delivery='queued'", ("the CLI abandoned the question before the answer was delivered: " + reason,
                                                     row["run_id"], row["answer_command"]))
            db.execute("UPDATE runs SET revision=revision+1, updated_at=? WHERE run_id=?", (now, row["run_id"]))
            return self._interaction(db.execute("SELECT * FROM interactions WHERE interaction_id=?", (interaction_id,)).fetchone())

    def resume_answers(self, run_id: str) -> list[dict[str, Any]] | None:
        """Atomically leave the held state once no blocking question is pending.

        Returns every answered interaction whose answer has no runtime echo yet, queued, written or failed
        alike: an answer that reached the stopped CLI's pipe, whole or in part, but never came back as a tool
        result may not be in the session that resumes.  A failed answer of a still answered interaction can
        only be a partly written frame (cancellation and the run's end close the interaction or the run).
        Returns None while some blocking question is still unanswered.
        """
        with self._write() as db:
            if db.execute("SELECT 1 FROM interactions WHERE run_id=? AND status='pending' AND blocking=1", (run_id,)).fetchone():
                return None
            rows = db.execute("SELECT i.* FROM interactions i JOIN commands c ON c.run_id=i.run_id AND c.command_id=i.answer_command "
                              "WHERE i.run_id=? AND i.status='answered' AND c.delivery IN ('queued', 'written', 'failed') "
                              "ORDER BY i.created_at", (run_id,)).fetchall()
            db.execute("UPDATE runs SET state='running', updated_at=? WHERE run_id=? AND state='held'", (time.time(), run_id))
            return [self._interaction(row) for row in rows]

    def resume_messages(self, run_id: str) -> list[dict[str, Any]]:
        """Updates and interrupts the stopped execution may not have taken as input, in the order Codex sent them.

        Queued ones never left; written or failed ones reached the pipe without a replay, so the session that
        resumes may not hold them.  Echoed ones are in the session already and are not sent again.
        """
        with self._read() as db:
            rows = db.execute("SELECT * FROM commands WHERE run_id=? AND kind IN ('update', 'interrupt') "
                              "AND delivery IN ('queued', 'written', 'failed') ORDER BY sequence", (run_id,)).fetchall()
        return [{**dict(row), "payload": _loads(row["payload"])} for row in rows]

    def withdraw_for_interrupt(self, run_id: str) -> list[dict[str, Any]]:
        """While the execution is stopped, an interrupt withdraws the questions it was sent after.

        This is what the live CLI does with a pending request when it is interrupted; the stopped one cannot,
        so the store does it in one transaction.  Returns the questions withdrawn (none when no undelivered
        interrupt follows a pending question).
        """
        now = time.time()
        with self._write() as db:
            run = db.execute("SELECT state FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if run is None or run["state"] != "held":
                return []
            latest = db.execute("SELECT command_id, created_at FROM commands WHERE run_id=? AND kind='interrupt' "
                                "AND delivery IN ('queued', 'written') ORDER BY sequence DESC LIMIT 1", (run_id,)).fetchone()
            if latest is None:
                return []
            rows = db.execute("SELECT * FROM interactions WHERE run_id=? AND status='pending' AND blocking=1 AND created_at<=? "
                              "ORDER BY created_at", (run_id, latest["created_at"])).fetchall()
            if not rows:
                return []
            reason = {"reason": f"withdrawn by Codex's interrupt {latest['command_id']} while the execution was stopped"}
            for row in rows:
                db.execute("UPDATE interactions SET status='cancelled', answer=?, closed_at=? WHERE interaction_id=?",
                           (_dumps(reason), now, row["interaction_id"]))
            db.execute("UPDATE runs SET revision=revision+1, updated_at=? WHERE run_id=?", (now, run_id))
            return [{**self._interaction(row), "status": "cancelled", "answer": reason} for row in rows]

    def pending_blocking(self, run_id: str) -> list[str]:
        with self._read() as db:
            return [row[0] for row in db.execute("SELECT interaction_id FROM interactions WHERE run_id=? AND status='pending' "
                                                 "AND blocking=1 ORDER BY created_at", (run_id,))]

    @staticmethod
    def _interaction(row: sqlite3.Row) -> dict[str, Any]:
        value = dict(row)
        value["blocking"] = bool(value["blocking"])
        for key in ("request", "answer", "hold"):
            value[key] = _loads(value[key])
        return value

    def interactions(self, run_id: str, *, status: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM interactions WHERE run_id=?" + (" AND status=?" if status else "") + " ORDER BY created_at"
        with self._read() as db:
            rows = db.execute(query, (run_id, status) if status else (run_id,)).fetchall()
        return [self._interaction(row) for row in rows]

    def interaction(self, interaction_id: str) -> dict[str, Any] | None:
        with self._read() as db:
            row = db.execute("SELECT * FROM interactions WHERE interaction_id=?", (interaction_id,)).fetchone()
        return self._interaction(row) if row else None

    def reserve_opening(self, namespace: str, chat: str, *, reopen: bool = False) -> dict[str, Any]:
        """Persist the automatic open request before Codex asks the host; only a new reservation may open."""
        now = time.time()
        with self._write() as db:
            row = db.execute("SELECT * FROM openings WHERE namespace=? AND chat=? AND surface=?",
                             (namespace, chat, SURFACE)).fetchone()
            if row is None:
                db.execute("INSERT INTO openings VALUES (?, ?, ?, 'attempted', 1, ?, NULL)", (namespace, chat, SURFACE, now))
                return {"action": "open_in_codex", "reason": "first workbench request for this chat", "state": "attempted"}
            if reopen:
                db.execute("UPDATE openings SET state='attempted', attempts=attempts+1, reserved_at=?, outcome_at=NULL "
                           "WHERE namespace=? AND chat=? AND surface=?", (now, namespace, chat, SURFACE))
                return {"action": "open_in_codex", "reason": "explicit reopen", "state": "attempted"}
            return {"action": "link_only", "state": row["state"], "reserved_at": row["reserved_at"],
                    "reason": f"already requested for this chat ({row['state']}); reopen only when the user asks"}

    def record_opening(self, namespace: str, chat: str, outcome: str) -> dict[str, Any]:
        if outcome not in OPEN_OUTCOMES:
            raise StoreError("outcome must be queued, failed or unknown")
        with self._write() as db:
            moved = db.execute("UPDATE openings SET state=?, outcome_at=? WHERE namespace=? AND chat=? AND surface=?",
                               (outcome, time.time(), namespace, chat, SURFACE)).rowcount
            if not moved:
                raise StoreError("no workbench request was reserved for this chat")
            row = db.execute("SELECT * FROM openings WHERE namespace=? AND chat=? AND surface=?",
                             (namespace, chat, SURFACE)).fetchone()
        return dict(row)

    def runs_for(self, namespace: str, chat: str) -> list[str]:
        with self._read() as db:
            return [row[0] for row in db.execute("SELECT run_id FROM runs WHERE namespace=? AND chat=?", (namespace, chat))]

    def overview(self) -> dict[str, dict[str, Any]]:
        """Owner and pending-interaction count of every interactive run, in one query for list views."""
        with self._read() as db:
            rows = db.execute("SELECT r.run_id, r.namespace, r.chat, r.state, r.revision, "
                              "(SELECT COUNT(*) FROM interactions i WHERE i.run_id=r.run_id AND i.status='pending') AS pending "
                              "FROM runs r").fetchall()
        return {row["run_id"]: dict(row) for row in rows}

    def summary(self, run_id: str) -> dict[str, Any]:
        """What a status view needs: revision, state and every interaction (pending ones first)."""
        row = self.run(run_id)
        if row is None:
            return {"interactive": False, "revision": None, "control_state": None, "interactions": [], "pending": []}
        items = self.interactions(run_id)
        pending = [item for item in items if item["status"] == "pending"]
        return {"interactive": bool(row["interactive"]), "revision": row["revision"], "control_state": row["state"],
                "context": {"namespace": row["namespace"], "chat": row["chat"]} if row["chat"] else None,
                "interactions": items, "pending": pending}
