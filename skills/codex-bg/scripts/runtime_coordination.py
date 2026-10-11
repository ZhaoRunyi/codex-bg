#!/usr/bin/env python3
"""Host-local locks that coordinate Codex clients with daemon replacement."""

from __future__ import annotations

import fcntl
import os
import pathlib
import stat
from contextlib import contextmanager
from typing import Iterator, TextIO


def runtime_directory() -> pathlib.Path:
    uid = os.getuid()
    configured = os.environ.get("XDG_RUNTIME_DIR")
    candidate = pathlib.Path(configured) if configured else pathlib.Path(f"/run/user/{uid}")
    try:
        info = candidate.stat()
        if candidate.is_dir() and info.st_uid == uid:
            root = candidate
        else:
            root = None
    except OSError:
        root = None
    if root is None:
        root = pathlib.Path("/tmp") / f"codex-runtime-{uid}"
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(root, 0o700)
    directory = root / "codex-shared"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(directory, 0o700)
    info = directory.stat()
    if info.st_uid != uid or stat.S_IMODE(info.st_mode) != 0o700:
        raise RuntimeError(f"unsafe Codex runtime directory: {directory}")
    return directory


def acquire_lock(name: str, *, exclusive: bool, blocking: bool = True) -> TextIO | None:
    handle = (runtime_directory() / name).open("a+")
    operation = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
    if not blocking:
        operation |= fcntl.LOCK_NB
    try:
        fcntl.flock(handle.fileno(), operation)
    except BlockingIOError:
        handle.close()
        return None
    return handle


def release_lock(handle: TextIO | None) -> None:
    if handle is None:
        return
    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    handle.close()


@contextmanager
def shared_daemon_operation() -> Iterator[None]:
    """Prevent daemon replacement while a non-sidebar client uses RPC."""
    handle = acquire_lock("daemon-operations.lock", exclusive=False)
    try:
        yield
    finally:
        release_lock(handle)


@contextmanager
def frontend_client() -> Iterator[None]:
    """Keep the daemon version stable for the lifetime of a sidebar bridge."""
    handle = acquire_lock("frontend-clients.lock", exclusive=False)
    try:
        yield
    finally:
        release_lock(handle)
