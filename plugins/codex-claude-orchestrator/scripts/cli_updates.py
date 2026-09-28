"""Nonblocking maintenance of bundled or signed official Claude CLI releases."""
from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
import time
from typing import Any, Callable

import cli_store
import official_releases


SCHEMA_VERSION = 1
RETRY_INTERVAL_SECONDS = 3600
DISCOVERY_INTERVAL_SECONDS = 6 * 60 * 60
DOWNLOAD_IDLE_TIMEOUT_SECONDS = 120
DOWNLOAD_ATTEMPT_TIMEOUT_SECONDS = 60 * 60
_VALID_STATES = {"idle", "available", "acquiring", "switched", "kept_newer",
                 "superseded", "failed", "interrupted", "manual", "external", "up_to_date"}


def _latest_cache_path(environ: dict[str, str] | None) -> Path:
    return _updates_root(environ) / "official-latest.json"


def _read_latest_cache(environ: dict[str, str] | None) -> dict[str, Any] | None:
    try:
        value = json.loads(_latest_cache_path(environ).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise RuntimeError("official latest discovery cache is unreadable") from exc
    if not isinstance(value, dict) or value.get("schema_version") != SCHEMA_VERSION:
        raise RuntimeError("official latest discovery cache is malformed")
    target = cli_store._validated_release_target(value.get("target"))
    checked_at = value.get("checked_at")
    if not isinstance(checked_at, (int, float)):
        raise RuntimeError("official latest discovery cache has no valid timestamp")
    if (isinstance(checked_at, bool) or not math.isfinite(checked_at)
            or checked_at < 0 or checked_at > time.time()):
        # Clock rollback or invalid local metadata must never pin discovery
        # forever. Keep the signed target but treat its freshness as unknown.
        checked_at = 0
    return {"schema_version": SCHEMA_VERSION, "target": target, "checked_at": checked_at}


def _write_latest_cache(target: dict[str, Any], environ: dict[str, str] | None) -> dict[str, Any]:
    value = {"schema_version": SCHEMA_VERSION,
             "target": cli_store._validated_release_target(target), "checked_at": time.time()}
    _atomic_json(_latest_cache_path(environ), value)
    return value


def _local_target(policy: dict[str, Any], environ: dict[str, str] | None) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    if policy.get("channel") == "latest":
        cached = _read_latest_cache(environ)
        return (cached["target"], cached) if cached else (None, None)
    return cli_store.official_release_target(environ=environ), None


def _discovery_due(policy: dict[str, Any], cache: dict[str, Any] | None, force: bool = False) -> bool:
    return bool(policy.get("channel") == "latest"
                and (force or cache is None
                     or time.time() >= float(cache["checked_at"]) + DISCOVERY_INTERVAL_SECONDS))


def _environment(environ: dict[str, str] | None) -> dict[str, str]:
    return dict(os.environ if environ is None else environ)


def _updates_root(environ: dict[str, str] | None) -> Path:
    return cli_store.store_root(environ) / "updates"


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary_path.chmod(0o600)
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def _read_state(environ: dict[str, str] | None) -> dict[str, Any] | None:
    path = _updates_root(environ) / "state.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise RuntimeError("CLI update state is unreadable") from exc
    if (not isinstance(value, dict) or value.get("schema_version") != SCHEMA_VERSION
            or value.get("state") not in _VALID_STATES):
        raise RuntimeError("CLI update state is malformed")
    return value


def _with_state(environ: dict[str, str] | None,
                mutate: Callable[[dict[str, Any] | None], dict[str, Any]]) -> dict[str, Any]:
    root = _updates_root(environ)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock = root / "state.lock"
    fd = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        changed = mutate(_read_state(environ))
        _atomic_json(root / "state.json", changed)
        return changed
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _worker_lock_held(environ: dict[str, str] | None) -> bool:
    path = _updates_root(environ) / "worker.lock"
    if not path.exists():
        return False
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


def _effective_policy(environ: dict[str, str] | None) -> dict[str, Any]:
    stored = cli_store.get_update_policy(environ)
    override = _environment(environ).get("CLAUDE_ORCHESTRATOR_UPDATE_POLICY", "").strip()
    if override:
        if override not in {"automatic", "manual"}:
            raise ValueError("CLAUDE_ORCHESTRATOR_UPDATE_POLICY must be automatic or manual")
        # The test/host override can pause automatic work, but it must never
        # defeat an explicit persistent manual rollback hold.
        if override == "automatic" and stored["mode"] == "manual":
            return {**stored, "source": "persistent_manual_hold", "persisted_mode": stored["mode"]}
        return {**stored, "mode": override, "source": "environment_override",
                "persisted_mode": stored["mode"]}
    return {**stored, "source": "persistent"}


def _current(environ: dict[str, str] | None) -> tuple[dict[str, Any], dict[str, Any] | None, bool, str | None]:
    selection = cli_store.get_selection(environ)
    if selection["active"] is None:
        return selection, None, False, None
    try:
        descriptor = cli_store.identity_metadata(selection["active"], environ)
        receipt = cli_store.recorded_qualification(selection["active"], cli_store._contract_id(), environ)
        qualified = cli_store._passes(receipt, ["core", "read_only"])
    except (OSError, RuntimeError, ValueError) as exc:
        return selection, None, False, str(exc)
    return selection, descriptor, qualified, None


def _orphan_notice(state: dict[str, Any], current: dict[str, Any] | None,
                   current_qualified: bool, target: dict[str, Any]) -> tuple[str, bool]:
    recovered = bool(current is not None and current_qualified
                     and current.get("version") == target["version"]
                     and current.get("sha256") == target["sha256"])
    prefix = "cli-update-recovered-" if recovered else "cli-update-interrupted-"
    return prefix + str(state.get("run_id") or "unknown"), recovered


def _base_status(environ: dict[str, str] | None) -> dict[str, Any]:
    policy = _effective_policy(environ)
    selection, current, current_qualified, current_error = _current(environ)
    target, discovery = _local_target(policy, environ)
    state = _read_state(environ)
    current_version = current.get("version") if current else None
    total_bytes = target["size"] if target is not None else None
    channel = {
        "source": target["source"] if target is not None else "official",
        "scope": target["scope"] if target is not None else "official_latest",
        "mode": policy.get("channel", "bundled"),
        "discovery_state": ("current" if discovery is not None and not _discovery_due(policy, discovery)
                            else "stale" if discovery is not None else "not_checked"),
    }
    if discovery is not None:
        channel.update(last_checked_at=discovery["checked_at"],
                       next_check_at=discovery["checked_at"] + DISCOVERY_INTERVAL_SECONDS)
    result: dict[str, Any] = {
        "policy": policy["mode"], "policy_source": policy["source"],
        "policy_generation": policy["generation"], "state": "idle",
        "channel": channel, "auto_qualify": bool(policy.get("auto_qualify")),
        "current_version": current_version, "target_version": target["version"] if target else None,
        "message": "CLI 版本维护尚未运行。", "reason": "not_checked",
        "next_action": None, "notice_id": None, "notice_pending": False,
        "progress": {"phase": "idle", "bytes_received": 0, "total_bytes": total_bytes, "percent": 0},
        "selection_generation": selection["generation"], "selection_mode": selection["mode"],
        "current_qualified": current_qualified,
    }
    try:
        dispatch = cli_store.dispatch_metadata(["core", "read_only"], cli_store._contract_id(), environ)
    except (OSError, RuntimeError, ValueError) as exc:
        dispatch = None
        result["effective_dispatch_error"] = str(exc)
    result["effective_dispatch_identity"] = dispatch
    result["effective_dispatch_groups"] = ["core", "read_only"]
    result["effective_dispatch_version"] = dispatch.get("version") if dispatch else None
    result["effective_dispatch_reason"] = (dispatch.get("selection_reason") if dispatch
                                           else "no_eligible_managed_identity")
    result["active_unqualified_for_contract"] = bool(
        current is not None and not current_qualified
        and (dispatch is None or dispatch.get("id") != current.get("id")))
    if state is not None:
        for key in ("state", "message", "reason", "next_action", "notice_id", "progress", "error",
                    "started_at", "finished_at", "worker_pid", "acquisition", "run_id",
                    "qualification", "missing_groups"):
            if key in state:
                result[key] = state[key]
        result["notice_pending"] = bool(result.get("notice_id") and not state.get("notice_acknowledged_at"))
        if state.get("state") == "failed" and state.get("discovery_due"):
            result["channel"] = {**result["channel"],
                                 "discovery_state": "failed_stale" if discovery is not None else "failed",
                                 "last_discovery_error": state.get("error"),
                                 "last_discovery_failed_at": state.get("finished_at")}
        if state.get("state") == "acquiring" and not _worker_lock_held(environ) and target is not None:
            orphan_notice, recovered = _orphan_notice(state, current, current_qualified, target)
            if recovered:
                result.update(state="switched",
                              message=(f"后台维护进程在发布结果前停止，但恢复检查确认审计目标 CLI {target['version']} "
                                       "已经成为 active；已启动任务仍使用各自固定的 CLI 身份。"),
                              reason="target_selected_before_worker_exit",
                              next_action="请确认此恢复结果；无需重复下载。",
                              progress={"phase": "complete", "bytes_received": target["size"],
                                        "total_bytes": target["size"], "percent": 100},
                              error="后台维护进程未发布最终状态", notice_id=orphan_notice)
            else:
                result.update(state="interrupted",
                              message="CLI 后台维护进程在发布结果前停止；未发现目标版本已切换，现有 active 保持不变，已启动任务不受影响。",
                              reason="worker_interrupted", next_action="可重试受支持 CLI 的后台维护。",
                              error="后台维护进程已不在运行", notice_id=orphan_notice)
            result["notice_pending"] = not bool(
                state.get("notice_id") == orphan_notice and state.get("notice_acknowledged_at"))
    if current_error:
        result.update(error=current_error, reason="active_integrity_error",
                      next_action="请修复受管 CLI，或显式回滚到已保留版本。")
    if selection["mode"] == "external":
        result.update(state="external", message="当前使用外部 CLI 模式，受管版本自动维护已暂停。",
                      reason="external_mode", next_action="如需自动维护，请先显式切换回受管模式。",
                      notice_id=None, notice_pending=False)
    elif policy["mode"] == "manual":
        suffix = " 正在获取的版本也不会在手动策略生效期间激活。" if result["state"] == "acquiring" else ""
        result.update(state="manual", message="受支持 CLI 的自动维护已按策略暂停。" + suffix,
                      reason=policy.get("reason") or "manual_policy",
                      next_action="如需恢复，请显式将策略设为 automatic。",
                      notice_id=None, notice_pending=False)
    elif (target is not None and current is not None and current_qualified
          and cli_store.version_key(current["version"]) >= cli_store.version_key(target["version"])):
        if cli_store.version_key(current["version"]) > cli_store.version_key(target["version"]):
            message = (f"保留已通过当前契约验证的 CLI {current['version']}；它高于本插件随包审计目标 "
                       f"{target['version']}，不会自动降级。")
            reason = "current_newer_than_target"
        else:
            qualifier = "官方 latest 通道" if policy.get("channel") == "latest" else "当前插件随包"
            message = f"CLI {current['version']} 已是{qualifier}已验证的目标版本。"
            reason = "already_current"
        # A prior failure for an older selection should not remain the live
        # status after a separate explicit selection reaches the target.
        failure_still_applies = bool(
            state is not None and state.get("state") in {"failed", "interrupted"}
            and state.get("selection_generation") == selection["generation"]
            and target is not None
            and state.get("target_version") == target.get("version")
            and state.get("target_sha256") == target.get("sha256"))
        if result["state"] not in {"switched", "kept_newer"} and not failure_still_applies:
            result.update(state="up_to_date", message=message, reason=reason, next_action=None,
                          notice_id=None, notice_pending=False,
                          progress={"phase": "complete", "bytes_received": target["size"],
                                    "total_bytes": target["size"], "percent": 100})
            result.pop("error", None)
    elif target is None and (state is None or state.get("state") in {"idle", "up_to_date"}):
        result.update(state="available",
                      message="official latest 通道尚未完成签名发布元数据发现；现有 active 保持服务。",
                      reason="official_discovery_pending",
                      next_action="automatic 策略会在后台验证官方 latest 与签名 manifest。")
    elif (state is None or state.get("state") in {"idle", "up_to_date"}
          or (state.get("state") in {"switched", "kept_newer", "superseded"}
              and state.get("target_version") != target["version"])):
        result.update(state="available",
                      message=(f"已发现可维护 CLI {target['version']}；后台维护期间现有 CLI "
                               "继续服务，已启动任务仍使用各自固定的 CLI 身份。"),
                      reason="supported_update_available",
                      next_action="automatic 策略会在后台获取并以 CAS 原子切换后续任务。")
    conflict = cli_store.explicit_switch_conflict(environ)
    if conflict is not None:
        result.update(
            state="failed", reason="explicit_switch_conflict",
            message=("检测到跨插件版本并发修改 CLI 选择或维护策略；已保留较新的磁盘值，"
                     "未用崩溃事务覆盖。"),
            next_action="请核对当前 active CLI 与 automatic/manual 策略，然后确认此通知。",
            error=conflict.get("diagnostic"), notice_id=conflict["notice_id"],
            notice_pending=True)
    if (dispatch is not None and current is not None
            and dispatch.get("id") != current.get("id")):
        result["dispatch_message"] = (
            f"当前 active CLI {current.get('version')} 尚无本契约基础资格；基础只读任务的派单预览暂由"
            f" {dispatch['version']}（{dispatch['selection_reason']}）执行。")
        if result.get("reason") == "supported_update_available":
            result["message"] = result["message"] + " " + result["dispatch_message"]
    qualification = result.get("qualification")
    if (policy["mode"] == "automatic" and selection["mode"] == "managed"
            and isinstance(qualification, dict) and qualification.get("state") == "pending"
            and isinstance(qualification.get("job_id"), str)):
        try:
            import cli_validation
            job = cli_validation.status(qualification["job_id"], environ=environ)
        except (OSError, RuntimeError, ValueError):
            job = None
        if isinstance(job, dict) and job.get("status") in {"completed", "cancelled", "needs_cleanup"}:
            outcome = job.get("outcome") or "inconclusive"
            result["qualification"] = {**qualification, "state": outcome,
                                       "attempt_final": True,
                                       "job_status": job.get("status")}
            result.update(
                state="available", reason="qualification_result_pending_maintenance",
                message=(f"CLI 资格验证任务已结束（{outcome}）；后台维护将在下一轮核对回执并决定是否切换，"
                         "当前 active 继续服务。"),
                next_action="automatic 策略会核对终态；无需重复启动资格验证。",
                progress={**result.get("progress", {}), "phase": "qualification_complete"})
    return result


def status(environ: dict[str, str] | None = None) -> dict[str, Any]:
    """Return local persisted maintenance state without network or model calls."""
    return _base_status(environ)


def _failure_explanation(error: str) -> tuple[str, str]:
    lower = error.lower()
    if any(token in lower for token in ("network", "url", "dns", "connection", "timed out", "timeout", "deadline")):
        return ("下载受支持 CLI 时网络不可用或超时，未进行切换；现有 active 版本和已启动任务均不受影响。",
                "请检查网络后重试维护。")
    if any(token in lower for token in ("sha", "digest", "size", "integrity", "signature", "codesign", "signed")):
        return ("下载内容未通过审计摘要、大小或 Anthropic 签名校验，因此未激活；现有 active 版本和已启动任务均不受影响。",
                "请保留当前版本，并在插件发布信息更新后重试。")
    if any(token in lower for token in ("spawn", "state", "worker", "process")):
        return ("CLI 后台维护进程或其持久状态启动失败，未进行切换；现有 active 版本和已启动任务均不受影响。",
                "请重试；若再次失败，可重启 Codex 后查看维护状态。")
    summary = " ".join(error.split())[:200] or "未提供错误详情"
    return (f"CLI 后台维护失败，未进行切换；原因摘要：{summary}。现有 active 版本和已启动任务均不受影响。",
            "请根据原因摘要处理后重试维护。")


def _notice_fields(existing: dict[str, Any], reason: str,
                   facts: dict[str, Any]) -> dict[str, Any]:
    """Reuse an acknowledgement while the user-visible cause is unchanged."""
    fingerprint = hashlib.sha256(json.dumps(
        {"reason": reason, **facts}, ensure_ascii=False, sort_keys=True,
        separators=(",", ":")).encode("utf-8")).hexdigest()
    candidates = [existing]
    prior = existing.get("prior_notice")
    if isinstance(prior, dict):
        candidates.append(prior)
    for candidate in candidates:
        if (candidate.get("notice_fingerprint") == fingerprint
                and isinstance(candidate.get("notice_id"), str)):
            return {"notice_id": candidate["notice_id"],
                    "notice_acknowledged_at": candidate.get("notice_acknowledged_at"),
                    "notice_fingerprint": fingerprint}
    return {"notice_id": "cli-update-" + secrets.token_urlsafe(12),
            "notice_acknowledged_at": None, "notice_fingerprint": fingerprint}


def _failure_state(existing: dict[str, Any], reason: str, error: str) -> dict[str, Any]:
    now = time.time()
    message, next_action = _failure_explanation(error)
    prior_progress = existing.get("progress") if isinstance(existing.get("progress"), dict) else {}
    progress = {**prior_progress, "phase": "paused"}
    notice = _notice_fields(existing, reason, {"error": " ".join(error.split()),
                                               "target_version": existing.get("target_version"),
                                               "target_sha256": existing.get("target_sha256")})
    return {**existing, "state": "failed", "reason": reason,
            "message": message, "next_action": next_action, "error": error,
            **notice, "finished_at": now, "updated_at": now,
            "progress": progress, "resumable": bool(progress.get("bytes_received"))}


def _fully_eligible_current(selection: dict[str, Any], current: dict[str, Any] | None,
                            target: dict[str, Any], environ: dict[str, str] | None) -> bool:
    if (current is None or not selection.get("active")
            or cli_store.version_key(current["version"]) < cli_store.version_key(target["version"])):
        return False
    try:
        return cli_store.eligible(selection["active"], ["core", "read_only"],
                                  cli_store._contract_id(), environ)
    except (OSError, RuntimeError, ValueError):
        return False


def _stable_waiting_state(prior: dict[str, Any] | None, target: dict[str, Any] | None,
                          policy: dict[str, Any], selection: dict[str, Any],
                          environ: dict[str, str] | None) -> bool:
    """Avoid reacquiring unchanged candidates while waiting for real input."""
    if (prior is None or target is None
            or prior.get("target_version") != target.get("version")
            or prior.get("target_sha256") != target.get("sha256")
            or prior.get("policy_generation") != policy.get("generation")
            or prior.get("selection_generation") != selection.get("generation")):
        return False
    reason = prior.get("reason")
    if reason in {"qualification_required", "fallback_evidence_required",
                  "qualification_not_eligible"}:
        qualification = prior.get("qualification")
        if isinstance(qualification, dict):
            identity_id = qualification.get("identity_id")
            contract_id = qualification.get("contract_id")
            if isinstance(identity_id, str) and isinstance(contract_id, str):
                try:
                    receipt = cli_store.recorded_qualification(identity_id, contract_id, environ)
                except (OSError, RuntimeError, ValueError):
                    return False
                if _missing_groups(receipt) != qualification.get("missing_groups"):
                    return False
        return True
    if reason != "qualification_pending":
        return False
    qualification = prior.get("qualification")
    job_id = qualification.get("job_id") if isinstance(qualification, dict) else None
    if not isinstance(job_id, str):
        return False
    try:
        import cli_validation
        job = cli_validation.status(job_id, environ=environ)
    except (OSError, RuntimeError, ValueError):
        return False
    return job.get("status") not in {"completed", "cancelled", "needs_cleanup"}


def start(reason: str = "startup", force: bool = False,
          environ: dict[str, str] | None = None) -> dict[str, Any]:
    """Start one coalesced maintenance worker and return without waiting."""
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("CLI maintenance reason is required")
    if type(force) is not bool:
        raise ValueError("force must be boolean")
    summary = status(environ)
    if summary["policy"] != "automatic" or summary["selection_mode"] == "external":
        return summary
    initial_selection, initial_current, _, _ = _current(environ)
    initial_policy = _effective_policy(environ)
    initial_target, initial_cache = _local_target(initial_policy, environ)
    discovery_due = _discovery_due(initial_policy, initial_cache, force)
    if (initial_target is not None and not discovery_due
            and _fully_eligible_current(initial_selection, initial_current, initial_target, environ)):
        return summary
    prior = _read_state(environ)
    if (not force and not discovery_due
            and _stable_waiting_state(prior, initial_target, initial_policy,
                                      initial_selection, environ)):
        return summary
    if (not force and prior is not None and prior.get("state") == "failed"
            and time.time() < float(prior.get("finished_at", 0)) + RETRY_INTERVAL_SECONDS):
        return summary

    root = _updates_root(environ)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    worker_lock = (root / "worker.lock").open("a+")
    try:
        fcntl.flock(worker_lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        worker_lock.close()
        return status(environ)

    logs: list[Any] = []
    state: dict[str, Any] | None = None
    try:
        # Recheck both state authorities after coalescing with another server.
        policy = _effective_policy(environ)
        selection, current, _, _ = _current(environ)
        target, cache = _local_target(policy, environ)
        needs_discovery = _discovery_due(policy, cache, force)
        if (policy["mode"] != "automatic" or selection["mode"] != "managed"
                or (target is not None and not needs_discovery
                    and _fully_eligible_current(selection, current, target, environ))):
            return status(environ)
        latest_prior = _read_state(environ)
        if (not force and not needs_discovery
                and _stable_waiting_state(latest_prior, target, policy, selection, environ)):
            return status(environ)

        now = time.time()
        run_id = "maintenance-" + secrets.token_urlsafe(16)
        state = {
            "schema_version": SCHEMA_VERSION, "run_id": run_id, "state": "acquiring",
            "reason": reason.strip(),
            "message": ("正在后台验证官方 latest 与签名 manifest；现有 active 版本继续服务，已启动任务不切换。"
                        if needs_discovery else
                        f"正在后台获取已验证的 CLI {target['version']}；现有 active 版本继续服务，已启动任务不切换。"),
            "next_action": None, "notice_id": None, "notice_acknowledged_at": None,
            "channel_mode": policy.get("channel", "bundled"),
            "auto_qualify": bool(policy.get("auto_qualify")),
            "discovery_due": needs_discovery, "target": target,
            "target_version": target["version"] if target else None,
            "target_sha256": target["sha256"] if target else None,
            "selection_generation": selection["generation"], "policy_generation": policy["generation"],
            "started_at": now, "updated_at": now,
            "progress": {"phase": "discovering" if needs_discovery else "starting", "bytes_received": 0,
                         "total_bytes": target["size"] if target else None, "percent": 0},
        }
        if (isinstance(latest_prior, dict) and isinstance(latest_prior.get("notice_id"), str)
                and isinstance(latest_prior.get("notice_fingerprint"), str)):
            state["prior_notice"] = {
                "notice_id": latest_prior["notice_id"],
                "notice_fingerprint": latest_prior["notice_fingerprint"],
                "notice_acknowledged_at": latest_prior.get("notice_acknowledged_at"),
            }
        _with_state(environ, lambda _old: state)
        stdout = (root / "worker.stdout.log").open("a", encoding="utf-8")
        stderr = (root / "worker.stderr.log").open("a", encoding="utf-8")
        logs = [stdout, stderr]
        env = _environment(environ)
        env["CLAUDE_ORCHESTRATOR_CLI_ROOT"] = str(cli_store.store_root(environ))
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        proc = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "_worker", run_id,
                                 str(worker_lock.fileno())], stdin=subprocess.DEVNULL,
                                stdout=stdout, stderr=stderr, text=True, env=env,
                                start_new_session=True, pass_fds=(worker_lock.fileno(),))
        # The persistent inherited lock is the liveness authority.  Avoid a
        # post-Popen state write whose failure could misreport a live worker.
        _ = proc.pid
    except Exception as exc:
        fallback = state or {"schema_version": SCHEMA_VERSION, "state": "idle",
                             "reason": reason.strip(), "progress": {"phase": "starting",
                                                                      "bytes_received": 0,
                                                                      "total_bytes": None, "percent": 0}}
        _with_state(environ, lambda value: _failure_state(value or fallback, "worker_spawn_failed", str(exc)))
    finally:
        for handle in logs:
            handle.close()
        worker_lock.close()
    return status(environ)


