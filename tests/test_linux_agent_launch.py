# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Linux: starting an agent must work, or say plainly why not.

Found on a real Ubuntu 24.04 machine, where the Dev, CTO and Reviewer all
"crashed immediately, no window". Three separate causes, all with the same
symptom - a tmux session that opened and vanished with its own error message:

1. bubblewrap missing, or present but refused by the kernel (Ubuntu 24.04
   blocks the unprivileged user namespaces it needs unless AppArmor allows it);
2. the launcher created `~/.claude.json` as an EMPTY file, which Claude Code
   reads as corrupt and stops on;
3. nothing kept a failed session's message on screen.
"""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from harness import platform_support
from harness.platform_support import defaults, linux


def fake_bwrap(directory: Path, *, exit_code: int, stderr: str = "") -> str:
    path = directory / "bwrap"
    path.write_text(f"#!/bin/sh\necho '{stderr}' >&2\nexit {exit_code}\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return str(path)


class LaunchProblemTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.dir = Path(self.temp.name)
        tmux = mock.patch.object(shutil, "which", side_effect=lambda name, *a, **k: "/usr/bin/tmux" if name == "tmux" else None)
        tmux.start()
        self.addCleanup(tmux.stop)

    def problem(self, **environment) -> str:
        with mock.patch.dict(os.environ, environment):
            return linux.launch_problem()

    def test_missing_bubblewrap_names_the_one_command_that_fixes_it(self):
        text = self.problem(HARNESS_BWRAP_BIN=str(self.dir / "absent"))
        self.assertIn("bubblewrap", text)
        self.assertIn("scripts/linux_enable_sandbox.sh", text)

    def test_bubblewrap_refused_by_the_kernel_is_reported_with_its_own_reason(self):
        text = self.problem(HARNESS_BWRAP_BIN=fake_bwrap(
            self.dir, exit_code=1, stderr="bwrap: setting up uid map: Permission denied"))
        self.assertIn("setting up uid map: Permission denied", text)
        self.assertIn("scripts/linux_enable_sandbox.sh", text)

    def test_a_sandbox_problem_keeps_the_distro_profile_commands_and_never_weakens_the_system(self):
        text = self.problem(HARNESS_BWRAP_BIN=fake_bwrap(
            self.dir, exit_code=1, stderr="bwrap: setting up uid map: Permission denied"))
        self.assertIn("apparmor-profiles", text)
        self.assertIn("bwrap-userns-restrict", text)
        self.assertIn("apparmor_parser -r", text)
        self.assertNotIn("sysctl -w", text)

    def test_a_tmux_only_problem_does_not_send_the_owner_to_apparmor(self):
        with mock.patch.object(shutil, "which", return_value=None), \
                mock.patch.object(linux.AGENT_CONFINEMENT, "binary", return_value=fake_bwrap(self.dir, exit_code=0)):
            text = linux.launch_problem()
        self.assertIn("tmux is not installed", text)
        self.assertNotIn("apparmor", text)

    def test_working_bubblewrap_has_no_problem(self):
        self.assertEqual(self.problem(HARNESS_BWRAP_BIN=fake_bwrap(self.dir, exit_code=0)), "")

    def test_missing_tmux_is_named_with_the_same_one_command_fix(self):
        with mock.patch.object(shutil, "which", return_value=None):
            text = linux.launch_problem()
        self.assertIn("tmux is not installed", text)
        self.assertIn("scripts/linux_enable_sandbox.sh", text)

    def test_every_problem_is_reported_together_not_one_per_attempt(self):
        with mock.patch.object(shutil, "which", return_value=None), \
                mock.patch.dict(os.environ, {"HARNESS_BWRAP_BIN": str(self.dir / "absent")}):
            text = linux.launch_problem()
        self.assertIn("tmux is not installed", text)
        self.assertIn("Bubblewrap is not installed", text)

    def test_the_seam_asks_linux_and_macos_has_nothing_to_check(self):
        with mock.patch.object(linux, "launch_problem", return_value="BLOCKED"), \
                mock.patch.object(platform_support.sys, "platform", "linux"):
            self.assertEqual(platform_support.launch_problem(), "BLOCKED")
        with mock.patch.object(platform_support.sys, "platform", "darwin"):
            self.assertEqual(platform_support.launch_problem(), "")


class OpenSessionRefusesBeforeOpeningTests(unittest.TestCase):
    def test_a_blocked_launch_opens_no_session_and_says_why(self):
        with mock.patch.object(linux, "launch_problem", return_value="THE PLAIN REASON"), \
                mock.patch("subprocess.run") as run:
            with self.assertRaises(defaults.UnsupportedPlatformOperation) as caught:
                linux.TERMINAL_HOST.open_session("reviewer-1", ["/bin/true"], color_rgb=(0, 0, 0))
        self.assertIn("THE PLAIN REASON", str(caught.exception))
        run.assert_not_called()


class FailedSessionStaysReadableTests(unittest.TestCase):
    """The wrapper is run for real, with the shell tmux would run it in."""

    def run_wrapped(self, script: str, *, answer: str = ""):
        argv = linux.TERMINAL_HOST.keep_failure_visible(["/bin/sh", "-c", script])
        return subprocess.run(argv, input=answer, capture_output=True, text=True, timeout=20)

    def test_a_failing_agent_shows_its_message_and_waits_for_a_key(self):
        done = self.run_wrapped("echo REFUSED: the real reason >&2; exit 3", answer="\n")
        self.assertEqual(done.returncode, 3)
        self.assertIn("REFUSED: the real reason", done.stderr)
        self.assertIn("The agent stopped (exit code 3)", done.stdout)

    def test_a_failing_agent_does_not_close_until_a_key_is_pressed(self):
        argv = linux.TERMINAL_HOST.keep_failure_visible(["/bin/sh", "-c", "exit 7"])
        process = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        self.addCleanup(lambda: [process.kill(), process.stdin.close(), process.stdout.close(), process.wait()])
        time.sleep(0.5)
        self.assertIsNone(process.poll(), "closed before the owner could read it")
        process.stdin.write("\n"); process.stdin.flush()
        self.assertEqual(process.wait(timeout=10), 7)

    def test_a_clean_exit_closes_at_once_with_nothing_added(self):
        done = self.run_wrapped("echo fine; exit 0")
        self.assertEqual((done.returncode, done.stdout.strip()), (0, "fine"))

    def test_arguments_with_spaces_and_quotes_survive_untouched(self):
        argv = linux.TERMINAL_HOST.keep_failure_visible(["/usr/bin/printf", "%s|", "a b", "it's", '"q"'])
        done = subprocess.run(argv, capture_output=True, text=True, timeout=10)
        self.assertEqual(done.stdout, "a b|it's|\"q\"|")


@unittest.skipUnless(sys.platform.startswith("linux") and shutil.which("tmux"), "needs Linux with tmux")
class RealTmuxTests(unittest.TestCase):
    """Real tmux, on a private socket directory so the owner's own sessions are never touched."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        patched = mock.patch.dict(os.environ, {"TMUX_TMPDIR": self.temp.name})
        patched.start(); self.addCleanup(patched.stop)
        self.addCleanup(lambda: subprocess.run(["tmux", "kill-server"], capture_output=True))
        problem = mock.patch.object(linux, "launch_problem", return_value="")
        problem.start(); self.addCleanup(problem.stop)

    def sessions(self) -> list[str]:
        done = subprocess.run(["tmux", "ls", "-F", "#S"], capture_output=True, text=True)
        return done.stdout.split()

    def test_a_failed_agent_leaves_its_window_with_the_message_and_a_clean_one_does_not(self):
        linux.TERMINAL_HOST.open_session("bad", ["/bin/sh", "-c", "echo REFUSED-BY-AGENT >&2; exit 3"], color_rgb=(1, 2, 3))
        linux.TERMINAL_HOST.open_session("good", ["/bin/sh", "-c", "exit 0"], color_rgb=(1, 2, 3))
        time.sleep(1.5)
        self.assertEqual(self.sessions(), ["nmhp-bad"])
        shown = subprocess.run(["tmux", "capture-pane", "-p", "-t", "nmhp-bad"], capture_output=True, text=True).stdout
        self.assertIn("REFUSED-BY-AGENT", shown)


