#!/usr/bin/env python3
"""Start or steer one turn through the shared Codex app-server daemon."""

import argparse
import asyncio
import json
import os
import pathlib
import subprocess
import sys
import uuid

import websockets


SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
CODEX_HOME = pathlib.Path(os.environ.get("CODEX_HOME", pathlib.Path.home() / ".codex"))

parser = argparse.ArgumentParser()
parser.add_argument("--session-id", required=True)
parser.add_argument("--prompt", required=True)
parser.add_argument("--marker", required=True)
parser.add_argument("--wait-active", action="store_true")
parser.add_argument("--probe-only", action="store_true")
parser.add_argument("--codex-home", default=str(CODEX_HOME))
parser.add_argument(
    "--config-guard",
    default=str(SCRIPT_DIR / "daemon_config_guard.py"),
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
        resumed = await request(
            websocket,
            2,
            "thread/resume",
            {"threadId": args.session_id},
        )
        if "error" in resumed:
            raise RuntimeError(resumed["error"])
        thread = resumed["result"]["thread"]
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
                3,
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
                3,
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
