"""Filesystem failures preserve old records and do not block on special files."""
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import shared_io


class SharedIOTests(unittest.TestCase):
    def test_failed_replace_preserves_record_and_cleans_temporary_file(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "record.json"
            shared_io.dump_json(target, {"old": "原始"})
            with patch.object(shared_io.os, "replace", side_effect=OSError("injected")):
                with self.assertRaises(OSError):
                    shared_io.dump_json(target, {"new": "候选"})
            self.assertEqual(shared_io.load_json(target), {"old": "原始"})
            self.assertEqual(list(Path(folder).iterdir()), [target])

    def test_bounded_reads_reject_fifo_symlink_and_oversized_file(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "regular").write_bytes(b"abcd")
            (root / "symlink").symlink_to(root / "regular")
            os.mkfifo(root / "fifo")
            for name, limit in (("symlink", 4), ("fifo", 4), ("regular", 3)):
                with self.subTest(name=name), self.assertRaises(ValueError):
                    shared_io.read_regular(root / name, limit)
            self.assertEqual(shared_io.read_regular(root / "regular", 4), b"abcd")


if __name__ == "__main__":
    unittest.main()
