# Git marketplace distribution

This describes the Git-based install path for `codex-claude-orchestrator`, as an
alternative to the local ZIP install driven by `Install.command` (see
[install-and-recovery.md](install-and-recovery.md) for that path and for the
first-use steps shared by both).

## What "Git-ready" means here, and what it does not

This repository can be built and inspected reproducibly from a clean commit
(`tools/build_distribution.py`), and its marketplace/plugin identity files are
present and internally consistent. That is **local Git-readiness**, not a
verified remote install:

- **Verified today**: the source tree structure, `.agents/plugins/marketplace.json`
  identity, deterministic build/hash of a local clone, and the file layout a
  Git host would serve.
- **Not verified yet**: actually running `codex plugin marketplace add` against
  a real hosted remote, installing from it, upgrading a pinned tag, or rolling
  back a Git-sourced install. Those require a real repository host, a chosen
  visibility/owner, and push authorization, none of which this task provides.

Do not read "Git-ready" as "Git install has been tested." Any claim about a
real remote install must cite the actual command output against a real host,
not this document.

## Identity

- Marketplace name: `codex-claude-team` (from `.agents/plugins/marketplace.json`
  at the repository root).
- Plugin id: `codex-claude-orchestrator`.
- In this repository, the marketplace entry points at a relative local path
  (`./plugins/codex-claude-orchestrator`). A Git-hosted marketplace serves the
  same repository content over Git instead of a local filesystem path; the
  plugin id and `.codex-plugin/plugin.json` contents do not change.

## Commands (placeholders — no address is published yet)

`<OWNER>/<REPO>` and `<TAG>` below are placeholders. This project has not
picked a host, visibility, license, or publisher identity yet (see
[README.md](../README.md) and `RELEASE-VERIFICATION.md`). Do not substitute a
guessed GitHub account or repository name.

```bash
codex plugin marketplace add <OWNER>/<REPO> --ref <TAG>
codex plugin add codex-claude-orchestrator@codex-claude-team
```

- `--ref <TAG>` pins an exact, reproducible revision. A first rollout should
  use a fixed tag or full commit SHA, not a moving branch.
- `codex plugin marketplace upgrade` refreshes an already-added source; it does
  not move a pinned tag to a newer release on its own. Changing which ref a
  marketplace points at is a separate, explicit action — the exact command and
  its effect on already-installed plugins must be confirmed against the
  running Codex CLI (`codex plugin marketplace --help`,
  `codex plugin --help`) before being written here as fact, and that
  confirmation has not happened yet in this task.

## Git install vs ZIP install

`Install.command` is specific to the ZIP/local-catalog path: it verifies
package hashes, registers the plugin with the local catalog under
`~/.codex/claude-orchestrator/catalog`, and can prepare a managed Claude CLI.
None of that runs automatically for a Git-sourced install — `codex plugin
marketplace add` / `codex plugin add` only fetch the source and register the
plugin with the host CLI. In particular:

- A Git install does **not** run `Install.command`, and this project does not
  attempt to patch a Git-installed source into that flow. Do not add or wire
  up a step that runs `Install.command` after a Git install — that would defeat
  the point of a separate, explicit source (see
  [install-and-recovery.md](install-and-recovery.md) for why).
- The dependency/CLI first-use steps (uv, the locked venv, managed Claude CLI,
  authentication) are identical in spirit for both paths, but must be done
  explicitly after a Git install since there is no installer script to do them
  for you. See [install-and-recovery.md](install-and-recovery.md).

## This machine already has a local `codex-claude-team`

If a marketplace named `codex-claude-team` is already registered locally
(pointing at `~/.codex/claude-orchestrator/catalog`), adding a Git source under
the same name is not a drop-in, conflict-free swap — the host CLI treats the
marketplace name as an identity. The step-by-step read/remove/add/verify/
rollback sequence, and which parts are only documented here versus actually
exercised against a real host, are covered in
[install-and-recovery.md](install-and-recovery.md#migrating-from-the-local-catalog-to-a-git-source).
A real production migration on this machine is out of scope for this task and
is not performed here.
