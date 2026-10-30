# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Rendered proof (headless Chrome, the real manager) for the Projects page of the Mission Control redesign, slice 1.

Now cards, the "needs you" banner, the empty-project button, the per-project Advanced menu (every moved control still works from there,
and an open menu survives the page's refresh with its scroll and focus), escaping of agent text, and the 375px phone width.

Run:  PYTHONPATH=. python3 -m unittest tests.test_mission_control_projects_rendered -v
"""
from __future__ import annotations

import unittest

from harness import project_manager, project_registry as registry
from tests.mission_control_support import (RenderedCase, empty_project_state, paused_project_state, ready_project_state,
                                           repair_project_state)

EVIL = '<img src=x onerror="window.pwned=1">'

CARDS = r"""
await until(() => document.querySelectorAll('#projects .project').length >= 5);
await wait(400);
const card = name => [...document.querySelectorAll('#projects .project')].find(node => node.querySelector('h3').textContent.trim() === name);
const text = (node, selector) => (node && node.querySelector(selector) ? node.querySelector(selector).textContent.trim() : null);
out.hero = document.querySelector('#hero-sentence').textContent;
const banner = document.querySelector('#needs-banner');
out.banner = {hidden: banner.hidden, text: banner.textContent.replace(/\s+/g, ' ').trim(), rows: banner.querySelectorAll('.need-row').length,
              buttons: [...banner.querySelectorAll('button')].map(b => b.textContent.trim())};
out.cards = {};
for (const name of ['Weather app', 'Sky Observatory', 'Marketing studio', 'Fresh idea', 'Signed out']) {
  const node = card(name);
  out.cards[name] = {
    now: text(node, '[data-now-sentence]'), next: text(node, '.now-next'), step: text(node, '.step-line'),
    bar: node.querySelector('[role=progressbar]') ? {now: node.querySelector('[role=progressbar]').getAttribute('aria-valuenow'), max: node.querySelector('[role=progressbar]').getAttribute('aria-valuemax')} : null,
    pills: [...node.querySelectorAll('.pill')].map(p => p.textContent.trim()),
    links: [...node.querySelectorAll('.card-links button')].map(b => b.textContent.trim()),
    tell: node.querySelector('.tell-builder') ? {text: node.querySelector('.tell-builder').textContent.trim(), act: node.querySelector('.tell-builder').dataset.act, go: node.querySelector('.tell-builder').dataset.go} : null,
    actions: [...node.querySelectorAll('.actions button')].map(b => b.textContent.trim()),
    needsLine: text(node, '.now-needs'),
    advOpen: node.querySelector('details.adv').open,
    advHas: {machine: !!node.querySelector('details.adv [data-act=machine]'), remove: !!node.querySelector('details.adv [data-act=remove]'),
             path: !!node.querySelector('details.adv .path'), metrics: node.querySelectorAll('details.adv .metric').length},
    visibleBadges: [...node.querySelectorAll('.project-top .badge')].map(b => b.textContent.trim()),
  };
}
out.lowContrast = lowContrast(['.now-label', '.now-sentence', '.now-next', '.now-needs', '.now-task', '.step-line span', '.pill', '.card-links button', '.need-title', '.need-detail', '.need-action', '.needs-banner h2', '.need-row .button', 'details.adv > summary', '.project .description', '.hero-copy']);
out.injected = document.querySelectorAll('#projects img, #needs-banner img').length;
out.pwned = window.pwned === undefined;
"""


NOT_OPEN = project_manager.NOT_OPEN_NOW


class RenderedProjectsPageTests(RenderedCase):
    def fixture(self, open_name: str | None = None):
        """Five projects. Only one project is open at a time (owner 2026-10-28): only that card shows its board; the rest are basics."""
        home = self.base / "home"
        entries = {}
        entries["Weather app"] = self.project(home, "Weather app", ready_project_state(), description="Find tomorrow's weather.")
        entries["Sky Observatory"] = self.project(home, "Sky Observatory", repair_project_state(), description="A sky map.")
        entries["Marketing studio"] = self.project(home, "Marketing studio", paused_project_state())
        entries["Fresh idea"] = self.project(home, "Fresh idea", empty_project_state())
        signed_out = repair_project_state("SIGNIN")
        signed_out["reviewer_needed"] = {"sign_in": True, "sign_in_how": f"run /login {EVIL}"}
        entries["Signed out"] = self.project(home, "Signed out", signed_out)
        if open_name:
            registry.activate(home, entries[open_name]["id"])
        return home

    def assert_closed_cards(self, cards, names):
        for name in names:
            card = cards[name]
            self.assertEqual(card["now"], NOT_OPEN, name)
            self.assertIsNone(card["step"], name)
            self.assertIsNone(card["bar"], name)
            self.assertIsNone(card["tell"], name)
            self.assertNotIn("History", " ".join(card["links"]), name)
            self.assertEqual(card["advHas"]["metrics"], 0, f"{name}: no task counts for a project that is not open")
            self.assertTrue(card["advHas"]["path"] and card["advHas"]["remove"], name)

    def test_now_cards_the_needs_you_banner_and_the_empty_project_button(self):
        result = self.run_probe(self.manager_url(self.fixture("Weather app")), CARDS, height=1400)
        self.assertIn("1 needs you", result["hero"], result["hero"])
        self.assertFalse(result["banner"]["hidden"])
        self.assertEqual(result["banner"]["rows"], 1, "only the open project's board is read, so only its work needs him")
        self.assertIn("Weather is ready for you to try", result["banner"]["text"])
        self.assertIn("6 requirements had tests written first", result["banner"]["text"])
        self.assertEqual(sorted(result["banner"]["buttons"]), ["Review and accept"])
        cards = result["cards"]
        weather = cards["Weather app"]
        self.assertIn("Step 25 of 26 · Your review", weather["step"])
        self.assertIn("Needs you:", weather["needsLine"])
        self.assertEqual(weather["advHas"]["metrics"], 4)
        self.assert_closed_cards(cards, ["Sky Observatory", "Marketing studio", "Fresh idea", "Signed out"])
        for name, card in cards.items():
            self.assertFalse(card["advOpen"], f"{name}: the Advanced menu starts closed")
        self.assertTrue(cards["Fresh idea"]["advHas"]["machine"] and cards["Fresh idea"]["advHas"]["remove"], "the test machine and Remove moved into Advanced")

    def test_each_open_project_shows_its_own_story(self):
        sky = self.run_probe(self.manager_url(self.fixture("Sky Observatory")), CARDS, height=1400)["cards"]["Sky Observatory"]
        self.assertEqual(sky["now"], "The builder is fixing the 2 problems the reviewer found.")
        self.assertIn("Step 19 of 26 · Repair", sky["step"])
        self.assertEqual(sky["bar"], {"now": "19", "max": "26"})
        self.assertIn("Builder · OpenAI Codex", sky["pills"])
        self.assertIn("Cost: not measured yet", sky["pills"], "the cost center has counted nothing for this project: never $0")
        self.assertEqual(sky["links"], ["Open the story →", "History (2 tasks)", "Ask about it"])
        self.assertIsNone(sky["tell"])
        self.home_reset()
        paused = self.run_probe(self.manager_url(self.fixture("Marketing studio")), CARDS, height=1400)["cards"]["Marketing studio"]
        self.assertEqual(paused["now"], "Paused safely. Nothing is lost.")
        self.assertEqual(paused["links"], [], "a paused project offers Resume, not links that would resume it silently")
        self.assertIn("Resume project", paused["actions"])
        self.home_reset()
        fresh = self.run_probe(self.manager_url(self.fixture("Fresh idea")), CARDS, height=1400)["cards"]["Fresh idea"]
        self.assertEqual(fresh["now"], "No work has been started yet.")
        self.assertEqual(fresh["tell"], {"text": "Tell the builder what to build", "act": "view", "go": "#start-direction"})   # it is the open project: straight to its board
        self.assertNotIn("History", " ".join(fresh["links"]), "no tasks, no history link")

    def home_reset(self):
        import shutil
        shutil.rmtree(self.base / "home", ignore_errors=True)
        shutil.rmtree(self.base / "code", ignore_errors=True)

    def test_agent_text_is_shown_as_text_never_run_and_the_words_are_readable(self):
        result = self.run_probe(self.manager_url(self.fixture("Signed out")), CARDS, height=1400)
        self.assertEqual(result["lowContrast"], [], "every word on the new cards has at least 4.5:1 contrast")
        self.assertEqual(result["injected"], 0)
        self.assertTrue(result["pwned"])
        self.assertIn("<img src=x", result["cards"]["Signed out"]["next"], "the markup is visible as words")
        self.assertIn("signed out", result["cards"]["Signed out"]["now"])
        self.assertIn("run /login", result["cards"]["Signed out"]["next"])

    def test_the_advanced_menu_holds_working_controls_and_keeps_its_place_across_refreshes(self):
        home = self.fixture()
        body = r"""
await until(() => document.querySelectorAll('#projects .project').length >= 5);
const list = document.querySelector('#projects');
const card = [...document.querySelectorAll('#projects .project')].find(node => node.querySelector('h3').textContent.trim() === 'Fresh idea');
const summary = card.querySelector('details.adv > summary');
summary.focus(); summary.click();   // opens the menu with the keyboard focus on it
await wait(200);
list.scrollTop = 120;
const before = {open: card.querySelector('details.adv').open, scroll: list.scrollTop};
await wait(9500);   // the page refreshes every 4 seconds: at least two refreshes happen here
const again = [...document.querySelectorAll('#projects .project')].find(node => node.querySelector('h3').textContent.trim() === 'Fresh idea');
out.before = before;
out.after = {open: again.querySelector('details.adv').open, scroll: list.scrollTop, focusOnSummary: document.activeElement === again.querySelector('details.adv > summary'), sameNode: again === card};
again.querySelector('[data-act=machine]').click();
await until(() => document.querySelector('#machine-dialog').open);
out.machineDialog = {open: document.querySelector('#machine-dialog').open, title: document.querySelector('#machine-title').textContent};
document.querySelector('#machine-close').click();
await wait(200);
again.querySelector('[data-act=remove]').click();
await until(() => document.querySelector('#remove-dialog').open);
out.removeDialog = {open: document.querySelector('#remove-dialog').open, name: document.querySelector('#remove-project-name').textContent, text: document.querySelector('#remove-dialog').innerText, folder: document.querySelector('#remove-project-folder').textContent};
"""
        result = self.run_probe(self.manager_url(home), body, height=500, wait=120)
        self.assertEqual(result["before"]["open"], True)
        self.assertEqual(result["after"]["open"], True, "an open Advanced menu stays open when the page refreshes")
        self.assertEqual(result["after"]["scroll"], result["before"]["scroll"], "the scroll position is kept")
        self.assertTrue(result["after"]["focusOnSummary"], "the keyboard focus is kept")
        self.assertIn("Fresh idea", result["machineDialog"]["title"])
        self.assertEqual(result["removeDialog"]["name"], "Fresh idea")
        self.assertIn("Delete project?", result["removeDialog"]["text"])
        self.assertIn("is not deleted", result["removeDialog"]["text"], "the dialog says plainly that the folder is left alone")
        self.assertTrue(result["removeDialog"]["folder"].startswith("/"), "the dialog names the folder that stays")

    def test_phone_width_has_no_sideways_scroll_and_big_enough_buttons(self):
        body = r"""
await until(() => document.querySelectorAll('#projects .project').length >= 5);
await wait(400);
const box = node => node.getBoundingClientRect();
out.viewport = innerWidth;
out.scroll = {docW: document.scrollingElement.scrollWidth, clientW: document.scrollingElement.clientWidth, listW: document.querySelector('#projects').scrollWidth, listClient: document.querySelector('#projects').clientWidth};
out.heights = {need: Math.min(...[...document.querySelectorAll('#needs-banner button')].map(b => box(b).height)),
               links: Math.min(...[...document.querySelectorAll('.card-links button')].map(b => box(b).height))};
out.overflowing = [...document.querySelectorAll('#projects *, #needs-banner *')].filter(n => box(n).right > innerWidth + 1 && box(n).width > 0).map(n => n.className || n.tagName).slice(0, 5);
"""
        result = self.run_probe(self.manager_url(self.fixture("Weather app")), body, width=375, height=900)
        self.assertLessEqual(result["viewport"], 500, "the window really is phone width: " + str(result["viewport"]))
        self.assertLessEqual(result["scroll"]["docW"], result["scroll"]["clientW"] + 1, result["scroll"])
        self.assertLessEqual(result["scroll"]["listW"], result["scroll"]["listClient"] + 1, result["scroll"])
        self.assertEqual(result["overflowing"], [], "nothing pokes out of the page on a phone")
        for name, height in result["heights"].items():
            self.assertGreaterEqual(height, 43.5, f"{name} buttons are at least 44px tall: {result['heights']}")


if __name__ == "__main__":
    unittest.main()
