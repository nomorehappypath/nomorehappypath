# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
"""Projects manager: the landing page and per-project board worker (spec §6.4, §7).

One stable manager process serves the scrolling project list. Opening a project
takes the exclusive activation lock and starts a SEPARATELY BOUND board worker
(the existing Mission Control viewer) on the board port for that project's
context; the manager never mutates a root captured by a running worker. Derived
information — task counts, running state, health — is computed from each
project's board at render time, never stored.

Phase 1 scope: Open, New project, Adopt existing, Repair, Remove, Close. The
Codex confidentiality notice is display-only (no sandbox enforcement claim).
"""
from __future__ import annotations

import html
import hmac
import json
import os
import re
import secrets
import signal
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse
from urllib.error import HTTPError
from urllib.request import Request, urlopen

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from harness import board, board_surface, control, global_settings, project_chat, project_memory, project_registry as registry, runtime_identity
from harness.project_context import context_cli_arguments
from harness import workspace_settings
from harness.project_manager_page import PAGE

MANAGER_PORT = 8740
BOARD_PORT = 8741
PROJECT_ROUTE = "/project/"
PROXY_HEADER = "X-Harness-Manager-Proxy"
PROXY_TOKEN_ENV = "HARNESS_MANAGER_PROXY_TOKEN"
PROXY_BODY_LIMIT = board.MAX_TOTAL_ATTACHMENT_BYTES + 512 * 1024
WORKER_START_TIMEOUT = 5.0
PAUSE_DRAIN_SECONDS = 1.0
PAUSE_STOP_TIMEOUT = 3.0
FOLDER_PROMPTS = {
    "new-parent": "Choose where the new project folder will be created",
    "adopt-project": "Choose the existing project folder to adopt",
    "repair-project": "Choose the project's current folder",
}

# Delivery runs on Codex in the current role defaults; the confidentiality
# limitation (outside reads can reach the model channel) is disclosed on every
# project view. Display-only in Phase 1 — it claims no sandbox enforcement.
CODEX_NOTICE = (
    "Codex agents can read files outside this project and could relay their "
    "contents through the model connection. Write access is what the harness "
    "confines today; full read confinement arrives with the sandbox phase."
)


def project_folder_name(name: str) -> str:
    """Return a predictable safe folder name derived from a project name."""
    value = re.sub(r"[^A-Za-z0-9._-]+", "-", str(name).strip()).strip("-._").lower()
    if not value:
        raise ValueError("the project name needs at least one letter or number")
    return value


def scaffold_code_root(parent_root: str, name: str) -> Path:
    parent = Path(str(parent_root)).expanduser()
    if not parent.is_absolute():
        raise ValueError("choose a parent folder")
    parent = parent.resolve()
    if not parent.is_dir():
        raise ValueError("the selected parent folder no longer exists")
    target = parent / project_folder_name(name)
    if target.exists():
        raise ValueError(f"{target.name} already exists in that folder; adopt it instead")
    return target


def choose_folder(purpose: str) -> str:
    """Open one fixed-purpose native macOS folder chooser."""
    prompt = FOLDER_PROMPTS.get(str(purpose))
    if not prompt:
        raise ValueError("unknown folder selection purpose")
    if os.uname().sysname != "Darwin":
        raise ValueError("native folder selection is available on macOS only")
    script = f'POSIX path of (choose folder with prompt "{prompt}")'
    try:
        result = subprocess.run(
            ["/usr/bin/osascript", "-e", script], capture_output=True, text=True,
            check=False, timeout=120,
        )
    except subprocess.TimeoutExpired as error:
        raise ValueError("folder selection timed out; try again") from error
    if result.returncode != 0 or not result.stdout.strip():
        return ""
    return str(Path(result.stdout.strip()).resolve())


def page_version() -> str:
    """One digest of the served page so stale browser tabs reload themselves."""
    import hashlib
    return hashlib.sha256(PAGE.encode("utf-8")).hexdigest()[:16]


