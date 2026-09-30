# Setup

## Prerequisites

- Linux/POSIX remote host with `bash`, `screen`, Python 3.10+, and Codex.
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

`chatgpt.cliExecutable` may have application scope in the installed extension. Use a dedicated
VS Code `--user-data-dir` when isolation from other local windows is required. In that instance's
User Settings, set:

```json
{
  "chatgpt.cliExecutable": "/absolute/remote/path/.codex/skills/codex-bg/scripts/shared_codex.sh",
  "chatgpt.followUpQueueMode": "steer"
}
```

VS Code JSON settings do not expand shell variables. Obtain the absolute path with
`printf '%s\n' "${CODEX_HOME:-$HOME/.codex}"`.

If a dedicated user-data directory is not possible, do not patch an installed extension silently.
Explain the scope tradeoff and obtain approval before changing extension metadata or machine-wide
settings. Extension upgrades can overwrite such a patch.

Run **Developer: Reload Window**. Confirm that the target window starts `shared_codex.sh`, not the
extension's bundled `codex app-server` command.

## Network configuration

Direct network access is the default. For a durable proxy, set `CODEX_PROXY_URL` (HTTP/HTTPS),
`CODEX_ALL_PROXY_URL`, and optionally `CODEX_PROXY_PREPARE_COMMAND` in the environment that launches
the remote extension host. Do not rely on a tunnel whose lifetime is tied to the SSH connection the
background turn must survive.
