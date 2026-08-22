#!/usr/bin/env python3
# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
"""Trusted Projects worker with the authenticated board command surface.

The legacy ``board_viewer`` remains the standalone compatibility server.  The
Projects manager launches this worker, which adds session credential bootstrap
and (in later slices) authenticated board commands without allowing an active
project context to fall back to direct agent-side storage access.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import signal
import shlex
import socket
import socketserver
import struct
import subprocess
import sys
import tempfile
import threading
import time
from collections import OrderedDict, deque
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from harness import board, board_viewer, control, control_plane, global_settings, project_chat, release_coordinator, release_preview, runtime_identity
from harness.board_surface import (
    MAX_ARTIFACT_WIRE_BYTES, PROTOCOL_VERSION, CommandGateway, SessionTokenAuthority,
    SurfaceAuthenticationError, SurfaceAuthorizationError, SurfaceProtocolError,
    SurfaceReplayError, session_environment,
)
from harness.project_context import add_context_arguments, context_cli_arguments, context_from_args


BOOTSTRAP_LIMIT = 8 * 1024
COMMAND_LIMIT = MAX_ARTIFACT_WIRE_BYTES + 256 * 1024
CHAT_BODY_LIMIT = 4 * 1024
CHAT_RESPONSE_LIMIT = 16 * 1024
CHAT_RATE_LIMIT = 12
CHAT_RATE_WINDOW_SECONDS = 60.0
CHAT_RECEIPT_LIMIT = 24
CHAT_REQUEST_ID = re.compile(r"^[A-Za-z0-9_-]{8,80}$")
MANAGER_PROXY_HEADER = "X-Harness-Manager-Proxy"
MANAGER_PROXY_TOKEN_ENV = "HARNESS_MANAGER_PROXY_TOKEN"


class ChatBusy(ValueError):
    pass


class ChatDuplicate(ValueError):
    pass


class ProjectChatService:
    """One project's bounded, cancellable, in-memory chat coordinator."""

    def __init__(self, root, settings_home: Path | None, project_id: str,
                 answerer: Callable | None = None) -> None:
        self.root = root
        self.settings_home = settings_home
        self.project_id = str(project_id or "")
        self.answerer = answerer or project_chat.answer_question
        self.lock = threading.Lock()
        self.drained = threading.Condition(self.lock)
        self.slot = threading.BoundedSemaphore(1)
        self.active: dict[str, threading.Event] = {}
        self.receipts: OrderedDict[str, tuple[str, dict]] = OrderedDict()
        self.started: deque[float] = deque(maxlen=CHAT_RATE_LIMIT)

    def _rate_admit(self) -> None:
        now = time.monotonic()
        while self.started and now - self.started[0] >= CHAT_RATE_WINDOW_SECONDS:
            self.started.popleft()
        if len(self.started) >= CHAT_RATE_LIMIT:
            raise ChatBusy("Project chat rate limit reached; retry shortly")
        self.started.append(now)

    def submit(self, request_id: str, question: str) -> dict:
        if not isinstance(request_id, str):
            raise ValueError("request_id must be text")
        if not CHAT_REQUEST_ID.fullmatch(request_id):
            raise ValueError("request_id is invalid")
        project_chat.question_targets(question)
        if self.settings_home is None:
            raise project_chat.ProviderFailure("Project chat provider settings are unavailable")
        availability = global_settings.chat_availability(self.settings_home)
        if not availability["available"]:
            raise project_chat.ProviderFailure(availability["reason"])
        question_digest = hashlib.sha256(question.encode("utf-8")).hexdigest()
        with self.lock:
            prior = self.receipts.get(request_id)
            if prior is not None:
                if not hmac.compare_digest(prior[0], question_digest):
                    raise ChatDuplicate("This request ID was already used for a different question")
                self.receipts.move_to_end(request_id)
                return {**json.loads(json.dumps(prior[1])), "duplicate": True}
            if request_id in self.active:
                raise ChatDuplicate("This chat request is already running")
            self._rate_admit()
            if not self.slot.acquire(blocking=False):
                raise ChatBusy("Another project chat request is already running")
            cancel = threading.Event()
            self.active[request_id] = cancel
        try:
            result = self.answerer(
                self.root, question, settings_home=self.settings_home,
                project_id=self.project_id, cancel_event=cancel,
            )
            if cancel.is_set():
                raise project_chat.ChatCancelled("The chat request was cancelled")
            response = {
                "request_id": request_id,
                "answer": result["answer"],
                "source_ids": list(result.get("source_ids", [])),
                "snapshot": {
                    key: result.get("snapshot", {}).get(key)
                    for key in ("at", "board_sequence", "digest")
                },
                "unknown": bool(result.get("unknown")),
                "duplicate": False,
            }
            if len(json.dumps(response, separators=(",", ":")).encode("utf-8")) > CHAT_RESPONSE_LIMIT:
                raise project_chat.AnswerValidationError("The chat response exceeded its size limit")
            with self.lock:
                self.receipts[request_id] = (
                    question_digest, json.loads(json.dumps(response)),
                )
                self.receipts.move_to_end(request_id)
                while len(self.receipts) > CHAT_RECEIPT_LIMIT:
                    self.receipts.popitem(last=False)
            return response
        finally:
            with self.drained:
                self.active.pop(request_id, None)
                self.slot.release()
                self.drained.notify_all()

    def cancel(self, request_id: str) -> bool:
        if not isinstance(request_id, str) or not CHAT_REQUEST_ID.fullmatch(request_id):
            raise ValueError("request_id is invalid")
        with self.lock:
            event = self.active.get(request_id)
            if event is None:
                return False
            event.set()
            return True

    def shutdown(self) -> None:
        with self.drained:
            for event in self.active.values():
                event.set()
            self.receipts.clear()
            # Do not let a normal manager restart return while the direct API
            # request is still unwinding, but keep shutdown strictly bounded.
            self.drained.wait_for(lambda: not self.active, timeout=2.0)


