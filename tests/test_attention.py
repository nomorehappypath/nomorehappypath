# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""A managed terminal that stops to ask the owner is launched not to, and shouted about if it does.

Found on 2026-09-23: every Claude terminal Mission Control opened ran in
Manual permission mode and stopped at "Do you want to proceed?" until a
person looked at the window; the CTO, unable to write its evidence file,
handed the owner a command to run. Three guarantees follow:

1. the runner launches Claude with bypass mode and the harness's own roots
   as working directories (the same grant Codex gets), on fresh and resume;
2. the supervisor recognises a waiting prompt in the terminal output and the
   session record says so, until the owner answers or the terminal moves on;
3. Mission Control and the studio board show a blinking banner while any
   terminal is waiting, and nothing when none is.
"""
from __future__ import annotations

import os
import pty
import select
import subprocess
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

from harness import attention, board, board_viewer, control, conversation, project_manager
from harness import project_registry as registry
from harness.agent_grant import agent_writable_roots
from tests.test_branding_rendered import probe_proxy
from tests.test_conversation_memory import fake_cli, run_runner, stage_relaunch
from tests.environment_support import require_loopback

ROOT = Path(__file__).resolve().parents[1]

# The prompt as a live Claude terminal printed it on 2026-09-23 (transcript
# claude_cto-92c186074c), with the cursor movement a full-screen redraw
# carries, split the way the PTY delivered it: in pieces.
REAL_PROMPT_PIECES = [
    b"\x1b[2J\x1b[H\x1b[38;5;244m\xe2\x94\x80\xe2\x94\x80\xe2\x94\x80\xe2\x94\x80\xe2\x94\x80\xe2\x94\x80\x1b[0m\r\n \x1b[1mBash command\x1b[0m\r\n",
    b" Tip: auto mode handles these prompts for you \xe2\x80\x94 choose \"switch to auto mode\" below\r\n",
    b"\r\n   \xe2\x94\x82 cd /Users/owner/.harness-home/projects/1374/workspaces/film-schema-policy && grep -n \"def test\" tests/test_film_spec.py\r\n",
    b"   List delivery test names in both candidate test files\r\n\r\n",
    b" Do you want to pro", b"ceed?\r\n \x1b[36m\xe2\x9d\xaf 1. Yes\x1b[0m\r\n   2. Yes, and don\xe2\x80\x99t ask again for: cd *\r\n",
    b"   3. Yes, and switch to auto mode \xc2\xb7 auto mode handles these prompts for you\r\n   4. No\r\n",
]
ORDINARY_OUTPUT = (
    b"\x1b[32m\xe2\x8f\xba\x1b[0m Read(tests/test_media_policy.py)\r\n  \xe2\x8e\xbf  Read 212 lines\r\n"
    b"Running the focused suite now.\r\n$ python3 -m unittest tests.test_media_policy\r\n"
    b"Ran 14 tests in 0.311s\r\nOK\r\nUSER ACTION: None\r\n"
)


class PromptRecognitionTests(unittest.TestCase):
    def test_the_real_claude_prompt_is_recognised_across_split_reads_and_redraws(self):
        watch = attention.PromptWatch()
        changes = [watch.feed(piece) for piece in REAL_PROMPT_PIECES]
        first = next((change for change in changes if change), None)
        self.assertIsNotNone(first, "the prompt was never recognised")
        self.assertEqual(first[0], "waiting")
        self.assertIn("Do you want to proceed?", first[1])
        # A redraw repeating the same prompt is not a second event.
        self.assertIsNone(watch.feed(b"".join(REAL_PROMPT_PIECES)))
        self.assertEqual(watch.reason, first[1])

    def test_ordinary_output_and_user_action_none_never_count_as_waiting(self):
        watch = attention.PromptWatch()
        self.assertIsNone(watch.feed(ORDINARY_OUTPUT))
        self.assertIsNone(watch.feed(b"HARNESS | interactive supervisor ready; terminal input remains yours and is visible.\r\n"))
        self.assertIsNone(watch.reason)

    def test_an_agents_own_user_action_needed_line_counts_as_waiting(self):
        watch = attention.PromptWatch()
        change = watch.feed(b"USER ACTION: Needed. Pick one:\r\n1. Run this one-line copy yourself\r\n")
        self.assertEqual(change[0], "waiting")
        self.assertIn("USER ACTION: Needed", change[1])

    def test_the_owner_typing_clears_the_wait_and_a_prompt_still_on_screen_does_not_re_raise_it(self):
        watch = attention.PromptWatch()
        for piece in REAL_PROMPT_PIECES:
            watch.feed(piece)
        self.assertIsNotNone(watch.reason)
        self.assertEqual(watch.owner_typed(), ("cleared", None))
        self.assertIsNone(watch.owner_typed(), "clearing twice is not two events")
        # The CLI has not redrawn yet: the prompt is still on screen, a spinner
        # line appears. That is the owner's answer being processed, not a new wait.
        self.assertIsNone(watch.feed(b"\r\n Thinking\xe2\x80\xa6\r\n"))
        self.assertIsNone(watch.reason)
        # Only a prompt that goes away and comes back raises the alarm again.
        self.assertIsNone(watch.feed(b"\x1b[2J\x1b[HProceeding.\r\n"))
        self.assertEqual(watch.feed(b"".join(REAL_PROMPT_PIECES))[0], "waiting")

    def test_a_screen_clear_and_short_completion_output_clears_the_wait(self):
        """The reviewer's reproduction (2026-09-23): the first version kept alarming here."""
        watch = attention.PromptWatch()
        self.assertEqual(watch.feed(b"Do you want to proceed?\n1. Yes\n4. No\n")[0], "waiting")
        self.assertEqual(watch.feed(b"\x1b[2J\x1b[HDone. Work completed.\n"), ("cleared", None))
        self.assertIsNone(watch.reason)
        self.assertEqual(watch.screen.text().strip(), "Done. Work completed.")

    def test_a_cursor_up_redraw_that_overwrites_the_prompt_clears_the_wait(self):
        """How an Ink-style CLI repaints: move up over the old block, erase below, write anew."""
        watch = attention.PromptWatch()
        watch.feed(b"working...\r\nDo you want to proceed?\r\n1. Yes\r\n4. No\r\n")
        self.assertIsNotNone(watch.reason)
        self.assertEqual(watch.feed(b"\x1b[4A\x1b[JProceeding with the read.\r\n"), ("cleared", None))
        self.assertNotIn("proceed?", watch.screen.text())
        self.assertIn("Proceeding with the read.", watch.screen.text())

    def test_a_full_screen_redraw_that_rewrites_the_prompt_rows_clears_the_wait(self):
        """How a full-screen CLI repaints: absolute cursor positioning and erase-to-end-of-line."""
        watch = attention.PromptWatch()
        watch.feed(b"\x1b[?1049h\x1b[5;1HDo you want to proceed?\x1b[6;1H1. Yes\x1b[7;1H4. No")
        self.assertIsNotNone(watch.reason)
        self.assertEqual(watch.feed(b"\x1b[5;1H\x1b[KReading tests/test_media_policy.py\x1b[6;1H\x1b[K\x1b[7;1H\x1b[K"), ("cleared", None))
        self.assertNotIn("proceed?", watch.screen.text())
        # Leaving the alternate screen empties it too.
        watch.feed(b"\x1b[5;1HDo you want to proceed?")
        self.assertIsNotNone(watch.reason)
        self.assertEqual(watch.feed(b"\x1b[?1049l"), ("cleared", None))

    def test_a_prompt_split_across_reads_is_recognised_on_the_screen(self):
        watch = attention.PromptWatch()
        self.assertIsNone(watch.feed(b"Do you want to pro"))
        self.assertEqual(watch.feed(b"ceed?\r\n")[0], "waiting")
        # A multibyte character split across reads is not corrupted into a gap.
        watch = attention.PromptWatch()
        watch.feed(b"caf\xc3"); watch.feed(b"\xa9 USER ACTION: Needed")
        self.assertEqual(watch.screen.text().strip(), "caf\u00e9 USER ACTION: Needed")
        self.assertIsNotNone(watch.reason)


