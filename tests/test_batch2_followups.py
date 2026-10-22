# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Batch-2 follow-ups: relinquish a review reservation, interruption wording, hard byte limit for the build copy."""
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from datetime import datetime, timedelta, timezone

from harness import board, build_collection, control
from tests import test_board as _tb


class Batch2FollowupTests(unittest.TestCase):
    setUp = _tb.BoardTests.setUp
    tearDown = _tb.BoardTests.tearDown
    ledger = _tb.BoardTests.ledger
    qa_command = _tb.BoardTests.qa_command
    declare_chunks = _tb.BoardTests.declare_chunks
    delivery = _tb.BoardTests.delivery

    def _reserved(self, task):
        dev = self.delivery(task)
        self.declare_chunks(dev["id"], [("core", "relinquish check")])
        first = board.register(self.root, "qa", "REVIEW-QUEUE", vendor="Anthropic")
        second = board.register(self.root, "qa", "REVIEW-QUEUE", vendor="Anthropic")
        request = board.request_review(self.root, dev["id"], self.ledger("relinquish-delivery.md"), "review", chunk="core", test_command=self.qa_command())
        board.reserve_qa(self.root, first["id"], request["id"])
        return first, second, request

    def test_a_reviewer_can_give_a_reservation_back_and_another_reviewer_gets_it(self):
        first, second, request = self._reserved("TASK-RELINQUISH")
        with self.assertRaisesRegex(ValueError, "why"):
            board.relinquish_review_reservation(self.root, first["id"], request["id"], "no")
        with self.assertRaisesRegex(ValueError, "holding this reservation"):
            board.relinquish_review_reservation(self.root, second["id"], request["id"], "I never held this one at all")
        board.relinquish_review_reservation(self.root, first["id"], request["id"], "This machine cannot run the browser the check needs")
        state = board.snapshot(self.root)
        self.assertEqual(state["qa_requests"][request["id"]]["status"], "open")
        self.assertEqual(state.get("delivery_attempt_failures") or [], [])
        self.assertIn("No test failed", [e for e in state["events"] if e["kind"] == "qa_reservation_relinquished"][-1]["message"])
        with self.assertRaisesRegex(ValueError, "gave this review back"):
            board.reserve_qa(self.root, first["id"], request["id"])
        self.assertEqual(board.reserve_qa(self.root, second["id"], request["id"])["reserved_by"], second["id"])

    def test_a_review_given_back_is_rerouted_to_another_reviewer_never_the_same_one(self):
        dev = self.delivery("TASK-RELINQUISH-ROUTE")
        self.declare_chunks(dev["id"], [("core", "relinquish routing")])
        sessions = []
        for _ in range(2):
            session = control.create(self.root, "claude_reviewer")
            agent = board.register(self.root, "qa", "REVIEW_QUEUE", vendor="Anthropic", session_id=session["id"])
            control.take_instructions(self.root, session["id"])
            sessions.append(agent)
        request = board.request_review(self.root, dev["id"], self.ledger("route-delivery.md"), "review", chunk="core", test_command=self.qa_command())
        board.route_open_reviews(self.root, retry_seconds=0)
        holder_id = board.snapshot(self.root)["qa_requests"][request["id"]]["routed_to"]
        other_id = next(a["id"] for a in sessions if a["id"] != holder_id)
        board.reserve_qa(self.root, holder_id, request["id"])
        board.relinquish_review_reservation(self.root, holder_id, request["id"], "This reviewer cannot run the browser the check needs")
        for _ in range(3):
            board.route_open_reviews(self.root, retry_seconds=0)
            self.assertEqual(board.snapshot(self.root)["qa_requests"][request["id"]]["routed_to"], other_id)
        # the second reviewer gives it back too: nobody is left, the request stays open with a plain line
        board.reserve_qa(self.root, other_id, request["id"])
        board.relinquish_review_reservation(self.root, other_id, request["id"], "I cannot do this one either at all")
        board.route_open_reviews(self.root, retry_seconds=0)
        state = board.snapshot(self.root)
        self.assertEqual(state["qa_requests"][request["id"]]["status"], "open")
        self.assertIsNone(state["qa_requests"][request["id"]]["routed_to"])
        self.assertIn("gave this review back", state["reviewer_needed"]["message"])

    def _interrupt_wording(self, patched_run):
        dev = self.delivery("TASK-INTERRUPT-WORDS")
        self.declare_chunks(dev["id"], [("core", "interruption wording")])
        reviewer = board.register(self.root, "qa", "REVIEW-QUEUE", vendor="Anthropic")
        wakes = []
        with patch.object(board.certified_execution, "run", side_effect=lambda *a, **k: patched_run(reviewer)), \
                patch.object(board, "_withdraw_reviewer_wake", side_effect=lambda root, wake: wakes.append(wake) or {}):
            with self.assertRaises(ValueError):
                board.request_review(self.root, dev["id"], self.ledger("interrupt-delivery.md"), "interruption wording", chunk="core", test_command=self.qa_command())
        return reviewer, wakes

    def test_an_interrupted_delivery_run_tells_the_reviewer_the_tests_were_interrupted(self):
        def interrupted(reviewer):
            board.reserve_qa(self.root, reviewer["id"])
            raise board.certified_execution.OwnerMachineActivity("the owner's machine showed a permission prompt")
        reviewer, wakes = self._interrupt_wording(interrupted)
        self.assertEqual(len(wakes), 1)
        self.assertIn("interrupted", wakes[0]["notice"])
        self.assertIn("not yet established", wakes[0]["notice"])
        self.assertNotIn("nothing is wrong", wakes[0]["notice"])
        state = board.snapshot(self.root)
        self.assertEqual(state["agents"][reviewer["id"]]["status_note"], board._INTERRUPTED_REVIEW_NOTE)
        self.assertEqual(state.get("delivery_attempt_failures") or [], [])

    def test_a_genuine_failure_keeps_the_failure_wording_and_is_recorded(self):
        def failed(reviewer):
            board.reserve_qa(self.root, reviewer["id"])
            raise ValueError("tests failed")
        reviewer, wakes = self._interrupt_wording(failed)
        self.assertNotIn("notice", wakes[0])
        state = board.snapshot(self.root)
        self.assertEqual(state["agents"][reviewer["id"]]["status_note"], "Delivery evidence failed before Reviewer execution")
        self.assertEqual(len(state.get("delivery_attempt_failures") or []), 1)

    def test_restart_recovery_of_an_interrupted_delivery_run_is_not_a_failure_and_withdraws_the_wake(self):
        dev = self.delivery("TASK-RECOVERY-WORDS")
        self.declare_chunks(dev["id"], [("core", "recovery wording")])
        reviewer = board.register(self.root, "qa", "REVIEW-QUEUE", vendor="Anthropic")
        request = board.request_review(self.root, dev["id"], self.ledger("recover-delivery.md"), "review", chunk="core", test_command=self.qa_command())
        board.reserve_qa(self.root, reviewer["id"], request["id"])
        old = (datetime.now(timezone.utc) - timedelta(seconds=board.REVIEW_EXECUTION_STALE_SECONDS + 60)).isoformat()
        with board.locked_state(self.root) as state:
            state["qa_requests"][request["id"]].update({"delivery_state": "executing", "requested_at": old})
            state["agents"][dev["id"]].pop("review_execution", None)
        wakes = []
        with patch.object(board, "_withdraw_reviewer_wake", side_effect=lambda root, wake: wakes.append(wake) or {}):
            recovered = board.recover_interrupted_executions(self.root)
        self.assertEqual(len(recovered), 1)
        state = board.snapshot(self.root)
        self.assertEqual(state.get("delivery_attempt_failures") or [], [])
        self.assertNotIn(request["id"], state["qa_requests"])
        self.assertEqual(len(wakes), 1)
        self.assertIn("interrupted", wakes[0]["notice"])
        self.assertIn("rerun the same review scope", state["agents"][dev["id"]]["status_note"])


    def test_restart_before_the_reviewer_reserves_withdraws_the_real_queued_assignment(self):
        dev = self.delivery("TASK-RECOVERY-ROUTED")
        self.declare_chunks(dev["id"], [("core", "routed recovery")])
        session = control.create(self.root, "claude_reviewer")
        reviewer = board.register(self.root, "qa", "REVIEW_QUEUE", vendor="Anthropic", session_id=session["id"])
        control.take_instructions(self.root, session["id"])
        request = board.request_review(self.root, dev["id"], self.ledger("routed-delivery.md"), "review", chunk="core", test_command=self.qa_command())
        board.route_open_reviews(self.root, retry_seconds=0)
        routed = board.snapshot(self.root)["qa_requests"][request["id"]]
        self.assertEqual((routed["routed_to"], routed.get("reserved_by")), (reviewer["id"], None))   # routed, not reserved
        instruction_id = routed["route_instruction_id"]
        self.assertEqual(control.instruction_receipt(self.root, instruction_id)["status"], "queued")
        old = (datetime.now(timezone.utc) - timedelta(seconds=board.REVIEW_EXECUTION_STALE_SECONDS + 60)).isoformat()
        with board.locked_state(self.root) as state:
            state["qa_requests"][request["id"]].update({"delivery_state": "executing", "requested_at": old})
            state["agents"][dev["id"]].pop("review_execution", None)
        self.assertEqual(len(board.recover_interrupted_executions(self.root)), 1)
        self.assertNotEqual(control.instruction_receipt(self.root, instruction_id)["status"], "queued")
        self.assertEqual(control.take_instructions(self.root, session["id"]), [], "the obsolete assignment must not be typed")
        agent = board.snapshot(self.root)["agents"][reviewer["id"]]
        self.assertEqual((agent["status"], agent["task"]), ("waiting", "REVIEW_QUEUE"))
        self.assertNotIn("routed; claim and execute", agent["status_note"])


