# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""The harness renders an agent's own local page (owner 2026-09-29, option 1).

Agents' browser tools refuse localhost inside their sandbox. The worker runs
outside it, so an agent asks the board: `screen-check --url ... --expect ...`.
These tests drive that through the real authenticated command surface and the
real harness browser, against a page whose text only exists after its script
runs.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from harness import board, browser_acceptance, control, owner_location
from harness.board_surface import (
    PROTOCOL_VERSION, CommandGateway, SessionTokenAuthority, SurfaceAuthorizationError, SurfaceProtocolError,
)
from harness.project_context import ProjectContext
from tests.environment_support import require_loopback

APP_PAGE = b"""<!doctype html><html><head><title>Studio</title></head><body>
<h1>Studio</h1><div style="display:contents"><div id="out">loading</div></div>
<button hidden>Accept</button><div style="display:none">Secret panel</div><p style="visibility:hidden">Ghost note</p>
<span style="opacity:0">Faded tip</span><span style="position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0)">Reader only</span><div style="position:absolute;left:-9999px">Parked away</div>
<script>setTimeout(()=>{document.querySelector('#out').textContent='Deliverables ready: 3 films';},300)</script>
</body></html>"""
# Review r1: a page whose only text is its title or hidden markup renders blank.
BLANK_PAGE = b"""<!doctype html><html><head><title>Only a title</title></head><body><button hidden>Accept</button></body></html>"""


class ScreenCheckFixture(unittest.TestCase):
    extra_pages: dict = {}

    def setUp(self):
        require_loopback()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name)
        code = base / "code"; code.mkdir()
        self.context = ProjectContext(code, base / "data", base / "workspaces")
        control.initialize(self.context)
        self.authority = SessionTokenAuthority(self.context)
        self.gateway = CommandGateway(self.context, self.authority)

        fixture = self

        class App(BaseHTTPRequestHandler):
            def log_message(self, *_): return
            def do_GET(self):
                page = fixture.extra_pages.get(self.path.split("?")[0]) or (BLANK_PAGE if self.path.startswith("/blank") else APP_PAGE)
                self.send_response(200); self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(page))); self.end_headers(); self.wfile.write(page)

        # The fixture server lives in this test process, whose folder is the repository, not the task's
        # work folder: tests state where the "listener" runs from (the work folder unless a test says otherwise).
        self.listener_cwd = str(code)
        patcher = mock.patch.object(owner_location, "process_cwd", side_effect=lambda pid: self.listener_cwd)
        patcher.start(); self.addCleanup(patcher.stop)
        patcher = mock.patch.object(owner_location, "listeners", side_effect=lambda port: ([os.getpid()], ""))
        self.listeners_patch = patcher.start(); self.addCleanup(patcher.stop)
        self.app = ThreadingHTTPServer(("127.0.0.1", 0), App)
        threading.Thread(target=self.app.serve_forever, daemon=True).start()
        self.addCleanup(self.app.server_close)
        self.addCleanup(self.app.shutdown)
        self.url = f"http://127.0.0.1:{self.app.server_address[1]}/deliverables"

    def agent(self, role):
        cache = self.__dict__.setdefault("_agents", {})
        if role in cache and getattr(self, "reuse_agents", False):
            return cache[role]
        cache[role] = self._new_agent(role)
        return cache[role]

    def _new_agent(self, role):
        kind = {"engineering": "codex_delivery", "qa": "claude_reviewer", "cto": "claude_cto"}[role]
        session = control.create(self.context, kind)
        self.authority.prepare(session["id"])
        control.attach(self.context, session["id"], os.getpid())
        agent = board.register(self.context, role, board.AWAITING_OWNER_DIRECTION, session_id=session["id"])
        self.agent_ids = getattr(self, "agent_ids", {})
        token = self.authority.claim(session["id"], os.getpid())
        self.agent_ids[token] = agent["id"]
        return agent, token

    def ask(self, token, *arguments):
        return self.gateway.execute(token, {
            "protocol": PROTOCOL_VERSION, "nonce": time.time_ns(),
            "operation": "screen-check",
            "arguments": ["screen-check", "--agent", self.agent_ids[token], *arguments],
        })


