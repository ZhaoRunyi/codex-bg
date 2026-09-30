---
name: codex-bg
description: Keep one VS Code Codex sidebar thread running on a persistent shared app-server while VS Code or Remote SSH reconnects, then let the sidebar watch and steer that same active turn. Use for background handoff of the current thread, shared-daemon setup, or reconnect troubleshooting; not for starting unrelated detached Codex jobs.
---

# Codex Background Handoff

Keep one thread core and attach two clients to it:

```text
shared Codex app-server
├── VS Code sidebar bridge
└── detached background client
```

Never resume the same thread through two independent app-servers. Never kill an active turn merely
to refresh configuration.

## Route

1. For first-time installation or a Codex/extension upgrade, read
   [setup.md](references/setup.md), bootstrap the runtime, configure the sidebar wrapper, reload the
   window, and complete the smoke test.
2. To hand off the current thread, read [handoff.md](references/handoff.md). The root agent must use
   its own `CODEX_THREAD_ID`; a subagent cannot launch the real handoff for the parent thread.
3. For ownership conflicts, stale configuration, reconnect failures, or multiple windows, read
   [troubleshooting.md](references/troubleshooting.md).
4. Before claiming support for a new Codex or extension build, run the checks in
   [verification.md](references/verification.md).

## Handoff contract

- Preserve the user's exact continuation prompt when supplied.
- Use `scripts/start_handoff.sh --dry-run` when process ownership is ambiguous.
- A real launch is successful only after the launcher reports `Armed`; inspect the handoff log for
  `action=started` or `action=steered` to prove that continuation began.
- After `Armed`, stop substantive foreground work and return control. The shared daemon owns the
  continuing turn.
- Keep runtime state under `${CODEX_HOME:-$HOME/.codex}`. Do not commit credentials, rollouts,
  daemon sockets, markers, logs, or thread IDs.

## Scope

The bundled implementation targets Linux/POSIX hosts with VS Code Remote SSH, Git, Python 3.10+,
`screen`, and a Codex build that exposes `app-server daemon`. The sidebar setting used by this
integration is an implementation surface rather than a stable cross-version promise; revalidate
after every Codex or OpenAI VS Code extension upgrade.
