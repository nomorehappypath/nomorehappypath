# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""The Project brief: a capped summary of the project that a new task starts from (task 2026-11-08_project-brief).

Covers the unit-testable criteria of the spec (section 6, criteria 1-10 and 13): the brief's parts and budgets, who receives it, the
`project-history` command, the "Already in the project:" block, the fallbacks, the setting and the rule texts. Criteria 11 and 12 are
whole-app scenarios run with real agents, not tests here.

Run:  PYTHONPATH=. python3 -m unittest tests.test_project_brief -v
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from harness import board, board_mcp, board_surface, contract_check, control, git_process, global_settings, project_memory
from harness.board_surface import CommandGateway, SessionTokenAuthority
from harness.project_context import ProjectContext
from tests.environment_support import require_loopback
from tests.test_contract_first_checks import TASK, TEXT, _Fixture
from tests.test_requirements_contract import context

PLAIN_FIT = "In plain words:\nA page will do the new thing.\nNothing for you to do.\n\nAlready in the project:\nNothing related exists yet.\n\nObjective: do it."
REPO_ROOT = Path(__file__).resolve().parents[1]


def add_task(state, task, *, plain=None, direction="Build something useful", accepted_at=None, commit="abc1234def5678", failed=False):
    """Put one task into a synthetic board state: always its direction; a plain line when plain is given; an Accept when accepted_at is given
    (with its merge record unless failed, in which case the Accept was saved but its merge failed)."""
    state.setdefault("task_owner_directions", {})[task] = direction
    if plain:
        state.setdefault("requirement_confirmations", {})[task] = {
            "text": f"In plain words:\n{plain}\n\nAlready in the project:\nNothing related exists yet.\n\nObjective: build it."}
    if accepted_at:
        decision = {"decision": "accepted", "recorded_at": accepted_at}
        if failed:
            decision["git_acceptance"] = {"status": "failed"}
        else:
            state.setdefault("git_acceptances", {})[task] = {"commit": commit, "accepted_at": accepted_at}
        state.setdefault("release_decisions", {})[task] = decision
    return state


def day(number: int) -> str:
    """A distinct time for each number: one day later per number, so the newest task is the highest number."""
    return (datetime(2026, 1, 1, 10, tzinfo=timezone.utc) + timedelta(days=number)).isoformat()


def modify(state, task, words, at):
    """Add the board event the owner's Modify request leaves, to the hot log."""
    state.setdefault("events", []).append({"sequence": len(state.get("events", [])) + 1, "at": at, "kind": "requirements_modify", "task": task, "owner_change_request": words})


def make_repository(directory: Path, names) -> Path:
    """A real Git repository with one tracked file per name (staged, no commit needed)."""
    directory.mkdir(parents=True, exist_ok=True)
    git_process.run(["git", "init", "-q"], cwd=directory, check=True)
    for name in names:
        (directory / name).write_text("x", encoding="utf-8")
    git_process.run(["git", "add", "."], cwd=directory, check=True)
    return directory


def section(text: str, start: str, end: str) -> list[str]:
    """The lines of the brief between the heading line `start` and the heading line `end`."""
    lines = text.split("\n")
    first = lines.index(start) + 1
    return lines[first:lines.index(end, first)]


