#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
codex_home="${CODEX_HOME:-$HOME/.codex}"
python_binary="${CODEX_BG_PYTHON:-$codex_home/skill-runtimes/codex-bg/bin/python}"
[[ -x "$python_binary" ]] || python_binary="$(command -v python3 || true)"
proxy_env_script="${CODEX_BG_PROXY_ENV_SCRIPT:-$script_dir/codex_proxy_env.sh}"
sync_script="$script_dir/sync_vscode_codex.py"
supervisor_log="$codex_home/app-server-control/supervisor.log"

mkdir -p "$(dirname "$supervisor_log")"
if [[ -f "$supervisor_log" ]] && (( $(stat -c %s "$supervisor_log") > 2097152 )); then
    mv -f "$supervisor_log" "$supervisor_log.previous"
fi
exec >>"$supervisor_log" 2>&1
printf '%s supervisor start pid=%s\n' "$(date --iso-8601=seconds)" "$$"

[[ -x "$python_binary" ]] || {
    echo "Codex shared Python is unavailable: $python_binary"
    exit 1
}
[[ -f "$proxy_env_script" ]] || {
    echo "Codex proxy environment is unavailable: $proxy_env_script"
    exit 1
}
[[ -f "$sync_script" ]] || {
    echo "Codex extension synchronizer is unavailable: $sync_script"
    exit 1
}

# shellcheck source=/dev/null
source "$proxy_env_script"
ensure_codex_proxy
export CODEX_HOME="$codex_home"

"$python_binary" "$sync_script" ensure
setsid "$python_binary" "$sync_script" watch </dev/null >/dev/null 2>&1 &
echo "shared daemon ready"
