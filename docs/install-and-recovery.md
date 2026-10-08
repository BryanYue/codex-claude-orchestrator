# Install, first use and recovery

This covers the two install paths for `codex-claude-orchestrator`, the
first-use preparation both share, upgrading from 0.7.x and recovering from a
failed source switch. See [git-marketplace.md](git-marketplace.md) for the
Git-specific commands and identity, and
[RELEASE-VERIFICATION.md](../RELEASE-VERIFICATION.md) for which of the steps
below have actually been exercised. This documentation targets `v1.0.0`, build
`1.0.0+codex.20261007165220`. Any install, upgrade or rollback takes effect
only in a new MCP connection, i.e. a new Codex task.

Use the same `CODEX_HOME` as the desktop profile you intend to upgrade. Separate
Codex profiles can keep independent marketplace refs and plugin caches; changing
one does not upgrade the other. Check that profile's inventory and prepare its
per-version dependencies. Old chats retain their already-loaded MCP process;
an inventory update alone does not change that process.

## Two install paths

| | ZIP install | Git install |
| --- | --- | --- |
| Entry point | `bash Install.command` from an extracted package | `codex plugin marketplace add` + `codex plugin add` (see [git-marketplace.md](git-marketplace.md)) |
| Verifies package hashes | Yes, against `FILE-SHA256.json` | No local step does this; the host CLI fetches the ref directly |
| Registers with local catalog (`$CODEX_HOME/claude-orchestrator/catalog`) | Yes | No — the plugin source is the Git ref itself |
| Downloads or switches a Claude CLI | No — it only reports your local Claude CLI | No |
| Runs automatically after install | Nothing further | Nothing further |

A Git install does **not** run `Install.command`. Running it after a Git
install would re-register the plugin against the local catalog and replace the
Git source you just chose. GitHub's "Download ZIP" is a source archive, not a
built package: it has no `FILE-SHA256.json`, and `Install.command` refuses it.

## First-time preparation (both paths)

An installed, registered plugin is not yet ready to delegate to Claude. There
are four independent things to get ready, in this order:

1. **uv.** Required for both paths. Check with `command -v uv`; install per
   [the official instructions](https://docs.astral.sh/uv/getting-started/installation/)
   if missing (`brew install uv` if Homebrew is already available). Nothing in
   this plugin installs uv or modifies your shell profile.
2. **Python ≥3.11 and the locked dependency environment.** uv resolves the
   interpreter and installs the locked dependencies (`mcp==2.2.0`, pinned in
   `plugins/codex-claude-orchestrator/uv.lock`) into a per-version environment
   at `$CODEX_HOME/claude-orchestrator/venvs/<full plugin version>` (default
   `~/.codex/...`; `CLAUDE_ORCHESTRATOR_ENV_DIR` overrides it). The first
   resolution can involve a download, which is slow compared to the
   120-second `startup_timeout_sec` in `.mcp.json`. Warm it explicitly
   **before** opening a Codex task that loads the plugin.

   For an extracted ZIP or a source checkout, start at that package root:

   ```bash
   bash plugins/codex-claude-orchestrator/scripts/launch.sh --prepare-dependencies
   ```

   For a Git marketplace install, first run `codex plugin list --json` and
   locate this plugin's installed path in the returned record (or ask Codex to
   inspect its plugin record), then run `bash scripts/launch.sh
   --prepare-dependencies` inside that exact plugin directory. Do not guess a
   cache version directory or run the ZIP installer to repair a Git source. If
   the host does not expose the installed path, run the same command from a
   checkout of the same fixed ref: the launcher selects the environment by the
   full plugin version, so both prepare the same one.

   This only runs `uv sync --frozen --no-dev` and prints a
   `{"status":"ready",...}` line. It never starts the MCP server, never calls a
   model, and never touches global config, PATH, shell profiles or
   authentication. If it fails, the printed error is the `uv sync` failure
   (missing uv, unreachable package index, disk space and so on); fix that and
   rerun the same command. Nothing has been partially registered.
3. **Open a new Codex task** so `.mcp.json` starts the plugin normally
   (`./scripts/launch.sh` with no arguments, which runs `scripts/server.py`).
   This is the first point at which the MCP server itself starts.
