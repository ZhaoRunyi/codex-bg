#!/usr/bin/env python3
"""Start or steer one turn through the shared Codex app-server daemon."""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import itertools
import json
import os
import pathlib
import sqlite3
import subprocess
import sys
import time
import uuid

import websockets

from runtime_coordination import acquire_lock, release_lock, shared_daemon_operation


CODEX_HOME = pathlib.Path(os.environ.get("CODEX_HOME", pathlib.Path.home() / ".codex"))
SCRIPT_DIR = pathlib.Path(__file__).resolve().parent

parser = argparse.ArgumentParser()
parser.add_argument("--session-id", required=True)
parser.add_argument("--prompt", required=True)
parser.add_argument("--marker", required=True)
parser.add_argument("--wait-active", action="store_true")
parser.add_argument("--probe-only", action="store_true")
parser.add_argument("--codex-home", default=str(CODEX_HOME))
parser.add_argument("--config-guard", default=str(SCRIPT_DIR / "daemon_config_guard.py"))
parser.add_argument(
    "--socket", default=str(CODEX_HOME / "app-server-control/app-server-control.sock")
)
parser.add_argument(
    "--rpc-timeout",
    type=float,
    default=float(os.environ.get("CODEX_WAKE_RPC_TIMEOUT", "30")),
)
parser.add_argument(
    "--resume-timeout",
    type=float,
    default=float(os.environ.get("CODEX_WAKE_RESUME_TIMEOUT", "900")),
)
args = parser.parse_args()


def require_current_daemon_config() -> None:
    result = subprocess.run(
        [
            sys.executable,
            args.config_guard,
            "check",
            "--codex-home",
            args.codex_home,
            "--socket",
            args.socket,
        ],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=max(args.rpc_timeout, 1),
    )
    if result.returncode == 0:
        return
    try:
        reason = json.loads(result.stdout).get("reason", "daemon-config-stale")
    except json.JSONDecodeError:
        reason = (result.stdout or result.stderr or "daemon-config-stale").strip()
    raise RuntimeError(f"daemon-config-not-current: {reason}")


def latest_in_progress_turn(thread: dict) -> dict | None:
    turns = thread.get("turns", [])
    if not turns:
        return None
    latest_turn = turns[-1]
    return latest_turn if latest_turn.get("status") == "inProgress" else None


def current_prompt() -> tuple[str, bool]:
    marker_script = SCRIPT_DIR / "thread_marker.py"
    current_marker = json.loads(
        subprocess.check_output(
            [
                sys.executable,
                str(marker_script),
                "--session-id",
                args.session_id,
                "--codex-home",
                args.codex_home,
            ],
            text=True,
            timeout=max(args.rpc_timeout, 1),
        )
    )
    armed_marker = json.loads(pathlib.Path(args.marker).read_text())
    changed = current_marker["last_user_sha256"] != armed_marker["last_user_sha256"]
    if not changed:
        return args.prompt, False
    return (
        "[codex-wake] 外部条件已满足，且等待期间 thread 收到了新消息。"
        "请先重读完整对话，并把下列 watcher 任务作为一项独立、持续的责任来判断。"
        "不要仅因出现更新消息就搁置它，也不要一味服从最后一条无关消息：无关消息"
        "不影响本任务；新增约束或状态变化应被吸收并据此调整实现；若原条件、方案或"
        "探针已过时，应取消旧 watcher、建立适配新情况的 watcher 并继续推进；只有"
        "用户明确针对本任务取消、替换或确认完成时才终止。无法从任务作用域和对话"
        "消解真实冲突、缺少必要授权或关键线索时，才停下询问。"
        f"\n\nWatcher 任务：{args.prompt}",
        True,
    )


def acquire_resume_lock():
    safe_thread_id = "".join(c for c in args.session_id.lower() if c in "0123456789abcdef-")
    if safe_thread_id != args.session_id.lower():
        raise RuntimeError("session ID contains unsafe lock-path characters")
    return acquire_lock(f"wake-resume-{safe_thread_id}.lock", exclusive=True)


def rollout_path_for_thread() -> str | None:
    database = pathlib.Path(args.codex_home) / "state_5.sqlite"
    if not database.is_file():
        return None
    try:
        connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
        try:
            columns = {row[1] for row in connection.execute("PRAGMA table_info(threads)")}
            if not {"id", "rollout_path"}.issubset(columns):
                return None
            row = connection.execute(
                "SELECT rollout_path FROM threads WHERE id = ?", (args.session_id,)
            ).fetchone()
        finally:
            connection.close()
    except sqlite3.Error:
        return None
    if not row or not row[0]:
        return None
    path = pathlib.Path(row[0])
    return str(path) if path.is_absolute() and path.is_file() else None


