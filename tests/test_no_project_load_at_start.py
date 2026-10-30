# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""App start touches no project (owner 2026-10-28): projects load when opened or resumed, one at a time, never at a restart.

The Mission Control restart on 2026-10-28 left the app down for minutes: the manager's constructor initialized the memory of EVERY
registered project, ran the legacy settings cleanup in every project's control folder and reconciled every project's test machine
before it could answer its first request.

Run:  PYTHONPATH=. python3 -m unittest tests.test_no_project_load_at_start -v
"""
from __future__ import annotations

import builtins
import os
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.request import urlopen

from harness import global_settings, project_manager, project_memory, project_registry as registry, test_machine
from tests.environment_support import require_loopback


def _tree(root: Path) -> dict[str, tuple[int, int]]:
    """Every path under a folder with its size and modification time: any create, write or touch changes it."""
    found = {}
    for current, directories, files in os.walk(root):
        for name in directories + files:
            path = os.path.join(current, name)
            stat = os.stat(path, follow_symlinks=False)
            found[path] = (stat.st_size, stat.st_mtime_ns)
    return found


class AppStartLoadsNoProject(unittest.TestCase):
    def setUp(self):
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        base = Path(self._directory.name).resolve()
        self.home = base / "home"
        self.home.mkdir()
        self.entries = []
        for number in range(4):
            code = base / f"project-{number}"
            code.mkdir()
            (code / "app.py").write_text("print('hello')\n", encoding="utf-8")
            self.entries.append(registry.register(self.home, f"Project {number}", code, kind="scaffold"))
        # global settings exist, as on every machine that has run the app before
        global_settings.initialize(self.home, [])
        self.roots = [Path(entry[key]) for entry in self.entries for key in ("code_root", "data_root", "workspace_root")]

    def _snapshot(self):
        return {str(root): _tree(root) for root in self.roots if root.exists()}

    def test_constructing_the_manager_touches_no_project(self):
        before = self._snapshot()
        spies = {"memory": [], "settings": [], "machine": [], "reservation": []}
        real_initialize = global_settings.initialize
        with patch.object(project_memory, "initialize", side_effect=lambda *a, **k: spies["memory"].append(a) or {}), \
                patch.object(global_settings, "initialize",
                             side_effect=lambda home, roots=None: spies["settings"].append(list(roots or [])) or real_initialize(home, roots)), \
                patch.object(test_machine, "reconcile_on_start", side_effect=lambda *a, **k: spies["machine"].append(a) or False), \
                patch.object(test_machine, "reconcile_reservation", side_effect=lambda *a, **k: spies["reservation"].append(a)):
            project_manager.ProjectManager(self.home)
        self.assertEqual(spies["memory"], [], "no project memory is initialized at app start")
        self.assertEqual(spies["machine"], [], "no project's test machine is reconciled at app start")
        self.assertEqual(spies["reservation"], [])
        self.assertEqual(spies["settings"], [[]], "global settings are checked once and look at no project's folder")
        self.assertEqual(self._snapshot(), before, "app start created, wrote or touched something inside a project")

    def test_the_first_page_request_reads_and_writes_no_project_file(self):
        require_loopback()
        opened = []
        real_open = builtins.open
        project_roots = tuple(str(root) for root in self.roots)

        def spy(file, *args, **kwargs):
            if isinstance(file, (str, os.PathLike)) and str(file).startswith(project_roots):
                opened.append(str(file))
            return real_open(file, *args, **kwargs)

        before = self._snapshot()
        with patch.object(builtins, "open", spy):
            manager = project_manager.ProjectManager(self.home)
            server = ThreadingHTTPServer(("127.0.0.1", 0), project_manager.make_handler(manager))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                page = urlopen(f"http://127.0.0.1:{server.server_address[1]}/", timeout=10).read().decode("utf-8")
            finally:
                server.shutdown()
                thread.join(timeout=3)
                server.server_close()
        self.assertIn("NoMoreHappyPath", page)
        self.assertEqual(opened, [], "app start plus the first page opened a project file")
        self.assertEqual(self._snapshot(), before)

    def test_a_first_ever_start_may_still_migrate_an_older_project_copy_once(self):
        (self.home / "settings.json").unlink()
        seen = []
        real_initialize = global_settings.initialize
        with patch.object(global_settings, "initialize",
                          side_effect=lambda home, roots=None: seen.append(len(roots or [])) or real_initialize(home, roots)):
            project_manager.ProjectManager(self.home)
        self.assertEqual(seen, [len(self.entries)], "only the very first creation of the global store looks at the projects")

    def test_opening_one_project_initializes_only_that_project(self):
        manager = project_manager.ProjectManager(self.home)
        target, others = self.entries[1], [entry for index, entry in enumerate(self.entries) if index != 1]

        def others_tree():
            return {entry["id"]: [_tree(Path(entry[key])) if Path(entry[key]).exists() else {}
                                  for key in ("code_root", "data_root", "workspace_root")] for entry in others}

        before = others_tree()
        memory, machine = [], []
        real_initialize = project_memory.initialize
        with patch.object(project_memory, "initialize",
                          side_effect=lambda root, **k: memory.append(str(root.code_root)) or real_initialize(root, **k)), \
                patch.object(test_machine, "reconcile_on_start", side_effect=lambda root, ident: machine.append(str(root.code_root)) or False), \
                patch.object(project_manager.subprocess, "Popen", return_value=MagicMock(pid=4242, poll=lambda: None)), \
                patch.object(project_manager.ProjectManager, "_wait_for_worker", lambda *a, **k: None), \
                patch.object(project_manager.ProjectManager, "_stop_worker", lambda *a, **k: None), \
                patch.object(project_manager.workspace_settings, "apply_provider_files", return_value={}):
            manager.open_project(target["id"])
        self.assertEqual(set(memory), {str(Path(target["code_root"]))}, "opening a project initializes that project's memory only")
        self.assertEqual(machine, [str(Path(target["code_root"]))])
        self.assertEqual(others_tree(), before, "opening one project touched another project's folders")


if __name__ == "__main__":
    unittest.main()
