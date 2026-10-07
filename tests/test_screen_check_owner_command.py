# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""When a screen check cannot run in the agent's sandbox, the owner gets the command.

Owner, 2026-09-28: "if running the app crashes, let the mission control give
the full command like what you do and the user will run it in the shell."
A certified check whose acceptance browser could not start, or crashed, inside
the agent's sandbox still FAILS, and Mission Control shows the owner a card
with the exact Terminal command that runs the same check outside the sandbox.

Run:  PYTHONPATH=. python3 -m unittest tests.test_screen_check_owner_command -v
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

from harness import board, browser_acceptance, platform_support
from tests import environment_support
from tests import test_owner_action_cards_rendered as _cards

REPOSITORY = Path(__file__).resolve().parents[1]
COMMAND = "PYTHONPATH=. python3 -m unittest tests.test_studio_surface"
COMMIT = "c3d4a336fcecc455c5be102a8e67925cacd78a98"


class BlockedMarkerTests(unittest.TestCase):
    def test_the_marker_is_printed_only_inside_a_sandbox(self):
        host = type(platform_support.browser_host())
        for inside, expected in ((True, True), (False, False)):
            stream = io.StringIO()
            with mock.patch.object(host, "inside_os_sandbox", return_value=inside), redirect_stderr(stream):
                browser_acceptance._report_blocked("the browser crashed (signal 5)")
            self.assertEqual(browser_acceptance.SANDBOX_BLOCKED in stream.getvalue(), expected)

    def test_the_note_is_found_anywhere_in_the_output(self):
        output = "start\n" + f"{browser_acceptance.SANDBOX_BLOCKED}: the browser crashed\n" + "x" * 5000
        self.assertTrue(browser_acceptance.blocked_note(output).startswith(browser_acceptance.SANDBOX_BLOCKED))
        self.assertEqual(browser_acceptance.blocked_note("FAILED (failures=1)"), "")


@unittest.skipUnless(sys.platform == "darwin", "the agent sandbox under test is macOS Seatbelt")
class RealSandboxCrashTests(unittest.TestCase):
    """A browser that crashes inside the real agent profile leaves the marker in the check's output."""

    def test_a_crashing_browser_in_the_agent_sandbox_reports_itself(self):
        self._run_fake_browser('#!/bin/sh\nif [ "$1" = "--version" ]; then echo CrashingBrowser; exit 0; fi\nkill -SEGV $$\n',
                               "the browser crashed")

    def _run_fake_browser(self, script: str, expected: str) -> None:
        environment_support.require_sandbox_exec()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            browser = root / "headless-shell"
            browser.write_text(script, encoding="utf-8")
            browser.chmod(0o755)
            child = (
                "import sys, tempfile, time\nfrom pathlib import Path\nsys.path.insert(0, sys.argv[1])\n"
                "from harness import browser_acceptance\n"
                "session = browser_acceptance.launch('http://127.0.0.1:49199/', Path(tempfile.mkdtemp()))\n"
                "time.sleep(1)\nsession.close(validate=False)\n"
            )
            confinement = platform_support.agent_confinement()
            argv = confinement.wrap(
                [sys.executable, "-c", child, str(REPOSITORY)], [str(root), *confinement.temp_paths()],
                store=root / "profiles",
            )
            completed = subprocess.run(
                argv, capture_output=True, text=True, timeout=60, cwd=root,
                env={**os.environ, "HARNESS_BROWSER_BIN": str(browser), "HOME": str(root), "TMPDIR": str(root)},
            )
        self.assertIn(f"{browser_acceptance.SANDBOX_BLOCKED}: {expected}", completed.stderr, completed.stderr[-800:])


    def test_a_browser_that_exits_with_an_error_code_reports_itself_too(self):
        # Review r2 B2: a startup failure exits 1, not by a signal.
        self._run_fake_browser('#!/bin/sh\nif [ "$1" = "--version" ]; then echo FailingBrowser; exit 0; fi\nsleep 0.3\nexit 1\n',
                               "the browser exited with code 1")


