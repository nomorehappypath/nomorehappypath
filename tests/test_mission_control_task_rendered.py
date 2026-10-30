# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Rendered proof (headless Chrome, the real board server) for the task page of the Mission Control redesign, slice 1.

The Now section, the agent strip (Message / View its screen and the spec's fallbacks), the Story and Advanced tabs (keyboard, every moved
control still present and wired), the empty-project button, escaping of agent text, a draft that survives the page's refresh, the paused
page and the 375px phone width.

Run:  PYTHONPATH=. python3 -m unittest tests.test_mission_control_task_rendered -v
"""
from __future__ import annotations

import json
import unittest

from harness import board, board_viewer, control
from tests.mission_control_support import RenderedCase
from tests.requirements_support import agreed_requirements
from tests.test_requirements_contract import context, delivery_with_direction

EVIL = '<img src=x onerror="window.pwned=1">'

# Every operational control that moved off the default page into the Advanced tab (spec section 4 and 5), by id.
MOVED = ["#codex", "#claude", "#cto", "#stop-all", "#relaunch-preserved", "#give-direction", "#direction-start-note", "#direction-discard",
         "#access-details", "#access-claude-path", "#access-codex-path", "#attention", "#active", "#open", "#claimed", "#passed", "#agents", "#queue"]
STAYING = ["#tasks", "#history", "#history-search", "#project-chat", "#story-now", "#agent-strip"]

STUB_VIEW = r"""
const realFetch = window.fetch.bind(window);
window.__viewCalls = [];
window.fetch = (url, options) => {
  const target = String(url);
  if (/\/api\/sessions\/[^/]+\/view$/.test(target)) { window.__viewCalls.push(target); return Promise.resolve(new Response(JSON.stringify({mode: 'native'}), {status: 200, headers: {'Content-Type': 'application/json'}})); }
  return realFetch(url, options);
};
"""


class RenderedTaskPageTests(RenderedCase):
    def board(self, task="WEATHER-APP", *, reviewer=True, cto=True, note=None, requirements=True):
        root = context(self.base)
        agent = delivery_with_direction(root, task)
        if requirements:
            agreed_requirements(root, agent["id"], "Final agreed requirements: a command prints a greeting.")
        self.sessions = {"builder": agent["session_id"]}
        if reviewer:
            session = control.create(root, "claude_reviewer")
            board.register(root, "qa", "REVIEW_QUEUE", vendor="Anthropic", session_id=session["id"])
            self.sessions["reviewer"] = session["id"]
        if cto:
            session = control.create(root, "claude_cto")
            board.register(root, "cto", "GLOBAL_MONITOR", vendor="Anthropic", session_id=session["id"])
            self.sessions["cto"] = session["id"]
        if note:
            with board.locked_state(root) as state:
                state.setdefault("task_briefs", {}).setdefault(task, {}).update({"update": note, "updated_at": board.now()})
        self.root = root
        return root

    def empty_board(self):
        """A project whose builder is waiting for its first task: nothing is live."""
        root = self.board(requirements=False)
        with board.locked_state(root) as state:
            state["agents"] = {key: value for key, value in state["agents"].items() if value.get("role") not in board.DEVELOPER_ROLES}
        session = control.create(root, "codex_delivery")
        board.register(root, "development", board.AWAITING_OWNER_DIRECTION, vendor="OpenAI", session_id=session["id"])
        return root

    def url(self, root, **kw) -> str:
        return self.serve(board_viewer.make_handler(root, project_name="Weather app", manager_url="http://127.0.0.1:1/",
                                                    settings_home=root / ".harness" / "home", project_id="weather", chat_action_token="owner-token", **kw))

    def test_the_default_page_is_the_story_with_the_now_section_and_three_agents(self):
        root = self.board(note=f"Building the forecast {EVIL}")
        body = STUB_VIEW + r"""
await until(() => document.querySelectorAll('#agent-strip .mc-agent').length === 3 && document.querySelector('[data-story-now]'));
const visible = selector => { const node = document.querySelector(selector); return !!node && !node.closest('[hidden]') && node.getBoundingClientRect().width > 0; };
out.tabs = {story: document.querySelector('#tab-story').hidden, advanced: document.querySelector('#tab-advanced').hidden,
            selected: [...document.querySelectorAll('[role=tab]')].map(b => b.getAttribute('aria-selected'))};
out.now = {text: document.querySelector('[data-story-now]').textContent, next: document.querySelector('[data-story-next]').textContent,
           step: document.querySelector('[data-story-step]').textContent, bar: document.querySelector('#story-now [role=progressbar]').getAttribute('aria-valuenow'),
           cost: document.querySelector('#story-now .mc-tile').textContent.replace(/\s+/g, ' ').trim()};
