# Codex–Claude Collaboration · 1.0.0

[简体中文](README.md) · **English**

<p align="center">
  <img src="plugins/codex-claude-orchestrator/assets/icon.png" alt="A loop of delegation, results and verification between Codex and Claude" width="128" height="128">
</p>

<p align="center">
  <strong>Get work done with Claude, from inside Codex.</strong><br>
  Codex understands the request, delegates it and verifies the result. Claude implements or analyzes in an independent copy.<br>
  You follow the progress in one conversation.
</p>

<p align="center">
  macOS · Codex desktop · Local Claude Code<br>
  <a href="docs/getting-started.en.md">Get started</a> ·
  <a href="#things-to-try">Things to try</a> ·
  <a href="https://github.com/BryanYue/codex-claude-orchestrator/issues">Report an issue</a>
</p>

## What it helps you do

If you use both Codex and Claude, you may spend time moving requirements, code and review feedback between windows. This plugin brings that collaboration into Codex. You describe the goal; Codex hands your exact words, the relevant files and its own understanding to your local Claude Code as separate parts. Claude returns a report and its changes, and Codex verifies them.

**Your request → Codex delegates → Claude implements or analyzes → Codex verifies and reports back**

| What you want to do | How the plugin helps |
| --- | --- |
| Implement a plan | Claude edits code and runs the tests itself in an independent copy, then returns a patch; Codex verifies it before applying it to your project. |
| Review, assess an approach, draft a plan | Claude answers your question first, then gives the evidence; when useful it can reproduce issues in the copy. |
| Follow the work | The workbench shows run facts, Claude's report, the exact brief Claude received and Codex's verdict. |
| Continue or correct | Add instructions in the same conversation; the next round continues the work with your new words appended verbatim, and Codex's corrections and item-by-item decisions carried over. |

