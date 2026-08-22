# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
"""Thin authenticated client used by board.py in a managed Projects session."""
from __future__ import annotations

import base64
import fcntl
import hashlib
import json
import os
import stat
import sys
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from harness.board_surface import (
    ENDPOINT_ENV, MAX_ARTIFACT_BYTES, PROTOCOL_ENV, PROTOCOL_VERSION, TOKEN_ENV,
)


ENVIRONMENT_NAMES = (TOKEN_ENV, ENDPOINT_ENV, PROTOCOL_ENV)
POLL_TRANSPORT_ATTEMPTS = 3
POLL_RETRY_DELAYS = (0.2, 0.5)
UPLOAD_ARGUMENTS = {
    "request-qa": ("--ledger", "ledger", True),
    "request-review": ("--ledger", "ledger", True),
    "attach-challenge-ledger": ("--challenge-ledger", "challenge_ledger", True),
    "claim-qa": ("--challenge-ledger", "challenge_ledger", False),
    "qa-result": ("--evidence", "evidence", True),
}


class WorkerTransportError(OSError):
    """The authenticated worker could not be reached before the deadline."""


def environment_state(environment: dict[str, str] | None = None) -> str:
    source = os.environ if environment is None else environment
    present = [bool(source.get(name)) for name in ENVIRONMENT_NAMES]
    if all(present):
        return "active"
    if any(present):
        return "invalid"
    return "legacy"


def _strip_context_arguments(arguments: list[str]) -> list[str]:
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        matched = next((name for name in ("--root", "--data-root", "--workspace-root") if argument == name), None)
        if matched:
            if index + 1 >= len(arguments):
                raise ValueError(f"{matched} requires a value")
            index += 2
            continue
        if any(argument.startswith(name + "=") for name in ("--root", "--data-root", "--workspace-root")):
            index += 1
            continue
        break
    return arguments[index:]


def _nonce(token: str) -> int:
    label = hashlib.sha256(token.encode("utf-8")).hexdigest()
    directory = Path(tempfile.gettempdir()) / "harness-board-nonces"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = directory / label
    with path.open("a+", encoding="utf-8") as handle:
        os.chmod(path, 0o600)
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        handle.seek(0)
        try:
            previous = int(handle.read().strip() or 0)
        except ValueError:
            previous = 0
        value = max(previous + 1, time.time_ns())
        handle.seek(0)
        handle.truncate()
        handle.write(str(value))
        handle.flush()
        os.fsync(handle.fileno())
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    return value


def _option_value(arguments: list[str], name: str) -> str | None:
    found: list[str] = []
    for index, argument in enumerate(arguments):
        if argument == name:
            if index + 1 >= len(arguments):
                raise ValueError(f"{name} requires a value")
            found.append(arguments[index + 1])
        elif argument.startswith(name + "="):
            found.append(argument[len(name) + 1:])
    if len(found) > 1:
        raise ValueError(f"{name} may be supplied only once")
    return found[0] if found else None


def _replace_option(arguments: list[str], name: str, value: str) -> list[str]:
    result: list[str] = []
    index = 0
    replaced = False
    while index < len(arguments):
        argument = arguments[index]
        if argument == name:
            if index + 1 >= len(arguments):
                raise ValueError(f"{name} requires a value")
            if not replaced:
                result.extend((name, value))
                replaced = True
            index += 2
            continue
        if argument.startswith(name + "="):
            if not replaced:
                result.extend((name, value))
                replaced = True
            index += 1
            continue
        result.append(argument)
        index += 1
    if not replaced:
        result.extend((name, value))
    return result