class BriefTests(unittest.TestCase):
    def tearDown(self):
        project_memory.configure(None)

    def test_only_real_acceptances_are_listed_newest_first_within_the_cap(self):  # criterion 1
        state = {}
        add_task(state, "older", plain="The older page now exists.", accepted_at="2026-11-01T10:00:00+00:00", commit="1111111aaaa")
        add_task(state, "newer", plain="The newer page now exists.", accepted_at="2026-11-03T10:00:00+00:00", commit="2222222bbbb")
        add_task(state, "broken", plain="This page never reached main.", accepted_at="2026-11-02T10:00:00+00:00", failed=True)
        add_task(state, "working", plain="Work is still going on here.")
        brief = project_memory.build_brief(state, {"description": "A small weather app."}, None)
        accepted = section(brief, "Accepted work, newest first:", "In progress:")
        self.assertEqual(len(accepted), 2)
        self.assertTrue(accepted[0].startswith("- newer (2026-11-03): The newer page now exists. [2222222]"), accepted)
        self.assertTrue(accepted[1].startswith("- older (2026-11-01): The older page now exists. [1111111]"), accepted)
        progress = section(brief, "In progress:", project_memory.BRIEF_HOW_TO_KNOW_MORE)
        self.assertTrue(any(line.startswith("- working: ") for line in progress), progress)
        self.assertTrue(any(line.startswith("- broken: ") for line in progress), "a saved Accept whose merge failed is not accepted work")
        self.assertNotIn("broken", "\n".join(accepted))
        self.assertLessEqual(len(brief), project_memory.brief_max_chars())

    def test_an_accept_with_no_merge_record_says_so_and_a_task_with_no_plain_block_uses_its_direction(self):  # criterion 1 (fallbacks)
        state = {}
        add_task(state, "old", direction="Add a  sign-in page\nwith email and password and a long tail " + "x" * 100, accepted_at="2026-10-01T10:00:00+00:00")
        state["git_acceptances"].clear()       # an Accept that is real because the task never used the governed Git path
        brief = project_memory.build_brief(state, {}, None)
        line = section(brief, "Accepted work, newest first:", "In progress:")[0]
        self.assertIn("Add a sign-in page with email and password and a long tail", line)
        self.assertIn("…", line, "the direction is cut to its first 80 characters")
        self.assertTrue(line.endswith("[merge not recorded]"), line)

    def test_the_minimum_cap_keeps_the_fixed_parts_the_newest_task_and_an_exact_count(self):  # criterion 2
        project_memory.configure({"project_brief_max_chars": 2000})
        state = {}
        for number in range(60):
            task = f"task-{number:02d}"
            add_task(state, task, plain=f"Feature number {number} of the project was added here and works.", accepted_at=day(number))
            modify(state, task, f"Please change thing {number} of the {task} page to say something different", f"2026-11-01T10:{number:02d}:00+00:00")
        add_task(state, "open-task")
        brief = project_memory.build_brief(state, {"description": "A project."}, None)
        self.assertLessEqual(len(brief), 2000)
        for part in ("What this project is:", "Folder map unavailable", "In progress:", "- open-task: ", project_memory.BRIEF_HOW_TO_KNOW_MORE):
            self.assertIn(part, brief)
        lines = section(brief, "Accepted work, newest first:", "In progress:")
        kept = [line for line in lines if line.startswith("- task-")]
        self.assertTrue(kept and kept[0].startswith("- task-59 ("), "the newest accepted task is kept first")
        collapsed = [line for line in lines if line.startswith("and ")]
        self.assertEqual(collapsed, [f"and {60 - len(kept)} earlier accepted tasks: `project-history --list`"])
        self.assertLess(len(kept), 60)

    def test_every_part_has_its_own_budget(self):  # criterion 3
        with tempfile.TemporaryDirectory() as base:
            short = make_repository(Path(base) / "short", [f"file{number:04d}" for number in range(60)])        # 8-character names
            state = {}
            for number in range(9):
                add_task(state, f"open-{number}")
            for number in range(30):
                modify(state, f"open-{number % 9}", f"Decision number {number} the owner made", f"2026-11-01T10:{number:02d}:00+00:00")
            brief = project_memory.build_brief(state, {"description": "d" * 2000}, short)
            self.assertIn("What this project is: " + "d" * 300 + "…\n", brief)
            self.assertNotIn("d" * 301, brief)
            names = [line for line in brief.split("\n") if line.startswith("Where things live:")][0]
            self.assertEqual(names.count("file0"), 25)
            self.assertTrue(names.endswith(" and 35 more."), names)
            progress = section(brief, "In progress:", "Decisions the owner made:")
            self.assertEqual(len([line for line in progress if line.startswith("- open-")]), 5)
            self.assertIn("and 4 more in progress", progress)
            decisions = section(brief, "Decisions the owner made:", project_memory.BRIEF_HOW_TO_KNOW_MORE)
            self.assertEqual(len(decisions), 10)
            long_names = make_repository(Path(base) / "long", [f"longname_number_{number:04d}" for number in range(60)])      # 20-character names
            names = [line for line in project_memory.build_brief({}, {}, long_names).split("\n") if line.startswith("Where things live:")][0]
            shown = names.count("longname_number_")
            self.assertEqual(shown, (300 + 2) // 22, "as many 20-character names as fit 300 characters")
            self.assertTrue(names.endswith(f" and {60 - shown} more."), names)

    def test_an_adopted_project_with_nothing_accepted_says_so(self):
        brief = project_memory.build_brief({}, {}, None)
        self.assertIn(project_memory.BRIEF_NO_ACCEPTED, brief)
        self.assertIn("Nothing is in progress.", brief)
        self.assertIn("No description recorded.", brief)

    def test_decisions_come_from_modify_words_and_non_recommended_answers_newest_first(self):  # criterion 13
        state = {}
        add_task(state, "shop", plain="The shop shows its prices now.", accepted_at="2026-11-01T10:00:00+00:00")
        modify(state, "shop", "Make the buy button green", "2026-11-02T09:00:00+00:00")
        state["requirement_proposals"] = {"shop": {"decided_at": "2026-11-02T12:00:00+00:00", "contract_check": {"answers": [
            {"question": 1, "text": "Which currency?", "answer": "euros", "recommended": False},
            {"question": 2, "text": "Which language?", "answer": "English", "recommended": True}]}}}
        decisions = section(project_memory.build_brief(state, {}, None), "Decisions the owner made:", project_memory.BRIEF_HOW_TO_KNOW_MORE)
        self.assertEqual(decisions, ["On shop: Which currency? -> euros", "On shop: Make the buy button green"])

    def test_a_normal_long_question_cannot_hide_the_selected_answer(self):  # criterion 13
        question = "Which currency should the shop show its prices in when a visitor browses from a country that has its own money? " * 2
        question = question[:185]
        self.assertEqual(len(question), 185)
        for accepted in (False, True):
            with self.subTest(accepted=accepted):
                state = {}
                add_task(state, "shop", plain="The shop shows its prices now.", accepted_at="2026-11-01T10:00:00+00:00" if accepted else None)
                state["requirement_proposals"] = {"shop": {"decided_at": "2026-11-02T12:00:00+00:00", "contract_check": {"answers": [
                    {"question": 1, "text": question, "answer": "euros", "recommended": False}]}}}
                decisions = section(project_memory.build_brief(state, {}, None), "Decisions the owner made:", project_memory.BRIEF_HOW_TO_KNOW_MORE)
                self.assertEqual(len(decisions), 1, decisions)
                self.assertIn("euros", decisions[0])
                self.assertTrue(decisions[0].endswith("-> euros"), decisions[0])
                self.assertLessEqual(len(decisions[0]), project_memory.BRIEF_DECISION_CHARS + len("…"))
        short = {"requirement_proposals": {"shop": {"decided_at": "2026-11-02T12:00:00+00:00", "contract_check": {"answers": [
            {"question": 1, "text": "Which currency?", "answer": "euros", "recommended": False}]}}}}
        self.assertEqual(project_memory._decision_rows(short, "shop"), [("2026-11-02T12:00:00+00:00", "On shop: Which currency? -> euros")])

    def test_a_decision_on_a_task_the_brief_does_not_keep_is_not_shown(self):  # criterion 13
        project_memory.configure({"project_brief_max_chars": 2000})
        state = {}
        for number in range(60):
            add_task(state, f"task-{number:02d}", plain=f"Feature number {number} of the project was added here and works.", accepted_at=day(number))
        modify(state, "task-00", "This decision belongs to a task the brief dropped", "2026-11-09T10:00:00+00:00")
        self.assertNotIn("belongs to a task the brief dropped", project_memory.build_brief(state, {}, None))


class FolderMapTests(unittest.TestCase):
    def test_an_unreadable_repository_is_one_plain_line_and_the_rest_of_the_brief_is_intact(self):  # criterion 8
        with tempfile.TemporaryDirectory() as base:
            for repository, reason in ((None, "no repository yet"), (Path(base) / "missing", "cannot be read"), (Path(base), "not a readable Git repository")):
                with self.subTest(repository=repository):
                    brief = project_memory.build_brief({}, {"description": "A project."}, repository)
                    line = [line for line in brief.split("\n") if line.startswith("Folder map unavailable:")]
                    self.assertEqual(len(line), 1, brief)
                    self.assertIn(reason, line[0])
                    self.assertIn("What this project is: A project.", brief)
                    self.assertIn(project_memory.BRIEF_HOW_TO_KNOW_MORE, brief)


class WhoGetsItTests(_Fixture):
    def test_begin_task_and_resume_task_carry_the_brief_and_no_poll_does(self):  # criteria 5, 8
        session = control.create(self.root, "codex_delivery")
        agent = board.register(self.root, "development", board.AWAITING_OWNER_DIRECTION, vendor="OpenAI", session_id=session["id"])
        board.record_owner_direction(self.root, session["id"], f"OWNER DIRECTION — {TASK}: build the thing.")
        begun = board.begin_task(self.root, agent["id"], TASK)
        self.assertEqual(begun["kind"], "task_begun")
        self.assertTrue(begun["project_brief"].startswith(project_memory.BRIEF_TITLE), begun["project_brief"])
        self.assertIn(f"- {TASK}: ", begun["project_brief"])
        stored = [event for event in board.snapshot(self.root)["events"] if event["kind"] == "task_begun"]
        self.assertNotIn("project_brief", stored[-1], "the brief rides on the result only; it is never stored")
        with board.locked_state(self.root) as state:
            state["agents"][agent["id"]].update({"active": False, "write_authority": False})
        successor = board.register(self.root, "development", board.AWAITING_OWNER_DIRECTION, vendor="OpenAI", session_id=control.create(self.root, "codex_delivery")["id"])
        resumed = board.resume_task(self.root, successor["id"], agent["id"], TASK)
        self.assertEqual(resumed["kind"], "task_resumed")
        self.assertIn(project_memory.BRIEF_TITLE, resumed["project_brief"])
        reviewer_session = control.create(self.root, "claude_reviewer")
        reviewer = board.register(self.root, "qa", "REVIEW_QUEUE", vendor="Anthropic", session_id=reviewer_session["id"])
        cto = board.register(self.root, "cto", "GLOBAL_MONITOR", vendor="Anthropic", session_id=control.create(self.root, "claude_cto")["id"])
        for who in (successor, reviewer, cto):
            self.assertNotIn(project_memory.BRIEF_TITLE, json.dumps(board.poll(self.root, who["id"]), default=str), who["role"])

    def test_a_brief_that_cannot_be_built_never_stops_the_task(self):  # criterion 8
        session = control.create(self.root, "codex_delivery")
        agent = board.register(self.root, "development", board.AWAITING_OWNER_DIRECTION, vendor="OpenAI", session_id=session["id"])
        board.record_owner_direction(self.root, session["id"], f"OWNER DIRECTION — {TASK}: build the thing.")
        with mock.patch.object(project_memory, "build_brief", side_effect=RuntimeError("the board was unreadable")):
            begun = board.begin_task(self.root, agent["id"], TASK)
        self.assertEqual(begun["project_brief"], "Project brief unavailable: the board was unreadable.")
        self.assertEqual(board.snapshot(self.root)["agents"][agent["id"]]["task"], TASK)

    def test_the_reviewer_gets_it_in_the_routed_message_and_in_contract_checks_before_confirmation(self):  # criterion 6
        delivery = self.delivery()
        session, reviewer = self.reviewer()
        board.record_requirement_proposal(self.root, delivery["id"], TEXT)
        wake = [item for item in control.take_instructions(self.root, session["id"]) if item.get("source") == "contract-check"]
        self.assertIn("CONTRACT CHECK ROUTED", wake[0]["text"])
        self.assertIn("\nPROJECT BRIEF:\n" + project_memory.BRIEF_TITLE, wake[0]["text"])
        pending = board.show_contract_checks(self.root, reviewer["id"], TASK)
        self.assertEqual(pending["pending_proposal"], TEXT)
        self.assertEqual(pending["status"], "reading")
        self.assertIn(project_memory.BRIEF_TITLE, pending["project_brief"])
        with self.assertRaisesRegex(ValueError, "no confirmed requirements"):
            board.show_contract_checks(self.root, delivery["id"])
        cto = board.register(self.root, "cto", "GLOBAL_MONITOR", vendor="Anthropic", session_id=control.create(self.root, "claude_cto")["id"])
        with self.assertRaisesRegex(ValueError, "no confirmed requirements"):
            board.show_contract_checks(self.root, cto["id"], TASK)

    def test_after_confirmation_contract_checks_carries_the_brief_for_the_reviewer_only(self):  # criterion 6
        delivery = self.delivery()
        _session, reviewer = self.reviewer()
        board.record_requirement_proposal(self.root, delivery["id"], TEXT)
        board.record_requirements_decision(self.root, TASK, "go_ahead", token=self.proposal()["contract_check"]["token"])
        board.record_requirement_confirmation(self.root, delivery["id"], TEXT)
        self.assertIn(project_memory.BRIEF_TITLE, board.show_contract_checks(self.root, reviewer["id"], TASK)["project_brief"])
        self.assertNotIn("project_brief", board.show_contract_checks(self.root, delivery["id"]))


class ProjectHistoryTests(_Fixture):
    def seed(self):
        """Twenty-five accepted tasks (so the list has two pages) and two awkward ones: one with a cold review summary, one with nothing recorded."""
        with board.locked_state(self.root) as state:
            for number in range(25):
                add_task(state, f"done-{number:02d}", plain=f"Feature number {number} was added and works.", accepted_at=f"2026-10-{1 + number:02d}T10:00:00+00:00", commit=f"{number:07d}fff")
            state["requirement_confirmations"]["done-00"]["text"] += "\nRequirement: it works."
            state["qa_request_index"] = {"qa-1": {"id": "qa-1", "task": "done-00", "phase": "final_acceptance", "status": "passed", "result": "passed", "completed_at": "2026-10-01T09:00:00+00:00"}}
            add_task(state, "bare")
            board._event(state, "requirements_modify", None, {"task": "done-00", "owner_change_request": "Please use the blue theme"})
        board._append_cold(self.root, "qa_requests", [{"id": "qa-1", "task": "done-00", "phase": "final_acceptance", "status": "passed", "result_summary": "Everything checked out."}])
        with board.locked_state(self.root) as state:
            state["events"] = []         # the Modify event is now only in the durable log (cold)
            state["requirement_proposals"] = {"done-00": {"contract_check": {"answers": [{"question": 1, "text": "Which theme?", "answer": "blue", "recommended": False}]}}}

    def test_the_list_pages_newest_first_and_the_task_view_reads_cold_records(self):  # criterion 4, 13
        agent = self.delivery()
        self.seed()
        first = board.show_project_history(self.root, agent["id"], list_all=True)
        self.assertEqual((first["page"], first["pages"], first["total"], len(first["tasks"])), (1, 2, 25, 20))
        self.assertEqual(first["tasks"][0], {"task": "done-24", "date": "2026-10-25", "plain": "Feature number 24 was added and works."})
        second = board.show_project_history(self.root, agent["id"], list_all=True, page=2)
        self.assertEqual([item["task"] for item in second["tasks"]], [f"done-{number:02d}" for number in range(4, -1, -1)])
        old = board.show_project_history(self.root, agent["id"], task="done-00")
        self.assertIn("Build something useful", old["owner_direction"])
        self.assertIn("Requirement: it works.", old["requirements"])
        self.assertEqual(old["merge"], {"commit": "0000000fff", "date": "2026-10-01"})
        self.assertEqual(old["final_review"], {"result": "passed", "summary": "Everything checked out."})
        self.assertEqual([item["words"] for item in old["owner_modify_requests"]], ["Please use the blue theme"], "a Modify that is cold is still returned")
        self.assertEqual(old["early_look_answers"], [{"question": "Which theme?", "answer": "blue", "recommended": False}])
        bare = board.show_project_history(self.root, agent["id"], task="bare")
        self.assertEqual(bare["requirements"], "not recorded")
        self.assertEqual(bare["merge"], {"commit": "merge not recorded", "date": "not recorded"})
        self.assertEqual(bare["final_review"], {"result": "not recorded", "summary": "not recorded"})
        self.assertEqual(bare["owner_modify_requests"], [])

    def test_it_asks_for_exactly_one_thing_and_knows_its_tasks(self):
        agent = self.delivery()
        for arguments in ({}, {"list_all": True, "task": "bare"}):
            with self.assertRaisesRegex(ValueError, "--list"):
                board.show_project_history(self.root, agent["id"], **arguments)
        with self.assertRaisesRegex(ValueError, "no task named nothing-here"):
            board.show_project_history(self.root, agent["id"], task="nothing-here")

    def test_the_cold_modify_is_absent_from_the_brief_but_the_early_look_answer_is_shown(self):  # criterion 13
        self.delivery()
        self.seed()
        brief = project_memory.build_brief(board.snapshot(self.root), {}, None)
        self.assertIn("On done-00: Which theme? -> blue", brief)
        self.assertNotIn("Please use the blue theme", brief)


class GatewayTests(unittest.TestCase):
    """The command is allowed for Delivery and the Reviewer, refused for the CTO, through the real surface and through MCP."""

    def setUp(self):
        require_loopback()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name)
        code = base / "code"
        code.mkdir()
        self.context = ProjectContext(code, base / "private-data", base / "workspaces")
        control.initialize(self.context)

    def session(self, kind, role, task):
        session = control.create(self.context, kind)
        authority = SessionTokenAuthority(self.context)
        authority.prepare(session["id"])
        control.attach(self.context, session["id"], os.getpid())
        agent = board.register(self.context, role, task, vendor=control.KINDS[kind]["vendor"], session_id=session["id"])
        return agent, authority.claim(session["id"], os.getpid()), CommandGateway(self.context, authority)

    @staticmethod
    def request(arguments, nonce):
        return {"protocol": board_surface.PROTOCOL_VERSION, "nonce": nonce, "operation": "project-history", "arguments": ["project-history", *arguments]}

    def test_roles_through_the_surface_and_mcp(self):  # criterion 5
        delivery, dtoken, dgateway = self.session("codex_delivery", "engineering", board.AWAITING_OWNER_DIRECTION)
        reviewer, rtoken, rgateway = self.session("claude_reviewer", "qa", "REVIEW_QUEUE")
        cto, ctoken, cgateway = self.session("claude_cto", "cto", "GLOBAL_MONITOR")
        with board.locked_state(self.context) as state:
            add_task(state, "done-1", plain="The first feature was added and works.", accepted_at="2026-10-01T10:00:00+00:00")
        for agent, token, gateway in ((delivery, dtoken, dgateway), (reviewer, rtoken, rgateway)):
            result = gateway.execute(token, self.request(["--agent", agent["id"], "--list"], 10))
            self.assertIn("done-1", json.dumps(result))
            result = gateway.execute(token, self.request(["--agent", agent["id"], "--task", "done-1"], 11))
            self.assertIn("The first feature", json.dumps(result), "the requested past task is kept, not replaced by the caller's own")
        with self.assertRaises(board_surface.SurfaceAuthorizationError):
            cgateway.execute(ctoken, self.request(["--agent", cto["id"], "--list"], 12))
        self.assertIn("project-history", board_surface.READ_ONLY_BOARD_OPERATIONS)
        self.assertEqual(board_surface.AUTHORIZATION_MATRIX["project-history"], frozenset({"engineering", "qa"}))
        names = {role: {tool["name"] for tool in board_mcp.tool_definitions(role)} for role in ("engineering", "qa", "cto")}
        self.assertIn("project-history", names["engineering"])
        self.assertIn("project-history", names["qa"])
        self.assertNotIn("project-history", names["cto"])
        self.assertEqual(board_mcp.argv_for("project-history", {"list": True, "page": 2}), ["project-history", "--list", "--page", "2"])


