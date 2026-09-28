#!/bin/bash
set -euo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
if [[ "${CLAUDE_BIN:-}" == /* && -x "${CLAUDE_BIN}" ]]; then
  # A concrete nvm npm shim often uses `#!/usr/bin/env node`; make its sibling
  # Node visible only to this MCP child.  We do not initialize nvm or select a
  # version directory.
  export PATH="$(dirname -- "$CLAUDE_BIN"):$PATH"
fi
PLUGIN_VERSION="$(sed -nE 's/^[[:space:]]*"version"[[:space:]]*:[[:space:]]*"([^"[:space:]]+)"[[:space:]]*,?[[:space:]]*$/\1/p' "$ROOT/.codex-plugin/plugin.json" | head -n 1)"
if [[ -z "$PLUGIN_VERSION" || "$PLUGIN_VERSION" == *"/"* || "$PLUGIN_VERSION" == *".."* ]]; then
  echo 'Claude Orchestrator could not read a safe complete plugin version from .codex-plugin/plugin.json.' >&2
  exit 65
fi
export UV_PROJECT_ENVIRONMENT="${CLAUDE_ORCHESTRATOR_ENV_DIR:-${CODEX_HOME:-$HOME/.codex}/claude-orchestrator/venvs/$PLUGIN_VERSION}"
if ! command -v uv >/dev/null 2>&1; then
  echo 'Claude Orchestrator needs uv. Install uv, then restart this plugin. See the included README.' >&2
  exit 127
fi
if [[ "${1:-}" == "--prepare-dependencies" ]]; then
  # Cold-start warmup only: resolve/download the locked venv so the first real
  # MCP launch does not spend .mcp.json's 120s startup_timeout_sec on `uv sync`.
  # Never starts server.py, never touches Claude/global/path/profile/auth state.
  uv sync --project "$ROOT" --frozen --no-dev
  printf '{"status":"ready","uv_project_environment":"%s"}\n' "$UV_PROJECT_ENVIRONMENT"
  exit 0
fi
exec uv run --project "$ROOT" --frozen --no-dev python "$ROOT/scripts/server.py" "$@"
