"""Retired managed CLI records stay readable, intact and outside execution."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import cli_store


class CliStoreHistoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.store = self.base / "store"
        self.env = {"HOME": str(self.base), "CLAUDE_ORCHESTRATOR_CLI_ROOT": str(self.store)}

    def tearDown(self):
        self.tmp.cleanup()

    def identity(self, version="2.1.278", *, size=True):
        identity_id = cli_store._identity_id_for("darwin", "arm64", version, "a" * 64)
        folder = self.store / "versions" / identity_id
        folder.mkdir(parents=True)
        binary = folder / "claude"
        binary.write_bytes(b"historical bytes")
        binary.chmod(0o700)
        metadata = {"schema_version": 1, "id": identity_id, "version": version, "sha256": "a" * 64,
                    "platform": "darwin", "machine": "arm64", "source": "fixture", "created_at": 1.0}
        if size:
            metadata["size"] = binary.stat().st_size
        (folder / "identity.json").write_text(json.dumps(metadata))
        return identity_id, folder

    def tree(self):
        return {str(path.relative_to(self.store)): path.read_bytes() for path in self.store.rglob("*") if path.is_file()}

    def test_absent_history_and_root_resolution_never_create_storage(self):
        self.assertEqual(cli_store.store_root(self.env), self.store)
        self.assertEqual(cli_store.store_root({"HOME": str(self.base)}), self.base / ".codex/claude-orchestrator/cli")
        self.assertEqual(cli_store.store_root({"HOME": str(self.base), "CLAUDE_ORCHESTRATOR_CLI_ROOT": "~/history"}), self.base / "history")
        result = cli_store.legacy_records(self.env)
        self.assertFalse(result["present"])
        self.assertEqual(result["storage_summary"]["retained_bytes"], 0)
        self.assertFalse(self.store.exists())

    def test_history_preserves_selection_transactions_receipts_and_private_bytes(self):
        identity_id, _ = self.identity()
        for name, value in (("selection.json", {"mode": "managed", "active": identity_id, "generation": 7}),
                            ("explicit-switch-transaction.json", {"phase": "prepared"}),
                            ("update-policy.json", {"mode": "automatic"})):
            (self.store / name).write_text(json.dumps(value))
        receipt = self.store / "versions" / identity_id / "qualifications" / "contract" / "receipt.json"
        receipt.parent.mkdir(parents=True)
        receipt.write_bytes(b"historical receipt\n")
        before = self.tree()
        with patch.object(os, "system", side_effect=AssertionError("history must not execute")):
            result = cli_store.legacy_records(self.env)
        self.assertTrue(result["ignored_for_dispatch"])
        self.assertFalse(result["files_deleted"])
        self.assertEqual(result["selection"]["active"], identity_id)
        self.assertEqual(self.tree(), before)
        self.assertFalse((self.store / "selection.lock").exists())

    def test_sizes_are_recorded_metadata_and_never_hash_or_run_private_binary(self):
        measured, _ = self.identity()
        unrecorded, _ = self.identity("2.1.277", size=False)
        records = cli_store.legacy_records(self.env)
        by_id = {item["id"]: item for item in records["versions"]}
        self.assertEqual(by_id[measured]["size_status"], "recorded_matches_file")
        self.assertEqual(by_id[unrecorded]["size_status"], "not_recorded")
        self.assertEqual(records["storage_summary"]["retained_bytes"], len(b"historical bytes"))
        self.assertEqual(records["storage_summary"]["unmeasured_versions"], 1)

    def test_size_mismatch_stays_unmeasured(self):
        identity_id, folder = self.identity()
        (folder / "claude").write_bytes(b"changed bytes")
        records = cli_store.legacy_records(self.env)
        self.assertEqual(records["versions"][0]["id"], identity_id)
        self.assertEqual(records["versions"][0]["size_status"], "mismatch")
        self.assertIsNone(records["versions"][0]["size"])

    def test_corrupt_metadata_or_selection_reports_error_without_repair(self):
        identity_id, folder = self.identity()
        (folder / "identity.json").write_text('{"schema_version":1,"id":"wrong"}')
        (self.store / "selection.json").write_text("broken json")
        before = self.tree()
        records = cli_store.legacy_records(self.env)
        self.assertIn("selection_error", records)
        self.assertEqual(records["versions"][0]["id"], identity_id)
        self.assertEqual(records["versions"][0]["metadata"], "unreadable")
        self.assertEqual(self.tree(), before)

    def test_symlinked_version_and_metadata_are_never_followed(self):
        _, folder = self.identity()
        outside = self.base / "outside"
        outside.mkdir()
        (self.store / "versions" / "linked").symlink_to(outside, target_is_directory=True)
        metadata = folder / "identity.json"
        content = metadata.read_bytes()
        metadata.unlink()
        target = outside / "identity.json"
        target.write_bytes(content)
        metadata.symlink_to(target)
        records = cli_store.legacy_records(self.env)
        self.assertEqual(len(records["versions"]), 1)
        self.assertEqual(records["versions"][0]["metadata"], "unreadable")
        self.assertEqual(target.read_bytes(), content)

    def test_invalid_metadata_identity_path_is_rejected(self):
        for identity_id in ("../outside", "", "x/y", None):
            with self.subTest(identity_id=identity_id):
                with self.assertRaises(ValueError):
                    cli_store.identity_metadata(identity_id, self.env)


if __name__ == "__main__":
    unittest.main()
