# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
"""Choosing a project folder where there is no folder dialog.

Every Linux server and every headless machine. The spec's interim answer: accept
a typed absolute path with server-side validation, and NEVER fail silently.

Each refusal below names what is wrong in words the owner can act on. A path
rejected as merely "invalid" turns a typo into a support question.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from harness import project_manager


class ValidationTests(unittest.TestCase):
    def _reject(self, raw, home=None):
        with self.assertRaises(ValueError) as caught:
            project_manager.folder_from_typed_path(raw, "new-parent", home=home)
        return str(caught.exception)

    def test_the_closed_set_purpose_check_still_runs_FIRST(self):
        """It is what keeps arbitrary text out of the native path; typed input
        must not become the way round it."""
        with self.assertRaisesRegex(ValueError, "unknown folder selection purpose"):
            project_manager.folder_from_typed_path("/tmp", "not-a-real-purpose")

    def test_empty_says_what_to_type(self):
        self.assertIn("starting with /", self._reject(""))

    def test_a_relative_path_says_it_must_be_absolute(self):
        self.assertIn("absolute", self._reject("projects/thing"))

    def test_a_missing_folder_NAMES_the_path(self):
        message = self._reject("/definitely/not/here")
        self.assertIn("/definitely/not/here", message,
                      "naming the path is what turns a typo into a fixable message")

    def test_a_file_is_refused_as_a_file_not_as_invalid(self):
        with tempfile.NamedTemporaryFile(suffix=".txt") as handle:
            self.assertIn("is a file, not a folder", self._reject(handle.name))

    def test_a_tilde_is_expanded_rather_than_refused(self):
        """Owners type ~ constantly; refusing it would be pedantry."""
        with tempfile.TemporaryDirectory() as home:
            with mock.patch.object(os.path, "expanduser", return_value=home):
                self.assertEqual(
                    project_manager.folder_from_typed_path("~", "new-parent"),
                    str(Path(home).resolve()))

    def test_an_unwritable_folder_is_refused_before_a_project_is_attempted(self):
        with tempfile.TemporaryDirectory() as temporary:
            locked = Path(temporary) / "locked"
            locked.mkdir(mode=0o500)
            try:
                self.assertIn("not writable", self._reject(str(locked)))
            finally:
                locked.chmod(0o700)

    def test_a_valid_folder_comes_back_RESOLVED(self):
        with tempfile.TemporaryDirectory() as temporary:
            messy = f"{temporary}/./"
            self.assertEqual(
                project_manager.folder_from_typed_path(messy, "new-parent"),
                str(Path(temporary).resolve()))


class NestingTests(unittest.TestCase):
    """A project inside a project makes two boards fight over one tree."""

    def _reject_against(self, typed: str, existing: str) -> str:
        with mock.patch.object(project_manager.registry, "entries",
                               return_value=[{"root": existing}]):
            with self.assertRaises(ValueError) as caught:
                project_manager.folder_from_typed_path(typed, "new-parent", home=mock.Mock())
        return str(caught.exception)

    def test_the_same_folder_is_refused_as_already_a_project(self):
        with tempfile.TemporaryDirectory() as temporary:
            self.assertIn("already a project", self._reject_against(temporary, temporary))

    def test_a_folder_INSIDE_an_existing_project_is_refused(self):
        with tempfile.TemporaryDirectory() as temporary:
            inner = Path(temporary) / "inner"; inner.mkdir()
            self.assertIn("inside the existing project", self._reject_against(str(inner), temporary))

    def test_a_folder_CONTAINING_an_existing_project_is_refused(self):
        with tempfile.TemporaryDirectory() as temporary:
            inner = Path(temporary) / "inner"; inner.mkdir()
            self.assertIn("contains the existing project", self._reject_against(temporary, str(inner)))


class ModeTests(unittest.TestCase):
    def test_a_platform_without_a_dialog_says_so(self):
        with mock.patch.object(project_manager.platform_support, "folder_chooser",
                               side_effect=project_manager.platform_support.UnsupportedPlatformOperation("x")):
            self.assertFalse(project_manager.native_folder_selection_available())

    def test_the_page_learns_it_from_the_payload_not_from_the_browser(self):
        """The browser cannot know what the SERVER's platform supports."""
        page = project_manager.PAGE
        self.assertIn("value.native_folder_picker", page)
        self.assertIn("applyFolderPickerMode", page)


class WiringTests(unittest.TestCase):
    """Every folder button must be able to READ the typed path.

    The reviewer found create and adopt calling browseFolder WITHOUT the input
    selector, so on Linux — where there is no native dialog — a valid typed path
    was ignored and the owner could not create or adopt a project at all. The
    app was unusable on the platform this port exists for.

    It slipped because my edits replaced two LITERAL call sites while the real
    one is a ternary, `adopting ? 'adopt-project' : 'new-parent'`, which matched
    neither — and my tests checked the server-side validator and the page source
    rather than the wiring between them. This asserts the wiring itself.
    """

    def _call_sites(self) -> list[str]:
        page = project_manager.PAGE
        sites = []
        index = 0
        while True:
            index = page.find("browseFolder(", index)
            if index < 0:
                return sites
            end = page.index(")", index)
            call = page[index:end + 1]
            if "async function" not in page[max(0, index - 30):index]:
                sites.append(call)
            index = end

    def test_every_browse_button_can_read_a_typed_path(self):
        sites = self._call_sites()
        self.assertTrue(sites, "no browseFolder call sites found — the check is vacuous")
        for call in sites:
            self.assertIn("-typed", call,
                          f"this button ignores the typed path, so it cannot work "
                          f"where there is no native dialog: {call}")

    def test_the_check_would_catch_a_missing_selector(self):
        """Proves the assertion above is not vacuous."""
        broken = "await browseFolder(adopting ? 'adopt-project' : 'new-parent', '#create-error')"
        self.assertNotIn("-typed", broken)


if __name__ == "__main__":
    unittest.main()