class StartupMaintenance:
    """Run trusted worker recovery once, after a controlled resume is active."""

    def __init__(self, root) -> None:
        self.root = root
        self.lock = threading.Lock()
        self.complete = False

    def run(self) -> None:
        with self.lock:
            if self.complete:
                return
            if board.pause_state(self.root).get("status") != "active":
                raise SurfaceAuthorizationError(
                    "project resume must complete before board commands are accepted"
                )
            board.recover_git_transactions(self.root)
            release_coordinator.coordinate(self.root)
            self.complete = True


class ProjectWatchdog:
    """Run bounded liveness and CTO monitoring checks for one open project."""

    def __init__(
        self, root, *, interval_seconds: float = board.WATCHDOG_INTERVAL_SECONDS,
        stale_after: int = board.AGENT_STALE_SECONDS,
    ) -> None:
        if interval_seconds <= 0 or stale_after < 1:
            raise ValueError("watchdog interval and stale threshold must be positive")
        self.root = root
        self.interval_seconds = interval_seconds
        self.stale_after = stale_after
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.last_report: dict = {}

    def tick(self) -> list[dict]:
        self.last_report = control_plane.tick(self.root, self.stale_after)
        return list(self.last_report.get("stalled", []))

    def _run(self) -> None:
        while not self.stop_event.wait(self.interval_seconds):
            try:
                self.tick()
            except (OSError, ValueError) as error:
                print(f"HARNESS PROJECT WATCHDOG | check failed: {str(error)[:300]}", flush=True)

    def start(self) -> None:
        if self.thread is not None:
            raise ValueError("project watchdog is already started")
        self.thread = threading.Thread(
            target=self._run, name="harness-project-watchdog", daemon=True,
        )
        self.thread.start()

    def shutdown(self) -> None:
        self.stop_event.set()
        if self.thread is not None:
            self.thread.join(timeout=max(1.0, min(3.0, self.interval_seconds + 0.5)))

    def status(self) -> dict:
        return {
            "active": bool(self.thread is not None and self.thread.is_alive()),
            "interval_seconds": self.interval_seconds,
            "cto_poll_deadline_seconds": board.CTO_MONITOR_INTERVAL_SECONDS,
        }


