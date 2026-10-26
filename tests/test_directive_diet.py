# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Directive diet (task 2026-10-25_directive-diet): a one-page rule card per role, the rest as named chapters read on demand.

The agents drift from a long rulebook, so each role now starts with a short card (directives/cards/) and opens the long text by
chapter name with the board command `reference`. Pinned here: the card shape and its pointers, the launch of every role with the card
on every path (system layer or first message, fresh or resumed), the one record of what was delivered per launch, the `reference`
command, and the `Open reference:` line on the messages the board routes.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from harness import board, board_mcp, claude_inbox, contract, contract_check, control, gemini_runtime, reference_chapters, test_machine
from harness.board_surface import CommandGateway, SessionTokenAuthority, SurfaceAuthenticationError, SurfaceAuthorizationError
from harness.project_context import ProjectContext
from tests import test_board
from tests import test_board_surface as surface
from tests.environment_support import require_loopback
from tests.requirements_support import agreed_requirements
from tests.test_conversation_memory import stage_relaunch
from tests.test_stage4_system_layer import Stage4RunnerTests

ROOT = Path(__file__).resolve().parents[1]
ROLES = ("engineering", "qa", "cto")
KINDS = {"codex_delivery": "engineering", "claude_reviewer": "qa", "claude_cto": "cto"}
CARD_FILES = {"codex_delivery": "directives/cards/DELIVERY.md", "claude_reviewer": "directives/cards/REVIEWER.md", "claude_cto": "directives/cards/CTO.md"}
# What the launcher delivered before this change: the whole reference file (bytes as `$(<file)` yields them), at the base of this task.
BASELINE_BYTES = {"delivery": 88_339, "reviewer": 88_339, "cto": 23_469}


def card_text(kind: str) -> str:
    return (ROOT / CARD_FILES[kind]).read_text(encoding="utf-8").rstrip("\n")


