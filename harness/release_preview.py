# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
"""Automatic candidate previews for releases awaiting the owner's visual test.

When a task reaches VISUAL_TEST_REQUIRED its reviewed code sits on a task
branch the owner never checks out.  This supervisor turns that gate into
something the owner can actually open: it materializes the exact reviewed
commit in a detached clone, launches the project's configured preview command
against a free loopback port, health-checks the URL, and records the result on
the release so Mission Control can show "Open the candidate preview".

The supervisor owns the full preview lifecycle: it starts previews for pending
releases, restarts them when a repaired candidate supersedes the old commit,
stops them once the owner responds, and reconciles stale processes after a
worker restart.  A preview failure is recorded on the release with the log
tail — never silently dropped.
"""
from __future__ import annotations

import shlex
import shutil
import socket
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.request import urlopen

from harness import board, git_process, workspace_settings


TICK_SECONDS = 5.0
HEALTH_POLL_SECONDS = 0.5
STOP_GRACE_SECONDS = 5.0
LOG_TAIL_BYTES = 1200


def preview_root(root: Any) -> Path:
    return board.board_dir(root) / "preview"


def _start_token(pid: int) -> str:
    """One ps-derived token so a reused PID is never mistaken for our child."""
    result = subprocess.run(
        ["ps", "-p", str(pid), "-o", "lstart="], capture_output=True, text=True,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _git(cwd: Path, *arguments: str) -> str:
    result = git_process.run(
        ["git", *arguments], cwd=cwd, capture_output=True, text=True, timeout=120,
    )
    if result.returncode != 0:
        raise ValueError(f"git {' '.join(arguments)} failed: {result.stderr.strip()[:300]}")
    return result.stdout.strip()


def _log_tail(path: Path) -> str:
    try:
        data = path.read_bytes()
    except OSError:
        return ""
    return data[-LOG_TAIL_BYTES:].decode("utf-8", "replace").strip()


APP_BUNDLE_GLOBS = (
    "src-tauri/target/release/bundle/macos/*.app",
    "src-tauri/target/release/bundle/dmg/*.app",
    "dist/mac*/*.app",
    "out/*/*.app",
    "*.app",
)


def find_app_bundle(workspace: str) -> Path | None:
    """The newest built macOS app bundle in a candidate workspace, if any."""
    base = Path(workspace or "")
    if not base.is_dir():
        return None
    candidates: list[Path] = []
    for pattern in APP_BUNDLE_GLOBS:
        for found in base.glob(pattern):
            if found.is_dir() and (found / "Contents" / "MacOS").is_dir():
                candidates.append(found)
    if not candidates:
        return None
    return max(candidates, key=lambda bundle: bundle.stat().st_mtime)


def open_app_bundle(root: Any, task: str) -> dict[str, Any]:
    """Open the release's recorded app bundle on the owner's desktop.

    Only the path the supervisor itself recorded is ever opened; the caller
    supplies nothing but the task name.
    """
    release = (board.snapshot(root).get("releases") or {}).get(str(task or "")) or {}
    preview = release.get("preview") or {}
    if preview.get("status") != "app_bundle":
        raise ValueError("this release has no built app bundle recorded")
    app_path = Path(str(preview.get("app_path") or ""))
    if (
        app_path.suffix != ".app"
        or not app_path.is_dir()
        or not (app_path / "Contents").is_dir()
    ):
        raise ValueError("the recorded app bundle is no longer present; rebuild or use the workspace path")
    completed = subprocess.run(["/usr/bin/open", str(app_path)], capture_output=True, text=True, timeout=30)
    if completed.returncode != 0:
        raise ValueError(f"the app could not be opened: {completed.stderr.strip()[:200]}")
    return {"opened": True, "app_path": str(app_path), "app_name": app_path.stem}


class Preview:
    """One running (or attempted) candidate preview process."""

    def __init__(self, task: str, head_commit: str, directory: Path) -> None:
        self.task = task
        self.head_commit = head_commit
        self.directory = directory
        self.process: subprocess.Popen | None = None
        self.port = 0
        self.url = ""
        self.command = ""

    @property
    def source(self) -> Path:
        return self.directory / "source"

    @property
    def state_dir(self) -> Path:
        return self.directory / "state"

    @property
    def log_path(self) -> Path:
        return self.directory / "preview.log"

    def alive(self) -> bool:
        return bool(self.process and self.process.poll() is None)

    def stop(self) -> None:
        process = self.process
        self.process = None
        if not process or process.poll() is not None:
            return
        try:
            subprocess.run(["kill", "-TERM", f"-{process.pid}"], capture_output=True)
        except OSError:
            pass
        deadline = time.monotonic() + STOP_GRACE_SECONDS
        while process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.1)
        if process.poll() is None:
            try:
                subprocess.run(["kill", "-KILL", f"-{process.pid}"], capture_output=True)
            except OSError:
                pass
            process.wait(timeout=5)