def _read_artifact(path_value: str, field: str) -> dict[str, Any]:
    """Read one regular single-link file entirely inside the agent process."""
    path = Path(path_value)
    try:
        before = os.lstat(path)
    except OSError as error:
        raise ValueError("artifact file is unavailable") from error
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
        raise ValueError("artifact must be a regular single-link file")
    if before.st_size < 0 or before.st_size > MAX_ARTIFACT_BYTES:
        raise ValueError(f"artifact exceeds the {MAX_ARTIFACT_BYTES}-byte limit")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise ValueError("artifact file could not be opened safely") from error
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1
            or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
            or opened.st_size != before.st_size
            or opened.st_mtime_ns != before.st_mtime_ns
            or opened.st_ctime_ns != before.st_ctime_ns
        ):
            raise ValueError("artifact changed while it was being opened")
        chunks: list[bytes] = []
        remaining = MAX_ARTIFACT_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    if len(payload) != before.st_size or len(payload) > MAX_ARTIFACT_BYTES:
        raise ValueError("artifact changed while it was being read")
    if (
        not stat.S_ISREG(after.st_mode) or after.st_nlink != 1
        or (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
        != (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns)
    ):
        raise ValueError("artifact changed while it was being read")
    try:
        payload.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError("artifact must be valid UTF-8") from error
    digest = hashlib.sha256(payload).hexdigest()
    return {
        "content": base64.b64encode(payload).decode("ascii"),
        "length": len(payload),
        "sha256": digest,
        "media_type": "text/markdown" if "ledger" in field else "text/plain",
    }


def _prepare_artifacts(operation: str, arguments: list[str]) -> tuple[list[str], dict[str, Any]]:
    specification = UPLOAD_ARGUMENTS.get(operation)
    if not specification:
        return arguments, {}
    option, field, required = specification
    path = _option_value(arguments, option)
    if path is None:
        if required:
            raise ValueError(f"{option} is required")
        return arguments, {}
    artifact = _read_artifact(path, field)
    return _replace_option(arguments, option, f"upload:{field}"), {field: artifact}


def _error_message(error: HTTPError) -> str:
    try:
        value: Any = json.loads(error.read())
        if isinstance(value, dict) and isinstance(value.get("error"), str):
            return value["error"]
    except (OSError, json.JSONDecodeError):
        pass
    return "authenticated board command failed"


def _loopback_endpoint(value: str) -> str:
    try:
        parsed = urlparse(value.rstrip("/"))
        port = parsed.port
    except ValueError as error:
        raise ValueError("board endpoint is invalid") from error
    if (
        parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or not port
        or parsed.username is not None or parsed.password is not None
        or parsed.path not in {"", "/"} or parsed.query or parsed.fragment
    ):
        raise ValueError("board endpoint is invalid")
    return value.rstrip("/")


def invoke(argv: list[str]) -> int:
    state = environment_state()
    if state != "active":
        print("error: authenticated board environment is incomplete", file=sys.stderr)
        return 2
    try:
        arguments = _strip_context_arguments(list(argv))
        if not arguments or arguments[0].startswith("-"):
            raise ValueError("board operation is required")
        token = os.environ[TOKEN_ENV]
        endpoint = _loopback_endpoint(os.environ[ENDPOINT_ENV])
        protocol = os.environ[PROTOCOL_ENV]
        if protocol != PROTOCOL_VERSION:
            raise ValueError("board protocol version is incompatible")
        arguments, artifacts = _prepare_artifacts(arguments[0], arguments)
        timeout = 360 if arguments[0] in {"request-review", "execute-challenge"} else 30
        attempts = POLL_TRANSPORT_ATTEMPTS if arguments[0] == "poll" else 1
        value = None
        for attempt in range(attempts):
            payload = json.dumps({
                "protocol": protocol,
                "nonce": _nonce(token),
                "operation": arguments[0],
                "arguments": arguments,
                "artifacts": artifacts,
            }, separators=(",", ":")).encode("utf-8")
            request = Request(
                endpoint + "/api/board/command", data=payload, method="POST",
                headers={"Content-Type": "application/json", "Authorization": "Bearer " + token},
            )
            try:
                with urlopen(request, timeout=timeout) as response:
                    value = json.loads(response.read())
                break
            except HTTPError:
                raise
            except (OSError, URLError) as error:
                if attempt + 1 < attempts:
                    time.sleep(POLL_RETRY_DELAYS[attempt])
                    continue
                raise WorkerTransportError from error
        if not isinstance(value, dict) or "result" not in value:
            raise ValueError("worker returned an invalid board response")
        print(json.dumps(value["result"], indent=2, sort_keys=True))
        return 0
    except HTTPError as error:
        print(f"error: {_error_message(error)}", file=sys.stderr)
    except WorkerTransportError:
        print("error: authenticated board worker is unavailable or temporarily busy", file=sys.stderr)
    except (ValueError, json.JSONDecodeError):
        print("error: authenticated board worker response is invalid or incompatible", file=sys.stderr)
    return 2