def set_policy(mode: str, reason: str = "user_request",
               environ: dict[str, str] | None = None, *,
               channel: str | None = None,
               auto_qualify: bool | None = None) -> dict[str, Any]:
    """Persist automatic/manual policy; activation observes the same lock."""
    cli_store.set_update_policy(mode, reason, environ,
                                channel=channel, auto_qualify=auto_qualify)
    return status(environ)


def activate_explicit(identity_id: str, expected_generation: int | None = None,
                      environ: dict[str, str] | None = None) -> dict[str, Any]:
    """Explicitly activate and pause automatic maintenance as one store transaction."""
    return cli_store.activate_explicit(identity_id, expected_generation=expected_generation,
                                       reason="manual_activation", environ=environ)


def rollback_explicit(expected_generation: int | None = None,
                      environ: dict[str, str] | None = None) -> dict[str, Any]:
    """Explicitly roll back and pause automatic maintenance as one store transaction."""
    return cli_store.rollback_explicit(expected_generation=expected_generation,
                                       reason="manual_rollback", environ=environ)


def acknowledge(notice_id: str, environ: dict[str, str] | None = None) -> dict[str, Any]:
    """Acknowledge exactly the notice that was shown to the user."""
    if not isinstance(notice_id, str) or not notice_id:
        raise ValueError("notice_id is required")
    if notice_id.startswith("cli-switch-conflict-"):
        cli_store.acknowledge_explicit_switch_conflict(notice_id, environ)
        return status(environ)
    def mark(value: dict[str, Any] | None) -> dict[str, Any]:
        if value is None:
            raise ValueError("CLI update notice is not current")
        current_notice = value.get("notice_id")
        if value.get("state") == "acquiring" and not _worker_lock_held(environ):
            _selection, current, qualified, _error = _current(environ)
            target, _cache = _local_target(_effective_policy(environ), environ)
            if target is None and isinstance(value.get("target"), dict):
                target = value["target"]
            if target is None:
                raise ValueError("CLI update notice is not current")
            current_notice, _recovered = _orphan_notice(value, current, qualified, target)
        if current_notice != notice_id or value.get("notice_acknowledged_at"):
            raise ValueError("CLI update notice is not current")
        return {**value, "notice_id": notice_id,
                "notice_acknowledged_at": time.time(), "updated_at": time.time()}
    _with_state(environ, mark)
    return status(environ)


