#!/usr/bin/env python3
"""Build a deterministic distribution from the tracked blobs of one Git commit."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tomllib
import zipfile
from pathlib import Path

SCRIPT_ROOT = Path(__file__).resolve().parent
DEFAULT_SOURCE_ROOT = SCRIPT_ROOT.parent
PLUGIN_RELATIVE = Path("plugins/codex-claude-orchestrator")
MANIFEST_RELATIVE = PLUGIN_RELATIVE / ".codex-plugin/plugin.json"
PYPROJECT_RELATIVE = PLUGIN_RELATIVE / "pyproject.toml"
UV_LOCK_RELATIVE = PLUGIN_RELATIVE / "uv.lock"
SERVER_RELATIVE = PLUGIN_RELATIVE / "scripts/server.py"
README_RELATIVE = Path("README.md")
MARKETPLACE_RELATIVE = Path(".agents/plugins/marketplace.json")
LAUNCHER_RELATIVE = PLUGIN_RELATIVE / "scripts/launch.sh"
INSTALL_COMMAND_RELATIVE = Path("Install.command")
EXECUTABLE_RELATIVE_PATHS = frozenset({INSTALL_COMMAND_RELATIVE, LAUNCHER_RELATIVE})

# Keep this list aligned with compatibility.bridge_contract_id(), without
# importing runtime code from a different installation into the builder.
CONTRACT_RELATIVE_PATHS = (
    "skills/codex-claude-orchestrator/scripts/bridge.py",
    "skills/codex-claude-orchestrator/scripts/runtime.py",
    "skills/codex-claude-orchestrator/scripts/workspace.py",
    "skills/codex-claude-orchestrator/scripts/events.py",
    "skills/codex-claude-orchestrator/scripts/named_workflow.py",
    "skills/codex-claude-orchestrator/scripts/compatibility.py",
    "scripts/cli_validation.py",
    "scripts/cli_store.py",
)

OMIT_DIR_NAMES = {".venv", "__pycache__", ".uv-cache", ".git", "dist",
                  ".pytest_cache", "coverage", ".mypy_cache", ".ruff_cache"}
ALLOWED_SUFFIXES = {".md", ".json", ".py", ".lock", ".toml", ".yaml", ".yml", ".sh", ".html", ".command", ".png"}
ALLOWED_BARE_NAMES = {".gitignore"}
VERSION_PATTERN = re.compile(r"^\d+\.\d+\.\d+$")
SERVER_VERSION_PATTERN = re.compile(r'MCPServer\(\s*["\']claude-orchestrator["\'],\s*version=["\']([^"\']+)["\']')
ZIP_EPOCH = (1980, 1, 1, 0, 0, 0)
FILE_MODE = 0o644
EXEC_MODE = 0o755


class BuildError(RuntimeError):
    """A condition that prevents a trustworthy release artifact."""


def run_git_bytes(source_root: Path, *args: str) -> bytes:
    # git -C alone does not override GIT_DIR/GIT_WORK_TREE from a caller's shell.
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    try:
        result = subprocess.run(["git", "-C", str(source_root), *args],
                                capture_output=True, timeout=30, env=environment)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise BuildError(f"git is required to build a distribution: {error}") from error
    if result.returncode:
        detail = (result.stderr or result.stdout).decode("utf-8", "replace").strip()
        raise BuildError(f"git {' '.join(args)} failed: {detail}")
    return result.stdout


def run_git(source_root: Path, *args: str) -> str:
    return run_git_bytes(source_root, *args).decode("utf-8", "replace")


def require_clean_commit(source_root: Path) -> str:
    top_level = Path(run_git(source_root, "rev-parse", "--show-toplevel").strip()).resolve()
    if top_level != source_root:
        raise BuildError(f"Source root must be the Git top-level: {top_level}")
    status = run_git(source_root, "status", "--porcelain=v1", "--untracked-files=all", "--", ".")
    if status.strip():
        raise BuildError("Source tree has uncommitted or untracked changes; commit or remove them before building.\n" + status)
    return run_git(source_root, "rev-parse", "--verify", "HEAD^{commit}").strip()


def tracked_blobs(source_root: Path, commit: str) -> tuple[dict[Path, bytes], dict[Path, str]]:
    blobs: dict[Path, bytes] = {}
    modes: dict[Path, str] = {}
    listing = run_git_bytes(source_root, "ls-tree", "-r", "-z", "--full-tree", commit)
    for entry in listing.split(b"\0"):
        if not entry:
            continue
        try:
            metadata, raw_path = entry.split(b"\t", 1)
            mode, kind, object_id = metadata.decode("ascii").split(" ")
            name = raw_path.decode("utf-8")
        except (ValueError, UnicodeDecodeError) as error:
            raise BuildError("Git tree contains an unsupported path or entry") from error
        path = Path(name)
        if path.is_absolute() or not name or any(part in {"", ".", ".."} for part in path.parts):
            raise BuildError(f"Unsafe path in Git tree: {name!r}")
        if path in {Path("FILE-SHA256.json"), Path("RELEASE-MANIFEST.json")}:
            raise BuildError(f"Generated release metadata path is already tracked: {name}")
        if mode == "120000" or kind == "commit" or mode == "160000":
            raise BuildError(f"Symlink or gitlink is not allowed in a distribution: {name}")
        if kind != "blob" or mode not in {"100644", "100755"}:
            raise BuildError(f"Unsupported Git tree entry: {name} ({mode} {kind})")
        if any(part in OMIT_DIR_NAMES for part in path.parts[:-1]) or path.suffix == ".pyc":
            continue
        if path.suffix.lower() not in ALLOWED_SUFFIXES and path.name not in ALLOWED_BARE_NAMES:
            raise BuildError(f"Unrecognized artifact type is not permitted in the distribution: {name}")
        if mode == "100755" and path not in EXECUTABLE_RELATIVE_PATHS:
            raise BuildError(f"Unexpected executable Git mode for distribution file: {name}")
        blobs[path] = run_git_bytes(source_root, "cat-file", "blob", object_id)
        modes[path] = mode
    return blobs, modes


def require_executable_git_mode(modes: dict[Path, str]) -> None:
    missing = sorted(str(path) for path in EXECUTABLE_RELATIVE_PATHS if path not in modes)
    if missing:
        raise BuildError(f"Executable scripts are not tracked in git: {missing}")
    wrong = {str(path): modes[path] for path in EXECUTABLE_RELATIVE_PATHS if modes[path] != "100755"}
    if wrong:
        raise BuildError(f"Executable scripts must have git mode 100755, found: {wrong}")


def required_bytes(blobs: dict[Path, bytes], path: Path) -> bytes:
    try:
        return blobs[path]
    except KeyError as error:
        raise BuildError(f"Required file is missing from the committed distribution: {path}") from error


def read_manifest_version(blobs: dict[Path, bytes]) -> tuple[str, str]:
    manifest = json.loads(required_bytes(blobs, MANIFEST_RELATIVE))
    full_version = manifest.get("version")
    if not isinstance(full_version, str) or not full_version:
        raise BuildError("Plugin manifest is missing a version string.")
    base = full_version.split("+", 1)[0]
    if not VERSION_PATTERN.fullmatch(base):
        raise BuildError(f"Plugin manifest base version is not a plain semantic version: {base!r}")
    return full_version, base


def require_matching_base_version(blobs: dict[Path, bytes], base: str) -> None:
    pyproject_version = tomllib.loads(required_bytes(blobs, PYPROJECT_RELATIVE).decode())["project"]["version"]
    if pyproject_version != base:
        raise BuildError(f"pyproject.toml version {pyproject_version!r} does not match manifest base version {base!r}")
    uv_lock = tomllib.loads(required_bytes(blobs, UV_LOCK_RELATIVE).decode())
    matches = [entry for entry in uv_lock.get("package", []) if entry.get("name") == "codex-claude-orchestrator"]
    if len(matches) != 1:
        raise BuildError("uv.lock does not have exactly one codex-claude-orchestrator package entry.")
    if matches[0].get("version") != base:
        raise BuildError(f"uv.lock codex-claude-orchestrator version {matches[0].get('version')!r} does not match manifest base version {base!r}")
    server = required_bytes(blobs, SERVER_RELATIVE).decode()
    server_match = SERVER_VERSION_PATTERN.search(server)
    if not server_match or server_match.group(1) != base:
        raise BuildError(f"server.py MCP version does not match manifest base version {base!r}")
    readme_heading = required_bytes(blobs, README_RELATIVE).decode().splitlines()[0]
    if not readme_heading.startswith("# ") or not readme_heading.endswith(" " + base):
        raise BuildError(f"README.md heading does not match manifest base version {base!r}")


def require_marketplace_identity(blobs: dict[Path, bytes]) -> None:
    marketplace = json.loads(required_bytes(blobs, MARKETPLACE_RELATIVE))
    if marketplace.get("name") != "codex-claude-team":
        raise BuildError(f"Marketplace identity name mismatch: {marketplace.get('name')!r}")


def contract_digest(blobs: dict[Path, bytes]) -> str:
    entries = []
    for relative in CONTRACT_RELATIVE_PATHS:
        path = PLUGIN_RELATIVE / relative
        entries.append((relative, hashlib.sha256(required_bytes(blobs, path)).hexdigest()))
    return hashlib.sha256(json.dumps(entries, sort_keys=True).encode()).hexdigest()


def exists_or_symlink(path: Path) -> bool:
    return os.path.lexists(path)


def validate_output_location(source_root: Path, source_argument: Path, output: Path) -> None:
    dist = source_root / "dist"
    if exists_or_symlink(dist) and dist.is_symlink():
        raise BuildError("Source dist/ directory must not be a symlink")
    # Check both the spelling supplied by the caller and its resolved target:
    # an in-tree symlink to an external directory must not bypass this rule.
    for candidate, root in ((output, source_argument), (output.resolve(strict=False), source_root)):
        if candidate.is_relative_to(root):
            relative = candidate.relative_to(root)
            if not relative.parts or relative.parts[0] != "dist" or len(relative.parts) != 2:
                raise BuildError("Output inside source is allowed only under the ignored dist/ directory")
            break
    else:
        return
    ignored = subprocess.run(["git", "-C", str(source_root), "check-ignore", "-q", "--", "dist/"],
                             env={k: v for k, v in os.environ.items() if not k.startswith("GIT_")},
                             capture_output=True)
    if ignored.returncode != 0:
        raise BuildError("Source dist/ directory must be Git-ignored before writing output")


def build_zip(staging: Path, archive_file, package_name: str) -> None:
    entries = sorted((path for path in staging.rglob("*") if path.is_file()),
                     key=lambda item: str(item.relative_to(staging)))
    with zipfile.ZipFile(archive_file, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in entries:
            relative = path.relative_to(staging)
            info = zipfile.ZipInfo(f"{package_name}/{relative.as_posix()}", date_time=ZIP_EPOCH)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            mode = EXEC_MODE if relative in EXECUTABLE_RELATIVE_PATHS else FILE_MODE
            info.external_attr = (mode | stat.S_IFREG) << 16
            archive.writestr(info, path.read_bytes())


def build(*, source_root: Path, output: Path | None = None) -> dict:
    source_argument = source_root.absolute()
    source_root = source_root.resolve()
    if not source_root.is_dir():
        raise BuildError(f"Source root is not a directory: {source_root}")
    commit = require_clean_commit(source_root)
    blobs, modes = tracked_blobs(source_root, commit)
    require_executable_git_mode(modes)
    full_version, base = read_manifest_version(blobs)
    require_matching_base_version(blobs, base)
    require_marketplace_identity(blobs)
    digest = contract_digest(blobs)

    if output is None:
        output = source_root / "dist" / f"codex-claude-orchestrator-macos-{base}"
    output = output.absolute()
    archive_path = output.with_name(output.name + ".zip")
    checksum_path = output.with_name(output.name + ".zip.sha256")
    staging = output.with_name(output.name + ".staging")
    validate_output_location(source_root, source_argument, output)
    for target in (output, archive_path, checksum_path, staging):
        if exists_or_symlink(target):
            raise BuildError(f"Output already exists; inspect before replacing: {target}")
    output.parent.mkdir(parents=True, exist_ok=True)

    try:
        staging.mkdir()  # exclusive; an existing directory or symlink is never reused
        manifest: dict[str, str] = {}
        for relative, content in sorted(blobs.items(), key=lambda item: item[0].as_posix()):
            target = staging / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
            target.chmod(EXEC_MODE if relative in EXECUTABLE_RELATIVE_PATHS else FILE_MODE)
            manifest[relative.as_posix()] = hashlib.sha256(content).hexdigest()
        release_manifest = {
            "schema_version": 1,
            "plugin_version": full_version,
            "base_version": base,
            "source_commit": commit,
            "contract_digest": digest,
            "file_count": len(manifest),
        }
        release_bytes = (json.dumps(release_manifest, indent=2, sort_keys=True) + "\n").encode()
        (staging / "RELEASE-MANIFEST.json").write_bytes(release_bytes)
        manifest["RELEASE-MANIFEST.json"] = hashlib.sha256(release_bytes).hexdigest()
        (staging / "FILE-SHA256.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")

        with archive_path.open("xb") as archive_file:
            build_zip(staging, archive_file, output.name)
        archive_digest = hashlib.sha256(archive_path.read_bytes()).hexdigest()
        if exists_or_symlink(output):
            raise BuildError(f"Output appeared during build; refusing to replace: {output}")
        output.mkdir()  # exclusive even if another process creates it after the check
        for child in staging.iterdir():
            child.rename(output / child.name)
        staging.rmdir()
        # The checksum is the completion marker and is written last.
        with checksum_path.open("x") as checksum_file:
            checksum_file.write(f"{archive_digest}  {archive_path.name}\n")
    except Exception as error:
        # Once a path is visible, another local process may have put its own
        # content there. Preserve every partial path for inspection instead of
        # recursively deleting a directory that might now contain foreign data.
        inspect = [str(path) for path in (staging, output, archive_path, checksum_path)
                   if exists_or_symlink(path)]
        raise BuildError(f"Could not complete distribution output: {error}. "
                         f"Inspect partial or competing paths; none were deleted: {inspect}") from error
    return {
        "directory": str(output), "archive": str(archive_path),
        "bytes": archive_path.stat().st_size, "files": len(manifest),
        "sha256": archive_digest, "version": base,
        "plugin_version": full_version, "source_commit": commit,
        "contract_digest": digest,
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="build_distribution.py",
        description="Build a reproducible distribution from the tracked blobs of a clean Git commit.")
    parser.add_argument("--source-root", type=Path, default=DEFAULT_SOURCE_ROOT,
                        help="Git repository root containing plugins/, docs/ and README.md")
    parser.add_argument("--output", type=Path, default=None,
                        help="Output directory; sibling <output>.zip and <output>.zip.sha256 are also created")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        result = build(source_root=args.source_root, output=args.output)
    except BuildError as error:
        print(str(error), file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