class CardShapeTests(unittest.TestCase):
    def test_every_card_is_one_page_and_every_pointer_resolves_to_a_chapter_of_that_roles_reference(self):
        for role in ROLES:
            with self.subTest(role=role):
                self.assertEqual(reference_chapters.card_problems(role), [])
                self.assertGreaterEqual(len(reference_chapters.card_rules(role)), 30)

    def test_chapters_are_contiguous_unique_and_each_is_named_by_a_card_of_a_role_that_reads_it(self):
        for role in ROLES:
            text = reference_chapters.reference_path(role).read_text(encoding="utf-8")
            names = [line[3:] for line in text.split("\n") if line.startswith("## ")]
            self.assertEqual(len(names), len(set(names)), "a chapter name appears twice")
            self.assertEqual(names, list(reference_chapters.role_chapters(role)), "every ## line is a chapter and they are in file order")
        self.assertEqual(reference_chapters.orphan_chapters(), {}, "a chapter no card rule points at can never be found")

    def test_each_role_has_its_own_card_and_the_cto_card_leaves_room_for_the_authority_note(self):
        delivery, reviewer = card_text("codex_delivery"), card_text("claude_reviewer")
        self.assertNotEqual(delivery, reviewer)
        self.assertNotEqual(delivery, card_text("claude_cto"))
        # the one surviving size assertion on what the CTO is handed (Gemini loads at most 24,000 bytes of a rules file)
        payload = len(card_text("claude_cto").encode("utf-8")) + len(claude_inbox.AUTHORITY_NOTE.encode("utf-8")) + 2
        self.assertLessEqual(payload, gemini_runtime.RULES_LIMIT_BYTES)
        for kind, before in (("codex_delivery", BASELINE_BYTES["delivery"]), ("claude_reviewer", BASELINE_BYTES["reviewer"]), ("claude_cto", BASELINE_BYTES["cto"])):
            self.assertLess(len(card_text(kind).encode("utf-8")), before // 2, kind)

    def test_protections_the_board_cannot_check_have_an_explicit_opening_trigger_not_a_bare_arrow(self):
        said = {role: " ".join(sentence for _n, sentence, _t in reference_chapters.card_rules(role)) for role in ROLES}
        delivery = said["engineering"]
        for text in (
            "Before you build any app, open `security-baseline` and `error-sink`",
            "When `enhanced_security.on` is true in your poll, open `enhanced-security` before coding",
            "on `owner_release_repair_required`, open `owner-rejection-repair`",
            "follow the `test-machine` chapter exactly",
            "Open `text-deliverables` before you build text a human reads",
            "Open `rendered-proof` before you prove a user-facing change",
            "for an application open `delivery-plan` before declaring subtasks",
        ):
            self.assertIn(text, delivery)
        reviewer = said["qa"]
        for text in (
            "Before reviewing any app, open `security-baseline` and `error-sink`",
            "When `enhanced_security.on` is true in the review brief, open `enhanced-security` before the ledger",
            "Open `text-deliverables` before you judge text a human reads",
            "On a repair review open `repair-review`",
            "On `CONTRACT CHECK ROUTED` open `contract-first`",
            "open `test-machine` first",
            "Open `review-rounds` when a review is routed",
            "At final acceptance open `final-acceptance`",
        ):
            self.assertIn(text, reviewer)
        self.assertIn("Open `materiality` before you hold or block", said["cto"])
        # the slide-counting detail that changes a decision is named on both cards that apply it
        self.assertIn("title, notes, sources and chart labels excluded", reviewer)
        self.assertIn("text inside an image counted", reviewer)


class ReferenceCommandTests(unittest.TestCase):
    """The board command that opens a chapter: authenticated per role, JSON, read-only apart from its own event."""

    def setUp(self):
        require_loopback()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name)
        code = base / "code"; code.mkdir()
        self.context = ProjectContext(code, base / "data", base / "workspaces")
        control.initialize(self.context)

    def agent(self, kind, role, vendor_task):
        session = control.create(self.context, kind)
        authority = SessionTokenAuthority(self.context)
        authority.prepare(session["id"])
        control.attach(self.context, session["id"], os.getpid())
        agent = board.register(self.context, role, vendor_task, session_id=session["id"])
        token = authority.claim(session["id"], os.getpid())
        return agent, token, CommandGateway(self.context, authority)

    def ask(self, gateway, token, agent, name, nonce):
        return gateway.execute(token, surface.BoardSurfaceCommandTests.request("reference", ["--agent", agent["id"], "--name", name], nonce=nonce))["result"]

    def test_each_role_gets_exactly_its_chapter_as_json_and_the_open_is_logged_without_changing_anything_else(self):
        seen = {}
        for kind, role, task in (("codex_delivery", "engineering", board.AWAITING_OWNER_DIRECTION), ("claude_reviewer", "qa", "REVIEW_QUEUE"), ("claude_cto", "cto", "GLOBAL_MONITOR")):
            agent, token, gateway = self.agent(kind, role, task)
            before = board.snapshot(self.context)
            result = self.ask(gateway, token, agent, "materiality", 1)
            expected = reference_chapters.role_chapters(role)["materiality"]
            self.assertEqual(result["text"], expected)
            self.assertEqual(result["chapter"], "materiality")
            self.assertEqual(result["bytes"], len(expected.encode("utf-8")))
            self.assertEqual(result["sha256"], hashlib.sha256(expected.encode("utf-8")).hexdigest())
            self.assertEqual(result["file"], "directives/" + reference_chapters.ROLE_FILES[role][0])
            after = board.snapshot(self.context)
            opened = [event for event in after["events"] if event["kind"] == "reference_opened" and event["agent_id"] == agent["id"]]
            self.assertEqual([(e["chapter"], e["bytes"]) for e in opened], [("materiality", result["bytes"])])
            for key in ("qa_requests", "delivery_plans", "release_lifecycle", "deferred_findings", "owner_actions", "task_briefs",
                        "requirement_confirmations", "project_pause", "control_plane_holds", "review_escalations"):
                self.assertEqual(before.get(key), after.get(key), f"{key} changed: the command must not touch workflow state")
            seen[role] = result["text"]
        self.assertNotEqual(seen["cto"], seen["engineering"], "the CTO's materiality is its own file's text")
        self.assertEqual(seen["engineering"], seen["qa"], "Delivery and the Reviewer read the same file, so the same bytes whoever asks")

    def test_an_unknown_chapter_lists_the_names_and_a_chapter_of_the_other_file_is_unknown(self):
        agent, token, gateway = self.agent("codex_delivery", "engineering", board.AWAITING_OWNER_DIRECTION)
        with self.assertRaisesRegex(ValueError, r"no chapter named 'nope'.*challenge-ledger.*zero-setup"):
            self.ask(gateway, token, agent, "nope", 1)
        with self.assertRaisesRegex(ValueError, "no chapter named 'monitoring-cycle'"):
            self.ask(gateway, token, agent, "monitoring-cycle", 2)
        self.assertFalse([e for e in board.snapshot(self.context)["events"] if e["kind"] == "reference_opened"], "a refused name records nothing")

    def test_a_bad_token_is_refused_like_any_board_command_and_the_tool_exists_for_every_role(self):
        agent, token, gateway = self.agent("claude_reviewer", "qa", "REVIEW_QUEUE")
        with self.assertRaises(SurfaceAuthenticationError):
            self.ask(gateway, "not-the-token", agent, "materiality", 1)
        other, other_token, other_gateway = self.agent("codex_delivery", "engineering", board.AWAITING_OWNER_DIRECTION)
        with self.assertRaises((SurfaceAuthorizationError, ValueError)):
            self.ask(gateway, token, other, "materiality", 2)           # asking in another agent's name
        for role in ROLES:
            self.assertIn("reference", [tool["name"] for tool in board_mcp.tool_definitions(role)])


