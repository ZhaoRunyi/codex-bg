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
        "[codex-bg] The handoff became ready after this thread received a new message. "
        "Reread the complete conversation before continuing. Absorb compatible constraints and "
        "new state, and stop the handed-off task only if the user explicitly canceled, replaced, "
        "or completed it. Ask only when the conversation cannot resolve a real scope conflict, "
        "missing authority, or a critical missing fact."
        f"\n\nHandoff task: {prompt}"
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
                    "name": "codex-bg-handoff",
                    "title": "Codex BG Handoff",
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
