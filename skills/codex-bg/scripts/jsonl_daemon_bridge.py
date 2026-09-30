#!/usr/bin/env python3
"""Bridge VS Code's JSONL stdio transport to a shared app-server WebSocket."""

import asyncio
import datetime
import json
import os
import pathlib
import sqlite3
import sys
import time

import websockets

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib


def codex_home():
    return pathlib.Path(os.environ.get("CODEX_HOME", pathlib.Path.home() / ".codex"))


def read_config():
    config_path = codex_home() / "config.toml"
    try:
        return tomllib.loads(config_path.read_text())
    except (OSError, tomllib.TOMLDecodeError):
        return {}


def workspace_candidates():
    candidates = [
        os.environ.get("CODEX_WORKSPACE_ROOT"),
        os.getcwd(),
    ]
    candidates.extend(read_config().get("projects", {}).keys())
    workspaces = []
    seen = set()
    for candidate in candidates:
        if not candidate:
            continue
        workspace = str(pathlib.Path(candidate).resolve())
        if workspace not in seen:
            seen.add(workspace)
            workspaces.append(workspace)
    return workspaces


def current_provider_key():
    config = read_config()
    provider = config.get("model_provider") or "openai"
    return str(provider).lower()


def latest_thread_names():
    index_path = codex_home() / "session_index.jsonl"
    names = {}
    try:
        for line in index_path.read_text(errors="replace").splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            thread_id = row.get("id")
            thread_name = row.get("thread_name")
            if thread_id and thread_name:
                names[thread_id] = thread_name
    except OSError:
        pass
    return names


def parse_rollout_meta(path):
    try:
        with pathlib.Path(path).open(encoding="utf-8", errors="replace") as handle:
            row = json.loads(handle.readline())
    except (OSError, json.JSONDecodeError):
        return {}
    if row.get("type") != "session_meta":
        return {}
    return row.get("payload") or {}