def launch_terminal(root, session: dict, bootstrap_socket: str) -> None:
    """Launch one managed Terminal with only a non-secret local socket in argv."""
    if sys.platform != "darwin":
        raise RuntimeError("central CLI launch currently requires macOS Terminal")
    runner = Path(__file__).resolve().parents[1] / "scripts" / "run_managed_agent.sh"
    arguments = [
        "/usr/bin/env", "-u", "BASH_ENV", "-u", "ENV",
        "/bin/bash", "--noprofile", "--norc", str(runner),
        *context_cli_arguments(root),
        "--python", sys.executable,
        "--session-id", session["id"],
        "--kind", session["kind"],
        "--board-bootstrap", bootstrap_socket,
    ]
    command = "exec " + shlex.join(arguments)
    color = control.SESSION_COLORS.get(session.get("color", "black"), control.SESSION_COLORS["black"])
    rgb = "{" + ", ".join(str(round(channel * 65535 / 255)) for channel in color["rgb"]) + "}"
    applescript = f'''on run argv
 tell application "Terminal"
 activate
 set newTab to do script (item 1 of argv)
 tell newTab
  set background color to {rgb}
  set normal text color to {{65535, 65535, 65535}}
 end tell
 end tell
end run'''
    subprocess.run(
        ["/usr/bin/osascript", "-e", applescript, command],
        check=True, capture_output=True, text=True,
    )


def _peer_pid(connection: socket.socket) -> int:
    if sys.platform == "darwin":
        return struct.unpack("i", connection.getsockopt(0, 0x002, 4))[0]
    if hasattr(socket, "SO_PEERCRED"):
        return struct.unpack(
            "3i", connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
        )[0]
    raise SurfaceAuthenticationError("session authentication failed")


def make_bootstrap_server(
    path: str, authority: SessionTokenAuthority, endpoint: Callable[[], str],
) -> socketserver.ThreadingUnixStreamServer:
    """Build the one-time credential handoff with an OS-derived peer PID."""
    class BootstrapHandler(socketserver.BaseRequestHandler):
        def handle(self):
            try:
                payload = bytearray()
                while len(payload) <= BOOTSTRAP_LIMIT:
                    part = self.request.recv(min(4096, BOOTSTRAP_LIMIT + 1 - len(payload)))
                    if not part:
                        break
                    payload.extend(part)
                    if b"\n" in part:
                        break
                if len(payload) > BOOTSTRAP_LIMIT or b"\n" not in payload:
                    raise ValueError("bootstrap request is invalid")
                raw, trailing = bytes(payload).split(b"\n", 1)
                if trailing:
                    raise ValueError("bootstrap request is invalid")
                data = json.loads(raw)
                if not isinstance(data, dict) or set(data) != {"session_id", "protocol"}:
                    raise ValueError("bootstrap request is invalid")
                if str(data.get("protocol", "")) != PROTOCOL_VERSION:
                    raise ValueError("board protocol version is incompatible")
                token = authority.claim_from_peer(
                    str(data.get("session_id", "")), _peer_pid(self.request),
                )
                response = {"environment": session_environment(endpoint(), token)}
            except (ValueError, TypeError, OSError, json.JSONDecodeError) as error:
                response = {"error": str(error)}
            self.request.sendall(
                json.dumps(response, separators=(",", ":")).encode("utf-8") + b"\n"
            )

    server = socketserver.ThreadingUnixStreamServer(path, BootstrapHandler)
    os.chmod(path, 0o600)
    return server


