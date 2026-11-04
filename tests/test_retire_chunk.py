# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""2026-11-02 owner plan change: Delivery retires (removes or merges) a planned root chunk that was never reviewed.

The owner told Delivery to merge the remaining chunks and leave one out; the board offered no way to do either, so the
dropped chunk stayed open and blocked the release. retire-chunk removes such a chunk from the work that is required, in the
board, the final-review admission, the release gates, Mission Control, the story and recovery, without waiving a deliverable.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from harness import board, board_mcp, board_surface, board_viewer, contract, control, cto, review_rounds, story
from harness.board_surface import CommandGateway, SessionTokenAuthority, SurfaceAuthorizationError
from tests import test_adaptive_planning
from tests.environment_support import require_loopback
from tests import test_board_viewer

_planning = vars(test_adaptive_planning.AdaptivePlanningSimulationTests)  # helpers only; its tests run in their own module
OWNER_WORDS = "The owner dropped Stage 2 and asked for the remaining stages to be merged."
TASK = "OWNER-PLAN"


class RetireChunkBase(unittest.TestCase):
    setUp = _planning["setUp"]
    tearDown = _planning["tearDown"]
    delivery = _planning["delivery"]
    ledger = _planning["ledger"]
    evidence = _planning["evidence"]
    request = _planning["request"]
    pass_request = _planning["pass_request"]

    def chunked(self, names=("s1", "s2", "s3", "s4", "s5"), task=TASK):
        agent = self.delivery(task)
        board.define_delivery_plan(self.root, agent["id"], "chunked", "Several stages with separate risk")
        board.declare_chunks(self.root, agent["id"], [(name, f"Stage {name}") for name in names])
        return agent

    def state(self):
        return board.snapshot(self.root)

    def chunks(self, task=TASK):
        return self.state()["task_chunks"][task]

    def plan(self, task=TASK):
        return self.state()["delivery_plans"][task]

    def cto_agent(self):
        return board.register(self.root, "cto", "GLOBAL_MONITOR", vendor="Anthropic")

    def structure(self, task=TASK):
        state = self.state()
        return json.dumps([state["task_chunks"].get(task), state["delivery_plans"].get(task)], sort_keys=True)

    def release_check_ready(self, task=TASK):
        artifact = {
            "branch": "main", "task_artifact_clean": True,
            "artifact_commit_pushed": True, "artifact_commit_exact": True,
            "artifact_health_verified": True, "task_artifact_release_verified": True,
            "artifact_health_output": "PASS", "head_commit": "abc",
        }
        with patch.object(cto, "_task_artifact_gate", return_value=artifact), \
                patch.object(cto, "ledger_complete", return_value=(True, [])), \
                patch.object(board, "simulation_evidence_complete", return_value=(True, [])), \
                patch.object(contract, "contract_complete", return_value=(True, [], {"objective": f"OWNER DIRECTION — {task}"})):
            return cto.release_check(self.root, task, self.root / self.ledger("release-final"), self.root)


