# codex-bg

A Codex skill for moving one live VS Code Codex sidebar thread onto a persistent shared app-server.
The detached turn survives sidebar and Remote SSH reconnects, while the sidebar continues to watch
and steer the same turn.

This repository contains one skill at `skills/codex-bg`. It is **not** a Codex plugin.

## Install

Ask Codex:

```text
Use $skill-installer to install ZhaoRunyi/codex-bg from path skills/codex-bg.
```

Restart Codex after installation, then run:

```bash
"${CODEX_HOME:-$HOME/.codex}/skills/codex-bg/scripts/bootstrap_runtime.sh"
```

Read `skills/codex-bg/references/setup.md` before changing the VS Code sidebar executable. The skill
currently targets Linux/POSIX remote hosts and VS Code Remote SSH. Revalidate it after Codex or the
OpenAI VS Code extension is upgraded.

The current implementation was synchronized from a working Remote SSH deployment on 2026-10-11
against Codex CLI `0.162.0-alpha.17.2` and the OpenAI VS Code extension
`26.1007.21434-linux-x64`. The wrapper reapplies its version-sensitive extension patch on activation
and updates the persistent daemon from the newest compatible installed extension only after all
sidebar, managed RPC, and active-turn leases drain. These versions are a tested baseline rather
than an upper bound; `skills/codex-bg/references/verification.md` remains mandatory after every
upgrade.

## Security and state

Runtime data lives under `${CODEX_HOME:-$HOME/.codex}` and is excluded from this repository. Do not
commit Codex credentials, rollouts, thread IDs, sockets, or handoff logs. Direct networking is the
default; optional durable proxy configuration is documented without embedding proxy credentials.

## Relationship to codex-wake

[`codex-wake`](https://github.com/ZhaoRunyi/codex-wake) builds on the shared daemon and ownership
guards from this skill. Install `codex-bg` first when both are needed.
