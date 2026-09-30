#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
codex_home="${CODEX_HOME:-$HOME/.codex}"
codex_binary="${CODEX_BINARY:-$codex_home/packages/standalone/current/codex}"
[[ -x "$codex_binary" ]] || codex_binary="$(command -v codex || true)"
python_binary="${CODEX_BG_PYTHON:-$codex_home/skill-runtimes/codex-bg/bin/python}"
[[ -x "$python_binary" ]] || python_binary="$(command -v python3 || true)"
guard_script="${CODEX_GUARD_SCRIPT:-$script_dir/daemon_config_guard.py}"
activity_script="${CODEX_ACTIVITY_SCRIPT:-$script_dir/daemon_activity.py}"
network_env_script="${CODEX_NETWORK_ENV_SCRIPT:-$script_dir/network_env.sh}"
control_dir="${CODEX_CONTROL_DIR:-$codex_home/app-server-control}"
lifecycle_lock="${CODEX_LIFECYCLE_LOCK:-$control_dir/shared-codex-lifecycle.lock}"
daemon_socket="${CODEX_APP_SERVER_SOCKET:-$control_dir/app-server-control.sock}"
guard_state="${CODEX_DAEMON_CONFIG_STATE:-$control_dir/daemon-config.json}"
supervisor_log="${CODEX_SUPERVISOR_LOG:-$control_dir/supervisor.log}"
ready_timeout="${CODEX_DAEMON_READY_TIMEOUT:-360}"

mkdir -p "$control_dir"
exec 9>"$lifecycle_lock"
flock -n 9 || exit 0

if [[ -f "$supervisor_log" ]] && (( $(stat -c %s "$supervisor_log") > 2097152 )); then
    mv -f "$supervisor_log" "$supervisor_log.previous"
fi
exec >>"$supervisor_log" 2>&1
printf '%s supervisor start pid=%s\n' "$(date --iso-8601=seconds)" "$$"

[[ -x "$codex_binary" ]] || {
    echo "Codex binary is unavailable: $codex_binary"
    exit 1
}
[[ -x "$python_binary" ]] || {
    echo "Codex shared Python is unavailable: $python_binary"
    exit 1
}
# shellcheck source=/dev/null
source "$network_env_script"
prepare_codex_network

daemon_status() {
    CODEX_HOME="$codex_home" "$codex_binary" app-server daemon version 2>&1 || true
}

daemon_usable() {
    local status
    status="$(daemon_status)"
    [[ "$status" == *'"status":"running"'* ]] \
        && [[ "$status" == *'"backend":"pid"'* ]] \
        && [[ -S "$daemon_socket" ]]
}

daemon_process_alive() {
    local pid_file="$codex_home/app-server-daemon/app-server.pid"
    local pid
    [[ -r "$pid_file" ]] || return 1
    pid="$($python_binary -c 'import json,sys; p=sys.argv[1]; s=open(p).read().strip(); print(json.loads(s).get("pid") if s.startswith("{") else s)' "$pid_file" 2>/dev/null)" || return 1
    [[ "$pid" =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null
}

guard_ok() {
    "$python_binary" "$guard_script" check \
        --codex-home "$codex_home" --socket "$daemon_socket" --state "$guard_state" \
        >/dev/null 2>&1
}

record_guard() {
    "$python_binary" "$guard_script" record \
        --codex-home "$codex_home" --socket "$daemon_socket" --state "$guard_state" \
        >/dev/null
}

wait_ready() {
    local deadline=$((SECONDS + ready_timeout))
    while (( SECONDS < deadline )); do
        if daemon_usable; then
            return 0
        fi
        if ! daemon_process_alive; then
            return 2
        fi
        sleep 0.5
    done
    return 1
}

if daemon_usable && guard_ok; then
    echo "shared daemon already healthy"
    exit 0
fi

action=start
if daemon_usable; then
    activity_status=0
    activity_output="$($python_binary "$activity_script" --socket "$daemon_socket" --require-idle 2>&1)" \
        || activity_status=$?
    if (( activity_status == 2 )); then
        echo "daemon config changed during an active turn; restart deferred"
        printf '%s\n' "$activity_output"
        exit 0
    fi
    if (( activity_status != 0 )); then
        echo "unable to prove daemon idle; restart refused"
        printf '%s\n' "$activity_output"
        exit 1
    fi
    action=restart
fi

for attempt in 1 2 3; do
    if [[ "$action" == restart ]]; then
        echo "restarting idle shared daemon (attempt $attempt)"
        CODEX_HOME="$codex_home" "$codex_binary" -c features.code_mode_host=true \
            app-server daemon restart 9>&- || true
    elif ! daemon_process_alive; then
        echo "starting shared daemon (attempt $attempt)"
        CODEX_HOME="$codex_home" "$codex_binary" -c features.code_mode_host=true \
            app-server daemon start 9>&- || true
    else
        echo "shared daemon is still initializing; waiting"
    fi
    action=start

    wait_status=0
    wait_ready || wait_status=$?
    if (( wait_status == 0 )); then
        record_guard
        echo "shared daemon ready"
        exit 0
    fi
    if (( wait_status == 1 )); then
        echo "shared daemon is still alive after ${ready_timeout}s; leaving it undisturbed"
        exit 1
    fi
    echo "shared daemon exited before it became ready"
done

echo "shared daemon failed to become ready after 3 attempts"
exit 1
