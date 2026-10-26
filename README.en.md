# Codex–Claude Collaboration · 1.1.1

[简体中文](README.md) · **English**

<p align="center">
  <img src="plugins/codex-claude-orchestrator/assets/icon.png" alt="A loop of delegation, results and review between Codex and Claude" width="128" height="128">
</p>

<p align="center">
  <strong>Claude does the work. Codex reviews it.</strong><br>
  You ask in Codex. Claude works in a copy of your project.<br>
  Codex reviews the result before it reaches you.
</p>

<p align="center">
  macOS · Codex desktop · Local Claude Code<br>
  <a href="docs/getting-started.en.md">Get started</a> ·
  <a href="#how-to-use">Examples</a> ·
  <a href="https://github.com/BryanYue/codex-claude-orchestrator/issues">Report an issue</a>
</p>

## What it does

If you use both Codex and Claude, you probably move requirements, code and review notes between two windows. This plugin lets you stay in Codex. You describe the goal, and Codex hands your exact words, the relevant files and its own reading to your local Claude Code as separate parts. Claude returns a report and its changes, and Codex reviews them.

**You ask → Claude works → Codex reviews → You get the result**

| Feature | What it means |
| --- | --- |
| Write code | Claude edits a copy and runs the tests |
| Review | Conclusion first, then the evidence |
| Talk mid-run | Claude asks as it works; Codex answers what it can |
| Track progress | See where it stands in the Workbench |
| Keep going | Add to the chat; the next round continues |

