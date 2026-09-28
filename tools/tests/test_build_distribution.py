"""Failure paths must reject before writing output; success must be reproducible."""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import build_distribution as bd  # noqa: E402

VERSION = "0.5.0"

CONTRACT_SOURCES = {
    "skills/codex-claude-orchestrator/scripts/bridge.py": "# bridge\n",
    "skills/codex-claude-orchestrator/scripts/runtime.py": "# runtime\n",
    "skills/codex-claude-orchestrator/scripts/workspace.py": "# workspace\n",
    "skills/codex-claude-orchestrator/scripts/events.py": "# events\n",
    "skills/codex-claude-orchestrator/scripts/named_workflow.py": "# named_workflow\n",
    "skills/codex-claude-orchestrator/scripts/compatibility.py": "# compatibility\n",
    "scripts/cli_validation.py": "# cli_validation\n",
    "scripts/cli_store.py": "# cli_store\n",
}


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)


def _write(path: Path, content: str, *, executable: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    if executable:
        path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


class RepositoryFixture:
    """A minimal, git-committed stand-in for the real plugin repository."""

    def __init__(self, base: Path, *, version: str = VERSION):
        self.root = base
        self.version = version
        self.root.mkdir(parents=True, exist_ok=True)
        _git(self.root, "init", "-q")
        _write(self.root / ".gitignore", "dist/\n.venv/\n")
        _write(self.root / "README.md", f"# Codex Claude Orchestrator {version}\n")
        _write(self.root / "RELEASE-VERIFICATION.md", "pending\n")
        _write(self.root / ".agents/plugins/marketplace.json",
              json.dumps({"name": "codex-claude-team", "plugins": []}))
        _write(self.root / "Install.command", "#!/bin/bash\necho install\n", executable=True)
        _write(self.root / "plugins/codex-claude-orchestrator/scripts/launch.sh",
              "#!/bin/bash\necho launch\n", executable=True)
        _write(self.root / "plugins/codex-claude-orchestrator/scripts/server.py",
              f'mcp = MCPServer("claude-orchestrator", version="{version}")\n')
        _write(self.root / "plugins/codex-claude-orchestrator/.codex-plugin/plugin.json",
              json.dumps({"name": "codex-claude-orchestrator", "version": version}))
        _write(self.root / "plugins/codex-claude-orchestrator/pyproject.toml",
              f'[project]\nname = "codex-claude-orchestrator"\nversion = "{version}"\n'
              'requires-python = ">=3.11"\ndependencies = ["mcp==2.2.0"]\n\n[tool.uv]\npackage = false\n')
        _write(self.root / "plugins/codex-claude-orchestrator/uv.lock",
              'version = 1\nrequires-python = ">=3.11"\n\n'
              '[[package]]\nname = "codex-claude-orchestrator"\n'
              f'version = "{version}"\nsource = {{ virtual = "." }}\n')
        for relative, content in CONTRACT_SOURCES.items():
            _write(self.root / "plugins/codex-claude-orchestrator" / relative, content)
        _write(self.root / "docs/git-marketplace.md", "# Git marketplace\n")
        _write(self.root / "docs/install-and-recovery.md", "# Install and recovery\n")

    def commit(self, message: str = "initial") -> str:
        _git(self.root, "add", "-A")
        _git(self.root, "-c", "user.email=test@example.com", "-c", "user.name=test",
             "-c", "commit.gpgsign=false", "commit", "-q", "-m", message)
        return _git(self.root, "rev-parse", "HEAD").stdout.strip()


class BuildDistributionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_successful_build_is_reproducible_and_carries_git_evidence(self):
        repo = RepositoryFixture(self.base / "repo")
        commit = repo.commit()
        # Reproducibility only holds for a matching output name: the ZIP embeds
        # a single top-level folder named after `output.name`, by design (it is
        # what a user extracts and runs Install.command inside).
        name = f"codex-claude-orchestrator-macos-{VERSION}"

        first = bd.build(source_root=repo.root, output=self.base / "run1" / name)
        second = bd.build(source_root=repo.root, output=self.base / "run2" / name)

        self.assertEqual(first["sha256"], second["sha256"])
        self.assertEqual(first["version"], VERSION)
        self.assertEqual(first["source_commit"], commit)
        self.assertEqual(first["contract_digest"], second["contract_digest"])

        for relative in bd.EXECUTABLE_RELATIVE_PATHS:
            self.assertEqual((Path(first["directory"]) / relative).stat().st_mode & 0o777, 0o755)
        self.assertEqual((Path(first["archive"] + ".sha256")).read_text(),
                         f"{first['sha256']}  {Path(first['archive']).name}\n")

        manifest = json.loads((Path(first["directory"]) / "FILE-SHA256.json").read_text())
        self.assertNotIn("FILE-SHA256.json", manifest)
        self.assertIn("RELEASE-MANIFEST.json", manifest)
        release_manifest = json.loads((Path(first["directory"]) / "RELEASE-MANIFEST.json").read_text())
        self.assertEqual(release_manifest["source_commit"], commit)
        self.assertEqual(release_manifest["base_version"], VERSION)

        with zipfile.ZipFile(first["archive"]) as archive:
            names = archive.namelist()
            self.assertEqual(names, sorted(names))
            launcher_info = archive.getinfo(
                f"{Path(first['directory']).name}/plugins/codex-claude-orchestrator/scripts/launch.sh")
            installer_info = archive.getinfo(f"{Path(first['directory']).name}/Install.command")
            readme_info = archive.getinfo(f"{Path(first['directory']).name}/README.md")
            self.assertEqual((launcher_info.external_attr >> 16) & 0o777, 0o755)
            self.assertEqual((installer_info.external_attr >> 16) & 0o777, 0o755)
            self.assertEqual((readme_info.external_attr >> 16) & 0o777, 0o644)
            self.assertEqual(launcher_info.date_time, bd.ZIP_EPOCH)

    def test_refuses_to_overwrite_existing_output_without_touching_it(self):
        repo = RepositoryFixture(self.base / "repo")
        repo.commit()
        output = self.base / "out"
        bd.build(source_root=repo.root, output=output)
        marker = output / "untouched.marker"
        marker.write_text("keep")

        with self.assertRaises(bd.BuildError):
            bd.build(source_root=repo.root, output=output)
        self.assertEqual(marker.read_text(), "keep")

    def test_refuses_dirty_working_tree(self):
        repo = RepositoryFixture(self.base / "repo")
        repo.commit()
        (repo.root / "README.md").write_text("# changed after commit\n")

        with self.assertRaises(bd.BuildError) as failure:
            bd.build(source_root=repo.root, output=self.base / "out")
        self.assertIn("uncommitted", str(failure.exception))
        self.assertFalse((self.base / "out").exists())

    def test_refuses_untracked_file_in_working_tree(self):
        repo = RepositoryFixture(self.base / "repo")
        repo.commit()
        (repo.root / "docs/extra.md").write_text("untracked\n")

        with self.assertRaises(bd.BuildError):
            bd.build(source_root=repo.root, output=self.base / "out")

    def test_refuses_non_git_source(self):
        plain = self.base / "plain"
        plain.mkdir()

        with self.assertRaises(bd.BuildError):
            bd.build(source_root=plain, output=self.base / "out")

    def test_refuses_launcher_tracked_without_executable_git_mode(self):
        repo = RepositoryFixture(self.base / "repo")
        launcher = repo.root / "plugins/codex-claude-orchestrator/scripts/launch.sh"
        launcher.chmod(0o644)
        repo.commit()

        with self.assertRaises(bd.BuildError) as failure:
            bd.build(source_root=repo.root, output=self.base / "out")
        self.assertIn("100755", str(failure.exception))
        self.assertFalse((self.base / "out").exists())

    def test_refuses_manifest_pyproject_version_mismatch(self):
        repo = RepositoryFixture(self.base / "repo")
        pyproject = repo.root / "plugins/codex-claude-orchestrator/pyproject.toml"
        pyproject.write_text(pyproject.read_text().replace(VERSION, "0.4.7"))
        repo.commit()

        with self.assertRaises(bd.BuildError) as failure:
            bd.build(source_root=repo.root, output=self.base / "out")
        self.assertIn("pyproject.toml", str(failure.exception))

    def test_refuses_manifest_uv_lock_version_mismatch(self):
        repo = RepositoryFixture(self.base / "repo")
        uv_lock = repo.root / "plugins/codex-claude-orchestrator/uv.lock"
        uv_lock.write_text(uv_lock.read_text().replace(VERSION, "0.4.7"))
        repo.commit()

        with self.assertRaises(bd.BuildError) as failure:
            bd.build(source_root=repo.root, output=self.base / "out")
        self.assertIn("uv.lock", str(failure.exception))

    def test_refuses_missing_marketplace_identity(self):
        repo = RepositoryFixture(self.base / "repo")
        (repo.root / ".agents/plugins/marketplace.json").write_text(json.dumps({"name": "someone-else"}))
        repo.commit()

        with self.assertRaises(bd.BuildError) as failure:
            bd.build(source_root=repo.root, output=self.base / "out")
        self.assertIn("Marketplace identity", str(failure.exception))

    def test_tracked_tree_rejects_symlink(self):
        repo = RepositoryFixture(self.base / "repo")
        (repo.root / "link.md").symlink_to("README.md")
        repo.commit()
        with self.assertRaisesRegex(bd.BuildError, "Symlink"):
            bd.build(source_root=repo.root, output=self.base / "out")

    def test_tracked_tree_rejects_disallowed_extension(self):
        repo = RepositoryFixture(self.base / "repo")
        _write(repo.root / "payload.exe", "MZ")
        repo.commit()
        with self.assertRaisesRegex(bd.BuildError, "Unrecognized artifact"):
            bd.build(source_root=repo.root, output=self.base / "out")

    def test_contract_digest_matches_the_live_compatibility_algorithm(self):
        # Guards against tools/build_distribution.py's duplicated file list and
        # hashing algorithm drifting from skills/.../compatibility.py, without
        # this build tool importing (and thus depending on) that runtime module.
        real_repo_root = Path(__file__).resolve().parents[2]
        skills_scripts = real_repo_root / "plugins/codex-claude-orchestrator/skills/codex-claude-orchestrator/scripts"
        sys.path.insert(0, str(skills_scripts))
        import compatibility  # noqa: E402

        commit = bd.run_git(real_repo_root, "rev-parse", "HEAD").strip()
        blobs, _ = bd.tracked_blobs(real_repo_root, commit)
        self.assertEqual(bd.contract_digest(blobs), compatibility.bridge_contract_id())

    def test_ignored_files_never_enter_distribution(self):
        repo = RepositoryFixture(self.base / "repo")
        repo.commit()
        (repo.root / ".gitignore").write_text("dist/\n.venv/\nnotes.md\n")
        repo.commit("ignore notes")
        _write(repo.root / "notes.md", "secret fixture\n")
        _write(repo.root / ".venv/lib/module.py", "ignored\n")
        result = bd.build(source_root=repo.root, output=self.base / "out")
        manifest = json.loads((Path(result["directory"]) / "FILE-SHA256.json").read_text())
        self.assertNotIn("notes.md", manifest)
        self.assertFalse(any("notes.md" in name for name in zipfile.ZipFile(result["archive"]).namelist()))

    def test_git_environment_cannot_replace_source_commit(self):
        source = RepositoryFixture(self.base / "source")
        source_commit = source.commit("source")
        other = RepositoryFixture(self.base / "other")
        other_commit = other.commit("other")
        self.assertNotEqual(source_commit, other_commit)
        with patch.dict(os.environ, {"GIT_DIR": str(other.root / ".git"),
                                     "GIT_WORK_TREE": str(source.root)}):
            result = bd.build(source_root=source.root, output=self.base / "out")
        self.assertEqual(result["source_commit"], source_commit)

    def test_linked_worktree_git_file_is_not_packaged(self):
        repo = RepositoryFixture(self.base / "repo")
        commit = repo.commit()
        linked = self.base / "linked"
        _git(repo.root, "worktree", "add", "-qb", "linked", str(linked))
        self.assertTrue((linked / ".git").is_file())
        result = bd.build(source_root=linked, output=self.base / "out")
        self.assertEqual(result["source_commit"], commit)
        self.assertFalse((Path(result["directory"]) / ".git").exists())

    def test_existing_zip_or_sidecar_rejected_without_partial_output(self):
        for sibling in ("out.zip", "out.zip.sha256", "out.staging"):
            with self.subTest(sibling=sibling), tempfile.TemporaryDirectory() as temp:
                base = Path(temp)
                repo = RepositoryFixture(base / "repo")
                repo.commit()
                occupied = base / sibling
                if sibling.endswith(".staging"):
                    occupied.mkdir()
                    (occupied / "keep.md").write_text("keep")
                else:
                    occupied.write_text("keep")
                with self.assertRaises(bd.BuildError):
                    bd.build(source_root=repo.root, output=base / "out")
                self.assertFalse((base / "out").exists())
                self.assertEqual((occupied / "keep.md").read_text() if occupied.is_dir()
                                 else occupied.read_text(), "keep")

    def test_dangling_output_symlinks_and_sidecar_target_are_untouched(self):
        for sibling in ("out", "out.zip", "out.zip.sha256", "out.staging"):
            with self.subTest(sibling=sibling), tempfile.TemporaryDirectory() as temp:
                base = Path(temp)
                repo = RepositoryFixture(base / "repo")
                repo.commit()
                occupied = base / sibling
                occupied.symlink_to(base / "absent")
                with self.assertRaises(bd.BuildError):
                    bd.build(source_root=repo.root, output=base / "out")
                self.assertTrue(occupied.is_symlink())
                self.assertFalse((base / "absent").exists())

    def test_snapshot_bytes_keep_zip_and_manifest_consistent_after_source_changes(self):
        repo = RepositoryFixture(self.base / "repo")
        repo.commit()
        original = bd.tracked_blobs

        def mutate_after_snapshot(source_root, commit):
            result = original(source_root, commit)
            (repo.root / "README.md").write_text("modified while building\n")
            return result

        with patch.object(bd, "tracked_blobs", side_effect=mutate_after_snapshot):
            result = bd.build(source_root=repo.root, output=self.base / "out")
        output = Path(result["directory"])
        manifest = json.loads((output / "FILE-SHA256.json").read_text())
        expected = f"# Codex Claude Orchestrator {VERSION}\n".encode()
        self.assertEqual((output / "README.md").read_bytes(), expected)
        self.assertEqual(manifest["README.md"], hashlib.sha256(expected).hexdigest())
        with zipfile.ZipFile(result["archive"]) as archive:
            self.assertEqual(archive.read("out/README.md"), expected)

    def test_output_race_does_not_replace_foreign_directory(self):
        repo = RepositoryFixture(self.base / "repo")
        repo.commit()
        original = bd.build_zip

        def occupy_after_archive(staging, archive_file, package_name):
            original(staging, archive_file, package_name)
            (self.base / "out").mkdir()
            (self.base / "out" / "keep.md").write_text("keep")

        with patch.object(bd, "build_zip", side_effect=occupy_after_archive):
            with self.assertRaisesRegex(bd.BuildError, "appeared during build") as failure:
                bd.build(source_root=repo.root, output=self.base / "out")
        self.assertEqual((self.base / "out" / "keep.md").read_text(), "keep")
        self.assertIn("out.zip", str(failure.exception))
        self.assertTrue((self.base / "out.zip").exists())
        self.assertFalse((self.base / "out.zip.sha256").exists())

    def test_sidecar_race_does_not_remove_foreign_file(self):
        repo = RepositoryFixture(self.base / "repo")
        repo.commit()
        original = bd.build_zip

        def occupy_sidecar(staging, archive_file, package_name):
            original(staging, archive_file, package_name)
            (self.base / "out.zip.sha256").write_text("keep")

        with patch.object(bd, "build_zip", side_effect=occupy_sidecar):
            with self.assertRaises(bd.BuildError):
                bd.build(source_root=repo.root, output=self.base / "out")
        self.assertEqual((self.base / "out.zip.sha256").read_text(), "keep")
        self.assertTrue((self.base / "out").exists())
        self.assertTrue((self.base / "out.zip").exists())

    def test_checksum_failure_preserves_foreign_file_added_to_created_output(self):
        repo = RepositoryFixture(self.base / "repo")
        repo.commit()
        original_open = Path.open
        sidecar = self.base / "out.zip.sha256"
        output = self.base / "out"

        def inject_foreign_file(path, mode="r", *args, **kwargs):
            if path == sidecar and mode == "x":
                (output / "keep.md").write_text("foreign")
                sidecar.write_text("foreign sidecar")
            return original_open(path, mode, *args, **kwargs)

        with patch.object(Path, "open", inject_foreign_file):
            with self.assertRaisesRegex(bd.BuildError, "none were deleted") as failure:
                bd.build(source_root=repo.root, output=output)
        self.assertEqual((output / "keep.md").read_text(), "foreign")
        self.assertEqual(sidecar.read_text(), "foreign sidecar")
        self.assertTrue((self.base / "out.zip").exists())
        self.assertIn("out.zip.sha256", str(failure.exception))

    def test_output_inside_source_is_limited_to_ignored_dist(self):
        repo = RepositoryFixture(self.base / "repo")
        repo.commit()
        invalid = repo.root / "docs" / "out"
        with self.assertRaisesRegex(bd.BuildError, "ignored dist"):
            bd.build(source_root=repo.root, output=invalid)
        self.assertFalse(invalid.exists())

    def test_source_dist_symlink_is_rejected(self):
        repo = RepositoryFixture(self.base / "repo")
        repo.commit()
        (repo.root / ".gitignore").write_text("dist\n.venv/\n")
        repo.commit("ignore dist symlink")
        destination = self.base / "other"
        destination.mkdir()
        (repo.root / "dist").symlink_to(destination, target_is_directory=True)
        with self.assertRaisesRegex(bd.BuildError, "must not be a symlink"):
            bd.build(source_root=repo.root, output=repo.root / "dist" / "out")
        self.assertFalse((destination / "out").exists())

    def test_source_symlink_cannot_make_an_in_tree_output_look_external(self):
        repo = RepositoryFixture(self.base / "repo")
        repo.commit()
        (repo.root / ".gitignore").write_text("dist/\n.venv/\ndocs/link\n")
        repo.commit("ignore local link")
        destination = self.base / "other"
        destination.mkdir()
        (repo.root / "docs/link").symlink_to(destination, target_is_directory=True)
        with self.assertRaisesRegex(bd.BuildError, "ignored dist"):
            bd.build(source_root=repo.root, output=repo.root / "docs/link/out")
        self.assertFalse((destination / "out").exists())

    def test_tracked_release_metadata_name_is_rejected(self):
        repo = RepositoryFixture(self.base / "repo")
        _write(repo.root / "FILE-SHA256.json", "{}\n")
        repo.commit()
        with self.assertRaisesRegex(bd.BuildError, "already tracked"):
            bd.build(source_root=repo.root, output=self.base / "out")

    def test_server_and_readme_versions_must_match_manifest(self):
        for relative in ("plugins/codex-claude-orchestrator/scripts/server.py", "README.md"):
            with self.subTest(relative=relative), tempfile.TemporaryDirectory() as temp:
                base = Path(temp)
                repo = RepositoryFixture(base / "repo")
                path = repo.root / relative
                path.write_text(path.read_text().replace(VERSION, "0.4.7"))
                repo.commit()
                with self.assertRaisesRegex(bd.BuildError, "does not match"):
                    bd.build(source_root=repo.root, output=base / "out")

    def test_cli_main_writes_default_output_and_returns_zero(self):
        repo = RepositoryFixture(self.base / "repo")
        repo.commit()
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = bd.main(["--source-root", str(repo.root)])
        self.assertEqual(code, 0)
        payload = json.loads(output.getvalue())
        expected_directory = repo.root.resolve() / "dist" / f"codex-claude-orchestrator-macos-{VERSION}"
        self.assertEqual(Path(payload["directory"]), expected_directory)
        self.assertTrue(Path(payload["archive"]).is_file())

    def test_cli_main_reports_failure_on_stderr_and_returns_one(self):
        repo = RepositoryFixture(self.base / "repo")
        repo.commit()
        (repo.root / "README.md").write_text("# dirty\n")
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = bd.main(["--source-root", str(repo.root), "--output", str(self.base / "out")])
        self.assertEqual(code, 1)
        self.assertIn("uncommitted", stderr.getvalue())
        self.assertEqual(stdout.getvalue(), "")
        self.assertFalse((self.base / "out").exists())


if __name__ == "__main__":
    unittest.main()