class EmptyClaudeStateFileTests(unittest.TestCase):
    """`~/.claude.json` must never be left empty: Claude Code treats that as corrupt."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        patched = mock.patch.dict(os.environ, {"HARNESS_BWRAP_BIN": fake_bwrap(self.home, exit_code=0)})
        patched.start(); self.addCleanup(patched.stop)

    def wrap(self, state: Path) -> None:
        linux.AGENT_CONFINEMENT.wrap(["claude"], [str(state)], store=str(self.home / "store"))

    def test_a_missing_state_file_is_created_as_valid_json(self):
        state = self.home / ".claude.json"
        self.wrap(state)
        self.assertEqual(state.read_text(encoding="utf-8").strip(), "{}")

    def test_an_empty_state_file_left_by_an_earlier_version_is_repaired(self):
        state = self.home / ".claude.json"
        state.write_text("", encoding="utf-8")
        self.wrap(state)
        self.assertEqual(state.read_text(encoding="utf-8").strip(), "{}")

    def test_a_real_state_file_is_never_touched(self):
        state = self.home / ".claude.json"
        state.write_text('{"projects": {"a": 1}}', encoding="utf-8")
        self.wrap(state)
        self.assertEqual(state.read_text(encoding="utf-8"), '{"projects": {"a": 1}}')


if __name__ == "__main__":
    unittest.main()
