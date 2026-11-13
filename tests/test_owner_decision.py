# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""The owner in control (task 2026-11-08_accept-after-repair, R8 and R9).

2026-11-08: the Reviewer failed Finish Support six times on "an outsider could guess the owner key" for a solo owner on his own Mac; he
closed the finding in writing, the Reviewer restated it, and the only way out was a merge by hand. Now: every open finding of a failed
final review can be closed by him; closing the last one (or Take what you have at the round cap) writes an owner review waiver for
exactly that candidate, which every release gate reads as the PASS while the Reviewer's FAIL record stays as written."""
from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from harness import board, story

TASK = "finish-support"
OTHER = "other-task"
SHA = "c6d44e70aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"


def _finding(identifier: str, summary: str, harm_tag: str = "") -> dict:
    return {"id": identifier, "summary": summary, "harm_tag": harm_tag, "category": "behavior", "harm": summary,
            "regression_check": "run the check", "evidence_ref": "the run", "letter": ""}


class OwnerDecisionCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / "project"
        self.root.mkdir()
        self.cto = board.register(self.root, "cto", "GLOBAL_MONITOR", session_id="cto-session")
        self.builder = board.register(self.root, "engineering", board.AWAITING_OWNER_DIRECTION, session_id="builder-session")
        self.reviewer = board.register(self.root, "qa", "REVIEW_QUEUE", session_id="reviewer-session")
        with board.locked_state(self.root, operation="test") as state:
            state["agents"][self.builder["id"]].update({"task": TASK, "active": True, "status": "independent_review_failed"})
            state.setdefault("task_owner_directions", {})[TASK] = "Finish Support."
            state.setdefault("task_repositories", {})[TASK] = "/repo"
            state.setdefault("delivery_plans", {})[TASK] = {"mode": "atomic", "structure_revision": 0, "subtasks": {}}
        # the completion gate and the release tail are the rest of the machine; these tests prove the decision reaches them
        for target in ("harness.board._completion_gate_problems",):
            patcher = mock.patch(target, return_value=[])
            patcher.start(); self.addCleanup(patcher.stop)
        tail = mock.patch("harness.release_coordinator.coordinate", return_value=None)
        tail.start(); self.addCleanup(tail.stop)

    def failed_review(self, findings, *, certified=True, mirrored=True, request_id="review-final-05", task=TASK, cycle=5):
        now = datetime.now(timezone.utc).isoformat()
        request = {
            "id": request_id, "task": task, "stage": board.INDEPENDENT_REVIEW, "phase": "final_acceptance", "cycle": cycle,
            "status": "failed", "developer_id": self.builder["id"], "requested_at": now, "completed_at": now,
            "subtask": "", "chunk": "final", "claimed_by": self.reviewer["id"], "review_wait_started_at": now,
            "structure_revision": 0, "reviewed_commit": SHA, "result": "failed", "result_summary": "TASK DONE: NO — a short key can be guessed.",
            "structured_findings": findings, "evidence": "/evidence.txt",
        }
        request["certified_artifacts"] = {"delivery_ledger": {"sha256": "abc"}}   # frozen artifacts exist even for a NOT TESTED review
        if certified:
            request["challenge_execution"] = {"bundle": "/evidence/challenge.json", "identities": ["x"]}
            request["reviewer_simulations"] = "/evidence/challenge.json"
        if mirrored:
            request["mirror_ref"] = "refs/harness/mirror/x"
        with board.locked_state(self.root, operation="test") as state:
            state["qa_requests"][request_id] = request
            state.setdefault("review_escalations", {})[f"{task}|final"] = {
                "task": task, "key": f"{task}|final", "scope": {"phase": "final_acceptance", "subtask": "", "chunk": "final", "structure_revision": 0},
                "status": "could_not_finish", "failed_judgments": 5, "open_ids": [f["id"] for f in findings], "request_ids": [request_id],
                "outcome": "Could not finish: B2 still failed.", "settled_at": now, "opened_at": now,
            }
        return request

    def state(self):
        return board.snapshot(self.root)

    def card(self):
        cards = board.harness_cards_pass(self.root)
        return next(c for c in cards if c.get("choice_kind") == "review_stop" and c["task"] == TASK)


