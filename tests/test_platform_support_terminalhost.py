# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""The visible agent terminal — the largest platform surface in Stage 0.

Taken alone rather than batched, because it carries the real macOS regression
risk: two launchers that LOOK like duplicates and are not, plus a closer that
identifies a window by its tty.

Stage 1 made the seam select for real, so every test here that means "the macOS
terminal" now has to SAY so. Left inheriting its host, this file asserted which
machine ran it: seven errored on Linux reaching for `_open_script` on the tmux
host, one failed because tmux launches where Terminal refuses, and two more
passed there without executing a line of the code they name.
"""
from __future__ import annotations

import contextlib
import json
import os
import subprocess
import unittest
from pathlib import Path
from unittest import mock

from harness import board_viewer, control, platform_support, project_worker

GOLDEN = json.loads((Path(__file__).parent / "terminal_golden.json").read_text(encoding="utf-8"))


@contextlib.contextmanager
def macos_selected():
    """The macOS terminal, taken from the REAL selector, on any machine.

    Claiming the platform rather than reaching past `terminal_host()` keeps the
    selection itself under test: if the seam ever stopped handing Darwin the
    macOS host, these fail — which a hard-wired reference would hide.
    """
    with mock.patch.object(platform_support.defaults.sys, "platform", "darwin"):
        yield platform_support.terminal_host()


class GoldenAppleScriptTests(unittest.TestCase):
    """Frozen output per role colour.

    Captured from the pre-seam implementation before a line was touched. The
    same technique caught a reworded sandbox rule; here it catches a colour
    scale or a script line drifting during the move.

    AppleScript is macOS's answer, so macOS is selected deliberately. On Linux
    the seam correctly returns the tmux host, which has no `_open_script` at
    all: unpinned, these reported the machine and not the behaviour.
    """

    def test_every_role_colour_renders_exactly_what_it_rendered_before(self):
        self.assertEqual(sorted(GOLDEN), sorted(control.SESSION_COLORS),
                         "a role colour was added or removed; recapture the golden")
        with macos_selected() as host:
            for name, expected in sorted(GOLDEN.items()):
                with self.subTest(colour=name):
                    rendered = host._open_script(control.SESSION_COLORS[name]["rgb"])
                    self.assertEqual(rendered, expected)

    def test_the_colour_scale_still_rounds_the_way_it_did(self):
        with macos_selected() as host:
            self.assertEqual(host._colour_literal((0, 128, 255)), "{0, 32896, 65535}")


class LauncherDifferenceTests(unittest.TestCase):
    """The two launchers are NOT duplicates, and the collapse must prove it."""

    def _captured_argv(self, launch) -> list[str]:
        recorded = {}

        def capture(session_id, argv, *, color_rgb):
            recorded["argv"] = list(argv)
            return platform_support.SessionSurface(session_id)

        host = mock.Mock()
        host.open_session.side_effect = capture
        with mock.patch.object(platform_support, "terminal_host", return_value=host):
            launch()
        return recorded["argv"]

    def _session(self, **extra):
        base = {"id": "s1", "kind": "delivery", "color": "blue", "task": ""}
        base.update(extra)
        return base

    def test_the_viewer_passes_close_on_exit_and_the_worker_does_not(self):
        viewer = self._captured_argv(
            lambda: board_viewer.launch_terminal(Path("/root"), self._session()))
        worker = self._captured_argv(
            lambda: project_worker.launch_terminal(Path("/root"), self._session(), "/tmp/sock"))
        self.assertIn("--close-terminal-on-exit", viewer)
        self.assertNotIn("--close-terminal-on-exit", worker,
                         "the worker's terminal must not close itself on exit")

    def test_the_worker_passes_the_bootstrap_socket_and_the_viewer_does_not(self):
        worker = self._captured_argv(
            lambda: project_worker.launch_terminal(Path("/root"), self._session(), "/tmp/sock"))
        viewer = self._captured_argv(
            lambda: board_viewer.launch_terminal(Path("/root"), self._session()))
        self.assertIn("--board-bootstrap", worker)
        self.assertIn("/tmp/sock", worker)
        self.assertNotIn("--board-bootstrap", viewer)

    def test_the_viewer_appends_task_only_when_there_is_one(self):
        without = self._captured_argv(
            lambda: board_viewer.launch_terminal(Path("/root"), self._session(task="")))
        with_task = self._captured_argv(
            lambda: board_viewer.launch_terminal(Path("/root"), self._session(task="T-9")))
        self.assertNotIn("--task", without)
        self.assertEqual(with_task[-2:], ["--task", "T-9"])


class RefusalTests(unittest.TestCase):
    """How the macOS terminal behaves when it is asked to act off macOS.

    The first two name `defaults.TERMINAL_HOST` directly, which is the one
    combination the selector cannot produce: a machine claiming Linux is handed
    the tmux host, and the tmux host launches rather than refusing. Addressing
    the implementation by name is the only way to keep asking the question these
    tests were written to ask.
    """

    def test_both_launchers_still_refuse_off_macos_in_the_same_words(self):
        macos_host = platform_support.defaults.TERMINAL_HOST
        with mock.patch.object(platform_support, "terminal_host", return_value=macos_host), \
                mock.patch.object(platform_support.defaults.sys, "platform", "linux"):
            for launch in (
                lambda: board_viewer.launch_terminal(Path("/root"), {
                    "id": "s", "kind": "delivery", "color": "blue", "task": ""}),
                lambda: project_worker.launch_terminal(Path("/root"), {
                    "id": "s", "kind": "delivery", "color": "blue", "task": ""}, "/tmp/s"),
            ):
                with self.assertRaisesRegex(RuntimeError, "requires macOS Terminal"):
                    launch()

    def test_the_closer_still_returns_silently_off_macos(self):
        """Silence, not a refusal: a session ending is not a failure.

        Read off the seam this passed on Linux for no reason at all: the tmux
        host returns early when $TMUX is unset, so the assertion held without
        the macOS early return ever being reached.
        """
        with mock.patch.object(platform_support.defaults.sys, "platform", "linux"):
            with mock.patch.object(subprocess, "Popen") as never:
                platform_support.defaults.TERMINAL_HOST.dismiss_current_session(0)
            never.assert_not_called()

    def test_an_unusable_fd_is_survived_not_raised(self):
        """A tty is a macOS way of naming the surface, so macOS is selected.

        Unpinned this also passed on Linux without touching what it names: the
        tmux host never calls `ttyname`, so patching it proved nothing.
        """
        with macos_selected() as host:
            with mock.patch.object(os, "ttyname", side_effect=OSError("not a tty")):
                with mock.patch.object(subprocess, "Popen") as never:
                    host.dismiss_current_session(0)
                never.assert_not_called()


class SelfDismissalTests(unittest.TestCase):
    def test_the_dismissal_needs_no_session_argument(self):
        """The caller IS the occupant; a session id would imply a registry.

        Asked of EVERY implementation, not of whichever one this machine
        selects. The shape is a seam contract, and Stage 1 added a second host
        to hold it — read off the seam, a Mac would never check the tmux one.
        """
        import inspect
        from harness.platform_support import linux
        for name, host in (("macOS", platform_support.defaults.TERMINAL_HOST),
                           ("tmux", linux.TERMINAL_HOST)):
            with self.subTest(host=name):
                signature = inspect.signature(host.dismiss_current_session)
                required = [
                    parameter_name
                    for parameter_name, parameter in signature.parameters.items()
                    if parameter.default is inspect.Parameter.empty
                ]
                self.assertEqual(required, [],
                                 "the self-dismissal grew a required argument; "
                                 "it identifies no session")


if __name__ == "__main__":
    unittest.main()