Current release: [`v1.1.1`](https://github.com/BryanYue/codex-claude-orchestrator/releases/tag/v1.1.1) · [Changelog](CHANGELOG.md) · [Verification record](RELEASE-VERIFICATION.md)

## Get started

### 1. What you need

- **Codex desktop**, signed in, with a version that supports plugins. [Setup guide](https://developers.openai.com/codex/app)
- **Claude Code**, installed locally, signed in and with usage available. The plugin uses your local Claude Code directly; it never downloads, updates or switches its version. [Install and sign in](https://code.claude.com/docs/en/quickstart)
- **uv**, which prepares the plugin's runtime environment. [Installation guide](https://docs.astral.sh/uv/getting-started/installation/)

The plugin targets macOS. Every task needs macOS's built-in `sandbox-exec` to protect your project directory. When it is unavailable (not macOS, or Codex itself runs inside another sandbox), the plugin refuses to start the task and says why. Intel Macs have not been separately verified.

### 2. Install

Run these commands in your terminal, one at a time:

```bash
codex plugin marketplace add https://github.com/BryanYue/codex-claude-orchestrator.git --ref v1.1.1
codex plugin add codex-claude-orchestrator@codex-claude-team
```

The first adds the plugin source; the second installs the plugin. The repository is public, so no GitHub invitation or sign-in is needed. Older release tags stay frozen. A fixed ref does not upgrade by itself; to switch versions, follow the [version-switching steps](docs/git-marketplace.md#change-a-fixed-ref-or-migrate-from-zip).

**On first installation, also prepare the dependencies, then open a new Codex task.** If you are not comfortable with the terminal, see `codex: command not found`, or installed a local version before, follow the [step-by-step guide](docs/getting-started.en.md).

### 3. Try it

Open your project in Codex, start a new task and send this request. Replace the filename with a real file in your project:

> Use the Codex–Claude collaboration plugin to have Claude look at the installation steps in README.md: where is a beginner most likely to get stuck? Don't change any files yet; check its conclusions and then tell me.

This is a real Claude request and uses your Claude allowance. You will see execution progress, Claude's report and Codex's review. **A returned report is not completion; the work is done only after Codex has reviewed it.**

## How to use

**Follow a plan**

> Following phase two of docs/plan.md, have Claude make the export asynchronous. When it's done, check the changes and run the tests, and apply them to the project only if everything holds up.

**Ask a question**

> Have Claude assess whether we can move session storage to SQLite, focusing on concurrency and migration cost. Give me the conclusion first.

**Review changes**

> Have Claude review the current uncommitted changes, looking only at error handling and test coverage, and reproduce any issue it suspects in the copy.

**Talking to Claude mid-run**

> If Claude asks something mid-run that doesn't affect the main flow, decide it yourself; ask me only when it involves a trade-off in the requirements.

> Tell Claude while it's working: start the export file names with the date. Don't wait for it to finish.

When Claude hits a question that would lead it astray if left unconfirmed, it stops and waits for an answer. Codex answers within the authority you have already given; only conflicts with core requirements or acceptance, or decisions beyond that authority, come to you. Your answer goes to Claude word for word and is marked as yours. Notes that don't block the work are recorded, and Claude keeps going. While the task runs you can also add information or redirect it at any time.

**Correct course**

> That's not what I meant: keep the existing login flow and only change the error messages. Have Claude continue from the previous round.

**Check progress**

> Open the Workbench for this task.

The filenames and tasks are examples; replace them with your own.

## Safety boundaries

- **The brief**: your words come first, verbatim. Then come the attachments (specs, plans and so on), Codex's notes, the constraints and the completion criteria. Codex's notes are marked "may be wrong". Each constraint is marked as coming from you, an attachment or Codex. The brief also says which part wins when they conflict. In the next round, your new words are appended verbatim, along with Codex's corrections and item-by-item review. You can read the exact brief Claude received in the Workbench. Claude decides from the task's complexity whether to plan first; there is no start confirmation.
- **Tools**: in both modes Claude has Claude Code's full native tool set in `auto` permission mode. The plugin keeps no tool allow or deny lists. Your project is protected by macOS write protection, not by hiding tools.
- **Copy mode (default for Git projects)**: Claude works in an independent Git copy of your project. The copy reflects the current state, including uncommitted changes. Claude can edit, run commands and run tests. macOS denies writes to the original repository, its `.git` and the plugin's run records. Network, credentials and other local paths are not isolated. Sensitive untracked files such as `.env`, `.claude`, `.codex` and `.ssh` are not copied.
- **Read-only mode**: Claude works directly in the original directory. macOS denies writes to that directory (including `.git` in a Git project) and to the plugin's run records, so writes fail. Claude can read, search and run read-only commands. The report comes back as structured output. Suited to plain document folders, or when the analysis has to happen in the original directory.
- **Write protection is required**: both modes need a working macOS `sandbox-exec`. When it is unavailable, the plugin refuses to start rather than fall back to protection that relies only on instructions in the brief.
- **Web access**: only with your consent. This is an authorization stated in the brief: without it, the brief tells Claude not to go online, but the plugin does not technically block the network.
- **Claude configuration**: Claude loads your global and project `CLAUDE.md`, rules, hooks, MCP servers and skills, and your organization's settings, as usual. The plugin lists which instruction files and hooks this session loaded. The brief states that it wins when they conflict.

## Workbench

- Each Codex conversation has one Workbench page. You switch between that conversation's tasks and rounds on this page. The rounds of a continued task share one card, including earlier rounds continued from 1.0.x; tasks with the same name that are not continuations of each other get separate cards.
- The page opens automatically once, at the conversation's first delegation. Reconnecting, conversation compaction, a new round or a changed link never opens it again. To see it, ask "Open the Workbench" or use the link Codex gives you. The plugin guarantees at most one automatic request of its own; it cannot confirm how many tabs Codex actually opens.
- The task name is a short line Codex writes, no longer the start of your original message. Each round has its own one-line description of what that round does. Your original words stay verbatim in the brief. Rounds left from 1.0.x keep their old titles, but those titles do not carry over: when continuing a 1.0.x task, Codex should give a short title on the first continuation; without one, the new round shows the task ID.
- The list and details show project, task, round (with that round's description) and state separately.
- When Claude is waiting for an answer, a "Claude 在等回答" (Claude is waiting for an answer) notice sits at the very top and the task card says "等回答". Answers are given in the Codex conversation; the page cannot reply. The labels are in Chinese.
- A one-line status at the top says where things stand, such as "Codex review: Passed with fixes, next: another round" or "Claude is working · 3 min so far".
- When a finished run still has a question only you can decide, a blue "Claude has a question for you" box appears at the very top.
- Below are three panels: **Status** (how the process ended), **Claude's self-check** (whether it says it finished) and **Codex review** (Passed, Passed with fixes or Sent back, plus the next step).
- Warnings are explained in Chinese, for example when a change touched a protected path. Technical details are collapsed.
- The details tabs show the report, this round's changes and the activity log. The activity log lists the tools Claude used, the files it read or wrote, the questions and answers along the way, and the updates and interrupts Codex sent mid-run, with their text and source (Codex coordinator or your own words). The brief, your original words and more sit under "More".
- The page is for viewing only. Answering Claude, additional instructions, corrections and continuing the task all happen in the Codex conversation. When the Codex panel is narrow, the task list moves into a "Tasks" button at the top left.

## Common questions

**Whose allowance does it use?**

You need your own Codex and Claude Code access. The plugin does not supply or share accounts. Actual Claude runs and the online check use your Claude allowance. Viewing local status does not send a model request.

**Where does my code go?**

The brief and the files Claude reads are sent to the model service configured in your Claude Code setup. The Workbench and run records stay on your computer (by default under `~/.codex/claude-orchestrator/v1`), as do the mid-run questions, answers and receipts (by default under `~/.codex/claude-orchestrator/v2`). Follow your project's rules when deciding which files may go to a model.

**Will it change my project?**

Not directly. Claude only changes the copy. The plugin packages the changes, and Codex applies them to your project with `git apply` after reviewing them. The plugin does not merge, commit or publish anything automatically.

**Can I edit the project while it runs?**

Yes. In copy mode Claude does not work in your project directory. If the project changes in the meantime, the result says so. Before applying the changes, Codex checks that they still apply without conflicts.

**What happens when Claude asks a question mid-run?**

Claude stops and waits, and the run's other processes are paused too. Codex answers what it can decide within its authority and passes the rest to you. While you have not answered, Claude stays paused; if the task's time limit arrives first, the round ends as timed out and the unanswered questions are listed in the result.

**Do two Codex conversations interfere with each other?**

No. Each conversation has its own Workbench page. Even if both use the same task name, queries, answers, cancellations and reviews act only on that conversation's own tasks.

**Codex restarted. Now what?**

The task keeps running. After reconnecting, query it with the original run ID. If the supervising process exits unexpectedly, the plugin stops any leftover Claude processes, records the round as lost (失联) and collects the report and changes already written.

The Workbench link changes. After Codex or the plugin restarts, the Workbench has a new port and access token, so a page that was already open cannot connect and keeps showing "暂时连不上，正在重试" (cannot connect, retrying). Ask "Open the Workbench" in Codex, or use the new link in Codex's replies. The plugin does not open the page again by itself, and the old page cannot switch to the new link on its own. After a brief disconnection, the old page recovers by itself.

**How is cost calculated?**

The Workbench shows "约 $X（Claude 估算）", meaning "about $X (Claude's estimate)". It is the Claude client's estimate, not an actual charge or remaining subscription quota. It covers the whole Claude session, subagents included, so a continued round may also count earlier rounds.

**What should I watch for when upgrading from 1.0.x?**

Tasks started with 1.0.x can be continued in 1.1; those earlier rounds simply have no mid-run channel. After upgrading, the Workbench may open automatically once per conversation, at the first 1.1 response it receives.

**What should I watch for when upgrading from 0.7?**

Since 1.0, old 0.7 tasks are not compatible. Old run records stay in their original location for reference but cannot be continued. Project adoption, automatic executor selection and online management of coordination guidance have been removed. See the [changelog](CHANGELOG.md).

## Learn more

| Topic | Where to look |
| --- | --- |
| First steps | [English guide](docs/getting-started.en.md) · [中文指南](docs/getting-started.zh-CN.md) |
| Update & rollback | [Git marketplace guide](docs/git-marketplace.md) · [Install and recovery](docs/install-and-recovery.md) |
| Advanced use | [Advanced guide — Chinese](docs/advanced-usage.zh-CN.md) |
| Release notes | [Changelog — Chinese](CHANGELOG.md) · [Verification record](RELEASE-VERIFICATION.md) |

## Release

Current release: [`v1.1.1`](https://github.com/BryanYue/codex-claude-orchestrator/releases/tag/v1.1.1).

1.1.1 fixes premature closure while waiting for Bash/Monitor background results and passed native acceptance after installation in the current Codex. The 1.1 series supports:

- Claude asks serious questions as they come up, and the run pauses until Codex answers. Codex answers what it can and asks you only about core requirements or decisions beyond its authority.
- While the task runs, Codex can add information or interrupt the current step to redirect it. Every reply gets a receipt.
- Each Codex conversation has one Workbench page, opened automatically only once.
- Tasks have a short title written by Codex, and each round has a one-line description.
- Claude uses Claude Code's full native tool set; both modes rely on macOS write protection to protect your project.
- Planning first depends on the task's complexity instead of a confirmation every time.

1.1.1 can continue tasks started with 1.0.x. This release does not include a Go rewrite, a move to the Agent SDK, new authentication or billing arrangements, or a distributed scheduler.

[`v1.0.1`](https://github.com/BryanYue/codex-claude-orchestrator/releases/tag/v1.0.1) (2026-10-15) improved the Workbench:

- In a narrow Codex panel, the task list moves behind a "任务" button and details take the full width.
- A one-line status sits on top, and all labels use plain words.
- The activity log lists each tool Claude used and the files it read or changed.

[`v1.0.0`](https://github.com/BryanYue/codex-claude-orchestrator/releases/tag/v1.0.0) (2026-10-08) was a ground-up rebuild:

- The brief Claude receives is role-specific Markdown that carries your words verbatim.
- Implementation runs in an independent copy and returns its changes.
- The plugin reports facts and warnings and no longer decides on Codex's behalf that a run failed.

See the [changelog](CHANGELOG.md) for the changes and the [verification record](RELEASE-VERIFICATION.md) for what was and was not verified.

A community project maintained by [BryanYue](https://github.com/BryanYue), with no official affiliation with OpenAI or Anthropic. No open-source license has been selected. Feedback and suggestions are welcome in [Issues](https://github.com/BryanYue/codex-claude-orchestrator/issues).
