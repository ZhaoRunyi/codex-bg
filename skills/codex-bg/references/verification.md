# Verification

After installing or upgrading Codex/the sidebar extension, verify in an expendable thread:

1. two bridge clients initialize against one daemon;
2. client B resumes client A's materialized thread, including a response larger than 1 MiB;
3. a thread-scoped event from one client reaches the other;
4. an active background turn survives bridge disconnect and reconnect;
5. a sidebar follow-up is accepted as `turn/steer`;
6. neither reconnect path sends a signal to the background process;
7. changing Codex configuration restarts only an idle daemon and is refused during an active turn.
8. the newest extension's bundled CLI and the managed daemon report the same version;
9. two concurrent same-thread `thread/resume` requests with identical effective parameters cause
   one daemon resume and return under both request IDs, while differing parameters remain separate;
10. a `codex-wake` client resumes a large historical thread with catalog `path` plus `excludeTurns`,
    then loads only its newest turn without blocking unrelated daemon RPCs;
11. an extension update during an active turn, sidebar connection, or managed non-turn RPC is
    patched but daemon replacement is deferred and retried after all leases drain;
12. thread-list augmentation preserves daemon cursors and does not re-inject first-page rows on
    later pages;
13. `start_handoff.sh --dry-run` succeeds without a live transport, and the real initial handoff
    refuses SIGKILL if graceful shutdown times out.

Record the tested Codex CLI version, OpenAI VS Code extension version, host OS, and whether Remote SSH
was used. The 2026-10-11 reference deployment passed the large-history resume test with an 849 MB
rollout on Codex CLI `0.162.0-alpha.17.2` and extension `26.1007.21434-linux-x64`. Do not generalize
that result into native Windows or macOS support.
