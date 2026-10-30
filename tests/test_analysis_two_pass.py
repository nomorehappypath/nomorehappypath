# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Code analysis as two equal passes, a cross-check, a CTO merge and one clean list (spec docs/specs/CODE_ANALYSIS_TWO_PASS.md, section 7).

One test group per acceptance criterion, on the real board: the Delivery agent, the Reviewer and the CTO are registered agents with sessions,
and every message the board sends is read from their instruction inboxes.

Run:  PYTHONPATH=. python3 -m unittest tests.test_analysis_two_pass -v
"""
from __future__ import annotations

import contextlib
import inspect
import io
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from harness import analysis_pass, board, board_surface, control, cto_wakes, story
from tests import test_analysis_lane_v2 as lane
from tests.test_contract_first_checks import TASK, TEXT
from tests.test_requirements_contract import context
from tests.test_task_kind_choice import agent_with_kind

D1 = "app/jobs.py:40|a retried job repeats the paid call|the customer is billed twice for one document"
D2 = "app/api/draft.py:80|the draft helper never checks the license state|a frozen customer keeps spending AI budget"
D3 = "app/store.py:12|the cache key ignores the tenant id|one tenant can read another tenant's rows"
R1 = "app/jobs.py:41|the paid call is not guarded against a restart|a restarted upload bills the customer again"
R2 = "app/queue.py:7|the worker never drops a poisoned message|one bad message blocks the whole queue"
EXAMINED = "every file under app/ and tests/, the three request flows and the worker, end to end"
EVIDENCE = "app/api/draft.py:77 already rejects a frozen account before the helper runs"


class Flow(unittest.TestCase):
    """A Delivery agent on a Code analysis task, one Reviewer from the other company and the CTO, all with sessions."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = context(Path(self._tmp.name))
        cto_wakes.configure({})
        self.addCleanup(cto_wakes.configure, {})
        self.cto_session = control.create(self.root, "claude_cto")
        self.cto = board.register(self.root, "cto", "GLOBAL_MONITOR", vendor="Anthropic", session_id=self.cto_session["id"])
        self.dev = agent_with_kind(self.root, "analysis")
        self.rev_session = control.create(self.root, "claude_reviewer")
        self.rev = board.register(self.root, "qa", "REVIEW_QUEUE", vendor="Anthropic", session_id=self.rev_session["id"])
        self.inbox_of = {"dev": self.dev["session_id"], "rev": self.rev_session["id"], "cto": self.cto_session["id"]}

    # -- helpers --------------------------------------------------------------------------------------------------------------------
    def inbox(self, who):
        return control.take_instructions(self.root, self.inbox_of[who])

    def drain(self):
        for who in self.inbox_of:
            self.inbox(who)

    def state(self):
        return board.snapshot(self.root)

    def record(self):
        """The stored record, unprojected (board.snapshot hides what the other side may not read yet)."""
        return board._read_state(self.root)["analysis_passes"][TASK]

    def go(self):
        board.record_requirement_proposal(self.root, self.dev["id"], TEXT)
        board.record_requirements_decision(self.root, TASK, "go_ahead")

    def add(self, who, *items):
        agent = self.dev if who == "dev" else self.rev
        return board.analysis_list(self.root, agent["id"], TASK, add=list(items))

    def close(self, who, examined=EXAMINED, none=""):
        agent = self.dev if who == "dev" else self.rev
        return board.analysis_list(self.root, agent["id"], TASK, done=True, examined=examined, none_reason=none)

    def lists_closed(self, dev_items=(D1, D2, D3), rev_items=(R1, R2)):
        self.go()
        self.add("dev", *dev_items)
        self.add("rev", *rev_items)
        self.close("dev")
        self.close("rev")

    def marks(self, who, *marks):
        agent = self.dev if who == "dev" else self.rev
        return board.analysis_crosscheck(self.root, agent["id"], TASK, list(marks))

    def both_checked(self):
        self.lists_closed()
        self.marks("dev", "R1=confirmed", f"R2=wrong|{EVIDENCE}")
        self.marks("rev", "D1=confirmed", f"D2=false alarm|{EVIDENCE}", "D3=confirmed")


class NoEarlyLookTests(Flow):
    def test_filing_the_requirements_starts_no_early_look_and_the_card_has_no_wait_choice(self):
        self.drain()
        board.record_requirement_proposal(self.root, self.dev["id"], TEXT)
        check = self.state()["requirement_proposals"][TASK]["contract_check"]
        self.assertEqual((check["status"], check["reviewer_id"]), ("not_checked", ""))
        self.assertNotIn("reading", check["status"])           # the card's "Wait for the review" button exists only while the check is reading
        self.assertIn("Code analysis", check["reason"])
        self.assertEqual(self.inbox("rev"), [], "the Reviewer is told nothing about this task before Go ahead")
        self.assertEqual(self.state().get("analysis_passes", {}), {})

    def test_begin_task_sends_the_reviewer_nothing(self):
        self.assertEqual(self.inbox("rev"), [])
        self.assertFalse([e for e in self.state()["events"] if e["kind"] in {"analysis_started", "analysis_reviewer_started"}])

    def test_a_development_task_still_gets_the_early_look(self):
        other = tempfile.TemporaryDirectory()
        self.addCleanup(other.cleanup)
        root = context(Path(other.name))
        agent = agent_with_kind(root, "development", task="DEV-TASK")
        session = control.create(root, "claude_reviewer")
        board.register(root, "qa", "REVIEW_QUEUE", vendor="Anthropic", session_id=session["id"])
        control.take_instructions(root, session["id"])
        board.record_requirement_proposal(root, agent["id"], TEXT)
        self.assertEqual(board.snapshot(root)["requirement_proposals"]["DEV-TASK"]["contract_check"]["status"], "reading")
        self.assertEqual([i["source"] for i in control.take_instructions(root, session["id"])], ["contract-check"])