class ViewportTests(unittest.TestCase):
    """Round-2 reviewer findings: a control sequence split across reads, and no viewport."""

    def test_a_clear_screen_sequence_split_across_reads_still_clears_the_wait(self):
        watch = attention.PromptWatch()
        self.assertEqual(watch.feed(b"Do you want to proceed?\n1. Yes\n4. No\n")[0], "waiting")
        self.assertIsNone(watch.feed(b"\x1b[2"), "half a sequence is held back, not applied")
        self.assertIn("proceed?", watch.screen.text(), "and not applied as text either")
        # The second half alone completes the clear: the screen is empty at once,
        # before any later text could overwrite the prompt (reviewer's note, round 3).
        self.assertEqual(watch.feed(b"J"), ("cleared", None))
        self.assertEqual(watch.screen.text().strip(), "")
        self.assertIsNone(watch.reason)
        self.assertIsNone(watch.feed(b"\x1b[HDone. Work completed.\n"))
        # The same for an OSC title and a bare ESC at the read boundary.
        watch = attention.PromptWatch()
        watch.feed(b"Do you want to proceed?\n")
        watch.feed(b"\x1b]0;title")
        self.assertIsNotNone(watch.reason)
        self.assertEqual(watch.feed(b"\x07\x1b"), None)
        self.assertEqual(watch.feed(b"[2J\x1b[HDone.\n"), ("cleared", None))

    def test_ordinary_output_scrolls_the_prompt_off_a_screen_of_the_terminals_height(self):
        """The reviewer's second reproduction: 40 line breaks and completion text left the prompt flagged."""
        watch = attention.PromptWatch()
        self.assertEqual(watch.feed(b"Do you want to proceed?\n1. Yes\n4. No\n")[0], "waiting")
        self.assertEqual(watch.feed(b"\n" * 40 + b"Work completed.\n"), ("cleared", None))
        self.assertLessEqual(len(watch.screen.rows), attention.DEFAULT_HEIGHT)
        self.assertNotIn("proceed?", watch.screen.text())
        # The real terminal's height is what counts.
        watch = attention.PromptWatch(height=24)
        watch.feed(b"Do you want to proceed?\n")
        self.assertIsNone(watch.feed(b"line\n" * 20), "still on a 24-row screen")
        self.assertEqual(watch.feed(b"line\n" * 5), ("cleared", None))

    def test_a_resize_that_shrinks_the_screen_drops_the_top_rows(self):
        watch = attention.PromptWatch(height=40)
        watch.feed(b"Do you want to proceed?\n" + b"x\n" * 10)
        self.assertIsNotNone(watch.reason)
        self.assertEqual(watch.resize(8), ("cleared", None))
        self.assertEqual(len(watch.screen.rows), 8)
        self.assertIsNone(watch.resize(None), "an unknown size changes nothing")

    def test_the_supervisor_reads_the_real_terminal_height(self):
        import pty
        from harness import interactive_supervisor
        master, slave = pty.openpty()
        try:
            import fcntl, struct, termios
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 31, 100, 0, 0))
            self.assertEqual(interactive_supervisor._terminal_rows(slave), 31)
        finally:
            os.close(master); os.close(slave)
        self.assertIsNone(interactive_supervisor._terminal_rows(-1))