class CrashedBrowserNeverPassesTests(unittest.TestCase):
    """Review r2 B3: a reported browser failure fails the check even when its tests say OK."""

    TEST = (
        "import sys, unittest\n"
        "class T(unittest.TestCase):\n"
        "    def test_screen(self):\n"
        f"        print('{browser_acceptance.SANDBOX_BLOCKED}: the browser crashed (signal 11) before the check finished', file=sys.stderr)\n"
    )

    def setUp(self):
        environment_support.require_process_table()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()
        (self.root / "test_crash_pass.py").write_text(self.TEST, encoding="utf-8")
        from harness import execution_identity
        self.candidate = execution_identity.candidate_evidence_identity("c" * 40, "t" * 40, "contract", {"ledger": "l" * 64})

    def certification(self) -> dict:
        return {"board_root": self.root, "candidate": self.candidate, "environment_sha256": "e" * 64,
                "lockfile_digests": {}, "role": "reviewer", "gate": "final", "retry_reason": "",
                "task": "screen-task", "rerun_source": {"workspace": str(self.root), "commit": ""}}

    def test_the_certified_run_fails_is_stored_as_failed_and_pins_the_card(self):
        from harness import certified_execution, execution_identity
        command = "python3 -m unittest test_crash_pass"
        with self.assertRaisesRegex(ValueError, "screen check could not run") as caught:
            board._execute_internal_qa(command, self.root, certification=self.certification())
        self.assertIn(browser_acceptance.SANDBOX_BLOCKED, str(caught.exception))
        cards = list((board.snapshot(self.root).get("owner_actions") or {}).values())
        self.assertEqual([card["command"] for card in cards], [f"cd {self.root} && {command}"])
        # Never stored as a success, so a later identical run cannot reuse it as a pass.
        stored = [json.loads(line) for line in (board.board_dir(self.root) / "execution-store.jsonl").read_text().splitlines()]
        self.assertTrue(stored and all(int(row.get("exit_code", 0)) != 0 for row in stored), stored)

    def test_the_uncertified_run_fails_too(self):
        with self.assertRaisesRegex(ValueError, "could not run its screen check"):
            board._execute_internal_qa("python3 -m unittest test_crash_pass", self.root)


class BrowserFailsBeforeAnyTestTests(CrashedBrowserNeverPassesTests):
    """Review r3 B4: the browser dies in setUpClass, so the run reports zero tests.

    The zero-tests refusal must keep the blocked-browser reason and the owner
    must still get the card. (The marker's real source, a browser dying inside
    the agent sandbox, is proven by RealSandboxCrashTests.)
    """

    TEST = (
        "import sys, unittest\n"
        "class T(unittest.TestCase):\n"
        "    @classmethod\n"
        "    def setUpClass(cls):\n"
        f"        print('{browser_acceptance.SANDBOX_BLOCKED}: the browser crashed (signal 11) before the check finished', file=sys.stderr)\n"
        "        raise RuntimeError('acceptance browser did not start')\n"
        "    def test_screen(self):\n"
        "        pass\n"
    )

    def test_the_certified_run_fails_is_stored_as_failed_and_pins_the_card(self):
        command = "python3 -m unittest test_crash_pass"
        with self.assertRaises(ValueError) as caught:
            board._execute_internal_qa(command, self.root, certification=self.certification())
        self.assertIn(browser_acceptance.SANDBOX_BLOCKED, str(caught.exception))
        self.assertIn("Mission Control now shows the owner the command", str(caught.exception))
        cards = list((board.snapshot(self.root).get("owner_actions") or {}).values())
        self.assertEqual([(card["command"], card["task"]) for card in cards], [(f"cd {self.root} && {command}", "screen-task")])
        stored = [json.loads(line) for line in (board.board_dir(self.root) / "execution-store.jsonl").read_text().splitlines()]
        self.assertTrue(stored and all(int(row.get("exit_code", 0)) != 0 for row in stored), stored)

    def test_the_uncertified_run_fails_too(self):
        with self.assertRaises(ValueError) as caught:
            board._execute_internal_qa("python3 -m unittest test_crash_pass", self.root)
        self.assertIn(browser_acceptance.SANDBOX_BLOCKED, str(caught.exception))


class RefusalsKeepTheReasonTests(unittest.TestCase):
    def test_every_refusal_keeps_the_blocked_browser_reason(self):
        output = f"{browser_acceptance.SANDBOX_BLOCKED}: the browser exited with code 1\nRan 0 tests in 0.001s"
        for message in ("internal-QA test command reported zero executed tests",
                        "internal-QA test command timed out after 300 seconds",
                        "internal-QA output must report a positive executed-test count"):
            with self.subTest(message=message):
                self.assertIn(browser_acceptance.SANDBOX_BLOCKED, browser_acceptance.with_blocked_note(message, output))
        self.assertEqual(browser_acceptance.with_blocked_note("plain", "Ran 1 test\nOK"), "plain")


