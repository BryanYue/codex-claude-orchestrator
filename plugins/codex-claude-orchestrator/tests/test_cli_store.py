"""Private CLI-store regressions use scripts only after mocking native signing.

The production code rejects these scripts before capture.  Mocking that one OS
boundary lets the tests exercise staging, hashes, receipt binding, CAS, and
selection without pretending that a fixture shell script is a real Mach-O.
"""
from __future__ import annotations

from pathlib import Path
import errno
from email.message import Message
from http.client import HTTPException, IncompleteRead
import json
import shutil
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import cli_store  # noqa: E402


class FakeResponse:
    def __init__(self, status: int, chunks: list[bytes | BaseException], headers: dict[str, str] | None = None):
        self.status = status
        self.headers = headers or {}
        self.chunks = list(chunks)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, _size: int) -> bytes:
        if not self.chunks:
            return b""
        value = self.chunks.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value


class FakeClock:
    def __init__(self, *values: float):
        self.values = list(values)
        self.last = values[-1]

    def __call__(self) -> float:
        if self.values:
            self.last = self.values.pop(0)
        return self.last


class CliStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.store = self.base / "private-cli-store"
        self.env = {"HOME": str(self.base / "home"), "PATH": "/usr/bin:/bin",
                    "CLAUDE_ORCHESTRATOR_CLI_ROOT": str(self.store)}
        (self.base / "home").mkdir()
        self.native = patch.object(cli_store, "_native_check", return_value=None)
        self.native.start()
        self.contract = patch.object(cli_store, "_contract_id", return_value="bridge-contract-test")
        self.contract.start()

    def tearDown(self):
        self.contract.stop()
        self.native.stop()
        self.tmp.cleanup()

    def candidate(self, name: str, version: str) -> Path:
        path = self.base / name
        path.write_text(f"#!/bin/sh\nif [ \"$1\" = \"--version\" ]; then echo '{version} (Claude Code)'; exit 0; fi\nexit 2\n")
        path.chmod(0o755)
        return path

    def matrix(self, *, core: str = "pass", read_only: str = "pass", write: str = "inconclusive",
               resume: str = "inconclusive", workflow: str = "inconclusive") -> dict:
        return {group: {"status": status, "reason": f"{group} {status}", "evidence": ["receipt.json"] if status == "pass" else []}
                for group, status in {"core": core, "read_only": read_only, "write": write,
                                      "resume": resume, "workflow": workflow}.items()}

    def report(self, job: str, descriptor: dict, matrix: dict) -> Path:
        report = self.store / "jobs" / job / "report.json"
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text(json.dumps({"schema_version": 1, "status": "completed", "job_id": job,
                                      "identity_id": descriptor["id"], "identity_sha256": descriptor["sha256"],
                                      "bridge_contract_id": "bridge-contract-test", "matrix": matrix}))
        return report

    def qualify(self, descriptor: dict, job: str | None = None, **statuses: str) -> dict:
        matrix = self.matrix(**statuses)
        job = job or descriptor["id"]
        return cli_store.record_qualification(descriptor["id"], "bridge-contract-test", matrix,
                                              str(self.report(job, descriptor, matrix)), self.env)

    def test_capture_rejects_script_without_mocked_native_boundary(self):
        self.native.stop()
        try:
            with self.assertRaises(ValueError):
                cli_store.capture(str(self.candidate("script", "2.1.278")), self.env)
        finally:
            self.native.start()

    def test_capture_is_private_content_identity_and_rechecks_drift(self):
        source = self.candidate("candidate", "9.9.278")
        descriptor = cli_store.capture(str(source), self.env)
        self.assertEqual(descriptor["version"], "9.9.278")
        self.assertTrue(descriptor["path"].startswith(str(self.store / "versions") + "/"))
        self.assertNotEqual(Path(descriptor["path"]), source)
        self.assertEqual(cli_store.identity(descriptor["id"], self.env)["sha256"], descriptor["sha256"])
        Path(descriptor["path"]).write_text("changed")
        with self.assertRaisesRegex(RuntimeError, "integrity"):
            cli_store.identity(descriptor["id"], self.env)

    def test_concurrent_same_identity_enotempty_revalidates_the_other_capture(self):
        source = self.candidate("candidate", "9.9.278")
        original_replace = cli_store.os.replace

        def concurrent_capture(staging, target):
            staging_path, target_path = Path(staging), Path(target)
            if target_path.parent == self.store / "versions":
                shutil.copytree(staging_path, target_path)
                raise OSError(errno.ENOTEMPTY, "Directory not empty")
            return original_replace(staging, target)

        with patch.object(cli_store.os, "replace", side_effect=concurrent_capture):
            descriptor = cli_store.capture(str(source), self.env)
        self.assertEqual(cli_store.identity(descriptor["id"], self.env)["sha256"], descriptor["sha256"])

    def test_qualification_receipt_is_bound_to_store_job_and_immutable(self):
        descriptor = cli_store.capture(str(self.candidate("candidate", "9.9.278")), self.env)
        outside = self.base / "outside.json"
        outside.write_text("{}")
        with self.assertRaisesRegex(ValueError, "jobs directory"):
            cli_store.record_qualification(descriptor["id"], "bridge-contract-test", self.matrix(),
                                           str(outside), self.env)
        receipt = self.qualify(descriptor)
        self.assertEqual(receipt["source"], "local_qualification")
        self.assertEqual(cli_store.qualification(descriptor["id"], "bridge-contract-test", self.env)["matrix"]["core"]["status"], "pass")
        report = self.report(descriptor["id"], descriptor, self.matrix())
        report.write_text("tampered")
        with self.assertRaisesRegex(RuntimeError, "hash changed"):
            cli_store.qualification(descriptor["id"], "bridge-contract-test", self.env)

    def test_activation_cas_rollback_and_lock_inode_retention(self):
        first = cli_store.capture(str(self.candidate("first", "9.9.278")), self.env)
        second = cli_store.capture(str(self.candidate("second", "9.9.279")), self.env)
        self.qualify(first); self.qualify(second)
        initial = cli_store.get_selection(self.env)
        self.assertEqual(initial["generation"], 0)
        selected_one = cli_store.activate(first["id"], 0, self.env)
        lock = self.store / "selection.lock"
        inode = lock.stat().st_ino
        selected_two = cli_store.activate(second["id"], selected_one["generation"], self.env)
        self.assertEqual(selected_two["active"], second["id"])
        self.assertEqual(selected_two["previous"], first["id"])
        self.assertEqual(lock.stat().st_ino, inode)
        with self.assertRaisesRegex(RuntimeError, "changed before activation"):
            cli_store.activate(first["id"], 0, self.env)
        rolled = cli_store.rollback(selected_two["generation"], self.env)
        self.assertEqual(rolled["active"], first["id"])
        self.assertEqual(rolled["previous"], second["id"])
        self.assertEqual(lock.stat().st_ino, inode)

    def test_explicit_switch_and_policy_are_one_cas_and_failures_do_not_pause_automatic(self):
        first = cli_store.capture(str(self.candidate("first", "2.1.276")), self.env)
        second = cli_store.capture(str(self.candidate("second", "2.1.278")), self.env)
        selected = cli_store.activate(first["id"], 0, self.env)
        lock = self.store / "selection.lock"
        inode = lock.stat().st_ino

        with self.assertRaisesRegex(RuntimeError, "changed before activation"):
            cli_store.activate_explicit(second["id"], selected["generation"] - 1,
                                        environ=self.env)
        self.assertEqual(cli_store.get_selection(self.env)["active"], first["id"])
        self.assertEqual(cli_store.get_update_policy(self.env)["mode"], "automatic")

        switched = cli_store.activate_explicit(second["id"], selected["generation"],
                                                environ=self.env)
        self.assertEqual(switched["active"], second["id"])
        self.assertEqual(switched["update_policy"]["mode"], "manual")
        self.assertEqual(switched["update_policy"]["reason"], "manual_activation")
        self.assertEqual(lock.stat().st_ino, inode)

        rolled = cli_store.rollback_explicit(switched["generation"], environ=self.env)
        self.assertEqual(rolled["active"], first["id"])
        self.assertEqual(rolled["update_policy"]["reason"], "manual_rollback")

    def test_latest_channel_config_is_explicit_and_legacy_policy_writer_takes_ownership(self):
        default = cli_store.get_update_policy(self.env)
        self.assertEqual(default["channel"], "bundled")
        self.assertFalse(default["auto_qualify"])
        latest = cli_store.set_update_policy(
            "automatic", "user_requested_official_latest", self.env,
            channel="latest", auto_qualify=True)
        self.assertEqual(latest["channel"], "latest")
        self.assertTrue(latest["auto_qualify"])
        raw_policy = json.loads((self.store / "update-policy.json").read_text())
        self.assertNotIn("channel", raw_policy)
        self.assertEqual(raw_policy["mode"], "manual")
        self.assertEqual(raw_policy["reason"], "latest_automatic_compatibility_hold")
        # A still-running 0.4.5 service sees manual and stops.  If it later
        # makes a new explicit policy choice, its generation owns the policy
        # and stale latest/paid intent is no longer effective.
        cli_store._atomic_json(self.store / "update-policy.json",
                               {**raw_policy, "generation": raw_policy["generation"] + 1,
                                "mode": "manual", "reason": "legacy_manual"})
        preserved = cli_store.get_update_policy(self.env)
        self.assertEqual(preserved["channel"], "latest")
        self.assertTrue(preserved["auto_qualify"])
        self.assertEqual(preserved["mode"], "manual")

    def test_policy_config_write_failure_restores_both_files(self):
        before = cli_store.set_update_policy(
            "manual", "before", self.env, channel="latest", auto_qualify=True)
        original_atomic = cli_store._atomic_json
        failed = False

        def fail_first_config(path, value):
            nonlocal failed
            if (Path(path).name == "update-config.json" and not failed
                    and value.get("_update_config_transaction_id")):
                failed = True
                raise OSError("config disk failure")
            return original_atomic(path, value)

        with patch.object(cli_store, "_atomic_json", side_effect=fail_first_config), \
             self.assertRaisesRegex(OSError, "config disk failure"):
            cli_store.set_update_policy(
                "automatic", "disable_paid", self.env,
                channel="latest", auto_qualify=False)
        recovered = cli_store.get_update_policy(self.env)
        self.assertEqual(recovered, before)
        self.assertFalse((self.store / "update-config-transaction.json").exists())

    def test_crash_after_policy_write_recovers_requested_config_forward(self):
        before = cli_store.set_update_policy(
            "manual", "before", self.env, channel="latest", auto_qualify=True)
        before_policy = json.loads((self.store / "update-policy.json").read_text())
        before_config = json.loads((self.store / "update-config.json").read_text())
        after_policy = {**before_policy, "generation": before_policy["generation"] + 1,
                        "mode": "manual", "reason": "latest_automatic_compatibility_hold",
                        "updated_at": 2.0}
        after_config = {"schema_version": 1, "channel": "latest", "auto_qualify": False,
                        "policy_generation": after_policy["generation"],
                        "effective_mode": "automatic", "effective_reason": "disable_paid"}
        transaction_id = "a" * 32
        cli_store._atomic_json(self.store / "update-config-transaction.json", {
            "schema_version": 1, "state": "prepared", "transaction_id": transaction_id,
            "before_policy": before_policy, "before_config": before_config,
            "after_policy": after_policy, "after_config": after_config})
        cli_store._atomic_json(self.store / "update-policy.json",
                               {**after_policy, "_update_config_transaction_id": transaction_id})

        recovered = cli_store.get_update_policy(self.env)
        self.assertEqual(recovered["mode"], "automatic")
        self.assertEqual(recovered["reason"], "disable_paid")
        self.assertEqual(recovered["channel"], "latest")
        self.assertFalse(recovered["auto_qualify"])
        self.assertFalse((self.store / "update-config-transaction.json").exists())
        self.assertNotIn("_update_config_transaction_id",
                         json.loads((self.store / "update-policy.json").read_text()))

    def test_crash_recovery_preserves_later_legacy_policy_and_disables_uncertain_paid_enable(self):
        before = cli_store.set_update_policy(
            "automatic", "before", self.env, channel="bundled", auto_qualify=False)
        before_policy = json.loads((self.store / "update-policy.json").read_text())
        before_config = json.loads((self.store / "update-config.json").read_text())
        after_policy = {**before_policy, "generation": before_policy["generation"] + 1,
                        "mode": "manual", "reason": "latest_automatic_compatibility_hold",
                        "updated_at": 2.0}
        after_config = {"schema_version": 1, "channel": "latest", "auto_qualify": True,
                        "policy_generation": after_policy["generation"],
                        "effective_mode": "automatic", "effective_reason": "enable_latest"}
        transaction_id = "b" * 32
        cli_store._atomic_json(self.store / "update-config-transaction.json", {
            "schema_version": 1, "state": "prepared", "transaction_id": transaction_id,
            "before_policy": before_policy, "before_config": before_config,
            "after_policy": after_policy, "after_config": after_config})
        cli_store._atomic_json(self.store / "update-policy.json",
                               {**after_policy, "_update_config_transaction_id": transaction_id})
        # A 0.4.5 process acquired the shared lock after the crash and made a
        # newer explicit manual choice without understanding update-config.
        legacy_policy = {**after_policy, "generation": after_policy["generation"] + 1,
                         "mode": "manual", "reason": "legacy_manual", "updated_at": 3.0}
        cli_store._atomic_json(self.store / "update-policy.json", legacy_policy)

        recovered = cli_store.get_update_policy(self.env)
        self.assertEqual(recovered["generation"], legacy_policy["generation"])
        self.assertEqual(recovered["mode"], "manual")
        self.assertEqual(recovered["reason"], "legacy_manual")
        self.assertEqual(recovered["channel"], "bundled")
        self.assertFalse(recovered["auto_qualify"])
        conflict = cli_store.explicit_switch_conflict(self.env)
        self.assertIsNotNone(conflict)
        self.assertIn("automatic paid qualification disabled", conflict["diagnostic"])

    def test_crash_recovery_completes_disable_without_overwriting_later_legacy_policy(self):
        before = cli_store.set_update_policy(
            "manual", "before", self.env, channel="latest", auto_qualify=True)
        before_policy = json.loads((self.store / "update-policy.json").read_text())
        before_config = json.loads((self.store / "update-config.json").read_text())
        after_policy = {**before_policy, "generation": before_policy["generation"] + 1,
                        "mode": "manual", "reason": "latest_automatic_compatibility_hold",
                        "updated_at": 2.0}
        after_config = {"schema_version": 1, "channel": "latest", "auto_qualify": False,
                        "policy_generation": after_policy["generation"],
                        "effective_mode": "automatic", "effective_reason": "disable_paid"}
        transaction_id = "c" * 32
        cli_store._atomic_json(self.store / "update-config-transaction.json", {
            "schema_version": 1, "state": "prepared", "transaction_id": transaction_id,
            "before_policy": before_policy, "before_config": before_config,
            "after_policy": after_policy, "after_config": after_config})
        cli_store._atomic_json(self.store / "update-policy.json",
                               {**after_policy, "_update_config_transaction_id": transaction_id})
        legacy_policy = {**after_policy, "generation": after_policy["generation"] + 1,
                         "mode": "manual", "reason": "legacy_manual", "updated_at": 3.0}
        cli_store._atomic_json(self.store / "update-policy.json", legacy_policy)

        recovered = cli_store.get_update_policy(self.env)
        self.assertEqual(recovered["generation"], legacy_policy["generation"])
        self.assertEqual(recovered["reason"], "legacy_manual")
        self.assertEqual(recovered["channel"], "latest")
        self.assertFalse(recovered["auto_qualify"])

    def test_remote_target_requires_signed_manifest_provenance(self):
        target = {"version": "2.1.280", "platform": "darwin-arm64",
                  "sha256": "a" * 64, "size": 123,
                  "url": f"{cli_store.OFFICIAL_RELEASES}/2.1.280/darwin-arm64/claude",
                  "source": "official", "scope": "official_latest",
                  "manifest_sha256": "b" * 64,
                  "signing_key_fingerprint": "wrong"}
        with patch.object(cli_store, "_official_platform", return_value="darwin-arm64"), \
             self.assertRaisesRegex(ValueError, "signing-key proof"):
            cli_store.acquire_official_target(target, self.env)

    def test_latest_effective_owner_activates_once_while_legacy_wire_stays_manual(self):
        current = cli_store.capture(str(self.candidate("owner-current", "2.1.278")), self.env)
        future = cli_store.capture(str(self.candidate("owner-future", "2.1.280")), self.env)
        self.qualify(future)
        selected = cli_store.activate(current["id"], 0, self.env)
        policy = cli_store.set_update_policy(
            "automatic", "latest_owner", self.env,
            channel="latest", auto_qualify=True)
        raw = json.loads((self.store / "update-policy.json").read_text())
        self.assertEqual(raw["mode"], "manual")
        self.assertEqual(raw["reason"], "latest_automatic_compatibility_hold")
        generation = policy["generation"]
        for _ in range(3):
            observed = cli_store.get_update_policy(self.env)
            self.assertEqual(observed["generation"], generation)
            self.assertEqual(observed["mode"], "automatic")
            self.assertEqual(observed["channel"], "latest")
        changed = cli_store.activate_automatic(
            future["id"], selected["generation"], generation,
            future["version"], self.env)
        self.assertEqual(changed["active"], future["id"])
        self.assertEqual(json.loads((self.store / "update-policy.json").read_text())["mode"], "manual")

    def test_new_explicit_switch_pauses_but_retains_latest_preferences_for_resume(self):
        first = cli_store.capture(str(self.candidate("preference-first", "2.1.278")), self.env)
        second = cli_store.capture(str(self.candidate("preference-second", "2.1.277")), self.env)
        selected = cli_store.activate(first["id"], 0, self.env)
        cli_store.set_update_policy("automatic", "latest", self.env,
                                    channel="latest", auto_qualify=True)
        switched = cli_store.activate_explicit(second["id"], selected["generation"], environ=self.env)
        paused = cli_store.get_update_policy(self.env)
        self.assertEqual(paused["mode"], "manual")
        self.assertEqual(paused["channel"], "latest")
        self.assertTrue(paused["auto_qualify"])
        resumed = cli_store.set_update_policy("automatic", "resume", self.env)
        self.assertEqual(resumed["mode"], "automatic")
        self.assertEqual(resumed["channel"], "latest")
        self.assertTrue(resumed["auto_qualify"])
        raw = json.loads((self.store / "update-policy.json").read_text())
        self.assertEqual(raw["mode"], "manual")
        self.assertEqual(raw["reason"], "latest_automatic_compatibility_hold")

    def test_legacy_automatic_generation_ignores_stale_latest_preferences(self):
        cli_store.set_update_policy("automatic", "latest", self.env,
                                    channel="latest", auto_qualify=True)
        raw = json.loads((self.store / "update-policy.json").read_text())
        cli_store._atomic_json(self.store / "update-policy.json", {
            **raw, "generation": raw["generation"] + 1,
            "mode": "automatic", "reason": "legacy_automatic"})
        effective = cli_store.get_update_policy(self.env)
        self.assertEqual(effective["mode"], "automatic")
        self.assertEqual(effective["reason"], "legacy_automatic")
        self.assertEqual(effective["channel"], "bundled")
        self.assertFalse(effective["auto_qualify"])

    def test_explicit_switch_restores_selection_if_policy_persistence_fails(self):
        first = cli_store.capture(str(self.candidate("first", "2.1.276")), self.env)
        second = cli_store.capture(str(self.candidate("second", "2.1.278")), self.env)
        selected = cli_store.activate(first["id"], 0, self.env)
        original_atomic = cli_store._atomic_json

        failed = False
        def fail_policy(path, value):
            nonlocal failed
            if Path(path).name == "update-policy.json" and not failed:
                failed = True
                raise OSError("policy disk failure")
            return original_atomic(path, value)

        with patch.object(cli_store, "_atomic_json", side_effect=fail_policy), \
             self.assertRaisesRegex(OSError, "policy disk failure"):
            cli_store.activate_explicit(second["id"], selected["generation"], environ=self.env)
        self.assertEqual(cli_store.get_selection(self.env), selected)
        self.assertEqual(cli_store.get_update_policy(self.env)["mode"], "automatic")

    def test_explicit_transaction_clears_only_unambiguous_prepared_and_committed_pairs(self):
        first = cli_store.capture(str(self.candidate("first", "2.1.276")), self.env)
        second = cli_store.capture(str(self.candidate("second", "2.1.278")), self.env)
        before_selection = cli_store.activate(first["id"], 0, self.env)
        before_policy = cli_store.get_update_policy(self.env)
        after_selection = {"schema_version": 1, "generation": before_selection["generation"] + 1,
                           "active": second["id"], "previous": first["id"],
                           "history": [first["id"], second["id"]], "mode": "managed"}
        after_policy = {"schema_version": 1, "generation": before_policy["generation"] + 1,
                        "mode": "manual", "reason": "manual_activation", "updated_at": 1.0}
        transaction_id = "1" * 32
        transaction = {"schema_version": 1, "state": "prepared", "transaction_id": transaction_id,
                       "before_selection": before_selection, "before_policy": before_policy,
                       "after_selection": after_selection, "after_policy": after_policy}
        cli_store._atomic_json(self.store / "explicit-switch-transaction.json", transaction)
        self.assertEqual(cli_store.get_selection(self.env), before_selection)
        self.assertEqual(cli_store.get_update_policy(self.env), before_policy)
        self.assertFalse((self.store / "explicit-switch-transaction.json").exists())

        cli_store._atomic_json(self.store / "explicit-switch-transaction.json", transaction)
        cli_store._atomic_json(self.store / "selection.json",
                               {**after_selection, "_explicit_transaction_id": transaction_id})
        self.assertEqual(cli_store.get_selection(self.env), before_selection)
        self.assertEqual(cli_store.get_update_policy(self.env), before_policy)
        self.assertFalse((self.store / "explicit-switch-transaction.json").exists())

        cli_store._atomic_json(self.store / "explicit-switch-transaction.json",
                               {**transaction, "state": "committed"})
        cli_store._atomic_json(self.store / "selection.json",
                               {**after_selection, "_explicit_transaction_id": transaction_id})
        cli_store._atomic_json(self.store / "update-policy.json",
                               {**after_policy, "_explicit_transaction_id": transaction_id})
        self.assertEqual({key: cli_store.get_update_policy(self.env)[key] for key in after_policy}, after_policy)
        self.assertEqual(cli_store.get_selection(self.env), after_selection)
        self.assertFalse((self.store / "explicit-switch-transaction.json").exists())

    def test_explicit_transaction_treats_after_selection_before_policy_as_legacy_conflict(self):
        first = cli_store.capture(str(self.candidate("first", "2.1.276")), self.env)
        second = cli_store.capture(str(self.candidate("second", "2.1.278")), self.env)
        before_selection = cli_store.activate(first["id"], 0, self.env)
        before_policy = cli_store.get_update_policy(self.env)
        after_selection = {"schema_version": 1, "generation": before_selection["generation"] + 1,
                           "active": second["id"], "previous": first["id"],
                           "history": [first["id"], second["id"]], "mode": "managed"}
        after_policy = {"schema_version": 1, "generation": before_policy["generation"] + 1,
                        "mode": "manual", "reason": "manual_activation", "updated_at": 1.0}
        transaction = {"schema_version": 1, "state": "prepared", "transaction_id": "2" * 32,
                       "before_selection": before_selection, "before_policy": before_policy,
                       "after_selection": after_selection, "after_policy": after_policy}
        cli_store._atomic_json(self.store / "explicit-switch-transaction.json", transaction)
        cli_store._atomic_json(self.store / "selection.json", after_selection)
        self.assertEqual(cli_store.get_selection(self.env), after_selection)
        self.assertEqual(cli_store.get_update_policy(self.env), before_policy)
        conflict = cli_store.explicit_switch_conflict(self.env)
        self.assertIsNotNone(conflict)
        self.assertIn("Concurrent legacy", conflict["diagnostic"])
        cli_store.acknowledge_explicit_switch_conflict(conflict["notice_id"], self.env)
        self.assertIsNone(cli_store.explicit_switch_conflict(self.env))

    def test_explicit_transaction_preserves_conflicting_legacy_writer_selection(self):
        first = cli_store.capture(str(self.candidate("first", "2.1.276")), self.env)
        second = cli_store.capture(str(self.candidate("second", "2.1.278")), self.env)
        legacy = cli_store.capture(str(self.candidate("legacy", "2.1.275")), self.env)
        before_selection = cli_store.activate(first["id"], 0, self.env)
        before_policy = cli_store.get_update_policy(self.env)
        after_selection = {"schema_version": 1, "generation": before_selection["generation"] + 1,
                           "active": second["id"], "previous": first["id"],
                           "history": [first["id"], second["id"]], "mode": "managed"}
        after_policy = {"schema_version": 1, "generation": before_policy["generation"] + 1,
                        "mode": "manual", "reason": "manual_activation", "updated_at": 1.0}
        transaction = {"schema_version": 1, "state": "prepared", "transaction_id": "3" * 32,
                       "before_selection": before_selection, "before_policy": before_policy,
                       "after_selection": after_selection, "after_policy": after_policy}
        legacy_selection = {"schema_version": 1, "generation": after_selection["generation"] + 1,
                            "active": legacy["id"], "previous": first["id"],
                            "history": [first["id"], legacy["id"]], "mode": "managed"}
        cli_store._atomic_json(self.store / "explicit-switch-transaction.json", transaction)
        cli_store._atomic_json(self.store / "selection.json", legacy_selection)

        self.assertEqual(cli_store.get_selection(self.env), legacy_selection)
        self.assertEqual(cli_store.get_update_policy(self.env), before_policy)
        self.assertFalse((self.store / "explicit-switch-transaction.json").exists())
        conflicts = list(self.store.glob("cli-switch-conflict-*.json"))
        self.assertEqual(len(conflicts), 1)
        recorded = json.loads(conflicts[0].read_text())
        self.assertEqual(recorded["observed_selection"], legacy_selection)
        self.assertEqual(recorded["state"], "conflict")

    def test_select_can_fall_back_for_capability_but_never_to_path(self):
        fallback = cli_store.capture(str(self.candidate("fallback", "9.9.278")), self.env)
        active = cli_store.capture(str(self.candidate("active", "9.9.279")), self.env)
        self.qualify(fallback, write="pass")
        self.qualify(active)
        cli_store.activate(fallback["id"], 0, self.env)
        cli_store.activate(active["id"], 1, self.env)
        selected = cli_store.select(["write"], "bridge-contract-test", self.env)
        self.assertEqual(selected["id"], fallback["id"])
        self.assertEqual(selected["selection_reason"], "previous")
        self.assertIsNone(cli_store.select(["workflow"], "bridge-contract-test", self.env))

    def test_select_falls_back_to_profile_that_declares_required_budget_capability(self):
        older = cli_store.capture(str(self.candidate("older", "2.1.276")), self.env)
        latest = cli_store.capture(str(self.candidate("latest", "2.1.278")), self.env)
        cli_store.activate(older["id"], 0, self.env)
        cli_store.activate(latest["id"], 1, self.env)

        selected = cli_store.select(["core"], "bridge-contract-test", self.env,
                                    required_capabilities=["max_turns"])
        self.assertEqual(selected["id"], older["id"])
        self.assertEqual(selected["selection_reason"], "previous")
        self.assertEqual(cli_store.select(["core"], "bridge-contract-test", self.env,
                                          required_capabilities=["unknown_flag"]), None)

        unknown = cli_store.capture(str(self.candidate("qualified-unknown", "9.9.999")), self.env)
        self.qualify(unknown)
        cli_store.activate(unknown["id"], cli_store.get_selection(self.env)["generation"], self.env)
        still_older = cli_store.select(["core"], "bridge-contract-test", self.env,
                                       required_capabilities=["max_turns"])
        self.assertEqual(still_older["id"], older["id"])

    def test_automatic_activation_is_linearized_with_policy_and_never_downgrades(self):
        lower = cli_store.capture(str(self.candidate("lower", "2.1.276")), self.env)
        target = cli_store.capture(str(self.candidate("target", "2.1.278")), self.env)
        higher = cli_store.capture(str(self.candidate("higher", "2.1.279")), self.env)
        cli_store.activate(lower["id"], 0, self.env)
        initial = cli_store.get_selection(self.env)
        policy = cli_store.get_update_policy(self.env)

        cli_store.set_update_policy("manual", "manual_rollback", self.env)
        with self.assertRaises(cli_store.AutomaticActivationSuperseded):
            cli_store.activate_automatic(target["id"], initial["generation"], policy["generation"],
                                         "2.1.278", self.env)
        self.assertEqual(cli_store.get_selection(self.env)["active"], lower["id"])

        automatic = cli_store.set_update_policy("automatic", "user_request", self.env)
        switched = cli_store.activate_automatic(target["id"], initial["generation"], automatic["generation"],
                                                "2.1.278", self.env)
        self.assertEqual(switched["active"], target["id"])
        self.qualify(higher)
        cli_store.activate(higher["id"], switched["generation"], self.env)
        current = cli_store.get_selection(self.env)
        kept = cli_store.activate_automatic(target["id"], current["generation"], automatic["generation"],
                                            "2.1.278", self.env)
        self.assertEqual(kept["active"], higher["id"])

    def test_official_target_sorting_and_retained_acquisition_are_digest_bound(self):
        self.assertGreater(cli_store.version_key("2.10.0"), cli_store.version_key("2.9.99"))
        self.assertGreater(cli_store.version_key("2.1.278"), cli_store.version_key("2.1.278-rc.1"))
        retained = cli_store.capture(str(self.candidate("retained", "2.1.278")), self.env)
        target = {"version": "2.1.278", "platform": "darwin-arm64",
                  "sha256": retained["sha256"], "size": Path(retained["path"]).stat().st_size,
                  "url": "https://example.invalid/claude", "source": "bundled",
                  "scope": "installed_plugin_release"}
        with patch.object(cli_store, "official_release_target", return_value=target), \
             patch.object(cli_store, "_download_official_release") as download:
            acquired = cli_store.acquire_official_release(environ=self.env)
        self.assertEqual(acquired["id"], retained["id"])
        self.assertEqual(acquired["acquisition"], "retained")
        download.assert_not_called()

    def test_retained_official_filters_metadata_before_full_binary_validation(self):
        older = cli_store.capture(str(self.candidate("metadata-older", "2.1.277")), self.env)
        target_descriptor = cli_store.capture(str(self.candidate("metadata-target", "2.1.280")), self.env)
        target = {"version": target_descriptor["version"], "platform": "darwin-arm64",
                  "sha256": target_descriptor["sha256"],
                  "size": Path(target_descriptor["path"]).stat().st_size,
                  "url": "https://example.invalid/claude", "source": "official",
                  "scope": "official_latest"}
        original_identity = cli_store.identity
        with patch.object(cli_store, "identity", wraps=original_identity) as full:
            retained = cli_store._retained_official(target, self.env)
        self.assertEqual(retained["id"], target_descriptor["id"])
        self.assertEqual(full.call_count, 1)
        self.assertEqual(full.call_args.args[0], target_descriptor["id"])
        self.assertNotEqual(older["id"], target_descriptor["id"])

    def test_download_interruption_resumes_with_validated_content_range_and_keeps_lock_inode(self):
        destination = self.base / "download" / "claude.part"
        requests = []
        responses = [FakeResponse(200, [b"abc", OSError("connection dropped")]),
                     FakeResponse(206, [b"def", b""], {"Content-Range": "bytes 3-5/6"})]

        def opener(request, **_kwargs):
            requests.append(request)
            return responses.pop(0)

        with self.assertRaisesRegex(RuntimeError, "resumable partial retained"):
            cli_store._download("https://example.invalid/claude", destination,
                                expected_size=6, opener=opener)
        self.assertEqual(destination.read_bytes(), b"abc")
        lock = destination.with_name("claude.part.lock")
        inode = lock.stat().st_ino
        progress = []
        cli_store._download("https://example.invalid/claude", destination,
                            expected_size=6, opener=opener,
                            progress=lambda received, total: progress.append((received, total)))
        self.assertEqual(destination.read_bytes(), b"abcdef")
        self.assertEqual(requests[1].get_header("Range"), "bytes=3-")
        self.assertEqual(progress[0], (3, 6))
        self.assertEqual(progress[-1], (6, 6))
        self.assertEqual(lock.stat().st_ino, inode)

    def test_download_resets_on_200_and_rejects_bad_206_without_corrupting_prefix(self):
        destination = self.base / "claude.part"
        destination.write_bytes(b"old")
        with self.assertRaisesRegex(RuntimeError, "resumable partial retained"):
            cli_store._download(
                "https://example.invalid/claude", destination, expected_size=6,
                opener=lambda *_args, **_kwargs: FakeResponse(200, [OSError("dropped")]))
        self.assertEqual(destination.read_bytes(), b"old")

        cli_store._download("https://example.invalid/claude", destination, expected_size=6,
                            opener=lambda *_args, **_kwargs: FakeResponse(200, [b"abcdef", b""]))
        self.assertEqual(destination.read_bytes(), b"abcdef")

        destination.write_bytes(b"abc")
        with self.assertRaisesRegex(RuntimeError, "requested offset"):
            cli_store._download(
                "https://example.invalid/claude", destination, expected_size=6,
                opener=lambda *_args, **_kwargs: FakeResponse(
                    206, [b"def"], {"Content-Range": "bytes 2-5/6"}))
        self.assertEqual(destination.read_bytes(), b"abc")

    def test_download_validates_206_end_and_body_length_and_preserves_incomplete_bytes(self):
        destination = self.base / "claude.part"
        destination.write_bytes(b"abc")
        with self.assertRaisesRegex(RuntimeError, "range bounds"):
            cli_store._download(
                "https://example.invalid/claude", destination, expected_size=6,
                opener=lambda *_args, **_kwargs: FakeResponse(
                    206, [b"def"], {"Content-Range": "bytes 3-6/6"}))
        self.assertEqual(destination.read_bytes(), b"abc")

        with self.assertRaisesRegex(RuntimeError, "body length"):
            cli_store._download(
                "https://example.invalid/claude", destination, expected_size=6,
                opener=lambda *_args, **_kwargs: FakeResponse(
                    206, [b"de", b""], {"Content-Range": "bytes 3-5/6"}))
        self.assertEqual(destination.read_bytes(), b"abcde")

        destination.write_bytes(b"abc")
        with self.assertRaisesRegex(RuntimeError, "ended incompletely"):
            cli_store._download(
                "https://example.invalid/claude", destination, expected_size=6,
                opener=lambda *_args, **_kwargs: FakeResponse(
                    206, [IncompleteRead(b"de", 1)], {"Content-Range": "bytes 3-5/6"}))
        self.assertEqual(destination.read_bytes(), b"abcde")

    def test_download_uses_read1_and_wraps_http_protocol_interruptions(self):
        destination = self.base / "claude.part"

        class ReadOneResponse(FakeResponse):
            def read(self, _size):
                raise AssertionError("read should not be used when read1 is available")

            def read1(self, _size):
                if not self.chunks:
                    return b""
                value = self.chunks.pop(0)
                if isinstance(value, BaseException):
                    raise value
                return value

        cli_store._download(
            "https://example.invalid/claude", destination, expected_size=3,
            opener=lambda *_args, **_kwargs: ReadOneResponse(200, [b"abc", b""]))
        self.assertEqual(destination.read_bytes(), b"abc")

        destination.unlink()
        with self.assertRaisesRegex(RuntimeError, "resumable partial retained"):
            cli_store._download(
                "https://example.invalid/claude", destination, expected_size=3,
                opener=lambda *_args, **_kwargs: FakeResponse(200, [HTTPException("truncated")]))

    def test_download_lock_wait_is_bounded_and_never_unlinks_shared_inode(self):
        lock = self.base / "download.lock"
        lock.parent.mkdir(parents=True, exist_ok=True)
        fd = cli_store.os.open(lock, cli_store.os.O_CREAT | cli_store.os.O_RDWR, 0o600)
        try:
            cli_store.fcntl.flock(fd, cli_store.fcntl.LOCK_EX)
            inode = lock.stat().st_ino
            with self.assertRaisesRegex(RuntimeError, "another official Claude acquisition"):
                with cli_store._download_lock(lock, wait_timeout_seconds=0.01):
                    self.fail("contended lock must not be acquired")
            self.assertTrue(lock.exists())
            self.assertEqual(lock.stat().st_ino, inode)
        finally:
            cli_store.fcntl.flock(fd, cli_store.fcntl.LOCK_UN)
            cli_store.os.close(fd)

    def test_download_handles_416_only_for_a_complete_exact_target(self):
        destination = self.base / "claude.part"
        destination.write_bytes(b"abcdef")

        def unsatisfied(total: int):
            headers = Message()
            headers["Content-Range"] = f"bytes */{total}"
            return HTTPError("https://example.invalid/claude", 416, "range", headers, None)

        cli_store._download("https://example.invalid/claude", destination, expected_size=6,
                            opener=lambda *_args, **_kwargs: (_ for _ in ()).throw(unsatisfied(6)))
        self.assertEqual(destination.read_bytes(), b"abcdef")
        destination.write_bytes(b"abc")
        with self.assertRaisesRegex(RuntimeError, "incomplete or mismatched"):
            cli_store._download("https://example.invalid/claude", destination, expected_size=6,
                                opener=lambda *_args, **_kwargs: (_ for _ in ()).throw(unsatisfied(6)))
        self.assertEqual(destination.read_bytes(), b"abc")

    def test_download_idle_and_attempt_bounds_preserve_resumable_bytes(self):
        idle_destination = self.base / "idle.part"
        with self.assertRaisesRegex(RuntimeError, "no progress"):
            cli_store._download(
                "https://example.invalid/claude", idle_destination, expected_size=3,
                idle_timeout_seconds=5, attempt_timeout_seconds=100,
                opener=lambda *_args, **_kwargs: FakeResponse(200, [b"a"]),
                monotonic=FakeClock(0, 0, 6))
        self.assertEqual(idle_destination.read_bytes(), b"")

        attempt_destination = self.base / "attempt.part"
        with self.assertRaisesRegex(RuntimeError, "attempt exceeded"):
            cli_store._download(
                "https://example.invalid/claude", attempt_destination, expected_size=3,
                idle_timeout_seconds=10, attempt_timeout_seconds=2,
                opener=lambda *_args, **_kwargs: FakeResponse(200, [b"a", b"b"]),
                monotonic=FakeClock(0, 0, 1, 3))
        self.assertEqual(attempt_destination.read_bytes(), b"a")

    def test_official_download_uses_stable_exact_target_partial_across_attempts(self):
        payload = b"audited bytes"
        target = {"version": "2.1.278", "platform": "darwin-arm64",
                  "sha256": __import__("hashlib").sha256(payload).hexdigest(),
                  "size": len(payload), "url": "https://example.invalid/claude",
                  "source": "bundled", "scope": "installed_plugin_release"}
        destinations = []

        def interrupted(_url, destination, **_kwargs):
            destinations.append(Path(destination))
            Path(destination).write_bytes(payload[:4])
            raise RuntimeError("interrupted")

        with patch.object(cli_store, "official_release_target", return_value=target), \
             patch.object(cli_store, "_download", side_effect=interrupted):
            for _ in range(2):
                with self.assertRaisesRegex(RuntimeError, "interrupted"):
                    cli_store._download_official_release(environ=self.env)
        self.assertEqual(destinations[0], destinations[1])
        self.assertEqual(destinations[0].read_bytes(), payload[:4])
        lock = destinations[0].parent / "download.lock"
        self.assertTrue(lock.exists())

    def test_official_download_verifies_final_digest_and_native_signature_before_staging(self):
        payload = b"audited executable bytes"
        target = {"version": "2.1.278", "platform": "darwin-arm64",
                  "sha256": __import__("hashlib").sha256(payload).hexdigest(),
                  "size": len(payload), "url": "https://example.invalid/claude",
                  "source": "bundled", "scope": "installed_plugin_release"}

        def complete(_url, destination, **_kwargs):
            Path(destination).write_bytes(payload)

        with patch.object(cli_store, "official_release_target", return_value=target), \
             patch.object(cli_store, "_download", side_effect=complete), \
             patch.object(cli_store, "_native_check", return_value=None) as native:
            binary, source, staging = cli_store._download_official_release(environ=self.env)
        self.assertEqual(binary.read_bytes(), payload)
        self.assertEqual(source, target["url"])
        native.assert_called_once()
        self.assertEqual(native.call_args.args[0].name, "claude.part")
        self.assertTrue((binary.parent.parent / "downloads").is_dir())
        self.assertTrue((binary.parent.parent / "downloads" / f"2.1.278-darwin-arm64-{target['sha256'][:16]}" / "download.lock").exists())
        shutil.rmtree(staging)

        def corrupt(_url, destination, **_kwargs):
            Path(destination).write_bytes(b"wrong bytes")

        with patch.object(cli_store, "official_release_target", return_value=target), \
             patch.object(cli_store, "_download", side_effect=corrupt), \
             self.assertRaisesRegex(RuntimeError, "SHA-256"):
            cli_store._download_official_release(environ=self.env)
        partial = self.store / "downloads" / f"2.1.278-darwin-arm64-{target['sha256'][:16]}" / "claude.part"
        self.assertFalse(partial.exists())

    def test_native_signature_fields_require_exact_key_values(self):
        candidate = self.candidate("signed", "2.1.278")
        exact = ("Identifier=com.anthropic.claude-code\n"
                 "TeamIdentifier=Q6L2SF6YDW\n"
                 "Authority=Developer ID Application: Anthropic PBC (Q6L2SF6YDW)\n")
        suffix = exact.replace("Identifier=com.anthropic.claude-code\n",
                               "Identifier=com.anthropic.claude-code.helper\n")

        def results(details: str):
            return [Mock(returncode=0, stdout="Mach-O 64-bit executable", stderr=""),
                    Mock(returncode=0, stdout="", stderr=""),
                    Mock(returncode=0, stdout="", stderr=details)]

        self.native.stop()
        try:
            with patch.object(cli_store.sys, "platform", "darwin"), \
                 patch.object(cli_store.subprocess, "run", side_effect=results(suffix)), \
                 self.assertRaisesRegex(ValueError, "code-signature"):
                cli_store._native_check(candidate)
            with patch.object(cli_store.sys, "platform", "darwin"), \
                 patch.object(cli_store.subprocess, "run", side_effect=results(exact)):
                cli_store._native_check(candidate)
        finally:
            self.native.start()

    def test_corrupt_official_acquisition_never_changes_selection(self):
        active = cli_store.capture(str(self.candidate("active", "2.1.276")), self.env)
        cli_store.activate(active["id"], 0, self.env)
        audited = self.candidate("audited", "2.1.278")
        target = {"version": "2.1.278", "platform": "darwin-arm64",
                  "sha256": cli_store._sha256(audited), "size": audited.stat().st_size,
                  "url": "https://example.invalid/claude", "source": "bundled",
                  "scope": "installed_plugin_release"}
        staging = self.base / "corrupt-stage"; staging.mkdir()
        corrupt = self.candidate("corrupt-stage/claude", "2.1.278")
        corrupt.write_text(corrupt.read_text() + "# corrupt\n")
        with patch.object(cli_store, "official_release_target", return_value=target), \
             patch.object(cli_store, "_retained_official", return_value=None), \
             patch.object(cli_store, "_capture_current_official", return_value=None), \
             patch.object(cli_store, "_download_official_release",
                          return_value=(corrupt, target["url"], staging)):
            with self.assertRaisesRegex(RuntimeError, "did not match"):
                cli_store.acquire_official_release(environ=self.env)
        self.assertEqual(cli_store.get_selection(self.env)["active"], active["id"])
        self.assertFalse(staging.exists())

    def test_unknown_native_candidate_is_retained_while_audited_baseline_becomes_active(self):
        candidate = self.candidate("unknown", "9.9.999")
        staging = self.base / "baseline-stage"; staging.mkdir()
        baseline = self.candidate("baseline-stage/claude", "2.1.278")
        with patch.object(cli_store, "_download_official_baseline", return_value=(baseline, "https://downloads.claude.ai/claude-code-releases/2.1.278/darwin-arm64/claude", staging)):
            prepared = cli_store.prepare(str(candidate), self.env)
        self.assertEqual(prepared["status"], "ready")
        self.assertEqual(prepared["candidate"]["version"], "9.9.999")
        self.assertEqual(prepared["identity"]["version"], "2.1.278")
        selection = cli_store.get_selection(self.env)
        self.assertEqual(selection["active"], prepared["identity"]["id"])
        self.assertIsNone(selection["previous"])
        self.assertFalse(staging.exists())

    def test_healthy_active_is_reused_without_downloading_or_switching_for_unknown_candidate(self):
        baseline = cli_store.capture(str(self.candidate("baseline", "2.1.278")), self.env)
        cli_store.activate(baseline["id"], 0, self.env)
        unknown = self.candidate("unknown", "9.9.999")
        with patch.object(cli_store, "_download_official_baseline") as download:
            prepared = cli_store.prepare(str(unknown), self.env)
        self.assertEqual(prepared["status"], "ready")
        self.assertEqual(prepared["identity"]["id"], baseline["id"])
        self.assertEqual(cli_store.get_selection(self.env)["active"], baseline["id"])
        download.assert_not_called()

    def test_prepare_uses_audited_private_download_without_running_global_installer(self):
        staging = self.base / "download-stage"; staging.mkdir()
        binary = self.candidate("download-stage/claude", "2.1.278")
        with patch.object(cli_store, "_download_official_baseline", return_value=(binary, "https://downloads.claude.ai/claude-code-releases/2.1.278/darwin-arm64/claude", staging)):
            prepared = cli_store.prepare(None, self.env)
        self.assertEqual(prepared["status"], "ready")
        self.assertEqual(prepared["identity"]["source"], "https://downloads.claude.ai/claude-code-releases/2.1.278/darwin-arm64/claude")
        self.assertFalse(staging.exists())

    def test_official_bootstrap_failure_is_recoverable_and_does_not_select_a_cli(self):
        with patch.object(cli_store, "_download_official_baseline", side_effect=RuntimeError("official download failed")):
            prepared = cli_store.prepare(None, self.env)
        self.assertEqual(prepared["status"], "bootstrap_error")
        self.assertIn("official download failed", prepared["action"])
        self.assertIsNone(cli_store.get_selection(self.env)["active"])

    def test_inconclusive_then_pass_and_partial_groups_promote_without_replacing_history(self):
        descriptor = cli_store.capture(str(self.candidate("candidate", "9.9.278")), self.env)
        self.qualify(descriptor, "first", core="inconclusive", read_only="inconclusive")
        first = cli_store.qualification(descriptor["id"], "bridge-contract-test", self.env)
        self.assertEqual(first["matrix"]["core"]["status"], "inconclusive")
        self.qualify(descriptor, "second", core="pass", read_only="inconclusive")
        self.qualify(descriptor, "third", core="inconclusive", read_only="pass")
        promoted = cli_store.qualification(descriptor["id"], "bridge-contract-test", self.env)
        self.assertEqual(promoted["matrix"]["core"]["status"], "pass")
        self.assertEqual(promoted["matrix"]["read_only"]["status"], "pass")
        self.assertEqual(len(promoted["receipt_ids"]), 3)

    def test_inconclusive_job_keeps_pass_but_confirmed_fail_revokes_that_group(self):
        descriptor = cli_store.capture(str(self.candidate("candidate", "9.9.278")), self.env)
        self.qualify(descriptor, "pass", core="pass", read_only="pass")
        self.qualify(descriptor, "network", core="inconclusive", read_only="inconclusive")
        retained = cli_store.qualification(descriptor["id"], "bridge-contract-test", self.env)
        self.assertEqual(retained["matrix"]["core"]["status"], "pass")
        self.qualify(descriptor, "confirmed-fail", core="fail", read_only="inconclusive")
        revoked = cli_store.qualification(descriptor["id"], "bridge-contract-test", self.env)
        self.assertEqual(revoked["matrix"]["core"]["status"], "fail")
        self.assertEqual(revoked["matrix"]["read_only"]["status"], "pass")


if __name__ == "__main__":
    unittest.main()
