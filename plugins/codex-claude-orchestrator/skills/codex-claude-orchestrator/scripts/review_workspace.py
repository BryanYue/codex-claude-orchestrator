"""Independent Git review copies with an inherited macOS write boundary.

This is not a whole-machine, credential, or network sandbox. Only the original
repository, its Git metadata, declared source files and workspace identity are
write-protected for the launched process and its descendants. ``sandbox-exec``
is Apple's deprecated local facility; unsupported/unavailable hosts fail closed.
Ignored files are not imported, symlinks are never followed during copying, and
submodules or links outside the copy boundary require a different review lane.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
from typing import Any, Sequence


class ReviewWorkspaceError(RuntimeError):
    pass


class ProtectionUnavailable(ReviewWorkspaceError):
    pass


METADATA_NAME = "review-workspace.json"
COPY_NAME = "review-workspace"
_GIT = ["git", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false", "-c", "init.templateDir="]


def _git(cwd: Path, *args: str, input_data: bytes | None = None) -> bytes:
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_TERMINAL_PROMPT": "0"})
    try:
        completed = subprocess.run(_GIT + list(args), cwd=cwd, env=env, input=input_data, capture_output=True, check=False)
    except OSError as exc:
        raise ReviewWorkspaceError(f"Git is unavailable: {exc}") from exc
    if completed.returncode:
        raise ReviewWorkspaceError(f"Git review workspace failed: {completed.stderr.decode('utf-8', 'replace').strip()}")
    return completed.stdout


def _inside(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _canonical(raw: Path | str, *, exists: bool = True) -> Path:
    try:
        return Path(raw).resolve(strict=exists)
    except (OSError, RuntimeError) as exc:
        raise ReviewWorkspaceError(f"Cannot resolve review path: {raw}") from exc


def _digest(value: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode()).hexdigest()


def _source_file(source: Path, root: Path) -> None:
    """Do not traverse a changed directory link or import an external link."""
    relative = source.relative_to(root)
    cursor = root
    for component in relative.parts[:-1]:
        cursor /= component
        if cursor.is_symlink():
            raise ReviewWorkspaceError(f"Symlink directory in review inputs: {cursor}")
    if source.is_symlink():
        target = Path(os.readlink(source))
        if target.is_absolute() or not _inside(_canonical(source.parent / target, exists=False), root):
            raise ReviewWorkspaceError(f"External or absolute symlink in review inputs: {source}")
    elif source.exists():
        info = source.stat()
        if not stat.S_ISREG(info.st_mode):
            raise ReviewWorkspaceError(f"Non-file review input (submodules are unsupported): {source}")
        if info.st_nlink != 1:
            raise ReviewWorkspaceError(f"Hardlinked source file cannot be path-protected: {source}")


def _validate_protected_tree(root: Path, *, git_metadata: bool = False) -> None:
    """Stat entries without importing ignored state or following directory links.

    Existing external hardlink aliases could bypass a path-based deny rule.
    Refuse them even in ignored source state and Git metadata. Metadata symlink
    targets outside the Git tree are similarly not covered by its path filter.
    """
    def walk_error(error: OSError) -> None:
        raise ReviewWorkspaceError(f"Cannot establish source write boundary: {error}") from error
    for directory, dirs, files in os.walk(root, followlinks=False, onerror=walk_error):
        for name in dirs + files:
            path = Path(directory) / name
            try:
                info = path.lstat()
                if stat.S_ISREG(info.st_mode) and info.st_nlink != 1:
                    raise ReviewWorkspaceError(f"Hardlinked source file cannot be path-protected: {path}")
                if git_metadata and stat.S_ISLNK(info.st_mode) and not _inside(_canonical(path), root):
                    raise ReviewWorkspaceError(f"External symlink in original Git metadata: {path}")
            except OSError as exc:
                raise ReviewWorkspaceError(f"Cannot establish source write boundary: {path}") from exc


def prepare_review_workspace(source_cwd: Path | str, run_dir: Path | str,
                             requirement_sources: Sequence[Path | str] = ()) -> dict[str, Any]:
    """Freeze current tracked/nonignored files over an independent HEAD clone.

    ``run_dir`` must already exist outside the source tree. Repeated calls load
    the same copy, keeping reviewer edits for correction/resume. Source state is
    never reset or committed. The copy has no remote, alternates or shared inode.
    """
    cwd = _canonical(source_cwd)
    run = _canonical(run_dir)
    if not cwd.is_dir() or not run.is_dir():
        raise ReviewWorkspaceError("Source cwd and run_dir must be existing directories")
    source = _canonical(os.fsdecode(_git(cwd, "rev-parse", "--show-toplevel").rstrip(b"\n")))
    if _inside(run, source) or _inside(source, run):
        raise ReviewWorkspaceError("Review run directory must not overlap the source repository")
    requested_sources = sorted({str(_canonical(Path(raw) if Path(raw).is_absolute() else cwd / raw))
                                for raw in requirement_sources})
    for raw in requested_sources:
        path = Path(raw)
        if not path.is_file() or path.stat().st_nlink != 1:
            raise ReviewWorkspaceError(f"Requirement source must be a non-hardlinked regular file: {path}")
        if _inside(path, run):
            raise ReviewWorkspaceError("Requirement source overlaps the writable review run directory")
    metadata_path = run / METADATA_NAME
    if metadata_path.exists():
        existing = load_review_workspace(run)
        if existing["source_root"] != str(source) or existing["source_cwd"] != str(cwd) or existing["requirement_sources"] != requested_sources:
            raise ReviewWorkspaceError("Existing review copy belongs to different source inputs")
        return existing
    destination = run / COPY_NAME
    if destination.exists() or destination.is_symlink():
        raise ReviewWorkspaceError("Unowned review workspace path already exists")
    git_dirs = sorted({str(_canonical(os.fsdecode(_git(cwd, "rev-parse", "--path-format=absolute", option).rstrip(b"\n"))))
                       for option in ("--git-dir", "--git-common-dir")})
    _validate_protected_tree(source)
    for raw in git_dirs:
        _validate_protected_tree(Path(raw), git_metadata=True)
        alternates = Path(raw) / "objects" / "info" / "alternates"
        if alternates.exists() or alternates.is_symlink():
            raise ReviewWorkspaceError(f"Original Git alternates are unsupported; external object paths are not protected: {alternates}")
    staged_entries = _git(source, "ls-files", "--stage", "-z").split(b"\0")
    if any(entry.startswith(b"160000 ") for entry in staged_entries):
        raise ReviewWorkspaceError("Review submodules are unsupported")
    if any(entry and entry.split(b"\t", 1)[0].split()[-1] != b"0" for entry in staged_entries):
        raise ReviewWorkspaceError("Review of an unmerged Git index is unsupported; use strict review until conflicts are resolved")
    index_flags = _git(source, "ls-files", "-v", "-z").split(b"\0")
    if any(entry and (entry.startswith(b"S ") or b"a" <= entry[:1] <= b"z") for entry in index_flags):
        raise ReviewWorkspaceError("Sparse/skip-worktree or assume-unchanged index entries are unsupported; use strict review")
    tracked = {os.fsdecode(raw) for raw in _git(source, "ls-files", "--cached", "-z").split(b"\0") if raw}
    untracked = {os.fsdecode(raw) for raw in _git(source, "ls-files", "--others", "--exclude-standard", "-z").split(b"\0") if raw}
    sensitive_untracked = sorted(name for name in untracked if any(
        part.lower() in {".claude", ".codex"} or part.lower() == ".env" or part.lower().startswith(".env.")
        for part in Path(name).parts))
    if sensitive_untracked:
        raise ReviewWorkspaceError("Sensitive untracked review inputs were not copied (.env, .claude, .codex); "
                                   "explicitly track authorized project files or ignore private state: " + ", ".join(sensitive_untracked))
    files = tracked | untracked
    # Validate all inputs before producing a partial copy or reading their bytes.
    for name in files:
        relative = Path(name)
        if relative.is_absolute() or ".." in relative.parts or relative.parts[0] == ".git":
            raise ReviewWorkspaceError(f"Unsafe Git input path: {name}")
        _source_file(source / relative, source)
    head = _git(source, "rev-parse", "HEAD").decode("ascii").strip()
    staged_diff = _git(source, "diff", "--cached", "--binary", "--full-index", "--no-ext-diff", "--no-textconv", "--no-renames", head, "--")
    staged_names = _git(source, "diff", "--cached", "--name-only", "-z", "--no-renames", head, "--")
    staged_input_count = len([name for name in staged_names.split(b"\0") if name])
    try:
        _git(run, "clone", "--no-local", "--no-hardlinks", "--no-checkout", "--", str(source), str(destination))
        _git(destination, "remote", "remove", "origin")
        # Populate only the index: checkout could execute smudge filters from
        # Git configuration before the provider's protected process starts.
        _git(destination, "read-tree", head)
        if staged_diff:
            # Apply only to the independent index. Working-tree bytes below may
            # intentionally differ from staged blobs, including binary files.
            _git(destination, "apply", "--cached", "--binary", "--whitespace=nowarn", "-", input_data=staged_diff)
        # Remove HEAD's file tree while retaining independent Git history/index.
        for child in destination.iterdir():
            if child.name == ".git":
                continue
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child)
            else:
                child.unlink()
        for name in sorted(files):
            original, target = source / name, destination / name
            _source_file(original, source)
            if not original.exists() and not original.is_symlink():
                continue  # A tracked deletion must remain deleted.
            target.parent.mkdir(parents=True, exist_ok=True)
            if original.is_symlink():
                target.symlink_to(os.readlink(original))
            else:
                shutil.copyfile(original, target)
                target.chmod(stat.S_IMODE(original.stat().st_mode))
        review_cwd = destination / cwd.relative_to(source)
        review_cwd.mkdir(parents=True, exist_ok=True)
        material = {"version": 1, "workspace_root": str(destination), "cwd": str(review_cwd),
                    "source_root": str(source), "source_cwd": str(cwd), "source_head": head,
                    "staged_input_count": staged_input_count, "staged_diff_sha256": hashlib.sha256(staged_diff).hexdigest(),
                    "source_git_dirs": git_dirs, "requirement_sources": requested_sources,
                    "protected_paths": sorted({str(source), *git_dirs, *requested_sources, str(metadata_path)}),
                    "metadata_path": str(metadata_path), "protection": "macos-sandbox-exec-source-write-deny",
                    "scope": "source writes only; credentials, other local paths and network are not isolated"}
        envelope = {"identity": material, "sha256": _digest(material)}
        with metadata_path.open("x", encoding="utf-8") as stream:
            json.dump(envelope, stream, ensure_ascii=True, indent=2)
            stream.write("\n")
        return material
    except BaseException:
        # Only this fresh, owned directory is removed on preparation failure.
        if destination.exists() and not destination.is_symlink():
            shutil.rmtree(destination)
        raise


def load_review_workspace(run_dir: Path | str) -> dict[str, Any]:
    """Validate persisted identity without refreshing/overwriting reviewer edits."""
    run = _canonical(run_dir)
    metadata_path = run / METADATA_NAME
    try:
        if metadata_path.is_symlink():
            raise ReviewWorkspaceError("Review metadata must not be a symlink")
        envelope = json.loads(metadata_path.read_text(encoding="utf-8"))
        identity = envelope["identity"]
        if not isinstance(identity, dict) or envelope["sha256"] != _digest(identity):
            raise ReviewWorkspaceError("Review workspace identity digest mismatch")
        if identity["version"] != 1 or identity["metadata_path"] != str(metadata_path):
            raise ReviewWorkspaceError("Review workspace identity location/version mismatch")
        destination = run / COPY_NAME
        if identity["workspace_root"] != str(destination) or destination.is_symlink() or not destination.is_dir():
            raise ReviewWorkspaceError("Review workspace copy is missing or redirected")
        source, cwd = Path(identity["source_root"]), Path(identity["source_cwd"])
        expected_cwd = destination / cwd.relative_to(source)
        if identity["cwd"] != str(expected_cwd) or _canonical(expected_cwd) != expected_cwd:
            raise ReviewWorkspaceError("Review cwd is redirected")
        paths = identity["protected_paths"]
        expected_paths = sorted({str(source), *identity["source_git_dirs"], *identity["requirement_sources"], str(metadata_path)})
        if paths != expected_paths or _inside(run, source) or _inside(source, run):
            raise ReviewWorkspaceError("Review protected path identity mismatch")
        for raw in paths:
            path = Path(raw)
            if not path.is_absolute() or _canonical(path) != path:
                raise ReviewWorkspaceError("Review protected path is redirected")
        if (destination / ".git").is_symlink() or not (destination / ".git").is_dir():
            raise ReviewWorkspaceError("Review Git metadata is redirected")
        if (destination / ".git" / "objects" / "info" / "alternates").exists():
            raise ReviewWorkspaceError("Review copy must not use shared Git objects")
        return identity
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ReviewWorkspaceError(f"Invalid or unavailable review workspace identity: {exc}") from exc


def protected_command(argv: Sequence[str], metadata: dict[str, Any],
                      extra_protected: Sequence[Path | str] = ()) -> list[str]:
    """Wrap a command; protect trusted run files separately from writable logs.

    Additional paths may name future control files (creation is denied too) or
    an immutable plugin code directory. Only their parent entries are denied,
    so sibling activity journals and the working copy remain writable. Select
    declared code/config files and source directories rather than the complete
    installation root: runtime environments such as .venv contain ordinary
    external interpreter links and are outside this code-protection contract.
    """
    if sys.platform != "darwin" or not Path("/usr/bin/sandbox-exec").is_file():
        raise ProtectionUnavailable("Isolated writable review requires macOS sandbox-exec; use strict review on this host")
    if not argv or not argv[0] or not all(isinstance(item, str) for item in argv):
        raise ReviewWorkspaceError("Review command must be a non-empty argument array")
    trusted = load_review_workspace(Path(metadata["metadata_path"]).parent)
    if trusted != metadata:
        raise ReviewWorkspaceError("Review command identity does not match persisted copy")
    extra_paths: list[str] = []
    for raw in extra_protected:
        if not Path(raw).is_absolute():
            raise ReviewWorkspaceError("Additional protected paths must be absolute")
        path = _canonical(raw, exists=False)
        if path.is_file() and path.stat().st_nlink != 1:
            raise ReviewWorkspaceError(f"Hardlinked protected control file cannot be path-protected: {path}")
        if path.is_dir():
            _validate_protected_tree(path, git_metadata=True)
        extra_paths.append(str(path))
    filters: set[tuple[str, str]] = set()
    for raw in trusted["protected_paths"] + extra_paths:
        path = Path(raw)
        filters.add(("subpath", str(path)))
        # Parent rename/chmod would otherwise move a protected tree out of its
        # path filter. Literals deny changing parents without denying siblings.
        filters.update(("literal", str(parent)) for parent in path.parents if parent != Path("/"))
    if any(any(ord(character) < 32 for character in path) for _, path in filters):
        raise ReviewWorkspaceError("Control characters in protected paths are unsupported")
    expressions = " ".join(f"({kind} {json.dumps(path, ensure_ascii=False)})" for kind, path in sorted(filters))
    profile = f"(version 1)(allow default)(deny file-write* {expressions})"
    return ["/usr/bin/sandbox-exec", "-p", profile, *argv]


def cleanup_review_workspace(run_dir: Path | str, *, terminal: bool, explicit: bool, status: str) -> bool:
    """Explicit terminal cleanup only; uncertain/returned copies remain usable.

    The caller must establish that no child or descendant still owns the copy.
    This function does not infer process termination from a status string.
    """
    if not terminal or not explicit or status not in {"accepted", "rejected", "failed", "cancelled", "blocked", "completed_by_codex"}:
        return False
    metadata = load_review_workspace(run_dir)
    shutil.rmtree(Path(metadata["workspace_root"]))
    # Keep identity as cleanup evidence; future resume fails rather than silently
    # making a new workspace and discarding the original reviewer changes.
    return True


def install_bundled_workflow(metadata: dict[str, Any]) -> dict[str, str]:
    """Make the optional multi-dimension review available only in the owned copy."""
    root = Path(metadata["workspace_root"])
    directory = root / ".claude" / "workflows"
    if not directory.resolve().is_relative_to(root):
        raise ReviewWorkspaceError("Workflow directory escapes the review copy")
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / "codex-full-review.js"
    source = Path(__file__).resolve().parents[3] / "assets" / "review-workflow.js"
    data = source.read_bytes()
    if target.is_symlink() or (target.exists() and target.read_bytes() != data):
        raise ReviewWorkspaceError("Review copy already contains a different codex-full-review workflow")
    if not target.exists():
        with target.open("xb") as handle:
            handle.write(data)
    return {"name": "codex-full-review", "path": str(target), "sha256": hashlib.sha256(data).hexdigest()}