def derive_status(entry: dict[str, Any]) -> dict[str, Any]:
    """Truthful render-time status straight from the project's own board."""
    health = registry.entry_health(entry)
    counts = {"total": 0, "passed": 0, "open": 0, "awaiting_owner": 0}
    agent_counts = {"total": 0, "delivery": 0, "reviewer": 0, "cto": 0}
    running = False
    latest_progress = "No work has been started yet."
    latest_task = ""
    last_board_activity = ""
    resume_available = False
    paused = False
    board_pause_status = ""
    control_plane_hold = ""
    state_path = Path(entry["data_root"]) / "board" / "state.json"
    if state_path.is_file():
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
            if not isinstance(state, dict):
                raise ValueError("board document is not an object")
            directions = state.get("task_owner_directions", {})
            releases = state.get("release_decisions", {})
            agents = state.get("agents", {})
            briefs = state.get("task_briefs", {})
            events = state.get("events", [])
            pause = state.get("project_pause", {"status": "active"})
            if not isinstance(directions, dict):
                raise ValueError("task_owner_directions is not an object")
            if not isinstance(releases, dict) or any(not isinstance(row, dict) for row in releases.values()):
                raise ValueError("release_decisions is not an object of records")
            if not isinstance(agents, dict) or any(not isinstance(row, dict) for row in agents.values()):
                raise ValueError("agents is not an object of records")
            if not isinstance(briefs, dict) or any(not isinstance(row, dict) for row in briefs.values()):
                raise ValueError("task_briefs is not an object of records")
            if not isinstance(events, list) or any(not isinstance(row, dict) for row in events):
                raise ValueError("events is not a list of records")
            if not isinstance(pause, dict):
                raise ValueError("project_pause is not an object")

            paused = pause.get("status") in {"paused", "resuming"}
            board_pause_status = str(pause.get("status") or "")
            for hold in (state.get("control_plane_holds") or {}).values():
                if isinstance(hold, dict) and hold.get("status") == "open":
                    control_plane_hold = str(hold.get("reason") or "control-plane repair is required")[:300]
                    break

            counts["total"] = len(directions)
            resume_available = bool(directions)
            known_tasks = {str(task) for task in directions}
            accepted_tasks = {
                str(task) for task, decision in releases.items()
                if str(task) in known_tasks and decision.get("decision") == "accepted"
            }
            counts["passed"] = len(accepted_tasks)
            sentinels = {"AWAITING_OWNER_DIRECTION", "GLOBAL_MONITOR", "REVIEW_QUEUE"}
            active_agents = [agent for agent in agents.values() if agent.get("active")]
            live = {
                agent.get("task") for agent in active_agents
                if agent.get("task") not in sentinels
            }
            # A live agent record on an ACCEPTED task is residue (a dead
            # terminal's board entry), never work in progress.
            live = {task for task in live if str(task) not in accepted_tasks}
            pending_release_records = state.get("releases", {})
            awaiting_owner = {
                str(task) for task, record in pending_release_records.items()
                if isinstance(record, dict)
                and record.get("status") == "VISUAL_TEST_REQUIRED"
                and str(task) not in accepted_tasks
                and not releases.get(str(task))
            }
            counts["awaiting_owner"] = len(awaiting_owner)
            counts["open"] = len({task for task in live if str(task) not in awaiting_owner})
            running = bool({task for task in live if str(task) not in awaiting_owner})
            agent_counts["total"] = len(active_agents)
            for agent in active_agents:
                role = str(agent.get("role", "")).casefold()
                if role in {"engineering", "delivery", "developer"}:
                    agent_counts["delivery"] += 1
                elif role in {"qa", "reviewer", "independent reviewer"}:
                    agent_counts["reviewer"] += 1
                elif role == "cto":
                    agent_counts["cto"] += 1

            pending_tasks = [str(task) for task in directions if str(task) not in accepted_tasks]
            active_tasks = [str(task) for task in live if task]
            brief_candidates = [
                (str(task), brief) for task, brief in briefs.items()
                if (not active_tasks or str(task) in active_tasks)
                and str(task) not in accepted_tasks
            ]
            if brief_candidates:
                latest_task, brief = max(
                    brief_candidates, key=lambda item: str(item[1].get("updated_at", ""))
                )
                update = brief.get("update") or brief.get("plan")
                latest_progress = update if isinstance(update, str) and update.strip() else (
                    "Work is in progress and ready to continue." if latest_task in active_tasks
                    else "Saved work is ready to resume."
                )
            elif active_tasks:
                latest_task = sorted(active_tasks)[0]
                latest_progress = "Work is in progress and ready to continue."
            elif pending_tasks:
                latest_task = pending_tasks[-1]
                brief = briefs.get(latest_task, {})
                update = brief.get("update") or brief.get("plan")
                latest_progress = update if isinstance(update, str) and update.strip() else "Saved work is ready to resume."
            elif accepted_tasks:
                latest_task = max(
                    accepted_tasks,
                    key=lambda task: (str(releases.get(task, {}).get("recorded_at", "")), task),
                )
                latest_progress = "Accepted and complete. Reopen to add new instructions or tasks."

            if awaiting_owner and (not latest_task or str(latest_task) in awaiting_owner):
                latest_task = latest_task or sorted(awaiting_owner)[0]
                latest_progress = "Complete — waiting for your test and acceptance."
            elif str(latest_task) in awaiting_owner:
                latest_progress = "Complete — waiting for your test and acceptance."
            event_times = [str(event.get("at", "")) for event in events if event.get("at")]
            if event_times:
                last_board_activity = max(event_times)
            if paused:
                running = False
                resume_available = True
                latest_progress = (
                    "Resume recovery is in progress. The board remains read-only until reconciliation completes."
                    if pause.get("status") == "resuming" else
                    "Paused safely. Saved work is read-only and ready to resume."
                )
        except (OSError, ValueError, TypeError, AttributeError, KeyError):
            health = {
                "ok": False,
                "reasons": health["reasons"] + ["board state unreadable or malformed (recovery will run on open)"],
            }
            counts = {"total": 0, "passed": 0, "open": 0, "awaiting_owner": 0}
            agent_counts = {"total": 0, "delivery": 0, "reviewer": 0, "cto": 0}
            running = False
            latest_progress = "No work has been started yet."
            latest_task = ""
            last_board_activity = ""
            resume_available = False
            paused = False
    return {
        "health": health,
        "task_counts": counts,
        "agent_counts": agent_counts,
        "running": running,
        "paused": paused,
        "board_pause_status": board_pause_status,
        "control_plane_hold": control_plane_hold,
        "resume_available": resume_available,
        "latest_task": latest_task,
        "latest_progress": latest_progress,
        "last_board_activity": last_board_activity,
    }


