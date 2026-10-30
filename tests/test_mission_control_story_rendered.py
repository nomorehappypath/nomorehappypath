# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Rendered proof (headless Chrome, the real board server) for the Story timeline and the Proof tab of the Mission Control redesign, slice 2.

The 26 steps with their plain tags, the Now step open, a click that shows what happened, folded skipped and later steps, the exception on
top, a step the owner opened that survives the page's refresh, the Proof tab (tiles, per-requirement table, the questioned test, code
quality notes, 'Not run on this version', the earlier version after Not accepted), agent text shown as text, contrast and 375px.

Run:  PYTHONPATH=. python3 -m unittest tests.test_mission_control_story_rendered -v
"""
from __future__ import annotations

import unittest

from harness import board, board_viewer, control
from tests.mission_control_support import RenderedCase
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
        self.assertEqual(result["tabs"], ["Story", "Proof", "Advanced"])
        self.assertEqual(result["step10"], "Tests checked on the empty project")
        self.assertIn("1 of 3 are covered", result["step8"])
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

    def test_the_proof_tab_shows_each_requirement_the_questioned_test_and_quality_notes(self):
        root = self.board()
        body = r"""
await until(() => document.querySelector('[data-story-steps]'));
document.querySelector('#tab-proof-btn').click();
await until(() => document.querySelector('[data-proof-table]'));
const text = node => node.textContent.replace(/\s+/g, ' ').trim();
out.header = text(document.querySelector('[data-proof-header]'));
out.tiles = [...document.querySelectorAll('[data-proof-tiles] .mc-tile')].map(t => [...t.children].map(text).join(' '));
out.rows = [...document.querySelectorAll('[data-proof-table] tbody tr')].map(tr => [...tr.children].map(text));
out.questioned = text(document.querySelector('[data-proof-questioned]'));
out.quality = text(document.querySelector('[data-proof-quality]'));
out.panelShown = !document.querySelector('#tab-proof').hidden && document.querySelector('#tab-story').hidden;
out.injected = document.querySelectorAll('#proof-body img').length;
out.pwned = window.pwned === undefined;
out.hasTable = !!document.querySelector('[data-proof-table] th[scope=col]');
out.regionLabel = document.querySelector('.mc-table-wrap').getAttribute('aria-label');
out.lowContrast = lowContrast(['.mc-pill', '.mc-table td', '.mc-table th', '.mc-table td small', '.mc-tile span', '.mc-tile strong', '.mc-tile small', '[data-proof-header]', '.mc-notes-grid li', '.mc-notes-grid h2']);
await wait(5200);
out.stillOnProof = !document.querySelector('#tab-proof').hidden;
out.link = '';
document.querySelector('#tab-story-btn').click();
await until(() => document.querySelector('[data-story-requirements]'));
out.requirementsTile = text(document.querySelector('[data-story-requirements]'));
document.querySelector('[data-story-requirements] [data-mc-goto]').click();
await wait(150);
out.afterLink = {proof: !document.querySelector('#tab-proof').hidden, focus: document.activeElement.id};
"""
        result = self.run_probe(self.url(root), body, wait=120)
        self.assertTrue(result["panelShown"])
        self.assertIn("1 of 3 requirements have a test written before any code", result["header"])
        self.assertIn("On this version 1 of 2 tested requirements pass", result["header"])
        flat = " | ".join(result["tiles"])
        self.assertIn("Requirements covered 1 of 3", flat)
        self.assertIn("Tests that failed first 1 of 2", flat)
        self.assertIn("1 did not distinguish", flat)
        self.assertIn("Passing now 1 of 2", flat)
        self.assertIn("Changed files a test notices 1 of 2 noticed", flat)
        self.assertIn("1 not checked", flat)
        self.assertEqual(len(result["rows"]), 3)
        first, second, third = result["rows"]
        self.assertIn("Lists the planets visible tonight", first[0])
        self.assertEqual(first[1:], ["Covered", "Failed before the work (good)", "Passing now"])
        self.assertTrue(second[1].startswith("Not covered"))
        self.assertIn("Did not distinguish", second[2])
        self.assertEqual(second[3], "Failed when the board ran it")
        self.assertIn("Quality requirement", third[0])
        self.assertIn("No test is tied to this requirement", third[3])
        self.assertNotIn("Passing now", third[3])
        self.assertIn("The builder questioned one test", result["questioned"])
        self.assertIn("kept it: compute it from the position", result["questioned"])
        self.assertIn("render.py: one 180-line function does three jobs", result["quality"])
        self.assertIn("nothing for you to do", result["quality"])
        self.assertEqual(result["injected"], 0)
        self.assertTrue(result["pwned"])
        self.assertTrue(result["hasTable"])
        self.assertEqual(result["regionLabel"], "Requirements and their tests")
        self.assertEqual(result["lowContrast"], [], "every word on the Proof tab has at least 4.5:1 contrast")
        self.assertTrue(result["stillOnProof"], "the refresh keeps the tab the owner chose")
        self.assertIn("1 / 3 proven", result["requirementsTile"])
        self.assertEqual(result["afterLink"], {"proof": True, "focus": "tab-proof-btn"}, "'See the proof' opens the Proof tab")

    def test_a_result_from_another_commit_is_not_run_on_this_version_and_never_passing_now(self):
        root = self.board(stale_commit=True)
        body = r"""
