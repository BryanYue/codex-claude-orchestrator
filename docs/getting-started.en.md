# Your first Codex–Claude collaboration

[Back to the overview](../README.en.md) · [简体中文](getting-started.zh-CN.md) · **English**

This guide takes you through installation and a small read-only review. You do not need to understand the plugin's internal tools or protocols first.

## Before you start

Use a Mac and prepare the following. This release has been checked on Apple Silicon Macs.

| What you need | How to check |
| --- | --- |
| Codex desktop | You can sign in, open a project and start a task; your version supports plugins. [Official setup guide](https://developers.openai.com/codex/app) |
| Claude Code | Install it and sign in with your own account using the [official guide](https://code.claude.com/docs/en/quickstart). The plugin uses this local Claude Code directly and never downloads or switches its version. The Claude desktop chat app does not replace Claude Code. |
| uv | Run `uv --version` in a terminal. If it is missing, follow the [installation guide](https://docs.astral.sh/uv/getting-started/installation/). If you already use Homebrew, `brew install uv` is an option. |

The plugin needs Python 3.11 or later. During dependency preparation, uv locates or downloads the required environment. The first download can take some time.

> **About versions:** `v0.6.1` fixes bridge startup from a deleted parent directory and adds startup code identity checks and evidence-bound early-failure recovery. The old `v0.5.0` and `v0.6.0` tags remain unchanged. Use the [version-switching steps](git-marketplace.md#change-a-fixed-ref-or-migrate-from-zip) to select `v0.6.1`; refreshing a fixed ref does not advance it to another tag.

## Install with help from Codex

Paste this request into an existing Codex conversation:

```text
Help me install the Codex–Claude collaboration plugin:
https://github.com/BryanYue/codex-claude-orchestrator
Use the fixed v0.6.1 release through a Git marketplace.

First check that I have a Codex desktop app with plugin commands and uv.
If codex is not on PATH, check for the CLI bundled with the desktop app.
If this marketplace already exists, inspect it and follow the repository's
migration instructions.

After installation, use the actual plugin registration to find its directory
and run scripts/launch.sh --prepare-dependencies with bash.
Keep the Git source; do not run the ZIP installer, Install.command.

Confirm the plugin is enabled and its dependencies are ready, then explain
anything I still need to do. Only install and check locally at this stage:
do not send a Claude model request or sign in on my behalf.
```

If a tool is missing, a download fails or a permission issue appears, address the specific cause Codex reports. Plugin registration and successful dependency preparation are both needed.

<details>
<summary>I prefer installing from the terminal</summary>

First check that your CLI supports plugin commands:

```bash
codex plugin --help
```

Then run these commands one at a time:

```bash
codex plugin marketplace add https://github.com/BryanYue/codex-claude-orchestrator.git --ref v0.6.1
codex plugin add codex-claude-orchestrator@codex-claude-team
```

This uses Codex's [Git marketplace mechanism](https://developers.openai.com/plugins/build/plugins). The repository is public; reading it does not require GitHub authentication.

Run `codex plugin list --json` to locate this plugin's actual installation directory. Change into that plugin directory and run:

```bash
bash scripts/launch.sh --prepare-dependencies
```

On success, the output includes `"status":"ready"`. This prepares Python dependencies without calling Claude. If the path in the inventory is unclear, ask Codex to locate it, or follow the [detailed preparation instructions](install-and-recovery.md#first-time-preparation-both-paths) using a checkout of the same release. Do not guess a cache version directory.

`codex: command not found` means the terminal cannot find that command; the desktop app may still be installed. Use the request above to ask Codex to locate the CLI bundled with your app. Installing only a terminal CLI does not establish the desktop environment this plugin requires.

If you already have a marketplace with this name, follow the [migration and rollback instructions](git-marketplace.md#change-a-fixed-ref-or-migrate-from-zip) and keep the old source available for recovery.

</details>

## Open a new task and check readiness

After installation and dependency preparation, **open your project in Codex and start a new task** so the client loads the plugin. A task that was already open during installation will not automatically replace all its tools.

Ask Codex:

> Use the Codex–Claude collaboration plugin to check local readiness: the plugin, my local Claude Code and local authentication. Do not send an online model request yet. If Claude Code is missing or lacks a feature the plugin needs, tell me what to install or upgrade myself, and which sign-in steps I need to complete.

If sign-in is needed, use the Claude Code executable identified by that check. Do not paste passwords, tokens or authentication files into the conversation. **Local readiness is followed by your first real task, which also checks whether the remote model is usable.**

## Try your first review

Choose a short document in your project, such as `README.md`, and ask:

> Have Claude review the installation steps in README.md for places a beginner might get stuck. Keep the review read-only, then check its suggestions yourself.

This makes a real Claude request using your own allowance. The first online readiness check can also use a small amount of usage.

Look for three things:

1. **The task starts.** Codex explains Claude's scope and reports the actual execution state.
2. **You can follow progress.** The workbench link shows recent activity.
3. **The result is checked.** Claude returns a report; Codex assesses the suggestions and explains what is supported or still uncertain.

“Environment check complete” alone does not mean Claude performed the review. If execution fails or is interrupted, ask Codex to check the existing task before starting another one.

## Using it day to day

**For occasional tasks**, explicitly ask Claude to review or implement something, as in the example above. No project setting is required.

**For ongoing use in a Git project**, tell Codex:

> Adopt the Codex–Claude collaboration workflow for this project.

This appends a collaboration entry to the project's `AGENTS.md` and saves configuration under `.agents/codex-claude/`, preserving existing rules. You can then use ordinary requests such as “continue with the agreed plan.” Each teammate still uses their own Claude authentication and allowance.

To switch back to occasional use, say “Only use Claude when I explicitly ask for it in this project.” To remove the project entry, say “Disable the automatic Codex–Claude workflow for this project.” If work is running, first ask Codex to stop it and confirm it has ended before disabling the workflow.

## If something goes wrong

| What you see | What to try first |
| --- | --- |
| The plugin or its tools are missing | Confirm it is enabled, then start a new Codex task after installation. |
| The first startup hangs or times out | Ask Codex to inspect the startup error and confirm dependencies are ready. Avoid repeatedly starting Claude tasks. |
| The plugin is installed, but Claude is unavailable | Check whether your local Claude Code is found, whether it lacks a required option, and whether it is authenticated. If a correction cannot resume after you upgraded Claude Code, ask Codex to start a fresh round. |
| The workbench did not open | Ask “Open the workbench for this task,” or use the link Codex provides. Opening the page does not require rerunning the task. |
| No recent activity appears | Ask Codex to check execution status. No new activity does not mean the task has stopped. |
| You want to update or roll back | Follow the [version-switching steps](git-marketplace.md#change-a-fixed-ref-or-migrate-from-zip). A fixed ref (for example `v0.5.0` or `v0.6.0`) does not automatically advance to another release. |
| You have a ZIP file | Built installation packages and Git installation use separate paths. GitHub's “Download ZIP” is a source archive, not a built installer package. See the [ZIP setup notes](install-and-recovery.md#two-install-paths). |

For further help, open an [issue](https://github.com/BryanYue/codex-claude-orchestrator/issues) with your macOS version, plugin version, the step that failed and a sanitized error. Leave out credentials, workbench URLs containing access tokens and private project code.
