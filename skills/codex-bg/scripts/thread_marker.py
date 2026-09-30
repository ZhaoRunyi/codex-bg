#!/usr/bin/env python3
"""Capture the last user-authored event in a persisted Codex thread."""

import argparse
import hashlib
import json
import os
import pathlib


parser = argparse.ArgumentParser()
parser.add_argument("--session-id", required=True)
parser.add_argument(
    "--codex-home",
    default=os.environ.get("CODEX_HOME", str(pathlib.Path.home() / ".codex")),
)
args = parser.parse_args()

session_root = pathlib.Path(args.codex_home) / "sessions"
matches = list(session_root.glob(f"**/*-{args.session_id}.jsonl"))
if len(matches) != 1:
    raise SystemExit(f"expected one rollout for {args.session_id}, found {len(matches)}")

last_user = None
with matches[0].open("rb") as stream:
    for line in stream:
        if b'"type":"user_message"' not in line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        payload = record.get("payload", {})
        if record.get("type") == "event_msg" and payload.get("type") == "user_message":
            last_user = payload

serialized = json.dumps(last_user, sort_keys=True, separators=(",", ":"))
print(
    json.dumps(
        {
            "rollout": str(matches[0]),
            "last_user_sha256": hashlib.sha256(serialized.encode()).hexdigest(),
            "client_id": (last_user or {}).get("client_id"),
            "last_user_message": (last_user or {}).get("message", "")[-2000:],
        },
        sort_keys=True,
    )
)