class RobustnessTests(unittest.TestCase):
    def test_huge_parameters_never_allocate_beyond_the_screen(self):
        """Reviewer finding, round 3: CSI 1000000 L allocated 16 MB before the height applied."""
        import tracemalloc
        sequences = (b"\x1b[1000000L", b"\x1b[1000000M", b"\x1b[1000000B", b"\x1b[1000000A", b"\x1b[1000000E",
                     b"\x1b[1000000F", b"\x1b[1000000C", b"\x1b[1000000D", b"\x1b[1000000d", b"\x1b[1000000G",
                     b"\x1b[999999999;999999999H", b"\x1b[99999999999999999999L", b"\x1b[1000000;1000000;1000000L")
        for sequence in sequences:
            watch = attention.PromptWatch(height=24)
            watch.feed(b"Do you want to proceed?\n")
            tracemalloc.start()
            watch.feed(sequence)
            _, peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
            self.assertLess(peak, 256 * 1024, f"{sequence!r} allocated {peak} bytes")
            self.assertLessEqual(len(watch.screen.rows), 24, sequence)
            self.assertLess(watch.screen.row, 24, sequence)
            self.assertLessEqual(watch.screen.col, attention.MAX_COLS, sequence)
            # The screen keeps working afterwards.
            watch.feed(b"\x1b[2J\x1b[HUSER ACTION: Needed\n")
            self.assertIsNotNone(watch.reason, sequence)

    def test_arbitrary_bytes_never_break_the_screen_model(self):
        """Whatever a CLI emits — partial escapes, junk, huge params — the watch keeps going."""
        import random
        rng = random.Random(20260923)
        watch = attention.PromptWatch(height=24)
        alphabet = [b"\x1b[", b"\x1b]", b"\x1b", b"\x07", b"\r", b"\n", b"\x08", b"\x0c", b";", b"?", b"H", b"J", b"K", b"A",
                    b"m", b"L", b"M", b"B", b"E", b"d", b"G", b"9999999", b"\xe2\x94\x80", b"\xc3", b"\xa9",
                    b"Do you want to ", b"proceed?", b"x" * 300]
        for _ in range(4000):
            chunk = b"".join(rng.choice(alphabet) for _ in range(rng.randint(1, 12)))
            watch.feed(chunk)
            self.assertLessEqual(len(watch.screen.rows), 24)
            self.assertLessEqual(watch.screen.row, 23)
        watch.feed(b"\x1b[2J\x1b[H")
        watch.owner_typed()
        self.assertIsNone(watch.reason)
        self.assertEqual(watch.screen.text().strip(), "")


