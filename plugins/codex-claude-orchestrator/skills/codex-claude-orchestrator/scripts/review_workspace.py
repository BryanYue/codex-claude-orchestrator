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
import time
from typing import Any, Sequence

PLUGIN_SCRIPTS = Path(__file__).resolve().parents[3] / "scripts"
if str(PLUGIN_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(PLUGIN_SCRIPTS))
import trusted_git


class ReviewWorkspaceError(RuntimeError):
    pass


class ProtectionUnavailable(ReviewWorkspaceError):
    pass


METADATA_NAME = "review-workspace.json"
COPY_NAME = "review-workspace"
_GIT = [trusted_git.GIT, "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false", "-c", "init.templateDir="]
GIT_TIMEOUT_SECONDS = 30
TREE_SCAN_TIMEOUT_SECONDS = 60


def _git(cwd: Path, *args: str, input_data: bytes | None = None) -> bytes:
    try:
        completed = trusted_git.run(cwd, *args, input=input_data, check=False, timeout=GIT_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired as exc:
        raise ReviewWorkspaceError(f"Git review workspace command timed out after {GIT_TIMEOUT_SECONDS}s: {args[0] if args else 'git'}") from exc
    except OSError as exc:
        raise ReviewWorkspaceError(f"Git is unavailable: {exc}") from exc
    if completed.returncode:
        raise ReviewWorkspaceError(f"Git review workspace failed: {completed.stderr.decode('utf-8', 'replace').strip()}")
    return completed.stdout


def _global_excludes(cwd: Path) -> str | None:
    """Read just the user's ignore path; never enable arbitrary global config."""
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0"})
    # An explicitly selected global file is still only read as configuration.
    if os.environ.get("GIT_CONFIG_GLOBAL"):
        env["GIT_CONFIG_GLOBAL"] = os.environ["GIT_CONFIG_GLOBAL"]
    try:
        result = subprocess.run(_GIT + ["config", "--includes", "--path", "--get", "core.excludesFile"],
                                cwd=cwd, env=env, capture_output=True, timeout=GIT_TIMEOUT_SECONDS)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ReviewWorkspaceError(f"Cannot read global Git ignore configuration: {exc}") from exc
    if result.returncode == 1:
        return None
    if result.returncode:
        raise ReviewWorkspaceError("Cannot read global Git ignore configuration: " + result.stderr.decode("utf-8", "replace").strip())
    return os.fsdecode(result.stdout.rstrip(b"\n"))


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


def _validate_protected_trees(roots: Sequence[Path], *, git_metadata: bool = False,
                              metadata_roots: Sequence[Path] = (), excluded_paths: Sequence[Path] = ()) -> None:
    """Stat entries without importing ignored state or following directory links.

    Existing external hardlink aliases could bypass a path-based deny rule.
    Refuse them even in ignored source state and Git metadata. Metadata symlink
    targets outside the Git tree are similarly not covered by its path filter.
    """
    def walk_error(error: OSError) -> None:
        raise ReviewWorkspaceError(f"Cannot establish source write boundary: {error}") from error
    deadline = time.monotonic() + TREE_SCAN_TIMEOUT_SECONDS
    seen: set[Path] = set()
    aliases: dict[tuple[int, int], tuple[int, set[Path]]] = {}
    for root in roots:
        if any(_inside(root, path) for path in excluded_paths):
            continue
        for directory, dirs, files in os.walk(root, followlinks=False, onerror=walk_error):
            if time.monotonic() > deadline:
                raise ReviewWorkspaceError("Source write-boundary scan timed out; use strict review for this tree")
            dirs[:] = [name for name in dirs if not any(_inside(Path(directory) / name, path) for path in excluded_paths)]
            for name in dirs + files:
                if len(seen) % 256 == 0 and time.monotonic() > deadline:
                    raise ReviewWorkspaceError("Source write-boundary scan timed out; use strict review for this tree")
                path = Path(directory) / name
                if any(_inside(path, item) for item in excluded_paths):
                    continue
                if path in seen:
                    continue
                seen.add(path)
                try:
                    info = path.lstat()
                    if stat.S_ISREG(info.st_mode) and info.st_nlink != 1:
                        key = (info.st_dev, info.st_ino)
                        count, paths = aliases.setdefault(key, (info.st_nlink, set()))
                        if count != info.st_nlink:
                            raise ReviewWorkspaceError(f"Hardlink changed during source boundary scan: {path}")
                        paths.add(path)
                    if (git_metadata or any(_inside(path, item) for item in metadata_roots)) and stat.S_ISLNK(info.st_mode) and not any(_inside(_canonical(path), item) for item in roots):
                        raise ReviewWorkspaceError(f"External symlink in original Git metadata: {path}")
                except OSError as exc:
                    raise ReviewWorkspaceError(f"Cannot establish source write boundary: {path}") from exc
    for count, paths in aliases.values():
        if len(paths) != count:
            path = min(paths)
            raise ReviewWorkspaceError(f"Hardlinked source file has aliases outside protected trees ({len(paths)}/{count} protected): {path}; "
                                       "use strict review or independently clone/copy the source without external hardlinks")


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
    # Git reports the actual prefix even when a case-insensitive filesystem
    # accepted a differently cased spelling of cwd.
    prefix = Path(os.fsdecode(_git(cwd, "rev-parse", "--show-prefix").rstrip(b"\n")))
    if prefix.is_absolute() or ".." in prefix.parts:
        raise ReviewWorkspaceError("Git source cwd prefix escapes the repository")
    cwd = source / prefix
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
    worktrees = sorted({str(_canonical(os.fsdecode(entry[len(b"worktree "):])))
                        for entry in _git(source, "worktree", "list", "--porcelain", "-z").split(b"\0")
                        if entry.startswith(b"worktree ")})
    if any(_inside(run, Path(raw)) or _inside(Path(raw), run) for raw in worktrees):
        raise ReviewWorkspaceError("Review run directory must not overlap any source worktree")
    _validate_protected_trees([Path(raw) for raw in sorted({*worktrees, *git_dirs})],
                              metadata_roots=[Path(raw) for raw in git_dirs])
    for raw in git_dirs:
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
    global_excludes = _global_excludes(source)
    ignore_args = ("-c", f"core.excludesFile={global_excludes}") if global_excludes is not None else ()
    untracked = {os.fsdecode(raw) for raw in _git(source, *ignore_args, "ls-files", "--others", "--exclude-standard", "-z").split(b"\0") if raw}
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
    # Git's public ITA switch reveals entries otherwise omitted from cached
    # diffs. Recreate them only after working-tree files have been copied.
    visible_ita = {raw for raw in _git(source, "diff", "--cached", "--ita-visible-in-index", "--name-only", "-z", head, "--").split(b"\0") if raw}
    hidden_ita = {raw for raw in _git(source, "diff", "--cached", "--ita-invisible-in-index", "--name-only", "-z", head, "--").split(b"\0") if raw}
    intent_to_add = sorted(visible_ita - hidden_ita)
    try:
        _git(run, "clone", "--no-local", "--no-hardlinks", "--no-checkout", "--", str(source), str(destination))
        _git(destination, "remote", "remove", "origin")
        # Only the ignore path is inherited; hooks, filters and other user's
        # executable Git configuration never become part of the copy.
        if global_excludes is not None:
            _git(destination, "config", "core.excludesFile", global_excludes)
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
        if intent_to_add:
            absent_ita = []
            modes = {entry.split(b"\t", 1)[1]: entry.split(b" ", 1)[0]
                     for entry in staged_entries if entry}
            for name in intent_to_add:
                target = destination / os.fsdecode(name)
                if not target.exists() and not target.is_symlink():
                    # An ITA entry may have been deleted after add -N. Git
                    # needs a temporary leaf to recreate its index flag/mode.
                    target.parent.mkdir(parents=True, exist_ok=True)
                    if modes[name] == b"120000":
                        target.symlink_to("missing-ita-target")
                    else:
                        target.touch(exist_ok=False)
                        target.chmod(0o755 if modes[name] == b"100755" else 0o644)
                    absent_ita.append(target)
            _git(destination, "add", "--intent-to-add", "--", *(os.fsdecode(name) for name in intent_to_add))
            for target in absent_ita:
                target.unlink()
        review_cwd = destination / cwd.relative_to(source)
        review_cwd.mkdir(parents=True, exist_ok=True)
        artifact_directory = destination / ".codex-review"
        suffix = hashlib.sha256(str(run).encode()).hexdigest()[:12]
        while artifact_directory.exists() or artifact_directory.is_symlink() or any(
                name == artifact_directory.name or name.startswith(artifact_directory.name + "/") for name in files):
            artifact_directory = artifact_directory.with_name(artifact_directory.name + "-" + suffix)
        material = {"version": 1, "workspace_root": str(destination), "cwd": str(review_cwd),
                    "source_root": str(source), "source_cwd": str(cwd), "source_head": head,
                    "staged_input_count": staged_input_count, "staged_diff_sha256": hashlib.sha256(staged_diff).hexdigest(),
                    "source_git_dirs": git_dirs, "source_worktrees": worktrees, "requirement_sources": requested_sources,
                    "artifact_directory": str(artifact_directory),
                    "protected_paths": sorted({str(source), *worktrees, *git_dirs, *requested_sources, str(metadata_path)}),
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
        expected_paths = sorted({str(source), *identity.get("source_worktrees", []), *identity["source_git_dirs"],
                                 *identity["requirement_sources"], str(metadata_path)})
        if paths != expected_paths or _inside(run, source) or _inside(source, run):
            raise ReviewWorkspaceError("Review protected path identity mismatch")
        for raw in paths:
            path = Path(raw)
            if not path.is_absolute() or _canonical(path) != path:
                raise ReviewWorkspaceError("Review protected path is redirected")
        if (destination / ".git").is_symlink() or not (destination / ".git").is_dir():
            raise ReviewWorkspaceError("Review Git metadata is redirected")
        alternates = destination / ".git" / "objects" / "info" / "alternates"
        if alternates.exists() or alternates.is_symlink():
            raise ReviewWorkspaceError("Review copy must not use shared Git objects")
        if "artifact_directory" in identity:
            artifact = Path(identity["artifact_directory"])
            if not artifact.is_absolute() or artifact.parent != destination or _canonical(artifact, exists=False) != artifact:
                raise ReviewWorkspaceError("Review artifact directory is redirected")
        return identity
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ReviewWorkspaceError(f"Invalid or unavailable review workspace identity: {exc}") from exc


def protected_command(argv: Sequence[str], metadata: dict[str, Any],
                      extra_protected: Sequence[Path | str] = (), *,
                      writable_exceptions: Sequence[Path | str] = ()) -> list[str]:
    """Wrap a command; protect trusted run files separately from writable logs.

    Writable exceptions apply only inside additional protected directories,
    never to original source or identity paths. Existing directories grant
    descendant writes; other paths grant writes to that single file. Root and
    ancestor entries stay immutable so a reviewer cannot redirect a writable
    tree, control parent, or the copy itself by renaming it.
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
        extra_paths.append(str(path))
    exceptions: list[tuple[str, str]] = []
    for raw in writable_exceptions:
        path = Path(raw)
        if not path.is_absolute() or _canonical(path, exists=False) != path or path.is_symlink():
            raise ReviewWorkspaceError("Writable exception must be an absolute, non-redirected path")
        if not any(_inside(path, Path(parent)) and path != Path(parent) for parent in extra_paths):
            raise ReviewWorkspaceError("Writable exception must be strictly inside an additional protected directory")
        if any(_inside(path, Path(item)) or _inside(Path(item), path) for item in trusted["protected_paths"]):
            raise ReviewWorkspaceError("Writable exception overlaps original source or trusted identity")
        if path.exists() and not path.is_dir() and (not path.is_file() or path.stat().st_nlink != 1):
            raise ReviewWorkspaceError("Writable file exception must be a non-hardlinked regular file")
        kind = "subpath" if path.is_dir() else "literal"
        exceptions.append((kind, str(path)))
    # An alias in the writable copy is not protected even when its enclosing
    # state root is denied. Count only aliases inside the effective deny area.
    _validate_protected_trees([Path(raw) for raw in extra_paths if Path(raw).is_dir()],
                              excluded_paths=[Path(raw) for _, raw in exceptions])
    filters: set[tuple[str, str]] = set()
    for raw in trusted["protected_paths"] + extra_paths:
        path = Path(raw)
        filters.add(("subpath", str(path)))
        # Parent rename/chmod would otherwise move a protected tree out of its
        # path filter. Literals deny changing parents without denying siblings.
        filters.update(("literal", str(parent)) for parent in path.parents if parent != Path("/"))
    # Protect the owned root even when its descendants are writable; this also
    # prevents post-run report capture or resume from following a replacement.
    filters.add(("literal", trusted["workspace_root"]))
    for kind, raw in exceptions:
        if kind == "subpath":
            filters.add(("literal", raw))
        filters.update(("literal", str(parent)) for parent in Path(raw).parents if parent != Path("/"))
    if any(any(ord(character) < 32 for character in path) for _, path in filters | set(exceptions)):
        raise ReviewWorkspaceError("Control characters in protected paths are unsupported")
    expressions_list = []
    for kind, path in sorted(filters):
        expression = f"({kind} {json.dumps(path, ensure_ascii=False)})"
        # Every overlapping broad deny gets the same narrow exclusions. SBPL
        # deny rules take precedence, so a later allow cannot safely undo them.
        if kind == "subpath" and path in extra_paths and exceptions:
            exclusions = " ".join(f"(require-not ({mode} {json.dumps(raw, ensure_ascii=False)}))"
                                  for mode, raw in exceptions)
            expression = f"(require-all {expression} {exclusions})"
        expressions_list.append(expression)
    expressions = " ".join(expressions_list)
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
    if load_review_workspace(Path(metadata["metadata_path"]).parent) != metadata:
        raise ReviewWorkspaceError("Workflow identity does not match persisted copy")
    root = Path(metadata["workspace_root"])
    directory = root / ".claude" / "workflows"
    if not directory.resolve().is_relative_to(root) or any(path.is_symlink() for path in (directory, directory.parent)):
        raise ReviewWorkspaceError("Workflow directory escapes the review copy")
    directory.mkdir(parents=True, exist_ok=True)
    source = Path(__file__).resolve().parents[3] / "assets" / "review-workflow.js"
    data = source.read_bytes()
    name = "codex-full-review"
    tracked = {os.fsdecode(raw) for raw in _git(root, "ls-files", "-z").split(b"\0") if raw}
    # A source workflow with the reserved name is legitimate project input.
    # Choose a separate exported name and never overwrite or hide that input.
    target = directory / (name + ".js")
    suffix = hashlib.sha256(metadata["metadata_path"].encode()).hexdigest()[:12]
    while (str(target.relative_to(root)) in tracked or target.is_symlink() or
           (target.exists() and (not target.is_file() or target.read_bytes() != data))):
        name += "-" + suffix
        target = directory / (name + ".js")
        data = source.read_bytes().replace(b'name: "codex-full-review"', f'name: "{name}"'.encode(), 1)
    if not target.exists():
        with target.open("xb") as handle:
            handle.write(data)
    exclude = root / ".git" / "info" / "exclude"
    if exclude.parent.is_symlink():
        raise ReviewWorkspaceError("Review Git exclude directory is redirected")
    exclude.parent.mkdir(exist_ok=True)
    # Trust no reviewer-controlled final component on a resumed copy.
    fd = os.open(exclude, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ReviewWorkspaceError("Review Git exclude file must be independent and regular")
        artifact = Path(metadata.get("artifact_directory", str(root / ".codex-review")))
        entries = "\n/" + str(artifact.relative_to(root)) + "/\n/" + str(target.relative_to(root)) + "\n"
        os.write(fd, entries.encode())
    finally:
        os.close(fd)
    return {"name": name, "path": str(target), "sha256": hashlib.sha256(data).hexdigest()}
