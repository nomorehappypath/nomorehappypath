# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Costs page figures (Mission Control redesign, slice 4): the three tiles and the table cells, computed from what was counted, never a bare $0.00.

Run:  PYTHONPATH=. python3 -m unittest tests.test_cost_center_costs_page -v
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from harness import cost_center
from tests.ledger_fixture_support import NOW, write_ledger

M = 1_000_000


class CostsPageFigureTests(unittest.TestCase):
    def setUp(self):
        cost_center.configure(None)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.board = Path(self.tmp.name) / "board"

    def tiles(self, *boards: Path):
        return cost_center.rollup([cost_center.summarize(board) for board in boards], now=NOW)

    def test_nothing_counted_says_not_measured_yet_in_every_tile(self):
        empty = Path(self.tmp.name) / "none"
        tiles = self.tiles(empty)
        for name in ("week", "average", "split"):
            self.assertEqual(tiles[name]["value"], "not measured yet", name)
            self.assertNotIn("$", tiles[name]["value"] + tiles[name]["note"])

    def test_measured_work_adds_up_and_the_split_uses_only_counted_roles(self):
        write_ledger(self.board, buckets={("delivery", "T1"): [M, 0, 0, 0], ("reviewer", "T1"): [0, M // 2, 0, 0], ("delivery", "T2"): [2 * M, 0, 0, 0],
                                          ("cto", ""): [M // 2, 0, 0, 0]}, active={"T1": 1, "T2": 2}, limits={"T1": (10.0, 24.0)})
        tiles = self.tiles(self.board)
        self.assertEqual(tiles["week"]["value"], "$11.00")          # T1 $7 + T2 $4; the CTO overhead belongs to no task
        self.assertEqual(tiles["average"]["value"], "$5.50")
        self.assertEqual(tiles["split"]["value"], "Builder 50% · Reviewer 42% · CTO 8%")
        view = cost_center.overview(self.board)
        self.assertEqual((view["builder"], view["reviewer"], view["cto"], view["total"]), ("$6.00", "$5.00", "$1.00", "$12.00"))
        first = {item["task"]: item for item in view["tasks"]}["T1"]
        self.assertEqual((first["builder"], first["reviewer"], first["total"]), ("$2.00", "$5.00", "$7.00"))
        self.assertEqual(first["window"], "about +14% of your 5-hour window")

    def test_partly_measured_names_the_coverage_and_old_tasks_stay_out_of_this_week(self):
        write_ledger(self.board, buckets={("delivery", "OLD"): [M, 0, 0, 0], ("delivery", "NEW"): [M, 0, 0, 0]},
                     active={"OLD": 30, "NEW": 1}, gemini={"GEM": 1}, partial={"HALF": "the session file was too large"})
        tiles = self.tiles(self.board)
        self.assertEqual(tiles["week"]["value"], "$2.00", "only NEW ran in the last 7 days")
        self.assertEqual(tiles["average"]["value"], "$2.00")
        self.assertIn("of 4", tiles["average"]["note"], "2 measured tasks of the 4 the project has")
        view = cost_center.overview(self.board)
        self.assertIn("Counted for 2 of 4 tasks measured.", view["notes"])
        self.assertIn("Excludes Gemini: it is not measured yet.", view["notes"])
        rows = {item["task"]: item for item in view["tasks"]}
        self.assertEqual((rows["GEM"]["builder"], rows["GEM"]["total"]), ("not measured", "not measured"))
        self.assertTrue(any("Gemini (builder)" in note for note in rows["GEM"]["notes"]))
        self.assertTrue(any("partial because the session file was too large" in note for note in rows["HALF"]["notes"]))
        self.assertEqual(rows["HALF"]["total"], "not measured", "a task with only a partial reading never shows $0.00")

    def test_a_week_with_only_unmeasured_work_is_not_measured_not_zero(self):
        write_ledger(self.board, buckets={("delivery", "OLD"): [M, 0, 0, 0]}, active={"OLD": 30}, gemini={"GEM": 1})
        tiles = self.tiles(self.board)
        self.assertEqual(tiles["week"]["value"], "not measured yet")
        self.assertIn("1 task worked on", tiles["week"]["note"])
        self.assertEqual(tiles["average"]["value"], "$2.00")

    def test_only_one_role_counted_names_the_missing_roles(self):
        write_ledger(self.board, buckets={("delivery", "T1"): [M, 0, 0, 0]}, active={"T1": 1})
        self.assertEqual(self.tiles(self.board)["split"], {"value": "Builder 100%", "note": "of what was counted; Reviewer, CTO not measured yet"})


if __name__ == "__main__":
    unittest.main()
