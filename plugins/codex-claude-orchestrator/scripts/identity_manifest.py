"""Purpose-specific executable identity declarations; package identity is separate."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath

MANIFEST_FILE = "code-identity.json"
PURPOSES = frozenset({"contract", "startup"})


def declared_paths(raw: bytes, purpose: str) -> tuple[str, ...]:
    """Read a strict declaration without importing any declared executable code."""
    if purpose not in PURPOSES:
        raise ValueError("unknown code identity purpose")
    try:
        declaration = json.loads(raw)
    except (ValueError, UnicodeError) as exc:
        raise ValueError("code identity declaration is malformed") from exc
    if not isinstance(declaration, dict) or declaration.get("schema_version") != 1:
        raise ValueError("unsupported code identity declaration")
    purposes = declaration.get("purposes")
    paths = purposes.get(purpose) if isinstance(purposes, dict) else None
    if not isinstance(paths, list) or not paths or any(not isinstance(path, str) for path in paths):
        raise ValueError("code identity paths must be a nonempty list of strings")
    if len(paths) != len(set(paths)):
        raise ValueError("code identity declaration contains duplicate paths")
    for path in paths:
        parsed = PurePosixPath(path)
        if parsed.is_absolute() or parsed.as_posix() != path or any(part in {".", ".."} for part in parsed.parts):
            raise ValueError("code identity declaration contains an unsafe path")
    if MANIFEST_FILE not in paths or "scripts/identity_manifest.py" not in paths:
        raise ValueError("code identity must include its declaration and reader")
    return tuple(paths)


def paths(root: Path, purpose: str) -> tuple[str, ...]:
    return declared_paths((Path(root) / MANIFEST_FILE).read_bytes(), purpose)


def file_hashes(root: Path, purpose: str) -> dict[str, str]:
    root = Path(root)
    return {relative: hashlib.sha256((root / relative).read_bytes()).hexdigest()
            for relative in paths(root, purpose)}


def digest(files: dict[str, str], purpose: str) -> str:
    """Keep persisted contract and startup digest formats distinct."""
    entries = sorted(files.items())
    if purpose == "contract":
        material = json.dumps(entries, sort_keys=True).encode()
    elif purpose == "startup":
        material = json.dumps(entries, separators=(",", ":")).encode()
    else:
        raise ValueError("unknown code identity purpose")
    return hashlib.sha256(material).hexdigest()
