# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""2026-10-15 (Studio): a Delivery whose own browser tool was denied localhost read the no-alternate-route rule as
also forbidding the project's own documented HTTP API and reported "will not mark complete" three times in one night.
The directives must say the project's own API is the sanctioned way, a real run through it is real-run evidence, and
the board must answer such a status itself; an ordinary status that merely mentions a browser is untouched."""
from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from harness import board, contract
from tests import test_board

ROOT = Path(__file__).resolve().parents[1]


def flat(path):
    return " ".join((ROOT / path).read_text(encoding="utf-8").split())


FULL_FILES = ("directives/AGENT.md", "harness/directives/00_SPAWN_DEVELOPMENT_DIRECTIVE.md")
SHORT_FILES = ("harness/directives/CTO_COMPLETION_DIRECTIVE.md", "harness/directives/AUTONOMOUS_COMPLETION_DIRECTIVE.md")


class DirectivesSayTheApiIsSanctioned(unittest.TestCase):
    def test_full_rule_in_the_agent_and_spawn_directives(self):
        for path in FULL_FILES:
            text = flat(path)
            with self.subTest(path=path):
                self.assertIn("A browser-tool denial is final for that tool: do not look for a permission setting and do not retry it", text)
                self.assertIn("The project's own documented API or CLI, or the exact call its own UI makes", text)
                self.assertIn("is the sanctioned way to run it and is never an alternate route", text)
                self.assertIn("is real-run evidence of the backend only when it reached terminal success and you inspected the promised outputs", text)
                self.assertIn("it does not prove UI behavior, which keeps its rendered proof", text)
                self.assertIn("authorization headers, cookies and tokens redacted", text)
                self.assertIn("Test: does its own normal UI make this call?", text)
                self.assertIn("A real credential, permission or security gate, and any host or tool restriction, is never bypassed, whatever the documentation says", text)
                self.assertIn("If genuinely unsure, ask once in one message; never refuse the whole task", text)

    def test_short_rule_in_the_cto_and_reviewer_directives(self):
        for path in SHORT_FILES:
            text = flat(path)
            with self.subTest(path=path):
                self.assertIn("A browser-tool denial is final for that tool (no permission hunt, no retry)", text)
                self.assertIn("is the sanctioned way to run it, never an alternate route", text)
                self.assertIn("real-run evidence of the backend only at terminal success with the outputs inspected and secrets redacted, never proof of UI behavior", text)
                self.assertIn("a real credential, permission, security or host gate is never bypassed; if unsure, ask once", text)

    def test_cto_directive_carries_it(self):
        text = flat("directives/CTO.md")
        self.assertIn("A browser-tool denial is final for that tool; the project's own API, CLI or the call its own UI makes is sanctioned, never an alternate route", text)
        self.assertIn("its successful, inspected run is backend evidence only; a real gate is never bypassed", text)


class ApiRunIsRealRunEvidence(unittest.TestCase):
    delivery = test_board.BoardTests.delivery

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / ".gitignore").write_text(".harness/\nproof/\n")
        for command in (["git", "init", "-q", "-b", "main"], ["git", "config", "user.email", "h@example.invalid"],
                        ["git", "config", "user.name", "Harness"], ["git", "add", ".gitignore"], ["git", "commit", "-qm", "baseline"]):
            subprocess.run(command, cwd=self.root, check=True)

    def test_a_recorded_api_run_completes_a_real_run_deliverable(self):
        task = "TASK-API-RUN"
        dev = self.delivery(task)
        record = self.root / "proof" / "api_run.json"
        record.parent.mkdir()
        record.write_text(json.dumps({
            "request": {"method": "POST", "path": "/api/run", "body": {"task": "brand strategy"}},
            "response": {"status": 200, "thread": "t-1", "result": "done", "artifacts": ["strategy.md"]}}))
        board.attach_contract_evidence(self.root, dev["id"], "delivery", str(record))
        complete = contract.contract_complete(self.root, task)[0]
        self.assertTrue(complete)


class TheBoardAnswersABrowserDenialStatus(unittest.TestCase):
    delivery = test_board.BoardTests.delivery

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.dev = self.delivery("TASK-BROWSER")

    def test_answers_a_status_that_will_not_complete_because_of_the_browser_denial_and_keeps_the_report(self):
        note = "CUA browser tool denied localhost; contract blocked by the unresolved real-run requirement, will not mark complete"
        event = board.status(self.root, self.dev["id"], note)
        self.assertIn("is the sanctioned way to run it and is never an alternate route", event["hint"])
        self.assertIn("route the rest to the CTO", event["hint"])
        self.assertEqual(board.snapshot(self.root)["agents"][self.dev["id"]]["status_note"], note)
        log = (self.root / ".harness" / "board" / "events.jsonl").read_text()
        self.assertIn("own_browser_denial_answered", log)

    def test_a_real_credential_gate_in_the_same_status_gets_no_clarification(self):
        for note in ("Browser denied localhost; the API also returns 401 without a token; cannot mark complete",
                     "Browser tool blocked; sign in missing for the project API"):
            event = board.status(self.root, self.dev["id"], note)
            self.assertNotIn("hint", event)

    def test_stores_a_plain_denial_report_with_the_clarification_as_a_hint(self):
        event = board.status(self.root, self.dev["id"], "Browser tool denied localhost; running the real run through the project's API instead")
        self.assertIn("never an alternate route", event["hint"])
        self.assertEqual(board.snapshot(self.root)["agents"][self.dev["id"]]["status_note"],
                         "Browser tool denied localhost; running the real run through the project's API instead")

    def test_an_ordinary_status_that_mentions_a_browser_is_stored_untouched(self):
        for note in ("Opened the page in the browser, layout checked, all tests pass", "Implementing the /api/run handler; real-run requirement met by the saved run"):
            event = board.status(self.root, self.dev["id"], note)
            self.assertNotIn("hint", event)
            self.assertEqual(board.snapshot(self.root)["agents"][self.dev["id"]]["status_note"], note)


if __name__ == "__main__":
    unittest.main()
