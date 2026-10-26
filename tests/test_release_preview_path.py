# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""The owner's View app must find npm/node: the launchd-started worker has only the system folders on PATH.

Run: PYTHONPATH=. python3 -m unittest tests.test_release_preview_path -v
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from urllib.request import urlopen

from harness import board, release_preview
from tests.environment_support import require_loopback
from tests.test_release_preview import PreviewFixture


class PreviewEnvironmentTests(unittest.TestCase):
    def setUp(self):
        self.tools = tempfile.TemporaryDirectory()
        self.addCleanup(self.tools.cleanup)
        self.discovery = release_preview.platform_support.discovery()

    def widened(self, path, extra=None):
        with mock.patch.object(self.discovery, "owner_tool_directories", return_value=(self.tools.name, "/does/not/exist")):
            return release_preview.preview_environment({"PATH": path, "HOME": "/h"}, extra)

    def test_the_tool_folders_are_added_after_the_workers_own_and_nothing_is_lost(self):
        values = self.widened("/usr/bin:/bin:/usr/sbin:/sbin")
        parts = values["PATH"].split(os.pathsep)
        self.assertEqual(parts[:4], ["/usr/bin", "/bin", "/usr/sbin", "/sbin"])
        self.assertIn(self.tools.name, parts)
        self.assertNotIn("/does/not/exist", parts)
        self.assertEqual(len(parts), len(set(parts)), "no repeats")
        self.assertEqual(values["HOME"], "/h")

    def test_port_and_browser_are_added_only_when_asked(self):
        self.assertNotIn("PORT", self.widened("/usr/bin"))
        asked = self.widened("/usr/bin", {"PORT": "5", "BROWSER": "true"})
        self.assertEqual((asked["PORT"], asked["BROWSER"]), ("5", "true"))

    def test_an_empty_path_still_gets_the_tool_folders(self):
        self.assertIn(self.tools.name, self.widened("")["PATH"].split(os.pathsep))


class OwnerCommandFindsItsToolsTests(PreviewFixture):
    """The real launch: a tool that lives in an owner tool folder, a worker with the bare system PATH."""

    def test_a_preview_command_whose_program_is_in_an_owner_tool_folder_starts(self):
        require_loopback()
        tools = Path(self.temporary.name) / "tools"
        tools.mkdir()
        program = tools / "fakenpm"
        program.write_text('#!/bin/sh\nexec python3 serve.py --port "$1"\n')
        program.chmod(0o755)
        self.seed_release()
        self.configure_command("fakenpm {port}")
        discovery = release_preview.platform_support.discovery()
        with mock.patch.dict(os.environ, {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin"}), \
                mock.patch.object(discovery, "owner_tool_directories", return_value=(str(tools),)):
            self.supervisor.tick()
            preview = board.snapshot(self.root)["releases"]["TASK"]["preview"]
            self.assertEqual(preview["status"], "ready", preview)
            self.assertEqual(urlopen(preview["url"], timeout=10).read(), b"candidate-one")

    def test_a_program_that_exists_nowhere_still_fails_with_the_shells_message(self):
        require_loopback()
        self.seed_release()
        self.configure_command("definitely-not-installed-xyz {port}")
        self.supervisor.tick()
        preview = board.snapshot(self.root)["releases"]["TASK"]["preview"]
        self.assertEqual(preview["status"], "failed")
        self.assertIn("definitely-not-installed-xyz", preview.get("log_tail", "") + preview.get("command", ""))
        self.assertIn("not found", preview.get("log_tail", ""))


from tests.release_fixture_support import allow_fixture_releases  # noqa: E402

setUpModule, tearDownModule = allow_fixture_releases()   # bare release fixtures: production demands a final review (2026-10-24)


if __name__ == "__main__":
    unittest.main()
