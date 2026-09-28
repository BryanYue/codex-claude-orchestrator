# 0.5.0 verification and supported scope

**Status: 0.5.0 is a pending, unverified distribution candidate.** This
document records what changed in the current pass (A4/A5: build tooling,
install/Git documentation, launcher dependency warmup) and what has not been
executed. Do not read any line below as a passed test unless it names the
actual runner and result; nothing here claims a full suite has passed for
0.5.0.

Plugin manifest version: 0.5.0 (base version; no `+codex.<build>` suffix has
been applied in this pass — that suffixing is done at final integration, not
here). MCP server version and any UI/skill/server-side 0.5.0 changes are
outside this worktree's scope; see "Out of scope" below.

## What changed in this pass (A4/A5)

- `tools/build_distribution.py` (new): builds a reproducible distribution
  archive from a clean, committed Git tree; validates that the manifest base
  version matches `pyproject.toml` and the `codex-claude-orchestrator` entry
  in `uv.lock`; validates `launch.sh`/`Install.command` have git mode 100755;
  refuses to overwrite existing output, symlinks, or undeclared file types;
  writes deterministic ZIP metadata plus `FILE-SHA256.json` and
  `RELEASE-MANIFEST.json` (version, source commit, execution-contract digest).
- `tools/tests/test_build_distribution.py` (new): unit tests covering the
  success path (including reproducible-hash and permission assertions) and
  each documented failure path (dirty tree, untracked file, non-git source,
  wrong git file mode, version mismatches, missing marketplace identity,
  symlink, disallowed extension, overwrite refusal, CLI success/failure exit
  codes).
- `docs/git-marketplace.md`, `docs/install-and-recovery.md` (new): first-use
  and migration documentation; both explicitly separate "local Git-ready" from
  "real remote verified" and do not claim a real remote install has happened.
- `plugins/codex-claude-orchestrator/scripts/launch.sh`: added an explicit
  `--prepare-dependencies` mode that runs `uv sync --frozen --no-dev` for
  cold-start dependency warmup only; it does not start the MCP server, call a
  model, or touch global/PATH/profile/authentication state. The default
  no-argument launch path (used by `.mcp.json`) is unchanged.
- `plugins/codex-claude-orchestrator/tests/test_launcher.py`: added coverage
  for the new mode (dependency-sync invocation without starting the server,
  failure propagation, and that a normal launch still goes through `uv run`).
- Version bump to 0.5.0 in `plugins/codex-claude-orchestrator/.codex-plugin/plugin.json`,
  `pyproject.toml`, and the `codex-claude-orchestrator` entry of `uv.lock`.
  Third-party dependency entries in `uv.lock` were not modified.
- `README.md`: updated version header and added a 0.5.0 section describing the
  above; `RELEASE-VERIFICATION.md` (this file) rewritten for 0.5.0.

## Not executed in this pass

This worktree has no shell access; every check below must be run and its
actual output recorded by whoever executes it before it can be marked passed.

- `python3 tools/tests/test_build_distribution.py` — not run.
- `plugins/codex-claude-orchestrator/tests/test_launcher.py`, including the
  three new `--prepare-dependencies` cases — not run.
- `uv run python scripts/run_tests.py --suite all` (full plugin/bridge
  regression) against this 0.5.0 change set — not run.
- Any real Git remote add/install/upgrade/rollback — not attempted; no
  repository host, owner, or visibility has been chosen (see
  [docs/git-marketplace.md](docs/git-marketplace.md)).
- A real cold-machine run of `--prepare-dependencies` (no local uv, an
  unreachable package index, actual first-download timing against the 120s
  `.mcp.json` startup window) — not performed; only fixture-level tests exist.
- The local same-name marketplace migration steps in
  [docs/install-and-recovery.md](docs/install-and-recovery.md#migrating-from-the-local-catalog-to-a-git-source)
  — documented only, not executed against a real local catalog.
- A reproduced build of the actual repository (as opposed to the test
  fixtures in `tools/tests/`) via `python3 tools/build_distribution.py` — not
  run in this pass; the test suite exercises the tool against synthetic git
  fixtures, not this repository's own commit.

## Out of scope for this worktree

- MCP server version string, dashboard/UI, Skill and server-side changes for
  0.5.0 are implemented in a separate worktree and are integrated, and their
  version consistency finally checked, by whoever merges both.
- `.codex-plugin/plugin.json`'s eventual `+codex.<UTC>` cachebuster suffix is
  applied at final integration, not by this pass.

## Historical: 0.4.7 baseline (superseded, not evidence for 0.5.0)

The following is retained for reference only. It describes 0.4.7 and must not
be read as current.

Plugin version: 0.4.7+codex.20260923044503; MCP server: 0.4.7; 24 tools.

### Historical verification

- Final offline regressions passed: 225 plugin tests and 72 bridge tests.
  These are historical 0.4.7 results, not evidence for 0.5.0.
- Managed CLI identity, update concurrency, qualification, cancellation/recovery
  and file-scoped guards were checked.
- Relocated ZIP installation, cache contents and local MCP/HTTP checks passed.
  Actual model execution and native host behavior are separate evidence layers.

### Historical limitations

- macOS team preview; local Apple Silicon checked. Intel, a second Mac,
  enterprise proxies/certificates and VoiceOver have not been fully verified.
- Full multi-seat Workflow, actual child permission-denial attempts and
  production long-task reliability remain outside accepted coverage.
- File hooks are not an OS sandbox and do not prove server-only or MDM policy
  enforcement.
- Cross-TMPDIR coordination and a stable cross-MCP viewer entry remain
  conditional or unimplemented. No unattended service or automatic
  merge/publish.
- Git marketplace cold installation, upgrade and rollback had not been
  demonstrated as of 0.4.7. This baseline commit is not an accepted Git
  rollback target.
- CLI maintenance and plugin upgrades are separate. Existing runs keep their
  execution identity; running MCP processes do not hot-load new code.
- Qualification can consume provider quota with bounded attempts/time but no
  dollar hard cap. Local diagnostics do not prove a successful remote model
  request.

Original archives and private evidence are unchanged. This public summary
omits actual run/session identifiers, internal paths, account information and
usage-cost details.
