#!/usr/bin/env bash

# Direct networking is the portable default. A durable proxy can be supplied by
# the host through these variables; no endpoint or credential is embedded here.
CODEX_PROXY_URL="${CODEX_PROXY_URL:-}"
CODEX_ALL_PROXY_URL="${CODEX_ALL_PROXY_URL:-}"
CODEX_NO_PROXY="${CODEX_NO_PROXY:-127.0.0.1,localhost,::1}"
CODEX_PROXY_CHECK_URL="${CODEX_PROXY_CHECK_URL:-https://chatgpt.com/backend-api/codex/responses}"

export CODEX_PROXY_URL CODEX_ALL_PROXY_URL CODEX_NO_PROXY CODEX_PROXY_CHECK_URL
if [[ -n "$CODEX_PROXY_URL" ]]; then
    export HTTP_PROXY="$CODEX_PROXY_URL" HTTPS_PROXY="$CODEX_PROXY_URL"
    export http_proxy="$CODEX_PROXY_URL" https_proxy="$CODEX_PROXY_URL"
fi
if [[ -n "$CODEX_ALL_PROXY_URL" ]]; then
    export ALL_PROXY="$CODEX_ALL_PROXY_URL" all_proxy="$CODEX_ALL_PROXY_URL"
fi
export NO_PROXY="$CODEX_NO_PROXY" no_proxy="$CODEX_NO_PROXY"

ensure_codex_proxy() {
    if [[ -n "${CODEX_PROXY_PREPARE_COMMAND:-}" ]]; then
        bash -lc "$CODEX_PROXY_PREPARE_COMMAND"
    fi
    [[ -n "$CODEX_PROXY_URL" ]] || return 0
    command -v curl >/dev/null || {
        echo "curl is required to validate the configured Codex proxy" >&2
        return 1
    }
    for _ in {1..10}; do
        if curl --silent --show-error --head --connect-timeout 5 --max-time 10 \
            --proxy "$CODEX_PROXY_URL" "$CODEX_PROXY_CHECK_URL" >/dev/null; then
            return 0
        fi
        sleep 2
    done
    echo "Codex endpoint is unreachable through $CODEX_PROXY_URL" >&2
    return 1
}
