# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Owner 2026-10-28: only one project is loaded at a time. Listing the Projects page reads the board of the OPEN project only;
every other card is built from the registry (name, folder, health) and says it is not open. No closed project's files are read."""
from __future__ import annotations

import builtins
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from harness import project_manager, project_registry as registry


class ProjectsListLoadsOneTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)
        self.home = self.base / "home"
        self.entries = []
        for index in range(9):
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
            self.entries.append(entry)

    def _reads_under(self, roots, action):
        seen = []
        real_open, real_read_text = builtins.open, Path.read_text

        def watching_open(file, *args, **kwargs):
            if any(str(file).startswith(root) for root in roots):
                seen.append(str(file))
            return real_open(file, *args, **kwargs)

        def watching_read_text(path, *args, **kwargs):
            if any(str(path).startswith(root) for root in roots):
                seen.append(str(path))
            return real_read_text(path, *args, **kwargs)

        with mock.patch.object(builtins, "open", watching_open), mock.patch.object(Path, "read_text", watching_read_text):
            value = action()
        return value, seen

    def test_listing_reads_only_the_open_projects_board(self):
        opened = self.entries[3]
        registry.activate(self.home, opened["id"])
        manager = project_manager.ProjectManager(self.home, board_port=0)
        closed_roots = [str(Path(entry["code_root"]).resolve()) for entry in self.entries if entry is not opened]
        closed_roots += [str(Path(entry["code_root"])) for entry in self.entries if entry is not opened]
        payload, seen = self._reads_under(closed_roots, manager.projects_payload)
        self.assertEqual(seen, [], "no file of a project that is not open may be read when the list is built")
        rows = {row["id"]: row for row in payload["projects"]}
        self.assertEqual(rows[opened["id"]]["latest_progress"], "Working on project 3.")
        self.assertTrue(rows[opened["id"]]["board_loaded"])
        for entry in self.entries:
            if entry is opened:
                continue
            row = rows[entry["id"]]
            self.assertFalse(row["board_loaded"])
            self.assertEqual(row["latest_progress"], project_manager.NOT_OPEN_NOW)
            self.assertEqual(row["project_now"]["now"], project_manager.NOT_OPEN_NOW)
            self.assertTrue(row["health"]["ok"], "the folder exists, so the card is healthy without reading the board")
            self.assertEqual(row["task_counts"]["total"], 0)

    def test_with_nothing_open_no_board_is_read_at_all(self):
        manager = project_manager.ProjectManager(self.home, board_port=0)
        roots = [str(Path(entry["code_root"])) for entry in self.entries] + [str(Path(entry["code_root"]).resolve()) for entry in self.entries]
        payload, seen = self._reads_under(roots, manager.projects_payload)
        self.assertEqual(seen, [])
        self.assertEqual(len(payload["projects"]), 9)
        self.assertTrue(all(row["latest_progress"] == project_manager.NOT_OPEN_NOW for row in payload["projects"]))

    def test_the_spy_would_catch_a_board_read(self):
        """The check above means something: reading a board for a closed project is seen by the spy."""
        entry = self.entries[0]
        roots = [str(Path(entry["code_root"])), str(Path(entry["code_root"]).resolve())]
        _, seen = self._reads_under(roots, lambda: project_manager.derive_status(entry))
        self.assertTrue(seen)


if __name__ == "__main__":
    unittest.main()
