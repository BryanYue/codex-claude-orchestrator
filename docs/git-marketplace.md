# Git marketplace distribution

Repository: [BryanYue/codex-claude-orchestrator](https://github.com/BryanYue/codex-claude-orchestrator).
This is a private macOS preview. Installation requires a GitHub account with
read access. It is a Git marketplace, not an OpenAI public-directory listing.
No open-source license has been selected.

## Identity and fixed releases

- Publisher: BryanYue.
- Marketplace: `codex-claude-team`.
- Plugin: `codex-claude-orchestrator` (display name: Codex–Claude 协作).
- Release ref: `v0.5.0`. Use a published fixed tag or full commit SHA; do not
  move a release tag after publishing it.
- The marketplace entry resolves `./plugins/codex-claude-orchestrator` relative
  to the fetched repository root. The Git market and ZIP catalog share the
  same plugin identity; do not install renamed duplicates to switch sources.

## First installation

Use a Codex desktop installation with the `codex plugin` commands, plus uv,
Python >=3.11 and an authenticated Claude Code installation. GitHub repository
access and Claude authentication are separate. A connected GitHub app does
not automatically authenticate terminal Git or the desktop's Git subprocess.

First confirm that Git can read the private repository using your own account:

```bash
git ls-remote https://github.com/BryanYue/codex-claude-orchestrator.git
```

If this fails, fix Git HTTPS credentials (or use an authorized SSH URL) before
changing any installed marketplace. With multiple GitHub accounts, check the
actual account and Git credential helper; `gh auth switch` alone does not
switch an SSH key. Never put a token in a remote URL or repository file.

For a machine without an existing `codex-claude-team` marketplace:

```bash
codex plugin marketplace add https://github.com/BryanYue/codex-claude-orchestrator.git --ref v0.5.0
codex plugin add codex-claude-orchestrator@codex-claude-team
codex plugin marketplace list --json
codex plugin list --json
```

Check the Git URL and ref in the market record and the installed/enabled
plugin's complete manifest version. The base MCP version remains 0.5.0;
full plugin versions also contain a cache-refresh suffix.

Before opening a new Codex task, prepare the locked Python dependencies from
that exact fetched plugin directory (the market inventory exposes its root):

```bash
bash /path/from-market-inventory/plugins/codex-claude-orchestrator/scripts/launch.sh --prepare-dependencies
```

A checkout of the same fixed ref/full plugin version can also warm the same
version-specific environment. Then open a new Codex task. Follow
[install-and-recovery.md](install-and-recovery.md) for managed Claude CLI
preparation and authentication. Registration alone does not prove delegation
is ready. Do not run `Install.command` after a Git installation: it belongs to
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
copy. These commands are sequential: stop on an error, inspect current state,
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

## Verification and support

[RELEASE-VERIFICATION.md](../RELEASE-VERIFICATION.md) distinguishes actual
remote installation, upgrade/rollback and MCP protocol checks from source
checks, synthetic UI tests and real Claude execution. The remote experiment
is pending until that document records the observed result; creating a GitHub
repository or pushing a commit alone is not installation evidence.

Report issues at [GitHub Issues](https://github.com/BryanYue/codex-claude-orchestrator/issues)
with the plugin version, operating system and a sanitized error. Do not attach
repository credentials, viewer tokens, private source or raw execution logs.
Private repository access, broader publication, licensing and third-party
brand usage are separate from the local install mechanism.
