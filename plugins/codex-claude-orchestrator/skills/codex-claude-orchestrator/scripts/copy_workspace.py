"""Independent Git working copies with an inherited macOS write boundary.

Every copy-profile run (implement or analyze) executes in a clone of the
original worktree's current state.  The original repository, its Git metadata
and the plugin's run state are write-protected for the launched process and its
descendants; credentials, other local paths and the network are not isolated.
``sandbox-exec`` is Apple's deprecated local facility; unsupported hosts fail
closed.  Ignored files are not imported, symlinks are never followed while
copying, and submodules or links outside the copy boundary need the readonly
profile instead.

Changes are measured against trees recorded in a separate object store that
lives in protected run state, so the executor cannot rewrite its own baseline.
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


class CopyWorkspaceError(RuntimeError):
    pass


class ProtectionUnavailable(CopyWorkspaceError):
    pass


METADATA_NAME = "copy.json"
COPY_NAME = "copy"
OUTBOX_NAME = ".codex-run"
WORKFLOW_NAME = "codex-analyze"
SENSITIVE_NAMES = frozenset({".claude", ".codex", ".env", ".ssh", ".aws", ".gnupg", ".netrc", ".npmrc", ".pypirc"})
_GIT = [trusted_git.GIT, "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false", "-c", "init.templateDir="]
GIT_TIMEOUT_SECONDS = 30
TREE_SCAN_TIMEOUT_SECONDS = 60


def _git(cwd: Path, *args: str, input_data: bytes | None = None) -> bytes:
    try:
        completed = trusted_git.run(cwd, *args, input=input_data, check=False, timeout=GIT_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired as exc:
        raise CopyWorkspaceError(f"Git copy command timed out after {GIT_TIMEOUT_SECONDS}s: {args[0] if args else 'git'}") from exc
    except OSError as exc:
        raise CopyWorkspaceError(f"Git is unavailable: {exc}") from exc
    if completed.returncode:
        raise CopyWorkspaceError(f"Git copy command failed: {completed.stderr.decode('utf-8', 'replace').strip()}")
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
        raise CopyWorkspaceError(f"Cannot read global Git ignore configuration: {exc}") from exc
    if result.returncode == 1:
        return None
    if result.returncode:
        raise CopyWorkspaceError("Cannot read global Git ignore configuration: " + result.stderr.decode("utf-8", "replace").strip())
    return os.fsdecode(result.stdout.rstrip(b"\n"))


def _inside(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _canonical(raw: Path | str, *, exists: bool = True) -> Path:
    try:
        return Path(raw).resolve(strict=exists)
    except (OSError, RuntimeError) as exc:
        raise CopyWorkspaceError(f"Cannot resolve copy path: {raw}") from exc


def _digest(value: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode()).hexdigest()


def _source_file(source: Path, root: Path) -> None:
    """Do not traverse a changed directory link or import an external link."""
    relative = source.relative_to(root)
    cursor = root
    for component in relative.parts[:-1]:
        cursor /= component
        if cursor.is_symlink():
            raise CopyWorkspaceError(f"Symlink directory in copy inputs: {cursor}")
    if source.is_symlink():
        target = Path(os.readlink(source))
        if target.is_absolute() or not _inside(_canonical(source.parent / target, exists=False), root):
            raise CopyWorkspaceError(f"External or absolute symlink in copy inputs: {source}")
    elif source.exists():
        info = source.stat()
        if not stat.S_ISREG(info.st_mode):
            raise CopyWorkspaceError(f"Non-file copy input (submodules are unsupported): {source}")


def _validate_protected_trees(roots: Sequence[Path], *, git_metadata: bool = False,
                              metadata_roots: Sequence[Path] = (), excluded_paths: Sequence[Path] = ()) -> None:
    """Stat entries without importing ignored state or following directory links.

    Existing external hardlink aliases could bypass a path-based deny rule.
    Refuse them even in ignored source state and Git metadata. Metadata symlink
    targets outside the Git tree are similarly not covered by its path filter.
    """
    def walk_error(error: OSError) -> None:
        raise CopyWorkspaceError(f"Cannot establish source write boundary: {error}") from error
    deadline = time.monotonic() + TREE_SCAN_TIMEOUT_SECONDS
    seen: set[Path] = set()
    aliases: dict[tuple[int, int], tuple[int, set[Path]]] = {}
    for root in roots:
        if any(_inside(root, path) for path in excluded_paths):
            continue
        for directory, dirs, files in os.walk(root, followlinks=False, onerror=walk_error):
            if time.monotonic() > deadline:
                raise CopyWorkspaceError("Source write-boundary scan timed out; use the readonly profile for this tree")
            dirs[:] = [name for name in dirs if not any(_inside(Path(directory) / name, path) for path in excluded_paths)]
            for name in dirs + files:
                if len(seen) % 256 == 0 and time.monotonic() > deadline:
                    raise CopyWorkspaceError("Source write-boundary scan timed out; use the readonly profile for this tree")
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
                            raise CopyWorkspaceError(f"Hardlink changed during source boundary scan: {path}")
                        paths.add(path)
                    if (git_metadata or any(_inside(path, item) for item in metadata_roots)) and stat.S_ISLNK(info.st_mode) and not any(_inside(_canonical(path), item) for item in roots):
                        raise CopyWorkspaceError(f"External symlink in original Git metadata: {path}")
                except OSError as exc:
                    raise CopyWorkspaceError(f"Cannot establish source write boundary: {path}") from exc
    for count, paths in aliases.values():
        if len(paths) != count:
            path = min(paths)
            raise CopyWorkspaceError(f"Hardlinked source file has aliases outside protected trees ({len(paths)}/{count} protected): {path}; "
                                       "use the readonly profile or clone the source without external hardlinks")


def prepare_copy(source_cwd: Path | str, run_dir: Path | str, extra_files: Sequence[str] = ()) -> dict[str, Any]:
    """Freeze current tracked/nonignored files over an independent HEAD clone.

    ``run_dir`` must already exist outside the source tree. Repeated calls load
    the same copy. Source state is never reset or committed. The copy has no
    remote, alternates or shared inode. Sensitive untracked files are skipped
    and listed rather than copied. ``extra_files`` (repository-relative) are
    copied when present even if ignored, so every file the task's deliveries
    touched is in its later copies whatever the original's ignore rules say.
    """
    cwd = _canonical(source_cwd)
    run = _canonical(run_dir)
    if not cwd.is_dir() or not run.is_dir():
        raise CopyWorkspaceError("Source cwd and run_dir must be existing directories")
    source = _canonical(os.fsdecode(_git(cwd, "rev-parse", "--show-toplevel").rstrip(b"\n")))
    # Git reports the actual prefix even when a case-insensitive filesystem
    # accepted a differently cased spelling of cwd.
    prefix = Path(os.fsdecode(_git(cwd, "rev-parse", "--show-prefix").rstrip(b"\n")))
    if prefix.is_absolute() or ".." in prefix.parts:
        raise CopyWorkspaceError("Git source cwd prefix escapes the repository")
    cwd = source / prefix
    if _inside(run, source) or _inside(source, run):
        raise CopyWorkspaceError("Run directory must not overlap the source repository")
    metadata_path = run / METADATA_NAME
    if metadata_path.exists():
        existing = load_copy(run)
        if existing["source_root"] != str(source) or existing["source_cwd"] != str(cwd):
            raise CopyWorkspaceError("Existing copy belongs to a different source")
        return existing
    destination = run / COPY_NAME
    if destination.exists() or destination.is_symlink():
        raise CopyWorkspaceError("Unowned copy path already exists")
    git_dirs = sorted({str(_canonical(os.fsdecode(_git(cwd, "rev-parse", "--path-format=absolute", option).rstrip(b"\n"))))
                       for option in ("--git-dir", "--git-common-dir")})
    worktrees = sorted({str(_canonical(os.fsdecode(entry[len(b"worktree "):])))
                        for entry in _git(source, "worktree", "list", "--porcelain", "-z").split(b"\0")
                        if entry.startswith(b"worktree ")})
    if any(_inside(run, Path(raw)) or _inside(Path(raw), run) for raw in worktrees):
        raise CopyWorkspaceError("Run directory must not overlap any source worktree")
    _validate_protected_trees([Path(raw) for raw in sorted({*worktrees, *git_dirs})],
                              metadata_roots=[Path(raw) for raw in git_dirs])
    for raw in git_dirs:
        alternates = Path(raw) / "objects" / "info" / "alternates"
        if alternates.exists() or alternates.is_symlink():
            raise CopyWorkspaceError(f"Original Git alternates are unsupported; external object paths are not protected: {alternates}")
    staged_entries = _git(source, "ls-files", "--stage", "-z").split(b"\0")
    if any(entry.startswith(b"160000 ") for entry in staged_entries):
        raise CopyWorkspaceError("Submodules are unsupported by the copy profile; use the readonly profile")
    if any(entry and entry.split(b"\t", 1)[0].split()[-1] != b"0" for entry in staged_entries):
        raise CopyWorkspaceError("An unmerged Git index is unsupported; resolve conflicts or use the readonly profile")
    index_flags = _git(source, "ls-files", "-v", "-z").split(b"\0")
    if any(entry and (entry.startswith(b"S ") or b"a" <= entry[:1] <= b"z") for entry in index_flags):
        raise CopyWorkspaceError("Sparse/skip-worktree or assume-unchanged index entries are unsupported; use the readonly profile")
    tracked = {os.fsdecode(raw) for raw in _git(source, "ls-files", "--cached", "-z").split(b"\0") if raw}
    global_excludes = _global_excludes(source)
    ignore_args = ("-c", f"core.excludesFile={global_excludes}") if global_excludes is not None else ()
    untracked = {os.fsdecode(raw) for raw in _git(source, *ignore_args, "ls-files", "--others", "--exclude-standard", "-z").split(b"\0") if raw}
    skipped_untracked = sorted(name for name in untracked if sensitive_path(name))
    files = tracked | (untracked - set(skipped_untracked))
    included_task_files = sorted(name for name in set(extra_files) - files
                                if name and not Path(name).is_absolute() and ".." not in Path(name).parts
                                and Path(name).parts[0] != ".git" and not sensitive_path(name)
                                and os.path.lexists(source / name))
    files |= set(included_task_files)
    # Validate all inputs before producing a partial copy or reading their bytes.
    for name in files:
        relative = Path(name)
        if relative.is_absolute() or ".." in relative.parts or relative.parts[0] == ".git":
            raise CopyWorkspaceError(f"Unsafe Git input path: {name}")
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
        copy_cwd = destination / cwd.relative_to(source)
        copy_cwd.mkdir(parents=True, exist_ok=True)
        outbox = destination / OUTBOX_NAME
        suffix = hashlib.sha256(str(run).encode()).hexdigest()[:12]
        while outbox.exists() or outbox.is_symlink() or any(
                name == outbox.name or name.startswith(outbox.name + "/") for name in files):
            outbox = outbox.with_name(outbox.name + "-" + suffix)
        outbox.mkdir()
        _append_exclude(destination / ".git" / "info" / "exclude", ["/" + outbox.name + "/"])
        material = {"version": 2, "workspace_root": str(destination), "cwd": str(copy_cwd),
                    "source_root": str(source), "source_cwd": str(cwd), "source_head": head,
                    "staged_input_count": staged_input_count, "staged_diff_sha256": hashlib.sha256(staged_diff).hexdigest(),
                    "source_git_dirs": git_dirs, "source_worktrees": worktrees,
                    "outbox": str(outbox), "skipped_untracked": skipped_untracked,
                    "included_task_files": included_task_files,
                    "protected_paths": sorted({str(source), *worktrees, *git_dirs, str(metadata_path)}),
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


def load_copy(run_dir: Path | str) -> dict[str, Any]:
    """Validate persisted identity without refreshing/overwriting executor edits."""
    run = _canonical(run_dir)
    metadata_path = run / METADATA_NAME
    try:
        if metadata_path.is_symlink():
            raise CopyWorkspaceError("Copy metadata must not be a symlink")
        envelope = json.loads(metadata_path.read_text(encoding="utf-8"))
        identity = envelope["identity"]
        if not isinstance(identity, dict) or envelope["sha256"] != _digest(identity):
            raise CopyWorkspaceError("Copy identity digest mismatch")
        if identity["version"] != 2 or identity["metadata_path"] != str(metadata_path):
            raise CopyWorkspaceError("Copy identity location/version mismatch")
        destination = run / COPY_NAME
        if identity["workspace_root"] != str(destination) or destination.is_symlink() or not destination.is_dir():
            raise CopyWorkspaceError("Copy is missing or redirected")
        source, cwd = Path(identity["source_root"]), Path(identity["source_cwd"])
        expected_cwd = destination / cwd.relative_to(source)
        if identity["cwd"] != str(expected_cwd) or _canonical(expected_cwd) != expected_cwd:
            raise CopyWorkspaceError("Copy cwd is redirected")
        paths = identity["protected_paths"]
        expected_paths = sorted({str(source), *identity.get("source_worktrees", []), *identity["source_git_dirs"],
                                 str(metadata_path)})
        if paths != expected_paths or _inside(run, source) or _inside(source, run):
            raise CopyWorkspaceError("Copy protected path identity mismatch")
        for raw in paths:
            path = Path(raw)
            if not path.is_absolute() or _canonical(path) != path:
                raise CopyWorkspaceError("Copy protected path is redirected")
        if (destination / ".git").is_symlink() or not (destination / ".git").is_dir():
            raise CopyWorkspaceError("Copy Git metadata is redirected")
        alternates = destination / ".git" / "objects" / "info" / "alternates"
        if alternates.exists() or alternates.is_symlink():
            raise CopyWorkspaceError("Copy must not use shared Git objects")
        outbox = Path(identity["outbox"])
        if not outbox.is_absolute() or outbox.parent != destination or _canonical(outbox, exists=False) != outbox:
            raise CopyWorkspaceError("Copy outbox is redirected")
        return identity
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise CopyWorkspaceError(f"Invalid or unavailable copy identity: {exc}") from exc


def protected_command(argv: Sequence[str], metadata: dict[str, Any],
                      extra_protected: Sequence[Path | str] = (), *,
                      writable_exceptions: Sequence[Path | str] = ()) -> list[str]:
    """Wrap a command; protect trusted run files separately from writable logs.

    Writable exceptions apply only inside additional protected directories,
    never to original source or identity paths. Existing directories grant
    descendant writes; other paths grant writes to that single file. Root and
    ancestor entries stay immutable so an executor cannot redirect a writable
    tree, control parent, or the copy itself by renaming it.
    """
    if sys.platform != "darwin" or not Path("/usr/bin/sandbox-exec").is_file():
        raise ProtectionUnavailable("The copy profile requires macOS sandbox-exec; use the readonly profile on this host")
    if not argv or not argv[0] or not all(isinstance(item, str) for item in argv):
        raise CopyWorkspaceError("Command must be a non-empty argument array")
    trusted = load_copy(Path(metadata["metadata_path"]).parent)
    if trusted != metadata:
        raise CopyWorkspaceError("Command identity does not match persisted copy")
    extra_paths: list[str] = []
    for raw in extra_protected:
        if not Path(raw).is_absolute():
            raise CopyWorkspaceError("Additional protected paths must be absolute")
        path = _canonical(raw, exists=False)
        if path.is_file() and path.stat().st_nlink != 1:
            raise CopyWorkspaceError(f"Hardlinked protected control file cannot be path-protected: {path}")
        extra_paths.append(str(path))
    exceptions: list[tuple[str, str]] = []
    for raw in writable_exceptions:
        path = Path(raw)
        if not path.is_absolute() or _canonical(path, exists=False) != path or path.is_symlink():
            raise CopyWorkspaceError("Writable exception must be an absolute, non-redirected path")
        if not any(_inside(path, Path(parent)) and path != Path(parent) for parent in extra_paths):
            raise CopyWorkspaceError("Writable exception must be strictly inside an additional protected directory")
        if any(_inside(path, Path(item)) or _inside(Path(item), path) for item in trusted["protected_paths"]):
            raise CopyWorkspaceError("Writable exception overlaps original source or trusted identity")
        if path.exists() and not path.is_dir() and (not path.is_file() or path.stat().st_nlink != 1):
            raise CopyWorkspaceError("Writable file exception must be a non-hardlinked regular file")
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
        raise CopyWorkspaceError("Control characters in protected paths are unsupported")
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


def discard_copy(run_dir: Path | str) -> bool:
    """Delete whatever is left of a copy; its identity file stays as evidence.

    The caller must establish that no child or descendant still owns the copy.
    """
    target = _canonical(run_dir) / COPY_NAME
    if target.is_symlink():
        target.unlink()
    elif target.exists():
        shutil.rmtree(target)
    return True


def sensitive_path(name: str) -> bool:
    return any(part.lower() in SENSITIVE_NAMES or part.lower().startswith(".env.") for part in Path(name).parts)


def install_workflow_template(metadata: dict[str, Any]) -> dict[str, str]:
    """Offer the optional parameterized analysis Workflow inside the owned copy only."""
    if load_copy(Path(metadata["metadata_path"]).parent) != metadata:
        raise CopyWorkspaceError("Workflow identity does not match persisted copy")
    root = Path(metadata["workspace_root"])
    directory = root / ".claude" / "workflows"
    if not directory.resolve().is_relative_to(root) or any(path.is_symlink() for path in (directory, directory.parent)):
        raise CopyWorkspaceError("Workflow directory escapes the copy")
    directory.mkdir(parents=True, exist_ok=True)
    source = Path(__file__).resolve().parents[3] / "assets" / "analyze-workflow.js"
    data = source.read_bytes()
    name = WORKFLOW_NAME
    tracked = {os.fsdecode(raw) for raw in _git(root, "ls-files", "-z").split(b"\0") if raw}
    # A project workflow with the reserved name is legitimate input; never overwrite it.
    target = directory / (name + ".js")
    suffix = hashlib.sha256(metadata["metadata_path"].encode()).hexdigest()[:12]
    while (str(target.relative_to(root)) in tracked or target.is_symlink() or
           (target.exists() and (not target.is_file() or target.read_bytes() != data))):
        name += "-" + suffix
        target = directory / (name + ".js")
        data = source.read_bytes().replace(f'name: "{WORKFLOW_NAME}"'.encode(), f'name: "{name}"'.encode(), 1)
    if not target.exists():
        with target.open("xb") as handle:
            handle.write(data)
    _append_exclude(root / ".git" / "info" / "exclude", ["/" + str(target.relative_to(root))])
    return {"name": name, "path": str(target), "sha256": hashlib.sha256(data).hexdigest()}


def _append_exclude(exclude: Path, patterns: Sequence[str]) -> None:
    if exclude.parent.is_symlink():
        raise CopyWorkspaceError("Git exclude directory is redirected")
    exclude.parent.mkdir(parents=True, exist_ok=True)
    # Trust no executor-controlled final component on a continued copy.
    fd = os.open(exclude, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise CopyWorkspaceError("Git exclude file must be independent and regular")
        os.write(fd, ("\n" + "\n".join(patterns) + "\n").encode())
    finally:
        os.close(fd)


def _store_git(store: Path, work_tree: Path, index: Path, *args: str, input_data: bytes | None = None) -> bytes:
    """Run plumbing against the protected baseline store with an explicit index."""
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, GIT_TERMINAL_PROMPT="0", LC_ALL="C",
               GIT_INDEX_FILE=str(index))
    command = [trusted_git.GIT, "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false",
               "-c", "core.autocrlf=false", "-c", "core.safecrlf=false", "-c", "core.quotePath=false",
               "--literal-pathspecs", f"--git-dir={store}", f"--work-tree={work_tree}", *args]
    try:
        completed = subprocess.run(command, cwd=work_tree, env=env, input=input_data, capture_output=True,
                                   timeout=TREE_SCAN_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired as exc:
        raise CopyWorkspaceError(f"Baseline store command timed out: {args[0] if args else 'git'}") from exc
    if completed.returncode:
        raise CopyWorkspaceError("Baseline store command failed: " + completed.stderr.decode("utf-8", "replace").strip())
    return completed.stdout


def record_tree(store: Path, metadata: dict[str, Any], index: Path, track: Sequence[str] = ()) -> str:
    """Record the copy's current working tree in the run's store and return its tree id.

    The first call seeds the index with every path the copy tracks (including
    force-added ignored files); later calls reuse that index, so tracked files
    stay measured while new ignored build output stays out of the patch.
    ``track`` adds paths that must be measured even when an ignore rule matches.
    """
    root = Path(metadata["workspace_root"])
    if not (store / "HEAD").is_file():
        store.mkdir(parents=True, exist_ok=True)
        _git(store, "init", "-q", "--bare")
        (store / "info").mkdir(exist_ok=True)
        excludes = ["/" + str(Path(metadata["outbox"]).relative_to(root)) + "/",
                    "/.claude/workflows/" + WORKFLOW_NAME + "*.js"]
        (store / "info" / "exclude").write_text("\n".join(excludes) + "\n")
        global_excludes = _global_excludes(root)
        if global_excludes is not None:
            _git(store, "config", "core.excludesFile", global_excludes)
    seed = not index.exists()
    _store_git(store, root, index, "add", "-A")
    if seed:
        tracked = [raw for raw in _git(root, "ls-files", "-z").split(b"\0")
                   if raw and os.path.lexists(root / os.fsdecode(raw))]
        if tracked:
            _store_git(store, root, index, "add", "-f", "--pathspec-from-file=-", "--pathspec-file-nul",
                       input_data=b"\0".join(tracked) + b"\0")
    track_paths(store, metadata, index, track)
    return _store_git(store, root, index, "write-tree").decode("ascii").strip()


def tree_patch(store: Path, metadata: dict[str, Any], index: Path, start: str, end: str) -> bytes:
    root = Path(metadata["workspace_root"])
    return _store_git(store, root, index, "diff", "--binary", "--full-index", "--no-renames", "--no-ext-diff",
                      "--no-textconv", start, end, "--")


def tree_changes(store: Path, metadata: dict[str, Any], index: Path, start: str, end: str) -> list[dict[str, Any]]:
    """Per-file status and line counts between two recorded trees (binary files count as None)."""
    root = Path(metadata["workspace_root"])
    statuses = _store_git(store, root, index, "diff", "--name-status", "-z", "--no-renames", start, end, "--").split(b"\0")
    numstat = _store_git(store, root, index, "diff", "--numstat", "-z", "--no-renames", start, end, "--").split(b"\0")
    counts: dict[str, tuple[int | None, int | None]] = {}
    for entry in numstat:
        if not entry:
            continue
        added, deleted, name = entry.split(b"\t", 2)
        counts[os.fsdecode(name)] = (None if added == b"-" else int(added), None if deleted == b"-" else int(deleted))
    changes = []
    for status, name in zip(statuses[0::2], statuses[1::2]):
        if not status:
            continue
        path = os.fsdecode(name)
        added, deleted = counts.get(path, (None, None))
        changes.append({"path": path, "status": status.decode("ascii"), "insertions": added, "deletions": deleted})
    return changes


def patch_applies(root: Path, patch: bytes, *, reverse: bool = False) -> tuple[bool, str]:
    """Check, without changing anything, whether a patch applies to a worktree's files."""
    args = ["apply", "--check", "--whitespace=nowarn"] + (["--reverse"] if reverse else []) + ["-"]
    completed = trusted_git.run(root, *args, input=patch, timeout=GIT_TIMEOUT_SECONDS)
    return completed.returncode == 0, completed.stderr.decode("utf-8", "replace").strip()


