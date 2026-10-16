# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""A Completion Contract deliverable can be waived by the owner, recorded by the CTO: ``deliverable-waived``.

Owner 2026-10-14/15: on the Studio project the confirmed requirements mandated a
fresh real run that became impossible; the owner told the CTO it was not
required; the Reviewer correctly FAILed against the written requirement and the
board refused to amend confirmed requirements. The waiver is recorded beside the
requirements (never in them), only from the owner's own recorded words, and
every gate that counts deliverables treats a waived one as not required.
"""
from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from harness import board, board_surface, board_viewer, contract, control, review_brief
from tests.environment_support import require_loopback
from tests.test_requirements_contract import context, delivery_with_direction

DELIVERABLE_KEPT = "An owner-readable diagnostic report on the existing deck"
DELIVERABLE_WAIVED = "A prior-versus-new real-run comparison"
OWNER_WORDS = "fresh run not required, the report on the existing deck is enough"


def _project(base: Path) -> tuple[Path, dict, dict]:
    root = context(base)
    delivery = delivery_with_direction(root, "TASK-W")
    cto_session = control.create(root, "claude_cto")
    cto = board.register(root, "cto", "GLOBAL_MONITOR", vendor="Anthropic", session_id=cto_session["id"])
    board.create_contract(root, delivery["id"], "Diagnose the deck", [DELIVERABLE_KEPT, DELIVERABLE_WAIVED])
    return root, delivery, cto


def _owner_said(root: Path, cto: dict, text: str) -> dict:
    return board.record_owner_message(root, cto["id"], text, "cto_message")["message"]


def _waive(root: Path, cto: dict, message: dict, deliverable: str = DELIVERABLE_WAIVED, words: str = OWNER_WORDS, **kw) -> dict:
    return board.waive_deliverable(root, cto["id"], "TASK-W", deliverable, message["id"], words, **kw)


class DeliverableWaiverTests(unittest.TestCase):
    def setUp(self):
        require_loopback()
        super().setUp()

    def test_cto_records_the_owner_waiver_from_the_owner_words_and_nothing_immutable_changes(self):
        with TemporaryDirectory() as tmp:
            root, delivery, cto = _project(Path(tmp))
            before_contract = json.loads((root / ".harness" / "tasks" / "TASK-W.json").read_text())
            before_state = board.snapshot(root)
            revision_before = board._contract_revision(root, "TASK-W")["sha256"]
            message = _owner_said(root, cto, f"CTO: {OWNER_WORDS}. Carry on with the report.")

            result = _waive(root, cto, message)

            waiver = result["waiver"]
            self.assertIn(OWNER_WORDS, waiver["owner_message_text"], "the full owner message is kept with the record")
            self.assertEqual(waiver["deliverable"], DELIVERABLE_WAIVED)
            self.assertEqual(waiver["owner_words"], OWNER_WORDS)
            self.assertEqual(waiver["owner_message_id"], message["id"])
            self.assertEqual(waiver["owner_message_at"], message["created_at"])
            self.assertEqual(waiver["recorded_by"], cto["id"])
            self.assertTrue(waiver["recorded_at"])
            after_contract = json.loads((root / ".harness" / "tasks" / "TASK-W.json").read_text())
            item = next(d for d in after_contract["deliverables"] if d["name"] == DELIVERABLE_WAIVED)
            self.assertEqual(item["owner_waiver"]["owner_words"], OWNER_WORDS)
            self.assertEqual(after_contract["immutable_scope"], before_contract["immutable_scope"])
            self.assertEqual(board._contract_revision(root, "TASK-W")["sha256"], revision_before)
            state = board.snapshot(root)
            self.assertEqual(state["task_owner_directions"], before_state["task_owner_directions"])
            self.assertEqual(state["requirement_confirmations"], before_state["requirement_confirmations"])
            self.assertEqual([w["deliverable"] for w in state["deliverable_waivers"]["TASK-W"]], [DELIVERABLE_WAIVED])
            event = next(e for e in state["events"] if e["kind"] == "deliverable_waived_by_owner")
            self.assertIn(OWNER_WORDS, event["message"])
            self.assertEqual(event["task"], "TASK-W")

    def test_the_contract_revision_a_pass_certified_is_unchanged_by_a_waiver(self):
        with TemporaryDirectory() as tmp:
            root, delivery, cto = _project(Path(tmp))
            before = board._contract_revision(root, "TASK-W")
            _waive(root, cto, _owner_said(root, cto, OWNER_WORDS))
            after = board._contract_revision(root, "TASK-W")
            self.assertEqual(after["sha256"], before["sha256"])
            self.assertEqual(after["revision"], "immutable-scope")

    def test_waived_deliverable_is_not_required_by_completeness_gate_and_release_check(self):
        with TemporaryDirectory() as tmp:
            root, delivery, cto = _project(Path(tmp))
            evidence = root / "report.md"
            evidence.write_text("the report\n")
            contract.add_evidence(root, "TASK-W", DELIVERABLE_KEPT, [evidence])
            complete, problems, value = contract.contract_complete(root, "TASK-W")
            self.assertFalse(complete, problems)
            self.assertIn("Completion Contract is incomplete", " ".join(
                board._completion_gate_problems(root, board.snapshot(root), "TASK-W", delivery["id"])))

            _waive(root, cto, _owner_said(root, cto, OWNER_WORDS))

            complete, problems, value = contract.contract_complete(root, "TASK-W")
            self.assertTrue(complete, problems)
            self.assertEqual(contract.owner_test_steps(root, "TASK-W"), [f"Verify {DELIVERABLE_KEPT}."],
                             "the owner is never asked to verify what they waived")
            self.assertEqual(value["remaining_work"], [])
            self.assertEqual(value["status"], "complete")
            gate = board._completion_gate_problems(root, board.snapshot(root), "TASK-W", delivery["id"])
            self.assertFalse(any("Completion Contract is incomplete" in p for p in gate), gate)

    def test_a_non_waived_unverified_deliverable_still_blocks(self):
        with TemporaryDirectory() as tmp:
            root, delivery, cto = _project(Path(tmp))
            _waive(root, cto, _owner_said(root, cto, OWNER_WORDS))
            complete, problems, value = contract.contract_complete(root, "TASK-W")
            self.assertFalse(complete)
            self.assertEqual(value["remaining_work"], [DELIVERABLE_KEPT])

    def test_a_part_waiver_keeps_the_rest_of_the_deliverable_required(self):
        with TemporaryDirectory() as tmp:
            root, delivery, cto = _project(Path(tmp))
            message = _owner_said(root, cto, "The fresh run is not required; the comparison against the prior deck still is.")
            waiver = _waive(root, cto, message, words="The fresh run is not required", part="the fresh run")["waiver"]
            self.assertEqual(waiver["part"], "the fresh run")
            complete, problems, value = contract.contract_complete(root, "TASK-W")
            self.assertIn(DELIVERABLE_WAIVED, value["remaining_work"], "a part waiver does not drop the deliverable")
            bundle = board._task_context_bundle(board.snapshot(root), "TASK-W")
            self.assertIn("prove the rest", bundle["owner_waived_deliverables"][0]["rule"])

    def test_waiver_needs_the_owner_words_on_the_board_and_is_cto_only_and_once(self):
        with TemporaryDirectory() as tmp:
            root, delivery, cto = _project(Path(tmp))
            with self.assertRaisesRegex(ValueError, "no recorded owner message"):
                board.waive_deliverable(root, cto["id"], "TASK-W", DELIVERABLE_WAIVED, "msg-missing", OWNER_WORDS)
            other = _owner_said(root, cto, "Please keep the slide count under 45.")
            with self.assertRaisesRegex(ValueError, "not in owner message"):
                _waive(root, cto, other)
            with self.assertRaisesRegex(ValueError, "owner-words"):
                _waive(root, cto, other, words="  ")
            with self.assertRaisesRegex(ValueError, "owner-words"):
                _waive(root, cto, other, words="under 4")
            message = _owner_said(root, cto, f"Decision: {OWNER_WORDS}.")
            with self.assertRaisesRegex(ValueError, "unknown deliverable"):
                _waive(root, cto, message, deliverable="something else")
            with self.assertRaisesRegex(ValueError, "CTO"):
                board.waive_deliverable(root, delivery["id"], "TASK-W", DELIVERABLE_WAIVED, message["id"], OWNER_WORDS)
            first = _waive(root, cto, message)["waiver"]
            again = _waive(root, cto, message)["waiver"]
            self.assertEqual(again["id"], first["id"], "an identical retry returns the existing record")
            with self.assertRaisesRegex(ValueError, "already waived"):
                _waive(root, cto, _owner_said(root, cto, f"Again: {OWNER_WORDS}"))

    def test_owner_clarification_to_delivery_also_counts_as_the_owner_words(self):
        with TemporaryDirectory() as tmp:
            root, delivery, cto = _project(Path(tmp))
            message = board.record_owner_message(root, delivery["id"], f"Clarification: {OWNER_WORDS}", "clarification")["message"]
            waiver = _waive(root, cto, message)["waiver"]
            self.assertEqual(waiver["owner_message_id"], message["id"])
            # the same clarification cannot waive a deliverable of another task
            other_delivery = delivery_with_direction(root, "TASK-OTHER")
            board.create_contract(root, other_delivery["id"], "Other objective", ["Other deliverable"])
            with self.assertRaisesRegex(ValueError, "another task"):
                board.waive_deliverable(root, cto["id"], "TASK-OTHER", "Other deliverable", message["id"], OWNER_WORDS)

    def test_brief_context_board_and_viewer_show_the_waiver(self):
        with TemporaryDirectory() as tmp:
            root, delivery, cto = _project(Path(tmp))
            _waive(root, cto, _owner_said(root, cto, OWNER_WORDS))
            with board.locked_state(root) as state:
                state["requirement_confirmations"]["TASK-W"] = {
                    "text": "Final agreed requirements: report plus a prior-versus-new real-run comparison.",
                    "confirmed_at": board.now(), "version": 1,
                }
            state = board.snapshot(root)
            brief = review_brief.build(root, state, {"id": "review-1", "task": "TASK-W", "test_scope": "full"})
            waived = brief["risk_and_scope"]["owner_waived_deliverables"]
            self.assertEqual([w["deliverable"] for w in waived], [DELIVERABLE_WAIVED])
            self.assertEqual(waived[0]["owner_words"], OWNER_WORDS)
            self.assertIn("real-run comparison", brief["requirements"]["text"], "the confirmed text is history, unchanged")
            final = review_brief.build(root, state, {"id": "review-2", "task": "TASK-W", "test_scope": "full", "phase": "final_acceptance"})
            self.assertIn("not required", final["final_stage"]["owner_waivers"])
            bundle = board._task_context_bundle(state, "TASK-W")
            self.assertEqual(bundle["owner_waived_deliverables"][0]["deliverable"], DELIVERABLE_WAIVED)
            self.assertIn("not required", bundle["owner_waived_deliverables"][0]["rule"])
            text = board._render_board(state)
            self.assertIn("## Owner-waived deliverables", text)
            self.assertIn(OWNER_WORDS, text)
            payload = board_viewer.dashboard_payload(root)
            item = next(d for d in payload["contracts"]["TASK-W"]["deliverables"] if d["name"] == DELIVERABLE_WAIVED)
            self.assertEqual(item["owner_waiver"]["owner_words"], OWNER_WORDS)
            self.assertIn("Waived by you", board_viewer.rendered_page())

    def test_a_repair_package_member_is_disposed_of_by_the_waiver_not_an_invented_repair(self):
        from harness import repair_package as repair_package_model
        with TemporaryDirectory() as tmp:
            root, delivery, cto = _project(Path(tmp))
            waiver = _waive(root, cto, _owner_said(root, cto, OWNER_WORDS))["waiver"]
            package = repair_package_model.build(
                "TASK-W", {"id": "review-1", "phase": "final_acceptance", "reviewed_files": ["report.md"]},
                "final review failed on two findings",
                members=[
                    {"id": "F-001", "summary": "No fresh-run comparison: the comparison deliverable has no real run",
                     "category": "behavior", "surface": "report", "regression_check": "re-run the comparison"},
                    {"id": "F-002", "summary": "Report lacks the failure map: section 3 is empty",
                     "category": "behavior", "surface": "report", "regression_check": "python3 -m unittest tests.test_report"},
                ],
            )
            with board.locked_state(root) as state:
                state.setdefault("repair_packages", {})[package["id"]] = package
            members = package["members"]
            with self.assertRaisesRegex(ValueError, "unknown waiver"):
                board.resolve_repair_package(root, delivery["id"], package["id"], [
                    {"id": members[0]["id"], "resolution": "waived:waiver-nope", "regression_check": "-"},
                    {"id": members[1]["id"], "resolution": "Filled section 3 with the failure map", "regression_check": "python3 -m unittest tests.test_report"},
                ])
            resolved = board.resolve_repair_package(root, delivery["id"], package["id"], [
                {"id": members[0]["id"], "resolution": f"waived:{waiver['id']}", "regression_check": "-"},
                {"id": members[1]["id"], "resolution": "Filled section 3 with the failure map", "regression_check": "python3 -m unittest tests.test_report"},
            ])
            first = next(m for m in resolved["members"] if m["id"] == members[0]["id"])
            self.assertIn("Waived by the owner", first["resolution"])
            self.assertIn(waiver["id"], first["resolution"])
            self.assertEqual(resolved["status"], "ready_for_review")
            event = next(e for e in board.snapshot(root)["events"] if e["kind"] == "repair_package_resolved")
            self.assertEqual(event["waived_members"], 1)

    def test_cli_and_surface_expose_the_command_to_the_cto_only(self):
        self.assertIn("deliverable-waived", board_surface.ALL_BOARD_OPERATIONS)
        self.assertEqual(board_surface.AUTHORIZATION_MATRIX["deliverable-waived"], frozenset({"cto"}))
        self.assertIn("deliverable-waived", board_surface.AGENT_ARGUMENT_OPERATIONS)
        with TemporaryDirectory() as tmp:
            root, delivery, cto = _project(Path(tmp))
            message = _owner_said(root, cto, OWNER_WORDS)
            code = board.main(["--root", str(root), "deliverable-waived", "--agent", cto["id"], "--task", "TASK-W",
                               "--deliverable", DELIVERABLE_WAIVED, "--owner-message", message["id"], "--owner-words", OWNER_WORDS])
            self.assertEqual(code, 0)
            self.assertEqual(board.snapshot(root)["deliverable_waivers"]["TASK-W"][0]["deliverable"], DELIVERABLE_WAIVED)


if __name__ == "__main__":
    unittest.main()
