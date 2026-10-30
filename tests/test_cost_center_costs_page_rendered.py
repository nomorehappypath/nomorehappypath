# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Rendered proof (headless Chrome, the real manager) for the Costs page of the Mission Control redesign, slice 4.

Empty, partly measured, measured and Gemini cases; escaping of names; a phone-width window where the table scrolls inside its own box;
4.5:1 contrast; and a refresh that leaves an unchanged page alone.

Run:  PYTHONPATH=. python3 -m unittest tests.test_cost_center_costs_page_rendered -v
"""
from __future__ import annotations

import unittest
from pathlib import Path

from harness import cost_center
from tests.ledger_fixture_support import write_ledger
from tests.mission_control_support import RenderedCase

M = 1_000_000
EVIL = '<img src=x onerror="window.pwned=1">'

READ = r"""
await until(() => document.querySelectorAll('#costs-list .cost-table').length || document.querySelector('#costs-list .empty'));
await wait(300);
const text = node => node ? node.textContent.replace(/\s+/g, ' ').trim() : null;
out.tiles = Object.fromEntries([...document.querySelectorAll('.cost-tile')].map(t => [t.dataset.costTile, {value: text(t.querySelector('.cost-tile-value')), note: text(t.querySelector('.cost-tile-note'))}]));
out.projects = {};
for (const row of document.querySelectorAll('tr.cost-project')) {
  const cells = [...row.querySelectorAll('td')].map(text);
  out.projects[text(row.querySelector('th').firstChild)] = {builder: cells[0], reviewer: cells[1], cto: cells[2], total: cells[3], name: text(row.querySelector('th')), line: text(row.querySelector('[data-cost-line]'))};
}
out.tasks = {};
for (const row of document.querySelectorAll('tr.cost-task')) {
  const cells = [...row.querySelectorAll('td')].map(text);
  out.tasks[row.dataset.costTask] = {builder: cells[0], reviewer: cells[1], total: cells[3], window: cells[4], head: text(row.querySelector('th'))};
}
out.heads = [...document.querySelectorAll('.cost-table thead th')].map(text);
out.foot = text(document.querySelector('#costs-foot'));
out.active = document.querySelector('nav button[aria-current=page]').textContent.trim();
out.lowContrast = lowContrast(['.cost-tile-label', '.cost-tile-value', '.cost-tile-note', '.cost-table th', '.cost-table td', '.cost-sub', '.cost-foot', '.hero-copy']);
out.injected = document.querySelectorAll('#costs-list img, #costs-tiles img').length;
out.pwned = window.pwned === undefined;
"""


class RenderedCostsPageTests(RenderedCase):
    def fixture(self, projects: dict[str, dict | None]) -> Path:
        home = self.base / "home"
        for name, ledger in projects.items():
            entry = self.project(home, name, None)
            if ledger is not None:
                write_ledger(Path(entry["data_root"]) / "board", **ledger)
        return home

    def read(self, projects, *, body=READ, **view):
        cost_center.configure(None)
        return self.run_probe(self.manager_url(self.fixture(projects)), body, path="/?page=costs", **view)

    def test_nothing_measured_says_so_and_never_shows_a_zero(self):
        result = self.read({"Fresh idea": None})
        self.assertEqual(result["active"], "Costs")
        self.assertEqual(result["heads"], ["Project / task", "Builder", "Reviewer", "CTO", "Total", "Codex window"])
        for name in ("This week", "Average per task", "Split"):
            self.assertEqual(result["tiles"][name]["value"], "not measured yet", name)
        row = result["projects"]["Fresh idea"]
        self.assertEqual((row["builder"], row["reviewer"], row["cto"], row["total"]), ("not measured",) * 4)
        self.assertNotIn("$0", str(result), "an unmeasured project is never $0.00")
        self.assertIn("Gemini", result["foot"])

    def test_measured_and_partly_measured_projects_show_their_numbers_and_coverage(self):
        result = self.read({
            "Weather app": dict(buckets={("delivery", "FORECAST"): [M, 0, 0, 0], ("reviewer", "FORECAST"): [0, M // 2, 0, 0], ("cto", ""): [M // 2, 0, 0, 0]},
                                active={"FORECAST": 1}, limits={"FORECAST": (10.0, 24.0)}),
            "Sky Observatory": dict(buckets={("delivery", "PLANETS"): [2 * M, 0, 0, 0]}, active={"PLANETS": 2}, gemini={"MOON": 1}),
        })
        weather = result["projects"]["Weather app"]
        self.assertEqual((weather["builder"], weather["reviewer"], weather["cto"], weather["total"]), ("$2.00", "$5.00", "$1.00", "$8.00"))
        self.assertEqual(result["tasks"]["FORECAST"]["total"], "$7.00")
        self.assertEqual(result["tasks"]["FORECAST"]["window"], "about +14% of your 5-hour window")
        sky = result["projects"]["Sky Observatory"]
        self.assertEqual((sky["builder"], sky["reviewer"], sky["total"]), ("$4.00", "not measured", "$4.00"))
        self.assertIn("Counted for 1 of 2 tasks measured.", sky["name"])
        self.assertIn("Excludes Gemini", sky["name"])
        moon = result["tasks"]["MOON"]
        self.assertEqual((moon["builder"], moon["total"]), ("not measured", "not measured"), "Gemini work is not measured, never $0.00")
        self.assertIn("Gemini (builder): not measured yet", moon["head"])
        self.assertEqual(result["tiles"]["This week"]["value"], "$11.00")
        self.assertEqual(result["tiles"]["Average per task"]["value"], "$5.50", "$7 and $4 over the two measured tasks; MOON is not counted")
        self.assertEqual(result["tiles"]["Split"]["value"], "Builder 50% · Reviewer 42% · CTO 8%")

    def test_names_are_text_and_the_words_are_readable(self):
        result = self.read({EVIL: dict(buckets={("delivery", "T<b>1</b>"): [M, 0, 0, 0]}, active={"T<b>1</b>": 1})})
        self.assertEqual(result["injected"], 0)
        self.assertTrue(result["pwned"])
        self.assertIn(EVIL, result["projects"][EVIL]["name"], "the markup is visible as words")
        self.assertEqual(result["lowContrast"], [], "every word on the Costs page has at least 4.5:1 contrast")

    def test_phone_width_scrolls_the_table_inside_its_box_not_the_page(self):
        body = READ + r"""
