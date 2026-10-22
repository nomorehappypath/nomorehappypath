# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Finished agent Terminal windows close themselves (2026-10-21_agent-windows-close).

The agent runs in a detached `screen` session and its Terminal window only runs `screen -r`. The old closer ran inside the
session (not in a window), so it matched no window and every finished window stayed open. Now the window's last command is
harness/platform_support/agent_window.py, which closes THAT window.

Unit tests pin the decisions with the machine inputs removed (no Terminal, no osascript: both are replaced). The real-Terminal
class at the bottom raises and closes windows on the owner's Mac, so it only runs when HARNESS_TEST_NATIVE_AGENT_VIEW=1 and
only ever closes windows it opened itself.
"""
from __future__ import annotations

import os
import pty
import select
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from harness import board, control
from harness.platform_support import agent_window
from harness.platform_support import defaults
from harness.terminal_titles import role_title

ROOT = Path(__file__).resolve().parents[1]


def _macos():
    return mock.patch.object(defaults.sys, "platform", "darwin")


class WindowCommandTests(unittest.TestCase):
    def open(self, environment=None, run_side_effect=None):
        with tempfile.TemporaryDirectory() as temporary:
            data = Path(temporary) / ".harness"
            env = {k: v for k, v in os.environ.items() if k != defaults.HEADLESS_ENVIRONMENT}
            env.update(environment or {})
            with mock.patch.dict(os.environ, env, clear=True), _macos(), mock.patch.object(
                    defaults.subprocess, "run", side_effect=run_side_effect,
                    return_value=subprocess.CompletedProcess([], 0, "", "")) as run:
                surface = defaults.TERMINAL_HOST.open_session("codex_delivery-abc", ["/bin/true", "--data-root", str(data)], color_rgb=(0, 0, 0))
                log = data / "control" / "terminal-close.log"
                self.log_text = log.read_text() if log.exists() else ""
            return run, surface, data

    def test_the_window_ends_in_the_closer_not_in_a_bare_screen_attach(self):
        run, _, data = self.open()
        self.assertEqual(run.call_count, 2)
        command = run.call_args_list[1].args[0][-1]
        parts = shlex.split(command)
        self.assertEqual(parts[:3], ["exec", "/bin/sh", "-c"])
        self.assertIn("/usr/bin/screen -c /dev/null -r", parts[3])
        self.assertIn(str(Path(defaults.__file__).resolve().parent / "agent_window.py"), parts)
        self.assertIn(str(data / "control" / "terminal-close.log"), parts)
        self.assertIn("Developer [codex_delivery-abc]", parts)
        self.assertIn("--exit-code", parts[3])
        self.assertNotIn("exec /usr/bin/env SCREENDIR", command)
        self.assertEqual(run.call_args_list[1].kwargs["timeout"], 30)

    def test_headless_starts_the_session_and_raises_no_window(self):
        run, surface, _ = self.open({defaults.HEADLESS_ENVIRONMENT: "1"})
        self.assertEqual(run.call_count, 1)
        self.assertIn("-dmS", run.call_args_list[0].args[0])
        self.assertEqual(surface.attach_hint, "")

    def test_only_the_value_one_means_headless(self):
        run, _, _ = self.open({defaults.HEADLESS_ENVIRONMENT: "0"})
        self.assertEqual(run.call_count, 2)

    def test_a_viewer_that_cannot_open_does_not_fail_a_running_agent(self):
        def side_effect(command, *args, **kwargs):
            if command[0] == "/usr/bin/osascript":
                raise subprocess.CalledProcessError(1, command, "", "not allowed to control Terminal")
            return subprocess.CompletedProcess(command, 0, "", "")
        run, surface, data = self.open(run_side_effect=side_effect)
        self.assertIn("could not be opened", surface.attach_hint)
        self.assertIn("View agent", surface.attach_hint)
        self.assertIn("could not be opened", self.log_text)

    def test_a_screen_that_will_not_start_still_fails_the_launch(self):
        def side_effect(command, *args, **kwargs):
            raise subprocess.CalledProcessError(1, command, "", "no screen")
        with self.assertRaises(subprocess.CalledProcessError):
            self.open(run_side_effect=side_effect)

    def test_view_agent_focuses_only_a_live_attachment(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            from harness.project_context import project_context
            directory = project_context(root).storage_path("control", "terminal-sockets")
            directory.mkdir(parents=True, exist_ok=True)
            (directory / "1.nmhp-codex_delivery-abc").touch()
            with _macos(), mock.patch.object(defaults.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "focused\n", "")) as run:
                defaults.TERMINAL_HOST.view_session({"id": "codex_delivery-abc"}, root=root)
            script = run.call_args.args[0][2]
            self.assertIn("(custom title of terminalTab is item 1 of argv) and (busy of terminalTab)", script)
            self.assertEqual(run.call_args.kwargs["timeout"], 30)

    def test_the_focus_scripts_survive_a_closed_window_that_lingers_in_terminals_list(self):
        self.assertIn("   try\n    repeat with terminalTab in tabs of terminalWindow", defaults.TERMINAL_HOST.FOCUS_SCRIPT)


class ScriptTextTests(unittest.TestCase):
    def test_the_closer_checks_the_window_again_and_never_forces_or_guesses(self):
        script = agent_window.CLOSE_SCRIPT
        for needle in ("close window id targetId", "count of tabs of window id targetId) is not 1",
                       "(custom title of agentTab) is not (item 3 of argv)", "(tty of agentTab) is not (item 2 of argv)",
                       "if not (busy of agentTab) then", "on error problem"):
            self.assertIn(needle, script)
        for forbidden in ("saving no", "close window 1", "do shell script", "whose name"):
            self.assertNotIn(forbidden, script)
        self.assertLess(script.index("on error problem"), script.index("delay 0.25"))
        self.assertIn('return "left open: " & problem', script, "an error must leave the window open, never authorise closing")

    def test_the_lookup_matches_tty_title_and_a_running_tab(self):
        script = agent_window.LOOKUP_SCRIPT
        self.assertIn("(tty of terminalTab is item 1 of argv) and (custom title of terminalTab is item 2 of argv) and (busy of terminalTab)", script)
        self.assertIn("set custom title of terminalTab to item 3 of argv", script)


class HelperDecisionTests(unittest.TestCase):
    def helper(self, osascript, *, exit_code=0, ttyname="/dev/ttys099"):
        with tempfile.TemporaryDirectory() as temporary:
            log = Path(temporary) / "close.log"
            with mock.patch.object(agent_window.sys, "platform", "darwin"), mock.patch.object(agent_window.os, "ttyname", return_value=ttyname) as tn, \
                    mock.patch.object(agent_window, "_osascript", side_effect=osascript) as run, mock.patch.object(agent_window.subprocess, "Popen") as popen:
                if ttyname is None:
                    tn.side_effect = OSError
                agent_window.close_after_exit(str(log), "Developer [x]", exit_code)
            return run, popen, (log.read_text() if log.exists() else "")

    def test_a_found_window_gets_a_detached_closer_for_that_window_id(self):
        run, popen, log = self.helper(lambda *a, **k: "16715")
        command = popen.call_args.args[0]
        self.assertIn("--close", command)
        self.assertEqual(command[command.index("--close") + 1:command.index("--close") + 4], ["16715", "/dev/ttys099", "Developer [x] (closing)"])
        self.assertTrue(popen.call_args.kwargs["start_new_session"])
        self.assertEqual(log, "")
        self.assertEqual(run.call_args.args[1], ["/dev/ttys099", "Developer [x]", "Developer [x] (closing)"])
        self.assertEqual(run.call_args.args[2], agent_window.LOOKUP_SECONDS)

    def test_a_window_not_found_alive_is_left_open_and_logged(self):
        _, popen, log = self.helper(lambda *a, **k: "not found")
        popen.assert_not_called()
        self.assertIn("not found while it was still running", log)
        self.assertIn("close this window by hand", log)
        self.assertIn("Developer [x]", log)

    def test_a_failed_or_stalled_lookup_leaves_the_window_open_and_logs_why(self):
        for error in (subprocess.TimeoutExpired("osascript", 10), OSError("Not authorized to send Apple events")):
            with self.subTest(error=type(error).__name__):
                def fail(*a, **k):
                    raise error
                _, popen, log = self.helper(fail)
                popen.assert_not_called()
                self.assertIn("could not look the window up", log)

    def test_no_terminal_to_identify_the_window_by_is_logged_not_guessed(self):
        run, popen, log = self.helper(lambda *a, **k: "1", ttyname=None)
        run.assert_not_called()
        popen.assert_not_called()
        self.assertIn("no terminal", log)

    def test_a_nonzero_screen_exit_is_logged_but_the_window_still_closes(self):
        _, popen, log = self.helper(lambda *a, **k: "16715", exit_code=1)
        self.assertIn("screen -r ended with code 1", log)
        popen.assert_called_once()

    def test_the_closer_logs_every_answer_except_closed(self):
        for answer, logged in (("closed", False), ("already closed", False),
                               ("left open: the window holds more than the agent's own tab", True),
                               ("left open: something is still running in it", True)):
            with self.subTest(answer=answer), tempfile.TemporaryDirectory() as temporary:
                log = Path(temporary) / "close.log"
                with mock.patch.object(agent_window, "_osascript", return_value=answer):
                    agent_window._closer("16715", "/dev/ttys099", "T (closing)", str(log), "T")
                self.assertEqual(log.exists(), logged)
                if logged:
                    self.assertIn(answer, log.read_text())
                    self.assertIn("window 16715", log.read_text())

    def test_a_closer_that_times_out_or_errors_leaves_the_window_open_and_says_so(self):
        for error in (subprocess.TimeoutExpired("osascript", 30), OSError("boom")):
            with self.subTest(error=type(error).__name__), tempfile.TemporaryDirectory() as temporary:
                log = Path(temporary) / "close.log"
                with mock.patch.object(agent_window, "_osascript", side_effect=error):
                    agent_window._closer("16715", "/dev/ttys099", "T (closing)", str(log), "T")
                self.assertIn("the close request failed", log.read_text())

    def test_other_platforms_do_nothing(self):
        with mock.patch.object(agent_window.sys, "platform", "linux"), mock.patch.object(agent_window, "_osascript") as run:
            agent_window.close_after_exit("", "T", 0)
        run.assert_not_called()


class FailureReasonSurvivesTheWindowTests(unittest.TestCase):
    """The window closes by itself now, so what the owner can read afterwards is the transcript and the board."""

    def supervise(self, child, root):
        session = control.create(root, "claude_reviewer")
        agent = board.register(root, "qa", "REVIEW", vendor="Anthropic", session_id=session["id"])
        master, slave = pty.openpty()
        command = [sys.executable, str(ROOT / "harness" / "interactive_supervisor.py"), "--root", str(root), "--session-id", session["id"],
                   "--agent-id", agent["id"], "--provider", "claude", "--", *child]
        process = subprocess.Popen(command, stdin=slave, stdout=slave, stderr=slave, close_fds=True)
        os.close(slave)
        output = b""
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and process.poll() is None:
            if select.select([master], [], [], .1)[0]:
                try:
                    output += os.read(master, 65536)
                except OSError:
                    break
        code = process.wait(timeout=10)
        os.close(master)
        transcript = next((root / ".harness" / "control" / "transcripts").glob(session["id"] + "*.log")).read_text()
        events = (root / ".harness" / "board" / "events.jsonl")
        return code, transcript, events.read_text() if events.exists() else ""

    def test_an_agent_that_refuses_at_once_leaves_its_message_in_the_transcript(self):
        for attempt in range(5):
            with self.subTest(attempt=attempt), tempfile.TemporaryDirectory() as temporary:
                code, transcript, _ = self.supervise(["/bin/sh", "-c", "echo REFUSED-model-not-available; exit 3"], Path(temporary))
                self.assertEqual(code, 3)
                self.assertIn("REFUSED-model-not-available", transcript)

    def test_an_agent_program_that_cannot_start_records_why_on_the_board_and_in_the_transcript(self):
        with tempfile.TemporaryDirectory() as temporary:
            code, transcript, events = self.supervise(["/nonexistent/provider-cli"], Path(temporary))
            self.assertEqual(code, 127)
            self.assertIn("could not be started", transcript)
            self.assertIn("/nonexistent/provider-cli", transcript)
            self.assertIn("/nonexistent/provider-cli", events)

    def test_the_transcript_no_longer_claims_a_terminal_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            _, transcript, _ = self.supervise(["/bin/sh", "-c", "exit 0"], Path(temporary))
            self.assertIn("supervisor ended", transcript)
            self.assertNotIn("terminal closed", transcript)



# ---------------------------------------------------------------------------------------------------------------------
# Real Terminal. RAISES AND CLOSES TERMINAL WINDOWS ON THE OWNER'S MAC: opt-in, and it only ever closes windows it opened
# itself (found by their unique title, closed by id). The owner's own windows are compared by exact id before and after.
# ---------------------------------------------------------------------------------------------------------------------
_LIST = r'''on run
 set out to ""
 tell application "Terminal"
  repeat with w in windows
   try
    if visible of w then set out to out & ((id of w) as string) & " "
   end try
  end repeat
 end tell
 return out
end run'''

_FIND = r'''on run argv
 tell application "Terminal"
  repeat with w in windows
   try
    if visible of w then
     repeat with t in tabs of w
      if (custom title of t) starts with (item 1 of argv) then return (id of w) as string
     end repeat
    end if
   end try
  end repeat
 end tell
 return ""
end run'''

_EXISTS = r'''on run argv
 tell application "Terminal"
  try
   if (exists window id ((item 1 of argv) as integer)) and (visible of window id ((item 1 of argv) as integer)) then return "yes"
  end try
 end tell
 return "no"
end run'''

_SET_TITLE = r'''on run argv
 tell application "Terminal"
  repeat with w in windows
   try
    if (id of w) is ((item 1 of argv) as integer) then set custom title of tab 1 of w to (item 2 of argv)
   end try
  end repeat
 end tell
end run'''

_CLOSE = r'''on run argv
 tell application "Terminal"
  try
   close window id ((item 1 of argv) as integer)
  end try
 end tell
end run'''


def _osa(script, *arguments):
    return subprocess.run(["/usr/bin/osascript", "-e", script, *arguments], capture_output=True, text=True, timeout=30, check=False).stdout.strip()


@unittest.skipUnless(sys.platform == "darwin" and os.environ.get("HARNESS_TEST_NATIVE_AGENT_VIEW") == "1" and Path("/usr/bin/screen").exists(),
                     "raises and closes real Terminal windows on the owner's Mac: set HARNESS_TEST_NATIVE_AGENT_VIEW=1")
class RealTerminalWindowTests(unittest.TestCase):
    def setUp(self):
        self.base = Path(tempfile.mkdtemp(dir="/private/tmp", prefix="awc"))
        self.data = self.base / "d"
        self.log = self.data / "control" / "terminal-close.log"
        self.owned = []
        self.sessions = []
        self.before = set(_osa(_LIST).split())
        self.addCleanup(self.cleanup)

    def cleanup(self):
        for window in self.owned:
            _osa(_CLOSE, window)
        sockets = self.data / "control" / "terminal-sockets"
        if sockets.is_dir():
            for entry in sockets.iterdir():
                name = entry.name.split(".", 1)[-1]
                subprocess.run(["/usr/bin/screen", "-S", name, "-X", "quit"], env={**os.environ, "SCREENDIR": str(sockets)}, capture_output=True, timeout=10)
        shutil.rmtree(self.base, ignore_errors=True)

    def start(self, seconds, exit_code=0, headless=False):
        child = self.base / "child.py"
        child.write_text(f"import sys,time\nprint('AGENT RUNNING',flush=True)\ntime.sleep({seconds})\nsys.exit({exit_code})\n")
        session_id = "codex_delivery-awc" + str(len(self.sessions)) + format(int(time.time() * 1000) % 100000, "d")
        self.sessions.append(session_id)
        environment = {**os.environ, "HARNESS_HEADLESS_AGENTS": "1"} if headless else {k: v for k, v in os.environ.items() if k != "HARNESS_HEADLESS_AGENTS"}
        with mock.patch.dict(os.environ, environment, clear=True):
            defaults.TERMINAL_HOST.open_session(session_id, [sys.executable, str(child), "--data-root", str(self.data)], color_rgb=(0, 0, 0))
        return session_id, role_title(session_id) + " [" + session_id + "]"

    def wait_window(self, title, timeout=8):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            found = _osa(_FIND, title)
            if found:
                self.owned.append(found)
                return found
            time.sleep(0.2)
        return ""

    def wait_gone(self, window, timeout=15):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if _osa(_EXISTS, window) == "no":
                return True
            time.sleep(0.25)
        return False

    def assert_owner_windows_untouched(self):
        self.assertEqual(self.before - set(_osa(_LIST).split()), set(), "a window that was open before the test is gone")

    def screen_state(self, session_id):
        sockets = self.data / "control" / "terminal-sockets"
        result = subprocess.run(["/usr/bin/screen", "-ls"], env={**os.environ, "SCREENDIR": str(sockets)}, capture_output=True, text=True, timeout=10)
        for line in result.stdout.splitlines():
            if "nmhp-" + session_id in line:
                return "(Attached)" if "(Attached)" in line else "(Detached)" if "(Detached)" in line else "dead"
        return ""

    def screen_alive(self, session_id):
        return self.screen_state(session_id) in ("(Attached)", "(Detached)")

    def wait_screen(self, session_id, state, timeout=10):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.screen_state(session_id) == state:
                return
            time.sleep(0.2)
        self.fail(f"screen session never became {state}")

    def test_a_window_closes_by_itself_when_the_session_ends(self):
        session_id, title = self.start(3)
        window = self.wait_window(title)
        self.assertTrue(window, "the agent's window never appeared")
        self.assertTrue(self.wait_gone(window), "the finished agent's window is still open")
        self.assert_owner_windows_untouched()

    def test_a_session_that_fails_at_once_closes_its_window_too(self):
        session_id, title = self.start(0, exit_code=3)
        window = self.wait_window(title, timeout=4)
        if window:
            self.assertTrue(self.wait_gone(window))
        self.assert_owner_windows_untouched()

    def test_headless_raises_no_window_and_view_agent_opens_one_that_also_closes(self):
        session_id, title = self.start(6, headless=True)
        time.sleep(2.5)
        self.assertEqual(_osa(_FIND, title), "", "headless raised a window")
        self.assertTrue(self.screen_alive(session_id), "the agent is not running headless")
        # View agent for a headless session: the same view command, opened by the host on demand.
        defaults.TERMINAL_HOST._open_screen_view(self.data / "control" / "terminal-sockets", session_id, (0, 0, 0))
        window = self.wait_window(title)
        self.assertTrue(window, "View agent did not open a window")
        self.assertTrue(self.wait_gone(window, timeout=20), "the viewer window did not close when the session ended")
        self.assert_owner_windows_untouched()

    def test_detaching_closes_the_viewer_but_not_the_agent_and_view_agent_reattaches(self):
        session_id, title = self.start(12)
        window = self.wait_window(title)
        self.assertTrue(window)
        sockets = self.data / "control" / "terminal-sockets"
        self.wait_screen(session_id, "(Attached)")
        subprocess.run(["/usr/bin/screen", "-S", "nmhp-" + session_id, "-X", "detach"], env={**os.environ, "SCREENDIR": str(sockets)}, capture_output=True, timeout=10)
        self.assertTrue(self.wait_gone(window), "the detached viewer window stayed open")
        self.assertTrue(self.screen_alive(session_id), "detaching must not end the agent")
        defaults.TERMINAL_HOST._open_screen_view(sockets, session_id, (0, 0, 0))
        again = self.wait_window(title)
        self.assertTrue(again and again != window, "View agent did not open a fresh window")
        self.wait_screen(session_id, "(Attached)")
        self.assertTrue(self.wait_gone(again, timeout=25))
        self.assert_owner_windows_untouched()

    def test_a_session_stopped_from_outside_closes_its_window_and_a_pause_resume_of_one_id_does_too(self):
        # What Stop, Pause, Stop all and the acceptance stop end up doing to the agent: the screen session ends while
        # its program still runs. Then the same session id is started again (resume) and ends the same way.
        sockets = self.data / "control" / "terminal-sockets"
        session_id, title = self.start(60)
        first = self.wait_window(title)
        self.assertTrue(first)
        self.wait_screen(session_id, "(Attached)")
        subprocess.run(["/usr/bin/screen", "-S", "nmhp-" + session_id, "-X", "quit"], env={**os.environ, "SCREENDIR": str(sockets)}, capture_output=True, timeout=10)
        self.assertTrue(self.wait_gone(first), "a stopped agent's window stayed open")
        defaults.TERMINAL_HOST._open_screen_view(sockets, session_id, (0, 0, 0))  # no screen to resume: ends at once
        again = self.wait_window(title, timeout=3)
        if again:
            self.assertTrue(self.wait_gone(again))
        self.assert_owner_windows_untouched()

    def test_a_tab_the_owner_renamed_is_left_open_with_a_reason(self):
        session_id, title = self.start(4)
        window = self.wait_window(title)
        self.assertTrue(window)
        _osa(_SET_TITLE, window, "my own notes")
        time.sleep(7)
        self.assertEqual(_osa(_EXISTS, window), "yes", "a renamed window was closed")
        self.assertIn("not found while it was still running", self.log.read_text())
        self.assert_owner_windows_untouched()

    def test_a_second_session_after_a_finished_one_closes_its_own_window(self):
        session_id, title = self.start(2)
        first = self.wait_window(title)
        self.assertTrue(first)
        # the old window keeps its title while the same session is resumed: a second start of the same id
        self.assertTrue(self.wait_gone(first))
        again, _ = self.start(3)
        window = self.wait_window(role_title(again) + " [" + again + "]")
        self.assertTrue(window)
        self.assertTrue(self.wait_gone(window))
        self.assert_owner_windows_untouched()


if __name__ == "__main__":
    unittest.main()