class GoAheadTests(Flow):
    def test_go_ahead_sends_the_reviewer_the_start_message_with_the_accepted_requirements(self):
        self.drain()
        self.go()
        got = self.inbox("rev")
        self.assertEqual([i["source"] for i in got], ["analysis-start"])
        for piece in (TASK, TEXT, "analysis-list --task " + TASK, "--done --examined", "analysis-crosscheck", "Do not ask for or read the Dev's list"):
            self.assertIn(piece, got[0]["text"])
        self.assertEqual(self.record()["reviewer_id"], self.rev["id"])
        self.assertTrue([e for e in self.state()["events"] if e["kind"] == "analysis_started"], "the Reviewer's poll also shows it")
        decision = [i for i in self.inbox("dev") if i["source"] == "owner-requirements-go_ahead"]
        self.assertEqual(len(decision), 1, "the Dev starts as today")
        self.assertIn("confirm-requirements", decision[0]["text"])
        self.assertIn(f"analysis-list --task {TASK}", decision[0]["text"])

    def test_a_development_or_spec_go_ahead_changes_nothing(self):
        for kind in ("development", "spec"):
            with self.subTest(kind=kind):
                other = tempfile.TemporaryDirectory()
                self.addCleanup(other.cleanup)
                root = context(Path(other.name))
                agent = agent_with_kind(root, kind, task="K-TASK")
                session = control.create(root, "claude_reviewer")
                board.register(root, "qa", "REVIEW_QUEUE", vendor="Anthropic", session_id=session["id"])
                control.take_instructions(root, session["id"])
                board.record_requirement_proposal(root, agent["id"], TEXT)
                token = board.snapshot(root)["requirement_proposals"]["K-TASK"]["contract_check"]["token"]
                board.record_requirements_decision(root, "K-TASK", "go_ahead", token=token)
                self.assertFalse(board.snapshot(root).get("analysis_passes"))
                self.assertNotIn("analysis-start", [i["source"] for i in control.take_instructions(root, session["id"])])
                routed = [i for i in control.take_instructions(root, agent["session_id"]) if i["source"] == "owner-requirements-go_ahead"]
                self.assertNotIn("analysis-list", routed[0]["text"] if routed else "")

    def test_no_live_reviewer_still_starts_the_dev_and_any_other_company_reviewer_may_file_later(self):
        other = tempfile.TemporaryDirectory()
        self.addCleanup(other.cleanup)
        root = context(Path(other.name))
        agent = agent_with_kind(root, "analysis")
        board.record_requirement_proposal(root, agent["id"], TEXT)
        board.record_requirements_decision(root, TASK, "go_ahead")
        self.assertEqual(board.snapshot(root)["analysis_passes"][TASK]["reviewer_id"], "")
        late = board.register(root, "qa", "REVIEW_QUEUE", vendor="Anthropic")
        board.analysis_list(root, late["id"], TASK, add=[R1])
        self.assertEqual(board._read_state(root)["analysis_passes"][TASK]["reviewer_id"], late["id"])


class ListTests(Flow):
    def test_items_are_kept_and_numbered_for_each_side(self):
        self.go()
        self.assertEqual(self.add("dev", D1)["added"], ["D1"])
        self.assertEqual(self.add("dev", D2, D3)["added"], ["D2", "D3"])
        self.assertEqual(self.add("rev", R1)["added"], ["R1"])
        sides = self.record()["sides"]
        self.assertEqual([i["id"] for i in sides["delivery"]["items"]], ["D1", "D2", "D3"])
        self.assertEqual([i["id"] for i in sides["reviewer"]["items"]], ["R1"])
        first = dict(sides["delivery"]["items"][0])
        self.add("dev", D1)                          # the same words again is a NEW item; an existing one is never touched
        self.assertEqual(self.record()["sides"]["delivery"]["items"][0], first)

    def test_editing_or_removing_an_item_is_not_possible(self):
        self.assertFalse({"edit", "remove", "delete", "replace"} & set(inspect.signature(board.analysis_list).parameters))
        parser = board.build_parser()
        for flag in ("--remove", "--edit"):
            with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
                parser.parse_args(["analysis-list", "--agent", "a", "--task", "T", flag, "D1"])
        args = parser.parse_args(["analysis-list", "--agent", "a", "--task", "T", "--add", D1, "--add", D2, "--done", "--examined", EXAMINED])
        self.assertEqual((args.add, args.done), ([D1, D2], True))
        self.assertEqual(parser.parse_args(["analysis-crosscheck", "--agent", "a", "--task", "T", "--mark", "R1=confirmed"]).mark, ["R1=confirmed"])
        self.assertEqual(parser.parse_args(["analysis-merge", "--agent", "a", "--task", "T", "--group", "D2,R4"]).group, ["D2,R4"])

    def test_a_list_has_no_cap(self):
        """2026-10-29 live run: a cap of 60 made the builder trim lower-severity findings from a large codebase's list."""
        self.go()
        many = [f"app/m{n}.py:{n}|problem number {n} in the module|the customer sees a wrong result in case {n}" for n in range(1, 121)]
        self.add("dev", *many[:70])
        self.add("dev", *many[70:])
        self.assertEqual(len(self.record()["sides"]["delivery"]["items"]), 120)

    def test_a_closed_list_takes_no_more_and_cannot_be_closed_again(self):
        self.go()
        self.add("dev", D1)
        self.close("dev")
        with self.assertRaisesRegex(ValueError, "closed"):
            self.add("dev", D2)
        with self.assertRaisesRegex(ValueError, "already closed"):
            self.close("dev")
        self.assertEqual(len(self.record()["sides"]["delivery"]["items"]), 1)

    def test_done_needs_an_examined_text_of_forty_characters(self):
        self.go()
        self.add("dev", D1)
        for short in ("", "everything", "x" * 39):
            with self.assertRaisesRegex(ValueError, "at least 40 characters"):
                self.close("dev", examined=short)
        self.assertFalse(self.record()["sides"]["delivery"]["closed_at"])
        self.close("dev", examined="x" * 40)
        self.assertTrue(self.record()["sides"]["delivery"]["closed_at"])

    def test_an_empty_list_needs_a_reason(self):
        self.go()
        with self.assertRaisesRegex(ValueError, "add at least one issue, or give `--none`"):
            self.close("dev")
        with self.assertRaisesRegex(ValueError, "at least 20 characters"):
            self.close("dev", none="short")
        self.close("dev", none="followed the three flows end to end and found each guarded")
        self.assertEqual(self.record()["sides"]["delivery"]["items"], [])

    def test_malformed_issues_are_refused_whole_in_plain_words(self):
        self.go()
        bad = {"only|two": "each issue is", "nofile|a long enough statement|a long enough reason": "file:line", "a.py:1|short|too short": "12 to 600"}
        for issue, words in bad.items():
            with self.subTest(issue=issue), self.assertRaisesRegex(ValueError, words):
                self.add("dev", D1, issue)
        self.assertEqual(self.record()["sides"]["delivery"]["items"], [], "a call that fails adds none of its items")

    def test_only_the_tasks_delivery_agent_and_a_reviewer_of_the_other_company_take_part(self):
        with self.assertRaisesRegex(ValueError, "no Code analysis is running"):
            self.add("dev", D1)
        self.go()
        same_vendor = board.register(self.root, "qa", "REVIEW_QUEUE", vendor="OpenAI")
        with self.assertRaisesRegex(ValueError, "other AI company"):
            board.analysis_list(self.root, same_vendor["id"], TASK, add=[R1])
        with self.assertRaisesRegex(ValueError, "other AI company"):
            board.analysis_list(self.root, self.cto["id"], TASK, add=[R1])
        with self.assertRaisesRegex(ValueError, "no Code analysis is running"):
            board.analysis_list(self.root, self.dev["id"], "OTHER-TASK", add=[D1])


