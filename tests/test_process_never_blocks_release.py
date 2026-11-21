# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""A process issue never stops a release; only product problems do (task 2026-11-20_process-never-blocks-release).

Owner 2026-11-20: "a process issue does not stop releasing the app, only bugs do" — "this needs to be fixed for any project".
Lease and contract analysis, 2026-11-21 00:20 UTC: the owner closed the last finding of a failed final review; the release was
held on completion_contract_complete and development_agents_complete (paperwork), the review's builder was offline, and its
active replacement never got the finish nudge, so the task waited for ever. Each rule is checked at all three gates: the
readiness answer, the coordinator's preparation, and the CTO's record-release.

Run:  PYTHONPATH=. python3 -m unittest tests.test_process_never_blocks_release -v
"""
from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from harness import board, contract, control_plane, cto, release_coordinator
from tests.requirements_support import setup_checked

TASK = "lease-contract-analyzer"
PRODUCT_SETS = {
    "legacy": set(board.RELEASE_REQUIRED_CHECKS) - {"ready_for_owner_test"},
    "broker": set(board.BROKER_RELEASE_REQUIRED_CHECKS) - {"ready_for_owner_test"},
}


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True)
    if result.returncode != 0:
        raise AssertionError(f"git {args} failed: {result.stderr}")
    return result.stdout.strip()


def _checks(required: set[str], failing: str = "") -> dict:
    """Release checks as cto.release_check reports them: every required check true except `failing`, readiness computed."""
    checks = {key: key != failing for key in required | board.PROCESS_RELEASE_CHECKS}
    checks["ready_for_owner_test"], checks["advisory_failures"] = cto.readiness(checks, required | board.PROCESS_RELEASE_CHECKS)
    return checks


class Readiness(unittest.TestCase):
    def test_each_process_check_failing_alone_is_ready_and_listed(self):
        for name, required in PRODUCT_SETS.items():
            for key in sorted(board.PROCESS_RELEASE_CHECKS):
                with self.subTest(set=name, check=key):
                    checks = _checks(required, key)
                    self.assertTrue(checks["ready_for_owner_test"])
                    self.assertEqual(checks["advisory_failures"], [key])

    def test_each_product_check_failing_alone_is_not_ready(self):
        for name, required in PRODUCT_SETS.items():
            product = required - board.PROCESS_RELEASE_CHECKS
            self.assertIn("contract_scope_intact", product)
            self.assertIn("completion_product_ready", product)
            for key in sorted(product):
                with self.subTest(set=name, check=key):
                    checks = _checks(required, key)
                    self.assertFalse(checks["ready_for_owner_test"])
                    self.assertEqual(checks["advisory_failures"], [])


class BoardCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)
        self.root = self.base / "board-root"
        self.root.mkdir()
        self.repo = self.base / "product"
        self.repo.mkdir()
        _git(self.repo, "init", "-q", "-b", "main")
        _git(self.repo, "config", "user.email", "h@example.invalid")
        _git(self.repo, "config", "user.name", "H")
        (self.repo / "delivered.txt").write_text("reviewed work\n")
        _git(self.repo, "add", "delivered.txt")
        _git(self.repo, "commit", "-qm", "reviewed work")
        self.reviewed = _git(self.repo, "rev-parse", "HEAD")
        enqueue = mock.patch("harness.control.enqueue_instruction", return_value={"id": "route"})
        self.enqueue = enqueue.start()
        self.addCleanup(enqueue.stop)
        setup_checked(self.root, TASK)        # NMHP's setup check passed (owner 2026-11-21); a list with no AI tool

    def final_review(self, *, status="passed", developer="engineering-old", broker=False, completed_at=None, waived=False):
        at = completed_at or board.now()
        with board.locked_state(self.root) as state:
            request = {
                "id": f"review-{TASK}-final-04", "task": TASK, "status": status, "phase": "final_acceptance",
                "stage": board.INDEPENDENT_REVIEW, "cycle": 4, "structure_revision": 0, "subtask": "", "chunk": "final",
                "developer_id": developer, "claimed_by": "qa", "review_wait_started_at": "", "requested_at": at,
                "completed_at": at, "reviewed_commit": self.reviewed,
            }
            if broker:
                request["mirror_ref"] = "refs/harness/mirror/x"
                state.setdefault("task_repositories", {})[TASK] = str(self.repo)
            state["qa_requests"][request["id"]] = request
            state["task_workspaces"][TASK] = str(self.repo)
            if waived:
                state.setdefault("owner_review_waivers", {})[TASK] = {
                    "task": TASK, "request_id": request["id"], "head_commit": self.reviewed, "kind": "closed_findings",
                    "certified": True, "closed_findings": ["B12"], "left_open": [], "words": "", "at": board.now()}
        return request

    def builder(self, agent_id: str, **fields) -> None:
        """A builder record shaped exactly like a registered one (register, then rename and set the fields under test)."""
        made = board.register(self.root, "engineering", board.AWAITING_OWNER_DIRECTION, session_id=f"tmp-{agent_id}")
        with board.locked_state(self.root) as state:
            record = state["agents"].pop(made["id"])
            record.update({"id": agent_id, "task": TASK, **fields})
            state["agents"][agent_id] = record

    def builders(self, *, old_active=False, new_active=True, new_session="codex_delivery-new"):
        self.builder("engineering-old", active=old_active, status="superseded", session_id="s-old",
                     spawned_at="2026-11-20T23:00:00+00:00")
        self.builder("engineering-new", active=new_active, status="waiting", session_id=new_session,
                     spawned_at="2026-11-21T00:27:00+00:00")
        with board.locked_state(self.root) as state:
            state.setdefault("task_lineage", {})[TASK] = [{"source_agent_id": "engineering-old", "agent_id": "engineering-new"}]

    def coordinate(self, checks: dict) -> list:
        with mock.patch.object(release_coordinator.cto, "release_check", return_value=checks):
            return release_coordinator.coordinate(self.root)

    def state(self):
        return board.snapshot(self.root)


class CoordinatorPreparation(BoardCase):
    def run_one(self, name: str, failing: str) -> dict:
        self.final_review(broker=name == "broker", waived=True, status="failed")
        self.builders()
        outcomes = self.coordinate(_checks(PRODUCT_SETS[name], failing))
        return next(outcome for outcome in outcomes if outcome["task"] == TASK)

    def test_each_process_check_failing_alone_is_prepared_without_a_hold(self):
        for name in PRODUCT_SETS:
            for key in sorted(board.PROCESS_RELEASE_CHECKS):
                with self.subTest(set=name, check=key):
                    self.setUp()
                    outcome = self.run_one(name, key)
                    self.assertEqual(outcome["status"], "prepared", outcome)
                    st = self.state()
                    self.assertNotEqual((st.get("control_plane_holds") or {}).get(TASK, {}).get("status"), "open")
                    self.assertEqual([c for c in (st.get("owner_actions") or {}).values() if c.get("status") == "open"], [])

    def test_each_product_check_failing_alone_holds_as_before(self):
        for name in PRODUCT_SETS:
            for key in sorted((PRODUCT_SETS[name] - board.PROCESS_RELEASE_CHECKS) - {"claim_scope_audit_passed"}):
                with self.subTest(set=name, check=key):
                    self.setUp()
                    outcome = self.run_one(name, key)
                    self.assertIn(outcome["status"], {"checks_failed", "reintegration_required"}, outcome)
                    if outcome["status"] == "checks_failed":
                        self.assertIn(key, outcome["failed"])
                        self.assertEqual(self.state()["control_plane_holds"][TASK]["status"], "open")


class RecordRelease(BoardCase):
    def setUp(self):
        super().setUp()
        self.cto_agent = board.register(self.root, "cto", "GLOBAL_MONITOR", session_id="cto-session")

    def record(self, name: str, failing: str):
        self.final_review(broker=name == "broker", waived=True, status="failed")
        checks = _checks(PRODUCT_SETS[name], failing)
        checks.update({"head_commit": self.reviewed, "git_broker_governed": name == "broker"})
        return board.record_release_ready(self.root, self.cto_agent["id"], TASK, checks)

    def test_each_process_check_failing_alone_is_recorded(self):
        for name in PRODUCT_SETS:
            for key in sorted(board.PROCESS_RELEASE_CHECKS):
                with self.subTest(set=name, check=key):
                    self.setUp()
                    release = self.record(name, key)
                    self.assertEqual(release["status"], "VISUAL_TEST_REQUIRED")

    def test_each_product_check_failing_alone_is_refused(self):
        for name in PRODUCT_SETS:
            for key in sorted(PRODUCT_SETS[name] - board.PROCESS_RELEASE_CHECKS):
                with self.subTest(set=name, check=key):
                    self.setUp()
                    with self.assertRaisesRegex(ValueError, "release checks failed: .*" + key):
                        self.record(name, key)


class LeaseReplay(BoardCase):
    """The exact Lease board: failed final review + the owner's decision, review's builder offline and replaced by an active
    builder that never runs complete, no contract evidence, a pre-upgrade hold, card and checks_failed journal."""

    def test_the_next_tick_prepares_the_release_and_clears_the_old_hold_and_card(self):
        self.final_review(status="failed", waived=True)
        self.builders()
        release_coordinator._incident(self.root, TASK, "checks", "mechanical checks failed: completion_contract_complete, "
                                      "development_agents_complete, ready_for_owner_test")
        release_coordinator._journal(self.root, {"task": TASK, "commit": self.reviewed, "step": "checks_failed",
                                                 "coordination_key": "pre-upgrade-key", "failed": ["completion_contract_complete"]})
        with board.locked_state(self.root) as state:
            board._harness_card(state, f"waiver_blocked:{TASK}", f"Blocked, needs you: {TASK} cannot be released yet",
                                "Your decision is recorded, but ...", TASK, incident="Completion Contract is incomplete")
        checks = _checks(PRODUCT_SETS["legacy"], "completion_contract_complete")
        checks["development_agents_complete"] = False
        checks["ready_for_owner_test"], checks["advisory_failures"] = cto.readiness(checks, PRODUCT_SETS["legacy"] | board.PROCESS_RELEASE_CHECKS)
        with mock.patch.object(release_coordinator.cto, "release_check", return_value=checks):
            report = control_plane.tick(self.root)
            outcome = next(o for o in report["release_outcomes"] if o["task"] == TASK)
            self.assertEqual(outcome["status"], "prepared", report)
            st = self.state()
            self.assertEqual(st["control_plane_holds"][TASK]["status"], "resolved")
            self.assertEqual([c for c in (st.get("owner_actions") or {}).values() if c.get("status") == "open"], [])
            notes = [e for e in st["events"] if e["kind"] == "release_process_notes"]
            self.assertEqual(len(notes), 1)
            self.assertIn("without waiting for paperwork", notes[0]["message"])
            self.assertIn("proof file for each deliverable", notes[0]["message"])
            from harness import story
            self.assertEqual(story.paperwork_note(st, TASK), "Paperwork still open, not holding the release: the builder's own "
                             "\"finished\" sign-off; the builder's proof file for each deliverable.")
            self.assertEqual(report["final_pass_routes"], [], "an owner-decided pass needs no builder nudge")
            again = control_plane.tick(self.root)
        self.assertEqual(next(o for o in again["release_outcomes"] if o["task"] == TASK)["status"], "already_prepared")
        self.assertEqual(len([e for e in self.state()["events"] if e["kind"] == "release_process_notes"]), 1, "once per candidate")


class SignoffGrace(BoardCase):
    def test_a_reviewer_pass_waits_for_the_builder_only_for_the_grace(self):
        self.builders()
        self.final_review(developer="engineering-new")
        self.assertEqual(release_coordinator._coordinatable_tasks(self.state()), [], "a builder still finishing is not raced")
        old = (datetime.now(timezone.utc) - timedelta(seconds=board.RELEASE_SIGNOFF_GRACE_SECONDS + 5)).isoformat()
        self.final_review(developer="engineering-new", completed_at=old)
        self.assertEqual([task for task, _ in release_coordinator._coordinatable_tasks(self.state())], [TASK])

    def test_an_owner_decided_pass_does_not_wait(self):
        self.builders()
        self.final_review(status="failed", waived=True)
        self.assertEqual([task for task, _ in release_coordinator._coordinatable_tasks(self.state())], [TASK])


class CurrentBuilderAndNudge(BoardCase):
    def test_the_replacement_is_the_current_builder_even_while_the_superseded_one_is_alive(self):
        self.builders(old_active=True)
        request = self.final_review(developer="engineering-old")
        self.assertEqual(board.current_builder(self.state(), TASK, request)["id"], "engineering-new")

    def test_a_chain_of_replacements_ends_at_the_last_one(self):
        self.builders(new_active=False)
        self.builder("engineering-third", active=False, status="waiting", session_id="", spawned_at="2026-11-21T01:00:00+00:00")
        with board.locked_state(self.root) as state:
            state["task_lineage"][TASK].append({"source_agent_id": "engineering-new", "agent_id": "engineering-third"})
        request = self.final_review(developer="engineering-old")
        self.assertEqual(board.current_builder(self.state(), TASK, request)["id"], "engineering-third")

    def test_no_builder_is_none(self):
        self.assertIsNone(board.current_builder(self.state(), TASK, self.final_review()))

    def test_the_finish_nudge_goes_to_the_active_replacement(self):
        self.builders()
        self.final_review(developer="engineering-old")
        routed = control_plane._route_final_pass_completion(self.root)
        self.assertEqual(len(routed), 1)
        self.assertEqual(self.enqueue.call_args.args[1], "codex_delivery-new")

    def test_a_replacement_without_a_session_is_not_nudged(self):
        self.builders(new_session="")
        self.final_review(developer="engineering-old")
        self.assertEqual(control_plane._route_final_pass_completion(self.root), [])


class ProductGatesStayWithoutABuilder(BoardCase):
    def test_a_waived_no_code_task_without_its_proof_file_is_not_ready(self):
        self.final_review(status="failed", waived=True)
        with board.locked_state(self.root) as state:
            request = state["qa_requests"][f"review-{TASK}-final-04"]
            request["no_product_code_task"] = True
            request["no_product_code_evidence_pins"] = [{"deliverable": "Report", "path": str(self.base / "gone.md"), "sha256": "0" * 64}]
        problems = board._completion_gate_problems(self.root, self.state(), TASK, "")
        self.assertTrue(problems, "the proof of a no-code task is the deliverable: still a product problem, builder or not")

    def test_an_unanswered_app_spec_card_with_no_direction_record_still_refuses(self):
        self.final_review()
        with board.locked_state(self.root) as state:
            state.setdefault("task_kinds", {})[TASK] = "spec"
        problems = board._completion_gate_problems(self.root, self.state(), TASK, "")
        self.assertTrue(any("question card" in p for p in problems), problems)

    def test_an_altered_agreed_scope_is_a_product_problem_and_missing_evidence_is_not(self):
        path = contract._task_path(self.root, TASK)
        path.parent.mkdir(parents=True, exist_ok=True)
        scope = {"objective": "Analyze leases", "exclusions": [], "deliverables": [{"name": "Report", "acceptance_proof": "p"}]}
        value = {"task": TASK, "objective": "Analyze leases", "deliverables": [{"name": "Report", "acceptance_proof": "p"}],
                 "exclusions": [], "remaining_work": ["Report"], "immutable_scope": scope}
        path.write_text(json.dumps(value))
        self.assertEqual(contract.scope_problems(self.root, TASK), [], "missing evidence rows are paperwork")
        value["deliverables"][0]["name"] = "Something else"
        path.write_text(json.dumps(value))
        self.assertTrue(contract.scope_problems(self.root, TASK))
        path.write_text("[1, 2]")
        self.assertTrue(contract.scope_problems(self.root, TASK), "an unreadable contract cannot prove its scope")



class OwnerFacingWords(BoardCase):
    """Codex task review 2026-11-20 notes (1) and (2): what the owner reads must match what happens."""

    def test_a_released_task_without_a_requirements_record_reads_as_ready_and_shows_the_paperwork_line(self):
        from harness import story
        self.builders()
        self.final_review(status="failed", waived=True)
        with board.locked_state(self.root) as state:
            state.setdefault("releases", {})[TASK] = {"task": TASK, "status": "VISUAL_TEST_REQUIRED", "head_commit": self.reviewed, "cto_id": "cto",
                                                     "recorded_at": board.now()}
        board.release_prepared_with_notes(self.root, TASK, self.reviewed, ["requirements_confirmation_recorded"])
        gate = story.gate(self.state(), TASK, story.task_facts(self.state(), TASK))
        self.assertNotEqual(gate["status"], "AWAITING FINAL REQUIREMENTS")
        self.assertIn("Paperwork still open, not holding the release: the requirements confirmation record.",
                      story.paperwork_note(self.state(), TASK))

    def test_no_builder_on_the_task_reads_as_going_to_release_checks(self):
        with mock.patch("harness.release_coordinator.coordinate", return_value=[]):
            request = self.final_review(status="failed")
            with board.locked_state(self.root) as state:
                finish = board._write_owner_review_waiver(self.root, state, TASK, state["qa_requests"][request["id"]],
                                                          "take_what_you_have", [], [], "Take what you have", None)
        self.assertTrue(finish["completed"])
        self.assertEqual(finish["problems"], [])
        self.assertEqual([c for c in (self.state().get("owner_actions") or {}).values() if c.get("status") == "open"], [])

if __name__ == "__main__":
    unittest.main()
