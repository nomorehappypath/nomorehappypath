# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""harness/story.py (task 2026-10-27_mission-control-redesign, slice 1): the board's state in, one plain story out.

Pure table tests: each fixture is a small board state; no files, no network, no browser. The scenarios are the spec's (section 12):
gate status to step, the runtime cases of the Accept rule, the exception priority list, the project card, the cost wording and the agent
strip with its fallbacks.

Run:  PYTHONPATH=. python3 -m unittest tests.test_story -v
"""
from __future__ import annotations

import unittest

from harness import story

TASK = "WEATHER-APP"
SUMMARY = {"available": True, "as_of": "2026-10-27", "card_note": "", "counted_through": "", "gemini": False,
           "tasks": {TASK: {"usd": 2.8, "tokens": 410_000, "estimated": False, "problems": [], "gemini": [], "codex_limit": {"reset": False, "minutes": 300, "increase": 14.0},
                            "roles": {"delivery": {"usd": 1.9, "tokens": 300_000}, "reviewer": {"usd": 0.9, "tokens": 110_000}}}},
           "overhead": {"usd": 0.1, "tokens": 20_000, "estimated": False, "roles": {"cto": {"usd": 0.1, "tokens": 20_000}}},
           "unallocated": {"usd": 0, "tokens": 0, "estimated": False, "roles": {}}, "total": {"usd": 2.9, "tokens": 430_000, "estimated": False, "roles": {}}}


def builder(task=TASK, **extra):
    return {"id": "dev-1", "role": "engineering", "vendor": "OpenAI", "task": task, "active": True, "status": "working", "session_id": "s-dev", **extra}


def reviewer(**extra):
    return {"id": "qa-1", "role": "qa", "vendor": "Anthropic", "task": "REVIEW_QUEUE", "active": True, "status": "working", "session_id": "s-qa", **extra}


def cto(**extra):
    return {"id": "cto-1", "role": "cto", "vendor": "Anthropic", "task": "GLOBAL_MONITOR", "active": True, "status": "working", "session_id": "s-cto", **extra}


def state(*, agents=None, confirmed=True, requests=None, **extra):
    out = {"agents": {a["id"]: a for a in (agents if agents is not None else [builder()])},
           "requirement_confirmations": {TASK: {"text": "agreed"}} if confirmed else {},
           "qa_requests": {r["id"]: r for r in (requests or [])}, "events": [{"kind": "task_begun", "task": TASK, "at": "2026-10-27T10:00:00+00:00"}]}
    out.update(extra)
    return out


def request(rid, status, **extra):
    return {"id": rid, "task": TASK, "status": status, "phase": "chunk", "stage": "independent_review", "cycle": 1, "subtask": "a", "chunk": "a1",
            "structure_revision": 0, "requested_at": f"2026-10-27T10:0{extra.pop('minute', 1)}:00+00:00", **extra}


def built(st, **kw):
    return story.build(st, {}, TASK, kw.pop("runtime", None), **kw)


class GateToStepTests(unittest.TestCase):
    """Spec section 3: every gate status lands on the step the table names, and exactly one step is Now."""

    def test_requirements_stage_walks_steps_2_to_6(self):
        none_yet = built(state(confirmed=False))
        self.assertEqual((none_yet["gate"], none_yet["step"], none_yet["step_title"]), ("AWAITING FINAL REQUIREMENTS", 2, "Requirements drafted"))
        self.assertFalse(none_yet["needs_owner"])
        reading = built(state(confirmed=False, requirement_proposals={TASK: {"status": "awaiting_owner", "contract_check": {"status": "reading"}}}))
        self.assertEqual(reading["step"], 3)
        asking = built(state(confirmed=False, requirement_proposals={TASK: {"status": "awaiting_owner", "contract_check": {"status": "questions"}}}))
        self.assertEqual((asking["step"], asking["needs_owner"]), (5, True))
        self.assertIn("Answer", asking["owner_action"])
        ready = built(state(confirmed=False, requirement_proposals={TASK: {"status": "awaiting_owner", "contract_check": {"status": "ready"}}}))
        self.assertEqual((ready["step"], ready["needs_owner"]), (6, True))
        self.assertIn("Go ahead", ready["owner_action"])

    def test_building_is_step_12_then_13_and_names_the_work_area(self):
        self.assertEqual(built(state())["step"], 12)
        planned = built(state(delivery_plans={TASK: {"mode": "application", "subtasks": {"a": {"status": "passed"}, "b": {"status": "working", "chunks": {}}}}}))
        self.assertEqual((planned["gate"], planned["step"]), ("DEVELOPMENT IN PROGRESS", 13))
        self.assertIn("work area 2 of 2", planned["now"])

    def test_tests_first_is_the_first_unmet_of_7_to_11(self):
        reserved = request("r1", "reserved", delivery_state="executing")
        self.assertEqual(built(state(requests=[reserved]))["step"], 7)
        checks = {"checks": [{"n": "1.1", "criterion_index": 1}], "compare": {"1": "fails_before"}, "checks_sha256": "abc", "red_proof": {"status": "pending"}}
        confirmation = {TASK: {"text": "agreed", "contract_check": checks}}
        self.assertEqual(built(state(requests=[reserved], requirement_confirmations=confirmation))["step"], 10)
        checks["red_proof"] = {"status": "settled"}
        self.assertEqual(built(state(requests=[reserved], requirement_confirmations=confirmation))["step"], 14)

    def test_review_steps(self):
        self.assertEqual(built(state(requests=[request("r1", "authoring")]))["step"], 14)
        self.assertEqual(built(state(requests=[request("r1", "authoring", delivery_state="passed", delivery_evidence="e")]))["step"], 15)
        self.assertEqual(built(state(requests=[request("r1", "reserved")]))["step"], 16)
        picking = built(state(requests=[request("r1", "open")]))
        self.assertEqual(picking["step"], 16)
        self.assertIn("waiting for the independent reviewer", picking["now"])
        self.assertEqual(built(state(requests=[request("r1", "claimed")]))["step"], 17)

    def test_round_two_and_the_last_retry_follow_the_lineage_round(self):
        findings = [{"id": "B1", "summary": "x"}, {"id": "B2", "summary": "y"}]
        failed1 = request("r1", "failed", cycle=1, review_round=1, structured_findings=findings, minute=1, completed_at="2026-10-27T10:02:00+00:00")
        second = built(state(requests=[failed1, request("r2", "claimed", cycle=2, minute=3)]))
        self.assertEqual((second["step"], second["step_title"]), (20, "Review round 2"))
        failed2 = request("r2", "failed", cycle=2, review_round=2, structured_findings=findings, minute=3, completed_at="2026-10-27T10:04:00+00:00")
        last = built(state(requests=[failed1, failed2, request("r3", "claimed", cycle=3, minute=5)]))
        self.assertEqual((last["step"], last["step_title"]), (21, "Last retry"))

    def test_a_failed_review_is_step_19_and_counts_what_the_reviewer_found(self):
        failed = request("r1", "failed", review_round=1, structured_findings=[{"id": "B1"}, {"id": "B2"}], completed_at="2026-10-27T10:02:00+00:00")
        result = built(state(requests=[failed]))
        self.assertEqual((result["gate"], result["step"], result["step_title"]), ("REPAIR IN PROGRESS", 19, "Repair"))
        self.assertIn("2 problems", result["now"])
        self.assertFalse(result["needs_owner"])

    def test_final_review_and_release_checks(self):
        final = request("r9", "claimed", phase="final_acceptance")
        self.assertEqual(built(state(requests=[final]))["step"], 22)
        passed = request("r9", "passed", phase="final_acceptance", completed_at="2026-10-27T11:00:00+00:00")
        self.assertEqual((built(state(requests=[passed]))["gate"], built(state(requests=[passed]))["step"]), ("FINAL RELEASE CHECKS", 23))

    def test_ready_for_your_test_is_step_25_with_the_one_owner_action(self):
        passed = request("r9", "passed", phase="final_acceptance", claimed_by="qa-1")
        ready = built(state(agents=[builder(), reviewer()], requests=[passed], releases={TASK: {"status": "VISUAL_TEST_REQUIRED", "head_commit": "abc"}}))
        self.assertEqual((ready["gate"], ready["step"], ready["needs_owner"]), ("READY FOR YOUR TEST", 25, True))
        self.assertIn("Accept", ready["owner_action"])

    def test_owner_not_accepted_returns_to_step_19_in_plain_words(self):
        rejected = built(state(releases={TASK: {"status": "VISUAL_TEST_REQUIRED"}}, release_decisions={TASK: {"decision": "not_accepted"}}))
        self.assertEqual((rejected["gate"], rejected["step"]), ("OWNER REJECTED / REPAIR REQUIRED", 19))
        self.assertIn("Repairing after your feedback", rejected["now"])
        self.assertFalse(rejected["needs_owner"])
        self.assertFalse(rejected["accepted"])

    def test_step_26_is_done_only_when_the_merge_is_recorded(self):
        saved_only = built(state(release_decisions={TASK: {"decision": "accepted", "git_acceptance": {"status": "failed"}}}))
        self.assertEqual((saved_only["gate"], saved_only["step"], saved_only["needs_owner"], saved_only["accepted"]), ("ACCEPT SAVED, NOT IN MAIN YET", 26, True, False))
        self.assertIn("not in main yet", saved_only["now"])
        self.assertIn("Try Accept again", saved_only["owner_action"])
        merged = built(state(release_decisions={TASK: {"decision": "accepted"}}, git_acceptances={TASK: {"commit": "abc"}}))
        self.assertTrue(merged["accepted"])
        self.assertEqual(merged["now"], "Accepted. It is on your project's main.")
        ungoverned = built(state(release_decisions={TASK: {"decision": "accepted"}}))
        self.assertEqual(ungoverned["now"], "Accepted.")
        self.assertNotIn("main", ungoverned["now"], "a task without governed Git is never promised to be in main")

    def test_reintegration_keeps_the_step_and_adds_one_plain_line(self):
        st = state(requests=[request("r1", "claimed")], git_reintegration_required={TASK: {"owner_line": "Other approved work changed the app. Nothing for you to do."}})
        result = built(st)
        self.assertEqual(result["gate"], "RE-INTEGRATION NEEDED")
        self.assertEqual(result["step"], 17, "the step is where the work really is")
        self.assertIn("Other approved work changed the app", result["next"])

    def test_every_state_has_exactly_one_step_and_never_an_empty_page(self):
        for st in (state(), state(agents=[]), state(confirmed=False, agents=[]), state(requests=[request("r1", "suspended")])):
            result = built(st)
            self.assertTrue(1 <= result["step"] <= 26, result)
            self.assertTrue(result["now"], result)
            self.assertEqual(result["of"], 26)


class RuntimeAcceptRuleTests(unittest.TestCase):
    """Spec 12.2: Ready/Accept is offered only when the ported gate allows it, including the external-target exception."""

    def release(self, **extra):
        return {TASK: {"status": "VISUAL_TEST_REQUIRED", "head_commit": "abc123", "checks": {"deployed_runtime_verified": True}, **extra}}

    def gate_for(self, runtime, **extra):
        return built(state(releases=self.release(**extra)), runtime=runtime)

    def test_matching_commit_is_ready(self):
        self.assertEqual(self.gate_for({"commit": "abc123", "managed": True})["gate"], "READY FOR YOUR TEST")

    def test_mismatching_commit_switches_accept_off(self):
        result = self.gate_for({"commit": "zzz999", "managed": True})
        self.assertEqual(result["gate"], "DEPLOYMENT REFRESH REQUIRED")
        self.assertFalse(result["needs_owner"], "no Accept is offered while the running version is not the reviewed one")
        self.assertEqual(result["step"], 23)

    def test_unknown_commit_switches_accept_off(self):
        self.assertEqual(self.gate_for({"commit": "", "managed": True})["gate"], "DEPLOYMENT REFRESH REQUIRED")

    def test_external_target_keeps_accept_on(self):
        self.assertEqual(self.gate_for({"commit": "zzz999", "managed": True}, runtime_verification_deferred_to_target_acceptance=True)["gate"], "READY FOR YOUR TEST")

    def test_the_manager_has_no_runtime_and_never_blocks_but_also_never_offers_accept_itself(self):
        result = self.gate_for(None)
        self.assertEqual(result["gate"], "READY FOR YOUR TEST")
        now = story.project_now(state(releases=self.release()), TASK, runtime=None)
        self.assertEqual(now["needs_you"]["kind"], "ready")
        self.assertIn("Accept", now["needs_you"]["action"], "the card only links to the project; the Accept button lives on the task page")


class ExceptionPriorityTests(unittest.TestCase):
    """Spec 12.6: an exception outranks progress, names the reason, the role and the ONE action, and a fresh builder note never hides it."""

    def exception(self, **extra):
        st = state(agents=extra.pop("agents", None), **extra)
        sessions = extra.pop("sessions", None)
        return built(st, sessions=sessions)

    def test_reviewer_sign_in(self):
        result = built(state(reviewer_needed={"sign_in": True, "sign_in_how": "run /login"}))
        exc = result["exception"]
        self.assertEqual((exc["kind"], exc["role"], exc["needs_owner"]), ("sign_in", "reviewer", True))
        self.assertIn("run /login", exc["action"])
        self.assertEqual(result["now"], exc["reason"])
        self.assertTrue(result["needs_owner"])

    def test_a_signed_out_agent(self):
        result = built(state(agents=[builder(liveness="needs_sign_in", liveness_note="sign in")]))
        self.assertEqual((result["exception"]["kind"], result["exception"]["role"]), ("sign_in", "builder"))

    def test_claude_permission_consent_from_the_session_card(self):
        sessions = [{"id": "s-qa", "kind": "claude_reviewer", "claude_auth_action": {"reason": "consent", "title": "Accept the permission question", "why": "Claude asks before it runs without prompts."}}]
        result = built(state(), sessions=sessions)
        self.assertEqual((result["exception"]["kind"], result["exception"]["role"]), ("consent", "reviewer"))
        self.assertEqual(result["exception"]["action"], "Accept the permission question")

    def test_gemini_quota_needs_waiting_not_the_owner(self):
        result = built(state(agents=[builder(liveness="needs_sign_in", liveness_note="out of Gemini quota")]))
        self.assertEqual(result["exception"]["kind"], "quota")
        self.assertFalse(result["needs_owner"])

    def test_unresponsive_cto(self):
        result = built(state(agents=[builder(), cto(recovery_state="unresponsive")]))
        self.assertEqual(result["exception"]["kind"], "cto_unresponsive")
        self.assertIn("/login", result["exception"]["action"])

    def test_exhausted_repairs(self):
        result = built(state(review_escalations={"e": {"task": TASK, "status": "could_not_finish", "outcome": "Could not finish the login page; nothing of it was released."}}))
        self.assertEqual(result["exception"]["kind"], "repairs_exhausted")
        self.assertIn("nothing of it was released", result["now"])
        self.assertFalse(result["needs_owner"])

    def test_verification_gap_asks_only_when_the_owner_decides(self):
        deciding = built(state(verification_gaps={"g": {"task": TASK, "state": "owner_decision", "reason": "the VM was off"}}))
        self.assertEqual((deciding["exception"]["kind"], deciding["needs_owner"]), ("verification_gap", True))
        settling = built(state(verification_gaps={"g": {"task": TASK, "state": "diagnosing", "reason": "the VM was off"}}))
        self.assertFalse(settling["needs_owner"])

    def test_paused_and_resuming(self):
        paused = built(state(project_pause={"status": "paused"}))
        self.assertEqual(paused["exception"]["kind"], "paused")
        self.assertIn("Resume", paused["next"])
        self.assertFalse(paused["needs_owner"], "a paused board cannot take Accept")
        self.assertIn("read-only", built(state(project_pause={"status": "resuming"}))["now"])

    def test_stopped_builder_keeps_the_step_and_names_the_one_action(self):
        stopped = state(agents=[builder(active=False, status="done", stopped_by_owner_at="2026-10-27T10:30:00+00:00")], requests=[request("r1", "claimed")])
        result = built(stopped)
        self.assertEqual(result["exception"]["kind"], "stopped")
        self.assertIn("Start a new builder", result["exception"]["action"])

    def test_git_write_refusal(self):
        result = built(state(agents=[builder(broker_refusal={"reason": "the folder is protected"})]))
        self.assertEqual(result["exception"]["kind"], "git_refused")
        self.assertIn("the folder is protected", result["now"])

    def test_control_plane_hold_is_redacted_by_the_caller(self):
        result = story.build(state(control_plane_holds={"h": {"task": TASK, "status": "open", "reason": "token sk-SECRET failed"}}), {}, TASK, None,
                             redact=lambda text: str(text).replace("sk-SECRET", "[hidden]"))
        self.assertEqual(result["exception"]["kind"], "hold")
        self.assertIn("[hidden]", result["now"])
        self.assertNotIn("sk-SECRET", result["now"])

    def test_the_first_match_wins_and_a_fresh_builder_note_does_not_override_it(self):
        st = state(reviewer_needed={"sign_in": True}, control_plane_holds={"h": {"task": TASK, "status": "open", "reason": "x"}},
                   task_briefs={TASK: {"update": "Building the forecast screen now.", "updated_at": "2026-10-27T10:59:00+00:00"}})
        result = built(st)
        self.assertEqual(result["exception"]["kind"], "sign_in")
        self.assertNotIn("forecast", result["now"])
        self.assertEqual(result["builder_note"], "", "an exception hides the builder's note from the Now sentence")

    def test_an_ordinary_task_has_no_exception_and_keeps_a_fresh_note_as_text(self):
        result = built(state(task_briefs={TASK: {"update": "Fixing <img src=x> now", "updated_at": "2026-10-27T10:59:00+00:00"}}))
        self.assertIsNone(result["exception"])
        self.assertEqual(result["builder_note"], "Fixing <img src=x> now", "the story keeps raw text; the pages escape it")


class ProjectCardTests(unittest.TestCase):
    def test_an_empty_project_gets_one_clear_way_to_start(self):
        now = story.project_now({}, "")
        self.assertEqual((now["state"], now["tell_builder"], now["has_task"]), ("empty", True, False))
        self.assertEqual(now["now"], "No work has been started yet.")

    def test_a_working_project_shows_step_n_of_26_and_the_pace(self):
        now = story.project_now(state(), TASK, tasks_total=1)
        self.assertEqual((now["state"], now["step"], now["of"], now["step_title"]), ("working", 12, 26, "Plan made"))
        self.assertTrue(now["elapsed"])

    def test_a_closed_project_says_its_saved_work_is_kept_instead_of_claiming_work_is_going_on(self):
        now = story.project_now(state(agents=[]), TASK, agents=[], tasks_total=1)
        self.assertEqual(now["state"], "idle")
        self.assertIn("saved work is kept", now["now"])
        self.assertIn("open the project", now["next"])
        self.assertEqual(now["step"], 12, "the step is still read from the facts")

    def test_a_proposal_the_owner_asked_to_change_is_not_waiting_for_his_go_ahead(self):
        rewriting = built(state(confirmed=False, requirement_proposals={TASK: {"status": "modify_requested", "contract_check": {"status": "ready"}}}))
        self.assertEqual((rewriting["step"], rewriting["needs_owner"]), (2, False))

    def test_a_finished_project_says_so(self):
        now = story.project_now(state(), TASK, accepted_tasks={TASK})
        self.assertEqual((now["state"], now["step"]), ("done", 26))

    def test_ready_work_raises_the_needs_you_banner_with_the_proof_sentence(self):
        passed = request("r9", "passed", phase="final_acceptance", claimed_by="qa-1")
        st = state(agents=[builder(), reviewer()], requests=[passed], releases={TASK: {"status": "VISUAL_TEST_REQUIRED"}},
                   requirement_confirmations={TASK: {"text": "agreed", "contract_check": {"checks": [{"criterion_index": 1}, {"criterion_index": 2}]}}})
        now = story.project_now(st, TASK, awaiting_owner={TASK}, cost=SUMMARY)
        self.assertEqual((now["state"], now["needs_you"]["kind"]), ("needs_you", "ready"))
        self.assertIn("2 requirements had tests written first", now["needs_you"]["detail"])
        self.assertIn("passed the independent review by a reviewer from Anthropic", now["needs_you"]["detail"])
        self.assertIn("about $2.80 API-equivalent", now["needs_you"]["detail"])

    def test_an_agent_question_and_a_cto_card_both_need_the_owner(self):
        question = story.project_now(state(agent_questions={"q": {"status": "open", "question": "Which city by default?"}}), TASK)
        self.assertEqual(question["needs_you"]["kind"], "question")
        card = story.project_now(state(), TASK, owner_cards=[{"title": "Paste this command", "why": "the CTO cannot"}])
        self.assertEqual(card["needs_you"]["kind"], "card")

    def test_an_exception_on_the_card_outranks_progress(self):
        now = story.project_now(state(reviewer_needed={"sign_in": True}), TASK)
        self.assertEqual((now["state"], now["exception"]["kind"]), ("needs_you", "sign_in"))
        self.assertIn("run /login", now["next"])


class CostWordingTests(unittest.TestCase):
    """Never $0, never invented; 'not measured' until the cost center has counted something."""

    def test_unmeasured_is_said_so(self):
        for cost in (None, {"available": False}):
            self.assertEqual(story.money_line(cost, TASK)["text"], "Cost: not measured yet")
        self.assertEqual(story.money_line({"available": True, "tasks": {}}, TASK)["text"], "Cost: not counted yet")

    def test_a_measured_task_shows_dollars_and_the_codex_window_only_when_it_moved(self):
        line = story.money_line(SUMMARY, TASK)
        self.assertEqual((line["text"], line["window"]), ("$2.80 so far", "Codex +14% of window"))
        flat = {**SUMMARY, "tasks": {TASK: {**SUMMARY["tasks"][TASK], "codex_limit": {"reset": False, "minutes": 300, "increase": 0.0}}}}
        self.assertEqual(story.money_line(flat, TASK)["window"], "", "0% is never shown")
        reset = {**SUMMARY, "tasks": {TASK: {**SUMMARY["tasks"][TASK], "codex_limit": {"reset": True, "minutes": 300, "increase": 0.0}}}}
        self.assertIn("reset", story.money_line(reset, TASK)["window"])

    def test_a_task_with_no_counted_tokens_is_never_zero_dollars(self):
        empty = {**SUMMARY, "tasks": {TASK: {"usd": 0.0, "tokens": 0, "estimated": False, "problems": [], "gemini": [], "codex_limit": None, "roles": {}}}}
        text = story.money_line(empty, TASK)["text"]
        self.assertNotIn("$0", text)
        self.assertEqual(text, "Cost: not counted yet")

    def test_gemini_is_never_counted_and_says_so(self):
        gem = {**SUMMARY, "tasks": {TASK: {"usd": 0.0, "tokens": 0, "estimated": False, "problems": [], "gemini": ["delivery"], "codex_limit": None, "roles": {}}}}
        self.assertIn("Gemini is not measured", story.money_line(gem, TASK)["text"])


class AgentStripTests(unittest.TestCase):
    def sessions(self, **status):
        return [{"id": "s-dev", "provider": "codex", "vendor": "OpenAI", "status": status.get("dev", "running")},
                {"id": "s-qa", "provider": "claude", "vendor": "Anthropic", "status": status.get("qa", "running"), "read_only": status.get("read_only", False)},
                {"id": "s-cto", "provider": "claude", "vendor": "Anthropic", "status": "running"}]

    def strip(self, st, **kw):
        sessions = kw.pop("sessions", self.sessions())
        return {row["role"]: row for row in story.agent_strip(st, TASK, agents=kw.pop("agents", None), sessions=sessions, **kw)}

    def test_three_roles_with_company_cost_and_the_buttons_the_spec_names(self):
        st = state(agents=[builder(), reviewer(), cto()], cto_wakes={"by_task": {TASK: {"heartbeat": 1, "wake": 1}}},
                   difficulty_notes={TASK: [{"role": "builder", "text": "the brief did not name a city", "at": "2026-10-27T10:00:00+00:00"}]})
        rows = self.strip(st, cost=SUMMARY, story=built(st))
        self.assertEqual([rows[r]["label"] for r in ("builder", "reviewer", "cto")], ["Builder", "Independent reviewer", "CTO"])
        self.assertEqual((rows["builder"]["company"], rows["reviewer"]["company"]), ("OpenAI Codex", "Anthropic Claude"))
        self.assertEqual(rows["builder"]["cost"], "$1.90 · 300K tokens")
        self.assertIn("What slowed me: the brief did not name a city", rows["builder"]["slowed"])
        self.assertEqual(rows["cto"]["detail"], "2 wake-ups this task.")
        self.assertEqual(rows["builder"]["message"], {"mode": "composer", "type": "clarification", "label": "Message", "note": ""})
        self.assertEqual(rows["cto"]["message"]["type"], "cto_message")
        self.assertTrue(rows["builder"]["view"]["enabled"])

    def test_the_reviewer_has_no_message_route_so_its_button_opens_its_screen(self):
        st = state(agents=[builder(), reviewer(), cto()])
        reviewer_row = self.strip(st)["reviewer"]
        self.assertEqual((reviewer_row["message"]["mode"], reviewer_row["message"]["label"]), ("screen", "Type to the reviewer"))

    def test_stopped_or_read_only_or_missing_sessions_say_so_and_point_to_a_plain_way(self):
        st = state(agents=[builder(), reviewer(), cto()])
        stopped = self.strip(st, sessions=self.sessions(qa="stopped"))["reviewer"]
        self.assertEqual((stopped["message"]["mode"], stopped["view"]["enabled"]), ("none", False))
        self.assertIn("Open its terminal window on this computer, or resume the project", stopped["message"]["note"])
        read_only = self.strip(st, sessions=self.sessions(read_only=True))["reviewer"]
        self.assertIn("read-only", read_only["view"]["note"])
        absent = self.strip(state(agents=[builder()]))["reviewer"]
        self.assertFalse(absent["present"])
        self.assertIn("No reviewer is running", absent["view"]["note"])
        self.assertEqual(absent["cost"], "Cost: not measured yet")

    def test_a_waiting_builder_is_asked_for_direction_not_a_clarification(self):
        waiting = builder(task="AWAITING_OWNER_DIRECTION")
        row = self.strip(state(agents=[waiting]))["builder"]
        self.assertEqual((row["message"]["mode"], row["message"]["type"]), ("composer", "direction"))

    def test_the_roles_company_falls_back_to_settings_when_nothing_is_running(self):
        settings = {"agent_settings": {"reviewer": {"provider": "gemini"}}}
        row = self.strip(state(agents=[builder()]), settings=settings)["reviewer"]
        self.assertEqual(row["company"], "Google Gemini")


class PageTests(unittest.TestCase):
    def test_two_live_tasks_lead_with_the_one_that_needs_the_owner(self):
        passed = {"id": "rp", "task": "OTHER", "status": "passed", "phase": "final_acceptance", "stage": "independent_review", "cycle": 1,
                  "requested_at": "2026-10-27T10:05:00+00:00"}
        st = state(agents=[builder(), builder("OTHER", id="dev-2", session_id="s-dev2")], requests=[passed], releases={"OTHER": {"status": "VISUAL_TEST_REQUIRED"}},
                   requirement_confirmations={TASK: {"text": "agreed"}, "OTHER": {"text": "agreed"}})
        page = story.for_page(st, {}, [TASK, "OTHER"])
        self.assertEqual(page["current"]["task"], "OTHER")
        self.assertEqual(page["order"], ["OTHER", TASK])
        self.assertFalse(page["empty"])

    def test_no_live_task_is_an_empty_page_with_no_current_story(self):
        page = story.for_page({"agents": {}}, {}, [])
        self.assertTrue(page["empty"])
        self.assertIsNone(page["current"])
        self.assertEqual(len(page["agents"]), 3)


# ---- slice 2: the 26-step timeline and the Proof tab -------------------------------------------------------------------------------

def contract(**extra):
    """A confirmed contract with three requirements: 1 covered and tested, 2 a gap, 3 a quality requirement with no stored test."""
    block = {"status": "ready", "reason": "", "labels": [
        {"n": 1, "criterion": "Lists the planets", "plain": "Lists the planets visible tonight", "label": "checked", "coverage": "covered", "kind": "", "note": ""},
        {"n": 2, "criterion": "Hides planets below the horizon", "plain": "Hides planets below the horizon", "label": "checked", "coverage": "gap: only the normal case is tested, no failure or unusual case", "kind": "", "note": ""},
        {"n": 3, "criterion": "Loads fast", "plain": "The page loads in under 2 seconds", "label": "not checked in advance", "coverage": "gap: no test covers this yet", "kind": "non-functional", "note": ""}],
        "checks": [{"n": "1.1", "criterion_index": 1, "sha256": "s11"}, {"n": "2.1", "criterion_index": 2, "sha256": "s21"}], "checks_sha256": "HASH",
        "compare": {"1": "fails_before", "2": "passes_on_nothing"}, "results": {"1": "passed", "2": "failed"},
        "red_proof": {"status": "settled", "checks": {"1.1": {"label": "fails_before", "sha256": "s11"}, "2.1": {"label": "passes_on_nothing", "sha256": "s21"}}},
        "questions": [], "answers": [], "deviations": []}
    block.update(extra)
    return {TASK: {"text": "agreed", "confirmed_at": "2026-10-27T10:00:00+00:00", "contract_check": block}}


def fresh_request(rid, status, **extra):
    return request(rid, status, reviewed_commit="C1", contract_check_runs=[{"commit": "C1", "checks_sha256": "HASH", "status": "passed"}], **extra)


def rows_by_step(result):
    return {(row["n"], row["area"]): row for row in result["timeline"]["steps"]}


def row(result, n, area=""):
    return rows_by_step(result)[(n, area)]


class TimelineTests(unittest.TestCase):
    """Slice 2: the 26 steps as a live timeline (spec sections 3 and 12). A display, never a gate."""

    def failed_round_one(self):
        return request("r1", "failed", review_round=1, structured_findings=[{"id": "B1"}, {"id": "B2"}], completed_at="2026-10-27T10:30:00+00:00",
                       board_decision={"notes": ["n1", "n2", "n3"]}, delivery_state="passed", delivery_evidence="e", evidence="board-run")

    def test_exactly_one_step_is_now_and_the_rest_follow_from_facts(self):
        result = built(state(requirement_confirmations=contract(), requests=[self.failed_round_one()],
                             delivery_plans={TASK: {"mode": "atomic"}}))
        steps = result["timeline"]["steps"]
        self.assertEqual([r["n"] for r in steps if r["status"] == "now"], [19])
        self.assertEqual(len(steps), 26)
        self.assertEqual((row(result, 17)["status"], row(result, 17)["text"]), ("done", "Round 1: 2 blockers, 3 notes."))
        self.assertEqual(row(result, 18)["text"], "Round 1 found 2 problems to fix.")
        self.assertEqual(row(result, 19)["tag"], "Now")
        self.assertIn("2 problems", row(result, 19)["text"])
        self.assertEqual({row(result, n)["status"] for n in (20, 21)}, {"next"})
        self.assertEqual([row(result, n)["fold"] for n in (20, 21, 22, 23)], ["", "", "later", "later"], "only the next two steps stay open; the rest fold under 'more steps to come'")
        self.assertEqual(result["timeline"]["counts"]["later"], 5)

    def test_a_gap_is_never_done_and_the_early_steps_say_what_happened(self):
        result = built(state(requirement_confirmations=contract(), requests=[self.failed_round_one()]))
        self.assertEqual(row(result, 8)["status"], "gap")
        self.assertEqual(row(result, 8)["tag"], "Not covered")
        self.assertIn("1 of 3 are covered", row(result, 8)["text"])
        self.assertEqual(row(result, 10)["title"], "Tests checked on the empty project", "'settled' never claims every test failed (spec 12.1)")
        self.assertIn("1 of 2 tests failed first (good), 1 did not distinguish", row(result, 10)["text"])
        self.assertEqual(row(result, 6)["status"], "done")

    def test_bypasses_show_skipped_with_the_reason_and_collapse(self):
        small = built(state(requirement_confirmations={TASK: {"text": "agreed", "contract_check": {"lane": "small", "status": "not_checked"}}}))
        for n in (3, 4, 5, 7, 8, 9, 10, 11):
            self.assertEqual((row(small, n)["status"], row(small, n)["fold"]), ("skipped", "skipped"), n)
            self.assertIn("small change", row(small, n)["text"])
        self.assertEqual(small["timeline"]["counts"]["skipped"], 8)
        unrecorded = built(state(requirement_confirmations={TASK: {"text": "agreed", "contract_check": {"status": "not_checked", "reason": "the reviewer was busy"}}}))
        self.assertIn("the reviewer was busy", row(unrecorded, 7)["text"])
        self.assertEqual(row(unrecorded, 7)["status"], "skipped")

    def test_a_step_with_no_fact_is_not_recorded_never_an_error(self):
        result = built(state(requirement_confirmations=contract(), requests=[request("r1", "claimed")]))
        self.assertEqual(row(result, 15)["status"], "not_recorded", "the review is under way but the board holds no record of its own run of the tests")
        self.assertEqual(row(result, 15)["text"], "This step was not tracked on its own for this task.")
        self.assertNotIn("error", result["timeline"])

    def test_repeated_cycles_one_band_per_work_area(self):
        plan = {"mode": "application", "subtasks": {"search": {"status": "passed"}, "map": {"status": "working", "chunks": {}}, "share": {"status": "waiting", "chunks": {}}}}
        reviews = [request("a1", "failed", subtask="search", review_round=1, structured_findings=[{"id": "B1"}], completed_at="2026-10-27T10:02:00+00:00", minute=1),
                   request("a2", "passed", subtask="search", review_round=2, completed_at="2026-10-27T10:04:00+00:00", cycle=2, minute=3),
                   request("b1", "claimed", subtask="map", minute=5)]
        result = built(state(requirement_confirmations=contract(), delivery_plans={TASK: plan}, requests=reviews))
        steps = result["timeline"]["steps"]
        self.assertEqual(result["timeline"]["areas"], 3)
        bands = sorted({r["area"] for r in steps if r["area"]})
        self.assertEqual(bands, ["Work area 1 of 3: Search", "Work area 2 of 3: Map", "Work area 3 of 3: Share"])
        self.assertEqual(len([r for r in steps if r["area"] == "Work area 1 of 3: Search"]), 10)
        first = lambda n: row(result, n, "Work area 1 of 3: Search")
        self.assertEqual((first(17)["status"], first(19)["status"], first(20)["status"], first(21)["status"]), ("done", "done", "done", "skipped"))
        self.assertEqual(first(20)["text"], "Round 2: passed.")
        second = lambda n: row(result, n, "Work area 2 of 3: Map")
        self.assertEqual(second(17)["status"], "now", "the work area that is being reviewed holds the Now step")
        third = lambda n: row(result, n, "Work area 3 of 3: Share")
        self.assertEqual(third(12)["status"], "done", "the plan covers every work area")
        self.assertEqual({third(n)["status"] for n in range(13, 22)}, {"next"})
        self.assertEqual(len([r for r in steps if r["status"] == "now"]), 1)

    def test_not_accepted_returns_to_repair_and_marks_the_old_review_as_the_earlier_version(self):
        passed = request("r9", "passed", phase="final_acceptance", claimed_by="qa-1", reviewed_commit="C1")
        result = built(state(agents=[builder(), reviewer()], requests=[passed], requirement_confirmations=contract(),
                             releases={TASK: {"status": "VISUAL_TEST_REQUIRED", "head_commit": "C1", "preview": {"status": "ready"}}},
                             release_decisions={TASK: {"decision": "not_accepted", "reason": "the colours are wrong", "recorded_at": "2026-10-27T12:00:00+00:00"}}))
        self.assertEqual(result["step"], 19)
        self.assertEqual(row(result, 19)["status"], "now")
        self.assertIn("Repairing after your feedback", row(result, 19)["text"])
        for n in (22, 23, 24, 25):
            self.assertEqual((row(result, n)["status"], row(result, n)["tag"]), ("earlier", "Done for the earlier version"), n)
        self.assertEqual(row(result, 26)["status"], "next")
        self.assertTrue(result["timeline"]["earlier"])

    def test_a_finished_task_has_no_now_and_step_26_is_done_only_with_the_merge(self):
        merged = built(state(release_decisions={TASK: {"decision": "accepted", "recorded_at": "2026-10-27T12:00:00+00:00"}}, git_acceptances={TASK: {"commit": "abc"}},
                             task_repositories={TASK: {"path": "/x"}}, requirement_confirmations=contract()))
        self.assertEqual([r["n"] for r in merged["timeline"]["steps"] if r["status"] == "now"], [])
        self.assertEqual((row(merged, 26)["status"], row(merged, 26)["title"]), ("done", "Accepted and in main"))
        ungoverned = built(state(release_decisions={TASK: {"decision": "accepted"}}))
        self.assertEqual(row(ungoverned, 26)["title"], "Accepted", "a task without governed Git is never promised to be in main")
        saved_only = built(state(release_decisions={TASK: {"decision": "accepted", "git_acceptance": {"status": "failed"}}}))
        self.assertEqual((row(saved_only, 26)["status"], row(saved_only, 26)["title"]), ("now", "Accepted, not in main yet"))
        self.assertTrue(row(saved_only, 26)["owner"])

    def test_an_exception_replaces_the_now_text_and_stays_on_the_now_row(self):
        result = built(state(reviewer_needed={"sign_in": True}, requests=[request("r1", "claimed")]))
        self.assertEqual(result["exception"]["kind"], "sign_in")
        now = [r for r in result["timeline"]["steps"] if r["status"] == "now"][0]
        self.assertIn("signed out", now["text"])

    def test_the_timeline_is_left_out_of_the_projects_card(self):
        self.assertNotIn("timeline", story.build(state(), {}, TASK, detail=False))

    def test_help_phases_list_every_step_exactly_once(self):
        phases = story.help_phases()
        self.assertEqual([p["title"] for p in phases], ["Describe and approve", "Tests come first", "Built and proven", "Reviews that end", "Try and accept"])
        numbers = [step["n"] for p in phases for step in p["steps"]]
        self.assertEqual(numbers, list(range(1, 27)))
        self.assertEqual([step["title"] for p in phases for step in p["steps"]][18], "Repair")


class ProofTests(unittest.TestCase):
    """Slice 2: the Proof tab. 'Passing now' only for the current version's commit and locked-check hash (spec 12.5)."""

    def proof(self, **kw):
        requests = kw.pop("requests", [fresh_request("r1", "passed", phase="final_acceptance", claimed_by="qa-1")])
        return built(state(agents=[builder(), reviewer()], requirement_confirmations=kw.pop("contract", contract()), requests=requests, **kw))["proof"]

    def test_each_requirement_shows_covered_before_and_now_and_a_missing_test_is_never_green(self):
        proof = self.proof()
        rows = {r["n"]: r for r in proof["rows"]}
        self.assertEqual(rows[1]["covered"], {"text": "Covered", "ok": True})
        self.assertEqual(rows[1]["before"], {"text": "Failed before the work (good)", "tone": "good"})
        self.assertEqual(rows[1]["now"], {"text": "Passing now", "tone": "good"})
        self.assertFalse(rows[2]["covered"]["ok"])
        self.assertEqual(rows[2]["before"]["tone"], "bad")
        self.assertIn("Did not distinguish", rows[2]["before"]["text"])
        self.assertEqual(rows[2]["now"]["tone"], "bad")
        self.assertTrue(rows[3]["quality"])
        self.assertEqual(rows[3]["tests"], 0)
        self.assertEqual(rows[3]["now"]["tone"], "none")
        self.assertIn("No test is tied", rows[3]["now"]["text"])
        self.assertEqual([t["value"] for t in proof["tiles"][:3]], ["1 of 3", "1 of 2", "1 of 2"])

    def test_passing_now_needs_this_versions_commit_and_the_same_locked_checks(self):
        other_commit = self.proof(requests=[request("r1", "passed", phase="final_acceptance", reviewed_commit="C2", contract_check_runs=[{"commit": "C1", "checks_sha256": "HASH"}])])
        self.assertEqual({r["now"]["text"] for r in other_commit["rows"] if r["tests"] and r["n"] == 1}, {"Not run on this version"})
        other_checks = self.proof(requests=[request("r1", "passed", phase="final_acceptance", reviewed_commit="C1", contract_check_runs=[{"commit": "C1", "checks_sha256": "OLD"}])])
        self.assertEqual(other_checks["rows"][0]["now"]["text"], "Not run on this version")
        self.assertFalse(other_checks["fresh"])
        self.assertEqual(other_checks["tiles"][2]["value"], "Not run on this version")
        no_run = self.proof(requests=[request("r1", "passed", phase="final_acceptance", reviewed_commit="C1")])
        self.assertEqual(no_run["rows"][0]["now"]["text"], "Not run on this version")
        self.assertNotIn("Passing now", [r["now"]["text"] for r in no_run["rows"]])

    def test_after_not_accepted_the_old_results_are_the_earlier_version_and_never_current(self):
        passed = fresh_request("r9", "passed", phase="final_acceptance", claimed_by="qa-1", minute=1)
        proof = self.proof(requests=[passed], release_decisions={TASK: {"decision": "not_accepted", "recorded_at": "2026-10-27T23:00:00+00:00"}})
        self.assertTrue(proof["earlier"])
        self.assertEqual(proof["rows"][0]["now"]["text"], "From the earlier version, not current")
        self.assertIn("before your feedback", proof["earlier_note"])
        self.assertNotIn("Passing now", [r["now"]["text"] for r in proof["rows"]])

    def test_the_revert_tile_says_n_of_m_noticed_and_k_not_checked_never_all(self):
        files = [{"path": "a.py", "outcome": "noticed", "check": "1.1"}, {"path": "b.py", "outcome": "noticed", "check": "1.1"}, {"path": "c.py", "outcome": "not_noticed"},
                 {"path": "d.py", "outcome": "over_limit"}, {"path": "e.py", "outcome": "could_not_check", "reason": "x"}, {"path": "README.md", "outcome": "skipped", "reason": "a document"}]
        proof = self.proof(revert_proofs={TASK: {"commit": "C1", "checks_sha256": "HASH", "files": files}})
        tile = proof["tiles"][3]
        self.assertEqual(tile["value"], "2 of 3 noticed")
        self.assertIn("3 not checked", tile["note"])
        self.assertIn("c.py", tile["note"], "the board's own sentence is carried verbatim")
        stale = self.proof(revert_proofs={TASK: {"commit": "OLD", "checks_sha256": "HASH", "files": files}})
        self.assertEqual(stale["tiles"][3]["value"], "Not run on this version")
        nothing = self.proof()
        self.assertEqual(nothing["tiles"][3]["value"], "Not measured")

    def test_questioned_tests_code_quality_and_tests_not_tied_to_a_requirement(self):
        devs = [{"check": "2.1", "why": "the data has no such field", "status": "kept", "answer_reason": "compute it from the position"}, {"check": "1.1", "why": "wrong time zone", "status": "open"}]
        ledger = {"delivery": {"scenarios": [{"status": "passed"}, {"status": "passed"}]}, "reviewer": {"scenarios": [{"status": "failed"}]}}
        st = state(agents=[builder(), reviewer()], requirement_confirmations=contract(deviations=devs), requests=[fresh_request("r1", "claimed")],
                   code_quality_notes={TASK: {"notes": ["planets.py: the city is typed in"]}})
        proof = story.build(st, {}, TASK, ledger=ledger)["proof"]
        self.assertEqual(len(proof["questioned"]), 2)
        self.assertIn("The reviewer kept it: compute it from the position", proof["questioned"][0].replace("kept it:", "kept it:").replace("The reviewer kept it:", "The reviewer kept it:"))
        self.assertIn("has not answered yet", proof["questioned"][1])
        self.assertEqual(proof["quality"], ["planets.py: the city is typed in"])
        self.assertEqual(proof["untied"]["title"], "Not tied to a requirement")
        self.assertIn("3 other checks", proof["untied"]["text"])
        self.assertIn("2 of them passed", proof["untied"]["text"])

    def test_no_contract_check_says_so_and_claims_nothing(self):
        proof = built(state(requirement_confirmations={TASK: {"text": "agreed", "contract_check": {"lane": "small", "reason": "small change"}}}))["proof"]
        self.assertEqual(proof["rows"], [])
        self.assertIn("No tests were written in advance", proof["header"])
        self.assertEqual(proof["tiles"][0]["value"], "None")
        self.assertEqual(proof["requirements_tile"]["text"], "")


