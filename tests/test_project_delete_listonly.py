# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Delete on a project card is list-only (owner, 2026-10-28): the project leaves the list, an open one is closed first,
and no file or folder is ever deleted. Real HTTP path, throwaway home and folders.

Run:  PYTHONPATH=. python3 -m unittest tests.test_project_delete_listonly -v
"""
from __future__ import annotations

import inspect
import json
import shutil
import socket
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from harness import project_manager, project_registry as registry
from tests.environment_support import require_loopback


def tree(root: Path) -> dict[str, str]:
    return {str(path.relative_to(root)): (path.read_text() if path.is_file() else "<dir>") for path in sorted(root.rglob("*"))}


class DeleteListOnlyTests(unittest.TestCase):
    def setUp(self):
        require_loopback()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)
        self.home = self.base / "home"

    def project(self, name: str) -> dict:
        code = self.base / name
        (code / "src").mkdir(parents=True)
        (code / "src" / "app.py").write_text("print('real source')\n")
        (code / "family-photos.txt").write_text("irreplaceable")
        entry = registry.register(self.home, name, code)
        for key in ("data_root", "workspace_root"):
            folder = Path(entry[key])
            folder.mkdir(parents=True, exist_ok=True)
            (folder / "note.txt").write_text(f"{name} {key}")
        return entry

    def folders(self, entry: dict) -> dict[str, dict[str, str]]:
        return {key: tree(Path(entry[key])) for key in ("code_root", "data_root", "workspace_root")}

    def assert_nothing_deleted(self, entry: dict, before: dict[str, dict[str, str]]) -> None:
        """Every file and folder that was there is still there with the same words. (The app's own bookkeeping files may be
        rewritten while a project is read or run; that is not Delete, so only what existed before is compared.)"""
        after = self.folders(entry)
        for key, files in before.items():
            for name, content in files.items():
                self.assertIn(name, after[key], f"{key}/{name} was deleted")
                if Path(name).name in ("app.py", "family-photos.txt", "note.txt"):
                    self.assertEqual(after[key][name], content, f"{key}/{name} was changed")

    def serve(self, manager):
        server = ThreadingHTTPServer(("127.0.0.1", 0), project_manager.make_handler(manager))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def stop():
            server.shutdown()
            thread.join(timeout=3)
            server.server_close()
        self.addCleanup(stop)
        return f"http://127.0.0.1:{server.server_address[1]}"

    @staticmethod
    def delete(base: str, project_id: str):
        try:
            with urlopen(Request(f"{base}/api/projects/{project_id}", method="DELETE"), timeout=10) as response:
                return response.status, json.loads(response.read())
        except HTTPError as error:
            return error.code, json.loads(error.read())

    def test_deleting_a_closed_project_leaves_every_file_and_folder(self):
        entry = self.project("closed-one")
        other = self.project("other-one")
        before, other_before = self.folders(entry), self.folders(other)
        manager = project_manager.ProjectManager(self.home, board_port=0)
        with patch.object(shutil, "rmtree", side_effect=AssertionError("Delete must never remove a folder")):
            status, _ = self.delete(self.serve(manager), entry["id"])
        self.assertEqual(status, 200)
        self.assertEqual([item["name"] for item in registry.entries(self.home)], ["other-one"])
        self.assert_nothing_deleted(entry, before)
        self.assert_nothing_deleted(other, other_before)

    def test_deleting_the_open_project_closes_it_first_then_removes_it(self):
        entry = self.project("open-one")
        before = self.folders(entry)
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()
        manager = project_manager.ProjectManager(self.home, board_port=port, worker_start_timeout=20)
        opened = manager.open_project(entry["id"])
        with urlopen(opened["board_url"] + "api/ready", timeout=5) as response:
            self.assertTrue(json.loads(response.read())["ready"])
        worker = manager.worker
        self.assertIsNone(worker.poll())
        self.addCleanup(lambda: manager.worker_project and manager.close_project(entry["id"]))
        status, _ = self.delete(self.serve(manager), entry["id"])
        self.assertEqual(status, 200)
        self.assertIsNotNone(worker.poll(), "the board worker was stopped")
        self.assertIsNone(registry.active_project(self.home), "nothing is left open")
        self.assertEqual(registry.entries(self.home), [])
        with socket.socket() as probe:
            probe.settimeout(1)
            self.assertNotEqual(probe.connect_ex(("127.0.0.1", port)), 0, "nothing is left serving the deleted project")
        self.assert_nothing_deleted(entry, before)

    def test_a_wrong_id_deletes_nothing_and_leaves_the_open_project_open(self):
        entry = self.project("stays-open")
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()
        manager = project_manager.ProjectManager(self.home, board_port=port, worker_start_timeout=20)
        manager.open_project(entry["id"])
        self.addCleanup(lambda: manager.worker_project and manager.close_project(entry["id"]))
        status, value = self.delete(self.serve(manager), "no-such-id")
        self.assertEqual(status, 400)
        self.assertIn("no project with id", value["error"])
        self.assertEqual(registry.active_project(self.home)["project_id"], entry["id"])
        self.assertIsNone(manager.worker.poll(), "the open project's board is still running")
        self.assertEqual([item["name"] for item in registry.entries(self.home)], ["stays-open"])

    def test_a_second_delete_says_plainly_it_is_gone(self):
        entry = self.project("twice")
        base = self.serve(project_manager.ProjectManager(self.home, board_port=0))
        self.assertEqual(self.delete(base, entry["id"])[0], 200)
        status, value = self.delete(base, entry["id"])
        self.assertEqual(status, 400)
        self.assertIn("no project with id", value["error"])

    def test_the_delete_path_contains_no_file_or_folder_removal(self):
        source = inspect.getsource(project_manager.ProjectManager.delete_project) + inspect.getsource(registry.remove)
        for forbidden in ("rmtree", "unlink", "rmdir", "os.remove", "os.removedirs", "trash"):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
