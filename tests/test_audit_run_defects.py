# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Defects found by the second Acremuse audit run (task 2026-10-27_audit-run-defects)."""
from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from harness import board, contract_check, control, cost_center, self_heal, standing_checks
from tests.test_cost_center import CLAUDE_ID, CLAUDE_ID_2, Fixture, T0
from tests.test_no_silent_stall import _Stall
from tests.test_self_restart import _ago

ROOT = Path(__file__).resolve().parents[1]


def text(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


class CleanAgentEnvironmentTests(unittest.TestCase):
    """Defect 1: a parent Claude Code session's variables made interactive agents write no transcript (measured with the real CLI)."""

    def scrubbed_env(self, extra: dict[str, str]) -> dict[str, str]:
        script = text("scripts/run_managed_agent.sh")
        start = script.index("for ambient_name in ${!CLAUDE@}")
        end = script.index("unset CLAUDECODE\n", start) + len("unset CLAUDECODE\n")
        env = {"PATH": os.environ["PATH"], **extra}
        out = subprocess.run(["bash", "-c", script[start:end] + "\nenv"], env=env, capture_output=True, text=True, check=True).stdout
        return dict(line.split("=", 1) for line in out.splitlines() if "=" in line)

    def test_every_parent_session_variable_is_dropped_and_only_the_owners_config_dir_stays(self) -> None:
        parent = {"CLAUDE_CODE_SESSION_ID": "x", "CLAUDE_CODE_CHILD_SESSION": "1", "CLAUDE_CODE_HOST_SESSION_ID": "h", "CLAUDECODE": "1",
                  "CLAUDE_CODE_ENTRYPOINT": "claude-desktop", "CLAUDE_CODE_SIMPLE": "1", "CLAUDE_AGENT_SDK_VERSION": "9", "CLAUDE_PID": "4",
                  "CLAUDE_CODE_MESSAGING_SOCKET": "/s", "CLAUDE_EFFORT": "high", "CLAUDE_CONFIG_DIR": "/owner/claude"}
        seen = self.scrubbed_env(parent)
        self.assertEqual({k for k in seen if k.startswith("CLAUDE")}, {"CLAUDE_CONFIG_DIR"})
        self.assertEqual(seen["CLAUDE_CONFIG_DIR"], "/owner/claude")

    def test_the_scrub_runs_before_anything_the_launcher_sets_on_purpose(self) -> None:
        script = text("scripts/run_managed_agent.sh")
        scrub = script.index("for ambient_name in ${!CLAUDE@}")
        for deliberate in ("export CLAUDE_CODE_SANDBOXED=1", "export CLAUDE_CODE_DISABLE_MOUSE=1"):
            self.assertGreater(script.index(deliberate), scrub, deliberate)


class ProjectRecordCostTests(unittest.TestCase):
    """Defect 1: a Claude session with no transcript file is counted from Claude's own project record, labelled, never as $0."""

    def setUp(self) -> None:
        cost_center.configure(None)
        self.fx = Fixture(self)

    def record(self, session_id: str, folder: str = "/p", **tokens: int) -> None:
        usage = {"inputTokens": 1000, "outputTokens": 500, "cacheReadInputTokens": 2000, "cacheCreationInputTokens": 300, **tokens}
        path = self.fx.root / ".claude.json"
        data = json.loads(path.read_text()) if path.exists() else {"projects": {}}
        data["projects"][folder] = {"lastSessionId": session_id, "lastCost": 9.99, "lastModelUsage": {"claude-sonnet-5-5": usage}}
        path.write_text(json.dumps(data))
        self.fx.claude_store.mkdir(parents=True, exist_ok=True)

    def test_a_session_without_a_file_is_counted_from_the_project_record_and_labelled(self) -> None:
        self.record(CLAUDE_ID)
        self.fx.collect([self.fx.claude_obs("task-a")])
        summary = self.fx.summary()
        row = summary["tasks"]["task-a"]
        self.assertAlmostEqual(row["usd"], (1000 * 2.0 + 500 * 10.0 + 2000 * 0.1 + 300 * 2.5) / 1e6, places=9)   # priced from tokens, not lastCost
        self.assertIn(cost_center.PROJECT_RECORD_WORDS, row["problems"])
        self.assertIn("project record", cost_center.PROJECT_RECORD_WORDS)

    def test_the_ledger_counts_both_claude_sessions_builder_and_cto(self) -> None:
        self.record(CLAUDE_ID, "/p/builder")
        self.record(CLAUDE_ID_2, "/p/cto", inputTokens=400, outputTokens=100, cacheReadInputTokens=0, cacheCreationInputTokens=0)
        self.fx.collect([self.fx.claude_obs("task-a"), self.fx.claude_obs("task-a", role="cto", managed="m-cto", vendor_id=CLAUDE_ID_2)])
        summary = self.fx.summary()
        self.assertEqual(summary["total"]["counted"], 2)
        self.assertTrue(summary["overhead"]["counted"] and summary["tasks"]["task-a"]["counted"])   # the CTO is overhead, the builder is the task
        self.assertGreater(summary["total"]["usd"], 0)

    def test_nothing_found_stays_not_measured_and_a_late_file_replaces_the_record_without_double_counting(self) -> None:
        self.fx.collect([self.fx.claude_obs("task-a")])
        unmeasured = self.fx.summary()["tasks"]["task-a"]
        self.assertEqual(unmeasured["counted"], 0)   # the screens print "not measured", never $0.00
        self.assertEqual(cost_center._amount(unmeasured), "not measured")
        self.record(CLAUDE_ID)
        self.fx.collect([self.fx.claude_obs("task-a")], minutes=11)
        self.fx.claude_file().write_text(
            json.dumps({"type": "assistant", "timestamp": "2026-10-27T09:12:00Z", "message": {"id": "m1", "model": "claude-sonnet-5-5", "usage": {
                "input_tokens": 10, "output_tokens": 0, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}}}) + "\n", encoding="utf-8")
        self.fx.collect([self.fx.claude_obs("task-a")], minutes=12)
        self.assertEqual(self.fx.summary()["tasks"]["task-a"]["tokens"], 10)


class BusyAgentIsNotSilentTests(_Stall):
    """Defect 2: no "has not checked the board" warning while the terminal is alive and printing."""

    def stalled(self, printed_seconds_ago, silence=None):
        session, agent, process = self.delivery_with_task("BUSY-TASK")
        self.silent(agent["id"], silence if silence is not None else self.window + 60)
        with control.locked_state(self.root) as state:
            state["sessions"][session["id"]]["last_output_at"] = _ago(printed_seconds_ago)
        return session, agent

    def plan(self):
        return [a["action"] for a in self_heal.plan(self.root, self.at(0)) if str(a["action"]).startswith("stall_")]

    def test_a_terminal_that_is_printing_gets_no_warning(self) -> None:
        self.stalled(5)
        self.assertEqual(self.plan(), [])
        self.assertEqual(self.events("agent_stall_step"), [])

    def test_a_terminal_that_printed_nothing_for_minutes_is_still_woken(self) -> None:
        self.stalled(self_heal.STALL_BUSY_OUTPUT_SECONDS + 60)
        self.assertEqual(self.plan(), ["stall_wake"])

    def test_busy_never_hides_a_real_stall_forever(self) -> None:
        self.stalled(5, silence=self_heal.STALL_BUSY_MAX_SECONDS + 60)
        self.assertEqual(self.plan(), ["stall_wake"])


class StandingChecksGateTests(unittest.TestCase):
    """Defects 3, 4, 6, 7: the rule text exists for both roles and the one gate sends incomplete work back."""

    def setUp(self) -> None:
        patcher = mock.patch.dict(os.environ, {"HARNESS_STANDING_CHECKS": "on"})
        patcher.start()
        self.addCleanup(patcher.stop)

    GOOD_HAND_IN = "Walked the path.\nSecurity: checked sign-in and sessions, who sees whose data, rate limits, secrets, input checks.\nCode quality: small named functions, no copies, settings used."

    def test_the_rules_are_in_the_chapter_and_on_both_cards(self) -> None:
        agent = text("directives/AGENT.md")
        chapter = " ".join(agent[agent.index("## standing-checks"):agent.index("## error-sink")].split())
        for needle in ("do a security check of what you touched or audited and say what you checked", "readable and maintainable", "`Security:`",
                       "`Code quality:`", "`Security check:`", "--code-quality", "checked, nothing to note", "Spec tasks", "`Not examined:`",
                       "temporary evidence folder", "against the code yourself", "it may say nothing",
                       "same checks as building", "no lighter rulebook"):
            self.assertIn(needle.lower() if needle.startswith("do a") else needle, chapter.lower() if needle.startswith("do a") else chapter)
        for removed in ("authentication", "authorization", "rate limiting", "entitlement", "billing", "background-job", "isolation", "spot-check",
                        "thin", "unexamined", "enough time", "not a review"):
            for name in ("directives/cards/DELIVERY.md", "directives/cards/REVIEWER.md"):
                if removed in ("authentication", "authorization", "rate limiting", "entitlement", "background-job", "isolation") and name.endswith("DELIVERY.md"):
                    continue   # rule 9 of the Delivery card still names the older security-baseline items
                self.assertIsNone(re.search(rf"\b{re.escape(removed)}", text(name).lower()), (removed, name))
            if removed != "authorization":   # the approved Code analysis checklist (315393a) names it in the chapter
                self.assertIsNone(re.search(rf"\b{re.escape(removed)}\b", chapter.lower()), removed)   # whole words: "things" is not "thin"
        self.assertIn("standing-checks", text("directives/cards/DELIVERY.md"))
        self.assertIn("`Code quality:`", text("directives/cards/DELIVERY.md"))
        reviewer = text("directives/cards/REVIEWER.md")
        for needle in ("standing-checks", "`Security check:`", "--code-quality", "`Not examined:`", "In a Code analysis", "analysis-crosscheck", "Clean list items in the report:"):
            self.assertIn(needle, reviewer)

    def test_requirements_without_a_security_line_are_sent_back(self) -> None:
        plain = "In plain words:\nA report of the problems.\nNothing for you to do.\n\nFind problems.\nNon-functional: none"
        with self.assertRaisesRegex(ValueError, "Security:"):
            board.record_requirement_proposal(Path("/nonexistent"), "agent", plain, plain_required=True)
        self.assertEqual(standing_checks.requirements_problem(plain + "\nSecurity: the report is read-only and touches no live data."), "")

    def test_a_hand_in_needs_both_sections_at_final_acceptance_only(self) -> None:
        self.assertEqual(standing_checks.hand_in_problem(self.GOOD_HAND_IN), "")
        self.assertIn("\"Security:\"", standing_checks.hand_in_problem("Code quality: tidy and small functions."))
        self.assertIn("\"Code quality:\"", standing_checks.hand_in_problem("Security: checked sessions and isolation."))
        with self.assertRaisesRegex(ValueError, "hand-in is incomplete"):
            board.request_review(Path("/nonexistent"), "agent", "ledger.md", "Done, all tests pass.", phase="final_acceptance")
        with self.assertRaises(Exception) as chunk:   # a chunk review is not gated: it fails later, for another reason
            board.request_review(Path("/nonexistent"), "agent", "ledger.md", "Done.", phase="chunk")
        self.assertNotIn("hand-in is incomplete", str(chunk.exception))

    def test_a_pass_needs_a_security_check_and_a_code_quality_line(self) -> None:
        said = "TASK DONE: YES\nSecurity check: read the auth and isolation code and ran two probes; nothing found."
        self.assertIn("Security check", standing_checks.verdict_problem("TASK DONE: YES", True, False, False))
        self.assertIn("--code-quality", standing_checks.verdict_problem(said, False, False, False))
        self.assertEqual(standing_checks.verdict_problem(said, True, False, False), "")
        self.assertEqual(standing_checks.verdict_problem(said, False, True, False), "", "notes stored by an earlier round count")

    def test_a_report_only_pass_must_name_what_was_not_examined(self) -> None:
        said = "Security check: read the changed code and ran two probes; found nothing new."
        self.assertIn("Not examined", standing_checks.verdict_problem(said, True, False, True))
        self.assertEqual(standing_checks.verdict_problem(said + "\nFindings verified independently: 3 of 3\nNot examined: the mobile client and the deploy scripts.", True, False, True), "")
        self.assertEqual(standing_checks.verdict_problem(said, True, False, False), "", "a build task is not asked for it")
        # security and code quality are asked of an analysis exactly as of a build: only "Not examined" differs
        self.assertIn("Security check", standing_checks.verdict_problem("Not examined: nothing", True, False, True))
        self.assertIn("--code-quality", standing_checks.verdict_problem(said + "\nFindings verified independently: 2 of 2\nNot examined: nothing", False, False, True))
        self.assertEqual(standing_checks.verdict_problem(said + "\nFindings verified independently: 2 of 2\nNot examined: nothing", True, False, True), "", "it may say nothing")
        for name in ("AREAS", "REPORT_SPOT_CHECK"):
            self.assertFalse(hasattr(standing_checks, name), "no prescribed areas")

    def test_a_report_only_task_adds_only_the_report_to_the_project(self) -> None:
        extra = standing_checks.report_only_extra_files(
            ["FINDINGS.md", "audit_evidence/test_findings_grounding.py", ".harness/x"], ["/work/proj/FINDINGS.md", "FINDINGS.md"])
        self.assertEqual(extra, ["audit_evidence/test_findings_grounding.py"])
        self.assertEqual(standing_checks.report_only_extra_files(["FINDINGS.md"], ["FINDINGS.md"]), [])
        self.assertIn("temporary evidence folder", standing_checks.report_only_files_problem(extra))

    def test_the_gate_can_be_switched_off_for_fixtures(self) -> None:
        with mock.patch.dict(os.environ, {"HARNESS_STANDING_CHECKS": "off"}):
            self.assertEqual(standing_checks.hand_in_problem("nothing"), "")
            self.assertEqual(standing_checks.verdict_problem("nothing", False, False, True), "")

    def test_the_history_view_always_shows_a_code_quality_line(self) -> None:
        viewer = text("harness/board_viewer.py")
        block = viewer[viewer.index("function reviewNotesHtml"):viewer.index("function historyResultLabel")]
        self.assertNotIn("&&!quality.length)return''", block)
        self.assertIn("no code-quality check was recorded for this task", block)


class ReportContractTests(unittest.TestCase):
    """Defect 5: a document task's contract is the report's required content, not "nothing to check"."""

    def test_the_reason_states_the_required_content(self) -> None:
        reason = contract_check.no_program_reason("Hand in one report. For each one, say what is wrong, where it is, and why it is a problem.")
        self.assertIn("Required content: For each one, say what is wrong, where it is, and why it is a problem", reason)
        self.assertNotIn("nothing to check in advance", reason)
        self.assertIn("contract is what the report must contain", contract_check.no_program_reason("a report"))


class WorkingAgentIsNeverCalledSilentTests(_Stall):
    """Re-run defect: the watchdog woke a working agent first, which switched the ladder's busy exemption off and produced
    "has not checked the board ... it had already been woken once and did not answer" while the builder was reading files."""

    def reading_agent(self, transcript_age=5.0, printed_ago=900, silence=None):
        session, agent, _process = self.delivery_with_task("READING-TASK")
        store = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(store, ignore_errors=True))
        vendor_id = "0199aaaa-bbbb-cccc-dddd-eeeeffff0001"
        rollout = store / "sessions" / "2026" / "10" / "27" / f"rollout-2026-10-27T18-00-00-{vendor_id}.jsonl"
        rollout.parent.mkdir(parents=True)
        rollout.write_text("{}\n", encoding="utf-8")
        stamp = datetime.now().timestamp() - transcript_age
        os.utime(rollout, (stamp, stamp))
        self.rollout = rollout
        with control.locked_state(self.root) as state:
            state["sessions"][session["id"]].update({
                "last_output_at": _ago(printed_ago), "cli_session_provider": "codex", "cli_session_id": vendor_id, "cli_session_store": str(store)})
        self.silent(agent["id"], silence if silence is not None else self.window + 60)
        return session, agent

    def plan(self):
        return [a["action"] for a in self_heal.plan(self.root, self.at(0)) if str(a["action"]).startswith("stall_")]

    def woken(self, agent_id):
        return board.snapshot(self.root)["agents"][agent_id].get("automatic_recovery_requested_at")

    def test_alive_and_reading_for_over_four_minutes_without_printing_is_never_woken_or_warned(self) -> None:
        session, agent = self.reading_agent()
        for _ in range(3):
            board.mark_stalled(self.root)
        self.assertFalse(self.woken(agent["id"]), "the watchdog must not wake a working agent")
        self.assertEqual([i for i in control.take_instructions(self.root, session["id"]) if i.get("source") == "stall-ladder"], [])
        self.assertEqual(self.plan(), [])
        self.assertEqual(self.events("agent_stall_step"), [])
        self.assertEqual(self.events("agent_stalled"), [])

    def test_an_unanswered_earlier_wake_does_not_make_a_working_agent_unresponsive(self) -> None:
        _session, agent = self.reading_agent()
        with board.locked_state(self.root) as state:
            state["agents"][agent["id"]].update({"recovery_state": "automatic_requested", "automatic_recovery_requested_at": _ago(self.window - 5)})
        self.assertEqual(self.plan(), [])

    def test_a_terminal_that_stopped_printing_and_stopped_writing_is_still_woken(self) -> None:
        _session, agent = self.reading_agent(transcript_age=3600)
        board.mark_stalled(self.root)
        self.assertTrue(self.woken(agent["id"]))

    def test_the_exemption_is_bounded(self) -> None:
        _session, agent = self.reading_agent(silence=self_heal.STALL_BUSY_MAX_SECONDS + 60)
        board.mark_stalled(self.root)
        self.assertTrue(self.woken(agent["id"]))

    def test_a_working_agent_in_a_running_ladder_is_not_restarted(self) -> None:
        session, agent = self.reading_agent(transcript_age=3600)
        with board.locked_state(self.root) as state:
            state.setdefault("stall_ladder", {})[f"{agent['role']}:{agent['task']}"] = {
                "since": _ago(self.window + 50), "started_at": _ago(self.step + 120), "step": 1, "step_at": _ago(self.step + 60), "card": False,
                "agent_id": agent["id"], "kind": "codex_delivery", "session_id": session["id"], "steps": ["woke it"]}
        self.assertEqual(self.plan(), ["stall_relaunch"], "a silent terminal climbs as before")
        with control.locked_state(self.root) as state:
            state["sessions"][session["id"]]["last_output_at"] = _ago(3)
        self.assertEqual(self.plan(), ["stall_relaunch"], "after an unanswered wake, printing alone proves nothing")
        os.utime(self.rollout, None)       # its session file moves: real messages and tool calls
        self.assertEqual(self.plan(), [])

    def test_printing_alone_exempts_a_first_silence_but_not_an_unanswered_wake(self) -> None:
        _session, agent = self.reading_agent(transcript_age=3600, printed_ago=5)
        board.mark_stalled(self.root)
        self.assertFalse(self.woken(agent["id"]), "a printing terminal is not woken the first time")
        self.assertEqual(self.plan(), [])


