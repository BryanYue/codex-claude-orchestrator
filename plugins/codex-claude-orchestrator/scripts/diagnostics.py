"""Local readiness report. Never include credentials, task text or raw CLI output."""
from __future__ import annotations

import json
import os
import platform
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/codex-claude-orchestrator/scripts"))
import bridge
import cli_store
import cli_validation
from executable_locator import discover_system_claude, locate_claude

try:
    import cli_updates
except ModuleNotFoundError:  # During an interrupted plugin upgrade, diagnostics remains read-only and usable.
    cli_updates = None


def _host_candidates() -> list[tuple[str, str]]:
    """Bundled host CLIs take precedence; PATH remains diagnostic-only fallback."""
    candidates = [(Path(app) / relative, 'desktop')
                  for app in ('/Applications/ChatGPT.app', '/Applications/Codex.app')
                  for relative in ('Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex',
                                   'Contents/Resources/codex')]
    result: list[tuple[str, str]] = []
    seen: set[Path] = set()
    for path, source in candidates:
        if path.is_file() and os.access(path, os.X_OK) and path.resolve() not in seen:
            seen.add(path.resolve()); result.append((str(path), source))
    path_cli = shutil.which('codex')
    if path_cli:
        path = Path(path_cli).resolve()
        if path not in seen:
            result.append((str(path), 'path'))
    return result


