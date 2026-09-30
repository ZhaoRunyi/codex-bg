#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
codex_home="${CODEX_HOME:-$HOME/.codex}"
codex_binary="${CODEX_BINARY:-$codex_home/packages/standalone/current/codex}"
[[ -x "$codex_binary" ]] || codex_binary="$(command -v codex || true)"
bridge_script="$script_dir/jsonl_daemon_bridge.py"
guard_script="$script_dir/daemon_config_guard.py"
activity_script="$script_dir/daemon_activity.py"
python_binary="${CODEX_BG_PYTHON:-$codex_home/skill-runtimes/codex-bg/bin/python}"
[[ -x "$python_binary" ]] || python_binary="$(command -v python3 || true)"
network_env_script="$script_dir/network_env.sh"
supervisor_script="$script_dir/shared_codex_supervisor.sh"
daemon_socket="${CODEX_APP_SERVER_SOCKET:-$codex_home/app-server-control/app-server-control.sock}"

# shellcheck source=/dev/null
source "$network_env_script"
prepare_codex_network

if [[ " $* " == *" app-server "* ]]; then
    [[ -x "$python_binary" ]] || {
        echo "Codex shared bridge Python is unavailable: $python_binary" >&2
        exit 1
    }
    fast_path_status="$(
        CODEX_HOME="$codex_home" \
            "$codex_binary" app-server daemon version 2>&1
    )" || true
    if [[ "$fast_path_status" == *'"status":"running"'* ]] \
        && [[ "$fast_path_status" == *'"backend":"pid"'* ]] \
        && [[ -S "$daemon_socket" ]] \
        && "$python_binary" "$guard_script" check >/dev/null 2>&1; then
        exec "$python_binary" "$bridge_script"
    fi
    [[ -x "$supervisor_script" ]] || {
        echo "Codex shared supervisor is unavailable: $supervisor_script" >&2
        exit 1
    }
    daemon_usable=false
    if [[ "$fast_path_status" == *'"status":"running"'* ]] \
        && [[ "$fast_path_status" == *'"backend":"pid"'* ]] \
        && [[ -S "$daemon_socket" ]]; then
        daemon_usable=true
    fi
    if $daemon_usable; then
        activity_status=0
        activity_output="$("$python_binary" "$activity_script" --require-idle 2>&1)" \
            || activity_status=$?
        if ((activity_status == 2)); then
            echo "Codex daemon config changed during an active turn; restart deferred" >&2
            exec "$python_binary" "$bridge_script"
        fi
        if ((activity_status != 0)); then
            echo "Unable to prove Codex daemon idle; preserving the running daemon" >&2
            printf '%s\n' "$activity_output" >&2
            exec "$python_binary" "$bridge_script"
        fi
    fi
    setsid bash "$supervisor_script" </dev/null >/dev/null 2>&1 &
    export CODEX_BRIDGE_WAIT_FOR_SOCKET=1
    exec "$python_binary" "$bridge_script"
fi

exec "$codex_binary" "$@"
