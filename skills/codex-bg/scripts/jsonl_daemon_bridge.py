#!/usr/bin/env python3
"""Bridge VS Code's JSONL stdio transport to a shared app-server WebSocket."""

from __future__ import annotations

import asyncio
import datetime
import json
import os
import pathlib
import sqlite3
import sys
import time
import tomllib
from typing import Any

import websockets

from runtime_coordination import frontend_client


def codex_home() -> pathlib.Path:
    return pathlib.Path(os.environ.get("CODEX_HOME", pathlib.Path.home() / ".codex"))


def read_config() -> dict:
    try:
        return tomllib.loads((codex_home() / "config.toml").read_text())
    except (OSError, tomllib.TOMLDecodeError):
        return {}


def workspace_candidates() -> list[str]:
    candidates = [os.environ.get("CODEX_WORKSPACE_ROOT"), os.getcwd()]
    candidates.extend(read_config().get("projects", {}).keys())
    workspaces = []
    seen = set()
    for candidate in candidates:
        if candidate:
            workspace = str(pathlib.Path(candidate).resolve())
            if workspace not in seen:
                seen.add(workspace)
                workspaces.append(workspace)
    return workspaces


def current_provider_key() -> str:
    return str(read_config().get("model_provider") or "openai").lower()


def latest_thread_names() -> dict[str, str]:
    names = {}
    try:
        lines = (codex_home() / "session_index.jsonl").read_text(errors="replace").splitlines()
    except OSError:
        return names
    for line in lines:
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if row.get("id") and row.get("thread_name"):
            names[row["id"]] = row["thread_name"]
    return names


def parse_rollout_meta(path: str | None) -> dict:
    if not path:
        return {}
    try:
        with pathlib.Path(path).open(encoding="utf-8", errors="replace") as handle:
            row = json.loads(handle.readline())
    except (OSError, json.JSONDecodeError):
        return {}
    return row.get("payload") or {} if row.get("type") == "session_meta" else {}


def thread_table_columns(connection: sqlite3.Connection) -> set[str]:
    return {row[1] for row in connection.execute("PRAGMA table_info(threads)")}


def rollout_path_for_thread(thread_id: str) -> str | None:
    database_path = codex_home() / "state_5.sqlite"
    try:
        connection = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True)
        try:
            if not {"id", "rollout_path"}.issubset(thread_table_columns(connection)):
                return None
            row = connection.execute(
                "SELECT rollout_path FROM threads WHERE id = ?", (thread_id,)
            ).fetchone()
        finally:
            connection.close()
    except sqlite3.Error:
        return None
    if not row or not row[0]:
        return None
    path = pathlib.Path(row[0])
    return str(path) if path.is_absolute() and path.is_file() else None


