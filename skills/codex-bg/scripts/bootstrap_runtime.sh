#!/usr/bin/env bash
set -euo pipefail

codex_home="${CODEX_HOME:-$HOME/.codex}"
runtime="${CODEX_BG_RUNTIME:-$codex_home/skill-runtimes/codex-bg}"
if command -v uv >/dev/null 2>&1; then
    export UV_PYTHON_INSTALL_DIR="${UV_PYTHON_INSTALL_DIR:-$codex_home/uv/python}"
    export UV_CACHE_DIR="${UV_CACHE_DIR:-$codex_home/uv/cache}"
    export UV_TOOL_DIR="${UV_TOOL_DIR:-$codex_home/uv/tools}"
    uv venv --python python3 "$runtime"
    uv pip install --python "$runtime/bin/python" 'websockets>=15,<17' tomli
else
    python3 -m venv "$runtime"
    "$runtime/bin/python" -m pip install --upgrade pip 'websockets>=15,<17' tomli
fi
printf 'Codex BG runtime ready: %s\n' "$runtime/bin/python"
