# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
"""The running harness is never restarted over an open project.

Run: PYTHONPATH=. python3 -m unittest tests.test_update_gate -v
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.request import urlopen

from harness import project_registry as registry, update_gate
from tests.environment_support import require_loopback

HARNESS_ROOT = Path(__file__).resolve().parents[1]


class RestartDecisionTests(unittest.TestCase):
    def test_active_project_defers_the_restart(self):
        self.assertFalse(update_gate.restart_allowed({"projects": [
            {"id": "a", "active": False}, {"id": "b", "active": True},
        ]}))

    def test_paused_project_releases_the_restart(self):
        self.assertTrue(update_gate.restart_allowed({"projects": [
            {"id": "a", "active": True, "board_pause_status": "paused"},
        ]}))

    def test_draining_and_resuming_projects_still_defer(self):
        for status in ("draining", "resuming", "active", "", None):
            with self.subTest(status=status):
                self.assertFalse(update_gate.restart_allowed({"projects": [
                    {"id": "a", "active": True, "board_pause_status": status},
                ]}))

    def test_idle_manager_allows_the_restart(self):
        self.assertTrue(update_gate.restart_allowed({"projects": [
            {"id": "a", "active": False}, {"id": "b", "running": True, "active": False},
        ]}))
        self.assertTrue(update_gate.restart_allowed({"projects": []}))

    def test_malformed_payloads_never_wedge_the_launcher(self):
        for payload in (None, "nope", {"projects": "nope"}, {"projects": [None, 4]}, {}):
            with self.subTest(payload=payload):
                self.assertTrue(update_gate.restart_allowed(payload))


class GateProcessTests(unittest.TestCase):
    def setUp(self):
        require_loopback()
        super().setUp()

    def serve(self, payload: dict) -> str:
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                return

            def do_GET(self):
                body = json.dumps(payload).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return f"http://127.0.0.1:{server.server_address[1]}/"

    def run_gate(self, url: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["python3", "-E", str(HARNESS_ROOT / "harness" / "update_gate.py"),
             "--manager-url", url],
            capture_output=True, text=True, timeout=20,
        )

    def test_active_project_exits_deferred_and_names_it(self):
        url = self.serve({"projects": [{"id": "p1", "name": "Dev Harness", "active": True}]})
        completed = self.run_gate(url)
        self.assertEqual(completed.returncode, update_gate.EXIT_RESTART_DEFERRED)
        self.assertIn("Dev Harness", completed.stdout)

    def test_idle_manager_exits_allowed(self):
        url = self.serve({"projects": [{"id": "p1", "active": False}]})
        self.assertEqual(self.run_gate(url).returncode, update_gate.EXIT_RESTART_ALLOWED)

    def test_paused_project_exits_allowed(self):
        url = self.serve({"projects": [
            {"id": "p1", "name": "Dev Harness", "active": True, "board_pause_status": "paused"},
        ]})
        self.assertEqual(self.run_gate(url).returncode, update_gate.EXIT_RESTART_ALLOWED)

    def test_unreachable_manager_allows_restart(self):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            dead_port = probe.getsockname()[1]
        completed = self.run_gate(f"http://127.0.0.1:{dead_port}/")
        self.assertEqual(completed.returncode, update_gate.EXIT_RESTART_ALLOWED)


class PausedRowChainTests(unittest.TestCase):
    """A real paused board yields a row that releases the gate end to end."""

    def row(self, pause_status: str) -> dict:
        from harness import project_manager
        with tempfile.TemporaryDirectory() as temporary:
            data_root = Path(temporary) / "data"
            (data_root / "board").mkdir(parents=True)
            (data_root / "board" / "state.json").write_text(json.dumps({
                "project_pause": {"status": pause_status},
                "task_owner_directions": {}, "release_decisions": {},
                "agents": {}, "task_briefs": {}, "events": [],
                "qa_requests": {},
            }))
            entry = {
                "id": "p1", "name": "Chain", "kind": "adopted",
                "code_root": temporary, "data_root": str(data_root),
                "workspace_root": temporary,
            }
            return {**project_manager.derive_status(entry), "active": True}

    def test_paused_board_row_releases_and_active_board_row_defers(self):
        paused_row = self.row("paused")
        self.assertEqual(paused_row["board_pause_status"], "paused")
        self.assertTrue(update_gate.restart_allowed({"projects": [paused_row]}))
        active_row = self.row("active")
        self.assertFalse(update_gate.restart_allowed({"projects": [active_row]}))
        resuming_row = self.row("resuming")
        self.assertFalse(update_gate.restart_allowed({"projects": [resuming_row]}))


@unittest.skipIf(os.environ.get("HARNESS_SKIP_LAUNCHER_E2E") == "1", "launcher e2e disabled")
class LauncherEndToEndTests(unittest.TestCase):
    """The real launcher against a real manager: change source, watch the gate."""

    def setUp(self):
        require_loopback()
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        base = Path(self.temporary.name)
        self.tree = base / "harness_copy"
        for part in ("harness", "scripts"):
            shutil.copytree(HARNESS_ROOT / part, self.tree / part)
        subprocess.run(["git", "init", "-q"], cwd=self.tree, check=True)
        subprocess.run(["git", "add", "-A"], cwd=self.tree, check=True,
                       env=self._git_env())
        subprocess.run(["git", "commit", "-qm", "seed"], cwd=self.tree, check=True,
                       env=self._git_env())
        self.home = base / "home"
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            self.port = probe.getsockname()[1]
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            self.worker_port = probe.getsockname()[1]
        self.log = base / "launcher.log"
        log_handle = self.log.open("w")
        self.addCleanup(log_handle.close)
        self.process = subprocess.Popen(
            ["/bin/bash", str(self.tree / "scripts" / "start_project_manager.sh"),
             "--home", str(self.home), "--port", str(self.port),
             "--worker-port", str(self.worker_port), "--no-open"],
            stdout=log_handle, stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        self.addCleanup(self._stop)
        self._wait_ready()

    def _git_env(self):
        return {
            "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
            "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
            "HOME": self.temporary.name,
        }

    def _stop(self):
        subprocess.run(["kill", "-TERM", f"-{self.process.pid}"], capture_output=True)
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            subprocess.run(["kill", "-KILL", f"-{self.process.pid}"], capture_output=True)

    def _wait_ready(self, timeout: float = 30.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                with urlopen(f"http://127.0.0.1:{self.port}/api/projects", timeout=2) as response:
                    json.loads(response.read())
                    return
            except OSError:
                time.sleep(0.2)
        self.fail("manager did not become ready: " + self.log.read_text()[-2000:])

    def _manager_pid(self) -> int:
        result = subprocess.run(
            ["lsof", "-ti", f"tcp:{self.port}", "-sTCP:LISTEN"],
            capture_output=True, text=True,
        )
        pids = [int(pid) for pid in result.stdout.split()]
        self.assertTrue(pids, "no manager is listening")
        return pids[0]

    def _register_project(self) -> str:
        code_root = Path(self.temporary.name) / "product"
        code_root.mkdir()
        entry = registry.register(
            self.home, name="busy-project", code_root=code_root, kind="adopted",
        )
        return entry["id"]

    def test_source_change_defers_while_active_and_restarts_when_released(self):
        project_id = self._register_project()
        registry.activate(self.home, project_id, os.getpid())
        first_pid = self._manager_pid()

        marker = self.tree / "harness" / "update_gate.py"
        marker.write_text(marker.read_text() + "\n# touched by launcher e2e\n")

        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if "UPDATE PENDING" in self.log.read_text():
                break
            time.sleep(0.3)
        self.assertIn("UPDATE PENDING", self.log.read_text())
        time.sleep(2)
        self.assertEqual(self._manager_pid(), first_pid, "manager restarted over an active project")
        self.assertNotIn("HARNESS NEXT REFRESH", self.log.read_text())

        registry.deactivate(self.home, project_id)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if "HARNESS NEXT REFRESH" in self.log.read_text():
                break
            time.sleep(0.3)
        self.assertIn("HARNESS NEXT REFRESH", self.log.read_text())
        self._wait_ready()
        deadline = time.monotonic() + 15
        new_pid = first_pid
        while time.monotonic() < deadline:
            new_pid = self._manager_pid()
            if new_pid != first_pid:
                break
            time.sleep(0.3)
        self.assertNotEqual(new_pid, first_pid, "manager did not restart after release")


if __name__ == "__main__":
    unittest.main()