class HiddenUntilBothClosedTests(Flow):
    SECRET_R = "the worker never drops a poisoned message"
    SECRET_D = "the cache key ignores the tenant id"

    def everything_an_agent_can_read(self, who):
        agent = self.dev if who == "dev" else self.rev
        polled = board.poll(self.root, agent["id"])
        return json.dumps([polled, board.analysis_list(self.root, agent["id"], TASK), self.state()["analysis_passes"]], default=str)

    def test_the_closed_side_cannot_read_the_open_side_and_vice_versa(self):
        self.go()
        self.add("dev", D3)
        self.add("rev", R2)
        self.close("dev")                                   # the Dev is closed, the Reviewer still reading
        shown = board.analysis_list(self.root, self.dev["id"], TASK)
        self.assertTrue(shown["yours"]["closed"])
        self.assertEqual(shown["other"], {"closed": False, "note": "hidden until both lists are closed"})
        self.assertNotIn(self.SECRET_R, self.everything_an_agent_can_read("dev"))
        self.assertNotIn(self.SECRET_D, self.everything_an_agent_can_read("rev"))     # the open side cannot read the closed list either
        self.assertEqual(board.analysis_list(self.root, self.rev["id"], TASK)["other"]["note"], "hidden until both lists are closed")
        with self.assertRaisesRegex(ValueError, "not available until both lists are closed"):
            self.marks("dev", "R1=confirmed")
        projected = self.state()["analysis_passes"][TASK]["sides"]["reviewer"]
        self.assertEqual(projected["items"], [{"id": "R1"}], "the board's snapshot keeps only the count and the numbers of an open list")

    def test_no_event_message_carries_an_issue(self):
        self.lists_closed(dev_items=(D3,), rev_items=(R2,))
        text = json.dumps([e for e in self.state()["events"] if str(e["kind"]).startswith("analysis_")])
        for secret in (self.SECRET_R, self.SECRET_D, "app/queue.py", "app/store.py"):
            self.assertNotIn(secret, text)

    def test_once_both_are_closed_each_side_can_read_the_other(self):
        self.lists_closed(dev_items=(D3,), rev_items=(R2,))
        self.assertIn(self.SECRET_R, json.dumps(board.analysis_list(self.root, self.dev["id"], TASK)))
        self.assertIn(self.SECRET_D, json.dumps(board.analysis_list(self.root, self.rev["id"], TASK)))


