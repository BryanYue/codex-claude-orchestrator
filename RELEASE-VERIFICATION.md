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

- The private GitHub remote is BryanYue/codex-claude-orchestrator. Remote
  installation and N-to-N+1-to-N verification are pending for this candidate;
  this text must be updated from observed results after the experiment.
- ZIP and Git sources retain distinct install/recovery paths. Git installation
  does not invoke the ZIP installer. General failure recovery remains a
  procedure unless a specific exercised failure is recorded.
- Opening-policy tests cover requested, queued and failed replies. A queued reply
  does not prove a visible browser. Cross-thread/cross-MCP stable viewer entry and
  automatic reuse of an existing host tab are not implemented guarantees.
- Existing tasks retain their loaded Skill/MCP. Start a new Codex task after
  reinstalling. A fresh MCP protocol test is not proof of a new task's natural
  language routing or automatic host-opening behavior.

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
- Publisher: BryanYue; private preview repository. No open-source license is
  assigned. Licensing and third-party brand usage must be resolved before
  broader public distribution.

Original archives and private evidence are retained outside the public source.
This summary omits actual run/session identifiers, internal paths, account data
and private usage-cost evidence.
