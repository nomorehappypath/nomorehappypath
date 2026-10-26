# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Review rounds inside a project end: the agreed scope is frozen at round 1 (task 2026-10-25_frozen-review-scope).

Owner, 2026-10-25: the reviewer "is always finding the smallest issue and round after round it continues"; "A blocking bug has to be
fixed"; and on an owner card at the cap: "BIG NO". These tests replay the loop on the real board (a temporary git project, the real
`request_review`, `qa_result`, repair packages and self-heal planner). Machine inputs removed: nothing is read from the machine; the
Delivery model comes from an explicit Settings document in a temporary home.

Run:  PYTHONPATH=. python3 -m unittest tests.test_frozen_review_scope -v
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from harness import board, control, global_settings, review_rounds, self_heal
from tests import test_repair_context as base

TESTED = "python3 -m unittest test_smoke -k passes"


def finding(identifier, **extra):
    value = {"id": identifier, "summary": f"{identifier}: the report page shows wrong totals",
             "harm": "in normal use the owner would see wrong totals on the report", "harm_tag": "none", "regression_check": TESTED}
    value.update(extra)
    return value


class Rounds(base.RepairContextTests):
    """Drives real review cycles; the inherited repair-context tests are not repeated here."""

    def setUp(self):
        super().setUp()
        self.dev = self._delivery()
        self.cycle = 0

    # -- helpers ----------------------------------------------------------------------------------------------------------------------
    def request(self, changes=""):
        if self.cycle:
            (self._workspace() / "module.py").write_text(f"VALUE = {self.cycle + 1}  # repair {self.cycle}\n")
            self._commit(f"repair {self.cycle}")
        self.cycle += 1
        return self._request(self.dev, changes=changes)

    def proof(self):
        path = self.root / "evidence" / "verdict-proof.txt"
        path.parent.mkdir(exist_ok=True)
        path.write_text("command: python3 -m unittest\nresult: FAIL\n")
        return str(path)

    def claim(self, request, certified=False):
        qa = board.register(self.root, "qa", "REVIEW_QUEUE", vendor="Anthropic")
        challenge = self.root / f"challenge-{request['cycle']}.md"
        challenge.write_text(base.HEADER + "| S-101 | An independent adversarial check confirms the repair through a distinct test scope. | adversarial | `" + TESTED + "` | Holds | PASS: executed | PASS |\n")
        board.claim_qa(self.root, qa["id"], request["id"], str(challenge))
        if certified:
            board.execute_challenge(self.root, qa["id"], request["id"])
        return qa

    def verdict(self, request, result, summary, findings=None, certified=False, qa=None):
        qa = qa or self.claim(request, certified)
        evidence = self.root / "evidence"
        evidence.mkdir(exist_ok=True)
        proof = evidence / f"verdict-{request['cycle']}.txt"
        proof.write_text("command: python3 -m unittest\nresult: FAIL\n")
        recorded = board.qa_result(self.root, qa["id"], request["id"], result, summary, "" if certified and result == "passed" else str(proof), failure_members=findings)
        return qa, recorded

    def fail_round_one(self, ids=("B1", "B2", "B3")):
        first = self.request()
        _qa, recorded = self.verdict(first, "failed", "TASK DONE: NO — three problems", [finding(i) for i in ids])
        return first, recorded

    def package(self, recorded):
        return board.snapshot(self.root)["repair_packages"][recorded["created_repair_package_id"]]

    def resolve(self, recorded, ids=("B1", "B2", "B3")):
        board.resolve_repair_package(self.root, self.dev["id"], recorded["created_repair_package_id"],
                                     [{"id": i, "resolution": f"fixed {i} at its cause", "regression_check": TESTED} for i in ids])

    def state(self):
        return board.snapshot(self.root)

    def owner_cards(self):
        return [a for a in (self.state().get("owner_actions") or {}).values() if a.get("status") == "open"]

    # -- round 1 -----------------------------------------------------------------------------------------------------------------------
    def test_three_blockers_in_round_one_are_recorded_and_delivery_must_resolve_all_three(self):
        _first, recorded = self.fail_round_one()
        package = self.package(recorded)
        self.assertEqual([m["id"] for m in package["members"]], ["B1", "B2", "B3"])
        self.assertEqual(package["members"][0]["harm"], "in normal use the owner would see wrong totals on the report")
        self.assertTrue(package["requires_explicit_resolution"])
        self.assertEqual(recorded["board_decision"]["blocking"], ["B1", "B2", "B3"])
        self.assertEqual(recorded["submitted_verdict"]["summary"], "TASK DONE: NO — three problems")
        (self._workspace() / "module.py").write_text("VALUE = 9\n")
        self._commit("a repair that names nothing")
        with self.assertRaisesRegex(ValueError, "still open: B1, B2, B3"):
            self._request(self.dev, changes="fixed things")
        with self.assertRaisesRegex(ValueError, "cover every package member"):
            board.resolve_repair_package(self.root, self.dev["id"], recorded["created_repair_package_id"],
                                         [{"id": "B1", "resolution": "fixed B1 at its cause", "regression_check": TESTED}])

    def test_a_finding_goes_through_the_command_line_and_the_agent_tool_as_json(self):
        import contextlib
        import io
        from harness import board_mcp
        first = self.request()
        qa = self.claim(first)
        with contextlib.redirect_stdout(io.StringIO()):
            code = board.main(["--root", str(self.root), "qa-result", "--agent", qa["id"], "--request", first["id"], "--result", "failed",
                               "--summary", "TASK DONE: NO — totals", "--evidence", self.proof(), "--failure", json.dumps(finding("B1")), "--failure", json.dumps(finding("B2"))])
        self.assertEqual(code, 0)
        self.assertEqual(self.state()["qa_requests"][first["id"]]["board_decision"]["blocking"], ["B1", "B2"])
        argv = board_mcp.argv_for("qa-result", {"request": "r", "result": "failed", "summary": "s", "failure": [
            {"id": "B1", "summary": "totals are wrong", "harm": "the owner sees wrong totals", "letter": "a", "evidence_ref": "rerun"}]})
        self.assertEqual(json.loads(argv[argv.index("--failure") + 1])["letter"], "a")
        tool = next(item for item in board_mcp.tool_definitions("qa") if item["name"] == "qa-result")
        self.assertIn("harm_tag", tool["inputSchema"]["properties"]["failure"]["items"]["properties"])

    def test_a_finding_without_a_harm_line_is_sent_back_and_nothing_is_recorded(self):
        first = self.request()
        qa = self.claim(first)
        bad = finding("B1")
        bad.pop("harm")
        with self.assertRaisesRegex(ValueError, "has no harm line.*To fix it"):
            board.qa_result(self.root, qa["id"], first["id"], "failed", "TASK DONE: NO — x", self.proof(), failure_members=[bad])
        self.assertEqual(self.state()["qa_requests"][first["id"]]["status"], "claimed", "the review is still open for the correction")
        self.assertEqual(self.state().get("review_escalations", {}), {})

    def test_a_safety_finding_without_a_harm_line_or_check_still_blocks(self):
        first = self.request()
        _qa, recorded = self.verdict(first, "failed", "TASK DONE: NO — a leak", [{"id": "B1", "summary": "the export writes the owner's key into a public log", "harm_tag": "leak"}])
        member, = self.package(recorded)["members"]
        self.assertEqual((member["harm_tag"], member["id"]), ("leak", "B1"))
        self.assertTrue(member["harm"] and member["regression_check"])

    def test_a_pass_that_carries_blocking_findings_is_refused(self):
        first = self.request()
        qa = self.claim(first, certified=True)
        with self.assertRaisesRegex(ValueError, "cannot carry blocking findings"):
            board.qa_result(self.root, qa["id"], first["id"], "passed", "TASK DONE: YES", "", failure_members=[finding("B1")])

    # -- round 2 -----------------------------------------------------------------------------------------------------------------------
    def second_round(self):
        _first, recorded = self.fail_round_one()
        self.resolve(recorded)
        return recorded, self.request(changes="resolved B1, B2 and B3")

    def test_a_full_repair_passes_in_round_two(self):
        _recorded, second = self.second_round()
        self.assertEqual(second["frozen_scope"]["round"], 2)
        self.assertEqual([f["id"] for f in second["frozen_scope"]["round_1_findings"]], ["B1", "B2", "B3"])
        qa = self.claim(second, certified=True)
        done = board.qa_result(self.root, qa["id"], second["id"], "passed", "TASK DONE: YES — the report shows right totals", "")
        self.assertEqual(done["status"], "passed")
        self.assertEqual(self.state()["review_escalations"][review_rounds.lineage_key(done)]["status"], "closed")

    def test_a_new_well_formed_finding_in_round_two_becomes_pass_with_notes_and_a_plain_reason(self):
        _recorded, second = self.second_round()
        verdict = "TASK DONE: YES — the report shows right totals\nBLOCKING: the footer is two pixels low"
        _qa, done = self.verdict(second, "failed", verdict, [finding("R2-1", letter="new", summary="the footer is two pixels low on the report page")], certified=True)
        self.assertEqual(done["status"], "passed")
        self.assertEqual(done["result_summary"], verdict, "the submitted text is kept verbatim")
        self.assertEqual(done["submitted_verdict"]["result"], "failed")
        decision = done["board_decision"]
        self.assertTrue(decision["reclassified"] and decision["effective_result"] == "passed")
        self.assertIn("PASS with notes", decision["message"])
        self.assertIn("kept as notes", decision["message"])
        self.assertEqual([n["id"] for n in decision["notes"]], ["R2-1"])
        self.assertEqual(self.owner_cards(), [])

    def test_an_unfixed_round_one_finding_still_blocks_and_only_it_must_be_repaired(self):
        _recorded, second = self.second_round()
        _qa, failed = self.verdict(second, "failed", "TASK DONE: NO — B2 is still wrong", [finding("B2", letter="a", evidence_ref="rerun of the report check still shows 41 instead of 42")])
        self.assertEqual(failed["status"], "failed")
        self.assertEqual([m["id"] for m in self.package(failed)["members"]], ["B2"])

    def test_something_the_repair_broke_blocks_when_the_same_check_passed_before_and_fails_now(self):
        first, recorded = self.fail_round_one()
        self.resolve(recorded)
        second = self.request(changes="resolved all three")
        paired = {"command": TESTED, "earlier_commit": first["reviewed_commit"], "earlier_result": "PASS", "repaired_commit": second["reviewed_commit"], "repaired_result": "FAIL"}
        _qa, failed = self.verdict(second, "failed", "TASK DONE: NO — the repair broke login", [finding("R2-1", letter="b", summary="login now rejects valid users", paired=paired, evidence_ref="login check output: 401 on the repaired version")])
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(self.package(failed)["members"][0]["letter"], "b")
        self.resolve(failed, ids=["R2-1"])
        third = self.request(changes="fixed login")
        qa = self.claim(third)
        bogus = dict(paired, earlier_commit="0" * 40)
        with self.assertRaisesRegex(ValueError, r"\(b\) does not point at something the board recorded as passing"):
            board.qa_result(self.root, qa["id"], third["id"], "failed", "TASK DONE: NO — x", self.proof(),
                            failure_members=[finding("R3-1", letter="b", paired=bogus, evidence_ref="it fails now on the repaired version")])

    def test_a_required_path_that_was_never_delivered_blocks_as_an_unmet_agreed_requirement(self):
        _recorded, second = self.second_round()
        _qa, failed = self.verdict(second, "failed", "TASK DONE: NO — downloading gives an empty file", [finding(
            "R2-1", letter="c", summary="the download of the report produces an empty file", criterion="ship the repair flow with evidence",
            evidence_ref="downloaded report.csv has 0 bytes on the repaired version")])
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(self.package(failed)["members"][0]["letter"], "c")

    def test_malformed_findings_in_round_two_get_a_correction_request_and_are_never_notes(self):
        _recorded, second = self.second_round()
        qa = self.claim(second, certified=True)
        proof = self.proof()
        cases = [
            (finding("R2-1"), "has no letter"),
            (finding("B9", letter="a", evidence_ref="rerun shows it still fails"), "not a round-1 finding of this scope"),
            (finding("R2-1", letter="a", evidence_ref="short"), "reuses a round-1 id|has no current evidence|not a round-1"),
            (finding("R2-1", letter="c", criterion="something nobody agreed to", evidence_ref="it fails on the repaired version"), "does not name an agreed requirement"),
            (finding("R2-1", letter="d"), "no harm_tag"),
        ]
        for bad, message in cases:
            with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                board.qa_result(self.root, qa["id"], second["id"], "failed", "TASK DONE: NO — x", proof, failure_members=[bad])
        with self.assertRaisesRegex(ValueError, "every blocker must be listed as a finding"):
            board.qa_result(self.root, qa["id"], second["id"], "failed", "TASK DONE: NO — a vague worry", proof)
        self.assertEqual(self.state()["qa_requests"][second["id"]]["status"], "claimed")

    def test_a_safety_finding_without_a_harm_line_blocks_in_round_two_too(self):
        _recorded, second = self.second_round()
        _qa, failed = self.verdict(second, "failed", "TASK DONE: NO — secrets in the log", [{"id": "R2-1", "summary": "the new code logs the login token", "harm_tag": "leak"}])
        self.assertEqual(failed["status"], "failed")
        member, = self.package(failed)["members"]
        self.assertEqual((member["letter"], member["harm_tag"]), ("d", "leak"))

    def test_a_verdict_that_says_no_or_not_tested_is_never_turned_into_a_pass(self):
        _recorded, second = self.second_round()
        qa = self.claim(second, certified=True)
        proof = self.proof()
        note = [finding("R2-1", letter="new", summary="the footer is two pixels low on the report page")]
        with self.assertRaisesRegex(ValueError, "does not start with TASK DONE: YES"):
            board.qa_result(self.root, qa["id"], second["id"], "failed", "TASK DONE: NO — the footer is low", proof, failure_members=note)
        recorded = board.qa_result(self.root, qa["id"], second["id"], "failed", "TASK DONE: NOT TESTED — the browser would not start in my sandbox", proof, failure_members=note)
        self.assertEqual(recorded["status"], "failed", "a testing gap is a failed judgment, never a pass")

    def test_an_early_fail_without_certification_never_becomes_a_pass(self):
        _recorded, second = self.second_round()
        qa = board.register(self.root, "qa", "REVIEW_QUEUE", vendor="Anthropic")
        board.reserve_qa(self.root, qa["id"], second["id"])
        failing = self.root / "evidence" / "early.txt"
        failing.write_text("command: python3 -m unittest test_smoke\nresult: FAIL\nRan 1 tests\nFAILED (failures=1)\n")
        recorded = board.qa_result(self.root, qa["id"], second["id"], "failed", "TASK DONE: YES — walk fine, but the checkout fails", str(failing),
                                   failure_members=[finding("R2-1", letter="new", summary="the frozen candidate's own tests fail")])
        self.assertEqual(recorded["status"], "failed")
        self.assertIn("cannot be turned into a PASS", recorded["board_decision"]["message"])

    def test_an_uncertified_review_cannot_become_a_pass_with_notes(self):
        _recorded, second = self.second_round()
        qa = self.claim(second, certified=False)
        with self.assertRaisesRegex(ValueError, "no certified run.*execute-challenge"):
            board.qa_result(self.root, qa["id"], second["id"], "failed", "TASK DONE: YES — fine", self.proof(),
                            failure_members=[finding("R2-1", letter="new", summary="the footer is two pixels low on the report page")])

    def test_a_changed_structure_starts_a_new_round_one(self):
        _recorded, second = self.second_round()
        self.verdict(second, "failed", "TASK DONE: NO — B2 again", [finding("B2", letter="a", evidence_ref="rerun still shows 41 instead of 42")])
        with board.locked_state(self.root) as state:
            state["delivery_plans"]["TASK-R"]["structure_revision"] = int(state["delivery_plans"]["TASK-R"].get("structure_revision", 0)) + 1
        third = self.request(changes="structure changed and repaired")
        self.assertEqual((third["frozen_scope"]["round"], third["frozen_scope"]["frozen"]), (1, False), "an earlier lineage's baseline does not carry over")
        self.assertEqual(third["repair_package_id"], "", "an older revision's unresolved package does not block the new round 1")
        _qa, recorded = self.verdict(third, "failed", "TASK DONE: NO — a new problem", [finding("N1")])
        self.assertEqual(recorded["board_decision"]["round"], 1)

    def test_a_review_recorded_before_this_rule_is_history_and_the_next_structured_fail_is_round_one(self):
        first = self.request()
        qa = self.claim(first)
        board.qa_result(self.root, qa["id"], first["id"], "failed", "BLOCKING: an old style verdict", self.proof())
        with board.locked_state(self.root) as state:
            for key in ("review_round", "submitted_verdict", "board_decision", "structured_findings"):
                state["qa_requests"][first["id"]].pop(key, None)        # what a request recorded by the previous version looks like
            state["review_escalations"].clear()
        second = self.request(changes="repaired")
        self.assertEqual(second["frozen_scope"]["round"], 1)
        _qa, recorded = self.verdict(second, "failed", "TASK DONE: NO — structured now", [finding("B1")])
        self.assertEqual(recorded["board_decision"]["round"], 1)
        self.assertEqual(self.state()["review_escalations"][review_rounds.lineage_key(recorded)]["failed_judgments"], 1, "the earlier failure is history, not counted")


