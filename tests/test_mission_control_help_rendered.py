# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Rendered proof (headless Chrome, the real manager) for the rewritten Help page of the Mission Control redesign, slice 2.

'How a task works' in five phases with the 26 step titles, a search box that filters the topics, the old topics regrouped under seven
headings with every old section id and anchor kept, the topic links, contrast and the 375px phone width.

Run:  PYTHONPATH=. python3 -m unittest tests.test_mission_control_help_rendered -v
"""
from __future__ import annotations

import json
import unittest

from harness import story
from harness.project_manager_page import PAGE
from tests.mission_control_support import RenderedCase

# The 23 sections Help had before the regroup (the id each title carries, which the anchors and the old tests rely on) and the two ids linked from elsewhere.
OLD_TITLE_IDS = ["help-start-title", "help-reviewer-first-title", "help-setup-title", "help-limit-title", "help-version-title", "help-nohappy-title", "help-windows-vm-title",
                 "help-windows-tools-title", "help-reach-title", "help-key-title", "help-settings-title", "help-direction-title", "help-go-title", "help-direct-title",
                 "help-buttons-title", "help-progress-title", "help-accept-title", "help-history-title", "help-lifecycle-title", "help-limits-title", "help-trouble-title",
                 "help-legal-title", "help-chat-title"]
GROUPS = ["Getting started", "Who does the work", "Talking to agents", "Windows programs", "Costs and limits", "When something is stuck", "Legal"]

BODY = r"""
await until(() => document.querySelector('#help-page') && !document.querySelector('#help-page').hidden && document.querySelector('#help-search'));
const text = node => node.textContent.replace(/\s+/g, ' ').trim();
const visible = node => !!node && !node.closest('[hidden]') && node.getBoundingClientRect().width > 0;
const sectionsShown = () => [...document.querySelectorAll('#help-page .help-section')].filter(visible).map(n => n.querySelector('h2').id);
out.how = {title: text(document.querySelector('#help-how-title')), shown: visible(document.querySelector('#help-how')),
           phases: [...document.querySelectorAll('.help-phase')].map(p => ({when: text(p.querySelector('.help-phase-when')), title: text(p.querySelector('h3')), text: text(p.querySelector('p')), steps: [...p.querySelectorAll('ol li')].map(text), start: p.querySelector('ol').getAttribute('start')}))};
out.groups = [...document.querySelectorAll('#help-page .help-group-title')].map(text);
out.oldIds = ${OLD_IDS}.map(id => [id, !!document.getElementById(id)]);
out.anchors = ['help-windows-vm', 'help-windows-tools'].map(id => [id, !!document.getElementById(id)]);
out.sectionCount = document.querySelectorAll('#help-page .help-section').length;
out.inGroups = [...document.querySelectorAll('#help-page .help-group')].map(g => [g.id, g.querySelectorAll('.help-section').length]);
const input = document.querySelector('#help-search');
out.search = {label: (document.querySelector('label[for=help-search]') || {}).textContent, type: input.type};
out.allShown = sectionsShown().length;
const type = async value => { input.value = value; input.dispatchEvent(new Event('input', {bubbles: true})); await wait(60); };
await type('windows');
out.windows = {sections: sectionsShown(), status: text(document.querySelector('#help-search-status')), how: visible(document.querySelector('#help-how')), topics: visible(document.querySelector('#help-topics')),
               groups: [...document.querySelectorAll('#help-page .help-group')].filter(visible).map(g => g.id)};
await type('zzzqqq');
out.none = {sections: sectionsShown().length, status: text(document.querySelector('#help-search-status')), groups: [...document.querySelectorAll('#help-page .help-group')].filter(visible).length};
await type('requirements go ahead');
out.words = sectionsShown();
await type('');
out.cleared = {sections: sectionsShown().length, status: text(document.querySelector('#help-search-status')), how: visible(document.querySelector('#help-how')), topics: visible(document.querySelector('#help-topics'))};
const link = document.querySelector('#help-topics a[href="#help-group-windows-programs"]');
out.links = [...document.querySelectorAll('#help-topics a')].map(a => [a.getAttribute('href'), !!document.querySelector(a.getAttribute('href'))]);
out.linkHeight = Math.min(...[...document.querySelectorAll('#help-topics a')].map(a => a.getBoundingClientRect().height));
link.click(); await wait(300);
out.afterLink = {target: document.querySelector('#help-group-windows-programs h2').getBoundingClientRect().top < innerHeight};
out.lowContrast = lowContrast(['.help-how p', '.help-phase-when', '.help-phase h3', '.help-phase p', '.help-phase li', '.help-find label', '.help-search-status', '.help-topics strong', '.help-topics span', '.help-group-title']);
out.sideways = {docW: document.scrollingElement.scrollWidth, clientW: document.scrollingElement.clientWidth, pageW: document.querySelector('#help-page').scrollWidth, pageClient: document.querySelector('#help-page').clientWidth,
                out: [...document.querySelectorAll('#help-page *')].filter(n => n.getBoundingClientRect().right > innerWidth + 1 && n.getBoundingClientRect().width > 0 && !n.closest('pre, .help-table, table')).map(n => n.className || n.tagName).slice(0, 6)};
