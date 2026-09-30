#!/usr/bin/env bash
set -euo pipefail

default_prompt="Review the current state and continue the task in the user's language."
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
codex_home="${CODEX_HOME:-$HOME/.codex}"
codex_binary="${CODEX_BINARY:-$codex_home/packages/standalone/current/codex}"
[[ -x "$codex_binary" ]] || codex_binary="$(command -v codex || true)"
python_binary="${CODEX_BG_PYTHON:-$codex_home/skill-runtimes/codex-bg/bin/python}"
[[ -x "$python_binary" ]] || python_binary="$(command -v python3 || true)"
daemon_socket="$codex_home/app-server-control/app-server-control.sock"
marker_script="$script_dir/thread_marker.py"
wake_script="$script_dir/wake_thread.py"
bridge_script="$script_dir/jsonl_daemon_bridge.py"
frontend_scope_script="$script_dir/frontend_scope.py"
config_guard_script="$script_dir/daemon_config_guard.py"
network_env_script="$script_dir/network_env.sh"
# shellcheck source=/dev/null
source "$network_env_script"
session_id="${CODEX_THREAD_ID:-}"
app_server_pid=""
transport_pid=""
workdir="$PWD"
prompt="$default_prompt"
log_path=""
screen_name=""
proxy_url="${HTTPS_PROXY:-${https_proxy:-}}"
dry_run=false
run_child=false
shared_mode=false

is_app_server() {
    local pid="$1"
    [[ "$pid" =~ ^[0-9]+$ ]] || return 1
    [[ "$(ps -p "$pid" -o comm= 2>/dev/null)" == "codex" ]] || return 1
    tr '\0' '\n' <"/proc/$pid/cmdline" 2>/dev/null | grep -Fxq "app-server"
}

is_ssh_transport() {
    local pid="$1"
    [[ "$pid" =~ ^[0-9]+$ ]] || return 1
    [[ "$(ps -p "$pid" -o comm= 2>/dev/null)" == "sshd" ]] || return 1
    ps -p "$pid" -o args= 2>/dev/null | grep -q '@'
}

frontend_bridge_ready() {
    "$python_binary" "$frontend_scope_script" \
        --session-id "$session_id" \
        --socket "$daemon_socket" >/dev/null
}

wait_for_managed_daemon() {
    local status
    for _ in {1..240}; do
        status="$("$codex_binary" -c features.code_mode_host=true \
            app-server daemon version 2>/dev/null || true)"
        if grep -q '"status":"running"' <<<"$status" \
            && grep -q '"backend":"pid"' <<<"$status" \
            && [[ -S "$daemon_socket" ]]; then
            return 0
        fi
        sleep 0.5
    done
    echo "Managed Codex daemon did not become ready." >&2
    return 1
}

find_transport_pid() {
    local pid="$1"
    local parent commit args command_pid candidate best="" best_delta="" delta
    local app_age candidate_age

    while ((pid > 1)); do
        is_ssh_transport "$pid" && { printf '%s\n' "$pid"; return 0; }
        pid="$(ps -p "$pid" -o ppid= 2>/dev/null | tr -d ' ')" || return 1
    done

    pid="$1"
    while ((pid > 1)); do
        args="$(ps -p "$pid" -o args= 2>/dev/null || true)"
        if [[ "$args" =~ /(Stable-|code-)([0-9a-f]{40})(/|[[:space:]]) ]]; then
            commit="${BASH_REMATCH[2]}"
            break
        fi
        pid="$(ps -p "$pid" -o ppid= 2>/dev/null | tr -d ' ')" || return 1
    done
    [[ -n "${commit:-}" ]] || return 1
    app_age="$(ps -p "$1" -o etimes= | tr -d ' ')"
    while read -r command_pid; do
        [[ -r "/proc/$command_pid/cmdline" ]] || continue
        mapfile -d '' -t command_args <"/proc/$command_pid/cmdline" 2>/dev/null || continue
        [[ "${command_args[0]:-}" == *"/code-$commit" ]] || continue
        printf '%s\n' "${command_args[@]:1}" | grep -Fxq "command-shell" || continue
        candidate="$command_pid"
        while ((candidate > 1)); do
            is_ssh_transport "$candidate" && break
            candidate="$(ps -p "$candidate" -o ppid= 2>/dev/null | tr -d ' ')" || break
        done
        is_ssh_transport "$candidate" || continue
        candidate_age="$(ps -p "$candidate" -o etimes= | tr -d ' ')"
        delta=$((candidate_age - app_age))
        ((delta < 0)) && delta=$((-delta))
        if [[ -z "$best_delta" ]] || ((delta < best_delta)); then
            best="$candidate"
            best_delta="$delta"
        elif ((delta == best_delta)) && [[ "$candidate" != "$best" ]]; then
            best=""
        fi
    done < <(ps -eo pid=)
    [[ -n "$best" ]] && printf '%s\n' "$best"
}

usage() {
    cat <<'EOF'
Usage: start_handoff.sh [options]
  --session-id ID
  --app-server-pid PID
  --transport-pid PID
  --workdir PATH
  --prompt TEXT
  --log PATH
  --screen-name NAME
  --proxy-url URL
  --dry-run
EOF
}

while (($#)); do
    case "$1" in
        --session-id) session_id="$2"; shift 2 ;;
        --app-server-pid) app_server_pid="$2"; shift 2 ;;
        --transport-pid) transport_pid="$2"; shift 2 ;;
        --workdir) workdir="$2"; shift 2 ;;
        --prompt) prompt="$2"; shift 2 ;;
        --log) log_path="$2"; shift 2 ;;
        --screen-name) screen_name="$2"; shift 2 ;;
        --proxy-url) proxy_url="$2"; shift 2 ;;
        --dry-run) dry_run=true; shift ;;
        --run-child) run_child=true; shift ;;
        --shared-mode) shared_mode=true; shift ;;
        --help|-h) usage; exit 0 ;;
        *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
    esac
