#!/usr/bin/env python3
"""Capture the newest user-authored event without scanning a whole rollout."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import sqlite3
from collections.abc import Iterator


def rollout_from_catalog(codex_home: pathlib.Path, session_id: str) -> pathlib.Path | None:
    database = codex_home / "state_5.sqlite"
    if not database.is_file():
        return None
    try:
        connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
        try:
            columns = {row[1] for row in connection.execute("PRAGMA table_info(threads)")}
            if not {"id", "rollout_path"}.issubset(columns):
                return None
            row = connection.execute(
                "SELECT rollout_path FROM threads WHERE id = ?", (session_id,)
            ).fetchone()
        finally:
            connection.close()
    except sqlite3.Error:
        return None
    if not row or not row[0]:
        return None
    path = pathlib.Path(row[0])
    return path if path.is_absolute() and path.is_file() else None


def rollout_path(codex_home: pathlib.Path, session_id: str) -> pathlib.Path:
    catalog_path = rollout_from_catalog(codex_home, session_id)
    if catalog_path is not None:
        return catalog_path
    matches = list((codex_home / "sessions").glob(f"**/*-{session_id}.jsonl"))
    if len(matches) != 1:
        raise SystemExit(f"expected one rollout for {session_id}, found {len(matches)}")
    return matches[0]


def reverse_lines(path: pathlib.Path, block_size: int = 64 * 1024) -> Iterator[bytes]:
    """Yield non-empty lines newest-first with bounded reads."""
    with path.open("rb") as stream:
        stream.seek(0, os.SEEK_END)
        position = stream.tell()
        remainder = b""
        while position:
            size = min(block_size, position)
            position -= size
            stream.seek(position)
            chunk = stream.read(size) + remainder
            lines = chunk.split(b"\n")
            remainder = lines.pop(0)
            for line in reversed(lines):
                if line:
                    yield line
        if remainder:
            yield remainder


def newest_user_message(path: pathlib.Path) -> dict | None:
    for raw_line in reverse_lines(path):
        try:
            record = json.loads(raw_line)
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        payload = record.get("payload", {})
        if record.get("type") == "event_msg" and payload.get("type") == "user_message":
            return payload
    return None


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--session-id", required=True)
    parser.add_argument(
        "--codex-home",
        default=os.environ.get("CODEX_HOME", str(pathlib.Path.home() / ".codex")),
    )
    args = parser.parse_args()

    path = rollout_path(pathlib.Path(args.codex_home), args.session_id)
    last_user = newest_user_message(path)
    serialized = json.dumps(last_user, sort_keys=True, separators=(",", ":"))
    print(
        json.dumps(
            {
                "rollout": str(path),
                "last_user_sha256": hashlib.sha256(serialized.encode()).hexdigest(),
                "client_id": (last_user or {}).get("client_id"),
                "last_user_message": (last_user or {}).get("message", "")[-2000:],
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