out.agents = [...document.querySelectorAll('#agent-strip .mc-agent')].map(node => ({role: node.dataset.agentRole, head: node.querySelector('.mc-agent-head').textContent.replace(/\s+/g, ' ').trim(),
    doing: node.querySelector('.mc-doing').textContent, buttons: [...node.querySelectorAll('button')].map(b => ({text: b.textContent.trim(), disabled: b.disabled})), small: [...node.querySelectorAll('small')].map(n => n.textContent)}));
out.visible = {staying: ${STAYING}.map(selector => [selector, visible(selector)]), moved: ${MOVED}.map(selector => [selector, visible(selector)]),
               movedExist: ${MOVED}.map(selector => [selector, !!document.querySelector('#tab-advanced ' + selector)]),
               stayingInStory: ${STAYING}.map(selector => [selector, !!document.querySelector('#tab-story ' + selector)])};
out.lowContrast = lowContrast(['.mc-eyebrow', '.mc-now-text', '.mc-next', '.mc-needs-line', '.mc-step span', '.mc-tile span', '.mc-tile strong', '.mc-agent-head strong', '.mc-agent-head span', '.mc-doing', '.mc-agent small', '.mc-tabs button', '.mc-agent .mc-note']);
out.injected = document.querySelectorAll('#agent-strip img, #story-now img').length;
out.pwned = window.pwned === undefined;
out.builderText = document.querySelector('#agent-strip [data-agent-role=builder] .mc-doing').textContent;
""".replace("${STAYING}", json.dumps(STAYING)).replace("${MOVED}", json.dumps(MOVED))
        result = self.run_probe(self.url(root), body)
        self.assertEqual(result["tabs"], {"story": False, "advanced": True, "selected": ["true", "false", "false"]})
        self.assertEqual(result["now"]["text"], "The builder is planning the work.")
        self.assertIn("Step 12 of 26 · Plan made", result["now"]["step"])
        self.assertEqual(result["now"]["bar"], "12")
        self.assertIn("not measured yet", result["now"]["cost"])
        agents = {a["role"]: a for a in result["agents"]}
        self.assertEqual(set(agents), {"builder", "reviewer", "cto"})
        self.assertIn("Builder", agents["builder"]["head"])
        self.assertIn("Independent reviewer", agents["reviewer"]["head"])
        self.assertEqual([b["text"] for b in agents["builder"]["buttons"]], ["Message", "View its screen"])
        self.assertEqual([b["text"] for b in agents["reviewer"]["buttons"]], ["Type to the reviewer", "View its screen"])
        self.assertEqual([b["text"] for b in agents["cto"]["buttons"]], ["Message", "View its screen"])
        self.assertIn("Quiet. Wakes only when a judgment is needed.", agents["cto"]["doing"])
        self.assertIn("Cost: not measured yet", agents["builder"]["small"])
        # what stays on the default page, and what moved to Advanced (hidden until its tab is chosen)
        self.assertTrue(all(visible for _name, visible in result["visible"]["staying"]), result["visible"]["staying"])
        self.assertFalse(any(visible for _name, visible in result["visible"]["moved"]), "the operational controls are not on the default page")
        self.assertTrue(all(present for _name, present in result["visible"]["movedExist"]), result["visible"]["movedExist"])
        self.assertTrue(all(present for _name, present in result["visible"]["stayingInStory"]), result["visible"]["stayingInStory"])
        self.assertEqual(result["injected"], 0)
        self.assertTrue(result["pwned"])
        self.assertEqual(result["lowContrast"], [], "every word on the new sections has at least 4.5:1 contrast")

    def test_message_and_view_its_screen_use_the_routes_that_already_exist(self):
        root = self.board()
        body = STUB_VIEW + r"""
await until(() => document.querySelectorAll('#agent-strip .mc-agent').length === 3);
const strip = role => document.querySelector(`#agent-strip [data-agent-role=${role}]`);
const dialog = document.querySelector('#owner-message-dialog');
strip('builder').querySelector('[data-strip-message]').click();
await wait(200);
out.builder = {open: dialog.open, title: document.querySelector('#owner-message-title').textContent};
document.querySelector('#owner-message-cancel').click();
await wait(200);
strip('cto').querySelector('[data-strip-message]').click();
await wait(200);
out.cto = {open: dialog.open, title: document.querySelector('#owner-message-title').textContent, submit: document.querySelector('#owner-message-submit').textContent};
document.querySelector('#owner-message-cancel').click();
await wait(200);
strip('reviewer').querySelector('button').click();
await wait(300);
strip('builder').querySelector('[data-strip-action=view]').click();
await wait(300);
out.view = {calls: window.__viewCalls.map(u => u.split('/api/sessions/')[1]), notice: document.querySelector('#notice').textContent};
"""
        result = self.run_probe(self.url(root), body)
        self.assertEqual(result["builder"], {"open": True, "title": "Send clarification to Delivery"})
        self.assertEqual(result["cto"], {"open": True, "title": "Message the CTO", "submit": "Send to the CTO"})
        reviewer_call = f"{self.sessions['reviewer']}/view"
        builder_call = f"{self.sessions['builder']}/view"
        self.assertEqual(result["view"]["calls"], [reviewer_call, builder_call], "Type to the reviewer opens the reviewer's own screen; View its screen opens the builder's")
        self.assertIn("open in Terminal", result["view"]["notice"])

    def test_a_missing_reviewer_says_so_and_offers_the_plain_way_instead_of_a_dead_button(self):
        root = self.board(reviewer=False, cto=False)
        body = r"""
