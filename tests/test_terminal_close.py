# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
"""The auto-close that never closed anything.

`--close-terminal-on-exit` silently did nothing. Not an error, not a report —
three independent ways to fail invisibly, stacked:

1. The closer runs from the supervisor's own `finally`, so the supervisor is
   still running in the tab it asks Terminal to close. `close` on a busy tab
   raises "Do you want to terminate the running processes?", a sheet no
   unattended script can answer — and AppleScript reports success anyway.
2. Once the shell exits, the tty is released, so matching by tty AFTER waiting
   can never find the window again.
3. When the shell exits, Terminal keeps the window but EMPTIES its tab
   collection, so every `busy` query errors from then on — and the old code
   swallowed that in a bare `try`.
"""
from __future__ import annotations

import unittest
from pathlib import Path

def _closer_script() -> str:
    """Follow the script, which moved into the seam when TerminalHost landed.

    This is the reconciliation those two branches require: one FIXED the
    script in place, the other MOVED it. A test pinned to one location
    silently stops checking anything once the other lands.
    """
    base = Path(__file__).resolve().parent.parent
    marker = "script = r" + chr(39) * 3
    end = chr(39) * 3
    supervisor = (base / "harness" / "interactive_supervisor.py").read_text(encoding="utf-8")
    if marker in supervisor:
        return supervisor.split(marker)[1].split(end)[0]
    seam = (base / "harness" / "platform_support" / "defaults.py").read_text(encoding="utf-8")
    return seam.split("DISMISS_SCRIPT = r" + chr(39) * 3)[1].split(end)[0]


SCRIPT = _closer_script()


class CloserScriptTests(unittest.TestCase):
    def test_identity_is_resolved_before_the_wait_not_after(self):
        """The tty is only valid while the session lives."""
        resolve = SCRIPT.index("set targetId to id of terminalWindow")
        wait = SCRIPT.index("repeat 40 times")
        self.assertLess(resolve, wait,
                        "identity must be captured before waiting; after the shell exits "
                        "the tty is released and the window can never be found again")

    def test_it_waits_on_the_condition_rather_than_a_fixed_delay(self):
        self.assertNotIn("delay 0.5", SCRIPT,
                         "a fixed delay races the supervisor's own exit and loses")
        self.assertIn("busy of tab 1 of window id targetId", SCRIPT)

    def test_an_emptied_tab_collection_counts_as_finished(self):
        """No tab means nothing is running — the one time closing is certainly safe."""
        self.assertIn("set readyToClose to true", SCRIPT)
        self.assertIn("on error", SCRIPT)

    def test_it_gives_up_rather_than_terminating_a_running_process(self):
        """Forcing the close would kill work the owner can see."""
        self.assertIn('return "still busy; window left open"', SCRIPT)
        self.assertNotIn("saving no", SCRIPT)
        self.assertNotIn("do shell script", SCRIPT)

    def test_windows_are_addressed_by_identity_never_by_position(self):
        """Positional indexing hit the owner's own live session during diagnosis."""
        self.assertNotIn("close window 1", SCRIPT)
        self.assertIn("close window id targetId", SCRIPT)

    def test_a_tty_matching_nothing_is_reported_not_guessed(self):
        self.assertIn('return "no matching window"', SCRIPT)


if __name__ == "__main__":
    unittest.main()
