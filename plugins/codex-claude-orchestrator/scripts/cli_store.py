"""Private, native-only Claude CLI retention store (historical).

Plugin-managed CLI versions are retired.  No production entry (MCP tools,
Viewer, installer, locator, bridge, model catalog) calls the capture,
download, qualification, activation or rollback functions below any more;
dispatch always uses the user's local CLI.  The implementation is kept so
retained identities, receipts and selection files from earlier releases stay
readable (see ``legacy_records``) and are never deleted.  It never invokes
Claude's installer, replaces its launcher, copies authentication material, or
imports the bridge at module import time.
"""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
from http.client import HTTPException, IncompleteRead
import json
import os
from pathlib import Path
import platform as platform_module
import re
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any, Callable, Iterator
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


SCHEMA_VERSION = 1
_IDENTITY_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,191}$")
_VERSION = re.compile(r"\b(\d+\.\d+\.\d+(?:[-+][A-Za-z0-9._-]+)?)\b")
_GROUPS = frozenset({"core", "read_only", "write", "resume", "workflow"})
_SELECTION_NAME = "selection.json"
_LOCK_NAME = "selection.lock"
_UPDATE_POLICY_NAME = "update-policy.json"
_UPDATE_CONFIG_NAME = "update-config.json"
_UPDATE_CONFIG_TRANSACTION_NAME = "update-config-transaction.json"
_EXPLICIT_TRANSACTION_NAME = "explicit-switch-transaction.json"
_LATEST_AUTOMATIC_HOLD_REASON = "latest_automatic_compatibility_hold"
OFFICIAL_RELEASES = "https://downloads.claude.ai/claude-code-releases"
BASELINE_VERSION = "2.1.278"
# Release-time audit: the fixed manifest URL below was verified with Anthropic's
# published GPG release key (fingerprint 31DD...1A7ECACE) and detached signature.
# Runtime users need no GPG installation: HTTPS delivery must match this audited
# digest, size, and the macOS Anthropic Developer ID signature before capture.
# Source: https://downloads.claude.ai/claude-code-releases/2.1.278/manifest.json
OFFICIAL_BASELINES = {
    "darwin-arm64": {"sha256": "bd245662fb8a0e321b3bf133e930371d6563c387527885f30b2613aef3ba14d6", "size": 217695408},
    "darwin-x64": {"sha256": "c522425e3d42275d2ac2238757ef8ba7f80d165a934044ec5a7a5fd7d7b9950b", "size": 226521952},
}
# Only releases shipped in this table can be acquired automatically.  Entries
# are release-audited inputs, not a mutable remote "latest" feed.
OFFICIAL_RELEASE_MATRIX = {BASELINE_VERSION: OFFICIAL_BASELINES}
ANTHROPIC_TEAM_ID = "Q6L2SF6YDW"
ANTHROPIC_IDENTIFIER = "com.anthropic.claude-code"
DOWNLOAD_IDLE_TIMEOUT_SECONDS = 120.0
DOWNLOAD_ATTEMPT_TIMEOUT_SECONDS = 60 * 60.0
DOWNLOAD_LOCK_TIMEOUT_SECONDS = 30.0
_CONTENT_RANGE = re.compile(r"^bytes (\d+)-(\d+)/(\d+)$")
_UNSATISFIED_CONTENT_RANGE = re.compile(r"^bytes \*/(\d+)$")


class AutomaticActivationSuperseded(RuntimeError):
    """An explicit selection or policy change won the activation CAS."""


class AutomaticQualificationSuperseded(RuntimeError):
    """Automatic paid qualification lost to a newer policy or selection."""


def _environment(environ: dict[str, str] | None) -> dict[str, str]:
    return dict(os.environ if environ is None else environ)


def _expand(value: str, environ: dict[str, str]) -> Path:
    if value == "~" or value.startswith("~/"):
        return Path(environ.get("HOME") or str(Path.home())) / value[2:]
    return Path(value)


def store_root(environ: dict[str, str] | None = None) -> Path:
    """Return the plugin-owned store root without creating it."""
    env = _environment(environ)
    explicit = env.get("CLAUDE_ORCHESTRATOR_CLI_ROOT", "").strip()
    if explicit:
        return Path(os.path.abspath(str(_expand(explicit, env))))
    codex_home = _expand(env.get("CODEX_HOME", "~/.codex"), env)
    return Path(os.path.abspath(str(codex_home / "claude-orchestrator/cli")))


# Compatibility with the initial implementation request.  New callers should
# use the less ambiguous store_root name from CLI-MANAGEMENT-CONTRACT.md.
root = store_root


def _ensure_root(environ: dict[str, str] | None) -> Path:
    value = store_root(environ)
    value.mkdir(mode=0o700, parents=True, exist_ok=True)
    for child in (value / "versions", value / "jobs"):
        child.mkdir(mode=0o700, exist_ok=True)
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as error:
        raise RuntimeError(f"invalid CLI store JSON: {path}") from error
    if not isinstance(data, dict):
        raise RuntimeError(f"invalid CLI store object: {path}")
    return data


def _atomic_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary_path.chmod(0o600)
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


@contextmanager
def _selection_lock(root_path: Path) -> Iterator[None]:
    """Serialize selection CAS writes with one persistent inode.

    The lock file is intentionally never unlinked.  Replacing/unlinking a
    flocked path creates a second inode and would let concurrent writers bypass
    the lock.
    """
    lock_path = root_path / _LOCK_NAME
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _empty_selection() -> dict[str, Any]:
    return {"schema_version": SCHEMA_VERSION, "generation": 0, "active": None,
            "previous": None, "history": [], "mode": "managed"}


def _validate_selection(value: dict[str, Any]) -> dict[str, Any]:
    if value.get("schema_version") != SCHEMA_VERSION:
        raise RuntimeError("unsupported CLI selection schema")
    generation = value.get("generation")
    if not isinstance(generation, int) or generation < 0:
        raise RuntimeError("invalid CLI selection generation")
    if value.get("mode") not in {"managed", "external"}:
        raise RuntimeError("invalid CLI selection mode")
    for key in ("active", "previous"):
        item = value.get(key)
        if item is not None and (not isinstance(item, str) or not _IDENTITY_ID.fullmatch(item)):
            raise RuntimeError("invalid CLI selection identity")
    history = value.get("history")
    if not isinstance(history, list) or any(not isinstance(item, str) or not _IDENTITY_ID.fullmatch(item) for item in history):
        raise RuntimeError("invalid CLI selection history")
    return {"schema_version": SCHEMA_VERSION, "generation": generation, "active": value["active"],
            "previous": value["previous"], "history": list(dict.fromkeys(history)), "mode": value["mode"]}


def get_selection(environ: dict[str, str] | None = None) -> dict[str, Any]:
    """Read selection after completing any interrupted explicit transaction."""
    root_path = store_root(environ)
    if not root_path.exists():
        return _empty_selection()
    with _selection_lock(root_path):
        return _selection_under_lock(root_path)


def _selection_under_lock(root_path: Path) -> dict[str, Any]:
    _recover_explicit_transaction_under_lock(root_path)
    _recover_update_config_transaction_under_lock(root_path)
    value = _read_json(root_path / _SELECTION_NAME)
    return _empty_selection() if value is None else _validate_selection(value)


def _empty_update_policy() -> dict[str, Any]:
    return {"schema_version": SCHEMA_VERSION, "generation": 0, "mode": "automatic",
            "reason": "default", "updated_at": None}


def _validate_update_policy(value: dict[str, Any]) -> dict[str, Any]:
    if value.get("schema_version") != SCHEMA_VERSION:
        raise RuntimeError("unsupported CLI update policy schema")
    generation = value.get("generation")
    if not isinstance(generation, int) or generation < 0:
        raise RuntimeError("invalid CLI update policy generation")
    if value.get("mode") not in {"automatic", "manual"}:
        raise RuntimeError("invalid CLI update policy mode")
    reason = value.get("reason")
    if not isinstance(reason, str) or not reason:
        raise RuntimeError("invalid CLI update policy reason")
    updated_at = value.get("updated_at")
    if updated_at is not None and not isinstance(updated_at, (int, float)):
        raise RuntimeError("invalid CLI update policy timestamp")
    return {"schema_version": SCHEMA_VERSION, "generation": generation,
            "mode": value["mode"], "reason": reason, "updated_at": updated_at}


def _empty_update_config() -> dict[str, Any]:
    # Keep pre-feature installations offline and free of automatic model calls.
    return {"schema_version": SCHEMA_VERSION, "channel": "bundled", "auto_qualify": False,
            "policy_generation": None, "effective_mode": None, "effective_reason": None}


def _update_config_under_lock(root_path: Path) -> dict[str, Any]:
    value = _read_json(root_path / _UPDATE_CONFIG_NAME)
    if value is None:
        return _empty_update_config()
    return _validate_update_config(value)


def _validate_update_config(value: dict[str, Any]) -> dict[str, Any]:
    if value.get("schema_version") != SCHEMA_VERSION or value.get("channel") not in {"bundled", "latest"}:
        raise RuntimeError("invalid CLI update channel configuration")
    if type(value.get("auto_qualify")) is not bool:
        raise RuntimeError("invalid CLI automatic qualification configuration")
    policy_generation = value.get("policy_generation")
    effective_mode = value.get("effective_mode")
    effective_reason = value.get("effective_reason")
    if policy_generation is not None and (not isinstance(policy_generation, int) or policy_generation < 0):
        raise RuntimeError("invalid CLI update configuration owner generation")
    if effective_mode is not None and effective_mode not in {"automatic", "manual"}:
        raise RuntimeError("invalid CLI effective update mode")
    if effective_reason is not None and (not isinstance(effective_reason, str) or not effective_reason):
        raise RuntimeError("invalid CLI effective update reason")
    if (policy_generation is None) != (effective_mode is None) or (policy_generation is None) != (effective_reason is None):
        raise RuntimeError("incomplete CLI update configuration owner binding")
    return {"schema_version": SCHEMA_VERSION, "channel": value["channel"],
            "auto_qualify": value["auto_qualify"], "policy_generation": policy_generation,
            "effective_mode": effective_mode, "effective_reason": effective_reason}


def _update_policy_under_lock(root_path: Path) -> dict[str, Any]:
    _recover_explicit_transaction_under_lock(root_path)
    _recover_update_config_transaction_under_lock(root_path)
    value = _read_json(root_path / _UPDATE_POLICY_NAME)
    return _empty_update_policy() if value is None else _validate_update_policy(value)


