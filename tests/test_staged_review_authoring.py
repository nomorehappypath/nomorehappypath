# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
from __future__ import annotations

import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from harness import board, contract, control
from tests.requirements_support import agreed_requirements


class StagedReviewAuthoringTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / "test_smoke.py").write_text(
            "import unittest\n\nclass Smoke(unittest.TestCase):\n"
            "    def test_passes(self): self.assertTrue(True)\n",
            encoding="utf-8",
        )
        session = control.create(self.root, "codex_delivery")
        self.delivery = board.register(
            self.root, "development", board.AWAITING_OWNER_DIRECTION,
            vendor="OpenAI", session_id=session["id"],
        )
        board.record_owner_direction(self.root, session["id"], "Build the staged review fixture")
        board.begin_task(self.root, self.delivery["id"], "STAGED-REVIEW")
        agreed_requirements(
            self.root, self.delivery["id"],
            "Review work may overlap Delivery tests, but review execution and verdict remain independent.",
        )
        contract.create_contract(
            self.root, "STAGED-REVIEW", "Build the staged review fixture", ["delivery"],
        )
        board.define_delivery_plan(
            self.root, self.delivery["id"], "chunked", "One bounded test scope",
        )
        board.declare_chunks(self.root, self.delivery["id"], [("core", "staged authoring")])
        self.reviewer = board.register(
            self.root, "qa", "REVIEW_QUEUE", vendor="Anthropic",
        )
        self.delivery_ledger = self._ledger(
            "delivery.md", "python3 -m unittest test_smoke",
            "The normal review path preserves all confirmed requirements.",
        )
        self.challenge_ledger = self._ledger(
            "challenge.md", "python3 -m unittest test_smoke -k passes",
            "The independent review path rejects an unexecuted verdict.",
        )

    def _ledger(self, name: str, command: str, description: str) -> str:
        path = self.root / "docs" / name
        path.parent.mkdir(exist_ok=True)
        path.write_text(
            "| ID | What was tested | Simulation command | Expected system response | Observed system response | QA result |\n"
            "|---|---|---|---|---|---|\n"
            f"| S-001 | {description} | `{command}` | The safe behavior is preserved. | PASS: safe behavior was observed. | PASS |\n",
            encoding="utf-8",
        )
        return str(path.relative_to(self.root))

    def _request_in_thread(self, execute):
        result: dict[str, object] = {}

        def run() -> None:
            try:
                result["request"] = board.request_review(
                    self.root, self.delivery["id"], self.delivery_ledger,
                    "Review the frozen candidate", chunk="core",
                    test_command="python3 -m unittest test_smoke",
                )
            except Exception as error:  # Asserted by the calling test.
                result["error"] = error

        with patch.object(board, "_execute_internal_qa", side_effect=execute):
            thread = threading.Thread(target=run)
            thread.start()
            yield result, thread
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive(), "staged Delivery thread did not finish")

    def test_reviewer_authors_before_delivery_finishes_without_seeing_delivery_evidence(self):
        started = threading.Event()
        release = threading.Event()
        calls = 0

        def execute(*_args, **_kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                started.set()
                self.assertTrue(release.wait(timeout=5))
            return "Ran 1 test in 0.001s\nOK\n"

        for result, thread in self._request_in_thread(execute):
            self.assertTrue(started.wait(timeout=5))
            staged = next(iter(board.snapshot(self.root)["qa_requests"].values()))
            self.assertEqual(staged["status"], "authoring")
            self.assertEqual(staged["delivery_state"], "executing")
            self.assertTrue(staged["review_brief"]["delivery_evidence_withheld"])

            reserved = board.reserve_qa(self.root, self.reviewer["id"], staged["id"])
            self.assertTrue(reserved["reviewer_authoring_overlap_started"])
            recorded = board.record_review_intents(
                self.root, self.reviewer["id"], staged["id"],
                ["Try an independent unexecuted-verdict case and require the board to fail closed."],
            )
            self.assertTrue(recorded["review_brief"]["delivery_evidence_withheld"])
            with self.assertRaisesRegex(ValueError, "blocked until Delivery evidence succeeds"):
                board.attach_challenge_ledger(
                    self.root, self.reviewer["id"], staged["id"], self.challenge_ledger,
                )
            with self.assertRaisesRegex(ValueError, "immutable"):
                board.record_review_intents(
                    self.root, self.reviewer["id"], staged["id"],
                    ["Replace the already recorded independent intention with a different one."],
                )
            release.set()

        self.assertNotIn("error", result)
        completed = result["request"]
        self.assertEqual(completed["status"], "reserved")
        self.assertEqual(completed["delivery_state"], "passed")
        self.assertFalse(completed["review_brief"]["delivery_evidence_withheld"])
        self.assertEqual(completed["reserved_by"], self.reviewer["id"])
        claimed = board.attach_challenge_ledger(
            self.root, self.reviewer["id"], completed["id"], self.challenge_ledger,
        )
        self.assertEqual(claimed["status"], "claimed")

    def test_delivery_failure_cancels_execution_and_archives_staged_review(self):
        started = threading.Event()
        release = threading.Event()

        def execute(*_args, **_kwargs):
            started.set()
            self.assertTrue(release.wait(timeout=5))
            raise ValueError("injected Delivery failure")

        for result, thread in self._request_in_thread(execute):
            self.assertTrue(started.wait(timeout=5))
            staged = next(iter(board.snapshot(self.root)["qa_requests"].values()))
            board.reserve_qa(self.root, self.reviewer["id"], staged["id"])
            board.record_review_intents(
                self.root, self.reviewer["id"], staged["id"],
                ["Exercise the rejected Delivery path and confirm no review command can execute."],
            )
            release.set()

        self.assertRegex(str(result.get("error")), "injected Delivery failure")
        state = board.snapshot(self.root)
        self.assertEqual(state["qa_requests"], {})
        failures = state["delivery_attempt_failures"]
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0]["status"], "cancelled")
        self.assertEqual(failures[0]["delivery_state"], "failed")
        self.assertEqual(state["agents"][self.reviewer["id"]]["status"], "review_cancelled")

    def test_expired_authoring_does_not_transfer_one_reviewers_intents_to_another(self):
        started = threading.Event()
        release = threading.Event()

        def execute(*_args, **_kwargs):
            started.set()
            self.assertTrue(release.wait(timeout=5))
            return "Ran 1 test in 0.001s\nOK\n"

        replacement = board.register(
            self.root, "qa", "REVIEW_QUEUE", vendor="Anthropic",
        )
        for result, thread in self._request_in_thread(execute):
            self.assertTrue(started.wait(timeout=5))
            staged = next(iter(board.snapshot(self.root)["qa_requests"].values()))
            board.reserve_qa(self.root, self.reviewer["id"], staged["id"])
            board.record_review_intents(
                self.root, self.reviewer["id"], staged["id"],
                ["The first Reviewer checks a failure boundary that must not transfer as new authorship."],
            )
            with board.locked_state(self.root) as state:
                old = (
                    datetime.now(timezone.utc)
                    - timedelta(seconds=board.REVIEW_RESERVATION_SECONDS + 1)
                ).isoformat()
                state["qa_requests"][staged["id"]]["reserved_at"] = old
                state["qa_requests"][staged["id"]]["authoring_last_activity_at"] = old
                state["agents"][self.reviewer["id"]]["last_poll_at"] = old
                state["agents"][self.reviewer["id"]]["last_progress_at"] = old
            board.release_expired_review_reservations(self.root)
            reopened = board.snapshot(self.root)["qa_requests"][staged["id"]]
            self.assertEqual(reopened["status"], "authoring")
            self.assertNotIn("reviewer_initial_intents", reopened)
            self.assertEqual(
                reopened["abandoned_reviewer_intents"][0]["reviewer_id"],
                self.reviewer["id"],
            )
            board.reserve_qa(self.root, replacement["id"], staged["id"])
            recorded = board.record_review_intents(
                self.root, replacement["id"], staged["id"],
                ["The replacement Reviewer independently checks a separate recovery boundary."],
            )
            self.assertEqual(
                recorded["reviewer_initial_intents"][0]["reviewer_id"],
                replacement["id"],
            )
            release.set()

        self.assertNotIn("error", result)


if __name__ == "__main__":
    unittest.main()
