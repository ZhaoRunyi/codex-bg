#!/usr/bin/env python3
"""Report whether the shared app-server owns any in-progress turns."""

import argparse
import asyncio
import json
import os
import pathlib
import sqlite3
import sys

import websockets


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


def reversed_lines(path: pathlib.Path, block_size: int = 64 * 1024):
    """Yield non-empty lines from a rollout without loading it into memory."""
    with path.open("rb") as rollout:
        rollout.seek(0, 2)
        position = rollout.tell()
        remainder = b""
        while position:
            read_size = min(block_size, position)
            position -= read_size
            rollout.seek(position)
            remainder = rollout.read(read_size) + remainder
            lines = remainder.split(b"\n")
            remainder = lines[0]
            for line in reversed(lines[1:]):
                if line:
                    yield line
        if remainder:
            yield remainder


def latest_task_event(path: pathlib.Path):
    """Return the latest persisted task boundary for one loaded thread."""
    for raw_line in reversed_lines(path):
        if b'"type":"task_' not in raw_line and b'"type":"turn_aborted"' not in raw_line:
            continue
        event = json.loads(raw_line)
        if event.get("type") != "event_msg":
            continue
        payload = event.get("payload", {})
        event_type = payload.get("type")
        if event_type == "task_started":
            return {"status": "inProgress", "turn_id": payload.get("turn_id")}
        if event_type in {"task_complete", "turn_aborted"}:
            return {"status": "completed", "turn_id": payload.get("turn_id")}
    return None


def rollout_paths(state_db: pathlib.Path, thread_ids: list[str]) -> dict[str, pathlib.Path]:
    if not thread_ids:
        return {}
    connection = sqlite3.connect(f"file:{state_db}?mode=ro", uri=True)
    try:
        placeholders = ",".join("?" for _ in thread_ids)
        rows = connection.execute(
            f"SELECT id, rollout_path FROM threads WHERE id IN ({placeholders})",
            thread_ids,
        )
        return {thread_id: pathlib.Path(path) for thread_id, path in rows}
    finally:
        connection.close()


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
                    "version": "1.0.0",
                },
                "capabilities": {"experimentalApi": True},
            },
        )
        await websocket.send(json.dumps({"method": "initialized", "params": {}}))
        loaded = await request(websocket, 2, "thread/loaded/list", {})
        thread_ids = loaded.get("data", [])
        paths = rollout_paths(pathlib.Path(args.state_db), thread_ids)
        missing = sorted(set(thread_ids) - set(paths))
        non_persisted_idle = []
        request_id = 10
        for thread_id in missing:
            try:
                await request(
                    websocket,
                    request_id,
                    "thread/read",
                    {"threadId": thread_id, "includeTurns": True},
                )
            except RuntimeError as exc:
                error_text = str(exc)
                known_non_persisted = (
                    "ephemeral threads do not support includeTurns" in error_text
                    or "is not materialized yet; includeTurns is unavailable before first user message"
                    in error_text
                )
                if not known_non_persisted:
                    raise RuntimeError(
                        f"missing rollout path for loaded thread {thread_id}: {error_text}"
                    ) from exc
            else:
                raise RuntimeError(
                    f"loaded thread {thread_id} is readable but has no rollout mapping"
                )
            request_id += 1
            metadata = await request(
                websocket,
                request_id,
                "thread/read",
                {"threadId": thread_id, "includeTurns": False},
            )
            request_id += 1
            thread = metadata.get("thread", {})
            status = thread.get("status", {}).get("type")
            if thread.get("id") != thread_id or status != "idle":
                raise RuntimeError(
                    f"non-persisted loaded thread {thread_id} is not safely idle: "
                    f"reported_id={thread.get('id')!r}, status={status!r}"
                )
            non_persisted_idle.append(thread_id)
    active = []
    for thread_id in (thread_id for thread_id in thread_ids if thread_id in paths):
        path = paths[thread_id]
        if not path.is_file():
            raise RuntimeError(f"rollout is unavailable for loaded thread {thread_id}: {path}")
        event = latest_task_event(path)
        if event is None:
            raise RuntimeError(f"no task boundary in rollout for loaded thread {thread_id}")
        if event["status"] == "inProgress":
            active.append({"thread_id": thread_id, "turn_id": event["turn_id"]})
    return active, non_persisted_idle


def main():
    codex_home = pathlib.Path(os.environ.get("CODEX_HOME", pathlib.Path.home() / ".codex"))
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--socket",
        default=str(codex_home / "app-server-control/app-server-control.sock"),
    )
    parser.add_argument("--state-db", default=str(codex_home / "state_5.sqlite"))
    parser.add_argument("--require-idle", action="store_true")
    args = parser.parse_args()

    try:
        active_turns, non_persisted_idle = asyncio.run(inspect(args))
    except Exception as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, sort_keys=True))
        raise SystemExit(1)

    print(
        json.dumps(
            {
                "status": "active" if active_turns else "idle",
                "active_turns": active_turns,
                "non_persisted_idle_threads": non_persisted_idle,
            },
            sort_keys=True,
        )
    )
    if args.require_idle and active_turns:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
