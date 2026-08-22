# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
"""Board durability & wipe recovery — the board must never be silently destroyed.

The load-bearing simulation is test_full_wipe_recovers_from_backup: it reproduces
the real incident (a full `.harness` deletion, as `git clean -fdx` in the board's
repo would cause) and proves the board comes back from the external backup with a
loud recovery marker. test_fresh_board_no_false_recovery guards the inverse.

Run:  PYTHONPATH=. python3 -m unittest tests.test_board_durability -v
"""
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from harness import board


def _events_path(root: Path) -> Path:
    return root / ".harness" / "board" / "events.jsonl"


def _event_count(root: Path) -> int:
    p = _events_path(root)
    return len([l for l in p.read_text().splitlines() if l.strip()]) if p.is_file() else 0


def _make_history(root: Path, ticks: int) -> str:
    """Generate board events and return the agent id (present in every backup)."""
    agent = board.register(root, "cto", "GLOBAL_MONITOR", vendor="Anthropic")["id"]
    for i in range(ticks):
        board.status(root, agent, f"tick {i}")
    return agent


class BoardDurability(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.root = self.base / "proj"
        self.root.mkdir()

    def _backups(self) -> Path:
        return self.root.parent / ".harness-board-backups" / self.root.name

    # ---- S-DUR-001 ----
    def test_event_log_is_append_only(self):
        agent = board.register(self.root, "cto", "GLOBAL_MONITOR", vendor="Anthropic")["id"]
        counts = []
        for i in range(6):
            board.status(self.root, agent, f"tick {i}")
            counts.append(_event_count(self.root))
        self.assertEqual(counts, sorted(counts), "event log line count must never shrink")
        self.assertGreater(counts[-1], counts[0])

    # ---- S-DUR-002 ----
    def test_backup_written_outside_harness(self):
        with patch.object(board, "BOARD_BACKUP_EVERY_EVENTS", 3):
            _make_history(self.root, 8)
        backups = self._backups()
        self.assertTrue(backups.is_dir(), "backup root not created outside .harness")
        snaps = [p for p in backups.iterdir() if p.is_dir()]
        self.assertTrue(snaps, "no backup snapshot written")
        # The backup is outside the .harness tree entirely.
        self.assertNotIn(".harness/board", str(backups))
        self.assertTrue((snaps[0] / "events.jsonl").is_file())

    # ---- S-DUR-003 (load-bearing: the real incident) ----
    def test_full_wipe_recovers_from_backup(self):
        with patch.object(board, "BOARD_BACKUP_EVERY_EVENTS", 3):
            agent = _make_history(self.root, 8)
            self.assertIn(agent, board.snapshot(self.root)["agents"])
            backups = self._backups()
            self.assertTrue(any(backups.iterdir()), "no backup existed before the wipe")

            # SIMULATE the real incident: git clean -fdx / rm -rf .harness
            shutil.rmtree(self.root / ".harness")
            self.assertFalse(_events_path(self.root).exists())
            self.assertTrue(backups.is_dir(), "backup must survive .harness deletion")

            # A board access recovers from the external backup.
            board.status(self.root, agent, "post-wipe tick")
            recovered = board.snapshot(self.root)
            self.assertIn(agent, recovered["agents"], "pre-wipe history was not recovered")
            self.assertTrue((backups / "RECOVERY.log").is_file(), "recovery was not loudly logged")
            self.assertIn("restored board from backup", (backups / "RECOVERY.log").read_text())

    # ---- S-DUR-004 ----
    def test_state_missing_log_present_continues_past_log(self):
        # High threshold → no backup is made, isolating the log-continuation path.
        # With no backup, rich agent state cannot be reconstructed from the log
        # alone — the guarantee here is narrower but critical: the durable log is
        # PRESERVED and sequence numbers are never reused (no corruption), rather
        # than a silent reset to sequence 1 that would clobber the log.
        with patch.object(board, "BOARD_BACKUP_EVERY_EVENTS", 10_000):
            _make_history(self.root, 4)
            log_before = _events_path(self.root).read_text()
            first_line = log_before.splitlines()[0]
            max_seq = max(int(json.loads(l)["sequence"]) for l in log_before.splitlines() if l.strip())

            # Delete only state.json; the durable log survives; no backup exists.
            (self.root / ".harness" / "board" / "state.json").unlink()

            # A fresh mutation must continue PAST the log's max sequence.
            board.register(self.root, "cto", "GLOBAL_MONITOR", vendor="Anthropic")
            state = board.snapshot(self.root)
            self.assertGreater(int(state["next_event"]), max_seq, "sequences must continue past the log")

            # The original log is intact (append-only), and new events land AFTER
            # the old max — never reusing an existing sequence number.
            log_after = _events_path(self.root).read_text()
            self.assertIn(first_line, log_after, "durable log must be preserved, not truncated")
            all_seqs = [int(json.loads(l)["sequence"]) for l in log_after.splitlines() if l.strip()]
            self.assertGreater(max(all_seqs), max_seq)
            self.assertEqual(len(all_seqs), len(set(all_seqs)), "no sequence number may be reused")

    # ---- S-DUR-005 ----
    def test_corrupt_state_recovers_from_backup(self):
        with patch.object(board, "BOARD_BACKUP_EVERY_EVENTS", 3):
            agent = _make_history(self.root, 8)
            # Corrupt state.json with garbage.
            (self.root / ".harness" / "board" / "state.json").write_text("{ this is not json")
            board.status(self.root, agent, "after corruption")
            self.assertIn(agent, board.snapshot(self.root)["agents"])

    # ---- S-DUR-006 (guard the inverse) ----
    def test_fresh_board_no_false_recovery(self):
        fresh = self.base / "brand_new"
        fresh.mkdir()
        agent = board.register(fresh, "cto", "GLOBAL_MONITOR", vendor="Anthropic")["id"]
        self.assertIn(agent, board.snapshot(fresh)["agents"])
        recovery_marker = fresh.parent / ".harness-board-backups" / fresh.name / "RECOVERY.log"
        self.assertFalse(recovery_marker.exists(), "a fresh board must not trigger recovery")

    # ---- S-DUR-007 ----
    def test_backup_rotation_bounded(self):
        with patch.object(board, "BOARD_BACKUP_EVERY_EVENTS", 2), \
             patch.object(board, "BOARD_BACKUP_KEEP", 3):
            _make_history(self.root, 40)  # many threshold crossings
            snaps = [p for p in self._backups().iterdir() if p.is_dir()]
            self.assertLessEqual(len(snaps), 3, "rotation must cap the snapshot count")
            self.assertGreater(len(snaps), 0)


if __name__ == "__main__":
    unittest.main()