class FitBlockTests(unittest.TestCase):
    FULL = "Objective: build it."
    PLAIN = "In plain words:\nA page will do the new thing.\nNothing for you to do."
    FIT = "Already in the project:\nThe sign-in page already exists.\nThis change adds a link to it."

    def split(self, text):
        _, text = contract_check.split_plain_summary(text)
        return contract_check.split_fit_block(text)

    def test_a_valid_block_is_accepted_and_the_text_is_kept(self):  # criterion 7
        lines, whole = self.split(f"{self.PLAIN}\n\n{self.FIT}\n\n{self.FULL}")
        self.assertEqual(lines, ["The sign-in page already exists.", "This change adds a link to it."])
        self.assertTrue(whole.endswith(self.FULL))
        one_line, _ = self.split(f"{self.PLAIN}\n\nAlready in the project: Nothing related exists yet.\n\n{self.FULL}")
        self.assertEqual(one_line, ["Nothing related exists yet."])

    def test_refusals_name_the_block(self):  # criterion 7
        cases = {
            "missing": f"{self.PLAIN}\n\n{self.FULL}",
            "nothing after it": f"{self.PLAIN}\n\n{self.FIT}",
            "no lines": f"{self.PLAIN}\n\nAlready in the project:\n\n{self.FULL}",
            "four lines": f"{self.PLAIN}\n\nAlready in the project:\nOne line of text here.\nTwo lines of text here.\nThree lines of text here.\nFour lines of text here.\n\n{self.FULL}",
            "names a file": f"{self.PLAIN}\n\nAlready in the project:\nThe page lives in signin_page.py today.\n\n{self.FULL}",
            "names a function": f"{self.PLAIN}\n\nAlready in the project:\nThe page is drawn by render(user) today.\n\n{self.FULL}",
        }
        for name, text in cases.items():
            with self.subTest(name), self.assertRaises(ValueError) as raised:
                self.split(text)
            self.assertIn("Already in the project", str(raised.exception))

    def test_the_board_refuses_a_proposal_without_the_block_and_old_text_is_untouched(self):  # criterion 7
        with tempfile.TemporaryDirectory() as base:
            root = context(Path(base))
            session = control.create(root, "codex_delivery")
            agent = board.register(root, "development", board.AWAITING_OWNER_DIRECTION, vendor="OpenAI", session_id=session["id"])
            board.record_owner_direction(root, session["id"], f"OWNER DIRECTION — {TASK}: build the thing.")
            board.begin_task(root, agent["id"], TASK)
            with self.assertRaisesRegex(ValueError, "Already in the project"):
                board.record_requirement_proposal(root, agent["id"], f"{self.PLAIN}\n\n{self.FULL}", plain_required=True)
            self.assertNotIn(TASK, board.snapshot(root).get("requirement_proposals", {}))
            board.record_requirement_proposal(root, agent["id"], f"{self.PLAIN}\n\n{self.FIT}\n\n{self.FULL}", plain_required=True)
            self.assertEqual(board.snapshot(root)["requirement_proposals"][TASK]["status"], "awaiting_owner")
            # a proposal filed by the board's own callers, or confirmed before this rule, is never refused
            with board.locked_state(root) as state:
                state["requirement_proposals"].pop(TASK)
            board.record_requirement_proposal(root, agent["id"], f"{self.PLAIN}\n\n{self.FULL}")
            self.assertEqual(board.snapshot(root)["requirement_proposals"][TASK]["status"], "awaiting_owner")


