"""A dispatch and its correction remain bound to the captured executable."""
import json
import os
import unittest
from pathlib import Path
from unittest.mock import patch

import test_runtime as fixtures
import bridge
import cli_store


class PinnedDispatchTests(unittest.TestCase):
    setUp = fixtures.RuntimeTests.setUp
    tearDown = fixtures.RuntimeTests.tearDown
    packet = fixtures.RuntimeTests.packet
    finish = fixtures.RuntimeTests.finish

    def test_environment_switch_between_selection_and_launch_keeps_first_binary(self):
        replacement = self.root / "other.py"
        replacement.write_text(self.fake.read_text().replace("'summary':'done'", "'summary':'WRONG EXECUTABLE'"))
        replacement.chmod(0o755)
        original = bridge.create_cli_descriptor

        def capture_then_switch(*args, **kwargs):
            result = original(*args, **kwargs)
            os.environ["CLAUDE_BIN"] = str(replacement)
            return result

        with patch.object(bridge, "create_cli_descriptor", side_effect=capture_then_switch):
            first = self.runtime.start(self.packet())
        final = self.finish(first["run_id"])["snapshot"]
        self.assertEqual(final["status"], "reported")
        self.assertEqual(final["summary"], "done")
        pinned = json.loads((Path(final["run_dir"]) / "cli-selection.json").read_text())
        self.assertEqual(pinned["selection"]["path"], str(self.fake))
        self.assertEqual(final["environment"]["cli_identity"]["sha256"], pinned["sha256"])

    def test_resume_uses_original_binary_after_active_path_changes(self):
        first = self.runtime.start(self.packet())
        initial = self.finish(first["run_id"])["snapshot"]
        replacement = self.root / "new-cli"
        replacement.write_text("#!/bin/sh\nexit 88\n")
        replacement.chmod(0o755)
        os.environ["CLAUDE_BIN"] = str(replacement)
        packet = self.packet(2)
        packet["correction"] = {"finding_id": "f1", "kind": "local", "reason": "check again", "attempt": 1}
        second = self.runtime.start(packet, resume_run_id=first["run_id"])
        final = self.finish(second["run_id"])["snapshot"]
        self.assertEqual(final["status"], "reported")
        self.assertEqual(initial["session_id"], final["session_id"])
        self.assertEqual(initial["environment"]["cli_identity"], final["environment"]["cli_identity"])

    def test_changed_binary_before_bridge_launch_fails_with_recoverable_receipt(self):
        original = bridge.create_cli_descriptor

        def capture_then_change(*args, **kwargs):
            result = original(*args, **kwargs)
            self.fake.write_text(self.fake.read_text() + "\n# changed after selection\n")
            return result

        with patch.object(bridge, "create_cli_descriptor", side_effect=capture_then_change):
            first = self.runtime.start(self.packet())
        final = self.finish(first["run_id"])["snapshot"]
        self.assertEqual(final["status"], "failed")
        self.assertFalse((Path(first["run_dir"]) / "child.json").exists())
        retry = self.runtime.start(self.packet(2))
        self.assertEqual(self.finish(retry["run_id"])["snapshot"]["status"], "reported")

    def test_modified_original_cannot_silently_resume_with_new_path(self):
        first = self.runtime.start(self.packet())
        self.finish(first["run_id"])
        self.fake.write_text(self.fake.read_text() + "\n# replace original\n")
        packet = self.packet(2)
        packet["correction"] = {"finding_id": "f1", "kind": "local", "reason": "check again", "attempt": 1}
        with self.assertRaisesRegex(bridge.BridgeError, "cli_identity_changed"):
            self.runtime.start(packet, resume_run_id=first["run_id"])

    def test_public_packet_cannot_inject_qualification_descriptor(self):
        for key in ("cli_descriptor", "cli_selection", "cli_identity", "qualification"):
            with self.subTest(key=key), self.assertRaises(bridge.BridgeError):
                self.runtime.start({**self.packet(), key: {"purpose": "qualification"}})

    def test_released_plugin_descriptor_resumes_with_current_qualification(self):
        first = self.runtime.start(self.packet())
        initial = self.finish(first["run_id"])["snapshot"]
        descriptor_path = Path(initial["run_dir"]) / "cli-selection.json"
        descriptor = json.loads(descriptor_path.read_text())
        old_contract = "a8c6eac1eaf4700efa4dc799f7c32f9495e95b29ad93441a4d8eaf040cfb3955"
        descriptor["contract_id"] = old_contract
        for field in ("dispatch_protocol_version", "contract_id_at_start", "contract_id_now"):
            descriptor.pop(field)
        descriptor_path.write_text(json.dumps(descriptor))
        correction = {**self.packet(2), "correction": {"finding_id": "f1", "kind": "local", "reason": "check again", "attempt": 1}}
        second = self.runtime.start(correction, resume_run_id=first["run_id"])
        final = self.finish(second["run_id"])["snapshot"]
        self.assertEqual(final["status"], "reported")
        self.assertEqual(initial["session_id"], final["session_id"])
        identity = final["receipt"]["cli_identity"]
        self.assertEqual(identity["sha256"], descriptor["sha256"])
        self.assertEqual(identity["contract_id_at_start"], old_contract)
        self.assertEqual(identity["contract_id_now"], bridge.bridge_contract_id())

    def test_compatible_protocol_survives_source_patch_but_unknown_protocol_does_not(self):
        first = self.runtime.start(self.packet())
        initial = self.finish(first["run_id"])["snapshot"]
        path = Path(initial["run_dir"]) / "cli-selection.json"
        descriptor = json.loads(path.read_text())
        descriptor["contract_id"] = descriptor["contract_id_at_start"] = "e" * 64
        path.write_text(json.dumps(descriptor))
        pinned = bridge.create_cli_descriptor(self.packet(2), Path(initial["run_dir"]))
        self.assertEqual(pinned["sha256"], descriptor["sha256"])
        self.assertEqual(pinned["contract_id_at_start"], "e" * 64)
        for changes in ({"dispatch_protocol_version": 999}, {"dispatch_protocol_version": None}, {"schema_version": 99}):
            path.write_text(json.dumps({**descriptor, **changes}))
            with self.assertRaisesRegex(bridge.BridgeError, "protocol_incompatible"):
                bridge.create_cli_descriptor(self.packet(2), Path(initial["run_dir"]))

    def test_resume_rechecks_current_resume_capability_before_model_launch(self):
        self.fake.write_text(self.fake.read_text().replace("--session-id --resume --permission-mode", "--session-id --permission-mode"))
        first = self.runtime.start(self.packet())
        self.assertEqual(self.finish(first["run_id"])["snapshot"]["status"], "reported")
        correction = {**self.packet(2), "correction": {"finding_id": "f1", "kind": "local", "reason": "check", "attempt": 1}}
        second = self.runtime.start(correction, resume_run_id=first["run_id"])
        final = self.finish(second["run_id"])["snapshot"]
        self.assertEqual(final["status"], "blocked")
        self.assertFalse((Path(final["run_dir"]) / "child.json").exists())

    def test_managed_capability_gap_is_not_reported_as_uninstalled(self):
        selection = {"path": None, "source": "managed_native", "candidate": "retained-id", "identity": {"id": "retained-id"}, "action": "validate required groups"}
        result = bridge.check_environment(self.repo, selection=selection, required_groups=["workflow"])
        self.assertEqual(result["status"], "cli_capability_unverified")
        self.assertTrue(result["installation"]["installed"])
        self.assertEqual(result["auth"]["status"], "not_checked")

    def test_supported_upgrade_preserves_task_requiring_old_optional_limit(self):
        newer_path = self.root / "newer-cli"
        newer_path.write_text(self.fake.read_text().replace("print('2.1.276')", "print('2.1.278')"))
        newer_path.chmod(0o755)
        # Mock only macOS signature verification; selection and packet routing
        # use real private objects and released capability profiles.
        with patch.object(cli_store, "_native_check", return_value=None):
            older = cli_store.capture(str(self.fake))
            newer = cli_store.capture(str(newer_path))
            cli_store.activate(older["id"])
            cli_store.activate(newer["id"])
            ordinary = bridge.create_cli_descriptor(self.packet())
            limited = bridge.create_cli_descriptor({**self.packet(), "budget": {"max_turns": 3}})
            self.assertEqual(ordinary["identity_id"], newer["id"])
            self.assertEqual(limited["identity_id"], older["id"])
            self.assertEqual(cli_store.get_selection()["active"], newer["id"])


if __name__ == "__main__":
    unittest.main()
