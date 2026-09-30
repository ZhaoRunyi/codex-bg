#!/usr/bin/env python3
"""Record and verify the config/auth snapshot owned by the shared daemon."""

import argparse
import hashlib
import json
import os
import pathlib
import tempfile
import time
import urllib.parse

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib


PROXY_ENV_NAMES = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "no_proxy",
)


def file_fingerprint(path: pathlib.Path, *, hash_contents: bool = True) -> dict:
    if not path.exists():
        return {"path": str(path), "exists": False}
    stat = path.stat()
    result = {
        "path": str(path),
        "exists": True,
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
    }
    try:
        result["realpath"] = str(path.resolve())
    except OSError:
        pass
    if hash_contents:
        result["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def redact_url(raw_url: str | None) -> str | None:
    if not raw_url:
        return raw_url
    parsed = urllib.parse.urlsplit(raw_url)
    if not parsed.username and not parsed.password:
        return raw_url
    host = parsed.hostname or ""
    if parsed.port:
        host = f"{host}:{parsed.port}"
    return urllib.parse.urlunsplit(
        (parsed.scheme, f"<redacted>@{host}", parsed.path, parsed.query, parsed.fragment)
    )


def config_summary(config_path: pathlib.Path) -> dict:
    try:
        config = tomllib.loads(config_path.read_text())
    except (OSError, tomllib.TOMLDecodeError) as exc:
        return {"error": str(exc)}
    provider_name = config.get("model_provider")
    provider = {}
    if provider_name:
        provider = config.get("model_providers", {}).get(provider_name, {})
    return {
        "model": config.get("model"),
        "model_provider": provider_name,
        "base_url": redact_url(provider.get("base_url")),
        "wire_api": provider.get("wire_api"),
        "requires_openai_auth": provider.get("requires_openai_auth"),
        "service_tier": config.get("service_tier"),
    }


def daemon_pid(codex_home: pathlib.Path) -> dict:
    pid_path = codex_home / "app-server-daemon" / "app-server.pid"
    try:
        raw = pid_path.read_text().strip()
    except OSError:
        return {"pid": None, "processStartTime": None}
    try:
        data = json.loads(raw)
        return {"pid": int(data["pid"]), "processStartTime": data.get("processStartTime")}
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        try:
            return {"pid": int(raw), "processStartTime": None}
        except ValueError:
            return {"pid": None, "processStartTime": None}


def required_daemon_environment() -> dict:
    environment = {}
    for name in PROXY_ENV_NAMES:
        value = os.environ.get(name)
        environment[name] = value if "NO_PROXY" in name.upper() else redact_url(value)
    return environment


def daemon_environment(codex_home: pathlib.Path) -> dict:
    pid = daemon_pid(codex_home).get("pid")
    if not pid:
        return {name: None for name in PROXY_ENV_NAMES}
    try:
        entries = pathlib.Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
    except OSError:
        return {name: None for name in PROXY_ENV_NAMES}
    environment = {}
    for entry in entries:
        if b"=" not in entry:
            continue
        raw_name, raw_value = entry.split(b"=", 1)
        name = raw_name.decode(errors="replace")
        if name in PROXY_ENV_NAMES:
            value = raw_value.decode(errors="replace")
            environment[name] = value if "NO_PROXY" in name.upper() else redact_url(value)
    return {name: environment.get(name) for name in PROXY_ENV_NAMES}


def socket_inode(socket_path: pathlib.Path) -> int | None:
    try:
        return socket_path.stat().st_ino
    except OSError:
        return None


def snapshot(args: argparse.Namespace) -> dict:
    codex_home = pathlib.Path(args.codex_home)
    config_path = codex_home / "config.toml"
    auth_path = codex_home / "auth.json"
    codex_binary = codex_home / "packages" / "standalone" / "current" / "codex"
    return {
        "recorded_at": time.time(),
        "codex_home": str(codex_home),
        "daemon": daemon_pid(codex_home),
        "daemon_environment": daemon_environment(codex_home),
        "required_daemon_environment": required_daemon_environment(),
        "socket": {
            "path": args.socket,
            "inode": socket_inode(pathlib.Path(args.socket)),
        },
        "config_summary": config_summary(config_path),
        "fingerprints": {
            "config": file_fingerprint(config_path),
            "auth": file_fingerprint(auth_path),
            "codex_binary": file_fingerprint(codex_binary, hash_contents=False),
        },
    }


def comparable(record: dict) -> dict:
    return {
        "codex_home": record.get("codex_home"),
        "daemon": record.get("daemon"),
        "required_daemon_environment": record.get("required_daemon_environment"),
        "socket": record.get("socket"),
        "fingerprints": record.get("fingerprints"),
    }


def write_json_atomic(path: pathlib.Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", dir=path.parent, prefix=f".{path.name}.", delete=False
    ) as temporary:
        json.dump(data, temporary, sort_keys=True)
        temporary.write("\n")
        temporary_path = pathlib.Path(temporary.name)
    os.replace(temporary_path, path)


def check(args: argparse.Namespace) -> tuple[bool, dict]:
    state_path = pathlib.Path(args.state)
    current = snapshot(args)
    try:
        recorded = json.loads(state_path.read_text())
    except FileNotFoundError:
        return False, {"reason": "daemon-config-record-missing", "current": current}
    except (OSError, json.JSONDecodeError) as exc:
        return False, {
            "reason": "daemon-config-record-unreadable",
            "error": str(exc),
            "current": current,
        }
    # The daemon may intentionally scrub proxy variables from its live process
    # environment after startup. The atomic startup record is the authoritative
    # proof that this daemon was launched with the durable proxy environment.
    if recorded.get("daemon_environment") != current["required_daemon_environment"]:
        return False, {
            "reason": "daemon-network-stale",
            "recorded": recorded,
            "current": current,
        }
    if comparable(recorded) != comparable(current):
        return False, {
            "reason": "daemon-config-stale",
            "recorded": recorded,
            "current": current,
        }
    return True, {"reason": "ok", "recorded": recorded, "current": current}


def main() -> None:
    codex_home = pathlib.Path(os.environ.get("CODEX_HOME", pathlib.Path.home() / ".codex"))
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["check", "record", "summary"])
    parser.add_argument("--codex-home", default=str(codex_home))
    parser.add_argument(
        "--socket",
        default=str(codex_home / "app-server-control/app-server-control.sock"),
    )
    parser.add_argument(
        "--state",
        default=str(codex_home / "app-server-control/daemon-config.json"),
    )
    args = parser.parse_args()

    if args.command == "record":
        data = snapshot(args)
        write_json_atomic(pathlib.Path(args.state), data)
        print(json.dumps({"status": "recorded", **data}, sort_keys=True))
        return
    if args.command == "summary":
        print(json.dumps(snapshot(args), sort_keys=True))
        return
    ok, result = check(args)
    print(json.dumps(result, sort_keys=True))
    raise SystemExit(0 if ok else 1)


if __name__ == "__main__":
    main()
