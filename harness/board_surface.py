# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
"""Authenticated loopback identity for the Projects board command surface.

The raw bearer credential exists only in worker memory, the bootstrap HTTP
response, and the managed session environment.  Durable state contains a
project-bound SHA-256 verifier, never the credential itself.  Session state
owns authority: a stopped/paused session or a changed authentication epoch
invalidates the credential even if a stale verifier remains on disk.
"""
from __future__ import annotations

import base64
import binascii
import fcntl
import hashlib
import hmac
import io
import json
import os
import re
import secrets
import subprocess
import threading
from contextlib import contextmanager
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urlparse

from harness import control
from harness.project_context import ProjectRoot, context_cli_arguments, project_context


PROTOCOL_VERSION = "1"
TOKEN_ENV = "HARNESS_BOARD_TOKEN"
ENDPOINT_ENV = "HARNESS_BOARD_ENDPOINT"
PROTOCOL_ENV = "HARNESS_BOARD_PROTOCOL"
TOKEN_BYTES = 48
TOKEN_RECORD_VERSION = 1
AUTHORIZED_SESSION_STATES = {"launching", "running", "stopping", "pausing"}

# Every command exposed by board.py has an explicit entry.  An empty role set
# means the operation belongs to an owner/worker surface and is deliberately
# unavailable to authenticated agent sessions.
ALL_BOARD_OPERATIONS = {
    "register", "poll", "recover", "status", "offline", "task-brief",
    "migrate-contract-scope", "expand-contract", "begin-task", "resume-task",
    "attach-workspace", "bind-repository", "reconcile-baseline",
    "owner-direction", "owner-message", "confirm-requirements", "propose-requirements", "record-finding",
    "finding-decision", "execute-challenge", "finding-triage", "finding-resolved",
    "findings", "request-qa", "request-independent-review", "define-plan",
    "declare-subtasks", "start-subtask", "declare-subtask-chunks", "git-commit",
    "declare-chunks", "request-review", "claim-qa", "reserve-qa",
    "review-brief", "review-intents", "attach-challenge-ledger", "qa-result",
    "resolve-repair-package", "split-repair-package", "complete", "claim-release-repair",
    "repin-final-review", "push-instruction", "push-confirm", "snapshot", "view",
    "cleanup", "migrate-review-ledgers", "migrate-integrity", "recover-git",
    "reopen-integrity", "watch", "reopen-candidate-scope",
}
COMMON_AGENT_OPERATIONS = {"register", "poll", "recover", "status", "offline"}
DELIVERY_OPERATIONS = COMMON_AGENT_OPERATIONS | {
    "task-brief", "migrate-contract-scope", "expand-contract", "begin-task",
    "resume-task", "reconcile-baseline", "confirm-requirements", "propose-requirements", "record-finding",
    "findings", "request-qa", "request-independent-review", "define-plan",
    "declare-subtasks", "start-subtask", "declare-subtask-chunks", "git-commit",
    "declare-chunks", "request-review", "resolve-repair-package", "complete", "claim-release-repair",
    "repin-final-review",
    # Recovery valve for the scaffold wedge: --repo is a PROTECTED argument the
    # surface refuses from clients, so an authenticated Delivery bind can only
    # ever target the server-derived project repository.
    "bind-repository",
}
REVIEWER_OPERATIONS = COMMON_AGENT_OPERATIONS | {
    "execute-challenge", "findings", "claim-qa", "reserve-qa",
    "review-brief", "review-intents", "attach-challenge-ledger", "qa-result", "split-repair-package",
}
CTO_OPERATIONS = COMMON_AGENT_OPERATIONS | {
    "record-finding", "finding-triage", "finding-resolved", "findings",
    "repin-final-review", "push-instruction", "push-confirm", "snapshot", "view",
    "cleanup", "migrate-review-ledgers", "migrate-integrity", "recover-git",
    "reopen-integrity", "reopen-candidate-scope",
}
AUTHORIZATION_MATRIX = {
    operation: frozenset(
        role for role, allowed in (
            ("engineering", DELIVERY_OPERATIONS),
            ("qa", REVIEWER_OPERATIONS),
            ("cto", CTO_OPERATIONS),
        ) if operation in allowed
    )
    for operation in ALL_BOARD_OPERATIONS
}