def iso_from_unix(timestamp: int | float) -> str:
    return (
        datetime.datetime.fromtimestamp(timestamp, datetime.UTC)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


SYNTHETIC_FIELDS: dict[str, str] = {
    "created_at": "0",
    "updated_at": "0",
    "source": "'vscode'",
    "model_provider": "'openai'",
    "title": "NULL",
    "cli_version": "NULL",
    "thread_source": "NULL",
    "preview": "''",
    "recency_at": "0",
    "history_mode": "'legacy'",
    "agent_nickname": "NULL",
    "agent_role": "NULL",
    "git_sha": "NULL",
    "git_branch": "NULL",
    "git_origin_url": "NULL",
}


def synthetic_thread_rows(existing_ids: set[str], limit: int = 200) -> list[dict]:
    database_path = codex_home() / "state_5.sqlite"
    workspaces = workspace_candidates()
    if not database_path.is_file() or not workspaces:
        return []
    names = latest_thread_names()
    try:
        connection = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        try:
            columns = thread_table_columns(connection)
            required = {"id", "rollout_path", "cwd", "model_provider"}
            if not required.issubset(columns):
                return []
            selects = ["id", "rollout_path", "cwd"]
            for field, default in SYNTHETIC_FIELDS.items():
                selects.append(field if field in columns else f"{default} AS {field}")
            conditions = [f"cwd IN ({', '.join('?' for _ in workspaces)})", "lower(model_provider) = ?"]
            parameters: list[Any] = [*workspaces, current_provider_key()]
            if "archived" in columns:
                conditions.append("archived = 0")
            if "source" in columns:
                conditions.append("source = 'vscode'")
            if "thread_source" in columns:
                conditions.append("(thread_source IS NULL OR thread_source = 'user')")
            order = "recency_at" if "recency_at" in columns else "updated_at"
            if order not in columns:
                order = "created_at" if "created_at" in columns else "id"
            query = (
                f"SELECT {', '.join(selects)} FROM threads WHERE {' AND '.join(conditions)} "
                f"ORDER BY {order} DESC LIMIT ?"
            )
            result_rows = list(connection.execute(query, (*parameters, limit)))
        finally:
            connection.close()
    except sqlite3.Error:
        return []

    rows = []
    for row in result_rows:
        thread_id = row["id"]
        if thread_id in existing_ids:
            continue
        metadata = parse_rollout_meta(row["rollout_path"])
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
                "threadSource": None if row["thread_source"] in (None, "user") else row["thread_source"],
                "agentNickname": row["agent_nickname"],
                "agentRole": row["agent_role"],
                "gitInfo": git_info,
                "name": names.get(thread_id) or row["title"] or thread_id,
                "turns": [],
            }
        )
    return rows


def is_first_thread_page(params: dict) -> bool:
    return not any(params.get(name) for name in ("cursor", "backwardsCursor"))


def augment_thread_list_response(
    message: str, params: dict, injected_thread_ids: set[str]
) -> str:
    try:
        response = json.loads(message)
    except json.JSONDecodeError:
        return message
    result = response.get("result")
    if not isinstance(result, dict) or not isinstance(result.get("data"), list):
        return message
    data = result["data"]
    if not is_first_thread_page(params):
        result["data"] = [
            row for row in data if not isinstance(row, dict) or row.get("id") not in injected_thread_ids
        ]
        return json.dumps(response, ensure_ascii=False)

    existing_ids = {row.get("id") for row in data if isinstance(row, dict)}
    requested_limit = params.get("limit", 50)
    try:
        synthetic_limit = max(1, min(int(requested_limit) * 4, 200))
    except (TypeError, ValueError):
        synthetic_limit = 200
    synthetic_rows = synthetic_thread_rows(existing_ids, synthetic_limit)
    if not synthetic_rows:
        return message
    data.extend(synthetic_rows)
    injected_thread_ids.update(row["id"] for row in synthetic_rows)
    data.sort(key=lambda row: row.get("recencyAt") or row.get("updatedAt") or 0, reverse=True)
    # Preserve the daemon's cursors exactly; synthetic first-page rows must not destroy pagination.
    if data and "backwardsCursor" not in result:
        oldest_created_at = min(row.get("createdAt") or 0 for row in data)
        if oldest_created_at:
            result["backwardsCursor"] = iso_from_unix(oldest_created_at)
    return json.dumps(response, ensure_ascii=False)