4. **Your local Claude CLI and authentication.** Independent of steps 1–3.
   - The plugin uses only the Claude CLI you installed or configured, in this
     order: `CLAUDE_BIN`, then `claude_bin` in the plugin settings file
     (`$CODEX_HOME/claude-orchestrator/settings.json`), then `claude` on the
     MCP process PATH. It never downloads, installs, updates or switches a
     Claude CLI, and it does not change shell profiles, persistent PATH,
     Claude's auto-update setting, login or account.
   - The MCP process does not source shell initialization files. If Claude
     Code was installed through nvm, fnm or Volta and the desktop app cannot
     find it, set `CLAUDE_BIN` to that executable's absolute path in the
     environment that starts Codex, then restart Codex. The launcher puts that
     executable's directory first on the plugin's own PATH so an npm shim finds
     its sibling `node`. A ZIP install can instead persist the path with
     `bash Install.command --configure-claude-bin /absolute/path/to/claude`;
     that option is not for Git installs.
   - The `claude_environment` tool (ask Codex to "check the local Claude
     setup") reports the CLI path and version, whether `--help` advertises
     every option dispatch needs, the result of `claude auth status --json`,
     and whether `/usr/bin/sandbox-exec` is available for the copy profile. It
     sends no model request. The version is recorded for diagnosis only; there
     is no version allowlist. With `verify=true` (ask for "an online
     verification") it sends one small paid request to confirm the login
     actually works.
   - Every run repeats the local check before launching Claude and ends as
     `not_started` if it fails. A run that sets `max_budget_usd` also needs the
     CLI to advertise `--max-budget-usd`.
   - On the ZIP path, `bash Install.command --diagnose-json` reports the same
     Claude readiness plus Codex host and uv discovery, without a model call.
   - Authentication is always your own action: run `claude auth login` with
     the executable the check reported (or configure your API/provider
     authentication), rather than assuming another `claude` on PATH shares its
     account. The plugin does not read, copy or upload credential files.

### Breakdown by what's missing

| Symptom | What it means | What to do |
| --- | --- | --- |
| `command -v uv` fails | uv is not installed | Install uv per the official instructions above |
| `launch.sh --prepare-dependencies` fails on `uv sync` | Locked dependency resolution or download failed (network, index, disk) | Read the printed `uv sync` error; rerun after fixing it |
| The MCP server does not come up within 120 s | A cold dependency download or another startup error | Run step 2, then open a new task. If preparation succeeds and startup still fails, read the actual server error; do not assume every timeout is a download problem |
| `claude_environment` status `cli_not_found` | No Claude executable is visible to the MCP process | Install Claude Code yourself, or set `CLAUDE_BIN` as in step 4 |
| Status `cli_unavailable` | `claude --version` or `--help` failed | Check the installation and host execution permissions |
| Status `cli_incompatible` | `--help` does not list an option dispatch needs (named in `missing_flags`) | Update your own Claude CLI; the plugin will not substitute another version |
| Status `not_logged_in` or `auth_check_failed` | Not signed in, or the login state could not be read | Sign in yourself; for a read failure, check keychain/host permissions without clearing credentials |
| Online verification reports `network_error`, `quota_or_rate_limited` or `access_denied` | The request failed for that reason | This alone does not mean the login expired; fix the reported cause |
| `sandbox.available` is false, or a run says the copy profile needs `sandbox-exec` | No macOS write protection on this host | Analysis tasks can use the read-only profile; implementation tasks cannot run here |
| A run ends `not_started` with "copy preparation failed" | The repository has submodules, sparse/skip-worktree entries, an unresolved merge, Git alternates, links outside the repository, or the boundary scan timed out | Analysis tasks can use the read-only profile; for implementation, resolve that repository state first |
| The old chat errors after a plugin switch, while a new task works | The old MCP process still refers to the removed installation | Use a new Codex task; do not infer the loaded version from the install inventory |
| A run ended as `lost` | Its supervising bridge exited without recording an outcome; the plugin stopped leftover Claude processes and collected what was written | Read the report and patch; once the process is confirmed stopped, decide or continue the run as usual |

## Upgrading from 0.7.x

1.0 is not compatible with 0.7.x tasks. Before switching:

1. **Let running 0.7.x tasks finish, or cancel them with the 0.7.x plugin.**
   1.0 does not list, observe or cancel runs started by an earlier version.
2. **Switch the source** with the fixed-ref steps in
   [git-marketplace.md](git-marketplace.md#change-a-fixed-ref-or-migrate-from-zip),
   or run the new package's `Install.command` for a ZIP install.
3. **Prepare dependencies** for the new version (step 2 above); each full
   plugin version has its own environment.
4. **Open a new Codex task.** Tasks that were already open keep the old MCP
   process.

What stays behind from 0.7.x:

- **Run records.** 1.0 keeps its records under
  `~/.codex/claude-orchestrator/v1/` (`CLAUDE_ORCHESTRATOR_STATE_DIR`
  overrides the root). The 0.7.x records directly under
  `~/.codex/claude-orchestrator/` (`registry.json`, `runs/` and related
  directories) are left untouched. 1.0's tools and workbench do not list them
  and they cannot be continued; keep them as files for reference, or delete
  them once you no longer need them.
- **Project adoption files.** Projects adopted with 0.7.x keep the
  `AGENTS.md` block between `<!-- codex-claude-orchestrator:begin -->` and
  `<!-- codex-claude-orchestrator:end -->` and the `.agents/codex-claude/`
  directory. 1.0 neither reads nor removes them, yet the block still tells
  Codex to use the plugin for multi-step work without being asked and to read
  a project workflow context that 1.0 no longer provides. Remove both by hand,
  and commit that change according to your project's rules.
- **Retired managed-CLI records** under `$CODEX_HOME/claude-orchestrator/cli`
  are not used by 1.0 and can be deleted.
- **Removed tools and options.** Project adoption, executor routing, online
  management of coordination guidance, strict/isolated review modes and
  `review_scope`, `workflow_review`, recovery/reconcile, managed-CLI tools and
  the separate doctor/diagnostics tools no longer exist. A packet using pre-1.0
  fields is rejected with the new field names. Claude is used when you ask for
  it; there is no project-level default.

To roll back to `v0.7.1`, use the same fixed-ref steps with that tag, prepare
its dependencies and open a new task. 1.0 does not modify the 0.7.x records;
0.7.1 does not see runs made by 1.0.

## Migrating from the local catalog to a Git source

This machine may already have a marketplace named `codex-claude-team` pointing
at the local catalog (`$CODEX_HOME/claude-orchestrator/catalog`). Because the
host CLI keys a marketplace by name, adding a Git source under the same name is
not a conflict-free operation. The sequence below changes only this
marketplace; its observed coverage is recorded in RELEASE-VERIFICATION.md, and
the procedure alone is not proof that every failure path has been exercised.

1. **Record current state before touching anything.**
   ```bash
   codex plugin marketplace list --json   # save the codex-claude-team entry: source, ref if any
   codex plugin list --json               # save the codex-claude-orchestrator entry: version, enabled, source
   ```
2. **Remove the old market entry** with
   `codex plugin marketplace remove codex-claude-team --json` (check the
   installed CLI's help if that command is unavailable). Then read both
   marketplace and plugin state. On the CLI tested earlier, this removal also
   removed the plugin from the installed inventory, so the reinstall in step 4
   is required. Do not infer cache deletion or process termination from that
   inventory change.
3. **Add the target Git source with a fixed ref** (see
   [git-marketplace.md](git-marketplace.md) for the exact `add`/`--ref`
   invocation and why a moving branch should not be used).
4. **Install the same plugin id**
   (`codex plugin add codex-claude-orchestrator@codex-claude-team`) and verify
   with `codex plugin list --json` that version, enabled state and marketplace
   source match. The list output omits the pinned ref: also compare the fetched
   market root's `git rev-parse HEAD` with the selected commit.
5. **On any failure at steps 2–4**, read the actual state before recovering.
   If the candidate source was added, remove that same-name entry first; if
   that removal failed or its result is unknown, stop instead of adding a
   conflicting source. Restore the old local path or Git source and fixed ref
   from step 1, reinstall the recorded old version, and restore its previous
   enabled/disabled state with the host's plugin controls. Read both
   inventories again and check source, ref/version and enabled state. If any
   recovery step or final check is inconclusive, keep its output and stop
   retrying automatically.
6. A switch is complete when source, ref/commit, plugin version and installed
   inventory all agree after step 4. Open a new Codex task to load the
   installed version; an MCP process that was already running keeps its code.

**Verification boundary:** consult [RELEASE-VERIFICATION.md](../RELEASE-VERIFICATION.md)
for the tested refs, host and outcomes. A successful remote installation and
MCP initialization do not prove natural-language delegation in a new task, a
cold second machine or another user's credentials. Failure recovery is tested
only where explicitly recorded.
