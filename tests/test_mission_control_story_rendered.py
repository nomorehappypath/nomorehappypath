# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Rendered proof (headless Chrome, the real board server) for the Story timeline and the Proof tab of the Mission Control redesign, slice 2.

The 26 steps with their plain tags, the Now step open, a click that shows what happened, folded skipped and later steps, the exception on
top, a step the owner opened that survives the page's refresh, the Proof tab (tiles, per-requirement table, the questioned test, code
quality notes, 'Not run on this version', the earlier version after Not accepted), agent text shown as text, contrast and 375px.

Run:  PYTHONPATH=. python3 -m unittest tests.test_mission_control_story_rendered -v
"""
from __future__ import annotations

import unittest

from harness import board, board_viewer, control, global_settings
from tests.mission_control_support import RenderedCase
from tests.pinned_settings import old_settings
from tests.requirements_support import agreed_requirements
from tests.test_requirements_contract import context, delivery_with_direction
from tests.test_story import TASK, contract, fresh_request, request

EVIL = '<img src=x onerror="window.pwned=1">'


def whole(review: dict) -> dict:
    """The fields the board's own BOARD.md table reads from every request."""
    return {"developer_id": "dev-1", "claimed_by": "", "review_wait_started_at": "", **review}


class RenderedStoryTests(RenderedCase):
    def board(self, *, rejected=False, sign_in=False, stale_commit=False):
        root = context(self.base)
        agent = delivery_with_direction(root, TASK)
        agreed_requirements(root, agent["id"], "Final agreed requirements: a command prints a greeting.")
        for kind, role, name in (("claude_reviewer", "qa", "REVIEW_QUEUE"), ("claude_cto", "cto", "GLOBAL_MONITOR")):
            session = control.create(root, kind)
            board.register(root, role, name, vendor="Anthropic", session_id=session["id"])
        with board.locked_state(root) as state:
            state["requirement_confirmations"][TASK]["contract_check"] = contract(
                deviations=[{"check": "2.1", "why": f"the data has no such field {EVIL}", "status": "kept", "answer_reason": "compute it from the position"}]
            )[TASK]["contract_check"]
            review = fresh_request("r1", "failed", review_round=1, structured_findings=[{"id": "B1"}, {"id": "B2"}], completed_at="2026-10-27T10:02:00+00:00",
                                   board_decision={"notes": ["n"]}, delivery_state="passed", delivery_evidence="e", evidence="board-run", minute=1)
            if stale_commit:
                review["contract_check_runs"] = [{"commit": "OLDER", "checks_sha256": "HASH", "status": "passed"}]
            state["qa_requests"] = {"r1": whole(review)}
            state.setdefault("task_briefs", {}).setdefault(TASK, {}).update({"update": f"Planning the forecast {EVIL}", "updated_at": board.now()})
            state["code_quality_notes"] = {TASK: {"notes": [f"planets.py: the city is typed in {EVIL}", "render.py: one 180-line function does three jobs"]}}
            state["revert_proofs"] = {TASK: {"commit": "C1", "checks_sha256": "HASH", "files": [
                {"path": "a.py", "outcome": "noticed", "check": "1.1"}, {"path": "b.py", "outcome": "not_noticed"}, {"path": "c.py", "outcome": "over_limit"}]}}
            if sign_in:
                state["reviewer_needed"] = {"sign_in": True, "sign_in_how": "run /login"}
            if rejected:
                final = fresh_request("r9", "passed", phase="final_acceptance", claimed_by="qa-1", minute=1)
                state["qa_requests"] = {"r9": whole(final)}
                state["releases"] = {TASK: {"status": "VISUAL_TEST_REQUIRED", "head_commit": "C1", "preview": {"status": "ready"}, "cto_id": "cto-1", "recorded_at": "2026-10-27T22:00:00+00:00"}}
                state["release_decisions"] = {TASK: {"decision": "not_accepted", "reason": "The colours are wrong", "recorded_at": "2026-10-27T23:00:00+00:00"}}
        self.root = root
        return root

    def url(self, root) -> str:
        return self.serve(board_viewer.make_handler(root, project_name="Weather app", manager_url="http://127.0.0.1:1/",
                                                    settings_home=root / ".harness" / "home", project_id="weather", chat_action_token="owner-token"))

    def test_the_timeline_shows_the_26_steps_in_plain_words_and_a_click_says_what_happened(self):
        root = self.board()
        body = r"""
const steps = () => [...document.querySelectorAll('#story-timeline [data-story-steps] > li[data-step]')];
await until(() => steps().length > 10);
const li = n => document.querySelector(`#story-timeline [data-story-steps] > li[data-step="${n}"]`);
out.shown = steps().map(n => [Number(n.dataset.step), n.dataset.status, n.querySelector('.mc-st-tag').textContent]);
out.titles = steps().map(n => n.querySelector('.mc-st-title').textContent);
out.nowOpen = li(19).querySelector('details').open;
out.nowAria = li(19).getAttribute('aria-current');
out.nowText = li(19).querySelector('.mc-st-text').textContent;
out.step8 = li(8).querySelector('.mc-st-text').textContent;
out.step10 = li(10).querySelector('.mc-st-title').textContent;
out.step1 = li(1).querySelector('.mc-st-text').textContent;
out.nowDetail = li(19).querySelector('.mc-st-more').textContent;
out.folds = [...document.querySelectorAll('#story-timeline .mc-fold')].map(d => [d.dataset.fold, d.querySelector('summary').textContent.trim(), d.open, d.querySelectorAll('li[data-step]').length]);
out.hint = document.querySelector('#story-timeline .mc-hint').textContent;
const summary = li(17).querySelector('summary');
out.summaryFocusable = summary.tabIndex >= 0;
out.closedBefore = !li(17).querySelector('details').open;
summary.click(); await wait(150);
out.opened = li(17).querySelector('details').open;
out.more = li(17).querySelector('.mc-st-more').textContent.replace(/\s+/g, ' ').trim();
await wait(5200);   // the page refreshes every 2 seconds: at least two refreshes happen here
out.stillOpen = li(17).querySelector('details').open;
out.nowStillOpen = li(19).querySelector('details').open;
out.injected = document.querySelectorAll('#story-timeline img, #proof-body img').length;
out.pwned = window.pwned === undefined;
out.lowContrast = lowContrast(['.mc-st-title', '.mc-st-who', '.mc-st-tag', '.mc-st-text', '.mc-st-more', '.mc-st-more li', '.mc-hint', '.mc-fold > summary', '.mc-band', '#story-timeline h2']);
out.tabs = [...document.querySelectorAll('[role=tab]')].map(b => b.textContent.trim());
"""
        result = self.run_probe(self.url(root), body, wait=120)
        shown = {n: (status, tag) for n, status, tag in result["shown"]}
        self.assertEqual([n for n, (status, _t) in shown.items() if status == "now"], [19], "exactly one step is Now")
        self.assertEqual(shown[19], ("now", "Now"))
        self.assertEqual(shown[17], ("done", "Done"))
        self.assertEqual(shown[8], ("gap", "Not covered"), "a coverage gap is never shown as Done")
        self.assertEqual(shown[20], ("next", "Next"))
        self.assertEqual(result["tabs"], ["Story", "Advanced", "Wish list"])
        self.assertEqual(result["step10"], "Tests checked on the empty project")
        self.assertIn("1 of 3 requirements have a test so far", result["step8"])
        self.assertIn("The builder is fixing the 2 problems", result["nowText"])
        self.assertTrue(result["nowOpen"], "the step that is happening is open")
        self.assertEqual(result["nowAria"], "step")
        self.assertIn("OWNER DIRECTION", result["step1"])
        self.assertIn("Planning the forecast", result["nowDetail"], "the builder's note is shown as text")
        folds = {kind: (label, opened, count) for kind, label, opened, count in result["folds"]}
        self.assertIn("steps not needed", folds["skipped"][0])
        self.assertFalse(folds["skipped"][1])
        self.assertIn("more steps to come", folds["later"][0])
        self.assertEqual(folds["later"][2], 5)
        self.assertEqual(result["hint"], "Select a step to see what happened.")
        self.assertTrue(result["summaryFocusable"])
        self.assertTrue(result["closedBefore"])
        self.assertTrue(result["opened"], "clicking a step opens it")
        self.assertIn("What happened", result["more"])
        self.assertIn("round 1: 2 blockers, 1 note", result["more"])
        self.assertTrue(result["stillOpen"], "a step the owner opened stays open across the page's refresh")
        self.assertTrue(result["nowStillOpen"])
        self.assertEqual(result["injected"], 0, "agent text is shown as text")
        self.assertTrue(result["pwned"])
        self.assertEqual(result["lowContrast"], [], "every word on the timeline has at least 4.5:1 contrast")

    def test_an_exception_is_shown_first_above_the_steps_and_names_the_one_action(self):
        root = self.board(sign_in=True)
        body = r"""
await until(() => document.querySelector('[data-story-attention]'));
const attention = document.querySelector('[data-story-attention]');
out.attention = attention.textContent;
out.role = attention.getAttribute('role');
out.before = !!(attention.compareDocumentPosition(document.querySelector('[data-story-steps]')) & Node.DOCUMENT_POSITION_FOLLOWING);
out.now = document.querySelector('#story-timeline li[data-status=now] .mc-st-text').textContent;
out.lowContrast = lowContrast(['.mc-attention', '.mc-attention strong']);
"""
        result = self.run_probe(self.url(root), body)
        self.assertIn("Needs attention first", result["attention"])
        self.assertIn("signed out", result["attention"])
        self.assertIn("run /login", result["attention"])
        self.assertEqual(result["role"], "alert")
        self.assertTrue(result["before"])
        self.assertIn("signed out", result["now"])
        self.assertEqual(result["lowContrast"], [])

    def test_there_is_no_proof_tab_and_the_requirements_tile_has_no_link_to_one(self):
        root = self.board()
        body = r"""
await until(() => document.querySelector('[data-story-requirements]'));
const text = node => node.textContent.replace(/\s+/g, ' ').trim();
out.tabs = [...document.querySelectorAll('#mc-tablist [role=tab]')].map(text);
out.proofPanel = !!document.querySelector('#tab-proof') || !!document.querySelector('#tab-proof-btn');
out.requirementsTile = text(document.querySelector('[data-story-requirements]'));
out.proofLink = !!document.querySelector('[data-mc-goto="proof"]');
"""
        result = self.run_probe(self.url(root), body, wait=120)
        self.assertEqual(result["tabs"], ["Story", "Advanced", "Wish list"])
        self.assertFalse(result["proofPanel"])
        self.assertIn("1 / 3 proven", result["requirementsTile"])
        self.assertFalse(result["proofLink"])

    def test_a_result_from_another_commit_never_claims_requirements_proven_on_this_version(self):
        root = self.board(stale_commit=True)
        body = r"""
await until(() => document.querySelector('[data-story-steps]'));
await wait(200);
out.requirementsTile = !!document.querySelector('[data-story-requirements]');
"""
        result = self.run_probe(self.url(root), body)
        self.assertFalse(result["requirementsTile"], "no 'N / M proven' claim without a run on this exact version")

    def test_after_not_accepted_the_steps_after_the_repair_are_the_earlier_version(self):
        root = self.board(rejected=True)
        body = r"""
await until(() => document.querySelector('[data-story-steps]'));
const li = n => document.querySelector(`#story-timeline li[data-step="${n}"]`);
const folded = n => document.querySelector(`#story-timeline .mc-fold li[data-step="${n}"]`);
out.now = li(19) ? [li(19).dataset.status, li(19).querySelector('.mc-st-text').textContent] : null;
const row = n => li(n) || folded(n);
out.earlier = [22, 23, 24, 25].map(n => row(n) ? [n, row(n).dataset.status, row(n).querySelector('.mc-st-tag').textContent] : [n, 'missing', '']);
"""
        result = self.run_probe(self.url(root), body, wait=120)
        self.assertEqual(result["now"][0], "now")
        self.assertIn("Repairing after your feedback", result["now"][1])
        self.assertEqual(result["earlier"], [[22, "earlier", "Done for the earlier version"], [23, "earlier", "Done for the earlier version"],
                                             [24, "earlier", "Done for the earlier version"], [25, "earlier", "Done for the earlier version"]])

    def test_phone_width_has_no_sideways_scroll_on_the_timeline_and_tabs_are_tall_enough(self):
        root = self.board()
        body = r"""
await until(() => document.querySelector('[data-story-steps]'));
await wait(300);
const measure = () => ({docW: document.scrollingElement.scrollWidth, clientW: document.scrollingElement.clientWidth,
  out: [...document.querySelectorAll('.mc-panel:not([hidden]) *')].filter(n => n.getBoundingClientRect().right > innerWidth + 1 && n.getBoundingClientRect().width > 0).map(n => n.id || n.className || n.tagName).slice(0, 6)});
out.viewport = innerWidth;
out.story = measure();
out.summaryHeight = Math.min(...[...document.querySelectorAll('#story-timeline summary')].filter(n => n.getBoundingClientRect().width > 0).map(n => n.getBoundingClientRect().height));
out.tabHeight = document.querySelector('#tab-advanced-btn').getBoundingClientRect().height;
"""
        result = self.run_probe(self.url(root), body, width=375, height=900)
        self.assertLessEqual(result["viewport"], 500)
        self.assertLessEqual(result["story"]["docW"], result["story"]["clientW"] + 1, f"story: {result['story']}")
        self.assertEqual(result["story"]["out"], [], "story: nothing pokes out of the page")
        self.assertGreaterEqual(result["summaryHeight"], 43.5, "every step is a target at least 44px tall")
        self.assertGreaterEqual(result["tabHeight"], 43.5)

    def test_each_role_has_one_fixed_color_on_its_name_its_row_and_its_screen(self):
        # Issue #92: builder, reviewer and CTO each wear one colour wherever they appear, and the name stays readable.
        root = self.board()
        body = r"""
await until(() => document.querySelectorAll('#agent-strip .mc-agent[data-agent-role]').length >= 3);
document.querySelector('#tab-advanced-btn').click();
await until(() => document.querySelectorAll('#agents .agent-row[data-agent-role]').length >= 3);
const css = node => getComputedStyle(node);
out.strip = {}; out.rows = {}; out.screen = {};
for (const node of document.querySelectorAll('#agent-strip .mc-agent[data-agent-role]'))
  out.strip[node.dataset.agentRole] = [css(node.querySelector('.mc-agent-head strong')).color, css(node).borderTopColor];
for (const node of document.querySelectorAll('#agents .agent-row[data-agent-role]'))
  out.rows[node.dataset.agentRole] = [css(node.querySelector(':scope > .row > div > strong')).color, css(node).borderLeftColor];
const dialog = document.querySelector('#agent-view-dialog');
for (const kind of ['codex_delivery', 'claude_reviewer', 'claude_cto']) {
  paintAgentRole(dialog, agentColorRole(null, {kind}));   // what viewAgent does when it opens that session's screen
  out.screen[dialog.dataset.agentRole] = [css(document.querySelector('#agent-view-title')).color, css(dialog).borderTopColor];
}
out.lowContrast = lowContrast(['#agent-strip .mc-agent-head strong', '#agents .agent-row > .row > div > strong']);
"""
        result = self.run_probe(self.url(root), body, wait=120)
        roles = ["builder", "reviewer", "cto"]
        for surface in ("strip", "rows", "screen"):
            self.assertEqual(sorted(result[surface]), sorted(roles), f"{surface}: every role is coloured")
        for role in roles:
            self.assertEqual(result["strip"][role], result["rows"][role], f"{role}: the row wears the strip's colour")
            self.assertEqual(result["strip"][role], result["screen"][role], f"{role}: its screen wears the same colour")
        self.assertEqual(len({tuple(result["strip"][role]) for role in roles}), 3, "the three roles are told apart")
        self.assertEqual(result["lowContrast"], [], "a coloured name keeps 4.5:1")

    def test_a_color_chosen_in_settings_is_the_one_mission_control_shows(self):
        root = self.board()
        global_settings.update_agent_settings(root / ".harness" / "home", old_settings(), None, None, {"cto": "blue"})
        body = r"""
await until(() => document.querySelectorAll('#agent-strip .mc-agent[data-agent-role]').length >= 3);
document.querySelector('#tab-advanced-btn').click();
await until(() => document.querySelector('#agents .agent-row[data-agent-role=cto]'));
const css = node => getComputedStyle(node);
const card = document.querySelector('#agent-strip [data-agent-role=cto]'), row = document.querySelector('#agents .agent-row[data-agent-role=cto]');
out.card = [css(card.querySelector('.mc-agent-head strong')).color, css(card).borderTopColor];
out.row = [css(row.querySelector(':scope > .row > div > strong')).color, css(row).borderLeftColor];
out.builder = css(document.querySelector('#agent-strip [data-agent-role=builder]')).borderTopColor;
out.lowContrast = lowContrast(['#agent-strip .mc-agent-head strong', '#agents .agent-row > .row > div > strong']);
"""
        result = self.run_probe(self.url(root), body, wait=120)
        self.assertEqual(result["card"], ["rgb(29, 78, 216)", "rgb(59, 130, 246)"], "the CTO wears the blue chosen in Settings")
        self.assertEqual(result["row"], result["card"])
        self.assertEqual(result["builder"], "rgb(20, 184, 166)", "a role left alone keeps its default")
        self.assertEqual(result["lowContrast"], [])


if __name__ == "__main__":
    unittest.main()