class ProjectManager:
    """Registry-backed state machine behind the HTTP surface (unit-testable)."""

    def __init__(self, home: Path, board_port: int = BOARD_PORT,
                 manager_url: str = "",
                 public_board_url: str = "",
                 worker_start_timeout: float = WORKER_START_TIMEOUT,
                 terminal_launcher: Any = None,
                 runtime: dict[str, Any] | None = None):
        self.home = Path(home)
        try:
            registered = registry.entries(self.home)
        except (OSError, ValueError, KeyError, TypeError):
            registered = []
        roots = [Path(entry["data_root"]) for entry in registered]
        for entry in registered:
            context = registry.context_for_entry(entry)
            if context.code_root.is_dir():
                project_memory.initialize(
                    context,
                    project_name=entry["name"],
                    description=entry.get("description", ""),
                )
        global_settings.initialize(self.home, roots)
        self.execution_root = Path.cwd().resolve()
        self.board_port = board_port
        self.worker_start_timeout = max(0.05, float(worker_start_timeout))
        self.manager_url = manager_url or f"http://127.0.0.1:{MANAGER_PORT}/"
        self.public_board_url = public_board_url.rstrip("/") + "/" if public_board_url else ""
        self.worker: subprocess.Popen | None = None
        self.worker_project: str = ""
        self.worker_action_token: str = ""
        self.worker_proxy_token: str = ""
        self.worker_failure: dict[str, str] = {}
        self.terminal_launcher = terminal_launcher
        self.runtime = runtime or runtime_identity.PROCESS

    # -- render ------------------------------------------------------------
    def projects_payload(self) -> dict[str, Any]:
        self._reconcile_worker()
        active = registry.active_project(self.home)
        rows = []
        for entry in registry.entries(self.home):
            row = dict(entry)
            row.update(derive_status(entry))
            row["active"] = bool(active and active.get("project_id") == entry["id"])
            row["board_url"] = self._browser_board_url() if row["active"] else ""
            row["worker_error"] = (
                self.worker_failure.get("message", "")
                if self.worker_failure.get("project_id") == entry["id"] else ""
            )
            if row["worker_error"]:
                row["latest_progress"] = row["worker_error"]
                row["resume_available"] = True
            rows.append(row)
        rows.sort(key=lambda row: row["name"].casefold())
        rows.sort(key=lambda row: row.get("last_board_activity") or row.get("last_active_at") or "", reverse=True)
        rows.sort(key=lambda row: not row["active"])
        return {
            "projects": rows, "active": active, "codex_notice": CODEX_NOTICE,
            "page_version": page_version(),
        }

    def settings_payload(self) -> dict[str, Any]:
        value = global_settings.load(self.home)
        return {
            **value,
            "roles": control.ROLE_SETTINGS,
            "providers": control.PROVIDERS,
            "provider_efforts": control.PROVIDER_EFFORTS,
            "provider_models": control.PROVIDER_MODELS,
            "openai": global_settings.openai_status(self.home),
            "chat_model": global_settings.chat_settings(self.home)["model"],
        }

    # -- OpenAI API key ----------------------------------------------------
    def _verify_openai_api_key(self, key: str, *, store: bool) -> dict[str, Any]:
        """Check a key against OpenAI, then store it only if OpenAI accepts it.

        A failed check is recorded only when it describes the key that is
        actually in force.  Typing a bad key into the form must not retract a
        working key's verified state and silently disable chat.
        """
        setting = global_settings.chat_settings(self.home)
        fingerprint = global_settings.openai_key_fingerprint(key)
        tested_at = datetime.now(timezone.utc).isoformat()
        try:
            in_force = global_settings.openai_api_key(self.home)
        except ValueError:
            in_force = ""
        try:
            result = project_chat.verify_api_key(
                key, model=setting["model"], effort=setting["effort"],
            )
        except project_chat.ChatError as error:
            if not in_force or global_settings.openai_key_fingerprint(in_force) == fingerprint:
                global_settings.record_openai_connectivity(self.home, {
                    "ok": False, "key_fingerprint": fingerprint, "model": setting["model"],
                    "tested_at": tested_at, "message": str(error),
                })
            raise ValueError(str(error)) from error
        if store:
            global_settings.store_openai_api_key(self.home, key)
        global_settings.record_openai_connectivity(self.home, {
            "ok": True, "key_fingerprint": fingerprint, "model": setting["model"],
            "tested_at": tested_at, "message": result["message"],
        })
        return self.settings_payload()

    def save_openai_api_key(self, key: str) -> dict[str, Any]:
        return self._verify_openai_api_key(
            global_settings.validate_openai_api_key(key), store=True,
        )

    def test_openai_api_key(self) -> dict[str, Any]:
        return self._verify_openai_api_key(
            global_settings.openai_api_key(self.home), store=False,
        )

    def remove_openai_api_key(self) -> dict[str, Any]:
        global_settings.remove_openai_api_key(self.home)
        return self.settings_payload()

    # -- lifecycle ---------------------------------------------------------
    def worker_argv(self, entry: dict[str, Any], ready_token: str = "") -> list[str]:
        """The exact, cwd-independent argv for the separately bound board worker."""
        context = registry.context_for_entry(entry)
        worker = Path(__file__).resolve().parent / "project_worker.py"
        arguments = [sys.executable, str(worker), *context_cli_arguments(context),
                     "--port", str(self.board_port),
                     "--project-name", entry["name"],
                     "--project-description", entry.get("description", ""),
                     "--manager-url", self.manager_url,
                     "--project-id", entry["id"],
                     "--settings-home", str(self.home.resolve())]
        if self.public_board_url:
            arguments.extend(["--browser-prefix", PROJECT_ROUTE.rstrip("/")])
        if ready_token:
            arguments.extend(["--ready-token", ready_token])
        return arguments

    def _worker_board_url(self) -> str:
        return f"http://127.0.0.1:{self.board_port}/"

    def _browser_board_url(self) -> str:
        return self.public_board_url or self._worker_board_url()

    def _stop_worker(self) -> None:
        worker = self.worker
        if worker and worker.poll() is None:
            worker.terminate()
            try:
                worker.wait(timeout=10)
            except subprocess.TimeoutExpired:
                worker.kill()
        self.worker, self.worker_project = None, ""
        self.worker_action_token, self.worker_proxy_token = "", ""

    def _worker_request(self, path: str, *, data: bytes | None = None,
                        method: str = "GET") -> Request:
        headers = {PROXY_HEADER: self.worker_proxy_token}
        if data is not None:
            headers["Content-Type"] = "application/json"
        return Request(
            self._worker_board_url().rstrip("/") + path,
            data=data, headers=headers, method=method,
        )

    def _reconcile_worker(self) -> None:
        """Turn an unexpected worker exit into truthful, recoverable UI state."""
        if not self.worker or self.worker.poll() is None:
            return
        project_id = self.worker_project
        status = self.worker.poll()
        self.worker, self.worker_project = None, ""
        self.worker_action_token, self.worker_proxy_token = "", ""
        if project_id:
            active = registry.active_project(self.home)
            if active and active.get("project_id") == project_id:
                registry.deactivate(self.home, project_id)
            self.worker_failure = {
                "project_id": project_id,
                "message": (
                    f"Project board stopped unexpectedly (status {status}). "
                    "Resume the project to restore its saved work."
                ),
            }

    def _wait_for_worker(self, ready_token: str, expected_project_ref: str) -> None:
        """Return only after this exact child serves its readiness nonce."""
        # Existing unit fixtures use port 0 as an explicit no-network sentinel;
        # production defaults and CLI launches always use a concrete port.
        if self.board_port == 0:
            return
        deadline = time.monotonic() + self.worker_start_timeout
        last_error = "connection refused"
        while time.monotonic() < deadline:
            if not self.worker or self.worker.poll() is not None:
                status = None if not self.worker else self.worker.poll()
                raise ValueError(
                    f"project board worker exited before serving on port "
                    f"{self.board_port} (status {status})"
                )
            try:
                with urlopen(self._worker_request("/api/ready"), timeout=0.25) as response:
                    payload = json.loads(response.read())
                if payload.get("ready") is not True or payload.get("ready_token") != ready_token:
                    last_error = "another service answered the readiness check"
                    time.sleep(0.05)
                    continue
                if payload.get("project_ref") != expected_project_ref:
                    last_error = "the worker answered for a different project"
                    time.sleep(0.05)
                    continue
                if not runtime_identity.matches(self.runtime, payload.get("runtime") or {}):
                    last_error = "the worker loaded a different Harness runtime"
                    time.sleep(0.05)
                    continue
                if (payload.get("surface") or {}).get("project_chat") is not True:
                    last_error = "the worker did not load the project chat surface"
                    time.sleep(0.05)
                    continue
                watchdog = payload.get("watchdog") or {}
                if (
                    watchdog.get("active") is not True
                    or watchdog.get("interval_seconds") != board.WATCHDOG_INTERVAL_SECONDS
                    or watchdog.get("cto_poll_deadline_seconds") != board.CTO_MONITOR_INTERVAL_SECONDS
                ):
                    last_error = "the worker did not start the bounded CTO watchdog"
                    time.sleep(0.05)
                    continue
                return
            except (OSError, ValueError, TypeError, json.JSONDecodeError) as error:
                last_error = str(error) or type(error).__name__
            time.sleep(0.05)
        raise ValueError(
            f"project board worker did not begin serving on port {self.board_port}: "
            f"{last_error}"
        )

    def _launch_resumed_terminal(self, context, session: dict[str, Any]) -> None:
        if self.terminal_launcher is not None:
            self.terminal_launcher(context, session)
            return
        request = self._worker_request(
            f"/api/sessions/{session['id']}/resume-launch", data=b"{}", method="POST",
        )
        with urlopen(request, timeout=5) as response:
            value = json.loads(response.read())
        if value.get("status") != "launch_requested":
            raise ValueError("project worker did not accept the resume launch")

    def _existing_worker_result(self, entry: dict[str, Any]) -> dict[str, Any] | None:
        self._reconcile_worker()
        if not self.worker or self.worker_project != entry["id"] or self.worker.poll() is not None:
            return None
        if self.board_port != 0:
            try:
                with urlopen(self._worker_request("/api/ready"), timeout=.5) as response:
                    value = json.loads(response.read())
                if (
                    value.get("ready") is not True
                    or value.get("project_name") != entry["name"]
                    or value.get("project_ref") != board_surface.project_id(registry.context_for_entry(entry))
                    or not runtime_identity.matches(self.runtime, value.get("runtime") or {})
                    or (value.get("surface") or {}).get("project_chat") is not True
                    or (value.get("watchdog") or {}).get("active") is not True
                    or (value.get("watchdog") or {}).get("interval_seconds") != board.WATCHDOG_INTERVAL_SECONDS
                    or (value.get("watchdog") or {}).get("cto_poll_deadline_seconds") != board.CTO_MONITOR_INTERVAL_SECONDS
                ):
                    raise ValueError("a different service answered the board readiness check")
            except (OSError, ValueError, TypeError, json.JSONDecodeError) as error:
                raise ValueError(f"resumed project board is not serving: {error}") from error
        return {
            "board_url": self._browser_board_url(),
            "worker_pid": self.worker.pid,
        }

    def open_project(self, project_id: str, _from_resume: bool = False) -> dict[str, Any]:
        self._reconcile_worker()
        entry = registry._find(registry.load(self.home), project_id)
        # The owner's rule: any registered project opens at any time. Health
        # problems are repaired by the open itself (board recovery runs on
        # read, a broken memory index is rebuilt from the board); only a
        # genuinely deleted or moved folder can stop an open.
        if not Path(entry["code_root"]).is_dir():
            raise ValueError(
                f"the project folder no longer exists at {entry['code_root']}; "
                "use Repair to point at its new location, or Remove the project"
            )
        if not _from_resume:
            probe_context = registry.context_for_entry(entry)
            if board.pause_state(probe_context).get("status") in {"paused", "resuming"}:
                # Open means "give me a usable project". Complete the paused
                # board's reconciliation; no terminal is spawned by this.
                return self.resume_project(project_id)
        # Opening a different project IS the owner asking to switch. Boards
        # are durable and certified executions resume, so the previous project
        # is drained and released automatically - no separate Pause/Close
        # ceremony. The one refusal left is a certified check mid-execution.
        active = registry.active_project(self.home)
        if active and active.get("project_id") not in {"", project_id}:
            # Switching is unconditional: certified executions survive
            # interruption and resume, so even a mid-check switch only costs
            # the interrupted remainder, never certified work.
            self.close_project(active["project_id"])
        context = registry.context_for_entry(entry)
        # Opening always materializes the external control document used by
        # session lifecycle recovery, but provider/model/effort stay solely in
        # manager-home settings.json.
        control.initialize(context)
        pause = board.pause_state(context)
        if pause.get("status") in {"paused", "resuming"}:
            resumed_memory = {"status": "paused_read_only", "pause_id": pause.get("pause_id", "")}
        else:
            try:
                resumed_memory = project_memory.resume_context(context, board_state=board.snapshot(context))
            except (ValueError, OSError):
                # A broken memory index never blocks an open: memory is
                # derived state and the board is the authority - rebuild it.
                resumed_memory = project_memory.reconcile_from_board(context, board.snapshot(context))
        record = registry.activate(self.home, project_id)
        Path(entry["data_root"]).mkdir(parents=True, exist_ok=True)
        Path(entry["workspace_root"]).mkdir(parents=True, exist_ok=True)
        # Provider access is automatic, per the owner's directive: every open
        # (re)applies this project's Claude permissions file and Codex trust
        # entry. Both writes are idempotent and project-scoped; a failure is
        # surfaced but never blocks the open - agents would surface it again
        # at launch anyway.
        provider_access: dict[str, Any] = {}
        access_settings = workspace_settings.load(context)
        for provider in ("claude", "codex"):
            try:
                provider_access[provider] = workspace_settings.apply_provider_files(
                    access_settings, provider,
                )
            except (OSError, ValueError) as error:
                provider_access[provider] = {"error": str(error)[:300]}
                print(
                    f"HARNESS PROVIDER ACCESS | {provider} apply failed for "
                    f"{entry['name']}: {str(error)[:200]}", flush=True,
                )
        ready_token = uuid.uuid4().hex
        try:
            self.worker_action_token = ready_token
            self.worker_proxy_token = secrets.token_urlsafe(32)
            environment = {**os.environ, PROXY_TOKEN_ENV: self.worker_proxy_token}
            self.worker = subprocess.Popen(
                self.worker_argv(entry, ready_token), env=environment,
            )
            self.worker_project = project_id
            self._wait_for_worker(ready_token, board_surface.project_id(context))
        except Exception:
            self._stop_worker()
            registry.deactivate(self.home, project_id)
            self.worker_failure = {
                "project_id": project_id,
                "message": (
                    "Project board could not start. Resume the project after "
                    "the worker error is corrected."
                ),
            }
            raise
        self.worker_failure = {}
        return {
            "provider_access": provider_access,
            "activated": record,
            "board_url": self._browser_board_url(),
            "worker_pid": self.worker.pid,
            "memory": resumed_memory,
            "runtime": runtime_identity.public(self.runtime),
        }

    def readiness_payload(self) -> dict[str, Any]:
        """Report the code loaded by this process and its exact active worker."""
        self._reconcile_worker()
        value: dict[str, Any] = {
            "ready": True,
            "runtime": runtime_identity.public(self.runtime),
            "worker": None,
            "board_url": self._browser_board_url(),
        }
        if self.worker and self.worker.poll() is None:
            try:
                with urlopen(self._worker_request("/api/ready"), timeout=.5) as response:
                    worker = json.loads(response.read())
                value["worker"] = worker
                value["ready"] = bool(
                    worker.get("ready") is True
                    and runtime_identity.matches(self.runtime, worker.get("runtime") or {})
                    and (worker.get("surface") or {}).get("project_chat") is True
                )
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                value["ready"] = False
        elif registry.active_project(self.home):
            value["ready"] = False
        return value

    def close_project(self, project_id: str) -> dict[str, Any]:
        if self.worker_project == project_id:
            self._stop_worker()
        registry.deactivate(self.home, project_id)
        return {"closed": project_id}

    def shutdown(self) -> None:
        """Stop the private worker and release this manager's activation."""
        project_id = self.worker_project
        self._stop_worker()
        if project_id:
            registry.deactivate(self.home, project_id)

    def pause_project(
        self, project_id: str, *, drain_seconds: float = PAUSE_DRAIN_SECONDS,
        stop_timeout: float = PAUSE_STOP_TIMEOUT,
    ) -> dict[str, Any]:
        """Quiesce, close the board write gate, and stop terminals as paused."""
        entry = registry._find(registry.load(self.home), project_id)
        context = registry.context_for_entry(entry)
        pause = board.begin_project_pause(context, drain_seconds)
        sessions = [
            value for value in control.snapshot(context).get("sessions", [])
            if value.get("status") in control.ACTIVE_STATUSES
        ]
        session_ids = [str(value["id"]) for value in sessions]
        signalled = []
        for session_id in session_ids:
            try:
                control.enqueue_instruction(
                    context, session_id,
                    "[SYSTEM CONTROL — project-pause] Project pause requested. "
                    "Finish the current board write and preserve your saved next action; "
                    "the terminal will stop after the bounded drain window.",
                    source="project-pause",
                )
                signalled.append(session_id)
            except ValueError:
                # A session that exits during signalling is reconciled below;
                # it cannot make the durable board pause fail.
                pass
        deadline = datetime.fromisoformat(pause["drain_deadline"])
        remaining = max(0.0, (deadline - datetime.now(timezone.utc)).total_seconds())
        if remaining:
            time.sleep(remaining)
        paused = board.finish_project_pause(context)
        stopped = control.pause_sessions(context, session_ids, timeout=stop_timeout)
        return {
            "project_id": project_id,
            "pause": paused,
            "signalled_sessions": signalled,
            "sessions": stopped,
        }

    def resume_project(self, project_id: str) -> dict[str, Any]:
        """Reconcile authoritative state and terminals, then reopen one project."""
        entry = registry._find(registry.load(self.home), project_id)
        context = registry.context_for_entry(entry)
        transaction = board.begin_project_resume(context)
        resume_id = str(transaction.get("resume_id") or transaction.get("last_resume", {}).get("resume_id", ""))
        already_complete = transaction.get("status") == "active"

        if already_complete:
            completed = transaction["last_resume"]
            memory = completed.get("checkpoints", {}).get("board_authority", {}).get("details", {})
            evidence_reuse = completed.get("checkpoints", {}).get("evidence_reconciled", {}).get("details", {})
        else:
            memory = project_memory.reconcile_from_board(context, board.snapshot(context))
            board.record_project_resume_checkpoint(context, resume_id, "board_authority", {
                **memory,
                "message": (
                    memory.get("warning")
                    or "Board and derived memory matched; board remained authoritative"
                ),
            })

            evidence_reuse = board.reconcile_evidence_reuse(context, resume_id)
            board.record_project_resume_checkpoint(context, resume_id, "evidence_reconciled", {
                **evidence_reuse,
                "message": (
                    f"Resume reused {len(evidence_reuse['reused'])} saved PASS records "
                    f"and invalidated {len(evidence_reuse['invalidated'])} after mechanical identity checks"
                ),
            })

            ownership = board.stage_required_delivery_resumes(context, resume_id)
            board.record_project_resume_checkpoint(context, resume_id, "delivery_ownership_reconciled", {
                **ownership,
                "message": (
                    f"Resume staged {len(ownership['staged'])} Delivery owner(s) "
                    "required by unfinished work"
                ),
            })

        # Board completion and Terminal attachment are separate durable
        # boundaries. Reconstruct a missing control record under the exact
        # saved session identity, then reconcile all preserved transports.
        # This prevents a resumed project from becoming "healthy" while an
        # unfinished task has no Delivery process.
        pause = board.pause_state(context)
        board_state = board.snapshot(context)
        settings_override = global_settings.load(self.home)["agent_settings"]
        role_kinds = {
            "cto": "claude_cto", "qa": "claude_reviewer",
            "engineering": "codex_delivery", "development": "codex_delivery",
            "delivery": "codex_delivery", "developer": "codex_delivery",
        }
        known_sessions = {
            str(item.get("id") or "")
            for item in control.snapshot(context).get("sessions", [])
        }
        for agent_id, saved in list((pause.get("agents") or {}).items()):
            agent = (board_state.get("agents") or {}).get(agent_id, {})
            kind = role_kinds.get(str(agent.get("role") or "").casefold())
            if not kind:
                raise ValueError(f"cannot restore saved agent {agent_id}: its role is unknown")
            session_id = str(saved.get("session_id") or "")
            if not session_id:
                if already_complete:
                    raise ValueError(f"cannot restore saved agent {agent_id}: its session identity is missing")
                replacement = control.create(
                    context, kind, settings_override=settings_override,
                )
                board.replace_project_resume_session(
                    context, resume_id, agent_id, str(replacement["id"]),
                )
                session_id = str(replacement["id"])
                known_sessions.add(session_id)
            elif session_id not in known_sessions:
                control.restore_missing_resume_session(
                    context, session_id, kind,
                    settings_override=settings_override,
                )
                known_sessions.add(session_id)

        pause = board.pause_state(context)
        session_ids = [
            str(saved.get("session_id", ""))
            for saved in (pause.get("agents") or {}).values()
            if saved.get("session_id")
        ]
        prepared = control.prepare_resume_sessions(context, session_ids)
        if not already_complete:
            board.record_project_resume_checkpoint(context, resume_id, "sessions_reconciled", {
                "message": (
                    f"Resume reconciled {len(prepared)} saved terminal sessions: "
                    f"{sum(item.get('action') == 're_adopted' for item in prepared)} re-adopted, "
                    f"{sum(item.get('action') == 'relaunch' for item in prepared)} staged to relaunch"
                ),
                "sessions": [
                    {"id": item.get("id", ""), "action": item.get("action", "")}
                    for item in prepared
                ],
            })

        worker = self._existing_worker_result(entry)
        if worker is None:
            active = registry.active_project(self.home)
            if active and active.get("project_id") == project_id and self.worker is None:
                registry.deactivate(self.home, project_id)
            opened = self.open_project(project_id, _from_resume=True)
            worker = {"board_url": opened["board_url"], "worker_pid": opened["worker_pid"]}

        # Preserved terminals are NEVER relaunched automatically: spawning
        # windows and spending tokens is the owner's decision. Staged sessions
        # surface on the board with a relaunch button; their saved next
        # actions wait durably until the owner presses it.
        if not already_complete:
            completed = board.finish_project_resume(context, resume_id)

        return {
            "project_id": project_id,
            "resume": completed,
            "memory": memory,
            "evidence_reuse": evidence_reuse,
            "sessions": prepared,
            **worker,
        }


