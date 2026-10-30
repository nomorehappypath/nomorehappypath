# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Build-off eval (task 2026-10-27_eval-buildoff): stub modes, the hidden judge passes the reference app and fails the naive one,
builder workspaces hold only brief.md, results are never overwritten, and the processes the judge starts are cleaned up. macOS only."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

BUILDOFF = Path(__file__).resolve().parents[1] / "evals" / "buildoff"
if not BUILDOFF.is_dir():
    raise unittest.SkipTest("build-off eval: only where evals/buildoff ships")
sys.path.insert(0, str(BUILDOFF))
import weather_stub as ws  # noqa: E402
import run_buildoff  # noqa: E402
from judge import runner  # noqa: E402


def fetch(stub, path):
    try:
        with urllib.request.urlopen(stub.url + path, timeout=10) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as err:
        return err.code, err.read()


def forecast(stub, key="zurich"):
    city = ws.CITIES[key]
    return fetch(stub, "/forecast?lat=%s&lon=%s" % (city["lat"], city["lon"]))


class StubTests(unittest.TestCase):
    def setUp(self):
        self.stub = ws.WeatherStub().start()
        self.addCleanup(self.stub.stop)

    def test_normal_geo_and_forecast(self):
        status, body = fetch(self.stub, "/geo?q=Z%C3%BCrich")
        place = json.loads(body)["results"][0]
        self.assertEqual((status, place["name"], place["country_code"], place["timezone"]), (200, "Zürich", "CH", "Europe/Zurich"))
        self.assertEqual(json.loads(fetch(self.stub, "/geo?q=zurich")[1])["results"][0]["name"], "Zürich")
        self.assertEqual(json.loads(fetch(self.stub, "/geo?q=Nowhere")[1]), {"results": []})
        fc = json.loads(forecast(self.stub)[1])
        self.assertEqual(fc["current"]["wind_speed_ms"], 5.0)
        self.assertEqual(len(fc["daily"]), 8)                       # yesterday, today and six more
        self.assertEqual(fc["daily"][0]["time"], "2026-10-25T23:00:00Z")   # yesterday begins at local midnight = 23:00 UTC the day before

    def test_cities_cover_the_hard_cases(self):
        zones = {k: c["tz"] for k, c in ws.CITIES.items()}
        for key in ("new york", "london", "toronto", "kolkata", "kathmandu", "auckland", "sydney", "melbourne"):
            self.assertIn(key, zones)
        self.assertEqual(ws.local_today("auckland").isoformat(), "2026-10-28")    # UTC is still Oct 27
        self.assertEqual({ws.CITIES[k]["country"] for k in ("new york", "london", "toronto")}, {"US", "GB", "CA"})

    def test_script_city_is_returned_with_its_markup(self):
        place = json.loads(fetch(self.stub, "/geo?q=%3Cscript%3Ealert(1)%3C/script%3E")[1])["results"][0]
        self.assertEqual(place["name"], "<script>alert(1)</script>")

    def test_down_is_connection_refused_and_comes_back(self):
        self.stub.set_mode("down")
        with self.assertRaises(urllib.error.URLError):
            fetch(self.stub, "/geo?q=zurich")
        self.stub.set_mode("normal")
        self.assertEqual(fetch(self.stub, "/geo?q=zurich")[0], 200)

    def test_garbage_and_500_respect_scope(self):
        self.stub.set_mode("garbage", scope=("forecast",))
        self.assertEqual(json.loads(fetch(self.stub, "/geo?q=zurich")[1])["results"][0]["name"], "Zürich")
        with self.assertRaises(ValueError):
            json.loads(forecast(self.stub)[1])
        self.stub.set_mode("error500")
        self.assertEqual(forecast(self.stub)[0], 500)

    def test_slow_holds_then_releases(self):
        self.stub.set_mode("slow", delay=30)
        started = time.time()
        import threading
        holder = threading.Thread(target=fetch, args=(self.stub, "/geo?q=zurich"), daemon=True)
        holder.start()
        time.sleep(0.5)
        self.assertTrue(holder.is_alive())
        self.stub.set_mode("normal")
        holder.join(5)
        self.assertFalse(holder.is_alive())
        self.assertLess(time.time() - started, 10)

    def test_wind_modes(self):
        for mode, degrees in (("dir_0", 0), ("dir_360", 360), ("dir_350", 350)):
            self.stub.set_mode(mode)
            fc = json.loads(forecast(self.stub)[1])
            self.assertEqual({fc["current"]["wind_direction_deg"]} | {d["wind_direction_deg"] for d in fc["daily"]}, {degrees})
        self.stub.set_mode("calm")
        self.assertEqual(json.loads(forecast(self.stub)[1])["current"]["wind_speed_ms"], 0.0)
        self.stub.set_mode("calm_nodir")
        cur = json.loads(forecast(self.stub)[1])["current"]
        self.assertNotIn("wind_direction_deg", cur)
        self.assertNotIn("wind_gust_ms", cur)
        self.stub.set_mode("no_gusts")
        fc = json.loads(forecast(self.stub)[1])
        self.assertFalse(any("wind_gust_ms" in d for d in fc["daily"]))
        self.stub.set_mode("missing_wind")
        fc = json.loads(forecast(self.stub)[1])
        self.assertNotIn("wind_speed_ms", fc["current"])
        self.assertTrue(any(d.get("wind_speed_ms", 1) is None for d in fc["daily"]))
        self.assertTrue(any("wind_speed_ms" not in d for d in fc["daily"]))
        self.stub.set_mode("short_daily")
        self.assertEqual(len(json.loads(forecast(self.stub)[1])["daily"]), 2)

    def test_version_changes_the_forecast(self):
        before = json.loads(forecast(self.stub)[1])["current"]["temperature_c"]
        self.stub.set_version(2)
        self.assertNotEqual(json.loads(forecast(self.stub)[1])["current"]["temperature_c"], before)

    def test_dst_days_have_the_right_clock(self):
        # New York clocks go back on 2026-11-01: that day's local midnight is 04:00Z, the next day's is 05:00Z.
        times = {d["time"] for d in ws.forecast_for("new york")["daily"]}
        self.assertIn("2026-11-01T04:00:00Z", times)
        self.assertIn("2026-11-02T05:00:00Z", times)
        # Sydney clocks go forward on 2026-10-04: midnight Oct 4 is 14:00Z Oct 3, midnight Oct 5 is 13:00Z Oct 4.
        times = {d["time"] for d in ws.forecast_for("sydney")["daily"]}
        self.assertIn("2026-10-03T14:00:00Z", times)
        self.assertIn("2026-10-04T13:00:00Z", times)

    def test_no_value_sits_on_a_rounding_tie(self):
        """A whole-number or one-decimal conversion must never be a coin toss between two correct implementations."""
        def near(x):
            return abs((x - int(x // 1)) - 0.5) < 0.005
        for key in ws.CITIES:
            fc = ws.forecast_for(key)
            for part in [fc["current"]] + fc["daily"]:
                for w in (part.get("wind_speed_ms"), part.get("wind_gust_ms")):
                    if w is not None:
                        self.assertFalse(near(w * 3.6) or near(w * 2.2369363), (key, w))
                for t in (part.get("temperature_c"), part.get("temp_min_c"), part.get("temp_max_c")):
                    if t is not None:
                        self.assertFalse(near((t * 9 / 5 + 32) * 10), (key, t))


class JudgeTests(unittest.TestCase):
    """The judge must pass the careful reference and fail the quick build, family by family."""

    @classmethod
    def setUpClass(cls):
        cls.scratch = Path(tempfile.mkdtemp(prefix="nmhp-buildoff-test-"))
        cls.results = {}
        for name in ("reference_app", "naive_app"):
            folder = cls.scratch / name
            folder.mkdir()
            shutil.copyfile(BUILDOFF / "judge" / (name + ".py"), folder / "app.py")
            cls.results[name] = runner.run(folder, workdir=cls.scratch / ("work-" + name))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.scratch, ignore_errors=True)

    def test_reference_passes_everything(self):
        failed = [(r["id"], r["detail"]) for r in self.results["reference_app"]["rows"] if not r["passed"]]
        self.assertEqual(failed, [])
        self.assertEqual(self.results["reference_app"]["crashes"], 0)
        self.assertGreaterEqual(len(self.results["reference_app"]["rows"]), 60)

    def test_naive_fails_a_meaningful_number_in_every_hard_family(self):
        rows = self.results["naive_app"]["rows"]
        passed = sum(1 for r in rows if r["passed"])
        self.assertLessEqual(passed, len(rows) // 3)
        by_family = {}
        for r in rows:
            by_family.setdefault(r["family"], []).append(r["passed"])
        for family in ("correctness", "units", "local time", "resilience", "input", "safety", "stress", "staleness"):
            self.assertIn(False, by_family[family], family)
        # the new local-time and units checks specifically
        for check_id in ("U1", "U2", "U5", "U6", "T1", "T2", "T3", "T4", "T5", "T6", "T7", "T8"):
            self.assertFalse(next(r for r in rows if r["id"] == check_id)["passed"], check_id)

    def test_every_check_has_id_family_and_one_plain_line(self):
        ids = [c["id"] for c in runner.REGISTRY]
        self.assertEqual(len(ids), len(set(ids)))
        for c in runner.REGISTRY:
            self.assertTrue(c["line"].strip() and "\n" not in c["line"], c["id"])
            self.assertEqual(runner.FAMILIES[c["id"][0]], c["family"])

    def test_judge_started_apps_are_gone(self):
        for name in ("reference_app", "naive_app"):
            ledger = [json.loads(line) for line in (self.scratch / ("work-" + name) / "pids.json").read_text().splitlines()]
            self.assertTrue(ledger)
            for entry in ledger:
                self.assertEqual(runner.start_time(entry["pid"]), "", "pid %s of %s is still running" % (entry["pid"], name))


class UnitLabelFairnessTests(unittest.TestCase):
    """U10/U11 judge the unit by its clear association with the number, not by one literal spelling."""

    TEMP_OLD = 'e(show(cur["temp"], tu))'
    WIND_OLD = 'e(show(cur["wind_speed"], " " + wu))'

    def run_variant(self, temp_new, wind_new):
        source = (BUILDOFF / "judge" / "reference_app.py").read_text()
        self.assertIn(self.TEMP_OLD, source)
        self.assertIn(self.WIND_OLD, source)
        source = source.replace(self.TEMP_OLD, temp_new).replace(self.WIND_OLD, wind_new)
        tmp = Path(tempfile.mkdtemp(prefix="nmhp-buildoff-test-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        (tmp / "app").mkdir()
        (tmp / "app" / "app.py").write_text(source)
        result = runner.run(tmp / "app", workdir=tmp / "work", only=["U10", "U11"])
        self.assertEqual(result["crashes"], 0)
        return {r["id"]: r for r in result["rows"]}

    def test_units_labelled_in_a_separate_element_pass(self):
        rows = self.run_variant('"<b>%s</b> <i>(temperature units %s)</i>" % (cur["temp"], tu)',
                                '"<b>%s</b> <i>(wind units: %s)</i>" % (cur["wind_speed"], wu)')
        self.assertTrue(rows["U10"]["passed"], rows["U10"]["detail"])
        self.assertTrue(rows["U11"]["passed"], rows["U11"]["detail"])

    def test_units_in_the_same_table_cell_but_a_different_tag_pass(self):
        rows = self.run_variant('"<span class=v>%s</span><span class=u title=\'%s\'>%s</span>" % (cur["temp"], tu, tu)',
                                '"<span>%s</span> <span class=u>%s</span>" % (cur["wind_speed"], wu)')
        self.assertTrue(rows["U10"]["passed"], rows["U10"]["detail"])
        self.assertTrue(rows["U11"]["passed"], rows["U11"]["detail"])

    def test_a_number_without_any_unit_still_fails(self):
        rows = self.run_variant('str(cur["temp"])', 'str(cur["wind_speed"])')
        self.assertFalse(rows["U10"]["passed"])
        self.assertFalse(rows["U11"]["passed"])

    def test_the_wrong_number_still_fails(self):
        rows = self.run_variant('"%s %s" % (cur["temp"] + 1, tu)', '"%s %s" % (cur["wind_speed"] + 1, wu)')
        self.assertFalse(rows["U10"]["passed"])
        self.assertFalse(rows["U11"]["passed"])

    def test_brief_states_the_rules_the_judge_uses(self):
        text = (BUILDOFF / "brief.md").read_text()
        for phrase in ("without JavaScript", "never older than 5 seconds", "with its unit"):
            self.assertIn(phrase, text)


class RunnerSafetyTests(unittest.TestCase):
    def setUp(self):
        from tests.environment_support import require_loopback
        require_loopback()

    def test_a_crashing_app_is_counted_and_restarted(self):
        with tempfile.TemporaryDirectory(prefix="nmhp-buildoff-test-") as tmp:
            folder = Path(tmp) / "crash"
            folder.mkdir()
            (folder / "app.py").write_text(
                "import os, socket\ns = socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)\n"
                "s.bind(('127.0.0.1', int(os.environ['PORT']))); s.listen(5)\nc, _ = s.accept(); os._exit(3)\n", encoding="utf-8")
            result = runner.run(folder, workdir=Path(tmp) / "work", only=["I1", "I2"])
            self.assertGreaterEqual(result["crashes"], 1)
            self.assertTrue(all(not r["passed"] for r in result["rows"]))
            self.assertTrue(any(r["crashed"] for r in result["rows"]))
            self.assertGreaterEqual(result["app_starts"], 2)           # restarted after the crash so later checks still run

    def test_stop_only_stops_the_recorded_process(self):
        with tempfile.TemporaryDirectory(prefix="nmhp-buildoff-test-") as tmp:
            app = runner.App(Path(tmp), Path(tmp) / "w", "http://127.0.0.1:1")
            other = subprocess.Popen(["sleep", "30"])
            try:
                app.proc, app.ledger = other, {"pid": other.pid, "started": "Mon Jan  1 00:00:00 2001"}   # start time does not match
                app.stop()
                self.assertEqual(other.poll(), None, "stopped a process whose start time did not match the record")
            finally:
                other.terminate()
                other.wait(5)


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="nmhp-buildoff-test-ws-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        for name in ("codex", "claude"):
            (self.tmp / name).mkdir()
        (self.tmp / "token").write_text("test-token", encoding="utf-8")
        self.homes = dict(codex_home=self.tmp / "codex", claude_home=self.tmp / "claude", token_file=self.tmp / "token")

    def test_builder_workspaces_hold_only_brief_md(self):
        for arm in ("nmhp", "plain"):
            run = run_buildoff.prepare(arm, BUILDOFF.parents[1], "t-" + arm, start_manager=False, **self.homes)
            self.addCleanup(shutil.rmtree, run, True)
            project = run / "project"
            self.assertEqual(sorted(p.name for p in project.iterdir()), [".git", "brief.md"])
            self.assertEqual((project / "brief.md").read_text(), (BUILDOFF / "brief.md").read_text())
            self.assertEqual(subprocess.run(["git", "-C", str(project), "rev-list", "--all", "--count"], capture_output=True, text=True).stdout.strip(), "0")
            record = json.loads((run / "run.json").read_text())
            self.assertEqual(record["isolation"]["claude_home"], str(self.tmp / "claude"))
            self.assertNotIn("test-token", (run / "run.json").read_text())
            self.assertNotIn(str(BUILDOFF / "judge"), (run / "task.txt").read_text())
        self.assertTrue((run.parent / run.name).exists())

    def test_both_arms_get_the_same_task_text(self):
        texts = []
        for arm in ("nmhp", "plain"):
            run = run_buildoff.prepare(arm, BUILDOFF.parents[1], "t", start_manager=False, **self.homes)
            self.addCleanup(shutil.rmtree, run, True)
            texts.append((run / "task.txt").read_text())
        self.assertEqual(texts[0], texts[1])

    def test_nmhp_arm_has_a_private_home_and_refuses_without_the_standing_sign_ins(self):
        run = run_buildoff.prepare("nmhp", BUILDOFF.parents[1], "t", start_manager=False, **self.homes)
        self.addCleanup(shutil.rmtree, run, True)
        self.assertTrue((run / "home").is_dir())
        self.assertNotEqual((run / "home").resolve(), Path.home().resolve() / ".harness-home")
        with self.assertRaises(SystemExit):
            run_buildoff.prepare("nmhp", BUILDOFF.parents[1], "t", start_manager=True, codex_home=self.tmp / "missing", claude_home=self.tmp / "claude",
                                 token_file=self.tmp / "token")

    def test_brief_gives_no_hint_of_the_judge(self):
        text = (BUILDOFF / "brief.md").read_text().lower()
        for word in ("judge", "hidden", "stress test", "edge case", "concurrent", "xss", "escape"):
            self.assertNotIn(word, text)

    def test_stop_deletes_the_workspace_and_refuses_foreign_folders(self):
        run = run_buildoff.prepare("plain", BUILDOFF.parents[1], "t", start_manager=False, **self.homes)
        run_buildoff.stop(run)
        self.assertFalse(run.exists())
        foreign = self.tmp / "keepme"
        foreign.mkdir()
        self.assertIn("Left alone", run_buildoff.stop(foreign))
        self.assertTrue(foreign.exists())


class ResultsTests(unittest.TestCase):
    def test_results_are_never_overwritten_and_compare_shows_both(self):
        with tempfile.TemporaryDirectory(prefix="nmhp-buildoff-test-") as tmp:
            folder = Path(tmp) / "app"
            folder.mkdir()
            shutil.copyfile(BUILDOFF / "judge" / "reference_app.py", folder / "app.py")
            root = Path(tmp) / "results"
            first = run_buildoff.judge_app(folder, "same", root, only=["C1"])
            second = run_buildoff.judge_app(folder, "same", root, only=["C1", "C2"])
            self.assertNotEqual(first, second)
            self.assertEqual(json.loads((first / "score.json").read_text())["total"], 1)       # the first sheet is untouched
            sheet = json.loads((second / "score.json").read_text())
            self.assertEqual((sheet["passed"], sheet["total"], sheet["crashes"]), (2, 2, 0))
            self.assertIn("correctness", sheet["families"])
            self.assertIn("| C1 |", (second / "score.md").read_text())
            text = run_buildoff.compare(first, second)
            self.assertIn("| Checks passed | 1 / 1 | 2 / 2 |", text)
            self.assertIn("C2", text)


if __name__ == "__main__":
    unittest.main()
