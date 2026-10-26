# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""The Reviewer reads the contract and writes the checks before coding (task 2026-10-23_contract-first-checks).

Real board calls: the early look is metadata on the proposal (never a QA request), the owner's Go ahead is never held, answers and
acceptance are one new version, and the board itself runs the stored checks against a committed candidate.

Run:  PYTHONPATH=. python3 -m unittest tests.test_contract_first_checks -v
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from harness import attention, board, contract_check, control
from harness.board_surface import CommandGateway, SessionTokenAuthority
from harness.project_context import ProjectContext
from tests.environment_support import require_loopback
from tests.test_requirements_contract import context, delivery_with_direction

TASK = "TASK-CONTRACT"
TEXT = "Final agreed requirements: a command prints a greeting. Acceptance: running it prints hello."
HEADER = (
    "| ID | What was tested | Simulation command | Expected system response | Observed system response | QA result |\n"
    "|---|---|---|---|---|---|\n"
)


def ledger(directory: str = "c1-1") -> str:
    return HEADER + (
        f"| S-001 | The greeting command prints the word hello when it is run. | "
        f"`python3 -m unittest discover -s .contract-checks/{directory} -p \"test_*.py\"` | Prints hello | PASS: declared | PASS |\n"
    )


TEST_FILE = (
    "import subprocess, sys, unittest\n\n"
    "class Greeting(unittest.TestCase):\n"
    "    def test_prints_hello(self):\n"
    "        out = subprocess.run([sys.executable, 'hello.py'], capture_output=True, text=True).stdout\n"
    "        self.assertEqual(out.strip(), 'hello')\n"
)


def submission(questions=None, checks=True, **extra) -> dict:
    criteria = [
        {"text": "Running the program prints hello", "checks": [{"coverage": "checked", "ledger": ledger(), "files": {"test_hello.py": TEST_FILE}}] if checks else [],
         **({} if checks else {"why_not_checked": "it is a feeling, not a result"}), **extra},
        {"text": "The greeting looks friendly", "why_not_checked": "a matter of taste"},
    ]
    return {"criteria": criteria, "questions": questions or []}


QUESTION = {"text": "Which greeting should it print?", "options": ["hello", "hi there", "good day"], "recommended": 0, "affects": [1]}


PLAIN_LINE = "This requirement protects what you asked for."


def plain_ready(value):
    """The early-look submission needs a plain sentence per requirement; fixtures that do not care about it get a harmless one."""
    if isinstance(value, dict):
        for criterion in value.get("criteria") or []:
            if isinstance(criterion, dict):
                criterion.setdefault("plain", PLAIN_LINE)
    return value


def write_json(directory: Path, name: str, value) -> str:
    path = directory / name
    path.write_text(json.dumps(plain_ready(value)), encoding="utf-8")
    return str(path)