def canonical_resume_key(params: dict) -> str:
    return json.dumps(params, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


async def expire_resume(
    primary_id: Any,
    timeout: float,
    pending_resumes: dict[str, Any],
    resume_key_by_request: dict[Any, str],
    resume_waiters: dict[Any, list[Any]],
) -> None:
    await asyncio.sleep(timeout)
    key = resume_key_by_request.pop(primary_id, None)
    if key is None:
        return
    pending_resumes.pop(key, None)
    ids = [primary_id, *resume_waiters.pop(primary_id, [])]
    for request_id in ids:
        error = {
            "id": request_id,
            "error": {"code": -32001, "message": f"thread/resume timed out after {timeout:g}s"},
        }
        sys.stdout.write(json.dumps(error, ensure_ascii=False) + "\n")
    sys.stdout.flush()


async def send_stdin(websocket, state: dict) -> None:
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
                if request.get("id") is not None:
                    state["thread_lists"][request["id"]] = request.get("params") or {}
            if isinstance(request, dict) and request.get("method") == "thread/resume":
                request_id = request.get("id")
                params = request.get("params")
                thread_id = params.get("threadId") if isinstance(params, dict) else None
                if request_id is not None and thread_id:
                    if not params.get("path"):
                        path = rollout_path_for_thread(thread_id)
                        if path is not None:
                            params["path"] = path
                    params["excludeTurns"] = True
                    message = json.dumps(request, ensure_ascii=False)
                    key = canonical_resume_key(params)
                    primary_id = state["pending_resumes"].get(key)
                    if primary_id is not None:
                        state["resume_waiters"][primary_id].append(request_id)
                        continue
                    state["pending_resumes"][key] = request_id
                    state["resume_key_by_request"][request_id] = key
                    state["resume_waiters"][request_id] = []
                    state["resume_timeouts"][request_id] = asyncio.create_task(
                        expire_resume(
                            request_id,
                            state["resume_timeout"],
                            state["pending_resumes"],
                            state["resume_key_by_request"],
                            state["resume_waiters"],
                        )
                    )
            await websocket.send(message)
    finally:
        transport.close()


async def receive_stdout(websocket, state: dict) -> None:
    async for message in websocket:
        if isinstance(message, bytes):
            message = message.decode()
        try:
            response = json.loads(message)
            response_id = response.get("id")
        except (AttributeError, json.JSONDecodeError):
            response = None
            response_id = None
        list_params = state["thread_lists"].pop(response_id, None)
        if list_params is not None:
            message = augment_thread_list_response(message, list_params, state["injected_ids"])
        sys.stdout.write(message + "\n")
        if response_id in state["resume_waiters"]:
            timeout_task = state["resume_timeouts"].pop(response_id, None)
            if timeout_task is not None:
                timeout_task.cancel()
            key = state["resume_key_by_request"].pop(response_id, None)
            if key is not None:
                state["pending_resumes"].pop(key, None)
            for waiter_id in state["resume_waiters"].pop(response_id):
                replay = dict(response)
                replay["id"] = waiter_id
                sys.stdout.write(json.dumps(replay, ensure_ascii=False) + "\n")
        sys.stdout.flush()


async def connect_websocket(socket_path: str):
    wait_for_socket = os.environ.get("CODEX_BRIDGE_WAIT_FOR_SOCKET") == "1"
    deadline = time.monotonic() + 360
    backoff = 0.02
    while True:
        try:
            return await websockets.unix_connect(
                socket_path, uri="ws://localhost", compression=None, max_size=None
            )
        except OSError:
            if not wait_for_socket or time.monotonic() >= deadline:
                raise
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 0.5)


async def run_bridge() -> None:
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
        temporary = marker_path.with_name(f".{marker_path.name}.{os.getpid()}")
        temporary.write_text(f"{os.stat(socket_path).st_ino}\n")
        os.replace(temporary, marker_path)
        state = {
            "thread_lists": {},
            "injected_ids": set(),
            "pending_resumes": {},
            "resume_key_by_request": {},
            "resume_waiters": {},
            "resume_timeouts": {},
            "resume_timeout": float(os.environ.get("CODEX_BRIDGE_RESUME_TIMEOUT", "900")),
        }
        sender = asyncio.create_task(send_stdin(websocket, state))
        receiver = asyncio.create_task(receive_stdout(websocket, state))
        done, pending = await asyncio.wait((sender, receiver), return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        for task in state["resume_timeouts"].values():
            task.cancel()
        for task in done:
            task.result()
    finally:
        await websocket.close()


def main() -> None:
    with frontend_client():
        asyncio.run(run_bridge())


if __name__ == "__main__":
    main()
