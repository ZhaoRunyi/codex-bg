# Verification

After installing or upgrading Codex/the sidebar extension, verify in an expendable thread:

1. two bridge clients initialize against one daemon;
2. client B resumes client A's materialized thread, including a response larger than 1 MiB;
3. a thread-scoped event from one client reaches the other;
4. an active background turn survives bridge disconnect and reconnect;
5. a sidebar follow-up is accepted as `turn/steer`;
6. neither reconnect path sends a signal to the background process;
7. changing Codex configuration restarts only an idle daemon and is refused during an active turn.

Record the tested Codex CLI version, OpenAI VS Code extension version, host OS, and whether Remote SSH
was used. Do not generalize a smoke test into native Windows or macOS support.
