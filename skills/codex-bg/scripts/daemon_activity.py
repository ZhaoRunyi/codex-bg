#!/usr/bin/env python3
"""Report whether the shared app-server owns any in-progress loaded turns."""

import argparse
import asyncio
import json
import os
import pathlib

import websockets

from runtime_coordination import shared_daemon_operation


async def request(websocket, request_id, method, params):
    await websocket.send(
        json.dumps({"id": request_id, "method": method, "params": params})
    )
    while True:
        message = json.loads(await websocket.recv())
        if message.get("id") == request_id:
            if "error" in message:
                raise RuntimeError(message["error"])
            return message["result"]


async def inspect(args):
    async with websockets.unix_connect(
        args.socket,
        uri="ws://localhost",
        compression=None,
        max_size=None,
    ) as websocket:
        await request(
            websocket,
            1,
            "initialize",
            {
                "clientInfo": {
                    "name": "codex-daemon-activity",
                    "title": "Codex Daemon Activity Guard",
                    "version": "3.0.0",
                },
                "capabilities": {"experimentalApi": True},
            },
        )
        await websocket.send(json.dumps({"method": "initialized", "params": {}}))
        thread_ids = (
            await request(websocket, 2, "thread/loaded/list", {})
        ).get("data", [])

        active = []
        idle = []
        request_id = 10
        for thread_id in thread_ids:
            result = await request(
                websocket,
                request_id,
                "thread/turns/list",
                {
                    "threadId": thread_id,
                    "limit": 1,
                    "sortDirection": "desc",
                    "itemsView": "notLoaded",
                },
            )
            request_id += 1
            turns = result.get("data", [])
            latest = turns[0] if turns else None
            status = latest.get("status") if latest else "noTurns"
            entry = {
                "thread_id": thread_id,
                "turn_id": latest.get("id") if latest else None,
                "status": status,
            }
            if status == "inProgress":
                active.append(entry)
            elif status in {"completed", "interrupted", "failed", "noTurns"}:
                idle.append(entry)
            else:
                raise RuntimeError(
                    f"unknown latest-turn status for loaded thread {thread_id}: {status!r}"
                )
        return active, idle


def main():
    codex_home = pathlib.Path(
        os.environ.get("CODEX_HOME", pathlib.Path.home() / ".codex")
    )
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--socket",
        default=str(codex_home / "app-server-control/app-server-control.sock"),
    )
    # Retained for compatibility with existing callers; live RPC is authoritative.
    parser.add_argument("--state-db", default=str(codex_home / "state_5.sqlite"))
    parser.add_argument("--require-idle", action="store_true")
    args = parser.parse_args()

    try:
        with shared_daemon_operation():
            active_turns, idle_threads = asyncio.run(
                asyncio.wait_for(inspect(args), timeout=30)
            )
    except Exception as exc:
        print(
            json.dumps(
                {"status": "error", "error": str(exc) or type(exc).__name__},
                sort_keys=True,
            )
        )
        raise SystemExit(1)

    print(
        json.dumps(
            {
                "status": "active" if active_turns else "idle",
                "active_turns": active_turns,
                "idle_threads": idle_threads,
            },
            sort_keys=True,
        )
    )
    if args.require_idle and active_turns:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
