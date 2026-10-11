#!/usr/bin/env python3
"""Keep the persistent shared Codex daemon aligned with the VS Code extension.

Persistent files live below CODEX_HOME. Unix sockets and PID hints live in the
host-local runtime directory and are recreated after every restart. The updater
never interrupts an active turn and never sends signals to an unverified process.
"""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import json
import os
import pathlib
import platform
import re
import shutil
import signal
import stat
import subprocess
import sys
import time
from typing import Any

import websockets

from runtime_coordination import acquire_lock, release_lock, runtime_directory


CODEX_HOME = pathlib.Path(
    os.environ.get("CODEX_HOME", pathlib.Path.home() / ".codex")
)
EXTENSION_ROOT = pathlib.Path(
    os.environ.get(
        "CODEX_VSCODE_EXTENSION_ROOT",
        CODEX_HOME / "vscode-server/.vscode-server/extensions"
        if (CODEX_HOME / "vscode-server/.vscode-server/extensions").is_dir()
        else pathlib.Path.home() / ".vscode-server/extensions",
    )
)
SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
CONTROL_DIR = CODEX_HOME / "app-server-control"
COMPAT_SOCKET = CONTROL_DIR / "app-server-control.sock"
WATCH_LOG = CONTROL_DIR / "extension-sync.log"
GUARD_SCRIPT = SCRIPT_DIR / "daemon_config_guard.py"
PYTHON = pathlib.Path(
    os.environ.get(
        "CODEX_BG_PYTHON",
        CODEX_HOME / "skill-runtimes/codex-bg/bin/python",
    )
)
WRAPPER = SCRIPT_DIR / "shared_codex.sh"
MANAGED_CLI = CODEX_HOME / "packages/app-server-daemon/current/bin/codex"
MANAGED_PID = CODEX_HOME / "app-server-daemon/app-server.pid"
STAGING_ROOT = CODEX_HOME / "packages/app-server-daemon-staging"
EXTENSION_BACKUPS = CODEX_HOME / "extension-backups"
RUNTIME_DIR = runtime_directory()
LIFECYCLE_LOCK = RUNTIME_DIR / "shared-codex-lifecycle.lock"
WATCH_LOCK = RUNTIME_DIR / "extension-sync.lock"


def emit(event: str, **fields: Any) -> None:
    print(json.dumps({"event": event, **fields}, sort_keys=True), flush=True)


