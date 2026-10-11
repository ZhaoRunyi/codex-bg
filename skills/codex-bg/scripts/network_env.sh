#!/usr/bin/env bash

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Compatibility shim for installations that still source network_env.sh.
# shellcheck source=/dev/null
source "$script_dir/codex_proxy_env.sh"

prepare_codex_network() {
    ensure_codex_proxy
}
