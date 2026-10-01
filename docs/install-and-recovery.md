# Install, first use and recovery

This covers the two install paths for `codex-claude-orchestrator` and the
first-use preparation both share. See [git-marketplace.md](git-marketplace.md)
for the Git-specific commands and identity, and [RELEASE-VERIFICATION.md](../RELEASE-VERIFICATION.md) for
which of the steps below have actually been exercised for the current version.
The current release is `v0.6.1`; the completed candidate and published-tag
reinstall checks are recorded there separately from older experiments.

## Two install paths

| | ZIP install | Git install |
| --- | --- | --- |
| Entry point | `bash Install.command` from an extracted package | `codex plugin marketplace add` + `codex plugin add` (see [git-marketplace.md](git-marketplace.md)) |
| Verifies package hashes | Yes, against `FILE-SHA256.json` | No local step does this; the host CLI fetches the ref directly |
| Registers with local catalog (`~/.codex/claude-orchestrator/catalog`) | Yes | No — the plugin source is the Git ref itself |
| Downloads or switches a Claude CLI | No — it only reports your local Claude CLI | No |
| Runs automatically after install | Nothing further | Nothing further |

A Git install does **not** run `Install.command`. This is deliberate: running
it after a Git install would silently re-register the plugin against the local
catalog and overwrite the Git source you just chose. If a step described for
the ZIP path below (hash verification, catalog registration) is needed for a
Git-sourced plugin, it must be reproduced as its own explicit action, not by
invoking `Install.command` against a Git checkout.

## First-time preparation (both paths)

The plugin becoming installed/registered is not the same as it being ready to
delegate to Claude. There are four independent things to get ready, in this
order:

1. **uv.** Required for both paths. Check with `command -v uv`; install per
   [the official instructions](https://docs.astral.sh/uv/getting-started/installation/)
   if missing (`brew install uv` if Homebrew is already available). Nothing in
   this plugin installs uv for you or modifies your shell profile to do so.
2. **Python ≥3.11 and the locked dependency venv.** `uv` resolves the
   interpreter and installs the locked dependencies (`mcp==2.2.0`, pinned in
   `plugins/codex-claude-orchestrator/uv.lock`) into a version-specific venv
   the first time they are needed. This first resolution can involve a
   download, which is slow compared to the 120-second `startup_timeout_sec`
   in `.mcp.json`. To avoid racing that timeout on a cold machine, warm the
   venv explicitly **before** opening a Codex task that loads the plugin:

   For an extracted ZIP or a source checkout, start at that package root:

   ```bash
   bash plugins/codex-claude-orchestrator/scripts/launch.sh --prepare-dependencies
   ```

   For a Git marketplace install, first run `codex plugin list --json` and
   locate this plugin's installed/cache path in the returned record (or ask
   Codex to inspect its plugin record). Invoke `scripts/launch.sh` inside that
   exact installed plugin directory. Do not guess a cache version directory or
   run the ZIP installer to repair a Git source. If the host does not expose
   the installed path, the dependency preparation can be run from a checkout
   of the same fixed ref and full manifest version: the launcher selects the
   same version-specific environment. Keep that checkout unchanged until the
   installed version has started successfully.

   This only runs `uv sync --frozen --no-dev` and prints a `{"status": ...}`
   line; it never starts the MCP server, never calls a model, and never
   touches global config, PATH, CLI profiles or authentication. If it fails,
   the printed error is the exact `uv sync` failure (e.g. missing `uv`,
   unreachable package index, disk space) — fix that and rerun the same
   command; nothing has been partially registered.
3. **Open a Codex task to actually start the MCP server.** Once dependencies
   are warm, open a new Codex task so `.mcp.json` loads the plugin normally
   (`./scripts/launch.sh` with no arguments, which runs `scripts/server.py`).
   This is the first point at which the MCP server itself starts; step 2 only
   prepares its dependencies.
4. **Your local Claude CLI and authentication.** Independent of steps 1–3, and
   does not require Claude to be logged in yet. The plugin uses only the local
   Claude CLI you installed or configured (`CLAUDE_BIN`, then the plugin
   `claude_bin` setting, then `claude` on the MCP process PATH). It never
   downloads, installs, updates, rolls back, copies or switches a Claude CLI,
   and it does not change shell profiles, persistent PATH, Claude's auto-update
   setting, login or account. The version is recorded for diagnosis only; a
   task is admitted when `--help` advertises the exact flags it needs, the
   local login check passes and any requested budget flag exists.
   - `bash Install.command --diagnose-json` (ZIP path) or the Skill's
     `claude_cli_status` tool (either path, once the MCP server is running)
     reports uv discovery, Codex host compatibility, and Claude discovery/auth
     status without starting a model call. It does not independently verify
     that a cold Python/dependency download can succeed. Successful dependency
     preparation and a fresh MCP initialization establish those separate facts.
   - If no Claude executable is found, install Claude Code yourself with the
     official instructions, or point the plugin at an existing one (below).
     Retained records from the retired managed-CLI feature of earlier releases
     (`~/.codex/claude-orchestrator/cli`) are left in place as history and
     never selected.
   - Authentication itself is always the user's own action. Log in with the
     executable reported by `claude_cli_status`, rather than assuming a
     different `claude` on PATH shares its account. The optional
     `Install.command --configure-claude-bin` is a ZIP-install operation only;
     for a Git-installed plugin set `CLAUDE_BIN` in the environment that
     starts Codex. The plugin does not ask Codex to read, copy or upload
     credential files.
   - After you upgrade your Claude CLI, a correction that would resume an
     earlier run is refused because the executable changed; start a fresh
     round instead.

### Breakdown by what's missing