def _progress_writer(run_id: str, environ: dict[str, str] | None) -> Callable[[int, int | None], None]:
    last_percent = -1
    def write(received: int, total: int | None) -> None:
        nonlocal last_percent
        percent = int(received * 100 / total) if total else 0
        if percent == last_percent and received != total:
            return
        last_percent = percent
        def update(value: dict[str, Any] | None) -> dict[str, Any]:
            if value is None or value.get("run_id") != run_id or value.get("state") != "acquiring":
                raise RuntimeError("CLI maintenance state changed during download")
            return {**value, "progress": {"phase": "downloading", "bytes_received": received,
                                           "total_bytes": total, "percent": percent},
                    "updated_at": time.time()}
        _with_state(environ, update)
    return write


def _missing_groups(receipt: dict[str, Any] | None) -> list[str]:
    matrix = receipt.get("matrix", {}) if isinstance(receipt, dict) else {}
    return [group for group in ("core", "read_only", "write", "resume", "workflow")
            if matrix.get(group, {}).get("status") != "pass"]


def _attempt_path(identity_id: str, contract_id: str,
                  environ: dict[str, str] | None) -> Path:
    token = hashlib.sha256(f"{identity_id}\0{contract_id}".encode()).hexdigest()
    return _updates_root(environ) / "qualification-attempts" / f"{token}.json"


