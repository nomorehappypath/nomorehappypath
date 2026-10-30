# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Golden eval set (task 2026-10-26_golden-eval-set): fixtures are valid, the scorer scores, results are never overwritten. macOS only."""
from __future__ import annotations

import importlib.util
import json
import shutil
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

GOLDEN = Path(__file__).resolve().parents[1] / "evals" / "golden"
EVENTS_FIXTURE = Path(__file__).resolve().parent / "golden_fixtures" / "weather_run_events.jsonl"
# A stand-in for project_manager.py: serves the three calls the runner makes and writes what environment it was started with.
FAKE_MANAGER = """
import json, os, sys
from http.server import BaseHTTPRequestHandler, HTTPServer
args = sys.argv
port = int(args[args.index("--port") + 1])
home = args[args.index("--home") + 1]
open(os.path.join(home, "env-seen.json"), "w").write(json.dumps({"CODEX_HOME": os.environ.get("CODEX_HOME"), "HOME": os.environ.get("HOME"),
    "token": os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"), "api_key": os.environ.get("ANTHROPIC_API_KEY")}))
class H(BaseHTTPRequestHandler):
    def _send(self):
        body = json.dumps({"id": "p1"}).encode()
        self.send_response(200); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
    def do_GET(self): self._send()
    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0)); self._send()
    def log_message(self, *a): pass
HTTPServer(("127.0.0.1", port), H).serve_forever()
"""


# A stand-in for the claude command (the real CLI is never run in tests): records how it was called, applies the reference fix on the build pass,
# writes a Claude session file in $HOME/.claude/projects/<working folder>/ and prints the --output-format json result.
FAKE_CLAUDE = """#!%(python)s
import json, os, re, sys, time, uuid
log = os.environ["FAKE_CLAUDE_LOG"]
argv = sys.argv[1:]
prompt = argv[argv.index("-p") + 1]
review = prompt.startswith("Review the changes")
cwd = os.getcwd()
names = sorted(os.path.relpath(os.path.join(d, f), cwd) for d, _s, fs in os.walk(cwd) if ".git" not in d.split(os.sep) for f in fs)
token = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
entry = {"argv": argv, "cwd": cwd, "files": names, "home": os.environ.get("HOME"), "token_ok": token == os.environ.get("FAKE_EXPECT_TOKEN"),
         "api_key": os.environ.get("ANTHROPIC_API_KEY"), "stdin_empty": sys.stdin.read() == "", "started": time.time()}
if os.environ.get("FAKE_SLEEP"):
    time.sleep(float(os.environ["FAKE_SLEEP"]))
if not review and os.environ.get("FAKE_FIX_DIR"):
    os.system("cp -R %%s/. %%s/" %% (os.environ["FAKE_FIX_DIR"], cwd))
session = str(uuid.uuid4())
folder = os.path.join(os.environ["HOME"], ".claude", "projects", re.sub(r"[^A-Za-z0-9]", "-", cwd))
os.makedirs(folder, exist_ok=True)
row = {"type": "assistant", "timestamp": "2026-10-27T12:00:00Z", "message": {"id": "msg_" + session[:8], "model": "claude-sonnet-5",
       "usage": {"input_tokens": 1000, "output_tokens": 500, "cache_read_input_tokens": 2000, "cache_creation_input_tokens": 100}}}
open(os.path.join(folder, session + ".jsonl"), "w").write(json.dumps(row) + "\\n")
entry["ended"] = time.time()
open(log, "a").write(json.dumps(entry) + "\\n")
if os.environ.get("FAKE_LEAK"):
    sys.stderr.write("debug " + (token or "") + "\\n")
print(json.dumps({"type": "result", "is_error": False, "num_turns": 9 if review else 14, "session_id": session,
                  "result": "done " + (token if os.environ.get("FAKE_LEAK") and token else "")}))
"""