class CrossCheckTests(Flow):
    def test_each_side_is_sent_the_others_list_when_both_close(self):
        self.go()
        self.add("dev", D1, D2)
        self.add("rev", R1)
        self.close("dev")
        self.drain()
        self.close("rev")
        to_dev, to_rev = self.inbox("dev"), self.inbox("rev")
        self.assertEqual(([i["source"] for i in to_dev], [i["source"] for i in to_rev]), (["analysis-lists"], ["analysis-lists"]))
        self.assertIn("R1 app/jobs.py:41", to_dev[0]["text"])
        self.assertNotIn("app/api/draft.py:80", to_dev[0]["text"])
        self.assertIn("D1 app/jobs.py:40", to_rev[0]["text"])
        self.assertIn("D2 app/api/draft.py:80", to_rev[0]["text"])
        self.assertNotIn("app/jobs.py:41", to_rev[0]["text"])
        self.assertIn(f"analysis-crosscheck --task {TASK}", to_rev[0]["text"])

    def test_a_cross_check_that_misses_an_item_or_repeats_is_refused(self):
        self.lists_closed()
        with self.assertRaisesRegex(ValueError, "not yet marked: R2"):
            self.marks("dev", "R1=confirmed")
        with self.assertRaisesRegex(ValueError, "not an item of the list"):
            self.marks("dev", "R1=confirmed", "R2=confirmed", "D1=confirmed")
        with self.assertRaisesRegex(ValueError, "marked twice"):
            self.marks("dev", "R1=confirmed", "R1=confirmed", "R2=confirmed")
        with self.assertRaisesRegex(ValueError, "needs your evidence"):
            self.marks("dev", "R1=confirmed", "R2=wrong")
        with self.assertRaisesRegex(ValueError, "needs your evidence"):
            self.marks("dev", "R1=confirmed", "R2=false alarm|no location here at all")
        with self.assertRaisesRegex(ValueError, "mark R1 as"):
            self.marks("dev", "R1=maybe", "R2=confirmed")
        self.assertFalse(self.record()["sides"]["delivery"]["checked_at"], "a refused submission records nothing")
        self.assertEqual(self.marks("dev", "R1=confirmed", f"R2=wrong|{EVIDENCE}")["marked"], 2)
        with self.assertRaisesRegex(ValueError, "already submitted"):
            self.marks("dev", "R1=confirmed", "R2=confirmed")

    def test_the_dev_marks_the_reviewers_items_and_the_reviewer_the_devs(self):
        self.lists_closed()
        with self.assertRaisesRegex(ValueError, "not an item of the list"):
            self.marks("rev", "R1=confirmed", "R2=confirmed")
        self.marks("rev", "D1=confirmed", "D2=confirmed", "D3=confirmed")
        self.assertEqual(sorted(self.record()["sides"]["reviewer"]["marks"]), ["D1", "D2", "D3"])

    def test_the_cto_is_woken_once_with_the_reason_analysis_merge(self):
        self.lists_closed()
        self.drain()
        self.marks("dev", "R1=confirmed", f"R2=wrong|{EVIDENCE}")
        self.assertEqual(self.inbox("cto"), [], "one cross-check is not enough")
        self.marks("rev", "D1=confirmed", f"D2=false alarm|{EVIDENCE}", "D3=confirmed")
        wake = self.inbox("cto")
        self.assertEqual([i["source"] for i in wake], ["analysis-merge"])
        for piece in ("analysis merge", TASK, "D2 app/api/draft.py:80", "R2 app/queue.py:7", "false alarm", "analysis-merge --agent " + self.cto["id"], "judge no item true or false"):
            self.assertIn(piece, wake[0]["text"])
        self.assertEqual(self.state()["cto_wakes"]["by_reason"], {"analysis_merge": 1})
        for _ in range(3):
            board.mark_stalled(self.root)
            board.poll(self.root, self.cto["id"])
        self.assertEqual(self.state()["cto_wakes"]["by_reason"].get("analysis_merge"), 1)
        self.assertEqual([i for i in self.inbox("cto") if i["source"] == "analysis-merge"], [])

    def test_a_cto_started_after_both_cross_checks_is_given_the_merge_once(self):
        with board.locked_state(self.root) as state:
            state["agents"][self.cto["id"]]["active"] = False          # no CTO is registered while the lists are made and checked
        self.lists_closed()
        self.marks("dev", "R1=confirmed", f"R2=wrong|{EVIDENCE}")
        self.marks("rev", "D1=confirmed", f"D2=false alarm|{EVIDENCE}", "D3=confirmed")
        self.assertEqual(analysis_pass.stage(self.record()), "merge")
        self.assertEqual([i for i in cto_wakes.open_items(self.state()) if i["reason"] == "analysis_merge"], [])
        self.assertIn("the CTO has to merge", board._analysis_clean_list_problem(board._read_state(self.root), TASK))
        late_session = self.cto_session                                  # the owner starts the CTO now (a fresh registration)
        late = board.register(self.root, "cto", "GLOBAL_MONITOR", vendor="Anthropic", session_id=late_session["id"])
        self.assertNotEqual(late["id"], self.cto["id"])
        got = []
        for _ in range(4):
            board.mark_stalled(self.root)
            board.poll(self.root, late["id"])
            got += [i for i in control.take_instructions(self.root, late_session["id"])]
        merges = [i for i in got if "analysis merge" in i["text"]]
        self.assertEqual(len(merges), 1, "exactly one merge instruction reaches the late CTO, however often it is polled")
        for piece in ("D2 app/api/draft.py:80", "R2 app/queue.py:7", "false alarm", "analysis-merge --agent " + late["id"], "judge no item true or false",
                      "also within one list"):
            self.assertIn(piece, merges[0]["text"])
        items = [i for i in cto_wakes.open_items(self.state()) if i["reason"] == "analysis_merge"]
        self.assertEqual(len(items), 1)
        self.assertEqual(self.state()["cto_wakes"]["by_reason"].get("analysis_merge"), 1)
        board.analysis_merge(self.root, late["id"], TASK, ["D1,R1"])
        self.assertIsNotNone(self.record()["clean"])
        self.assertEqual([i for i in cto_wakes.open_items(self.state()) if i["reason"] == "analysis_merge"], [])
        for _ in range(3):
            board.mark_stalled(self.root)
        self.assertEqual([i for i in control.take_instructions(self.root, late_session["id"]) if "analysis merge" in i["text"]], [])
        self.assertEqual(self.state()["cto_wakes"]["by_reason"].get("analysis_merge"), 1)

    def test_a_merge_already_filed_is_not_opened_again_by_the_watchdog(self):
        self.both_checked()
        board.analysis_merge(self.root, self.cto["id"], TASK, [])
        for _ in range(3):
            board.mark_stalled(self.root)
        self.assertEqual([i for i in cto_wakes.open_items(self.state()) if i["reason"] == "analysis_merge"], [])

    def test_the_merge_wake_says_to_group_within_one_list_too_and_the_cto_texts_agree(self):
        self.both_checked()
        text = board._analysis_merge_text(board._read_state(self.root), self.record(), TASK, self.cto["id"])
        self.assertIn("between the two lists and also within one list", text)
        self.assertIn("within one list", Path("directives/CTO.md").read_text(encoding="utf-8"))
        self.assertIn("also two of one list", Path("directives/cards/CTO.md").read_text(encoding="utf-8"))

    def test_the_reason_has_a_plain_name(self):
        self.assertIn("analysis_merge", cto_wakes.REASONS)
        self.assertEqual(cto_wakes.PLAIN["analysis_merge"], "an analysis merge")