class RoutedMessageTests(unittest.TestCase):
    """The board tells the agent which chapter a situation needs: one `Open reference:` line on the messages it already routes."""
    evidence = test_board.BoardTests.evidence
    ledger = test_board.BoardTests.ledger
    qa_command = test_board.BoardTests.qa_command
    atomic_plan = test_board.BoardTests.atomic_plan
    delivery = test_board.BoardTests.delivery

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "test_smoke.py").write_text("import unittest\n\nclass Smoke(unittest.TestCase):\n    def test_passes(self): self.assertTrue(True)\n")

    def test_direction_and_begin_task_name_their_chapters(self):
        session = control.create(self.root, "codex_delivery")
        agent = board.register(self.root, "development", board.AWAITING_OWNER_DIRECTION, vendor="OpenAI", session_id=session["id"])
        board.record_owner_message(self.root, agent["id"], "Build me a forecast page.", "direction", [])
        routed = control.take_instructions(self.root, session["id"])
        self.assertTrue(any("Open reference: requirements" in row["text"] for row in routed), routed)
        began = board.begin_task(self.root, agent["id"], "TASK-HINT")
        self.assertRegex(began["open_reference"], r"^Open reference: delivery-workflow(, inherited-changes)?$")
        self.assertEqual(reference_chapters.reference_hint("delivery-workflow", "inherited-changes"), "Open reference: delivery-workflow, inherited-changes")
        # a Delivery agent registered by hand under the older role name reads the same chapters
        self.assertEqual(board.open_reference(self.root, agent["id"], "requirements")["text"], reference_chapters.role_chapters("engineering")["requirements"])

    def test_a_failed_review_and_a_final_pass_name_repair_rounds_and_release(self):
        session = control.create(self.root, "codex_delivery")
        dev = board.register(self.root, "engineering", board.AWAITING_OWNER_DIRECTION, vendor="OpenAI", session_id=session["id"])
        board.record_owner_direction(self.root, session["id"], "OWNER DIRECTION — TASK-HINT-FAIL")
        board.begin_task(self.root, dev["id"], "TASK-HINT-FAIL")
        contract.create_contract(self.root, "TASK-HINT-FAIL", "OWNER DIRECTION — TASK-HINT-FAIL", ["delivery"])
        agreed_requirements(self.root, dev["id"], "Final agreed requirements: route the QA result and repair the requested task.")
        self.atomic_plan(dev["id"])
        qa = board.register(self.root, "qa", "QA-QUEUE", vendor="Anthropic")
        request = board.request_qa(self.root, dev["id"], self.ledger("route.md"), "run QA")
        board.claim_qa(self.root, qa["id"], request["id"])
        board.qa_result(self.root, qa["id"], request["id"], "failed", "S-1 failed", self.evidence("route.txt"))
        routed = control.take_instructions(self.root, session["id"])
        self.assertTrue(any("FAILED" in row["text"] and "Open reference: repair, review-rounds" in row["text"] for row in routed), routed)

    def test_final_pass_tells_delivery_and_the_cto_to_open_the_release_chapter(self):
        dev = self.delivery("TASK-HINT-PASS")
        self.atomic_plan(dev["id"])
        reviewer = board.register(self.root, "qa", "REVIEW_QUEUE", vendor="Anthropic")
        cto_session = control.create(self.root, "claude_cto")
        board.register(self.root, "cto", "GLOBAL_MONITOR", vendor="Anthropic", session_id=cto_session["id"])
        control.take_instructions(self.root, dev["session_id"])
        request = board.request_review(self.root, dev["id"], self.ledger("hint-delivery.md"), "final candidate", phase="final_acceptance", test_command=self.qa_command())
        board.claim_qa(self.root, reviewer["id"], request["id"], self.ledger("hint-challenge.md"))
        board.execute_challenge(self.root, reviewer["id"], request["id"])
        board.qa_result(self.root, reviewer["id"], request["id"], "passed", "final checks passed", self.evidence("hint-final.txt"))
        delivery_text = " ".join(row["text"] for row in control.take_instructions(self.root, dev["session_id"]))
        cto_text = " ".join(row["text"] for row in control.take_instructions(self.root, cto_session["id"]))
        self.assertIn("Open reference: release", delivery_text)
        self.assertIn("Open reference: release", cto_text)

    def test_a_review_names_its_chapters_by_what_kind_of_review_it_is(self):
        self.assertEqual(board._review_chapters({"phase": "chunk"}), ["challenge-ledger", "review-rounds"])
        self.assertEqual(board._review_chapters({"phase": "final_acceptance", "repair_context": {"prior_request_id": "r"}}),
                         ["challenge-ledger", "review-rounds", "final-acceptance", "repair-review"])

    def test_the_contract_check_messages_point_at_the_chapter_not_at_the_old_section_names(self):
        first = contract_check.reviewer_instruction("T", 1, "reqs", "dir", 10)
        again = contract_check.recheck_instruction("T", 1, [{"text": "q", "answer": "a"}], [1], [{"n": 1, "text": "c"}], 10)
        for text in (first, again):
            self.assertIn("Open reference: contract-first", text)
            self.assertNotIn("Independent Reviewer workflow", text)

    def test_the_machine_note_and_the_owner_rejection_name_their_chapters(self):
        source = (ROOT / "harness" / "board.py").read_text(encoding="utf-8")
        self.assertIn('reference_chapters.reference_hint("owner-rejection-repair")', source)
        with mock.patch.object(test_machine, "read", return_value=None):
            self.assertEqual(test_machine.launch_note(self.root, open_reference=True), "")
        with mock.patch.object(test_machine, "read", side_effect=test_machine.Damaged("x")):
            self.assertTrue(test_machine.launch_note(self.root, open_reference=True).endswith("Open reference: test-machine"))
            self.assertNotIn("Open reference", test_machine.launch_note(self.root))

    def test_enhanced_security_on_names_its_chapter_in_the_poll_and_the_review_brief(self):
        dev = self.delivery("TASK-SEC")
        self.atomic_plan(dev["id"])
        bundle = board.poll(self.root, dev["id"])["context_bundle"]["tasks"][0]
        self.assertNotIn("open_reference", bundle["enhanced_security"])
        board.set_task_security(self.root, "TASK-SEC", True)
        bundle = board.poll(self.root, dev["id"])["context_bundle"]["tasks"][0]
        self.assertEqual(bundle["enhanced_security"]["open_reference"], "Open reference: enhanced-security")
        board.request_review(self.root, dev["id"], self.ledger("sec.md"), "final", phase="final_acceptance", test_command=self.qa_command())
        request = next(value for value in board.snapshot(self.root)["qa_requests"].values() if value["task"] == "TASK-SEC")
        self.assertEqual(request["review_brief"]["open_reference"], "Open reference: enhanced-security")
        self.assertEqual(request["review_brief"]["enhanced_security"], {"on": True, "revision": 1}, "the frozen choice itself is unchanged")


