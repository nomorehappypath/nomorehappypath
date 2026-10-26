# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""A governed test/scenario command is hermetic: it cannot call the live board, and rendered proof is cited, not run inside.

Found on the owner's live weather project (2026-10-09/10): a Delivery test shelled out to `board.py screen-check`, which in the
review sandbox failed on the board's protected lock file, so a feature built in minutes could not pass review for a day."""
from __future__ import annotations

import hashlib
import json
import os
import shlex
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from harness import board, browser_acceptance, child_process, contract, execution_preflight
from harness.project_context import project_context

ROOT = Path(__file__).resolve().parents[1]
BOARD = str(ROOT / "harness" / "board.py")


def refused(command: str, cwd: Path) -> bool:
    return execution_preflight.calls_the_board(shlex.split(command), cwd=cwd, search_path=os.environ.get("PATH", ""))


class StaticGuardTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.cwd = Path(self._tmp.name).resolve()

    def test_every_form_that_runs_the_harness_board_is_refused(self):
        for command in (
            f"python3 {BOARD} --root x screen-check --agent a --url http://127.0.0.1:1/",
            f"python3 -u {BOARD} poll --agent a",
            f"python3 -E -B {BOARD} poll --agent a",
            f"/usr/bin/env FOO=1 python3 {BOARD} poll --agent a",
            f"env -u BASH_ENV python3 {BOARD} poll --agent a",
            f"bash {BOARD}",
            f"{BOARD} poll --agent a",
            "python3 -m harness.board poll --agent a",
            f"node {BOARD}",
        ):
            with self.subTest(command=command):
                self.assertTrue(refused(command, self.cwd))

    def test_a_relative_path_is_resolved_against_the_execution_directory(self):
        link = self.cwd / "tools"
        link.mkdir()
        (link / "b.py").symlink_to(BOARD)
        self.assertTrue(refused("python3 tools/b.py poll --agent a", self.cwd))

    def test_mentions_and_other_programs_are_not_refused(self):
        (self.cwd / "board.py").write_text("print('a different project')\n")
        for command in (
            "pytest board.py",
            "python3 board.py",                      # this project's own board.py
            "python3 keyboard.py",
            "npm test -- screen-check",
            f"python3 -c \"print('{BOARD}')\"",
            "python3 -m unittest discover",
            "python3 -m pytest tests/test_board.py",
            "go test ./...",
        ):
            with self.subTest(command=command):
                self.assertFalse(refused(command, self.cwd))

    def test_preflight_refuses_with_the_plain_correction_before_anything_runs(self):
        with self.assertRaises(ValueError) as caught:
            execution_preflight.validate_commands(self.cwd, [f"python3 {BOARD} screen-check --agent a --url http://127.0.0.1:1/"])
        message = str(caught.exception)
        self.assertIn("hermetic", message)
        self.assertIn("screen-... evidence id", message)
        self.assertIn("fixture", message)


class RuntimeGuardTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name).resolve()
        self.live, self.fixture = base / "live", base / "fixture"
        self.live.mkdir(), self.fixture.mkdir()
        self.live_data = Path(project_context(self.live).data_root).resolve()

    def run_board(self, root: Path, *arguments: str, marker: bool = True):
        environment = {key: value for key, value in os.environ.items() if not key.startswith("HARNESS_")}
        if marker:
            environment = child_process.with_live_board(environment, self.live)
        return subprocess.run([sys.executable, "-E", BOARD, "--root", str(root), *arguments],
                              capture_output=True, text=True, env=environment, timeout=60)

    def test_the_marker_is_the_canonical_live_data_root(self):
        env = child_process.with_live_board({"PATH": "/usr/bin"}, self.live)
        self.assertEqual(env[child_process.LIVE_BOARD_ENV], str(self.live_data))
        alias = self.live.parent / "alias"
        alias.symlink_to(self.live, target_is_directory=True)
        self.assertEqual(child_process.with_live_board({}, alias)[child_process.LIVE_BOARD_ENV], str(self.live_data))

    def test_a_call_aimed_at_the_live_board_is_refused_at_once_in_plain_words(self):
        done = self.run_board(self.live, "screen-check", "--agent", "a", "--url", "http://127.0.0.1:1/")
        self.assertEqual(done.returncode, 2, done.stderr)
        self.assertIn("hermetic", done.stderr)
        self.assertNotIn("Operation not permitted", done.stderr)
        self.assertFalse((self.live_data / "board" / ".lock").exists(), "refused before any state or lock was touched")

    def test_the_same_call_through_a_symlink_to_the_live_board_is_refused_too(self):
        alias = self.live.parent / "alias2"
        alias.symlink_to(self.live, target_is_directory=True)
        self.assertEqual(self.run_board(alias, "poll", "--agent", "a").returncode, 2)

    def test_a_disposable_fixture_board_still_works(self):
        done = self.run_board(self.fixture, "poll", "--agent", "nobody")
        self.assertIn("unknown agent", done.stderr)      # the board itself answered, as it does for any fixture
        self.assertNotIn("hermetic", done.stderr)

    def test_without_the_marker_nothing_changes_and_help_always_answers(self):
        self.assertNotIn("hermetic", self.run_board(self.live, "poll", "--agent", "nobody", marker=False).stderr)
        self.assertIn("usage", self.run_board(self.live, "--help").stdout.lower())

    def test_every_governed_execution_path_sets_the_marker(self):
        for relative, needle in (
            ("harness/certified_execution.py", "child_process.with_live_board(environment, board_root)"),
            ("harness/board.py", "child_process.with_live_board(execution_env, live_board_root)"),
            ("harness/cto.py", "child_process.with_live_board(child_process.execution_environment(), root)"),
        ):
            with self.subTest(path=relative):
                self.assertIn(needle, (ROOT / relative).read_text(encoding="utf-8"))
        source = (ROOT / "harness" / "board.py").read_text(encoding="utf-8")
        self.assertEqual(source.count("live_board_root=root"), 7, "every caller of the governed runners passes the live board (the red-proof run on the unbuilt project is the seventh)")

    def test_the_development_qa_branch_really_gives_the_command_the_marker(self):
        work = Path(self._tmp.name).resolve() / "work"
        work.mkdir()
        (work / "test_probe.py").write_text(
            "import os, unittest\n\nclass Probe(unittest.TestCase):\n"
            "    def test_marker(self):\n"
            f"        self.assertEqual(os.environ.get({child_process.LIVE_BOARD_ENV!r}), {str(self.live_data)!r})\n",
            encoding="utf-8",
        )
        output = board._run_internal_qa("python3 -m unittest test_probe", work, live_board_root=self.live)
        self.assertIn("OK", output)


class ExceptionRowTests(unittest.TestCase):
    HEADER = ("| ID | What was tested | Simulation command | Expected system response | Observed system response | QA result |\n"
              "|---|---|---|---|---|---|\n")

    def ledger(self, row: str) -> Path:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "ledger.md"
        path.write_text(self.HEADER + row + "\n", encoding="utf-8")
        return path

    def test_an_na_or_deferred_row_is_refused_at_intake_with_the_owner_card_instruction(self):
        path = self.ledger("| S-1 | Interactive flow | `python3 -m unittest x` | It works | N/A: approved by the CTO because the browser is blocked | N/A: approved by the CTO because the browser is blocked |")
        for check in (contract.scenario_simulations, contract.scenario_submission_simulations):
            valid, problems, _ = check(path)
            self.assertFalse(valid)
            self.assertTrue(any("N/A or DEFERRED" in problem and "owner-action card" in problem for problem in problems), problems)
        self.assertFalse(contract.scenario_ledger_exists(path)[0])
        self.assertFalse(contract.scenario_submission_exists(path)[0])

    def test_an_ordinary_executable_row_is_unchanged(self):
        path = self.ledger("| S-1 | The notes file exists | `python3 -m unittest x` | The file holds notes | PASS: it did | PASS |")
        self.assertTrue(contract.scenario_simulations(path)[0])


class CitationTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()
        self.directory = board.board_dir(self.root) / "evidence" / "screen-checks"
        self.directory.mkdir(parents=True)

    def capture(self, check_id="screen-20261010T081500-a1b2c3d4", *, task="T", role="engineering", found=True, blank=False):
        files = {}
        for kind, suffix, payload in (("html", "html", b"<p>74 mph</p>"), ("text", "txt", b"74 mph"), ("screenshot", "png", b"\x89PNG")):
            path = self.directory / f"{check_id}.{suffix}"
            path.write_bytes(payload)
            files[kind] = {"path": str(path), "sha256": hashlib.sha256(payload).hexdigest()}
        (self.directory / f"{check_id}.json").write_text(json.dumps({
            "id": check_id, "task": task, "agent_id": "x", "role": role, "url": "http://127.0.0.1:1/",
            "expect": "mph", "expect_found": found, "blank": blank, "evidence": files}), encoding="utf-8")
        return check_id

    def ledger(self, text: str) -> Path:
        path = self.root / "ledger.md"
        path.write_text(text, encoding="utf-8")
        return path

    def test_a_real_capture_of_this_task_is_accepted_and_recorded_with_its_hashes(self):
        check_id = self.capture()
        cited = board._cited_screen_checks(self.root, "T", self.ledger(f"observed: {check_id} showed 74 mph"))
        self.assertEqual([item["id"] for item in cited], [check_id])
        self.assertEqual(set(cited[0]["hashes"]), {"html", "text", "screenshot"})

    def test_a_ledger_that_cites_nothing_is_unchanged(self):
        self.assertEqual(board._cited_screen_checks(self.root, "T", self.ledger("no citations here")), [])

    def test_invented_other_task_failed_and_modified_captures_are_refused(self):
        invented = "screen-20261010T081501-deadbeef"
        other = self.capture("screen-20261010T081502-0badf00d", task="OTHER")
        missed = self.capture("screen-20261010T081503-0badcafe", found=False)
        blank = self.capture("screen-20261010T081504-0badbabe", blank=True)
        changed = self.capture("screen-20261010T081505-0badface")
        (self.directory / f"{changed}.txt").write_bytes(b"edited afterwards")
        for check_id, words in ((invented, "no screen-check record"), (other, "another task"),
                                (missed, "did not find its expected text"), (blank, "blank"), (changed, "no longer matches")):
            with self.subTest(check_id=check_id), self.assertRaisesRegex(ValueError, words):
                board._cited_screen_checks(self.root, "T", self.ledger(f"see {check_id}"))

    def test_the_reviewers_ledger_must_cite_the_reviewers_own_capture(self):
        delivery = self.capture("screen-20261010T081506-aaaaaaaa", role="engineering")
        reviewer = self.capture("screen-20261010T081507-bbbbbbbb", role="qa")
        with self.assertRaisesRegex(ValueError, "Reviewer runs its own"):
            board._cited_screen_checks(self.root, "T", self.ledger(f"see {delivery}"), require_role="qa")
        self.assertEqual(len(board._cited_screen_checks(self.root, "T", self.ledger(f"see {delivery} and {reviewer}"), require_role="qa")), 2)

    def test_a_pass_re_checks_what_was_cited_when_the_ledger_was_accepted(self):
        check_id = self.capture()
        cited = board._cited_screen_checks(self.root, "T", self.ledger(f"see {check_id}"))
        board._verify_recorded_screen_checks(self.root, "T", cited)
        (self.directory / f"{check_id}.png").write_bytes(b"swapped")
        with self.assertRaisesRegex(ValueError, "changed after it was accepted"):
            board._verify_recorded_screen_checks(self.root, "T", cited)

    def test_screen_check_writes_the_durable_record(self):
        class Agent(dict):
            pass
        with mock.patch.object(board, "locked_state") as locked, \
                mock.patch.object(board.browser_acceptance, "validate_url"), \
                mock.patch.object(board.browser_acceptance, "render_page", return_value={"text": "74 mph", "html": "<p>74 mph</p>", "png": b"\x89PNG"}), \
                mock.patch.object(board, "_require_writable_agent", return_value={"id": "a", "role": "engineering", "active": True, "task": "T"}), \
                mock.patch.object(board, "_event"):
            locked.return_value.__enter__.return_value = {}
            result = board.screen_check(self.root, "a", "http://127.0.0.1:9/", "mph")
        record = json.loads((self.directory / f"{result['id']}.json").read_text())
        self.assertEqual((record["task"], record["role"], record["expect_found"]), ("T", "engineering", True))
        self.assertTrue(board._cited_screen_checks(self.root, "T", self.ledger(f"see {result['id']}")))