def _read_attempt(identity_id: str, contract_id: str,
                  environ: dict[str, str] | None) -> dict[str, Any] | None:
    path = _attempt_path(identity_id, contract_id, environ)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise RuntimeError("automatic qualification attempt record is unreadable") from exc
    if (not isinstance(value, dict) or value.get("identity_id") != identity_id
            or value.get("contract_id") != contract_id
            or value.get("state") not in {"reserved", "deferred", "started", "terminal"}):
        raise RuntimeError("automatic qualification attempt record is malformed")
    return value


def _qualification_action(descriptor: dict[str, Any], state: dict[str, Any],
                          environ: dict[str, str] | None) -> tuple[str, dict[str, Any]]:
    """Return eligible, pending, required, or terminal for the current contract."""
    contract_id = cli_store._contract_id()
    receipt = cli_store.qualification(descriptor["id"], contract_id, environ)
    missing = _missing_groups(receipt)
    proof = {"identity_id": descriptor["id"], "contract_id": contract_id,
             "state": "qualified" if not any(group in missing for group in ("core", "read_only")) else "missing",
             "missing_groups": missing, "attempt_final": False}
    if not any(group in missing for group in ("core", "read_only")):
        return "eligible", proof
    if receipt is not None:
        statuses = {receipt.get("matrix", {}).get(group, {}).get("status")
                    for group in ("core", "read_only")}
        outcome = "fail" if "fail" in statuses else "inconclusive"
        return "terminal", {**proof, "state": outcome, "attempt_final": True}
    if not state.get("auto_qualify"):
        return "required", proof
    attempt = _read_attempt(descriptor["id"], contract_id, environ)
    if attempt is None or attempt["state"] == "deferred":
        import cli_validation
        try:
            # Lost-job recovery may read selection state.  Complete it before
            # entering the selection/policy authorization transaction below.
            cli_validation.reconcile_startability(environ)
        except cli_validation.QualificationStartDeferred as exc:
            deferred = {"schema_version": SCHEMA_VERSION,
                        "identity_id": descriptor["id"], "contract_id": contract_id,
                        "state": "deferred", "error": str(exc), "job_id": exc.job_id,
                        "created_at": time.time(), "deferred_at": time.time()}
            _atomic_json(_attempt_path(descriptor["id"], contract_id, environ), deferred)
            return "deferred", {**proof, "state": "deferred", "attempt_final": False,
                                "job_id": exc.job_id, "error": str(exc)}

        # Recovery may have committed the exact receipt this candidate was
        # waiting for.  Consume that proof instead of paying for a duplicate.
        receipt = cli_store.qualification(descriptor["id"], contract_id, environ)
        missing = _missing_groups(receipt)
        proof = {**proof,
                 "state": ("qualified" if not any(group in missing for group in ("core", "read_only"))
                           else "missing"),
                 "missing_groups": missing}
        if not any(group in missing for group in ("core", "read_only")):
            return "eligible", proof
        if receipt is not None:
            statuses = {receipt.get("matrix", {}).get(group, {}).get("status")
                        for group in ("core", "read_only")}
            outcome = "fail" if "fail" in statuses else "inconclusive"
            return "terminal", {**proof, "state": outcome, "attempt_final": True}

        expected_selection_generation = state.get("selection_generation")
        expected_policy_generation = state.get("policy_generation")
        if not isinstance(expected_selection_generation, int):
            expected_selection_generation = cli_store.get_selection(environ)["generation"]
        if not isinstance(expected_policy_generation, int):
            expected_policy_generation = cli_store.get_update_policy(environ)["generation"]
        try:
            with cli_store.automatic_qualification_authorization(
                    expected_selection_generation, expected_policy_generation, environ):
                reserved = {"schema_version": SCHEMA_VERSION, "identity_id": descriptor["id"],
                            "contract_id": contract_id, "state": "reserved", "created_at": time.time()}
                _atomic_json(_attempt_path(descriptor["id"], contract_id, environ), reserved)
                try:
                    job = cli_validation.start(
                        descriptor["id"], model="sonnet", groups=None,
                        activate_on_success=False, environ=environ,
                        selection_generation=expected_selection_generation,
                        reconcile_unfinished=False)
                except cli_validation.QualificationStartDeferred as exc:
                    deferred = {**reserved, "state": "deferred", "error": str(exc),
                                "job_id": exc.job_id, "deferred_at": time.time()}
                    _atomic_json(_attempt_path(descriptor["id"], contract_id, environ), deferred)
                    return "deferred", {**proof, "state": "deferred", "attempt_final": False,
                                        "job_id": exc.job_id, "error": str(exc)}
                except cli_validation.QualificationLaunchAmbiguous as exc:
                    terminal = {**reserved, "state": "terminal", "outcome": "inconclusive",
                                "job_id": exc.job_id, "error": str(exc), "finished_at": time.time()}
                    _atomic_json(_attempt_path(descriptor["id"], contract_id, environ), terminal)
                    return "terminal", {**proof, "state": "inconclusive", "attempt_final": True,
                                        "job_id": exc.job_id, "error": str(exc)}
                except Exception as exc:
                    # Unknown third-party/start failures remain conservative:
                    # retrying cannot prove that a child was never launched.
                    terminal = {**reserved, "state": "terminal", "outcome": "inconclusive",
                                "error": str(exc), "finished_at": time.time()}
                    _atomic_json(_attempt_path(descriptor["id"], contract_id, environ), terminal)
                    return "terminal", {**proof, "state": "inconclusive", "attempt_final": True,
                                        "error": str(exc)}
                attempt = {**reserved, "state": "started", "job_id": job["job_id"],
                           "started_at": time.time()}
                _atomic_json(_attempt_path(descriptor["id"], contract_id, environ), attempt)
                return "pending", {**proof, "state": "pending", "job_id": job["job_id"]}
        except cli_store.AutomaticQualificationSuperseded as exc:
            return "superseded", {**proof, "state": "superseded", "attempt_final": False,
                                  "error": str(exc)}
    if attempt["state"] == "reserved":
        # A process died after reserving the one paid attempt.  Retrying could
        # duplicate a request whose start result was lost, so remain bounded.
        return "terminal", {**proof, "state": "inconclusive", "attempt_final": True,
                            "error": "automatic qualification start outcome is unknown"}
    if attempt["state"] == "terminal":
        return "terminal", {**proof, "state": attempt.get("outcome", "inconclusive"),
                            "job_id": attempt.get("job_id"), "attempt_final": True,
                            "error": attempt.get("error")}
    import cli_validation
    job = cli_validation.status(attempt["job_id"], environ=environ)
    if job.get("status") not in {"completed", "cancelled", "needs_cleanup"}:
        return "pending", {**proof, "state": "pending", "job_id": attempt["job_id"]}
    # The validator publishes the immutable receipt before terminal state.
    receipt = cli_store.qualification(descriptor["id"], contract_id, environ)
    missing = _missing_groups(receipt)
    eligible = not any(group in missing for group in ("core", "read_only"))
    outcome = "pass" if eligible else (job.get("outcome") or "inconclusive")
    terminal = {**attempt, "state": "terminal", "outcome": outcome,
                "finished_at": time.time(), "missing_groups": missing}
    _atomic_json(_attempt_path(descriptor["id"], contract_id, environ), terminal)
    result = {**proof, "state": "qualified" if eligible else outcome,
              "job_id": attempt["job_id"], "missing_groups": missing, "attempt_final": True}
    return ("eligible" if eligible else "terminal"), result


