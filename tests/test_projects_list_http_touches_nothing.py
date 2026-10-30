# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Owner 2026-10-28: only one project is loaded at a time. Codex's review of the start-up fix proved that the Projects page's own data
request (GET /api/projects, on load and every few seconds) still touched every closed project and reset its test machine authorization.

This is the request the page really makes, over real HTTP: the list of projects must leave every closed project exactly as it was:
no file under its folders read, no byte changed, and its test machine authorization untouched."""
from __future__ import annotations

import builtins
import hashlib
import json
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock
from urllib.request import urlopen

from tests.environment_support import require_loopback
from harness import global_settings, project_manager, project_registry as registry, test_machine
from tests.test_test_machine import connected_descriptor


def snapshot(root: Path) -> dict[str, tuple[int, int, str]]:
    out = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            data = path.read_bytes()
            out[str(path.relative_to(root))] = (len(data), path.stat().st_mtime_ns, hashlib.sha256(data).hexdigest())
    return out


class ProjectsListOverHttpTests(unittest.TestCase):
    def setUp(self):
        require_loopback()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)
        self.home = self.base / "home"
        self.entries = []
        previous_manager = {**test_machine.manager_identity(), "instance_id": "a-manager-that-has-since-restarted"}
        for index in range(5):
            code = self.base / f"project-{index}"
            code.mkdir()
            entry = registry.register(self.home, f"Project {index}", code, kind="scaffold")
            board = Path(entry["data_root"]) / "board"
            board.mkdir(parents=True)
            (board / "state.json").write_text(json.dumps({
                "task_owner_directions": {"T1": "one"},
                "agents": {"a": {"active": True, "task": "T1", "role": "engineering"}},
                "task_briefs": {"T1": {"update": f"Working on project {index}.", "updated_at": "2026-10-28T10:00:00+00:00"}},
                "events": [{"kind": "status_update", "at": "2026-10-28T10:00:00+00:00"}],
            }), encoding="utf-8")
            # a connected test machine authorized by a manager instance that is not this one: a restart check would reset it
            connected_descriptor(registry.context_for_entry(entry), identity=previous_manager)
            self.entries.append(entry)
        self.opened = self.entries[2]
        registry.activate(self.home, self.opened["id"])
        global_settings.initialize(self.home)   # the app's own settings file exists long before any restart

    def get_list(self):
        manager = project_manager.ProjectManager(self.home, board_port=0)
        server = ThreadingHTTPServer(("127.0.0.1", 0), project_manager.make_handler(manager))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = f"http://127.0.0.1:{server.server_address[1]}"
            with urlopen(url + "/", timeout=10) as page:
                page.read()
            replies = []
            for _ in range(3):   # the page asks again every few seconds
                with urlopen(url + "/api/projects", timeout=10) as reply:
                    replies.append(json.loads(reply.read().decode()))
            return replies
        finally:
            server.shutdown()
            thread.join(timeout=3)
            server.server_close()

    def test_the_pages_own_list_request_leaves_every_closed_project_untouched(self):
        closed = [entry for entry in self.entries if entry is not self.opened]
        before = {entry["id"]: snapshot(Path(entry["code_root"])) for entry in closed}
        epochs = {entry["id"]: test_machine.read(registry.context_for_entry(entry)) for entry in closed}
        roots = [str(Path(entry["code_root"])) for entry in closed] + [str(Path(entry["code_root"]).resolve()) for entry in closed]
        touched = []
        real_open, real_read_text, real_write_text = builtins.open, Path.read_text, Path.write_text

        def watching_open(file, *args, **kwargs):
            if any(str(file).startswith(root) for root in roots):
                touched.append(str(file))
            return real_open(file, *args, **kwargs)

        def watching_read_text(path, *args, **kwargs):
            if any(str(path).startswith(root) for root in roots):
                touched.append(str(path))
            return real_read_text(path, *args, **kwargs)

        def watching_write_text(path, *args, **kwargs):
            if any(str(path).startswith(root) for root in roots):
                touched.append(str(path))
            return real_write_text(path, *args, **kwargs)

        with mock.patch.object(builtins, "open", watching_open), mock.patch.object(Path, "read_text", watching_read_text), \
                mock.patch.object(Path, "write_text", watching_write_text):
            replies = self.get_list()
        self.assertEqual(touched, [], "the list must not read or write any file of a project that is not open")
        for entry in closed:
            self.assertEqual(snapshot(Path(entry["code_root"])), before[entry["id"]], "not one byte or time of a closed project changes")
            now = test_machine.read(registry.context_for_entry(entry))
            self.assertFalse(now["reauth_required"], "listing must not reset a closed project's test machine authorization")
            self.assertEqual(now, epochs[entry["id"]])
        for reply in replies:
            rows = {row["id"]: row for row in reply["projects"]}
            self.assertEqual(rows[self.opened["id"]]["latest_progress"], "Working on project 2.", "the open project keeps its live status")
            for entry in closed:
                self.assertEqual(rows[entry["id"]]["latest_progress"], project_manager.NOT_OPEN_NOW)
                self.assertEqual(rows[entry["id"]]["test_machine"], {"state": "unknown"})

    def test_opening_one_closed_projects_machine_window_touches_only_that_project(self):
        """The Test machine window of a closed project asks for THAT project's saved target (an explicit machine operation)."""
        asked, other = self.entries[0], self.entries[1]
        manager = project_manager.ProjectManager(self.home, board_port=0)
        before_other = test_machine.read(registry.context_for_entry(other))
        status = manager.machine_status(asked["id"])
        self.assertEqual((status["address"], status["user"], status["port"]), ("127.0.0.1", "u", 22), "the saved target comes back")
        self.assertEqual(test_machine.read(registry.context_for_entry(other)), before_other, "no other project's machine is touched")
        self.assertFalse(test_machine.read(registry.context_for_entry(other))["reauth_required"])

    def test_the_spy_would_notice_a_reset(self):
        """The check above means something: a restart check on a closed project does change its machine authorization."""
        entry = self.entries[0]
        context = registry.context_for_entry(entry)
        self.assertTrue(test_machine.reconcile_on_start(context, test_machine.manager_identity()))
        self.assertTrue(test_machine.read(context)["reauth_required"])


if __name__ == "__main__":
    unittest.main()
