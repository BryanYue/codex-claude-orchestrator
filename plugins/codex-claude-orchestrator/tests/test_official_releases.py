from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import official_releases  # noqa: E402


class Response:
    status = 200

    def __init__(self, data: bytes):
        self.data = data

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, amount: int) -> bytes:
        value, self.data = self.data[:amount], self.data[amount:]
        return value


class Clock:
    def __init__(self, values):
        self.values = list(values)
        self.last = self.values[-1]

    def __call__(self):
        if self.values:
            self.last = self.values.pop(0)
        return self.last


class OfficialReleaseTests(unittest.TestCase):
    def manifest(self, *, checksum: str = "a" * 64, size: int = 123) -> bytes:
        return json.dumps({"version": "2.1.280", "platforms": {
            "darwin-arm64": {"binary": "claude", "checksum": checksum, "size": size}
        }}).encode()

    def opener(self, manifest: bytes):
        payloads = {
            f"{official_releases.OFFICIAL_RELEASES}/latest": b"2.1.280\n",
            f"{official_releases.OFFICIAL_RELEASES}/2.1.280/manifest.json": manifest,
            f"{official_releases.OFFICIAL_RELEASES}/2.1.280/manifest.json.sig": b"signature",
            official_releases.OFFICIAL_KEY_URL: b"key",
        }
        def open_url(request, **_kwargs):
            return Response(payloads[request.full_url])
        return open_url

    def test_signed_latest_yields_exact_platform_target(self):
        with patch.object(official_releases, "_verify_manifest") as verify:
            target = official_releases.discover_latest(
                "darwin-arm64", opener=self.opener(self.manifest()))
        verify.assert_called_once()
        self.assertEqual(target["version"], "2.1.280")
        self.assertEqual(target["sha256"], "a" * 64)
        self.assertEqual(target["size"], 123)
        self.assertEqual(target["signing_key_fingerprint"], official_releases.RELEASE_KEY_FINGERPRINT)
        self.assertEqual(target["source"], "official")

    def test_missing_or_invalid_manifest_metadata_is_rejected(self):
        missing = json.dumps({"version": "2.1.280", "platforms": {}}).encode()
        with patch.object(official_releases, "_verify_manifest"), \
             self.assertRaisesRegex(RuntimeError, "no metadata"):
            official_releases.discover_latest("darwin-arm64", opener=self.opener(missing))
        with patch.object(official_releases, "_verify_manifest"), \
             self.assertRaisesRegex(RuntimeError, "invalid platform checksum"):
            official_releases.discover_latest(
                "darwin-arm64", opener=self.opener(self.manifest(checksum="bad")))

    def test_signature_failure_is_terminal_for_discovery(self):
        with patch.object(official_releases, "_verify_manifest",
                          side_effect=RuntimeError("manifest signature verification failed")), \
             self.assertRaisesRegex(RuntimeError, "signature verification failed"):
            official_releases.discover_latest(
                "darwin-arm64", opener=self.opener(self.manifest()))

    def test_gpg_fallback_and_no_agent_autostart_are_explicit(self):
        calls = []
        def runner(command, **_kwargs):
            calls.append(command)
            if "--show-keys" in command:
                stdout = f"fpr:::::::::{official_releases.RELEASE_KEY_FINGERPRINT}:\n"
            elif "--verify" in command:
                stdout = f"[GNUPG:] VALIDSIG {official_releases.RELEASE_KEY_FINGERPRINT} 0 0 0 0 0 0 0 0 0\n"
            else:
                stdout = ""
            return subprocess.CompletedProcess(command, 0, stdout, "")
        with patch.object(official_releases, "_find_gpg", return_value="/opt/homebrew/bin/gpg"):
            official_releases._verify_manifest(b"{}", b"sig", b"key", runner=runner)
        self.assertEqual(len(calls), 3)
        self.assertTrue(all("--no-autostart" in command for command in calls))
        self.assertTrue(all(command[0] == "/opt/homebrew/bin/gpg" for command in calls))

    def test_total_deadline_stops_between_bounded_reads(self):
        with self.assertRaisesRegex(RuntimeError, "total deadline"):
            official_releases.discover_latest(
                "darwin-arm64", opener=self.opener(self.manifest()), timeout_seconds=2,
                monotonic=Clock([0.0, 0.0, 0.0, 3.0]))

    def test_fetch_prefers_read1_and_tightens_live_socket_deadline(self):
        timeouts = []
        class StreamingResponse(Response):
            def __init__(self):
                super().__init__(b"")
                self.fp = SimpleNamespace(raw=SimpleNamespace(
                    _sock=SimpleNamespace(settimeout=timeouts.append)))
                self.chunks = [b"abc", b""]

            def read1(self, _amount):
                return self.chunks.pop(0)

            def read(self, _amount):
                raise AssertionError("read() should not be used when read1() exists")

        value = official_releases._fetch(
            "https://example.invalid/metadata", 16,
            lambda *_args, **_kwargs: StreamingResponse(), 20,
            deadline=10, monotonic=Clock([0, 1, 2, 3]))
        self.assertEqual(value, b"abc")
        self.assertEqual(timeouts, [9, 8])


if __name__ == "__main__":
    unittest.main()
