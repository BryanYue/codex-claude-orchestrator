"""Bundled-only content, preserved historical pins, bounded reads and integrity checks."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import content_store
from content_store import ContentError, ContentStore

REFERENCES = ROOT / "skills/codex-claude-orchestrator/references"
PROTOCOL_SHA256 = "63479da00c8cf9a2467d0efe860fe327a364fb51b7592eece18266319cd6a17b"
CAPABILITIES = ["read", "task_pin"]

def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def write_source(directory: Path, files: dict, version: str = "1.0", manifest_extra: dict | None = None,
                 requires: dict | None = None, declared: dict | None = None) -> str:
    directory.mkdir(parents=True, exist_ok=True)
    for name, text in files.items():
        (directory / name).write_bytes(text if isinstance(text, bytes) else text.encode("utf-8"))
    entries = declared if declared is not None else {
        name: sha(text if isinstance(text, bytes) else text.encode("utf-8")) for name, text in files.items()}
    manifest = {"schema": content_store.SCHEMA, "content_version": version, "entry": "guide.md",
                "protocol_reference": "protocol.md",
                "requires": requires or {"content_api": 1, "capabilities": CAPABILITIES},
                "files": [{"path": name, "sha256": digest} for name, digest in entries.items()], **(manifest_extra or {})}
    data = json.dumps(manifest).encode("utf-8")
    (directory / "manifest.json").write_bytes(data)
    return sha(data)


def historical_snapshot(store: ContentStore, source: Path, digest: str, decision="approved") -> dict:
    """Create a pre-retirement fixture on disk; never invoke an activation API."""
    store.snapshots.mkdir(parents=True, exist_ok=True)
    store.reviews.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, store.snapshots / digest)
    record = {"schema_version": 1, "digest": digest, "decision": decision, "recorded_at": 1.0,
              "content_version": json.loads((source / "manifest.json").read_text())["content_version"],
              "source": {"kind": "github", "commit": "a" * 40}, "reason": "historical fixture"}
    (store.reviews / f"{digest}.json").write_text(json.dumps(record))
    return store._binding(digest, "active") if decision == "approved" else {}


class ContentStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.bundled = self.root / "bundled"
        self.bundled_digest = write_source(self.bundled, {"guide.md": "# bundled guide\n", "protocol.md": "protocol v1\n"}, version="0.1")
        self.store = ContentStore(self.root / "state" / "content", bundled_dir=self.bundled)

    def tearDown(self):
        self.tmp.cleanup()

    def source(self, name, guide="# guide v2\n", version="2.0", **kwargs):
        directory = self.root / name
        digest = write_source(directory, {"guide.md": guide, "protocol.md": "protocol v1\n"}, version=version, **kwargs)
        return directory, digest

    def history(self):
        source, digest = self.source("history")
        return digest, historical_snapshot(self.store, source, digest)

    def test_new_tasks_ignore_old_active_and_corrupt_activation_state_without_modifying_it(self):
        digest, old_binding = self.history()
        state = self.store.root / "state.json"
        for raw in (json.dumps({"schema_version": 1, "active": digest, "disabled": False, "history": []}).encode(), b"invalid state"):
            state.write_bytes(raw)
            self.assertEqual(self.store.read()["digest"], self.bundled_digest)
            self.assertEqual(self.store.pin()["mode"], "bundled")
            self.assertEqual(self.store.pin()["digest"], self.bundled_digest)
            self.assertEqual(self.store.status()["effective"]["digest"], self.bundled_digest)
            self.assertEqual(state.read_bytes(), raw)
            self.assertEqual(self.store.read(digest=digest)["text"], "# guide v2\n")
            content_store.verify_binding(old_binding)
        self.assertFalse((self.store.root / "staging").exists())

    def test_retired_methods_do_not_read_fetch_stage_or_write_any_state(self):
        for method, args in ((self.store.check, ()), (self.store.review, ("0" * 64, "approve", "x", ["y"])),
                             (self.store.switch, ("rollback", "x"))):
            with self.subTest(method=method.__name__), self.assertRaisesRegex(ContentError, "retired"):
                method(*args)
            self.assertFalse(self.store.root.exists())
        self.assertFalse(hasattr(content_store, "http_get"))
        self.assertFalse(hasattr(content_store, "fetch_github"))

    def test_stale_digest_requires_reading_new_bundled_content(self):
        self.store.pin()
        write_source(self.bundled, {"guide.md": "# new bundled\n", "protocol.md": "protocol v1\n"}, version="0.2")
        with self.assertRaisesRegex(ContentError, "changed since it was read"):
            self.store.pin(self.bundled_digest, require_read=True)
        self.assertEqual(self.store.pin(self.store.read()["digest"])["content_version"], "0.2")

    def test_bundled_guide_references_readable_by_digest_before_dispatch(self):
        self.assertEqual(self.store.read(path="protocol.md", digest=self.bundled_digest)["text"], "protocol v1\n")
        self.assertFalse(self.store.root.exists())

    def test_historical_capabilities_are_rechecked_for_resume_but_evidence_stays_readable(self):
        capability = "future_fixture_interface"
        source, digest = self.source("future", requires={"content_api": 1, "capabilities": CAPABILITIES + [capability]})
        with mock.patch.object(content_store, "CAPABILITIES", content_store.CAPABILITIES | {capability}):
            binding = historical_snapshot(self.store, source, digest)
        with self.assertRaises(ContentError):
            content_store.verify_binding(binding)
        self.assertEqual(self.store.read(digest=digest)["text"], "# guide v2\n")
        self.assertEqual(self.store.pin()["digest"], self.bundled_digest)

    def test_historical_update_requirement_labels_remain_compatible_without_update_actions(self):
        source, digest = self.source("old", requires={"content_api": 1, "capabilities": ["check", "read", "review", "disable", "rollback", "task_pin"]})
        binding = historical_snapshot(self.store, source, digest)
        self.assertEqual(content_store.verify_binding(binding)["digest"], digest)

    def test_non_regular_source_file_does_not_wait_for_writer(self):
        (self.bundled / "guide.md").unlink()
        os.mkfifo(self.bundled / "guide.md")
        with self.assertRaisesRegex(ContentError, "not a regular file"):
            self.store.read()

    def test_manifest_cannot_approve_itself_or_disable_checks(self):
        for extra in ({"approved": True}, {"skip_review": True}, {"review": {"decision": "approved"}}):
            directory, _ = self.source("self-" + next(iter(extra)), manifest_extra=extra)
            with self.subTest(extra=extra), self.assertRaisesRegex(ContentError, "manifest fields must be exactly"):
                content_store.read_directory(directory)

    def test_source_paths_hashes_symlinks_and_text_are_verified(self):
        cases = {
            "traversal": {"declared": {"../guide.md": "0" * 64, "protocol.md": sha(b"protocol v1\n")}},
            "subdir": {"declared": {"sub/guide.md": "0" * 64, "protocol.md": sha(b"protocol v1\n")}},
            "script": {"declared": {"guide.md": sha(b"# guide v2\n"), "protocol.md": sha(b"protocol v1\n"), "run.sh": "0" * 64}},
            "hash": {"declared": {"guide.md": "1" * 64, "protocol.md": sha(b"protocol v1\n")}},
        }
        for name, kwargs in cases.items():
            directory, _ = self.source(name, **kwargs)
            with self.subTest(name=name), self.assertRaises(ContentError):
                content_store.verify_material(*content_store.read_directory(directory))
        for name, text in (("binary", b"\xff\xfe"), ("nul", b"a\x00b"), ("large", b"x" * (content_store.MAX_FILE_BYTES + 1))):
            directory, _ = self.source(name, guide=text)
            with self.subTest(name=name), self.assertRaises(ContentError):
                content_store.verify_material(*content_store.read_directory(directory))
        directory, _ = self.source("linked")
        outside = self.root / "outside.md"; outside.write_text("# guide v2\n")
        (directory / "guide.md").unlink(); (directory / "guide.md").symlink_to(outside)
        with self.assertRaises(ContentError):
            content_store.read_directory(directory)
        link = self.root / "linked-dir"; link.symlink_to(self.bundled, target_is_directory=True)
        with self.assertRaises(ContentError):
            content_store.read_directory(link)

    def test_manifest_limits_duplicate_case_and_invalid_entry_fields(self):
        manifest = json.loads((self.bundled / "manifest.json").read_text())
        for changes in ({"entry": []}, {"protocol_reference": {}}, {"requires": {"content_api": True, "capabilities": []}},
                        {"files": manifest["files"] + [{"path": "GUIDE.md", "sha256": "0" * 64}]},
                        {"files": manifest["files"] * (content_store.MAX_FILES + 1)}):
            with self.subTest(changes=changes), self.assertRaises(ContentError):
                content_store.parse_manifest(json.dumps({**manifest, **changes}).encode())
        with self.assertRaises(ContentError):
            content_store.parse_manifest(b" " * (content_store.MAX_MANIFEST_BYTES + 1))
        data, files = content_store.read_directory(self.bundled)
        with mock.patch.object(content_store, "MAX_TOTAL_BYTES", len(data)), self.assertRaises(ContentError):
            content_store.verify_material(data, files)

    def test_bundled_content_requires_supported_executable_capabilities(self):
        for requires in ({"content_api": 2, "capabilities": CAPABILITIES}, {"content_api": 1, "capabilities": ["exec"]}):
            write_source(self.bundled, {"guide.md": "# guide\n", "protocol.md": "p\n"}, requires=requires)
            with self.subTest(requires=requires), self.assertRaises(ContentError):
                self.store.pin()
            self.assertFalse(self.store.status()["effective"]["verified"])

    def test_historical_snapshot_tampering_does_not_block_new_shipped_tasks_but_refuses_resume(self):
        digest, binding = self.history()
        (self.store.snapshots / digest / "guide.md").write_text("# changed\n")
        with self.assertRaises(ContentError):
            self.store.read(digest=digest)
        with self.assertRaises(ContentError):
            content_store.verify_binding(binding)
        self.assertEqual(self.store.pin()["digest"], self.bundled_digest)

    def test_immutable_shipped_snapshot_tampering_is_not_overwritten(self):
        binding = self.store.pin()
        snapshot = self.store.snapshots / binding["digest"] / "guide.md"
        snapshot.write_text("# changed\n")
        with self.assertRaises(ContentError):
            self.store.pin()
        with self.assertRaises(ContentError):
            content_store.verify_binding(binding)
        self.assertEqual(snapshot.read_text(), "# changed\n")

    def test_historical_review_and_snapshot_aliases_and_oversize_records_are_refused(self):
        digest, binding = self.history()
        review = self.store.reviews / f"{digest}.json"
        original = review.read_bytes()
        outside = self.root / "outside-review.json"; outside.write_bytes(original)
        review.unlink(); review.symlink_to(outside)
        with self.assertRaises(ContentError):
            content_store.verify_binding(binding)
        review.unlink(); review.write_bytes(b"x" * (content_store.MAX_REVIEW_BYTES + 1))
        with self.assertRaises(ContentError):
            self.store.read(digest=digest)
        review.write_bytes(original)
        snapshot = self.store.snapshots / digest
        actual = self.root / "actual-snapshot"; snapshot.rename(actual)
        snapshot.symlink_to(actual, target_is_directory=True)
        with self.assertRaises(ContentError):
            content_store.verify_binding(binding)
        self.assertEqual(self.store.pin()["digest"], self.bundled_digest)

    def test_review_record_change_invalidates_pinned_binding(self):
        digest, binding = self.history()
        record = self.store.reviews / f"{digest}.json"
        value = json.loads(record.read_text()); value["reason"] = "edited later"
        record.write_text(json.dumps(value))
        with self.assertRaises(ContentError):
            content_store.verify_binding(binding)
        with self.assertRaises(ContentError):
            content_store.verify_binding({**binding, "review": {"record_sha256": "0" * 64}})

    def test_declared_reads_and_snapshot_entries_are_bounded(self):
        with self.assertRaises(ContentError):
            self.store.read(path="../secret")
        with self.assertRaises(ContentError):
            self.store.read(digest="0" * 64)
        digest, binding = self.history()
        (self.store.snapshots / digest / "extra.txt").write_text("undeclared")
        with self.assertRaises(ContentError):
            content_store.verify_binding(binding)
        with self.assertRaises(ContentError):
            self.store.read(digest=digest)

    def test_rejected_snapshot_is_not_trusted(self):
        source, digest = self.source("rejected")
        historical_snapshot(self.store, source, digest, decision="rejected")
        with self.assertRaises(ContentError):
            self.store.read(digest=digest)

    def test_concurrent_pins_create_one_immutable_snapshot_and_do_not_create_activation_state(self):
        errors = []
        def pin():
            try:
                content_store.verify_binding(self.store.pin())
            except Exception as exc:
                errors.append(exc)
        threads = [threading.Thread(target=pin) for _ in range(6)]
        for thread in threads: thread.start()
        for thread in threads: thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(list(self.store.snapshots.iterdir())), 1)
        self.assertEqual(len(list(self.store.reviews.iterdir())), 1)
        self.assertFalse((self.store.root / "state.json").exists())


class BundledContentTests(unittest.TestCase):
    def test_protocol_reference_bytes_are_unchanged(self):
        self.assertEqual(sha((REFERENCES / "orchestration-protocol.md").read_bytes()), PROTOCOL_SHA256)

    def test_bundled_manifest_matches_the_distributed_references(self):
        manifest_bytes, files = content_store.read_directory(REFERENCES)
        manifest = content_store.verify_material(manifest_bytes, files)
        self.assertIsNone(content_store.compatibility_issue(manifest))
        self.assertEqual(manifest["entry"], "guide.md")
        self.assertEqual(manifest["protocol_reference"], "orchestration-protocol.md")
        self.assertEqual(manifest["files"]["orchestration-protocol.md"], PROTOCOL_SHA256)
        self.assertEqual(set(manifest["files"]), {p.name for p in REFERENCES.glob("*.md")},
                         "every distributed reference is declared, and nothing undeclared is fetched")
        guide = files["guide.md"].decode("utf-8")
        for target in re.findall(r"\]\(([^)#]+\.md)\)", guide):
            with self.subTest(link=target):
                self.assertIn(target, manifest["files"], "guide links resolve inside the same content version")

    def test_skill_uses_shipped_guide_and_does_not_instruct_online_updates(self):
        skill = (ROOT / "skills/codex-claude-orchestrator/SKILL.md").read_text(encoding="utf-8")
        self.assertTrue(skill.startswith("---\nname: codex-claude-orchestrator\ndescription: "))
        self.assertIn("[references/guide.md](references/guide.md)", skill)
        self.assertTrue((ROOT / "skills/codex-claude-orchestrator/references/guide.md").is_file())
        self.assertIn("随插件版本发布", skill)
        for call in ("claude_content_check(", "claude_content_review(", "claude_content_switch("):
            self.assertNotIn(call, skill)

    def test_bundled_store_serves_the_guide_without_network(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ContentStore(Path(tmp) / "content")
            read = store.read()
            self.assertEqual(read["trust"], "bundled")
            self.assertEqual(read["path"], "guide.md")
            self.assertEqual(read["text"], (REFERENCES / "guide.md").read_text(encoding="utf-8"))
            pin = store.pin()
            self.assertEqual(pin["mode"], "bundled")
            content_store.verify_binding(pin)
            self.assertFalse((REFERENCES / "staging").exists(), "the Skill directory is never written")

    def test_command_line_verifier_accepts_the_bundled_source(self):
        completed = subprocess.run([sys.executable, str(ROOT / "scripts/content_store.py"), "verify"],
                                   capture_output=True, text=True, timeout=30)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout)["digest"], sha((REFERENCES / "manifest.json").read_bytes()))


if __name__ == "__main__":
    unittest.main(verbosity=2)
