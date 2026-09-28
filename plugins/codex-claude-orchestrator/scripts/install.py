"""Install via the host's supported plugin CLI without editing user config."""
import argparse
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import hashlib
import re
import stat
import time
import diagnostics
import cli_store
from executable_locator import configure_claude_bin, external_mode_requested


def execute(args):
    result = subprocess.run(args, text=True, capture_output=True, timeout=90)
    if result.returncode:
        raise RuntimeError(f"Command failed ({result.returncode}): {args[0]} {' '.join(args[1:3])}. {result.stderr[:1000]}")
    return result.stdout


def verify_package(root):
    root = root.resolve()
    manifest = root / "FILE-SHA256.json"
    if not manifest.is_file():
        raise RuntimeError("缺少 FILE-SHA256.json；请使用完整分发包。")
    entries = json.loads(manifest.read_text())
    if not isinstance(entries, dict) or not entries:
        raise RuntimeError("安装包哈希清单无效。")
    required = {'.agents/plugins/marketplace.json', 'plugins/codex-claude-orchestrator/.codex-plugin/plugin.json'}
    if not required.issubset(entries):
        raise RuntimeError("安装包哈希清单缺少插件或 marketplace 身份文件。")
    for relative, expected in entries.items():
        if not isinstance(relative, str) or not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected):
            raise RuntimeError("安装包哈希条目无效。")
        path = root / relative
        if Path(relative).is_absolute() or ".." in Path(relative).parts or not path.resolve().is_relative_to(root):
            raise RuntimeError("安装包存在不安全路径。")
        if any(parent.is_symlink() for parent in (path, *path.parents) if parent != root and parent.is_relative_to(root)) or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise RuntimeError(f"安装包文件校验不符：{relative}")
    return entries


def managed_backups(target):
    result = []
    for path in target.parent.glob('catalog-previous-*'):
        if path.is_symlink() or not path.is_dir():
            continue
        try:
            verify_package(path)
            market = json.loads((path / '.agents/plugins/marketplace.json').read_text())
            manifest = json.loads((path / 'plugins/codex-claude-orchestrator/.codex-plugin/plugin.json').read_text())
            if market.get('name') != 'codex-claude-team':
                continue
            result.append({'id': path.name, 'version': manifest['version'], 'saved_at': path.stat().st_mtime})
        except (RuntimeError, OSError, ValueError, KeyError):
            continue
    return sorted(result, key=lambda value: value['saved_at'], reverse=True)


LAUNCHER_RELATIVE = "plugins/codex-claude-orchestrator/scripts/launch.sh"
PLUGIN_ID = 'codex-claude-orchestrator@codex-claude-team'
PLUGIN_MANIFEST_RELATIVE = 'plugins/codex-claude-orchestrator/.codex-plugin/plugin.json'


def catalog_version(root):
    return json.loads((root / PLUGIN_MANIFEST_RELATIVE).read_text())['version']