def make_handler(manager: ProjectManager):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # quiet server
            return

        def _send(self, code: int, value: Any, content_type="application/json"):
            body = (value if isinstance(value, (bytes, str)) else json.dumps(value))
            if isinstance(body, str):
                body = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _body(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(length) or b"{}") if length else {}

        def _browser_close_token(self) -> str:
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError as error:
                raise ValueError("request length is invalid") from error
            if length < 1 or length > 4096:
                raise ValueError("Mission Control close token is required")
            if not self.headers.get("Content-Type", "").lower().startswith(
                "application/x-www-form-urlencoded"
            ):
                raise ValueError("Mission Control close form encoding is invalid")
            values = parse_qs(self.rfile.read(length).decode("utf-8"), keep_blank_values=True)
            return str(values.get("action_token", [""])[0])

        def _require_json_api(self):
            if not self.headers.get("Content-Type", "").lower().startswith("application/json"):
                raise ValueError("project API mutations require application/json")

        def _redirect(self, location: str):
            self.send_response(303)
            self.send_header("Location", location)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def _proxy_project(self):
            """Proxy the private worker under the next app's single public origin."""
            manager._reconcile_worker()
            if not manager.worker or manager.worker.poll() is not None:
                return self._send(503, {"error": "no project is currently open"})
            parsed = urlparse(self.path)
            if parsed.path == PROJECT_ROUTE.rstrip("/"):
                return self._redirect(PROJECT_ROUTE)
            if not parsed.path.startswith(PROJECT_ROUTE):
                return self._send(404, {"error": "not found"})
            internal_path = "/" + parsed.path[len(PROJECT_ROUTE):]
            if parsed.query:
                internal_path += "?" + parsed.query
            internal_origin = f"http://127.0.0.1:{manager.board_port}"
            if self.command != "GET":
                public = urlparse(manager.manager_url)
                public_port = public.port or 80
                allowed_origins = {
                    f"http://127.0.0.1:{public_port}", f"http://localhost:{public_port}",
                }
                if (
                    self.headers.get("Origin", "") not in allowed_origins
                    or self.headers.get("Sec-Fetch-Site", "same-origin") not in {"same-origin", "none"}
                ):
                    return self._send(403, {"error": "same-origin project request required"})
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                return self._send(400, {"error": "request length is invalid"})
            if length < 0 or length > PROXY_BODY_LIMIT:
                return self._send(413, {"error": "project request is too large"})
            body = self.rfile.read(length) if length else None
            headers = {
                "Origin": internal_origin,
                "Sec-Fetch-Site": "same-origin",
                PROXY_HEADER: manager.worker_proxy_token,
            }
            for name in ("Authorization", "Content-Type", "X-Harness-Chat-Action"):
                if self.headers.get(name):
                    headers[name] = self.headers[name]
            request = Request(
                internal_origin + internal_path, data=body, headers=headers, method=self.command,
            )
            try:
                with urlopen(request, timeout=40) as response:
                    status = response.status
                    content_type = response.headers.get("Content-Type", "application/octet-stream")
                    response_body = response.read(PROXY_BODY_LIMIT + 1)
            except HTTPError as error:
                status = error.code
                content_type = error.headers.get("Content-Type", "application/json")
                response_body = error.read(PROXY_BODY_LIMIT + 1)
            except OSError:
                return self._send(502, {"error": "the open project's worker is unavailable"})
            if len(response_body) > PROXY_BODY_LIMIT:
                return self._send(502, {"error": "project worker response is too large"})
            return self._send(status, response_body, content_type)

        def do_GET(self):
            request_path = urlparse(self.path).path
            if request_path == PROJECT_ROUTE.rstrip("/") or request_path.startswith(PROJECT_ROUTE):
                return self._proxy_project()
            if request_path == "/":
                return self._send(
                    200, PAGE.replace("__PAGE_VERSION__", page_version()),
                    "text/html; charset=utf-8",
                )
            if request_path == "/favicon.png":
                icon = Path(__file__).resolve().parent / "assets" / "nomorehappypath.png"
                try:
                    body = icon.read_bytes()
                except OSError:
                    return self._send(404, {"error": "not found"})
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Cache-Control", "public, max-age=86400")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if request_path == "/api/ready":
                return self._send(200, manager.readiness_payload())
            if request_path == "/api/projects":
                return self._send(200, manager.projects_payload())
            if request_path == "/api/settings":
                return self._send(200, manager.settings_payload())
            return self._send(404, {"error": "not found"})

        def do_POST(self):
            try:
                if urlparse(self.path).path.startswith(PROJECT_ROUTE):
                    return self._proxy_project()
                browser_parts = urlparse(self.path).path.strip("/").split("/")
                if len(browser_parts) == 3 and browser_parts[0] == "projects" and browser_parts[2] == "close":
                    project_id = unquote(browser_parts[1])
                    action_token = self._browser_close_token()
                    if (
                        manager.worker_project != project_id
                        or not manager.worker_action_token
                        or not hmac.compare_digest(action_token, manager.worker_action_token)
                    ):
                        raise ValueError("Mission Control close token is invalid or expired")
                    manager.close_project(project_id)
                    return self._redirect("/")
                self._require_json_api()
                if self.path == "/api/folders/browse":
                    selected = choose_folder(self._body().get("purpose", ""))
                    return self._send(200, {"path": selected, "cancelled": not bool(selected)})
                if self.path == "/api/settings":
                    value = self._body()
                    updated = global_settings.update_agent_settings(
                        manager.home, value.get("agent_settings", value),
                    )
                    return self._send(200, {**manager.settings_payload(), **updated})
                if self.path == "/api/settings/openai-key":
                    return self._send(200, manager.save_openai_api_key(self._body().get("key", "")))
                if self.path == "/api/settings/openai-key/test":
                    return self._send(200, manager.test_openai_api_key())
                if self.path == "/api/settings/connect":
                    value = self._body()
                    result = global_settings.test_connection(
                        manager.home, value.get("provider", ""), value.get("model", ""),
                        value.get("effort", ""), manager.execution_root,
                    )
                    return self._send(200, result)
                if self.path == "/api/projects":
                    value = self._body()
                    kind = value.get("kind", "scaffold")
                    code_root = value.get("code_root", "")
                    created_paths: list[Path] = []
                    if kind == "scaffold" and value.get("parent_root"):
                        code_root = scaffold_code_root(value["parent_root"], value.get("name", ""))
                        try:
                            Path(code_root).mkdir()
                            created_paths.append(Path(code_root))
                            (Path(code_root) / ".harness").mkdir()
                            created_paths.append(Path(code_root) / ".harness")
                            # Disclosed build attribution (Legal page names it).
                            # Scaffolded projects only - adopted repositories
                            # are never written to.
                            stamp = Path(code_root) / "BUILT_WITH.md"
                            stamp.write_text(
                                "# Built with NoMoreHappyPath\n\n"
                                "This project was created by NoMoreHappyPath "
                                "(nomorehappypath.com), the AI development-governance "
                                "platform by KpiMinds LLC.\n\n"
                                f"- Created: {datetime.now(timezone.utc).isoformat()}\n"
                                f"- Installation: {global_settings.installation_id(manager.home)}\n\n"
                                "This attribution file is disclosed on the platform's "
                                "Legal page. The project's code belongs to its owner; "
                                "this file only records the build tool.\n",
                                encoding="utf-8",
                            )
                            created_paths.append(stamp)
                        except OSError:
                            for path in reversed(created_paths):
                                try:
                                    path.rmdir() if path.is_dir() else path.unlink()
                                except OSError:
                                    pass
                            raise
                    kwargs = {}
                    if kind == "adopted":
                        kwargs = {"kind": "adopted", "data_root": value.get("data_root") or None,
                                  "workspace_root": value.get("workspace_root") or None}
                    try:
                        entry = registry.register(
                            manager.home, value.get("name", ""), Path(code_root),
                            description=value.get("description", ""), **kwargs)
                    except (OSError, ValueError, KeyError, RuntimeError):
                        for path in reversed(created_paths):
                            try:
                                path.rmdir() if path.is_dir() else path.unlink()
                            except OSError:
                                pass
                        raise
                    return self._send(201, entry)
                parts = self.path.strip("/").split("/")
                if len(parts) == 4 and parts[:2] == ["api", "projects"]:
                    project_id, action = parts[2], parts[3]
                    if action == "open":
                        return self._send(200, manager.open_project(project_id))
                    if action == "close":
                        return self._send(200, manager.close_project(project_id))
                    if action == "pause":
                        return self._send(200, manager.pause_project(project_id))
                    if action == "resume":
                        return self._send(200, manager.resume_project(project_id))
                    if action == "repair":
                        fields = {k: v for k, v in self._body().items()
                                  if k in ("name", "description", "code_root", "data_root", "workspace_root")}
                        current = registry._find(registry.load(manager.home), project_id)
                        if fields.get("code_root") and current.get("kind") == "scaffold":
                            old_code = Path(current["code_root"])
                            if Path(current["data_root"]) == old_code / ".harness":
                                fields["data_root"] = str(Path(fields["code_root"]) / ".harness")
                        return self._send(200, registry.update_entry(manager.home, project_id, **fields))
                return self._send(404, {"error": "not found"})
            except (OSError, ValueError, KeyError, RuntimeError) as error:
                return self._send(400, {"error": str(error)})

        def do_DELETE(self):
            if urlparse(self.path).path.startswith(PROJECT_ROUTE):
                return self._proxy_project()
            parts = self.path.strip("/").split("/")
            try:
                if urlparse(self.path).path == "/api/settings/openai-key":
                    return self._send(200, manager.remove_openai_api_key())
                if len(parts) == 3 and parts[:2] == ["api", "projects"]:
                    return self._send(200, registry.remove(manager.home, parts[2]))
                return self._send(404, {"error": "not found"})
            except (OSError, ValueError, KeyError, RuntimeError) as error:
                return self._send(400, {"error": str(error)})

    return Handler


