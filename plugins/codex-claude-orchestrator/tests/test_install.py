import hashlib
import contextlib
import io
import json
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import install
import diagnostics


class InstallationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def package(self, name, version):
        root = self.base / name
        values = {'.agents/plugins/marketplace.json': {'name': 'codex-claude-team'},
                  'plugins/codex-claude-orchestrator/.codex-plugin/plugin.json': {'name': 'codex-claude-orchestrator', 'version': version}}
        hashes = {}
        for name, value in values.items():
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(value))
            hashes[name] = hashlib.sha256(path.read_bytes()).hexdigest()
        (root / 'FILE-SHA256.json').write_text(json.dumps(hashes))
        return root

    def package_with_zip_mode_launcher(self, name, version, label):
        root = self.package(name, version)
        launcher = root / install.LAUNCHER_RELATIVE
        launcher.parent.mkdir(parents=True, exist_ok=True)
        launcher.write_text(f"#!/bin/sh\nprintf '%s\\n' {label!r}\n")
        launcher.chmod(0o644)
        hashes = json.loads((root / 'FILE-SHA256.json').read_text())
        hashes[install.LAUNCHER_RELATIVE] = hashlib.sha256(launcher.read_bytes()).hexdigest()
        (root / 'FILE-SHA256.json').write_text(json.dumps(hashes))
        return root, launcher

    def test_failed_install_cleans_only_its_staging_and_preserves_old_catalog(self):
        old = self.package('old', '0.3.0')
        new, _ = self.package_with_zip_mode_launcher('new', '0.4.0', 'candidate')
        entries = install.verify_package(new)
        original_rename = Path.rename

        def fail_stage_swap(path, target):
            if path.name.startswith('catalog-stage-'):
                raise OSError('injected swap failure')
            return original_rename(path, target)

        failures = (patch.object(install.shutil, 'copy2', side_effect=OSError('injected copy failure')),
                    patch.object(Path, 'chmod', side_effect=OSError('injected chmod failure')),
                    patch.object(Path, 'rename', new=fail_stage_swap))
        for index, failure in enumerate(failures):
            with self.subTest(index=index):
                target = self.base / str(index) / 'catalog'
                install.install_catalog(old, target, install.verify_package(old))
                with failure, self.assertRaises(OSError):
                    install.install_catalog(new, target, entries)
                self.assertEqual(install.catalog_version(target), '0.3.0')
                self.assertEqual(install.verify_package(target), install.verify_package(old))
                self.assertEqual(list(target.parent.glob('catalog-stage-*')), [])

    def test_failed_catalog_restore_copy_removes_partial_staging_but_keeps_backup(self):
        old = self.package('old', '0.3.0')
        new = self.package('new', '0.4.0')
        target = self.base / 'managed/catalog'
        install.install_catalog(old, target, install.verify_package(old))
        backup = install.install_catalog(new, target, install.verify_package(new))

        def partial_copy(source, destination, **kwargs):
            destination.mkdir()
            (destination / 'partial').write_text('partial copy')
            raise OSError('injected restore copy failure')

        with patch.object(install.shutil, 'copytree', side_effect=partial_copy):
            result = install.restore_catalog_after_registration_failure(target, backup, '0.4.0')
        self.assertEqual(result['catalog_rollback'], 'not_attempted')
        self.assertEqual(list(target.parent.glob('catalog-restore-*')), [])
        self.assertEqual(install.catalog_version(target), '0.4.0')
        self.assertEqual(install.catalog_version(backup), '0.3.0')
        self.assertEqual(install.verify_package(backup), install.verify_package(old))

    def test_failed_swap_and_failed_rollback_keep_both_recoverable_copies(self):
        old = self.package('old', '0.3.0')
        new = self.package('new', '0.4.0')
        target = self.base / 'managed/catalog'
        install.install_catalog(old, target, install.verify_package(old))
        entries = install.verify_package(new)
        original_rename = Path.rename

        def fail_swaps(path, destination):
            if path.name.startswith(('catalog-stage-', 'catalog-previous-')):
                raise OSError('injected swap and restore failure')
            return original_rename(path, destination)

        with patch.object(Path, 'rename', new=fail_swaps), self.assertRaises(OSError):
            install.install_catalog(new, target, entries)
        self.assertFalse(target.exists())
        stages = list(target.parent.glob('catalog-stage-*'))
        backups = list(target.parent.glob('catalog-previous-*'))
        self.assertEqual(len(stages), 1)
        self.assertEqual(len(backups), 1)
        self.assertEqual(install.verify_package(stages[0]), entries)
        self.assertEqual(install.verify_package(backups[0]), install.verify_package(old))

    def test_upgrade_and_verified_rollback_preserve_previous_version(self):
        old = self.package('old', '0.3.0')
        new = self.package('new', '0.4.0')
        target = self.base / 'managed/catalog'
        install.install_catalog(old, target, install.verify_package(old))
        backup = install.install_catalog(new, target, install.verify_package(new))
        candidates = install.managed_backups(target)
        self.assertEqual([(r['id'], r['version']) for r in candidates], [(backup.name, '0.3.0')])
        newer_backup = install.install_catalog(backup, target, install.verify_package(backup))
        version_path = 'plugins/codex-claude-orchestrator/.codex-plugin/plugin.json'
        self.assertEqual(json.loads((target / version_path).read_text())['version'], '0.3.0')
        self.assertEqual(json.loads((newer_backup / version_path).read_text())['version'], '0.4.0')
        self.assertEqual(len(install.managed_backups(target)), 2)

    def test_zip_mode_launcher_is_executable_after_install_and_verified_rollback(self):
        old, old_source = self.package_with_zip_mode_launcher('old-zip', '0.3.0', 'old-launcher')
        new, new_source = self.package_with_zip_mode_launcher('new-zip', '0.4.1', 'new-launcher')
        target = self.base / 'managed/catalog'
        self.assertFalse(old_source.stat().st_mode & stat.S_IXUSR)
        install.install_catalog(old, target, install.verify_package(old))
        installed = target / install.LAUNCHER_RELATIVE
        self.assertTrue(installed.stat().st_mode & stat.S_IXUSR)
        self.assertEqual(hashlib.sha256(installed.read_bytes()).hexdigest(), hashlib.sha256(old_source.read_bytes()).hexdigest())
        self.assertEqual(subprocess.run([str(installed)], text=True, capture_output=True, check=True).stdout.strip(), 'old-launcher')
        backup = install.install_catalog(new, target, install.verify_package(new))
        self.assertIsNotNone(backup)
        self.assertEqual(subprocess.run([str(target / install.LAUNCHER_RELATIVE)], text=True, capture_output=True, check=True).stdout.strip(), 'new-launcher')
        install.install_catalog(backup, target, install.verify_package(backup))
        restored = target / install.LAUNCHER_RELATIVE
        self.assertTrue(restored.stat().st_mode & stat.S_IXUSR)
        self.assertEqual(hashlib.sha256(restored.read_bytes()).hexdigest(), hashlib.sha256(old_source.read_bytes()).hexdigest())
        self.assertEqual(subprocess.run([str(restored)], text=True, capture_output=True, check=True).stdout.strip(), 'old-launcher')

    def test_bad_hash_symlink_and_foreign_catalog_are_refused(self):
        root = self.package('source', '0.4.0')
        manifest = root / 'FILE-SHA256.json'
        expected = json.loads(manifest.read_text())
        path = root / '.agents/plugins/marketplace.json'
        original = path.read_text()
        path.write_text('{}')
        with self.assertRaises(RuntimeError):
            install.verify_package(root)
        path.unlink()
        outside = self.base / 'outside.json'; outside.write_text(original)
        path.symlink_to(outside)
        with self.assertRaises(RuntimeError):
            install.verify_package(root)
        target = self.base / 'foreign'; target.mkdir()
        (target / 'keep.txt').write_text('do not change')
        with self.assertRaises(RuntimeError):
            install.install_catalog(root, target, expected)
        self.assertEqual((target / 'keep.txt').read_text(), 'do not change')

    def test_registration_rejects_success_exit_with_stale_host_version(self):
        target = self.base / 'managed/catalog'
        source = self.package('candidate', '0.4.0')
        install.install_catalog(source, target, install.verify_package(source))
        row = {'pluginId':'codex-claude-orchestrator@codex-claude-team', 'installed':True, 'enabled':True,
               'version':'0.3.0', 'source':{'source':'local','path':str(target/'plugins/codex-claude-orchestrator')},
               'marketplaceSource':{'sourceType':'local','source':str(target)}}
        err = io.StringIO()
        with patch.object(install, 'execute', side_effect=['{}', json.dumps({'installed':[row]}),
                                                           json.dumps({'installed':[row]})]), contextlib.redirect_stderr(err):
            with self.assertRaises(RuntimeError):
                install.register_plugin('fake', target, '0.4.0', None, add_marketplace=False)
        self.assertIn('没有旧版本备份', err.getvalue())
        self.assertTrue(target.exists())
        self.assertEqual(install.managed_backups(target), [])
        row['version'] = '0.4.0'
        with patch.object(install, 'execute', return_value=json.dumps({'installed':[row]})):
            self.assertTrue(install.verify_registration('fake', target, '0.4.0')['registration_verified'])
        row['marketplaceSource']['source'] = str(self.base/'wrong-source')
        with patch.object(install, 'execute', return_value=json.dumps({'installed':[row]})):
            with self.assertRaises(RuntimeError):
                install.verify_registration('fake', target, '0.4.0')

    def test_failed_upgrade_restores_catalog_and_keeps_candidate_for_auditing(self):
        target = self.base / 'managed/catalog'
        old = self.package('old', '0.3.0'); new = self.package('new', '0.4.0')
        install.install_catalog(old, target, install.verify_package(old))
        backup = install.install_catalog(new, target, install.verify_package(new))
        old_row = {'pluginId': install.PLUGIN_ID, 'installed': True, 'enabled': True,
                   'version': '0.3.0', 'source': {'source': 'local', 'path': str(target / 'plugins/codex-claude-orchestrator')},
                   'marketplaceSource': {'sourceType': 'local', 'source': str(target)}}
        err = io.StringIO()
        with patch.object(install, 'execute', side_effect=[RuntimeError('registration rejected'),
                                                           json.dumps({'installed': [old_row]}),
                                                           json.dumps({'installed': [old_row]})]), contextlib.redirect_stderr(err):
            with self.assertRaises(RuntimeError):
                install.register_plugin('fake', target, '0.4.0', backup, add_marketplace=False,
                                        candidate_entries=install.verify_package(new))
        self.assertIn('catalog 和宿主登记均已恢复', err.getvalue())
        self.assertEqual(install.catalog_version(target), '0.3.0')
        candidates = list(target.parent.glob('catalog-unregistered-*'))
        self.assertEqual(len(candidates), 1)
        self.assertEqual(install.catalog_version(candidates[0]), '0.4.0')
        self.assertEqual(install.managed_backups(target)[0]['version'], '0.3.0')
        install.verify_package(target)

    def test_registration_error_is_accepted_when_followup_host_audit_confirms_candidate(self):
        target = self.base / 'managed/catalog'
        old = self.package('old', '0.3.0'); new = self.package('new', '0.4.0')
        install.install_catalog(old, target, install.verify_package(old))
        backup = install.install_catalog(new, target, install.verify_package(new))
        stale_row = {'pluginId': install.PLUGIN_ID, 'installed': True, 'enabled': True,
                     'version': '0.3.0', 'source': {'source': 'local', 'path': str(target / 'plugins/codex-claude-orchestrator')},
                     'marketplaceSource': {'sourceType': 'local', 'source': str(target)}}
        candidate_row = {**stale_row, 'version': '0.4.0'}
        err = io.StringIO()
        with patch.object(install, 'execute', side_effect=['{}', json.dumps({'installed': [stale_row]}),
                                                           json.dumps({'installed': [candidate_row]})]), contextlib.redirect_stderr(err):
            registration = install.register_plugin('fake', target, '0.4.0', backup, add_marketplace=False,
                                                   candidate_entries=install.verify_package(new))
        self.assertTrue(registration['registration_verified'])
        self.assertEqual(install.catalog_version(target), '0.4.0')
        self.assertTrue(backup.exists())
        self.assertIn('宿主登记已复核为候选版本', err.getvalue())

    def test_failed_upgrade_does_not_claim_host_rollback_when_post_failure_audit_is_unknown(self):
        target = self.base / 'managed/catalog'
        old = self.package('old', '0.3.0'); new = self.package('new', '0.4.0')
        install.install_catalog(old, target, install.verify_package(old))
        backup = install.install_catalog(new, target, install.verify_package(new))
        err = io.StringIO()
        with patch.object(install, 'execute', side_effect=RuntimeError('host unavailable')), contextlib.redirect_stderr(err):
            with self.assertRaises(RuntimeError):
                install.register_plugin('fake', target, '0.4.0', backup, add_marketplace=False,
                                        candidate_entries=install.verify_package(new))
        self.assertEqual(install.catalog_version(target), '0.3.0')
        self.assertEqual(len(list(target.parent.glob('catalog-unregistered-*'))), 1)
        self.assertIn('不能声称已完全回滚', err.getvalue())

    def test_catalog_recovery_refuses_to_replace_a_same_version_changed_candidate(self):
        target = self.base / 'managed/catalog'
        old = self.package('old', '0.3.0'); new = self.package('new', '0.4.0')
        install.install_catalog(old, target, install.verify_package(old))
        backup = install.install_catalog(new, target, install.verify_package(new))
        replacement = self.package('replacement', '0.4.0')
        manifest_path = replacement / install.PLUGIN_MANIFEST_RELATIVE
        manifest = json.loads(manifest_path.read_text())
        manifest['build'] = 'concurrent'
        manifest_path.write_text(json.dumps(manifest))
        hashes = json.loads((replacement / 'FILE-SHA256.json').read_text())
        hashes[install.PLUGIN_MANIFEST_RELATIVE] = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
        (replacement / 'FILE-SHA256.json').write_text(json.dumps(hashes))
        install.install_catalog(replacement, target, install.verify_package(replacement))
        recovery = install.restore_catalog_after_registration_failure(
            target, backup, '0.4.0', install.verify_package(new))
        self.assertEqual(recovery['catalog_rollback'], 'not_attempted')
        self.assertEqual(recovery['reason'], 'catalog_changed_since_candidate')
        self.assertEqual(install.catalog_version(target), '0.4.0')
        self.assertTrue(backup.exists())

    def test_diagnostics_do_not_export_account_or_provider_payload(self):
        env = {'ready': False, 'status': 'not_authenticated',
               'auth': {'status': 'not_logged_in', 'email': 'private@example.test', 'token': 'PRIVATE'},
               'cli': {'version': 'test'}, 'raw': 'PRIVATE'}
        with patch.object(diagnostics, 'codex_cli', return_value=None), \
                patch.object(diagnostics.bridge, 'check_environment', return_value=env), \
                patch.object(diagnostics.shutil, 'which', return_value=None):
            report = diagnostics.collect(str(self.base))
        encoded = json.dumps(report)
        self.assertNotIn('PRIVATE', encoded)
        self.assertNotIn('private@example', encoded)
        self.assertFalse(report['ready'])
        self.assertFalse(report['remote_credentials_verified'])
        self.assertGreaterEqual(len(report['next_steps']), 3)

    def test_bundled_nested_and_legacy_host_paths_precede_path_cli(self):
        for app in ('ChatGPT.app', 'Codex.app'):
            for relative in ('Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex',
                             'Contents/Resources/codex'):
                bundled = Path('/Applications') / app / relative
                with self.subTest(bundle=str(bundled)), \
                        patch.object(Path, 'is_file', lambda p: p == bundled), \
                        patch.object(Path, 'resolve', lambda p: p), \
                        patch.object(diagnostics.os, 'access', return_value=True), \
                        patch.object(diagnostics.shutil, 'which', return_value='/fixture/bin/codex'):
                    self.assertEqual(diagnostics._host_candidates(),
                                     [(str(bundled), 'desktop'), ('/fixture/bin/codex', 'path')])
                    self.assertEqual(diagnostics.host_source(str(bundled)), 'desktop')

    def test_path_cli_is_not_reported_as_a_verified_desktop_host(self):
        fake = self.base / 'codex'
        fake.write_text("#!/bin/sh\nif [ \"$1\" = \"--version\" ]; then echo 1.0; fi\nexit 0\n")
        fake.chmod(0o755)
        env = {'ready': True, 'status': 'ready', 'auth': {'status': 'reported_logged_in'},
               'cli': {'version': '2.1.277', 'profile': {'status': 'tested'}}}
        with patch.object(diagnostics, 'codex_cli', return_value=str(fake)), \
                patch.object(diagnostics, 'host_source', return_value='path'), \
                patch.object(diagnostics.bridge, 'check_environment', return_value=env), \
                patch.object(diagnostics, 'locate_claude', return_value={'path': '/fixture/claude', 'source': 'PATH', 'configured': False, 'candidate': 'claude'}), \
                patch.object(diagnostics.shutil, 'which', return_value='/fixture/uv'):
            report = diagnostics.collect(str(self.base))
        self.assertTrue(report['codex']['plugins_supported'])
        self.assertEqual(report['codex']['source'], 'path')
        self.assertEqual(report['codex']['desktop_compatibility'], 'unverified')
        self.assertFalse(report['ready'])
        self.assertIn('PATH 中的 codex 不作为桌面宿主能力证据。', report['next_steps'][0])

    def test_install_reports_the_local_cli_and_never_prepares_a_managed_copy(self):
        readiness = {'claude': {'ready': True, 'version': '2.1.284',
                                'discovery': {'path': '/fixture/claude', 'source': 'CLAUDE_BIN'}}}
        summary = install.local_cli_summary(readiness)
        self.assertEqual(summary['policy'], 'user_local_cli')
        self.assertEqual(summary['version_management'], 'retired')
        self.assertEqual((summary['path'], summary['source'], summary['version']), ('/fixture/claude', 'CLAUDE_BIN', '2.1.284'))
        self.assertFalse(hasattr(install, 'prepare_managed_cli'))
        source = (ROOT / 'scripts' / 'install.py').read_text()
        for forbidden in ('cli_store', 'prepare(', 'claude_cli_update prepare'):
            self.assertNotIn(forbidden, source)
        missing = install.local_cli_summary({'claude': {'ready': False, 'discovery': {'path': None}}})
        self.assertIsNone(missing['path'])
        self.assertFalse(missing['ready'])


if __name__ == '__main__':
    unittest.main()