await until(() => document.querySelectorAll('#agent-strip .mc-agent').length === 3);
const node = document.querySelector('#agent-strip [data-agent-role=reviewer]');
out.reviewer = {buttons: [...node.querySelectorAll('button')].map(b => ({text: b.textContent.trim(), disabled: b.disabled})), note: (node.querySelector('.mc-note') || {}).textContent, doing: node.querySelector('.mc-doing').textContent};
"""
        result = self.run_probe(self.url(root), body)
        self.assertTrue(all(b["disabled"] for b in result["reviewer"]["buttons"]))
        self.assertIn("No reviewer is running", result["reviewer"]["doing"])
        self.assertTrue(result["reviewer"]["note"], "the owner is told why, not just shown a grey button")

    def test_tabs_work_with_the_keyboard_and_every_moved_control_is_in_advanced(self):
        root = self.board()
        body = r"""
await until(() => document.querySelectorAll('#agent-strip .mc-agent').length === 3);
const list = document.querySelector('#mc-tablist');
document.querySelector('#tab-story-btn').focus();
list.dispatchEvent(new KeyboardEvent('keydown', {key: 'ArrowRight', bubbles: true}));
await wait(100);
out.afterFirstArrow = document.activeElement.id;   // Story -> Proof (slice 2 added the Proof tab between Story and Advanced)
list.dispatchEvent(new KeyboardEvent('keydown', {key: 'ArrowRight', bubbles: true}));
await wait(100);
const shown = selector => { const node = document.querySelector(selector); return !!node && !node.closest('[hidden]') && node.getBoundingClientRect().width > 0; };
out.afterArrow = {selected: [...document.querySelectorAll('[role=tab]')].map(b => b.getAttribute('aria-selected')), focus: document.activeElement.id,
                  panel: document.querySelector('#tab-advanced').hidden, story: document.querySelector('#tab-story').hidden,
                  controls: ['#codex', '#claude', '#cto', '#stop-all', '#access-details', '#agents', '#queue'].map(s => [s, shown(s)]),
                  giveDirection: !!document.querySelector('#tab-advanced #give-direction')};   // Give direction hides itself while a builder is running, as before
out.buttons = {codex: document.querySelector('#codex').textContent, stop: document.querySelector('#stop-all').disabled, rows: document.querySelectorAll('#agents .agent-row').length,
               rowButtons: [...document.querySelectorAll('#agents .agent-row')].map(r => [...r.querySelectorAll('button,a')].map(b => b.textContent.trim()))};
document.querySelector('#codex').click();
await wait(300);
out.launchDialogOpen = document.querySelector('#color-dialog').open;
document.querySelector('#color-close').click();
list.dispatchEvent(new KeyboardEvent('keydown', {key: 'Home', bubbles: true}));
await wait(100);
out.afterHome = {selected: [...document.querySelectorAll('[role=tab]')].map(b => b.getAttribute('aria-selected')), focus: document.activeElement.id};
"""
        result = self.run_probe(self.url(root), body)
        self.assertEqual(result["afterFirstArrow"], "tab-proof-btn")
        self.assertEqual(result["afterArrow"]["selected"], ["false", "false", "true"])
        self.assertEqual(result["afterArrow"]["focus"], "tab-advanced-btn")
        self.assertFalse(result["afterArrow"]["panel"])
        self.assertTrue(result["afterArrow"]["story"])
        self.assertTrue(all(shown for _s, shown in result["afterArrow"]["controls"]), result["afterArrow"]["controls"])
        self.assertTrue(result["afterArrow"]["giveDirection"])
        self.assertIn("Delivery", result["buttons"]["codex"])
        self.assertEqual(result["buttons"]["rows"], 3, "the three agents are listed with their terminal controls")
        flat = " ".join(" ".join(row) for row in result["buttons"]["rowButtons"])
        for label in ("View agent", "Send clarification", "Message the CTO", "View status", "Conversation", "Stop terminal"):
            self.assertIn(label, flat)
        self.assertTrue(result["launchDialogOpen"], "the launch buttons still open the launch dialog from Advanced")
        self.assertEqual(result["afterHome"], {"selected": ["true", "false", "false"], "focus": "tab-story-btn"})

    def test_a_draft_survives_the_pages_refresh_and_the_empty_project_button_opens_the_composer(self):
        root = self.empty_board()
        body = r"""
