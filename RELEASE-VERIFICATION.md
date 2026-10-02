# 0.6.2 release verification

Full plugin version: `0.6.2+codex.20261002042337`. Fixed release ref: `v0.6.2`.
[Release and artifacts](https://github.com/BryanYue/codex-claude-orchestrator/releases/tag/v0.6.2).

The release packages the independently reviewed and regression-tested repair
from source commit `e570676cf1fabc5dad02e9924d1e0593a1c02885`. The release delta
updates plugin/MCP/project/lockfile version metadata and current installation
documentation. Supervision and report-delivery code, dependency versions,
permissions and acceptance requirements are unchanged. Existing tags and
assets remain immutable.

The repair evidence below records 385 plugin, 176 Bridge and 29 distribution
test methods, plus the independent controls and actual native Claude runs.
The final 0.6.2 environment ran all 385 plugin methods: the only failing
method contained two README subcases missing explicit old-tag migration
examples. Restoring that documentation passed all 16 methods in the affected
regression file, with no test changes. This is composite coverage of the 385
methods, not a claim of one green full invocation. The original failure log is
retained. Pre-release evidence does not itself prove a published-tag reinstall.
The package's FILE-SHA256.json and RELEASE-MANIFEST.json bind all shipped bytes
to the exact release commit. Post-publication installation and native checks
are appended to this record on `main`; the release archive keeps its
publication-time document snapshot.

- Startup executable identity: `84a43660108e1d57b8fdd92391d45cb2dddb1fa50e08bc63bcd99b1423ea23f4`.
- Bridge contract digest: `932ad8634c840bd434f8d4095d0d2747cf96197e80fea77c75dea1f6d374aca8`.
- Previous package/whole-plugin digests below identify their own builds; the
  0.6.2 metadata produces a different whole-plugin digest.


## Completed release installation checks

Verified on 2026-10-02 on Apple Silicon macOS, using desktop Codex CLI 0.159.0
and the user's unchanged Claude CLI 2.1.287. The fixed `v0.6.2` tag resolves
to `fe464ee41cb40ce2b9dc0235c00e3ed9106b0fde`.

- Final committed-tree distribution tests passed **29/29**. The release contains
  100 source files plus its generated release manifest; all **101** manifest
  entries and ZIP file bytes matched. The package and installed whole-plugin
  digest is `f1267247ae9aae8ccf5b1efd5895f0f3c4f215a6665a0b44626a64c9d0129086`.
- ZIP SHA-256: `0242dcc837e8fee5c0e6cb91103741c6e42657dc2c92e7361294d57a382f48d6`.
  The uploaded ZIP, checksum file and RELEASE-MANIFEST.json were downloaded
  from GitHub and compared byte-for-byte with the verified build.
- The official marketplace remove/add/plugin-add sequence fetched the remote
  `v0.6.2` Git tag and installed `0.6.2+codex.20261002042337`. All **85 plugin
  files** matched the package and fetched source; all **23 other installed
  plugin entries** remained unchanged against this release's immediate
  pre-install snapshot. Earlier counts below refer to their own snapshots.
- A fresh native MCP connection reported this complete version, with loaded
  and on-disk identities matching the startup executable identity listed above.
  Native ordinary
  smoke `run-nOJOdWsnc_OiAWB5` actually used **Sonnet 5.5 / medium**, returned
  provider exit **0**, delivered all **2,534 prompt bytes**, and reproduced all
  four fixture lines and the required arithmetic correctly.
- Codex independently checked **1/1 audited Read**, zero denials/missing guards
  or scope/session/invariant/tool-policy errors, unchanged input hashes and
  terminal lifecycle. Both recorded process groups were absent in Recovery
  and an independent host process check. One malformed formatter input was
  rejected before execution and then corrected within the same Claude run;
  its original proof was retained separately. Codex accepted the run only
  after checking these original records.
- The previous long/short Workflow tests remain evidence for the identical
  supervision/delivery modules. They were not repeated under a new run ID just
  for the version change. The final native receipt records an unknown Git
  source revision in the copied cache; the remote commit association is
  established by the separate fetched-source/package/cache byte comparison.

No Claude CLI, login, account or active coordination-content setting was
changed. Installation and fresh-connection verification do not hot-reload an
older connection. This `main` document records completed release installation
checks; the fixed tag and release archive retain their publication-time source
and documentation snapshot.

---

# Pre-release repair verification — 2026-10-02

Full source build: `0.6.1+codex.20261002030356`; base version remains `0.6.1`.
This records the Workflow/lifecycle repair before its 0.6.2 packaging. At this
checkpoint it had no new tag or GitHub Release. The fixed `v0.6.1` release below
remains unchanged and does not include these repairs. Use the source commit and distribution manifest to
identify a build; the base version alone is insufficient.

## Repair and architecture scope

The existing ownership remains: MCP exposes the interface, Runtime owns
admission and run lifecycle, Bridge supervises one Claude process, Claude
provides analysis, and Codex verifies and records acceptance. This repair adds
no daemon, parallel state machine, automatic paid retry or permission bypass.
It fixes supervised prompt delivery and cancellation, evidence-bound terminal
marker cleanup, complete Workflow report capture and paging, parent result
selection, scope revalidation, literal metadata parsing and content capability
checks. The demand list defaults to descending start time of each demand's
latest execution; polling, completion and acceptance do not reorder it.
See [CHANGELOG](CHANGELOG.md) for individual behavior changes.

## Offline regression evidence

Three repository suites passed, totaling **590 test methods**: plugin **385**,
Bridge **176**, distribution **29**. These are separate suite runs, not one
combined invocation. Existing assertions, deadlines and recovery proof
requirements were retained. Reproduce the repository checks with:

```bash
uv run --project plugins/codex-claude-orchestrator --frozen python plugins/codex-claude-orchestrator/scripts/run_tests.py --suite all
uv run --project plugins/codex-claude-orchestrator --frozen python -m unittest discover -s tools/tests
```

Independent controls were rerun separately and are not added to that total:
Runtime supervision/recovery **25**, Workflow transport **18**, metadata and
parent result selection **25**, content compatibility **4**, artifact paging
and path safety **7**, actual UI renderer ordering **8**, and termination
**4**. The earlier release's **7 startup scenarios** and **16 recovery
adversarial scenarios** also passed against the repaired source. They include
a deleted parent cwd, a Bridge dying before a run directory exists, proven
recovery followed by a fresh successful run, corrupted binding evidence,
uncertain launch intent, and preserving an immutable recovery receipt across
a registry-write failure. The adversarial comparison uses the actual frozen
`v0.6.0` reader/Bridge, not the now-modified working tree.

The first revalidation failures remain in the task evidence. Ordinary success
fakes previously reported before consuming stdin; the new supervision correctly
recorded incomplete prompt delivery. Those fakes now consume input before
success. The double-crash fixture now proves it has passed its initial stdout
write before its owner and Bridge are killed: a PID record alone allowed the
fake to exit with BrokenPipe before entering the intended live-child scenario.
The diagnostic fake uses the current test interpreter instead of ambient PATH.
No production code, assertion or timeout was changed in this revalidation
follow-up. One separate cold-start diagnostic still timed out at its original
0.3-second limit; the interpreter change is not a guarantee of startup latency.
The complete Bridge suite passed with that limit unchanged.

## Real Claude execution and identity

Real tests used Apple Silicon macOS, the user's existing Claude CLI **2.1.287**,
and native `claude_*` MCP calls. The independently accepted source review used
Claude Opus 5.5; the following read-only execution tests actually used Sonnet
5.5 at medium effort, with provider exit 0:

| Native case | Checked outcome |
| --- | --- |
| Ordinary review, fresh revalidation `run-RZq3mNzephQ_bjf5` | Same two-file fixture and packet contract as the original release smoke, except task ID. All four source lines and arithmetic checked; 1/1 guarded file operation, no denials/errors, unchanged inputs, both process groups absent. |
| Long Workflow `run-gbcHAjXhEY7tX_8w` | One acknowledged and completed Workflow; 576,068-byte exact-text report and 589,045-byte original envelope, nine pages each. All 12,000 Unicode rows and boundaries checked; report bytes equal envelope result encoded as UTF-8. |
| Short Workflow `run-lsanWW_2J34foFMn` | One acknowledged and completed Workflow; 79-byte JSON-value report and 1,081-byte original envelope, one page each. Whole projected value equals the envelope result and the expected fixture object. |

Both Workflow runs had 2/2 executed guarded operations with no denials or
missing audit. Each also contained a separately proved formatter parse
rejection before execution; it does not exempt executed tools from guards.
Codex independently checked artifacts, page offsets/hashes, unchanged fixture
files and both stopped process groups before accepting the reports.

These native tests used installed build `0.6.1+codex.20261002001100`.
The later `20261002030356` source build changes packaged test fixtures and the
build label, with **identical executable modules**. The long/short evidence is
therefore reused explicitly; it is not described as another pair of paid runs
on a different installed package. Source/install comparison confirms:

- Executable startup identity: `84a43660108e1d57b8fdd92391d45cb2dddb1fa50e08bc63bcd99b1423ea23f4`.
- Bridge contract digest: `932ad8634c840bd434f8d4095d0d2747cf96197e80fea77c75dea1f6d374aca8`.
- Current source whole-plugin digest (85 files): `6a8339bd432ac107811a78c8b7eee71dfde0abc477d39b5c7a6de40685bf2d87`.
- Native-tested installed whole-plugin digest: `8947a82b1b0b1e3b39560a307e291bc4557324b3700df95b215f7feb88c7df48`.

Whole-plugin digests include tests and bundled documentation; they must not be
compared as if they were executable-only identities. The installation used for
native acceptance matched all 85 packaged plugin files, and all 101 package
file hashes were verified. Fourteen other installed plugins were unchanged.
A source push does not hot-reload an existing MCP process or replace a fixed
release installation; a fresh connection must prove its loaded identity.

## Evidence boundaries

Task-local evidence preserves the full logs (`push-regression-plugin-complete.log`,
`push-regression-all-final.log` for Bridge, the final distribution log),
independent control outputs, `push-old-bootstrap.json`, `push-old-recovery.json`,
`push-final-identities.json`, and root checks of all three native reports.
Earlier failed/blocked runs remain failed/blocked and are not accepted or
rewritten. An earlier sandbox cleanup failure lacks enough original diagnostic
detail to prove its precise cause; the 25 host controls passed separately.
Decoder-depth handling was verified by fault injection, not a claimed native
failure at a universal nesting threshold.

This establishes the tested transport, supervision and recovery behavior; it
does not establish every report's semantic quality, every CLI version, Intel
Mac support or full Dynamic Workflow Review. Unknown historical records without
adequate evidence stay blocked. CLI installation, login and account settings
were not changed. Bundled coordination content `2026.10.02.4` declares the two
Workflow report capabilities; the locally reviewed active `2026.10.02.1`
remained pinned during native tests. Uploading source neither approves nor
activates new dynamic content.

---

# 0.6.1 verification and supported scope

Full plugin version: `0.6.1+codex.20261001182536`. Published fixed ref: `v0.6.1`.
Release commit: `871818a9fabc297e53b8ca398ab317cd6470461f`.
[GitHub release and artifacts](https://github.com/BryanYue/codex-claude-orchestrator/releases/tag/v0.6.1).
This section is updated with the completed delivery checks. Its code and
package digests identify that immutable release, not later source or documentation
commits on `main`; the packaged copy retains its publication-time wording.
Verified on 2026-10-02 (Asia/Shanghai), Apple Silicon macOS, Codex desktop CLI
0.159.0 and the user's existing Claude CLI 2.1.285. Older sections below are
historical evidence for their own versions.

## Verified source, package and installed release

- The reviewed repair passed 375 plugin tests and 125 Bridge tests. The 29
  added test methods cover deleted parent cwd, startup code identity,
  nonce-bound handoff and early-failure recovery. Existing test expectations
  were not relaxed. Independent baseline/candidate probes also passed seven
  startup scenarios and sixteen recovery/adversarial scenarios.
- The full 375-test plugin suite passed again after the 0.6.1 version and
  installation-documentation changes. Sandbox process-group restrictions
  were preserved in the first failing log and the original suite was rerun
  with normal host permissions. The Bridge suite had the same explicit host
  permission boundary; its unchanged 125 tests passed with those permissions.
- Claude implemented the initial repair; Codex reproduced and fixed three
  recovery edge cases. A fresh read-only Claude Opus 5.5 review of the frozen
  final repair found no blocking issues, with 35/35 audited tool calls and
  zero permission denials. Codex independently accepted that source review.
- A normal local marketplace installation copied all 83 plugin files with
  identical hashes. A fresh native Codex MCP connection reported this exact
  full version and matching loaded/disk startup identity. Its real read-only
  run `run-5dETZmR3l8jCh4nD` used `claude-sonnet-5-5`, returned provider exit 0,
  had 1/1 audited Read calls with no denials, and left the fixture unchanged.
  Codex independently verified and accepted that installed-candidate smoke.
- The committed-tree distribution suite passed all 29 tests, bringing the
  release regression total to 529 (375 plugin + 125 Bridge + 29 distribution).
  The ZIP was built twice with identical bytes; all 99 manifest entries and
  archive contents were verified. Its SHA-256 is
  `c0b96a4bcfd3710e00d6fda98ef4842d6b6a896efcb0c041a66d758f72cc2a50`.
  The ZIP and release manifest downloaded back from GitHub matched the build.
- After publication, the candidate marketplace registration was removed and
  the published `v0.6.1` Git ref fetched and installed. Its checkout resolved
  to the release commit above; all 83 plugin files matched the candidate and
  installed cache. A second fresh native MCP connection completed real run
  `run-4qwB9Dz7_J48O0O1` with `claude-sonnet-5-5`: provider exit 0, 1/1 audited
  Read calls, no denials, unchanged input and accepted by independent Codex
  checks. Seven startup/recovery probes also passed against the installed
  release. The existing CLI binary and 23 unrelated plugin installations
  were unchanged.
- The released plugin file digest is
  `dcf3f5bdb620e6a5fdcae150094bce8b92fd3e78f561ac32b76a7b371b36dee4`.
  The startup executable-code digest is
  `ca7a0c47cf908d30de051ae6b09eb0992ca751af711381a30a631588f7f780f7`.
  Installed caches without Git metadata honestly report unknown revision;
  their bytes must be compared with the independently verified source/ref.

## Delivery and recovery boundaries

- The installed-candidate acceptance preceded commit/publication; the fixed-tag
  reinstall and second native-provider smoke above happened after publication.
  These are distinct observed stages, not conclusions inferred from tests.
- Existing MCP processes retain loaded code. The original old connection
  returned a diagnostics error after the installation switch; the two fresh
  connections passed. Reconnect or restart before dispatching from an old
  connection. Installation inventory alone proves neither loaded code nor a
  provider execution.
- No CLI binary, account, login, credential or auto-update policy is changed.
  No historical unknown run is automatically reconciled. Old records lacking
  proof of whether Claude started remain blocked; deleting a marker or
  fabricating a startup record is not a supported recovery.
- A single-host smoke is not a fresh-machine cold install, Intel/Windows/Linux
  qualification, every-provider test or a long-running load test. Earlier
  release tags remain immutable.

---

# 0.6.0 verification and supported scope

Full plugin version: `0.6.0+codex.20260930055451`. Fixed release target: `v0.6.0`.
This section records the 2026-09-30 source and host checks; the v0.5.0 record
below remains unchanged apart from its scope note. No user Claude CLI,
login settings or installed plugin cache was changed to obtain these results.

## Verified source and execution behavior

- Final regression coverage: 346 plugin tests, 125 Bridge tests and 29
  distribution tests (500 unique tests, no skips). The final full run passed
  all behavior assertions; one new test expected ValueError where the
  established failed-run decision interface raises RuntimeError. Correcting
  that test's exception class and rerunning it passed. Original logs are
  retained. An earlier pass updated the unreleased-version and discarded-
  report assertions to the user's authorized release/report requirements;
  failure status, permission denials and scope checks were not relaxed.
- The actual earlier denied-Glob run was replayed read-only: before the fix,
  coverage was 17 of 18; after it, all 18 have audit evidence (17 allowed,
  1 denied, no missing IDs). The denial still blocks successful execution.
  Valid provider reports remain readable as unaccepted evidence when a run
  fails; malformed reports retain validation errors. Denial entries no
  longer embed the entire result event.
- New identity tests cover the plugin's own Git revision, dirty source,
  unrelated parent repositories, ZIP manifest provenance, legacy/corrupt
  records, frozen identities and nonblocking handling of a FIFO. A real new
  Claude run records the full plugin version, source revision and actual
  code digest before dispatch. The ZIP builder emits the same digest scheme.
- The new content manifest's seven Markdown hashes were verified. The
  protected orchestration-protocol.md bytes retain SHA-256
  `63479da00c8cf9a2467d0efe860fe327a364fb51b7592eece18266319cd6a17b`.
  Reviewed activation, sticky disable, explicit rollback and per-run content
  binding retain the earlier real-MCP and regression evidence.
- Real Claude implementation used the user's local CLI 2.1.285 and actual
  `claude-sonnet-5-5`; 68 of 68 guarded tool calls had audit evidence and no
  permission denials. Codex supplemented the implementation and recorded
  returned/completed_by_codex after independent checks.
- The earlier three-seat whole-repository Workflow and accepted closing
  review remain evidence for unchanged code. The release-delta saved Workflow
  completed and its child had 65/65 audited tools. Its parent run remains
  failed: one malformed StructuredOutput request was rejected by the CLI
  before a hook ran (parent coverage 3/4). The new failed-report preservation
  kept the full report readable. Codex independently resolved its three low
  severity code/guidance findings and documented the expected unknown Git
  revision in metadata-free caches; the failed run was not marked accepted.
- The current plugin-file digest (as computed by the builder and runtime) is
  `a30a111d0e8f0bd988a07dc8b3b7dab999d312021488ef43bb115ff9d014909b`.
  An installed cache without Git metadata can be matched by this digest and
  the full plugin version; no commit is guessed. Reading the actual old
  installed cache with the new collector confirmed this unknown-revision
  layout. That read-only check is not a new-version installation test.

## Delivery boundaries

- Source tests and the actual package build do not establish installation
  into a user's active Codex process. The user will update from the published
  fixed ref and reload the plugin. Existing processes keep loaded code.
- No new-host cold install, every Claude version/provider, long-run load
  test or hard DNS/header timeout stress test is claimed. Response-body
  deadlines, failed/offline content checks and previous approved snapshots
  have regression coverage.
- CLI versions are diagnostic. Only the user's installed/configured CLI is
  selected; required flags, authentication and run evidence still gate use.
- Git remote ref and content availability are checked again after pushing.
  No GitHub Release asset upload or automatic local plugin update is part of
  this source delivery. The old v0.5.0 tag is not moved.

---

# 0.5.0 verification and supported scope

> The section below is retained historical v0.5.0 evidence. Its test counts, tool counts, CLI qualification and unchanged-contract statements do not describe v0.6.0 or v0.6.1.


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
