# 0.5.0 verification and supported scope

The integrated candidate contains the read-only task workbench, single-opening
host guidance, dependency warmup and reproducible Git-based distribution tooling.
This document records source-level verification at freeze. Archive, installed-cache,
fresh-host and real-provider results are recorded separately by the release owner;
a source document cannot certify installation of an archive that does not yet exist.

## Verified before release freeze

- Full offline runner: 240 plugin tests and 72 bridge tests passed after final
  browser corrections, including the modal keyboard regression. A later narrow
  desktop bundle-path fix has separate installer regression coverage.
- Builder regression: 29 tests passed (including PNG asset hashing), including tracked-HEAD-only packaging,
  inherited Git-environment isolation, linked worktrees, output/sidecar collisions,
  symlinks, immutable bytes, deterministic ZIP hashes and executable modes.
- Actual dashboard script executes in Node VM tests for summary/detail freshness,
  current-round identity, bounded successor reads, fair observation, pagination,
  request generations, cancellation, recovery/backoff and safe next-step actions.
- The original MCP/HTTP viewer tests remain unchanged. They pass with the existing
  loopback/token/Host/Origin/CSP/GET-only/artifact restrictions preserved.
- Synthetic browser checks cover 1440/1024/900/420/360px, light and dark themes,
  historical rounds, long Chinese text, deep links and public events, clipboard,
  narrow return focus, modal Tab/Escape focus and HTTP503/recovery messaging.
  Browser observations prompted narrow-layout, focus-trap and recovered-label fixes.
- Isolated cold dependency acquisition exceeded a 90-second observation window.
  This is not a passed direct cold MCP startup. Explicit warmup then succeeded;
  a fresh offline MCP session after warmup initialized in 1.886 seconds with
  server version 0.5.0 and 24 tools. Offline missing-Python/missing-wheel cases
  returned failure without falsely reporting ready.
- Desktop CLI discovery supports both the legacy Resources/codex path and
  the nested codex-cli/CodexCLI.app bundle layout observed in the current host.
  PATH-only CLI remains insufficient to claim desktop compatibility.
- The eight execution-contract files, protocol and viewer backend are unchanged.
  Third-party locked dependencies are unchanged; only root package metadata moves
  to 0.5.0. No new CLI qualification is required by this source change.

## Distribution and host boundaries

- The GitHub remote is BryanYue/codex-claude-orchestrator. Actual fixed-commit
  Git installation, upgrade, rollback and missing-ref recovery passed while
  the repository was private on 2026-09-29; exact coverage is recorded below.
  It was subsequently made public at the owner's request. Anonymous HTTPS
  `git ls-remote`, with system/global Git config and credential helpers
  disabled, read HEAD and the unchanged v0.5.0 tag successfully.
- ZIP and Git sources retain distinct install/recovery paths. Git installation
  did not invoke the ZIP installer. Other network/authentication/interruption
  failures remain untested; one recovered invalid ref is not universal coverage.
- Opening-policy tests cover requested, queued and failed replies. A queued reply
  does not prove a visible browser. Cross-thread/cross-MCP stable viewer entry and
  automatic reuse of an existing host tab are not implemented guarantees.
- Existing tasks retain their loaded Skill/MCP. Start a new Codex task after
  reinstalling. A fresh MCP protocol test is not proof of a new task's natural
  language routing or automatic host-opening behavior.

## 2026-09-29 real Git marketplace verification

Host: macOS 26.6.2 / arm64, desktop-bundled `codex-cli 0.158.0-alpha.2.1`.
Private HTTPS access used the repository owner's authenticated GitHub account.

| Stage | Exact source commit | Full plugin version | Result |
| --- | --- | --- | --- |
| N: first Git install | `a40de37f583e51f333e61b93b0b2636d7620c1da` | `0.5.0+codex.20260929020601` | Installed/enabled; 63 plugin files match Git snapshot and cache |
| N+1: upgrade | `51c55ca5100f540540c549428a02e5f507460fdf` | `0.5.0+codex.20260929032040` | Installed/enabled; publisher changes to BryanYue; 63 files match |
| N: rollback | `a40de37f583e51f333e61b93b0b2636d7620c1da` | `0.5.0+codex.20260929020601` | Original version/metadata restored; 63 files match |

Each stage used the official remove/add/install CLI and a real GitHub source,
then compared the persisted fixed ref, fetched Git HEAD, manifest version,
icon bytes and all plugin source/cache files. Other 20 installed plugins and
all other marketplace records remained unchanged. Removing the marketplace
also removed this plugin's inventory entry on this host; reinstall was explicit.

At each stage, the actual installed launcher initialized a fresh MCP session
(version 0.5.0, 24 tools) after dependency warmup. Three finished historical
records were read from an isolated copy; the selected report digest stayed
identical and original historical files stayed unchanged. MCP initialization
and these reads took 0.39s / 4.48s / 0.38s respectively. These are warm-host
observations, not cold downloads or end-to-end model task timings.

One deliberately invalid 40-zero commit failed during checkout. The test then
restored N's Git URL/ref, enabled registration and matching cache contents.
Refreshing the pinned N marketplace left the ref/version at N, despite a newer
commit being present on the remote default branch.

The harness used a separate CLI store with manual update policy and issued no
Claude start/doctor/model calls. It did not reset HOME or CODEX_HOME, change the
user's existing CLI update policy, or terminate existing task MCP processes.
Natural-language routing and automatic browser-opening behavior in a new Codex
task remain separate user smoke checks. No second account or machine was tested.
The release tag's plugin tree matches N+1; subsequent release documentation
updates do not change the plugin source or its complete manifest version.

## Supported scope and remaining limitations

- macOS team preview; Apple Silicon checked. Intel, a second Mac, enterprise
  proxies/certificates and VoiceOver are not fully verified.
- Full multi-seat Workflow, complete long Workflow report collection and production
  long-task reliability remain outside this release. No backend fields or new
  success states are invented for those future features.
- File hooks are not an OS sandbox. No unattended service or automatic merge/publish.
- CLI maintenance, provider authentication and plugin upgrades are separate.
  Existing runs keep their execution identity; no historical receipt is rewritten.
- Synthetic UI data and offline tests are not actual provider/Workflow execution.
  Real review smoke tests, where recorded separately, do not validate Workflow.
- Publisher: BryanYue; public preview repository. No open-source license is
  assigned. Third-party brand usage has not been separately reviewed.

Original archives and private evidence are retained outside the public source.
This summary omits actual run/session identifiers, internal paths, account data
and private usage-cost evidence.
