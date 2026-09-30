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

The release was packaged and isolation-tested on 2026-09-30 against Codex CLI `0.153.0` and the
OpenAI VS Code extension `26.901.22334`. Shared-daemon/sidebar behavior remains version-sensitive,
so `skills/codex-bg/references/verification.md` is part of every upgrade.

## Security and state

Runtime data lives under `${CODEX_HOME:-$HOME/.codex}` and is excluded from this repository. Do not
commit Codex credentials, rollouts, thread IDs, sockets, or handoff logs. Direct networking is the
default; optional durable proxy configuration is documented without embedding proxy credentials.

## Relationship to codex-wake

[`codex-wake`](https://github.com/ZhaoRunyi/codex-wake) builds on the shared daemon and ownership
guards from this skill. Install `codex-bg` first when both are needed.
