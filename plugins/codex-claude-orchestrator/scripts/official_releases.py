"""Resolve Anthropic's signed official Claude Code latest release metadata (historical).

The latest-channel maintenance that used this module is retired; no production
entry imports it, so the plugin never contacts the release server or needs
GnuPG.  It stays only as the verifier referenced by retained history.

Discovery is deliberately separate from status rendering.  Callers persist a
successful result and may use that cache without network access.  A release is
returned only after the detached manifest signature verifies against the
pinned Anthropic release-key fingerprint.
"""
from __future__ import annotations

from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time
from typing import Any, Callable
from urllib.request import Request, urlopen


OFFICIAL_RELEASES = "https://downloads.claude.ai/claude-code-releases"
OFFICIAL_KEY_URL = "https://downloads.claude.ai/keys/claude-code.asc"
RELEASE_KEY_FINGERPRINT = "31DDDE24DDFAB679F42D7BD2BAA929FF1A7ECACE"
DISCOVERY_TIMEOUT_SECONDS = 20.0
_VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:-[A-Za-z0-9._-]+)?$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _response_status(response: Any) -> int:
    status = getattr(response, "status", None)
    if status is None and hasattr(response, "getcode"):
        status = response.getcode()
    if not isinstance(status, int):
        raise RuntimeError("official release metadata returned no HTTP status")
    return status


def _set_response_timeout(response: Any, seconds: float) -> None:
    """Tighten urllib's live socket to the remaining total deadline."""
    candidates = [getattr(response, "_sock", None)]
    fp = getattr(response, "fp", None)
    raw = getattr(fp, "raw", None)
    candidates.extend((getattr(fp, "_sock", None), getattr(raw, "_sock", None)))
    for candidate in candidates:
        setter = getattr(candidate, "settimeout", None)
        if callable(setter):
            setter(max(0.001, seconds))
            return


def _fetch(url: str, limit: int, opener: Callable[..., Any], timeout: float, *,
           deadline: float, monotonic: Callable[[], float]) -> bytes:
    remaining = deadline - monotonic()
    if remaining <= 0:
        raise RuntimeError("official release discovery exceeded its total deadline")
    request = Request(url, headers={"Accept": "application/octet-stream",
                                    "User-Agent": "codex-claude-orchestrator-release-discovery/1"})
    response = opener(request, timeout=min(timeout, remaining))
    manager = response if hasattr(response, "__enter__") else closing(response)
    with manager as handle:
        status = _response_status(handle)
        if status != 200:
            raise RuntimeError(f"official release metadata returned HTTP {status}")
        chunks: list[bytes] = []
        received = 0
        while received <= limit:
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise RuntimeError("official release discovery exceeded its total deadline")
            _set_response_timeout(handle, remaining)
            # HTTPResponse.read1 returns buffered/currently available bytes and
            # avoids waiting to fill 64 KiB.  BytesIO and test doubles use read.
            reader = getattr(handle, "read1", None) or handle.read
            chunk = reader(min(64 * 1024, limit + 1 - received))
            if not chunk:
                break
            chunks.append(chunk)
            received += len(chunk)
        data = b"".join(chunks)
    if monotonic() >= deadline:
        raise RuntimeError("official release discovery exceeded its total deadline")
    if len(data) > limit:
        raise RuntimeError("official release metadata exceeded its bounded size")
    return data


def _find_gpg(env: dict[str, str]) -> str | None:
    discovered = shutil.which("gpg", path=env.get("PATH"))
    if discovered:
        return discovered
    for candidate in ("/opt/homebrew/bin/gpg", "/usr/local/bin/gpg"):
        path = Path(candidate)
        if path.is_file() and os.access(path, os.X_OK):
            return candidate
    return None


