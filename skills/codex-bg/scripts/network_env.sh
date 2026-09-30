#!/usr/bin/env bash

prepare_codex_network() {
    if [[ -n "${CODEX_PROXY_PREPARE_COMMAND:-}" ]]; then
        bash -lc "$CODEX_PROXY_PREPARE_COMMAND"
    fi

    local proxy_url="${CODEX_PROXY_URL:-}"
    if [[ -n "$proxy_url" ]]; then
        export HTTP_PROXY="$proxy_url" HTTPS_PROXY="$proxy_url"
        export http_proxy="$proxy_url" https_proxy="$proxy_url"
    fi
    if [[ -n "${CODEX_ALL_PROXY_URL:-}" ]]; then
        export ALL_PROXY="$CODEX_ALL_PROXY_URL"
        export all_proxy="$CODEX_ALL_PROXY_URL"
    fi
}
