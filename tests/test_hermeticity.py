# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
"""The suite may never write the owner's real provider configuration.

`tests/__init__` pins CODEX_HOME so a test cannot reach `~/.codex`. That pin is
a mechanism; this is the assertion. It exists because the mechanism was added
after an incident - a hundred trust entries written into the owner's real
config across three suite runs - and because a reviewer can only observe the
file's hash on a live machine, where the owner's own Codex CLI is also running
and legitimately writes it. A hash is therefore ambiguous evidence. The
signature of a leaking suite is not, and this asserts on the signature: no
entry naming a temporary directory may ever appear in the real file.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import tests
from harness import board_client, project_context, project_memory

# Only paths a test could have produced. Real project directories - including
# the owner's task and review clones, which the owner's own CLI records
# legitimately - must never be flagged here.
TEST_ONLY_MARKERS = ("harness-tests-codex-home", "/var/folders/", "/private/var/folders/")


class SharedTempDirectoryTests(unittest.TestCase):
    """The 2026-09-21 incident: the suite filled the production box's /tmp.

    Everything a test can leave behind must resolve inside the suite's private
    run root, so that removing the root at exit removes all of it. This names
    each class that was found littering the box, not only the one that
    exhausted the inodes.
    """

    def setUp(self):
        self.run_root = getattr(tests, "RUN_ROOT", None)
        self.assertIsNotNone(self.run_root, "tests/__init__ pins no private run root")
        self.run_root = Path(self.run_root).resolve()
        self.system_temp = Path(tests.SYSTEM_TEMP).resolve()

    def assert_inside_run_root(self, path: Path, what: str):
        resolved = Path(path).resolve(strict=False)
        self.assertTrue(
            resolved.is_relative_to(self.run_root),
            f"{what} escapes the suite's private root: {resolved} is not under {self.run_root}",
        )

    def test_the_run_root_is_private_to_this_process_and_below_the_system_temp(self):
        self.assertEqual(self.run_root.parent, self.system_temp)
        self.assertEqual(self.run_root.name, f"{tests.RUN_ROOT_PREFIX}{os.getpid()}")
        self.assertTrue(self.run_root.is_dir())
        self.assertEqual(Path(tempfile.gettempdir()).resolve(), self.run_root)
        self.assertEqual(Path(os.environ["TMPDIR"]).resolve(), self.run_root)

    def test_every_sibling_a_compatibility_project_creates_stays_inside_the_run_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            project.mkdir()
            context = project_context.project_context(project)
            self.assert_inside_run_root(project, "the project itself")
            self.assert_inside_run_root(context.board_backup_root, "board backups")
            self.assert_inside_run_root(project_memory.external_backup_root(context), "memory backups")
            self.assert_inside_run_root(context.workspace_root, "task workspaces")
        self.assert_inside_run_root(
            Path(tempfile.gettempdir()) / "harness-board-nonces", "the board-client nonce journal",
        )
        self.assert_inside_run_root(Path(os.environ["CODEX_HOME"]), "the pinned CODEX_HOME")

    def test_the_board_client_nonce_journal_is_written_inside_the_run_root(self):
        board_client._nonce("hermeticity-probe")
        journal = self.run_root / "harness-board-nonces"
        self.assertTrue(journal.is_dir(), f"nonce journal not under the run root: {journal}")

    def test_a_subprocess_inherits_the_run_root(self):
        completed = subprocess.run(
            [sys.executable, "-c", "import tempfile; print(tempfile.gettempdir())"],
            capture_output=True, text=True, check=True,
        )
        self.assertEqual(Path(completed.stdout.strip()).resolve(), self.run_root)

    def test_a_killed_run_is_swept_by_the_next_one_and_a_live_run_is_not(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_system_temp = Path(tmp)
            victim = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
            victim.kill()
            victim.wait()
            dead = fake_system_temp / f"{tests.RUN_ROOT_PREFIX}{victim.pid}"
            (dead / "litter").mkdir(parents=True)
            alive = fake_system_temp / f"{tests.RUN_ROOT_PREFIX}{os.getpid()}"
            alive.mkdir()
            unrelated = fake_system_temp / f"{tests.RUN_ROOT_PREFIX}not-a-pid"
            unrelated.mkdir()
            plain = fake_system_temp / "tmpsomething"
            plain.mkdir()

            removed = tests._sweep_dead_run_roots(fake_system_temp)

            self.assertEqual(removed, [dead])
            self.assertFalse(dead.exists(), "the dead run's root must be removed")
            self.assertTrue(alive.exists(), "a live run's root must be left alone")
            self.assertTrue(unrelated.exists(), "a name without a pid is not ours")
            self.assertTrue(plain.exists(), "anything else in the temp directory is not ours")


class HermeticityTests(unittest.TestCase):
    def test_codex_home_is_pinned_away_from_the_real_home(self):
        pinned = os.environ.get("CODEX_HOME", "")
        self.assertTrue(pinned, "CODEX_HOME is not pinned; tests/__init__ did not run")
        self.assertTrue(
            Path(pinned).resolve().is_relative_to(Path(tempfile.gettempdir()).resolve()),
            f"CODEX_HOME must point inside the temporary directory, not {pinned!r}",
        )
        self.assertFalse(
            Path(pinned).resolve().is_relative_to(Path.home().resolve()),
            "CODEX_HOME points inside the owner's real home",
        )

    def test_the_real_codex_config_carries_no_test_written_entry(self):
        """The invariant the 2026-08-21 incident violated, stated as a test."""
        real = Path(os.path.expanduser("~/.codex/config.toml"))
        if not real.is_file():
            self.skipTest("no real codex configuration on this machine")
        try:
            content = real.read_text(encoding="utf-8", errors="replace")
        except OSError as error:            # unreadable is not evidence of a leak
            self.skipTest(f"real codex configuration unreadable: {error}")
        leaked = sorted({
            line.strip() for line in content.splitlines()
            if any(marker in line for marker in TEST_ONLY_MARKERS)
        })
        self.assertEqual(
            leaked, [],
            "the suite wrote temporary-directory entries into the owner's real "
            "codex configuration: " + "; ".join(leaked),
        )


if __name__ == "__main__":
    unittest.main()
