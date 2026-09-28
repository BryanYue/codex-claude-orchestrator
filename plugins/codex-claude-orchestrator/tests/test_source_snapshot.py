"""The source adapter freezes only explicit authorized text for artifacts review."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/codex-claude-orchestrator/scripts"))
from source_snapshot import prepare


class SourceSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / "code"
        self.root.mkdir()
        self.source = self.root / "sample.py"
        self.source.write_text("# 来源\nanswer = 2 + 3\n", encoding="utf-8")

    def test_explicit_files_preserve_source_identity_and_original_line_numbers(self):
        (self.root / "not-authorized.txt").write_text("must not read or copy")
        before = self.source.read_bytes()
        result = prepare(self.root, self.base / "snapshot", ["sample.py"])
        manifest = json.loads(Path(result["manifest"]).read_text())
        self.assertEqual(manifest["files"][0]["sha256"], hashlib.sha256(before).hexdigest())
        text = (Path(result["cwd"]) / manifest["files"][0]["snapshot"]).read_text()
        self.assertIn("     2 | answer = 2 + 3", text)
        self.assertEqual(self.source.read_bytes(), before)
        self.assertEqual(sorted(p.name for p in Path(result["cwd"]).iterdir()), ["manifest.json", "source-0001.txt"])
        with self.assertRaises(ValueError):
            prepare(self.root, Path(result["cwd"]), ["sample.py"])

    def test_rejects_git_traversal_symlink_and_binary(self):
        (self.root / "link.py").symlink_to(self.source)
        (self.root / "bytes.py").write_bytes(b"a\0b")
        for files in [["../secret.txt"], ["link.py"], ["bytes.py"], ["sample.py", "sample.py"]]:
            with self.subTest(files=files), self.assertRaises(ValueError):
                prepare(self.root, self.base / "rejected", files)
            self.assertFalse((self.base / "rejected").exists())
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        with self.assertRaisesRegex(ValueError, "Git review"):
            prepare(self.root, self.base / "rejected", ["sample.py"])

    def test_ambient_git_overrides_cannot_disguise_an_existing_repository(self):
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        with patch.dict(os.environ, {"GIT_DIR": str(self.base / "missing"),
                                     "GIT_WORK_TREE": str(self.base / "not-a-worktree")}):
            with self.assertRaisesRegex(ValueError, "Git review"):
                prepare(self.root, self.base / "snapshot", ["sample.py"])
        self.assertFalse((self.base / "snapshot").exists())

    def test_invalid_output_cannot_create_directories_in_source(self):
        before = sorted(self.root.iterdir())
        with self.assertRaisesRegex(ValueError, "outside the source"):
            prepare(self.root, self.root / "new-parent/snapshot", ["sample.py"])
        self.assertEqual(sorted(self.root.iterdir()), before)

    def test_publication_failure_preserves_concurrent_files(self):
        destination = self.base.resolve() / "snapshot"
        real_mkdir = Path.mkdir
        def mkdir(path, *args, **kwargs):
            result = real_mkdir(path, *args, **kwargs)
            if path == destination:
                (path / "concurrent.txt").write_text("another writer")
                (path / "source-0001.txt").write_text("claimed concurrently")
            return result
        with patch.object(Path, "mkdir", mkdir), self.assertRaises(FileExistsError):
            prepare(self.root, destination, ["sample.py"])
        self.assertEqual((destination / "concurrent.txt").read_text(), "another writer")
        self.assertEqual((destination / "source-0001.txt").read_text(), "claimed concurrently")
        self.assertFalse((destination / "manifest.json").exists())


if __name__ == "__main__":
    unittest.main()
