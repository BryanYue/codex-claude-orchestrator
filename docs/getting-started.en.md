# Your first Codex–Claude collaboration

[Back to the overview](../README.en.md) · [简体中文](getting-started.zh-CN.md) · **English**

This guide takes you through installation and a small analysis task that changes no files. You do not need to understand the plugin's internal tools or parameters first.

## Before you start

Use a Mac and prepare the following:

| What you need | How to check |
| --- | --- |
| Codex desktop | You can sign in, open a project and start a task; your version supports plugins. [Official setup guide](https://developers.openai.com/codex/app) |
| Claude Code | Install it and sign in with your own account using the [official guide](https://code.claude.com/docs/en/quickstart). The plugin uses this local Claude Code directly and never downloads, updates or switches its version. The Claude desktop chat app does not replace Claude Code. |
| uv | Run `uv --version` in a terminal. If it is missing, follow the [installation guide](https://docs.astral.sh/uv/getting-started/installation/). If you already use Homebrew, `brew install uv` is an option. |

The plugin needs Python 3.11 or later. During dependency preparation, uv locates or downloads the required environment; the first download can take some time. Implementation tasks and the default analysis tasks in Git projects also use macOS's built-in `/usr/bin/sandbox-exec` to protect the original repository; normally there is nothing extra to install. Intel Macs have not been separately verified.

> **About versions:** This page targets `v1.0.0`, full build `1.0.0+codex.20261007165220`. For release status and verified coverage, see the [verification record](../RELEASE-VERIFICATION.md). Older release tags stay frozen, and a fixed ref does not advance to another tag by itself; use the [version-switching steps](git-marketplace.md#change-a-fixed-ref-or-migrate-from-zip). If you are upgrading from 0.7.x, read [Upgrading from 0.7.x](install-and-recovery.md#upgrading-from-07x) first: 1.0 cannot continue old tasks.

## Install with help from Codex

Paste this request into an existing Codex conversation:

```text
Help me install the Codex–Claude collaboration plugin:
https://github.com/BryanYue/codex-claude-orchestrator
Use the fixed v1.0.0 release through a Git marketplace.

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
codex plugin marketplace add https://github.com/BryanYue/codex-claude-orchestrator.git --ref v1.0.0
codex plugin add codex-claude-orchestrator@codex-claude-team
```

This uses Codex's [Git marketplace mechanism](https://developers.openai.com/plugins/build/plugins). The repository is public; reading it does not require GitHub authentication.

Run `codex plugin list --json` to locate this plugin's actual installation directory. Change into that plugin directory and run:

```bash
bash scripts/launch.sh --prepare-dependencies
```

On success, the output includes `"status":"ready"`. This prepares Python dependencies only; it does not start the plugin server or call Claude. If the path in the inventory is unclear, ask Codex to locate it, or follow the [detailed preparation instructions](install-and-recovery.md#first-time-preparation-both-paths) using a checkout of the same version. Do not guess a cache version directory.

`codex: command not found` means the terminal cannot find that command; the desktop app may still be installed. Use the request above to ask Codex to locate the CLI bundled with your app. Installing only a terminal CLI does not establish the desktop environment this plugin requires.

If you already have a marketplace with this name, follow the [migration and rollback instructions](git-marketplace.md#change-a-fixed-ref-or-migrate-from-zip) and keep the old source available for recovery.

</details>

## Open a new task and check readiness

After installation and dependency preparation, **open your project in Codex and start a new task** so the client loads the plugin. A task that was already open during installation does not switch to the new tools.

Ask Codex:

> Use the Codex–Claude collaboration plugin to check local readiness: whether Claude Code can be found, whether it supports the options the plugin needs, my local sign-in state, and whether macOS write protection is available. Only check for now; do not send an online model request. If Claude Code is missing or lacks a required feature, tell me what to install or upgrade myself, and which sign-in steps I need to complete.

If sign-in is needed, run `claude auth login` in a terminal (or configure your API/provider authentication), using the Claude Code executable that the check identified. Do not paste passwords, tokens or authentication files into the conversation.

The local check only reads local state. To confirm that the sign-in works online, you can then ask for "an online verification": it sends one very small model request and uses a little of your Claude allowance. A failure caused by network, quota or model access does not by itself mean the sign-in is invalid.

## Try your first task

Choose a short document in your project, such as `README.md`, and ask:

> Use the Codex–Claude collaboration plugin to have Claude look at the installation steps in README.md: where is a beginner most likely to get stuck? Don't change any files yet; check its conclusions and then tell me.

This makes a real Claude request using your own allowance. In a Git project, Claude analyzes in an independent copy of the project by default and can run commands to reproduce problems; in a plain directory it only reads and searches. Either way, Claude does not write to your project directory.

Look for three things:

1. **The task starts.** Codex explains what it handed to Claude, the task type (implement or analyze) and how it runs, and reports the actual state.
2. **You can follow progress.** The workbench link shows progress and recent activity.
3. **The result is checked.** Claude answers your question first, then gives the evidence; Codex checks it and tells you what holds up and what is still uncertain.

**A returned report is not completion; the work is done only after Codex has verified it.** "Environment check complete" alone does not mean Claude performed the task. If execution fails or is interrupted, ask Codex to query the original run ID before starting another one.

## Using it day to day

No project setting is required. When you want Claude involved, say so in the request:

- **Implement**: "Have Claude implement phase two of docs/plan.md; when it's done, check the patch and run the tests, and apply it only if everything holds up." Claude edits code and runs the tests itself in the copy, then returns a patch; Codex verifies it before applying it to your project. The plugin does not commit or push.
- **Analyze**: "Have Claude assess … and give me the conclusion first", "Have Claude review the current uncommitted changes, looking only at error handling." Claude answers the question first, then gives the evidence.
- **Correct or add to it**: say it in the same conversation, for example "That's not what I meant: keep the existing login flow and have Claude build on the previous round." The next round builds on the previous result, with your new words appended verbatim. If the direction changes completely, Codex starts a new task instead.
- **Stop**: "Stop this Claude task." The report and changes already written are still collected.
- **Wrap up**: when the task is done, you can say "This task is finished; delete Claude's copy." Patches, reports and verdicts are kept, but the task can no longer be continued.

When Claude raises a question only you can decide, Codex passes it on verbatim instead of deciding for you.

## If something goes wrong

| What you see | What to try first |
| --- | --- |
| The plugin or its tools are missing | Confirm it is enabled, then start a new Codex task after installation. |
| The first startup hangs or times out | Ask Codex to inspect the startup error and confirm dependencies are ready. Avoid repeatedly starting Claude tasks. |
| The plugin is installed, but Claude is unavailable | Ask Codex to check whether your local Claude Code is found, whether it lacks a required option, and whether it is signed in. If Claude Code was installed through nvm or similar and the desktop app cannot find it, see [pointing the plugin at Claude](install-and-recovery.md#first-time-preparation-both-paths). |
| The copy cannot be created (submodules, sparse checkout, unresolved conflicts and similar) | Analysis tasks can switch to read-only; implementation tasks need those repository states resolved first, and Codex explains why. |
| A message says `sandbox-exec` is required | macOS write protection is not available on this machine. Analysis tasks can switch to read-only; implementation tasks cannot run for now. |
| Tests in the copy are missing dependencies | Files ignored by `.gitignore` (such as `node_modules` or `.venv`) are not copied. If Claude may install dependencies over the network, say so explicitly in the request. |
| The workbench did not open | Ask "Open the workbench for this collaboration," or use the link Codex provides. Opening the page does not require rerunning the task. |
| No recent activity appears | Ask Codex to check execution status. No new activity does not mean the task has stopped. |
| You want to update or roll back | Follow the [version-switching steps](git-marketplace.md#change-a-fixed-ref-or-migrate-from-zip). A fixed ref (for example `v0.7.1`) does not automatically advance to another release. |
| You have a ZIP file | Built installation packages and Git installation use separate paths. GitHub's "Download ZIP" is a source archive, not a built installer package. See [the two install paths](install-and-recovery.md#two-install-paths). |

For further help, open an [issue](https://github.com/BryanYue/codex-claude-orchestrator/issues) with your macOS version, plugin version, the step that failed and a sanitized error. Leave out credentials, workbench URLs containing access tokens and private project code.