def serve(home: Path, host: str = "127.0.0.1", port: int = MANAGER_PORT,
          board_port: int = BOARD_PORT) -> None:
    if port == board_port:
        raise ValueError("manager and private worker ports must be different")
    manager = ProjectManager(home, board_port=board_port)
    server = ThreadingHTTPServer((host, port), make_handler(manager))
    manager.manager_url = f"http://{host}:{server.server_address[1]}/"
    manager.public_board_url = manager.manager_url.rstrip("/") + PROJECT_ROUTE
    previous_term = None
    if threading.current_thread() is threading.main_thread():
        previous_term = signal.getsignal(signal.SIGTERM)

        def graceful_stop(_signum, _frame):
            raise KeyboardInterrupt

        signal.signal(signal.SIGTERM, graceful_stop)
    print(f"NoMoreHappyPath Projects on http://{host}:{server.server_address[1]}/ (home: {home})", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        manager.shutdown()
        server.server_close()
        if previous_term is not None:
            signal.signal(signal.SIGTERM, previous_term)


def main(argv=None) -> int:
    import argparse
    parser = argparse.ArgumentParser(description="Harness projects landing page")
    parser.add_argument("--home", default=str(registry.default_home()))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=MANAGER_PORT)
    parser.add_argument("--board-port", type=int, default=BOARD_PORT)
    parser.add_argument("--migrate-root", default="", help="register this existing single-root harness as the default project")
    args = parser.parse_args(argv)
    home = Path(args.home).expanduser()
    if args.migrate_root:
        registry.migrate_single_root(home, Path(args.migrate_root).expanduser())
    serve(home, args.host, args.port, args.board_port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