def atomic_write(path: pathlib.Path, data: str, mode: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("w") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    if mode is not None:
        os.chmod(temporary, stat.S_IMODE(mode))
    os.replace(temporary, path)


def version_key(name: str) -> tuple[int, ...]:
    match = re.search(r"openai\.chatgpt-([0-9.]+)-linux-(?:x64|arm64)$", name)
    return tuple(int(part) for part in match.group(1).split(".")) if match else ()


def extension_binary_dir(extension: pathlib.Path) -> pathlib.Path | None:
    machine = platform.machine().lower()
    preferred = {
        "x86_64": "linux-x86_64",
        "amd64": "linux-x86_64",
        "aarch64": "linux-aarch64",
        "arm64": "linux-aarch64",
    }.get(machine)
    candidates = []
    if preferred:
        candidates.append(extension / "bin" / preferred)
    candidates.extend(sorted((extension / "bin").glob("linux-*")))
    seen = set()
    for path in candidates:
        if path in seen:
            continue
        seen.add(path)
        required = [path / "codex", path / "codex-package.json"]
        if all(item.is_file() for item in required):
            return path
    return None


def installed_extensions() -> list[pathlib.Path]:
    candidates = []
    for path in EXTENSION_ROOT.glob("openai.chatgpt-*-linux-*"):
        if (
            (path / "package.json").is_file()
            and (path / "out/extension.js").is_file()
            and extension_binary_dir(path) is not None
            and version_key(path.name)
        ):
            candidates.append(path)
    return sorted(candidates, key=lambda path: version_key(path.name), reverse=True)


def cli_version(cli: pathlib.Path) -> str | None:
    if not cli.is_file():
        return None
    result = subprocess.run(
        [str(cli), "--version"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=20,
        check=False,
    )
    match = re.search(r"codex-cli\s+(\S+)", result.stdout)
    return match.group(1) if result.returncode == 0 and match else None


def patch_extension(extension: pathlib.Path) -> bool:
    changed = False
    version = extension.name.removeprefix("openai.chatgpt-")
    backup_dir = EXTENSION_BACKUPS / version
    package_path = extension / "package.json"
    package = json.loads(package_path.read_text())
    setting = package["contributes"]["configuration"]["properties"][
        "chatgpt.cliExecutable"
    ]
    if setting.get("scope") != "machine":
        backup_dir.mkdir(parents=True, exist_ok=True)
        package_backup = backup_dir / "package.json"
        if not package_backup.exists():
            shutil.copy2(package_path, package_backup)
        setting["scope"] = "machine"
        atomic_write(
            package_path,
            json.dumps(package, indent="\t", ensure_ascii=False) + "\n",
            package_path.stat().st_mode,
        )
        changed = True

    javascript_path = extension / "out/extension.js"
    javascript = javascript_path.read_text()
    original = javascript
    if "CODEX_VSCODE_IPC_DIR" not in javascript:
        ipc_pattern = re.compile(
            r"let (?P<var>[A-Za-z_$][\w$]*)=(?P<join>[A-Za-z_$][\w$]*)\.join\("
            r"(?P<base>[A-Za-z_$][\w$]*)\(\),[\"']ipc[\"']\);"
            r"(?P<fs>[A-Za-z_$][\w$]*)\.mkdirSync\((?P=var),"
            r"\{mode:448,recursive:!0\}\)"
        )
        matches = list(ipc_pattern.finditer(javascript))
        if len(matches) != 1:
            raise RuntimeError(
                f"expected one semantic IPC patch target in {javascript_path}, found {len(matches)}"
            )
        match = matches[0]
        ipc_patched = (
            f"let {match['var']}=process.env.CODEX_VSCODE_IPC_DIR??{match['join']}.join("
            f'process.env.XDG_RUNTIME_DIR??"/tmp","codex-vscode-ipc",'
            f'`uid-${{process.getuid?.()??"unknown"}}`);'
            f"{match['fs']}.mkdirSync({match['var']},{{mode:448,recursive:!0}})"
        )
        javascript = javascript[: match.start()] + ipc_patched + javascript[match.end() :]

    wrapper_literal = json.dumps(str(WRAPPER))
    if str(WRAPPER) not in javascript:
        prior_wrapper_pattern = re.compile(
            r"return(?P<path>[\"'][^\"']*/shared_codex\.sh[\"']);let "
        )
        prior_matches = list(prior_wrapper_pattern.finditer(javascript))
        if len(prior_matches) == 1:
            match = prior_matches[0]
            replacement = f"return{wrapper_literal};let "
            javascript = javascript[: match.start()] + replacement + javascript[match.end() :]
        else:
            cli_pattern = re.compile(
                r"let (?P<value>[A-Za-z_$][\w$]*)=(?P<setting>[A-Za-z_$][\w$]*)"
                r"\([\"']cliExecutable[\"']\);if\((?P=value)&&(?P=value)\.trim\(\)"
                r"\.length>0\)return (?P=value);let (?P<fallback>[A-Za-z_$][\w$]*)="
            )
            matches = list(cli_pattern.finditer(javascript))
            if len(matches) != 1:
                raise RuntimeError(
                    f"expected one semantic CLI patch target in {javascript_path}, found {len(matches)}"
                )
            match = matches[0]
            replacement = match.group(0).replace(
                f";let {match['fallback']}=",
                f";return{wrapper_literal};let {match['fallback']}=",
            )
            javascript = javascript[: match.start()] + replacement + javascript[match.end() :]

    if javascript != original:
        backup = backup_dir / "extension.js"
        if not backup.exists():
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(javascript_path, backup)
        atomic_write(javascript_path, javascript, javascript_path.stat().st_mode)
        changed = True

    if changed:
        emit("extension_patched", extension=str(extension))
    return changed


def runtime_paths() -> tuple[pathlib.Path, pathlib.Path, pathlib.Path]:
    return (
        RUNTIME_DIR,
        RUNTIME_DIR / "app-server-control.sock",
        RUNTIME_DIR / "app-server.pid",
    )


def ensure_runtime() -> tuple[pathlib.Path, pathlib.Path]:
    directory, socket_path, pid_path = runtime_paths()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(directory, 0o700)
    info = directory.stat()
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise RuntimeError(f"unsafe runtime directory: {directory}")
    CONTROL_DIR.mkdir(parents=True, exist_ok=True)
    expected = str(socket_path)
    if COMPAT_SOCKET.is_symlink():
        if os.readlink(COMPAT_SOCKET) != expected:
            COMPAT_SOCKET.unlink()
            COMPAT_SOCKET.symlink_to(socket_path)
    elif os.path.lexists(COMPAT_SOCKET):
        raise RuntimeError(f"refusing to replace non-symlink socket entry: {COMPAT_SOCKET}")
    else:
        COMPAT_SOCKET.symlink_to(socket_path)
    return socket_path, pid_path


def verified_pid(pid_path: pathlib.Path, socket_path: pathlib.Path) -> int | None:
    try:
        raw = pid_path.read_text().strip()
        data = json.loads(raw) if raw.startswith("{") else {"pid": int(raw)}
        pid = int(data["pid"])
        command = pathlib.Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ")
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        return None
    required = [b"codex", b"app-server", str(socket_path).encode()]
    return pid if all(fragment in command for fragment in required) else None


def write_pid(pid: int, runtime_pid: pathlib.Path) -> None:
    atomic_write(runtime_pid, f"{pid}\n", 0o600)
    atomic_write(MANAGED_PID, f"{pid}\n", 0o600)


async def rpc_request(websocket, request_id: int, method: str, params: dict) -> dict:
    await websocket.send(json.dumps({"id": request_id, "method": method, "params": params}))
    while True:
        message = json.loads(await websocket.recv())
        if message.get("id") == request_id:
            if "error" in message:
                raise RuntimeError(str(message["error"]))
            return message["result"]


async def daemon_threads(socket_path: pathlib.Path) -> list[dict]:
    async with websockets.unix_connect(
        str(socket_path), uri="ws://localhost", compression=None, max_size=None
    ) as websocket:
        await rpc_request(
            websocket,
            1,
            "initialize",
            {
                "clientInfo": {
                    "name": "codex-extension-sync",
                    "title": "Codex extension sync",
                    "version": "1.0.0",
                },
                "capabilities": {"experimentalApi": True},
            },
        )
        await websocket.send(json.dumps({"method": "initialized", "params": {}}))
        loaded = (await rpc_request(websocket, 2, "thread/loaded/list", {})).get(
            "data", []
        )
        output = []
        request_id = 10
        for thread_id in loaded:
            result = await rpc_request(
                websocket,
                request_id,
                "thread/turns/list",
                {
                    "threadId": thread_id,
                    "limit": 1,
                    "sortDirection": "desc",
                    "itemsView": "notLoaded",
                },
            )
            request_id += 1
            turns = result.get("data", [])
            latest = turns[0] if turns else None
            turn_status = latest.get("status") if latest else "noTurns"
            if turn_status == "inProgress":
                status = "active"
            elif turn_status in {"completed", "interrupted", "failed", "noTurns"}:
                status = "idle"
            else:
                status = "unknown"
            output.append(
                {
                    "id": thread_id,
                    "turn_id": latest.get("id") if latest else None,
                    "turn_status": turn_status,
                    "status": status,
                }
            )
        return output

def live_threads(socket_path: pathlib.Path) -> list[dict]:
    return asyncio.run(asyncio.wait_for(daemon_threads(socket_path), timeout=30))


def daemon_is_idle(socket_path: pathlib.Path) -> tuple[bool, list[dict]]:
    if not socket_path.is_socket():
        return True, []
    threads = live_threads(socket_path)
    return all(thread["status"] == "idle" for thread in threads), threads


def daemon_environment() -> dict[str, str]:
    environment = os.environ.copy()
    environment["CODEX_HOME"] = str(CODEX_HOME)
    return environment


def stop_daemon(socket_path: pathlib.Path, runtime_pid: pathlib.Path) -> None:
    pid = verified_pid(runtime_pid, socket_path) or verified_pid(MANAGED_PID, socket_path)
    if not socket_path.is_socket() and pid is None:
        return
    if not MANAGED_CLI.is_file():
        raise RuntimeError("managed Codex CLI is unavailable for graceful stop")
    result = subprocess.run(
        [str(MANAGED_CLI), "app-server", "daemon", "stop"],
        env=daemon_environment(),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=40,
        check=False,
    )
    stop_output = result.stdout.strip()
    if result.returncode != 0:
        if pid is None:
            raise RuntimeError(f"graceful daemon stop failed: {stop_output}")
        # A directly launched `app-server --listen` is official but is not
        # registered with `app-server daemon`. The verified PID is exact, so
        # SIGTERM is the graceful fallback; SIGKILL is intentionally forbidden.
        os.kill(pid, signal.SIGTERM)
        stop_output = f"verified SIGTERM fallback: {stop_output}"
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        alive = pid is not None and pathlib.Path(f"/proc/{pid}").exists()
        if not alive and not socket_path.exists():
            emit("daemon_stopped", output=stop_output)
            return
        time.sleep(0.25)
    raise RuntimeError("daemon did not exit after graceful stop; refusing to send a signal")


def stage_extension_package(extension: pathlib.Path, version: str) -> pathlib.Path:
    source = extension_binary_dir(extension)
    if source is None:
        raise RuntimeError(f"no compatible bundled Codex package in {extension}")
    manifest = json.loads((source / "codex-package.json").read_text())
    target = manifest.get("target", "unknown-target")
    final = STAGING_ROOT / f"{version}-{target}"
    temporary = STAGING_ROOT / f".{version}-{target}.{os.getpid()}.tmp"
    if final.exists():
        return final
    temporary.mkdir(parents=True, exist_ok=False)
    (temporary / "bin").mkdir()
    shutil.copy2(source / "codex", temporary / "bin/codex")
    code_mode_host = source / "codex-code-mode-host"
    if code_mode_host.is_file():
        shutil.copy2(code_mode_host, temporary / "bin/codex-code-mode-host")
    shutil.copy2(source / "codex-package.json", temporary / "codex-package.json")
    for directory_name in ("codex-resources", "codex-path"):
        source_directory = source / directory_name
        if source_directory.is_dir():
            shutil.copytree(source_directory, temporary / directory_name)
    os.replace(temporary, final)
    return final


def install_extension_package(extension: pathlib.Path, version: str) -> None:
    stage = stage_extension_package(extension, version)
    cli = stage / "bin/codex"
    if not MANAGED_CLI.is_file():
        # `daemon update` needs a selected package. Seed it with the official
        # start command on a fresh install, then stop before binding our socket.
        if COMPAT_SOCKET.is_symlink() and not COMPAT_SOCKET.is_socket():
            COMPAT_SOCKET.unlink()
        initial = subprocess.run(
            [str(cli), "-c", "features.code_mode_host=true", "app-server", "daemon", "start"],
            env=daemon_environment(), text=True, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, timeout=90, check=False,
        )
        if cli_version(MANAGED_CLI) != version:
            raise RuntimeError(f"initial daemon package install failed: {initial.stdout.strip()}")
        stopped = subprocess.run(
            [str(cli), "app-server", "daemon", "stop"],
            env=daemon_environment(), text=True, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, timeout=40, check=False,
        )
        if stopped.returncode != 0:
            raise RuntimeError(f"initial daemon stop failed: {stopped.stdout.strip()}")
        emit("daemon_package_bootstrapped", version=version)
        shutil.rmtree(stage)
        return
    result = subprocess.run(
        [
            str(cli),
            "-c",
            "features.code_mode_host=true",
            "app-server",
            "daemon",
            "update",
            "--from-cli",
            "-y",
        ],
        env=daemon_environment(),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=300,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"daemon package update failed: {result.stdout.strip()}")
    emit("daemon_package_updated", version=version, output=result.stdout.strip())
    shutil.rmtree(stage)


def start_daemon(socket_path: pathlib.Path, runtime_pid: pathlib.Path) -> int:
    ensure_runtime()
    existing = verified_pid(runtime_pid, socket_path) or verified_pid(MANAGED_PID, socket_path)
    if existing is not None and socket_path.is_socket():
        return existing
    if socket_path.exists():
        if not socket_path.is_socket():
            raise RuntimeError(f"runtime socket path is not a socket: {socket_path}")
        socket_path.unlink()
    if not MANAGED_CLI.is_file():
        raise RuntimeError(f"managed Codex CLI is unavailable: {MANAGED_CLI}")
    stdout_path = CODEX_HOME / "app-server-daemon/daemon.stdout.log"
    stderr_path = CODEX_HOME / "app-server-daemon/daemon.stderr.log"
    stdout_path.parent.mkdir(parents=True, exist_ok=True)
    stdout = stdout_path.open("ab", buffering=0)
    stderr = stderr_path.open("ab", buffering=0)
    process = subprocess.Popen(
        [
            str(MANAGED_CLI),
            "-c",
            "features.code_mode_host=true",
            "app-server",
            "--listen",
            f"unix://{socket_path}",
        ],
        env=daemon_environment(),
        stdin=subprocess.DEVNULL,
        stdout=stdout,
        stderr=stderr,
        start_new_session=True,
        close_fds=True,
    )
    stdout.close()
    stderr.close()
    write_pid(process.pid, runtime_pid)
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"daemon exited during startup with code {process.returncode}")
        if socket_path.is_socket():
            live_threads(socket_path)
            emit("daemon_started", pid=process.pid, socket=str(socket_path))
            return process.pid
        time.sleep(0.5)
    raise RuntimeError("daemon did not become ready within 60 seconds")


def record_guard() -> None:
    result = subprocess.run(
        [
            str(PYTHON),
            str(GUARD_SCRIPT),
            "record",
            "--codex-home",
            str(CODEX_HOME),
            "--socket",
            str(COMPAT_SOCKET),
            "--state",
            str(CONTROL_DIR / "daemon-config.json"),
        ],
        env=daemon_environment(),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=30,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"failed to record daemon guard: {result.stdout.strip()}")


def daemon_config_current() -> bool:
    result = subprocess.run(
        [str(PYTHON), str(GUARD_SCRIPT), "check", "--codex-home", str(CODEX_HOME),
         "--socket", str(COMPAT_SOCKET), "--state", str(CONTROL_DIR / "daemon-config.json")],
        env=daemon_environment(), stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, timeout=30, check=False,
    )
    return result.returncode == 0


def sync_once(allow_update: bool) -> dict[str, Any]:
    extensions = installed_extensions()
    if not extensions:
        raise RuntimeError(f"no supported Codex extension found below {EXTENSION_ROOT}")
    extension = extensions[0]
    patch_extension(extension)
    binary_dir = extension_binary_dir(extension)
    if binary_dir is None:
        raise RuntimeError(f"no compatible bundled Codex package in {extension}")
    bundled_cli = binary_dir / "codex"
    bundled_version = cli_version(bundled_cli)
    if bundled_version is None:
        raise RuntimeError(f"cannot read bundled CLI version: {bundled_cli}")

    socket_path, runtime_pid = ensure_runtime()
    managed_version = cli_version(MANAGED_CLI)
    updated = False
    restarted = False
    deferred = False
    version_changed = managed_version != bundled_version
    config_changed = socket_path.is_socket() and not daemon_config_current()
    if version_changed or config_changed:
        client_lock = None
        operation_lock = None
        try:
            client_lock = acquire_lock("frontend-clients.lock", exclusive=True, blocking=False)
            operation_lock = acquire_lock("daemon-operations.lock", exclusive=True, blocking=False)
            idle, threads = daemon_is_idle(socket_path)
            if not allow_update or client_lock is None or operation_lock is None or not idle:
                deferred = True
                if not allow_update:
                    reason = "patch-only-mode"
                elif client_lock is None:
                    reason = "frontend-connected"
                elif operation_lock is None:
                    reason = "daemon-rpc-active"
                else:
                    reason = "active-turn"
                emit(
                    "daemon_update_deferred",
                    config_changed=config_changed,
                    bundled_version=bundled_version,
                    managed_version=managed_version,
                    reason=reason,
                    threads=threads,
                )
            else:
                stop_daemon(socket_path, runtime_pid)
                if version_changed:
                    install_extension_package(extension, bundled_version)
                socket_path, runtime_pid = ensure_runtime()
                start_daemon(socket_path, runtime_pid)
                managed_version = cli_version(MANAGED_CLI)
                updated = version_changed
                restarted = True
        finally:
            release_lock(operation_lock)
            release_lock(client_lock)

    if not socket_path.is_socket():
        start_daemon(socket_path, runtime_pid)
    else:
        pid = verified_pid(runtime_pid, socket_path) or verified_pid(MANAGED_PID, socket_path)
        if pid is None:
            raise RuntimeError("daemon socket exists but its process identity cannot be verified")
        write_pid(pid, runtime_pid)
        live_threads(socket_path)
    if not deferred:
        record_guard()
    return {
        "extension": str(extension),
        "bundled_version": bundled_version,
        "managed_version": cli_version(MANAGED_CLI),
        "updated": updated,
        "restarted": restarted,
        "deferred": deferred,
        "socket": str(socket_path),
    }


def locked_sync(allow_update: bool, blocking: bool = True) -> dict[str, Any] | None:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(RUNTIME_DIR, 0o700)
    with LIFECYCLE_LOCK.open("a+") as lock:
        operation = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
        try:
            fcntl.flock(lock.fileno(), operation)
        except BlockingIOError:
            return None
        return sync_once(allow_update=allow_update)


def rotate_watch_log() -> None:
    if WATCH_LOG.exists() and WATCH_LOG.stat().st_size > 2 * 1024 * 1024:
        previous = WATCH_LOG.with_suffix(".log.previous")
        if previous.exists():
            previous.unlink()
        os.replace(WATCH_LOG, previous)


def extension_fingerprint() -> tuple[Any, ...]:
    extensions = installed_extensions()
    if not extensions:
        return ()
    extension = extensions[0]
    binary_dir = extension_binary_dir(extension)
    if binary_dir is None:
        return (str(extension), "unsupported-binary-layout")
    files = [
        extension / "package.json",
        extension / "out/extension.js",
        binary_dir / "codex-package.json",
        binary_dir / "codex",
    ]
    fingerprint: list[Any] = [str(extension)]
    for path in files:
        info = path.stat()
        fingerprint.append((info.st_mtime_ns, info.st_size))
    return tuple(fingerprint)


def watch(interval: int) -> None:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(RUNTIME_DIR, 0o700)
    with WATCH_LOCK.open("a+") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        previous_fingerprint = None
        while True:
            try:
                current_fingerprint = extension_fingerprint()
                if current_fingerprint == previous_fingerprint:
                    time.sleep(interval)
                    continue
                rotate_watch_log()
                result = locked_sync(allow_update=True, blocking=False)
                if result is not None:
                    with WATCH_LOG.open("a") as log:
                        log.write(json.dumps({"time": time.time(), **result}, sort_keys=True) + "\n")
                    if not result.get("deferred"):
                        previous_fingerprint = extension_fingerprint()
            except Exception as exc:
                with WATCH_LOG.open("a") as log:
                    log.write(
                        json.dumps(
                            {"time": time.time(), "error": str(exc)}, sort_keys=True
                        )
                        + "\n"
                    )
            time.sleep(interval)


def main() -> None:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("ensure")
    subparsers.add_parser("patch")
    watch_parser = subparsers.add_parser("watch")
    watch_parser.add_argument("--interval", type=int, default=30)
    args = parser.parse_args()

    if args.command == "watch":
        watch(max(10, args.interval))
        return
    if args.command == "patch":
        extensions = installed_extensions()
        if not extensions:
            raise RuntimeError(f"no supported Codex extension found below {EXTENSION_ROOT}")
        changed = patch_extension(extensions[0])
        emit("patch_complete", extension=str(extensions[0]), changed=changed)
        return
    result = locked_sync(allow_update=args.command == "ensure")
    emit("sync_complete", **(result or {"skipped": "lifecycle-lock-busy"}))


if __name__ == "__main__":
    main()
