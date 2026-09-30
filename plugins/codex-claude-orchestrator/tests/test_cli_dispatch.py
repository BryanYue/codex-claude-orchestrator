"""A dispatch and its correction remain bound to the same local executable."""
import json
import os
import unittest
from pathlib import Path
from unittest.mock import patch

import test_runtime as fixtures
import bridge


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

    def test_resume_is_refused_after_the_user_switches_local_cli_and_fresh_uses_the_new_one(self):
        first = self.runtime.start(self.packet())
        initial = self.finish(first["run_id"])["snapshot"]
        upgraded = self.root / "upgraded-cli.py"
        upgraded.write_text(self.fake.read_text() + "\n# user upgrade\n")
        upgraded.chmod(0o755)
        os.environ["CLAUDE_BIN"] = str(upgraded)
        packet = self.packet(2)
        packet["correction"] = {"finding_id": "f1", "kind": "local", "reason": "check again", "attempt": 1}
        with self.assertRaisesRegex(bridge.BridgeError, "resume_cli_identity_changed.*fresh revision"):
            self.runtime.start(packet, resume_run_id=first["run_id"])
        self.assertEqual(len(self.runtime.list_runs()), 1, "a refused resume must not create a run")
        fresh = self.finish(self.runtime.start(self.packet(3))["run_id"])["snapshot"]
        self.assertEqual(fresh["status"], "reported")
        self.assertNotEqual(initial["session_id"], fresh["session_id"])
        pinned = json.loads((Path(fresh["run_dir"]) / "cli-selection.json").read_text())
        self.assertEqual(pinned["selection"]["path"], str(upgraded))

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

    def test_legacy_private_identity_descriptor_cannot_be_resumed(self):
        first = self.runtime.start(self.packet())
        initial = self.finish(first["run_id"])["snapshot"]
        path = Path(initial["run_dir"]) / "cli-selection.json"
        descriptor = json.loads(path.read_text())
        descriptor["identity_id"] = "darwin-arm64-2.1.278-deadbeefdeadbeefdead"
        descriptor["selection"] = {**descriptor["selection"], "source": "managed_native",
                                   "identity": {"id": descriptor["identity_id"]}}
        path.write_text(json.dumps(descriptor))
        correction = {**self.packet(2), "correction": {"finding_id": "f1", "kind": "local", "reason": "check", "attempt": 1}}
        with self.assertRaisesRegex(bridge.BridgeError, "resume_cli_identity_retired"):
            self.runtime.start(correction, resume_run_id=first["run_id"])
        with self.assertRaisesRegex(bridge.BridgeError, "cli_selection_retired"):
            bridge.verify_cli_descriptor({**descriptor, "contract_id": bridge.bridge_contract_id()})

    def test_missing_budget_flag_blocks_instead_of_switching_to_another_cli(self):
        store = Path(os.environ["CLAUDE_ORCHESTRATOR_CLI_ROOT"])
        store.mkdir(parents=True, exist_ok=True)
        (store / "selection.json").write_text(json.dumps({
            "schema_version": 1, "generation": 2, "active": "darwin-arm64-2.1.276-deadbeefdeadbeefdead",
            "previous": None, "history": [], "mode": "managed"}))
        limited = {**self.packet(), "budget": {"max_turns": 3}}
        final = self.finish(self.runtime.start(limited)["run_id"])["snapshot"]
        self.assertEqual(final["status"], "blocked")
        self.assertEqual(final["receipt"]["reason"], "budget_capability_unavailable")
        self.assertFalse((Path(final["run_dir"]) / "child.json").exists())
        pinned = json.loads((Path(final["run_dir"]) / "cli-selection.json").read_text())
        self.assertEqual(pinned["selection"]["path"], str(self.fake))


if __name__ == "__main__":
    unittest.main()