class R8CloseAFinding(OwnerDecisionCase):
    def test_closing_the_last_finding_releases_the_candidate_on_the_owners_decision(self):
        request = self.failed_review([_finding("B2", "A short owner key can be guessed."), _finding("B7", "The guide names a printed key.")])
        card = self.card()
        self.assertEqual([f["id"] for f in card["findings"]], ["B2", "B7"])
        self.assertTrue(all(f["closable_by_click"] for f in card["findings"]))
        first = board.owner_finding_close(self.root, TASK, request["id"], "B2")
        self.assertEqual(first["remaining"], ["B7"])
        refreshed = next(c for c in self.state()["owner_actions"].values() if c["status"] == "open" and c.get("choice_kind") == "review_stop")
        self.assertEqual([f["id"] for f in refreshed["findings"]], ["B7"], "the red card shows only what is still open, at once (Codex r1-1)")
        self.assertEqual(refreshed["choices"], ["Fix once more", "Take what you have", "Stop"])
        self.assertIsNone(self.state().get("owner_review_waivers", {}).get(TASK), "one finding left: no decision yet")
        second = board.owner_finding_close(self.root, TASK, request["id"], "B7")
        self.assertEqual(second["remaining"], [])
        self.assertIn("released on your decision", second["message"])
        st = self.state()
        waiver = st["owner_review_waivers"][TASK]
        self.assertEqual((waiver["request_id"], waiver["head_commit"], waiver["kind"], waiver["closed_findings"]), (request["id"], SHA, "closed_findings", ["B2", "B7"]))
        # the Reviewer's record is as written; every gate reads the waiver
        stored = st["qa_requests"][request["id"]]
        self.assertEqual((stored["status"], stored["result_summary"]), ("failed", request["result_summary"]))
        self.assertTrue(board.review_counts_as_passed(st, stored))
        self.assertTrue(board._final_pass_for(st, TASK, SHA))
        self.assertFalse(board._final_pass_for(st, TASK, "0" * 40), "never another candidate")
        self.assertIn(TASK, dict(board.unreleased_final_passes(st)), "release discovery finds the task")
        # the escalation is settled, the card is gone, the Delivery is completed as after a PASS
        self.assertEqual(st["review_escalations"][f"{TASK}|final"]["status"], "closed_by_owner")
        self.assertEqual([c for c in st["owner_actions"].values() if c["status"] == "open"], [])
        developer = st["agents"][self.builder["id"]]
        self.assertEqual((developer["status"], developer["active"]), ("done", False))
        kinds = [e["kind"] for e in st["events"]]
        self.assertIn("owner_review_waiver_recorded", kinds)
        self.assertIn("development_complete", kinds)
        self.assertTrue(st["release_lifecycle"][TASK].get("development_completed_at"))
        # the Story says so, and the decision survives a reload of the board
        findings, decision = story.owner_findings_and_decision(board.snapshot(self.root), TASK)
        self.assertEqual(findings, [])
        self.assertEqual(decision, "Released on your decision: you closed B2, B7.")
        self.assertIn("B2 (review-final-05)", board.owner_decisions_text(st, TASK))

    def test_a_harm_tagged_finding_takes_the_owners_words_not_a_click(self):
        request = self.failed_review([_finding("B9", "Passwords are written to the log.", harm_tag="leak")])
        card = self.card()
        self.assertFalse(card["findings"][0]["closable_by_click"])
        self.assertNotIn("Take what you have", card["choices"])
        with self.assertRaisesRegex(ValueError, "leak"):
            board.owner_finding_close(self.root, TASK, request["id"], "B9")
        with self.assertRaisesRegex(ValueError, "leak"):
            board.owner_finding_close(self.root, TASK, request["id"], "B9", words="no")
        result = board.owner_finding_close(self.root, TASK, request["id"], "B9", words="This app runs only on my machine and the log is mine; accepted as it is.")
        self.assertEqual(result["remaining"], [])
        recorded = self.state()["owner_closed_findings"][TASK][request["id"]]["B9"]
        self.assertEqual(recorded["words"], "This app runs only on my machine and the log is mine; accepted as it is.")
        self.assertTrue(self.state()["owner_review_waivers"].get(TASK))

    def test_a_review_whose_checks_never_ran_cannot_be_waived(self):
        request = self.failed_review([_finding("B2", "A short owner key can be guessed.")], certified=False)
        result = board.owner_finding_close(self.root, TASK, request["id"], "B2")
        self.assertIn("cannot be released on your decision", result["message"])
        self.assertIn("Fix once more", result["message"])
        self.assertIn("could not be tested", result["message"])
        self.assertIsNone(self.state().get("owner_review_waivers", {}).get(TASK))
        self.assertNotIn("Take what you have", self.card()["choices"], "a review whose checks never ran offers no release as it is")
        self.assertEqual(self.state()["agents"][self.builder["id"]]["status"], "independent_review_failed")

    def test_a_review_whose_checks_ran_and_failed_can_be_waived_but_a_timeout_cannot(self):
        # Codex task review r2-2: a one-row reviewer test that ran and asserted was called "never ran"
        request = self.failed_review([_finding("B1", "The counter shows 1 instead of 100.")], certified=False)
        with board.locked_state(self.root, operation="test") as state:
            state["qa_requests"][request["id"]]["challenge_execution_attempts"] = [
                {"status": "failed", "recorded_at": "2026-11-08T17:00:00+00:00", "completed": True, "error_type": "ScenarioSimulationsFailed", "failed_scenarios": ["S-001"],
                 "reason": "Scenario Ledger simulations failed: S-001: AssertionError: 'Welcome' not found in '1' : Required heading not found / Ran 1 test / FAILED (failures=1)"}]
        self.assertIn("Take what you have", self.card()["choices"], "checks that ran to a FAIL were run")
        result = board.owner_finding_close(self.root, TASK, request["id"], "B1")
        self.assertIn("released on your decision", result["message"])
        self.assertTrue(self.state()["owner_review_waivers"].get(TASK))
        st = self.state()
        ok, note = board.reviewer_checks_satisfied(st, st["qa_requests"][request["id"]], False)
        self.assertTrue(ok, "the release check accepts the completed failing run under the owner's waiver (Codex r3-1)")
        self.assertIn("released on the owner's decision", note)
        # a run that never started is still untested, whatever words its reason carries (Codex r3-2)
        other = self.failed_review([_finding("B1", "Elsewhere.")], certified=False, request_id="review-other-01", task=OTHER, cycle=1)
        with board.locked_state(self.root, operation="test") as state:
            state["qa_requests"][other["id"]]["challenge_execution_attempts"] = [
                {"status": "failed", "recorded_at": "2026-11-08T17:00:00+00:00", "completed": False, "error_type": "ValueError", "reason": "Ran 0 tests; FAILED: the check timed out after 300 seconds"}]
        result = board.owner_finding_close(self.root, OTHER, other["id"], "B1")
        self.assertIn("could not be tested", result["message"])
        self.assertNotIn(OTHER, self.state().get("owner_review_waivers", {}))

    def test_a_decision_on_a_version_the_builder_has_moved_past_is_kept_but_releases_nothing(self):
        # Codex task review r2-1: Delivery committed its repair before the owner closed the last finding of the earlier review
        import subprocess
        repo = Path(self._tmp.name) / "work"; repo.mkdir()
        env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@x", "HOME": self._tmp.name, "PATH": "/usr/bin:/bin"}
        subprocess.run(["git", "init", "-q", str(repo)], check=True, env=env)
        (repo / "a.txt").write_text("reviewed\n"); subprocess.run(["git", "-C", str(repo), "add", "a.txt"], check=True, env=env)
        subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "reviewed"], check=True, env=env)
        reviewed = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], check=True, capture_output=True, text=True, env=env).stdout.strip()
        request = self.failed_review([_finding("B2", "A short owner key can be guessed.")])
        with board.locked_state(self.root, operation="test") as state:
            state["qa_requests"][request["id"]]["reviewed_commit"] = reviewed
            state.setdefault("task_workspaces", {})[TASK] = str(repo)
        self.assertIn("Take what you have", self.card()["choices"], "saved work still is the reviewed version")
        (repo / "a.txt").write_text("repaired\n"); subprocess.run(["git", "-C", str(repo), "commit", "-q", "-am", "repair"], check=True, env=env)
        card = self.card()
        self.assertNotIn("Take what you have", card["choices"])
        self.assertIn("changed the work since that review", card["why"])
        result = board.owner_finding_close(self.root, TASK, request["id"], "B2")
        self.assertIn("changed the work since that review", result["message"])
        st = self.state()
        self.assertNotIn(TASK, st.get("owner_review_waivers", {}), "nothing old is released")
        self.assertEqual(st["agents"][self.builder["id"]]["status"], "independent_review_failed", "the builder keeps working")
        self.assertIn("B2 (review-final-05)", board.owner_decisions_text(st, TASK), "the decision carries to the next review")

    def test_the_same_finding_id_on_another_task_or_an_older_review_is_untouched(self):
        request = self.failed_review([_finding("B2", "A short owner key can be guessed.")])
        other = self.failed_review([_finding("B2", "Another B2 elsewhere.")], request_id="review-other-01", task=OTHER, cycle=1)
        board.owner_finding_close(self.root, TASK, request["id"], "B2")
        st = self.state()
        self.assertEqual(board.open_findings(st, OTHER, st["qa_requests"][other["id"]])[0]["id"], "B2", "the other task's B2 is still open")
        self.assertNotIn(OTHER, st.get("owner_review_waivers", {}))
        with self.assertRaisesRegex(ValueError, "newer review"):
            # a newer review exists now: closing on the old one is refused
            self.failed_review([_finding("B3", "New.")], request_id="review-final-06", cycle=6)
            board.owner_finding_close(self.root, TASK, request["id"], "B2")

    def test_the_old_choice_words_still_work(self):
        # the CTO may now close the card too (owner 2026-11-12, D-018; test_cto_closes_harness_cards.py); the owner's own choice is unchanged
        request = self.failed_review([_finding("B2", "A short owner key can be guessed.")])
        card = self.card()
        with mock.patch("harness.control.enqueue_instruction", return_value={"id": "q1"}):
            result = board.owner_action_choose(self.root, card["id"], "Try once more")   # the old name is an alias of Fix once more
        self.assertIn("Fix once more", result["message"])
        self.assertEqual(self.state()["review_escalations"][f"{TASK}|final"]["status"], "watching")
        self.assertEqual(self.state()["owner_actions"][card["id"]]["outcome"], "You chose: Fix once more")


