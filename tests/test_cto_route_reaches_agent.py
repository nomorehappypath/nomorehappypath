# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""The CTO's route reaches the agent it is for, and a final review that names a superseded version is not recycled.

Found by the independent reviewer's whole-app walk on 2026-11-05 (R3-1 of task 2026-11-05_blocked-escalation), on a normal
correction during review: Delivery fixed a test while its final review was pending; the request kept naming the old commit,
so each Reviewer gave it back as obsolete and a replacement Reviewer was started over the same request; the CTO twice recorded
a route telling Delivery to request the review again, but a route was only a board event read "on the next poll", and a waiting
agent never polls. The project stayed active with nobody working and no card for the owner.

Tested here, on a real temporary board (and a real git workspace for the review part):
  * a route disposition is typed to the agent it is for, complete: the note's first word names the role (Delivery or
    Reviewer); with no role named it goes to the Delivery agent of the judged task, or to the Reviewer for an item about a
    blocked Reviewer; any other outcome types nothing;
  * a final review given back while the task's saved version has moved past the one it names is withdrawn, its scope is
    free, and Delivery is told to request the review again on what is saved now; one that still names the saved version
    stays open for another Reviewer, as before.

Run:  PYTHONPATH=. python3 -m unittest tests.test_cto_route_reaches_agent -v
"""
import subprocess
import tempfile
import unittest
from pathlib import Path

from harness import board, control, cto_wakes
from tests.test_no_silent_stall import _Stall
from tests.test_self_restart import _ago

ROUTE = "Delivery: the open review names old 37fc20c; file a NEW final review request on 2531d34 and make no further edits."


def queued(root: Path, session_id: str) -> list[dict]:
    """The instructions waiting to be typed into a terminal (taking them, as the supervisor does)."""
    return control.take_instructions(root, session_id)


class TheCtoRouteIsTypedToItsAgent(_Stall):
    def stuck_item(self, task: str) -> dict:
        return next(item for item in cto_wakes.open_items(board.snapshot(self.root)) if item["task"] == task and item["reason"] == "stuck")

    def test_a_route_for_delivery_wakes_delivery_with_the_whole_note(self):
        delivery_session, builder, _ = self.delivery_with_task("review-counter")
        reviewer_session, _, _ = self.reviewer("review-counter")
        _, cto, _ = self.cto()
        board.status(self.root, builder["id"], "Waiting for the active review to finish, then I resubmit.", "blocked")
        queued(self.root, delivery_session["id"]), queued(self.root, reviewer_session["id"])      # start from empty queues
        result = board.record_cto_disposition(self.root, cto["id"], self.stuck_item("review-counter")["id"], "route", ROUTE)
        self.assertEqual(result["routed_to"], [builder["id"]])
        typed = queued(self.root, delivery_session["id"])
        self.assertEqual(len(typed), 1)
        self.assertEqual(typed[0]["source"], "cto-route")
        self.assertIn(ROUTE, typed[0]["text"], "the note arrives complete, not as a summary")
        self.assertIn("THE CTO ROUTED THIS TO YOU (task review-counter)", typed[0]["text"])
        self.assertEqual(queued(self.root, reviewer_session["id"]), [], "the Reviewer is not typed a note addressed to Delivery")
        routed = [e for e in self.events("instruction_route_queued") if e.get("source") == "cto-route"]
        self.assertEqual([e["agent_id"] for e in routed], [builder["id"]])

    def test_a_route_that_names_the_reviewer_goes_to_the_reviewer(self):
        delivery_session, builder, _ = self.delivery_with_task("review-counter")
        reviewer_session, reviewer, _ = self.reviewer("review-counter")
        _, cto, _ = self.cto()
        board.status(self.root, builder["id"], "The Reviewer's stored check has the wrong path.", "blocked")
        queued(self.root, delivery_session["id"]), queued(self.root, reviewer_session["id"])
        note = "Reviewer: correct your stored check S-QA-004 to app/start.sh, then run the review."
        result = board.record_cto_disposition(self.root, cto["id"], self.stuck_item("review-counter")["id"], "route", note)
        self.assertEqual(result["routed_to"], [reviewer["id"]])
        self.assertIn(note, queued(self.root, reviewer_session["id"])[0]["text"])
        self.assertEqual(queued(self.root, delivery_session["id"]), [])

    def test_with_no_role_named_the_item_decides(self):
        _, builder, _ = self.delivery_with_task("review-counter")
        _, reviewer, _ = self.reviewer("review-counter")
        about_delivery = [{"task": "review-counter", "subject": "delivery_blocked:review-counter"}]
        about_reviewer = [{"task": "REVIEW_QUEUE", "subject": "reviewer_blocked:REVIEW_QUEUE"}]
        state = board.snapshot(self.root)
        self.assertEqual([a for a, _ in board._route_addressees(state, about_delivery, "Use the second test machine.")], [builder["id"]])
        self.assertEqual(board._route_addressees(state, about_reviewer, "Use the second test machine."), [],
                         "no Reviewer says it is blocked: nobody is guessed")
        with board.locked_state(self.root) as locked:
            locked["agents"][reviewer["id"]].update({"status": "blocked", "task": "REVIEW_QUEUE"})
        state = board.snapshot(self.root)
        self.assertEqual([a for a, _ in board._route_addressees(state, about_reviewer, "Use the second test machine.")], [reviewer["id"]])
        self.assertEqual(board._route_addressees(state, [{"task": "another-task", "subject": "x"}], "Do this."), [],
                         "nobody live on that task: nothing is typed to the wrong agent")

    def two_reviews(self) -> tuple[dict, dict, dict, dict, dict]:
        """Two tasks under review at once, each reserved by its own Reviewer (the reviewer's round-1 scenario).
        Returns the counter Reviewer's session and agent, the weather Reviewer's session, the CTO and the counter's stuck item."""
        _, counter_builder, _ = self.delivery_with_task("counter")
        _, weather_builder, _ = self.delivery_with_task("weather")
        counter_session, counter_reviewer, _ = self.reviewer("counter")
        weather_session, weather_reviewer, _ = self.reviewer("weather")
        with board.locked_state(self.root) as state:
            template = state["qa_requests"].pop("review-1")
            for task, developer, reviewer in (("counter", counter_builder, counter_reviewer), ("weather", weather_builder, weather_reviewer)):
                state["qa_requests"]["review-" + task] = {
                    **template, "id": "review-" + task, "task": task, "developer_id": developer["id"],
                    "status": "reserved", "reserved_by": reviewer["id"], "reserved_at": _ago(30), "routed_to": reviewer["id"]}
        _, cto, _ = self.cto()                     # the CTO must exist for the blocked status to open its item
        board.status(self.root, counter_builder["id"], "The counter Reviewer needs the correct rendered test path.", "blocked")
        queued(self.root, counter_session["id"]), queued(self.root, weather_session["id"])
        return counter_session, counter_reviewer, weather_session, cto, self.stuck_item("counter")

    def test_with_two_reviews_running_only_the_judged_task_s_reviewer_is_typed(self):
        counter_session, counter_reviewer, weather_session, cto, item = self.two_reviews()
        note = "Reviewer: use counter/tests/rendered.py for this task and finish the current review."
        result = board.record_cto_disposition(self.root, cto["id"], item["id"], "route", note)
        self.assertEqual(result["routed_to"], [counter_reviewer["id"]])
        self.assertIn(note, queued(self.root, counter_session["id"])[0]["text"])
        self.assertEqual(queued(self.root, weather_session["id"]), [], "the weather review is not typed the counter's instruction")

    def test_when_nobody_live_is_responsible_nothing_is_typed_to_anybody_else(self):
        _, counter_reviewer, weather_session, cto, item = self.two_reviews()
        board.offline(self.root, counter_reviewer["id"], "visible CLI terminal ended", transport_ended=True)
        result = board.record_cto_disposition(self.root, cto["id"], item["id"], "route", "Reviewer: use counter/tests/rendered.py.")
        self.assertEqual(result["routed_to"], [])
        self.assertIn("typed to nobody", result["not_routed"])
        self.assertEqual(queued(self.root, weather_session["id"]), [])
        self.assertEqual([e for e in self.events("instruction_route_queued") if e.get("source") == "cto-route"], [])

    def test_an_outcome_that_is_not_a_route_types_nothing(self):
        delivery_session, builder, _ = self.delivery_with_task("review-counter")
        _, cto, _ = self.cto()
        board.status(self.root, builder["id"], "Waiting for the active review to finish.", "blocked")
        queued(self.root, delivery_session["id"])
        result = board.record_cto_disposition(self.root, cto["id"], self.stuck_item("review-counter")["id"], "nothing", "It is working; nothing to route.")
        self.assertNotIn("routed_to", result)
        self.assertNotIn("not_routed", result)
        self.assertEqual(queued(self.root, delivery_session["id"]), [])


class ASupersededFinalReviewIsWithdrawnNotRecycled(_Stall):
    def setUp(self):
        super().setUp()
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.workspace = Path(holder.name).resolve()
        self.git("init", "-q")
        self.git("config", "user.email", "test@example.invalid")
        self.git("config", "user.name", "Test")
        (self.workspace / "app.py").write_text("print('one')\n")
        self.git("add", "app.py")
        self.git("commit", "-qm", "the version the review was requested on")
        self.named = self.git("rev-parse", "HEAD")

    def git(self, *args: str) -> str:
        return subprocess.run(["git", *args], cwd=self.workspace, check=True, capture_output=True, text=True).stdout.strip()

    def reserved_final_review(self, phase: str = "final_acceptance") -> tuple[dict, dict, dict]:
        delivery_session, builder, _ = self.delivery_with_task("review-counter")
        _, reviewer, _ = self.reviewer("review-counter")
        with board.locked_state(self.root) as state:
            state.setdefault("task_workspaces", {})["review-counter"] = str(self.workspace)
            state["qa_requests"]["review-1"].update({
                "task": "review-counter", "phase": phase, "status": "reserved", "reserved_by": reviewer["id"], "reserved_at": _ago(60),
                "developer_id": builder["id"], "reviewed_commit": self.named})
        queued(self.root, delivery_session["id"])
        return delivery_session, builder, reviewer

    def correct_the_work(self) -> str:
        (self.workspace / "app.py").write_text("print('two')\n")
        self.git("commit", "-qam", "a correction made while the review was pending")
        return self.git("rev-parse", "HEAD")

    def test_a_given_back_review_that_names_an_older_version_is_withdrawn_and_delivery_is_told(self):
        delivery_session, builder, reviewer = self.reserved_final_review()
        saved = self.correct_the_work()
        result = board.relinquish_review_reservation(
            self.root, reviewer["id"], "review-1", "The saved page changed during review preparation; it needs a fresh review assignment.")
        self.assertTrue(result.get("superseded"))
        request = board.snapshot(self.root)["qa_requests"]["review-1"]
        self.assertEqual(request["status"], "withdrawn", "not open for another Reviewer")
        self.assertIn(self.named[:12], request["withdrawn_reason"])
        self.assertIn(saved[:12], request["withdrawn_reason"])
        withdrawn = self.events("review_withdrawn_superseded")
        self.assertEqual(len(withdrawn), 1)
        self.assertEqual((withdrawn[0]["named_commit"], withdrawn[0]["saved_commit"]), (self.named, saved))
        typed = queued(self.root, delivery_session["id"])
        self.assertEqual([i["source"] for i in typed], ["review-superseded"])
        self.assertIn("REVIEW SUPERSEDED: request review-1", typed[0]["text"])
        self.assertIn("run request-review again", typed[0]["text"])
        self.assertIn(saved[:12], typed[0]["text"])

    def test_a_given_back_review_that_still_names_the_saved_version_stays_open_for_another_reviewer(self):
        delivery_session, _, reviewer = self.reserved_final_review()
        result = board.relinquish_review_reservation(self.root, reviewer["id"], "review-1", "My own browser tool cannot start here.")
        self.assertNotIn("superseded", result)
        self.assertEqual(board.snapshot(self.root)["qa_requests"]["review-1"]["status"], "open")
        self.assertEqual(self.events("review_withdrawn_superseded"), [])
        self.assertEqual(queued(self.root, delivery_session["id"]), [])

    def test_a_chunk_review_is_left_to_its_own_rules(self):
        _, _, reviewer = self.reserved_final_review(phase="chunk")
        self.correct_the_work()
        board.relinquish_review_reservation(self.root, reviewer["id"], "review-1", "The saved page changed during review preparation.")
        self.assertEqual(board.snapshot(self.root)["qa_requests"]["review-1"]["status"], "open")


if __name__ == "__main__":
    unittest.main()