done

[[ "$session_id" =~ ^[0-9a-fA-F-]{36}$ ]] || {
    echo "A UUID-shaped Codex session ID is required." >&2
    exit 2
}
[[ -d "$workdir" ]] || { echo "Workdir not found: $workdir" >&2; exit 2; }
[[ -x "$codex_binary" ]] || { echo "Managed Codex binary is missing." >&2; exit 2; }
[[ -x "$python_binary" ]] || { echo "Shared Codex Python is missing." >&2; exit 2; }
command -v screen >/dev/null || { echo "screen is required." >&2; exit 2; }

if [[ -S "$daemon_socket" ]]; then
    shared_mode=true
elif [[ -z "$app_server_pid" ]]; then
    mapfile -t app_server_pids < <(
        while read -r pid; do
            is_app_server "$pid" && printf '%s\n' "$pid"
        done < <(ps -eo pid=,comm= | awk '$2 == "codex" {print $1}')
    )
    ((${#app_server_pids[@]} == 1)) || {
        echo "Expected one initial VS Code app-server; pass --app-server-pid." >&2
        exit 2
    }
    app_server_pid="${app_server_pids[0]}"
fi

if ! $shared_mode; then
    is_app_server "$app_server_pid" || {
        echo "PID $app_server_pid is not a running Codex app-server." >&2
        exit 2
    }
    [[ -n "$transport_pid" ]] \
        || transport_pid="$(find_transport_pid "$app_server_pid" || true)"
    is_ssh_transport "$transport_pid" || {
        echo "Could not identify the current VS Code SSH transport." >&2
        exit 2
    }
fi

short_session="${session_id:0:8}"
timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
state_dir="$codex_home/handoffs"
[[ -n "$screen_name" ]] || screen_name="codex_handoff_$short_session"
[[ -n "$log_path" ]] || log_path="$state_dir/${short_session}_${timestamp}.log"
marker_path="$state_dir/${session_id}.marker.json"

if $dry_run; then
    printf 'session_id=%s\nmode=%s\napp_server_pid=%s\ntransport_pid=%s\n' \
        "$session_id" "$($shared_mode && echo shared || echo initial)" \
        "${app_server_pid:-<daemon>}" "${transport_pid:-<daemon>}"
    printf 'screen_name=%s\nlog=%s\nprompt=%s\n' "$screen_name" "$log_path" "$prompt"
    exit 0
fi

mkdir -p "$state_dir"
prepare_codex_network

if ! $run_child; then
    frontend_bridge_ready || {
        echo "This thread is not safely owned by the shared VS Code bridge." >&2
        "$python_binary" "$frontend_scope_script" \
            --session-id "$session_id" --socket "$daemon_socket" >&2 || true
        exit 2
    }
    screen -ls | grep -Fq ".$screen_name" && {
        echo "Screen already exists: $screen_name" >&2
        exit 2
    }
    "$codex_binary" -c features.code_mode_host=true app-server daemon start >/dev/null || true
    wait_for_managed_daemon
    "$python_binary" "$marker_script" --session-id "$session_id" >"$marker_path"
    "$python_binary" "$wake_script" \
        --session-id "$session_id" \
        --prompt "$prompt" \
        --marker "$marker_path" \
        --probe-only
    child_args=(
        --run-child
        --session-id "$session_id"
        --workdir "$workdir"
        --prompt "$prompt"
        --log "$log_path"
        --screen-name "$screen_name"
        --proxy-url "$proxy_url"
    )
    if $shared_mode; then
        child_args+=(--shared-mode)
    else
        child_args+=(--app-server-pid "$app_server_pid" --transport-pid "$transport_pid")
    fi
    screen -dmS "$screen_name" "$0" "${child_args[@]}"
    sleep 1
    screen -ls | grep -F ".$screen_name" | grep -Fq "(Detached)" || {
        echo "Handoff child did not remain alive; inspect $log_path" >&2
        exit 2
    }
    printf 'Armed shared-thread handoff %s; continuation status: %s\n' \
        "$screen_name" "$log_path"
    exit 0
fi

{
    exec 9>"$state_dir/${session_id}.lock"
    flock -n 9 || { echo "Another handoff owns session $session_id"; exit 2; }
    if ! $shared_mode; then
        echo "Waiting for initial VS Code SSH transport PID $transport_pid to exit."
        while is_ssh_transport "$transport_pid"; do
            sleep 1
        done
        if is_app_server "$app_server_pid"; then
            kill -TERM "$app_server_pid" 2>/dev/null || true
            timeout 15 tail --pid="$app_server_pid" -f /dev/null || true
            is_app_server "$app_server_pid" && kill -KILL "$app_server_pid" 2>/dev/null || true
        fi
    fi
    if [[ -n "$proxy_url" ]]; then
        export HTTP_PROXY="$proxy_url" HTTPS_PROXY="$proxy_url"
        export http_proxy="$proxy_url" https_proxy="$proxy_url"
    fi
    "$codex_binary" -c features.code_mode_host=true app-server daemon start 9>&- || true
    wait_for_managed_daemon
    "$python_binary" "$config_guard_script" record >/dev/null
    echo "Shared daemon ready; waiting for the arming turn to finish."
    cd "$workdir"
    "$python_binary" "$wake_script" \
        --session-id "$session_id" \
        --prompt "$prompt" \
        --marker "$marker_path" \
        --wait-active
} >>"$log_path" 2>&1