@contextmanager
def catalog_install_lock(target):
    """Serialize catalog swap plus host registration for this installer.

    The lock intentionally spans registration and rollback.  A second
    installer must not replace a catalog while the first one is deciding
    whether its host registration succeeded.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    lock_path = target.parent / '.catalog-install.lock'
    with lock_path.open('a+') as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def install_catalog(root, target, entries):
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_symlink():
        raise RuntimeError("插件目录不能是符号链接。")
    if target.exists():
        marker = target / ".agents/plugins/marketplace.json"
        if not marker.is_file() or json.loads(marker.read_text()).get("name") != "codex-claude-team":
            raise RuntimeError(f"已有非本插件管理目录：{target}")
    staging = Path(tempfile.mkdtemp(prefix="catalog-stage-", dir=target.parent))
    for relative in entries:
        dest = staging / relative
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(root / relative, dest)
    # zip extraction can discard execute bits. Restore only the known MCP
    # launcher after its source content was verified by FILE-SHA256; no other
    # package path receives a mode change.
    if LAUNCHER_RELATIVE in entries:
        launcher = staging / LAUNCHER_RELATIVE
        if launcher.is_symlink() or not launcher.is_file():
            raise RuntimeError("安装包 MCP 启动器无效。")
        launcher.chmod(launcher.stat().st_mode | stat.S_IXUSR)
    shutil.copy2(root / "FILE-SHA256.json", staging / "FILE-SHA256.json")
    backup = None
    if target.exists():
        backup = target.with_name(staging.name.replace("stage", "previous"))
        target.rename(backup)
    try:
        staging.rename(target)
    except OSError:
        if backup is not None:
            backup.rename(target)
        raise
    return backup


def restore_catalog_after_registration_failure(target, backup, candidate_version, candidate_entries=None):
    """Put the verified previous catalog back without discarding the candidate.

    This is deliberately catalog-only: a failed command may have changed the
    host before reporting an error.  Callers must audit host registration
    separately and may only call the rollback complete when that audit proves
    the old registration is still active.
    """
    if backup is None:
        return {'catalog_rollback': 'not_available', 'reason': 'no_previous_catalog'}
    try:
        observed_entries = verify_package(target)
        verify_package(backup)
        if (catalog_version(target) != candidate_version
                or (candidate_entries is not None and observed_entries != candidate_entries)):
            return {'catalog_rollback': 'not_attempted', 'reason': 'catalog_changed_since_candidate'}
        previous_version = catalog_version(backup)
    except (RuntimeError, OSError, ValueError, KeyError) as error:
        return {'catalog_rollback': 'not_attempted', 'reason': f'catalog_not_verifiable: {error}'}

    failed_candidate = Path(tempfile.mkdtemp(prefix='catalog-unregistered-', dir=target.parent))
    failed_candidate.rmdir()
    restore_staging = Path(tempfile.mkdtemp(prefix='catalog-restore-', dir=target.parent))
    restore_staging.rmdir()
    try:
        # Keep the original managed backup in place.  It remains an auditable
        # rollback artifact even if the subsequent host audit is unavailable.
        shutil.copytree(backup, restore_staging, copy_function=shutil.copy2)
    except OSError as error:
        return {'catalog_rollback': 'not_attempted', 'reason': f'catalog_restore_stage_failed: {error}'}
    try:
        target.rename(failed_candidate)
        try:
            restore_staging.rename(target)
        except OSError:
            failed_candidate.rename(target)
            raise
    except OSError as error:
        if restore_staging.exists():
            shutil.rmtree(restore_staging, ignore_errors=True)
        return {'catalog_rollback': 'not_attempted', 'reason': f'catalog_swap_failed: {error}'}
    return {'catalog_rollback': 'restored', 'previous_version': previous_version,
            'candidate_backup': failed_candidate}


def verify_registration(cli, target, version):
    inventory = json.loads(execute([cli, 'plugin', 'list', '--json']))
    matching = [row for row in inventory.get('installed', [])
                if row.get('pluginId') == PLUGIN_ID]
    if len(matching) != 1:
        raise RuntimeError('宿主插件登记身份未确认。')
    row = matching[0]
    source = row.get('marketplaceSource', {})
    plugin_source = row.get('source', {})
    if (not row.get('installed') or not row.get('enabled') or row.get('version') != version
            or source.get('sourceType') != 'local'
            or Path(source.get('source', '')).resolve() != target.resolve()
            or plugin_source.get('source') != 'local'
            or Path(plugin_source.get('path', '')).resolve() != (target / 'plugins/codex-claude-orchestrator').resolve()):
        raise RuntimeError('宿主登记的插件版本、启用状态或来源与目标安装包不一致；安装结果未确认。')
    return {'plugin_id': row['pluginId'], 'version': row['version'], 'registration_verified': True}


def register_plugin(cli, target, version, backup, *, add_marketplace, candidate_entries=None):
    try:
        if add_marketplace:
            execute([cli, 'plugin', 'marketplace', 'add', str(target), '--json'])
        added = json.loads(execute([cli, 'plugin', 'add', 'codex-claude-orchestrator@codex-claude-team', '--json']))
        if not isinstance(added, dict) or added.get('error'):
            raise RuntimeError('宿主返回了无法确认的插件注册结果。')
        return verify_registration(cli, target, version)
    except Exception as original_error:
        # A command can time out after Codex has applied it.  Before changing
        # the catalog, ask the host once more whether the candidate is already
        # the verified registration.  This turns a transport failure into a
        # confirmed install only when the host inventory proves it.
        try:
            registration = verify_registration(cli, target, version)
        except Exception as candidate_audit_error:
            recovery = restore_catalog_after_registration_failure(target, backup, version, candidate_entries)
            if recovery['catalog_rollback'] == 'restored':
                try:
                    verify_registration(cli, target, recovery['previous_version'])
                except Exception as host_audit_error:
                    print(
                        '更新注册未确认；catalog 已恢复到旧版本 '
                        f"{recovery['previous_version']}，候选副本保留在 {recovery['candidate_backup'].name}。"
                        '宿主登记未能核实，因此不能声称已完全回滚；'
                        f'原始错误：{original_error}；候选审计：{candidate_audit_error}；'
                        f'恢复后宿主审计：{host_audit_error}。',
                        file=sys.stderr)
                else:
                    print(
                        '更新注册未确认；catalog 和宿主登记均已恢复到旧版本 '
                        f"{recovery['previous_version']}，候选副本保留在 {recovery['candidate_backup'].name}。"
                        f'原始错误：{original_error}；候选审计：{candidate_audit_error}。',
                        file=sys.stderr)
            elif recovery['catalog_rollback'] == 'not_available':
                print(
                    f'首次安装尚未确认成功；新安装源保留在 {target}，没有旧版本备份可回退。'
                    f'宿主登记状态未确认；原始错误：{original_error}；候选审计：{candidate_audit_error}。',
                    file=sys.stderr)
            else:
                print(
                    f'更新注册未确认；未自动恢复 catalog（{recovery["reason"]}），以避免覆盖其他安装。'
                    f'旧版本备份仍保留为 {backup.name if backup else "无"}；宿主登记状态未确认；'
                    f'原始错误：{original_error}；候选审计：{candidate_audit_error}。',
                    file=sys.stderr)
            raise
        else:
            print('注册命令报错，但宿主登记已复核为候选版本；安装已确认。', file=sys.stderr)
            return registration


def prepare_managed_cli() -> dict:
    """Stage the managed CLI without making plugin registration depend on it.

    A failed private bootstrap leaves the plugin install and its diagnostics
    usable.  This helper never calls Anthropic's global installer; the store
    reports a concrete recovery action instead of claiming managed readiness.
    """
    try:
        selection = cli_store.get_selection()
        if selection.get("mode") == "external" or external_mode_requested():
            return {"status": "external_preserved", "selection": selection,
                    "action": "User-selected external Claude CLI mode was preserved. Run the explicit claude_cli_update prepare action to switch to managed retention."}
        print("正在检查并准备插件支持的 Claude CLI；如需下载，将显示可续传进度。", flush=True)
        last_percent = -5
        last_printed_at = 0.0

        def progress(received: int, total: int | None) -> None:
            nonlocal last_percent, last_printed_at
            now = time.monotonic()
            percent = int(received * 100 / total) if total else 0
            if received != total and percent < last_percent + 5 and now - last_printed_at < 15:
                return
            last_percent = percent
            last_printed_at = now
            received_mib = received / (1024 * 1024)
            if total:
                print(f"Claude CLI 下载：{received_mib:.1f}/{total / (1024 * 1024):.1f} MiB ({percent}%)",
                      flush=True)
            else:
                print(f"Claude CLI 下载：{received_mib:.1f} MiB", flush=True)

        outcome = cli_store.prepare(progress=progress)
        print(f"Claude CLI 准备结果：{outcome.get('status', 'unknown')}。", flush=True)
        return outcome
    except (RuntimeError, ValueError, OSError) as error:
        return {"status": "bootstrap_error", "action": f"Managed Claude CLI preparation failed: {error}"}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--package-root", type=Path, required=True)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--diagnose-json", action="store_true", help="Print local diagnostic summary without installing")
    parser.add_argument("--list-backups", action="store_true", help="List verified managed rollback versions")
    parser.add_argument("--rollback", metavar="BACKUP_ID", help="Restore an id returned by --list-backups")
    parser.add_argument("--configure-claude-bin", metavar="ABSOLUTE_PATH", help="Persist an explicit Claude executable path for the Codex MCP process")
    args = parser.parse_args()
    if sys.platform != "darwin":
        raise RuntimeError("首发版仅支持 macOS。")
    root = args.package_root.resolve()
    target = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))).expanduser().resolve() / "claude-orchestrator/catalog"
    if args.list_backups:
        print(json.dumps({'backups': managed_backups(target)}, ensure_ascii=False, indent=2))
        return
    if args.rollback:
        backups = managed_backups(target)
        if args.rollback not in {value['id'] for value in backups}:
            raise RuntimeError('回退版本未通过验证；先用 --list-backups 查看可用 ID。')
        root = target.parent / args.rollback
    entries = verify_package(root)
    if args.configure_claude_bin:
        saved = configure_claude_bin(args.configure_claude_bin)
        print(json.dumps({'claude_bin_settings': str(saved), 'configured': True}, ensure_ascii=False, indent=2))
        return
    readiness = diagnostics.collect(str(root))
    if args.diagnose_json:
        print(json.dumps(readiness, ensure_ascii=False, indent=2))
        return
    print(json.dumps(readiness, ensure_ascii=False, indent=2), flush=True)
    if readiness['codex'].get('desktop_compatibility') != 'verified' or not readiness['uv_installed']:
        raise RuntimeError('安装所需的 Codex 插件支持或 uv 尚未就绪；请按 next_steps 处理。')
    if not readiness['claude'].get('ready'):
        print('Claude 委派暂不可用；插件仍可安装用于诊断。按 next_steps 完成 Claude 安装、登录或版本处理后再委派。', flush=True)
    cli = diagnostics.codex_cli()
    if not cli:
        raise RuntimeError("未找到 Codex；请先安装/更新 Codex 桌面客户端。")
    execute([cli, "plugin", "--help"])
    market_path = root / ".agents/plugins/marketplace.json"
    market = json.loads(market_path.read_text())
    name = market["name"]
    if name != 'codex-claude-team':
        raise RuntimeError('安装包 marketplace 名称不匹配。')
    markets = json.loads(execute([cli, "plugin", "marketplace", "list", "--json"]))["marketplaces"]
    matches = [m for m in markets if m["name"] == name]
    for match in matches:
        source = match.get("marketplaceSource", {}).get("source", match.get("root", ""))
        if not source or Path(source).expanduser().resolve() != target:
            raise RuntimeError(f"已有同名 marketplace {name}: {source}。请让维护者把插件加入现有 marketplace；未修改现有配置。")
    default_market = Path.home() / ".agents/plugins/marketplace.json"
    if default_market.exists() and default_market.resolve() != market_path:
        existing = json.loads(default_market.read_text())
        if existing.get("name") == name:
            raise RuntimeError(f"已有默认 marketplace {default_market} 使用名称 {name}；请合并插件条目后安装，未覆盖配置。")
    if readiness['ready']:
        print("环境检查通过；本地认证已配置（远端有效性需插件在线检查）。", flush=True)
    if args.check:
        print("检查模式：未注册 marketplace 或安装插件。")
        if not readiness['ready']:
            raise RuntimeError("安装检查完成，Claude 委派尚未就绪。")
        return
    managed_cli = prepare_managed_cli()
    print(json.dumps({"claude_cli_management": managed_cli}, ensure_ascii=False, indent=2), flush=True)
    if managed_cli.get("status") != "ready":
        print("Claude 私有受管版本尚未就绪；插件继续安装用于诊断。完成输出中的 bootstrap action 后，再运行 claude_cli_update prepare/validate。", flush=True)
    # The catalog is the local marketplace source.  Hold one installer lock
    # across its swap and the host confirmation so failure recovery cannot
    # roll back another installer's catalog.
    with catalog_install_lock(target):
        backup = install_catalog(root, target, entries)
        version = catalog_version(target)
        registration = register_plugin(cli, target, version, backup, add_marketplace=not matches,
                                       candidate_entries=entries)
    print(json.dumps(registration, ensure_ascii=False, indent=2), flush=True)
    if backup:
        print(f"上次安装源保留在：{backup}")
    print("插件版本和来源登记已核验。请打开一个新 Codex 任务加载更新。已有项目入口时直接提任务；首次项目可说‘这个项目以后采用 Codex–Claude 协作流程’。日常无需长口令。")


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, OSError, ValueError, subprocess.TimeoutExpired) as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1)
