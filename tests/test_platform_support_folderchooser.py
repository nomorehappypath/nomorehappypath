# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
"""The folder picker behind the seam — including one wart kept on purpose.

A cancel and a genuine failure both return "". The page cannot tell them apart.
That is wrong for the product and correct for Stage 0: changing it here would
change what the page does, so it is pinned as a test and recorded as a defect
rather than quietly fixed under cover of a refactor.
"""
from __future__ import annotations

import collections
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

from harness import platform_support, project_manager

Uname = collections.namedtuple("Uname", "sysname")


def completed(returncode: int, stdout: str) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(["/usr/bin/osascript"], returncode, stdout, "")


class FolderChooserTests(unittest.TestCase):
    """The macOS implementation, pinned deterministically on ANY host.

    These tests exercise the Darwin path, so they force Darwin rather than
    inheriting whatever machine they happen to run on. Without that they pass on
    a Mac and ERROR on Linux — where the chooser correctly refuses — which is a
    test that reports the host, not the behaviour. The Linux verification run
    caught exactly that.
    """

    def setUp(self):
        self.chooser = platform_support.folder_chooser()
        darwin = mock.patch.object(os, "uname", return_value=Uname(sysname="Darwin"))
        darwin.start()
        self.addCleanup(darwin.stop)

    def test_a_chosen_folder_comes_back_absolute(self):
        with mock.patch.object(subprocess, "run", return_value=completed(0, "/tmp/../tmp/x\n")):
            self.assertEqual(self.chooser.choose("Pick"), str(Path("/tmp/x").resolve()))

    def test_a_cancel_returns_empty_and_so_does_a_failure(self):
        """Both branches, pinned together, because that IS the wart."""
        with mock.patch.object(subprocess, "run", return_value=completed(1, "")):
            self.assertEqual(self.chooser.choose("Pick"), "", "a cancel returns empty")
        with mock.patch.object(subprocess, "run", return_value=completed(0, "   \n")):
            self.assertEqual(self.chooser.choose("Pick"), "", "an empty answer returns empty")

    def test_the_timeout_is_still_120_seconds_and_is_named_not_raw(self):
        recorded = {}

        def capture(*args, **kwargs):
            recorded.update(kwargs)
            raise subprocess.TimeoutExpired(["/usr/bin/osascript"], 120)

        with mock.patch.object(subprocess, "run", side_effect=capture):
            with self.assertRaises(platform_support.FolderSelectionTimeout):
                self.chooser.choose("Pick")
        self.assertEqual(recorded.get("timeout"), 120,
                         "the owner-facing wait must not change length under a refactor")

    def test_off_macos_it_refuses_by_name_rather_than_failing_to_find_osascript(self):
        """The refusal lives in the macOS implementation, not the selector.

        Stage 0 selects this module on every platform, so a refusal placed only
        in the selector would leave Linux hitting a missing /usr/bin/osascript
        and raising FileNotFoundError instead of a named refusal.
        """
        with mock.patch.object(os, "uname", return_value=Uname(sysname="Linux")):
            with self.assertRaises(platform_support.UnsupportedPlatformOperation):
                self.chooser.choose("Pick")


class ApplicationSideTests(unittest.TestCase):
    """What must NOT move into the platform layer."""

    def setUp(self):
        darwin = mock.patch.object(os, "uname", return_value=Uname(sysname="Darwin"))
        darwin.start()
        self.addCleanup(darwin.stop)

    def test_the_closed_set_check_runs_before_any_applescript_is_built(self):
        """It is the only thing keeping arbitrary text out of the script."""
        with mock.patch.object(subprocess, "run") as never:
            with self.assertRaises(ValueError):
                project_manager.choose_folder('"; do shell script "rm -rf /"; --')
        never.assert_not_called()

    def test_every_known_purpose_still_has_owner_facing_copy(self):
        for purpose, prompt in project_manager.FOLDER_PROMPTS.items():
            self.assertTrue(prompt.strip(), f"{purpose} lost its prompt copy")

    def test_the_timeout_reaches_the_owner_in_the_same_words_as_before(self):
        with mock.patch.object(
            subprocess, "run", side_effect=subprocess.TimeoutExpired(["/usr/bin/osascript"], 120)
        ):
            with self.assertRaisesRegex(ValueError, "folder selection timed out; try again"):
                project_manager.choose_folder("new-parent")

    def test_off_macos_the_owner_still_sees_the_macos_only_message(self):
        with mock.patch.object(os, "uname", return_value=Uname(sysname="Linux")):
            with self.assertRaisesRegex(ValueError, "available on macOS only"):
                project_manager.choose_folder("new-parent")


class ScriptModeTests(unittest.TestCase):
    """project_manager.py is ALSO run as a script, not only imported.

    Run that way, `harness` is not importable until the bootstrap at the top of
    the module puts the repository root on sys.path. An import of the seam
    placed above that line raises ModuleNotFoundError on launch — the module
    imports perfectly under the test suite and the app will not start.
    """

    def test_it_still_imports_when_run_as_a_script(self):
        root = Path(__file__).resolve().parent.parent
        result = subprocess.run(
            [sys.executable, str(root / "harness" / "project_manager.py"), "--nonsense-flag"],
            capture_output=True, text=True, timeout=60,
            cwd=root / "harness",  # the launcher's own working directory
        )
        self.assertNotIn("ModuleNotFoundError", result.stderr,
                         "a harness import was placed above the sys.path bootstrap")

    def test_every_harness_import_sits_below_the_bootstrap(self):
        source = (Path(__file__).resolve().parent.parent / "harness" / "project_manager.py")
        lines = source.read_text(encoding="utf-8").splitlines()
        bootstrap = next(i for i, line in enumerate(lines) if "sys.path.insert" in line)
        early = [
            f"{number + 1}: {line}"
            for number, line in enumerate(lines[:bootstrap])
            if line.startswith("from harness") or line.startswith("import harness")
        ]
        self.assertEqual(early, [], f"these run before `harness` is importable: {early}")


if __name__ == "__main__":
    unittest.main()