out.viewport = innerWidth;
"""


class RenderedHelpWalkthroughTests(RenderedCase):
    def read(self, **kw):
        home = self.base / "home"
        home.mkdir(exist_ok=True)
        return self.run_probe(self.manager_url(home), BODY.replace("${OLD_IDS}", json.dumps(OLD_TITLE_IDS)), path="/?page=help", **kw)

    def test_how_a_task_works_the_search_the_regrouped_topics_and_every_old_anchor(self):
        result = self.read(height=1000)
        self.assertEqual(result["how"]["title"], "How a task works")
        self.assertTrue(result["how"]["shown"])
        phases = result["how"]["phases"]
        self.assertEqual([p["title"] for p in phases], ["Describe and approve", "Tests come first", "Built and proven", "Reviews that end", "Try and accept"])
        self.assertIn("steps 1 to 6", phases[0]["when"])
        self.assertIn("steps 17 to 22", phases[3]["when"])
        every_step = [step for p in phases for step in p["steps"]]
        self.assertEqual(every_step, [story.step_title(n) for n in range(1, 27)], "the five phases list the 26 step titles of the Story tab, from one table")
        self.assertEqual([p["start"] for p in phases], ["1", "7", "12", "17", "23"])
        self.assertEqual(result["groups"], GROUPS)
        self.assertTrue(all(present for _id, present in result["oldIds"]), [i for i, present in result["oldIds"] if not present])
        self.assertTrue(all(present for _id, present in result["anchors"]))
        self.assertEqual(result["sectionCount"], 23, "every old section is still there")
        self.assertEqual(sum(count for _g, count in result["inGroups"]), 23)
        self.assertTrue(all(count >= 1 for _g, count in result["inGroups"]))
        self.assertEqual(result["search"], {"label": "Search help", "type": "search"})
        self.assertEqual(result["allShown"], 23)
        # search
        self.assertIn("help-windows-vm-title", result["windows"]["sections"])
        self.assertNotIn("help-start-title", result["windows"]["sections"])
        self.assertLess(len(result["windows"]["sections"]), 10)
        self.assertRegex(result["windows"]["status"], r"^\d+ help topics? match(es)?\.$")
        self.assertIn("help-group-windows-programs", result["windows"]["groups"])
        self.assertLess(len(result["windows"]["groups"]), 7, "a group with no matching topic is hidden")
        self.assertFalse(result["windows"]["how"], "the walkthrough does not mention Windows, so it gives way to the matches")
        self.assertFalse(result["windows"]["topics"], "the topic cards give way to the matches")
        self.assertEqual(result["none"], {"sections": 0, "status": "No help topic matches. Try fewer or different words.", "groups": 0})
        self.assertIn("help-go-title", result["words"], "every word must match: 'requirements go ahead' finds the Go ahead section")
        self.assertEqual(result["cleared"], {"sections": 23, "status": "", "how": True, "topics": True})
        self.assertTrue(all(present for _href, present in result["links"]))
        self.assertEqual(len(result["links"]), 7)
        self.assertGreaterEqual(result["linkHeight"], 43.5)
        self.assertTrue(result["afterLink"]["target"], "a topic card takes the owner to its group")
        self.assertEqual(result["lowContrast"], [], "every new word has at least 4.5:1 contrast")

    def test_phone_width_has_no_sideways_scroll_on_the_help_page(self):
        result = self.read(width=375, height=900)
        self.assertLessEqual(result["viewport"], 500)
        self.assertLessEqual(result["sideways"]["docW"], result["sideways"]["clientW"] + 1, result["sideways"])
        self.assertLessEqual(result["sideways"]["pageW"], result["sideways"]["pageClient"] + 1, result["sideways"])
        self.assertEqual(result["sideways"]["out"], [])

    def test_the_page_carries_no_unfilled_placeholder(self):
        self.assertNotIn("__HELP_PHASES__", PAGE)
        self.assertEqual(PAGE.count('class="help-phase"'), 5)


if __name__ == "__main__":
    unittest.main()
