# Troubleshooting

- **Target thread is not bridge-owned:** reload its VS Code window after configuring
  `chatgpt.cliExecutable`. An unrelated bundled app-server in another window is not itself a
  conflict; activity for the same thread is.
- **Daemon configuration is stale:** reload through `shared_codex.sh`. The wrapper restarts only an
  idle daemon. It preserves an active turn and defers restart.
- **Conversation title appears but hydration spins:** inspect the bridge and daemon logs. The bridge
  should add the catalog rollout path and coalesce same-thread retries. Wake clients additionally use
  `excludeTurns` and then ask only for the newest turn. Do not delete or rewrite the rollout.
- **Extension and daemon versions differ:** reload once all loaded turns are idle. The synchronizer
  deliberately refuses to replace a daemon while any latest turn is `inProgress` or RPC status is
  unknown.
- **Runtime import fails:** rerun `scripts/bootstrap_runtime.sh` or set `CODEX_BG_PYTHON` to a Python
  containing `websockets`.
- **Codex binary is not found:** set `CODEX_BINARY` to the exact executable used by the extension.
- **VS Code logs are elsewhere:** set `CODEX_VSCODE_LOG_ROOT` for the ownership
  guard; set `CODEX_VSCODE_EXTENSION_ROOT` for extension discovery.
- **Multiple windows:** a successful thread claim is scoped to the target thread and socket inode.
  Reload or close only a window that later reopens or writes that same thread; do not kill unrelated
  app-servers.
- **Container restart:** daemon sockets and active handoffs are ephemeral. Sessions, configuration,
  packages, and watcher definitions remain under `CODEX_HOME`; the wrapper recreates runtime IPC,
  but a detached process that died with the container must be armed again.
