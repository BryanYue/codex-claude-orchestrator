import hashlib
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/codex-claude-orchestrator/scripts"))
from bridge import validate_packet, BridgeError


class ProtocolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name).resolve()
        self.git("init", "-q")
        self.git("config", "user.name", "Fixture")
        self.git("config", "user.email", "fixture@example.invalid")
        for name in ("protocol.md", "delta.md", "dispatch.md", "PROGRESS.md"):
            (self.repo / name).write_text(name + " original\n")
        self.git("add", ".")
        self.git("commit", "-qm", "fixture baseline")
        self.base = self.git("rev-parse", "HEAD")
        self.packet = {"task_id": "formal", "revision": 1, "role": "review", "cwd": str(self.repo),
            "objective": "Review", "requirement_sources": [], "constraints": ["Read only"],
            "acceptance": ["Evidence"], "protected_files": [], "owned_files": [], "model": "sonnet", "effort": "low",
            "protocol_binding": {"baseline_commit": self.base, "approval_source": "Fixture approval record",
                "protocol_path": "protocol.md", "delta_path": "delta.md", "dispatch_path": "dispatch.md", "progress_path": "PROGRESS.md",
                "protocol_sha256": self.digest("protocol.md"), "delta_sha256": self.digest("delta.md")}}

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.repo), *args], capture_output=True, text=True, check=True).stdout.strip()

    def digest(self, name):
        return hashlib.sha256((self.repo / name).read_bytes()).hexdigest()

    def test_approved_identity_survives_new_head_and_dispatch_changes(self):
        (self.repo / "dispatch.md").write_text("changed command, same acceptance\n")
        self.git("add", "dispatch.md")
        self.git("commit", "-qm", "adjust method")
        result = validate_packet(self.packet)
        self.assertEqual(result["protocol_binding"]["baseline_commit"], self.base)
        self.assertIn(str(self.repo / "delta.md"), result["requirement_sources"])
        self.assertNotIn(str(self.repo / "dispatch.md"), result["requirement_sources"])

    def test_working_drift_and_false_baseline_hash_fail(self):
        (self.repo / "delta.md").write_text("weakened acceptance")
        with self.assertRaisesRegex(BridgeError, "Working delta differs"):
            validate_packet(self.packet)
        self.packet["protocol_binding"]["delta_sha256"] = self.digest("delta.md")
        with self.assertRaisesRegex(BridgeError, "approved baseline/hash"):
            validate_packet(self.packet)

    def test_approval_paths_and_write_ownership_fail_closed(self):
        self.packet["protocol_binding"]["approval_source"] = ""
        with self.assertRaises(BridgeError): validate_packet(self.packet)
        self.packet["protocol_binding"]["approval_source"] = "Fixture"
        self.packet["protocol_binding"]["progress_path"] = "../outside"
        with self.assertRaises(BridgeError): validate_packet(self.packet)
        self.packet["protocol_binding"]["progress_path"] = "PROGRESS.md"
        self.packet.update(role="implement", owned_files=["delta.md"])
        with self.assertRaisesRegex(BridgeError, "cannot be owned_files"):
            validate_packet(self.packet)

    def test_derived_protocol_source_rejects_case_alias_write_ownership(self):
        self.packet.update(role="implement", owned_files=["DeLtA.md"])
        with self.assertRaisesRegex(BridgeError, "cannot be owned_files"):
            validate_packet(self.packet)

    def test_plain_supervised_packet_does_not_require_formal_plan(self):
        del self.packet["protocol_binding"]
        self.assertEqual(validate_packet(self.packet)["task_id"], "formal")


if __name__ == "__main__":
    unittest.main()
