import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN / "scripts"))
import plugin_identity  # noqa: E402

BUILDER = PLUGIN.parents[1] / "tools" / "build_distribution.py"
PLUGIN_RELATIVE = "plugins/codex-claude-orchestrator"
MANIFEST = ".codex-plugin/plugin.json"


def git(cwd, *args):
    return subprocess.run(["git", "-c", "user.email=t@example.invalid", "-c", "user.name=t",
                           "-c", "commit.gpgsign=false", *args], cwd=cwd, check=True,
                          capture_output=True, text=True).stdout.strip()


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def make_plugin(root, version="0.5.0+test"):
    write(root / MANIFEST, json.dumps({"name": "codex-claude-orchestrator", "version": version}))
    write(root / "scripts/a.py", "print(1)\n")
    write(root / "skills/x/SKILL.md", "# skill\n")
    return root


def make_repo(base, name="repo"):
    repo = base / name
    repo.mkdir()
    git(repo, "init", "-q")
    plugin = make_plugin(repo / PLUGIN_RELATIVE)
    write(repo / "README.md", "# repo\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "initial")
    return repo, plugin, git(repo, "rev-parse", "HEAD")


class PluginIdentityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_git_probe_failure_is_unknown_with_the_actual_failure_kind(self):
        plugin = make_plugin(self.base / "p")
        for outcome in (FileNotFoundError("git"), subprocess.TimeoutExpired("git", 20),
                        subprocess.CompletedProcess([], 128, b"", b"fatal: detected dubious ownership")):
            with self.subTest(outcome=type(outcome).__name__):
                options = {"side_effect": outcome} if isinstance(outcome, Exception) else {"return_value": outcome}
                with mock.patch.object(plugin_identity, "_git", **options):
                    identity = plugin_identity.collect(plugin)
                self.assertEqual(identity["source"]["kind"], "unknown")
                self.assertIn("git", identity["source"]["error"])
                self.assertIsNone(identity["source"]["revision"])

    def test_clean_git_checkout_reports_revision_version_and_code_digest(self):
        repo, plugin, head = make_repo(self.base)
        identity = plugin_identity.collect(plugin, "contract-1")
        self.assertEqual(identity["plugin_version"], "0.5.0+test")
        self.assertEqual(identity["plugin_name"], "codex-claude-orchestrator")
        self.assertEqual(identity["bridge_contract_id"], "contract-1")
        source = identity["source"]
        self.assertEqual((source["kind"], source["revision"], source["state"]), ("git", head, "clean"))
        self.assertEqual((source["dirty_count"], source["dirty_scope"]), (0, "plugin_root"))
        digest = identity["code_digest"]
        self.assertRegex(digest["value"], r"^[0-9a-f]{64}$")
        self.assertEqual(digest["file_count"], 3)
        self.assertIn("plugin root", digest["scope"])

    def test_dirty_code_keeps_the_commit_but_changes_state_and_digest(self):
        repo, plugin, head = make_repo(self.base)
        clean = plugin_identity.collect(plugin)
        (plugin / "scripts/a.py").write_text("print(2)\n")
        modified = plugin_identity.collect(plugin)
        self.assertEqual(modified["source"]["revision"], head)
        self.assertEqual(modified["source"]["state"], "dirty")
        self.assertIn(f"{PLUGIN_RELATIVE}/scripts/a.py", modified["source"]["dirty_paths"])
        self.assertNotEqual(modified["code_digest"]["value"], clean["code_digest"]["value"])
        (plugin / "scripts/a.py").write_text("print(1)\n")
        write(plugin / "scripts/new.py", "x = 1\n")
        untracked = plugin_identity.collect(plugin)
        self.assertEqual(untracked["source"]["state"], "dirty")
        self.assertIn(f"{PLUGIN_RELATIVE}/scripts/new.py", untracked["source"]["dirty_paths"])

    def test_dirty_paths_are_bounded_and_flagged_truncated(self):
        repo, plugin, _ = make_repo(self.base)
        for index in range(plugin_identity.DIRTY_SAMPLE_LIMIT + 5):
            write(plugin / f"scripts/extra_{index:02d}.py", "x = 1\n")
        source = plugin_identity.collect(plugin)["source"]
        self.assertEqual(source["dirty_count"], plugin_identity.DIRTY_SAMPLE_LIMIT + 5)
        self.assertEqual(len(source["dirty_paths"]), plugin_identity.DIRTY_SAMPLE_LIMIT)
        self.assertTrue(source["dirty_paths_truncated"])

    def test_change_outside_the_plugin_root_does_not_make_the_plugin_dirty(self):
        repo, plugin, _ = make_repo(self.base)
        (repo / "README.md").write_text("# changed\n")
        self.assertEqual(plugin_identity.collect(plugin)["source"]["state"], "clean")

    def test_rename_status_does_not_invent_paths(self):
        raw = b"R  new.py\0old.py\0 M other.py\0"
        self.assertEqual(plugin_identity._status_paths(raw), ["new.py", "other.py"])

    def test_revision_comes_from_the_plugin_not_from_the_working_directory(self):
        repo, plugin, head = make_repo(self.base, "plugin-repo")
        other, _, other_head = make_repo(self.base, "reviewed-repo")
        write(other / "extra.txt", "second\n")
        git(other, "add", "-A")
        git(other, "commit", "-qm", "second")
        other_head = git(other, "rev-parse", "HEAD")
        previous = os.getcwd()
        os.chdir(other)
        try:
            explicit = plugin_identity.collect(plugin)
            default = plugin_identity.collect()
        finally:
            os.chdir(previous)
        self.assertEqual(explicit["source"]["revision"], head)
        self.assertNotEqual(explicit["source"]["revision"], other_head)
        self.assertEqual(default["plugin_root"], str(Path(plugin_identity.__file__).resolve().parents[1]))
        self.assertEqual(plugin_identity.PLUGIN_ROOT, Path(plugin_identity.__file__).resolve().parents[1])
        self.assertNotEqual(default["source"].get("revision"), other_head)

    def test_git_directory_that_does_not_track_the_plugin_is_not_its_checkout(self):
        repo = self.base / "unrelated"
        repo.mkdir()
        git(repo, "init", "-q")
        write(repo / "README.md", "# unrelated\n")
        git(repo, "add", "-A")
        git(repo, "commit", "-qm", "initial")
        plugin = make_plugin(repo / "installed" / "codex-claude-orchestrator")
        source = plugin_identity.collect(plugin)["source"]
        self.assertEqual((source["kind"], source["revision"], source["state"]), ("unknown", None, "unknown"))

    def test_zip_distribution_manifest_is_provenance_and_never_verification(self):
        dist = self.base / "dist"
        plugin = make_plugin(dist / PLUGIN_RELATIVE)
        commit = "a" * 40
        write(dist / "RELEASE-MANIFEST.json", json.dumps({
            "source_commit": commit, "plugin_version": "0.5.0+built", "contract_digest": "c" * 64,
            "plugin_code_digest": "0" * 64}))
        identity = plugin_identity.collect(plugin)
        source = identity["source"]
        self.assertEqual((source["kind"], source["revision"], source["state"]), ("release_manifest", commit, "unknown"))
        self.assertIs(source["provenance_only"], True)
        self.assertEqual(source["verification"], "not_performed")
        self.assertEqual(source["manifest_plugin_code_digest"], "0" * 64)
        # The live digest is measured from the files; the manifest value does not stand in for it.
        self.assertNotEqual(identity["code_digest"]["value"], "0" * 64)
        self.assertEqual(identity["plugin_version"], "0.5.0+test")

    def test_release_manifest_with_bad_content_yields_no_revision(self):
        dist = self.base / "dist"
        plugin = make_plugin(dist / PLUGIN_RELATIVE)
        for text in ("{broken", json.dumps([1]), json.dumps({"source_commit": "not-a-sha"})):
            write(dist / "RELEASE-MANIFEST.json", text)
            source = plugin_identity.collect(plugin)["source"]
            self.assertEqual(source["kind"], "release_manifest")
            self.assertIsNone(source["revision"])
            self.assertEqual(source["state"], "unknown")

    def test_no_git_and_no_manifest_is_unknown_not_invented(self):
        plugin = make_plugin(self.base / "loose" / "codex-claude-orchestrator")
        identity = plugin_identity.collect(plugin)
        self.assertEqual((identity["source"]["kind"], identity["source"]["revision"], identity["source"]["state"]),
                         ("unknown", None, "unknown"))
        self.assertIsNotNone(identity["code_digest"]["value"])

    def test_missing_or_broken_plugin_manifest_is_reported_not_guessed(self):
        plugin = self.base / "bare"
        write(plugin / "scripts/a.py", "print(1)\n")
        identity = plugin_identity.collect(plugin)
        self.assertIsNone(identity["plugin_version"])
        self.assertIn("plugin_version_error", identity)

    def test_code_digest_covers_path_and_bytes_and_ignores_noise(self):
        plugin = make_plugin(self.base / "p")
        base = plugin_identity.code_digest(plugin)["value"]
        for noise in ("scripts/__pycache__/a.cpython-311.pyc", ".DS_Store", ".venv/lib/mod.py", "dist/out.py",
                      ".pytest_cache/x.json", "notes.txt"):
            write(plugin / noise, "noise\n")
        self.assertEqual(plugin_identity.code_digest(plugin)["value"], base)
        (plugin / "scripts/a.py").rename(plugin / "scripts/b.py")
        renamed = plugin_identity.code_digest(plugin)["value"]
        self.assertNotEqual(renamed, base)
        (plugin / "scripts/b.py").write_text("print(3)\n")
        self.assertNotEqual(plugin_identity.code_digest(plugin)["value"], renamed)

    def test_runtime_digest_matches_the_builders_release_manifest_algorithm(self):
        if not BUILDER.is_file():
            self.skipTest("tools/build_distribution.py is not part of this installation")
        spec = importlib.util.spec_from_file_location("build_distribution_for_identity", BUILDER)
        builder = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(builder)
        plugin = make_plugin(self.base / "p")
        write(plugin / "assets/dashboard.html", "<html></html>\n")
        write(plugin / ".gitignore", "dist/\n")
        blobs = {Path(PLUGIN_RELATIVE) / path.relative_to(plugin): path.read_bytes()
                 for path in plugin.rglob("*") if path.is_file()}
        blobs[Path("README.md")] = b"# outside the plugin\n"
        self.assertEqual(builder.plugin_code_digest(blobs), plugin_identity.code_digest(plugin)["value"])
        self.assertEqual(builder.OMIT_DIR_NAMES, set(plugin_identity.OMIT_DIR_NAMES))
        self.assertEqual(builder.ALLOWED_SUFFIXES, set(plugin_identity.ALLOWED_SUFFIXES))
        self.assertEqual(builder.ALLOWED_BARE_NAMES, set(plugin_identity.ALLOWED_BARE_NAMES))

    def test_fifo_does_not_block_diagnostic_hashing(self):
        plugin = make_plugin(self.base / "p")
        os.mkfifo(plugin / "scripts/block.py")
        probe = subprocess.run([sys.executable, "-c", "import sys,json; from pathlib import Path; sys.path.insert(0,sys.argv[1]); import plugin_identity; print(json.dumps(plugin_identity.collect(Path(sys.argv[2]))))", str(plugin_identity.PLUGIN_ROOT / "scripts"), str(plugin)], capture_output=True, text=True, timeout=3, check=True)
        identity = json.loads(probe.stdout)
        self.assertIsNone(identity["code_digest"]["value"])
        self.assertIn("error", identity["code_digest"])

    def test_freeze_persists_once_and_a_later_change_does_not_rewrite_it(self):
        repo, plugin, head = make_repo(self.base)
        run = self.base / "run"
        run.mkdir()
        first = plugin_identity.freeze(run, "contract-1", plugin)
        self.assertEqual(first["source"]["revision"], head)
        self.assertEqual(json.loads((run / plugin_identity.FILE_NAME).read_text()), first)
        (plugin / "scripts/a.py").write_text("print('later')\n")
        second = plugin_identity.freeze(run, "contract-2", plugin)
        self.assertEqual(second, first)
        self.assertEqual(plugin_identity.receipt_fields(run), {"plugin_identity": first})
        self.assertEqual(list(run.glob(".*.tmp")), [])

    def test_legacy_corrupt_or_foreign_files_are_readable_as_unavailable(self):
        run = self.base / "legacy"
        run.mkdir()
        self.assertIsNone(plugin_identity.load_frozen(run))
        self.assertEqual(plugin_identity.receipt_fields(run)["plugin_identity"]["status"], "unavailable")
        for text in ("{broken", json.dumps([1]), json.dumps({"schema_version": 99})):
            (run / plugin_identity.FILE_NAME).write_text(text)
            self.assertIsNone(plugin_identity.load_frozen(run))
            self.assertEqual(plugin_identity.receipt_fields(run)["plugin_identity"]["status"], "unavailable")

    def test_freeze_failure_never_raises_into_the_run(self):
        identity = plugin_identity.freeze(self.base / "missing-run-dir", "contract-1", make_plugin(self.base / "p"))
        self.assertIn("freeze_error", identity)
        self.assertEqual(identity["bridge_contract_id"], "contract-1")

    def test_collect_failure_is_recorded_without_stopping_freeze(self):
        run = self.base / "run"
        run.mkdir()
        original = plugin_identity.collect

        def boom(root=None, contract_id=None):
            raise RuntimeError("collector broke")

        plugin_identity.collect = boom
        try:
            identity = plugin_identity.freeze(run, "contract-1")
        finally:
            plugin_identity.collect = original
        self.assertIn("collector broke", identity["collect_error"])
        self.assertEqual(identity["source"]["state"], "unknown")
        self.assertEqual(plugin_identity.load_frozen(run)["collect_error"], identity["collect_error"])


if __name__ == "__main__":
    unittest.main()
