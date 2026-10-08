# Git marketplace distribution

Repository: [BryanYue/codex-claude-orchestrator](https://github.com/BryanYue/codex-claude-orchestrator).
This macOS repository is public. HTTPS reads and Git marketplace fetches do
not require a GitHub account or repository invitation. This is a Git
marketplace distribution; no OpenAI public-directory listing is claimed. No
open-source license has been selected.

## Identity and fixed releases

- Publisher: BryanYue.
- Marketplace: `codex-claude-team`.
- Plugin: `codex-claude-orchestrator` (display name: Codex–Claude 协作).
- Release ref for this version: `v1.0.0`, build `1.0.0+codex.20261007165220`.
  The [release page](https://github.com/BryanYue/codex-claude-orchestrator/releases/tag/v1.0.0)
  provides the built ZIP, its checksum and both manifests. Source, installation
  and real Claude checks are recorded in
  [RELEASE-VERIFICATION.md](../RELEASE-VERIFICATION.md).
- 1.0 is a rebuild and cannot continue tasks started by 0.7.x or earlier; read
  [Upgrading from 0.7.x](install-and-recovery.md#upgrading-from-07x) before
  switching. Older tags, including `v0.7.1`, stay immutable, and refreshing an
  older fixed ref never installs 1.0.
- The marketplace entry resolves `./plugins/codex-claude-orchestrator` relative
  to the fetched repository root. The Git market and the ZIP catalog share the
  same plugin identity; do not install renamed duplicates to switch sources.

## First installation

Use a Codex desktop installation with the `codex plugin` commands, plus uv,
Python >=3.11 and an authenticated Claude Code installation. Reading this
public repository needs no GitHub authentication; Claude authentication and
usage allowance are still required for actual delegation.

First confirm that Git can reach the repository and that the ref exists:

```bash
git ls-remote https://github.com/BryanYue/codex-claude-orchestrator.git refs/tags/v1.0.0
```

An empty result means the tag is not available; do not substitute a moving
branch. If the command fails, check network/proxy settings and any URL
rewrites or credential helper errors before changing an installed
marketplace. An SSH URL still requires working SSH authentication;
`gh auth switch` alone does not switch an SSH key.

For a machine without an existing `codex-claude-team` marketplace:

```bash
codex plugin marketplace add https://github.com/BryanYue/codex-claude-orchestrator.git --ref v1.0.0
codex plugin add codex-claude-orchestrator@codex-claude-team
codex plugin marketplace list --json
codex plugin list --json
```

Check `marketplaceSource.sourceType=git` and the Git URL, plus the installed
and enabled plugin's full manifest version (`1.0.0+codex.20261007165220`). The
CLI omits the pinned ref from these list outputs: compare
`git -C /market/root rev-parse HEAD` with the commit the tag points to. A
plugin entry may still say `source=local` because it points into the fetched
Git snapshot; the marketplace source establishes its Git origin.

Before opening a new Codex task, prepare the locked Python dependencies from
that exact fetched plugin directory (the market inventory exposes its root):

```bash
bash /path/from-market-inventory/plugins/codex-claude-orchestrator/scripts/launch.sh --prepare-dependencies
```

A checkout of the same fixed ref prepares the same per-version environment.
Then open a new Codex task. Follow
[install-and-recovery.md](install-and-recovery.md#first-time-preparation-both-paths)
for local Claude CLI discovery and authentication; the plugin uses your own
Claude CLI and never downloads or switches one. Registration alone does not
prove delegation is ready. Do not run `Install.command` after a Git
installation: it belongs to the ZIP path and would select the local catalog
again.

## Change a fixed ref or migrate from ZIP

Record both inventories before changing this one marketplace. Keep the old Git
URL and ref, or local catalog path, and the old enabled state for recovery.
Confirm the target ref exists with `git ls-remote` first. When moving from
0.7.x to 1.0, let running 0.7.x tasks finish first; 1.0 cannot see them.

```bash
codex plugin marketplace remove codex-claude-team --json
codex plugin marketplace add https://github.com/BryanYue/codex-claude-orchestrator.git --ref FULL_COMMIT_SHA_OR_TAG --json
codex plugin add codex-claude-orchestrator@codex-claude-team --json
codex plugin marketplace list --json
codex plugin list --json
```

`FULL_COMMIT_SHA_OR_TAG` is the selected published ref, not literal text to
copy. On the tested CLI, removing the market also removes its installed plugin
from the inventory, so reinstall explicitly after adding the selected source.
These commands are sequential: stop on an error, inspect the current state,
and restore the recorded source before retrying. Do not remove any other
marketplace. The full recovery procedure is in
[install-and-recovery.md](install-and-recovery.md#migrating-from-the-local-catalog-to-a-git-source).
After switching, prepare dependencies for the new version (each full plugin
version has its own environment) and open a new Codex task; an existing MCP
process keeps the code it loaded.

For rollback, use the same sequence with the previously verified ref, for
example `v0.7.1`. For recovery to a prior ZIP catalog, add its recorded local
path instead of a Git URL. Do not claim that an untested historical SHA is a
verified rollback.

`codex plugin marketplace upgrade codex-claude-team --json` refreshes the
configured snapshot. It does not advance a fixed ref to a different release.
Check both inventories after refresh; changing the pinned ref uses the
sequence above.

## Version recorded with each run

Each run directory records the full plugin version that executed it
(`plan.json`) and the local Claude CLI path and version it used
(`context.json`).
Run records live on this machine under `~/.codex/claude-orchestrator/v1/` and
are not part of the distribution. A Git marketplace cache may contain plugin
files without Git metadata, so compare the recorded version with the release
verification record rather than looking for a commit in the cache.

## Verification and support

[RELEASE-VERIFICATION.md](../RELEASE-VERIFICATION.md) distinguishes actual
remote installation, upgrade/rollback and MCP checks from source checks,
synthetic UI tests and real Claude execution, and keeps the historical
evidence for earlier tags. Source and candidate checks do not prove remote
installation or provider execution. Documentation corrections on `main` do
not move a published tag or replace its ZIP, and installation does not replace
an MCP process that is already running.

Report issues at [GitHub Issues](https://github.com/BryanYue/codex-claude-orchestrator/issues)
with the plugin version, operating system and a sanitized error. Do not attach
repository credentials, viewer tokens, private source or raw execution logs.
