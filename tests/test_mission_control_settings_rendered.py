# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Rendered proof (headless Chrome, the real manager) for the regrouped Settings page of the Mission Control redesign, slice 1.

Who does the work / Safety nets / Costs / Project chat / Advanced; every existing control keeps its id and its endpoint; the same-company
swap and the refused-Save message still work; the Advanced limits are shown read-only; 375px phone width.

Run:  PYTHONPATH=. python3 -m unittest tests.test_mission_control_settings_rendered -v
"""
from __future__ import annotations

import json
import unittest

from harness import global_settings
from tests.mission_control_support import RenderedCase

# Every id and data attribute the Settings page had before the regroup (what the old tests and the endpoints rely on).
OLD_IDS = ["#settings-form", "#settings-fields", "#recovery-fields", "#settings-notices", "#settings-error", "#settings-save", "#openai-card", "#openai-badge",
           "#openai-detail", "#openai-key", "#openai-save", "#openai-test", "#openai-remove", "#openai-result"]
ROLE_IDS = [f"#{kind}-{role}" for role in ("delivery", "reviewer", "cto") for kind in ("provider", "model", "effort")]

PAGE = r"""
await until(() => document.querySelectorAll('[data-settings-role]').length === 3 && document.querySelector('[data-recovery-card]'));
await wait(300);
const form = document.querySelector('#settings-form');
out.sections = [...form.children].map(node => (node.querySelector('h2, summary') || {}).textContent).filter(Boolean).map(text => text.trim());
out.roleHeadings = [...document.querySelectorAll('[data-settings-role] h2')].map(node => node.textContent.trim());
out.ids = {old: ${OLD_IDS}.map(selector => [selector, !!document.querySelector(selector)]), roles: ${ROLE_IDS}.map(selector => [selector, !!document.querySelector(selector)]),
           tests: document.querySelectorAll('[data-settings-test]').length, recovery: document.querySelectorAll('[data-recovery-model]').length, retry: document.querySelectorAll('[data-reviewer-retry-model]').length,
           customs: document.querySelectorAll('[data-setting-custom]').length, switches: document.querySelectorAll('[data-settings-switch]').length};
out.where = {who: !!document.querySelector('#settings-who #settings-fields'), safety: !!document.querySelector('#settings-safety [data-recovery-card]'), chat: !!document.querySelector('#settings-form > #openai-card')};
out.heartbeat = document.querySelector('#cto-heartbeat-line').textContent;
out.costs = {copy: document.querySelector('#costs-settings-copy').textContent, link: !!document.querySelector('#settings-costs button[data-page=costs]')};
const advanced = document.querySelector('#settings-advanced');
out.advancedClosed = !advanced.open;
advanced.open = true;
out.plumbing = {where: document.querySelector('#plumbing-where').textContent, rows: [...document.querySelectorAll('#plumbing-view tbody tr')].map(r => [...r.children].map(c => c.textContent.trim())),
                inputs: document.querySelectorAll('#plumbing-view input, #plumbing-view select, #plumbing-view textarea').length};