| Symptom | What it means | What to do |
| --- | --- | --- |
| `command -v uv` fails | uv is not installed | Install uv per the official instructions above; do not let anything auto-run Homebrew/curl for you |
| `./scripts/launch.sh --prepare-dependencies` fails on `uv sync` | Locked dependency resolution/download failed (network, index, disk) | Read the printed `uv sync` error directly; rerun after fixing it |
| MCP server does not come up within 120s | Dependency download or another startup error may be responsible | Preserve stderr, run step 2, then retry MCP initialization. If warmup succeeds but startup still fails, inspect the actual server error; do not assume every timeout is a download problem or raise the timeout without evidence |
| `claude_cli_status` reports no discovered Claude CLI | No local Claude executable is visible to the MCP process | Install Claude Code yourself, or set `CLAUDE_BIN` for Codex; a ZIP install may instead use its installer with `--configure-claude-bin` |
| Environment check reports `cli_incompatible` | Your local CLI's `--help` does not list a flag this task needs (named in the report) | Upgrade or reconfigure your own Claude CLI; the plugin will not substitute another version |
| `claude_cli_status` reports not authenticated | Claude CLI is present but the user has not logged in | Log in with the Claude CLI yourself; the plugin will not do this for you |
| 0.6.1 diagnostics reports a startup code mismatch or unreadable plugin code | The loaded Runtime and current files cannot establish the same identity | Start a new MCP connection with the installed version; do not delete markers or repeatedly dispatch |
| The old chat errors after a plugin source switch, while a fresh connection works | The original MCP process may still refer to the removed installation | Reconnect or restart the client, then check the actual loaded version; do not infer it from the install inventory |
| A previous `unknown` still blocks dispatch after reconnecting | The old run has separate unresolved execution evidence | Inspect that run with the normal recovery tools; reconnecting alone cannot establish whether Claude started |
| Delegation is blocked even though the plugin is installed | Claude readiness (steps 2–4) is separate from plugin registration (this doc's "Two install paths") | Re-check readiness with `claude_cli_status`/diagnostics, not just install success |

## Migrating from the local catalog to a Git source

This machine may already have a marketplace named `codex-claude-team` pointing
at the local catalog (`~/.codex/claude-orchestrator/catalog`). Because the host
CLI keys a marketplace by name, adding a Git source under the same name is not
a conflict-free operation. The sequence below changes only this marketplace. Its observed coverage is
recorded in RELEASE-VERIFICATION.md; the procedure alone is not proof that
every failure path has been exercised.

1. **Record current state before touching anything.**
   ```bash
   codex plugin marketplace list --json   # save the codex-claude-team entry: source, ref if any
   codex plugin list --json               # save the codex-claude-orchestrator entry: version, enabled, source
   ```
2. **Remove the old market entry.** The locally checked CLI exposes
   `codex plugin marketplace remove codex-claude-team --json`. Check the
   installed CLI's help if that command is unavailable. After the operation,
   read both marketplace and plugin state. On the CLI tested for 0.5.0,
   this removal also removed the plugin from the installed inventory; the
   reinstall in step 4 is required. Do not infer cache deletion or running
   process termination from that inventory change.
3. **Add the target Git source with a fixed ref** (see
   [git-marketplace.md](git-marketplace.md) for the exact `add`/`--ref`
   invocation and why a moving branch should not be used for a first rollout).
4. **Install the same plugin id** (`codex plugin add codex-claude-orchestrator@codex-claude-team`)
   and verify with `codex plugin list --json` that version, enabled state
   and marketplace source match. The tested list output omits the pinned
   ref: also compare the fetched market root's `git rev-parse HEAD` with the
   selected commit and inspect the persisted ref read-only when needed.
5. **On any failure at steps 2–4**, read actual state before recovery. If the
   candidate source was added, remove that candidate same-name entry first;
   if removal itself failed or its result is unknown, stop instead of adding
   a conflicting source. Restore the old local path or Git source and fixed
   ref from step 1, reinstall the recorded old version, and restore its prior
   enabled/disabled state using the host's supported plugin controls. Read
   both inventories again and check source, ref/version and enabled state.
   A failed initial removal whose old source is still present needs no
   source replacement; a successful candidate install followed by failed
   verification needs the full recovery sequence. If any recovery step or
   final check is inconclusive, retain its evidence and stop automatic retries.
6. A completed switch means source, ref/commit, plugin version and the
   installed catalog all agree after step 4's verification. A run that was
   already using the old MCP process keeps its original execution identity;
   it is not hot-replaced by a later switch. Open a new Codex task to pick up
   whichever version is now installed.

**Verification boundary:** consult [RELEASE-VERIFICATION.md](../RELEASE-VERIFICATION.md)
for the tested fixed refs, host and outcomes. Successful remote installation
and MCP initialization do not prove a new task's natural-language routing,
automatic browser reuse, a cold second machine or another user's credentials.
Failure recovery is tested only where explicitly recorded; do not infer that
every interruption or permission failure was exercised.

## 0.6.1 startup recovery

A fresh 0.6.1 MCP connection launches its bridge from a valid state directory,
even when the MCP process inherited a deleted working directory. The Claude
process still uses the task's approved working directory. Diagnostics expose
bridge startup readiness separately from CLI authentication.

If plugin executable code changes after the Runtime loaded it, dispatch fails
before creating a new run. Start a new connection with the installed version;
reinstalling alone does not replace code already loaded by an old process.

For new runs, recovery can use a nonce- and identity-bound startup record to
prove failure before Claude started, including failure before an execution
directory exists. Recovery persists an audit receipt before updating state or
removing that run's marker. Use the normal recovery inspection and reconciliation
tools; do not fabricate startup records or delete markers. Historical unknown
runs without sufficient evidence remain blocked, and changing connections does
not resolve that separate uncertainty.