class ScriptedRouteTests(unittest.TestCase):
    """A scripted integration test of the route (routed line -> `reference` -> the verdict text), NOT proof that a live agent obeys the rule."""

    def test_the_verdict_composed_from_the_retrieved_chapter_carries_what_that_chapter_requires(self):
        chapter = reference_chapters.chapter_result("qa", "enhanced-security")["text"]
        # what the chapter asks of the Reviewer, read out of the retrieved text itself (not typed into the script)
        required = [phrase for phrase in ("History scan", "`S-SEC-`", "unjustified disposition") if phrase in chapter]
        self.assertEqual(len(required), 3, "the chapter must hold the items the verdict is checked against")
        retrieved = chapter                                       # what the agent got back from `reference`
        verdict = "TASK DONE: YES - " + " / ".join(f"checked {phrase}" for phrase in required if phrase in retrieved)
        for phrase in required:
            self.assertIn(phrase, verdict)
        nothing_retrieved = "TASK DONE: YES - " + " / ".join(f"checked {phrase}" for phrase in required if phrase in "")
        self.assertNotIn("S-SEC", nothing_retrieved, "a chapter that is not retrieved leaves the verdict without its items")


class LaunchMatrixTests(Stage4RunnerTests):
    """Every role, with the system layer on and off, fresh and resumed, gets its own card and the launch is recorded once."""

    def setUp(self):
        super().setUp()
        self.codex_home = self.base / "codex-home"

    def settings_for(self, kind: str, provider: str) -> dict:
        """Settings that run `kind` on `provider` (Delivery and the Reviewer must use different providers, so the other one takes the other)."""
        model = {"claude": "opus", "codex": "gpt-5.6-sol"}
        other = {"claude": "codex", "codex": "claude"}[provider]
        entry = lambda name: {"provider": name, "model": model[name], "effort": "high"}  # noqa: E731
        if kind == "codex_delivery":
            return {"delivery": entry(provider), "reviewer": entry(other), "cto": entry("claude")}
        if kind == "claude_reviewer":
            return {"delivery": entry(other), "reviewer": entry(provider), "cto": entry("claude")}
        return {"delivery": entry("codex"), "reviewer": entry("claude"), "cto": entry(provider)}

    def delivered(self, argv, provider):
        """(system layer text or None, first message) as this CLI received them."""
        system = None
        if provider == "claude" and "--append-system-prompt" in argv:
            system = argv[argv.index("--append-system-prompt") + 1]
        if provider == "codex":
            flags = [argv[index + 1] for index, item in enumerate(argv) if item == "-c"]
            found = [flag for flag in flags if flag.startswith("developer_instructions=")]
            system = json.loads(found[0].split("=", 1)[1]) if found else None
        return system, argv[-1]

    def stage_resume(self, session, provider, argv):
        if provider == "claude":
            minted = argv[argv.index("--session-id") + 1]
            store = self.claude_dir / "projects" / "-code"; store.mkdir(parents=True, exist_ok=True)
            (store / f"{minted}.jsonl").write_text("{}\n", encoding="utf-8")
        else:
            cli_id = "00000000-0000-0000-0000-00000000cafe"
            day = self.codex_home / "sessions" / "2026" / "09" / "22"; day.mkdir(parents=True, exist_ok=True)
            (day / f"rollout-2026-09-22T16-00-00-{cli_id}.jsonl").write_text("{}\n", encoding="utf-8")
            control.record_cli_session(self.context, session["id"], cli_id, "codex")
        stage_relaunch(self.context, session["id"])

    def record_of(self, session_id):
        return next(item for item in control.snapshot(self.context)["sessions"] if item["id"] == session_id)

    def test_every_kind_and_provider_carries_its_card_on_every_path_and_records_it_once_per_launch(self):
        self.require_confinement()
        for kind in KINDS:
            card = card_text(kind)
            for provider in ("claude", "codex"):
                for stage4 in (True, False):
                    self.switch(provider, stage4=stage4)
                    session = control.create(self.context, kind, settings_override=self.settings_for(kind, provider), fresh=True)
                    with self.subTest(kind=kind, provider=provider, stage4=stage4):
                        launches = []
                        argv, _ = self.launch_with(session, kind)
                        launches.append(("fresh", argv))
                        self.stage_resume(session, provider, argv)
                        argv, _ = self.launch_with(session, kind)
                        self.assertIn("resume" if provider == "claude" else "resume", " ".join(argv), "the second launch is a resume")
                        launches.append(("resume", argv))
                        for mode, argv in launches:
                            system, first = self.delivered(argv, provider)
                            self.assertFalse(any("\n## " in part for part in argv), "the whole reference file is never delivered")
                            if stage4:
                                self.assertEqual(system, card, f"{mode}: the card is the system layer")
                                self.assertFalse(first.startswith(card))
                            else:
                                self.assertIsNone(system)
                                self.assertTrue(first.startswith(card + "\n\n"), f"{mode}: no system layer, so the card rides the first message")
                        record = self.record_of(session["id"])
                        entries = record["instruction_deliveries"]
                        self.assertEqual([entry["launch_mode"] for entry in entries], ["fresh", "resume"])
                        for entry in entries:
                            self.assertEqual(entry["file"], CARD_FILES[kind])
                            self.assertEqual(entry["bytes"], len(card.encode("utf-8")))
                            self.assertEqual(entry["sha256"], hashlib.sha256(card.encode("utf-8")).hexdigest())
                            self.assertEqual(entry["carrier"], "system_layer" if stage4 else "first_message")
                        self.assertEqual(record["instruction_delivery"], entries[-1])
                        self.assertEqual(sum(entry["bytes"] for entry in entries), 2 * len(card.encode("utf-8")), "the per-session sum is the entries")

    def wrapper(self, name: str, body: str) -> Path:
        """A python that behaves normally except for the commands `body` (shell `case` arms) takes over."""
        import stat
        import sys
        path = self.base / name
        path.write_text(f'#!/bin/bash\ncase "$*" in\n{body}\nesac\nexec {os.path.realpath(sys.executable)} "$@"\n', encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IEXEC)
        return path

    def launch_with(self, session, kind, *, python=None, env=None):
        """Like the base launch(), but with a chosen python and extra environment; returns (CLI argv, the runner's result)."""
        import subprocess
        import sys
        from tests.claude_auth_support import auth_arguments
        if self.capture.exists():
            self.capture.unlink()
        authority = SessionTokenAuthority(self.context)
        authority.prepare(session["id"])
        with self.served(authority) as endpoint, self.bootstrap_served(authority, endpoint) as bootstrap:
            completed = subprocess.run([
                "/bin/bash", str(ROOT / "scripts" / "run_managed_agent.sh"), "--root", str(self.context.code_root),
                "--data-root", str(self.context.data_root), "--workspace-root", str(self.context.workspace_root),
                "--python", str(python or os.path.realpath(sys.executable)), "--session-id", session["id"], "--kind", kind,
                "--manager-home", str(self.home), "--board-bootstrap", bootstrap, *auth_arguments(self.context, session),
            ], cwd=self.context.code_root, env={**os.environ, "HARNESS_EXECUTION_ROOT": str(self.context.code_root), **self.environment, **(env or {})},
                capture_output=True, text=True, timeout=60)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        # The CLI is probed (version, help, features) before the runner replaces itself with the agent: the agent is the last call.
        launched = [json.loads(line) for line in self.capture.read_text().splitlines() if json.loads(line) != ["--version"]]
        self.assertTrue(launched, "the agent started")
        return launched[-1], completed

    def test_a_recording_failure_is_said_out_loud_and_the_agent_still_starts(self):
        self.require_confinement()
        self.switch("claude", stage4=True)
        session = control.create(self.context, "claude_reviewer", fresh=True)
        broken = self.wrapper("python-without-recorder", '*note-instruction*) echo "recorder broke" >&2; exit 1;;')
        argv, completed = self.launch_with(session, "claude_reviewer", python=broken)
        self.assertIn("--append-system-prompt", argv)
        self.assertIn("the size of the rules delivered to this agent could not be recorded; the agent starts normally", completed.stderr)
        self.assertIsNone(self.record_of(session["id"]).get("instruction_delivery"))

    def test_gemini_gets_the_card_in_its_first_message_when_the_rules_file_cannot_carry_it_fresh_and_resumed(self):
        """The two fallbacks with Stage 4 on: the runtime preparation fails outright, and it succeeds but without Stage 4.
        (A third outcome, prepared with Stage 4, puts the card in Gemini's rules file instead and the message stays the kickoff.)"""
        self.require_confinement()
        home = self.base / "gemini-home"
        home.mkdir()
        agy = self.fake("fake-agy")
        environment = {"HARNESS_AGY_BIN": str(agy), "HOME": str(home)}
        self.environment["HARNESS_AGY_BIN"] = str(agy)
        runtime_dir = self.base / "gemini-runtime"
        runtime_dir.mkdir()
        outcomes = {
            "preparation fails": '*gemini_runtime.py\\ --manager-home*) cat >/dev/null; exit 1;;',
            "prepared without stage 4": f'*gemini_runtime.py\\ --manager-home*) cat >/dev/null; echo \'{{"add_dir": "{runtime_dir}", "stage4": false, "supervisor_args": [], "gate_timeout": 5}}\'; exit 0;;',
            "prepared with stage 4": f'*gemini_runtime.py\\ --manager-home*) cat >/dev/null; echo \'{{"add_dir": "{runtime_dir}", "stage4": true, "supervisor_args": [], "gate_timeout": 5}}\'; exit 0;;',
        }
        card = card_text("claude_cto")
        for outcome, arm in outcomes.items():
            self.switch("gemini", stage4=True)
            session = control.create(self.context, "claude_cto", fresh=True, settings_override={
                "delivery": {"provider": "codex", "model": "gpt-5.6-sol", "effort": "high"},
                "reviewer": {"provider": "claude", "model": "opus", "effort": "high"},
                "cto": {"provider": "gemini", "model": "gemini-3.8-flash-medium", "effort": "medium"}})
            python = self.wrapper(f"python-{abs(hash(outcome))}", arm)
            with self.subTest(outcome=outcome):
                argv, _ = self.launch_with(session, "claude_cto", python=python, env=environment)
                conversation_id = "00000000-0000-0000-0000-00000000beef"
                transcript = home / ".gemini" / "antigravity-cli" / "brain" / conversation_id / ".system_generated" / "logs"
                transcript.mkdir(parents=True, exist_ok=True)
                (transcript / "transcript.jsonl").write_text("{}\n", encoding="utf-8")
                control.record_cli_session(self.context, session["id"], conversation_id, "gemini")
                stage_relaunch(self.context, session["id"])
                resumed, _ = self.launch_with(session, "claude_cto", python=python, env=environment)
                self.assertIn("--conversation", resumed)
                entries = self.record_of(session["id"])["instruction_deliveries"]
                for mode, message, entry in (("fresh", argv[-1], entries[0]), ("resume", resumed[-1], entries[1])):
                    if outcome == "prepared with stage 4":
                        self.assertFalse(message.startswith(card), mode)
                        self.assertEqual(entry["carrier"], "system_layer")
                    else:
                        self.assertTrue(message.startswith(card + "\n\n"), f"{mode}: the first message carries the card")
                        self.assertEqual(entry["carrier"], "first_message")
                    self.assertEqual(entry["launch_mode"], mode)
                    self.assertEqual(entry["bytes"], len(card.encode("utf-8")))


# The base class's own tests run in test_stage4_system_layer; only the helpers (fake CLIs, switch, launch) are borrowed here.
for _name in [name for name in dir(Stage4RunnerTests) if name.startswith("test_")]:
    setattr(LaunchMatrixTests, _name, None)


class InstructionRecordTests(unittest.TestCase):
    def test_the_record_is_the_file_its_delivered_size_its_hash_the_carrier_and_the_launch_mode(self):
        record = control.instruction_record("directives/cards/CTO.md", b"abc", "first_message", "resume")
        self.assertEqual(record, {"file": "directives/cards/CTO.md", "bytes": 3, "sha256": hashlib.sha256(b"abc").hexdigest(),
                                  "carrier": "first_message", "launch_mode": "resume"})
        with self.assertRaises(ValueError):
            control.instruction_record("x", b"", "somewhere", "fresh")


if __name__ == "__main__":
    unittest.main()
