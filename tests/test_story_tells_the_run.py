# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""The Story page must tell a finished run truthfully (a real weather-app run showed four wrong or alarming lines to the very end).

Pure tests on small hand-made boards: no files, no network, no browser.
  1. "Every requirement covered" says "not yet" while work runs, and says the final review judged the rest once it passed.
  2. "Tests checked on the empty project" never says "0 ... failed first (good)" when nothing could be compared.
  3. A task delivered as one piece (only final reviews) still tells building, own testing, review rounds and repair.
  4. "Replacing the Delivery Agent" is not listed after the replacement ended, a review passed or the owner accepted.

Run:  PYTHONPATH=. python3 -m unittest tests.test_story_tells_the_run -v
"""
from __future__ import annotations

import json
import unittest

from harness import board_viewer, story

TASK = "weather-app"
SHA = "a" * 64


def labels(uncovered=()):
    """Eleven requirement labels; those numbered in `uncovered` have no test."""
    return [{"n": n, "plain": f"Requirement {n}", "criterion": f"Requirement {n}",
             "coverage": "gap: no test covers this yet" if n in uncovered else "covered"} for n in range(1, 12)]


def review(round_no, status, summary="", **extra):
    """A final whole-task review record, as an atomic task produces (no chunk reviews)."""
    return {"id": f"review-{TASK}-final-0{round_no}", "task": TASK, "phase": "final_acceptance", "status": status, "review_round": round_no, "cycle": round_no,
            "claimed_by": "qa-1", "stage": "independent_review", "requested_at": f"2026-11-05T20:{10 + round_no}:00+00:00", "completed_at": f"2026-11-05T20:{10 + round_no}:30+00:00",
            "result": status, "result_summary": summary, "delivery_state": "passed", "evidence": "/x", "structure_revision": 1, **extra}


def board(red=None, uncovered=(3, 4, 5, 6, 7), reviews=(), events=(), decision=None):
    """A minimal board state for TASK: agreed requirements, one stored contract check, the given reviews and events."""
    cc = {"status": "ready", "labels": labels(uncovered), "checks": [{"n": "1.1", "criterion_index": 1, "sha256": SHA}], "checks_sha256": SHA,
          "red_proof": red or {}}
    return {"agents": {}, "events": list(events) + [{"kind": "task_begun", "task": TASK, "at": "2026-11-05T20:00:00+00:00"}],
            "requirement_confirmations": {TASK: {"text": "agreed", "contract_check": cc}}, "qa_requests": {r["id"]: r for r in reviews},
            "delivery_plans": {TASK: {"mode": "atomic", "structure_revision": 1, "subtasks": {}}},
            "release_decisions": {TASK: decision} if decision else {}}


def rows(state):
    """The Story timeline rows of TASK keyed by step number."""
    return {r["n"]: r for r in story.build(state, {}, TASK, None)["timeline"]["steps"]}


class CoverageWording(unittest.TestCase):
    def test_not_yet_while_no_review_has_passed(self):
        row = rows(board())[8]
        self.assertEqual(row["status"], "gap")
        self.assertIn("6 of 11 requirements have a test so far", row["text"])
        self.assertIn("not yet", row["text"])
        self.assertNotIn("Not covered:", row["text"])

    def test_final_review_pass_is_named_instead_of_a_failure(self):
        row = rows(board(reviews=[review(1, "passed", "TASK DONE: YES")]))[8]
        self.assertEqual(row["status"], "done")
        self.assertIn("final whole-app review judged the finished work", row["text"])

    def test_requirements_tile_does_not_read_as_failure(self):
        st = board(reviews=[review(1, "passed", "TASK DONE: YES", reviewed_commit="c1", contract_check_runs=[{"commit": "c1", "checks_sha256": SHA}])])
        st["requirement_confirmations"][TASK]["contract_check"]["results"] = {"1": "passed"}
        tile = story.build(st, {}, TASK, None)["requirements"]
        self.assertEqual(tile["text"], "1 / 11 proven by tests; 10 by the final review")


class ARoundThatCouldNotTestFoundNoProblem(unittest.TestCase):
    """Codex R1-1: a first final review that ends NOT TESTED with no findings was told as "found problems" and a repair."""

    # review_round=0: the rounds are told in the order the reviews were judged, as for a board that stored no round number.
    NOT_TESTED = "TASK DONE: NOT TESTED — the map could not be verified in the review browser. No implementation repair requested."

    def test_round_1_not_tested_invents_no_problem_and_no_repair(self):
        seen = rows(board(reviews=[review(1, "failed", self.NOT_TESTED, structured_findings=[], review_round=0),
                                   review(2, "passed", "TASK DONE: YES", review_round=0)]))
        self.assertEqual(seen[17]["text"], "Round 1: could not be fully tested.")
        self.assertEqual(seen[18]["text"], "Round 1 could not be fully tested. No problem was found in the work.")
        self.assertNotIn("found problems", seen[18]["text"])
        self.assertEqual(seen[19]["status"], "skipped")
        self.assertIn("nothing to repair", seen[19]["text"])
        self.assertNotIn("fixed what the reviewer found", seen[19]["text"])
        everything = " ".join(str(row) for row in seen.values())
        self.assertNotIn("Every real problem was listed", everything)

    def test_the_final_review_s_own_detail_does_not_count_an_untested_attempt_as_a_problem(self):
        """Codex R2-1: NOT TESTED then PASS on unchanged work said "Final review: 1 earlier attempt found problems"."""
        seen = rows(board(reviews=[review(1, "failed", self.NOT_TESTED, structured_findings=[], review_round=0, reviewed_commit="c1"),
                                   review(2, "passed", "TASK DONE: YES", review_round=0, reviewed_commit="c1")]))
        detail = " ".join(seen[22]["detail"]) if isinstance(seen[22].get("detail"), list) else str(seen[22])
        self.assertNotIn("found problems", detail)
        self.assertIn("1 earlier attempt could not be fully tested; no problem was found in it", detail)
        mixed = rows(board(reviews=[review(1, "failed", "TASK DONE: NO — units.", structured_findings=[{"t": 1}], review_round=0),
                                    review(2, "failed", self.NOT_TESTED, structured_findings=[], review_round=0),
                                    review(3, "passed", "TASK DONE: YES", review_round=0)]))
        mixed_detail = " ".join(mixed[22]["detail"]) if isinstance(mixed[22].get("detail"), list) else str(mixed[22])
        self.assertIn("1 earlier attempt found problems.", mixed_detail)
        self.assertIn("1 earlier attempt could not be fully tested", mixed_detail)
        whole = json.dumps(story.build(board(reviews=[review(1, "failed", self.NOT_TESTED, structured_findings=[], review_round=0),
                                                      review(2, "passed", "TASK DONE: YES", review_round=0)]), {}, TASK, None))
        self.assertNotIn("found problems", whole, "nowhere in the whole Story of an untested-then-passed run")
        self.assertNotIn("Every real problem was listed", whole)

    def test_round_1_not_tested_alone_says_only_that(self):
        seen = rows(board(reviews=[review(1, "failed", self.NOT_TESTED, structured_findings=[], review_round=0)]))
        self.assertEqual(seen[18]["text"], "Round 1 could not be fully tested. No problem was found in the work.")
        self.assertNotIn("fixed what the reviewer found", str(seen.get(19)))

    def test_a_real_round_1_failure_still_says_problems_and_a_repair(self):
        seen = rows(board(reviews=[review(1, "failed", "TASK DONE: NO — the units switch left wind in km/h.", structured_findings=[{"title": "units"}], review_round=0),
                                   review(2, "passed", "TASK DONE: YES", review_round=0)]))
        self.assertEqual(seen[18]["text"], "Round 1 found 1 problem to fix.")
        self.assertEqual(seen[19]["text"], "The builder fixed what the reviewer found.")


class EmptyProjectComparison(unittest.TestCase):
    def red(self, labels_by_test):
        return {"status": "settled", "checks": {n: {"label": label, "reason": "file could not be opened successfully", "sha256": SHA}
                                                for n, label in labels_by_test.items()}}

    def test_nothing_compared_says_so_once(self):
        row = rows(board(red=self.red({"1.1": "could_not_compare"})))[10]
        self.assertEqual(row["status"], "not_recorded")
        self.assertEqual(row["title"], "Tests checked on the empty project")
        self.assertEqual(row["text"], "The tests could not be run on the empty project (the board noted: file could not be opened successfully).")
        self.assertNotIn("good", row["text"])
        self.assertIn("This says nothing about the finished work.", row["detail"])

    def test_mixed_results_keep_the_old_sentence(self):
        st = board(red=self.red({"1.1": "fails_before"}))
        st["requirement_confirmations"][TASK]["contract_check"]["checks"].append({"n": "2.1", "criterion_index": 2, "sha256": "b" * 64})
        st["requirement_confirmations"][TASK]["contract_check"]["red_proof"]["checks"]["2.1"] = {"label": "could_not_compare", "sha256": "b" * 64}
        self.assertEqual(rows(st)[10]["text"], "Tests checked on the empty project: 1 of 2 tests failed first (good), 1 could not be compared.")


class OnePieceTaskTellsItsSteps(unittest.TestCase):
    def test_final_reviews_fill_build_and_review_steps(self):
        reviews = [review(1, "failed", "TASK DONE: NO", structured_findings=[{"id": "B1"}, {"id": "B2"}]), review(2, "failed", "TASK DONE: NOT TESTED - the map"),
                   review(3, "passed", "TASK DONE: YES")]
        r = rows(board(reviews=reviews))
        for n in range(13, 19):
            self.assertEqual(r[n]["status"], "done", (n, r[n]["text"]))
            self.assertNotIn("not tracked", r[n]["text"])
        self.assertEqual(r[17]["text"], "Round 1: 2 blockers.")
        self.assertEqual(r[19]["status"], "done")
        self.assertEqual(r[20]["text"], "Round 2: could not be fully tested.")
        self.assertEqual(r[21]["text"], "A last retry was used: passed.")
        self.assertEqual(r[22]["status"], "done")


class ReplacementBanner(unittest.TestCase):
    REPLACING = {"kind": "agent_replacement_requested", "task": TASK, "at": "2026-11-05T21:18:04+00:00", "message": "The harness is replacing the Delivery Agent"}

    def lines(self, *later, decision=None):
        state = {"events": [self.REPLACING, *later], "release_decisions": {TASK: decision} if decision else {}}
        return [a["kind"] for a in board_viewer._harness_actions(state)]

    def test_shown_while_the_fresh_attempt_is_pending(self):
        self.assertEqual(self.lines(), ["agent_replacement_requested"])

    def test_hidden_after_the_review_passed(self):
        self.assertEqual(self.lines({"kind": "release_routing_requested", "task": TASK, "at": "2026-11-05T21:29:18+00:00", "message": "PASS"}), [])

    def test_hidden_after_the_owner_accepted(self):
        self.assertEqual(self.lines(decision={"decision": "accepted"}), [])
        self.assertEqual(self.lines({"kind": "git_acceptance_completed", "task": TASK, "at": "2026-11-05T21:34:48+00:00", "message": "Accepted"}), [])

    def test_other_tasks_and_earlier_events_do_not_hide_it(self):
        self.assertEqual(self.lines({"kind": "release_routing_requested", "task": "other", "at": "2026-11-05T21:29:18+00:00", "message": "PASS"}),
                         ["agent_replacement_requested"])
        self.assertEqual(self.lines({"kind": "release_routing_requested", "task": TASK, "at": "2026-11-05T21:00:00+00:00", "message": "old"}),
                         ["agent_replacement_requested"])


if __name__ == "__main__":
    unittest.main()
