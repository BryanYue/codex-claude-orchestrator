# Git marketplace distribution

Repository: [BryanYue/codex-claude-orchestrator](https://github.com/BryanYue/codex-claude-orchestrator).
This macOS preview repository is public. HTTPS reads and Git marketplace
fetches do not require a GitHub account or repository invitation. This is a
Git marketplace distribution; no OpenAI public-directory listing is claimed.
No open-source license has been selected.

## Identity and fixed releases

- Publisher: BryanYue.
- Marketplace: `codex-claude-team`.
- Plugin: `codex-claude-orchestrator` (display name: Codex–Claude 协作).
- Fixed release ref: `v0.7.0`, build `0.7.0+codex.20261003090000`.
  Source, distribution, installed-cache and real-provider verification are
  recorded separately in the release verification record.
- Release boundary: independent writable review with OS source-write protection,
  original request and review coverage, explicit finding adjudication, and bundled
  guidance. Older tags, including `v0.5.0`, `v0.6.0`, `v0.6.1` and `v0.6.2`, remain
  immutable. Refreshing an older fixed ref does not install 0.7.0; use the explicit
  version-switching steps below.
- The marketplace entry resolves `./plugins/codex-claude-orchestrator` relative
  to the fetched repository root. The Git market and ZIP catalog share the
  same plugin identity; do not install renamed duplicates to switch sources.

## First installation

Use a Codex desktop installation with the `codex plugin` commands, plus uv,
Python >=3.11 and an authenticated Claude Code installation. Reading this
public Git repository needs no GitHub authentication; Claude authentication
and usage allowance are still required for actual delegation.

First confirm that Git can reach the public repository:

```bash
git ls-remote https://github.com/BryanYue/codex-claude-orchestrator.git
```

If this fails, check network/proxy settings and any URL rewrites or credential
helper errors before changing an installed marketplace. Public HTTPS reads
need no token. An SSH URL still requires working SSH authentication;
`gh auth switch` alone does not switch an SSH key.

For a machine without an existing `codex-claude-team` marketplace:

```bash
codex plugin marketplace add https://github.com/BryanYue/codex-claude-orchestrator.git --ref v0.7.0
codex plugin add codex-claude-orchestrator@codex-claude-team
codex plugin marketplace list --json
codex plugin list --json
```

Check `marketplaceSource.sourceType=git` and the Git URL, plus the installed/
enabled plugin's complete manifest version. The current CLI omits the pinned
ref from these list outputs: compare `git -C /market/root rev-parse HEAD` with
the selected published commit (or inspect the persisted marketplace ref
read-only). A plugin entry may still say `source=local` because it points into
the fetched Git snapshot; the marketplace source establishes its Git origin.
The target base MCP version is 0.7.0; full plugin versions also contain a
cache-refresh suffix.

Before opening a new Codex task, prepare the locked Python dependencies from
that exact fetched plugin directory (the market inventory exposes its root):

```bash
bash /path/from-market-inventory/plugins/codex-claude-orchestrator/scripts/launch.sh --prepare-dependencies
```

A checkout of the same fixed ref/full plugin version can also warm the same
version-specific environment. Then open a new Codex task. Follow
[install-and-recovery.md](install-and-recovery.md) for local Claude CLI
discovery and authentication; the plugin uses your own installed Claude CLI
and never downloads or switches one. Registration alone does not prove
delegation is ready. Do not run `Install.command` after a Git installation: it belongs to
the ZIP path and would select the local catalog again.

## Change a fixed ref or migrate from ZIP

Record both inventories before changing this one marketplace. Keep the old
Git URL/ref, or local catalog path, and the old enabled state for recovery.
Confirm the target ref exists with `git ls-remote` first.

```bash
codex plugin marketplace remove codex-claude-team --json
codex plugin marketplace add https://github.com/BryanYue/codex-claude-orchestrator.git --ref FULL_COMMIT_SHA_OR_TAG --json
codex plugin add codex-claude-orchestrator@codex-claude-team --json
codex plugin marketplace list --json
codex plugin list --json
```

`FULL_COMMIT_SHA_OR_TAG` is the selected published ref, not literal text to
copy. On the tested CLI, removing the market also removes its installed plugin
from the inventory. Reinstall explicitly after adding the selected source.
These commands are sequential: stop on an error, inspect current state,
and restore the recorded source before retrying. Do not remove any other
marketplace or erase cached executions. The full recovery procedure is in
[install-and-recovery.md](install-and-recovery.md#migrating-from-the-local-catalog-to-a-git-source).

For rollback, use the same sequence with the previously verified Git ref.
For recovery to a prior ZIP catalog, add its recorded local path instead of
a Git URL. Do not claim that an untested historical SHA is a verified rollback.
Existing MCP processes retain their loaded code; start a new task after the
operation to load the selected version.

`codex plugin marketplace upgrade codex-claude-team --json` refreshes the
configured snapshot. It does not advance a fixed ref to a different release.
Check both inventories after refresh; do not assume it changed the installed
plugin. Changing the pinned ref explicitly uses the sequence above.

## Run identity in installed caches

Each new run freezes the full plugin version and actual plugin-file digest.
A Git marketplace cache may contain copied plugin files without Git metadata;
its source revision is then null/unknown, not an inferred repository commit.
ZIP provenance is available only while the distribution manifest remains in
its verified package layout. Compare the run's code digest with the release
verification record; a missing revision is not evidence of a failed run.
Version, provenance and byte identity are separate fields.

## Verification and support

[RELEASE-VERIFICATION.md](../RELEASE-VERIFICATION.md) distinguishes actual
remote installation, upgrade/rollback and MCP protocol checks from source
checks, synthetic UI tests and real Claude execution. Candidate checks do not
prove remote installation or provider execution. The 0.6.2 packaging and
published-tag checks remain historical evidence. The following is
historical 0.6.1 installation evidence: on 2026-10-02, the
published `v0.6.1` tag resolved to
`871818a9fabc297e53b8ca398ab317cd6470461f`. After candidate acceptance it was
fetched and reinstalled through the normal Git marketplace commands. All
83 plugin files matched the accepted candidate, and a fresh native MCP
connection completed a real Sonnet 5.5 read-only smoke. Downloaded release
assets also matched the reproducible build. This is a single-host result,
not a cold second-machine or every-provider qualification.

The 2026-09-29 upgrade, rollback, invalid-ref and pinned-refresh experiments
remain historical v0.5.0 evidence. They do not prove every later-version rollback
or failure path. When rolling back, restore the source and fixed ref actually
recorded before your update. The predecessor `v0.6.0` still points to
`7dd38ca9bf40f092884e6a81d1a39981bff4c595`; the older tested v0.5.0 preview
SHA remains in the historical verification section rather than being the
default rollback target for all installations.

Documentation corrections on `main` do not move a published tag or replace its ZIP.
From 0.7.0, Markdown under `references/` ships with the plugin release; online
content updates are retired. Historical snapshots remain readable. Neither a
document correction nor installation replaces an already-running MCP process.

Report issues at [GitHub Issues](https://github.com/BryanYue/codex-claude-orchestrator/issues)
with the plugin version, operating system and a sanitized error. Do not attach
repository credentials, viewer tokens, private source or raw execution logs.
Repository visibility, licensing and third-party brand usage are separate
from the local install mechanism.