class MergeTests(Flow):
    def merge(self, *groups, agent=None):
        return board.analysis_merge(self.root, (agent or self.cto)["id"], TASK, list(groups))

    def test_an_item_in_two_groups_or_an_unknown_id_is_refused_and_nothing_is_stored(self):
        self.both_checked()
        for groups, words in ((("D1,R1", "D1,R2"), "in two groups"), (("D1,D9",), "not an item"), (("D1",), "two or more"), (("D1,D1",), "two or more")):
            with self.subTest(groups=groups), self.assertRaisesRegex(ValueError, words):
                self.merge(*groups)
        self.assertIsNone(self.record()["clean"])
        self.assertEqual(analysis_pass.stage(self.record()), "merge")

    def test_only_the_cto_and_only_when_both_cross_checks_are_in(self):
        self.lists_closed()
        with self.assertRaisesRegex(ValueError, "waits until both lists are closed and both cross-checks"):
            self.merge()
        self.marks("dev", "R1=confirmed", "R2=confirmed")
        self.marks("rev", "D1=confirmed", "D2=confirmed", "D3=confirmed")
        for agent in (self.dev, self.rev):
            with self.assertRaisesRegex(ValueError, "only the active CTO"):
                self.merge(agent=agent)

    def test_the_clean_list_holds_every_item_confirmed_plain_disputed_marked_and_groups_once(self):
        self.both_checked()
        self.drain()
        self.merge("D1,R2")                       # one group holding a confirmed member (D1) and a disputed one (R2)
        clean = self.record()["clean"]
        sources = [i for entry in clean for i in entry["sources"]]
        self.assertEqual(sorted(sources), ["D1", "D2", "D3", "R1", "R2"], "every item appears exactly once")
        by_source = {tuple(entry["sources"]): entry for entry in clean}
        group = by_source[("D1", "R2")]
        marks = {m["id"]: (m["mark"], m["disputed"]) for m in group["members"]}
        self.assertEqual(marks, {"D1": ("confirmed", False), "R2": ("wrong", True)})
        self.assertFalse(group["disputed"], "a group with a confirmed member side by side with a disputed one stays in the list plain")
        disputed = by_source[("D2",)]
        self.assertTrue(disputed["disputed"])
        member = disputed["members"][0]
        self.assertIn("never checks the license state", member["wrong"])          # the builder's words
        self.assertEqual(member["evidence"], EVIDENCE)                           # the reviewer's evidence
        for plain in (("D3",), ("R1",)):
            self.assertEqual((by_source[plain]["disputed"], by_source[plain]["members"][0]["mark"]), (False, "confirmed"))
        counts = analysis_pass.counts(clean)
        self.assertEqual((counts["items"], counts["disputed"], counts["sources"]), (4, 1, 5))
        self.assertEqual(analysis_pass.stage(self.record()), "clean")

    def test_the_clean_list_goes_to_delivery_and_the_wake_item_is_closed(self):
        self.both_checked()
        self.drain()
        self.merge()
        sent = self.inbox("dev")
        self.assertEqual([i["source"] for i in sent], ["analysis-clean-list"])
        text = sent[0]["text"]
        for piece in ("5 item(s), 2 disputed", "C1 [D1]", "DISPUTED", "false alarm according to the reviewer", EVIDENCE, "Write EVERY item into FINDINGS.md", "withdraw only your own disputed item"):
            self.assertIn(piece, text)
        self.assertEqual(self.inbox("rev"), [], "the Reviewer gets the clean list in its review brief")
        self.assertEqual([i for i in cto_wakes.open_items(self.state()) if i["reason"] == "analysis_merge"], [])
        with self.assertRaisesRegex(ValueError, "already merged"):
            self.merge()

    def test_a_merge_with_no_duplicates_is_allowed(self):
        self.both_checked()
        self.merge()
        self.assertEqual(len(self.record()["clean"]), 5)


class FoundWhileCheckingTests(Flow):
    """Owner-added 2026-10-29: a side may add a problem neither list has, in its one cross-check call."""
    F1 = "app/mail.py:9|a failed send is logged as sent|the customer never learns the notice did not arrive"
    F2 = "app/export.py:55|the export ignores the tenant filter|one tenant downloads another tenant's rows"

    def check(self, who, marks, add=()):
        agent = self.dev if who == "dev" else self.rev
        return board.analysis_crosscheck(self.root, agent["id"], TASK, list(marks), list(add))

    def dev_check(self, add=()):
        return self.check("dev", ["R1=confirmed", "R2=confirmed"], add)

    def rev_check(self, add=()):
        return self.check("rev", ["D1=confirmed", "D2=confirmed", "D3=confirmed"], add)

    def test_added_items_continue_the_adders_numbering_and_are_flagged(self):
        self.lists_closed()
        self.assertEqual(self.dev_check([self.F1, self.F2])["added"], ["D4", "D5"])
        items = self.record()["sides"]["delivery"]["items"]
        self.assertEqual([i["id"] for i in items], ["D1", "D2", "D3", "D4", "D5"])
        self.assertEqual([bool(i.get("found_while_checking")) for i in items], [False, False, False, True, True])
        self.assertEqual(self.record()["sides"]["delivery"]["marks"].keys(), {"R1", "R2"})
        self.assertEqual(self.rev_check([self.F2])["added"], ["R3"])

    def test_the_submission_still_has_to_mark_every_item_and_a_bad_add_refuses_everything(self):
        self.lists_closed()
        with self.assertRaisesRegex(ValueError, "not yet marked: R2"):
            self.check("dev", ["R1=confirmed"], [self.F1])
        with self.assertRaisesRegex(ValueError, "file:line|each issue is"):
            self.dev_check(["no location|something wrong here|why it matters here"])
        with self.assertRaisesRegex(ValueError, "each issue is"):
            self.dev_check([self.F1, "app/x.py:1|too short"])
        side = self.record()["sides"]["delivery"]
        self.assertEqual((len(side["items"]), side["checked_at"], side["marks"]), (3, "", {}), "a refused submission records nothing")
        self.dev_check([self.F1])
        with self.assertRaisesRegex(ValueError, "already submitted"):
            self.dev_check([self.F2])
        self.assertEqual(len(self.record()["sides"]["delivery"]["items"]), 4)

    def test_the_other_side_does_not_mark_it_and_cannot_read_it_before_the_merge(self):
        self.lists_closed()
        self.dev_check([self.F1])
        self.drain()
        shown = json.dumps(board.analysis_list(self.root, self.rev["id"], TASK))
        self.assertNotIn("failed send is logged", shown)
        self.assertNotIn("failed send is logged", json.dumps([board.poll(self.root, self.rev["id"]), self.state()["analysis_passes"]], default=str))
        self.assertIn("D3", shown)
        with self.assertRaisesRegex(ValueError, "not an item of the list"):
            self.check("rev", ["D1=confirmed", "D2=confirmed", "D3=confirmed", "D4=confirmed"])
        self.rev_check()
        self.assertIn("failed send is logged", json.dumps(board.analysis_list(self.root, self.rev["id"], TASK)))

    def test_it_reaches_the_cto_the_merge_and_the_clean_list_plain_with_its_label(self):
        self.lists_closed()
        self.drain()
        self.dev_check([self.F1])
        self.rev_check([self.F2])
        wake = self.inbox("cto")[0]["text"]
        self.assertIn("D4 app/mail.py:9", wake)
        self.assertIn("R3 app/export.py:55", wake)
        self.assertIn("found while checking", wake)
        board.analysis_merge(self.root, self.cto["id"], TASK, ["D4,R3"])
        clean = self.record()["clean"]
        self.assertEqual(sorted(i for e in clean for i in e["sources"]), ["D1", "D2", "D3", "D4", "R1", "R2", "R3"])
        group = next(e for e in clean if "D4" in e["sources"])
        self.assertEqual(group["sources"], ["D4", "R3"])
        self.assertFalse(group["disputed"])
        self.assertTrue(all(m["found_while_checking"] and not m["disputed"] and not m["mark"] for m in group["members"]))
        self.assertEqual(analysis_pass.counts(clean)["items"], 6)
        text = analysis_pass.clean_text(clean)
        self.assertIn("found while checking", text)
        self.assertNotIn("DISPUTED", text)
        from harness import standing_checks
        self.assertIn("M = 6", standing_checks.clean_list_problem("Clean list items in the report: 6 of 5", 6))
        self.assertEqual(standing_checks.clean_list_problem("Clean list items in the report: 6 of 6", 6), "")

    def test_the_hand_over_message_and_the_command_say_so(self):
        self.go()
        self.add("dev", D1)
        self.add("rev", R1)
        self.close("dev")
        self.drain()
        self.close("rev")
        for who in ("dev", "rev"):
            text = self.inbox(who)[0]["text"]
            self.assertIn("a real problem that neither list has", text)
            self.assertIn(f"analysis-crosscheck --task {TASK}", text)
            self.assertIn("--add", text)

    def test_a_write_code_task_is_unaffected(self):
        self.assertNotIn("found_while_checking", json.dumps(analysis_pass.new_record("T", "t")))
        record = analysis_pass.new_record("T", "t")
        self.assertEqual(analysis_pass.add_items(record, "delivery", [D1], "t")[0].get("found_while_checking"), None)