class TimeoutOutputTests(unittest.TestCase):
    def test_a_timeout_with_one_empty_and_one_bytes_stream_is_a_timeout_refusal(self):
        # Review r4 B5: stdout None, stderr bytes crashed with TypeError.
        error = subprocess.TimeoutExpired("python3 -m unittest", 300, output=None,
                                          stderr=f"{browser_acceptance.SANDBOX_BLOCKED}: x\n".encode())
        with mock.patch.object(board.subprocess, "run", side_effect=error), \
                mock.patch.object(board.execution_preflight, "validate_commands", return_value=None):
            with self.assertRaisesRegex(ValueError, "timed out after 300 seconds") as caught:
                board._run_internal_qa("python3 -m unittest", Path(tempfile.gettempdir()))
        self.assertIn(browser_acceptance.SANDBOX_BLOCKED, str(caught.exception))


class StoredSuccessWithABlockedBrowserTests(unittest.TestCase):
    """Round-3 sweep: output that reports a blocked browser is never reused as a pass."""

    def test_a_cached_success_carrying_the_marker_is_executed_again_not_reused(self):
        from harness import certified_execution, execution_identity
        environment_support.require_process_table()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            candidate = execution_identity.candidate_evidence_identity("c" * 40, "t" * 40, "contract", {"ledger": "l" * 64})
            hit = {"status": "hit", "entry": {"record_id": "old"}}
            with mock.patch.object(execution_identity, "lookup", return_value=hit), \
                    mock.patch.object(execution_identity, "load_output",
                                      return_value=f"{browser_acceptance.SANDBOX_BLOCKED}: x\nRan 1 test\nOK"):
                result = certified_execution.run(
                    root, root, "python3 -c \"print('Ran 1 test in 0.001s'); print('OK')\"",
                    candidate=candidate, environment_sha256="e" * 64, environment=os.environ,
                    lockfile_digests={}, role="reviewer", gate="final",
                )
        self.assertEqual(result["measurement"]["cache_decision"], "executed_and_certified")

    def test_release_health_is_never_taken_from_output_reporting_a_blocked_browser(self):
        import hashlib
        from harness import cto, execution_identity
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            ledger = root / "ledger.md"
            ledger.write_text("certified ledger\n")
            ledger_sha = hashlib.sha256(ledger.read_bytes()).hexdigest()
            request = {
                "phase": "final_acceptance", "test_scope": "full", "delivery_state": "passed", "status": "passed",
                "unit_test_command": "python3 -m unittest", "ledger_sha256": ledger_sha,
                "certified_artifacts": {"delivery_ledger": {"path": str(ledger), "sha256": ledger_sha}},
                "contract_revision": {"sha256": "k" * 64}, "environment_identity": {"sha256": "e" * 64},
                "reviewed_commit": "c" * 40, "reviewed_tree_hash": "t" * 40, "subtask": "", "chunk": "",
                "command_executions": [{"kind": "unit_test", "exit_code": 0, "execution_identity": "i", "execution_record_id": "r"}],
            }
            candidate = execution_identity.candidate_evidence_identity(
                "c" * 40, "t" * 40, "k" * 64,
                {"delivery_ledger": ledger_sha, "review_scope": board._review_scope_identity(request)["sha256"]},
            )
            entry = {"identity_fields": {
                "candidate_sha256": candidate["sha256"], "argv": ["python3", "-m", "unittest"], "cwd": ".",
                "environment_sha256": "e" * 64, "role": "delivery", "gate": "final_acceptance::",
                "policy_version": execution_identity.POLICY_VERSION,
            }}
            for output, verified in (("Ran 12 tests in 1.0s\nOK", True),
                                     (f"{browser_acceptance.SANDBOX_BLOCKED}: the browser crashed\nRan 12 tests in 1.0s\nOK", False)):
                with self.subTest(verified=verified), \
                        mock.patch.object(execution_identity, "certified_success", return_value=entry), \
                        mock.patch.object(execution_identity, "load_output", return_value=output), \
                        mock.patch.object(cto, "_clean_process_audit", return_value=True):
                    self.assertEqual(cto._certified_delivery_health(root, request)["verified"], verified)


class OwnerCardTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()
        self.workspace = self.root / "task-workspace"
        self.workspace.mkdir()

    def certification(self, commit: str = COMMIT) -> dict:
        return {"board_root": self.root, "task": "event-networking",
                "rerun_source": {"workspace": str(self.workspace), "commit": commit}}

    def run_failing(self, message: str, certification: dict | None):
        with mock.patch.object(board, "_run_internal_qa", side_effect=ValueError(message)):
            with self.assertRaises(ValueError) as caught:
                board._execute_internal_qa(COMMAND, self.root, certification=certification)
        return str(caught.exception), board.snapshot(self.root).get("owner_actions") or {}

    def test_a_blocked_screen_check_fails_and_pins_the_exact_command(self):
        error, cards = self.run_failing(
            f"internal-QA test command failed with exit code 1: ... | {browser_acceptance.SANDBOX_BLOCKED}: the browser crashed (signal 5)",
            self.certification(),
        )
        self.assertIn("Mission Control now shows the owner the command", error, "the check must still fail")
        self.assertEqual(len(cards), 1)
        card = next(iter(cards.values()))
        self.assertEqual(card["title"], "Run a screen check yourself")
        self.assertEqual(card["task"], "event-networking")
        self.assertEqual(
            card["command"],
            f'd="$(mktemp -d)" && git -C {self.workspace} archive {COMMIT} | tar -x -C "$d" && cd "$d" && {COMMAND}',
        )
        self.assertNotIn("sandbox-exec", card["command"])

    def test_the_command_really_rebuilds_the_reviewed_checkout_and_runs_there(self):
        subprocess.run(["git", "init", "-q", str(self.workspace)], check=True)
        (self.workspace / "marker.txt").write_text("reviewed bytes\n")
        subprocess.run(["git", "-C", str(self.workspace), "add", "marker.txt"], check=True)
        subprocess.run(["git", "-C", str(self.workspace), "-c", "user.name=t", "-c", "user.email=t@t",
                        "commit", "-qm", "reviewed"], check=True)
        commit = subprocess.run(["git", "-C", str(self.workspace), "rev-parse", "HEAD"],
                                capture_output=True, text=True, check=True).stdout.strip()
        (self.workspace / "marker.txt").write_text("moved on\n")
        with mock.patch.object(board, "_run_internal_qa", side_effect=ValueError(f"x | {browser_acceptance.SANDBOX_BLOCKED}: y")), \
                self.assertRaises(ValueError):
            board._execute_internal_qa("cat marker.txt", self.root, certification=self.certification(commit))
        card = next(iter(board.snapshot(self.root)["owner_actions"].values()))
        pasted = subprocess.run(["/bin/bash", "-c", card["command"]], capture_output=True, text=True, timeout=30)
        self.assertEqual((pasted.returncode, pasted.stdout), (0, "reviewed bytes\n"), pasted.stderr)

    def test_an_uncommitted_workspace_review_runs_in_that_workspace(self):
        _error, cards = self.run_failing(f"x | {browser_acceptance.SANDBOX_BLOCKED}: y", self.certification(commit=""))
        self.assertEqual(next(iter(cards.values()))["command"], f"cd {self.workspace} && {COMMAND}")

    def test_other_failures_and_uncertified_runs_pin_nothing(self):
        _error, cards = self.run_failing("internal-QA test command failed with exit code 1: AssertionError", self.certification())
        self.assertEqual(cards, {})
        _error, cards = self.run_failing(f"x | {browser_acceptance.SANDBOX_BLOCKED}: y", None)
        self.assertEqual(cards, {})


class RenderedOwnerCommandTests(unittest.TestCase):
    """Mission Control, in headless Chrome, shows the card with its command and Copy."""

    setUp = _cards.RenderedOwnerActionCardTests.setUp
    render = _cards.RenderedOwnerActionCardTests.render

    def test_mission_control_shows_the_command_to_paste(self):
        certification = {"board_root": self.root, "task": "event-networking",
                         "rerun_source": {"workspace": "/Users/owner/Projects/studio", "commit": COMMIT}}
        with mock.patch.object(board, "_run_internal_qa", side_effect=ValueError(f"x | {browser_acceptance.SANDBOX_BLOCKED}: y")), \
                self.assertRaises(ValueError):
            board._execute_internal_qa(COMMAND, self.root, certification=certification)
        reading = self.render()
        self.assertTrue(reading["sectionVisible"], reading)
        self.assertEqual([card["title"] for card in reading["cards"]], ["Run a screen check yourself"])
        self.assertTrue(all(card["visible"] for card in reading["cards"]))
        self.assertIn(f"git -C /Users/owner/Projects/studio archive {COMMIT}", reading["pageText"])
        self.assertIn(COMMAND, reading["pageText"])
        self.assertIn("Copy", reading["pageText"])
        self.assertIn("Paste it into Terminal and press Return.", reading["pageText"])
        self.assertIn("browser cannot run inside the agent's sandbox", reading["pageText"])


if __name__ == "__main__":
    unittest.main()