class ViewerParityTests(unittest.TestCase):
    """Spec section 3: `taskGate` (the viewer's JavaScript) stays the status source until slice 3, so the Python port is pinned to it:
    the same fixtures go through both and the status and the board's own sentence must agree."""

    def fixtures(self):
        findings = [{"id": "B1"}]
        passed = request("r9", "passed", phase="final_acceptance", claimed_by="qa-1")
        release = {TASK: {"status": "VISUAL_TEST_REQUIRED", "head_commit": "abc123", "checks": {"deployed_runtime_verified": True}}}
        yield "no agent", state(agents=[]), None
        yield "clarifying", state(confirmed=False), None
        yield "building", state(), None
        yield "authoring", state(requests=[request("r1", "authoring")]), None
        yield "reserved executing", state(requests=[request("r1", "reserved", delivery_state="executing")]), None
        yield "reserved", state(requests=[request("r1", "reserved")]), None
        yield "open", state(requests=[request("r1", "open")]), None
        yield "claimed", state(requests=[request("r1", "claimed")]), None
        yield "failed", state(requests=[request("r1", "failed", review_round=1, structured_findings=findings)]), None
        yield "final passed", state(requests=[passed]), None
        yield "ready", state(requests=[passed], releases=release), {"commit": "abc123", "managed": True}
        yield "ready, other commit", state(requests=[passed], releases=release), {"commit": "zzz", "managed": True}
        yield "ready, no commit known", state(requests=[passed], releases=release), {"commit": "", "managed": True}
        yield "ready, unmanaged", state(requests=[passed], releases=release), {"commit": "zzz", "managed": False}
        yield "ready, external target", state(requests=[passed], releases={TASK: {**release[TASK], "runtime_verification_deferred_to_target_acceptance": True}}), {"commit": "zzz", "managed": True}
        yield "not accepted", state(releases=release, release_decisions={TASK: {"decision": "not_accepted"}}), None
        yield "accepted", state(release_decisions={TASK: {"decision": "accepted"}}), None
        yield "accepted, merge failed", state(release_decisions={TASK: {"decision": "accepted", "git_acceptance": {"status": "failed"}}}), None
        yield "reintegration", state(requests=[request("r1", "claimed")], git_reintegration_required={TASK: {"owner_line": "Other work changed the app."}}), None
        yield "stopped", state(agents=[builder(active=False, status="done", stopped_by_owner_at="2026-10-27T10:30:00+00:00")]), None
        yield "git refused", state(agents=[builder(broker_refusal={"reason": "protected"})]), None
        yield "stalled", state(agents=[builder(liveness="stalled", recent_output_at="2000-01-01T00:00:00+00:00")]), None
        yield "repairing", state(agents=[builder(status="repairing")]), None
        yield "all parts passed", state(delivery_plans={TASK: {"mode": "application", "subtasks": {"a": {"status": "passed"}}}}), None

    def test_python_gate_agrees_with_the_viewers_task_gate_on_every_fixture(self):
        import json
        import subprocess
        from harness import board_viewer
        for name, st, runtime in self.fixtures():
            runtime = runtime or {"commit": "", "managed": False}
            page = board_viewer.rendered_page(project_id="p1" if runtime["managed"] else "", runtime={"commit": runtime["commit"]})
            script = page.split("<script>", 1)[1].split("</script>", 1)[0]
            declarations = script.split("el('#status-dialog-close')", 1)[0]
            facts = story.task_facts(st, TASK)
            js_state = {**st, "stopped_tasks": story.stopped_tasks(st)}
            invocation = ("process.stdout.write(JSON.stringify(taskGate(" + json.dumps(js_state) + "," + json.dumps(TASK) + ",{}," + json.dumps(facts["agent"]) + ","
                          + json.dumps(facts["reviews"]) + f",{facts['total']},{facts['done']})));")
            completed = subprocess.run(["node", "--input-type=commonjs", "-"], input=declarations + "\n" + invocation, capture_output=True, text=True)
            self.assertEqual(completed.returncode, 0, f"{name}: {completed.stderr}")
            js = json.loads(completed.stdout)
            mine = story.gate(st, TASK, facts, runtime_commit=runtime["commit"], runtime_managed=runtime["managed"], stopped=story.stopped_tasks(st))
            self.assertEqual(mine["status"], js["status"], name)
            self.assertEqual(mine["next"], js["next"], name)


if __name__ == "__main__":
    unittest.main()