class ReviewRequestGateTests(lane.Fixture):
    """The Dev's review request is refused until the clean list exists; then the review proceeds as today."""

    def task(self):
        return board.snapshot(self.root)["agents"][self.dev["id"]]["task"]

    def start_analysis(self):
        with board.locked_state(self.root) as state:
            task = state["agents"][self.dev["id"]]["task"]
            state.setdefault("task_kinds", {})[task] = "analysis"
            board._start_analysis_pass(state, task, state["agents"][self.dev["id"]], "the accepted requirements", board.now())

    def test_no_clean_list_no_review_request_and_each_stage_says_what_is_missing(self):
        self.start_analysis()
        with self.assertRaisesRegex(ValueError, "cannot go to review before the clean list exists: both lists must be closed first"):
            self.request()
        task = self.task()
        with board.locked_state(self.root) as state:
            record = state["analysis_passes"][task]
            for side in record["sides"].values():
                side.update({"closed_at": "t", "examined": "x" * 40})
        with self.assertRaisesRegex(ValueError, "both cross-checks must be in first"):
            self.request()
        with board.locked_state(self.root) as state:
            for side in state["analysis_passes"][task]["sides"].values():
                side["checked_at"] = "t"
        with self.assertRaisesRegex(ValueError, "the CTO has to merge"):
            self.request()
        self.assertFalse([r for r in self.state()["qa_requests"].values()], "a refused request leaves nothing behind")

    def test_with_the_clean_list_the_review_proceeds_and_the_brief_carries_it(self):
        self.start_analysis()
        clean = [{"id": "C1", "sources": ["D1", "R1"], "disputed": False, "members": [
            {"id": "D1", "side": "delivery", "location": "app/jobs.py:40", "wrong": "a retried job repeats the paid call", "matters": "billed twice", "mark": "confirmed", "evidence": "", "disputed": False},
            {"id": "R1", "side": "reviewer", "location": "app/jobs.py:41", "wrong": "the paid call is not guarded", "matters": "billed again", "mark": "confirmed", "evidence": "", "disputed": False}]}]
        task = self.task()
        with board.locked_state(self.root) as state:
            state["analysis_passes"][task].update({"clean": clean, "clean_at": "t"})
        request = self.request()
        qa = board.register(self.root, "qa", "REVIEW_QUEUE", vendor="Anthropic")
        board.reserve_qa(self.root, qa["id"], request["id"])
        brief = board.review_brief(self.root, request["id"], qa["id"])
        self.assertEqual(brief["analysis_clean_list"]["items"], 1)
        self.assertIn("C1 [D1, R1]", brief["analysis_clean_list"]["text"])
        self.assertIn("PASS only if EVERY clean-list item is in FINDINGS.md", brief["process"]["rule"])
        self.assertNotIn("analysis_discovery", brief)
        # no discovery gate any more: the ledger attaches without one
        self.assertEqual(board.attach_challenge_ledger(self.root, qa["id"], request["id"], self.ledger(request))["status"], "claimed")

    def test_the_reviewer_can_pass_only_with_every_clean_list_item_in_the_report(self):
        from unittest import mock
        with mock.patch.dict("os.environ", {"HARNESS_STANDING_CHECKS": "on"}):
            base = "Security check: read it.\nNot examined: nothing.\nFindings verified independently: 2 of 2\n"
            state = {"task_kinds": {"T": "analysis"}, "analysis_passes": {"T": {"clean": [{"id": "C1"}, {"id": "C2"}]}}}
            with mock.patch.object(board.code_quality, "notes", return_value=[{"note": "checked"}]):
                def problem(extra):
                    return board._standing_verdict_problem(Path("/x"), state, {"task": "T", "summary": ""}, "T", base + extra, True)
                self.assertIn("Clean list items in the report", problem(""))
                self.assertIn("1 of 2", problem("Clean list items in the report: 1 of 2\n"))
                self.assertEqual(problem("Clean list items in the report: 2 of 2\n"), "")

    def test_a_write_code_task_is_not_gated(self):
        request = self.request()                                   # the fixture's task is not an analysis: nothing changes
        self.assertEqual(request["status"], "open")
        self.assertFalse(self.state().get("analysis_passes"))