class SessionRecordTests(unittest.TestCase):
    def test_attention_is_recorded_once_per_wait_cleared_and_listed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            session = control.create(root, "claude_cto")
            self.assertEqual((session["attention_since"], session["attention_reason"]), (None, None))
            first = control.record_attention(root, session["id"], "is asking permission to continue")
            self.assertTrue(first["attention_since"])
            time.sleep(0.01)
            again = control.record_attention(root, session["id"], "is asking permission to continue")
            self.assertEqual(again["attention_since"], first["attention_since"], "a repeated prompt keeps the original time")
            listed = control.waiting_sessions(root)
            self.assertEqual([item["id"] for item in listed], [session["id"]])
            self.assertEqual(listed[0]["attention_reason"], "is asking permission to continue")
            control.clear_attention(root, session["id"])
            self.assertEqual(control.waiting_sessions(root), [])
            with self.assertRaises(ValueError):
                control.record_attention(root, session["id"], "")


class SupervisorTests(unittest.TestCase):
    """The real supervisor over a real PTY: a child that prints the prompt and waits."""

    def _wait_for(self, predicate, timeout=8.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.05)
        return False

    def _read_until(self, master, needle: bytes, timeout=8.0) -> bytes:
        output, deadline = b"", time.monotonic() + timeout
        while needle not in output and time.monotonic() < deadline:
            readable, _, _ = select.select([master], [], [], .2)
            if readable:
                try:
                    output += os.read(master, 65536)
                except OSError:
                    break
        return output

    def test_a_waiting_prompt_marks_the_session_and_the_owners_answer_clears_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            session = control.create(root, "claude_cto")
            agent = board.register(root, "cto", "GLOBAL_MONITOR", vendor="Anthropic", session_id=session["id"])
            master, slave = pty.openpty()
            prompt = b"".join(REAL_PROMPT_PIECES)
            child = (
                "import os,tty,sys\n"
                "tty.setraw(0)\n"
                "os.write(1,b'CHILD_READY\\r\\n')\n"
                f"os.write(1,{prompt!r})\n"
                "answer=b''\n"
                "while b'\\r' not in answer and b'\\n' not in answer: answer += os.read(0,4096)\n"
                "os.write(1,b'ANSWERED:'+answer.strip()+b'\\r\\n')\n"
                "os.write(1,b'Proceeding with the read.\\r\\n')\n"
            )
            command = [
                "python3", str(ROOT / "harness" / "interactive_supervisor.py"), "--root", str(root),
                "--session-id", session["id"], "--agent-id", agent["id"], "--provider", "claude", "--",
                "python3", "-c", child,
            ]
            process = subprocess.Popen(command, stdin=slave, stdout=slave, stderr=slave, close_fds=True)
            os.close(slave)
            try:
                self._read_until(master, b"4. No")
                waiting = lambda: next(item for item in control.snapshot(root)["sessions"] if item["id"] == session["id"])
                self.assertTrue(self._wait_for(lambda: waiting()["attention_since"]), "the session was never marked waiting")
                self.assertIn("Do you want to proceed?", waiting()["attention_reason"])
                self.assertEqual([item["id"] for item in control.waiting_sessions(root)], [session["id"]])
                os.write(master, b"1\r")
                output = self._read_until(master, b"ANSWERED:")
                self.assertIn(b"ANSWERED:1", output)
                self.assertTrue(self._wait_for(lambda: not waiting()["attention_since"]), "the owner's answer did not clear the wait")
                process.wait(timeout=8)
            finally:
                if process.poll() is None:
                    process.terminate()
                process.wait(timeout=8)
                os.close(master)
            transcript = conversation.transcript_path(root, session["id"]).read_text(encoding="utf-8")
            self.assertIn("terminal is waiting for the owner", transcript)
            self.assertIn("terminal no longer waiting for the owner", transcript)