class _Fixture(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)
        self.root = context(self.base)

    def delivery(self):
        return delivery_with_direction(self.root, TASK)

    def reviewer(self, vendor="Anthropic", kind="claude_reviewer"):
        session = control.create(self.root, kind)
        agent = board.register(self.root, "qa", "REVIEW_QUEUE", vendor=vendor, session_id=session["id"])
        control.take_instructions(self.root, session["id"])
        return session, agent

    def proposal(self):
        return board.snapshot(self.root)["requirement_proposals"][TASK]

    def submit(self, reviewer, value, version=None):
        path = write_json(self.base, "submission.json", value)
        return board.record_contract_check_submission(self.root, reviewer["id"], TASK, version or self.proposal()["version"], path)

    def age_deadline(self, seconds=3600):
        with board.locked_state(self.root) as state:
            check = state["requirement_proposals"][TASK]["contract_check"]
            check["deadline_at"] = (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()


class EarlyLookTests(_Fixture):
    def test_reading_state_holds_nothing_and_the_wake_names_the_task(self):
        agent = self.delivery()
        session, reviewer = self.reviewer()
        board.record_requirement_proposal(self.root, agent["id"], TEXT)
        check = self.proposal()["contract_check"]
        self.assertEqual(check["status"], "reading")
        self.assertEqual(check["reviewer_id"], reviewer["id"])
        deadline = datetime.fromisoformat(check["deadline_at"])
        self.assertAlmostEqual((deadline - datetime.fromisoformat(check["started_at"])).total_seconds(), contract_check.wait_seconds(), delta=2)
        wake = [item for item in control.take_instructions(self.root, session["id"]) if item.get("source") == "contract-check"]
        self.assertEqual(len(wake), 1)
        self.assertIn("CONTRACT CHECK ROUTED", wake[0]["text"])
        self.assertIn(TEXT, wake[0]["text"])
        self.assertFalse([r for r in board.snapshot(self.root)["qa_requests"].values()], "the early look is never a QA request")

    def test_no_reviewer_or_no_program_means_go_ahead_at_once_not_checked(self):
        agent = self.delivery()
        board.record_requirement_proposal(self.root, agent["id"], TEXT)
        self.assertEqual(self.proposal()["contract_check"]["status"], "not_checked")
        self.assertIn("no Reviewer", self.proposal()["contract_check"]["reason"])
        board.record_requirements_decision(self.root, TASK, "go_ahead")   # an old-style call with no version still works
        self.assertEqual(self.proposal()["status"], "accepted")

    def test_documents_only_task_skips_the_look(self):
        agent = self.delivery()
        self.reviewer()
        board.record_requirement_proposal(self.root, agent["id"], TEXT, no_program=True)
        self.assertEqual(self.proposal()["contract_check"]["status"], "not_checked")
        self.assertIn("no program", self.proposal()["contract_check"]["reason"])

    def test_reviewer_that_cannot_answer_is_not_asked(self):
        agent = self.delivery()
        session, _reviewer = self.reviewer()
        control.record_attention(self.root, session["id"], attention.detect("Login expired · Please run /login"))
        board.record_requirement_proposal(self.root, agent["id"], TEXT)
        self.assertEqual(self.proposal()["contract_check"]["status"], "not_checked")

    def test_same_vendor_reviewer_is_not_used(self):
        agent = self.delivery()
        self.reviewer(vendor="OpenAI")
        board.record_requirement_proposal(self.root, agent["id"], TEXT)
        self.assertEqual(self.proposal()["contract_check"]["status"], "not_checked")

    def test_a_reading_reviewer_is_not_handed_a_review(self):
        agent = self.delivery()
        session, reviewer = self.reviewer()
        board.record_requirement_proposal(self.root, agent["id"], TEXT)
        with board.locked_state(self.root) as state:
            state["qa_requests"]["review-x"] = {"id": "review-x", "task": "OTHER", "status": "open", "stage": board.DEVELOPMENT_QA,
                                                "developer_id": agent["id"], "requested_at": board.now(), "cycle": 1, "claimed_by": None,
                                                "phase": "chunk", "subtask": "", "chunk": "c", "review_wait_started_at": board.now()}
        board.route_open_reviews(self.root, retry_seconds=0)
        self.assertNotEqual(board.snapshot(self.root)["qa_requests"]["review-x"].get("routed_to"), reviewer["id"])

    def test_deadline_expiry_settles_not_checked_and_a_late_submission_is_refused_without_change(self):
        agent = self.delivery()
        _session, reviewer = self.reviewer()
        board.record_requirement_proposal(self.root, agent["id"], TEXT)
        self.age_deadline()
        before = json.dumps(self.proposal(), sort_keys=True)
        with self.assertRaisesRegex(ValueError, "ran out"):
            self.submit(reviewer, submission())
        self.assertEqual(json.dumps(self.proposal(), sort_keys=True), before, "a refused late submission changes nothing")
        board.mark_stalled(self.root)
        self.assertEqual(self.proposal()["contract_check"]["status"], "not_checked")
        self.assertIn("in time", self.proposal()["contract_check"]["reason"])

    def test_pause_defers_the_settlement_until_resume(self):
        agent = self.delivery()
        self.reviewer()
        board.record_requirement_proposal(self.root, agent["id"], TEXT)
        self.age_deadline()
        board.begin_project_pause(self.root, drain_seconds=0)
        board.finish_project_pause(self.root)
        with self.assertRaises(board.ProjectPausedError):
            board.mark_stalled(self.root)
        transaction = board.begin_project_resume(self.root)
        board.finish_project_resume(self.root, transaction["resume_id"])
        board.mark_stalled(self.root)
        self.assertEqual(self.proposal()["contract_check"]["status"], "not_checked")

    def test_going_ahead_without_waiting_refuses_a_later_submission(self):
        agent = self.delivery()
        _session, reviewer = self.reviewer()
        board.record_requirement_proposal(self.root, agent["id"], TEXT)
        check = self.proposal()["contract_check"]
        board.record_requirements_decision(self.root, TASK, "go_ahead", version=1, token=check["token"])
        self.assertEqual(self.proposal()["contract_check"]["status"], "not_checked")
        with self.assertRaisesRegex(ValueError, "is over"):
            self.submit(reviewer, submission())


class SubmissionAndAnswersTests(_Fixture):
    def start(self):
        agent = self.delivery()
        session, reviewer = self.reviewer()
        board.record_requirement_proposal(self.root, agent["id"], TEXT)
        return agent, session, reviewer

    def test_full_path_questions_answers_new_version_confirm_records_the_digest(self):
        agent, _session, reviewer = self.start()
        result = self.submit(reviewer, submission([QUESTION]))
        self.assertEqual(result, {"status": "questions", "criteria": 2, "questions": 1})
        check = self.proposal()["contract_check"]
        self.assertEqual(check["questions"][0]["recommended"], 0)
        stored = check["criteria"][0]["checks"][0]
        data = Path(stored["file"]).read_bytes()
        self.assertEqual(hashlib.sha256(data).hexdigest(), stored["sha256"])
        board.record_requirements_decision(self.root, TASK, "go_ahead", version=1, token=check["token"],
                                           answers=[{"question": 1, "choice": 0, "other": ""}])
        proposal = self.proposal()
        self.assertEqual((proposal["status"], proposal["version"]), ("accepted", 2))
        self.assertIn("Decided before coding:", proposal["text"])
        self.assertIn("Which greeting should it print? -> hello (recommended)", proposal["text"])
        self.assertTrue(proposal["contract_check"]["used_recommended"])
        confirmation = board.record_requirement_confirmation(self.root, agent["id"], proposal["text"])
        recorded = board.snapshot(self.root)["requirement_confirmations"][TASK]["contract_check"]
        self.assertEqual([c["n"] for c in recorded["checks"]], ["1.1"])
        self.assertEqual(recorded["checks_sha256"], hashlib.sha256(f"1.1:{stored['sha256']}".encode()).hexdigest())
        self.assertEqual(recorded["accepted_version"], 2)
        self.assertEqual({item["n"]: item["label"] for item in recorded["labels"]}, {1: "checked", 2: "not checked in advance"})
        shown = board.show_contract_checks(self.root, agent["id"])
        self.assertEqual(shown["checks"][0]["files"], {"test_hello.py": TEST_FILE})
        self.assertTrue(confirmation)

    def test_answers_and_acceptance_are_atomic_and_recommended_default_is_used_when_none_given(self):
        _agent, _session, reviewer = self.start()
        self.submit(reviewer, submission([QUESTION]))
        token = self.proposal()["contract_check"]["token"]
        board.record_requirements_decision(self.root, TASK, "go_ahead", version=1, token=token)
        self.assertTrue(self.proposal()["contract_check"]["used_recommended"])
        self.assertEqual(self.proposal()["version"], 2)

    def test_other_or_a_different_option_drops_only_the_named_criterion(self):
        agent, _session, reviewer = self.start()
        self.submit(reviewer, submission([QUESTION]))
        token = self.proposal()["contract_check"]["token"]
        board.record_requirements_decision(self.root, TASK, "go_ahead", version=1, token=token,
                                           answers=[{"question": 1, "choice": None, "other": "a loud HELLO"}])
        self.assertIn("-> a loud HELLO", self.proposal()["text"])
        board.record_requirement_confirmation(self.root, agent["id"], self.proposal()["text"])
        recorded = board.snapshot(self.root)["requirement_confirmations"][TASK]["contract_check"]
        self.assertEqual(recorded["checks"], [], "the old check is not attached to changed text")
        self.assertEqual({item["label"] for item in recorded["labels"]}, {"not checked in advance"})

    def test_a_decision_on_an_old_card_is_refused_and_nothing_is_recorded(self):
        _agent, _session, reviewer = self.start()
        stale_token = self.proposal()["contract_check"]["token"]
        self.submit(reviewer, submission([QUESTION]))          # the reviewer finishes while the owner is looking at the old card
        with self.assertRaisesRegex(ValueError, "look at the current card"):
            board.record_requirements_decision(self.root, TASK, "go_ahead", version=1, token=stale_token)
        self.assertEqual(self.proposal()["status"], "awaiting_owner")
        with self.assertRaisesRegex(ValueError, "look at the current card"):
            board.record_requirements_decision(self.root, TASK, "go_ahead", version=1)      # no token while a check is open
        with self.assertRaisesRegex(ValueError, "changed since you looked"):
            board.record_requirements_decision(self.root, TASK, "go_ahead", version=7, token=stale_token)
        self.assertEqual(self.proposal()["status"], "awaiting_owner")

    def test_an_old_go_ahead_click_is_refused_after_a_completed_reading_and_after_a_timeout(self):
        _agent, _session, reviewer = self.start()
        stale = self.proposal()["contract_check"]["token"]
        self.submit(reviewer, submission())                       # reading -> ready, new token, no questions
        self.assertEqual(self.proposal()["contract_check"]["status"], "ready")
        with self.assertRaisesRegex(ValueError, "look at the current card"):
            board.record_requirements_decision(self.root, TASK, "go_ahead", version=1, token=stale)
        with self.assertRaisesRegex(ValueError, "look at the current card"):
            board.record_requirements_decision(self.root, TASK, "go_ahead", version=1)
        self.assertEqual(self.proposal()["status"], "awaiting_owner")
        board.record_requirements_decision(self.root, TASK, "go_ahead", version=1, token=self.proposal()["contract_check"]["token"])
        self.assertEqual(self.proposal()["status"], "accepted")

    def test_a_click_on_a_reading_card_after_the_timeout_is_refused(self):
        _agent, _session, _reviewer = self.start()
        stale = self.proposal()["contract_check"]["token"]
        self.age_deadline()
        board.mark_stalled(self.root)                              # reading -> not_checked, new token
        with self.assertRaisesRegex(ValueError, "look at the current card"):
            board.record_requirements_decision(self.root, TASK, "go_ahead", version=1, token=stale)
        board.record_requirements_decision(self.root, TASK, "go_ahead", version=1, token=self.proposal()["contract_check"]["token"])

    def test_refusals(self):
        _agent, _session, reviewer = self.start()
        other_session, other = self.reviewer()
        too_many = submission([dict(QUESTION, text=f"Question number {n}?") for n in range(5)])
        with self.assertRaisesRegex(ValueError, "at most 4 questions"):
            self.submit(reviewer, too_many)
        with self.assertRaisesRegex(ValueError, "2 to 4 options"):
            self.submit(reviewer, submission([dict(QUESTION, options=["only one"])]))
        with self.assertRaisesRegex(ValueError, "existing criterion"):
            self.submit(reviewer, submission([dict(QUESTION, affects=[9])]))
        with self.assertRaisesRegex(ValueError, "why_not_checked"):
            self.submit(reviewer, {"criteria": [{"text": "Something to check", "checks": []}], "questions": []})
        with self.assertRaisesRegex(ValueError, "test file"):
            self.submit(reviewer, {"criteria": [{"text": "Something to check", "checks": [{"ledger": ledger(), "files": {}}]}]})
        with self.assertRaisesRegex(ValueError, "another Reviewer"):
            self.submit(other, submission())
        with self.assertRaisesRegex(ValueError, "older version"):
            self.submit(reviewer, submission(), version=3)
        with self.assertRaisesRegex(ValueError, "not usable"):
            self.submit(reviewer, {"criteria": [{"text": "Something to check", "checks": [{"ledger": "no table here", "files": {"t.py": "x"}}]}]})
        self.assertEqual(self.proposal()["contract_check"]["status"], "reading", "every refusal left the look open")

    def test_answers_that_could_not_fit_in_the_limit_are_refused_at_submission(self):
        agent = self.delivery()
        _session, reviewer = self.reviewer()
        board.record_requirement_proposal(self.root, agent["id"], "Final agreed requirements: " + "x" * 11900)
        with self.assertRaisesRegex(ValueError, "would not fit"):
            self.submit(reviewer, submission([QUESTION]))
        self.assertEqual(self.submit(reviewer, submission())["status"], "ready")

    def test_modify_keeps_the_owners_one_unanswered_batch_on_the_next_version(self):
        agent, _session, reviewer = self.start()
        self.submit(reviewer, submission([QUESTION]))
        board.record_requirements_decision(self.root, TASK, "modify", "Please change the greeting wording.")
        board.record_requirement_proposal(self.root, agent["id"], TEXT + " Revised.")
        check = self.proposal()["contract_check"]
        self.assertEqual((self.proposal()["version"], check["status"], [q["text"] for q in check["questions"]]),
                         (2, "questions", [QUESTION["text"]]), "the same questions, once, on the new version")
        self.assertEqual({item["label"] for item in contract_check.labels(check["criteria"])}, {"not checked in advance"})
        with self.assertRaisesRegex(ValueError, "look at the current card"):
            board.record_requirements_decision(self.root, TASK, "go_ahead", version=2)
        board.record_requirements_decision(self.root, TASK, "go_ahead", version=2, token=check["token"])
        self.assertIn("Decided before coding:", self.proposal()["text"])
        self.assertIn("-> hello (recommended)", self.proposal()["text"])

    def test_a_second_batch_is_refused_after_the_first_was_answered_and_a_modify_follows_a_ready_look(self):
        agent, _session, reviewer = self.start()
        self.submit(reviewer, submission())                                  # ready, no questions
        board.record_requirements_decision(self.root, TASK, "modify", "Please change the greeting wording.")
        board.record_requirement_proposal(self.root, agent["id"], TEXT + " Revised.")
        self.assertEqual(self.proposal()["contract_check"]["status"], "reading")
        self.assertEqual(self.submit(reviewer, submission([QUESTION]), version=2)["status"], "questions")   # the one batch, first time
        board.record_requirements_decision(self.root, TASK, "modify", "Again.")
        board.record_requirement_proposal(self.root, agent["id"], TEXT + " Revised twice.")
        self.assertEqual(self.proposal()["contract_check"]["status"], "questions")      # carried, not asked again

    def test_sharper_wording_after_questions_keeps_the_batch_and_records_the_answers(self):
        """The live path: version 1 has two open questions and a sharper sentence; Delivery adopts it (version 2)."""
        agent, _session, reviewer = self.start()
        second = {"text": "Should the greeting be loud?", "options": ["no", "yes"], "recommended": 0, "affects": []}
        self.submit(reviewer, submission([QUESTION, second], sharper="Running it prints exactly hello and exits with code 0"))
        old_token = self.proposal()["contract_check"]["token"]
        board.record_requirement_proposal(self.root, agent["id"], TEXT + " Exactly hello, exit code 0.")
        proposal = self.proposal()
        check = proposal["contract_check"]
        self.assertEqual((proposal["version"], check["status"], len(check["questions"])), (2, "questions", 2))
        self.assertNotEqual(check["token"], old_token)
        self.assertEqual(check["version"], 2)
        with self.assertRaisesRegex(ValueError, "look at the current card"):
            board.record_requirements_decision(self.root, TASK, "go_ahead", version=2, token=old_token)
        board.record_requirements_decision(self.root, TASK, "go_ahead", version=2, token=check["token"])
        accepted = self.proposal()
        self.assertEqual((accepted["status"], accepted["version"]), ("accepted", 3))
        self.assertIn("Which greeting should it print? -> hello (recommended)", accepted["text"])
        self.assertIn("Should the greeting be loud? -> no (recommended)", accepted["text"])
        confirmation = board.record_requirement_confirmation(self.root, agent["id"], accepted["text"])
        recorded = board.snapshot(self.root)["requirement_confirmations"][TASK]["contract_check"]
        self.assertEqual(len(recorded["answers"]), 2)
        self.assertEqual(recorded["checks"], [], "the old checks are not attached to the revised wording")
        self.assertTrue(confirmation)

    def test_the_asked_flag_never_outlives_its_questions(self):
        agent, _session, reviewer = self.start()
        self.submit(reviewer, submission([QUESTION], sharper="Exactly hello"))
        state = board.snapshot(self.root)
        self.assertTrue(state["contract_question_batches"][TASK] and state["contract_open_questions"][TASK])
        board.record_requirement_proposal(self.root, agent["id"], TEXT + " Exactly hello.")
        board.record_requirements_decision(self.root, TASK, "go_ahead", version=2, token=self.proposal()["contract_check"]["token"])
        state = board.snapshot(self.root)
        self.assertTrue(state["contract_question_batches"][TASK], "answered: the batch is used up")
        self.assertNotIn(TASK, state.get("contract_open_questions", {}))

    def test_a_sharper_sentence_lets_delivery_replace_the_waiting_proposal_once(self):
        agent, session, reviewer = self.start()
        self.submit(reviewer, submission(sharper="Running it prints exactly hello and exits with code 0"))
        delivery_wake = [i for i in control.take_instructions(self.root, agent["session_id"]) if i.get("source") == "contract-check"]
        self.assertTrue(delivery_wake and "sharper wording" in delivery_wake[0]["text"])
        board.record_requirement_proposal(self.root, agent["id"], TEXT + " Exactly hello, exit code 0.")
        self.assertEqual((self.proposal()["version"], self.proposal()["contract_check"]["status"]), (2, "reading"))
        self.submit(reviewer, submission(sharper="again"), version=2)
        with self.assertRaisesRegex(ValueError, "already awaiting"):
            board.record_requirement_proposal(self.root, agent["id"], TEXT + " A third time.")

    def test_a_second_open_proposal_never_picks_the_busy_reviewer(self):
        _agent, _session, reviewer = self.start()
        other_task = "TASK-OTHER"
        session = control.create(self.root, "codex_delivery")
        second = board.register(self.root, "development", board.AWAITING_OWNER_DIRECTION, vendor="OpenAI", session_id=session["id"])
        board.record_owner_direction(self.root, session["id"], f"OWNER DIRECTION — {other_task}: build another thing.")
        board.begin_task(self.root, second["id"], other_task)
        board.record_requirement_proposal(self.root, second["id"], "Final agreed requirements: another thing.")
        self.assertEqual(board.snapshot(self.root)["requirement_proposals"][other_task]["contract_check"]["status"], "not_checked")
        self.assertEqual(self.proposal()["contract_check"]["reviewer_id"], reviewer["id"])

    def two_checked(self, **extra):
        check = {"coverage": "checked", "ledger": ledger(), "files": {"test_hello.py": TEST_FILE}}
        return {"criteria": [{"text": "Running the program prints the greeting", "checks": [check]},
                             {"text": "The program exits cleanly", "checks": [check]}], "questions": [QUESTION], **extra}

    def answer(self, choice=1, version=1):
        check = self.proposal()["contract_check"]
        return board.record_requirements_decision(self.root, TASK, "answer", version=version, token=check["token"],
                                                  answers=[{"question": 1, "choice": choice, "other": ""}])

    def correction(self, name="fix.json", extra=""):
        return write_json(self.base, name, {"checks": [{"coverage": "checked", "ledger": ledger(), "files": {"test_hello.py": TEST_FILE + extra}}]})

    def test_a_changed_answer_sends_the_reviewer_back_to_update_only_the_affected_tests(self):
        agent, session, reviewer = self.start()
        self.submit(reviewer, self.two_checked())
        control.take_instructions(self.root, session["id"])
        old_token = self.proposal()["contract_check"]["token"]
        self.answer(choice=1)                                          # not the recommended answer: requirement 1 is affected
        proposal, check = self.proposal(), self.proposal()["contract_check"]
        self.assertEqual((proposal["status"], proposal["version"], check["status"]), ("awaiting_owner", 2, "rechecking"))
        self.assertIn("-> hi there", proposal["text"])
        self.assertNotEqual(check["token"], old_token)
        wake = [i for i in control.take_instructions(self.root, session["id"]) if i.get("source") == "contract-check"]
        self.assertTrue(wake and "CONTRACT RECHECK ROUTED" in wake[0]["text"] and "hi there" in wake[0]["text"])
        self.assertEqual({c["n"]: bool(c.get("stale")) for c in check["criteria"]}, {1: True, 2: False})
        # the Reviewer updates requirement 1 through the existing correction operation: the state becomes ready, the other check stays
        result = board.record_contract_check_correction(self.root, reviewer["id"], TASK, 1, "updated for the owner's answers", self.correction(extra="# hi there\n"))
        self.assertEqual(result["status"], "ready")
        check = self.proposal()["contract_check"]
        self.assertEqual(check["status"], "ready")
        self.assertEqual({c["n"]: bool(c.get("stale")) for c in check["criteria"]}, {1: False, 2: False})
        board.record_requirements_decision(self.root, TASK, "go_ahead", version=2, token=check["token"])
        accepted = self.proposal()
        self.assertEqual((accepted["status"], accepted["version"]), ("accepted", 2), "accepting adds nothing more")
        self.assertEqual(accepted["text"].count("Decided before coding:"), 1)
        board.record_requirement_confirmation(self.root, agent["id"], accepted["text"])
        recorded = board.snapshot(self.root)["requirement_confirmations"][TASK]["contract_check"]
        self.assertEqual([c["n"] for c in recorded["checks"]], ["1.1", "2.1"])
        self.assertEqual(len(recorded["answers"]), 1)

    def test_going_ahead_while_the_reviewer_updates_the_tests_never_holds_and_marks_the_unfinished_part(self):
        agent, session, reviewer = self.start()
        self.submit(reviewer, self.two_checked())
        self.answer(choice=1)
        check = self.proposal()["contract_check"]
        with self.assertRaisesRegex(ValueError, "look at the current card"):
            board.record_requirements_decision(self.root, TASK, "go_ahead", version=2)
        board.record_requirements_decision(self.root, TASK, "go_ahead", version=2, token=check["token"])
        self.assertEqual((self.proposal()["status"], self.proposal()["contract_check"]["status"]), ("accepted", "answered"))
        board.record_requirement_confirmation(self.root, agent["id"], self.proposal()["text"])
        recorded = board.snapshot(self.root)["requirement_confirmations"][TASK]["contract_check"]
        self.assertEqual([c["n"] for c in recorded["checks"]], ["2.1"])
        self.assertEqual({i["n"]: i["label"] for i in recorded["labels"]}, {1: "not checked in advance", 2: "checked"})
        with self.assertRaisesRegex(ValueError, "no stored check|not a|another|nothing"):
            board.record_contract_check_correction(self.root, reviewer["id"], TASK, 1, "too late to update this", self.correction())

    def test_the_recheck_has_one_deadline_and_a_late_update_is_refused(self):
        _agent, _session, reviewer = self.start()
        self.submit(reviewer, self.two_checked())
        self.answer(choice=1)
        deadline = self.proposal()["contract_check"]["deadline_at"]
        self.age_deadline()
        before = json.dumps(self.proposal(), sort_keys=True)
        with self.assertRaisesRegex(ValueError, "ran out"):
            board.record_contract_check_correction(self.root, reviewer["id"], TASK, 1, "updated for the owner's answers", self.correction())
        self.assertEqual(json.dumps(self.proposal(), sort_keys=True), before)
        board.mark_stalled(self.root)
        check = self.proposal()["contract_check"]
        self.assertEqual(check["status"], "ready")
        self.assertIn("in time", check["reason"])
        self.assertEqual({c["n"]: bool(c.get("stale")) for c in check["criteria"]}, {1: True, 2: False}, "the changed requirement stays not checked; the other keeps its test")
        self.assertNotEqual(deadline, "")

    def test_recommended_answers_or_no_free_reviewer_need_no_recheck(self):
        agent, session, reviewer = self.start()
        self.submit(reviewer, self.two_checked())
        self.answer(choice=0)                                          # the recommended answer: nothing changed
        self.assertEqual(self.proposal()["contract_check"]["status"], "ready")
        self.assertEqual(self.proposal()["status"], "awaiting_owner")

    def test_without_a_free_reviewer_the_changed_requirement_is_simply_not_checked(self):
        agent, session, reviewer = self.start()
        self.submit(reviewer, self.two_checked())
        control.record_attention(self.root, session["id"], attention.detect("Login expired · Please run /login"))
        self.answer(choice=1)
        check = self.proposal()["contract_check"]
        self.assertEqual(check["status"], "ready")
        self.assertIn("no reviewer", check["reason"])
        self.assertEqual({c["n"]: bool(c.get("stale")) for c in check["criteria"]}, {1: True, 2: False})

    def test_modify_during_the_recheck_takes_the_wake_back_and_keeps_the_batch_used(self):
        agent, session, reviewer = self.start()
        self.submit(reviewer, self.two_checked())
        control.take_instructions(self.root, session["id"])              # the Reviewer has typed the first wake
        self.answer(choice=1)
        board.record_requirements_decision(self.root, TASK, "modify", "Please change the wording again.")
        self.assertEqual(self.proposal()["contract_check"]["status"], "superseded")
        self.assertEqual([i for i in control.take_instructions(self.root, session["id"]) if i.get("source") == "contract-check"], [])
        with self.assertRaisesRegex(ValueError, "no stored check|nothing|not|another"):
            board.record_contract_check_correction(self.root, reviewer["id"], TASK, 1, "updated for the owner's answers", self.correction())
        board.record_requirement_proposal(self.root, agent["id"], TEXT + " Revised.")
        self.assertEqual(self.proposal()["contract_check"]["status"], "reading", "the answered batch is used up; no questions are carried")
        self.assertNotIn(TASK, board.snapshot(self.root).get("contract_open_questions", {}))

    def test_only_the_reviewer_that_was_asked_can_finish_the_recheck(self):
        _agent, _session, reviewer = self.start()
        self.submit(reviewer, self.two_checked())
        self.answer(choice=1)
        _s2, other = self.reviewer()
        with self.assertRaisesRegex(ValueError, "another Reviewer"):
            board.record_contract_check_correction(self.root, other["id"], TASK, 0, "nothing more to change", write_json(self.base, "none.json", {"checks": []}))
        board.record_contract_check_correction(self.root, reviewer["id"], TASK, 0, "nothing more to change", write_json(self.base, "none2.json", {"checks": []}))
        check = self.proposal()["contract_check"]
        self.assertEqual(check["status"], "ready")
        self.assertIn("marked not checked in advance", check["reason"])

    def test_modify_withdraws_the_wake_and_supersedes_the_look(self):
        _agent, session, reviewer = self.start()
        board.record_requirements_decision(self.root, TASK, "modify", "Please change the greeting wording.")
        self.assertEqual(self.proposal()["contract_check"]["status"], "superseded")
        self.assertEqual([i for i in control.take_instructions(self.root, session["id"]) if i.get("source") == "contract-check"], [],
                         "the queued early-look wake was taken back before the Reviewer typed it")
        with self.assertRaisesRegex(ValueError, "is over"):
            self.submit(reviewer, submission(), version=1)

    def test_timeout_also_takes_the_wake_back(self):
        agent, session, _reviewer = self.start()
        self.age_deadline()
        board.mark_stalled(self.root)
        self.assertEqual([i for i in control.take_instructions(self.root, session["id"]) if i.get("source") == "contract-check"], [])


class ResumeTests(_Fixture):
    def test_stop_and_resume_moves_the_proposal_to_the_new_agent(self):
        agent = self.delivery()
        board.record_requirement_proposal(self.root, agent["id"], TEXT)
        with board.locked_state(self.root) as state:
            state["agents"][agent["id"]].update({"active": False, "write_authority": False})
        session = control.create(self.root, "codex_delivery")
        successor = board.register(self.root, "development", board.AWAITING_OWNER_DIRECTION, vendor="OpenAI", session_id=session["id"])
        board.resume_task(self.root, successor["id"], agent["id"], TASK)
        self.assertEqual(self.proposal()["agent_id"], successor["id"])
        board.record_requirements_decision(self.root, TASK, "go_ahead")
        self.assertEqual(board.snapshot(self.root)["agents"][successor["id"]]["status"], "requirements_accepted")


class StoredChecksRunTests(_Fixture):
    """The board runs the stored checks itself, on a committed candidate, and notices an edited, removed or failing one."""

    def committed_repo(self, greeting="hello", extra=None):
        repo = self.base / "candidate"
        repo.mkdir()
        (repo / "hello.py").write_text(f"print({greeting!r})\n", encoding="utf-8")
        for name, body in (extra or {}).items():
            (repo / name).write_text(body, encoding="utf-8") if not isinstance(body, tuple) else os.symlink(body[0], repo / name)
        env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
        for command in (["init", "-q"], ["add", "-A"], ["commit", "-q", "-m", "candidate"]):
            subprocess.run(["git", *command], cwd=repo, env=env, check=True, capture_output=True)
        commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()
        with board.locked_state(self.root) as state:
            state.setdefault("task_workspaces", {})[TASK] = str(repo)
        return repo, commit

    def confirmed(self):
        agent = self.delivery()
        _session, reviewer = self.reviewer()
        board.record_requirement_proposal(self.root, agent["id"], TEXT)
        self.submit(reviewer, submission())
        board.record_requirements_decision(self.root, TASK, "go_ahead", version=1, token=self.proposal()["contract_check"]["token"])
        board.record_requirement_confirmation(self.root, agent["id"], self.proposal()["text"])
        return agent, reviewer

    def run_checks(self, commit, role="delivery"):
        request = {"task": TASK, "subtask": "", "stage": board.INDEPENDENT_REVIEW, "reviewed_commit": commit,
                   "reviewed_tree_hash": "tree", "contract_revision": {"sha256": "rev"}}
        certification = {"board_root": self.root, "candidate": {}, "environment_sha256": "env", "lockfile_digests": {},
                         "role": role, "gate": "final_acceptance::", "retry_reason": "", "task": TASK}
        return board._run_contract_checks(self.root, TASK, request, certification, role)

    def test_real_check_passes_on_good_work_and_fails_on_wrong_work(self):
        self.confirmed()
        _repo, good = self.committed_repo("hello")
        run = self.run_checks(good)
        self.assertEqual(run["run"]["status"], "passed")
        self.assertEqual(run["bundle"]["scenario_ids"], ["S-001"])
        self.assertEqual(board.snapshot(self.root)["requirement_confirmations"][TASK]["contract_check"]["results"], {"1": "passed"})
        request: dict = {}
        board._apply_contract_check_run(request, run)
        self.assertEqual(request["contract_check_sha256"], run["checks_sha256"])

    def test_wrong_work_is_refused_with_the_requirement_named_and_the_way_out(self):
        self.confirmed()
        _repo, bad = self.committed_repo("goodbye")
        with self.assertRaises(ValueError) as raised:
            self.run_checks(bad)
        self.assertIn("Running the program prints hello", str(raised.exception))
        self.assertIn("Never edit the check", str(raised.exception))
        self.assertEqual(board.snapshot(self.root)["requirement_confirmations"][TASK]["contract_check"]["results"], {"1": "failed"})

    def test_an_edited_or_removed_stored_check_is_caught_before_anything_runs(self):
        self.confirmed()
        _repo, good = self.committed_repo("hello")
        stored = Path(board.snapshot(self.root)["requirement_confirmations"][TASK]["contract_check"]["checks"][0]["file"])
        original = stored.read_bytes()
        stored.write_bytes(original.replace(b"hello", b"HELLO"))
        with self.assertRaisesRegex(ValueError, "changed or removed"):
            self.run_checks(good)
        stored.unlink()
        with self.assertRaisesRegex(ValueError, "changed or removed"):
            self.run_checks(good)

    def test_a_candidate_that_already_has_the_checks_folder_is_refused_even_as_a_symlink(self):
        self.confirmed()
        outside = self.base / "outside"
        outside.mkdir()
        _repo, commit = self.committed_repo("hello", {".contract-checks": (str(outside),)})
        run = self.run_checks(commit)       # the checks could not be placed: they did not run (no failure of the work, no block)
        self.assertEqual(run["run"]["status"], "partly")
        self.assertIn(".contract-checks", run["run"]["did_not_run"]["1"])
        self.assertEqual(list(outside.iterdir()), [], "nothing was written through the link")

    def test_no_saved_version_is_recorded_honestly_not_as_a_pass(self):
        self.confirmed()
        run = self.run_checks("")
        self.assertEqual(run["run"]["status"], "skipped")
        self.assertIn("no saved version", run["run"]["reason"])

    def test_correction_keeps_the_original_and_the_reason_and_changes_the_digest(self):
        _agent, reviewer = self.confirmed()
        _repo, good = self.committed_repo("hello")
        before = board.snapshot(self.root)["requirement_confirmations"][TASK]["contract_check"]
        path = write_json(self.base, "fix.json", {"checks": [{"coverage": "checked", "ledger": ledger(), "files": {"test_hello.py": TEST_FILE + "\n# corrected\n"}}]})
        with self.assertRaisesRegex(ValueError, "at least 8"):
            board.record_contract_check_correction(self.root, reviewer["id"], TASK, 1, "wrong", path)
        board.record_contract_check_correction(self.root, reviewer["id"], TASK, 1, "the check expected the wrong word", path)
        after = board.snapshot(self.root)["requirement_confirmations"][TASK]["contract_check"]
        self.assertNotEqual(before["checks_sha256"], after["checks_sha256"])
        self.assertEqual(after["corrections"][0]["replaced"][0]["sha256"], before["checks"][0]["sha256"])
        self.assertEqual(after["corrections"][0]["reason"], "the check expected the wrong word")
        self.assertEqual(self.run_checks(good)["run"]["status"], "passed")
        stale = board._contract_check_stale_reason(board.snapshot(self.root), {"task": TASK, "contract_check_sha256": before["checks_sha256"]})
        self.assertIn("changed after they were run", stale, "a certification or PASS made with the old checks no longer counts")
        delivery = self.delivery_agent()
        with self.assertRaises(ValueError):
            board.record_contract_check_correction(self.root, delivery["id"], TASK, 1, "I want it my way", path)

    def delivery_agent(self):
        return next(a for a in board.snapshot(self.root)["agents"].values() if a["role"] == "development")

    def test_hooks_run_the_checks_only_for_the_final_review(self):
        source = (Path(board.__file__)).read_text(encoding="utf-8")
        self.assertIn('if phase == "final_acceptance":\n                # The board runs the Reviewer\'s stored early checks itself', source)
        self.assertIn('None if review_request.get("phase") != "final_acceptance" else _run_contract_checks', source)


class FinalReviewFlowTests(unittest.TestCase):
    """The real final-review request and the Reviewer's challenge on a committed task workspace."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name)
        self.root = base / "project"
        self.root.mkdir()
        self.base = base
        root = self.root
        (root / "test_smoke.py").write_text("import unittest\n\nclass Smoke(unittest.TestCase):\n    def test_passes(self): self.assertTrue(True)\n")
        (root / "hello.py").write_text("print('hello')\n")
        for extra in ("test_views.py", "test_models.py"):    # other modules at the repository root, as real projects have: an unquoted pattern would expand to them
            (root / extra).write_text("import unittest\n\nclass Extra(unittest.TestCase):\n    def test_it(self):\n        self.assertTrue(True)\n")
        (root / ".gitignore").write_text(".harness/\n")
        (root / "ledger.md").write_text(HEADER + "| S-001 | Delivery behavior remains correct under the focused smoke check. | `python3 -m unittest test_smoke` | Holds | PASS: executed | PASS |\n")
        (root / "challenge.md").write_text(HEADER + "| S-101 | The independent check confirms the behavior through a distinct test scope. | `python3 -m unittest test_smoke -k passes` | Holds | PASS: executed | PASS |\n")
        (root / "proof.txt").write_text("delivery proven\n")
        for command in (["init", "-q", "-b", "main"], ["config", "user.email", "h@e.invalid"], ["config", "user.name", "H"], ["add", "-A"], ["commit", "-qm", "candidate"]):
            subprocess.run(["git", *command], cwd=root, check=True, capture_output=True)
        from harness import contract
        contract.create_contract(root, TASK, "Ship task", ["delivery"])
        contract.add_evidence(root, TASK, "delivery", [root / "proof.txt"])
        session = control.create(root, "codex_delivery")
        self.dev = board.register(root, "development", board.AWAITING_OWNER_DIRECTION, vendor="OpenAI", session_id=session["id"])
        board.record_owner_direction(root, session["id"], "Ship task")
        board.begin_task(root, self.dev["id"], TASK)
        rsession = control.create(root, "claude_reviewer")
        self.qa = board.register(root, "qa", "REVIEW_QUEUE", vendor="Anthropic", session_id=rsession["id"])
        control.take_instructions(root, rsession["id"])

    def confirm_with_checks(self, expected="hello"):
        board.record_requirement_proposal(self.root, self.dev["id"], TEXT)
        files = {"test_hello.py": TEST_FILE.replace("'hello'", repr(expected))}
        value = {"criteria": [{"text": "Running the program prints the greeting", "checks": [{"coverage": "checked", "ledger": ledger(), "files": files}]}]}
        path = write_json(self.base, "sub.json", value)
        board.record_contract_check_submission(self.root, self.qa["id"], TASK, 1, path)
        token = board.snapshot(self.root)["requirement_proposals"][TASK]["contract_check"]["token"]
        board.record_requirements_decision(self.root, TASK, "go_ahead", version=1, token=token)
        board.record_requirement_confirmation(self.root, self.dev["id"], TEXT)
        board.define_delivery_plan(self.root, self.dev["id"], "atomic", "One cohesive task")

    def request(self):
        return board.request_review(self.root, self.dev["id"], str(self.root / "ledger.md"), "review independently",
                                    phase="final_acceptance", test_command="python3 -m unittest test_smoke")

    def test_the_board_runs_the_checks_for_delivery_and_again_for_the_reviewer_and_a_correction_voids_old_results(self):
        self.confirm_with_checks()
        request = self.request()
        run = request["contract_check_runs"][0]
        self.assertEqual((run["role"], run["status"], run["scenario_ids"]), ("delivery", "passed", ["S-001"]))
        digest = request["contract_check_sha256"]
        board.claim_qa(self.root, self.qa["id"], request["id"], str(self.root / "challenge.md"))
        board.execute_challenge(self.root, self.qa["id"], request["id"])
        after = board.snapshot(self.root)["qa_requests"][request["id"]]
        self.assertEqual([item["role"] for item in after["contract_check_runs"]], ["delivery", "reviewer"])
        self.assertEqual(after["challenge_execution"]["contract_check_sha256"], digest)
        # a wrong check is corrected: the certification no longer counts, a verdict is refused until the checks run again
        fix = write_json(self.base, "fix.json", {"checks": [{"coverage": "checked", "ledger": ledger(), "files": {"test_hello.py": TEST_FILE + "# fixed\n"}}]})
        board.record_contract_check_correction(self.root, self.qa["id"], TASK, 1, "the check was wrong in one detail", fix)
        evidence = self.base / "evidence.txt"
        evidence.write_text("command: python3 -m unittest\nresult: PASS\n")
        with self.assertRaisesRegex(ValueError, "stored checks changed after they were run"):
            board.qa_result(self.root, self.qa["id"], request["id"], "passed", "TASK DONE: YES - all good", str(evidence))
        board.execute_challenge(self.root, self.qa["id"], request["id"])
        self.assertNotEqual(board.snapshot(self.root)["qa_requests"][request["id"]]["contract_check_sha256"], digest)
        board.qa_result(self.root, self.qa["id"], request["id"], "passed", "TASK DONE: YES - all good: the checks and my own scenario hold", str(evidence))
        passed = board.snapshot(self.root)["qa_requests"][request["id"]]
        self.assertEqual(passed["status"], "passed")
        self.assertTrue(passed["contract_check_simulations"]["scenario_ids"])
        # a later correction voids the saved PASS (the integrity sweep runs once the board is migrated, as on a real project)
        with board.locked_state(self.root) as state:
            state["integrity_migration_complete"] = True
        again = write_json(self.base, "fix2.json", {"checks": [{"coverage": "checked", "ledger": ledger(), "files": {"test_hello.py": TEST_FILE + "# fixed again\n"}}]})
        board.record_contract_check_correction(self.root, self.qa["id"], TASK, 1, "another correction after the pass", again)
        voided = board.snapshot(self.root)["qa_requests"][request["id"]]
        self.assertEqual(voided["status"], "failed")
        self.assertIn("stored checks changed", voided["result_summary"])

    def test_delivery_cannot_ask_for_review_while_a_stored_check_fails(self):
        self.confirm_with_checks(expected="goodbye")        # the work prints hello; the check wants goodbye
        with self.assertRaises(ValueError) as raised:
            self.request()
        self.assertIn("Running the program prints the greeting", str(raised.exception))
        self.assertIn("post a blocked status", str(raised.exception))
        self.assertEqual(board.snapshot(self.root)["qa_requests"], {}, "no review was left open")


class SurfaceTests(unittest.TestCase):
    """The Reviewer's submission travels the real authenticated surface (bytes uploaded, roles enforced)."""

    def setUp(self):
        require_loopback()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name)
        code = base / "code"; code.mkdir()
        self.context = ProjectContext(code, base / "private-data", base / "workspaces")
        control.initialize(self.context)

    def session(self, kind, role, task):
        session = control.create(self.context, kind)
        authority = SessionTokenAuthority(self.context)
        authority.prepare(session["id"])
        control.attach(self.context, session["id"], os.getpid())
        agent = board.register(self.context, role, task, vendor=control.KINDS[kind]["vendor"], session_id=session["id"])
        return session, agent, authority.claim(session["id"], os.getpid()), CommandGateway(self.context, authority)

    @staticmethod
    def request(operation, arguments, nonce, artifact=None):
        body = {"protocol": "1", "nonce": nonce, "operation": operation, "arguments": [operation, *arguments]}
        if artifact is not None:
            payload = json.dumps(plain_ready(artifact)).encode("utf-8")
            body["artifacts"] = {"submission": {"content": base64.b64encode(payload).decode("ascii"), "length": len(payload),
                                                "sha256": hashlib.sha256(payload).hexdigest(), "media_type": "text/plain"}}
        return body

    def test_submit_through_the_surface_and_only_the_reviewer_may(self):
        from harness.board_surface import PROTOCOL_VERSION, SurfaceAuthorizationError
        dsession, delivery, dtoken, dgateway = self.session("codex_delivery", "engineering", board.AWAITING_OWNER_DIRECTION)
        rsession, reviewer, rtoken, rgateway = self.session("claude_reviewer", "qa", "REVIEW_QUEUE")
        board.record_owner_direction(self.context, dsession["id"], f"OWNER DIRECTION — {TASK}: build the thing.")
        board.begin_task(self.context, delivery["id"], TASK)
        board.record_requirement_proposal(self.context, delivery["id"], TEXT)
        self.assertEqual(board.snapshot(self.context)["requirement_proposals"][TASK]["contract_check"]["status"], "reading",
                         board.snapshot(self.context)["requirement_proposals"][TASK]["contract_check"])
        arguments = ["--agent", reviewer["id"], "--task", TASK, "--version", "1", "--submission", "upload:submission"]
        request = self.request("contract-check-submit", arguments, 10, submission([QUESTION]))
        request["protocol"] = PROTOCOL_VERSION
        with self.assertRaises(SurfaceAuthorizationError):
            wrong = self.request("contract-check-submit", ["--agent", delivery["id"], *arguments[2:]], 11, submission())
            wrong["protocol"] = PROTOCOL_VERSION
            dgateway.execute(dtoken, wrong)
        result = rgateway.execute(rtoken, request)
        self.assertIn('"questions"', json.dumps(result))
        self.assertEqual(board.snapshot(self.context)["requirement_proposals"][TASK]["contract_check"]["status"], "questions")


if __name__ == "__main__":
    unittest.main()
