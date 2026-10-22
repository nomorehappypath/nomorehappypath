# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Real processes against the version 2 machine lock rule (windows-walk item 1; reference in tests/machine_lock_reference.py).

Machine inputs removed: nothing is read from the machine (a private temporary folder, real processes, no ssh).
The Windows PowerShell form is NOT exercised: neither test machine has PowerShell; the owner's Windows walk is its proof.

Run:  PYTHONPATH=. python3 -m unittest tests.test_machine_lock_semantics -v
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = r"""
import os, subprocess, sys, time
sys.path.insert(0, {root!r})
from pathlib import Path
from tests import machine_lock_reference as lock
folder = Path({folder!r})
mode = {mode!r}
try:
    held = lock.acquire(folder, {agent!r}, "reviewer", wait={wait})
except lock.Held as error:
    print("HELD " + str(error), flush=True); sys.exit(3)
print("ACQUIRED", flush=True)
if mode == "child":
    # a surviving child started the prescribed way (Python subprocess: descriptors are not passed)
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    print("CHILD %d" % child.pid, flush=True)
if mode in ("child", "hold"):
    time.sleep(60)
if mode == "quick":
    time.sleep({hold})
"""


class MachineLockSemanticsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name) / ".lock-v2"
        self.processes: list[subprocess.Popen] = []
        self.addCleanup(self.reap)

    def reap(self):
        for process in self.processes:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=10)
            if process.stdout:
                process.stdout.close()
        for pid in getattr(self, "children", []):
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass

    def run_script(self, mode: str, agent: str = "reviewer-1", wait: float = 0.5, hold: float = 0.2) -> subprocess.Popen:
        code = SCRIPT.format(root=str(ROOT), folder=str(self.folder), mode=mode, agent=agent, wait=wait, hold=hold)
        process = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, cwd=ROOT,
                                   env={**os.environ, "PYTHONPATH": str(ROOT)})
        self.processes.append(process)
        return process

    def first_line(self, process: subprocess.Popen) -> str:
        return process.stdout.readline().strip()

    def test_a_killed_holder_never_blocks_anyone_even_its_own_agent_and_a_live_one_is_not_taken_over(self):
        holder = self.run_script("hold", agent="qa-0001")
        self.assertEqual(self.first_line(holder), "ACQUIRED")
        # a live holder: the contender waits the bounded time and fails naming it from holder.txt
        started = time.monotonic()
        contender = self.run_script("quick", agent="qa-0001", wait=0.4)
        line = self.first_line(contender)
        contender.wait(timeout=20)
        self.assertTrue(line.startswith("HELD v2 qa-0001 reviewer pid="), line)
        self.assertLess(time.monotonic() - started, 15, "the wait is bounded")
        self.assertEqual(contender.returncode, 3)
        # the holder dies without releasing (the incident: its script died holding the lock)
        holder.kill()
        holder.wait(timeout=10)
        again = self.run_script("quick", agent="qa-0001", wait=0.4)
        self.assertEqual(self.first_line(again), "ACQUIRED", "its own agent id gets the lock at once; the OS freed it")

    def test_a_killed_supervisor_with_a_surviving_python_child_frees_the_lock(self):
        holder = self.run_script("child")
        self.assertEqual(self.first_line(holder), "ACQUIRED")
        child = int(self.first_line(holder).split()[1])
        self.children = [child]
        holder.kill()
        holder.wait(timeout=10)
        os.kill(child, 0)                  # the child really survived the supervisor
        nxt = self.run_script("quick", wait=0.4)
        self.assertEqual(self.first_line(nxt), "ACQUIRED", "a subprocess child does not keep the lock descriptor")

    def test_a_kill_between_taking_the_lock_and_publishing_the_note_leaves_only_debris_that_is_overwritten(self):
        self.folder.mkdir(parents=True)
        (self.folder / "holder.txt").write_text("v2 half-writt")     # a truncated note from a run that died mid-publication
        process = self.run_script("quick")
        self.assertEqual(self.first_line(process), "ACQUIRED")
        process.wait(timeout=10)
        self.assertTrue((self.folder / "holder.txt").read_text().startswith("v2 reviewer-1 reviewer pid="))
        self.assertFalse(list(self.folder.glob(".holder.txt.*")), "no temporary file is left")

    def test_twelve_contenders_at_first_creation_exactly_one_holds_at_a_time(self):
        contenders = [self.run_script("quick", agent=f"agent-{i}", wait=0.05, hold=1.5) for i in range(12)]
        lines = [self.first_line(process) for process in contenders]
        winners = [line for line in lines if line == "ACQUIRED"]
        losers = [line for line in lines if line.startswith("HELD ")]
        self.assertEqual(len(winners), 1, lines)
        self.assertEqual(len(losers), 11, lines)
        self.assertTrue(all("v2 agent-" in line or "not published" in line for line in losers), lines)

    def test_anything_but_contention_fails_at_once_with_its_real_cause(self):
        from tests import machine_lock_reference as lock
        blocked = Path(self.tmp.name) / "a-file"
        blocked.write_text("not a folder")
        started = time.monotonic()
        with self.assertRaises(OSError) as caught:
            lock.acquire(blocked / "-v2", "a", "reviewer", wait=30)
        self.assertNotIsInstance(caught.exception, lock.Held)
        self.assertLess(time.monotonic() - started, 5, "no waiting for a permanent failure")

    def test_cleanup_style_deletion_of_the_folder_is_not_part_of_the_rule(self):
        # the rule: the lock file and its folder are never deleted, renamed or replaced by cleanup; the reference never does it
        source = (ROOT / "tests" / "machine_lock_reference.py").read_text()
        for word in ("rmtree", "unlink", "os.remove", "rmdir"):
            self.assertNotIn(word, source)


if __name__ == "__main__":
    unittest.main()
