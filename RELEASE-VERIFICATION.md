# 0.4.7 verification and supported scope

Sanitized distribution baseline. Original release archives and internal audit evidence remain outside this source repository.

Plugin version: 0.4.7+codex.20260923044503; MCP server: 0.4.7; 24 tools.

## Historical verification

- Final offline regressions passed: 225 plugin tests and 72 bridge tests. These are historical 0.4.7 results, not evidence for later versions.
- Managed CLI identity, update concurrency, qualification, cancellation/recovery and file-scoped guards were checked.
- Relocated ZIP installation, cache contents and local MCP/HTTP checks passed. Actual model execution and native host behavior are separate evidence layers.

## Limitations

- macOS team preview; local Apple Silicon checked. Intel, a second Mac, enterprise proxies/certificates and VoiceOver have not been fully verified.
- Full multi-seat Workflow, actual child permission-denial attempts and production long-task reliability remain outside accepted coverage.
- File hooks are not an OS sandbox and do not prove server-only or MDM policy enforcement.
- Cross-TMPDIR coordination and a stable cross-MCP viewer entry remain conditional or unimplemented. No unattended service or automatic merge/publish.
- Git marketplace cold installation, upgrade and rollback have not been demonstrated. This baseline commit is not an accepted Git rollback target.
- CLI maintenance and plugin upgrades are separate. Existing runs keep their execution identity; running MCP processes do not hot-load new code.
- Qualification can consume provider quota with bounded attempts/time but no dollar hard cap. Local diagnostics do not prove a successful remote model request.

Original archives and private evidence are unchanged. This public summary omits actual run/session identifiers, internal paths, account information and usage-cost details.