def iso_from_unix(timestamp):
    return (
        datetime.datetime.fromtimestamp(timestamp, datetime.UTC)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def synthetic_thread_rows(existing_ids):
    database_path = codex_home() / "state_5.sqlite"
    if not database_path.is_file():
        return []
    workspaces = workspace_candidates()
    if not workspaces:
        return []
    provider_key = current_provider_key()
    names = latest_thread_names()
    workspace_placeholders = ", ".join("?" for _ in workspaces)
    query = """
        SELECT
            id, rollout_path, created_at, updated_at, source, model_provider,
            cwd, title, cli_version, thread_source, preview, recency_at,
            history_mode, agent_nickname, agent_role, git_sha, git_branch,
            git_origin_url
        FROM threads
        WHERE archived = 0
          AND source = 'vscode'
          AND cwd IN ({workspace_placeholders})
          AND lower(model_provider) = ?
          AND (thread_source IS NULL OR thread_source = 'user')
    """.format(workspace_placeholders=workspace_placeholders)
    rows = []
    try:
        connection = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        try:
            result_rows = list(connection.execute(query, (*workspaces, provider_key)))
        finally:
            connection.close()
    except sqlite3.Error:
        return []
    for row in result_rows:
        thread_id = row["id"]
        if thread_id in existing_ids:
            continue
        metadata = parse_rollout_meta(row["rollout_path"])
        thread_source = row["thread_source"]
        if thread_source in (None, "user"):
            thread_source = None
        git_info = None
        if row["git_sha"] or row["git_branch"] or row["git_origin_url"]:
            git_info = {
                "sha": row["git_sha"],
                "branch": row["git_branch"],
                "originUrl": row["git_origin_url"],
            }
        rows.append(
            {
                "id": thread_id,
                "extra": None,
                "sessionId": thread_id,
                "forkedFromId": metadata.get("forked_from_id"),
                "parentThreadId": metadata.get("parent_thread_id"),
                "preview": row["preview"] or "",
                "ephemeral": False,
                "historyMode": row["history_mode"] or "legacy",
                "modelProvider": row["model_provider"],
                "createdAt": row["created_at"],
                "updatedAt": row["updated_at"],
                "recencyAt": row["recency_at"] or row["updated_at"] or row["created_at"],
                "status": {"type": "notLoaded"},
                "path": row["rollout_path"],
                "cwd": row["cwd"],
                "cliVersion": row["cli_version"],
                "source": row["source"],
                "threadSource": thread_source,
                "agentNickname": row["agent_nickname"],
                "agentRole": row["agent_role"],
                "gitInfo": git_info,
                "name": names.get(thread_id) or row["title"] or thread_id,
                "turns": [],
            }
        )
    return rows


def augment_thread_list_response(message):
    try:
        response = json.loads(message)
    except json.JSONDecodeError:
        return message
    result = response.get("result")
    if not isinstance(result, dict) or not isinstance(result.get("data"), list):
        return message
    data = result["data"]
    existing_ids = {row.get("id") for row in data if isinstance(row, dict)}
    synthetic_rows = synthetic_thread_rows(existing_ids)
    if not synthetic_rows:
        return message
    data.extend(synthetic_rows)
    data.sort(key=lambda row: row.get("recencyAt") or row.get("updatedAt") or 0, reverse=True)
    result["nextCursor"] = None
    if data:
        oldest_created_at = min(row.get("createdAt") or 0 for row in data)
        if oldest_created_at:
            result["backwardsCursor"] = iso_from_unix(oldest_created_at)
    return json.dumps(response, ensure_ascii=False)


async def send_stdin(websocket, pending_thread_lists):
    reader = asyncio.StreamReader(limit=64 * 1024 * 1024)
    transport, _ = await asyncio.get_running_loop().connect_read_pipe(
        lambda: asyncio.StreamReaderProtocol(reader), sys.stdin.buffer
    )
    try:
        while line := await reader.readline():
            message = line.decode().rstrip("\r\n")
            try:
                request = json.loads(message)
            except json.JSONDecodeError:
                request = None
            if isinstance(request, dict) and request.get("method") == "thread/list":
                request_id = request.get("id")
                if request_id is not None:
                    pending_thread_lists.add(request_id)
            await websocket.send(message)
    finally:
        transport.close()


async def receive_stdout(websocket, pending_thread_lists):
    async for message in websocket:
        if isinstance(message, bytes):
            message = message.decode()
        try:
            response_id = json.loads(message).get("id")
        except (AttributeError, json.JSONDecodeError):
            response_id = None
        if response_id in pending_thread_lists:
            pending_thread_lists.discard(response_id)
            message = augment_thread_list_response(message)
        sys.stdout.write(message + "\n")
        sys.stdout.flush()


async def connect_websocket(socket_path):
    wait_for_socket = os.environ.get("CODEX_BRIDGE_WAIT_FOR_SOCKET") == "1"
    deadline = time.monotonic() + 360
    backoff = 0.02
    while True:
        try:
            return await websockets.unix_connect(
                socket_path,
                uri="ws://localhost",
                compression=None,
                max_size=None,
            )
        except OSError:
            if not wait_for_socket or time.monotonic() >= deadline:
                raise
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 0.5)


async def main():
    socket_path = os.environ.get(
        "CODEX_APP_SERVER_SOCKET",
        str(codex_home() / "app-server-control/app-server-control.sock"),
    )
    websocket = await connect_websocket(socket_path)
    try:
        marker_path = pathlib.Path(
            os.environ.get(
                "CODEX_FRONTEND_BRIDGE_MARKER",
                str(codex_home() / "app-server-control/frontend-bridge.inode"),
            )
        )
        marker_path.parent.mkdir(parents=True, exist_ok=True)
        marker_temporary = marker_path.with_name(f".{marker_path.name}.{os.getpid()}")
        marker_temporary.write_text(f"{os.stat(socket_path).st_ino}\n")
        os.replace(marker_temporary, marker_path)
        pending_thread_lists = set()
        sender = asyncio.create_task(send_stdin(websocket, pending_thread_lists))
        receiver = asyncio.create_task(receive_stdout(websocket, pending_thread_lists))
        done, pending = await asyncio.wait(
            (sender, receiver),
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
        for task in done:
            task.result()
    finally:
        await websocket.close()


if __name__ == "__main__":
    asyncio.run(main())