class RunnerPermissionTests(unittest.TestCase):
    """The real runner, fake CLIs: Claude never launches in a mode that asks."""

    def setUp(self):
        require_loopback()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "project"
        self.root.mkdir()
        self.capture = self.root / "captured.txt"
        self.claude_dir = Path(self.tmp.name) / "claude-config"
        self.environment = {
            **os.environ,
            "HARNESS_CAPTURE": str(self.capture),
            "HARNESS_CLAUDE_BIN": str(fake_cli(self.root / "fake-claude")),
            "HARNESS_CODEX_BIN": str(fake_cli(self.root / "fake-codex")),
            "CLAUDE_CONFIG_DIR": str(self.claude_dir),
            "CODEX_HOME": str(Path(self.tmp.name) / "codex-home"),
        }

    def argv(self) -> list[str]:
        return self.capture.read_text(encoding="utf-8").splitlines()

    def _granted_outside_execution_root(self) -> list[str]:
        roots = agent_writable_roots(str(self.root), str(self.root / ".harness"),
                                     str(self.root.parent / ".harness-task-workspaces"), str(self.root), None)
        return [root for root in roots if root != str(self.root.resolve())]

    def _add_dirs(self, argv: list[str]) -> list[str]:
        return [argv[index + 1] for index, item in enumerate(argv) if item == "--add-dir"]

    def test_claude_launches_fresh_and_resumed_with_bypass_mode_and_the_harness_roots(self):
        session = control.create(self.root, "claude_cto")
        completed = run_runner(self.root, session, self.environment)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        argv = self.argv()
        self.assertIn("--permission-mode", argv)
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "bypassPermissions")
        expected = self._granted_outside_execution_root()
        self.assertTrue(expected, "the grant must name the data and workspace roots")
        self.assertEqual(sorted(self._add_dirs(argv)), sorted(expected))
        self.assertIn("--session-id", argv)
        minted = argv[argv.index("--session-id") + 1]

        store = self.claude_dir / "projects" / "-project"; store.mkdir(parents=True)
        (store / f"{minted}.jsonl").write_text("{}\n", encoding="utf-8")
        stage_relaunch(self.root, session["id"])
        completed = run_runner(self.root, session, self.environment)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        argv = self.argv()
        self.assertIn("--resume", argv)
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "bypassPermissions",
                         "bypass is never restored on resume by the CLI, so the runner passes it every time")
        self.assertEqual(sorted(self._add_dirs(argv)), sorted(expected))

    def test_codex_launch_is_unchanged_and_never_gets_claude_flags(self):
        session = control.create(self.root, "codex_delivery")
        completed = run_runner(self.root, session, self.environment)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        argv = self.argv()
        self.assertIn("approval_policy=never", argv)
        self.assertNotIn("--permission-mode", argv)
        self.assertNotIn("--add-dir", argv)
        writable = next(item for item in argv if item.startswith("sandbox_workspace_write.writable_roots="))
        for root in self._granted_outside_execution_root():
            self.assertIn(root, writable)


LANDING_PROBE = r"""
<script>
(async () => {
  for (let attempt = 0; attempt < 60; attempt++) {
    const banner = document.querySelector('#waiting-banner');
    if (banner && !banner.hidden && banner.textContent.trim()) break;
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  const banner = document.querySelector('#waiting-banner');
  const visible = node => node && node.getBoundingClientRect().width > 0 && node.getBoundingClientRect().height > 0;
  const badge = document.querySelector('.badge.waiting');
  await fetch('/__probe__', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({
    bannerVisible: visible(banner), bannerText: banner ? banner.innerText : null,
    animation: banner ? getComputedStyle(banner).animationName : null,
    badgeVisible: visible(badge), badgeText: badge ? badge.textContent : null,
  })});
})();
</script>
"""

STUDIO_PROBE = r"""
<script>
(async () => {
  for (let attempt = 0; attempt < 60; attempt++) {
    const banner = document.querySelector('#waiting-banner');
    if (banner && !banner.hidden && banner.textContent.trim()) break;
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  const banner = document.querySelector('#waiting-banner');
  const visible = node => node && node.getBoundingClientRect().width > 0 && node.getBoundingClientRect().height > 0;
  const badge = document.querySelector('.badge.tone-waiting');
  await fetch('/__probe__', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({
    bannerVisible: visible(banner), bannerText: banner ? banner.innerText : null,
    animation: banner ? getComputedStyle(banner).animationName : null,
    badgeVisible: visible(badge), badgeText: badge ? badge.textContent : null,
  })});
})();
</script>
"""