# These operations carry agent-authored file bytes through the authenticated
# request body.  The worker accepts only the upload marker, never a client path.
UPLOAD_ARGUMENTS = {
    "request-qa": ("--ledger", "ledger", True),
    "request-review": ("--ledger", "ledger", True),
    "attach-challenge-ledger": ("--challenge-ledger", "challenge_ledger", True),
    "claim-qa": ("--challenge-ledger", "challenge_ledger", False),
    "qa-result": ("--evidence", "evidence", True),
}
RAW_PATH_OPERATIONS = {"attach-workspace", "bind-repository"}
IDENTITY_AGENT_ARGUMENT = "--agent"
IDENTITY_SESSION_ARGUMENT = "--session-id"
CURRENT_TASK_OPERATIONS = {"record-finding", "claim-release-repair", "repin-final-review"}
AGENT_ARGUMENT_OPERATIONS = {
    "poll", "recover", "status", "offline", "task-brief",
    "migrate-contract-scope", "expand-contract", "begin-task", "resume-task",
    "attach-workspace", "bind-repository", "reconcile-baseline", "owner-message",
    "confirm-requirements", "propose-requirements", "execute-challenge", "request-qa",
    "request-independent-review", "define-plan", "declare-subtasks",
    "start-subtask", "declare-subtask-chunks", "git-commit", "declare-chunks",
    "request-review", "claim-qa", "reserve-qa", "review-brief", "review-intents", "attach-challenge-ledger",
    "qa-result", "resolve-repair-package", "split-repair-package", "complete", "claim-release-repair", "repin-final-review",
    "reopen-candidate-scope",
}
PROTECTED_ARGUMENTS = {
    "--agent", "--session-id", "--task", "--role", "--vendor", "--name",
    "--root", "--data-root", "--workspace-root", "--challenge-ledger", "--repo",
}
MAX_COMMAND_ARGUMENTS = 256
MAX_COMMAND_ARGUMENT_BYTES = 128 * 1024
MAX_ARTIFACT_BYTES = 1024 * 1024
MAX_ARTIFACT_WIRE_BYTES = (MAX_ARTIFACT_BYTES * 4 // 3) + 4096
NONCE_RECORD_VERSION = 1
_PROJECT_COMMAND_LOCKS: dict[str, threading.Lock] = {}
_PROJECT_NONCE_LOCKS: dict[str, threading.Lock] = {}
_PROJECT_COMMAND_LOCKS_GUARD = threading.Lock()
# These board operations are deliberately safe during a long certified command.
# Their board functions use the durable state lock, and staged review authoring
# depends on them remaining responsive while Delivery evidence executes.
CONCURRENT_BOARD_OPERATIONS = {
    "poll", "reserve-qa", "review-brief", "review-intents",
}
# Short heartbeat and read commands never queue behind a long serialized
# command (finding: agent commands failed under load while the worker
# reported ready). They skip only the gateway's coarse serialization; the
# board's own file lock still orders every state access.
SHORT_BOARD_OPERATIONS = {"status", "recover", "findings", "snapshot", "view"}


# Reserved legacy frame sentinel (pre-1.0 wire format); kept for compatibility.
_LEGACY_SENTINEL = b"S3BpTWluZHMgTExD"

class SurfaceAuthenticationError(ValueError):
    """A deliberately sanitized authentication failure."""


class SurfaceAuthorizationError(ValueError):
    """A command is not authorized for the board-derived identity."""


class SurfaceReplayError(ValueError):
    """A command nonce was duplicated or arrived out of order."""


class SurfaceProtocolError(ValueError):
    """A command request did not satisfy the bounded wire protocol."""


@dataclass(frozen=True)
class SessionIdentity:
    project_id: str
    session_id: str
    agent_id: str
    role: str
    task: str
    status: str
    auth_epoch: int

    def public(self) -> dict[str, Any]:
        return asdict(self)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def project_id(root: ProjectRoot) -> str:
    context = project_context(root)
    material = "\0".join(
        (str(context.code_root), str(context.data_root), str(context.workspace_root))
    ).encode("utf-8")
    return hashlib.sha256(material).hexdigest()


def _verifier(project: str, session_id: str, token: str) -> str:
    material = f"{project}\0{session_id}\0{token}".encode("utf-8")
    return hashlib.sha256(material).hexdigest()


def _role_for_session(session: dict[str, Any]) -> str:
    role = control.role_for_kind(str(session.get("kind", "")))
    return {"delivery": "engineering", "reviewer": "qa", "cto": "cto"}[role]


class SessionTokenAuthority:
    """Issue and validate project-private, session-scoped credentials."""

    def __init__(self, root: ProjectRoot):
        self.context = project_context(root)
        self.project_id = project_id(self.context)
        self.directory = self.context.storage_path("control")
        self.path = self.directory / "session-token-verifiers.json"
        self.lock_path = self.directory / ".session-token-verifiers.lock"
        self._pending: dict[str, str] = {}

    @contextmanager
    def _records(self) -> Iterator[dict[str, Any]]:
        self.directory.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a+", encoding="utf-8") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                if self.path.is_file():
                    value = json.loads(self.path.read_text(encoding="utf-8"))
                    if value.get("version") != TOKEN_RECORD_VERSION or not isinstance(value.get("sessions"), dict):
                        raise SurfaceAuthenticationError("session authentication state is unavailable")
                else:
                    value = {"version": TOKEN_RECORD_VERSION, "project_id": self.project_id, "sessions": {}}
                if not hmac.compare_digest(str(value.get("project_id", "")), self.project_id):
                    raise SurfaceAuthenticationError("session authentication state belongs to another project")
                before = json.dumps(value, sort_keys=True)
                yield value
                if json.dumps(value, sort_keys=True) != before or not self.path.exists():
                    temporary = self.path.with_suffix(".tmp")
                    with temporary.open("w", encoding="utf-8") as handle:
                        json.dump(value, handle, indent=2, sort_keys=True)
                        handle.write("\n")
                        handle.flush()
                        os.fsync(handle.fileno())
                    os.replace(temporary, self.path)
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def _session(self, session_id: str) -> dict[str, Any]:
        sessions = {item["id"]: item for item in control.snapshot(self.context).get("sessions", [])}
        session = sessions.get(str(session_id))
        if not session:
            raise SurfaceAuthenticationError("session authentication failed")
        return session

    def prepare(self, session_id: str) -> None:
        """Create a verifier and retain the matching token only in worker memory."""
        session = self._session(session_id)
        if session.get("status") not in AUTHORIZED_SESSION_STATES:
            raise SurfaceAuthenticationError("session authentication failed")
        token = secrets.token_urlsafe(TOKEN_BYTES)
        epoch = int(session.get("auth_epoch", 1))
        with self._records() as value:
            value["sessions"][session_id] = {
                "verifier": _verifier(self.project_id, session_id, token),
                "auth_epoch": epoch,
                "issued_at": _now(),
                "claimed_at": "",
                "revoked_at": "",
            }
        self._pending[session_id] = token

    def claim(self, session_id: str, pid: int) -> str:
        """Return a prepared token once to the attached managed process.

        If the worker restarted before the first claim, its verifier survives
        but the raw token does not.  In that one state a replacement token is
        generated and the old verifier is atomically replaced.
        """
        session = self._session(session_id)
        if (
            session.get("status") != "running"
            or int(session.get("pid") or 0) != int(pid)
            or int(pid) <= 0
        ):
            raise SurfaceAuthenticationError("session authentication failed")
        epoch = int(session.get("auth_epoch", 1))
        with self._records() as value:
            record = value["sessions"].get(session_id)
            if not record or record.get("revoked_at") or record.get("claimed_at"):
                raise SurfaceAuthenticationError("session authentication failed")
            if int(record.get("auth_epoch", 0)) != epoch:
                raise SurfaceAuthenticationError("session authentication failed")
            token = self._pending.pop(session_id, "")
            if not token:
                token = secrets.token_urlsafe(TOKEN_BYTES)
                record.update({
                    "verifier": _verifier(self.project_id, session_id, token),
                    "issued_at": _now(),
                })
            record["claimed_at"] = _now()
        return token

    def claim_from_peer(self, session_id: str, peer_pid: int) -> str:
        """Claim from an OS-authenticated local peer, never a caller PID claim."""
        session = self._session(session_id)
        attached_pid = int(session.get("pid") or 0)
        if peer_pid <= 0 or attached_pid <= 0:
            raise SurfaceAuthenticationError("session authentication failed")
        current_pid = peer_pid
        for _ in range(16):
            if current_pid == attached_pid:
                break
            try:
                result = subprocess.run(
                    ["/bin/ps", "-o", "ppid=", "-p", str(current_pid)],
                    check=True, capture_output=True, text=True, timeout=2,
                )
                parent_pid = int(result.stdout.strip())
            except (OSError, ValueError, subprocess.SubprocessError) as error:
                raise SurfaceAuthenticationError("session authentication failed") from error
            if parent_pid <= 1 or parent_pid == current_pid:
                raise SurfaceAuthenticationError("session authentication failed")
            current_pid = parent_pid
        else:
            raise SurfaceAuthenticationError("session authentication failed")
        return self.claim(session_id, attached_pid)

    def revoke(self, session_id: str) -> None:
        self._pending.pop(session_id, None)
        with self._records() as value:
            record = value["sessions"].get(session_id)
            if record and not record.get("revoked_at"):
                record["revoked_at"] = _now()

    def authenticate(self, token: str) -> SessionIdentity:
        if not isinstance(token, str) or len(token) < 32:
            raise SurfaceAuthenticationError("session authentication failed")
        matched_session = ""
        matched_record: dict[str, Any] | None = None
        with self._records() as value:
            for session_id, record in value["sessions"].items():
                candidate = _verifier(self.project_id, session_id, token)
                if hmac.compare_digest(str(record.get("verifier", "")), candidate):
                    matched_session, matched_record = session_id, dict(record)
        if not matched_session or not matched_record or matched_record.get("revoked_at"):
            raise SurfaceAuthenticationError("session authentication failed")
        session = self._session(matched_session)
        epoch = int(session.get("auth_epoch", 1))
        if (
            session.get("status") not in AUTHORIZED_SESSION_STATES
            or session.get("read_only")
            or session.get("superseded_by_session_id")
            or int(matched_record.get("auth_epoch", 0)) != epoch
        ):
            raise SurfaceAuthenticationError("session authentication failed")

        # Import lazily to keep control/session lifecycle usable without
        # importing the large board module during manager initialization.
        from harness import board

        state = board.snapshot(self.context)
        agents = [
            item for item in state.get("agents", {}).values()
            if item.get("session_id") == matched_session and item.get("active")
        ]
        agent = agents[0] if len(agents) == 1 else {}
        derived_role = str(agent.get("role") or _role_for_session(session))
        expected_role = _role_for_session(session)
        if derived_role != expected_role:
            raise SurfaceAuthenticationError("session authentication failed")
        return SessionIdentity(
            project_id=self.project_id,
            session_id=matched_session,
            agent_id=str(agent.get("id", "")),
            role=derived_role,
            task=str(agent.get("task") or session.get("task") or ""),
            status=str(session.get("status", "")),
            auth_epoch=epoch,
        )


def session_environment(endpoint: str, token: str) -> dict[str, str]:
    """Return the exact environment injected into the provider session."""
    endpoint = str(endpoint).rstrip("/")
    try:
        parsed = urlparse(endpoint)
        port = parsed.port
    except ValueError as error:
        raise ValueError("board endpoint must use IPv4 loopback") from error
    if (
        parsed.scheme != "http"
        or parsed.hostname != "127.0.0.1"
        or parsed.username is not None
        or parsed.password is not None
        or not port
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("board endpoint must use IPv4 loopback")
    if not token:
        raise ValueError("board session token is required")
    return {TOKEN_ENV: token, ENDPOINT_ENV: endpoint, PROTOCOL_ENV: PROTOCOL_VERSION}


def _project_command_lock(project: str) -> threading.Lock:
    with _PROJECT_COMMAND_LOCKS_GUARD:
        return _PROJECT_COMMAND_LOCKS.setdefault(project, threading.Lock())


def _project_nonce_lock(project: str) -> threading.Lock:
    with _PROJECT_COMMAND_LOCKS_GUARD:
        return _PROJECT_NONCE_LOCKS.setdefault(project, threading.Lock())


def _argument_value(arguments: list[str], name: str) -> str | None:
    found: list[str] = []
    for index, argument in enumerate(arguments):
        if argument == name:
            if index + 1 >= len(arguments):
                raise SurfaceProtocolError(f"{name} requires a value")
            found.append(arguments[index + 1])
        if argument.startswith(name + "="):
            found.append(argument[len(name) + 1:])
    if len(found) > 1:
        raise SurfaceProtocolError(f"{name} may be supplied only once")
    return found[0] if found else None


def _concurrent_arguments(
    operation: str, arguments: list[str],
) -> tuple[str, str, list[str], bool]:
    """Strictly parse the small direct-call surface without argparse side effects."""
    value_options = {"--agent"}
    if operation in {"reserve-qa", "review-brief", "review-intents"}:
        value_options.add("--request")
    if operation == "review-intents":
        value_options.add("--intent")
    flag_options = {"--amend"} if operation == "review-intents" else set()
    values: dict[str, list[str]] = {name: [] for name in value_options}
    flags: set[str] = set()
    index = 1
    while index < len(arguments):
        argument = arguments[index]
        name, separator, inline = argument.partition("=")
        if name in value_options:
            if separator:
                value = inline
                index += 1
            else:
                if index + 1 >= len(arguments) or arguments[index + 1].startswith("--"):
                    raise SurfaceProtocolError(f"{name} requires a value")
                value = arguments[index + 1]
                index += 2
            if not value:
                raise SurfaceProtocolError(f"{name} requires a value")
            values[name].append(value)
            continue
        if argument in flag_options:
            if argument in flags:
                raise SurfaceProtocolError(f"{argument} may be supplied only once")
            flags.add(argument)
            index += 1
            continue
        raise SurfaceProtocolError("concurrent board command arguments are invalid")
    for name in ("--agent",):
        if len(values.get(name, [])) != 1:
            raise SurfaceProtocolError(f"{name} must be supplied exactly once")
    request_values = values.get("--request", [])
    if operation in {"review-brief", "review-intents"} and len(request_values) != 1:
        raise SurfaceProtocolError("--request must be supplied exactly once")
    if operation == "reserve-qa" and len(request_values) > 1:
        raise SurfaceProtocolError("--request may be supplied only once")
    intents = values.get("--intent", [])
    if operation == "review-intents" and not intents:
        raise SurfaceProtocolError("--intent must be supplied at least once")
    return (
        values["--agent"][0], request_values[0] if request_values else "",
        intents, "--amend" in flags,
    )


def _without_argument(arguments: list[str], name: str) -> list[str]:
    result: list[str] = []
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        if argument == name:
            if index + 1 >= len(arguments):
                raise SurfaceProtocolError(f"{name} requires a value")
            index += 2
            continue
        if argument.startswith(name + "="):
            index += 1
            continue
        result.append(argument)
        index += 1
    return result


def _canonical_argument(
    arguments: list[str], name: str, value: str, *, required: bool = True,
) -> list[str]:
    claimed = _argument_value(arguments, name)
    if required and claimed is None:
        raise SurfaceProtocolError(f"{name} is required")
    if claimed is not None and claimed != value:
        raise SurfaceAuthorizationError(f"{name.removeprefix('--')} does not match the authenticated session")
    return [*_without_argument(arguments, name), name, value]


def _sanitized_text(value: str, context: ProjectRoot) -> str:
    result = value
    root = project_context(context)
    for path in (root.data_root,):
        raw = str(path)
        if raw and raw in result:
            label = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]
            result = result.replace(raw, f"trusted://{label}")
    return result


def _sanitized_value(value: Any, context: ProjectRoot) -> Any:
    if isinstance(value, str):
        return _sanitized_text(value, context)
    if isinstance(value, list):
        return [_sanitized_value(item, context) for item in value]
    if isinstance(value, dict):
        return {
            key: "[redacted]" if any(word in str(key).lower() for word in ("token", "verifier", "secret"))
            else _sanitized_value(item, context)
            for key, item in value.items()
        }
    return value


@contextmanager
def _without_client_environment() -> Iterator[None]:
    saved = {name: os.environ.pop(name) for name in (TOKEN_ENV, ENDPOINT_ENV, PROTOCOL_ENV) if name in os.environ}
    try:
        yield
    finally:
        os.environ.update(saved)


class CommandGateway:
    """Authenticate, authorize, serialize, and execute board CLI commands."""

    REFUSAL_HOLD_THRESHOLD = 5
    REFUSAL_WINDOW_SECONDS = 45 * 60.0

    def __init__(self, root: ProjectRoot, authority: SessionTokenAuthority):
        self.context = project_context(root)
        self.authority = authority
        self.directory = self.context.storage_path("control")
        self.nonce_path = self.directory / "board-command-nonces.json"
        self.lock_path = self.directory / ".board-command.lock"
        self.execution_lock_path = self.directory / ".board-command-execution.lock"
        self.thread_lock = _project_command_lock(authority.project_id)
        self.nonce_thread_lock = _project_nonce_lock(authority.project_id)
        self._refusals: dict[tuple[str, str], list[float]] = {}
        self._refusal_holds: set[tuple[str, str]] = set()

    def _track_refusal(self, identity, operation: str, error: str) -> None:
        """Repeated identical refusals are a wedge, not agent noise.

        The 2026-08-21 run repeated one refusal for four hours while the
        watchdog spammed recoveries. After the threshold, record a
        control-plane hold so Mission Control shows the diagnosis in red.
        """
        import time as _time
        key = (operation, error[:160])
        stamp = _time.monotonic()
        window = [t for t in self._refusals.get(key, []) if stamp - t < self.REFUSAL_WINDOW_SECONDS]
        window.append(stamp)
        self._refusals[key] = window
        if len(window) < self.REFUSAL_HOLD_THRESHOLD or key in self._refusal_holds:
            return
        self._refusal_holds.add(key)
        try:
            from harness import board
            state = board.snapshot(self.context)
            agent = next((
                value for value in (state.get("agents") or {}).values()
                if value.get("session_id") == identity.session_id
            ), None)
            task = str((agent or {}).get("task") or "")
            if not task or task in {"AWAITING_OWNER_DIRECTION", "GLOBAL_MONITOR", "REVIEW_QUEUE"}:
                task = next(iter(state.get("task_owner_directions") or {}), "")
            if not task:
                return
            board.record_control_plane_hold(
                self.context, task, f"repeated_refusal:{operation}",
                f"The same operation was refused {len(window)} times: {error[:900]}",
            )
        except (OSError, ValueError):
            return

    def _consume_nonce(self, session_id: str, nonce: int) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        if self.nonce_path.is_file():
            try:
                state = json.loads(self.nonce_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as error:
                raise SurfaceReplayError("command replay state is unavailable") from error
            if state.get("version") != NONCE_RECORD_VERSION or not isinstance(state.get("sessions"), dict):
                raise SurfaceReplayError("command replay state is unavailable")
        else:
            state = {"version": NONCE_RECORD_VERSION, "project_id": self.authority.project_id, "sessions": {}}
        if not hmac.compare_digest(str(state.get("project_id", "")), self.authority.project_id):
            raise SurfaceReplayError("command replay state belongs to another project")
        previous = int(state["sessions"].get(session_id, 0))
        if nonce <= previous:
            raise SurfaceReplayError("command nonce was duplicated or arrived out of order")
        state["sessions"][session_id] = nonce
        temporary = self.nonce_path.with_suffix(".tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(state, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, self.nonce_path)

    def _validated_request(self, request: Any) -> tuple[int, str, list[str], dict[str, Any]]:
        required = {"protocol", "nonce", "operation", "arguments"}
        if (
            not isinstance(request, dict) or not required.issubset(request)
            or set(request) - (required | {"artifacts"})
        ):
            raise SurfaceProtocolError("command request fields are invalid")
        if request.get("protocol") != PROTOCOL_VERSION:
            raise SurfaceProtocolError("board protocol version is incompatible")
        nonce = request.get("nonce")
        operation = request.get("operation")
        arguments = request.get("arguments")
        artifacts = request.get("artifacts", {})
        if isinstance(nonce, bool) or not isinstance(nonce, int) or nonce <= 0:
            raise SurfaceProtocolError("command nonce must be a positive integer")
        if not isinstance(operation, str) or operation not in ALL_BOARD_OPERATIONS:
            raise SurfaceAuthorizationError("board operation is not authorized")
        if not isinstance(arguments, list) or len(arguments) > MAX_COMMAND_ARGUMENTS:
            raise SurfaceProtocolError("command arguments are invalid")
        if any(not isinstance(item, str) or "\0" in item for item in arguments):
            raise SurfaceProtocolError("command arguments are invalid")
        if sum(len(item.encode("utf-8")) for item in arguments) > MAX_COMMAND_ARGUMENT_BYTES:
            raise SurfaceProtocolError("command arguments are too large")
        if not isinstance(artifacts, dict) or len(artifacts) > 1:
            raise SurfaceProtocolError("command artifacts are invalid")
        if not arguments or arguments[0] != operation:
            raise SurfaceProtocolError("command operation does not match arguments")
        for argument in arguments[1:]:
            option = argument.partition("=")[0]
            if option.startswith("--") and option not in PROTECTED_ARGUMENTS and any(
                protected.startswith(option) for protected in PROTECTED_ARGUMENTS
            ):
                raise SurfaceAuthorizationError("abbreviated protected arguments are not accepted")
        for forbidden in ("--root", "--data-root", "--workspace-root"):
            if _argument_value(arguments, forbidden) is not None:
                raise SurfaceAuthorizationError("client-supplied trusted roots are not accepted")
        return nonce, operation, list(arguments), dict(artifacts)

    def _authorized_arguments(self, identity: SessionIdentity, operation: str, arguments: list[str]) -> list[str]:
        roles = AUTHORIZATION_MATRIX.get(operation)
        if roles is None or identity.role not in roles:
            raise SurfaceAuthorizationError("board operation is not authorized for this session")
        if operation in RAW_PATH_OPERATIONS:
            raise SurfaceAuthorizationError("raw trusted-store paths are not accepted by the Projects command surface")

        if operation == "register":
            session = self.authority._session(identity.session_id)
            vendor = "Anthropic" if str(session.get("provider", "")).lower() == "claude" else "OpenAI"
            display = {"engineering": "Delivery Agent", "qa": "Independent Reviewer", "cto": "CTO"}[identity.role]
            initial_task = {"engineering": "AWAITING_OWNER_DIRECTION", "qa": "REVIEW_QUEUE", "cto": "GLOBAL_MONITOR"}[identity.role]
            canonical = [operation]
            for name, value in (
                ("--role", identity.role), ("--task", initial_task), ("--name", display),
                ("--vendor", vendor), ("--session-id", identity.session_id),
            ):
                canonical.extend((name, value))
            return canonical

        if operation in AGENT_ARGUMENT_OPERATIONS:
            if not identity.agent_id:
                raise SurfaceAuthorizationError("the authenticated session has no board agent")
            arguments = _canonical_argument(
                arguments, IDENTITY_AGENT_ARGUMENT, identity.agent_id,
            )
        claimed_session = _argument_value(arguments, IDENTITY_SESSION_ARGUMENT)
        if claimed_session is not None:
            raise SurfaceAuthorizationError("caller-supplied session identity is not accepted")
        if identity.role == "engineering" and operation in CURRENT_TASK_OPERATIONS:
            claimed_task = _argument_value(arguments, "--task")
            if claimed_task is not None and claimed_task != identity.task:
                raise SurfaceAuthorizationError("caller task does not match the authenticated session")
            if operation in {"record-finding", "claim-release-repair", "repin-final-review"}:
                arguments = _canonical_argument(arguments, "--task", identity.task)
        if operation == "repin-final-review":
            if _argument_value(arguments, "--repo") is not None:
                raise SurfaceAuthorizationError("client-supplied repository paths are not accepted")
            task = _argument_value(arguments, "--task") or identity.task
            from harness import board
            repository = str(board.snapshot(self.context).get("task_repositories", {}).get(task, ""))
            if not repository:
                raise SurfaceAuthorizationError("the task has no board-derived repository")
            arguments = [*arguments, "--repo", repository]

        return arguments

    def _validated_artifacts(
        self, operation: str, arguments: list[str], artifacts: dict[str, Any],
    ) -> dict[str, dict[str, Any]]:
        specification = UPLOAD_ARGUMENTS.get(operation)
        if not specification:
            if artifacts:
                raise SurfaceAuthorizationError("this board operation does not accept artifact bytes")
            return {}
        option, field, required = specification
        marker = _argument_value(arguments, option)
        if marker is None:
            if required:
                raise SurfaceProtocolError(f"{option} is required")
            if artifacts:
                raise SurfaceProtocolError("unexpected artifact bytes")
            return {}
        if marker != f"upload:{field}":
            raise SurfaceAuthorizationError("agent paths are not accepted by the artifact ingestion surface")
        if set(artifacts) != {field}:
            raise SurfaceProtocolError("artifact field does not match the command")
        metadata = artifacts[field]
        if not isinstance(metadata, dict) or set(metadata) != {"content", "length", "sha256", "media_type"}:
            raise SurfaceProtocolError("artifact metadata is invalid")
        content = metadata.get("content")
        length = metadata.get("length")
        digest = metadata.get("sha256")
        media_type = metadata.get("media_type")
        if not isinstance(content, str) or len(content) > MAX_ARTIFACT_WIRE_BYTES:
            raise SurfaceProtocolError("artifact payload is too large")
        if isinstance(length, bool) or not isinstance(length, int) or length < 0 or length > MAX_ARTIFACT_BYTES:
            raise SurfaceProtocolError("artifact length is invalid")
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise SurfaceProtocolError("artifact hash is invalid")
        expected_media_type = "text/markdown" if "ledger" in field else "text/plain"
        if media_type != expected_media_type:
            raise SurfaceProtocolError("artifact media type is invalid")
        try:
            payload = base64.b64decode(content, validate=True)
        except (ValueError, binascii.Error) as error:
            raise SurfaceProtocolError("artifact payload is not valid base64") from error
        if len(payload) != length:
            raise SurfaceProtocolError("artifact length does not match its bytes")
        actual = hashlib.sha256(payload).hexdigest()
        if not hmac.compare_digest(actual, digest):
            raise SurfaceProtocolError("artifact hash does not match its bytes")
        try:
            payload.decode("utf-8")
        except UnicodeDecodeError as error:
            raise SurfaceProtocolError("artifact must be valid UTF-8") from error
        return {field: {"payload": payload, "length": length, "sha256": digest, "media_type": media_type, "option": option}}

    @staticmethod
    def _write_new_file(path: Path, payload: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(path.parent, 0o700)
        if os.path.lexists(path):
            raise SurfaceProtocolError("artifact destination already exists")
        temporary = path.with_name(path.name + f".tmp-{secrets.token_hex(6)}")
        try:
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            try:
                offset = 0
                while offset < len(payload):
                    written = os.write(descriptor, payload[offset:])
                    if written <= 0:
                        raise OSError("artifact write made no progress")
                    offset += written
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            try:
                os.link(temporary, path, follow_symlinks=False)
            except FileExistsError as error:
                raise SurfaceProtocolError("artifact destination already exists") from error
            temporary.unlink()
        except Exception:
            if os.path.lexists(temporary):
                temporary.unlink()
            raise

    def _store_artifacts(
        self, identity: SessionIdentity, operation: str, nonce: int,
        artifacts: dict[str, dict[str, Any]],
    ) -> dict[str, dict[str, Any]]:
        stored: dict[str, dict[str, Any]] = {}
        session_label = hashlib.sha256(identity.session_id.encode("utf-8")).hexdigest()[:20]
        directory = self.context.storage_path("evidence", "uploads", session_label)
        try:
            for field, metadata in artifacts.items():
                suffix = ".md" if "ledger" in field else ".txt"
                stem = f"{nonce}-{field}-{metadata['sha256']}"
                payload_path = directory / f"{stem}{suffix}"
                manifest_path = directory / f"{stem}.json"
                stored[field] = {
                    **metadata,
                    "path": payload_path,
                    "manifest_path": manifest_path,
                    "created_paths": [],
                    "artifact_id": hashlib.sha256(
                        f"{identity.project_id}\0{identity.session_id}\0{nonce}\0{field}\0{metadata['sha256']}".encode("utf-8")
                    ).hexdigest(),
                }
                self._write_new_file(payload_path, metadata["payload"])
                stored[field]["created_paths"].append(payload_path)
                manifest = {
                    "version": 1,
                    "project_id": identity.project_id,
                    "session_id": identity.session_id,
                    "agent_id": identity.agent_id,
                    "task": identity.task,
                    "operation": operation,
                    "nonce": nonce,
                    "field": field,
                    "sha256": metadata["sha256"],
                    "byte_count": metadata["length"],
                    "media_type": metadata["media_type"],
                    "stored_at": _now(),
                }
                self._write_new_file(
                    manifest_path,
                    (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8"),
                )
                stored[field]["created_paths"].append(manifest_path)
        except Exception:
            self._cleanup_artifacts(stored)
            raise
        return stored

    @staticmethod
    def _cleanup_artifacts(stored: dict[str, dict[str, Any]]) -> None:
        for metadata in stored.values():
            for path in reversed(metadata.get("created_paths", [])):
                if isinstance(path, Path) and os.path.lexists(path):
                    path.unlink()

    @staticmethod
    def _artifact_arguments(
        arguments: list[str], stored: dict[str, dict[str, Any]],
    ) -> list[str]:
        result = list(arguments)
        for metadata in stored.values():
            result = [*_without_argument(result, metadata["option"]), metadata["option"], str(metadata["path"])]
        return result

    def execute(self, token: str, request: Any) -> dict[str, Any]:
        nonce, operation, arguments, raw_artifacts = self._validated_request(request)
        # Authentication, replay protection, and byte ingestion are one short
        # transaction. Do not hold the long-command serialization lock here:
        # Reviewer authoring and board polls must remain responsive while
        # request-review executes its certified suite.
        with self.nonce_thread_lock:
            self.directory.mkdir(parents=True, exist_ok=True)
            with self.lock_path.open("a+", encoding="utf-8") as file_lock:
                fcntl.flock(file_lock.fileno(), fcntl.LOCK_EX)
                try:
                    identity = self.authority.authenticate(token)
                    if any(token in argument for argument in arguments):
                        raise SurfaceAuthorizationError("session credentials are not accepted as command data")
                    arguments = self._authorized_arguments(identity, operation, arguments)
                    artifacts = self._validated_artifacts(operation, arguments, raw_artifacts)
                    self._consume_nonce(identity.session_id, nonce)
                    stored = self._store_artifacts(identity, operation, nonce, artifacts)
                finally:
                    fcntl.flock(file_lock.fileno(), fcntl.LOCK_UN)
        board_succeeded = False
        try:
            execution_arguments = self._artifact_arguments(arguments, stored)
            try:
                if operation in CONCURRENT_BOARD_OPERATIONS:
                    output = self._execute_concurrent(operation, execution_arguments)
                elif operation in SHORT_BOARD_OPERATIONS:
                    output = self._execute_serialized(execution_arguments)
                else:
                    with self.thread_lock:
                        with self.execution_lock_path.open("a+", encoding="utf-8") as execution_lock:
                            fcntl.flock(execution_lock.fileno(), fcntl.LOCK_EX)
                            try:
                                output = self._execute_serialized(execution_arguments)
                            finally:
                                fcntl.flock(execution_lock.fileno(), fcntl.LOCK_UN)
            except SurfaceProtocolError as refusal:
                self._track_refusal(identity, operation, str(refusal))
                raise
            board_succeeded = True
            return {
                "result": _sanitized_value(output, self.context),
                "identity": identity.public(),
                "artifacts": {
                    field: {
                        "artifact_id": metadata["artifact_id"],
                        "sha256": metadata["sha256"],
                        "byte_count": metadata["length"],
                        "media_type": metadata["media_type"],
                    }
                    for field, metadata in stored.items()
                },
            }
        finally:
            if stored and not board_succeeded:
                self._cleanup_artifacts(stored)

    def _execute_serialized(self, arguments: list[str]) -> Any:
        from harness import board

        stdout, stderr = io.StringIO(), io.StringIO()
        with _without_client_environment(), redirect_stdout(stdout), redirect_stderr(stderr):
            try:
                return_code = board.main([*context_cli_arguments(self.context), *arguments])
            except SystemExit as error:
                return_code = int(error.code or 0)
        if return_code != 0:
            message = stderr.getvalue().strip() or "board command failed"
            raise SurfaceProtocolError(
                _sanitized_text(message.removeprefix("error: ").strip(), self.context)
            )
        try:
            return json.loads(stdout.getvalue())
        except json.JSONDecodeError as error:
            raise SurfaceProtocolError("board command returned an invalid response") from error

    def _execute_concurrent(self, operation: str, arguments: list[str]) -> Any:
        """Run the small staged-authoring surface without global command contention."""
        from harness import board

        agent_id, request_id, intents, amendment = _concurrent_arguments(
            operation, arguments,
        )
        if operation == "poll":
            return board.poll(self.context, agent_id)
        if operation == "reserve-qa":
            return board.reserve_qa(self.context, agent_id, request_id)
        if operation == "review-brief":
            return board.review_brief(self.context, request_id, agent_id)
        if operation == "review-intents":
            return board.record_review_intents(
                self.context, agent_id, request_id, intents,
                amendment=amendment,
            )
        raise SurfaceProtocolError("concurrent board operation is not supported")