def _runner():
    spec = importlib.util.spec_from_file_location("golden_run_eval", str(GOLDEN / "run_eval.py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules["golden_run_eval"] = module
    spec.loader.exec_module(module)
    return module


@unittest.skipUnless(sys.platform == "darwin" and (GOLDEN / "run_eval.py").is_file(), "golden eval set: macOS, and only where evals/golden ships")
class GoldenEvalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.run_eval = _runner()

    def setUp(self):
        self.scratch = Path(tempfile.mkdtemp(prefix="golden-test-"))
        self.addCleanup(shutil.rmtree, self.scratch, True)

    def test_dry_run_every_seed_runs_and_every_planted_bug_is_observed(self):
        ids = self.run_eval.set_ids("easy")
        self.assertEqual(len(ids), 10)
        report = self.run_eval.validate(ids)
        for row in report:
            self.assertEqual(row["problems"], [], row["project"])
            self.assertTrue(row["bugs"])
            for bug_id, result in row["bugs"].items():
                self.assertTrue(result["seed_fails"], "%s %s must fail on the seed" % (row["project"], bug_id))
                self.assertTrue(result["fix_passes"], "%s %s must pass with the reference fix" % (row["project"], bug_id))

    def test_scorer_scores_a_fixed_copy_and_an_unfixed_seed(self):
        run_eval = self.run_eval
        seed = run_eval.prepare_copy("03_json_api", self.scratch / "seed")
        fixed = run_eval.prepare_copy("03_json_api", self.scratch / "fixed", with_fix=True)
        self.assertEqual(run_eval.score_project("03_json_api", seed, None)["fixed"], 0)
        scored = run_eval.score_project("03_json_api", fixed, None)
        self.assertEqual((scored["fixed"], scored["planted"]), (4, 4))
        self.assertTrue(all(b["fixed"] for b in scored["bugs"]))
        self.assertEqual(scored["outcome"], "no board given")
        self.assertIsNone(scored["agent_steps"])

    def test_scorer_reads_the_boards_own_counters_and_finds_false_blockers(self):
        run_eval = self.run_eval
        fixed = run_eval.prepare_copy("03_json_api", self.scratch / "fixed", with_fix=True)
        state = {
            "task_turns": {"t1": 7, "t2": 2}, "task_review_seconds": {"t1": 185},
            "qa_requests": {
                "q1": {"id": "q1", "stage": "independent_review", "status": "failed", "completed_at": "2026-10-26T10:00:00+00:00", "cycle": 1,
                       "structured_findings": [{"id": "B1", "summary": "POST without a name is accepted, the missing name creates an empty item", "harm": "bad data"},
                                               {"id": "B2", "summary": "the README has an odd heading", "harm": "none"}]},
                "q2": {"id": "q2", "stage": "independent_review", "status": "passed", "completed_at": "2026-10-26T11:00:00+00:00", "cycle": 2},
                "dev": {"id": "d1", "stage": "development_qa", "status": "failed", "completed_at": "2026-10-26T09:00:00+00:00"},
            },
        }
        board = self.scratch / "state.json"
        board.write_text(json.dumps(state), encoding="utf-8")
        scored = run_eval.score_project("03_json_api", fixed, board)
        self.assertEqual(scored["review_rounds"], 2)
        self.assertEqual(scored["flagged_by_review"], ["A1"])
        self.assertEqual(scored["false_blockers"], 1)
        self.assertIn("README", scored["false_blocker_texts"][0])
        self.assertEqual((scored["agent_steps"], scored["review_minutes"]), (9, 3))
        self.assertIn("PASSED", scored["outcome"])

    def test_results_are_kept_never_overwritten_and_sheets_compare(self):
        run_eval = self.run_eval
        fixed = run_eval.prepare_copy("03_json_api", self.scratch / "fixed", with_fix=True)
        seed = run_eval.prepare_copy("03_json_api", self.scratch / "seed")
        root = self.scratch / "results"
        first = run_eval.write_sheet(root, "v1", [run_eval.score_project("03_json_api", fixed, None)])
        first_bytes = (first / "score.json").read_bytes()
        second = run_eval.write_sheet(root, "v1", [run_eval.score_project("03_json_api", seed, None)])  # same label, same second
        self.assertNotEqual(first, second)
        self.assertEqual((first / "score.json").read_bytes(), first_bytes)
        self.assertEqual(len(list(root.iterdir())), 2)
        self.assertTrue((first / "score.md").read_text(encoding="utf-8").startswith("# Golden eval score sheet"))
        table = run_eval.compare(first, second)
        self.assertIn("4/4 fixed", table)
        self.assertIn("0/4 fixed", table)

    def test_live_prepare_makes_a_private_git_copy_and_starts_nothing(self):
        run_eval = self.run_eval
        run = run_eval.live("03_json_api", self.scratch, "t", start_manager=False)
        self.addCleanup(shutil.rmtree, run, True)
        record = json.loads((run / "run.json").read_text(encoding="utf-8"))
        self.assertNotIn(".harness-home", record["home"])
        self.assertTrue(Path(record["home"]).is_dir() and str(run) in record["home"])
        self.assertNotIn("manager_pid", record)
        log = subprocess.run(["git", "-C", record["project_dir"], "log", "--oneline"], capture_output=True, text=True).stdout
        self.assertIn("seed", log)
        task = (run / "task.txt").read_text(encoding="utf-8")
        self.assertIn("shopping list", task)
        self.assertFalse((run / "project" / "observe.py").exists() or (run / "project" / "bugs.json").exists(), "the answers must not be in the project")
        self.assertTrue((run / "project" / "contract.md").is_file())

    # ---- eval-runner fixes (task 2026-10-27_eval-runner-fixes) ----------------------------------------------------------------
    def _real_shaped_board(self, failed_request=None):
        """A board folder shaped like a finished run: accepted tasks keep only the short index in state.json (the full request goes to
        archive/qa_requests.jsonl) and the full event log is events.jsonl."""
        board = self.scratch / "board"
        (board / "archive").mkdir(parents=True)
        shutil.copy2(EVENTS_FIXTURE, board / "events.jsonl")
        state = {"task_turns": {"weather-page": 12}, "task_review_seconds": {"weather-page": 400}, "qa_requests": {}, "archive": [],
                 "agents": {"a": {"id": "cto-0001-965b9b", "role": "cto"}},
                 "qa_request_index": {"review-weather-page-final-01": {
                     "id": "review-weather-page-final-01", "task": "weather-page", "cycle": 1, "stage": "independent_review",
                     "phase": "final_acceptance", "status": "passed", "result": "passed", "completed_at": "2026-10-27T13:24:06+00:00"}}}
        (board / "state.json").write_text(json.dumps(state), encoding="utf-8")
        if failed_request:
            (board / "archive" / "qa_requests.jsonl").write_text(json.dumps(failed_request) + "\n", encoding="utf-8")
        return board / "state.json"

    def test_scorer_counts_a_final_acceptance_review_and_the_cto_from_the_recorded_run(self):
        run_eval = self.run_eval
        fixed = run_eval.prepare_copy("01_weather_page", self.scratch / "fixed", with_fix=True)
        scored = run_eval.score_project("01_weather_page", fixed, self._real_shaped_board())
        self.assertEqual((scored["review_rounds"], scored["review_passed"], scored["review_failed"]), (1, 1, 0))
        self.assertEqual(scored["outcome"], "review PASSED after 1 review")
        self.assertEqual((scored["cto_polls"], scored["cto_commands"]), (6, 20))  # the CTO line of the first live run's own stats
        self.assertEqual(scored["false_blockers"], 0)
        # the events alone (no request record anywhere) still show the round and the verdict
        events_only = self.scratch / "events-only"
        events_only.mkdir()
        shutil.copy2(EVENTS_FIXTURE, events_only / "events.jsonl")
        (events_only / "state.json").write_text("{}", encoding="utf-8")
        again = run_eval.read_board(events_only / "state.json", run_eval.load_spec("01_weather_page")["bugs"])
        self.assertEqual((again["review_rounds"], again["review_passed"]), (1, 1))

    def test_scorer_counts_a_failed_final_review_and_its_false_blockers(self):
        run_eval = self.run_eval
        bugs = run_eval.load_spec("01_weather_page")["bugs"]
        failed = {"id": "review-weather-page-final-00", "stage": "independent_review", "phase": "final_acceptance", "status": "failed",
                  "cycle": 0, "completed_at": "2026-10-27T12:00:00+00:00",
                  "structured_findings": [{"summary": "%s is wrong in the output" % bugs[0]["keywords"][0], "harm": "x"},
                                          {"summary": "the README has an odd heading", "harm": "cosmetic only"}]}
        counted = run_eval.read_board(self._real_shaped_board(failed), bugs)
        self.assertEqual((counted["review_rounds"], counted["review_passed"], counted["review_failed"]), (2, 1, 1))
        self.assertEqual(counted["flagged_by_review"], [bugs[0]["id"]])
        self.assertEqual(counted["false_blockers"], 1)
        self.assertEqual(counted["outcome"], "review PASSED after 2 reviews")

    def test_live_refuses_without_the_isolated_sign_ins_and_never_uses_the_owners_codex_folder(self):
        run_eval = self.run_eval
        missing = self.scratch / "nothing-here"
        with self.assertRaises(SystemExit) as refused:
            run_eval.live("03_json_api", self.scratch, "t", codex_home=missing / "c", claude_home=missing / "h", token_file=missing / "t")
        self.assertIn("does not exist", str(refused.exception))
        self.assertEqual([p for p in self.scratch.iterdir() if p.name.startswith("nmhp-golden-")], [])
        fake_home = self.scratch / "owner"
        (fake_home / ".codex").mkdir(parents=True)
        (self.scratch / "tok").write_text("x" * 12, encoding="utf-8")
        (self.scratch / "claude-home").mkdir()
        original = run_eval.owner_home
        run_eval.owner_home = lambda: fake_home
        self.addCleanup(setattr, run_eval, "owner_home", original)
        problems = run_eval.isolation_problems(fake_home / ".codex", self.scratch / "claude-home", self.scratch / "tok")
        self.assertTrue(any("owner's own ~/.codex" in p for p in problems), problems)
        (self.scratch / "codex-home").mkdir()
        self.assertEqual(run_eval.isolation_problems(self.scratch / "codex-home", self.scratch / "claude-home", self.scratch / "tok"), [])
        violation = run_eval.codex_home_violations({11: str(fake_home / ".codex"), 12: str(self.scratch / "codex-home")})
        self.assertEqual(len(violation), 1)
        self.assertIn("pid 11", violation[0])

    def test_live_gives_the_manager_the_isolated_homes_keeps_the_token_out_of_the_record_and_stop_cleans_up(self):
        run_eval = self.run_eval
        nmhp = self.scratch / "nmhp"
        (nmhp / "harness").mkdir(parents=True)
        (nmhp / "harness" / "project_manager.py").write_text(FAKE_MANAGER, encoding="utf-8")
        codex, home = self.scratch / "signin-codex", self.scratch / "signin-home"
        codex.mkdir()
        home.mkdir()
        token = "tok-" + "Zq7" * 8
        (self.scratch / "token").write_text(token + "\n", encoding="utf-8")
        saved = {k: os.environ.get(k) for k in ("ANTHROPIC_API_KEY",)}
        os.environ["ANTHROPIC_API_KEY"] = "must-not-reach-the-app"
        self.addCleanup(lambda: [os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v) for k, v in saved.items()])
        run = run_eval.live("03_json_api", nmhp, "t", codex_home=codex, claude_home=home, token_file=self.scratch / "token")
        self.addCleanup(shutil.rmtree, run, True)
        seen = json.loads((run / "home" / "env-seen.json").read_text(encoding="utf-8"))
        self.assertEqual((seen["CODEX_HOME"], seen["HOME"], seen["token"], seen["api_key"]), (str(codex), str(home), token, None))
        record_text = (run / "run.json").read_text(encoding="utf-8")
        self.assertNotIn(token, record_text)
        self.assertNotIn(token, (run / "manager.log").read_text(encoding="utf-8"))
        isolation = json.loads(record_text)["isolation"]
        self.assertEqual((isolation["codex_home"], isolation["claude_home"]), (str(codex), str(home)))
        # --stop: kept with --keep, otherwise scored and deleted
        results = self.scratch / "results"
        message, code = run_eval.stop(run, keep=True, results_root=results, term_wait=3)
        self.assertEqual(code, 0, message)
        self.assertIn("Stopped pid", message)
        self.assertTrue(run.is_dir())
        self.assertEqual(run_eval.select_run_processes(run_eval.process_rows(), run_eval.run_markers(run), None, run_eval.ancestors_of_self()), [])
        message, code = run_eval.stop(run, results_root=results, term_wait=3)
        self.assertEqual(code, 0, message)
        self.assertFalse(run.exists())
        self.assertEqual(len(list(results.iterdir())), 1)
        for leaked in results.rglob("*"):
            if leaked.is_file():
                self.assertNotIn(token, leaked.read_text(encoding="utf-8"))

    def test_stop_selects_only_processes_that_reference_the_run_folder(self):
        run_eval = self.run_eval
        run = "/private/var/folders/xx/T/nmhp-golden-03_json_api-abc123"
        rows = [
            {"pid": 100, "ppid": 1, "started": "t1", "command": "python3 harness/project_manager.py --home %s/home --port 5" % run},
            {"pid": 101, "ppid": 100, "started": "t2", "command": "python3 harness/project_worker.py"},               # child of the manager
            {"pid": 102, "ppid": 1, "started": "t3", "command": "codex exec --cd /var/folders/xx/T/nmhp-golden-03_json_api-abc123/project"},  # other spelling
            {"pid": 103, "ppid": 1, "started": "t4", "command": "claude --session claude_cto-3d38875359"},          # session id from the board
            {"pid": 104, "ppid": 1, "started": "t5", "command": "node agent-cli"},                                    # cwd inside the run folder
            {"pid": 200, "ppid": 1, "started": "t6", "command": "python3 harness/project_manager.py --home /Users/someone/.harness-home"},  # the owner's app
            {"pid": 201, "ppid": 1, "started": "t7", "command": "codex exec --cd %s-other/project" % run},          # a different run folder with the same prefix
            {"pid": 202, "ppid": 1, "started": "t8", "command": "python3 evals/golden/run_eval.py --stop --run-dir %s" % run},  # this runner
            {"pid": 203, "ppid": 1, "started": "t9", "command": "node other-tool"},
        ]
        markers = run_eval.run_markers(Path(run), ["claude_cto-3d38875359"])
        chosen = run_eval.select_run_processes(rows, markers, {104: run + "/project", 203: "/Users/someone/work"}, protected={202})
        self.assertEqual([r["pid"] for r in chosen], [100, 101, 102, 103, 104])
        for marker in run_eval.run_markers(Path(run)):
            self.assertTrue(marker.startswith("/private/var/") or marker.startswith("/var/"))

    def test_ps_rows_are_parsed_with_their_start_time(self):
        rows = self.run_eval.process_rows()
        mine = [r for r in rows if r["pid"] == os.getpid()]
        self.assertEqual(len(mine), 1)
        self.assertEqual(mine[0]["started"], self.run_eval.start_time(os.getpid()))


    # ---- plain-Claude arm (task 2026-10-27_eval-plain-claude) ----------------------------------------------------------------
    PLAIN_TOKEN = "tok-" + "Qw9" * 8

    def _plain_setup(self, **extra):
        bin_dir, home, logs = self.scratch / "bin", self.scratch / "plain-home", self.scratch / "plain-logs"
        for d in (bin_dir, home, logs):
            d.mkdir()
        fake = bin_dir / "claude"
        fake.write_text(FAKE_CLAUDE % {"python": sys.executable}, encoding="utf-8")
        fake.chmod(0o755)
        (self.scratch / "plain-token").write_text(self.PLAIN_TOKEN + "\n", encoding="utf-8")
        env = {"PATH": "%s:%s" % (bin_dir, os.environ["PATH"]), "FAKE_CLAUDE_LOG": str(logs / "calls.jsonl"), "FAKE_EXPECT_TOKEN": self.PLAIN_TOKEN,
               "ANTHROPIC_API_KEY": "must-not-reach-claude", "FAKE_FIX_DIR": str(GOLDEN / "03_json_api" / "fix"), **extra}
        return home, env, logs / "calls.jsonl"

    def _plain_run(self, home, env, **kw):
        settings = self.run_eval.load_plain_settings()
        settings.update(kw.pop("settings", {}))
        return self.run_eval.plain("03_json_api", "plain-t", results_root=self.scratch / "results", claude_home=home,
                                   token_file=self.scratch / "plain-token", settings=settings, base_env=env, **kw)

    def test_plain_arm_runs_two_passes_in_an_answer_free_copy_scores_and_costs_it(self):
        home, env, calls = self._plain_setup(FAKE_LEAK="1")
        folder, code = self._plain_run(home, env)
        self.assertEqual(code, 0)
        rows = [json.loads(line) for line in calls.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(rows), 2)
        self.assertLessEqual(rows[0]["ended"], rows[1]["started"], "one process at a time")
        for row in rows:
            self.assertEqual(row["cwd"].split("/")[-1], "project")
            self.assertEqual(row["files"].count("contract.md"), 1)
            self.assertEqual(row["files"].count("brief.md"), 1)
            for answer in ("bugs.json", "observe.py"):
                self.assertFalse([f for f in row["files"] if f.split("/")[-1] == answer], "the answers must not be in the project")
            self.assertFalse([f for f in row["files"] if f.startswith("fix/")])
            self.assertEqual((row["home"], row["api_key"], row["token_ok"], row["stdin_empty"]), (str(home), None, True, True))
            self.assertEqual(row["argv"][row["argv"].index("--model") + 1], "claude-sonnet-5")
            self.assertEqual(row["argv"][row["argv"].index("--output-format") + 1], "json")
        self.assertEqual(rows[0]["argv"][1], self.run_eval.PLAIN_PROMPT_A)
        self.assertEqual(rows[1]["argv"][1], self.run_eval.PLAIN_PROMPT_B)
        self.assertTrue(rows[0]["argv"][1].startswith("You are working in this project folder. Read brief.md and contract.md."))
        sheet = json.loads((folder / "score.json").read_text(encoding="utf-8"))
        self.assertEqual(sheet["arm"], "plain-claude")
        project = sheet["projects"][0]
        self.assertEqual((project["fixed"], project["planted"], project["arm"]), (4, 4, "plain-claude"))
        self.assertEqual((project["plain"]["turns"], project["agent_steps"]), (23, 23))
        self.assertGreater(project["plain"]["wall_seconds"], 0)
        cost = project["cost"]
        self.assertTrue(cost["available"], cost)
        self.assertEqual(sorted(cost["roles"]), ["delivery", "reviewer"])   # build pass and review pass are told apart
        self.assertEqual(cost["roles"]["delivery"]["tokens"], cost["roles"]["reviewer"]["tokens"])
        self.assertGreater(cost["roles"]["delivery"]["tokens"], 0)
        self.assertGreater(cost["usd"], 0)
        self.assertIn("Arm: plain-claude", (folder / "score.md").read_text(encoding="utf-8"))
        self.assertEqual(json.loads((folder / "plain-run.json").read_text(encoding="utf-8"))["arm"], "plain-claude")
        for leaked in folder.rglob("*"):
            if leaked.is_file():
                self.assertNotIn(self.PLAIN_TOKEN, leaked.read_text(encoding="utf-8"), str(leaked))
        self.assertEqual([p for p in Path(tempfile.gettempdir()).glob("nmhp-golden-plain-03_json_api-*")], [], "the run folder is deleted")

    def test_plain_arm_refuses_without_the_isolated_sign_in_and_starts_nothing(self):
        home, env, calls = self._plain_setup()
        before = set(Path(tempfile.gettempdir()).glob("nmhp-golden-plain-03_json_api-*"))
        for kwargs, words in (({"claude_home": self.scratch / "nope"}, "does not exist"), ({"token_file": self.scratch / "no-token"}, "token file is missing")):
            args = {"claude_home": home, "token_file": self.scratch / "plain-token", **kwargs}
            with self.assertRaises(SystemExit) as refused:
                self.run_eval.plain("03_json_api", "t", results_root=self.scratch / "results", settings=self.run_eval.load_plain_settings(), base_env=env, **args)
            self.assertIn(words, str(refused.exception))
        original = self.run_eval.owner_home
        self.run_eval.owner_home = lambda: home
        self.addCleanup(setattr, self.run_eval, "owner_home", original)
        with self.assertRaises(SystemExit) as refused:
            self.run_eval.plain("03_json_api", "t", results_root=self.scratch / "results", claude_home=home, token_file=self.scratch / "plain-token",
                                settings=self.run_eval.load_plain_settings(), base_env=env)
        self.assertIn("real home", str(refused.exception))
        self.assertFalse(calls.exists(), "claude was never started")
        self.assertFalse((self.scratch / "results").exists())
        self.assertEqual(set(Path(tempfile.gettempdir()).glob("nmhp-golden-plain-03_json_api-*")), before)

    def test_plain_arm_stops_a_pass_that_runs_too_long_scores_what_is_there_and_leaves_nothing_running(self):
        home, env, calls = self._plain_setup(FAKE_SLEEP="60")
        folder, code = self._plain_run(home, env, settings={"step_timeout_seconds": 1, "stop_wait_seconds": 2})
        self.assertEqual(code, 0)
        project = json.loads((folder / "score.json").read_text(encoding="utf-8"))["projects"][0]
        self.assertEqual(project["fixed"], 0)
        self.assertIn("timed out", project["outcome"])
        self.assertEqual([p for p in Path(tempfile.gettempdir()).glob("nmhp-golden-plain-03_json_api-*")], [])

    def test_plain_settings_come_from_the_settings_file_and_bad_values_are_refused(self):
        settings = self.run_eval.load_plain_settings()
        self.assertEqual((settings["model"], settings["permission_mode"]), ("claude-sonnet-5", "acceptEdits"))
        self.assertEqual(self.run_eval.load_plain_settings(model="claude-sonnet-5-5", timeout=900)["step_timeout_seconds"], 900)
        for bad in ({"model": "x; rm -rf"}, {"timeout": 5}):
            with self.assertRaises(SystemExit):
                self.run_eval.load_plain_settings(**bad)

    def test_compare_lines_up_an_nmhp_run_against_a_plain_claude_run(self):
        home, env, _calls = self._plain_setup()
        plain_folder, _code = self._plain_run(home, env)
        fixed = self.run_eval.prepare_copy("03_json_api", self.scratch / "fixed", with_fix=True)
        nmhp = self.run_eval.write_sheet(self.scratch / "results", "nmhp-v1", [self.run_eval.score_project("03_json_api", fixed, None)])
        table = self.run_eval.compare(nmhp, plain_folder)
        self.assertIn("nmhp-v1 (nmhp)", table)
        self.assertIn("plain-t (plain-claude)", table)
        self.assertIn("23 turns", table)
        self.assertIn("4/4 fixed", table)

    def test_fixtures_carry_no_personal_traces_and_no_network(self):
        trace = "aba" + "lgir"
        for path in GOLDEN.rglob("*"):
            if path.is_file() and path.suffix in {".py", ".md", ".json", ".html"}:
                text = path.read_text(encoding="utf-8")
                self.assertNotIn(trace, text, str(path))
                self.assertNotIn("/Users/", text, str(path))
                if "seed" in path.parts or "fix" in path.parts:
                    for word in ("urllib", "requests", "socket", "http.client"):
                        self.assertNotIn("import " + word, text, str(path))


if __name__ == "__main__":
    unittest.main()
