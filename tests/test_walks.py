# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Tests that exist before the build for an app with screens (task 2026-11-24_tdd-walks).

The app is a real small web app, started by NMHP's own start code and walked in the real harness browser.

Run:  PYTHONPATH=. python3 -m unittest tests.test_walks -v
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from harness import board, contract_check, contract_screens, walks
from tests import test_contract_first_checks as cf

APP = '''
import html, os, pathlib
from http.server import BaseHTTPRequestHandler, HTTPServer
DATA = pathlib.Path(os.environ.get("DATA_DIR") or "data")
PAGE = """<!doctype html><title>Notes</title><h1>Notes</h1>
<label for=n>Your name</label><input id=n>
<label>Day <input type=date id=d></label>
<label>Kind <select id=k><option>Please pick</option><option>Lease dispute</option></select></label>
<label for=f>Add documents</label><input type=file multiple id=f style="display:none">
<button id=b>Save note</button><div id=o></div>
<script>
document.getElementById('b').onclick = async () => {
  const names = Array.from(document.getElementById('f').files).map((f) => f.name).join('+');
  const v = (i) => document.getElementById(i).value;
  const r = await fetch('/save', {method: 'POST', body: [v('n'), v('d'), v('k'), names].join(' | ')});
  const text = await r.text();
  setTimeout(() => { document.getElementById('o').textContent = text; }, 7000);
};
</script>"""
class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def say(self, body):
        body = body.encode(); self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
    def do_GET(self): self.say(PAGE)
    def do_POST(self):
        text = self.rfile.read(int(self.headers["Content-Length"])).decode()
        DATA.mkdir(exist_ok=True); (DATA / "note.txt").write_text(text)
        self.say("Saved: " + text)
HTTPServer(("127.0.0.1", int(os.environ["PORT"])), H).serve_forever()
'''
STEPS = [{"open": "/"}, {"fill": {"field": "Your name", "text": "Maya"}}, {"fill": {"field": "Day", "text": "2026-11-24"}},
         {"choose": {"field": "Kind", "option": "Lease dispute"}}, {"upload": {"field": "Add documents", "files": ["lease.pdf", "extra.txt"]}},
         {"click": {"text": "Save note"}}, {"wait_for": {"text": "Saved: Maya | 2026-11-24 | Lease dispute | lease.pdf+extra.txt", "seconds": 20}}]
SCREEN = (b"<h1>Notes</h1><label for=n>Your name</label><input id=n><label>Day <input type=date></label><label>Kind <select><option>Lease dispute"
          b"</option></select></label><label for=f>Add documents</label><input id=f type=file><button><span>Save</span> note</button>")


def walk(steps=None, example=1):
    return {"example": example, "walk": {"steps": steps or STEPS, "files": {"extra.txt": "hello"}}}


def stored(steps=None):
    return contract_check.parse_checks([walk(steps)], "requirement 1", examples=1)[0][0]


class WalkData(unittest.TestCase):
    def test_a_walk_is_a_counted_check_with_a_plain_sentence(self):
        check = stored()
        self.assertTrue(contract_check.check_counted(check))
        self.assertTrue(walks.is_walk(check))
        self.assertIn("Type “Maya” in “Your name”.", check["walk"])
        self.assertIn("Within 20 seconds, see", check["walk"])

    def test_the_labels_come_from_the_agreed_screens(self):
        words = walks.screen_words([SCREEN])
        self.assertEqual(walks.label_problem(stored(), words), "")
        problem = walks.label_problem(stored([{"click": {"text": "Store it"}}, {"expect": "x"}]), words)
        self.assertIn("Store it", problem)
        self.assertIn("no agreed screen shows", problem)
        named = [{"fill": {"field": "Your name", "text": "Suite 210"}}, {"click": {"text": "Suite 210"}}, {"upload": {"files": ["a.txt"]}}, {"expect": "x"}]
        self.assertEqual(walks.label_problem(stored(named), words), "")

    def test_the_time_is_todays_allowance_plus_the_waits(self):
        self.assertEqual(walks.budget([{"open": "/"}, {"expect": "x"}]), 600)
        self.assertEqual(walks.budget(STEPS), 620)
        self.assertEqual(walks.budget([{"wait_for": {"text": "x", "seconds": 600}}]), 1200)