class ScreenCheckCommandTests(ScreenCheckFixture):
    def test_agent_gets_the_rendered_page_and_the_reviewer_gets_saved_evidence(self):
        for role in ("engineering", "qa"):
            with self.subTest(role=role):
                agent, token = self.agent(role)
                result = self.ask(token, "--url", self.url, "--expect", "deliverables ready")["result"]
                self.assertIn("Deliverables ready: 3 films", result["visible_text"], "scripts ran before the capture")
                self.assertNotIn("loading", result["visible_text"])
                self.assertTrue(result["expect_found"])
                self.assertFalse(result["blank"])
                event = [e for e in board.snapshot(self.context)["events"] if e.get("kind") == "screen_check"][-1]
                self.assertEqual((event["agent_id"], event["check_id"]), (agent["id"], result["id"]))
                for kind, item in event["evidence"].items():
                    data = Path(item["path"]).read_bytes()
                    self.assertEqual(hashlib.sha256(data).hexdigest(), item["sha256"], kind)
                self.assertTrue(Path(event["evidence"]["screenshot"]["path"]).read_bytes().startswith(b"\x89PNG"))
        # Review r1: only what a person sees counts - hidden, invisible and head text never do.
        for hidden in ("Accept", "Secret panel", "Ghost note", "Studio Studio", "Faded tip", "Reader only", "Parked away"):
            self.assertNotIn(hidden, self.ask(token, "--url", self.url)["result"]["visible_text"])
        self.assertFalse(self.ask(token, "--url", self.url, "--expect", "Accept")["result"]["expect_found"])
        blank = self.ask(token, "--url", self.url.replace("/deliverables", "/blank"), "--expect", "Accept")["result"]
        self.assertEqual((blank["blank"], blank["expect_found"], blank["visible_text"]), (True, False, ""))

    def test_refusals_happen_before_any_browser_starts(self):
        _agent, token = self.agent("engineering")
        self.gateway.served_ports.add(self.app.server_address[1])
        with mock.patch.object(browser_acceptance, "launch", side_effect=AssertionError("browser started")):
            with self.assertRaisesRegex(SurfaceAuthorizationError, "own board"):
                self.ask(token, "--url", self.url)
            for url in (
                "https://example.com/", "http://192.168.1.10:8900/", "file:///etc/passwd",
                "http://127.0.0.1:8740/", "http://127.0.0.1:8741/", "http://127.0.0.1:8742/",
            ):
                with self.subTest(url=url), self.assertRaises(SurfaceProtocolError):
                    self.ask(token, "--url", url)
            _cto, cto_token = self.agent("cto")
            with self.assertRaises(SurfaceAuthorizationError):
                self.ask(cto_token, "--url", self.url)

    def test_a_browser_failure_is_a_plain_error(self):
        _agent, token = self.agent("engineering")
        with mock.patch.object(browser_acceptance, "render_page", side_effect=RuntimeError("browser capture failed: crashed")):
            with self.assertRaisesRegex(SurfaceProtocolError, "harness browser could not render .*crashed"):
                self.ask(token, "--url", self.url)