const box = document.querySelector('.cost-box');
out.viewport = innerWidth;
out.scroll = {docW: document.scrollingElement.scrollWidth, clientW: document.scrollingElement.clientWidth, boxW: box.scrollWidth, boxClient: box.clientWidth};
out.tileOverflow = [...document.querySelectorAll('.cost-tile')].filter(n => n.getBoundingClientRect().right > innerWidth + 1).length;
"""
        result = self.read({"Weather app": dict(buckets={("delivery", "FORECAST"): [M, 0, 0, 0]}, active={"FORECAST": 1})}, body=body, width=375, height=900)
        self.assertLessEqual(result["viewport"], 500)
        self.assertLessEqual(result["scroll"]["docW"], result["scroll"]["clientW"] + 1, "the page itself never scrolls sideways")
        self.assertGreater(result["scroll"]["boxW"], result["scroll"]["boxClient"], "the table scrolls inside its own box")
        self.assertEqual(result["tileOverflow"], 0)

    def test_a_refresh_with_the_same_answer_leaves_the_page_alone(self):
        body = r"""
await until(() => document.querySelector('#costs-list .cost-table'));
const table = document.querySelector('#costs-list .cost-table');
const box = document.querySelector('.cost-box');
box.scrollLeft = 40;
await loadCosts(); await loadCosts();
out.same = table === document.querySelector('#costs-list .cost-table');
out.scrollKept = box === document.querySelector('.cost-box') && document.querySelector('.cost-box').scrollLeft;
"""
        result = self.read({"Weather app": dict(buckets={("delivery", "FORECAST"): [M, 0, 0, 0]}, active={"FORECAST": 1})}, body=body, width=375, height=900)
        self.assertTrue(result["same"], "the table was not rewritten")
        self.assertEqual(result["scrollKept"], 40, "the table's own scroll position is kept")


if __name__ == "__main__":
    unittest.main()