class RenderedBannerTests(unittest.TestCase):
    def setUp(self):
        from harness import browser_acceptance
        try:
            browser_acceptance.resolve_binary()
        except (FileNotFoundError, ValueError) as error:
            raise unittest.SkipTest(str(error)) from error
        require_loopback()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)

    def _render(self, target_url: str, script: str) -> dict:
        from harness import browser_acceptance
        sink: dict = {}
        proxy = ThreadingHTTPServer(("127.0.0.1", 0), probe_proxy(target_url, sink, script))
        threading.Thread(target=proxy.serve_forever, daemon=True).start()
        self.addCleanup(proxy.server_close)
        self.addCleanup(proxy.shutdown)
        profile = tempfile.TemporaryDirectory()
        self.addCleanup(profile.cleanup)
        process = browser_acceptance.launch(f"http://127.0.0.1:{proxy.server_address[1]}/", Path(profile.name), width=1280, height=900)
        try:
            deadline = time.monotonic() + 45
            while time.monotonic() < deadline and "value" not in sink:
                time.sleep(0.1)
        finally:
            process.close()
        self.assertIn("value", sink, "Chrome reported nothing")
        return sink["value"]

    def _serve(self, handler) -> str:
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return f"http://127.0.0.1:{server.server_address[1]}"

    def test_projects_page_blinks_a_banner_naming_the_project_and_agent_while_one_waits(self):
        home = self.base / "home"
        code = self.base / "film"; code.mkdir()
        entry = registry.register(home, "Film project", code)
        context = registry.context_for_entry(entry)
        control.initialize(context)
        session = control.create(context, "claude_cto")
        control.record_attention(context, session["id"], "is asking permission to continue ('Do you want to proceed?')")
        manager = project_manager.ProjectManager(home, board_port=0)
        listed = manager.projects_payload()["projects"][0]
        self.assertEqual(listed["waiting_sessions"][0]["role"], "CTO")
        reading = self._render(self._serve(project_manager.make_handler(manager)), LANDING_PROBE)
        self.assertTrue(reading["bannerVisible"], reading)
        self.assertIn("waiting for you", reading["bannerText"])
        self.assertIn("Film project", reading["bannerText"])
        self.assertIn("CTO", reading["bannerText"])
        self.assertIn("Do you want to proceed?", reading["bannerText"])
        self.assertEqual(reading["animation"], "waiting-blink")
        self.assertTrue(reading["badgeVisible"], reading)
        self.assertEqual(reading["badgeText"], "Waiting for you")

    def test_projects_page_shows_no_banner_when_nothing_waits(self):
        home = self.base / "home"
        code = self.base / "quiet"; code.mkdir()
        registry.register(home, "Quiet project", code)
        manager = project_manager.ProjectManager(home, board_port=0)
        self.assertEqual(manager.projects_payload()["projects"][0]["waiting_sessions"], [])
        probe = LANDING_PROBE.replace("attempt < 60", "attempt < 8")
        reading = self._render(self._serve(project_manager.make_handler(manager)), probe)
        self.assertFalse(reading["bannerVisible"], reading)
        self.assertFalse(reading["badgeVisible"], reading)

    def test_studio_board_blinks_a_banner_and_badges_the_waiting_agent(self):
        root = self.base / "studio"; root.mkdir()
        session = control.create(root, "claude_cto")
        board.register(root, "cto", "GLOBAL_MONITOR", vendor="Anthropic", session_id=session["id"])
        control.record_attention(root, session["id"], "says it needs you to act ('USER ACTION: Needed')")
        reading = self._render(self._serve(board_viewer.make_handler(root)), STUDIO_PROBE)
        self.assertTrue(reading["bannerVisible"], reading)
        self.assertIn("waiting for you", reading["bannerText"])
        self.assertIn("USER ACTION: Needed", reading["bannerText"])
        self.assertEqual(reading["animation"], "waiting-blink")
        self.assertTrue(reading["badgeVisible"], reading)
        self.assertEqual(reading["badgeText"], "WAITING FOR YOU")


if __name__ == "__main__":
    unittest.main()
