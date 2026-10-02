# Codex–Claude Collaboration · 0.7.1

[简体中文](README.md) · **English**

<p align="center">
  <img src="plugins/codex-claude-orchestrator/assets/icon.png" alt="A loop of delegation, results and verification between Codex and Claude" width="128" height="128">
</p>

<p align="center">
  <strong>Bring Claude into your Codex workflow.</strong><br>
  Codex coordinates and checks the work. Claude helps implement or review it.<br>
  You follow the progress in one conversation.
</p>

<p align="center">
  macOS · Codex desktop · Local Claude Code<br>
  <a href="docs/getting-started.en.md">Get started</a> ·
  <a href="#things-to-try">Things to try</a> ·
  <a href="https://github.com/BryanYue/codex-claude-orchestrator/issues">Report an issue</a>
</p>

## What it helps you do

If you use both Codex and Claude, you may spend time moving requirements, code and review feedback between windows. This plugin brings that collaboration into Codex. Describe your goal; Codex can handle it directly or delegate a clearly scoped task to your local Claude Code, then check the result.

**Your request → Codex coordinates → Claude implements or reviews → Codex verifies**

Small tasks can stay with Codex. You can also choose who handles a particular task.

| What you want to do | How the plugin helps |
| --- | --- |
| Get another review of a plan | Claude looks for gaps and edge cases; Codex checks the feedback against your requirements. |
| Implement an agreed plan | Claude works within a defined scope; Codex inspects the changes and runs the required checks. |
| Follow the work | A shared workbench shows tasks, execution rounds, reports and verification results. |
| Continue or correct a task | Add instructions in the same conversation; Codex checks existing progress before proceeding. |

This page targets **v0.7.1**. Reviews preserve the original request and can reproduce issues and run tests in an independent writable copy, with OS protection against writes to the source repository. Strict read-only mode remains available. Guidance ships with the plugin; historical reports and task bindings remain intact. See the [changelog](CHANGELOG.md) and [verification record](RELEASE-VERIFICATION.md) for changes, actual checks and outstanding acceptance.

## Get started

### 1. Have these ready