def make_handler(
    root,
    *,
    authority: SessionTokenAuthority,
    gateway: CommandGateway | None = None,
    endpoint: Callable[[], str],
    bootstrap_socket: Callable[[], str] = lambda: "",
    project_name: str = "",
    project_description: str = "",
    manager_url: str = "",
    project_id: str = "",
    settings_home: Path | None = None,
    ready_token: str = "",
    before_command: Callable[[str], None] = lambda _token: None,
    chat_service: ProjectChatService | None = None,
    chat_action_token: str = "",
    runtime: dict | None = None,
    browser_prefix: str = "",
    manager_proxy_token: str = "",
    worker_health=None,
):
    gateway = gateway or CommandGateway(root, authority)
    chat_action_token = chat_action_token or secrets.token_urlsafe(32)
    chat_service = chat_service or ProjectChatService(root, settings_home, project_id)
    BaseHandler = board_viewer.make_handler(
        root, project_name, project_description, manager_url, settings_home, ready_token, project_id,
        chat_action_token, runtime=runtime, api_prefix=browser_prefix,
        worker_health=worker_health,
    )

    class Handler(BaseHandler):
        def _manager_proxy_authorized(self) -> bool:
            return bool(
                manager_proxy_token
                and hmac.compare_digest(
                    self.headers.get(MANAGER_PROXY_HEADER, ""), manager_proxy_token,
                )
            )

        def do_GET(self):
            if browser_prefix and not self._manager_proxy_authorized():
                self.send_json(403, {"error": "project worker is private to the manager"})
                return
            super().do_GET()

        def _small_json(self, limit: int = BOOTSTRAP_LIMIT) -> dict:
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError as error:
                raise ValueError("request length is invalid") from error
            if length < 0 or length > limit:
                raise ValueError("request is too large")
            payload = self.rfile.read(length)
            if len(payload) != length:
                raise ValueError("request body is incomplete")
            value = json.loads(payload or b"{}")
            if not isinstance(value, dict):
                raise ValueError("request must be a JSON object")
            return value

        def do_POST(self):
            path = urlparse(self.path).path
            if (
                browser_prefix and path != "/api/board/command"
                and not self._manager_proxy_authorized()
            ):
                self.send_json(403, {"error": "project worker is private to the manager"})
                return
            if path in {"/api/project-chat", "/api/project-chat/cancel"}:
                try:
                    host = self.headers.get("Host", "")
                    port = int(self.server.server_address[1])
                    allowed_origins = {
                        f"http://127.0.0.1:{port}", f"http://localhost:{port}",
                    }
                    origin = self.headers.get("Origin", "")
                    if origin not in allowed_origins or host not in {
                        f"127.0.0.1:{port}", f"localhost:{port}",
                    }:
                        raise SurfaceAuthorizationError("same-origin project chat request required")
                    if self.headers.get("Sec-Fetch-Site", "same-origin") not in {"same-origin", "none"}:
                        raise SurfaceAuthorizationError("cross-site project chat request refused")
                    if not hmac.compare_digest(
                        self.headers.get("X-Harness-Chat-Action", ""), chat_action_token,
                    ):
                        raise SurfaceAuthenticationError("project chat action credential is stale or invalid")
                    content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
                    if content_type != "application/json":
                        raise ValueError("project chat requires application/json")
                    if board.pause_state(root).get("status") != "active":
                        raise SurfaceAuthorizationError("project chat is unavailable while the project is paused")
                    data = self._small_json(CHAT_BODY_LIMIT)
                    if path == "/api/project-chat":
                        if set(data) != {"request_id", "question"}:
                            raise ValueError("project chat accepts only request_id and question")
                        response = chat_service.submit(data["request_id"], data["question"])
                        self.send_json(200, response)
                    else:
                        if set(data) != {"request_id"}:
                            raise ValueError("project chat cancellation accepts only request_id")
                        self.send_json(200, {
                            "request_id": data["request_id"],
                            "cancelled": chat_service.cancel(data["request_id"]),
                        })
                except SurfaceAuthenticationError as error:
                    self.send_json(401, {"error": str(error), "code": "stale_action"})
                except SurfaceAuthorizationError as error:
                    self.send_json(403, {"error": str(error), "code": "forbidden"})
                except ChatDuplicate as error:
                    self.send_json(409, {"error": str(error), "code": "duplicate"})
                except ChatBusy as error:
                    self.send_json(429, {"error": str(error), "code": "busy"})
                except project_chat.ProviderTimeout as error:
                    self.send_json(504, {"error": str(error), "code": error.code})
                except (project_chat.StaleSnapshotError, project_chat.ChatCancelled) as error:
                    self.send_json(409, {"error": str(error), "code": error.code})
                except project_chat.ChatError as error:
                    self.send_json(502, {"error": str(error), "code": error.code})
                except (ValueError, TypeError, json.JSONDecodeError) as error:
                    self.send_json(400, {"error": str(error), "code": "invalid_request"})
                return
            if path == "/api/board/command":
                if gateway is None:
                    self.send_json(503, {"error": "authenticated board command surface is unavailable"})
                    return
                try:
                    authorization = self.headers.get("Authorization", "")
                    if not authorization.startswith("Bearer ") or authorization.count(" ") != 1:
                        raise SurfaceAuthenticationError("session authentication failed")
                    token = authorization.removeprefix("Bearer ")
                    before_command(token)
                    result = gateway.execute(token, self._small_json(COMMAND_LIMIT))
                    self.send_json(200, result)
                except SurfaceAuthenticationError as error:
                    self.send_json(401, {"error": str(error)})
                except SurfaceAuthorizationError as error:
                    self.send_json(403, {"error": str(error)})
                except SurfaceReplayError as error:
                    self.send_json(409, {"error": str(error)})
                except (SurfaceProtocolError, ValueError, TypeError, json.JSONDecodeError) as error:
                    self.send_json(400, {"error": str(error)})
                return
            if path == "/api/session/bootstrap":
                self.send_json(410, {"error": "HTTP session bootstrap is disabled"})
                return

            if path == "/api/sessions":
                session = None
                try:
                    if board.pause_state(root).get("status") in {"paused", "resuming"}:
                        self.send_json(409, {
                            "error": "This project is paused and read-only. Resume it from Projects before making changes."
                        }); return
                    data = self._small_json()
                    settings_override = (
                        global_settings.load(settings_home)["agent_settings"]
                        if settings_home else None
                    )
                    session = control.create(
                        root, data.get("kind", ""), data.get("task", ""),
                        data.get("color", "black"), settings_override=settings_override,
                    )
                    authority.prepare(session["id"])
                    launch_terminal(root, session, bootstrap_socket())
                    self.send_json(201, {"session": session})
                except Exception as error:
                    if session:
                        authority.revoke(session["id"])
                        session = control.fail_launch(root, session["id"], f"unable to open Terminal: {error}")
                    self.send_json(500, {"error": str(error), "session": session or {}})
                return

            resume_prefix, resume_suffix = "/api/sessions/", "/resume-launch"
            if path.startswith(resume_prefix) and path.endswith(resume_suffix):
                session_id = path[len(resume_prefix):-len(resume_suffix)]
                prepared = False
                launched = False
                try:
                    session = next(
                        item for item in control.snapshot(root).get("sessions", [])
                        if item.get("id") == session_id
                    )
                    if session.get("status") != "launching":
                        raise ValueError("resume terminal is not staged for launch")
                    if not session.get("resume_launch_requested_at"):
                        # The owner's click IS the launch request: claim the
                        # staged relaunch here so no separate manager step is
                        # needed and duplicates stay impossible.
                        session = control.mark_resume_launch_requested(root, session_id)
                    authority.prepare(session_id)
                    prepared = True
                    launch_terminal(root, session, bootstrap_socket())
                    launched = True
                    self.send_json(201, {"session_id": session_id, "status": "launch_requested"})
                except Exception as error:
                    # Never revoke an existing live credential merely because
                    # an unrelated or duplicate resume request failed
                    # validation.  Revoke only a fresh credential prepared by
                    # this request whose terminal launch did not complete.
                    if prepared and not launched:
                        authority.revoke(session_id)
                    self.send_json(400, {"error": str(error)})
                return

            super().do_POST()

        def log_message(self, *_):
            return

    return Handler