class ReportOnlyContractStepTests(unittest.TestCase):
    """Re-run defect: the contract step refused "the correction needs at least one check" until the task was put on hold at Go ahead."""

    def setUp(self) -> None:
        from tests.test_contract_first_checks import _Fixture
        self.fx = _Fixture("setUp")
        self.fx.setUp()
        self.addCleanup(self.fx.doCleanups)

    def answer_empty(self, reviewer, number, name):
        from tests.test_contract_first_checks import write_json
        path = write_json(self.fx.base, name, {"checks": []})
        return board.record_contract_check_correction(self.fx.root, reviewer["id"], "TASK-CONTRACT", number, "nothing to add here", path)

    def test_a_report_with_no_program_to_check_gets_no_coverage_request_and_no_refusal_after_go_ahead(self) -> None:
        from tests.test_coverage_check import criteria
        agent = self.fx.delivery()
        session, reviewer = self.fx.reviewer()
        board.record_requirement_proposal(self.fx.root, agent["id"], "Hand in one report. For each one, say what is wrong and where.")
        control.take_instructions(self.fx.root, session["id"])
        self.fx.submit(reviewer, criteria((False, False), (False, False)))
        asked = [i for i in control.take_instructions(self.fx.root, session["id"]) if "COVERAGE" in i["text"]]
        self.assertEqual(asked, [], "nothing to map, so the Reviewer is not asked to close gaps")
        board.record_requirements_decision(self.fx.root, "TASK-CONTRACT", "go_ahead", version=1, token=self.fx.proposal()["contract_check"]["token"])
        for attempt in range(7):                  # even a Reviewer that keeps answering is never refused
            self.assertTrue(self.answer_empty(reviewer, 1 + attempt % 2, f"e{attempt}.json")["unchanged"])
        self.assertEqual(board.snapshot(self.fx.root).get("control_plane_holds") or {}, {})

    def test_an_empty_answer_to_a_coverage_request_is_accepted_once_and_never_refused(self) -> None:
        from tests.test_coverage_check import criteria
        agent = self.fx.delivery()
        session, reviewer = self.fx.reviewer()
        board.record_requirement_proposal(self.fx.root, agent["id"], "Final agreed requirements: a command prints hello.")
        control.take_instructions(self.fx.root, session["id"])
        self.fx.submit(reviewer, criteria((True, True), (False, False)))      # requirement 2 cannot be tested: a gap is asked ONCE
        asked = [i for i in control.take_instructions(self.fx.root, session["id"]) if "COVERAGE" in i["text"]]
        self.assertEqual(len(asked), 1)
        self.assertEqual(self.answer_empty(reviewer, 2, "e.json")["criterion"], 2)
        self.assertEqual(self.fx.proposal()["contract_check"]["coverage_requested"], [])
        self.assertEqual(self.answer_empty(reviewer, 2, "e2.json")["checks"], 0)     # a repeat is a no-op, not a refusal

    def test_a_no_program_task_accepts_a_correction_with_nothing_to_change(self) -> None:
        agent = self.fx.delivery()
        _session, reviewer = self.fx.reviewer()
        with board.locked_state(self.fx.root) as state:
            state.setdefault("task_document_only", {})["TASK-CONTRACT"] = True      # the owner's checkbox (D-014)
        board.record_requirement_proposal(self.fx.root, agent["id"], "Hand in one report. For each one, say what is wrong and where.", no_program=True)
        board.record_requirements_decision(self.fx.root, "TASK-CONTRACT", "go_ahead")
        self.assertEqual(self.answer_empty(reviewer, 1, "n.json"), {"criterion": 1, "checks": 0, "unchanged": True})



if __name__ == "__main__":
    unittest.main()