def _verify_manifest(manifest: bytes, signature: bytes, key: bytes,
                     *, environ: dict[str, str] | None = None,
                     runner: Callable[..., Any] = subprocess.run,
                     deadline: float | None = None,
                     monotonic: Callable[[], float] = time.monotonic) -> None:
    env = dict(os.environ if environ is None else environ)
    gpg = _find_gpg(env)
    if not gpg:
        raise RuntimeError("GPG is required to verify the official Claude release manifest")
    with tempfile.TemporaryDirectory(prefix="claude-release-verify-") as temporary:
        root = Path(temporary)
        home = root / "gnupg"
        home.mkdir(mode=0o700)
        key_path = root / "release-key.asc"
        manifest_path = root / "manifest.json"
        signature_path = root / "manifest.json.sig"
        key_path.write_bytes(key)
        manifest_path.write_bytes(manifest)
        signature_path.write_bytes(signature)
        common = [gpg, "--no-options", "--no-autostart", "--batch", "--homedir", str(home)]
        def remaining(maximum: float) -> float:
            value = maximum if deadline is None else min(maximum, deadline - monotonic())
            if value <= 0:
                raise RuntimeError("official release discovery exceeded its total deadline")
            return value
        shown = runner([*common, "--with-colons", "--show-keys", "--fingerprint", str(key_path)],
                       text=True, capture_output=True, timeout=remaining(10), check=False)
        fingerprints = {line.split(":")[9].upper() for line in shown.stdout.splitlines()
                        if line.startswith("fpr:") and len(line.split(":")) > 9}
        if shown.returncode != 0 or RELEASE_KEY_FINGERPRINT not in fingerprints:
            raise RuntimeError("official Claude release key fingerprint did not match the pinned Anthropic key")
        imported = runner([*common, "--import", str(key_path)], text=True, capture_output=True,
                          timeout=remaining(10), check=False)
        if imported.returncode != 0:
            raise RuntimeError("official Claude release key could not be imported for verification")
        verified = runner([*common, "--status-fd=1", "--verify", str(signature_path), str(manifest_path)],
                          text=True, capture_output=True, timeout=remaining(20), check=False)
        valid = any(line.startswith("[GNUPG:] VALIDSIG ")
                    and line.split()[2].upper() == RELEASE_KEY_FINGERPRINT
                    for line in verified.stdout.splitlines())
        if verified.returncode != 0 or not valid:
            raise RuntimeError("official Claude release manifest signature verification failed")


def discover_latest(platform_name: str, *, environ: dict[str, str] | None = None,
                    opener: Callable[..., Any] | None = None,
                    runner: Callable[..., Any] = subprocess.run,
                    timeout_seconds: float = DISCOVERY_TIMEOUT_SECONDS,
                    monotonic: Callable[[], float] = time.monotonic) -> dict[str, Any]:
    """Return a signed, platform-specific target from Anthropic's latest channel."""
    if not isinstance(platform_name, str) or not re.fullmatch(r"darwin-(?:arm64|x64)", platform_name):
        raise ValueError("official latest discovery requires a supported macOS platform")
    if (not isinstance(timeout_seconds, (int, float)) or timeout_seconds <= 0
            or timeout_seconds > 60):
        raise ValueError("official latest discovery timeout is invalid")
    open_url = opener or urlopen
    deadline = monotonic() + float(timeout_seconds)
    latest_raw = _fetch(f"{OFFICIAL_RELEASES}/latest", 128, open_url, float(timeout_seconds),
                        deadline=deadline, monotonic=monotonic)
    try:
        version = latest_raw.decode("ascii").strip()
    except UnicodeDecodeError as exc:
        raise RuntimeError("official latest channel returned a non-ASCII version") from exc
    if not _VERSION.fullmatch(version):
        raise RuntimeError("official latest channel returned an invalid release version")
    manifest_url = f"{OFFICIAL_RELEASES}/{version}/manifest.json"
    manifest = _fetch(manifest_url, 1024 * 1024, open_url, float(timeout_seconds),
                      deadline=deadline, monotonic=monotonic)
    signature = _fetch(manifest_url + ".sig", 64 * 1024, open_url, float(timeout_seconds),
                       deadline=deadline, monotonic=monotonic)
    key = _fetch(OFFICIAL_KEY_URL, 256 * 1024, open_url, float(timeout_seconds),
                 deadline=deadline, monotonic=monotonic)
    _verify_manifest(manifest, signature, key, environ=environ, runner=runner,
                     deadline=deadline, monotonic=monotonic)
    try:
        parsed = json.loads(manifest.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise RuntimeError("official Claude release manifest is malformed") from exc
    if not isinstance(parsed, dict) or parsed.get("version") != version:
        raise RuntimeError("official Claude release manifest version does not match the latest channel")
    platforms = parsed.get("platforms")
    item = platforms.get(platform_name) if isinstance(platforms, dict) else None
    if not isinstance(item, dict):
        raise RuntimeError(f"official Claude release manifest has no metadata for {platform_name}")
    checksum, size, binary = item.get("checksum"), item.get("size"), item.get("binary")
    if not isinstance(checksum, str) or not _SHA256.fullmatch(checksum):
        raise RuntimeError("official Claude release manifest has an invalid platform checksum")
    if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
        raise RuntimeError("official Claude release manifest has an invalid platform size")
    if binary != "claude":
        raise RuntimeError("official Claude release manifest has an unexpected platform binary")
    if monotonic() >= deadline:
        raise RuntimeError("official release discovery exceeded its total deadline")
    return {"version": version, "platform": platform_name, "sha256": checksum,
            "size": size, "url": f"{OFFICIAL_RELEASES}/{version}/{platform_name}/claude",
            "source": "official", "scope": "official_latest",
            "manifest_url": manifest_url,
            "manifest_sha256": hashlib.sha256(manifest).hexdigest(),
            "signing_key_fingerprint": RELEASE_KEY_FINGERPRINT,
            "discovered_at": time.time()}