class ScreenCheckBindingTests(ScreenCheckFixture):
    """P2a: a capture is tied to the server's folder and to the code in the task's work folder."""

    reuse_agents = True

    def task_of(self, check_id):
        return self.record(check_id)["task"]

    def git_tree(self):
        code = self.context.code_root
        (code / "app.py").write_text("print('v1')\n")
        for command in (["init", "-q"], ["add", "."], ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "one"]):
            subprocess.run(["git", *command], cwd=code, check=True, capture_output=True)
        return code

    def capture(self, role="engineering"):
        agent, token = self.agent(role)
        return agent, token, self.ask(token, "--url", self.url, "--expect", "deliverables ready")["result"]

    def ledger(self, *ids):
        path = self.context.data_root / "ledger.md"
        path.write_text("Scenario ledger\n" + "\n".join(ids) + "\n")
        return path

    def record(self, check_id):
        return json.loads((board.board_dir(self.context) / "evidence" / "screen-checks" / f"{check_id}.json").read_text())

    def test_a_server_running_outside_the_task_folders_is_refused_before_the_browser_starts(self):
        self.listener_cwd = "/Users/someone/other-project"
        _agent, token = self.agent("engineering")
        with mock.patch.object(browser_acceptance, "launch", side_effect=AssertionError("browser started")):
            with self.assertRaisesRegex(SurfaceProtocolError, "runs from /Users/someone/other-project.*run screen-check again"):
                self.ask(token, "--url", self.url)

    def test_a_server_whose_folder_cannot_be_read_is_recorded_unverified_not_refused(self):
        for cwd, nothing_seen in (("", False), ("/", False), (None, True)):
            with self.subTest(cwd=cwd):
                self.listener_cwd = cwd or ""
                self.listeners_patch.side_effect = (lambda port: ([], "no tool to see the listener")) if nothing_seen else (lambda port: ([os.getpid()], ""))
                _agent, _token, result = self.capture()
                self.assertTrue(result["expect_found"])
                self.assertEqual(self.record(result["id"])["listener"]["status"], "unverified")

    def test_a_server_inside_the_work_folder_is_recorded_with_pid_folder_and_fingerprint(self):
        self.git_tree()
        _agent, _token, result = self.capture()
        record = self.record(result["id"])
        self.assertEqual(record["listener"]["status"], "verified")
        self.assertEqual(record["listener"]["pids"], [os.getpid()])
        self.assertEqual(record["listener"]["cwd"], [str(self.context.code_root)])
        entry = record["candidate"]["roots"][0]
        self.assertIn("app.py", entry["files"])
        self.assertEqual(len(entry["digest"]), 64)
        self.assertIn("Not detected: a long-running server", record["note"])
        self.assertEqual((result["listener"], result["candidate"], result["unproven"]), ("verified", "bound", []))

    def test_a_temp_copy_is_recorded_but_not_called_verified_and_the_reason_reaches_the_agent(self):
        self.git_tree()
        self.listener_cwd = tempfile.gettempdir()
        _agent, _token, result = self.capture()
        self.assertEqual(self.record(result["id"])["listener"]["status"], "temp_copy")
        self.assertTrue(any("temp folder" in item for item in result["unproven"]))
        ledger = self.ledger(result["id"])
        cited = board._cited_screen_checks(self.context, self.task_of(result["id"]), ledger, candidate_root=self.context.code_root)
        self.assertEqual((cited[0]["listener"], cited[0]["bound"]), ("temp_copy", False))

    def test_a_mixed_set_of_listeners_passes_when_the_right_one_is_in_the_work_folder(self):
        self.git_tree()
        self.listeners_patch.side_effect = lambda port: ([os.getpid(), os.getpid() + 1], "")
        self.listener_cwds = {os.getpid(): str(self.context.code_root), os.getpid() + 1: "/Users/someone/other"}
        with mock.patch.object(owner_location, "process_cwd", side_effect=lambda pid: self.listener_cwds[pid]):
            _agent, _token, result = self.capture()
        self.assertEqual(self.record(result["id"])["listener"]["status"], "verified")

    def test_an_edit_after_the_capture_makes_the_citation_refuse_but_commit_and_ledger_do_not(self):
        code = self.git_tree()
        _agent, token, result = self.capture()
        task = self.task_of(result["id"])
        ledger = self.ledger(result["id"])
        # the normal flow: the ledger is written (it lives in the tree here) and the work is committed unchanged
        (code / "ledger.md").write_text(ledger.read_text())
        subprocess.run(["git", "add", "."], cwd=code, check=True, capture_output=True)
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "two"], cwd=code, check=True, capture_output=True)
        cited = board._cited_screen_checks(self.context, task, code / "ledger.md", candidate_root=code)
        self.assertEqual([item["id"] for item in cited], [result["id"]])
        self.assertTrue(cited[0]["bound"])
        (code / "app.py").write_text("print('v2')\n")
        with self.assertRaisesRegex(ValueError, r"code changed after this capture \(app.py\).*run screen-check again on the current tree"):
            board._cited_screen_checks(self.context, task, code / "ledger.md", candidate_root=code)
        # Executable-bit and new-file changes count too
        (code / "app.py").write_text("print('v1')\n")
        os.chmod(code / "app.py", 0o755)
        with self.assertRaisesRegex(ValueError, "app.py"):
            board._cited_screen_checks(self.context, task, code / "ledger.md", candidate_root=code)
        os.chmod(code / "app.py", 0o644)
        # the capture was taken on the old tree; a fresh capture on the new tree is accepted
        (code / "app.py").write_text("print('v2')\n")
        fresh = self.ask(token, "--url", self.url, "--expect", "deliverables ready")["result"]
        fresh_ledger = self.ledger(fresh["id"])
        self.assertEqual(len(board._cited_screen_checks(self.context, task, fresh_ledger, candidate_root=code)), 1)
        # a stale id left in the ledger beside the fresh one keeps the refusal until it is replaced
        fresh_ledger.write_text(fresh_ledger.read_text() + result["id"] + "\n")
        with self.assertRaisesRegex(ValueError, r"replace this id"):
            board._cited_screen_checks(self.context, task, fresh_ledger, candidate_root=code)

    def test_the_reviewers_ledger_is_compared_with_the_reviewed_commit_not_the_mutable_workspace(self):
        code = self.git_tree()
        _agent, _token, result = self.capture("qa")
        task = self.task_of(result["id"])
        ledger = self.ledger(result["id"])
        reviewed = board._commit_files(code, "HEAD")
        board._cited_screen_checks(self.context, task, ledger, require_role="qa", candidate_root=code, candidate_files=reviewed)
        (code / "app.py").write_text("print('changed by delivery')\n")
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qam", "three"], cwd=code, check=True, capture_output=True)
        newer = board._commit_files(code, "HEAD")
        with self.assertRaisesRegex(ValueError, "run screen-check again on the current tree"):
            board._cited_screen_checks(self.context, task, ledger, require_role="qa", candidate_root=code, candidate_files=newer)
        source = Path(board.__file__).read_text()
        self.assertEqual(source.count("candidate_root=execution_root)"), 1, "request_review binds the capture")
        self.assertIn("candidate_files=reviewed_files", source, "the challenge binds it to the reviewed commit")

    def test_a_record_from_before_binding_is_refused_for_a_new_citation_but_still_verifies_when_already_accepted(self):
        code = self.git_tree()
        _agent, _token, result = self.capture()
        task = self.task_of(result["id"])
        accepted = board._cited_screen_checks(self.context, task, self.ledger(result["id"]), candidate_root=code)
        path = board.board_dir(self.context) / "evidence" / "screen-checks" / f"{result['id']}.json"
        record = json.loads(path.read_text())
        for key in ("candidate", "listener"):
            record.pop(key)
        path.write_text(json.dumps(record))
        with self.assertRaisesRegex(ValueError, "captured before captures were tied to the code.*run screen-check again"):
            board._cited_screen_checks(self.context, task, self.ledger(result["id"]), candidate_root=code)
        (code / "app.py").write_text("print('later')\n")   # and a later edit never invalidates an accepted request
        board._verify_recorded_screen_checks(self.context, task, accepted)

    def test_a_capture_of_another_subtask_folder_is_not_accepted_for_this_one(self):
        code = self.git_tree()
        _agent, _token, result = self.capture()
        other = self.context.workspace_root / "other-subtask"
        other.mkdir(parents=True)
        with self.assertRaisesRegex(ValueError, "server running from .* not the candidate's folder"):
            board._cited_screen_checks(self.context, self.task_of(result["id"]), self.ledger(result["id"]), candidate_root=other)

    def test_code_changing_during_the_capture_or_a_redirect_is_refused(self):
        code = self.git_tree()
        _agent, token = self.agent("engineering")
        real = browser_acceptance.render_page
        def edits_while_rendering(url, runtime, timeout=30):
            page = real(url, runtime, timeout=timeout)
            (code / "app.py").write_text("print('edited mid-capture')\n")
            return page
        with mock.patch.object(browser_acceptance, "render_page", edits_while_rendering):
            with self.assertRaisesRegex(SurfaceProtocolError, "code changed while the page was being captured"):
                self.ask(token, "--url", self.url, "--expect", "deliverables ready")
        redirected = {"text": "x", "html": "<p>x</p>", "png": b"\x89PNG", "url": "http://127.0.0.1:9/other"}
        with mock.patch.object(browser_acceptance, "render_page", return_value=redirected):
            with self.assertRaisesRegex(SurfaceProtocolError, "redirected to http://127.0.0.1:9/other"):
                self.ask(token, "--url", self.url)

    def test_a_work_folder_git_cannot_read_is_recorded_unverified_not_refused(self):
        _agent, _token, result = self.capture()   # the folder is not a git tree
        record = self.record(result["id"])
        self.assertEqual(record["candidate"]["status"], "unverified")
        self.assertIn("not a git tree", record["candidate"]["reason"])
        cited = board._cited_screen_checks(self.context, self.task_of(result["id"]), self.ledger(result["id"]), candidate_root=self.context.code_root)
        self.assertEqual((cited[0]["candidate"], cited[0]["bound"]), ("unverified", False))


