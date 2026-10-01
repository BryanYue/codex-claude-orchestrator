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
- Release ref: `v0.6.1`. Use a published fixed tag or full commit SHA; do not
  move a release tag after publishing it.
- Release boundary: `v0.6.1` adds stable bridge startup, startup code identity checks
  and evidence-bound early-failure recovery to the user-local CLI policy, reviewed
  Markdown content updates and execution-evidence fixes. `v0.5.0` remains
  immutable and retains its historical behavior. Refreshing that fixed ref
  does not install 0.6.1; use the explicit version-switching steps below.
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
codex plugin marketplace add https://github.com/BryanYue/codex-claude-orchestrator.git --ref v0.6.1
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
The base MCP version is 0.6.1; full plugin versions also contain a
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
checks, synthetic UI tests and real Claude execution. The remote experiment
was completed on 2026-09-29 for the two fixed commits recorded there, including
an invalid-ref recovery and pinned refresh. This is a single-host result,
not a second-machine, new-user or Claude model execution test. It is a
historical record for the published release, not verification of the
0.6.1 installation. The new source/test/provider evidence is listed separately in the same verification document.

Verified rollback reference for this preview:
`a40de37f583e51f333e61b93b0b2636d7620c1da` (full plugin version
`0.5.0+codex.20260929020601`). It retains the older publisher metadata;
`v0.5.0` contains the personal publisher metadata. Use the same explicit
remove/add/install sequence to select this rollback SHA.

Report issues at [GitHub Issues](https://github.com/BryanYue/codex-claude-orchestrator/issues)
with the plugin version, operating system and a sanitized error. Do not attach
repository credentials, viewer tokens, private source or raw execution logs.
Repository visibility, licensing and third-party brand usage are separate
from the local install mechanism.
