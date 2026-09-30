# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""An app update keeps the owner's open paused project open (owner, 2026-09-28).

Every update restarts the manager (the launcher's REFRESH sends it SIGTERM and
starts a new one). Its shutdown released the activation, so the owner's open,
paused studio came back closed after each update. The real restart path is
replayed here: a real manager process, SIGTERM exactly as the launcher sends
it, a new manager process, and the Projects API the page reads.

Run:  PYTHONPATH=. python3 -m unittest tests.test_refresh_keeps_open_project -v
"""
from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from urllib.request import urlopen

from harness import board, project_manager, project_registry as registry
from tests.environment_support import require_loopback

ROOT = Path(__file__).resolve().parents[1]


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class RestartKeepsOpenProjectTests(unittest.TestCase):
    def setUp(self):
        require_loopback()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name).resolve()
        self.home = base / "home"
        code = base / "studio"
        code.mkdir()
        self.entry = registry.register(self.home, "studio", code)
        self.context = registry.context_for_entry(self.entry)
        self.managers: list[subprocess.Popen] = []
        self.addCleanup(self._stop_all)

    def _stop_all(self):
        for process in self.managers:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=10)

    def pause(self):
        board.begin_project_pause(self.context, drain_seconds=0)
        board.finish_project_pause(self.context)

    def start_manager(self) -> tuple[subprocess.Popen, int]:
        port = _free_port()
        process = subprocess.Popen(
            [sys.executable, "-m", "harness.project_manager", "--home", str(self.home),
             "--port", str(port), "--board-port", str(_free_port())],
            cwd=ROOT, env={**os.environ, "PYTHONPATH": str(ROOT), "PYTHONDONTWRITEBYTECODE": "1"},
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        self.managers.append(process)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                with urlopen(f"http://127.0.0.1:{port}/api/projects", timeout=2):
                    return process, port
            except OSError:
                time.sleep(0.2)
        self.fail("the manager did not start")

    def studio_row(self, port: int) -> dict:
        with urlopen(f"http://127.0.0.1:{port}/api/projects", timeout=5) as response:
            rows = json.loads(response.read())["projects"]
        return next(row for row in rows if row["id"] == self.entry["id"])

    def refresh(self, process: subprocess.Popen) -> None:
        """What start_project_manager.sh does on REFRESH: kill, wait, start."""
        process.send_signal(signal.SIGTERM)
        process.wait(timeout=30)

    def test_open_paused_project_is_still_open_and_paused_after_an_update(self):
        self.pause()
        pause_before = board.pause_state(self.context)
        old, old_port = self.start_manager()
        registry.activate(self.home, self.entry["id"], pid=old.pid)   # what Open records
        self.assertTrue(self.studio_row(old_port)["active"])
        self.refresh(old)
        new, new_port = self.start_manager()
        row = self.studio_row(new_port)
        self.assertTrue(row["active"], "an app update closed the owner's open project")
        self.assertTrue(row["paused"])
        self.assertEqual(board.pause_state(self.context), pause_before, "the pause must be kept exactly")
        self.assertEqual(registry.active_project(self.home)["pid"], new.pid)
        # And again: the new manager keeps it through the next update too.
        self.refresh(new)
        third, third_port = self.start_manager()
        self.assertTrue(self.studio_row(third_port)["active"])
        audit = (self.home / "registry-audit.log").read_text(encoding="utf-8")
        self.assertNotIn(f"deactivated project {self.entry['id']}", audit)
        self.assertIn("still open after the app restart", audit)

    def test_an_open_project_that_is_not_paused_is_released_as_before(self):
        old, _port = self.start_manager()
        registry.activate(self.home, self.entry["id"], pid=old.pid)
        self.refresh(old)
        _new, new_port = self.start_manager()
        self.assertFalse(self.studio_row(new_port)["active"])
        self.assertIsNone(registry.active_project(self.home))


class OwnerCloseTests(unittest.TestCase):
    def test_the_owner_closing_a_paused_project_still_closes_it(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / "home"
            code = Path(temporary) / "studio"
            code.mkdir()
            entry = registry.register(home, "studio", code)
            context = registry.context_for_entry(entry)
            board.begin_project_pause(context, drain_seconds=0)
            board.finish_project_pause(context)
            manager = project_manager.ProjectManager(home, board_port=0)
            registry.activate(home, entry["id"])
            manager.close_project(entry["id"])
            self.assertIsNone(registry.active_project(home))
            self.assertIsNone(registry.adopt_held(home))

    def test_a_kept_project_that_was_removed_is_not_reopened(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / "home"
            code = Path(temporary) / "studio"
            code.mkdir()
            entry = registry.register(home, "studio", code)
            dead = subprocess.Popen([sys.executable, "-c", "pass"])
            dead.wait()
            registry.activate(home, entry["id"], pid=dead.pid)
            self.assertTrue(registry.hold_for_restart(home, entry["id"]))
            registry.remove(home, entry["id"])
            self.assertIsNone(registry.adopt_held(home))


if __name__ == "__main__":
    unittest.main()


class ConcurrentResumeTests(unittest.TestCase):
    """2026-09-28: two Resume clicks at once orphaned the first board worker.

    After an update the owner's project is open with no worker. Two resumes ran
    together: the second released the first's activation and started a second
    worker, overwriting the manager's only handle to the first, which stayed on
    the board port. Every later Resume then failed with "Address already in use".
    """

    def test_two_resumes_at_once_start_exactly_one_worker_and_orphan_nothing(self):
        import threading
        from unittest import mock
        require_loopback()
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary).resolve()
            home = base / "home"
            code = base / "studio"
            code.mkdir()
            entry = registry.register(home, "studio", code)
            context = registry.context_for_entry(entry)
            board.begin_project_pause(context, drain_seconds=0)
            board.finish_project_pause(context)
            # The state an update leaves: kept open, adopted, no worker.
            dead = subprocess.Popen([sys.executable, "-c", "pass"]); dead.wait()
            registry.activate(home, entry["id"], pid=dead.pid)
            registry.hold_for_restart(home, entry["id"])
            board_port = _free_port()
            manager = project_manager.ProjectManager(home, board_port=board_port, worker_start_timeout=10)
            self.assertTrue(manager.keep_held_project_open())

            spawned = []
            real_popen = project_manager.subprocess.Popen
            def recording_popen(*args, **kwargs):
                process = real_popen(*args, **kwargs)
                spawned.append(process)
                return process
            real_argv = project_manager.ProjectManager.worker_argv
            def slow_argv(self, *args, **kwargs):
                time.sleep(0.5)          # widen the window between activation and the stored handle
                return real_argv(self, *args, **kwargs)

            barrier = threading.Barrier(2)
            errors, results = [], []
            def resume():
                barrier.wait()
                try:
                    results.append(manager.resume_project(entry["id"]))
                except Exception as error:   # noqa: BLE001 - recorded for the assertion
                    errors.append(repr(error))
            try:
                with mock.patch.object(project_manager.subprocess, "Popen", recording_popen), \
                        mock.patch.object(project_manager.ProjectManager, "worker_argv", slow_argv):
                    threads = [threading.Thread(target=resume) for _ in range(2)]
                    for thread in threads: thread.start()
                    for thread in threads: thread.join(timeout=60)
                workers = [p for p in spawned if "project_worker" in " ".join(map(str, p.args))]
                self.assertEqual(errors, [], "a concurrent Resume failed")
                self.assertEqual(len(workers), 1, f"{len(workers)} board workers were started")
                self.assertIs(manager.worker, workers[0])
                self.assertIsNone(workers[0].poll())
                with urlopen(f"http://127.0.0.1:{board_port}/api/ready", timeout=5) as response:
                    self.assertTrue(json.loads(response.read())["ready"])
                self.assertEqual(board.pause_state(context)["status"], "active")
            finally:
                manager.close_project(entry["id"])
                for process in spawned:
                    if process.poll() is None:
                        process.kill(); process.wait(timeout=10)
            # Nothing is left holding the board port: a new worker can bind it
            # (with address reuse, as the worker's HTTP server does).
            with socket.socket() as probe:
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                probe.bind(("127.0.0.1", board_port))