class R9TakeWhatYouHaveAndTheUsersLine(OwnerDecisionCase):
    def test_take_what_you_have_releases_the_executed_version_with_the_findings_left_open(self):
        request = self.failed_review([_finding("B2", "A short owner key can be guessed."), _finding("B7", "The guide names a printed key.")])
        card = self.card()
        self.assertEqual(card["choices"], ["Fix once more", "Take what you have", "Stop"])
        result = board.owner_action_choose(self.root, card["id"], "Take what you have")
        self.assertIn("released on your decision", result["message"])
        self.assertIn("leaving open: B2, B7", result["message"])
        st = self.state()
        waiver = st["owner_review_waivers"][TASK]
        self.assertEqual((waiver["kind"], waiver["left_open"], waiver["head_commit"]), ("take_what_you_have", ["B2", "B7"], SHA))
        self.assertTrue(board._final_pass_for(st, TASK, SHA))
        self.assertEqual(st["agents"][self.builder["id"]]["status"], "done")
        self.assertEqual(st["owner_actions"][card["id"]]["status"], "done")
        self.assertEqual(story.owner_findings_and_decision(st, TASK)[1], "Released on your decision; left open: B2, B7.")

    def test_take_what_you_have_is_not_offered_for_an_uncertified_version_or_a_harm_finding(self):
        self.failed_review([_finding("B2", "A short owner key can be guessed.")], mirrored=False)
        card = self.card()
        self.assertEqual(card["choices"], ["Fix once more", "Stop"])
        self.assertIn("never mirrored", card["why"])
        with self.assertRaisesRegex(ValueError, "choose one of|not possible"):   # the card does not offer it, so the board refuses it
            board.owner_action_choose(self.root, card["id"], "Take what you have")
        board.owner_action_choose(self.root, card["id"], "Give up")   # the old name is an alias of Stop
        self.assertTrue(self.state()["review_escalations"][f"{TASK}|final"].get("given_up_at"))

    def test_every_contract_has_a_users_line_the_owners_own_or_the_default(self):
        self.assertEqual(board.users_line("Requirement 1: a page.\n"), board.USERS_LINE_DEFAULT)
        stated = "Requirement 1: a shop.\nUsers: my two colleagues and me, on the office network.\nSecurity: none needed.\n"
        self.assertEqual(board.users_line(stated), "Users: my two colleagues and me, on the office network.")
        self.assertEqual(board.users_line("- Users:  the whole street  \n"), "Users: the whole street")
        self.assertIn("solo owner", board.USERS_LINE_DEFAULT)

if __name__ == "__main__":
    unittest.main()
