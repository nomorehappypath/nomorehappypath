# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""The harness runtime directory for one session (plumbing spec §4.7, Q-5b).

It holds only the sockets Stage 3 adds - the Codex TUI multiplexer and the
Claude inbox relay's handover - and nothing an agent may write. It lives under
the manager home, never in temp space (every agent may write `/tmp`), is
created 0700 by the supervisor and removed when the session ends.

The path is short on purpose: a Unix socket path is limited to about 104
bytes on macOS, so the directory name is a digest, not the session id.
"""
from __future__ import annotations

import hashlib
import os
import shutil
from pathlib import Path

RUNTIME_DIRECTORY = "rt"
MAX_SOCKET_PATH_BYTES = 100


class RuntimeDirectoryRefused(ValueError):
    pass


def session_directory(manager_home: str | os.PathLike, project_key: str, session_id: str) -> Path:
    digest = hashlib.sha256(f"{project_key}\0{session_id}".encode("utf-8")).hexdigest()[:12]
    return Path(manager_home) / RUNTIME_DIRECTORY / digest


def _inside(path: str, root: str) -> bool:
    path, root = os.path.realpath(path), os.path.realpath(root)
    return path == root or path.startswith(root.rstrip("/") + "/")


def validate(directory: Path, agent_write_paths: list[str], socket_names: tuple[str, ...] = ("t.sock", "h.sock")) -> None:
    """Refuse when an agent could write the directory, or a socket path would be too long."""
    real = os.path.realpath(directory)
    for root in agent_write_paths:
        if _inside(real, root) or _inside(root, real):
            raise RuntimeDirectoryRefused(f"the harness runtime directory {real} is inside an agent write grant ({root})")
    for name in socket_names:
        if len(os.path.join(real, name).encode("utf-8")) > MAX_SOCKET_PATH_BYTES:
            raise RuntimeDirectoryRefused(f"the harness runtime directory path is too long for a socket: {real}")


def create(directory: Path) -> Path:
    previous = os.umask(0o077)
    try:
        directory.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(directory.parent, 0o700)
        directory.mkdir(exist_ok=True)
        os.chmod(directory, 0o700)
    finally:
        os.umask(previous)
    return directory


def remove(directory: Path) -> None:
    shutil.rmtree(directory, ignore_errors=True)