Stable release: [`v1.0.0`](https://github.com/BryanYue/codex-claude-orchestrator/releases/tag/v1.0.0) (2026-10-08). 1.0 is a ground-up rebuild: the brief Claude receives is role-specific Markdown that carries your words verbatim; implementation runs in an independent copy and returns a patch; the plugin reports facts and warnings and no longer decides on Codex's behalf that a run failed. See the [changelog](CHANGELOG.md) for the changes and the [verification record](RELEASE-VERIFICATION.md) for what was and was not verified.

## Get started

### 1. Have these three ready

- **Codex desktop**, signed in, with a version that supports plugins. [Setup guide](https://developers.openai.com/codex/app)
- **Claude Code**, installed locally with working authentication and available usage. The plugin uses your local Claude Code directly; it never downloads, updates or switches its version. [Install and sign in](https://code.claude.com/docs/en/quickstart)
- **uv**, which prepares the plugin's runtime environment. [Installation guide](https://docs.astral.sh/uv/getting-started/installation/)

The plugin targets macOS. Implementation tasks and the default analysis tasks need macOS's built-in `sandbox-exec` to protect the original repository; when it is unavailable, analysis tasks can use the read-only profile instead. Intel Macs have not been separately verified.

### 2. Install the plugin

Run these commands in your terminal, one at a time:

```bash
codex plugin marketplace add https://github.com/BryanYue/codex-claude-orchestrator.git --ref v1.0.0
codex plugin add codex-claude-orchestrator@codex-claude-team
```

The first adds the plugin source; the second installs the plugin. The repository is public, so no GitHub invitation or sign-in is needed. Older release tags stay frozen, and a fixed ref does not upgrade by itself; to switch versions, follow the [version-switching steps](docs/git-marketplace.md#change-a-fixed-ref-or-migrate-from-zip).

**On first installation, also prepare the dependencies, then open a new Codex task.** If you are not comfortable with the terminal, see `codex: command not found`, or installed a local version before, follow the [step-by-step guide](docs/getting-started.en.md).

### 3. Start with a small task

Open your project in Codex, start a new task and send this request. Replace the filename with a real file in your project:

> Use the Codex–Claude collaboration plugin to have Claude look at the installation steps in README.md: where is a beginner most likely to get stuck? Don't change any files yet; check its conclusions and then tell me.

This is a real Claude request and uses your Claude allowance. You will see execution progress, Claude's report and Codex's verification. **A returned report is not completion; the work is done only after Codex has verified it.**

## Things to try

**Implement a plan**

> Following phase two of docs/plan.md, have Claude make the export asynchronous. When it's done, check the patch and run the tests, and apply it to the project only if everything holds up.

**Answer first, then the evidence**

> Have Claude assess whether we can move session storage to SQLite, focusing on concurrency and migration cost. Give me the conclusion first.

**Review a change**

> Have Claude review the current uncommitted changes, looking only at error handling and test coverage, and reproduce any issue it suspects in the copy.

**Correct the direction**

> That's not what I meant: keep the existing login flow and only change the error messages. Have Claude continue from the previous round.

**Check progress**

> Open the workbench for this collaboration.

The filenames and tasks are examples; replace them with your own.

## What Claude receives and what it can do

- **The brief**: your words come first, verbatim, followed by the source documents (specs, plans and so on), Codex's notes (marked "may be wrong"), constraints (each marked as coming from you, a source document or Codex) and the completion criteria, plus which part wins when they conflict. You can read the exact brief Claude received in the workbench.
- **Copy profile (default for Git projects)**: Claude works in an independent Git copy of your project's current state, including uncommitted changes. It can edit, run commands and tests, and use Agent/Skill/Workflow. macOS denies writes to the original repository, its `.git` and the plugin's run records; network, credentials and other local paths are not isolated, and external MCP is off. Sensitive untracked files such as `.env`, `.claude`, `.codex` and `.ssh` are not copied.
- **Read-only profile**: works read-only in the original directory, can only read and search (web access only with your consent), and returns its report as structured output. Suited to plain document folders, or when you don't want any command executed.
- **Host instructions**: Claude loads your global and project `CLAUDE.md`, rules and hooks as usual. The plugin lists which ones this session loaded and states in the brief that the brief wins when they conflict.

## What you see in the workbench

The workbench keeps all rounds of the same task together and shows three things separately: **run facts** (how the process ended), **Claude's claim** (what it says about its own completeness) and **Codex's verdict** (accepted, accepted with corrections or rejected, plus the next step). You also see factual warnings (for example, the change touched a protected path, or Claude has a question for you to decide), the list of changed files, the report and the full brief.

The page is for viewing only; additional instructions, corrections and continuing the task still happen in the Codex conversation.

## Common questions

**Do I need both accounts? Whose allowance is used?**

You need your own Codex and Claude Code access. The plugin does not supply or share accounts; actual Claude runs and the online check use your Claude allowance. Viewing local status does not send a model request.

**Where does my code go?**

The brief and the files Claude reads are sent to the model service configured in your Claude Code setup. The workbench and run records stay on your computer (by default under `~/.codex/claude-orchestrator/v1`). Follow your project's rules when deciding which files may go to a model.

**Will it change my project directly?**

No. Claude only changes the copy; the plugin turns the changes into a patch, and Codex applies it to your project with `git apply` after verifying it. The plugin does not merge, commit or publish anything automatically.

**Can I keep editing the project while a task runs?**

Yes. Claude does not work in your project directory. If the project changes in the meantime, the result says so, and Codex checks that the patch still applies cleanly before applying it.

**Codex restarted. What happens to a running task?**

The task keeps running; after reconnecting, query it with the original run ID. If the supervising process exits unexpectedly, the plugin stops any leftover Claude processes, records the round as lost and collects the report and changes already written.

**What do the tokens and cost in the workbench mean?**

The workbench labels are in Chinese. "CLI 会话累计估算" (CLI session cumulative estimate) lists, per model, the cumulative usage and estimated cost reported by the Claude CLI, including subagents; for a continued session it may include earlier rounds. The cost is the Claude CLI's client-side estimate, not an actual charge or remaining subscription quota.

**What should I watch for when upgrading from 0.7?**

1.0 is not compatible with old tasks: old run records stay in their original location for reference but cannot be continued. Project adoption, automatic executor selection and online management of coordination guidance have been removed. See the [changelog](CHANGELOG.md).

## Learn more

| What you need | Where to look |
| --- | --- |
| Installation and your first task | [English guide](docs/getting-started.en.md) · [中文指南](docs/getting-started.zh-CN.md) |
| Updates, migration and rollback | [Git marketplace guide](docs/git-marketplace.md) · [Install and recovery](docs/install-and-recovery.md) |
| Run details and development | [Advanced guide — Chinese](docs/advanced-usage.zh-CN.md) |
| Release changes and verified coverage | [Changelog — Chinese](CHANGELOG.md) · [Verification record](RELEASE-VERIFICATION.md) |

A community project maintained by [BryanYue](https://github.com/BryanYue), with no official affiliation with OpenAI or Anthropic. No open-source license has been selected. Feedback and suggestions are welcome in [Issues](https://github.com/BryanYue/codex-claude-orchestrator/issues).