class ScreenCheckLedgerSourceTests(ScreenCheckFixture):
    """Review F1: the gateway runs request-review / attach-challenge-ledger against an UPLOADED COPY of the ledger.
    The committed ledger in the work folder gaining the capture id is not a code change; a product file is."""

    reuse_agents = True
    task_of = ScreenCheckBindingTests.task_of
    git_tree = ScreenCheckBindingTests.git_tree
    capture = ScreenCheckBindingTests.capture
    record = ScreenCheckBindingTests.record

    def commit(self, message="more"):
        code = self.context.code_root
        subprocess.run(["git", "add", "."], cwd=code, check=True, capture_output=True)
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", message], cwd=code, check=True, capture_output=True)

    def through_gateway(self, token, operation, ledger_file, task, **extra):
        """Upload the committed ledger exactly as the client does, and run the real CLI dispatch up to the citation check."""
        import base64
        code = self.context.code_root
        seen = {}

        def probe(root, agent_id, ledger, *args, **kwargs):
            seen["path"] = Path(ledger)
            if operation == "attach-challenge-ledger":
                return board._cited_screen_checks(
                    root, task, Path(ledger), require_role="qa", candidate_root=code,
                    candidate_files=board._commit_files(code, "HEAD"))
            return board._cited_screen_checks(root, task, Path(ledger), candidate_root=code)

        payload = Path(ledger_file).read_bytes()
        field, option = ("challenge_ledger", "--challenge-ledger") if operation == "attach-challenge-ledger" else ("ledger", "--ledger")
        arguments = [operation, "--agent", self.agent_ids[token], option, f"upload:{field}"]
        arguments += ["--request", "r-1"] if operation == "attach-challenge-ledger" else ["--summary", "s", "--unit-test-command", "true"]
        target = "attach_challenge_ledger" if operation == "attach-challenge-ledger" else "request_review"
        wrapper = (lambda root, agent, request, ledger, *a, **k: probe(root, agent, ledger)) if operation == "attach-challenge-ledger" else probe
        with mock.patch.object(board, target, wrapper):
            out = self.gateway.execute(token, {
                "protocol": PROTOCOL_VERSION, "nonce": time.time_ns(), "operation": operation, "arguments": arguments,
                "artifacts": {field: {"content": base64.b64encode(payload).decode(), "length": len(payload),
                                      "sha256": hashlib.sha256(payload).hexdigest(),
                                      "media_type": "text/markdown"}},
            })
        self.assertNotEqual(seen["path"], Path(ledger_file), "the board really ran against the uploaded copy")
        return out["result"]

    def prepared(self, role="engineering"):
        code = self.git_tree()
        _agent, token, result = self.capture(role)
        task = self.task_of(result["id"])
        ledger = code / "SCENARIO_LEDGER.md"
        ledger.write_text("| S-1 | counter | " + result["id"] + " showed 1 |\n")
        return code, token, result, task, ledger

    def test_a_committed_ledger_that_only_gains_the_capture_id_passes_through_the_gateway_upload(self):
        code, token, result, task, ledger = self.prepared()
        self.commit()
        cited = self.through_gateway(token, "request-review", ledger, task)
        self.assertEqual([item["id"] for item in cited], [result["id"]])

    def test_a_product_file_edit_after_the_capture_still_refuses_through_the_gateway(self):
        code, token, result, task, ledger = self.prepared()
        (code / "app.py").write_text("print('changed after capture')\n")
        self.commit()
        with self.assertRaisesRegex(SurfaceProtocolError, r"code changed after this capture \(app.py\)"):
            self.through_gateway(token, "request-review", ledger, task)

    def test_only_the_cited_ledger_itself_is_exempt(self):
        code, token, result, task, ledger = self.prepared()
        (code / "OTHER_LEDGER.md").write_text("another committed file named like a ledger\n")
        self.commit()
        with self.assertRaisesRegex(SurfaceProtocolError, r"code changed after this capture \(OTHER_LEDGER.md\)"):
            self.through_gateway(token, "request-review", ledger, task)
        # a ledger edited again after what was uploaded is not the cited ledger any more
        (code / "OTHER_LEDGER.md").unlink()
        self.commit()
        uploaded = Path(self._tmp.name) / "uploaded.md"
        uploaded.write_bytes(ledger.read_bytes())
        ledger.write_text(ledger.read_text() + "edited after the upload\n")
        self.commit()
        with self.assertRaisesRegex(SurfaceProtocolError, r"SCENARIO_LEDGER.md"):
            self.through_gateway(token, "request-review", uploaded, task)

    def test_the_reviewers_challenge_ledger_path_behaves_the_same(self):
        code, token, result, task, ledger = self.prepared("qa")
        self.commit()
        cited = self.through_gateway(token, "attach-challenge-ledger", ledger, task)
        self.assertEqual([item["id"] for item in cited], [result["id"]])
        (code / "app.py").write_text("print('changed')\n")
        self.commit("product edit")
        with self.assertRaisesRegex(SurfaceProtocolError, r"code changed after this capture \(app.py\)"):
            self.through_gateway(token, "attach-challenge-ledger", ledger, task)


if __name__ == "__main__":
    unittest.main()
