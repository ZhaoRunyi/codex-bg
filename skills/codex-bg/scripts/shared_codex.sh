#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
codex_home="${CODEX_HOME:-$HOME/.codex}"
codex_binary="${CODEX_BINARY:-$codex_home/packages/app-server-daemon/current/bin/codex}"
bridge_script="$script_dir/jsonl_daemon_bridge.py"
python_binary="${CODEX_BG_PYTHON:-$codex_home/skill-runtimes/codex-bg/bin/python}"
[[ -x "$python_binary" ]] || python_binary="$(command -v python3 || true)"
proxy_env_script="${CODEX_BG_PROXY_ENV_SCRIPT:-$script_dir/codex_proxy_env.sh}"
supervisor_script="$script_dir/shared_codex_supervisor.sh"
sync_script="$script_dir/sync_vscode_codex.py"
daemon_socket="${CODEX_APP_SERVER_SOCKET:-$codex_home/app-server-control/app-server-control.sock}"

[[ -f "$proxy_env_script" ]] || {
    echo "Codex proxy environment is unavailable: $proxy_env_script" >&2
    exit 1
}
# shellcheck source=/dev/null
source "$proxy_env_script"
ensure_codex_proxy

# The extension may use a container-local directory for private IPC, but every
# Codex child must use the persistent shared home.
export CODEX_HOME="$codex_home"

if [[ " $* " == *" app-server "* ]]; then
    [[ -x "$python_binary" ]] || {
        echo "Codex shared bridge Python is unavailable: $python_binary" >&2
        exit 1
    }

    if [[ -f "$sync_script" ]]; then
        if ! "$python_binary" "$sync_script" ensure \
            >>"$codex_home/app-server-control/sync-activation.log" 2>&1; then
            echo "Codex extension/daemon synchronization failed; trying the existing daemon" >&2
        fi
        setsid "$python_binary" "$sync_script" watch </dev/null >/dev/null 2>&1 &
    fi

    fast_path_status="$("$codex_binary" app-server daemon version 2>&1)" || true
    if [[ "$fast_path_status" == *'"status":"running"'* ]] \
        && [[ -S "$daemon_socket" ]]; then
        exec "$python_binary" "$bridge_script"
    fi

    [[ -x "$supervisor_script" ]] || {
        echo "Codex shared supervisor is unavailable: $supervisor_script" >&2
        exit 1
    }
    setsid bash "$supervisor_script" </dev/null >/dev/null 2>&1 &
    export CODEX_BRIDGE_WAIT_FOR_SOCKET=1
    exec "$python_binary" "$bridge_script"
fi

[[ -x "$codex_binary" ]] || codex_binary="$(command -v codex || true)"
[[ -x "$codex_binary" ]] || {
    echo "Codex binary is unavailable; install Codex or run the sidebar once." >&2
    exit 1
}
exec "$codex_binary" "$@"