class RetireTests(RetireChunkBase):
    def test_retiring_records_the_chunk_the_change_and_the_event_without_a_new_revision(self):
        agent = self.chunked()
        revision = self.plan()["structure_revision"]
        result = board.retire_chunk(self.root, agent["id"], "s2", OWNER_WORDS)
        self.assertEqual(result["status"], "retired")
        saved = self.chunks()["s2"]
        self.assertEqual(saved["status"], "retired")
        self.assertEqual(saved["retired"]["reason"], OWNER_WORDS)
        self.assertEqual(saved["retired"]["by_agent"], agent["id"])
        self.assertEqual(saved["retired"]["merged_into"], "")
        self.assertTrue(saved["retired"]["at"])
        self.assertEqual(self.plan()["structure_revision"], revision)
        change = self.plan()["structure_changes"][-1]
        self.assertEqual((change["kind"], change["removed"], change["reason"], change["revision"]),
                         ("delivery work removed", ["s2"], OWNER_WORDS, revision))
        events = [e for e in self.state()["events"] if e["kind"] == "chunk_retired"]
        self.assertEqual(len(events), 1)
        self.assertEqual((events[0]["chunk"], events[0]["task"]), ("s2", TASK))
        self.assertTrue(all(self.chunks()[name]["status"] == "open" for name in ("s1", "s3", "s4", "s5")))

    def test_a_merge_records_its_target_and_reports_the_unfinished_step(self):
        agent = self.chunked()
        result = board.retire_chunk(self.root, agent["id"], "s3", "Merged by the owner's direction.", into="s34")
        self.assertEqual(self.chunks()["s3"]["retired"]["merged_into"], "s34")
        self.assertEqual(self.plan()["structure_changes"][-1]["merged_into"], "s34")
        self.assertIn("declare-chunks", result["next_action"])
        self.assertIn("--reason", result["next_action"])
        self.assertIn("s34", result["next_action"])
        event = [e for e in self.state()["events"] if e["kind"] == "chunk_retired"][-1]
        self.assertIn("declare the combined chunk", event["message"])
        self.assertEqual(board.unfinished_merge_targets(self.chunks()), ["s34"])
        board.declare_chunks(self.root, agent["id"], [("s34", "Stages 3 and 4 together")], "Owner merged stages 3 and 4.")
        self.assertEqual(board.unfinished_merge_targets(self.chunks()), [])
        self.assertEqual(board.retired_chunk_text(self.chunks()["s3"]), "Merged into s34")
        self.assertEqual(board.retired_chunk_text(self.chunks()["s3"] | {"retired": {"reason": "no longer wanted"}}),
                         "Removed by Delivery: no longer wanted")

    def test_a_retry_with_the_same_reason_changes_nothing_and_another_reason_is_refused(self):
        agent = self.chunked()
        first = board.retire_chunk(self.root, agent["id"], "s2", OWNER_WORDS)
        before = self.structure()
        events = len(self.state()["events"])
        again = board.retire_chunk(self.root, agent["id"], "s2", OWNER_WORDS)
        self.assertEqual(again, first)
        self.assertEqual(self.structure(), before)
        self.assertEqual(len(self.state()["events"]), events)
        with self.assertRaisesRegex(ValueError, "already retired"):
            board.retire_chunk(self.root, agent["id"], "s2", "A different explanation.")
        with self.assertRaisesRegex(ValueError, "already retired"):
            board.retire_chunk(self.root, agent["id"], "s2", OWNER_WORDS, into="s3")
        self.assertEqual(self.structure(), before)

    def test_a_chunk_sent_to_review_in_any_state_or_revision_is_never_retired(self):
        agent = self.chunked()
        live = self.request(agent, "s1", "chunk", chunk="s1")
        with self.assertRaisesRegex(ValueError, live["id"]):
            board.retire_chunk(self.root, agent["id"], "s1", OWNER_WORDS)
        before = self.structure()
        for name, status, revision in (("s2", "claimed", 1), ("s3", "failed", 1), ("s4", "passed", 1), ("s5", "open", 0)):
            with board.locked_state(self.root) as state:
                state["qa_requests"][f"old-{name}"] = {
                    "id": f"old-{name}", "task": TASK, "phase": "chunk", "subtask": "", "chunk": name, "status": status,
                    "cycle": 1, "stage": board.INDEPENDENT_REVIEW, "structure_revision": revision, "requested_at": board.now(),
                    "developer_id": agent["id"], "claimed_by": None, "review_wait_started_at": board.now(),
                }
            with self.subTest(status=status, revision=revision), self.assertRaisesRegex(ValueError, f"old-{name}.*{status}"):
                board.retire_chunk(self.root, agent["id"], name, OWNER_WORDS)
        self.assertEqual(self.structure(), before)

    def test_a_chunk_whose_request_was_archived_is_still_a_reviewed_chunk(self):
        agent = self.chunked()
        with board.locked_state(self.root) as state:
            state["archive"].append({"kind": "qa_request", "value": {
                "id": "archived-s2", "task": TASK, "phase": "chunk", "subtask": "", "chunk": "s2", "status": "failed", "cycle": 1}})
        with self.assertRaisesRegex(ValueError, "archived-s2"):
            board.retire_chunk(self.root, agent["id"], "s2", OWNER_WORDS)

    def test_passed_unknown_and_blank_refusals_change_nothing(self):
        agent = self.chunked()
        self.pass_request(self.request(agent, "s1", "chunk", chunk="s1"), "s1")
        before = self.structure()
        with self.assertRaisesRegex(ValueError, "already passed review|sent to review"):
            board.retire_chunk(self.root, agent["id"], "s1", OWNER_WORDS)
        with self.assertRaisesRegex(ValueError, "no declared chunk nope; declared chunks: s1, s2"):
            board.retire_chunk(self.root, agent["id"], "nope", OWNER_WORDS)
        for blank in ("", "   "):
            with self.assertRaisesRegex(ValueError, "plain-language --reason"):
                board.retire_chunk(self.root, agent["id"], "s2", blank)
        with self.assertRaisesRegex(ValueError, "requires --chunk"):
            board.retire_chunk(self.root, agent["id"], " ", OWNER_WORDS)
        with self.assertRaisesRegex(ValueError, "into itself"):
            board.retire_chunk(self.root, agent["id"], "s2", OWNER_WORDS, into="s2")
        board.retire_chunk(self.root, agent["id"], "s3", OWNER_WORDS)
        before = self.structure()
        with self.assertRaisesRegex(ValueError, "itself retired"):
            board.retire_chunk(self.root, agent["id"], "s2", OWNER_WORDS, into="s3")
        self.assertEqual(self.structure(), before)

    def test_only_the_active_current_delivery_agent_may_retire(self):
        agent = self.chunked()
        before = self.structure()
        with self.assertRaisesRegex(ValueError, "only the task's Delivery Agent"):
            board.retire_chunk(self.root, self.reviewer["id"], "s2", OWNER_WORDS)
        with self.assertRaisesRegex(ValueError, "only the task's Delivery Agent"):
            board.retire_chunk(self.root, self.cto_agent()["id"], "s2", OWNER_WORDS)
        with board.locked_state(self.root) as state:
            state["agents"][agent["id"]]["active"] = False
        with self.assertRaisesRegex(ValueError, "inactive Delivery Agent"):
            board.retire_chunk(self.root, agent["id"], "s2", OWNER_WORDS)
        with board.locked_state(self.root) as state:
            state["agents"][agent["id"]].update({"active": True, "superseded_by_agent_id": "engineering-new"})
        with self.assertRaisesRegex(ValueError, "superseded Delivery Agent is read-only"):
            board.retire_chunk(self.root, agent["id"], "s2", OWNER_WORDS)
        self.assertEqual(self.structure(), before)

    def test_only_a_chunked_plan_has_root_chunks_to_retire(self):
        agent = self.delivery("ATOMIC-ONE")
        board.define_delivery_plan(self.root, agent["id"], "atomic", "One piece")
        with self.assertRaisesRegex(ValueError, "chunked delivery plan"):
            board.retire_chunk(self.root, agent["id"], "s2", OWNER_WORDS)

    def test_the_real_board_cli_retires_and_merges(self):
        agent = self.chunked()
        base = [sys.executable, str(Path(board.__file__)), "--root", str(self.root)]
        done = subprocess.run(base + ["retire-chunk", "--agent", agent["id"], "--chunk", "s2", "--reason", OWNER_WORDS],
                              capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(self.chunks()["s2"]["status"], "retired")
        merged = subprocess.run(base + ["retire-chunk", "--agent", agent["id"], "--chunk", "s3", "--reason", "merge", "--into", "s34"],
                                capture_output=True, text=True)
        self.assertEqual(merged.returncode, 0, merged.stderr)
        self.assertEqual(json.loads(merged.stdout)["next_action"].count("declare-chunks"), 1)
        refused = subprocess.run(base + ["retire-chunk", "--agent", agent["id"], "--chunk", "s3", "--reason", "other"],
                                 capture_output=True, text=True)
        self.assertNotEqual(refused.returncode, 0)


class SurfaceTests(RetireChunkBase):
    def gateway(self, agent, delivery=False):
        if delivery:
            with board.locked_state(self.root) as state:
                state["agents"][agent["id"]]["role"] = "engineering"   # the planning fixture registers the other Delivery role name
        authority = SessionTokenAuthority(self.root)
        authority.prepare(agent["session_id"])
        control.attach(self.root, agent["session_id"], os.getpid())
        token = authority.claim(agent["session_id"], os.getpid())
        return token, CommandGateway(self.root, authority)

    @staticmethod
    def message(arguments, nonce):
        return {"protocol": board_surface.PROTOCOL_VERSION, "nonce": nonce, "operation": "retire-chunk",
                "arguments": ["retire-chunk", *arguments]}

    def test_only_delivery_may_use_the_operation_and_the_caller_is_bound_to_the_session(self):
        require_loopback()
        self.assertIn("retire-chunk", board_surface.ALL_BOARD_OPERATIONS)
        self.assertEqual(board_surface.AUTHORIZATION_MATRIX["retire-chunk"], frozenset({"engineering"}))
        self.assertIn("retire-chunk", board_surface.AGENT_ARGUMENT_OPERATIONS)
        agent = self.chunked()
        token, gateway = self.gateway(agent, delivery=True)
        with self.assertRaises(SurfaceAuthorizationError):
            gateway.execute(token, self.message(["--agent", "engineering-forged", "--chunk", "s2", "--reason", OWNER_WORDS], 11))
        self.assertEqual(self.chunks()["s2"]["status"], "open")
        result = gateway.execute(token, self.message(["--agent", agent["id"], "--chunk", "s2", "--reason", OWNER_WORDS], 12))["result"]
        self.assertEqual(result["status"], "retired")
        self.assertEqual(self.chunks()["s2"]["retired"]["by_agent"], agent["id"])
        merged = gateway.execute(token, self.message(["--agent", agent["id"], "--chunk", "s3", "--reason", "merge", "--into", "s34"], 13))["result"]
        self.assertIn("declare-chunks", merged["next_action"])

    def test_the_reviewer_and_the_cto_are_refused_by_the_surface(self):
        require_loopback()
        self.chunked()
        for role, task in (("qa", board.AWAITING_OWNER_DIRECTION), ("cto", "GLOBAL_MONITOR")):
            session = control.create(self.root, "claude_reviewer" if role == "qa" else "claude_cto")
            agent = board.register(self.root, role, task, session_id=session["id"])
            token, gateway = self.gateway(agent)
            with self.subTest(role=role), self.assertRaises(SurfaceAuthorizationError):
                gateway.execute(token, self.message(["--chunk", "s2", "--reason", OWNER_WORDS], 21))
        self.assertEqual(self.chunks()["s2"]["status"], "open")

    def test_the_mcp_tool_exists_for_delivery_only_and_builds_the_cli_argv(self):
        names = {role: {tool["name"] for tool in board_mcp.tool_definitions(role)} for role in ("engineering", "qa", "cto")}
        self.assertIn("retire-chunk", names["engineering"])
        self.assertNotIn("retire-chunk", names["qa"])
        self.assertNotIn("retire-chunk", names["cto"])
        tool = next(tool for tool in board_mcp.tool_definitions("engineering") if tool["name"] == "retire-chunk")
        self.assertEqual(sorted(tool["inputSchema"]["required"]), ["chunk", "reason"])
        self.assertIn("into", tool["inputSchema"]["properties"])
        self.assertEqual(board_mcp.argv_for("retire-chunk", {"chunk": "s3", "reason": "merge", "into": "s34"}),
                         ["retire-chunk", "--chunk", "s3", "--reason", "merge", "--into", "s34"])


class AdmissionAndReleaseTests(RetireChunkBase):
    def test_a_retired_chunk_cannot_be_sent_to_review(self):
        agent = self.chunked()
        board.retire_chunk(self.root, agent["id"], "s2", OWNER_WORDS)
        with self.assertRaisesRegex(ValueError, "chunk s2 was retired .*Removed by Delivery.*not reviewed"):
            self.request(agent, "s2", "chunk", chunk="s2")
        self.assertFalse(self.state()["qa_requests"])
        self.assertEqual(self.chunks()["s2"]["status"], "retired")

    def test_final_acceptance_needs_every_live_chunk_passed_and_one_passed_chunk(self):
        agent = self.chunked(("a", "b", "c"))
        board.retire_chunk(self.root, agent["id"], "c", OWNER_WORDS)
        with self.assertRaisesRegex(ValueError, "every chunk to pass first: a, b"):
            self.request(agent, "early", "final_acceptance")
        self.pass_request(self.request(agent, "a", "chunk", chunk="a"), "a")
        with self.assertRaisesRegex(ValueError, "every chunk to pass first: b"):
            self.request(agent, "early", "final_acceptance")
        self.pass_request(self.request(agent, "b", "chunk", chunk="b"), "b")
        self.pass_request(self.request(agent, "final", "final_acceptance"), "final")

    def test_all_chunks_retired_is_never_admitted_or_complete(self):
        agent = self.chunked(("a", "b"))
        board.retire_chunk(self.root, agent["id"], "a", OWNER_WORDS)
        board.retire_chunk(self.root, agent["id"], "b", OWNER_WORDS, into="ab")
        with self.assertRaisesRegex(ValueError, "at least one live chunk that passed.*declare the combined chunk ab with declare-chunks"):
            self.request(agent, "none", "final_acceptance")
        checked = self.release_check_ready()
        self.assertFalse(checked["delivery_chunks_complete"])
        self.assertFalse(checked["product_structure_complete"])
        self.assertFalse(checked["internal_chunk_qa_evidence_complete"])

    def test_an_interrupted_merge_into_an_undeclared_chunk_blocks_final_acceptance_and_the_release(self):
        agent = self.chunked(("done", "remaining1", "remaining2"))
        self.pass_request(self.request(agent, "done", "chunk", chunk="done"), "done")
        board.retire_chunk(self.root, agent["id"], "remaining1", OWNER_WORDS, into="combined")
        board.retire_chunk(self.root, agent["id"], "remaining2", OWNER_WORDS, into="combined")
        with self.assertRaisesRegex(ValueError, "merged work to be declared and passed first.*declare the combined chunk combined with declare-chunks"):
            self.request(agent, "final", "final_acceptance")
        self.assertFalse(self.release_check_ready()["delivery_chunks_complete"], "work merged into a chunk that was never declared is not done")
        board.declare_chunks(self.root, agent["id"], [("combined", "Stages 1 and 2 together")], reason=OWNER_WORDS)
        with self.assertRaisesRegex(ValueError, "every chunk to pass first: combined"):
            self.request(agent, "final", "final_acceptance")
        self.pass_request(self.request(agent, "combined", "chunk", chunk="combined"), "combined")
        self.pass_request(self.request(agent, "final", "final_acceptance"), "final")

    def test_release_gates_ignore_retired_chunks_and_keep_every_live_requirement(self):
        agent = self.chunked(("a", "b", "c"))
        evidence = self.evidence("gates")
        board.retire_chunk(self.root, agent["id"], "c", OWNER_WORDS)
        with board.locked_state(self.root) as state:
            state["agents"][agent["id"]].update({"status": "done", "active": False})
            state["task_chunks"][TASK]["a"]["status"] = "passed"
            common = {"task": TASK, "stage": board.INDEPENDENT_REVIEW, "status": "passed", "cycle": 1, "evidence": evidence,
                      "delivery_evidence": evidence, "unit_test_command": "python3 -m unittest test_smoke",
                      "unit_test_evidence": evidence, "ledger": self.ledger("g-final"), "challenge_ledger": self.ledger("g-ch", reviewer=True),
                      "developer_id": agent["id"], "claimed_by": self.reviewer["id"], "requested_at": board.now(),
                      "review_wait_started_at": board.now(), "structure_revision": state["delivery_plans"][TASK]["structure_revision"]}
            state["qa_requests"]["a-chunk"] = {"id": "a-chunk", "phase": "chunk", "subtask": "", "chunk": "a", **common}
            state["qa_requests"]["final"] = {"id": "final", "phase": "final_acceptance", "subtask": "", "chunk": "final", **common}
        blocked = self.release_check_ready()
        self.assertFalse(blocked["delivery_chunks_complete"], "live chunk b is still open")
        with board.locked_state(self.root) as state:
            state["task_chunks"][TASK]["b"]["status"] = "passed"
        no_proof = self.release_check_ready()
        self.assertTrue(no_proof["delivery_chunks_complete"])
        self.assertFalse(no_proof["internal_chunk_qa_evidence_complete"], "live chunk b has no review proof")
        self.assertFalse(no_proof["unit_tests_passed"], "live chunk b has no unit-test proof")
        with board.locked_state(self.root) as state:
            state["qa_requests"]["b-chunk"] = {"id": "b-chunk", "phase": "chunk", "subtask": "", "chunk": "b", **common}
        ready = self.release_check_ready()
        for key in ("delivery_chunks_complete", "product_structure_complete", "internal_chunk_qa_evidence_complete",
                    "unit_tests_passed", "development_qa_passed", "independent_review_passed"):
            self.assertTrue(ready[key], key)
        with board.locked_state(self.root) as state:
            state["qa_requests"]["b-chunk"]["unit_test_evidence"] = ""
        self.assertFalse(self.release_check_ready()["unit_tests_passed"])
        with board.locked_state(self.root) as state:
            state["qa_requests"]["b-chunk"]["unit_test_evidence"] = evidence
            state["qa_requests"]["b-chunk"]["delivery_evidence"] = ""
        self.assertFalse(self.release_check_ready()["internal_chunk_qa_evidence_complete"])

    def test_retirement_never_waives_a_completion_contract_deliverable(self):
        agent = self.chunked()
        before = json.dumps(contract._load(contract._task_path(self.root, TASK)), sort_keys=True)
        board.retire_chunk(self.root, agent["id"], "s2", OWNER_WORDS)
        after = contract._load(contract._task_path(self.root, TASK))
        self.assertEqual(json.dumps(after, sort_keys=True), before)
        self.assertTrue(all(not item.get("owner_waiver") for item in after.get("deliverables") or []))
        self.assertFalse(self.state().get("deliverable_waivers"))
        text = (Path(board.__file__).resolve().parents[1] / "directives" / "AGENT.md").read_text(encoding="utf-8")
        sentence = next(line for line in text.splitlines() if "retire-chunk" in line)
        self.assertIn("declare-chunks", sentence)
        self.assertIn("--into", sentence)
        self.assertIn("waiver", sentence)
        self.assertIn("Completion Contract deliverable", sentence)


class PreservationTests(RetireChunkBase):
    def stop_scope(self, chunk):
        entry = {"task": TASK, "phase": "chunk", "subtask": "", "chunk": chunk,
                 "structure_revision": self.plan()["structure_revision"]}
        with board.locked_state(self.root) as state:
            state.setdefault("review_escalations", {})[review_rounds.lineage_key(entry)] = {
                "task": TASK, "status": "could_not_finish", "outcome": "three failed reviews", "scope": entry}

    def refused_with_stop(self, chunk):
        with self.assertRaisesRegex(ValueError, "could not be finished automatically"):
            board._escalation_refusal(self.state(), TASK, "chunk", "", chunk)

    def test_an_unrelated_review_stop_and_its_review_history_survive_the_retirement(self):
        agent = self.chunked(("stuck", "kept", "dropped"))
        failed = self.request(agent, "stuck", "chunk", chunk="stuck")
        with board.locked_state(self.root) as state:
            state["qa_requests"][failed["id"]].update({"status": "failed", "result": "failed", "completed_at": board.now()})
        self.stop_scope("stuck")
        self.assertEqual(len(board.stopped_scopes(self.state(), TASK)), 1)
        revision = self.plan()["structure_revision"]
        stored = json.dumps(self.state()["qa_requests"][failed["id"]], sort_keys=True)
        board.retire_chunk(self.root, agent["id"], "dropped", OWNER_WORDS)
        self.assertEqual(self.plan()["structure_revision"], revision)
        self.assertEqual(len(board.stopped_scopes(self.state(), TASK)), 1)
        self.refused_with_stop("stuck")
        self.assertEqual(json.dumps(self.state()["qa_requests"][failed["id"]], sort_keys=True), stored)
        self.assertEqual(self.state()["qa_requests"][failed["id"]]["structure_revision"], revision)
        # recovery: the kept chunk is still work left outside the stopped scope; the retired one is not
        self.assertTrue(board.work_left_outside_stopped_scopes(self.state(), TASK))
        board.retire_chunk(self.root, agent["id"], "kept", OWNER_WORDS)
        self.assertFalse(board.work_left_outside_stopped_scopes(self.state(), TASK))

    def test_passed_evidence_is_still_reconciled_after_retirement_and_after_the_merge_declaration(self):
        agent = self.chunked(("done", "m1", "m2"))
        passed = self.pass_request(self.request(agent, "done", "chunk", chunk="done"), "done")
        passed_id = passed["id"]
        board.retire_chunk(self.root, agent["id"], "m1", "merge", into="m12")
        board.retire_chunk(self.root, agent["id"], "m2", "merge", into="m12")
        # the retirement leaves the revision alone, so the passed review is still in the reconciled (current) revision
        self.assertEqual(self.state()["qa_requests"][passed_id]["structure_revision"], self.plan()["structure_revision"])
        # saved-PASS reconciliation still examines the unrelated passed review after the retirement (it is not skipped as an older revision)
        reconciled = board.reconcile_evidence_reuse(self.root)
        self.assertEqual([row["request_id"] for row in reconciled["reused"] + reconciled["invalidated"]], [passed_id])
        board.reconcile_integrity(self.root)
        with board.locked_state(self.root) as state:
            state["qa_requests"][passed_id].update({"status": "passed", "result": "passed", "integrity_invalidated": False})
            state["task_chunks"][TASK]["done"]["status"] = "passed"
        board.declare_chunks(self.root, agent["id"], [("m12", "Both together")], "Owner merged them.")
        # the declaration is the existing structure-revision step; the passed chunk and its review stay in force
        self.assertEqual(self.chunks()["done"]["status"], "passed")
        self.assertEqual(self.state()["qa_requests"][passed_id]["status"], "passed")
        self.assertEqual(self.chunks()["m1"]["status"], "retired")
        released = self.release_check_ready()
        self.assertFalse(released["delivery_chunks_complete"], "the combined chunk m12 is live and not passed")

    def test_the_merge_declaration_does_not_delete_the_stop_record_of_an_unrelated_scope(self):
        agent = self.chunked(("stuck", "m1", "m2"))
        self.stop_scope("stuck")
        board.retire_chunk(self.root, agent["id"], "m1", "merge", into="m12")
        board.retire_chunk(self.root, agent["id"], "m2", "merge", into="m12")
        self.assertEqual(len(board.stopped_scopes(self.state(), TASK)), 1)
        self.refused_with_stop("stuck")
        before_revision = self.plan()["structure_revision"]
        board.declare_chunks(self.root, agent["id"], [("m12", "Both together")], "Owner merged them.")
        self.assertEqual(self.plan()["structure_revision"], before_revision + 1)
        # The stop is stored with the scope's own lineage and is not deleted by the declaration.
        stops = [e for e in self.state()["review_escalations"].values() if e["scope"]["chunk"] == "stuck"]
        self.assertEqual(len(stops), 1)
        self.assertEqual(stops[0]["status"], "could_not_finish")
        self.assertEqual(stops[0]["scope"]["structure_revision"], before_revision)


class DisplayTests(RetireChunkBase):
    def hand_state(self):
        chunks = {
            "a": {"description": "A", "status": "passed"},
            "b": {"description": "B", "status": "retired", "retired": {"reason": "Owner dropped stage B.", "at": "t", "by_agent": "x", "merged_into": ""}},
            "c": {"description": "C", "status": "retired", "retired": {"reason": "merge", "at": "t", "by_agent": "x", "merged_into": "cd"}},
            "d": {"description": "D", "status": "retired", "retired": {"reason": "merge", "at": "t", "by_agent": "x", "merged_into": "cd"}},
        }
        changes = [
            {"revision": 2, "kind": "delivery work removed", "removed": ["b"], "merged_into": "", "reason": "Owner dropped stage B.", "at": "2026-11-02T10:00:00+00:00"},
            {"revision": 2, "kind": "delivery work removed", "removed": ["c"], "merged_into": "cd", "reason": "merge", "at": "2026-11-02T10:01:00+00:00"},
        ]
        return {"task_chunks": {"T": chunks}, "qa_requests": {}, "agents": {},
                "delivery_plans": {"T": {"mode": "chunked", "rationale": "Stages", "structure_revision": 2, "structure_changes": changes}}}

    def test_the_owner_card_counts_live_work_and_words_removals_truthfully(self):
        html = test_board_viewer.BoardViewerTests.rendered_task_cards(
            test_board_viewer.BoardViewerTests("run"), self.hand_state(), {"T": {"task": "T"}}, {"T": "Ship the stages."}, ["T"],
        )[0]["html"]
        self.assertIn("1 change checked and passed · 0 remaining", html)
        self.assertIn("Removed by Delivery: Owner dropped stage B.", html)
        self.assertIn("Merged into cd", html)
        self.assertIn("Work removed", html)
        self.assertNotIn("Work added after planning", html)
        self.assertIn("Merge not finished", html)
        self.assertIn("declare-chunks --reason", html)

    def test_history_counts_only_live_chunks(self):
        state = self.hand_state()
        entry = next(item for item in board_viewer._task_history(state, {}, []) if item["task"] == "T")
        self.assertEqual((entry["chunks_passed"], entry["chunks_total"]), (1, 1))

    def test_the_story_counts_live_parts_only(self):
        facts = story.task_facts(self.hand_state(), "T", [])
        self.assertEqual((facts["mode"], facts["done"], facts["total"]), ("chunked", 1.0, 1))

    def test_recovery_does_not_treat_a_retired_chunk_as_work_left(self):
        state = self.hand_state()
        entry = {"task": "T", "phase": "chunk", "subtask": "", "chunk": "a", "structure_revision": 2}
        state["review_escalations"] = {review_rounds.lineage_key(entry): {"task": "T", "status": "could_not_finish", "outcome": "x", "scope": entry}}
        self.assertFalse(board.work_left_outside_stopped_scopes(state, "T"))
        state["task_chunks"]["T"]["e"] = {"description": "E", "status": "open"}
        self.assertTrue(board.work_left_outside_stopped_scopes(state, "T"))


class IntegratedScenarioTests(RetireChunkBase):
    def test_merge_two_chunks_drop_one_review_the_live_chunks_and_pass_the_release_gates(self):
        # The owner: "Merge the remaining stages into two chunks ... Leave Stage 2 out completely."
        agent = self.chunked(("s1", "s2", "s3", "s4", "s5"))
        self.pass_request(self.request(agent, "s1", "chunk", chunk="s1"), "s1")
        board.retire_chunk(self.root, agent["id"], "s2", "Owner: leave Stage 2 out completely.")
        board.retire_chunk(self.root, agent["id"], "s3", "Owner: merge the remaining stages.", into="s34")
        half = board.retire_chunk(self.root, agent["id"], "s4", "Owner: merge the remaining stages.", into="s34")
        self.assertIn("declare the combined chunk s34", half["next_action"])
        with self.assertRaisesRegex(ValueError, "every chunk to pass first: s5"):
            self.request(agent, "premature", "final_acceptance")
        board.declare_chunks(self.root, agent["id"], [("s34", "Stages 3 and 4 together")], "Owner merged Stages 3 and 4.")
        self.assertEqual(sorted(name for name, value in self.chunks().items() if value["status"] != "retired"), ["s1", "s34", "s5"])
        for name in ("s34", "s5"):
            self.pass_request(self.request(agent, name, "chunk", chunk=name), name)
        final = self.request(agent, "final", "final_acceptance")
        board.claim_qa(self.root, self.reviewer["id"], final["id"], self.ledger("challenge-final", reviewer=True))
        brief = board.review_brief(self.root, final["id"], self.reviewer["id"])
        removed = {row["id"]: row["status"] for row in brief["product_structure"]["removed_chunks"]}
        self.assertEqual(removed, {"s2": "Removed by Delivery: Owner: leave Stage 2 out completely.",
                                   "s3": "Merged into s34", "s4": "Merged into s34"})
        self.assertTrue(any(change["kind"] == "delivery work removed" for change in brief["product_structure"]["scope_changes"]))
        board.execute_challenge(self.root, self.reviewer["id"], final["id"])
        board.qa_result(self.root, self.reviewer["id"], final["id"], "passed",
                        "TASK DONE: YES - merged and reduced plan independently accepted", self.evidence("final"))
        with board.locked_state(self.root) as state:
            state["agents"][agent["id"]].update({"status": "done", "active": False})
        ready = self.release_check_ready()
        for key in ("delivery_chunks_complete", "product_structure_complete", "internal_chunk_qa_evidence_complete",
                    "internal_final_qa_evidence_present", "unit_tests_passed", "development_qa_passed",
                    "independent_review_passed", "final_acceptance_review_present", "final_review_says_task_done_yes"):
            self.assertTrue(ready[key], key)
        self.assertEqual(self.chunks()["s2"]["status"], "retired")


if __name__ == "__main__":
    unittest.main()
