#!/usr/bin/env python3
"""Verify that one thread is owned by the shared VS Code bridge."""

import argparse
import asyncio
import datetime
import fcntl
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import time

import websockets

SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
CODEX_HOME = pathlib.Path(os.environ.get("CODEX_HOME", pathlib.Path.home() / ".codex"))
VSCODE_LOGS = os.environ.get(
    "VSCODE_LOGS_ROOT",
    str(pathlib.Path(os.environ.get("VSCODE_AGENT_FOLDER", pathlib.Path.home() / ".vscode-server")) / "data/logs"),
)

parser = argparse.ArgumentParser()
parser.add_argument("--session-id", required=True)
parser.add_argument(
    "--socket",
    default=str(CODEX_HOME / "app-server-control/app-server-control.sock"),
)
parser.add_argument(
    "--bridge-marker",
    default=str(CODEX_HOME / "app-server-control/frontend-bridge.inode"),
)
parser.add_argument(
    "--bridge-script",
    default=str(SCRIPT_DIR / "jsonl_daemon_bridge.py"),
)
parser.add_argument(
    "--claim-root",
    default=str(CODEX_HOME / "app-server-control/thread-claims"),
)
parser.add_argument(
    "--config-guard",
    default=str(SCRIPT_DIR / "daemon_config_guard.py"),
)
parser.add_argument(
    "--vscode-logs",
    default=VSCODE_LOGS,
)
parser.add_argument("--rpc-timeout", type=float, default=30.0)
parser.add_argument("--lock-timeout", type=float, default=5.0)
args = parser.parse_args()


def cmdline(process_id):
    try:
        return pathlib.Path(f"/proc/{process_id}/cmdline").read_bytes().split(b"\0")
    except (FileNotFoundError, PermissionError, ProcessLookupError):
        return []


def process_ids(predicate):
    matches = []
    for process_path in pathlib.Path("/proc").glob("[0-9]*"):
        arguments = cmdline(process_path.name)
        if arguments and predicate(arguments):
            matches.append(int(process_path.name))
    return matches


def is_bridge(arguments):
    return args.bridge_script.encode() in arguments


def is_bundled(arguments):
    executable = arguments[0].decode(errors="replace")
    return (
        "/extensions/openai.chatgpt-" in executable
        and executable.endswith("/codex")
        and b"app-server" in arguments[1:]
    )


async def daemon_request(method, params):
    async with websockets.unix_connect(
        args.socket,
        uri="ws://localhost",
        compression=None,
        max_size=None,
    ) as websocket:
        await websocket.send(
            json.dumps(
                {
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "clientInfo": {
                            "name": "codex-frontend-scope",
                            "title": "Codex Frontend Scope",
                            "version": "1.0.0",
                        },
                        "capabilities": {"experimentalApi": True},
                    },
                }
            )
        )
        while True:
            initialized = json.loads(await websocket.recv())
            if initialized.get("id") == 1:
                if "error" in initialized:
                    raise RuntimeError(initialized["error"])
                break
        await websocket.send(json.dumps({"method": "initialized", "params": {}}))
        await websocket.send(
            json.dumps(
                {
                    "id": 2,
                    "method": method,
                    "params": params,
                }
            )
        )
        while True:
            response = json.loads(await websocket.recv())
            if response.get("id") == 2:
                if "error" in response:
                    raise RuntimeError(response["error"])
                return response["result"]


async def loaded_threads():
    return (await daemon_request("thread/loaded/list", {}))["data"]


async def resume_thread():
    await daemon_request("thread/resume", {"threadId": args.session_id})


def run_rpc(coroutine):
    return asyncio.run(asyncio.wait_for(coroutine, timeout=args.rpc_timeout))


def extension_log(parent_id):
    log_root = pathlib.Path(args.vscode_logs)
    marker = f"Extension host with pid {parent_id} started"
    for host_log in log_root.glob("**/remoteexthost.log"):
        try:
            if marker in host_log.read_text(errors="replace")[:4096]:
                candidate = host_log.parent / "openai.chatgpt" / "Codex.log"
                return candidate if candidate.is_file() else None
        except OSError:
            continue
    return None


def log_time(line):
    try:
        return datetime.datetime.fromisoformat(line[:23]).replace(
            tzinfo=datetime.timezone.utc
        ).timestamp()
    except ValueError:
        return None


def bundled_conflicts(process_id, claimed_at):
    try:
        parent_id = int(pathlib.Path(f"/proc/{process_id}/stat").read_text().split()[3])
    except (FileNotFoundError, PermissionError, ValueError):
        return "bundled-owner-unknown"
    log_path = extension_log(parent_id)
    if log_path is None:
        return "bundled-owner-unknown"
    active = False
    try:
        for line in log_path.read_text(errors="replace").splitlines():
            timestamp = log_time(line)
            if timestamp is None or timestamp <= claimed_at or args.session_id not in line:
                continue
            if "thread_stream_view_activity_changed active=false" in line:
                active = False
            elif "thread_stream_view_activity_changed active=true" in line:
                active = True
            elif "Reasoning summary turn-start" in line or (
                "Reasoning summary item completed" in line
                and f"threadId={args.session_id}" in line
            ):
                return "bundled-same-thread-writer"
    except OSError:
        return "bundled-owner-unknown"
    return "bundled-same-thread-active" if active else None