def _effective_update_policy(policy: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    """Resolve new intent only while its legacy-policy owner still matches."""
    bound = config.get("policy_generation") == policy["generation"]
    if bound:
        effective_mode = config.get("effective_mode")
        if (config["channel"] == "latest" and effective_mode == "automatic"
                and policy["mode"] == "manual" and policy["reason"] == _LATEST_AUTOMATIC_HOLD_REASON):
            return {**policy, "mode": "automatic", "reason": config["effective_reason"],
                    "channel": "latest", "auto_qualify": config["auto_qualify"],
                    "legacy_compatibility_hold": True}
        if effective_mode == policy["mode"]:
            return {**policy, "reason": config["effective_reason"],
                    "channel": config["channel"], "auto_qualify": config["auto_qualify"],
                    "legacy_compatibility_hold": False}
    # An old writer changed generation or removed the compatibility marker.
    # Manual remains a hard pause, so retaining channel/qualification fields as
    # preferences cannot spend or activate; a later new automatic request will
    # bind them to a fresh generation.  Legacy automatic is different: it can
    # run the bundled 0.4.5 worker, so stale latest intent must be ignored to
    # prevent old/new services from alternating active versions.
    if policy["mode"] == "manual":
        return {**policy, "channel": config["channel"],
                "auto_qualify": config["auto_qualify"],
                "legacy_compatibility_hold": False,
                "paused_preferences_retained": True}
    return {**policy, "channel": "bundled", "auto_qualify": False,
            "legacy_compatibility_hold": False,
            "paused_preferences_retained": False}


def get_update_policy(environ: dict[str, str] | None = None) -> dict[str, Any]:
    """Read policy after completing any interrupted explicit transaction."""
    root_path = store_root(environ)
    if not root_path.exists():
        return _effective_update_policy(_empty_update_policy(), _empty_update_config())
    with _selection_lock(root_path):
        return _effective_update_policy(_update_policy_under_lock(root_path),
                                        _update_config_under_lock(root_path))


def _recover_explicit_transaction_under_lock(root_path: Path) -> None:
    """Resolve a crash-interrupted selection/policy transaction.

    Recovery only clears a journal when the persisted pair is the unambiguous
    pre-commit or committed pair.  A mixed pair could have been written by a
    still-running 0.4.3 MCP process that knows the shared lock but not this
    journal, so it is preserved and surfaced as a conflict rather than guessed.
    """
    transaction_path = root_path / _EXPLICIT_TRANSACTION_NAME
    transaction = _read_json(transaction_path)
    if transaction is None:
        return
    if transaction.get("schema_version") != SCHEMA_VERSION or transaction.get("state") not in {"prepared", "committed"}:
        raise RuntimeError("invalid explicit CLI switch transaction")
    transaction_id = transaction.get("transaction_id")
    if not isinstance(transaction_id, str) or not re.fullmatch(r"[0-9a-f]{32}", transaction_id):
        raise RuntimeError("invalid explicit CLI switch transaction witness")
    before_selection = transaction.get("before_selection")
    before_policy = transaction.get("before_policy")
    after_selection = transaction.get("after_selection")
    after_policy = transaction.get("after_policy")
    if not all(isinstance(value, dict) for value in
               (before_selection, before_policy, after_selection, after_policy)):
        raise RuntimeError("incomplete explicit CLI switch transaction")
    before_selection = _validate_selection(before_selection)
    before_policy = _validate_update_policy(before_policy)
    after_selection = _validate_selection(after_selection)
    after_policy = _validate_update_policy(after_policy)
    observed_selection_value = _read_json(root_path / _SELECTION_NAME)
    observed_policy_value = _read_json(root_path / _UPDATE_POLICY_NAME)
    observed_selection = (_empty_selection() if observed_selection_value is None
                          else _validate_selection(observed_selection_value))
    observed_policy = (_empty_update_policy() if observed_policy_value is None
                       else _validate_update_policy(observed_policy_value))
    before_pair = (json.dumps(before_selection, sort_keys=True),
                   json.dumps(before_policy, sort_keys=True))
    after_pair = (json.dumps(after_selection, sort_keys=True),
                  json.dumps(after_policy, sort_keys=True))
    observed_pair = (json.dumps(observed_selection, sort_keys=True),
                     json.dumps(observed_policy, sort_keys=True))
    selection_witness = observed_selection_value is not None and observed_selection_value.get(
        "_explicit_transaction_id") == transaction_id
    policy_witness = observed_policy_value is not None and observed_policy_value.get(
        "_explicit_transaction_id") == transaction_id
    if transaction["state"] == "prepared":
        recoverable = (observed_pair == before_pair
                       or (observed_pair == (after_pair[0], before_pair[1]) and selection_witness)
                       or (observed_pair == after_pair and selection_witness and policy_witness))
        recovery_selection, recovery_policy = before_selection, before_policy
    else:
        recoverable = observed_pair == after_pair and selection_witness and policy_witness
        recovery_selection, recovery_policy = after_selection, after_policy
    if not recoverable:
        # A 0.4.3 process can still hold the legacy lock after a plugin upgrade.
        # If it wrote after our process crashed, its explicit/current selection
        # wins; never replace it with stale transaction snapshots.
        notice_id = f"cli-switch-conflict-{time.time_ns()}"
        conflict_path = root_path / f"{notice_id}.json"
        _atomic_json(conflict_path, {**transaction, "state": "conflict",
                                    "observed_selection": observed_selection,
                                    "observed_policy": observed_policy,
                                    "notice_id": notice_id, "notice_acknowledged_at": None,
                                    "diagnostic": ("Concurrent legacy CLI selection or policy update was preserved; "
                                                   "the interrupted explicit switch was not recovered.")})
        transaction_path.unlink()
        return
    _atomic_json(root_path / _SELECTION_NAME, recovery_selection)
    _atomic_json(root_path / _UPDATE_POLICY_NAME, recovery_policy)
    transaction_path.unlink()


def _recover_update_config_transaction_under_lock(root_path: Path) -> None:
    """Recover a crash between the legacy policy and advanced config writes.

    The legacy policy is written first so its generation blocks an older
    maintenance worker.  Witness fields distinguish our partial commit from a
    later 0.4.5 policy write.  A later legacy write is preserved; uncertainty
    about enabling paid qualification always resolves to ``auto_qualify=false``.
    """
    transaction_path = root_path / _UPDATE_CONFIG_TRANSACTION_NAME
    transaction = _read_json(transaction_path)
    if transaction is None:
        return
    if transaction.get("schema_version") != SCHEMA_VERSION or transaction.get("state") not in {"prepared", "committed"}:
        raise RuntimeError("invalid CLI update-config transaction")
    transaction_id = transaction.get("transaction_id")
    if not isinstance(transaction_id, str) or not re.fullmatch(r"[0-9a-f]{32}", transaction_id):
        raise RuntimeError("invalid CLI update-config transaction witness")
    before_policy_value = transaction.get("before_policy")
    after_policy_value = transaction.get("after_policy")
    before_config_value = transaction.get("before_config")
    after_config_value = transaction.get("after_config")
    if not all(isinstance(value, dict) for value in
               (before_policy_value, after_policy_value, before_config_value, after_config_value)):
        raise RuntimeError("incomplete CLI update-config transaction")
    before_policy = _validate_update_policy(before_policy_value)
    after_policy = _validate_update_policy(after_policy_value)
    before_config = _validate_update_config(before_config_value)
    after_config = _validate_update_config(after_config_value)

    observed_policy_raw = _read_json(root_path / _UPDATE_POLICY_NAME)
    observed_config_raw = _read_json(root_path / _UPDATE_CONFIG_NAME)
    observed_policy = (_empty_update_policy() if observed_policy_raw is None
                       else _validate_update_policy(observed_policy_raw))
    observed_config = (_empty_update_config() if observed_config_raw is None
                       else _validate_update_config(observed_config_raw))
    policy_witness = (observed_policy_raw is not None
                      and observed_policy_raw.get("_update_config_transaction_id") == transaction_id)
    config_witness = (observed_config_raw is not None
                      and observed_config_raw.get("_update_config_transaction_id") == transaction_id)

    def same(left: dict[str, Any], right: dict[str, Any]) -> bool:
        return json.dumps(left, sort_keys=True) == json.dumps(right, sort_keys=True)

    if transaction["state"] == "prepared":
        if same(observed_policy, before_policy) and same(observed_config, before_config):
            transaction_path.unlink()
            return
        recover_forward = (same(observed_policy, after_policy) and policy_witness
                           and ((same(observed_config, before_config) and not config_witness)
                                or (same(observed_config, after_config) and config_witness)))
        if recover_forward:
            _atomic_json(root_path / _UPDATE_POLICY_NAME, after_policy)
            _atomic_json(root_path / _UPDATE_CONFIG_NAME, after_config)
            transaction_path.unlink()
            return
    elif (same(observed_config, after_config)
          and (config_witness or not isinstance(observed_config_raw, dict)
               or "_update_config_transaction_id" not in observed_config_raw)):
        # Both writes completed before the committed journal was persisted.
        # A later legacy service may have changed policy; preserve that newer
        # explicit value while normalizing our completed config write.
        policy = (after_policy if same(observed_policy, after_policy) and policy_witness
                  else observed_policy)
        _atomic_json(root_path / _UPDATE_POLICY_NAME, policy)
        _atomic_json(root_path / _UPDATE_CONFIG_NAME, after_config)
        transaction_path.unlink()
        return

    # A legacy writer changed policy or the advanced config changed without
    # our witness.  Preserve its observed policy.  Disabling automatic paid
    # qualification is safe to complete; enabling it remains off until a new
    # explicit request.  This conflict is surfaced through the existing notice
    # channel instead of silently overwriting the concurrent decision.
    safe_config = (after_config if after_config["auto_qualify"] is False
                   else {"schema_version": SCHEMA_VERSION,
                         "channel": observed_config["channel"], "auto_qualify": False})
    _atomic_json(root_path / _UPDATE_POLICY_NAME, observed_policy)
    _atomic_json(root_path / _UPDATE_CONFIG_NAME, safe_config)
    notice_id = f"cli-switch-conflict-{time.time_ns()}"
    _atomic_json(root_path / f"{notice_id}.json", {
        **transaction, "state": "conflict", "notice_id": notice_id,
        "notice_acknowledged_at": None, "observed_policy": observed_policy,
        "observed_config": observed_config, "safe_config": safe_config,
        "diagnostic": ("Concurrent legacy CLI policy update was preserved; interrupted latest/qualification "
                       "configuration was recovered with automatic paid qualification disabled."),
    })
    transaction_path.unlink()


def _commit_update_config_transaction_under_lock(root_path: Path,
                                                 before_policy: dict[str, Any],
                                                 before_config: dict[str, Any],
                                                 after_policy: dict[str, Any],
                                                 after_config: dict[str, Any]) -> None:
    transaction_path = root_path / _UPDATE_CONFIG_TRANSACTION_NAME
    transaction_id = os.urandom(16).hex()
    prepared = {"schema_version": SCHEMA_VERSION, "state": "prepared",
                "transaction_id": transaction_id,
                "before_policy": before_policy, "before_config": before_config,
                "after_policy": after_policy, "after_config": after_config}
    _atomic_json(transaction_path, prepared)
    try:
        _atomic_json(root_path / _UPDATE_POLICY_NAME,
                     {**after_policy, "_update_config_transaction_id": transaction_id})
        _atomic_json(root_path / _UPDATE_CONFIG_NAME,
                     {**after_config, "_update_config_transaction_id": transaction_id})
        _atomic_json(transaction_path, {**prepared, "state": "committed"})
        _atomic_json(root_path / _UPDATE_POLICY_NAME, after_policy)
        _atomic_json(root_path / _UPDATE_CONFIG_NAME, after_config)
        transaction_path.unlink()
    except Exception as error:
        # No other lock-aware writer can interleave while this process owns the
        # shared selection lock, so a synchronous failure can restore exactly.
        try:
            _atomic_json(root_path / _UPDATE_POLICY_NAME, before_policy)
            _atomic_json(root_path / _UPDATE_CONFIG_NAME, before_config)
            transaction_path.unlink(missing_ok=True)
        except Exception as recovery_error:
            raise RuntimeError(
                "CLI update configuration failed and left a recovery journal; automatic maintenance must recover before use") from recovery_error
        raise error


def _commit_explicit_transaction_under_lock(root_path: Path,
                                            before_selection: dict[str, Any],
                                            before_policy: dict[str, Any],
                                            after_selection: dict[str, Any],
                                            after_policy: dict[str, Any]) -> None:
    transaction_path = root_path / _EXPLICIT_TRANSACTION_NAME
    transaction_id = os.urandom(16).hex()
    prepared = {"schema_version": SCHEMA_VERSION, "state": "prepared",
                "transaction_id": transaction_id,
                "before_selection": before_selection, "before_policy": before_policy,
                "after_selection": after_selection, "after_policy": after_policy}
    _atomic_json(transaction_path, prepared)
    try:
        _atomic_json(root_path / _SELECTION_NAME,
                     {**after_selection, "_explicit_transaction_id": transaction_id})
        _atomic_json(root_path / _UPDATE_POLICY_NAME,
                     {**after_policy, "_explicit_transaction_id": transaction_id})
        _atomic_json(transaction_path, {**prepared, "state": "committed"})
        transaction_path.unlink()
    except Exception as error:
        # The current process still owns the shared lock, so no legacy writer
        # can have interleaved here.  Restore synchronously; crash recovery is
        # deliberately more conservative because writer provenance is unknown.
        try:
            _atomic_json(root_path / _SELECTION_NAME, before_selection)
            _atomic_json(root_path / _UPDATE_POLICY_NAME, before_policy)
            transaction_path.unlink(missing_ok=True)
        except Exception as recovery_error:
            raise RuntimeError(
                "explicit CLI switch failed and left a recovery journal; current values were preserved for diagnosis") from recovery_error
        raise error


def explicit_switch_conflict(environ: dict[str, str] | None = None) -> dict[str, Any] | None:
    """Return the newest unacknowledged cross-version switch conflict."""
    root_path = store_root(environ)
    if not root_path.is_dir():
        return None
    for path in sorted(root_path.glob("cli-switch-conflict-*.json"), reverse=True):
        value = _read_json(path)
        if (value is not None and value.get("state") == "conflict"
                and isinstance(value.get("notice_id"), str)
                and value.get("notice_acknowledged_at") is None):
            return {"notice_id": value["notice_id"], "path": str(path),
                    "diagnostic": value.get("diagnostic")}
    return None


def acknowledge_explicit_switch_conflict(notice_id: str,
                                         environ: dict[str, str] | None = None) -> None:
    if not isinstance(notice_id, str) or not re.fullmatch(r"cli-switch-conflict-\d+", notice_id):
        raise ValueError("explicit CLI switch conflict notice is invalid")
    root_path = store_root(environ)
    path = root_path / f"{notice_id}.json"
    with _selection_lock(root_path):
        value = _read_json(path)
        if value is None or value.get("notice_id") != notice_id or value.get("state") != "conflict":
            raise ValueError("explicit CLI switch conflict notice is not current")
        if value.get("notice_acknowledged_at") is None:
            _atomic_json(path, {**value, "notice_acknowledged_at": time.time()})


def set_update_policy(mode: str, reason: str = "user_request",
                      environ: dict[str, str] | None = None, *,
                      channel: str | None = None,
                      auto_qualify: bool | None = None) -> dict[str, Any]:
    """Persist policy under the selection lock used by automatic activation."""
    if mode not in {"automatic", "manual"}:
        raise ValueError("CLI update policy must be automatic or manual")
    if channel is not None and channel not in {"bundled", "latest"}:
        raise ValueError("CLI update channel must be bundled or latest")
    if auto_qualify is not None and type(auto_qualify) is not bool:
        raise ValueError("CLI automatic qualification policy must be boolean")
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("CLI update policy reason is required")
    root_path = _ensure_root(environ)
    with _selection_lock(root_path):
        current = _update_policy_under_lock(root_path)
        config = _update_config_under_lock(root_path)
        effective = _effective_update_policy(current, config)
        next_channel = effective["channel"] if channel is None else channel
        next_auto_qualify = effective["auto_qualify"] if auto_qualify is None else auto_qualify
        reason = reason.strip()
        if (effective["mode"] == mode and effective["reason"] == reason
                and effective["channel"] == next_channel
                and effective["auto_qualify"] == next_auto_qualify):
            return effective
        generation = current["generation"] + 1
        legacy_hold = mode == "automatic" and next_channel == "latest"
        changed = {"schema_version": SCHEMA_VERSION, "generation": generation,
                   "mode": "manual" if legacy_hold else mode,
                   "reason": _LATEST_AUTOMATIC_HOLD_REASON if legacy_hold else reason,
                   "updated_at": time.time()}
        changed_config = {"schema_version": SCHEMA_VERSION, "channel": next_channel,
                          "auto_qualify": next_auto_qualify,
                          "policy_generation": generation, "effective_mode": mode,
                          "effective_reason": reason}
        # Policy generation is still written first so an in-flight old worker
        # loses its CAS.  The journal makes the two-file choice recoverable and
        # distinguishes our partial commit from a later 0.4.5 policy write.
        _commit_update_config_transaction_under_lock(
            root_path, current, config, changed, changed_config)
        return _effective_update_policy(changed, changed_config)


@contextmanager
def automatic_qualification_authorization(expected_selection_generation: int,
                                          expected_policy_generation: int,
                                          environ: dict[str, str] | None = None) -> Iterator[dict[str, Any]]:
    """Linearize a short qualification launch with explicit user choices.

    The caller may reserve an attempt and create/start the validation worker
    while this context is held.  The worker itself inherits separate locks and
    runs after this context exits; the selection lock is never held for the
    bounded 900-second validation.  A completed manual/policy/selection change
    therefore either precedes this authorization and prevents launch, or
    follows an already-created job whose start is reported explicitly.
    """
    if not isinstance(expected_selection_generation, int) or expected_selection_generation < 0:
        raise ValueError("expected CLI selection generation is invalid")
    if not isinstance(expected_policy_generation, int) or expected_policy_generation < 0:
        raise ValueError("expected CLI update policy generation is invalid")
    root_path = _ensure_root(environ)
    with _selection_lock(root_path):
        selection = _selection_under_lock(root_path)
        policy = _effective_update_policy(_update_policy_under_lock(root_path),
                                          _update_config_under_lock(root_path))
        if (selection["generation"] != expected_selection_generation
                or selection["mode"] != "managed"):
            raise AutomaticQualificationSuperseded(
                "CLI selection changed before automatic qualification launch")
        if (policy["generation"] != expected_policy_generation
                or policy["mode"] != "automatic" or not policy.get("auto_qualify")):
            raise AutomaticQualificationSuperseded(
                "CLI update policy no longer authorizes automatic qualification")
        yield {"selection": selection, "policy": policy}


def _manual_policy_under_lock(root_path: Path, reason: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """Build the explicit manual hold while the shared selection lock is held."""
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("CLI update policy reason is required")
    current = _update_policy_under_lock(root_path)
    reason = reason.strip()
    if current["mode"] == "manual" and current["reason"] == reason:
        return current, current
    changed = {"schema_version": SCHEMA_VERSION, "generation": current["generation"] + 1,
               "mode": "manual", "reason": reason, "updated_at": time.time()}
    return current, changed


def _native_check(path: Path) -> None:
    if sys.platform != "darwin":
        raise RuntimeError("managed Claude CLI retention is supported only on macOS native binaries")
    if path.is_symlink() or not path.is_file() or not os.access(path, os.X_OK):
        raise ValueError("Claude candidate must be a regular executable file")
    try:
        inspected = subprocess.run(["file", "-b", str(path)], text=True, capture_output=True,
                                    timeout=10, check=False)
    except OSError as error:
        raise RuntimeError("macOS file inspector is unavailable") from error
    if inspected.returncode != 0 or "Mach-O" not in inspected.stdout:
        raise ValueError("managed CLI capture accepts a signed native Mach-O executable only")
    try:
        signature = subprocess.run(["codesign", "--verify", "--strict", str(path)], text=True,
                                   capture_output=True, timeout=20, check=False)
        details = subprocess.run(["codesign", "-dv", "--verbose=4", str(path)], text=True,
                                 capture_output=True, timeout=20, check=False)
    except OSError as error:
        raise RuntimeError("macOS codesign verifier is unavailable") from error
    identity_lines = [line.strip() for line in (details.stdout + details.stderr).splitlines()]
    signature_fields: dict[str, list[str]] = {}
    for line in identity_lines:
        key, separator, value = line.partition("=")
        if separator:
            signature_fields.setdefault(key, []).append(value)
    if (signature.returncode != 0 or details.returncode != 0
            or signature_fields.get("TeamIdentifier") != [ANTHROPIC_TEAM_ID]
            or signature_fields.get("Identifier") != [ANTHROPIC_IDENTIFIER]
            or f"Developer ID Application: Anthropic PBC ({ANTHROPIC_TEAM_ID})"
            not in signature_fields.get("Authority", [])):
        raise ValueError("Claude native executable did not pass macOS code-signature verification")


def _bounded_output(text: str | None) -> str:
    value = " ".join((text or "").split())
    value = re.sub(r"sk-ant-[A-Za-z0-9_-]+|[^\s@]+@[^\s@]+\.[A-Za-z]{2,}|[A-Za-z0-9+/_-]{40,}", "[redacted]", value)
    return value[:300] + ("…" if len(value) > 300 else "")


def _version(path: Path, environ: dict[str, str] | None) -> str:
    env = _environment(environ)
    try:
        result = subprocess.run([str(path), "--version"], text=True, capture_output=True,
                                timeout=15, check=False, env=env)
    except subprocess.TimeoutExpired as error:
        raise RuntimeError("Claude native executable could not report its version: timed out after 15 seconds") from error
    except OSError as error:
        raise RuntimeError(f"Claude native executable could not report its version: {type(error).__name__}: {error}") from error
    match = _VERSION.search(result.stdout if result.returncode == 0 else "")
    if not match:
        outcome = (f"signal {-result.returncode}" if result.returncode < 0
                   else f"exit code {result.returncode}")
        raise ValueError(f"Claude native executable did not return a recognizable version ({outcome}; "
                         f"stdout: {_bounded_output(result.stdout)!r}; stderr: {_bounded_output(result.stderr)!r})")
    return match.group(1)


def _descriptor(metadata: dict[str, Any], executable: Path) -> dict[str, Any]:
    keys = ("id", "version", "sha256", "platform", "machine", "source", "created_at")
    if any(key not in metadata for key in keys):
        raise RuntimeError("CLI identity metadata is incomplete")
    return {"id": metadata["id"], "path": str(executable), "version": metadata["version"],
            "sha256": metadata["sha256"], "platform": metadata["platform"],
            "machine": metadata["machine"], "source": metadata["source"],
            "created_at": metadata["created_at"]}


def _identity_path(identity_id: str, environ: dict[str, str] | None) -> tuple[Path, Path]:
    if not isinstance(identity_id, str) or not _IDENTITY_ID.fullmatch(identity_id):
        raise ValueError("invalid CLI identity id")
    base = store_root(environ) / "versions" / identity_id
    return base, base / "identity.json"


def identity(identity_id: str, environ: dict[str, str] | None = None) -> dict[str, Any]:
    """Return an immutable descriptor only after rechecking its native identity."""
    base, metadata_path = _identity_path(identity_id, environ)
    if base.is_symlink() or metadata_path.is_symlink():
        raise RuntimeError("CLI store identity cannot be a symbolic link")
    metadata = _read_json(metadata_path)
    if metadata is None:
        raise ValueError(f"managed CLI identity is not retained: {identity_id}")
    if metadata.get("schema_version") != SCHEMA_VERSION or metadata.get("id") != identity_id:
        raise RuntimeError("CLI identity metadata does not bind its path")
    executable = base / "claude"
    if executable.is_symlink() or not executable.is_file() or not os.access(executable, os.X_OK):
        raise RuntimeError("managed CLI executable is missing or is not executable")
    if metadata.get("kind") != "native_macho" or metadata.get("sha256") != _sha256(executable):
        raise RuntimeError("managed CLI identity integrity check failed")
    _native_check(executable)
    descriptor = _descriptor(metadata, executable)
    expected_id = _identity_id_for(descriptor["platform"], descriptor["machine"], descriptor["version"], descriptor["sha256"])
    if descriptor["id"] != expected_id:
        raise RuntimeError("managed CLI identity id does not match its content")
    return descriptor


def identity_metadata(identity_id: str, environ: dict[str, str] | None = None) -> dict[str, Any]:
    """Read immutable identity metadata without hashing/codesigning the binary.

    This is for frequent read-only status rendering.  Dispatch, acquisition,
    qualification and activation must continue to call :func:`identity`.
    """
    base, metadata_path = _identity_path(identity_id, environ)
    if base.is_symlink() or metadata_path.is_symlink():
        raise RuntimeError("CLI store identity cannot be a symbolic link")
    metadata = _read_json(metadata_path)
    if metadata is None or metadata.get("schema_version") != SCHEMA_VERSION or metadata.get("id") != identity_id:
        raise RuntimeError("CLI identity metadata does not bind its path")
    executable = base / "claude"
    if executable.is_symlink() or not executable.is_file() or not os.access(executable, os.X_OK):
        raise RuntimeError("managed CLI executable is missing or is not executable")
    descriptor = _descriptor(metadata, executable)
    expected_id = _identity_id_for(descriptor["platform"], descriptor["machine"], descriptor["version"], descriptor["sha256"])
    if descriptor["id"] != expected_id:
        raise RuntimeError("managed CLI identity id does not match its metadata")
    return descriptor


def _identity_id_for(platform_name: str, machine: str, version: str, digest: str) -> str:
    safe_version = re.sub(r"[^A-Za-z0-9._-]", "-", version)
    safe_platform = re.sub(r"[^A-Za-z0-9._-]", "-", platform_name)
    safe_machine = re.sub(r"[^A-Za-z0-9._-]", "-", machine)
    return f"{safe_platform}-{safe_machine}-{safe_version}-{digest[:20]}"


def _capture(path: str, environ: dict[str, str] | None = None, *, source_label: str | None = None) -> dict[str, Any]:
    """Stage a signed native executable as a private immutable candidate.

    Capture never activates the candidate or touches Anthropic's launcher,
    authentication, updater configuration, or global paths.
    """
    if not isinstance(path, str) or not path.strip():
        raise ValueError("Claude capture path is required")
    env = _environment(environ)
    source_lexical = Path(os.path.abspath(str(_expand(path.strip(), env))))
    # Following a user's launcher here is acceptable: only the resulting native
    # file is copied, and the stored object itself is never a symlink.
    try:
        source = source_lexical.resolve(strict=True)
    except OSError as error:
        raise ValueError("Claude capture path does not resolve to a file") from error
    _native_check(source)
    version = _version(source, environ)
    digest = _sha256(source)
    platform_name = sys.platform
    machine = platform_module.machine()
    identity_id = _identity_id_for(platform_name, machine, version, digest)
    root_path = _ensure_root(environ)
    target = root_path / "versions" / identity_id
    if target.exists():
        return identity(identity_id, environ)
    staging = Path(tempfile.mkdtemp(prefix="capture-", dir=root_path / "versions"))
    try:
        executable = staging / "claude"
        shutil.copy2(source, executable, follow_symlinks=True)
        executable.chmod(0o700)
        if _sha256(executable) != digest:
            raise RuntimeError("captured Claude binary hash did not match its source")
        _native_check(executable)
        metadata = {"schema_version": SCHEMA_VERSION, "id": identity_id, "kind": "native_macho",
                    "version": version, "sha256": digest, "size": executable.stat().st_size,
                    "platform": platform_name, "machine": machine, "source": source_label or str(source_lexical),
                    "created_at": time.time(), "codesign_verified": True}
        _atomic_json(staging / "identity.json", metadata)
        try:
            os.replace(staging, target)
        except OSError:
            # On macOS, replacing a non-empty directory created by another
            # capture can be ENOTEMPTY rather than FileExistsError.  Treat it
            # as a successful concurrent capture only if the target is now a
            # fully revalidated immutable object; otherwise preserve the OS
            # failure rather than accepting a partial directory.
            if target.exists():
                return identity(identity_id, environ)
            raise
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return identity(identity_id, environ)


def capture(path: str, environ: dict[str, str] | None = None) -> dict[str, Any]:
    """Public native-only capture API for a local executable path."""
    return _capture(path, environ)


def _compatibility() -> Any:
    """Lazy import avoids a bridge/runtime import cycle in the standalone CLI."""
    scripts = Path(__file__).resolve().parents[1] / "skills/codex-claude-orchestrator/scripts"
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
    import compatibility  # type: ignore
    return compatibility


def _contract_id() -> str:
    value = _compatibility().bridge_contract_id()
    if not isinstance(value, str) or not value:
        raise RuntimeError("bridge compatibility contract id is unavailable")
    return value


def _qualification_dir(identity_id: str, contract_id: str, environ: dict[str, str] | None) -> Path:
    base, _ = _identity_path(identity_id, environ)
    token = hashlib.sha256(contract_id.encode("utf-8")).hexdigest()
    return base / "qualifications" / token


@contextmanager
def _qualification_lock(directory: Path) -> Iterator[None]:
    """Serialize aggregation while retaining one flock inode per contract."""
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(directory / "current.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _valid_matrix(matrix: dict[str, Any]) -> dict[str, dict[str, Any]]:
    if not isinstance(matrix, dict) or set(matrix) != _GROUPS:
        raise ValueError("qualification matrix must contain exactly core/read_only/write/resume/workflow")
    result: dict[str, dict[str, Any]] = {}
    for group in sorted(_GROUPS):
        value = matrix.get(group)
        if not isinstance(value, dict) or value.get("status") not in {"pass", "fail", "inconclusive"}:
            raise ValueError(f"qualification matrix {group} has an invalid status")
        if not isinstance(value.get("reason"), str) or not isinstance(value.get("evidence"), list) or not all(isinstance(item, str) for item in value["evidence"]):
            raise ValueError(f"qualification matrix {group} must include reason and evidence list")
        result[group] = {"status": value["status"], "reason": value["reason"], "evidence": list(value["evidence"])}
    return result


def _evidence_path(evidence_path: str, environ: dict[str, str] | None) -> tuple[Path, str]:
    if not isinstance(evidence_path, str) or not evidence_path:
        raise ValueError("qualification evidence path is required")
    try:
        evidence = Path(evidence_path).resolve(strict=True)
    except OSError as error:
        raise ValueError("qualification evidence path does not exist") from error
    jobs = (store_root(environ) / "jobs").resolve()
    if not evidence.is_file() or not evidence.is_relative_to(jobs):
        raise ValueError("qualification evidence must be a report file under this CLI store jobs directory")
    return evidence, _sha256(evidence)


def _completed_report(evidence: Path, evidence_digest: str, descriptor: dict[str, Any], contract_id: str,
                      matrix: dict[str, dict[str, Any]], environ: dict[str, str] | None) -> str:
    report = _read_json(evidence)
    jobs = (store_root(environ) / "jobs").resolve()
    relative = evidence.relative_to(jobs)
    job_id = relative.parts[0] if relative.parts else ""
    if (report is None or report.get("status") != "completed" or report.get("job_id") != job_id
            or report.get("identity_id") != descriptor["id"] or report.get("identity_sha256") != descriptor["sha256"]
            or report.get("bridge_contract_id") != contract_id):
        raise ValueError("qualification report is not a completed job bound to this identity and contract")
    if _valid_matrix(report.get("matrix")) != matrix:
        raise ValueError("qualification report matrix does not match the result being recorded")
    return hashlib.sha256((str(relative) + "\0" + evidence_digest).encode("utf-8")).hexdigest()


def _receipt_path(directory: Path, receipt_id: str) -> Path:
    if not re.fullmatch(r"[a-f0-9]{64}", receipt_id):
        raise RuntimeError("invalid qualification receipt id")
    return directory / "receipts" / f"{receipt_id}.json"


def _receipt(identity_id: str, contract_id: str, receipt_id: str, environ: dict[str, str] | None) -> dict[str, Any]:
    stored = _read_json(_receipt_path(_qualification_dir(identity_id, contract_id, environ), receipt_id))
    descriptor = identity(identity_id, environ)
    if (stored is None or stored.get("receipt_id") != receipt_id or stored.get("identity") != descriptor["id"]
            or stored.get("sha256") != descriptor["sha256"] or stored.get("bridge_contract_id") != contract_id
            or stored.get("source") != "local_qualification" or not isinstance(stored.get("evidence_sha256"), str)):
        raise RuntimeError("qualification receipt is not bound to this CLI identity and contract")
    stored["matrix"] = _valid_matrix(stored.get("matrix"))
    evidence, actual = _evidence_path(str(stored.get("evidence_path") or ""), environ)
    if actual != stored["evidence_sha256"]:
        raise RuntimeError(f"qualification evidence hash changed after recording: {evidence}")
    return stored


def _aggregate_receipts(receipts: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Merge local evidence; confirmed failures remain a hard capability block."""
    aggregate = {group: {"status": "inconclusive", "reason": "no completed qualification has passed this group", "evidence": []}
                 for group in sorted(_GROUPS)}
    for receipt in sorted(receipts, key=lambda value: (value.get("recorded_at", 0), value["receipt_id"])):
        for group in sorted(_GROUPS):
            candidate = receipt["matrix"][group]
            previous = aggregate[group]
            if previous["status"] == "fail":
                continue
            if candidate["status"] == "fail":
                aggregate[group] = {"status": "fail", "reason": candidate["reason"], "evidence": list(candidate["evidence"])}
            elif candidate["status"] == "pass":
                if previous["status"] != "pass":
                    aggregate[group] = {"status": "pass", "reason": candidate["reason"], "evidence": list(candidate["evidence"])}
                else:
                    aggregate[group]["evidence"] = list(dict.fromkeys([*previous["evidence"], *candidate["evidence"]]))
            elif previous["status"] != "pass":
                aggregate[group] = {"status": candidate["status"], "reason": candidate["reason"], "evidence": list(candidate["evidence"])}
    return aggregate


def _merge_historical_matrix(historical: dict[str, Any] | None,
                             local: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Keep released profile support through inconclusive local rechecks.

    A real local ``fail`` is different from an unavailable network/tool attempt:
    it revokes that group for this exact identity.  Inconclusive rechecks only
    add history and never turn a known working bundled capability off.
    """
    combined = {group: {"status": value["status"], "reason": value["reason"], "evidence": list(value["evidence"])}
                for group, value in local.items()}
    if historical is None:
        return combined
    historic_matrix = _valid_matrix(historical["matrix"])
    for group in sorted(_GROUPS):
        if local[group]["status"] == "fail":
            continue
        if historic_matrix[group]["status"] == "pass":
            if local[group]["status"] == "pass":
                combined[group]["evidence"] = list(dict.fromkeys([*historic_matrix[group]["evidence"], *local[group]["evidence"]]))
            else:
                combined[group] = {"status": "pass", "reason": historic_matrix[group]["reason"],
                                   "evidence": list(historic_matrix[group]["evidence"])}
    return combined


def record_qualification(identity_id: str, contract_id: str, matrix: dict[str, Any], evidence_path: str,
                         environ: dict[str, str] | None = None) -> dict[str, Any]:
    """Persist an immutable bridge-bound qualification receipt."""
    descriptor = identity(identity_id, environ)
    if not isinstance(contract_id, str) or not contract_id.strip():
        raise ValueError("bridge contract id is required")
    normalized = _valid_matrix(matrix)
    evidence, evidence_digest = _evidence_path(evidence_path, environ)
    receipt_id = _completed_report(evidence, evidence_digest, descriptor, contract_id, normalized, environ)
    directory = _qualification_dir(identity_id, contract_id, environ)
    receipt = {"schema_version": SCHEMA_VERSION, "receipt_id": receipt_id, "identity": descriptor["id"],
               "sha256": descriptor["sha256"], "bridge_contract_id": contract_id, "suite_revision": contract_id,
               "matrix": normalized, "source": "local_qualification", "evidence_path": str(evidence),
               "evidence_sha256": evidence_digest, "recorded_at": time.time()}
    location = _receipt_path(directory, receipt_id)
    current = _read_json(location)
    if current is not None:
        comparable = {key: current.get(key) for key in receipt if key != "recorded_at"}
        expected = {key: receipt[key] for key in receipt if key != "recorded_at"}
        if comparable != expected:
            raise RuntimeError("qualification receipt identity collision does not match its immutable evidence")
    else:
        _atomic_json(location, receipt)
    # A receipt is immutable.  The small current pointer is the only mutable
    # object, atomically recomputed from every retained receipt under its lock.
    with _qualification_lock(directory):
        receipt_ids = sorted(path.stem for path in (directory / "receipts").glob("*.json") if re.fullmatch(r"[a-f0-9]{64}", path.stem))
        receipts = [_receipt(identity_id, contract_id, item, environ) for item in receipt_ids]
        local_matrix = _aggregate_receipts(receipts)
        historical = _historical_qualification(descriptor, contract_id)
        aggregate = _merge_historical_matrix(historical, local_matrix)
        pointer = {"schema_version": SCHEMA_VERSION, "identity": descriptor["id"], "sha256": descriptor["sha256"],
                   "bridge_contract_id": contract_id, "suite_revision": contract_id, "matrix": aggregate,
                   "local_matrix": local_matrix, "historical_matrix": historical["matrix"] if historical else None,
                   "source": "combined_qualification" if historical else "local_qualification", "receipt_ids": receipt_ids,
                   "evidence_path": str(evidence), "evidence_paths": [item["evidence_path"] for item in receipts],
                   "updated_at": time.time()}
        _atomic_json(directory / "current.json", pointer)
    return pointer


def _historical_qualification(descriptor: dict[str, Any], contract_id: str) -> dict[str, Any] | None:
    compatibility = _compatibility()
    profile = getattr(compatibility, "SUPPORTED_CLI_PROFILES", {}).get(descriptor["version"])
    if not isinstance(profile, dict) or not profile.get("tested"):
        return None
    # Bundled exact profiles are release evidence maintained with this plugin;
    # unlike a local receipt they are not a stale on-disk qualification for an
    # older contract.  A future profile may bind an explicit historical suite
    # revision, but absence of that optional field must not erase the released
    # 2.1.278 bootstrap path.
    profile_contract = profile.get("bridge_contract_id", "bundled_profile")
    groups = profile.get("groups")
    if profile.get("source") != "bundled_profile" or not isinstance(groups, (list, tuple, set)):
        return None
    matrix = {group: {"status": "pass" if group in groups else "inconclusive",
                      "reason": "historical bundled compatibility profile" if group in groups else "not historically qualified",
                      "evidence": ["compatibility.py"] if group in groups else []}
              for group in sorted(_GROUPS)}
    return {"schema_version": SCHEMA_VERSION, "identity": descriptor["id"], "sha256": descriptor["sha256"],
            "bridge_contract_id": contract_id, "suite_revision": profile_contract, "matrix": matrix,
            "source": "bundled_profile", "evidence_path": "compatibility.py"}


def qualification(identity_id: str, contract_id: str, environ: dict[str, str] | None = None) -> dict[str, Any] | None:
    """Return a receipt bound to exactly this identity and bridge contract."""
    descriptor = identity(identity_id, environ)
    if not isinstance(contract_id, str) or not contract_id:
        raise ValueError("bridge contract id is required")
    directory = _qualification_dir(identity_id, contract_id, environ)
    stored = _read_json(directory / "current.json")
    if stored is not None:
        if (stored.get("identity") != descriptor["id"] or stored.get("sha256") != descriptor["sha256"]
                or stored.get("bridge_contract_id") != contract_id or stored.get("source") not in {"local_qualification", "combined_qualification"}):
            raise RuntimeError("qualification receipt is not bound to this CLI identity and contract")
        stored["matrix"] = _valid_matrix(stored.get("matrix"))
        receipt_ids = stored.get("receipt_ids")
        if not isinstance(receipt_ids, list) or not receipt_ids:
            raise RuntimeError("qualification current pointer has no immutable receipts")
        receipts = [_receipt(identity_id, contract_id, item, environ) for item in receipt_ids]
        local_matrix = _aggregate_receipts(receipts)
        historical = _historical_qualification(descriptor, contract_id)
        if stored.get("local_matrix") != local_matrix or stored.get("historical_matrix") != (historical["matrix"] if historical else None):
            raise RuntimeError("qualification current pointer does not match its immutable receipts and historical profile")
        if _merge_historical_matrix(historical, local_matrix) != stored["matrix"]:
            raise RuntimeError("qualification current pointer does not match its immutable receipts")
        return stored
    return _historical_qualification(descriptor, contract_id)


def _passes(receipt: dict[str, Any] | None, groups: list[str]) -> bool:
    if receipt is None:
        return False
    matrix = receipt.get("matrix")
    return isinstance(matrix, dict) and all(isinstance(matrix.get(group), dict) and matrix[group].get("status") == "pass" for group in groups)


def recorded_qualification(identity_id: str, contract_id: str,
                           environ: dict[str, str] | None = None) -> dict[str, Any] | None:
    """Read lightweight current-contract status evidence for UI polling.

    It intentionally does not validate executable bytes or immutable evidence
    files.  Call ``qualification`` before any execution or selection mutation.
    """
    descriptor = identity_metadata(identity_id, environ)
    historical = _historical_qualification(descriptor, contract_id)
    directory = _qualification_dir(identity_id, contract_id, environ)
    stored = _read_json(directory / "current.json")
    if stored is None:
        return historical
    if (stored.get("identity") != descriptor["id"] or stored.get("sha256") != descriptor["sha256"]
            or stored.get("bridge_contract_id") != contract_id
            or stored.get("source") not in {"local_qualification", "combined_qualification"}):
        return None
    try:
        stored["matrix"] = _valid_matrix(stored.get("matrix"))
    except ValueError:
        return None
    return stored


def eligible(identity_id: str, required_groups: list[str], contract_id: str,
             environ: dict[str, str] | None = None, *,
             required_capabilities: list[str] | None = None) -> bool:
    """Fully revalidate an identity and its current compatibility evidence."""
    groups = _required_groups(required_groups)
    capabilities = _required_capabilities(required_capabilities)
    descriptor = identity(identity_id, environ)
    return _passes(qualification(identity_id, contract_id, environ), groups) and _has_capabilities(descriptor, capabilities)


def _required_groups(required_groups: list[str]) -> list[str]:
    if not isinstance(required_groups, list) or not required_groups:
        raise ValueError("at least one required CLI compatibility group is required")
    if any(group not in _GROUPS for group in required_groups):
        raise ValueError("unknown CLI compatibility group")
    return list(dict.fromkeys(required_groups))


def _required_capabilities(required_capabilities: list[str] | None) -> list[str]:
    if required_capabilities is None:
        return []
    if (not isinstance(required_capabilities, list)
            or any(not isinstance(item, str) or not item for item in required_capabilities)):
        raise ValueError("required CLI capabilities must be a list of non-empty names")
    return list(dict.fromkeys(required_capabilities))


def _has_capabilities(descriptor: dict[str, Any], capabilities: list[str]) -> bool:
    if not capabilities:
        return True
    # Syntax/help output and an arbitrary local receipt do not grant optional
    # invocation capabilities.  Only the installed plugin's released profile
    # is an authority for these flags.
    profile = _compatibility().profile_for(descriptor["version"])
    declared = profile.get("capabilities") if isinstance(profile, dict) and profile.get("tested") is True else None
    return isinstance(declared, dict) and all(name in declared for name in capabilities)


def select(required_groups: list[str], contract_id: str, environ: dict[str, str] | None = None, *,
           required_capabilities: list[str] | None = None) -> dict[str, Any] | None:
    """Select the first retained managed identity qualified for all groups."""
    groups = _required_groups(required_groups)
    capabilities = _required_capabilities(required_capabilities)
    selection = get_selection(environ)
    if selection["mode"] != "managed":
        return None
    candidates = [selection["active"], selection["previous"], *selection["history"]]
    seen: set[str] = set()
    for position, identity_id in enumerate(candidates):
        if identity_id is None or identity_id in seen:
            continue
        seen.add(identity_id)
        # A selected object that has drifted is a hard stop.  Trying PATH or a
        # later history entry would defeat the immutable selection invariant.
        try:
            descriptor = identity(identity_id, environ)
        except ValueError:
            if position == 0 and selection["active"] == identity_id:
                raise RuntimeError("active managed CLI identity is missing; repair or explicitly roll back")
            continue
        receipt = qualification(identity_id, contract_id, environ)
        if _passes(receipt, groups) and _has_capabilities(descriptor, capabilities):
            reason = "active" if identity_id == selection["active"] else ("previous" if identity_id == selection["previous"] else "history")
            return {**descriptor, "generation": selection["generation"], "qualification": receipt,
                    "selection_reason": reason}
    return None


def dispatch_metadata(required_groups: list[str], contract_id: str,
                      environ: dict[str, str] | None = None) -> dict[str, Any] | None:
    """Preview the next managed dispatch using lightweight retained metadata.

    This status-only helper mirrors selection order and current-contract group
    eligibility without hashing or codesigning large executables.  Real
    dispatch continues to call :func:`select`, which fully verifies identity,
    receipts, and optional capability declarations before execution.
    """
    groups = _required_groups(required_groups)
    selection = get_selection(environ)
    if selection["mode"] != "managed":
        return None
    candidates = [selection["active"], selection["previous"], *selection["history"]]
    seen: set[str] = set()
    for identity_id in candidates:
        if identity_id is None or identity_id in seen:
            continue
        seen.add(identity_id)
        try:
            descriptor = identity_metadata(identity_id, environ)
            receipt = recorded_qualification(identity_id, contract_id, environ)
        except ValueError as exc:
            if identity_id == selection["active"]:
                raise RuntimeError(
                    "active managed CLI identity is missing; repair or explicitly roll back") from exc
            continue
        if _passes(receipt, groups):
            reason = ("active" if identity_id == selection["active"] else
                      "previous" if identity_id == selection["previous"] else "history")
            return {"id": descriptor["id"], "version": descriptor["version"],
                    "sha256": descriptor["sha256"], "selection_reason": reason,
                    "selection_generation": selection["generation"]}
    return None


def _eligible_for_activation(identity_id: str, environ: dict[str, str] | None) -> tuple[dict[str, Any], dict[str, Any]]:
    descriptor = identity(identity_id, environ)
    receipt = qualification(identity_id, _contract_id(), environ)
    if not _passes(receipt, ["core", "read_only"]):
        raise RuntimeError("CLI identity needs a current core/read_only qualification before activation")
    return descriptor, receipt


def activate(identity_id: str, expected_generation: int | None = None,
             environ: dict[str, str] | None = None) -> dict[str, Any]:
    """CAS-switch the active pointer after core/read_only qualification."""
    _eligible_for_activation(identity_id, environ)
    root_path = _ensure_root(environ)
    with _selection_lock(root_path):
        current = _selection_under_lock(root_path)
        if expected_generation is not None and current["generation"] != expected_generation:
            raise RuntimeError("CLI selection changed before activation; refresh generation and retry deliberately")
        if current["active"] == identity_id and current["mode"] == "managed":
            return current
        previous = current["active"] if current["active"] != identity_id else current["previous"]
        history = list(dict.fromkeys([*current["history"], *([current["active"]] if current["active"] else []),
                                      *([current["previous"]] if current["previous"] else []), identity_id]))
        changed = {"schema_version": SCHEMA_VERSION, "generation": current["generation"] + 1,
                   "active": identity_id, "previous": previous, "history": history, "mode": "managed"}
        _atomic_json(root_path / _SELECTION_NAME, changed)
        return changed


def activate_explicit(identity_id: str, expected_generation: int | None = None,
                      reason: str = "manual_activation",
                      environ: dict[str, str] | None = None) -> dict[str, Any]:
    """Validate and explicitly switch selection plus the manual hold in one lock.

    All CAS and eligibility failures happen before either persistent authority is
    changed.  The selection is restored if the subsequent policy write fails,
    so a reported failure does not silently pause automatic maintenance.
    """
    root_path = _ensure_root(environ)
    with _selection_lock(root_path):
        current = _selection_under_lock(root_path)
        if expected_generation is not None and current["generation"] != expected_generation:
            raise RuntimeError("CLI selection changed before activation; refresh generation and retry deliberately")
        _eligible_for_activation(identity_id, environ)
        current_policy, policy = _manual_policy_under_lock(root_path, reason)
        if current["active"] == identity_id and current["mode"] == "managed":
            changed = current
        else:
            previous = current["active"] if current["active"] != identity_id else current["previous"]
            history = list(dict.fromkeys([*current["history"],
                                          *([current["active"]] if current["active"] else []),
                                          *([current["previous"]] if current["previous"] else []),
                                          identity_id]))
            changed = {"schema_version": SCHEMA_VERSION, "generation": current["generation"] + 1,
                       "active": identity_id, "previous": previous,
                       "history": history, "mode": "managed"}
        if changed != current or policy != current_policy:
            _commit_explicit_transaction_under_lock(root_path, current, current_policy,
                                                    changed, policy)
        return {**changed, "update_policy": policy}


def activate_automatic(identity_id: str, expected_generation: int,
                       expected_policy_generation: int,
                       target_version: str,
                       environ: dict[str, str] | None = None) -> dict[str, Any]:
    """CAS-activate an audited target only while automatic policy still wins.

    Eligibility is checked before taking the short shared lock.  The lock then
    linearizes the final switch with explicit policy changes and manual
    selection/rollback operations.
    """
    descriptor, _ = _eligible_for_activation(identity_id, environ)
    if descriptor["version"] != target_version:
        raise RuntimeError("automatic CLI candidate does not match the requested target version")
    before = get_selection(environ)
    active_descriptor: dict[str, Any] | None = None
    active_eligible = False
    active_receipt: dict[str, Any] | None = None
    if before["generation"] == expected_generation and before.get("active"):
        try:
            active_descriptor = identity(before["active"], environ)
            active_receipt = qualification(before["active"], _contract_id(), environ)
            active_eligible = _passes(active_receipt, ["core", "read_only"])
        except (OSError, RuntimeError, ValueError):
            active_descriptor = None
    root_path = _ensure_root(environ)
    with _selection_lock(root_path):
        current = _selection_under_lock(root_path)
        policy = _effective_update_policy(_update_policy_under_lock(root_path),
                                          _update_config_under_lock(root_path))
        if current["generation"] != expected_generation:
            raise AutomaticActivationSuperseded(
                "CLI selection changed during automatic acquisition; keeping the explicit selection")
        if policy["generation"] != expected_policy_generation or policy["mode"] != "automatic":
            raise AutomaticActivationSuperseded(
                "CLI update policy changed during automatic acquisition; keeping the current selection")
        if current["mode"] != "managed":
            raise AutomaticActivationSuperseded("external CLI mode was selected during automatic acquisition")
        if current["active"] == identity_id:
            return current
        if active_eligible and active_descriptor is not None and current["active"] == active_descriptor["id"]:
            recorded = recorded_qualification(active_descriptor["id"], _contract_id(), environ)
            evidence_unchanged = (recorded is not None and active_receipt is not None
                                  and recorded.get("matrix") == active_receipt.get("matrix")
                                  and recorded.get("receipt_ids") == active_receipt.get("receipt_ids")
                                  and recorded.get("source") == active_receipt.get("source"))
            if evidence_unchanged and version_key(active_descriptor["version"]) >= version_key(target_version):
                return current
        previous = current["active"]
        history = list(dict.fromkeys([*current["history"], *([current["active"]] if current["active"] else []),
                                      *([current["previous"]] if current["previous"] else []), identity_id]))
        changed = {"schema_version": SCHEMA_VERSION, "generation": current["generation"] + 1,
                   "active": identity_id, "previous": previous, "history": history, "mode": "managed"}
        _atomic_json(root_path / _SELECTION_NAME, changed)
        return changed


def set_external_mode(environ: dict[str, str] | None = None) -> dict[str, Any]:
    """Explicitly choose an externally managed CLI without deleting retention.

    This is used only by the user-facing explicit external-path configuration.
    It never results from a missing managed object or a failed qualification.
    """
    root_path = _ensure_root(environ)
    with _selection_lock(root_path):
        current = _selection_under_lock(root_path)
        if current["mode"] == "external":
            return current
        changed = {**current, "generation": current["generation"] + 1, "mode": "external"}
        _atomic_json(root_path / _SELECTION_NAME, changed)
        return changed


def rollback(expected_generation: int | None = None, environ: dict[str, str] | None = None) -> dict[str, Any]:
    """Atomically select the retained previous qualified identity."""
    root_path = _ensure_root(environ)
    with _selection_lock(root_path):
        current = _selection_under_lock(root_path)
        if expected_generation is not None and current["generation"] != expected_generation:
            raise RuntimeError("CLI selection changed before rollback; refresh generation and retry deliberately")
        previous = current["previous"]
        if not previous:
            raise RuntimeError("no retained previous CLI identity is available for rollback")
        _eligible_for_activation(previous, environ)
        changed = {"schema_version": SCHEMA_VERSION, "generation": current["generation"] + 1,
                   "active": previous, "previous": current["active"], "history": current["history"], "mode": "managed"}
        _atomic_json(root_path / _SELECTION_NAME, changed)
        return changed


def rollback_explicit(expected_generation: int | None = None,
                      reason: str = "manual_rollback",
                      environ: dict[str, str] | None = None) -> dict[str, Any]:
    """Validate and explicitly roll back selection plus the manual hold in one lock."""
    root_path = _ensure_root(environ)
    with _selection_lock(root_path):
        current = _selection_under_lock(root_path)
        if expected_generation is not None and current["generation"] != expected_generation:
            raise RuntimeError("CLI selection changed before rollback; refresh generation and retry deliberately")
        previous = current["previous"]
        if not previous:
            raise RuntimeError("no retained previous CLI identity is available for rollback")
        _eligible_for_activation(previous, environ)
        current_policy, policy = _manual_policy_under_lock(root_path, reason)
        changed = {"schema_version": SCHEMA_VERSION, "generation": current["generation"] + 1,
                   "active": previous, "previous": current["active"],
                   "history": current["history"], "mode": "managed"}
        _commit_explicit_transaction_under_lock(root_path, current, current_policy,
                                                changed, policy)
        return {**changed, "update_policy": policy}


def _recorded_size(identity_id: str, environ: dict[str, str] | None) -> dict[str, Any]:
    """Report the identity.json size only when it matches the retained file."""
    base, metadata_path = _identity_path(identity_id, environ)
    try:
        metadata = _read_json(metadata_path) or {}
        actual = (base / "claude").stat().st_size
    except (OSError, RuntimeError):
        return {"size": None, "size_status": "unreadable"}
    recorded = metadata.get("size")
    if type(recorded) is not int or recorded < 0:
        return {"size": None, "size_status": "not_recorded"}
    if recorded != actual:
        return {"size": None, "size_status": "mismatch"}
    return {"size": recorded, "size_status": "recorded_matches_file"}


def inventory(environ: dict[str, str] | None = None) -> dict[str, Any]:
    """List retained private descriptors without external CLI discovery."""
    root_path = store_root(environ)
    versions: list[dict[str, Any]] = []
    versions_root = root_path / "versions"
    if versions_root.is_dir() and not versions_root.is_symlink():
        for entry in sorted(versions_root.iterdir(), key=lambda item: item.name):
            if entry.is_dir() and not entry.is_symlink():
                try:
                    versions.append({**identity(entry.name, environ), **_recorded_size(entry.name, environ)})
                except (ValueError, RuntimeError, OSError) as error:
                    versions.append({"id": entry.name, "integrity": "failed", "error": str(error)})
    return {"root": str(root_path), "selection": get_selection(environ), "versions": versions}


def legacy_records(environ: dict[str, str] | None = None) -> dict[str, Any]:
    """Summarize retained managed-CLI history without locks, writes or hashing.

    Nothing here is executable authority: the dispatch path never reads it.
    Files are left in place because they are the user's historical evidence.
    """
    root_path = store_root(environ)
    result: dict[str, Any] = {"root": str(root_path), "present": root_path.is_dir(),
                              "ignored_for_dispatch": True, "files_deleted": False,
                              "selection": None, "versions": []}
    if not result["present"]:
        result["storage_summary"] = {"retained_bytes": 0, "unmeasured_versions": 0}
        return result
    try:
        selection = _read_json(root_path / _SELECTION_NAME)
    except RuntimeError as error:
        result["selection_error"] = str(error)
    else:
        if selection is not None:
            result["selection"] = {key: selection.get(key) for key in ("mode", "active", "previous", "generation")}
    versions_root = root_path / "versions"
    if versions_root.is_dir() and not versions_root.is_symlink():
        for entry in sorted(versions_root.iterdir(), key=lambda item: item.name):
            if not entry.is_dir() or entry.is_symlink() or not _IDENTITY_ID.fullmatch(entry.name):
                continue
            try:
                metadata = identity_metadata(entry.name, environ)
            except (ValueError, RuntimeError, OSError) as error:
                result["versions"].append({"id": entry.name, "metadata": "unreadable", "error": str(error),
                                           "size": None, "size_status": "unreadable"})
                continue
            result["versions"].append({"id": metadata["id"], "version": metadata["version"],
                                       "source": metadata["source"], "created_at": metadata["created_at"],
                                       **_recorded_size(entry.name, environ)})
    sizes = [item["size"] for item in result["versions"] if type(item.get("size")) is int]
    result["storage_summary"] = {"retained_bytes": sum(sizes),
                                 "unmeasured_versions": len(result["versions"]) - len(sizes),
                                 "excludes": ["download_partials", "run_evidence"],
                                 "automatic_cleanup": False}
    return result


def version_key(version: str) -> tuple[int, int, int, int, str]:
    """Return a numeric release ordering key; stable releases sort last."""
    match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)(?:([-+])([A-Za-z0-9._-]+))?", version)
    if match is None:
        raise ValueError(f"invalid CLI release version: {version}")
    suffix_kind, suffix = match.group(4), match.group(5) or ""
    # A pre-release is below the corresponding stable release.  Build metadata
    # does not make a release newer than its stable semantic version.
    stability = 0 if suffix_kind == "-" else 1
    if suffix_kind != "-":
        suffix = ""
    return int(match.group(1)), int(match.group(2)), int(match.group(3)), stability, suffix


def _official_platform() -> str:
    if sys.platform != "darwin":
        raise RuntimeError("official private bootstrap currently supports macOS native Claude Code only")
    machine = platform_module.machine().lower()
    if machine in {"arm64", "aarch64"}:
        arch = "arm64"
    elif machine in {"x86_64", "amd64"}:
        # Anthropic's official install.sh selects arm64 when an x64 shell is
        # running under Rosetta on Apple Silicon.  Match that behavior without
        # reading any shell configuration.
        translated = subprocess.run(["sysctl", "-n", "sysctl.proc_translated"], text=True,
                                    capture_output=True, timeout=5, check=False)
        arch = "arm64" if translated.returncode == 0 and translated.stdout.strip() == "1" else "x64"
    else:
        raise RuntimeError(f"unsupported macOS machine for official Claude bootstrap: {machine}")
    return f"darwin-{arch}"


def official_release_target(version: str | None = None,
                            environ: dict[str, str] | None = None) -> dict[str, Any]:
    """Resolve an installed-plugin release target with audited platform bytes."""
    platform_name = _official_platform()
    supported = _compatibility().SUPPORTED_CLI_PROFILES
    candidates = [item for item, platforms in OFFICIAL_RELEASE_MATRIX.items()
                  if item in supported and platform_name in platforms]
    if version is None:
        if not candidates:
            raise RuntimeError(f"no audited supported CLI release is available for {platform_name}")
        version = max(candidates, key=version_key)
    if version not in candidates:
        raise ValueError("requested CLI release is not audited and supported by this installed plugin")
    expected = OFFICIAL_RELEASE_MATRIX[version][platform_name]
    return {"version": version, "platform": platform_name,
            "sha256": expected["sha256"], "size": expected["size"],
            "url": f"{OFFICIAL_RELEASES}/{version}/{platform_name}/claude",
            "source": "bundled", "scope": "installed_plugin_release"}


def _validated_release_target(target: dict[str, Any]) -> dict[str, Any]:
    """Validate a bundled or previously signature-verified official target."""
    if not isinstance(target, dict):
        raise ValueError("official Claude release target must be an object")
    required = {key: target.get(key) for key in
                ("version", "platform", "sha256", "size", "url", "source", "scope")}
    version_key(str(required["version"]))
    if required["platform"] != _official_platform():
        raise ValueError("official Claude release target platform does not match this machine")
    if not isinstance(required["sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", required["sha256"]):
        raise ValueError("official Claude release target has an invalid SHA-256")
    if not isinstance(required["size"], int) or isinstance(required["size"], bool) or required["size"] <= 0:
        raise ValueError("official Claude release target has an invalid size")
    expected_url = f"{OFFICIAL_RELEASES}/{required['version']}/{required['platform']}/claude"
    if required["url"] != expected_url:
        raise ValueError("official Claude release target URL is not the published distribution object")
    if (required["source"], required["scope"]) == ("bundled", "installed_plugin_release"):
        expected = official_release_target(str(required["version"]))
        if any(required[key] != expected[key] for key in required):
            raise ValueError("bundled Claude release target differs from the audited matrix")
    elif (required["source"], required["scope"]) == ("official", "official_latest"):
        fingerprint = target.get("signing_key_fingerprint")
        manifest_sha = target.get("manifest_sha256")
        from official_releases import RELEASE_KEY_FINGERPRINT
        if fingerprint != RELEASE_KEY_FINGERPRINT:
            raise ValueError("official latest target lacks the pinned Anthropic signing-key proof")
        if not isinstance(manifest_sha, str) or not re.fullmatch(r"[0-9a-f]{64}", manifest_sha):
            raise ValueError("official latest target lacks a verified manifest digest")
    else:
        raise ValueError("official Claude release target has an unsupported provenance")
    return dict(target)


@contextmanager
def _download_lock(path: Path, *, wait_timeout_seconds: float = DOWNLOAD_LOCK_TIMEOUT_SECONDS,
                   monotonic: Callable[[], float] | None = None,
                   sleeper: Callable[[float], None] | None = None) -> Iterator[None]:
    """Serialize one exact target with a persistent inode and bounded wait."""
    if (not isinstance(wait_timeout_seconds, (int, float))
            or wait_timeout_seconds <= 0 or wait_timeout_seconds > 5 * 60):
        raise ValueError("official Claude download lock wait timeout is invalid")
    monotonic = monotonic or time.monotonic
    sleeper = sleeper or time.sleep
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    acquired = False
    try:
        deadline = monotonic() + float(wait_timeout_seconds)
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except BlockingIOError:
                remaining = deadline - monotonic()
                if remaining <= 0:
                    raise RuntimeError(
                        "another official Claude acquisition is still in progress; retry later and keep its resumable partial")
                sleeper(min(0.1, remaining))
        yield
    finally:
        if acquired:
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _response_status(response: Any) -> int:
    status = getattr(response, "status", None)
    if status is None and hasattr(response, "getcode"):
        status = response.getcode()
    if not isinstance(status, int):
        raise RuntimeError("official Claude download returned no HTTP status")
    return status


def _response_header(response: Any, name: str) -> str | None:
    headers = getattr(response, "headers", None)
    value = headers.get(name) if headers is not None and hasattr(headers, "get") else None
    return value.strip() if isinstance(value, str) else None


def _download(url: str, destination: Path, *, expected_size: int | None = None,
              idle_timeout_seconds: float = DOWNLOAD_IDLE_TIMEOUT_SECONDS,
              attempt_timeout_seconds: float = DOWNLOAD_ATTEMPT_TIMEOUT_SECONDS,
              progress: Callable[[int, int | None], None] | None = None,
              opener: Callable[..., Any] | None = None,
              monotonic: Callable[[], float] | None = None,
              _already_locked: bool = False) -> None:
    """Fetch a fixed official object with validated HTTP resume semantics.

    Ordinary interruption and bounded timeout leave ``destination`` in place.
    The next call requests exactly the missing suffix.  The caller is still
    responsible for digest and native-signature verification before use.
    """
    for name, value, maximum in (("idle", idle_timeout_seconds, 30 * 60),
                                 ("attempt", attempt_timeout_seconds, 24 * 60 * 60)):
        if not isinstance(value, (int, float)) or value <= 0 or value > maximum:
            raise ValueError(f"official Claude download {name} timeout is invalid")
    if expected_size is not None and (not isinstance(expected_size, int) or expected_size <= 0):
        raise ValueError("official Claude expected size must be a positive integer")
    if not _already_locked:
        with _download_lock(destination.with_name(destination.name + ".lock")):
            return _download(url, destination, expected_size=expected_size,
                             idle_timeout_seconds=idle_timeout_seconds,
                             attempt_timeout_seconds=attempt_timeout_seconds,
                             progress=progress, opener=opener, monotonic=monotonic,
                             _already_locked=True)

    opener = opener or urlopen
    monotonic = monotonic or time.monotonic
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    received = destination.stat().st_size if destination.exists() else 0
    if expected_size is not None and received > expected_size:
        destination.unlink()
        received = 0
    headers = {"User-Agent": "codex-claude-orchestrator-private-bootstrap/0.4.4"}
    if received:
        headers["Range"] = f"bytes={received}-"
    request = Request(url, headers=headers)
    attempt_started = monotonic()
    last_progress = attempt_started
    if progress is not None:
        progress(received, expected_size)

    response: Any
    try:
        response = opener(request, timeout=min(float(idle_timeout_seconds),
                                               float(attempt_timeout_seconds)))
    except HTTPError as error:
        if error.code != 416:
            raise RuntimeError(f"official Claude download HTTP {error.code} for {url}") from error
        content_range = error.headers.get("Content-Range") if error.headers is not None else None
        match = _UNSATISFIED_CONTENT_RANGE.fullmatch(content_range.strip()) if isinstance(content_range, str) else None
        total = int(match.group(1)) if match else None
        if received and total == received and (expected_size is None or total == expected_size):
            if progress is not None:
                progress(received, total)
            return
        raise RuntimeError("official Claude download returned HTTP 416 for an incomplete or mismatched partial") from error
    except (OSError, URLError, HTTPException) as error:
        raise RuntimeError(f"official Claude download failed for {url}; resumable partial retained: {error}") from error

    try:
        with response:
            status = _response_status(response)
            content_range = _response_header(response, "Content-Range")
            total = expected_size
            append = False
            write_destination = destination
            restart_destination: Path | None = None
            if status == 206:
                match = _CONTENT_RANGE.fullmatch(content_range or "")
                if match is None:
                    raise RuntimeError("official Claude HTTP 206 response had an invalid Content-Range")
                start, end, range_total = (int(match.group(index)) for index in range(1, 4))
                if start != received:
                    raise RuntimeError("official Claude HTTP 206 response did not start at the requested offset")
                if end < start or end >= range_total:
                    raise RuntimeError("official Claude HTTP 206 response had invalid range bounds")
                if expected_size is not None and range_total != expected_size:
                    raise RuntimeError("official Claude HTTP 206 total did not match the signed manifest size")
                total = range_total
                append = received > 0
                expected_response_bytes = end - start + 1
            elif status == 200:
                # Servers and intermediaries may ignore Range.  A complete 200
                # response restarts from zero.  Keep the older prefix intact
                # until the replacement has completed, so a second failure
                # cannot turn useful resumable progress into a shorter file.
                if received:
                    restart_destination = destination.with_name(destination.name + ".restart")
                    write_destination = restart_destination
                received = 0
                total = expected_size
                expected_response_bytes = None
                if progress is not None:
                    progress(0, total)
            else:
                raise RuntimeError(f"official Claude download returned unexpected HTTP {status}")

            with write_destination.open("ab" if append else "wb") as handle:
                response_received = 0
                while True:
                    now = monotonic()
                    if now - attempt_started >= attempt_timeout_seconds:
                        raise RuntimeError(
                            f"official Claude download attempt exceeded {attempt_timeout_seconds:g} seconds; resumable partial retained")
                    if now - last_progress >= idle_timeout_seconds:
                        raise RuntimeError(
                            f"official Claude download made no progress for {idle_timeout_seconds:g} seconds; resumable partial retained")
                    # HTTPResponse.read1 returns currently available buffered
                    # bytes rather than waiting to fill the requested size.
                    # Fake/minimal response objects keep the read fallback.
                    reader = getattr(response, "read1", None) or response.read
                    incomplete: IncompleteRead | None = None
                    try:
                        block = reader(64 * 1024)
                    except IncompleteRead as error:
                        block = error.partial or b""
                        incomplete = error
                    after_read = monotonic()
                    if not block:
                        if incomplete is not None:
                            raise RuntimeError(
                                "official Claude response ended incompletely; resumable partial retained") from incomplete
                        break
                    if after_read - attempt_started >= attempt_timeout_seconds:
                        raise RuntimeError(
                            f"official Claude download attempt exceeded {attempt_timeout_seconds:g} seconds; resumable partial retained")
                    if after_read - last_progress >= idle_timeout_seconds:
                        raise RuntimeError(
                            f"official Claude download made no progress for {idle_timeout_seconds:g} seconds; resumable partial retained")
                    if (expected_response_bytes is not None
                            and response_received + len(block) > expected_response_bytes):
                        raise RuntimeError("official Claude HTTP 206 body exceeded its declared Content-Range")
                    if expected_size is not None and received + len(block) > expected_size:
                        raise RuntimeError("official Claude binary exceeded its signed manifest size")
                    handle.write(block)
                    handle.flush()
                    received += len(block)
                    response_received += len(block)
                    last_progress = after_read
                    if progress is not None:
                        progress(received, total)
                    if incomplete is not None:
                        raise RuntimeError(
                            "official Claude response ended incompletely; resumable partial retained") from incomplete
                if (expected_response_bytes is not None
                        and response_received != expected_response_bytes):
                    raise RuntimeError(
                        "official Claude HTTP 206 body length did not match its declared Content-Range; resumable partial retained")
    except (OSError, URLError, HTTPException) as error:
        raise RuntimeError(f"official Claude download failed for {url}; resumable partial retained: {error}") from error

    if expected_size is not None and received != expected_size:
        raise RuntimeError(
            f"official Claude download ended at {received} of {expected_size} bytes; resumable partial retained")
    if restart_destination is not None:
        os.replace(restart_destination, destination)


def _download_partial_path(target: dict[str, Any], environ: dict[str, str] | None) -> Path:
    cache_key = f"{target['version']}-{target['platform']}-{target['sha256'][:16]}"
    return store_root(environ) / "downloads" / cache_key / "claude.part"


def _restore_verified_download(binary: Path, target: dict[str, Any], environ: dict[str, str] | None) -> None:
    """Return digest- and signature-verified bytes to the resumable cache.

    Capture can still fail afterwards (for example a version probe).  Keeping
    the verified file lets the next attempt finish with a Range request instead
    of discarding the complete download.
    """
    partial = _download_partial_path(target, environ)
    try:
        if binary.is_file() and not partial.exists():
            partial.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            os.replace(binary, partial)
    except OSError:
        # The capture failure is the error to report; losing the cache only
        # costs a later full download.
        pass


def _download_official_release(version: str | None = None,
                               environ: dict[str, str] | None = None, *,
                               target: dict[str, Any] | None = None,
                               idle_timeout_seconds: float = DOWNLOAD_IDLE_TIMEOUT_SECONDS,
                               attempt_timeout_seconds: float = DOWNLOAD_ATTEMPT_TIMEOUT_SECONDS,
                               progress: Callable[[int, int | None], None] | None = None) -> tuple[Path, str, Path]:
    """Download and verify the exact baseline into private staging only.

    The URL layout mirrors Anthropic's published install.sh.  Release-time GPG
    verification supplies the fixed platform digest below; runtime verifies the
    downloaded size, SHA-256, and Anthropic macOS code identity, but never runs
    the published install script.  The caller owns the returned staging dir.
    """
    root_path = _ensure_root(environ)
    if target is not None and version is not None:
        raise ValueError("specify either an official release version or target metadata")
    target = (official_release_target(version, environ) if target is None
              else _validated_release_target(target))
    partial = _download_partial_path(target, environ)
    download_root = partial.parent
    download_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock_path = download_root / "download.lock"
    staging: Path | None = None
    with _download_lock(lock_path):
        try:
            _download(target["url"], partial, expected_size=target["size"],
                      idle_timeout_seconds=idle_timeout_seconds,
                      attempt_timeout_seconds=attempt_timeout_seconds,
                      progress=progress, _already_locked=True)
            if _sha256(partial) != target["sha256"]:
                partial.unlink(missing_ok=True)
                raise RuntimeError("official Claude binary SHA-256 did not match the audited signed-manifest baseline")
            partial.chmod(0o700)
            try:
                _native_check(partial)
            except Exception:
                partial.unlink(missing_ok=True)
                raise
            staging = Path(tempfile.mkdtemp(prefix="official-baseline-", dir=root_path))
            binary = staging / "claude"
            os.replace(partial, binary)
            return binary, target["url"], staging
        except Exception:
            if staging is not None:
                shutil.rmtree(staging, ignore_errors=True)
            raise


def _download_official_baseline(environ: dict[str, str] | None = None, *,
                                idle_timeout_seconds: float = DOWNLOAD_IDLE_TIMEOUT_SECONDS,
                                attempt_timeout_seconds: float = DOWNLOAD_ATTEMPT_TIMEOUT_SECONDS,
                                progress: Callable[[int, int | None], None] | None = None) -> tuple[Path, str, Path]:
    """Backward-compatible first-install entry for the fixed baseline."""
    return _download_official_release(BASELINE_VERSION, environ,
                                      idle_timeout_seconds=idle_timeout_seconds,
                                      attempt_timeout_seconds=attempt_timeout_seconds,
                                      progress=progress)


def _retained_official(target: dict[str, Any], environ: dict[str, str] | None) -> dict[str, Any] | None:
    versions_root = store_root(environ) / "versions"
    if not versions_root.is_dir() or versions_root.is_symlink():
        return None
    for entry in versions_root.iterdir():
        if not entry.is_dir() or entry.is_symlink():
            continue
        try:
            metadata = identity_metadata(entry.name, environ)
        except (OSError, RuntimeError, ValueError):
            continue
        if metadata["version"] == target["version"] and metadata["sha256"] == target["sha256"]:
            # Only the single matching candidate pays the full hash/native
            # verification cost; unrelated retained 217MB objects are filtered
            # by their immutable identity metadata.
            return identity(entry.name, environ)
    return None


def _capture_current_official(target: dict[str, Any], environ: dict[str, str] | None) -> dict[str, Any] | None:
    """Reuse an ambient native only when its bytes exactly match the audit."""
    try:
        from executable_locator import discover_external_claude
        candidate = discover_external_claude(environ).get("path")
        if not candidate:
            return None
        source = Path(candidate).resolve(strict=True)
        _native_check(source)
        if (source.stat().st_size != target["size"] or _sha256(source) != target["sha256"]
                or _version(source, environ) != target["version"]):
            return None
        return _capture(str(source), environ, source_label=str(source))
    except (OSError, RuntimeError, ValueError):
        return None


def acquire_official_release(version: str | None = None,
                             environ: dict[str, str] | None = None, *,
                             idle_timeout_seconds: float = DOWNLOAD_IDLE_TIMEOUT_SECONDS,
                             attempt_timeout_seconds: float = DOWNLOAD_ATTEMPT_TIMEOUT_SECONDS,
                             progress: Callable[[int, int | None], None] | None = None) -> dict[str, Any]:
    """Retain exact audited release bytes, preferring local verified copies."""
    target = official_release_target(version, environ)
    descriptor = _retained_official(target, environ)
    if descriptor is not None:
        return {**descriptor, "acquisition": "retained", "release": target}
    descriptor = _capture_current_official(target, environ)
    if descriptor is not None:
        return {**descriptor, "acquisition": "current_native", "release": target}
    staging: Path | None = None
    try:
        binary, source_url, staging = _download_official_release(
            target["version"], environ,
            idle_timeout_seconds=idle_timeout_seconds,
            attempt_timeout_seconds=attempt_timeout_seconds, progress=progress)
        try:
            descriptor = _capture(str(binary), environ, source_label=source_url)
        except Exception:
            _restore_verified_download(binary, target, environ)
            raise
        if descriptor["sha256"] != target["sha256"] or descriptor["version"] != target["version"]:
            raise RuntimeError("captured official CLI did not match the audited target")
        return {**descriptor, "acquisition": "downloaded", "release": target}
    finally:
        if staging is not None:
            shutil.rmtree(staging, ignore_errors=True)


def acquire_official_target(target: dict[str, Any],
                            environ: dict[str, str] | None = None, *,
                            idle_timeout_seconds: float = DOWNLOAD_IDLE_TIMEOUT_SECONDS,
                            attempt_timeout_seconds: float = DOWNLOAD_ATTEMPT_TIMEOUT_SECONDS,
                            progress: Callable[[int, int | None], None] | None = None) -> dict[str, Any]:
    """Retain bytes for signed latest-channel metadata without activating them."""
    target = _validated_release_target(target)
    descriptor = _retained_official(target, environ)
    if descriptor is not None:
        return {**descriptor, "acquisition": "retained", "release": target}
    descriptor = _capture_current_official(target, environ)
    if descriptor is not None:
        return {**descriptor, "acquisition": "current_native", "release": target}
    staging: Path | None = None
    try:
        binary, source_url, staging = _download_official_release(
            environ=environ, target=target,
            idle_timeout_seconds=idle_timeout_seconds,
            attempt_timeout_seconds=attempt_timeout_seconds, progress=progress)
        try:
            descriptor = _capture(str(binary), environ, source_label=source_url)
        except Exception:
            _restore_verified_download(binary, target, environ)
            raise
        if descriptor["sha256"] != target["sha256"] or descriptor["version"] != target["version"]:
            raise RuntimeError("captured official CLI did not match the signed latest target")
        return {**descriptor, "acquisition": "downloaded", "release": target}
    finally:
        if staging is not None:
            shutil.rmtree(staging, ignore_errors=True)


def prepare(path: str | None = None, environ: dict[str, str] | None = None, *,
            progress: Callable[[int, int | None], None] | None = None,
            idle_timeout_seconds: float = DOWNLOAD_IDLE_TIMEOUT_SECONDS,
            attempt_timeout_seconds: float = DOWNLOAD_ATTEMPT_TIMEOUT_SECONDS) -> dict[str, Any]:
    """Ensure a healthy managed baseline while preserving external observations.

    When no local native candidate is available, the exact released baseline is
    fetched from Anthropic's signed manifest into this private store.  The
    published global install script is never executed.
    """
    contract_id = _contract_id()
    selected = select(["core", "read_only"], contract_id, environ)
    if selected is not None:
        return {"status": "ready", "selection": selected, "identity": selected}
    explicit_path = path is not None
    candidate_path = path
    candidate: dict[str, Any] | None = None
    candidate_action: str | None = None
    if candidate_path is None:
        # Local import keeps locator -> store use cycle-free at module import.
        from executable_locator import discover_external_claude
        discovered = discover_external_claude(environ)
        candidate_path = discovered.get("path")
    downloaded_staging: Path | None = None
    if candidate_path:
        try:
            descriptor = capture(candidate_path, environ)
        except (RuntimeError, ValueError, OSError) as error:
            if explicit_path:
                return {"status": "bootstrap_error", "action": f"Explicit Claude candidate could not be captured as a signed native binary: {error}"}
            candidate_action = f"Observed system Claude candidate was not a capturable signed native binary: {error}"
        else:
            try:
                selection = activate(descriptor["id"], expected_generation=get_selection(environ)["generation"], environ=environ)
            except RuntimeError as error:
                candidate, candidate_action = descriptor, str(error)
            else:
                return {"status": "ready", "selection": selection, "identity": descriptor}
    # A newly updated native CLI is useful evidence but does not become active
    # on syntax alone.  In both that case and automatic npm/shim discovery,
    # install the audited baseline privately so a first-time user is not left
    # without a managed execution version.
    try:
        downloaded, source_url, downloaded_staging = _download_official_baseline(
            environ, progress=progress, idle_timeout_seconds=idle_timeout_seconds,
            attempt_timeout_seconds=attempt_timeout_seconds)
        baseline = _capture(str(downloaded), environ, source_label=source_url)
        selection = activate(baseline["id"], expected_generation=get_selection(environ)["generation"], environ=environ)
    except (RuntimeError, ValueError, OSError) as error:
        result = {"status": "bootstrap_error", "action": str(error)}
        if candidate is not None:
            result["candidate"] = candidate
            result["candidate_action"] = candidate_action
        elif candidate_action:
            result["candidate_action"] = candidate_action
        return result
    finally:
        if downloaded_staging is not None:
            shutil.rmtree(downloaded_staging, ignore_errors=True)
    result = {"status": "ready", "selection": selection, "identity": baseline}
    if candidate is not None:
        result["candidate"] = candidate
        result["candidate_action"] = candidate_action
    elif candidate_action:
        result["candidate_action"] = candidate_action
    return result