class StallTests(Flow):
    """The side that owes the next analysis step is watched; the side that is only waiting is not (spec 3, Stalls)."""

    WINDOW = cto_wakes.NO_PROGRESS_DEFAULT_SECONDS

    def actionable(self, agent):
        return board._agent_has_actionable_work(self.state(), self.state()["agents"][agent["id"]])

    def age(self, who, seconds):
        """Nothing recorded by this side for `seconds`: its own list actions and its status posts both get older."""
        def older(value):
            return (datetime.fromisoformat(value) - timedelta(seconds=seconds)).isoformat() if value else value
        agent = self.dev if who == "dev" else self.rev
        side = "delivery" if who == "dev" else "reviewer"
        with board.locked_state(self.root) as state:
            row = state["agents"][agent["id"]]
            for key in ("last_progress_at", "last_status_at", "spawned_at"):
                row[key] = older(row.get(key))
            state["analysis_passes"][TASK]["sides"][side]["last_at"] = older(state["analysis_passes"][TASK]["sides"][side]["last_at"])

    def stalled_items(self):
        copy = self.state()
        cto_wakes._no_progress(copy, datetime.now(timezone.utc))
        return [i["subject"] for i in cto_wakes.open_items(copy) if i["reason"] == "no_progress"]

    def test_a_reviewer_with_an_open_list_or_an_owed_cross_check_has_actionable_work(self):
        self.assertFalse(self.actionable(self.rev), "nothing is owed before Go ahead")
        self.go()
        self.assertTrue(self.actionable(self.rev))            # its list is open
        self.add("rev", R1)
        self.close("rev")
        self.assertFalse(self.actionable(self.rev), "its list is closed and it only waits for the Dev")
        self.add("dev", D1)
        self.close("dev")
        self.assertTrue(self.actionable(self.rev), "its cross-check is owed")
        self.marks("rev", "D1=confirmed")
        self.assertFalse(self.actionable(self.rev))

    def test_a_silent_reviewer_is_stalled_while_a_waiting_delivery_keeps_posting_status(self):
        self.go()
        self.add("dev", D1)
        self.close("dev")                                      # Delivery is done with its step and only waits
        self.age("rev", self.WINDOW + 120)
        board.status(self.root, self.dev["id"], "waiting for the reviewer", "working")   # fresh status posts must not hide the Reviewer
        items = self.stalled_items()
        self.assertEqual(len(items), 1, items)
        self.assertTrue(items[0].startswith(f"no_progress:{TASK}:reviewer:"), items)
        self.assertTrue(self.actionable(self.rev), "so the existing silence and recovery handling applies to it")

    def test_a_silent_delivery_is_stalled_while_the_waiting_reviewer_posts(self):
        self.go()
        self.add("rev", R1)
        self.close("rev")
        self.age("dev", self.WINDOW + 120)
        board.status(self.root, self.rev["id"], "waiting for the builder", "working")
        items = self.stalled_items()
        self.assertEqual(len(items), 1, items)
        self.assertTrue(items[0].startswith(f"no_progress:{TASK}:delivery:"), items)

    def test_a_side_that_files_or_posts_inside_the_window_is_not_stalled(self):
        self.go()
        self.age("rev", self.WINDOW + 120)
        self.age("dev", self.WINDOW + 120)
        self.assertEqual(len(self.stalled_items()), 2, "both owe their list and both are silent")
        self.add("rev", R1)                                   # a list action is progress
        board.status(self.root, self.dev["id"], "reading the worker", "working")    # a status post is progress
        self.assertEqual(self.stalled_items(), [])

    def test_a_silent_reviewer_with_a_cross_check_owed_is_stalled_and_one_item_per_episode(self):
        self.lists_closed()
        self.marks("dev", "R1=confirmed", "R2=confirmed")
        self.age("rev", self.WINDOW + 120)
        self.assertEqual([s.split(":")[2] for s in self.stalled_items()], ["reviewer"])
        copy = self.state()
        for _ in range(3):
            cto_wakes._no_progress(copy, datetime.now(timezone.utc))
        self.assertEqual(len([i for i in cto_wakes.open_items(copy) if i["reason"] == "no_progress"]), 1)

    def test_the_merge_wait_is_the_ctos_duty_and_nobody_is_counted_stalled_for_it(self):
        self.both_checked()
        self.age("dev", self.WINDOW + 120)
        self.age("rev", self.WINDOW + 120)
        self.assertEqual(self.stalled_items(), [])

    def test_an_ordinary_task_is_counted_as_before(self):
        other = tempfile.TemporaryDirectory()
        self.addCleanup(other.cleanup)
        root = context(Path(other.name))
        cto_session = control.create(root, "claude_cto")
        board.register(root, "cto", "GLOBAL_MONITOR", vendor="Anthropic", session_id=cto_session["id"])
        agent = agent_with_kind(root, "development", task="PLAIN")
        with board.locked_state(root) as state:
            for key in ("last_progress_at", "last_status_at", "spawned_at"):
                row = state["agents"][agent["id"]]
                row[key] = (datetime.now(timezone.utc) - timedelta(seconds=self.WINDOW + 120)).isoformat()
        copy = board.snapshot(root)
        cto_wakes._no_progress(copy, datetime.now(timezone.utc))
        subjects = [i["subject"] for i in cto_wakes.open_items(copy) if i["reason"] == "no_progress"]
        self.assertEqual(len(subjects), 1)
        self.assertTrue(subjects[0].startswith("no_progress:PLAIN:20"))