def codex_cli() -> str | None:
    """Find a plugin-capable CLI, preferring either verified desktop bundle."""
    fallback = None
    for candidate, _ in _host_candidates():
        fallback = fallback or candidate
        try:
            probe = subprocess.run([candidate, 'plugin', '--help'], capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            continue
        if probe.returncode == 0:
            return candidate
    return fallback


def host_source(cli: str | None) -> str | None:
    if not cli:
        return None
    resolved = Path(cli).resolve()
    return next((source for candidate, source in _host_candidates() if Path(candidate).resolve() == resolved), 'path')


def _recent_real_provider_call(runs: list[dict] | None) -> dict:
    """Require a response witness; CLI initialization alone is not a model call."""
    initialization_seen = False
    for run in runs or []:
        if not isinstance(run, dict):
            continue
        initialization_seen |= isinstance(run.get("session_id"), str)
        if run.get("provider_response_observed") is not True and run.get("execution_evidence") != "provider_event":
            continue
        environment = run.get("environment") if isinstance(run.get("environment"), dict) else {}
        source = run.get("actual_model_source")
        models = [name for name in run.get("actual_models", []) if isinstance(name, str) and name] if isinstance(run.get("actual_models"), list) else []
        if not models and source and isinstance(run.get("actual_model"), str):
            models = [run["actual_model"]]
        return {
            "status": "recorded",
            "at": run.get("updated_at") or run.get("started_at"),
            "cli_version": environment.get("cli_version"),
            "actual_model": models[0] if len(models) == 1 else None,
            "actual_models": models,
            "actual_model_source": source,
            "run_status": run.get("status"),
            "omitted_fields": ["run_id", "session_id"],
            "detail_note": "Raw run and session identifiers are available only in the corresponding run details.",
        }
    if initialization_seen:
        return {"status": "unconfirmed", "note": "A CLI session is recorded, but these records do not establish a provider response. Initialization or missing legacy evidence is not a confirmed model call."}
    return {"status": "not_recorded", "note": "No persisted supervised provider response is available."}


def _store_inventory() -> dict:
    try:
        value = cli_store.inventory()
        if isinstance(value, dict):
            versions = value.get("versions", [])
            sizes = [item["size"] for item in versions if isinstance(item, dict)
                     and type(item.get("size")) is int and item["size"] >= 0]
            value["storage_summary"] = {"verified_retained_bytes": sum(sizes),
                                        "unmeasured_versions": len(versions) - len(sizes),
                                        "excludes": ["download_partials", "run_evidence"],
                                        "automatic_cleanup": False}
        return value if isinstance(value, dict) else {"status": "unavailable", "reason": "CLI inventory had an invalid shape"}
    except (OSError, RuntimeError, ValueError) as exc:
        return {"status": "unavailable", "reason": str(exc)}


def _validation_status(job_id: str | None) -> dict | None:
    if job_id is None:
        return None
    try:
        value = cli_validation.status(job_id)
        return value if isinstance(value, dict) else {"status": "unavailable", "reason": "validation status had an invalid shape"}
    except (OSError, RuntimeError, ValueError) as exc:
        return {"status": "unavailable", "job_id": job_id, "reason": str(exc)}


def _maintenance_status() -> dict:
    """Read the updater's persisted public state; never start or acknowledge an update."""
    if cli_updates is None:
        return {"state": "unavailable", "reason": "CLI maintenance module is unavailable"}
    try:
        value = cli_updates.status()
        return value if isinstance(value, dict) else {"state": "unavailable", "reason": "CLI maintenance status had an invalid shape"}
    except (OSError, RuntimeError, ValueError) as exc:
        return {"state": "unavailable", "reason": str(exc)}


def collect(cwd: str, *, recent_runs: list[dict] | None = None, job_id: str | None = None) -> dict:
    folder = Path(cwd)
    if not folder.is_absolute() or not folder.is_dir():
        raise ValueError('cwd must be an existing absolute directory')
    manifest = json.loads((ROOT / '.codex-plugin/plugin.json').read_text())
    cli = codex_cli()
    source = host_source(cli)
    host = {'installed': cli is not None, 'version': None, 'plugins_supported': False,
            'source': source, 'desktop_compatibility': 'unverified'}
    if cli:
        try:
            version = subprocess.run([cli, '--version'], capture_output=True, text=True, timeout=10)
            probe = subprocess.run([cli, 'plugin', '--help'], capture_output=True, text=True, timeout=10)
            host.update(version=version.stdout.strip()[:100] if version.returncode == 0 else None,
                        plugins_supported=probe.returncode == 0)
            if host['plugins_supported'] and source == 'desktop':
                host['desktop_compatibility'] = 'verified'
        except (OSError, subprocess.TimeoutExpired):
            host['error'] = 'host_check_unavailable'
    environment = bridge.check_environment(folder, verify=False)
    discovery = locate_claude()
    system_discovery = discover_system_claude()
    inventory = _store_inventory()
    validation = _validation_status(job_id)
    maintenance = _maintenance_status()
    # Intentionally exclude the account identifier and full environment payload.
    claude = {key: environment.get(key) for key in ('ready', 'status') if key in environment}
    claude['version'] = (environment.get('cli') or {}).get('version')
    claude['auth_status'] = (environment.get('auth') or {}).get('status')
    claude['credential_validity'] = (environment.get('auth') or {}).get('credential_validity')
    claude['compatibility'] = (environment.get('cli') or {}).get('profile')
    claude['discovery'] = {
        key: discovery.get(key) for key in ('path', 'source', 'configured', 'candidate', 'settings_path')
    }
    installation = environment.get("installation") if isinstance(environment.get("installation"), dict) else {
        "status": "installed" if (environment.get("cli") or {}).get("installed") else "missing",
        "path": (environment.get("cli") or {}).get("path"),
    }
    auth = environment.get("auth") if isinstance(environment.get("auth"), dict) else {"status": "not_checked"}
    probe = environment.get("probe") if isinstance(environment.get("probe"), dict) else {"status": "not_requested"}
    compatibility = environment.get("compatibility") if isinstance(environment.get("compatibility"), dict) else {
        "status": (environment.get("cli") or {}).get("profile", {}).get("status", "unknown"),
        "capabilities": (environment.get("cli") or {}).get("capabilities", {}),
        "unavailable_capabilities": (environment.get("cli") or {}).get("unavailable_capabilities", []),
    }
    active = {
        "path": (environment.get("cli") or {}).get("path"),
        "source": (environment.get("cli") or {}).get("source"),
        "version": (environment.get("cli") or {}).get("version"),
        "identity": (environment.get("cli") or {}).get("identity"),
        "dispatch_ready": bool(environment.get("ready")),
        "status": environment.get("status"),
        "compatibility": compatibility,
    }
    cli_management = {
        "schema_version": 1,
        "system": system_discovery,
        "active": active,
        "inventory": inventory,
        "validation": validation,
        "maintenance": maintenance,
        "dimensions": {
            "installation": installation,
            "login": {key: auth.get(key) for key in ("status", "credential_validity", "cleanup_status") if key in auth},
            "probe": {key: probe.get(key) for key in ("status", "checked_at", "requested_model", "cleanup_status") if key in probe},
            "recent_real_provider_call": _recent_real_provider_call(recent_runs),
            "compatibility": compatibility,
        },
        "note": "Candidate or validation state is observational and never changes active dispatch readiness by itself.",
    }
    uv_ready = shutil.which('uv') is not None
    steps = []
    if host['desktop_compatibility'] != 'verified':
        steps.append('未确认 Codex 桌面宿主的内置插件 CLI；安装或更新 Codex.app 后重新运行检查。PATH 中的 codex 不作为桌面宿主能力证据。')
    if not uv_ready:
        steps.append('安装 uv 后重新运行安装检查：https://docs.astral.sh/uv/getting-started/installation/')
    if not environment.get('ready'):
        steps.append(environment.get('action') or '在 Codex 中说“检查 Claude 安装、登录与版本兼容情况”，按检查结果处理。')
    if discovery.get('action') and discovery.get('action') not in steps:
        steps.append(discovery['action'])
    return {'schema_version': 2, 'plugin': {'name': manifest['name'], 'version': manifest['version']},
            'platform': {'system': platform.system(), 'machine': platform.machine()},
            'codex': host, 'uv_installed': uv_ready, 'claude': claude,
            'cli_management': cli_management,
            'cli_maintenance': maintenance,
            'ready': bool(host['desktop_compatibility'] == 'verified' and uv_ready and environment.get('ready')),
            'next_steps': steps,
            'remote_credentials_verified': False,
            'data_policy': 'Local diagnostics omit credentials, account identity, prompts and task artifacts; no telemetry is sent.'}