await until(() => document.querySelector('#story-tell-builder'));
out.now = document.querySelector('[data-story-now]').textContent;
document.querySelector('#story-tell-builder').click();
await wait(300);
const dialog = document.querySelector('#owner-message-dialog');
out.dialog = {open: dialog.open, title: document.querySelector('#owner-message-title').textContent};
const box = document.querySelector('#owner-message-text');
box.focus(); box.value = 'Build me a small weather page'; box.dispatchEvent(new Event('input', {bubbles: true}));
await wait(6500);   // the page refreshes every 2 seconds: at least three refreshes happen here
out.kept = {open: dialog.open, text: box.value, focus: document.activeElement === box};
"""
        result = self.run_probe(self.url(root), body, wait=120)
        self.assertEqual(result["now"], "Nothing is in progress.")
        self.assertEqual(result["dialog"], {"open": True, "title": "Give direction to Delivery"})
        self.assertEqual(result["kept"], {"open": True, "text": "Build me a small weather page", "focus": True})

    def test_the_link_from_the_projects_page_opens_the_composer_by_itself(self):
        root = self.empty_board()
        body = r"""
await until(() => document.querySelector('#owner-message-dialog').open);
out.dialog = {open: document.querySelector('#owner-message-dialog').open, title: document.querySelector('#owner-message-title').textContent, hash: location.hash};
"""
        result = self.run_probe(self.url(root), body, path="/#start-direction")
        self.assertEqual(result["dialog"], {"open": True, "title": "Give direction to Delivery", "hash": "#start-direction"})

    def test_a_paused_board_keeps_its_tabs_and_start_button_and_switches_the_rest_off(self):
        root = self.empty_board()
        with board.locked_state(root) as state:
            state["project_pause"] = {"status": "paused"}
        body = r"""
await until(() => document.querySelector('#story-tell-builder') && document.body.classList.contains('project-paused'));
await wait(600);
out.tabs = [...document.querySelectorAll('[role=tab]')].map(b => b.disabled);
out.tell = document.querySelector('#story-tell-builder').disabled;
out.strip = [...document.querySelectorAll('#agent-strip button')].map(b => b.disabled);
document.querySelector('#tab-advanced-btn').click();
await wait(100);
out.advancedShown = !document.querySelector('#tab-advanced').hidden;
out.give = document.querySelector('#give-direction').disabled;
out.stopAll = document.querySelector('#stop-all').disabled;
"""
        result = self.run_probe(self.url(root), body, wait=120)
        self.assertEqual(result["tabs"], [False, False, False], "the owner can still look around a paused project")
        self.assertFalse(result["tell"])
        self.assertTrue(all(result["strip"]), "messaging an agent is off while paused (the server refuses it too)")
        self.assertTrue(result["advancedShown"])
        self.assertFalse(result["give"], "saving a direction while paused still works")
        self.assertTrue(result["stopAll"])

    def test_phone_width_has_no_sideways_scroll_on_either_tab(self):
        root = self.board(note="A long note that must wrap and never push the page sideways because it has a veryveryveryverylongwordinit")
        body = r"""
await until(() => document.querySelectorAll('#agent-strip .mc-agent').length === 3);
await wait(400);
const measure = () => ({docW: document.scrollingElement.scrollWidth, clientW: document.scrollingElement.clientWidth,
  out: [...document.querySelectorAll('.mc-panel:not([hidden]) *')].filter(n => n.getBoundingClientRect().right > innerWidth + 1 && n.getBoundingClientRect().width > 0).map(n => n.id || n.className || n.tagName).slice(0, 6)});
out.viewport = innerWidth;
out.story = measure();
out.stripButtons = Math.min(...[...document.querySelectorAll('#agent-strip button')].filter(b => !b.disabled).map(b => b.getBoundingClientRect().height));
out.tabHeight = document.querySelector('#tab-story-btn').getBoundingClientRect().height;
document.querySelector('#tab-advanced-btn').click();
await wait(300);
out.advanced = measure();
"""
        result = self.run_probe(self.url(root), body, width=375, height=900)
        self.assertLessEqual(result["viewport"], 500)
        for tab in ("story", "advanced"):
            self.assertLessEqual(result[tab]["docW"], result[tab]["clientW"] + 1, f"{tab}: {result[tab]}")
            self.assertEqual(result[tab]["out"], [], f"{tab}: nothing pokes out of the page")
        self.assertGreaterEqual(result["stripButtons"], 43.5)
        self.assertGreaterEqual(result["tabHeight"], 43.5)


if __name__ == "__main__":
    unittest.main()
