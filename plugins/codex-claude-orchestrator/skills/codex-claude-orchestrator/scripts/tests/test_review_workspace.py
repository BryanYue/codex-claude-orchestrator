import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
from review_workspace import (ReviewWorkspaceError, ProtectionUnavailable, prepare_review_workspace,
                              load_review_workspace, protected_command, cleanup_review_workspace)
import lane_lock


class ReviewWorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='review workspace "引用" ')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.source = self.root / 'source'; self.source.mkdir()
        self.run = self.root / 'run'; self.run.mkdir()
        self.git(self.source, 'init', '-q')
        self.git(self.source, 'config', 'user.email', 'test@example.invalid')
        self.git(self.source, 'config', 'user.name', 'Review Test')
        (self.source / 'sub').mkdir()
        (self.source / 'sub' / 'tracked.txt').write_text('baseline\n')
        (self.source / 'deleted.txt').write_text('delete me\n')
        (self.source / '.gitignore').write_text('private-key\ncache/\n')
        self.git(self.source, 'add', '.')
        self.git(self.source, 'commit', '-qm', 'baseline')

    def git(self, cwd, *args):
        result = subprocess.run(['git', '-c', 'core.hooksPath=/dev/null', *args], cwd=cwd, capture_output=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr.decode('utf-8', 'replace'))
        return result.stdout

    def prepare(self, sources=()):
        return prepare_review_workspace(self.source / 'sub', self.run, sources)

    def test_current_dirty_deleted_staged_and_untracked_inputs_without_ignored_state(self):
        tracked = self.source / 'sub' / 'tracked.txt'; tracked.write_bytes(b'dirty\xff\n')
        (self.source / 'deleted.txt').unlink()
        staged = self.source / 'staged.txt'; staged.write_text('staged')
        self.git(self.source, 'add', 'staged.txt')
        (self.source / 'new.txt').write_text('untracked')
        (self.source / 'private-key').write_text('DO NOT IMPORT')
        (self.source / 'cache').mkdir(); (self.source / 'cache' / 'session').write_text('DO NOT IMPORT')
        before = self.git(self.source, 'status', '--porcelain=v1', '-z')
        metadata = self.prepare()
        copy = Path(metadata['workspace_root'])
        self.assertEqual(Path(metadata['cwd']), copy / 'sub')
        self.assertEqual((copy / 'sub' / 'tracked.txt').read_bytes(), b'dirty\xff\n')
        self.assertFalse((copy / 'deleted.txt').exists())
        self.assertEqual((copy / 'staged.txt').read_text(), 'staged')
        self.assertEqual((copy / 'new.txt').read_text(), 'untracked')
        self.assertFalse((copy / 'private-key').exists()); self.assertFalse((copy / 'cache').exists())
        self.assertEqual(before, self.git(self.source, 'status', '--porcelain=v1', '-z'))
        self.assertEqual(before, self.git(copy, 'status', '--porcelain=v1', '-z'))
        self.assertEqual(metadata['staged_input_count'], 1)
        self.assertEqual(self.git(copy, 'remote'), b'')
        self.assertFalse((copy / '.git' / 'objects' / 'info' / 'alternates').exists())
        self.assertNotEqual(tracked.stat().st_ino, (copy / 'sub' / 'tracked.txt').stat().st_ino)
        original_inodes = {(path.stat().st_dev, path.stat().st_ino) for path in (self.source / '.git').rglob('*') if path.is_file()}
        copied_inodes = {(path.stat().st_dev, path.stat().st_ino) for path in (copy / '.git').rglob('*') if path.is_file()}
        self.assertFalse(original_inodes & copied_inodes)

    def test_index_binary_content_mode_add_delete_and_working_tree_are_preserved(self):
        tracked = self.source / 'sub' / 'tracked.txt'
        tracked.write_text('staged content\n'); tracked.chmod(0o755)
        binary = self.source / 'binary.dat'; binary.write_bytes(b'\0staged\xffbinary')
        self.git(self.source, 'add', 'sub/tracked.txt', 'binary.dat')
        self.git(self.source, 'rm', '-q', 'deleted.txt')
        tracked.write_text('later unstaged content\n')
        binary.write_bytes(b'\0later\x80binary')
        (self.source / 'deleted.txt').write_text('untracked recreation')
        staged_before = self.git(self.source, 'diff', '--cached', '--binary', '--full-index', '--no-renames')
        unstaged_before = self.git(self.source, 'diff', '--binary', '--full-index', '--no-renames')
        status_before = self.git(self.source, 'status', '--porcelain=v1', '-z')
        metadata = self.prepare(); copy = Path(metadata['workspace_root'])
        self.assertEqual(metadata['staged_input_count'], 3)
        self.assertEqual(self.git(copy, 'diff', '--cached', '--binary', '--full-index', '--no-renames'), staged_before)
        self.assertEqual(self.git(copy, 'diff', '--binary', '--full-index', '--no-renames'), unstaged_before)
        self.assertEqual(self.git(copy, 'status', '--porcelain=v1', '-z'), status_before)
        self.assertEqual(self.git(copy, 'show', ':binary.dat'), b'\0staged\xffbinary')
        self.assertEqual((copy / 'binary.dat').read_bytes(), b'\0later\x80binary')
        self.assertEqual(self.git(self.source, 'status', '--porcelain=v1', '-z'), status_before)

    def test_sensitive_untracked_is_rejected_but_explicitly_tracked_files_are_preserved(self):
        sensitive_paths = ['.env', 'sub/.env.production', '.claude/settings.json', 'sub/.codex/auth.json']
        for name in sensitive_paths:
            path = self.source / name; path.parent.mkdir(parents=True, exist_ok=True); path.write_text('private-state')
            with self.assertRaisesRegex(ReviewWorkspaceError, 'Sensitive untracked review inputs were not copied') as raised:
                self.prepare()
            self.assertIn(name, str(raised.exception))
            self.assertFalse((self.run / 'review-workspace').exists())
            self.git(self.source, 'add', name)
        metadata = self.prepare(); copy = Path(metadata['workspace_root'])
        self.assertEqual(metadata['staged_input_count'], len(sensitive_paths))
        for name in sensitive_paths:
            self.assertEqual((copy / name).read_text(), 'private-state')

    def test_original_alternates_are_rejected_even_when_empty(self):
        alternates = self.source / '.git' / 'objects' / 'info' / 'alternates'
        alternates.parent.mkdir(parents=True, exist_ok=True); alternates.write_text('')
        with self.assertRaisesRegex(ReviewWorkspaceError, 'Original Git alternates are unsupported'):
            self.prepare()
        self.assertFalse((self.run / 'review-workspace').exists())

    def test_custom_index_flags_are_not_silently_dropped(self):
        for flag, undo in (('--assume-unchanged', '--no-assume-unchanged'), ('--skip-worktree', '--no-skip-worktree')):
            self.git(self.source, 'update-index', flag, 'sub/tracked.txt')
            with self.assertRaisesRegex(ReviewWorkspaceError, 'index entries are unsupported'):
                self.prepare()
            self.git(self.source, 'update-index', undo, 'sub/tracked.txt')

    def test_unmerged_index_is_rejected_with_explicit_reason(self):
        blob = self.git(self.source, 'rev-parse', 'HEAD:sub/tracked.txt').strip()
        entries = b''.join(b'100644 ' + blob + b' ' + str(stage).encode() + b'\tsub/tracked.txt\n'
                           for stage in (1, 2, 3))
        result = subprocess.run(['git', 'update-index', '--index-info'], cwd=self.source, input=entries, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        with self.assertRaisesRegex(ReviewWorkspaceError, 'unmerged Git index is unsupported'):
            self.prepare()

    def test_resume_retains_changes_and_refuses_different_sources(self):
        metadata = self.prepare(); copy = Path(metadata['workspace_root'])
        (copy / 'reviewer-output.txt').write_text('retain me')
        (self.source / 'sub' / 'tracked.txt').write_text('source advanced')
        self.assertEqual(self.prepare(), metadata)
        self.assertEqual((copy / 'sub' / 'tracked.txt').read_text(), 'baseline\n')
        self.assertEqual((copy / 'reviewer-output.txt').read_text(), 'retain me')
        with self.assertRaisesRegex(ReviewWorkspaceError, 'different source inputs'):
            prepare_review_workspace(self.source, self.run)

    def test_copy_directory_and_source_overlap_rejected(self):
        inside = self.source / 'run'; inside.mkdir()
        with self.assertRaisesRegex(ReviewWorkspaceError, 'overlap'):
            prepare_review_workspace(self.source, inside)
        (self.run / 'review-workspace').mkdir()
        with self.assertRaisesRegex(ReviewWorkspaceError, 'Unowned'):
            self.prepare()

    def test_safe_relative_link_is_preserved_external_absolute_and_directory_links_rejected(self):
        link = self.source / 'alias'; link.symlink_to('sub/tracked.txt')
        metadata = self.prepare()
        copied = Path(metadata['workspace_root']) / 'alias'
        self.assertTrue(copied.is_symlink()); self.assertEqual(os.readlink(copied), 'sub/tracked.txt')
        for target in (str(self.source / 'sub' / 'tracked.txt'), '../outside'):
            other_run = self.root / f'run{len(target)}'; other_run.mkdir()
            link.unlink(); link.symlink_to(target)
            with self.assertRaisesRegex(ReviewWorkspaceError, 'External or absolute symlink'):
                prepare_review_workspace(self.source, other_run)
        link.unlink()
        (self.source / 'sub' / 'tracked.txt').unlink(); (self.source / 'sub').rmdir()
        (self.source / 'sub').symlink_to(self.root)
        directory_run = self.root / 'directory-run'; directory_run.mkdir()
        with self.assertRaisesRegex(ReviewWorkspaceError, 'Symlink directory|External or absolute symlink'):
            prepare_review_workspace(self.source, directory_run)

    def test_hardlinked_inputs_and_submodules_are_explicitly_rejected(self):
        os.link(self.source / 'sub' / 'tracked.txt', self.root / 'alias')
        with self.assertRaisesRegex(ReviewWorkspaceError, 'Hardlinked'):
            self.prepare()
        (self.root / 'alias').unlink()
        self.git(self.source, 'update-index', '--add', '--cacheinfo', '160000', self.git(self.source, 'rev-parse', 'HEAD').decode().strip(), 'module')
        (self.source / 'module').mkdir()
        with self.assertRaisesRegex(ReviewWorkspaceError, 'submodules'):
            self.prepare()

    def test_original_ignored_and_git_metadata_hardlink_aliases_are_rejected(self):
        ignored = self.source / 'private-key'; ignored.write_text('not imported')
        os.link(ignored, self.root / 'ignored-alias')
        with self.assertRaisesRegex(ReviewWorkspaceError, 'Hardlinked'):
            self.prepare()
        (self.root / 'ignored-alias').unlink()
        os.link(self.source / '.git' / 'config', self.root / 'config-alias')
        with self.assertRaisesRegex(ReviewWorkspaceError, 'Hardlinked'):
            self.prepare()

    def test_linked_worktree_has_independent_gitdir_and_common_dir_protection(self):
        worktree = self.root / 'linked-source'
        self.git(self.source, 'worktree', 'add', '-qb', 'review-branch', str(worktree))
        (worktree / 'sub' / 'tracked.txt').write_text('linked dirty')
        metadata = prepare_review_workspace(worktree / 'sub', self.run)
        self.assertIn(str((self.source / '.git').resolve()), metadata['source_git_dirs'])
        self.assertEqual(len(metadata['source_git_dirs']), 2)
        copy = Path(metadata['workspace_root'])
        self.assertEqual((copy / 'sub' / 'tracked.txt').read_text(), 'linked dirty')
        self.assertTrue((copy / '.git').is_dir())
        self.assertEqual(self.git(copy, 'remote'), b'')
        if sys.platform == 'darwin':
            command = protected_command([sys.executable, '-c',
                'import pathlib,sys;pathlib.Path(sys.argv[1]).write_text("attack")',
                str(Path(metadata['source_git_dirs'][0]) / 'denied')], metadata)
            result = subprocess.run(command, capture_output=True)
            if result.returncode == 71 and b'sandbox_apply: Operation not permitted' in result.stderr:
                self.skipTest('outer execution sandbox forbids nested sandbox-exec; rerun outside it')
            self.assertNotEqual(result.returncode, 0)
            self.assertIn(b'Operation not permitted', result.stderr)

    def test_identity_tamper_and_redirected_cwd_fail_closed(self):
        metadata = self.prepare()
        metadata_path = Path(metadata['metadata_path'])
        envelope = json.loads(metadata_path.read_text()); envelope['identity']['protected_paths'] = []
        metadata_path.write_text(json.dumps(envelope))
        with self.assertRaisesRegex(ReviewWorkspaceError, 'digest mismatch'):
            load_review_workspace(self.run)

    def test_cleanup_is_explicit_terminal_and_retains_uncertain_or_returned_copies(self):
        metadata = self.prepare(); copy = Path(metadata['workspace_root'])
        for terminal, explicit, status in ((False, True, 'accepted'), (True, False, 'accepted'),
                                           (True, True, 'unknown'), (True, True, 'returned'),
                                           (True, True, 'revision_requested'), (True, True, 'running')):
            self.assertFalse(cleanup_review_workspace(self.run, terminal=terminal, explicit=explicit, status=status))
            self.assertTrue(copy.exists())
        self.assertTrue(cleanup_review_workspace(self.run, terminal=True, explicit=True, status='accepted'))
        self.assertTrue(Path(metadata['metadata_path']).exists())
        self.assertTrue(self.source.exists())
        with self.assertRaisesRegex(ReviewWorkspaceError, 'missing or redirected'):
            load_review_workspace(self.run)

    def test_unsupported_host_never_returns_unprotected_command(self):
        metadata = self.prepare()
        with mock.patch('review_workspace.sys.platform', 'linux'):
            with self.assertRaisesRegex(ProtectionUnavailable, 'strict review'):
                protected_command(['/bin/true'], metadata)

    @unittest.skipUnless(sys.platform == 'darwin', 'actual macOS process boundary')
    def test_actual_descendants_cannot_mutate_source_gitdir_requirements_or_identity(self):
        requirement = self.root / 'requirements.md'; requirement.write_text('protected requirement')
        metadata = self.prepare([requirement])
        copy = Path(metadata['workspace_root'])
        outside_alias = self.root / 'symlink-alias'; outside_alias.symlink_to(self.source, target_is_directory=True)
        cache = self.root / 'cli-session-cache'
        attack = r'''
import json, os, pathlib, subprocess, sys
source, copy, requirement, identity, alias, cache = map(pathlib.Path, sys.argv[1:])
attempts = {}
def attempt(name, action):
    try: action()
    except OSError: attempts[name] = 'denied'
    else: attempts[name] = 'allowed'
attempt('tracked', lambda: (source/'sub/tracked.txt').write_text('attack'))
attempt('gitdir', lambda: (source/'.git/config').write_text('attack'))
attempt('new', lambda: (source/'new-file').write_text('attack'))
attempt('delete', lambda: (source/'deleted.txt').unlink())
attempt('rename', lambda: source.rename(source.parent/'moved-source'))
attempt('ancestor-rename', lambda: source.parent.rename(source.parent.with_name(source.parent.name+'-moved')))
attempt('chmod', lambda: (source/'sub/tracked.txt').chmod(0o777))
attempt('symlink-alias', lambda: (alias/'sub/tracked.txt').write_text('attack'))
attempt('hardlink-alias', lambda: os.link(source/'sub/tracked.txt', source.parent/'hardlink-alias'))
attempt('requirement', lambda: requirement.write_text('attack'))
attempt('identity', lambda: identity.write_text('{}'))
attempt('copy', lambda: (copy/'reviewer-output').write_text('allowed'))
attempt('cache', lambda: cache.write_text('allowed'))
attempts['read'] = (source/'sub/tracked.txt').read_text()
child = subprocess.run(['/bin/sh', '-c', 'printf attack > "$1"', 'sh', str(source/'sub/tracked.txt')], capture_output=True)
attempts['shell-child'] = 'denied' if child.returncode and b'Operation not permitted' in child.stderr else 'wrong-outcome'
grandchild_code = 'import pathlib,sys;pathlib.Path(sys.argv[1]).write_text("attack")'
child_code = 'import subprocess,sys;sys.exit(subprocess.run([sys.executable,"-c",sys.argv[2],sys.argv[1]]).returncode)'
grandchild = subprocess.run([sys.executable, '-c', child_code, str(source/'sub/tracked.txt'), grandchild_code], capture_output=True)
attempts['grandchild'] = 'denied' if grandchild.returncode and b'Operation not permitted' in grandchild.stderr else 'wrong-outcome'
print(json.dumps(attempts))
'''
        command = protected_command([sys.executable, '-c', attack, str(self.source), str(copy), str(requirement),
                                     metadata['metadata_path'], str(outside_alias), str(cache)], metadata)
        result = subprocess.run(command, cwd=copy, capture_output=True, text=True)
        if result.returncode == 71 and 'sandbox_apply: Operation not permitted' in result.stderr:
            self.skipTest('outer execution sandbox forbids nested sandbox-exec; rerun this test outside it')
        self.assertEqual(result.returncode, 0, result.stderr)
        attempts = json.loads(result.stdout)
        for name in ('tracked', 'gitdir', 'new', 'delete', 'rename', 'ancestor-rename', 'chmod', 'symlink-alias',
                     'hardlink-alias', 'requirement', 'identity', 'shell-child', 'grandchild'):
            self.assertEqual(attempts[name], 'denied', (name, attempts))
        self.assertEqual(attempts['copy'], 'allowed'); self.assertEqual(attempts['cache'], 'allowed')
        self.assertEqual(attempts['read'], 'baseline\n')
        self.assertEqual((self.source/'sub/tracked.txt').read_text(), 'baseline\n')
        self.assertEqual(requirement.read_text(), 'protected requirement')

    @unittest.skipUnless(sys.platform == 'darwin', 'actual macOS control-file boundary')
    def test_extra_control_files_and_plugin_code_denied_activity_and_report_allowed(self):
        metadata = self.prepare(); copy = Path(metadata['workspace_root'])
        current_run = self.root / 'resumed-run'; current_run.mkdir()
        result_path = current_run / 'result.json'; result_path.write_text('trusted result')
        link = current_run / 'review-workspace-link.json'; link.write_text('trusted link')
        receipt = current_run / 'future-receipt.json'
        plugin = self.root / 'plugin-code'; plugin.mkdir(); (plugin / 'hook.py').write_text('trusted code')
        activity = current_run / 'activity.jsonl'; activity.write_text('before\n')
        code = r'''
import json, os, pathlib, sys
result, link, receipt, plugin, activity, copy, old_metadata = map(pathlib.Path, sys.argv[1:])
outcomes = {}
def attempt(name, action):
    try: action()
    except OSError: outcomes[name] = 'denied'
    else: outcomes[name] = 'allowed'
attempt('result', lambda: result.write_text('forged'))
attempt('link', lambda: link.write_text('forged'))
attempt('receipt-create', lambda: receipt.write_text('forged'))
attempt('plugin-code', lambda: (plugin/'hook.py').write_text('forged'))
attempt('old-metadata', lambda: old_metadata.write_text('{}'))
attempt('result-rename', lambda: result.rename(result.with_name('moved-result')))
attempt('run-rename', lambda: result.parent.rename(result.parent.with_name('moved-run')))
attempt('result-hardlink-alias', lambda: os.link(result, result.parent/'alias-result'))
attempt('metadata-hardlink-alias', lambda: os.link(old_metadata, result.parent/'alias-metadata'))
attempt('future-control-hardlink', lambda: os.link(activity, receipt))
attempt('future-control-symlink', lambda: receipt.symlink_to(activity))
attempt('activity-append', lambda: activity.open('a').write('allowed\n'))
attempt('copy-write', lambda: (copy/'review-output').write_text('allowed'))
report_dir = copy/'.codex-review'; report_dir.mkdir()
attempt('report', lambda: (report_dir/'report.json').write_text('{}'))
print(json.dumps(outcomes))
'''
        command = protected_command([sys.executable, '-c', code, str(result_path), str(link), str(receipt), str(plugin),
                                     str(activity), str(copy), metadata['metadata_path']], metadata,
                                     extra_protected=[result_path, link, receipt, plugin])
        got = subprocess.run(command, cwd=copy, capture_output=True, text=True)
        if got.returncode == 71 and 'sandbox_apply: Operation not permitted' in got.stderr:
            self.skipTest('outer execution sandbox forbids nested sandbox-exec; rerun outside it')
        self.assertEqual(got.returncode, 0, got.stderr)
        outcomes = json.loads(got.stdout)
        for name in ('result', 'link', 'receipt-create', 'plugin-code', 'old-metadata', 'result-rename', 'run-rename',
                     'result-hardlink-alias', 'metadata-hardlink-alias', 'future-control-hardlink', 'future-control-symlink'):
            self.assertEqual(outcomes[name], 'denied', (name, outcomes))
        for name in ('activity-append', 'copy-write', 'report'):
            self.assertEqual(outcomes[name], 'allowed')
        self.assertEqual(activity.read_text(), 'before\nallowed\n')
        self.assertEqual(result_path.read_text(), 'trusted result')
        self.assertEqual(link.read_text(), 'trusted link')
        self.assertFalse(receipt.exists())

    def test_extra_protected_hardlinked_control_file_is_rejected_before_launch(self):
        metadata = self.prepare()
        control = self.root / 'control.json'; control.write_text('trusted')
        os.link(control, self.root / 'control-alias')
        with mock.patch('review_workspace.sys.platform', 'darwin'):
            with mock.patch('review_workspace.Path.is_file', return_value=True):
                with self.assertRaises(ReviewWorkspaceError):
                    protected_command(['/bin/true'], metadata, [control])

    @unittest.skipUnless(sys.platform == 'darwin', 'actual macOS Runtime control-plane boundary')
    def test_runtime_registry_receipts_and_dual_lane_roots_protected_with_journal_and_stderr_allowed(self):
        state_root = self.root / 'runtime-state'
        run = state_root / 'runs' / 'review-run'; run.mkdir(parents=True)
        metadata = prepare_review_workspace(self.source / 'sub', run)
        copy = Path(metadata['workspace_root'])
        lifecycle = state_root / 'bridge-receipts' / 'review-run.json'
        controls = {
            'registry-decision': state_root / 'registry.json',
            'registry-lock': state_root / 'registry.lock',
            'packet': state_root / 'packets' / 'review-run.json',
            'bridge-receipt': lifecycle,
            'cancel-request': state_root / 'cancel-requests' / 'review-run.json',
            'content': state_root / 'content' / 'requirements.txt',
        }
        for path in controls.values():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('trusted control')
        recovery = state_root / 'recovery-receipts' / 'future-run.json'
        journal = run / 'activity.jsonl'; journal.write_text('before\n')
        bridge_log = state_root / 'bridge-logs' / 'review-run.log'
        bridge_log.parent.mkdir()
        temporary_root = self.root / 'lane-temp'; temporary_root.mkdir()
        with mock.patch.dict(os.environ, {'CODEX_CLAUDE_COORDINATION_ROOT': str(self.root / 'coordination')}):
            with mock.patch('lane_lock.tempfile.gettempdir', return_value=str(temporary_root)):
                lock = lane_lock.acquire(str(self.source))
                try:
                    controls['durable-lock'] = Path(lock.durable.name)
                    controls['legacy-lock'] = Path(lock.legacy.name)
                    marker_roots = lane_lock.marker_roots()
                    for label, root in zip(('durable-marker', 'legacy-marker'), marker_roots):
                        controls[label] = root / 'review-run.json'
                        controls[label].write_text('trusted marker')
                    # These fixed names match the trusted lifecycle layout; absent directories
                    # are included so the reviewer cannot forge future control records.
                    protected = [lifecycle.parent.parent / name for name in (
                        'registry.json', 'registry.lock', 'packets', 'bridge-receipts',
                        'recovery-receipts', 'cancel-requests', 'content')]
                    protected += [lane_lock.coordination_root(),
                                  lane_lock.legacy_lock_path(str(self.source)).parent, *marker_roots]
                    payload = {name: str(path) for name, path in controls.items()}
                    code = r'''
import json, os, pathlib, sys
paths = {name: pathlib.Path(path) for name, path in json.loads(sys.argv[1]).items()}
recovery, journal, copy = map(pathlib.Path, sys.argv[2:5])
outcomes = {}
def attempt(name, action):
    try: action()
    except OSError: outcomes[name] = 'denied'
    else: outcomes[name] = 'allowed'
for name, path in paths.items():
    attempt(name+'-rewrite', lambda path=path: path.write_text('{"decision":"accepted"}'))
    attempt(name+'-delete', lambda path=path: path.unlink())
    attempt(name+'-hardlink', lambda path=path, name=name: os.link(path, copy/('alias-'+name)))
attempt('future-recovery-directory', lambda: recovery.parent.mkdir())
attempt('future-recovery-receipt', lambda: recovery.write_text('forged'))
attempt('future-packet', lambda: (paths['packet'].parent/'forged.json').write_text('forged'))
attempt('future-durable-lock', lambda: (paths['durable-lock'].parent/'forged.lock').write_text('forged'))
attempt('future-legacy-marker', lambda: (paths['legacy-marker'].parent/'forged.json').write_text('forged'))
attempt('journal-append', lambda: journal.open('a').write('allowed\n'))
attempt('copy-write', lambda: (copy/'review-output').write_text('allowed'))
attempt('stderr-write', lambda: sys.stderr.write('allowed stderr\n'))
print(json.dumps(outcomes))
'''
                    command = protected_command([sys.executable, '-c', code, json.dumps(payload),
                                                 str(recovery), str(journal), str(copy)], metadata, protected)
                    with bridge_log.open('w') as stderr:
                        got = subprocess.run(command, cwd=copy, stdout=subprocess.PIPE, stderr=stderr, text=True)
                    if got.returncode == 71 and 'sandbox_apply: Operation not permitted' in bridge_log.read_text():
                        self.skipTest('outer execution sandbox forbids nested sandbox-exec; rerun outside it')
                    self.assertEqual(got.returncode, 0, bridge_log.read_text())
                    outcomes = json.loads(got.stdout)
                    for name in controls:
                        for operation in ('rewrite', 'delete', 'hardlink'):
                            self.assertEqual(outcomes[name+'-'+operation], 'denied', (name, operation, outcomes))
                    for name in ('future-recovery-directory', 'future-recovery-receipt', 'future-packet',
                                 'future-durable-lock', 'future-legacy-marker'):
                        self.assertEqual(outcomes[name], 'denied', (name, outcomes))
                    for name in ('journal-append', 'copy-write', 'stderr-write'):
                        self.assertEqual(outcomes[name], 'allowed', (name, outcomes))
                    self.assertEqual(journal.read_text(), 'before\nallowed\n')
                    self.assertEqual(bridge_log.read_text(), 'allowed stderr\n')
                    self.assertFalse(recovery.parent.exists())
                    self.assertEqual(controls['registry-decision'].read_text(), 'trusted control')
                    for name in ('durable-marker', 'legacy-marker'):
                        self.assertEqual(controls[name].read_text(), 'trusted marker')
                    # The trusted parent remains free to persist lifecycle updates.
                    controls['registry-decision'].write_text('trusted parent update')
                    self.assertEqual(controls['registry-decision'].read_text(), 'trusted parent update')
                finally:
                    lock.close()

    @unittest.skipUnless(sys.platform == 'darwin', 'actual macOS installed-code boundary')
    def test_installed_plugin_with_external_venv_interpreter_uses_specific_code_protection(self):
        metadata = self.prepare(); copy = Path(metadata['workspace_root'])
        plugin = self.root / 'installed-plugin'; plugin.mkdir()
        code_directories = [plugin / name for name in ('scripts', 'skills', 'assets', '.codex-plugin')]
        for directory in code_directories:
            directory.mkdir()
        helper = plugin / 'scripts' / 'helper.py'; helper.write_text('trusted helper')
        bridge_file = plugin / 'skills' / 'bridge.py'; bridge_file.write_text('trusted bridge')
        asset = plugin / 'assets' / 'review-workflow.js'; asset.write_text('trusted workflow')
        config_paths = [plugin / name for name in ('code-identity.json', 'pyproject.toml', 'uv.lock', '.mcp.json')]
        for config in config_paths:
            config.write_text('trusted config')
        interpreter = plugin / '.venv' / 'bin' / 'python3'; interpreter.parent.mkdir(parents=True)
        interpreter.symlink_to(sys.executable)
        runtime_cache = plugin / '.venv' / 'session-cache'
        code = r'''
import json, pathlib, sys
plugin, copy, runtime_cache = map(pathlib.Path, sys.argv[1:4])
outcomes = {}
def attempt(name, action):
    try: action()
    except OSError: outcomes[name] = 'denied'
    else: outcomes[name] = 'allowed'
attempt('script-change', lambda: (plugin/'scripts/helper.py').write_text('forged'))
attempt('script-shadow', lambda: (plugin/'scripts/json.py').write_text('forged'))
attempt('bridge-change', lambda: (plugin/'skills/bridge.py').write_text('forged'))
attempt('workflow-change', lambda: (plugin/'assets/review-workflow.js').write_text('forged'))
attempt('plugin-metadata-shadow', lambda: (plugin/'.codex-plugin/new.json').write_text('forged'))
attempt('config-change', lambda: (plugin/'code-identity.json').write_text('forged'))
attempt('copy-write', lambda: (copy/'review-output').write_text('allowed'))
attempt('runtime-cache', lambda: runtime_cache.write_text('allowed'))
outcomes['empty-argument'] = sys.argv[4]
outcomes['code-read'] = (plugin/'scripts/helper.py').read_text()
print(json.dumps(outcomes))
'''
        command = protected_command([str(interpreter), '-c', code, str(plugin), str(copy), str(runtime_cache), ''],
                                     metadata, code_directories + config_paths)
        got = subprocess.run(command, cwd=copy, capture_output=True, text=True)
        if got.returncode == 71 and 'sandbox_apply: Operation not permitted' in got.stderr:
            self.skipTest('outer execution sandbox forbids nested sandbox-exec; rerun outside it')
        self.assertEqual(got.returncode, 0, got.stderr)
        outcomes = json.loads(got.stdout)
        for name in ('script-change', 'script-shadow', 'bridge-change', 'workflow-change', 'plugin-metadata-shadow', 'config-change'):
            self.assertEqual(outcomes[name], 'denied', (name, outcomes))
        self.assertEqual(outcomes['copy-write'], 'allowed')
        self.assertEqual(outcomes['runtime-cache'], 'allowed')
        self.assertEqual(outcomes['empty-argument'], '')
        self.assertEqual(outcomes['code-read'], 'trusted helper')


if __name__ == '__main__':
    unittest.main()