out.save = document.querySelector('#settings-save').textContent;
// the same-company swap: pick the builder's company for the reviewer, the builder moves aside and says so
const reviewer = document.querySelector('#provider-reviewer'), builder = document.querySelector('#provider-delivery');
out.before = {builder: builder.value, reviewer: reviewer.value};
reviewer.value = builder.value; reviewer.dispatchEvent(new Event('change', {bubbles: true}));
await wait(200);
out.swap = {builder: builder.value, reviewer: reviewer.value, note: document.querySelector('[data-settings-switch=delivery]').textContent, noteShown: !document.querySelector('[data-settings-switch=delivery]').hidden};
// a Save with both on one company (set without the swap) is refused, in plain words
builder.value = reviewer.value;
document.querySelector('#settings-save').click();
await until(() => document.querySelector('#settings-error').textContent);
out.refused = document.querySelector('#settings-error').textContent;
"""


class RenderedSettingsTests(RenderedCase):
    def test_the_five_groups_keep_every_setting_and_the_same_company_rule(self):
        home = self.base / "home"
        home.mkdir()
        body = PAGE.replace("${OLD_IDS}", json.dumps(OLD_IDS)).replace("${ROLE_IDS}", json.dumps(ROLE_IDS))
        result = self.run_probe(self.manager_url(home), body, path="/?page=settings", height=1400)
        self.assertEqual(result["sections"], ["Who does the work", "Safety nets", "Costs", "Project chat key", "Advanced (time limits, review input limits, check-in timing)"])
        self.assertEqual(result["roleHeadings"], ["Builder", "Independent reviewer", "CTO"])
        self.assertTrue(all(present for _s, present in result["ids"]["old"]), result["ids"]["old"])
        self.assertTrue(all(present for _s, present in result["ids"]["roles"]), result["ids"]["roles"])
        self.assertEqual((result["ids"]["tests"], result["ids"]["customs"], result["ids"]["switches"]), (3, 3, 3))
        self.assertEqual((result["ids"]["recovery"], result["ids"]["retry"]), (3, 3), "both safety nets, one per AI company")
        self.assertEqual(result["where"], {"who": True, "safety": True, "chat": True})
        self.assertIn("check-in every", result["heartbeat"])
        self.assertIn("not a bill", result["costs"]["copy"])
        self.assertTrue(result["costs"]["link"])
        self.assertTrue(result["advancedClosed"])
        self.assertEqual(result["plumbing"]["inputs"], 0, "the plumbing limits are read-only here")
        limits = {row[0]: row for row in result["plumbing"]["rows"]}
        self.assertEqual(len([name for name in global_settings.PLUMBING_LIMITS]), 17)
        heartbeat = limits["How often the CTO checks in while a task is live"]
        default, low, high = global_settings.PLUMBING_LIMITS["cto_heartbeat_seconds"]
        self.assertEqual(heartbeat[1], f"{default} s")
        self.assertIn(f"{low} to {high} s", heartbeat[2])
        self.assertIn("edit the settings file", result["plumbing"]["where"])
        self.assertEqual(result["save"], "Save agent settings")
        self.assertNotEqual(result["swap"]["builder"], result["swap"]["reviewer"], "the other role moved to the other company")
        self.assertTrue(result["swap"]["noteShown"])
        self.assertIn("Builder switched to", result["swap"]["note"])
        self.assertIn("different AI company", result["swap"]["note"])
        self.assertIn("must be different AI companies", result["refused"])
        self.assertIn("Not saved.", result["refused"])

    def test_phone_width_has_no_sideways_scroll(self):
        home = self.base / "home"
        home.mkdir()
        body = r"""
await until(() => document.querySelectorAll('[data-settings-role]').length === 3);
document.querySelector('#settings-advanced').open = true;
await wait(400);
const page = document.querySelector('#settings-page');
out.viewport = innerWidth;
out.scroll = {w: page.scrollWidth, client: page.clientWidth, doc: document.scrollingElement.scrollWidth, docClient: document.scrollingElement.clientWidth};
out.overflowing = [...page.querySelectorAll('*')].filter(n => n.getBoundingClientRect().right > innerWidth + 1 && n.getBoundingClientRect().width > 0).map(n => n.id || n.className || n.tagName).slice(0, 6);
"""
        result = self.run_probe(self.manager_url(home), body, path="/?page=settings", width=375, height=900)
        self.assertLessEqual(result["viewport"], 500)
        self.assertLessEqual(result["scroll"]["w"], result["scroll"]["client"] + 1, result["scroll"])
        self.assertLessEqual(result["scroll"]["doc"], result["scroll"]["docClient"] + 1, result["scroll"])
        self.assertEqual(result["overflowing"], [])


if __name__ == "__main__":
    unittest.main()