class BoundedExecutionTests(unittest.TestCase):
    def test_a_command_that_never_finishes_is_stopped_with_everything_it_started(self):
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "child.pid"
            started = time.monotonic()
            code, output, timed_out = child_process.run_bounded(
                ["/bin/sh", "-c", f"sleep 60 & echo $! > {marker}; wait"], cwd=tmp, env=os.environ, timeout=1.5)
            self.assertTrue(timed_out)
            self.assertLess(time.monotonic() - started, 20)
            child = int(marker.read_text().strip())
            time.sleep(0.5)
            with self.assertRaises(ProcessLookupError):
                os.kill(child, 0)

    def test_a_finishing_command_returns_its_output(self):
        code, output, timed_out = child_process.run_bounded(["/bin/sh", "-c", "echo hello; exit 3"], cwd="/", env=os.environ, timeout=10)
        self.assertEqual((code, output.strip(), timed_out), (3, "hello", False))

    def test_the_browser_launch_gives_up_waiting_for_a_busy_browser(self):
        held = threading.Lock()
        held.acquire()
        self.addCleanup(held.release)
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(browser_acceptance, "_PROCESS_LOCK", held), \
                mock.patch.object(browser_acceptance, "LAUNCH_LOCK_TIMEOUT_SECONDS", 0.3):
            try:
                with self.assertRaisesRegex(RuntimeError, "another browser check held the harness browser"):
                    browser_acceptance.launch("http://127.0.0.1:9/", Path(tmp))
            except RuntimeError as error:
                if "browser" in str(error).lower() and "another browser check" not in str(error):
                    self.skipTest("no browser on this machine: " + str(error)[:80])
                raise


class DirectiveTests(unittest.TestCase):
    def test_the_agent_directive_has_one_consistent_rule(self):
        text = (ROOT / "directives" / "AGENT.md").read_text(encoding="utf-8")
        self.assertIn("every command the board runs stays hermetic", text)
        self.assertIn("must never run\n  `board.py` or `screen-check`", text)
        self.assertNotIn("its `Simulation command` must\n  drive the running surface", text)
        self.assertIn("raise an owner-action card naming the unproven scope", text)
        self.assertIn("never one that runs `board.py` or `screen-check`", text)
        self.assertIn("a screen capture alone never\nproves a real provider workflow", text)


class OwnerCardAndMessageTests(unittest.TestCase):
    def test_the_screen_check_failure_messages_tell_the_agent_what_to_do(self):
        source = (ROOT / "harness" / "board.py").read_text(encoding="utf-8")
        for needle in ("nothing answered at that address", "the harness browser was busy",
                       "must stay out of the governed command"):
            self.assertIn(needle, source)
        self.assertNotIn("Paste this into Terminal to run the same check on your Mac", source)


def tearDownModule():
    pass


if __name__ == "__main__":
    unittest.main()
