"""Read-only status of the user's local Claude CLI.

Plugin-managed CLI maintenance is retired: this module never downloads,
installs, updates, rolls back, copies, qualifies, or switches a Claude CLI, and
it runs no subprocess or network request.  Earlier maintenance state on disk is
reported as history and never influences dispatch.
"""
from __future__ import annotations

import fcntl
import os
from typing import Any

import cli_store
from executable_locator import discover_external_claude


RETIREMENT_MESSAGE = ("插件不再下载、安装、更新、回退、复制或切换 Claude CLI，也不做版本资格验证；"
                      "新任务只使用本机已安装或明确配置的 Claude CLI，版本号仅作诊断。")


_WORKER_RUNNING = {"held": True, "free": False, "absent": False}


def _legacy_worker_observation(environ: dict[str, str] | None) -> str:
    """Observe an older plugin's worker lock as absent, free, held or unreadable.

    The probe opens an existing lock read-only and never creates it.  It takes
    a shared lock so that concurrent status readers do not see each other as
    the old exclusive worker.
    """
    path = cli_store.store_root(environ) / "updates" / "worker.lock"
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        return "absent"
    except OSError:
        return "unreadable"
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            return "held"
        except OSError:
            return "unreadable"
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        return "free"
    finally:
        os.close(descriptor)


def status(environ: dict[str, str] | None = None) -> dict[str, Any]:
    """Return local CLI discovery plus retired-maintenance history."""
    discovery = discover_external_claude(environ)
    path = discovery.get("path")
    legacy = cli_store.legacy_records(environ)
    observation = _legacy_worker_observation(environ)
    legacy["maintenance_worker_lock"] = observation
    # None means the lock exists but could not be observed, not "stopped".
    legacy["maintenance_worker_running"] = _WORKER_RUNNING.get(observation)
    if path:
        state, reason = "local_cli", "user_local_cli"
        message = f"使用本机 Claude CLI（来源 {discovery.get('source')}）：{path}。" + RETIREMENT_MESSAGE
        next_action = None
    else:
        state, reason = "cli_missing", "local_cli_not_found"
        message = "未找到本机 Claude CLI。" + RETIREMENT_MESSAGE
        next_action = discovery.get("action")
    if legacy.get("present"):
        message += " 旧版本留下的受管 CLI 记录只作历史保留，不参与派单，也未被删除。"
    return {
        "policy": "user_local_cli", "version_management": "retired", "state": state, "reason": reason,
        "message": message, "next_action": next_action, "notice_id": None, "notice_pending": False,
        "local_cli": {"path": path, "source": discovery.get("source"), "candidate": discovery.get("candidate"),
                      "configured": discovery.get("configured")},
        "current_version": None,
        "version_note": "本状态不启动 CLI；版本在环境检查和各轮执行记录中读取，只作诊断。",
        "legacy_managed_state": legacy,
    }


def retired_action(action: str, environ: dict[str, str] | None = None) -> dict[str, Any]:
    """Explain a retired version-management request without changing anything."""
    return {"operation_type": "cli_version_management_retired", "action": action, "retired": True,
            "changed": False, "claude_started": False, "message": RETIREMENT_MESSAGE,
            "next_action": "如需升级 Claude CLI，请用你自己的官方安装方式升级，然后用 claude_environment 重新检查。",
            "cli_maintenance": status(environ)}