def fail(reason):
    print(reason)
    raise SystemExit(1)


def read_claim(claim_path):
    try:
        claim = json.loads(claim_path.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    if (
        not isinstance(claim, dict)
        or claim.get("session_id") != args.session_id
        or not isinstance(claim.get("socket_inode"), int)
        or not isinstance(claim.get("claimed_at"), (int, float))
        or not isinstance(claim.get("bridge_pids"), list)
        or not isinstance(claim.get("bridge_claimed_at"), (int, float))
    ):
        return None
    return claim


def claim_conflict(claimed_at):
    for bundled_id in process_ids(is_bundled):
        reason = bundled_conflicts(bundled_id, claimed_at)
        if reason:
            return f"{reason} pid={bundled_id}"
    return None


def acquire_lock(lock_path):
    lock_handle = lock_path.open("a+")
    deadline = time.monotonic() + args.lock_timeout
    while True:
        try:
            fcntl.flock(lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return lock_handle
        except BlockingIOError:
            if time.monotonic() >= deadline:
                lock_handle.close()
                fail("thread-claim-busy")
            time.sleep(0.05)


def config_guard_reason():
    guard_path = pathlib.Path(args.config_guard)
    if not guard_path.is_file():
        return "daemon-config-guard-missing"
    result = subprocess.run(
        [
            sys.executable,
            str(guard_path),
            "check",
            "--socket",
            args.socket,
        ],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode == 0:
        return None
    try:
        data = json.loads(result.stdout)
        return data.get("reason", "daemon-config-stale")
    except json.JSONDecodeError:
        return (result.stdout or result.stderr or "daemon-config-stale").strip()


socket_path = pathlib.Path(args.socket)
marker_path = pathlib.Path(args.bridge_marker)
if not socket_path.is_socket():
    fail("shared-daemon-missing")
config_reason = config_guard_reason()
if config_reason:
    fail(config_reason)
try:
    socket_inode = socket_path.stat().st_ino
    if int(marker_path.read_text().strip()) != socket_inode:
        fail("frontend-marker-stale")
    bridge_claimed_at = marker_path.stat().st_mtime
except (FileNotFoundError, PermissionError, ValueError):
    fail("frontend-reload-required")

bridge_ids = process_ids(is_bridge)
if not bridge_ids:
    fail("frontend-bridge-missing")

claim_root = pathlib.Path(args.claim_root)
claim_root.mkdir(parents=True, exist_ok=True)
claim_path = claim_root / f"{args.session_id}.json"
claim_lock = acquire_lock(claim_root / f".{args.session_id}.lock")
claim = read_claim(claim_path)
claimed_at = claim["claimed_at"] if claim else bridge_claimed_at

try:
    shared_threads = run_rpc(loaded_threads())
except Exception:
    fail("shared-daemon-unreachable")
if args.session_id not in shared_threads:
    # A stale claim is evidence that this exact thread was previously owned by
    # the shared core. It is not sufficient for wake by itself: require the
    # current config, socket, bridge, and absence of a bundled same-thread owner,
    # then re-materialize the thread read-only and verify it became loaded.
    if claim is None:
        fail("thread-not-loaded-on-shared-core")
    reason = claim_conflict(claimed_at)
    if reason:
        fail(reason)
    try:
        run_rpc(resume_thread())
        shared_threads = run_rpc(loaded_threads())
    except Exception:
        fail("thread-rehydrate-failed")
    if args.session_id not in shared_threads:
        fail("thread-rehydrate-not-loaded")

try:
    current_socket_inode = socket_path.stat().st_ino
    current_marker_inode = int(marker_path.read_text().strip())
    current_bridge_claimed_at = marker_path.stat().st_mtime
except (FileNotFoundError, PermissionError, ValueError):
    fail("frontend-reload-required")
if (
    current_socket_inode != socket_inode
    or current_marker_inode != socket_inode
    or current_bridge_claimed_at != bridge_claimed_at
):
    fail("shared-daemon-changed")

reason = claim_conflict(claimed_at)
if reason:
    fail(reason)

if (
    not claim
    or claim.get("socket_inode") != socket_inode
    or not set(claim.get("bridge_pids", [])) & set(bridge_ids)
    or claim.get("bridge_claimed_at") != bridge_claimed_at
):
    claim = {
        "session_id": args.session_id,
        "socket_inode": socket_inode,
        "bridge_pids": bridge_ids,
        "bridge_claimed_at": bridge_claimed_at,
        "claimed_at": claimed_at,
        "refreshed_at": time.time(),
    }
    with tempfile.NamedTemporaryFile(
        "w", dir=claim_root, prefix=f".{args.session_id}.", delete=False
    ) as temporary:
        json.dump(claim, temporary, sort_keys=True)
        temporary.write("\n")
        temporary_path = temporary.name
    os.replace(temporary_path, claim_path)
print("ready")