class SettingTests(unittest.TestCase):
    def tearDown(self):
        project_memory.configure(None)

    def test_the_range_is_enforced_like_every_other_limit(self):  # criterion 9
        self.assertEqual(global_settings.PLUMBING_LIMITS["project_brief_max_chars"], (4000, 2000, 12000))
        self.assertEqual(global_settings.default_plumbing()["project_brief_max_chars"], 4000)
        for good in (2000, 12000):
            self.assertEqual(global_settings._validated_plumbing({"project_brief_max_chars": good})["project_brief_max_chars"], good)
        for bad in (1999, 12001, "4000", True):
            with self.subTest(bad), self.assertRaisesRegex(ValueError, "project_brief_max_chars must be a whole number from 2000 to 12000"):
                global_settings._validated_plumbing({"project_brief_max_chars": bad})

    def test_the_worker_installs_it_and_the_next_brief_uses_it(self):  # criterion 9
        self.assertIn('project_memory.configure(global_settings.load(settings_home).get("plumbing"))', (REPO_ROOT / "harness" / "project_worker.py").read_text(encoding="utf-8"))
        state = {}
        for number in range(60):
            add_task(state, f"task-{number:02d}", plain=f"Feature number {number} of the project was added here and works.", accepted_at=day(number))
        project_memory.configure({"project_brief_max_chars": 12000})
        big = project_memory.build_brief(state, {}, None)
        project_memory.configure({"project_brief_max_chars": 2000})
        small = project_memory.build_brief(state, {}, None)
        self.assertLessEqual(len(small), 2000)
        self.assertGreater(len(big), 2000)
        self.assertLessEqual(len(big), 12000)
        project_memory.configure({"project_brief_max_chars": 5})        # unusable: keeps the default
        self.assertEqual(project_memory.brief_max_chars(), project_memory.BRIEF_MAX_CHARS_DEFAULT)