class ReleasePreviewSupervisor:
    """Keep one healthy preview per release that awaits the owner's test."""

    def __init__(self, root: Any, *, tick_seconds: float = TICK_SECONDS) -> None:
        self.root = root
        self.tick_seconds = tick_seconds
        self.previews: dict[str, Preview] = {}
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> None:
        if self.thread is not None:
            raise ValueError("release preview supervisor is already started")
        self.thread = threading.Thread(
            target=self._run, name="harness-release-preview", daemon=True,
        )
        self.thread.start()

    def _run(self) -> None:
        while not self.stop_event.wait(self.tick_seconds):
            try:
                self.tick()
            except Exception as error:  # noqa: BLE001 — a tick failure must never kill the worker
                print(f"HARNESS RELEASE PREVIEW | tick failed: {str(error)[:300]}", flush=True)

    def shutdown(self) -> None:
        self.stop_event.set()
        if self.thread is not None:
            self.thread.join(timeout=max(1.0, self.tick_seconds + 0.5))
        with self.lock:
            for preview in self.previews.values():
                preview.stop()
            self.previews.clear()

    # -- one pass ----------------------------------------------------------
    def tick(self) -> dict[str, Any]:
        with self.lock:
            return self._tick_locked()

    def _tick_locked(self) -> dict[str, Any]:
        report: dict[str, Any] = {"pending": [], "stopped": [], "paused": False}
        paused = board.pause_state(self.root).get("status") != "active"
        report["paused"] = paused
        try:
            state = board.snapshot(self.root)
        except (OSError, ValueError):
            return report
        releases = state.get("releases") or {}
        decisions = state.get("release_decisions") or {}
        pending = {
            task: release for task, release in releases.items()
            if release.get("status") == "VISUAL_TEST_REQUIRED" and not decisions.get(task)
        }
        # Stop previews whose release closed, disappeared, or changed commit.
        for task in list(self.previews):
            release = pending.get(task)
            preview = self.previews[task]
            if release is None or str(release.get("head_commit") or "") != preview.head_commit:
                preview.stop()
                shutil.rmtree(preview.directory, ignore_errors=True)
                del self.previews[task]
                report["stopped"].append(task)
        if paused:
            # Never write board state or launch work under a paused project;
            # a preview that is already healthy keeps serving read-only.
            return report
        self._reconcile_recorded_processes(releases)
        settings = workspace_settings.load(self.root).get("preview") or {}
        for task, release in pending.items():
            report["pending"].append(task)
            self._ensure(task, release, settings, state)
        return report

    def _reconcile_recorded_processes(self, releases: dict[str, Any]) -> None:
        """Kill previous-worker preview processes we no longer own."""
        for task, release in releases.items():
            recorded = release.get("preview") or {}
            pid = recorded.get("pid")
            token = str(recorded.get("start_token") or "")
            if not pid or task in self.previews:
                continue
            if token and _start_token(int(pid)) == token:
                subprocess.run(["kill", "-TERM", f"-{int(pid)}"], capture_output=True)

    def _record(self, task: str, preview_value: dict[str, Any]) -> None:
        try:
            board.record_release_preview(self.root, task, preview_value)
        except (OSError, ValueError) as error:
            print(f"HARNESS RELEASE PREVIEW | record failed for {task}: {str(error)[:200]}", flush=True)

    def _ensure(
        self, task: str, release: dict[str, Any], settings: dict[str, Any],
        state: dict[str, Any],
    ) -> None:
        head_commit = str(release.get("head_commit") or "")
        recorded = release.get("preview") or {}
        workspace = str((state.get("task_workspaces") or {}).get(task) or "")
        branch_record = (state.get("task_branches") or {}).get(task)
        branch = (
            str(branch_record.get("branch") or "") if isinstance(branch_record, dict)
            else str(branch_record or "")
        ).removeprefix("refs/heads/")
        command = str(settings.get("command") or "").strip()
        if not head_commit:
            return
        if not command:
            preview = self.previews.pop(task, None)
            if preview is not None:
                # The owner cleared the command while a preview served: stop
                # the process before its record is overwritten, or an unclean
                # worker death later would orphan it invisibly.
                preview.stop()
                shutil.rmtree(preview.directory, ignore_errors=True)
            bundle = find_app_bundle(workspace)
            if bundle is not None:
                built_at = datetime.fromtimestamp(
                    bundle.stat().st_mtime, tz=timezone.utc,
                ).isoformat()
                if (
                    recorded.get("status") != "app_bundle"
                    or recorded.get("app_path") != str(bundle)
                    or recorded.get("built_at") != built_at
                    or preview is not None
                ):
                    self._record(task, {
                        "status": "app_bundle",
                        "app_path": str(bundle),
                        "app_name": bundle.stem,
                        "built_at": built_at,
                        "head_commit": head_commit,
                        "workspace": workspace,
                        "branch": branch,
                    })
                return
            if recorded.get("status") != "unconfigured" or preview is not None:
                self._record(task, {
                    "status": "unconfigured",
                    "head_commit": head_commit,
                    "workspace": workspace,
                    "branch": branch,
                })
            return
        preview = self.previews.get(task)
        if preview and preview.alive() and preview.command == command:
            if recorded.get("status") != "ready" or recorded.get("pid") != preview.process.pid:
                self._record(task, self._ready_value(preview, workspace, branch))
            return
        if preview:
            preview.stop()
            del self.previews[task]
        if recorded.get("status") == "failed" and recorded.get("command") == command:
            # A recorded failure stays visible until the owner changes the
            # command or asks for a retry (which clears the record).
            return
        self._launch(task, head_commit, command, settings, workspace, branch)

    def _ready_value(self, preview: Preview, workspace: str, branch: str) -> dict[str, Any]:
        return {
            "status": "ready",
            "url": preview.url,
            "command": preview.command,
            "pid": preview.process.pid,
            "start_token": _start_token(preview.process.pid),
            "head_commit": preview.head_commit,
            "workspace": workspace,
            "branch": branch,
            "started_at": board.now(),
        }

    def _launch(
        self, task: str, head_commit: str, command: str, settings: dict[str, Any],
        workspace: str, branch: str,
    ) -> None:
        directory = preview_root(self.root) / task
        preview = Preview(task, head_commit, directory)
        failure = ""
        try:
            self._materialize(preview, workspace)
            preview.port = _free_port()
            url_template = str(settings.get("url_template") or "http://127.0.0.1:{port}/")
            preview.url = url_template.replace("{port}", str(preview.port))
            preview.command = command
            rendered = command.replace("{port}", str(preview.port)).replace(
                "{state_dir}", shlex.quote(str(preview.state_dir)),
            )
            self._record(task, {
                "status": "starting", "url": preview.url, "command": command,
                "head_commit": head_commit, "workspace": workspace, "branch": branch,
            })
            with preview.log_path.open("ab") as log:
                log.write(f"\n=== preview launch {board.now()} | {rendered}\n".encode())
                log.flush()
                preview.process = subprocess.Popen(
                    rendered, shell=True, cwd=preview.source,
                    stdout=log, stderr=log, start_new_session=True,
                )
            failure = self._await_health(preview, settings)
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            failure = str(error)[:300]
        if failure:
            preview.stop()
            self._record(task, {
                "status": "failed", "command": command, "head_commit": head_commit,
                "workspace": workspace, "branch": branch, "error": failure,
                "log_tail": _log_tail(preview.log_path),
            })
            return
        self.previews[task] = preview
        self._record(task, self._ready_value(preview, workspace, branch))

    def _materialize(self, preview: Preview, workspace: str) -> None:
        """Check out the exact reviewed commit without touching the workspace."""
        if not workspace or not Path(workspace).is_dir():
            raise ValueError("the task workspace with the reviewed commit is not available")
        source = preview.source
        if source.is_dir():
            try:
                if _git(source, "rev-parse", "HEAD") == preview.head_commit:
                    preview.state_dir.mkdir(parents=True, exist_ok=True)
                    return
            except ValueError:
                pass
            shutil.rmtree(preview.directory, ignore_errors=True)
        preview.directory.mkdir(parents=True, exist_ok=True)
        preview.state_dir.mkdir(parents=True, exist_ok=True)
        result = git_process.run(
            ["git", "clone", "--no-checkout", workspace, str(source)],
            cwd=preview.directory, capture_output=True, text=True, timeout=300,
        )
        if result.returncode != 0:
            raise ValueError(f"could not clone the reviewed workspace: {result.stderr.strip()[:300]}")
        _git(source, "checkout", "--detach", preview.head_commit)

    def _await_health(self, preview: Preview, settings: dict[str, Any]) -> str:
        timeout = float(settings.get("startup_timeout_seconds") or 45)
        deadline = time.monotonic() + max(5.0, min(300.0, timeout))
        while time.monotonic() < deadline:
            if not preview.alive():
                return "the preview command exited before serving its URL"
            try:
                with urlopen(preview.url, timeout=3) as response:
                    if response.status < 500:
                        return ""
            except OSError:
                pass
            time.sleep(HEALTH_POLL_SECONDS)
        return f"the preview did not answer at {preview.url} within {int(timeout)}s"
