# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""The board file must not grow with the app's own files (2026-10-16_baseline-bloat).

A live project's board reached 45 MB because every task start record listed the whole `git status`
twice, mostly the app's own `.harness/` files. These tests use real Git output (renames, quoted and
non-ASCII names), not hand-written status lines.
"""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from harness import board, cto, git_broker


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


class RepoCase(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "code"
        self.root.mkdir()
        _git(self.root, "init", "-q", "-b", "main")
        _git(self.root, "config", "user.name", "Fixture")
        _git(self.root, "config", "user.email", "fixture@example.invalid")
        _git(self.root, "config", "diff.renames", "true")
        (self.root / "owner.txt").write_text("owner\n")
        (self.root / "keep.txt").write_text("keep\n")
        _git(self.root, "add", "owner.txt", "keep.txt")
        _git(self.root, "commit", "-qm", "base")

    def harness_files(self, count: int = 5) -> None:
        folder = self.root / ".harness"
        folder.mkdir(exist_ok=True)
        for index in range(count):
            (folder / f"file{index}.lock").write_text("x")


class BaselineCaptureTests(RepoCase):
    def test_baseline_lists_only_real_files_and_has_no_status_lines(self):
        self.harness_files(50)
        (self.root / "real-one.txt").write_text("1")
        (self.root / "real-two.txt").write_text("2")
        baseline = board._git_task_baseline(self.root)
        self.assertEqual(baseline["dirty_files"], ["real-one.txt", "real-two.txt"])
        self.assertNotIn("status_lines", baseline)
        self.assertEqual(len(baseline["fingerprint"]), 64)
        self.assertFalse(baseline["requires_owner_action"])
        self.assertLess(len(json.dumps(baseline)), 1500)

    def test_clean_tree_keeps_identity_fields(self):
        baseline = board._git_task_baseline(self.root)
        self.assertEqual(baseline["dirty_files"], [])
        for key in ("head", "branch", "fingerprint", "captured_at", "mode", "policy", "requires_owner_action"):
            self.assertIn(key, baseline)

    def test_rename_into_harness_and_out_of_harness_are_kept_with_both_endpoints(self):
        (self.root / ".harness").mkdir()
        # owner.txt -> .harness/owner-copy.txt : the project file leaves the project
        (self.root / ".harness" / "owner-copy.txt").write_text("owner\n")
        (self.root / "owner.txt").unlink()
        _git(self.root, "add", "-A", "owner.txt", ".harness/owner-copy.txt")
        baseline = board._git_task_baseline(self.root)
        self.assertIn("owner.txt", baseline["dirty_files"])

    def test_quoted_and_non_ascii_and_arrow_names_are_exact(self):
        for name in ('quote"d.txt', "café.txt", "a -> b.txt"):
            (self.root / name).write_text("x")
        baseline = board._git_task_baseline(self.root)
        self.assertEqual(sorted(baseline["dirty_files"]), sorted(['quote"d.txt', "café.txt", "a -> b.txt"]))

    def test_harness_only_change_keeps_the_review_digest(self):
        before = board._git_review_artifact(self.root)
        self.harness_files(10)
        after = board._git_review_artifact(self.root)
        self.assertEqual(before["working_tree_digest"], after["working_tree_digest"])
        self.assertTrue(after["immutable_clean"])


class ReviewCaptureTests(RepoCase):
    def test_rename_of_owner_file_into_harness_stays_visible_to_the_review(self):
        (self.root / ".harness").mkdir()
        (self.root / ".harness" / "owner.txt").write_text("owner\n")
        (self.root / "owner.txt").unlink()
        _git(self.root, "add", "-A", "owner.txt", ".harness/owner.txt")
        artifact = board._git_review_artifact(self.root)
        self.assertFalse(artifact["immutable_clean"])
        self.assertIn("owner.txt", artifact["working_tree_files"])

    def test_content_change_of_a_quoted_untracked_file_changes_the_digest(self):
        name = "café file.txt"
        (self.root / name).write_text("one")
        first = board._git_review_artifact(self.root)["working_tree_digest"]
        (self.root / name).write_text("two")
        second = board._git_review_artifact(self.root)["working_tree_digest"]
        self.assertNotEqual(first, second)

    def test_committed_manifest_names_are_decoded_for_overlap(self):
        name = "café.txt"
        (self.root / name).write_text("x")
        _git(self.root, "add", name)
        _git(self.root, "commit", "-qm", "accent")
        base = subprocess.run(["git", "rev-parse", "HEAD~1"], cwd=self.root, capture_output=True, text=True).stdout.strip()
        artifact = board._git_review_artifact(self.root, base)
        self.assertEqual(artifact["files"], [name])
        (self.root / name).write_text("changed again")
        dirty = board._git_review_artifact(self.root, base)
        self.assertFalse(dirty["immutable_clean"])  # dirt overlapping the candidate is seen, not unrelated


class CompactionTests(RepoCase):
    def legacy_baseline(self, lines: list[str]) -> dict:
        return {"head": "abc", "branch": "main", "fingerprint": "f" * 64, "captured_at": "t", "mode": "task_start",
                "policy": "p", "requires_owner_action": False, "available": True,
                "dirty_files": [line[3:] for line in lines], "status_lines": lines}

    def test_compaction_classifies_from_status_lines_and_keeps_owner_renames(self):
        lines = ["?? .harness/a.lock", '?? ".harness/b c"', "?? owner.txt", "R  .harness/old.txt -> owner2.txt",
                 "R  owner3.txt -> .harness/new.txt", "?? weird -> name.txt", "R  .harness/x -> y -> .harness/z"]
        state = {"task_baselines": {"t": self.legacy_baseline(lines)}}
        removed, saved = board._compact_task_baselines(state)
        baseline = state["task_baselines"]["t"]
        self.assertNotIn("status_lines", baseline)
        self.assertEqual(baseline["dirty_files"], [
            "owner.txt", ".harness/old.txt -> owner2.txt", "owner3.txt -> .harness/new.txt",
            "weird -> name.txt", ".harness/x -> y -> .harness/z"])
        self.assertEqual(removed, 2)
        self.assertGreater(saved, 0)
        self.assertEqual(baseline["head"], "abc")
        self.assertEqual(baseline["fingerprint"], "f" * 64)
        self.assertEqual(board._compact_task_baselines(state), (0, 0))

    def test_without_status_lines_arrow_entries_are_kept_conservatively(self):
        baseline = self.legacy_baseline([])
        baseline.pop("status_lines")
        baseline["dirty_files"] = [".harness/a", "x -> .harness/y", ".harness/p -> q"]
        state = {"task_baselines": {"t": baseline}}
        board._compact_task_baselines(state)
        self.assertEqual(state["task_baselines"]["t"]["dirty_files"], ["x -> .harness/y", ".harness/p -> q"])

    def test_transaction_compacts_once_records_one_event_and_aborted_transaction_persists_nothing(self):
        junk = [f"?? .harness/f{index}" for index in range(2000)] + ["?? owner.txt"]
        with board.locked_state(self.root) as state:
            state["task_baselines"] = {"t": self.legacy_baseline(junk)}
        path = board._state_path(self.root)
        # the first write above already compacted on the NEXT open; rewrite the legacy shape on disk directly
        raw = json.loads(path.read_text())
        raw.pop("baselines_compacted_at", None)
        raw["task_baselines"] = {"t": self.legacy_baseline(junk)}
        path.write_text(json.dumps(raw))
        big = path.stat().st_size
        with self.assertRaises(RuntimeError):
            with board.locked_state(self.root) as state:
                raise RuntimeError("abort")
        self.assertEqual(path.stat().st_size, big)  # an aborted transaction persists neither compaction nor event
        with board.locked_state(self.root) as state:
            pass
        small = path.stat().st_size
        self.assertLess(small, big // 5)
        state = json.loads(path.read_text())
        self.assertEqual(state["task_baselines"]["t"]["dirty_files"], ["owner.txt"])
        self.assertEqual(sum(1 for e in state["events"] if e["kind"] == "board_compacted"), 1)
        with board.locked_state(self.root) as state:
            pass
        state = json.loads(path.read_text())
        self.assertEqual(sum(1 for e in state["events"] if e["kind"] == "board_compacted"), 1)


class CompactionBoundaryTests(RepoCase):
    """Where the compaction sits in the transaction: pause refusals and recovery from a backup."""

    legacy_baseline = CompactionTests.legacy_baseline

    def junk_state(self) -> dict:
        junk = [f"?? .harness/f{index}" for index in range(500)] + ["?? owner.txt"]
        return {"task_baselines": {"t": self.legacy_baseline(junk)}}

    def test_paused_write_refusal_still_persists_the_compaction_and_still_refuses(self):
        with board.locked_state(self.root) as state:
            state["project_pause"] = {"status": "paused", "pause_id": "p1"}
        path = board._state_path(self.root)
        raw = json.loads(path.read_text())
        raw.pop("baselines_compacted_at", None)
        raw["task_baselines"] = self.junk_state()["task_baselines"]
        path.write_text(json.dumps(raw))
        big = path.stat().st_size
        with self.assertRaises(board.ProjectPausedError):
            with board.locked_state(self.root):
                self.fail("a paused project must refuse the write")
        saved = json.loads(path.read_text())
        self.assertLess(path.stat().st_size, big // 3)
        self.assertEqual(saved["task_baselines"]["t"]["dirty_files"], ["owner.txt"])
        self.assertEqual(sum(1 for e in saved["events"] if e["kind"] == "board_compacted"), 1)

    def test_state_recovered_from_an_old_backup_is_compacted(self):
        with board.locked_state(self.root) as state:
            state["next_event"] = 1000
            state["task_baselines"] = self.junk_state()["task_baselines"]
            state.pop("baselines_compacted_at", None)
        path = board._state_path(self.root)
        raw = json.loads(path.read_text())
        raw.pop("baselines_compacted_at", None)
        raw["task_baselines"] = self.junk_state()["task_baselines"]
        raw["_last_backup_event"] = 0
        (board.board_dir(self.root) / "events.jsonl").touch()
        board._snapshot_board(self.root, raw)  # an old-shape backup of the board
        path.write_text("{ not json")
        with board.locked_state(self.root) as state:
            self.assertEqual(state["task_baselines"]["t"]["dirty_files"], ["owner.txt"])
        saved = json.loads(path.read_text())
        self.assertNotIn("status_lines", saved["task_baselines"]["t"])


class CtoStatusTests(RepoCase):
    def test_status_paths_stay_strict_and_the_diagnostic_filters_harness_files(self):
        (self.root / ".harness").mkdir()
        (self.root / ".harness" / "x").write_text("x")
        (self.root / "real.txt").write_text("r")
        paths = cto._status_paths(self.root)
        self.assertIn(".harness/x", paths)  # integrity checks must still see it
        self.assertIn("real.txt", paths)
        self.assertEqual([p for p in paths if not board._harness_owned_path(p)], ["real.txt"])


class UnquoteTests(unittest.TestCase):
    def test_git_unquote(self):
        self.assertEqual(git_broker.git_unquote('"caf\\303\\251.txt"'), "café.txt")
        self.assertEqual(git_broker.git_unquote('"a\\"b\\\\c"'), 'a"b\\c')
        self.assertEqual(git_broker.git_unquote("plain -> name"), "plain -> name")

    def test_porcelain_entries_keep_both_rename_endpoints(self):
        self.assertEqual(git_broker.porcelain_entries("R  new.txt\0old.txt\0?? a\0"),
                         [("R ", ["new.txt", "old.txt"]), ("??", ["a"])])


if __name__ == "__main__":
    unittest.main()
