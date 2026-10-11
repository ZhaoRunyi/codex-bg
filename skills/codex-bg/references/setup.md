# Setup

## Prerequisites

- Linux/POSIX remote host with `bash`, `screen`, Python 3.11+, and Codex.
- VS Code with the OpenAI Codex extension connected to that host.
- The `codex-bg` skill installed at `${CODEX_HOME:-$HOME/.codex}/skills/codex-bg`.

Run once:

```bash
"${CODEX_HOME:-$HOME/.codex}/skills/codex-bg/scripts/bootstrap_runtime.sh"
```

The runtime is stored outside the skill at
`${CODEX_HOME:-$HOME/.codex}/skill-runtimes/codex-bg`. Set `CODEX_BG_PYTHON` only when an
equivalent Python environment already provides `websockets`.

## Configure the sidebar

The wrapper's synchronizer changes `chatgpt.cliExecutable` to machine scope in the newest installed
remote extension and patches its internal CLI fallback and frontend IPC path. It stores backups of
both `package.json` and `extension.js` under `${CODEX_HOME:-$HOME/.codex}/extension-backups`, then reapplies the patch after extension
upgrades. In Remote Settings, set:

```json
{
  "chatgpt.cliExecutable": "/absolute/remote/path/.codex/skills/codex-bg/scripts/shared_codex.sh",
  "chatgpt.followUpQueueMode": "steer"
}
```

VS Code JSON settings do not expand shell variables. Obtain the absolute path with
`printf '%s\n' "${CODEX_HOME:-$HOME/.codex}"`.

Run the synchronizer once before the first reload:

```bash
"${CODEX_BG_PYTHON:-${CODEX_HOME:-$HOME/.codex}/skill-runtimes/codex-bg/bin/python}" \
  "${CODEX_HOME:-$HOME/.codex}/skills/codex-bg/scripts/sync_vscode_codex.py" ensure
```

This is a host-wide Remote SSH integration for the current Unix account, not a per-workspace patch.
Review that scope before installation. The watcher patches future extension versions immediately,
then retries daemon replacement until sidebar, managed RPC, and active-turn leases are all idle.
`sync_vscode_codex.py patch` never starts or replaces a daemon.

Run **Developer: Reload Window**. Confirm that the target window starts `shared_codex.sh`, not the
extension's bundled `codex app-server` command.

## Network configuration

Direct network access is the default. For a durable proxy, set `CODEX_PROXY_URL` (HTTP/HTTPS),
`CODEX_ALL_PROXY_URL`, and optionally `CODEX_PROXY_PREPARE_COMMAND` in the environment that launches
the remote extension host. Do not rely on a tunnel whose lifetime is tied to the SSH connection the
background turn must survive.

Keep the only persistent copy of Codex state and packages below `CODEX_HOME`. Socket, PID, and lock
files are deliberately host-local and reconstructible; moving the persistent home to a new mount
must not copy stale runtime locks.