class WalkRuns(unittest.TestCase):
    """The real app, NMHP's own start, the real harness browser."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.source = self.base / "app"
        self.source.mkdir()
        (self.source / "lease.pdf").write_bytes(b"%PDF-1.4 the owner's lease")
        for target in ("harness.walks.owner_env.read",):
            patch = mock.patch(target, return_value={"PATH": "/usr/bin:/bin:/opt/homebrew/bin:/usr/local/bin"})
            patch.start()
            self.addCleanup(patch.stop)

    def run_walk(self, steps=None):
        return walks.run(self.base / "root", self.source, stored(steps), {"real_data": ["lease.pdf"]}, self.base / "evidence")

    def test_nothing_built_fails_and_the_built_app_passes_in_its_own_data_folder(self):
        result = self.run_walk()
        self.assertEqual((result["status"], result["reason"]), ("failed", walks.NOTHING_BUILT))
        (self.source / "app.py").write_text(APP)
        (self.source / "data").mkdir()
        (self.source / "data" / "note.txt").write_text("the owner's real note")
        result = self.run_walk()
        self.assertEqual(result["status"], "passed", result)
        self.assertTrue(Path(result["picture"]).read_bytes().startswith(b"\x89PNG"))
        self.assertEqual((self.source / "data" / "note.txt").read_text(), "the owner's real note")

    def test_one_changed_button_fails_naming_the_step_and_the_pages_words(self):
        (self.source / "app.py").write_text(APP.replace("Save note", "Keep note"))
        result = self.run_walk()
        self.assertEqual(result["status"], "failed")
        self.assertIn("step 6 (Click “Save note”.)", result["reason"])
        self.assertIn("Keep note", result["reason"])

    def test_an_ordinary_walk_with_no_wait_passes(self):
        (self.source / "app.py").write_text(APP)
        self.assertEqual(self.run_walk([{"open": "/"}, {"click": {"text": "Save note"}}, {"expect": "Notes"}])["status"], "passed")

    def test_an_app_that_dies_at_start_fails_with_its_own_error(self):
        (self.source / "app.py").write_text("raise SystemExit('no database')")
        result = self.run_walk()
        self.assertEqual(result["status"], "failed")
        self.assertIn("no database", result["reason"])


class BoardWalks(cf._Fixture):
    """Stored before any code, run by the board on the saved version."""

    committed_repo = cf.StoredChecksRunTests.committed_repo
    run_checks = cf.StoredChecksRunTests.run_checks

    def setUp(self):
        super().setUp()
        for target in ("harness.walks.owner_env.read", "harness.release_preview.owner_env.read"):
            patch = mock.patch(target, return_value={"PATH": "/usr/bin:/bin:/opt/homebrew/bin:/usr/local/bin"})
            patch.start()
            self.addCleanup(patch.stop)

    def agreed(self, steps, screens=True):
        agent = self.delivery()
        _session, reviewer = self.reviewer()
        board.record_requirement_proposal(self.root, agent["id"], cf.TEXT)
        if screens:
            item = {"name": "Main", **contract_screens.store(board.screens_folder(self.root, cf.TASK), SCREEN)}
            with board.locked_state(self.root) as state:
                state["requirement_proposals"][cf.TASK]["screens"] = {"manifest": [item], "id": "x"}
        value = cf.submission()
        value["criteria"][0]["checks"] = [walk(steps)]
        value["criteria"][0]["examples"] = ["Saving a note with two files shows the saved note with both file names."]
        self.submit(reviewer, value)
        board.record_requirements_decision(self.root, cf.TASK, "go_ahead", version=1, token=self.proposal()["contract_check"]["token"])
        board.record_requirement_confirmation(self.root, agent["id"], self.proposal()["text"])
        with board.locked_state(self.root) as state:
            state.setdefault("environment_checks", {})[cf.TASK] = {"needs": {"real_data": ["lease.pdf"], "ai_tools": []}, "runs": {}}
        return agent, reviewer

    def block(self):
        return board.snapshot(self.root)["requirement_confirmations"][cf.TASK]["contract_check"]

    def test_the_walk_is_the_exam_red_before_the_code_green_after_and_run_on_the_saved_version(self):
        agent, _reviewer = self.agreed(STEPS)
        label = self.block()["labels"][0]
        self.assertEqual((label["label"], label["counted"]), ("checked", True))
        self.assertIn("Click \u201cSave note\u201d.", label["walks"][0])
        self.assertIn("1 of", self.block()["real_tests"])
        repo, _commit = self.committed_repo("hello", extra={"lease.pdf": "%PDF", "readme.txt": "no app"})
        answer = board.run_walk(self.root, agent["id"])
        self.assertFalse(answer["passed"])
        self.assertEqual(answer["walks"][0]["reason"], walks.NOTHING_BUILT)
        (repo / "app.py").write_text(APP)
        self.assertTrue(board.run_walk(self.root, agent["id"], "1")["passed"])
        with self.assertRaisesRegex(ValueError, "no stored walk for requirement 9"):
            board.run_walk(self.root, agent["id"], "9")

    def test_the_review_request_is_refused_while_a_walk_fails_and_the_passing_run_is_kept_and_reused(self):
        self.agreed(STEPS)
        _repo, bad = self.committed_repo("hello", extra={"app.py": APP.replace("Save note", "Keep note"), "lease.pdf": "%PDF"})
        with self.assertRaisesRegex(ValueError, "walk 1.1.*Save note.*Keep note"):
            self.run_checks(bad)
        self.assertEqual(self.block()["results"], {"1": "failed"})
        self.assertEqual(self.block()["walks"]["1.1"]["status"], "failed")

    def test_the_passing_run_is_recorded_and_the_reviewer_reuses_it(self):
        self.agreed(STEPS)
        _repo, good = self.committed_repo("hello", extra={"app.py": APP, "lease.pdf": "%PDF"})
        run = self.run_checks(good)
        self.assertEqual(run["run"]["status"], "passed")
        self.assertEqual(self.block()["results"], {"1": "passed"})
        picture = self.block()["walks"]["1.1"]["picture"]
        self.assertTrue(Path(picture).is_file())
        request: dict = {}
        board._apply_contract_check_run(request, run)
        with mock.patch("harness.walks.run", side_effect=AssertionError("not run a second time")):
            request_again = {**request, "task": cf.TASK, "subtask": "", "stage": board.INDEPENDENT_REVIEW, "reviewed_commit": good,
                             "reviewed_tree_hash": "tree", "contract_revision": {"sha256": "rev"}}
            certification = {"board_root": self.root, "candidate": {}, "environment_sha256": "env", "lockfile_digests": {}, "role": "reviewer",
                             "gate": "g", "retry_reason": "", "task": cf.TASK}
            again = board._run_contract_checks(self.root, cf.TASK, request_again, certification, "reviewer")
        self.assertTrue(again["run"]["reused"])


if __name__ == "__main__":
    unittest.main()