await until(() => document.querySelector('[data-story-steps]'));
document.querySelector('#tab-proof-btn').click();
await until(() => document.querySelector('[data-proof-table]'));
const text = node => node.textContent.replace(/\s+/g, ' ').trim();
out.rows = [...document.querySelectorAll('[data-proof-table] tbody tr')].map(tr => [...tr.children].map(text));
out.tiles = [...document.querySelectorAll('[data-proof-tiles] .mc-tile')].map(t => [...t.children].map(text).join(' '));
out.page = document.querySelector('#proof-body').textContent;
document.querySelector('#tab-story-btn').click();
await wait(200);
out.requirementsTile = !!document.querySelector('[data-story-requirements]');
"""
        result = self.run_probe(self.url(root), body)
        self.assertEqual(result["rows"][0][3], "Not run on this version")
        self.assertNotIn("Passing now", " ".join(row[3] for row in result["rows"]), "no row reads Passing now without a run on this exact version")
        self.assertIn("Passing now Not run on this version", " | ".join(result["tiles"]))
        self.assertFalse(result["requirementsTile"], "no 'N / M proven' claim without a run on this exact version")

    def test_after_not_accepted_the_old_proof_is_the_earlier_version_and_the_story_says_so(self):
        root = self.board(rejected=True)
        body = r"""
await until(() => document.querySelector('[data-story-steps]'));
const li = n => document.querySelector(`#story-timeline li[data-step="${n}"]`);
const folded = n => document.querySelector(`#story-timeline .mc-fold li[data-step="${n}"]`);
out.now = li(19) ? [li(19).dataset.status, li(19).querySelector('.mc-st-text').textContent] : null;
const row = n => li(n) || folded(n);
out.earlier = [22, 23, 24, 25].map(n => row(n) ? [n, row(n).dataset.status, row(n).querySelector('.mc-st-tag').textContent] : [n, 'missing', '']);
document.querySelector('#tab-proof-btn').click();
await until(() => document.querySelector('[data-proof-table]'));
out.note = (document.querySelector('[data-proof-earlier]') || {}).textContent;
out.nowCells = [...document.querySelectorAll('[data-proof-table] tbody tr td:last-child')].map(td => td.textContent.replace(/\s+/g, ' ').trim());
"""
        result = self.run_probe(self.url(root), body, wait=120)
        self.assertEqual(result["now"][0], "now")
        self.assertIn("Repairing after your feedback", result["now"][1])
        self.assertEqual(result["earlier"], [[22, "earlier", "Done for the earlier version"], [23, "earlier", "Done for the earlier version"],
                                             [24, "earlier", "Done for the earlier version"], [25, "earlier", "Done for the earlier version"]])
        self.assertIn("before your feedback", result["note"])
        self.assertNotIn("Passing now", " ".join(result["nowCells"]))
        self.assertEqual(result["nowCells"][0], "From the earlier version, not current")

    def test_phone_width_has_no_sideways_scroll_on_the_timeline_or_the_proof_and_the_table_stacks(self):
        root = self.board()
        body = r"""
await until(() => document.querySelector('[data-story-steps]'));
await wait(300);
const measure = () => ({docW: document.scrollingElement.scrollWidth, clientW: document.scrollingElement.clientWidth,
  out: [...document.querySelectorAll('.mc-panel:not([hidden]) *')].filter(n => n.getBoundingClientRect().right > innerWidth + 1 && n.getBoundingClientRect().width > 0).map(n => n.id || n.className || n.tagName).slice(0, 6)});
out.viewport = innerWidth;
out.story = measure();
out.summaryHeight = Math.min(...[...document.querySelectorAll('#story-timeline summary')].filter(n => n.getBoundingClientRect().width > 0).map(n => n.getBoundingClientRect().height));
document.querySelector('#tab-proof-btn').click();
await until(() => document.querySelector('[data-proof-table]'));
await wait(300);
out.proof = measure();
out.stacked = getComputedStyle(document.querySelector('[data-proof-table] tbody tr td')).display;
out.labels = [...document.querySelectorAll('[data-proof-table] tbody tr:first-child td')].map(td => td.dataset.label);
out.tabHeight = document.querySelector('#tab-proof-btn').getBoundingClientRect().height;
"""
        result = self.run_probe(self.url(root), body, width=375, height=900)
        self.assertLessEqual(result["viewport"], 500)
        for tab in ("story", "proof"):
            self.assertLessEqual(result[tab]["docW"], result[tab]["clientW"] + 1, f"{tab}: {result[tab]}")
            self.assertEqual(result[tab]["out"], [], f"{tab}: nothing pokes out of the page")
        self.assertGreaterEqual(result["summaryHeight"], 43.5, "every step is a target at least 44px tall")
        self.assertEqual(result["stacked"], "block")
        self.assertEqual(result["labels"], ["Requirement", "Covered", "Before the work", "Now"])
        self.assertGreaterEqual(result["tabHeight"], 43.5)


if __name__ == "__main__":
    unittest.main()
