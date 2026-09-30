# Handoff

Use the current root thread and current working directory:

```bash
skill_root="${CODEX_HOME:-$HOME/.codex}/skills/codex-bg"
"$skill_root/scripts/start_handoff.sh" \
  --session-id "$CODEX_THREAD_ID" \
  --workdir "$PWD" \
  --prompt 'Overall task and exact next action.'
```

The launcher verifies that the thread is loaded through the shared bridge, performs a read-only
resume probe, creates a detached `screen`, and waits for the foreground turn to finish before it
starts or steers the continuation.

Useful options:

- `--dry-run`: resolve the thread, app-server, transport, log, and screen without launching.
- `--proxy-url URL`: use a durable proxy for the detached client.
- `--log PATH` and `--screen-name NAME`: override runtime names when necessary.

Inspect `${CODEX_HOME:-$HOME/.codex}/handoffs`. `Armed` proves that the detached owner is alive;
`action=started` or `action=steered` proves that the continuation RPC was accepted.