def apply_patch(metadata: dict[str, Any], patch: bytes) -> None:
    """Apply a patch to the copy's working tree (never its index)."""
    _git(Path(metadata["workspace_root"]), "apply", "--whitespace=nowarn", "-", input_data=patch)


def track_paths(store: Path, metadata: dict[str, Any], index: Path, paths: Sequence[str]) -> None:
    """Keep these paths in the baseline index even when an ignore rule now matches them."""
    root = Path(metadata["workspace_root"])
    present = [path.encode() for path in paths if os.path.lexists(root / path)]
    if present:
        _store_git(store, root, index, "add", "-f", "--pathspec-from-file=-", "--pathspec-file-nul",
                   input_data=b"\0".join(present) + b"\0")


def ignored_paths(metadata: dict[str, Any], paths: Sequence[str]) -> list[str]:
    """The given paths that the copy's ignore rules match."""
    if not paths:
        return []
    completed = trusted_git.run(Path(metadata["workspace_root"]), "check-ignore", "--no-index", "-z", "--stdin",
                                input=b"\0".join(path.encode() for path in paths) + b"\0", timeout=GIT_TIMEOUT_SECONDS)
    if completed.returncode not in (0, 1):
        raise CopyWorkspaceError("Cannot evaluate ignore rules: " + completed.stderr.decode("utf-8", "replace").strip())
    return sorted(os.fsdecode(raw) for raw in completed.stdout.split(b"\0") if raw)


def patch_paths(root: Path, patch: bytes) -> list[str]:
    """Repository-relative paths a patch touches, read without applying it."""
    completed = trusted_git.run(root, "apply", "--numstat", "-z", "-", input=patch, timeout=GIT_TIMEOUT_SECONDS)
    if completed.returncode:
        raise CopyWorkspaceError("Cannot read patch paths: " + completed.stderr.decode("utf-8", "replace").strip())
    return sorted({os.fsdecode(entry.split(b"\t", 2)[2]) for entry in completed.stdout.split(b"\0") if entry.count(b"\t") >= 2})