async def request(websocket, request_id: int, method: str, params: dict, timeout: float):
    await websocket.send(json.dumps({"id": request_id, "method": method, "params": params}))
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(f"{method} timed out after {timeout:g}s")
        raw = await asyncio.wait_for(websocket.recv(), timeout=remaining)
        message = json.loads(raw)
        if message.get("id") == request_id:
            return message


def result_or_raise(message: dict) -> dict:
    if "error" in message:
        raise RuntimeError(message["error"])
    return message.get("result", {})


async def newest_turn(websocket, request_ids) -> dict | None:
    response = await request(
        websocket,
        next(request_ids),
        "thread/turns/list",
        {
            "threadId": args.session_id,
            "limit": 1,
            "sortDirection": "desc",
            "itemsView": "notLoaded",
        },
        args.rpc_timeout,
    )
    turns = result_or_raise(response).get("data", [])
    return turns[0] if turns else None


async def wait_until_turn_finishes(websocket, request_ids, turn_id: str) -> None:
    while True:
        latest = await newest_turn(websocket, request_ids)
        if latest is None or latest.get("id") != turn_id or latest.get("status") != "inProgress":
            return
        await asyncio.sleep(1)


async def run() -> None:
    prompt, thread_changed = current_prompt()
    request_ids = itertools.count(1)
    resume_lock = acquire_resume_lock()
    try:
        async with websockets.unix_connect(
            args.socket, uri="ws://localhost", compression=None, max_size=None
        ) as websocket:
            initialized = await request(
                websocket,
                next(request_ids),
                "initialize",
                {
                    "clientInfo": {
                        "name": "codex-wake",
                        "title": "Codex Wake",
                        "version": "2.0.0",
                    },
                    "capabilities": {"experimentalApi": True},
                },
                args.rpc_timeout,
            )
            result_or_raise(initialized)
            await websocket.send(json.dumps({"method": "initialized", "params": {}}))

            loaded = await request(
                websocket,
                next(request_ids),
                "thread/loaded/list",
                {},
                args.rpc_timeout,
            )
            already_loaded = args.session_id in result_or_raise(loaded).get("data", [])
            if not already_loaded:
                resume_params = {"threadId": args.session_id, "excludeTurns": True}
                rollout_path = rollout_path_for_thread()
                if rollout_path is not None:
                    resume_params["path"] = rollout_path
                resumed = await request(
                    websocket,
                    next(request_ids),
                    "thread/resume",
                    resume_params,
                    args.resume_timeout,
                )
                result_or_raise(resumed)
            latest = await newest_turn(websocket, request_ids)
            release_lock(resume_lock)
            resume_lock = None

            thread = {"id": args.session_id, "turns": [latest] if latest else []}
            if args.probe_only:
                print(
                    json.dumps(
                        {
                            "action": "probed",
                            "serialized_bytes": len(json.dumps(thread).encode()),
                            "thread_changed": thread_changed,
                        },
                        sort_keys=True,
                    ),
                    flush=True,
                )
                return

            active_turn = latest_in_progress_turn(thread)
            if active_turn and args.wait_active:
                await wait_until_turn_finishes(websocket, request_ids, active_turn["id"])
                active_turn = None

            user_input = [{"type": "text", "text": prompt}]
            client_id = str(uuid.uuid4())
            if active_turn:
                response = await request(
                    websocket,
                    next(request_ids),
                    "turn/steer",
                    {
                        "threadId": args.session_id,
                        "expectedTurnId": active_turn["id"],
                        "clientUserMessageId": client_id,
                        "input": user_input,
                    },
                    args.rpc_timeout,
                )
                action = "steered"
                turn_id = active_turn["id"]
            else:
                response = await request(
                    websocket,
                    next(request_ids),
                    "turn/start",
                    {
                        "threadId": args.session_id,
                        "clientUserMessageId": client_id,
                        "input": user_input,
                    },
                    args.rpc_timeout,
                )
                action = "started"
                turn_id = result_or_raise(response).get("turn", {}).get("id")
            result_or_raise(response)
            print(
                json.dumps(
                    {"action": action, "thread_changed": thread_changed, "turn_id": turn_id},
                    sort_keys=True,
                ),
                flush=True,
            )
    finally:
        release_lock(resume_lock)


def main() -> None:
    if args.rpc_timeout <= 0 or args.resume_timeout <= 0:
        raise SystemExit("RPC timeouts must be positive")
    with shared_daemon_operation():
        require_current_daemon_config()
        asyncio.run(run())


if __name__ == "__main__":
    main()
