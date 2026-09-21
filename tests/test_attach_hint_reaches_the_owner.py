# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
"""The attach command must reach the owner, or a Linux launch looks like nothing.

On macOS the agent's window appears in front of the owner and there is nothing
to say. On Linux the session runs in tmux with no window at all: without the
exact attach command surfaced in Mission Control, the owner has started an agent
they have no way to reach, and the launch is indistinguishable from a failure.

`SessionSurface.attach_hint` existed and was built by the tmux host, and NOTHING
CONSUMED IT — the launchers discarded the return value. The feature was complete
everywhere except where a person could see it.
"""
from __future__ import annotations

import shutil
import unittest
from pathlib import Path
from unittest import mock

from harness import board_viewer, platform_support
from harness.platform_support import defaults, linux

SESSION = {"id": "delivery-1", "kind": "delivery", "color": "blue", "task": ""}


class SurfaceIsReturnedTests(unittest.TestCase):
    def test_linux_returns_the_exact_attach_command(self):
        with mock.patch.object(platform_support, "terminal_host", return_value=linux.TERMINAL_HOST), \
                mock.patch.object(shutil, "which", return_value="/usr/bin/tmux"), \
                mock.patch("subprocess.run"):
            surface = board_viewer.launch_terminal(Path("/root"), SESSION)
        self.assertEqual(surface.attach_hint, "tmux attach -t nmhp-delivery-1")

    def test_macos_returns_NO_hint_because_the_window_is_already_there(self):
        """Honest in both directions: inventing a hint on macOS would be noise.

        The macOS PLATFORM is forced, not just the macOS host. Forcing only the
        host left this failing on Linux — where that host correctly refuses —
        which made a test written to check platform honesty itself report the
        machine it ran on. Exactly the class it exists to guard against.
        """
        with mock.patch.object(platform_support, "terminal_host", return_value=defaults.TERMINAL_HOST), \
                mock.patch.object(defaults.sys, "platform", "darwin"), \
                mock.patch("subprocess.run"):
            surface = board_viewer.launch_terminal(Path("/root"), SESSION)
        self.assertEqual(surface.attach_hint, "")

    def test_the_launcher_does_not_DISCARD_the_surface(self):
        """The defect this file exists for: the hint was built and thrown away."""
        source = (Path(__file__).resolve().parent.parent / "harness" / "board_viewer.py").read_text(encoding="utf-8")
        body = source[source.index("def launch_terminal("):]
        body = body[:body.index("\ndef ", 1)]
        self.assertIn("return platform_support.terminal_host().open_session", body,
                      "launch_terminal must RETURN the surface, not discard it")


class ItReachesThePageTests(unittest.TestCase):
    def test_the_hint_is_read_defensively_from_whatever_a_launcher_returned(self):
        """Behaviour, not source text.

        The first version of this test asserted the literal implementation line
        and broke the moment the implementation improved — the same habit this
        port keeps finding in other tests.
        """
        self.assertEqual(
            board_viewer._attach_hint(platform_support.SessionSurface("s", "tmux attach -t x")),
            "tmux attach -t x")
        self.assertEqual(board_viewer._attach_hint(platform_support.SessionSurface("s")), "")
        # A stubbed launcher returns a Mock whose every attribute is truthy.
        # Reading it directly put an unserialisable object in the API payload
        # and errored three suites.
        self.assertEqual(board_viewer._attach_hint(mock.MagicMock()), "")
        self.assertEqual(board_viewer._attach_hint(None), "")

    def test_both_api_paths_forward_the_hint(self):
        source = (Path(__file__).resolve().parent.parent / "harness" / "board_viewer.py").read_text(encoding="utf-8")
        self.assertEqual(source.count('"attach_hint"] = hint'), 2,
                         "both the delivery-start path and the session endpoint must forward it")

    def test_the_page_shows_the_command_rather_than_a_generic_notice(self):
        page = board_viewer.rendered_page()
        self.assertIn("started.attach_hint", page,
                      "the page must read the hint the API sends")
        self.assertIn("Attach to watch it", page,
                      "the owner must be told what to DO with it")


if __name__ == "__main__":
    unittest.main()
