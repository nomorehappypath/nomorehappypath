# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Process identity from /proc.

Hermetic: every test builds a FAKE /proc tree. An earlier test of mine called
the real `ps` and failed in the reviewer's shell where `ps` was denied — a test
that reports the environment, not the behaviour.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from harness.platform_support import linux


def plant(root: Path, pid: int, *, comm: str = "bash", ppid: int = 1,
          pgid: int = 0, start_ticks: str = "12345", cmdline: str = "/bin/bash\x00-c\x00echo hi") -> None:
    """Write a /proc/<pid> entry in the real kernel's field order."""
    d = root / str(pid)
    d.mkdir(parents=True)
    state_onwards = f"S {ppid} {pgid or pid} " + " ".join(["0"] * 16) + f" {start_ticks} " + " ".join(["0"] * 30)
    (d / "stat").write_text(f"{pid} ({comm}) {state_onwards}\n", encoding="utf-8")
    (d / "cmdline").write_bytes(cmdline.encode("utf-8"))


class ShapeTests(unittest.TestCase):
    """The mapping shape is duck-typed by three consumers and must not drift."""

    def test_it_produces_exactly_the_keys_the_ps_reader_produced(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plant(root, 100, ppid=1, pgid=100, start_ticks="999")
            with mock.patch.object(linux.PROCESS_IDENTITY, "PROC", root):
                table = linux.PROCESS_IDENTITY.process_table()
        self.assertEqual(set(table[100]), {"pid", "ppid", "pgid", "start_token", "command"})
        self.assertEqual(table[100]["ppid"], 1)
        self.assertEqual(table[100]["pgid"], 100)
        self.assertEqual(table[100]["start_token"], "999")
        self.assertEqual(table[100]["command"], "/bin/bash -c echo hi")


class HostileCommTests(unittest.TestCase):
    """A process can name itself anything, including its own field separators."""

    def test_a_comm_containing_spaces_and_parens_does_not_shift_every_field(self):
        """`(evil) 1 2 3` splits from the left into total nonsense.

        This is not theoretical: comm is attacker-controlled by any process that
        can call prctl. Parsing from the LAST ')' is what makes it safe.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plant(root, 200, comm="evil) 1 2 3 (nested", ppid=7, pgid=200, start_ticks="4242")
            with mock.patch.object(linux.PROCESS_IDENTITY, "PROC", root):
                table = linux.PROCESS_IDENTITY.process_table()
        self.assertEqual(table[200]["ppid"], 7, "comm shifted the parent pid")
        self.assertEqual(table[200]["pgid"], 200, "comm shifted the process group")
        self.assertEqual(table[200]["start_token"], "4242", "comm shifted the start token")


class LivenessTests(unittest.TestCase):
    def test_a_process_that_exits_mid_scan_is_skipped_not_fatal(self):
        """Listing then reading is inherently racy; that is normal, not an error."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plant(root, 300)
            (root / "301").mkdir()          # a pid dir with no stat: already gone
            with mock.patch.object(linux.PROCESS_IDENTITY, "PROC", root):
                table = linux.PROCESS_IDENTITY.process_table()
        self.assertIn(300, table)
        self.assertNotIn(301, table)

    def test_a_missing_pid_yields_an_EMPTY_token_not_an_exception(self):
        """An empty token is the ANSWER 'that pid is gone', as on macOS."""
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(linux.PROCESS_IDENTITY, "PROC", Path(tmp)):
                self.assertEqual(linux.PROCESS_IDENTITY.start_token(99999), "")

    def test_an_unreadable_proc_is_the_NAMED_failure(self):
        """ProcessTableUnavailable derives from OSError so existing handlers catch it."""
        missing = Path("/nonexistent-proc-for-this-test")
        with mock.patch.object(linux.PROCESS_IDENTITY, "PROC", missing):
            with self.assertRaises(linux.ProcessTableUnavailable):
                linux.PROCESS_IDENTITY.process_table()
        self.assertTrue(issubclass(linux.ProcessTableUnavailable, OSError))

    def test_a_kernel_thread_with_no_cmdline_still_appears(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plant(root, 400, comm="kworker/0:1", cmdline="")
            with mock.patch.object(linux.PROCESS_IDENTITY, "PROC", root):
                table = linux.PROCESS_IDENTITY.process_table()
        self.assertIn(400, table)


class ParentTests(unittest.TestCase):
    def test_the_parent_comes_from_proc_with_no_subprocess(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plant(root, 500, ppid=42)
            with mock.patch.object(linux.PROCESS_IDENTITY, "PROC", root):
                self.assertEqual(linux.PROCESS_IDENTITY.parent_process_id(500), 42)

    def test_a_vanished_child_reports_no_parent_rather_than_guessing(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(linux.PROCESS_IDENTITY, "PROC", Path(tmp)):
                self.assertEqual(linux.PROCESS_IDENTITY.parent_process_id(99999), 0)


if __name__ == "__main__":
    unittest.main()


class EmptyProcTests(unittest.TestCase):
    """An empty table is unreadable, not 'nothing is running'.

    This process is always in a readable /proc, so an empty result can only mean
    the reader could not see. Returning {} reports that as SUCCESS — and the
    release gate proves process ownership by reading this table, so a pass built
    on an empty one would be false. A sandbox with /proc masked as an empty
    tmpfs produces exactly this; measured on the target host, zero pid entries.
    """

    def test_an_empty_proc_is_a_named_refusal_not_an_empty_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(linux.PROCESS_IDENTITY, "PROC", Path(tmp)):
                with self.assertRaises(linux.ProcessTableUnavailable) as caught:
                    linux.PROCESS_IDENTITY.process_table()
        self.assertIn("unreadable", str(caught.exception))

    def test_a_proc_with_only_non_numeric_entries_is_also_a_refusal(self):
        """/proc carries plenty of non-pid entries; none of them is a process."""
        with tempfile.TemporaryDirectory() as tmp:
            for name in ("cpuinfo", "meminfo", "self"):
                (Path(tmp) / name).mkdir()
            with mock.patch.object(linux.PROCESS_IDENTITY, "PROC", Path(tmp)):
                with self.assertRaises(linux.ProcessTableUnavailable):
                    linux.PROCESS_IDENTITY.process_table()
