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
import cli_env  # noqa: E402
from executable_locator import locate_claude  # noqa: E402


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


def collect(cwd: str) -> dict:
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
    environment = cli_env.check(folder)
    discovery = locate_claude()
    # Only selected fields: never the account identifier or the full environment payload.
    claude = {key: environment.get(key) for key in ('ready', 'status') if key in environment}
    claude['version'] = (environment.get('cli') or {}).get('version')
    claude['auth_status'] = (environment.get('auth') or {}).get('status')
    claude['credential_validity'] = (environment.get('auth') or {}).get('credential_validity')
    claude['missing_flags'] = (environment.get('cli') or {}).get('missing_flags') or []
    claude['sandbox_available'] = (environment.get('sandbox') or {}).get('available')
    claude['discovery'] = {key: discovery.get(key) for key in ('path', 'source', 'configured', 'candidate', 'settings_path')}
    uv_ready = shutil.which('uv') is not None
    steps = []
    if host['desktop_compatibility'] != 'verified':
        steps.append('未确认 Codex 桌面宿主的内置插件 CLI；安装或更新 Codex.app 后重新运行检查。PATH 中的 codex 不作为桌面宿主能力证据。')
    if not uv_ready:
        steps.append('安装 uv 后重新运行安装检查：https://docs.astral.sh/uv/getting-started/installation/')
    if not environment.get('ready'):
        steps.append(environment.get('action') or '在 Codex 中说“检查 Claude 安装和登录”，按检查结果处理。')
    if discovery.get('action') and discovery.get('action') not in steps:
        steps.append(discovery['action'])
    return {'schema_version': 3, 'plugin': {'name': manifest['name'], 'version': manifest['version']},
            'platform': {'system': platform.system(), 'machine': platform.machine()},
            'codex': host, 'uv_installed': uv_ready, 'claude': claude,
            'ready': bool(host['desktop_compatibility'] == 'verified' and uv_ready and environment.get('ready')),
            'next_steps': steps, 'remote_credentials_verified': False,
            'data_policy': 'Local diagnostics omit credentials, account identity, prompts and task artifacts; no telemetry is sent.'}