- **Codex desktop**, signed in, with a version that supports plugins. [Setup guide](https://developers.openai.com/codex/app)
- **Claude Code**, installed locally with working authentication and available usage. The plugin uses your local Claude Code directly and never downloads, updates or switches its version; you upgrade it the official way. [Install and sign in](https://code.claude.com/docs/en/quickstart)
- **uv**, which prepares the plugin's runtime environment. [Installation guide](https://docs.astral.sh/uv/getting-started/installation/)

The plugin targets macOS. Earlier releases have **Apple Silicon Mac** verification records; 0.7.1 coverage is recorded separately. Writable isolated review requires working macOS OS write protection and fails explicitly when unavailable; strict review is an alternative. Intel Macs have not been separately verified.

### 2. Install the plugin

Run these commands in your terminal, one at a time:

```bash
codex plugin marketplace add https://github.com/BryanYue/codex-claude-orchestrator.git --ref v0.7.1
codex plugin add codex-claude-orchestrator@codex-claude-team
```

The first adds the plugin source; the second installs the plugin. The repository is public, so no GitHub invitation or sign-in is needed.

> **About versions:** This page targets `v0.7.1`, full build `0.7.1+codex.20261003035940`. The commands pin this release; source, distribution and real Claude verification are recorded separately. Existing `v0.5.0`, `v0.6.0`, `v0.6.1` `v0.6.2`, and `v0.7.0` tags remain frozen. A fixed ref does not advance to another tag; use the [version-switching steps](docs/git-marketplace.md#change-a-fixed-ref-or-migrate-from-zip).

**On first installation, prepare the dependencies before opening a new Codex task.** If you prefer help with the terminal, see `codex: command not found`, or have an older local installation, follow the [step-by-step guide](docs/getting-started.en.md). It also includes an installation request you can paste into Codex.

### 3. Try a small review

Open your project in Codex, start a new task and send this request. Replace the filename with a real file in your project:

> Use the Codex–Claude collaboration plugin to have Claude review the installation steps in README.md for places a beginner might get stuck. Use strict read-only mode, then check its suggestions yourself.

This starts a real Claude request and uses your Claude allowance. You should see execution progress, Claude's report and Codex's assessment of the suggestions. **A returned report still needs verification before the work is considered complete.**

To make collaboration the default for this Git project, you can then say:

> Adopt the Codex–Claude collaboration workflow for this project.

This optional setting saves collaboration rules in the project. After that, ordinary requests such as “continue” or “fix this issue” can use the workflow without repeating the plugin name.

## Things to try

**Review a plan before implementation**

> Have Claude review docs/plan.md for gaps and unclear requirements. Check its findings and give me your assessment before making any code changes.

**Review the complete implementation**

> Have Claude review defects, architecture, complexity, tests, documentation and prompts. Preserve my original request, reproduce issues and run tests in an independent copy, then verify each finding and report what remains unchecked.

**Implement an agreed scope**

> Have Claude implement this part of the plan we just agreed on. Inspect the changes, run the necessary tests and tell me what remains unresolved.

**Correct the direction**

> That's not what I meant. Keep the existing login flow and only change the error messages. Check the current progress before continuing within that scope.

**Check progress or choose an executor**

> Open the workbench for this task.
>
> Handle this one directly in Codex.
>
> Only use Claude when I explicitly ask for it in this project.

The filenames and tasks are examples; replace them with your own.

## What you see in the workbench

The workbench groups execution rounds by task. You can follow recent activity, switch rounds and read reports. It keeps three things distinct: **whether execution ended, whether a report was returned, and whether Codex verified the result**.

Light and dark themes and narrow panels are supported. The page is for viewing; give corrections and continue work in the Codex conversation. Later executions within the same Codex task use the workbench list where possible. A new task or an app restart may need a new link.

## Common questions

**Do I need both accounts? Whose allowance is used?**

You need your own Codex and Claude Code access. The plugin does not supply or share accounts. Claude execution and the first online readiness check use your Claude allowance. Viewing local status does not send a model request.

**Where does my code go?**

Task instructions and files Claude is allowed to read are sent to the model service configured in your Claude Code setup. The workbench and execution records stay on your computer. Follow your project's rules when deciding what to share with a model.

**Will it change my whole project automatically?**

Strict reviews only read and search. Isolated review can edit the copy, run commands and use Agent/Workflow; OS write protection covers the source repository and requirement files. This does not isolate credentials, network access or arbitrary external effects. External MCP is disabled by default. Implementation retains its explicit file scope, and Codex verifies reports and changes. The plugin does not automatically merge or publish.

**Does the collaboration guide update online?**

From 0.7.0, guidance ships with each plugin version and new tasks use only bundled content. Online checking, review activation and switching are retired. Historical tasks retain their original guidance snapshots for reading and compatibility checks. An upgrade needs a new MCP connection; it does not replace running tasks.

**What do the tokens and cost in the workbench mean?**

The workbench labels are in Chinese. “CLI 会话累计估算” (CLI session cumulative estimate) lists, per model, the cumulative usage and estimated cost reported by the Claude CLI, including subagents; for a resumed session it may include earlier rounds. “主代理最终回报” (main agent final report) is only the main agent's last report, not the task total, and the two are never added together. “未缓存输入” (uncached input) excludes cache reads and writes. The cost is the Claude CLI's client-side estimate, not an actual charge or remaining subscription quota.

**Installed, but nothing starts?**

Check dependency preparation, open a new Codex task and confirm that Claude Code's authentication and execution environment are ready. [Troubleshooting](docs/getting-started.en.md#if-something-goes-wrong)

## Learn more

| What you need | Where to look |
| --- | --- |
| Installation and your first task | [English guide](docs/getting-started.en.md) · [中文指南](docs/getting-started.zh-CN.md) |
| Updates, migration and rollback | [Git marketplace guide](docs/git-marketplace.md) |
| Version management, internals and development | [Advanced guide — Chinese](docs/advanced-usage.zh-CN.md) |
| Release changes and verified coverage | [Changelog — Chinese](CHANGELOG.md) · [Verification record](RELEASE-VERIFICATION.md) |

A community project maintained by [BryanYue](https://github.com/BryanYue), with no official affiliation with OpenAI or Anthropic. No open-source license has been selected. Feedback and suggestions are welcome in [Issues](https://github.com/BryanYue/codex-claude-orchestrator/issues).