class RuleTextTests(unittest.TestCase):
    def read(self, *parts):
        return (REPO_ROOT.joinpath(*parts)).read_text(encoding="utf-8")

    def test_the_cards_and_the_chapters_name_the_brief_the_code_and_the_block(self):  # criterion 10
        delivery = [line for line in self.read("directives", "cards", "DELIVERY.md").splitlines() if line.startswith("15. ")][0]
        self.assertIn("Project brief", delivery)
        self.assertIn("existing code", delivery)
        self.assertIn('"Already in the project:"', delivery)
        reviewer = [line for line in self.read("directives", "cards", "REVIEWER.md").splitlines() if line.startswith("32. ")][0]
        self.assertIn("duplicates or contradicts", reviewer)
        self.assertIn("project-history --list", reviewer)
        agent = self.read("directives", "AGENT.md")
        requirements = agent[agent.index("\n## requirements\n"):agent.index("\n## contract-first\n")]
        for needle in ("Project brief", "OPEN\nthe existing code", '"Already in the project:"', "project-history --task <id>"):
            self.assertIn(needle, requirements)
        contract_first = agent[agent.index("\n## contract-first\n"):]
        self.assertIn("PROJECT BRIEF", contract_first)
        self.assertIn("duplicates or contradicts an accepted task", contract_first)


if __name__ == "__main__":
    unittest.main()