def serve(
    root,
    host: str = "127.0.0.1",
    port: int = 8742,
    project_name: str = "",
    project_description: str = "",
    manager_url: str = "",
    project_id: str = "",
    settings_home: Path | None = None,
    ready_token: str = "",
    browser_prefix: str = "",
) -> None:
    if host not in {"127.0.0.1", "localhost"}:
        raise ValueError("Projects worker must bind only to loopback")
    maintenance = StartupMaintenance(root)
    loaded_runtime = runtime_identity.PROCESS
    if board.pause_state(root).get("status") == "active":
        maintenance.run()
    authority = SessionTokenAuthority(root)
    gateway = CommandGateway(root, authority)
    chat = ProjectChatService(root, settings_home, project_id)
    watchdog = ProjectWatchdog(root)
    preview_supervisor = release_preview.ReleasePreviewSupervisor(root)
    chat_action_token = secrets.token_urlsafe(32)
    manager_proxy_token = os.environ.get(MANAGER_PROXY_TOKEN_ENV, "")
    if browser_prefix and not manager_proxy_token:
        raise ValueError("Projects worker requires its manager proxy credential")

    def before_command(token: str) -> None:
        # Do not let an unauthenticated local caller trigger trusted recovery.
        authority.authenticate(token)
        maintenance.run()

    address: dict[str, str] = {"endpoint": ""}
    bootstrap_directory = Path(tempfile.mkdtemp(prefix=f"harness-bootstrap-{authority.project_id[:12]}-"))
    os.chmod(bootstrap_directory, 0o700)
    bootstrap_path = bootstrap_directory / "claim.sock"
    server = None
    bootstrap_server = None
    bootstrap_thread = None
    previous_term = None
    try:
        if threading.current_thread() is threading.main_thread():
            previous_term = signal.getsignal(signal.SIGTERM)

            def graceful_stop(_signum, _frame):
                raise KeyboardInterrupt

            signal.signal(signal.SIGTERM, graceful_stop)
        server = ThreadingHTTPServer(
            (host, port),
            make_handler(
                root,
                authority=authority,
                gateway=gateway,
                endpoint=lambda: address["endpoint"],
                bootstrap_socket=lambda: str(bootstrap_path),
                project_name=project_name,
                project_description=project_description,
                manager_url=manager_url,
                project_id=project_id,
                settings_home=settings_home,
                ready_token=ready_token,
                before_command=before_command,
                chat_service=chat,
                chat_action_token=chat_action_token,
                runtime=loaded_runtime,
                browser_prefix=browser_prefix,
                manager_proxy_token=manager_proxy_token,
                worker_health=watchdog.status,
            ),
        )
        address["endpoint"] = f"http://127.0.0.1:{server.server_address[1]}"
        bootstrap_server = make_bootstrap_server(
            str(bootstrap_path), authority, lambda: address["endpoint"],
        )
        bootstrap_thread = threading.Thread(target=bootstrap_server.serve_forever, daemon=True)
        bootstrap_thread.start()
        watchdog.start()
        preview_supervisor.start()
        print(f"Live Harness Project Worker: {address['endpoint']}/", flush=True)
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        preview_supervisor.shutdown()
        watchdog.shutdown()
        chat.shutdown()
        if previous_term is not None:
            signal.signal(signal.SIGTERM, previous_term)
        if server is not None:
            server.server_close()
        if bootstrap_server is not None:
            bootstrap_server.shutdown()
        if bootstrap_thread is not None:
            bootstrap_thread.join(timeout=3)
        if bootstrap_server is not None:
            bootstrap_server.server_close()
        if bootstrap_path.exists():
            bootstrap_path.unlink()
        bootstrap_directory.rmdir()


def main(argv=None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Authenticated NoMoreHappyPath Projects worker")
    add_context_arguments(parser)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8742)
    parser.add_argument("--project-name", default="")
    parser.add_argument("--project-description", default="")
    parser.add_argument("--manager-url", default="")
    parser.add_argument("--project-id", default="", help=argparse.SUPPRESS)
    parser.add_argument("--settings-home", default="")
    parser.add_argument("--ready-token", default="", help=argparse.SUPPRESS)
    parser.add_argument("--browser-prefix", default="", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    serve(
        context_from_args(args), args.host, args.port,
        project_name=args.project_name,
        project_description=args.project_description,
        manager_url=args.manager_url,
        project_id=args.project_id,
        settings_home=Path(args.settings_home).resolve() if args.settings_home else None,
        ready_token=args.ready_token,
        browser_prefix=args.browser_prefix,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