class BuildCopyByteLimitTests(unittest.TestCase):
    def _collect(self, remote_bytes: bytes, certified: bytes):
        """Run build_collection.collect with a fake scp that delivers `remote_bytes`, certified as `certified`."""
        import hashlib
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        base = Path(folder.name)
        (base / "remote.bin").write_bytes(remote_bytes)
        fake = base / "scp"
        fake.write_text(f"#!/bin/sh\nfor last; do :; done\ncat '{base / 'remote.bin'}' > \"$last\"\n")
        fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
        store = base / "store"
        sha = hashlib.sha256(certified).hexdigest()
        published = []
        usable = {"user": "u", "address": "127.0.0.1", "port": 22}
        paths = {"known_hosts": base / "kh", "key": base / "key"}
        with patch.dict(os.environ, {"HARNESS_TEST_MACHINE_SCP": str(fake)}), patch.object(build_collection, "_usable", return_value=usable), \
                patch.object(build_collection, "_paths", return_value=paths), patch.object(build_collection, "_task_folder", return_value=store), \
                patch.object(build_collection, "remote_facts", return_value=(sha, len(certified))):
            try:
                manifest = build_collection.collect(None, "task", {"out.bin": {"sha256": sha, "size": len(certified)}}, {}, published.append, lambda: set())
                error = None
            except build_collection.BuildError as caught:
                manifest, error = None, caught
        return manifest, error, published, store

    def test_a_file_that_grew_past_the_certified_size_is_refused_with_nothing_kept_or_published(self):
        manifest, error, published, store = self._collect(b"x" * 5_000_000, b"x" * 1000)
        self.assertIsNone(manifest)
        self.assertIn("larger on the machine", str(error))
        self.assertEqual(published, [])
        self.assertEqual([c.name for c in store.iterdir() if c.name != ".lock"], [], "the temporary folder and any published folder are removed")

    def test_a_normal_collection_still_publishes(self):
        manifest, error, published, store = self._collect(b"x" * 1000, b"x" * 1000)
        self.assertIsNone(error)
        self.assertEqual(len(published), 1)
        self.assertTrue((store / manifest["manifest_id"] / "out.bin").is_file())


if __name__ == "__main__":
    unittest.main()
