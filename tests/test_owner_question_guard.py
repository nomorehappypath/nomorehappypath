# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""2026-10-15 01:19Z, Studio project: the CTO had the owner's waiver and its directive's order and still ended
its explanation with "Want me to do that now?". Nothing moved until the owner, who happened to be at the
screen, answered. Owner: "do the necessary and do not ask me such questions, if i was not infront of the
screen we will be waiting and wasting time until tomorrow".

The board now enforces it on every CTO-to-owner surface it has (the status note and the owner-action card):
- a question about something inside the CTO's own authority is handed back ("act, then report"), not shown;
- any other question must state a default (inside the CTO's authority) and a deadline, or it is refused;
- when the deadline passes the board routes the CTO to do the default and only then closes the card;
- the owner sees what happened in "What the harness did for you" instead of a question.
"""
from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from harness import board, board_viewer, control, global_settings

ROOT = Path(__file__).resolve().parents[1]
AUTHORITY_QUESTION = "Want me to push Delivery to file the amendment reflecting your waiver now?"
WHAT_QUESTION = "Which of the two decks should ship?"
SAFE_DEFAULT = "continue with deck A as the working candidate"
WHAT_QUESTION_WITH_DEFAULT = f"{WHAT_QUESTION} If no answer in 10 minutes I will {SAFE_DEFAULT}."


class _Fixture(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.session = control.create(self.root, "claude_cto")
        self.cto = board.register(self.root, "cto", "GLOBAL_MONITOR", vendor="Anthropic", session_id=self.session["id"])
        delivery_session = control.create(self.root, "codex_delivery")
        self.delivery = board.register(
            self.root, "engineering", board.AWAITING_OWNER_DIRECTION, vendor="OpenAI", session_id=delivery_session["id"],
        )
        # tolerant of main before this change, so the regression proof fails on substance, not on the fixture
        configure = getattr(board, "configure_owner_questions", lambda _section: None)
        configure(None)
        self.addCleanup(configure, None)

    def events(self, kind: str) -> list[dict]:
        return [event for event in board.snapshot(self.root)["events"] if event["kind"] == kind]

    def open_cards(self) -> list[dict]:
        return [card for card in board.snapshot(self.root).get("owner_actions", {}).values() if card["status"] == "open"]

    def card(self, action_id: str) -> dict:
        return board.snapshot(self.root)["owner_actions"][action_id]

    def cto_agent(self) -> dict:
        return board.snapshot(self.root)["agents"][self.cto["id"]]

    def pin(self, title: str, **kwargs) -> dict:
        return board.record_owner_action(self.root, self.cto["id"], title, **kwargs)

    def note(self, text: str, state: str = "working") -> dict:
        return board.status(self.root, self.cto["id"], text, state)

    def overdue(self, action_id: str) -> None:
        with board.locked_state(self.root) as state:
            state["owner_actions"][action_id]["default_due_at"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()

    def harness_lines(self) -> str:
        return json.dumps(board_viewer._compact_dashboard_state(board.snapshot(self.root))["harness_actions"])


class HandBackTests(_Fixture):
    def test_a_question_inside_the_cto_authority_is_handed_back_and_never_shown(self):
        with self.assertRaisesRegex(ValueError, "act, then report") as refused:
            self.note(AUTHORITY_QUESTION, "waiting")
        self.assertIn("inside your authority", str(refused.exception))
        self.assertNotEqual(self.cto_agent().get("status_note"), AUTHORITY_QUESTION, "the question was stored as the CTO's status")
        self.assertEqual(self.open_cards(), [], "a handed-back question must not become a card")
        self.assertEqual(len(self.events("status_update")), 0)
        handed = self.events("cto_question_handed_back")
        self.assertEqual(len(handed), 1, "the hand-back persists although the command was refused")
        self.assertEqual(handed[0]["question"], AUTHORITY_QUESTION)
        self.assertEqual(self.cto_agent()["handed_back_question"]["text"], AUTHORITY_QUESTION)

    def test_a_question_card_inside_the_cto_authority_is_handed_back(self):
        for title in (
            "Should I clear the browser-permission card now?",
            "Shall I route Delivery to the existing deck?",
            "Do you want me to resolve the nine findings about the fresh run?",
            "Waiting for your permission to route Delivery to the deck",
            "Can I mark the task-brief length finding as not material?",
        ):
            with self.assertRaisesRegex(ValueError, "act, then report", msg=title):
                self.pin(title, kind="decision")
            with self.assertRaisesRegex(ValueError, "act, then report", msg=title):
                self.note("OWNER ACTION: " + title)
        self.assertEqual(self.open_cards(), [])

    def test_an_authority_question_is_handed_back_even_when_it_offers_a_default(self):
        # review 2026-10-14, blocking 2: the default must not buy a routine question a seat in the owner's queue
        question = "Should I route Delivery to file the amendment? If no answer in 10 minutes I will route Delivery to file the amendment."
        with self.assertRaisesRegex(ValueError, "act, then report"):
            self.note(question, "waiting")
        with self.assertRaisesRegex(ValueError, "act, then report"):
            self.pin("Should I route Delivery to file the amendment?", default="route Delivery to file the amendment")
        self.assertEqual(self.open_cards(), [])
        self.assertEqual(len(self.events("cto_question_handed_back")), 2)

    def test_the_report_after_a_hand_back_is_recorded_as_the_cto_acting(self):
        with self.assertRaises(ValueError):
            self.note(AUTHORITY_QUESTION, "waiting")
        self.note("Routed Delivery to file the amendment for the waived fresh run; re-review requested.")
        acted = self.events("cto_acted")
        self.assertEqual(len(acted), 1)
        self.assertEqual(
            acted[0]["message"],
            "CTO acted: Routed Delivery to file the amendment for the waived fresh run; re-review requested. "
            "(its own report after the board handed back a question)",
        )
        self.assertFalse(self.cto_agent().get("handed_back_question"))
        self.assertIn("CTO acted: Routed Delivery", self.harness_lines())
        # a second plain report is just a report
        self.note("Delivery filed the amendment; Reviewer re-running.")
        self.assertEqual(len(self.events("cto_acted")), 1)

    def test_a_question_naming_an_owner_only_step_is_never_handed_back(self):
        # "stalled" is in the CTO's authority, but installing a security tool is the owner's (directive): a real question
        with self.assertRaisesRegex(ValueError, "state the default"):
            self.note("May I install a security tool to diagnose the stalled Reviewer?", "waiting")
        self.assertEqual(self.events("cto_question_handed_back"), [])

    def test_a_wholly_quoted_question_is_still_a_question_and_the_owner_machine_is_the_owner_s(self):
        with self.assertRaisesRegex(ValueError, "act, then report"):
            self.note('"Want me to file the amendment?"', "waiting")
        # a bare noun is not authority: the owner's host, a physical device, an explanation are the owner's
        for question in (
            "Can you restart the stalled database on your machine?",
            "Can you reproduce the finding on the physical GPU?",
            "Can you reproduce findings about the physical GPU?",
            "Can you explain the requirement about the fresh run?",
            "Is the stalled Reviewer on your side?",
            'Should I route Delivery to "publish the result"?',
        ):
            with self.assertRaisesRegex(ValueError, "state the default", msg=question):
                self.note(question, "waiting")
        self.assertEqual(len(self.events("cto_question_handed_back")), 1)

    def test_a_question_in_the_card_reason_is_guarded_too_and_the_command_is_not_read(self):
        with self.assertRaisesRegex(ValueError, "act, then report"):
            self.pin("Browser permission card", why="Should I clear the card now?")
        card = self.pin("Run the film GPU proof", command="bash /tmp/release_film.sh  # ship it? no, just the proof")
        self.assertEqual(card["status"], "open")

    def test_a_second_sweeper_does_not_dispatch_a_card_another_sweep_just_claimed(self):
        card = self.pin("Which deck ships?", kind="decision", default=SAFE_DEFAULT)
        self.overdue(card["id"])
        with board.locked_state(self.root) as state:
            state["owner_actions"][card["id"]]["default_dispatch"] = {"status": "queueing", "attempts": 1, "claimed_at": board.now()}
        with patch("harness.control.enqueue_instruction") as enqueue:
            board.mark_stalled(self.root)
        enqueue.assert_not_called()
        with board.locked_state(self.root) as state:
            state["owner_actions"][card["id"]]["default_dispatch"]["claimed_at"] = (datetime.now(timezone.utc) - timedelta(seconds=board.OWNER_DEFAULT_CLAIM_SECONDS + 1)).isoformat()
        with patch("harness.control.enqueue_instruction", return_value={"id": "d-5", "source": "owner-default"}) as enqueue:
            board.mark_stalled(self.root)
        self.assertEqual(enqueue.call_count, 1, "a stale claim is released by time")

    def test_a_card_cleared_while_the_sweep_was_queueing_is_not_closed_over(self):
        card = self.pin("Which deck ships?", kind="decision", default=SAFE_DEFAULT)
        self.overdue(card["id"])

        def clear_meanwhile(*_args, **_kwargs):
            board.clear_owner_action(self.root, self.cto["id"], card["id"], "owner chose deck B in its terminal")
            return {"id": "d-7", "source": "owner-default"}

        with patch("harness.control.enqueue_instruction", side_effect=clear_meanwhile):
            board.mark_stalled(self.root)
        self.assertEqual(self.card(card["id"])["outcome"], "owner chose deck B in its terminal", "the newer disposition stands")
        self.assertEqual(self.events("owner_default_applied"), [])

    def test_a_cto_terminal_showing_a_trust_or_permission_menu_is_not_routed(self):
        from harness import attention
        card = self.pin("Which deck ships?", kind="decision", default=SAFE_DEFAULT)
        self.overdue(card["id"])
        with control.locked_state(self.root) as state:
            state["sessions"][self.session["id"]]["attention_reason"] = attention.PERMISSION_REASON
        with patch("harness.control.enqueue_instruction") as enqueue:
            board.mark_stalled(self.root)
        enqueue.assert_not_called()
        self.assertIn("menu the owner must answer", self.card(card["id"])["default_dispatch"]["last_error"])

    def test_a_what_marker_wins_over_the_authority_list(self):
        # "amend" is in the CTO's authority, but WHICH requirement is the owner's call: a real question, default required
        with self.assertRaisesRegex(ValueError, "state the default"):
            self.note("Which requirement should I amend, the fresh run or the deck count?", "waiting")
        self.assertEqual(self.events("cto_question_handed_back"), [])
        self.note("Which requirement should I amend, the fresh run or the deck count? If no answer in 15 minutes I will amend the fresh-run line.", "waiting")
        self.assertEqual(self.open_cards()[0]["default_action"], "amend the fresh-run line")

    def test_owner_only_cards_quoted_questions_and_plain_reports_are_accepted_unchanged(self):
        # the standing owner-only cards from live projects (2026-10-08, 2026-10-14) are not questions
        for title in (
            "Reviewer is not answering: open its terminal, press Enter once; if blank, type /login",
            "Optional: Reviewer real-AI test needs your own Claude setup-token (never paste it in chat)",
            "Check Maya's crash fix with View app, then Accept or Reject",
            "Run the film GPU proof",
            "Requirements decision needed: choose Go ahead or Modify in Mission Control",
        ):
            card = self.pin(title, command="bash /tmp/x.sh" if "proof" in title else "")
            self.assertEqual(card["status"], "open", title)
            self.assertNotIn("default_action", card)
        self.assertEqual(len(self.open_cards()), 5)
        # a report that merely contains authority words, quotes a question or cites a URL is a report
        for report in (
            "Routed Delivery to the existing deck; cleared the browser card (the cause was its own tool).",
            'Delivery asked "should I amend the requirement?"; I answered: yes, routed it to file the amendment.',
            "Review brief at http://127.0.0.1:8900/brief?task=deck&view=full; Reviewer claimed it.",
            "I'll route Delivery back to the deck once the Reviewer's verdict lands.",
        ):
            self.assertEqual(self.note(report)["kind"], "status_update", report)
        self.assertEqual(self.events("cto_question_handed_back"), [])
        self.assertEqual(len(self.open_cards()), 5)


class DefaultWhenSilentTests(_Fixture):
    def test_a_what_question_without_a_default_is_refused(self):
        with self.assertRaisesRegex(ValueError, "act, then report, or state the default") as refused:
            self.note(WHAT_QUESTION, "waiting")
        self.assertIn("if no answer in N minutes I will", str(refused.exception))
        self.assertIn("at most 15 minutes", str(refused.exception))
        self.assertEqual(self.open_cards(), [])
        self.assertEqual(self.events("status_update"), [])

    def test_a_question_card_without_a_default_is_refused_and_with_one_carries_the_deadline(self):
        with self.assertRaisesRegex(ValueError, "state the default"):
            self.pin("Should the deck keep the 48 slides?", kind="decision")
        card = self.pin("Should the deck keep the 48 slides?", kind="decision", default="keep the 48 slides as the working candidate")
        self.assertEqual(card["default_action"], "keep the 48 slides as the working candidate")
        self.assertEqual(card["default_minutes"], 15)
        self.assertEqual(card["kind"], "decision")
        due = datetime.fromisoformat(card["default_due_at"])
        recorded = datetime.fromisoformat(card["recorded_at"])
        self.assertEqual(due - recorded, timedelta(minutes=15))
        compact = board_viewer._compact_dashboard_state(board.snapshot(self.root))
        footer = compact["owner_actions"][card["id"]]["clears_when"]
        self.assertTrue(footer.startswith("If you do not answer by "), footer)
        self.assertIn("the CTO will keep the 48 slides as the working candidate", footer)

    def test_a_what_question_with_a_default_clause_is_one_decision_card(self):
        event = self.note(WHAT_QUESTION_WITH_DEFAULT, "waiting")
        self.assertEqual(event["kind"], "status_update")
        cards = self.open_cards()
        self.assertEqual(len(cards), 1)
        card = cards[0]
        self.assertEqual((card["kind"], card["default_action"], card["default_minutes"]), ("decision", SAFE_DEFAULT, 10))
        self.assertEqual(card["title"], WHAT_QUESTION)

    def test_a_default_outside_the_cto_authority_is_refused_but_a_prohibition_is_not_an_action(self):
        card = self.pin("Which deck ships?", default="continue with deck A and do not publish anything; leave the release to you")
        self.assertEqual(card["status"], "open")
        ready = self.pin("Should the review re-run first?", default="run record-release once the review passes and mark the release readiness")
        self.assertEqual(ready["status"], "open", "the CTO's own release-readiness step is not the owner's release")
        with self.assertRaisesRegex(ValueError, "something you may do yourself"):
            self.pin("Should I wait?", default="investigate; never wait unnecessarily; publish the result")
        with self.assertRaisesRegex(ValueError, "something you may do yourself"):
            self.pin("Should I wait longer?", default="investigate without delay and publish the result")
        with self.assertRaisesRegex(ValueError, "something you may do yourself"):
            self.pin("Should I leave now?", default="leave and publish the result")
        # ordinary engineering words are not the owner's decisions
        for default in ("modify the implementation of the deck export", "install project dependencies and rerun the tests", "delete the temporary test file"):
            self.assertEqual(self.pin(f"Should Delivery {default}?", default=default)["status"], "open", default)
        for default in ("ship deck A", "accept the task for the owner", "deploy deck A to production", "delete the project folder"):
            with self.assertRaisesRegex(ValueError, "something you may do yourself", msg=default):
                self.pin(WHAT_QUESTION, default=default)
            with self.assertRaisesRegex(ValueError, "something you may do yourself", msg=default):
                self.note(f"{WHAT_QUESTION} If no answer in 10 minutes I will {default}.", "waiting")
        self.assertEqual(len(self.open_cards()), 5)

    def test_a_deadline_longer_than_the_setting_is_refused_not_capped(self):
        with self.assertRaisesRegex(ValueError, "from 1 to 15"):
            self.note(f"{WHAT_QUESTION} If no answer in 240 minutes I will {SAFE_DEFAULT}.", "waiting")
        with self.assertRaisesRegex(ValueError, "from 1 to 15"):
            self.pin(WHAT_QUESTION, default=SAFE_DEFAULT, default_minutes=240)
        self.assertEqual(self.open_cards(), [])

    def test_conflicting_defaults_are_refused_and_a_path_in_the_default_survives(self):
        with self.assertRaisesRegex(ValueError, "state the default once"):
            self.pin(WHAT_QUESTION_WITH_DEFAULT, default=SAFE_DEFAULT)
        with self.assertRaisesRegex(ValueError, "different deadlines"):
            self.pin(WHAT_QUESTION_WITH_DEFAULT, default_minutes=5)
        # a quoted phrase inside the CTO's own clause is part of the action
        self.note('Which candidate? If no answer in 5 minutes I will route Delivery to "repair candidate A" and report.', "waiting")
        by_title = {card["title"].split("?")[0]: card for card in self.open_cards()}
        self.assertEqual(by_title["Which candidate"]["default_action"], 'route Delivery to "repair candidate A" and report')
        self.assertEqual(self.pin("Should the parser change?", default="investigate the token parser and report")["status"], "open")
        # a quoted clause is reported speech, not the CTO's promise: the live question still needs its own default
        with self.assertRaisesRegex(ValueError, "state the default"):
            self.note('Which candidate? Reviewer wrote "if no answer in 5 minutes I will investigate."', "waiting")
        with self.assertRaisesRegex(ValueError, "state one default"):
            self.note(f"{WHAT_QUESTION} If no answer in 5 minutes I will wait. If no answer in 10 minutes I will {SAFE_DEFAULT}.", "waiting")
        self.note("Which log should Delivery attach? If no answer in 5 minutes I will attach logs/run.2026-10-15.log and continue. Only the existing sandbox.", "waiting")
        by_title = {card["title"].split("?")[0]: card for card in self.open_cards()}
        self.assertEqual(by_title["Which log should Delivery attach"]["default_action"], "attach logs/run.2026-10-15.log and continue. Only the existing sandbox", "the whole stated action is kept, constraints included")

    def test_two_different_questions_are_two_cards_and_a_repost_keeps_the_first_deadline(self):
        first = self.pin("Which deck ships?", task="DECK", default=SAFE_DEFAULT, default_minutes=5)
        again = self.pin("Which deck ships?", task="DECK", default=SAFE_DEFAULT, default_minutes=5)
        self.assertEqual(again["id"], first["id"])
        with self.assertRaisesRegex(ValueError, "already pinned with the default"):
            self.pin("Which deck ships?", task="DECK", default="continue with deck B as the working candidate", default_minutes=15)
        self.assertEqual((self.card(first["id"])["default_action"], self.card(first["id"])["default_minutes"]), (SAFE_DEFAULT, 5), "a repost never changes the default or extends the deadline")
        second = self.pin("Should the deck keep the 48 slides?", task="DECK", default="keep the 48 slides")
        self.assertNotEqual(second["id"], first["id"])
        other_task = self.pin("Which deck ships?", task="BRIEF", default=SAFE_DEFAULT)
        self.assertNotEqual(other_task["id"], first["id"], "the same words on another task are another question")
        self.assertEqual(len(self.open_cards()), 3)

    def test_one_question_is_one_card_whatever_its_default_or_surface(self):
        # review 2026-10-14, blocking 1: the default clause is not part of the question's identity
        self.note("Which layout do you prefer? If no answer in 5 minutes I will continue testing layout A.", "waiting")
        with self.assertRaisesRegex(ValueError, "already pinned with the default"):
            self.note("Which layout do you prefer? If no answer in 5 minutes I will continue testing layout B.", "waiting")
        cards = self.open_cards()
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0]["title"], "Which layout do you prefer?", "the card is the question; the default is on the card itself")
        self.assertEqual(cards[0]["default_action"], "continue testing layout A")
        # the same question through the other surface, same default: the same card
        same = self.pin("Which layout do you prefer?", default="continue testing layout A", default_minutes=5)
        self.assertEqual(same["id"], cards[0]["id"])
        with_clause = self.pin("Which layout do you prefer? If no answer in 5 minutes I will continue testing layout A.")
        self.assertEqual(with_clause["id"], cards[0]["id"])
        with self.assertRaisesRegex(ValueError, "already pinned with the default"):
            self.pin("Which layout do you prefer?", default="continue testing layout B", default_minutes=5)
        self.assertEqual(len(self.open_cards()), 1)
        # one deadline, one instruction
        self.overdue(cards[0]["id"])
        with patch("harness.control.enqueue_instruction", return_value={"id": "d-8", "source": "owner-default"}) as enqueue:
            board.mark_stalled(self.root)
        self.assertEqual(enqueue.call_count, 1)
        self.assertIn("Do now: continue testing layout A", enqueue.call_args.args[2])
        self.assertNotIn("layout B", enqueue.call_args.args[2])

    def test_a_question_in_the_card_reason_shares_its_identity_with_the_status_note(self):
        # review 2026-10-14 round 2, blocking 1a: the same question through the reason field is the same card
        self.note("Which layout do you prefer? If no answer in 5 minutes I will continue testing layout A.", "waiting")
        with self.assertRaisesRegex(ValueError, "already pinned with the default"):
            self.pin("Layout decision", why="Which layout do you prefer?", default="continue testing layout B", default_minutes=5)
        same = self.pin("Layout decision", why="Which layout do you prefer?", default="continue testing layout A", default_minutes=5)
        cards = self.open_cards()
        self.assertEqual(len(cards), 1)
        self.assertEqual(same["id"], cards[0]["id"])
        self.assertEqual(cards[0]["question"], "which layout do you prefer?")
        self.overdue(cards[0]["id"])
        with patch("harness.control.enqueue_instruction", return_value={"id": "d-10", "source": "owner-default"}) as enqueue:
            board.mark_stalled(self.root)
        self.assertEqual(enqueue.call_count, 1)
        self.assertIn("Do now: continue testing layout A", enqueue.call_args.args[2])
        self.assertNotIn("layout B", enqueue.call_args.args[2])

    def test_different_questions_in_the_reason_field_are_different_cards(self):
        # review 2026-10-14 round 2, blocking 1b: a second question under the same heading must not disappear
        first = self.pin("Design decision", why="Which layout do you prefer?", default=SAFE_DEFAULT, default_minutes=5)
        second = self.pin("Design decision", why="Which color do you prefer?", default=SAFE_DEFAULT, default_minutes=5)
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual({card["question"] for card in self.open_cards()}, {"which layout do you prefer?", "which color do you prefer?"})

    def test_the_deadline_passes_and_the_default_is_routed_once_then_the_card_closes(self):
        card = self.pin("Which deck ships?", kind="decision", default=SAFE_DEFAULT, default_minutes=5)
        self.overdue(card["id"])
        with patch("harness.control.enqueue_instruction", return_value={"id": "d-1", "source": "owner-default"}) as enqueue:
            board.mark_stalled(self.root)
            board.mark_stalled(self.root)
        self.assertEqual(enqueue.call_count, 1, "the default is routed exactly once")
        self.assertEqual(enqueue.call_args.args[1], self.session["id"])
        routed = enqueue.call_args.args[2]
        self.assertTrue(routed.startswith(f"OWNER DEFAULT APPLIED [default {card['id']}]"), routed)
        self.assertIn(f"Do now: {SAFE_DEFAULT}", routed)
        self.assertEqual(enqueue.call_args.kwargs["source"], "owner-default")
        done = self.card(card["id"])
        self.assertEqual((done["status"], done["outcome"], done["default_instruction_id"]), ("done", f"default applied: {SAFE_DEFAULT}", "d-1"))
        applied = self.events("owner_default_applied")
        self.assertEqual(len(applied), 1)
        self.assertEqual(applied[0]["message"], f"Default applied (no answer in 5 minutes): the CTO was routed to {SAFE_DEFAULT}")
        self.assertIn(applied[0]["message"], self.harness_lines())

    def test_a_refused_queue_keeps_the_card_open_and_the_next_tick_retries(self):
        card = self.pin("Which deck ships?", kind="decision", default=SAFE_DEFAULT)
        self.overdue(card["id"])
        with patch("harness.control.enqueue_instruction", side_effect=ValueError("cannot route to an inactive managed session")) as enqueue:
            board.mark_stalled(self.root)
        self.assertEqual(enqueue.call_count, 1)
        kept = self.card(card["id"])
        self.assertEqual(kept["status"], "open")
        self.assertEqual(kept["default_dispatch"]["status"], "retry")
        self.assertEqual(self.events("owner_default_applied"), [])
        footer = board.owner_action_clears_when(kept)
        self.assertIn("could not be reached (cannot route to an inactive managed session)", footer)
        self.assertIn("keeps trying", footer)
        with patch("harness.control.enqueue_instruction", return_value={"id": "d-2", "source": "owner-default"}) as enqueue:
            board.mark_stalled(self.root)
        self.assertEqual(enqueue.call_count, 1)
        self.assertEqual(self.card(card["id"])["status"], "done")

    def test_a_crash_after_queueing_is_recovered_without_a_second_instruction(self):
        card = self.pin("Which deck ships?", kind="decision", default=SAFE_DEFAULT)
        self.overdue(card["id"])
        # the earlier tick queued the instruction for real and died before closing the card
        queued = control.enqueue_instruction(
            self.root, self.session["id"], f"OWNER DEFAULT APPLIED [default {card['id']}]: earlier tick", source="owner-default",
        )
        with patch("harness.control.enqueue_instruction") as enqueue:
            board.mark_stalled(self.root)
        enqueue.assert_not_called()
        done = self.card(card["id"])
        self.assertEqual((done["status"], done["default_instruction_id"]), ("done", queued["id"]))
        self.assertEqual(len(self.events("owner_default_applied")), 1)
        # a withdrawn receipt does not count: the next tick queues afresh
        other = self.pin("Should the deck keep the 48 slides?", kind="decision", default="keep the 48 slides")
        self.overdue(other["id"])
        stale = control.enqueue_instruction(
            self.root, self.session["id"], f"OWNER DEFAULT APPLIED [default {other['id']}]: withdrawn", source="owner-default",
        )
        with control.locked_state(self.root) as state:
            state["instruction_receipts"][stale["id"]]["status"] = "withdrawn"
        with patch("harness.control.enqueue_instruction", return_value={"id": "d-9", "source": "owner-default"}) as enqueue:
            board.mark_stalled(self.root)
        self.assertEqual(enqueue.call_count, 1)
        self.assertEqual(self.card(other["id"])["default_instruction_id"], "d-9")

    def test_an_overdue_default_at_startup_is_routed_on_the_first_tick_and_a_signed_out_cto_is_not_routed(self):
        card = self.pin("Which deck ships?", kind="decision", default=SAFE_DEFAULT)
        self.overdue(card["id"])
        with patch("harness.attention.needs_owner", return_value=True), \
                patch("harness.control.enqueue_instruction") as enqueue:
            board.mark_stalled(self.root)
        enqueue.assert_not_called()
        kept = self.card(card["id"])
        self.assertEqual(kept["status"], "open")
        self.assertIn("no active, signed-in CTO session", kept["default_dispatch"]["last_error"])
        with patch("harness.control.enqueue_instruction", return_value={"id": "d-3", "source": "owner-default"}) as enqueue:
            board.mark_stalled(self.root)
        self.assertEqual(enqueue.call_count, 1)
        self.assertEqual(self.card(card["id"])["status"], "done")

    def test_an_owner_message_is_noted_on_the_card_and_never_closes_it_by_guess(self):
        first = self.pin("Which deck ships?", task="DECK", default=SAFE_DEFAULT)
        second = self.pin("Should the deck keep the 48 slides?", task="DECK", default="keep the 48 slides")
        with patch("harness.control.enqueue_instruction", return_value={"id": "m-1"}):
            board.record_owner_message(self.root, self.cto["id"], "How is DECK going?", "cto_message")
        for action_id in (first["id"], second["id"]):
            card = self.card(action_id)
            self.assertEqual(card["status"], "open", "a message never closes a question by guess")
            self.assertTrue(card["owner_replied_at"])
            self.assertTrue(board.owner_action_clears_when(card).startswith("Owner message received at "), board.owner_action_clears_when(card))
        # the CTO, which read the message, clears the one it answered
        board.clear_owner_action(self.root, self.cto["id"], first["id"], "you chose deck B")
        # a default that still falls due routes the CTO to the owner's message, not to X
        self.overdue(second["id"])
        with patch("harness.control.enqueue_instruction", return_value={"id": "d-4", "source": "owner-default"}) as enqueue:
            board.mark_stalled(self.root)
        routed = enqueue.call_args.args[2]
        self.assertIn("The owner wrote to you at", routed)
        self.assertIn("if it answers this question, do what the owner said, otherwise do the default: keep the 48 slides", routed)
        self.assertNotIn("Do now:", routed)

    def test_a_requirements_decision_does_not_clear_a_question_card_but_task_acceptance_does(self):
        question = self.pin("Which deck ships?", task="DECK", default=SAFE_DEFAULT)
        plain = self.pin("Requirements decision needed: choose Go ahead or Modify", task="DECK", kind="decision")
        with board.locked_state(self.root) as state:
            board._event(state, "requirements_go_ahead", None, {"task": "DECK", "message": "go ahead"})
        self.assertEqual(self.card(plain["id"])["status"], "done")
        self.assertEqual(self.card(question["id"])["status"], "open", "a requirements decision is not the answer to the CTO's question")
        with board.locked_state(self.root) as state:
            board._event(state, "task_cancelled", None, {"task": "DECK", "message": "cancelled"})
        self.assertEqual(self.card(question["id"])["status"], "done")
        self.assertEqual(self.card(question["id"])["outcome"], "Resolved: task cancelled")

    def test_a_question_card_never_expires_before_its_default_runs(self):
        card = self.pin("Which deck ships?", default=SAFE_DEFAULT)
        self.assertIsNone(board.owner_action_expires_at(self.card(card["id"])))


class SettingsTests(_Fixture):
    def test_defaults_and_validation(self):
        value = global_settings.default_owner_questions()
        self.assertEqual(value["default_minutes"], 15)
        self.assertTrue(any("delivery" in item for item in value["authority_patterns"]))
        self.assertIn("which", value["what_markers"])
        self.assertIn("want me to", value["question_markers"])
        self.assertIn("ship", value["owner_only_markers"])
        self.assertEqual(global_settings._validate_document(global_settings._default())["owner_questions"], value)
        for bad in ({"default_minutes": 0}, {"default_minutes": "soon"}, {"default_minutes": 1441}, {"unknown": 1},
                    {"authority_patterns": "route"}, {"authority_patterns": ["(unclosed"]}, {"what_markers": []}, {"question_markers": ["", "ok"]}):
            with self.assertRaises(ValueError, msg=json.dumps(bad)):
                global_settings._validated_owner_questions(bad)
        # a saved document without the section (written before this change) gets the defaults
        document = {**global_settings._default()}
        document.pop("owner_questions")
        self.assertEqual(global_settings._validate_document(document)["owner_questions"], value)

    def test_the_deadline_comes_from_settings_and_a_save_preserves_the_section(self):
        board.configure_owner_questions({**global_settings.default_owner_questions(), "default_minutes": 30})
        card = self.pin("Which deck ships?", kind="decision", default=SAFE_DEFAULT)
        self.assertEqual(card["default_minutes"], 30)
        with self.assertRaisesRegex(ValueError, "at most 30 minutes"):
            self.note(WHAT_QUESTION, "waiting")
        home = self.root / "home"
        home.mkdir()
        document = {**global_settings._default(), "owner_questions": {**global_settings.default_owner_questions(), "default_minutes": 30}}
        (home / global_settings.SETTINGS_FILENAME).write_text(json.dumps(document), encoding="utf-8")
        self.assertEqual(global_settings.load(home)["owner_questions"]["default_minutes"], 30)
        with patch("harness.cli_capabilities.provider_availability", return_value={"available": True, "reason": ""}):
            saved = global_settings.update_agent_settings(home, control.default_agent_settings())
        self.assertEqual(saved["owner_questions"]["default_minutes"], 30, "a Settings save keeps the owner's section")

    def test_the_worker_hands_the_section_to_the_board(self):
        source = (ROOT / "harness" / "project_worker.py").read_text(encoding="utf-8")
        self.assertIn('board.configure_owner_questions(global_settings.load(settings_home).get("owner_questions"))', source)


class CommandLineTests(_Fixture):
    def run_cli(self, *argv: str) -> tuple[int, str]:
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(out):
            code = board.main(["--root", str(self.root), *argv])
        return code, out.getvalue()

    def test_owner_action_takes_default_and_default_minutes(self):
        code, out = self.run_cli(
            "owner-action", "--agent", self.cto["id"], "--title", "Which deck ships?", "--kind", "decision",
            "--default", SAFE_DEFAULT, "--default-minutes", "7",
        )
        self.assertEqual(code, 0, out)
        card = json.loads(out)
        self.assertEqual((card["default_action"], card["default_minutes"]), (SAFE_DEFAULT, 7))

    def test_a_refused_question_tells_the_cto_what_to_do_instead(self):
        code, out = self.run_cli("status", "--agent", self.cto["id"], "--note", AUTHORITY_QUESTION, "--state", "waiting")
        self.assertNotEqual(code, 0)
        self.assertIn("act, then report", out)
        code, out = self.run_cli("status", "--agent", self.cto["id"], "--note", WHAT_QUESTION, "--state", "waiting")
        self.assertNotEqual(code, 0)
        self.assertIn("state the default", out)


class ViewerTests(unittest.TestCase):
    def test_the_two_events_are_harness_actions_the_owner_sees(self):
        self.assertIn("cto_acted", board_viewer.HARNESS_ACTION_EVENTS)
        self.assertIn("owner_default_applied", board_viewer.HARNESS_ACTION_EVENTS)


if __name__ == "__main__":
    unittest.main()