class StoryTests(Flow):
    def now_line(self):
        return story.build(self.state(), {}, TASK)["now"]

    def confirmed(self):
        self.go()
        board.record_requirement_confirmation(self.root, self.dev["id"], TEXT)

    def test_the_now_line_names_the_phase_in_plain_words(self):
        self.confirmed()
        self.add("dev", D1, D2)
        self.assertEqual(self.now_line(), "The builder and the reviewer are each reading all the code (builder: 2 issues so far; reviewer: still reading).")
        self.add("rev", R1)
        self.close("dev")
        self.assertIn("builder: done, 2 issues; reviewer: 1 issue so far", self.now_line())
        self.close("rev")
        self.assertEqual(self.now_line(), "Each is checking the other's list.")
        self.marks("dev", "R1=confirmed")
        self.marks("rev", "D1=confirmed", "D2=confirmed")
        self.assertEqual(self.now_line(), "The CTO is merging duplicates.")
        board.analysis_merge(self.root, self.cto["id"], TASK, [])
        self.assertEqual(self.now_line(), "The builder is writing FINDINGS.md from the clean list.")

    def test_steps_three_to_eleven_are_not_needed_for_an_analysis(self):
        self.confirmed()
        rows = story.build(self.state(), {}, TASK)["timeline"]["steps"]
        by_n = {r["n"]: r for r in rows if r["n"] <= 11}
        for n in (3, 4, 5, 7, 8, 9, 10, 11):
            self.assertEqual(by_n[n]["status"], "skipped", n)
            self.assertIn("Not needed", by_n[n]["text"])

    def test_the_final_review_step_of_an_analysis_says_the_report_is_checked_not_the_app(self):
        self.confirmed()
        sentences = story.sentences(self.state(), TASK, 22, {"status": "INDEPENDENT REVIEW UNDER WAY"}, {})
        self.assertEqual(sentences[0], "The reviewer is checking the report against the code.")
        self.assertNotIn("whole app", sentences[0] + sentences[1])

    def test_help_describes_the_new_steps(self):
        from harness import project_manager_page
        source = Path(project_manager_page.__file__).read_text(encoding="utf-8")
        self.assertIn("each read all the code and make their own list", source)
        self.assertIn("the CTO removes the duplicates", source)
        self.assertNotIn("then they compare notes", source)
        self.assertIn("each side can also add a problem it notices while checking", source)


class LongListsReachTheCtoTests(Flow):
    """2026-10-29 live run: 60 + 17 issues made a 26,782-character merge text, the wake keeps 12,000, so the CTO saw less than half and no reviewer item."""

    def long_merge(self):
        self.go()
        dev = [f"app/mod{n}.py:{n}|a long enough description of the problem number {n} in the module, with its trigger, the data it touches, the request that reaches it and the exact condition under which it fails|the customer sees a wrong or missing result in case {n} and cannot tell why" for n in range(1, 61)]
        rev = [f"app/other{n}.py:{n}|a long enough description of the reviewer problem number {n} in the module, with its trigger, the data it touches, the request that reaches it and the exact condition under which it fails|the customer sees a wrong or missing result in case {n}" for n in range(1, 18)]
        self.add("dev", *dev)
        self.add("rev", *rev)
        self.close("dev")
        self.close("rev")
        self.drain()
        self.marks("dev", *[f"R{n}=confirmed" for n in range(1, 18)])
        self.marks("rev", *[f"D{n}=confirmed" for n in range(1, 61)])

    def test_a_long_wake_says_it_is_cut_and_how_to_read_the_whole_lists(self):
        self.long_merge()
        wake = [i for i in self.inbox("cto") if i["source"] == "analysis-merge"]
        self.assertEqual(len(wake), 1)
        text = wake[0]["text"]
        self.assertLessEqual(len(text), 12000)
        self.assertIn("[CUT:", text)
        self.assertIn("analysis-merge --agent " + self.cto["id"] + " --task " + TASK + " --show", text)

    def test_show_returns_both_lists_whole_and_changes_nothing(self):
        self.long_merge()
        shown = board.analysis_merge(self.root, self.cto["id"], TASK, show=True)["lists"]
        for item_id in ("D1 ", "D60 ", "R1 ", "R17 "):
            self.assertIn(item_id, shown)
        self.assertGreater(len(shown), 12000)
        self.assertEqual(analysis_pass.stage(self.record()), "merge", "reading is not merging")
        board.analysis_merge(self.root, self.cto["id"], TASK, ["D2,R4"])
        self.assertEqual(analysis_pass.stage(self.record()), "clean")

    def test_show_is_for_the_active_cto_at_the_merge_stage_only(self):
        self.lists_closed()
        with self.assertRaisesRegex(ValueError, "once both lists are closed and both cross-checks are in"):
            board.analysis_merge(self.root, self.cto["id"], TASK, show=True)
        with self.assertRaisesRegex(ValueError, "only the active CTO"):
            board.analysis_merge(self.root, self.dev["id"], TASK, show=True)

    def test_the_cli_takes_show(self):
        parser = board.build_parser() if hasattr(board, "build_parser") else None
        if parser is not None:
            self.assertTrue(parser.parse_args(["analysis-merge", "--agent", "a", "--task", "T", "--show"]).show)


class ReviewerCostTests(Flow):
    """Round 1 of the 2026-10-29 review: a REAL registered reviewer sits on REVIEW_QUEUE, not on an empty task, so a mock with task '' missed the real state."""

    def test_a_really_registered_reviewer_holds_the_analysis_task_for_cost(self):
        from harness import cost_center
        self.go()
        stored = board._read_state(self.root)["agents"][self.rev["id"]]
        self.assertEqual(stored["task"], "REVIEW_QUEUE", "the production state the mock must not hide")
        observed = {item["managed"]: item for item in cost_center.gather(self.root)}
        self.assertEqual(observed[self.rev_session["id"]]["holder"]["task"], TASK)
        self.assertEqual(board._read_state(self.root)["agents"][self.rev["id"]]["task"], "REVIEW_QUEUE", "gather changes nothing stored")

    def test_before_go_ahead_the_waiting_reviewer_holds_no_task(self):
        from harness import cost_center
        observed = {item["managed"]: item for item in cost_center.gather(self.root)}
        self.assertEqual(observed[self.rev_session["id"]]["holder"]["task"], "REVIEW_QUEUE")


if __name__ == "__main__":
    unittest.main()
