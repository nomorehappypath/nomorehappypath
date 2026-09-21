# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
"""A group signal must never be addressed by a recycled number.

`harness/release_preview.py` used to run `kill -TERM -<pid>`. The leading minus
addresses a process GROUP by number, which is only safe while that number still
belongs to the process we started AND that process leads the group. Neither was
checked. On Linux — where pids recycle quickly — the signal landed on whoever
held the number next: it killed the test runner mid-suite, so the suite ended
with no verdict at all, and twice it killed the operator's SSH session.

Reproduced before the fix by shimming `kill` on the target host:

    KILL CALLED: -TERM -180743   (caller pgid=180724)
    KILL CALLED: -KILL -180743

with the child already exited. Shimmed, the test merely failed; unshimmed, the
shell died.
"""
from __future__ import annotations

import os
import re
import signal
import subprocess
import sys
import time
import unittest

from harness import platform_support


def identity():
    return platform_support.process_identity()


class TerminateGroupTests(unittest.TestCase):
    def _sleeper(self, *, own_session: bool):
        process = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            start_new_session=own_session,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        self.addCleanup(self._reap, process)
        return process

    @staticmethod
    def _reap(process):
        if process.poll() is None:
            process.kill()
        try:
            process.wait(timeout=5)
        except Exception:
            pass

    def test_a_live_group_leader_is_signalled(self):
        process = self._sleeper(own_session=True)
        self.assertEqual(os.getpgid(process.pid), process.pid, "fixture is not a group leader")
        self.assertTrue(identity().terminate_group(process, signal.SIGTERM))
        deadline = time.monotonic() + 5
        while process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertIsNotNone(process.poll(), "the leader's group was not signalled")

    def test_a_child_that_is_not_a_group_leader_is_refused(self):
        """The number would name somebody else's group."""
        process = self._sleeper(own_session=False)
        if os.getpgid(process.pid) == process.pid:
            self.skipTest("this child happens to lead its group; nothing to refuse")
        self.assertFalse(identity().terminate_group(process, signal.SIGTERM))
        self.assertIsNone(process.poll(), "a non-leader was signalled anyway")

    def test_an_exited_child_is_refused(self):
        """The pid-reuse race: the number may already belong to someone else."""
        process = subprocess.Popen([sys.executable, "-c", "pass"])
        process.wait(timeout=10)
        self.assertFalse(identity().terminate_group(process, signal.SIGKILL))

    def test_refusal_is_not_an_error(self):
        process = subprocess.Popen([sys.executable, "-c", "pass"])
        process.wait(timeout=10)
        self.assertIs(identity().terminate_group(process, signal.SIGTERM), False)


NEGATIVE_TARGET = re.compile(
    r"""
      f"-\{                      # f"-{pid}" — a group addressed by number
    | os\.kill\(\s*-           # os.kill(-pid, ...)
    | kill\s+-(?:TERM|KILL|9)\s+-   # shelled: kill -TERM -<pid>
    """,
    re.VERBOSE,
)


class NoGroupSignalByRecoveredNumberTests(unittest.TestCase):
    """The source rule, so the pattern cannot come back somewhere else."""

    def test_no_module_shells_out_to_kill_with_a_negative_pid(self):
        from pathlib import Path
        # The WHOLE repository. Scanning harness/ alone is how this defect
        # survived: production code was cleaned, tests/ stayed exempt, and the
        # offending teardown there killed the runner that was meant to prove
        # the fix. A rule that does not apply to test code is not a rule.
        root = Path(__file__).resolve().parents[1]
        offenders = []
        seen = []
        scanned = 0
        for path in sorted(root.rglob("*.py")):
            if ".git" in path.parts or path.name == Path(__file__).name:
                continue
            body = path.read_text(encoding="utf-8", errors="ignore")
            name = path.relative_to(root).as_posix()
            scanned += 1
            seen.append(name)
            if NEGATIVE_TARGET.search(body):
                offenders.append(name)
        # State the reach. A guard that reports safety over ground it never
        # looked at buys false confidence: the harness/-only version of this
        # rule reported green while the offending line sat in tests/ and killed
        # the runner meant to prove the earlier fix.
        self.assertGreater(scanned, 100,
                           f"the guard only scanned {scanned} files; its reach has shrunk")
        self.assertTrue(any(name.startswith("tests/") for name in seen),
                        "the guard is not looking at tests/ — that is where the defect was")
        self.assertTrue(any(name.startswith("harness/") for name in seen),
                        "the guard is not looking at harness/")
        self.assertEqual(offenders, [],
                         f"a group signal addressed by number is back "
                         f"(scanned {scanned} files); use terminate_group")