class RetainedPlan(Rounds):
    """Round 1's executed checks and their supporting files are kept; round 2 confirms them and runs them through execute-challenge."""

    def support_file(self, files):
        path = self.root / "evidence" / "support.json"
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps({"files": files}))
        return str(path)

    def reserve_and_attach(self, request, files):
        qa = board.register(self.root, "qa", "REVIEW_QUEUE", vendor="Anthropic")
        board.reserve_qa(self.root, qa["id"], request["id"])
        self.assertEqual(board.record_review_support(self.root, qa["id"], request["id"], self.support_file(files))["files"], sorted(files))
        challenge = self.root / f"challenge-{request['cycle']}.md"
        challenge.write_text(base.HEADER + "| S-101 | An independent adversarial check confirms the repair through a distinct test scope. | adversarial | `" + TESTED + "` | Holds | PASS: executed | PASS |\n")
        board.attach_challenge_ledger(self.root, qa["id"], request["id"], str(challenge))
        return qa

    def test_round_one_keeps_the_checks_and_round_two_gets_them_back_with_changed_bytes_never_reusing_evidence(self):
        first = self.request()
        qa = self.reserve_and_attach(first, {"check_totals.py": "print('totals')\n"})
        recorded = board.qa_result(self.root, qa["id"], first["id"], "failed", "TASK DONE: NO — totals", self.proof(), failure_members=[finding("B1")])
        plan = self.state()["retained_plans"][review_rounds.lineage_key(recorded)]
        self.assertEqual(plan["commands"], [TESTED])
        self.assertIn("S-101", plan["ledger"])
        self.assertEqual(list(plan["support_files"]), ["check_totals.py"])
        self.resolve(recorded, ids=["B1"])
        second = self.request(changes="resolved B1")
        self.assertEqual(second["frozen_scope"]["retained_plan"]["support_files"], ["check_totals.py"])
        self.assertEqual(second["challenge_support_files"], {"check_totals.py": "print('totals')\n"}, "the stored files come back with the next round")
        reviewer = self.claim(second)
        self.assertEqual(self.state()["qa_requests"][second["id"]]["status"], "claimed")
        shown = board.show_retained_plan(self.root, reviewer["id"], second["id"])
        self.assertTrue(shown["retained"] and shown["commands"] == [TESTED])
        # the files are frozen with the attached ledger: they cannot change afterwards, so earlier evidence can never be reused for other bytes
        with self.assertRaisesRegex(ValueError, "frozen when the Challenge Ledger is attached"):
            board.record_review_support(self.root, reviewer["id"], second["id"], self.support_file({"check_totals.py": "print('changed')\n"}))
        authorization = self.state()["qa_requests"][second["id"]]["challenge_execution_authorization"]
        self.assertTrue(authorization["sha256"])

    def test_the_supporting_files_are_placed_in_the_checkout_and_run_through_execute_challenge(self):
        first = self.request()
        qa = board.register(self.root, "qa", "REVIEW_QUEUE", vendor="Anthropic")
        board.reserve_qa(self.root, qa["id"], first["id"])
        board.record_review_support(self.root, qa["id"], first["id"], self.support_file({
            "check_report.py": "import unittest\n\nclass Report(unittest.TestCase):\n    def test_marker_file_is_here(self): self.assertTrue(True)\n"}))
        command = 'python3 -m unittest discover -s .review-checks -p "check_*.py"'
        challenge = self.root / "challenge-support.md"
        challenge.write_text(base.HEADER + "| S-101 | The reviewer's own supporting check runs from the folder the board places it in. | adversarial | `" + command + "` | Holds | PASS: executed | PASS |\n")
        board.attach_challenge_ledger(self.root, qa["id"], first["id"], str(challenge))
        executed = board.execute_challenge(self.root, qa["id"], first["id"])
        self.assertEqual(executed["scenarios"], 1)
        self.assertIn("Ran 1 test", Path(executed["evidence"]).read_text())

    def test_a_round_two_ledger_that_leaves_out_a_retained_check_is_told_so_but_not_refused(self):
        first = self.request()
        qa = self.reserve_and_attach(first, {"check_totals.py": "print('totals')\n"})
        recorded = board.qa_result(self.root, qa["id"], first["id"], "failed", "TASK DONE: NO — totals", self.proof(), failure_members=[finding("B1")])
        self.resolve(recorded, ids=["B1"])
        second = self.request(changes="resolved B1")
        reviewer = board.register(self.root, "qa", "REVIEW_QUEUE", vendor="Anthropic")
        board.reserve_qa(self.root, reviewer["id"], second["id"])
        ledger = self.root / "challenge-other.md"
        ledger.write_text(base.HEADER + "| S-102 | A different check of the repaired report path through another focus. | adversarial | `python3 -m unittest test_smoke -v` | Holds | PASS: executed | PASS |\n")
        attached = board.attach_challenge_ledger(self.root, reviewer["id"], second["id"], str(ledger))
        self.assertEqual(attached["retained_commands_missing"], [TESTED])
        self.assertIn("say in your verdict why", attached["retained_plan_note"])
        self.assertEqual(attached["status"], "claimed")

    def test_support_file_names_are_flat_and_bounded(self):
        first = self.request()
        qa = self.claim(first)
        for bad in ({"../escape.py": "x"}, {"sub/dir.py": "x"}, {".hidden": "x"}, {"ok.py": 5}, {}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                board.record_review_support(self.root, qa["id"], first["id"], self.support_file(bad))

    def test_a_managed_reviewer_may_use_the_new_operations_and_delivery_may_not(self):
        from harness import board_surface
        for operation in ("review-support", "retained-plan"):
            self.assertEqual(board_surface.AUTHORIZATION_MATRIX[operation], frozenset({"qa"}))
        self.assertEqual(board_surface.UPLOAD_ARGUMENTS["review-support"], ("--submission", "submission", True))
        from harness import board_client
        self.assertIn("review-support", board_client.UPLOAD_ARGUMENTS)


class Escalation(Rounds):
    """The third failed judgment: one fresh Delivery attempt, then an honest outcome. Never an owner card."""

    def fail_three(self):
        _first, recorded = self.fail_round_one()
        last = recorded
        for number in (2, 3):
            self.resolve(last, ids=[m["id"] for m in self.package(last)["members"]])
            request = self.request(changes=f"repair {number}")
            _qa, last = self.verdict(request, "failed", "TASK DONE: NO — B2 is still wrong", [finding("B2", letter="a", evidence_ref="rerun still shows 41 instead of 42")])
        return last

    def entry(self):
        entries = self.state()["review_escalations"]
        return next(iter(entries.values()))

    def test_the_third_failed_judgment_records_one_recovery_that_survives_a_restart(self):
        self.fail_three()
        entry = self.entry()
        self.assertEqual((entry["status"], entry["phase"], entry["failed_judgments"], entry["recovery_attempts"]), ("recovery", "requested", 3, 1))
        self.assertIn("B2", entry["brief"])
        self.assertIn("fixed B2 at its cause", entry["brief"], "the brief says what the last repair claimed")
        # a restart: nothing is held in memory, the planner reads the board again and plans exactly one stop, never a second attempt
        actions = self_heal.plan(self.root)
        self.assertEqual([a["action"] for a in actions if a["action"].startswith("review_recovery")], ["review_recovery_stop"])
        self.assertEqual(self.owner_cards(), [])
        with self.assertRaisesRegex(ValueError, "fresh Delivery attempt is being started"):
            self.request(changes="the old agent tries again")
        cto = board.register(self.root, "cto", "GLOBAL_MONITOR", vendor="Anthropic")
        polled = board.poll(self.root, cto["id"])
        self.assertEqual(polled["review_escalations"][0]["status"], "recovery")
        self.assertIn("the owner is not asked", polled["review_escalations"][0]["next_step"])
        self.assertIn("REVIEW LIMIT", board._review_escalation_note(self.state()))
        brief_poll = board.poll(self.root, self.dev["id"])
        self.assertIn("B2", brief_poll["review_recovery"][0]["brief"])

    def test_recovery_runs_through_the_steps_once_and_a_later_failure_ends_it_honestly(self):
        self.fail_three()
        sessions = []
        launched = []

        def create(kind, continues="", recovery=False):
            session = control.create(self.root, kind, fresh=recovery)
            sessions.append(session)
            return session

        for _ in range(4):
            for action in self_heal.plan(self.root):
                if action["action"].startswith("review_recovery"):
                    self_heal.execute(self.root, action, launched.append, create)
        entry = self.entry()
        self.assertEqual(len(sessions), 1, "exactly one fresh session, however many ticks")
        self.assertEqual((entry["status"], entry["phase"]), ("recovery", "started"))
        self.assertEqual(sessions[0].get("continues_session"), None, "a blank conversation")
        self.assertEqual(launched, sessions)
        binding = self.state()["replacement_bindings"][sessions[0]["id"]]
        self.assertEqual(binding["task"], "TASK-R")
        # the fresh Delivery session registers (the board hands it the bound task) and its repair fails review once more
        replacement = board.register(self.root, "development", board.AWAITING_OWNER_DIRECTION, vendor="OpenAI", session_id=sessions[0]["id"])
        self.assertEqual(replacement["task"], "TASK-R")
        self.dev = replacement
        self.resolve({"created_repair_package_id": self._latest_package_id()}, ids=["B2"])
        request = self.request(changes="the fresh attempt")
        self.verdict(request, "failed", "TASK DONE: NO — B2 is still wrong", [finding("B2", letter="a", evidence_ref="rerun still shows 41 instead of 42")])
        entry = self.entry()
        self.assertEqual(entry["status"], "could_not_finish")
        self.assertIn("Could not finish", entry["outcome"])
        self.assertEqual(self.owner_cards(), [])
        with self.assertRaisesRegex(ValueError, "could not be finished automatically"):
            self.request(changes="once more")
        for _ in range(2):
            self.assertEqual([a["action"] for a in self_heal.plan(self.root) if a["action"].startswith("review_recovery")], ["review_recovery_retire"], "only the stop of the finished attempt, never a relaunch")
        review_rounds.terminal_for(self.state()["review_escalations"], "TASK-R")
        self.assertFalse(board._agent_has_actionable_work(self.state(), next(a for a in self.state()["agents"].values() if a["role"] == "development")),
                         "a stopped part is not work the watchdog chases")

    def _latest_package_id(self):
        packages = sorted(self.state()["repair_packages"].values(), key=lambda p: p["created_at"])
        return packages[-1]["id"]

    def test_a_fresh_attempt_that_cannot_start_ends_in_could_not_finish_at_once(self):
        self.fail_three()

        def create(kind, continues="", recovery=False):
            raise ValueError("no capacity")

        for action in self_heal.plan(self.root):
            if action["action"] == "review_recovery_stop":
                continue
        # the old terminal is gone: the planner moves to creating
        with board.locked_state(self.root) as state:
            next(iter(state["review_escalations"].values())).update(phase="stopping", old_session_id="gone", old_agent_id=self.dev["id"])
        for action in self_heal.plan(self.root):
            if action["action"].startswith("review_recovery"):
                with self.assertRaises(ValueError):
                    self_heal.execute(self.root, action, lambda s: None, create)
        entry = self.entry()
        self.assertEqual(entry["status"], "could_not_finish")
        self.assertIn("could not be started", entry["outcome"])
        self.assertEqual(self.owner_cards(), [])
        self.assertEqual([a for a in self_heal.plan(self.root) if a["action"].startswith("review_recovery")], [])

    def test_a_recovery_that_never_reaches_another_review_settles_after_the_allowed_wait(self):
        self.fail_three()
        with board.locked_state(self.root) as state:
            next(iter(state["review_escalations"].values())).update(phase="started", session_id="x", started_at="2000-01-01T00:00:00+00:00")
        names = [a["action"] for a in self_heal.plan(self.root) if a["action"].startswith("review_recovery")]
        self.assertEqual(names, ["review_recovery_settle"])


class StoppedRecovery(Escalation):
    """Round-1 review of c0d2c9e: a stopped recovery is retired through the managed lifecycle and no ordinary route brings it back."""

    def fresh_attempt(self):
        self.fail_three()
        for _ in range(4):
            for action in self_heal.plan(self.root):
                if action["action"].startswith("review_recovery"):
                    self_heal.execute(self.root, action, lambda session: None,
                                      lambda kind, continues="", recovery=False: control.create(self.root, kind, fresh=recovery))
        session_id = self.entry()["session_id"]
        self.dev = board.register(self.root, "development", board.AWAITING_OWNER_DIRECTION, vendor="OpenAI", session_id=session_id)
        return session_id

    def final_failure(self):
        self.resolve({"created_repair_package_id": self._latest_package_id()}, ids=["B2"])
        request = self.request(changes="the fresh repair still fails")
        self.verdict(request, "failed", "TASK DONE: NO — B2 remains", [finding("B2", letter="a", evidence_ref="the report still has incorrect totals")])
        self.assertEqual(self.entry()["status"], "could_not_finish")

    def session(self, session_id):
        return next(s for s in control.snapshot(self.root)["sessions"] if s["id"] == session_id)

    def test_a_natural_exit_after_the_final_failure_is_never_relaunched(self):
        session_id = self.fresh_attempt()
        self.final_failure()
        with control.locked_state(self.root) as state:
            state["sessions"][session_id].update(status="exited", ended_at=board.now())
        planned = self_heal.plan(self.root)
        self.assertFalse([a for a in planned if a["action"] in {"relaunch_ended", "relaunch", "restart"} and a.get("session_id") == session_id])

    def test_the_session_of_a_stopped_recovery_is_stopped_by_the_planner(self):
        session_id = self.fresh_attempt()
        self.final_failure()
        retire = [a for a in self_heal.plan(self.root) if a["action"] == "review_recovery_retire"]
        self.assertEqual([a["session_id"] for a in retire], [session_id])
        self_heal.execute(self.root, retire[0], lambda session: None)
        live = self.session(session_id)
        self.assertTrue(live.get("stop_requested_at") or live["status"] not in control.ACTIVE_STATUSES)
        self.assertEqual([a for a in self_heal.plan(self.root) if a["action"] == "review_recovery_retire"], [], "retired once")

    def test_a_recovery_that_times_out_is_stopped_not_left_launching(self):
        session_id = self.fresh_attempt()
        with board.locked_state(self.root) as state:
            next(iter(state["review_escalations"].values()))["started_at"] = "2000-01-01T00:00:00+00:00"
        for action in self_heal.plan(self.root):
            if action["action"] == "review_recovery_settle":
                self_heal.execute(self.root, action, lambda session: None)
        self.assertEqual(self.entry()["status"], "could_not_finish")
        live = self.session(session_id)
        self.assertTrue(live.get("stop_requested_at") or live["status"] not in control.ACTIVE_STATUSES, live)

    def exhaust_with_restart_pending(self):
        session_id = self.fresh_attempt()
        board.request_agent_restart(self.root, self.dev["id"], "terminal stopped responding during the last review")
        self.final_failure()
        return session_id

    def test_a_restart_requested_before_the_final_failure_never_relaunches_the_stopped_recovery(self):
        session_id = self.exhaust_with_restart_pending()
        with control.locked_state(self.root) as state:
            state["sessions"][session_id].update(status="exited", ended_at=board.now())
        planned = [a for a in self_heal.plan(self.root) if a.get("session_id") == session_id]
        self.assertEqual([a for a in planned if a["action"] in {"relaunch", "relaunch_ended", "restart"}], [])
        launches = []
        stale = {"action": "relaunch", "session_id": session_id, "agent_id": self.dev["id"], "kind": "codex_delivery", "self_heal_key": "agent:x", "reason": "pending"}
        outcome = self_heal.execute(self.root, stale, lambda session: launches.append(session["id"]))
        self.assertEqual(launches, [], "even a plan made a tick earlier is re-checked just before it acts")
        self.assertIn("not brought back", outcome["skipped"])

    def test_a_silent_running_session_of_a_stopped_recovery_is_retired_not_restarted(self):
        from datetime import datetime, timedelta, timezone
        session_id = self.fresh_attempt()
        self.final_failure()
        stale = (datetime.now(timezone.utc) - timedelta(minutes=20)).isoformat()
        with control.locked_state(self.root) as state:
            state["sessions"][session_id].update(status="running", last_output_at=stale, attached_at=stale)
            state.setdefault("inbox", {})[session_id] = [{"id": "old-message", "queued_at": stale, "text": "poll"}]
        names = [a["action"] for a in self_heal.plan(self.root) if a.get("session_id") == session_id]
        self.assertEqual(names, ["review_recovery_retire"], "retirement wins; no restart is scheduled")

    def test_a_pending_restart_is_cleared_when_the_recovery_is_retired(self):
        session_id = self.exhaust_with_restart_pending()
        retire = next(a for a in self_heal.plan(self.root) if a["action"] == "review_recovery_retire")
        self_heal.execute(self.root, retire, lambda session: None)
        agent = self.state()["agents"][self.dev["id"]]
        self.assertFalse(agent.get("restart_requested_at"))


class TwoChunks(Escalation):
    """Round-1 review of c0d2c9e: only the exhausted chunk stops; the independent chunk stays watched, relaunched and served."""

    _mode = "chunked"

    def _delivery(self):
        dev = super()._delivery()
        board.declare_chunks(self.root, dev["id"], [("part-1", "report calculation"), ("part-2", "independent export")])
        return dev

    def _request(self, dev, changes=""):
        ledger = self.root / "ledger.md"
        ledger.write_text(base.HEADER + "| S-001 | The repaired calculation produces the correct report totals. | flow | `python3 -m unittest test_smoke` | Holds | PASS: executed | PASS |\n")
        return board.request_review(self.root, dev["id"], str(ledger), "review the report calculation", phase="chunk", chunk="part-1",
                                    test_command="python3 -m unittest test_smoke", changes=changes)

    def stop_part_one(self):
        StoppedRecovery.fresh_attempt(self)
        StoppedRecovery.final_failure(self)

    def test_conversion_to_pass_needs_the_first_line_statement_for_a_chunk_too(self):
        _recorded, second = self.second_round()
        qa = self.claim(second, certified=True)
        note = [finding("R2-1", letter="new")]
        with self.assertRaisesRegex(ValueError, "does not start with TASK DONE: YES"):
            board.qa_result(self.root, qa["id"], second["id"], "failed", "BLOCKING: report footer alignment needs changing", self.proof(), failure_members=note)
        self.assertEqual(self.state()["qa_requests"][second["id"]]["status"], "claimed", "nothing was recorded")
        done = board.qa_result(self.root, qa["id"], second["id"], "failed", "TASK DONE: YES — part 1 is complete\nBLOCKING: footer alignment", self.proof(), failure_members=note)
        self.assertEqual(done["status"], "passed")
        self.assertEqual(done["submitted_verdict"]["result"], "failed")

    def test_the_other_chunk_keeps_the_watchdog_and_the_relaunch_while_the_stopped_one_does_not(self):
        self.stop_part_one()
        agent = next(a for a in self.state()["agents"].values() if a["id"] == self.dev["id"])
        self.assertTrue(board._agent_has_actionable_work(self.state(), agent), "part-2 is unfinished")
        self.assertTrue(self_heal._unfinished(self.state(), agent))
        self.assertEqual([a for a in self_heal.plan(self.root) if a["action"] == "review_recovery_retire"], [], "the session still serves part-2")
        board.request_agent_restart(self.root, self.dev["id"], "terminal stopped responding")
        session_id = self.state()["agents"][self.dev["id"]]["session_id"]
        with control.locked_state(self.root) as state:
            state["sessions"][session_id].update(status="exited", ended_at=board.now())
        self.assertTrue([a for a in self_heal.plan(self.root) if a["action"] == "relaunch" and a.get("session_id") == session_id],
                        "a session serving the unfinished part-2 is still restarted")
        with board.locked_state(self.root) as state:
            state["task_chunks"]["TASK-R"]["part-2"]["status"] = "passed"
        agent = next(a for a in self.state()["agents"].values() if a["id"] == self.dev["id"])
        self.assertFalse(board._agent_has_actionable_work(self.state(), agent), "only the stopped part is left")
        self.assertFalse(self_heal._unfinished(self.state(), agent))

    def test_a_new_structure_revision_is_watched_again(self):
        self.stop_part_one()
        with board.locked_state(self.root) as state:
            state["task_chunks"]["TASK-R"]["part-2"]["status"] = "passed"
            state["delivery_plans"]["TASK-R"]["structure_revision"] = int(state["delivery_plans"]["TASK-R"].get("structure_revision", 0)) + 1
        agent = next(a for a in self.state()["agents"].values() if a["id"] == self.dev["id"])
        self.assertTrue(board._agent_has_actionable_work(self.state(), agent), "a changed structure is new work")


class RecoveryModel(unittest.TestCase):
    """The owner chooses the model of the one recovery attempt upfront in Settings; nothing is asked when it runs."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)

    def settings(self):
        return control.default_agent_settings()

    def test_the_saved_vendor_model_is_used_silently_and_an_unset_field_uses_the_default(self):
        global_settings.initialize(self.home)
        saved = global_settings.load(self.home)["recovery_models"]
        self.assertEqual(saved, {"claude": "", "codex": "", "gemini": ""}, "unset by default")
        unset = control.recovery_launch_choice(self.settings(), saved)
        self.assertEqual(unset["model"], control.STRONGEST_MODELS["codex"])
        global_settings.update_agent_settings(self.home, self.settings(), {"codex": "gpt-6-sol", "claude": "", "gemini": ""})
        configured = control.recovery_launch_choice(self.settings(), global_settings.load(self.home)["recovery_models"])
        self.assertEqual((configured["provider"], configured["model"]), ("codex", "gpt-6-sol"))
        self.assertIn(configured["effort"], ("high", "xhigh", "max", "ultra"))
        import inspect
        source = inspect.getsource(control.recovery_launch_choice) + inspect.getsource(self_heal._execute_recovery)
        for forbidden in ("owner_action", "ask_owner", "record_agent_question", "input("):
            self.assertNotIn(forbidden, source, "no dialog, card or question at recovery time")

    def test_an_older_settings_document_without_the_field_loads_unchanged_and_defaults_are_in_the_catalog(self):
        path = self.home / global_settings.SETTINGS_FILENAME
        document = global_settings.initialize(self.home)
        document.pop("recovery_models")
        path.write_text(json.dumps(document), encoding="utf-8")
        self.assertEqual(global_settings.load(self.home)["recovery_models"], {"claude": "", "codex": "", "gemini": ""})
        for provider, model in control.STRONGEST_MODELS.items():
            self.assertIn(model, control.PROVIDER_MODELS[provider])
        with self.assertRaises(ValueError):
            control.validated_recovery_models({"gemini": "gpt-6-sol"})


for _cls in (Rounds, Escalation, RetainedPlan, StoppedRecovery, TwoChunks):
    for _name in [n for n in dir(base.RepairContextTests) if n.startswith("test_") and n not in _cls.__dict__]:
        setattr(_cls, _name, None)
# Escalation extends Rounds: its own subclass of the round tests would repeat them, so only its own tests run there.
for _cls in (Escalation, RetainedPlan, StoppedRecovery, TwoChunks):
    for _name in [n for n in dir(Rounds) if n.startswith("test_") and n in Rounds.__dict__ and n not in _cls.__dict__]:
        setattr(_cls, _name, None)
for _cls in (StoppedRecovery, TwoChunks):
    for _name in [n for n in Escalation.__dict__ if n.startswith("test_") and n not in _cls.__dict__]:
        setattr(_cls, _name, None)


if __name__ == "__main__":
    unittest.main()
