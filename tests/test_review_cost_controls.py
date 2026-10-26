# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Review cost controls (task 2026-10-25_review-cost-controls): a bounded reading list for a huge change, and one plain line of what a task used.

Run:  PYTHONPATH=. python3 -m unittest tests.test_review_cost_controls -v
Hermetic: temporary git repositories and board roots only; nothing is read from the machine.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from datetime import datetime, timedelta, timezone
from pathlib import Path

from harness import board, board_viewer, control, global_settings, review_brief, review_cost
from tests.test_requirements_contract import context, delivery_with_direction


def git(repo: Path, *args: str) -> str:
    environment = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    return subprocess.run(["git", *args], cwd=repo, env=environment, check=True, capture_output=True, text=True).stdout.strip()


class ReducedReadingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(review_cost.configure, None)
        self.repo = Path(self.tmp.name)
        git(self.repo, "init", "-q")
        (self.repo / "README").write_text("base\n")
        git(self.repo, "add", "README")
        git(self.repo, "commit", "-q", "-m", "base")
        self.base = git(self.repo, "rev-parse", "HEAD")

    def candidate(self, files: dict[str, str]):
        for name, text in files.items():
            (self.repo / name).write_text(text)
        git(self.repo, "add", *files)
        git(self.repo, "commit", "-q", "-m", "work")
        head = git(self.repo, "rev-parse", "HEAD")
        request = {"id": "r1", "task": "T", "phase": "chunk", "test_scope": "full", "reviewed_commit": head, "reviewed_base_commit": self.base,
                   "reviewed_tree_hash": "b" * 40, "reviewed_files": sorted(files), "contract_revision": {}, "environment_identity": {}}
        state = {"requirement_confirmations": {"T": {"text": "Do it.", "confirmed_at": "x", "version": 1}}, "delivery_plans": {},
                 "qa_requests": {"r1": request}, "owner_clarifications": {}, "deferred_findings": {}, "task_repositories": {}}
        return review_brief.build(self.repo, state, request)

    def test_many_files_are_reduced_with_the_plain_line_and_nothing_is_dropped(self):
        review_cost.configure({"review_input_max_files": 5})
        files = {f"f{n}.txt": "line\n" * (n + 1) for n in range(8)}
        brief = self.candidate(files)
        cut = brief["input_reduction"]
        self.assertEqual((cut["total_files"], cut["read_in_full_count"], cut["not_read_in_full_count"]), (8, 5, 3))
        self.assertEqual(sorted(cut["read_in_full"] + cut["not_read_in_full"]), sorted(files), "every changed file is in exactly one list")
        self.assertEqual(cut["read_in_full"][0], "f7.txt", "largest change first")
        self.assertIn("This change is large (8 files", cut["plain_line"])
        self.assertIn("Challenge Ledger", cut["plain_line"])
        unsigned = {key: value for key, value in brief.items() if key != "sha256"}
        self.assertEqual(brief["sha256"], hashlib.sha256(json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode()).hexdigest())

    def test_bytes_alone_reduce_and_a_file_larger_than_the_limit_is_never_read_in_full(self):
        review_cost.configure({"review_input_max_bytes": 10_000})
        brief = self.candidate({"huge.txt": "x" * 12_000, "a.txt": "a" * 3000, "b.txt": "b" * 3000})
        cut = brief["input_reduction"]
        self.assertEqual(cut["total_files"], 3)
        self.assertNotIn("huge.txt", cut["read_in_full"])
        self.assertIn("huge.txt", cut["not_read_in_full"])
        self.assertEqual(sorted(cut["read_in_full"]), ["a.txt", "b.txt"])

    def test_an_ordinary_change_and_one_exactly_at_the_limit_are_untouched(self):
        review_cost.configure({"review_input_max_files": 5})
        self.assertNotIn("input_reduction", self.candidate({f"n{n}.txt": "ok\n" for n in range(5)}))

    def test_when_git_cannot_measure_the_file_count_still_limits_and_says_so(self):
        review_cost.configure({"review_input_max_files": 5})
        request = {"reviewed_commit": "", "reviewed_base_commit": ""}
        paths = [f"p{n}" for n in range(7)]
        cut = review_cost.reduce_candidate(self.repo, request, paths)
        self.assertFalse(cut["size_measured"])
        self.assertIn("could not be measured", cut["plain_line"])
        self.assertEqual(cut["read_in_full_count"] + cut["not_read_in_full_count"], 7)
        self.assertIsNone(review_cost.reduce_candidate(self.repo, request, paths[:3]))

    def test_more_than_eighty_files_keep_a_complete_retrievable_partition_and_a_small_brief(self):
        review_cost.configure({"review_input_max_files": 5})
        files = {f"m{n:03d}.txt": "line\n" * (n + 1) for n in range(100)}
        brief = self.candidate(files)
        cut = brief["input_reduction"]
        self.assertLessEqual(len(cut["read_in_full"]) + len(cut["not_read_in_full"]), 2 * review_cost.LISTED_PATHS)
        self.assertEqual((cut["read_in_full_count"], cut["not_read_in_full_count"]), (5, 95))
        listing = cut["complete_lists_file"]
        raw = Path(listing["path"]).read_bytes()
        self.assertEqual(hashlib.sha256(raw).hexdigest(), listing["sha256"])
        complete = json.loads(raw)
        self.assertEqual(sorted(complete["read_in_full"] + complete["not_read_in_full"]), sorted(files), "every path is in exactly one list")
        self.assertEqual(len(complete["read_in_full"]) + len(complete["not_read_in_full"]), 100)
        self.assertEqual(len(complete["diff_summary"]), 100)

    def test_the_limits_are_settings_fields_with_bounds(self):
        defaults = global_settings.default_plumbing()
        for name in ("review_input_max_files", "review_input_max_bytes", "review_input_measure_seconds"):
            self.assertIn(name, defaults)
        with self.assertRaises(ValueError):
            global_settings._validated_plumbing({"review_input_max_files": 0})
        review_cost.configure({"review_input_max_files": 0, "review_input_max_bytes": True})
        self.assertEqual(review_cost.limits(), (review_cost.MAX_FILES_DEFAULT, review_cost.MAX_BYTES_DEFAULT))


class UsageLineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = context(Path(self.tmp.name))

    def test_steps_count_for_delivery_not_the_cto_and_minutes_survive_archiving_and_a_fresh_process(self):
        agent = delivery_with_direction(self.root, "T")
        cto_session = control.create(self.root, "claude_cto")
        cto = board.register(self.root, "cto", "GLOBAL_MONITOR", vendor="Anthropic", session_id=cto_session["id"])
        before = board.snapshot(self.root)["task_turns"]["T"]
        board.status(self.root, agent["id"], "working on it")
        board.status(self.root, cto["id"], "watching")
        self.assertEqual(board.snapshot(self.root)["task_turns"]["T"], before + 1, "a Delivery step counts once; the CTO's does not")
        start = datetime(2026, 10, 25, 10, 0, tzinfo=timezone.utc)
        stamp = lambda moment: moment.isoformat()
        whole = lambda **fields: {"phase": "chunk", "subtask": "", "chunk": "", "cycle": 1, "developer_id": "d", "claimed_by": "q", "requested_at": stamp(start),
                                  "review_wait_started_at": stamp(start), **fields}
        owner_cards = dict(board.snapshot(self.root).get("owner_actions") or {})
        with board.locked_state(self.root) as state:
            state["qa_requests"]["done"] = whole(**{"id": "done", "task": "T", "status": "passed", "reserved_at": stamp(start), "claimed_at": stamp(start + timedelta(minutes=3)),
                                            "completed_at": stamp(start + timedelta(minutes=12))})
            board._event(state, "qa_result", None, {"task": "T", "request_id": "done"})
            state["qa_requests"]["paused"] = whole(**{"id": "paused", "task": "T", "status": "suspended", "reserved_at": stamp(start), "claimed_at": stamp(start)})
            state["qa_requests"]["running"] = whole(**{"id": "running", "task": "T", "status": "claimed", "reserved_at": stamp(start + timedelta(hours=1)),
                                               "claimed_at": stamp(start + timedelta(hours=1, minutes=1))})
            state["qa_requests"]["other"] = whole(**{"id": "other", "task": "OTHER", "status": "claimed", "reserved_at": stamp(start), "claimed_at": stamp(start)})
        at = start + timedelta(hours=1, minutes=5)
        state = board.snapshot(self.root)
        self.assertEqual(review_cost.usage(state, "T", at)["review_minutes"], 12 + 5, "settled 12, running 5, the paused one adds nothing")
        with board.locked_state(self.root) as state:
            del state["qa_requests"]["done"]          # archived: the finished review leaves the live board
            state["qa_requests"].pop("running")
        usage = review_cost.usage(board.snapshot(self.root), "T", at)
        self.assertEqual(usage["review_minutes"], 12, "archiving keeps the total")
        self.assertIn("not a provider bill", usage["line"])
        self.assertIn("leaves out the CTO", usage["line"])
        code = ("import json,sys;from harness import board,review_cost;from pathlib import Path;"
                "print(json.dumps(review_cost.usage(board.snapshot(Path(sys.argv[1])),'T')))")
        fresh = subprocess.run([sys.executable, "-c", code, str(self.root)], cwd=Path(__file__).resolve().parent.parent, capture_output=True, text=True,
                               env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parent.parent), "PYTHONDONTWRITEBYTECODE": "1"})
        self.assertEqual(fresh.returncode, 0, fresh.stderr)
        self.assertEqual(json.loads(fresh.stdout)["agent_turns"], usage["agent_turns"], "a fresh process reads the same counts")
        self.assertEqual(json.loads(fresh.stdout)["review_minutes"], 12)
        self.assertEqual(dict(board.snapshot(self.root).get("owner_actions") or {}), owner_cards, "information only: no card asks the owner anything")

    def test_a_pause_is_not_review_time_and_an_abandoned_attempt_keeps_its_minutes(self):
        start = datetime(2026, 10, 25, 10, 0, tzinfo=timezone.utc)
        agent = delivery_with_direction(self.root, "T")
        reviewer = board.register(self.root, "qa", "REVIEW_QUEUE", vendor="Anthropic", session_id="reviewer-session")
        request = {"id": "rq", "task": "T", "phase": "chunk", "subtask": "", "chunk": "", "cycle": 1, "status": "claimed", "result": None,
                   "developer_id": agent["id"], "claimed_by": reviewer["id"], "reserved_by": reviewer["id"], "requested_at": start.isoformat(),
                   "review_wait_started_at": start.isoformat(), "reserved_at": start.isoformat(), "claimed_at": start.isoformat(), "route_state": "review_executing"}
        with board.locked_state(self.root) as state:
            state["qa_requests"]["rq"] = request
        minutes = lambda moment: review_cost.usage(board.snapshot(self.root), "T", moment)["review_minutes"]
        with mock.patch.object(review_cost, "_clock", lambda: start + timedelta(minutes=10)):
            board.begin_project_pause(self.root, drain_seconds=0)
            board.finish_project_pause(self.root)
        self.assertEqual(board.snapshot(self.root)["qa_requests"]["rq"]["status"], "suspended")
        self.assertEqual(minutes(start + timedelta(minutes=70)), 10, "10 minutes reviewing stay 10 for the whole hour of the pause")
        with mock.patch.object(review_cost, "_clock", lambda: start + timedelta(minutes=70)):
            resume = board.begin_project_resume(self.root)
            board.finish_project_resume(self.root, resume["resume_id"])
        self.assertEqual(board.snapshot(self.root)["qa_requests"]["rq"]["status"], "claimed")
        self.assertEqual(minutes(start + timedelta(minutes=75)), 15, "5 more minutes after the resume make 15, not 75")
        with board.locked_state(self.root) as state:
            state["qa_requests"]["rq"]["completed_at"] = (start + timedelta(minutes=75)).isoformat()
            state["qa_requests"]["rq"]["status"] = "passed"
            board._event(state, "qa_result", None, {"task": "T", "request_id": "rq"})
            state["qa_requests"].pop("rq")                                   # archived
        self.assertEqual(minutes(start + timedelta(hours=5)), 15, "the finished total is kept and does not grow")
        again = {"id": "rq2", "task": "T", "status": "claimed", "reserved_at": start.isoformat(), "claimed_at": start.isoformat()}
        review_cost.close_stretch(again, start + timedelta(minutes=7))      # an attempt abandoned (reopened) after 7 minutes
        self.assertEqual(again["review_active_seconds"], 7 * 60)
        again.update(status="claimed", reserved_at=(start + timedelta(hours=2)).isoformat(), claimed_at=(start + timedelta(hours=2)).isoformat())
        self.assertEqual(review_cost._active_seconds(again, start + timedelta(hours=2, minutes=3)), 10 * 60, "the next attempt adds to the banked 7")

    def test_the_dashboard_and_the_history_both_carry_the_line(self):
        delivery_with_direction(self.root, "T")
        state = board.snapshot(self.root)
        self.assertIn("recorded Delivery and Reviewer step", board_viewer._compact_dashboard_state(state, ["T"])["task_usage"]["T"]["line"])
        history = board_viewer._task_history(state, {}, [])
        self.assertTrue(all("usage_line" in item for item in history))


if __name__ == "__main__":
    unittest.main()