class RecordedPidPathTests(unittest.TestCase):
    """The path with no handle to prove liveness — and no test, until now.

    `_reconcile_recorded_processes` cleans up preview processes left by a
    PREVIOUS worker. It has only a pid and a start token from the board, so it
    cannot hold a Popen handle. Review found the first version of this fix
    raising `NameError: name 'os' is not defined` there — the module had no
    `os` import and the whole path was uncovered, so both full suites passed
    over a routine that could not run at all.

    These cover it: it must signal, it must not raise, and it must never
    address a GROUP by that number.
    """

    def setUp(self):
        from harness import board, release_preview
        self.release_preview = release_preview
        self.board = board
        temporary = __import__("tempfile").TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = __import__("pathlib").Path(temporary.name)

    def _recorded_child(self):
        """A live child recorded on the board the way a previous worker would."""
        process = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        self.addCleanup(lambda: (process.kill(), process.wait(timeout=5)) if process.poll() is None else None)
        token = self.release_preview._start_token(process.pid)
        self.assertTrue(token, "fixture could not read a start token")
        with self.board.locked_state(self.root) as state:
            state.setdefault("releases", {})["TASK"] = {
                "task": "TASK", "status": "VISUAL_TEST_REQUIRED",
                "cto_id": "cto-0001-test", "recorded_at": self.board.now(),
                "preview": {"pid": process.pid, "start_token": token, "status": "ready"},
            }
        return process

    def test_a_recorded_process_is_signalled_without_raising(self):
        process = self._recorded_child()
        supervisor = self.release_preview.ReleasePreviewSupervisor(self.root)
        releases = self.board.snapshot(self.root)["releases"]
        supervisor._reconcile_recorded_processes(releases)      # must not raise
        deadline = time.monotonic() + 5
        while process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertIsNotNone(process.poll(), "the recorded process was never signalled")

    def test_the_caller_survives_that_reconciliation(self):
        """It must not address a GROUP by that number.

        An earlier version of this test asserted `True` — reaching the line WAS
        the test. I flagged it and the reviewer confirmed it: a test that
        cannot fail for the right reason. It now watches a SENTINEL child that
        shares this process's group. If the reconciliation signalled the group
        rather than the pid, the sentinel dies and this fails; the old version
        could only have failed by killing the runner outright.
        """
        sentinel = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )                                    # no start_new_session: OUR group
        self.addCleanup(self._reap_sentinel, sentinel)
        self.assertEqual(os.getpgid(sentinel.pid), os.getpgid(0),
                         "sentinel must share the caller's process group to be meaningful")

        process = self._recorded_child()
        supervisor = self.release_preview.ReleasePreviewSupervisor(self.root)
        supervisor._reconcile_recorded_processes(self.board.snapshot(self.root)["releases"])

        time.sleep(0.3)
        self.assertIsNone(sentinel.poll(),
                          "the caller's process group was signalled; only the recorded pid may be")

    @staticmethod
    def _reap_sentinel(process):
        if process.poll() is None:
            process.kill()
        try:
            process.wait(timeout=5)
        except Exception:
            pass

    def test_a_stale_token_is_left_alone(self):
        process = self._recorded_child()
        with self.board.locked_state(self.root) as state:
            state["releases"]["TASK"]["preview"]["start_token"] = "not the real token"
        supervisor = self.release_preview.ReleasePreviewSupervisor(self.root)
        supervisor._reconcile_recorded_processes(self.board.snapshot(self.root)["releases"])
        time.sleep(0.3)
        self.assertIsNone(process.poll(),
                          "a pid whose token does not match must never be signalled")


if __name__ == "__main__":
    unittest.main()
