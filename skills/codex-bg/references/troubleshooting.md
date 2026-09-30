# Troubleshooting

- **Target thread is not bridge-owned:** reload its VS Code window after configuring
  `chatgpt.cliExecutable`. An unrelated bundled app-server in another window is not itself a
  conflict; activity for the same thread is.
- **Daemon configuration is stale:** reload through `shared_codex.sh`. The wrapper restarts only an
  idle daemon. It preserves an active turn and defers restart.
- **Runtime import fails:** rerun `scripts/bootstrap_runtime.sh` or set `CODEX_BG_PYTHON` to a Python
  containing `websockets`.
- **Codex binary is not found:** set `CODEX_BINARY` to the exact executable used by the extension.
- **VS Code logs are elsewhere:** set `VSCODE_LOGS_ROOT` or `VSCODE_AGENT_FOLDER`.
- **Multiple windows:** a successful thread claim is scoped to the target thread and socket inode.
  Reload or close only a window that later reopens or writes that same thread; do not kill unrelated
  app-servers.
- **Container restart:** daemon sockets and active handoffs are ephemeral. Sessions and configuration
  remain under `CODEX_HOME`, but a watcher or detached process must be armed again.
