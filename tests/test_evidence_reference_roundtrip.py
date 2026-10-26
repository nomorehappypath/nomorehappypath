# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""The board's own evidence never has to travel back through the agent, and local upload problems say what they are (windows-walk item 10).

2026-10-21: a Reviewer on the owner's Windows project could not record its verdict. `execute-challenge` answered with the certified
evidence as `trusted://<label>/...`; the Reviewer passed that to `qa-result --evidence`, the client could not read it as a path, and
printed "authenticated board worker response is invalid or incompatible" on every retry.

Machine inputs removed: nothing is read from the machine (temporary project, real board and gateway, no network beyond loopback).

Run:  PYTHONPATH=. python3 -m unittest tests.test_evidence_reference_roundtrip -v
"""
from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

from harness import board, board_client, board_mcp
from tests import test_execute_once as base
from tests import test_board_surface_integration as surface


class VerdictWithoutEvidenceTests(base.ExecuteOnceTests):
    def test_the_verdict_can_use_the_certified_evidence_the_board_named(self):
        qa, request, _, _ = self._claimed_review()
        outcome = board.execute_challenge(self.root, qa["id"], request["id"])
        self.assertTrue(outcome["output_excerpt"].strip(), "the Reviewer reads what it judges without a path")
        self.assertLessEqual(len(outcome["output_excerpt"]), board.EVIDENCE_EXCERPT_CHARACTERS + 100)
        again = board.execute_challenge(self.root, qa["id"], request["id"])
        self.assertEqual(again["status"], "already_certified")
        self.assertEqual(again["output_excerpt"], outcome["output_excerpt"])
        with mock.patch.object(board, "_execute_scenario_simulations", side_effect=AssertionError("no rerun")):
            recorded = board.qa_result(self.root, qa["id"], request["id"], "passed", "TASK DONE: YES - verdict on the board's own evidence", "")
        self.assertEqual(recorded["status"], "passed")

    def test_the_excerpt_is_bounded_marked_and_never_echoes_a_login_token(self):
        long_text = "x" * (board.EVIDENCE_EXCERPT_CHARACTERS + 500)
        token = "sk-ant-oat01-" + "a" * 40
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "e.txt"
            path.write_text(long_text)
            excerpt = board._evidence_excerpt(path)
            self.assertIn(f"[excerpt: {board.EVIDENCE_EXCERPT_CHARACTERS} of {len(long_text)} characters", excerpt)
            self.assertLess(len(excerpt), board.EVIDENCE_EXCERPT_CHARACTERS + 300)
            path.write_text("command: make test\nCLAUDE_CODE_OAUTH_TOKEN=" + token + "\nresult: PASS\n")
            self.assertNotIn(token, board._evidence_excerpt(path))
            self.assertIn("[login token removed]", board._evidence_excerpt(path))
        self.assertEqual(board._evidence_excerpt(None), "")

    def test_a_failing_verdict_can_use_it_too(self):
        qa, request, _, _ = self._claimed_review()
        board.execute_challenge(self.root, qa["id"], request["id"])
        recorded = board.qa_result(self.root, qa["id"], request["id"], "failed", "a real finding", "")
        self.assertEqual(recorded["status"], "failed")

    def test_without_a_certified_execution_the_refusal_says_what_to_do(self):
        qa, request, _, _ = self._claimed_review()
        with self.assertRaisesRegex(ValueError, "run execute-challenge .a failed run stores nothing"):
            board.qa_result(self.root, qa["id"], request["id"], "failed", "no run yet", "")

    def test_only_the_claiming_reviewer_may_use_the_certified_evidence(self):
        qa, request, _, _ = self._claimed_review()
        board.execute_challenge(self.root, qa["id"], request["id"])
        other = board.register(self.root, "qa", "REVIEW_QUEUE", vendor="Anthropic")
        with self.assertRaisesRegex(ValueError, "only the QA agent that claimed this request"):
            board.qa_result(self.root, other["id"], request["id"], "passed", "not mine", "")

    def test_a_certified_execution_whose_evidence_file_is_gone_or_changed_is_executed_again(self):
        qa, request, _, _ = self._claimed_review()
        first = board.execute_challenge(self.root, qa["id"], request["id"])
        evidence = Path(first["evidence"])
        evidence.write_text(evidence.read_text() + "tampered\n")
        second = board.execute_challenge(self.root, qa["id"], request["id"])
        self.assertNotEqual(second.get("status"), "already_certified", "a reference to broken evidence is never returned again")
        self.assertNotEqual(second["execution_id"], first["execution_id"])
        recorded = board.qa_result(self.root, qa["id"], request["id"], "passed", "TASK DONE: YES - after the board executed again", "")
        self.assertEqual(recorded["status"], "passed")

    def test_a_real_path_still_works_exactly_as_before(self):
        qa, request, _, review_evidence = self._claimed_review()
        board.execute_challenge(self.root, qa["id"], request["id"])
        recorded = board.qa_result(self.root, qa["id"], request["id"], "passed", "TASK DONE: YES - own evidence", str(review_evidence))
        self.assertEqual(recorded["status"], "passed")


# the inherited tests run in tests.test_execute_once; only this class's own tests run here
for _name in [n for n in dir(base.ExecuteOnceTests) if n.startswith("test_") and n not in VerdictWithoutEvidenceTests.__dict__]:
    setattr(VerdictWithoutEvidenceTests, _name, None)


class DirectiveTests(unittest.TestCase):
    def test_the_directive_tells_the_reviewer_not_to_pass_a_trusted_reference_back(self):
        text = " ".join((Path(__file__).resolve().parents[1] / "directives" / "AGENT.md").read_text().split())
        self.assertIn("never pass that to `--evidence`", text)
        self.assertIn("leave `--evidence` out to use that certified output, or pass the real path of a file you wrote", text)


class ClientMessageTests(unittest.TestCase):
    ARGS = ["qa-result", "--agent", "a-1", "--request", "r-1", "--result", "passed", "--summary", "s"]

    def run_client(self, extra, response=None):
        environment = {board_client.TOKEN_ENV: "t" * 32, board_client.ENDPOINT_ENV: "http://127.0.0.1:9", board_client.PROTOCOL_ENV: board_client.PROTOCOL_VERSION}
        errors = io.StringIO()
        with mock.patch.dict(os.environ, environment), mock.patch.object(board_client, "environment_state", return_value="active"), redirect_stderr(errors):
            if response is None:
                code = board_client.invoke([*self.ARGS, *extra])
            else:
                with mock.patch.object(board_client, "urlopen", return_value=response):
                    code = board_client.invoke([*self.ARGS, *extra])
        return code, errors.getvalue()

    def test_a_missing_file_says_so_and_does_not_blame_the_board(self):
        code, text = self.run_client(["--evidence", "/nonexistent/evidence.txt"])
        self.assertEqual(code, 2)
        self.assertIn("artifact file is unavailable: /nonexistent/evidence.txt", text)
        self.assertNotIn("invalid or incompatible", text)

    def test_other_local_problems_say_what_they_are(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder, binary, big = Path(temporary), Path(temporary) / "b.bin", Path(temporary) / "big.txt"
            binary.write_bytes(b"\xff\xfe\x00")
            for argument, expected in ((str(folder), "artifact must be a regular single-link file"), (str(binary), "artifact must be valid UTF-8")):
                code, text = self.run_client(["--evidence", argument])
                self.assertIn(expected, text)
                self.assertNotIn("invalid or incompatible", text)
            code, text = self.run_client(["--evidence", "a", "--evidence", "b"])
            self.assertIn("may be supplied only once", text)
            self.assertNotIn("invalid or incompatible", text)
            with mock.patch.object(board_client, "MAX_ARTIFACT_BYTES", 5):
                big.write_text("123456789")
                code, text = self.run_client(["--evidence", str(big)])
            self.assertIn("exceeds the 5-byte limit", text)
            # an OSError while reading (not only the explicit checks) is reported too
            readable = Path(temporary) / "ok.txt"
            readable.write_text("fine")
            with mock.patch.object(board_client.os, "read", side_effect=OSError(5, "Input/output error")):
                code, text = self.run_client(["--evidence", str(readable)])
            self.assertIn("artifact could not be read: Input/output error", text)
            self.assertNotIn("invalid or incompatible", text)

    def test_a_trusted_reference_is_explained_not_blamed_on_the_board(self):
        code, text = self.run_client(["--evidence", "trusted://abc123def456/board/evidence/internal-qa-1.txt"])
        self.assertEqual(code, 2)
        self.assertIn("is how the board names a file in its own private folder, not a path on this computer", text)
        self.assertIn("Omit --evidence to use the certified evidence of your execute-challenge", text)
        self.assertNotIn("invalid or incompatible", text)
        code, text = self.run_client(["--ledger", "x"])   # another operation's option is unaffected
        self.assertNotIn("private folder", text)

    def test_a_malformed_worker_response_keeps_the_old_line(self):
        class Response:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return b'{"unexpected": true}'
        code, text = self.run_client([], response=Response())     # --evidence omitted is allowed; the answer is malformed
        self.assertEqual(code, 2)
        self.assertIn("authenticated board worker response is invalid or incompatible", text)

    def test_the_typed_tools_report_the_real_reason_too(self):
        with mock.patch.object(board_client, "call", side_effect=board_client.ArtifactError("artifact file is unavailable: /x")):
            result = board_mcp._call("qa-result", {"request": "r-1", "result": "passed", "summary": "s", "evidence": "/x"}, agent="a-1")
        self.assertTrue(result["isError"])
        self.assertIn("artifact file is unavailable: /x", result["content"][0]["text"])
        argv = board_mcp.argv_for("qa-result", {"request": "r-1", "result": "passed", "summary": "s"})
        self.assertNotIn("--evidence", argv, "the typed tool accepts a verdict without evidence")


class GatewayAcceptsAVerdictWithoutEvidenceTests(surface.BoardArtifactIngestionTests):
    def test_the_gateway_passes_qa_result_without_an_evidence_upload_to_the_board(self):
        session, agent, _, token, gateway = self.session(kind="claude_reviewer", role="qa", task="REVIEW_QUEUE")
        seen = {}

        def fake_main(arguments):
            seen["arguments"] = list(arguments)
            print(json.dumps({"accepted": True}))
            return 0

        request = {"protocol": board_client.PROTOCOL_VERSION, "nonce": 12, "operation": "qa-result", "arguments": [
            "qa-result", "--agent", agent["id"], "--request", "r-1", "--result", "failed", "--summary", "s"], "artifacts": {}}
        with mock.patch.object(board, "main", side_effect=fake_main):
            gateway.execute(token, request)
        self.assertNotIn("--evidence", seen["arguments"], "nothing was uploaded; the board derives the evidence itself")


# the inherited ingestion tests run in tests.test_board_surface_integration; only this class's own test runs here
for _name in [n for n in dir(surface.BoardArtifactIngestionTests) if n.startswith("test_") and n not in GatewayAcceptsAVerdictWithoutEvidenceTests.__dict__]:
    setattr(GatewayAcceptsAVerdictWithoutEvidenceTests, _name, None)


if __name__ == "__main__":
    unittest.main()
