#!/usr/bin/env python3
"""Start or steer one turn through the shared Codex app-server daemon."""

import argparse
import asyncio
import fcntl
import json
import os
import pathlib
import sqlite3
import subprocess
import sys
import uuid

import websockets


CODEX_HOME = pathlib.Path(
    os.environ.get("CODEX_HOME", pathlib.Path.home() / ".codex")
)
CODEX_BG_ROOT = pathlib.Path(
    os.environ.get("CODEX_BG_SKILL_ROOT", CODEX_HOME / "skills/codex-bg")
)

parser = argparse.ArgumentParser()
parser.add_argument("--session-id", required=True)
parser.add_argument("--prompt", required=True)
parser.add_argument("--marker", required=True)
parser.add_argument("--wait-active", action="store_true")
parser.add_argument("--probe-only", action="store_true")
parser.add_argument("--codex-home", default=str(CODEX_HOME))
parser.add_argument(
    "--config-guard",
    default=str(CODEX_BG_ROOT / "scripts/daemon_config_guard.py"),
)
parser.add_argument(
    "--socket",
    default=str(CODEX_HOME / "app-server-control/app-server-control.sock"),
)
args = parser.parse_args()


def require_current_daemon_config():
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
    )
    if result.returncode == 0:
        return
    try:
        reason = json.loads(result.stdout).get("reason", "daemon-config-stale")
    except json.JSONDecodeError:
        reason = (result.stdout or result.stderr or "daemon-config-stale").strip()
    raise SystemExit(f"daemon-config-not-current: {reason}")


def latest_in_progress_turn(thread):
    """Return the current turn without reviving inherited orphan history."""
    turns = thread.get("turns", [])
    if not turns:
        return None
    latest_turn = turns[-1]
    if latest_turn.get("status") != "inProgress":
        return None
    return latest_turn


require_current_daemon_config()

marker_script = pathlib.Path(__file__).with_name("thread_marker.py")
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
    )
)
armed_marker = json.loads(pathlib.Path(args.marker).read_text())
thread_changed = (
    current_marker["last_user_sha256"] != armed_marker["last_user_sha256"]
)


prompt = args.prompt
if thread_changed:
    prompt = (
        "[codex-wake] 外部条件已满足，且等待期间 thread 收到了新消息。"
        "请先重读完整对话，并把下列 watcher 任务作为一项独立、持续的责任来判断。"
        "不要仅因出现更新消息就搁置它，也不要一味服从最后一条无关消息：无关消息"
        "不影响本任务；新增约束或状态变化应被吸收并据此调整实现；若原条件、方案或"
        "探针已过时，应取消旧 watcher、建立适配新情况的 watcher 并继续推进；只有"
        "用户明确针对本任务取消、替换或确认完成时才终止。无法从任务作用域和对话"
        "消解真实冲突、缺少必要授权或关键线索时，才停下询问。"
        f"\n\nWatcher 任务：{prompt}"
    )


async def request(websocket, request_id, method, params):
    await websocket.send(
        json.dumps({"id": request_id, "method": method, "params": params})
    )
    while True:
        message = json.loads(await websocket.recv())
        if message.get("id") == request_id:
            return message


def acquire_resume_lock():
    configured = os.environ.get("XDG_RUNTIME_DIR")
    candidate = pathlib.Path(configured) if configured else pathlib.Path(f"/run/user/{os.getuid()}")
    try:
        info = candidate.stat()
        runtime_root = candidate if candidate.is_dir() and info.st_uid == os.getuid() else None
    except OSError:
        runtime_root = None
    if runtime_root is None:
        runtime_root = pathlib.Path("/tmp") / f"codex-runtime-{os.getuid()}"
        runtime_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(runtime_root, 0o700)
    lock_directory = runtime_root / "codex-shared"
    lock_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(lock_directory, 0o700)
    lock_handle = (lock_directory / "wake-resume.lock").open("a+")
    fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
    return lock_handle


def rollout_path_for_thread():
    database = pathlib.Path(args.codex_home) / "state_5.sqlite"
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        row = connection.execute(
            "SELECT rollout_path FROM threads WHERE id = ?", (args.session_id,)
        ).fetchone()
    finally:
        connection.close()
    if not row or not row[0]:
        return None
    rollout_path = pathlib.Path(row[0])
    if not rollout_path.is_absolute() or not rollout_path.is_file():
        return None
    return str(rollout_path)


async def main():
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
                    "name": "codex-wake",
                    "title": "Codex Wake",
                    "version": "1.0.0",
                },
                "capabilities": {"experimentalApi": True},
            },
        )
        await websocket.send(json.dumps({"method": "initialized", "params": {}}))
        resume_lock = acquire_resume_lock()
        try:
            loaded = await request(websocket, 2, "thread/loaded/list", {})
            if "error" in loaded:
                raise RuntimeError(loaded["error"])
            already_loaded = args.session_id in loaded.get("result", {}).get(
                "data", []
            )
            if not already_loaded:
                resume_params = {
                    "threadId": args.session_id,
                    "excludeTurns": True,
                }
                rollout_path = rollout_path_for_thread()
                if rollout_path is not None:
                    resume_params["path"] = rollout_path
                resumed = await asyncio.wait_for(
                    request(websocket, 3, "thread/resume", resume_params),
                    timeout=900,
                )
                if "error" in resumed:
                    raise RuntimeError(resumed["error"])
            latest = await request(
                websocket,
                3 if already_loaded else 4,
                "thread/turns/list",
                {
                    "threadId": args.session_id,
                    "limit": 1,
                    "sortDirection": "desc",
                    "itemsView": "notLoaded",
                },
            )
            if "error" in latest:
                raise RuntimeError(latest["error"])
            thread = {
                "id": args.session_id,
                "turns": latest.get("result", {}).get("data", []),
            }
        finally:
            fcntl.flock(resume_lock.fileno(), fcntl.LOCK_UN)
            resume_lock.close()
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
        user_input = [{"type": "text", "text": prompt}]
        client_id = str(uuid.uuid4())
        if active_turn and args.wait_active:
            async for raw_message in websocket:
                message = json.loads(raw_message)
                if (
                    message.get("method") == "turn/completed"
                    and message.get("params", {}).get("turn", {}).get("id")
                    == active_turn["id"]
                ):
                    break
            active_turn = None
        if active_turn:
            response = await request(
                websocket,
                5,
                "turn/steer",
                {
                    "threadId": args.session_id,
                    "expectedTurnId": active_turn["id"],
                    "clientUserMessageId": client_id,
                    "input": user_input,
                },
            )
            action = "steered"
            turn_id = active_turn["id"]
        else:
            response = await request(
                websocket,
                5,
                "turn/start",
                {
                    "threadId": args.session_id,
                    "clientUserMessageId": client_id,
                    "input": user_input,
                },
            )
            action = "started"
            turn_id = response.get("result", {}).get("turn", {}).get("id")
        if "error" in response:
            raise RuntimeError(response["error"])
        print(
            json.dumps(
                {
                    "action": action,
                    "thread_changed": thread_changed,
                    "turn_id": turn_id,
                },
                sort_keys=True,
            ),
            flush=True,
        )


asyncio.run(main())
