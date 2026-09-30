#!/bin/bash
set -euo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
export PATH="${PATH:+$PATH:}$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin"
PLUGIN_ROOT="$ROOT/plugins/codex-claude-orchestrator"
PLUGIN_VERSION="$(sed -nE 's/^[[:space:]]*"version"[[:space:]]*:[[:space:]]*"([^"[:space:]]+)"[[:space:]]*,?[[:space:]]*$/\1/p' "$PLUGIN_ROOT/.codex-plugin/plugin.json" | head -n 1)"
if [[ -z "$PLUGIN_VERSION" || "$PLUGIN_VERSION" == *"/"* || "$PLUGIN_VERSION" == *".."* ]]; then
  echo '无法从插件清单读取安全的完整版本号。' >&2
  exit 65
fi
export UV_PROJECT_ENVIRONMENT="${CLAUDE_ORCHESTRATOR_ENV_DIR:-${CODEX_HOME:-$HOME/.codex}/claude-orchestrator/venvs/$PLUGIN_VERSION}"
if ! command -v uv >/dev/null 2>&1; then
  echo '缺少 uv。请先安装：https://docs.astral.sh/uv/getting-started/installation/' >&2
  exit 1
fi
exec uv run --project "$PLUGIN_ROOT" --frozen --no-dev python "$PLUGIN_ROOT/scripts/install.py" --package-root "$ROOT" "$@"