def _fallback_evidence(missing_groups: list[str], candidate_id: str,
                       contract_id: str, environ: dict[str, str] | None) -> tuple[list[dict[str, Any]], list[str]]:
    evidence: list[dict[str, Any]] = []
    unavailable: list[str] = []
    for group in missing_groups:
        selected = cli_store.select(["core", group], contract_id, environ)
        if selected is None or selected.get("id") == candidate_id:
            unavailable.append(group)
        else:
            evidence.append({"group": group, "identity_id": selected["id"],
                             "version": selected["version"],
                             "selection_reason": selected.get("selection_reason")})
    return evidence, unavailable


def _run_worker(run_id: str, environ: dict[str, str] | None = None) -> None:
    state = _read_state(environ)
    if state is None or state.get("run_id") != run_id or state.get("state") != "acquiring":
        raise RuntimeError("CLI maintenance worker is not bound to the active run")
    try:
        target = state.get("target")
        if state.get("discovery_due"):
            discovered = official_releases.discover_latest(
                cli_store._official_platform(), environ=environ)
            cached = _read_latest_cache(environ)
            if cached is not None:
                old = cached["target"]
                if cli_store.version_key(discovered["version"]) < cli_store.version_key(old["version"]):
                    # A stale or rolled-back channel response never selects an
                    # older release over already verified discovery evidence.
                    target = old
                elif (discovered["version"] == old["version"]
                      and (discovered["sha256"] != old["sha256"]
                           or discovered["size"] != old["size"])):
                    raise RuntimeError("official latest manifest changed bytes for an already discovered release")
                else:
                    target = discovered
            else:
                target = discovered
            _write_latest_cache(target, environ)
            def discovered_state(value: dict[str, Any] | None) -> dict[str, Any]:
                if value is None or value.get("run_id") != run_id:
                    raise RuntimeError("CLI maintenance state changed during official discovery")
                return {**value, "target": target, "target_version": target["version"],
                        "target_sha256": target["sha256"], "discovery_due": False,
                        "progress": {"phase": "starting", "bytes_received": 0,
                                     "total_bytes": target["size"], "percent": 0},
                        "updated_at": time.time()}
            _with_state(environ, discovered_state)
            state = _read_state(environ) or state
        if target is None:
            # Backward compatibility for a 0.4.5-created bundled worker state.
            target = cli_store.official_release_target(state.get("target_version"), environ)
        if target.get("source") == "official":
            target = cli_store._validated_release_target(target)

        current_selection, current, _, _ = _current(environ)
        if _fully_eligible_current(current_selection, current, target, environ):
            now = time.time()
            def already_current(value: dict[str, Any] | None) -> dict[str, Any]:
                if value is None or value.get("run_id") != run_id:
                    raise RuntimeError("CLI maintenance state changed after discovery")
                return {**value, "state": "up_to_date", "reason": "current_not_older_than_target",
                        "message": (f"保留已通过当前契约验证的 CLI {current['version']}；官方目标为 "
                                    f"{target['version']}，未覆盖健康 active。"),
                        "next_action": None, "notice_id": None, "finished_at": now, "updated_at": now,
                        "progress": {"phase": "complete", "bytes_received": target["size"],
                                     "total_bytes": target["size"], "percent": 100}}
            _with_state(environ, already_current)
            return

        acquire = (cli_store.acquire_official_target if target["source"] == "official"
                   else lambda selected, env, **kwargs: cli_store.acquire_official_release(
                       selected["version"], env, **kwargs))
        descriptor = acquire(
            target, environ,
            idle_timeout_seconds=DOWNLOAD_IDLE_TIMEOUT_SECONDS,
            attempt_timeout_seconds=DOWNLOAD_ATTEMPT_TIMEOUT_SECONDS,
            progress=_progress_writer(run_id, environ))

        # A manual/external/policy change during discovery or acquisition wins
        # before any paid qualification can start.
        live_policy = cli_store.get_update_policy(environ)
        live_selection = cli_store.get_selection(environ)
        if (live_policy["generation"] != state["policy_generation"]
                or live_policy["mode"] != "automatic"
                or live_selection["generation"] != state["selection_generation"]
                or live_selection["mode"] != "managed"):
            now = time.time()
            notice_id = "cli-update-" + secrets.token_urlsafe(12)
            def changed_before_qualification(value: dict[str, Any] | None) -> dict[str, Any]:
                if value is None or value.get("run_id") != run_id:
                    raise RuntimeError("CLI maintenance state changed before supersession was recorded")
                return {**value, "state": "superseded", "reason": "explicit_change_won",
                        "message": "维护期间发生了显式策略或选择变更；已保留下载内容，但未启动资格验证或切换 active。",
                        "next_action": "如仍需自动晋升，请重新启用 automatic 策略。",
                        "notice_id": notice_id, "notice_acknowledged_at": None,
                        "acquisition": descriptor.get("acquisition"), "finished_at": now,
                        "updated_at": now,
                        "progress": {"phase": "retained", "bytes_received": target["size"],
                                     "total_bytes": target["size"], "percent": 100}}
            _with_state(environ, changed_before_qualification)
            return

        qualification_action, qualification = _qualification_action(descriptor, state, environ)
        if qualification_action == "eligible" and qualification.get("missing_groups"):
            fallback, unavailable = _fallback_evidence(
                qualification["missing_groups"], descriptor["id"],
                qualification["contract_id"], environ)
            qualification["fallback_evidence"] = fallback
            if unavailable:
                qualification["fallback_unavailable"] = unavailable
                qualification_action = "required"
        if qualification_action in {"required", "pending", "deferred", "terminal", "superseded"}:
            now = time.time()
            terminal = qualification_action == "terminal"
            def await_qualification(value: dict[str, Any] | None) -> dict[str, Any]:
                if value is None or value.get("run_id") != run_id:
                    raise RuntimeError("CLI maintenance state changed before qualification was recorded")
                if qualification_action == "required":
                    if qualification.get("fallback_unavailable"):
                        message = (f"官方 CLI {descriptor['version']} 已通过 core/read_only，但缺失能力没有可用的"
                                   "保留版本回退证据；现有 active 保持不变。")
                        reason = "fallback_evidence_required"
                    else:
                        message = (f"官方 CLI {descriptor['version']} 已通过来源、摘要和签名校验并保留，"
                                   "但尚无当前契约资格证明；现有 active 保持不变。")
                        reason = "qualification_required"
                elif qualification_action == "pending":
                    message = (f"官方 CLI {descriptor['version']} 已保留，当前契约的单次自动资格验证正在运行；"
                               "通过前现有 active 保持不变。")
                    reason = "qualification_pending"
                elif qualification_action == "deferred":
                    message = (f"官方 CLI {descriptor['version']} 已保留；资格验证尚未启动，"
                               "因为另一验证或清理仍占用启动条件。现有 active 保持不变。")
                    reason = "qualification_deferred"
                elif qualification_action == "superseded":
                    message = ("维护期间发生了显式策略或选择变更；已保留下载内容，"
                               "但未启动资格验证或切换 active。")
                    reason = "explicit_change_won"
                else:
                    message = (f"官方 CLI {descriptor['version']} 的单次自动资格验证未证明 core/read_only；"
                               "不会自动重试，现有 active 保持不变。")
                    reason = "qualification_not_eligible"
                notice = (_notice_fields(value, reason, {
                    "identity_id": qualification.get("identity_id"),
                    "contract_id": qualification.get("contract_id"),
                    "qualification_state": qualification.get("state"),
                    "missing_groups": qualification.get("missing_groups"),
                    "error": qualification.get("error"),
                }) if terminal else {"notice_id": None, "notice_acknowledged_at": None})
                return {**value,
                        "state": ("failed" if terminal else
                                  "superseded" if qualification_action == "superseded" else "available"),
                        "reason": reason, "message": message,
                        "next_action": ("需要显式人工验证或新契约后再尝试。" if terminal else
                                        "automatic 策略会在启动条件可用后重试。"
                                        if qualification_action == "deferred" else
                                        "如仍需自动晋升，请重新启用 automatic 策略。"
                                        if qualification_action == "superseded" else None),
                        **notice,
                        "qualification": qualification,
                        "missing_groups": qualification.get("missing_groups", []),
                        "acquisition": descriptor.get("acquisition"), "finished_at": now,
                        "updated_at": now,
                        "progress": {"phase": "qualification", "bytes_received": target["size"],
                                     "total_bytes": target["size"], "percent": 100}}
            _with_state(environ, await_qualification)
            return
        try:
            selection = cli_store.activate_automatic(
                descriptor["id"], state["selection_generation"], state["policy_generation"],
                state["target_version"], environ)
        except cli_store.AutomaticActivationSuperseded as exc:
            now = time.time()
            notice_id = "cli-update-" + secrets.token_urlsafe(12)
            def superseded(value: dict[str, Any] | None) -> dict[str, Any]:
                if value is None or value.get("run_id") != run_id:
                    raise RuntimeError("CLI maintenance state changed before supersession was recorded")
                return {**value, "state": "superseded", "reason": "explicit_change_won",
                        "message": f"维护期间发生了显式策略或选择变更，现有 active 保持不变：{exc}",
                        "next_action": "审计版本已保留；如仍需切换，请显式重新启用 automatic 策略。",
                        "notice_id": notice_id, "notice_acknowledged_at": None,
                        "acquisition": descriptor.get("acquisition"), "finished_at": now, "updated_at": now,
                        "progress": {"phase": "retained", "bytes_received": descriptor["release"]["size"],
                                     "total_bytes": descriptor["release"]["size"], "percent": 100}}
            _with_state(environ, superseded)
            return
        active = cli_store.identity(selection["active"], environ) if selection.get("active") else None
        if active is not None and active["id"] == descriptor["id"]:
            final_state = "switched"
            message = (f"已自动将后续任务切换到 CLI {descriptor['version']}；其官方签名发布元数据、原生签名"
                       "和当前契约 core/read_only 资格均已验证。已启动任务继续使用各自固定的 CLI 身份。")
            final_reason = "supported_target_activated"
        elif active is not None and cli_store.version_key(active["version"]) >= cli_store.version_key(descriptor["version"]):
            final_state = "kept_newer"
            message = (f"保留已通过当前契约验证的 CLI {active['version']}，因为它不低于本插件随包审计目标 "
                       f"{descriptor['version']}；已启动任务不受影响。")
            final_reason = "current_selection_not_older"
        else:
            final_state = "superseded"
            message = "维护期间的显式策略或选择变更优先；已获取版本仅保留，不会激活，已启动任务不受影响。"
            final_reason = "selection_preserved"
        now = time.time()
        notice_id = "cli-update-" + secrets.token_urlsafe(12)
        def finish(value: dict[str, Any] | None) -> dict[str, Any]:
            if value is None or value.get("run_id") != run_id:
                raise RuntimeError("CLI maintenance state changed before completion")
            return {**value, "state": final_state, "reason": final_reason, "message": message,
                    "next_action": None, "notice_id": notice_id, "notice_acknowledged_at": None,
                    "acquisition": descriptor.get("acquisition"), "qualification": qualification,
                    "missing_groups": qualification.get("missing_groups", []),
                    "finished_at": now, "updated_at": now,
                    "progress": {"phase": "complete", "bytes_received": descriptor["release"]["size"],
                                 "total_bytes": descriptor["release"]["size"], "percent": 100}}
        _with_state(environ, finish)
    except Exception as exc:
        def fail(value: dict[str, Any] | None) -> dict[str, Any]:
            if value is None or value.get("run_id") != run_id:
                raise RuntimeError("CLI maintenance state changed while reporting failure") from exc
            return _failure_state(value, "maintenance_failed", str(exc))
        _with_state(environ, fail)


def close() -> None:
    """Workers are independent processes; MCP shutdown has nothing to join."""


def _main(argv: list[str]) -> int:
    if len(argv) != 3 or argv[0] != "_worker":
        return 2
    lock_fd = int(argv[2])
    try:
        os.fstat(lock_fd)
        _run_worker(argv[1], os.environ)
        return 0
    finally:
        os.close(lock_fd)


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
