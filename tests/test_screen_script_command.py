"""The harness runs a click/type step list against an agent's own local page (`screen-script`, owner order 2026-10-15).

Everything goes through the real authenticated command surface (steps travel as uploaded bytes) and the real harness
browser, against a small page served from this process. Without a usable browser the browser tests skip, like
tests/test_screen_check_command.py; the step-list rules and the refusals that happen before a browser starts always run.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import time
import unittest
from pathlib import Path
from unittest import mock

from harness import board, board_client, browser_acceptance, screen_script
from harness.board_surface import (
    PROTOCOL_VERSION, SessionTokenAuthority, SurfaceAuthorizationError, SurfaceProtocolError,
)
from tests.test_screen_check_command import ScreenCheckFixture

PAGE = b"""<!doctype html><html><head><title>Todo</title></head><body>
<h1>Todo list</h1>
<input id="name" type="text" placeholder="item"><button id="add">Add item</button><ul id="list"></ul>
<button id="off" disabled>Disabled button</button>
<button id="ghost" style="opacity:0">Ghost</button><button id="reveal">Reveal</button><div id="msg"></div>
<div style="position:relative"><button id="under">Under cover</button>
<div style="position:absolute;left:0;top:0;width:100%;height:100%;background:#fff;opacity:.01"></div></div>
<label for="up">Upload report</label><input id="up" type="file" style="display:none">
<a href="http://203.0.113.9/leave" id="ext">External link</a>
<button id="call" onclick="fetch('http://203.0.113.9/data').catch(()=>{document.querySelector('#msg').textContent='call failed'})">Call out</button>
<button id="pop" onclick="window.open('/other')">Open window</button>
<select id="pick"><option>a</option></select>
<script>
document.querySelector('#add').onclick=()=>{const li=document.createElement('li');li.textContent=document.querySelector('#name').value;document.querySelector('#list').append(li)};
document.querySelector('#reveal').onclick=()=>{document.querySelector('#ghost').style.opacity='1'};
document.querySelector('#ghost').onclick=()=>{document.querySelector('#msg').textContent='Ghost clicked'};
</script></body></html>"""


def steps_bytes(*steps) -> bytes:
    return json.dumps(list(steps)).encode()


class ScreenScriptStepListTests(unittest.TestCase):
    def refused(self, payload, words):
        with self.assertRaisesRegex(screen_script.ScriptError, words):
            screen_script.parse_steps(payload)

    def test_a_valid_list_is_accepted_and_returned_as_plain_data(self):
        steps = screen_script.parse_steps(steps_bytes(
            {"open": "/"}, {"type": {"selector": "#name", "text": "Buy milk"}},
            {"click": {"text": "Add item"}}, {"click": {"selector": "#add"}}, {"expect": "Buy milk"}))
        self.assertEqual(len(steps), 5)

    def test_bad_lists_are_refused_with_a_plain_reason(self):
        self.refused(steps_bytes(*[{"expect": "x1"}] * 13), "at most 12 steps; this one has 13")
        self.refused(steps_bytes({"click": {"text": "Go"}}), "last step must be an expect step")
        self.refused(steps_bytes({"eval": "alert(1)"}), "exactly one of: open, click, type, expect")
        self.refused(steps_bytes({"click": {"text": "a", "selector": "b"}, "expect": "x"}), "exactly one of")
        self.refused(steps_bytes({"click": {"text": "a", "selector": "b"}}, {"expect": "ok"}), r"click must be")
        self.refused(steps_bytes({"type": {"selector": "#a"}}, {"expect": "ok"}), "type must be")
        self.refused(steps_bytes({"expect": ""}), "non-empty")
        self.refused(steps_bytes({"expect": "x" * 501}), "500 characters or fewer")
        self.refused(b"not json", "not valid JSON")
        self.refused(b"{}", "JSON list")
        self.refused(b"[]", "JSON list")
        self.refused(b" " * 60_000, "larger than 50 KB")


class ScreenScriptFixture(ScreenCheckFixture):
    reuse_agents = True

    def run_script_command(self, token, steps, *arguments, nonce=None):
        payload = steps if isinstance(steps, bytes) else steps_bytes(*steps)
        return self.gateway.execute(token, {
            "protocol": PROTOCOL_VERSION, "nonce": nonce or time.time_ns(), "operation": "screen-script",
            "arguments": ["screen-script", "--agent", self.agent_ids[token], "--steps", "upload:steps", *arguments],
            "artifacts": {"steps": {
                "content": base64.b64encode(payload).decode(), "length": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(), "media_type": "text/plain",
            }},
        })



class ScreenScriptRefusalTests(ScreenScriptFixture):
    """Refusals that must happen before any browser starts."""

    def test_refusals_happen_before_any_browser_starts(self):
        _agent, token = self.agent("engineering")
        good = [{"expect": "Todo"}]
        with mock.patch.object(browser_acceptance, "launch", side_effect=AssertionError("browser started")):
            for url in ("https://example.com/", "http://192.168.1.10:8900/", "file:///etc/passwd",
                        "http://127.0.0.1:8740/", "http://127.0.0.1:8741/", "http://127.0.0.1:8742/"):
                with self.subTest(url=url), self.assertRaises(SurfaceProtocolError):
                    self.run_script_command(token, good, "--url", url)
            with self.assertRaisesRegex(SurfaceProtocolError, "at most 12 steps"):
                self.run_script_command(token, [{"expect": "ab"}] * 13, "--url", self.url)
            with self.assertRaisesRegex(SurfaceProtocolError, "last step must be an expect"):
                self.run_script_command(token, [{"click": {"text": "Go"}}], "--url", self.url)
            self.gateway.served_ports.add(self.app.server_address[1])
            with self.assertRaisesRegex(SurfaceAuthorizationError, "own board"):
                self.run_script_command(token, good, "--url", self.url)

    def test_only_delivery_and_reviewer_may_run_a_script_and_the_cto_may_not(self):
        _cto, cto_token = self.agent("cto")
        with self.assertRaises(SurfaceAuthorizationError):
            self.run_script_command(cto_token, [{"expect": "Todo"}], "--url", self.url)

    def test_the_client_waits_long_enough_for_a_whole_script_and_never_retries_it(self):
        steps_file = Path(self._tmp.name) / "steps.json"
        steps_file.write_bytes(steps_bytes({"expect": "Todo"}))
        seen = []

        def stop(request, timeout=None):
            seen.append(timeout)
            raise OSError("stop here")
        environment = {board_client.TOKEN_ENV: "t" * 40, board_client.ENDPOINT_ENV: "http://127.0.0.1:9", board_client.PROTOCOL_ENV: PROTOCOL_VERSION}
        with mock.patch.dict(os.environ, environment), mock.patch.object(board_client, "urlopen", stop):
            board_client.invoke(["screen-script", "--agent", "a", "--url", self.url, "--steps", str(steps_file)])
        self.assertEqual(seen, [240], "one attempt, with more time than the 90 second script budget")
        self.assertIn("screen-script", board_client.UPLOAD_ARGUMENTS)


class ScreenScriptBrowserTests(ScreenScriptFixture):
    extra_pages = {"/todo": PAGE}

    def setUp(self):
        super().setUp()
        self.page_url = self.url.replace("/deliverables", "/todo")
        self.addCleanup(lambda: None)

    def go(self, steps, role="engineering", url=None):
        _agent, token = self.agent(role)
        try:
            return self.run_script_command(token, steps, "--url", url or self.page_url)["result"]
        except SurfaceProtocolError as error:
            if "no usable browser" in str(error) or "tell the owner" in str(error) and "browser" in str(error):
                self.skipTest("no usable harness browser on this machine: " + str(error)[:120])
            raise

    def record(self, check_id):
        return json.loads((board.board_dir(self.context) / "evidence" / "screen-checks" / f"{check_id}.json").read_text())

    def test_type_click_and_expect_pass_and_the_run_is_hashed_evidence_a_ledger_can_cite(self):
        result = self.go([{"type": {"selector": "#name", "text": "Buy milk"}}, {"click": {"text": "Add item"}}, {"expect": "Buy milk"}])
        self.assertTrue(result["all_passed"], result)
        self.assertRegex(result["id"], board.SCREEN_CHECK_ID)
        record = self.record(result["id"])
        self.assertEqual((record["kind"], record["all_passed"], len(record["steps"])), ("screen-script", True, 3))
        self.assertIn("step03-screenshot", record["evidence"])
        for kind, item in record["evidence"].items():
            self.assertEqual(hashlib.sha256(Path(item["path"]).read_bytes()).hexdigest(), item["sha256"], kind)
        self.assertTrue(Path(record["evidence"]["screenshot"]["path"]).read_bytes().startswith(b"\x89PNG"))
        self.assertIn("Buy milk", Path(record["evidence"]["text"]["path"]).read_text())
        # the record carries the candidate binding like a screen-check, and a ledger can cite its id
        self.assertIn(record["listener"]["status"], {"verified"})
        ledger = Path(self._tmp.name) / "ledger.md"
        ledger.write_text(f"| S-1 | add an item | {result['id']} showed Buy milk |\n")
        cited = board._cited_screen_checks(self.context, record["task"], ledger, candidate_root=self.context.code_root)
        self.assertEqual((cited[0]["id"], cited[0]["kind"]), (result["id"], "screen-script"))
        # tampering with the saved steps afterwards is caught like any screen-check evidence
        Path(record["evidence"]["steps"]["path"]).write_text("[]")
        with self.assertRaisesRegex(ValueError, "no longer matches the capture"):
            board._cited_screen_checks(self.context, record["task"], ledger, candidate_root=self.context.code_root)

    def test_reading_the_page_never_changes_it_so_a_control_that_appears_later_can_still_be_clicked(self):
        result = self.go([{"expect": "Todo list"}, {"click": {"selector": "#reveal"}}, {"click": {"text": "Ghost"}}, {"expect": "Ghost clicked"}])
        self.assertTrue(result["all_passed"], result)

    def test_a_selector_or_text_that_matches_nothing_stops_the_run_and_the_record_cannot_be_cited(self):
        result = self.go([{"expect": "Todo list"}, {"click": {"selector": "#nope"}}, {"expect": "never"}])
        self.assertFalse(result["all_passed"])
        self.assertEqual([step["ok"] for step in result["steps"]], [True, False])
        self.assertIn("no element matches the selector", result["steps"][1]["reason"])
        self.assertIn("stopped at step 2", result["outcome"])
        ledger = Path(self._tmp.name) / "ledger.md"
        ledger.write_text(result["id"])
        with self.assertRaisesRegex(ValueError, "blank or did not find|did not pass every step"):
            board._cited_screen_checks(self.context, self.record(result["id"])["task"], ledger)
        self.assertFalse(result["citable"])

    def test_controls_a_person_could_not_use_are_refused_with_the_reason(self):
        for step, words in (
            ({"click": {"selector": "#off"}}, "disabled"),
            ({"click": {"selector": "#under"}}, "covered by another element"),
            ({"click": {"text": "Upload report"}}, "file upload is not allowed"),
            ({"click": {"selector": "#ghost"}}, "not visible"),
            ({"click": {"selector": "button"}}, "visible elements match"),
            ({"type": {"selector": "#pick", "text": "b"}}, "typing is supported in text inputs"),
            ({"click": {"selector": "#("}}, "selector is not valid"),
        ):
            with self.subTest(step=step):
                result = self.go([step, {"expect": "Todo"}])
                self.assertFalse(result["all_passed"])
                self.assertRegex(result["steps"][0]["reason"], words)

    def test_the_page_cannot_leave_its_own_server_by_link_request_or_new_window(self):
        for step, words in (
            ({"click": {"text": "External link"}}, "203.0.113.9"),
            ({"click": {"text": "Call out"}}, "203.0.113.9"),
            ({"click": {"text": "Open window"}}, "new window"),
        ):
            with self.subTest(step=step):
                result = self.go([step, {"expect": "Todo list"}])
                self.assertFalse(result["all_passed"], result)
                self.assertRegex(result["steps"][0]["reason"], words)

    def test_open_may_only_go_to_the_same_server(self):
        ok = self.go([{"open": "/deliverables"}, {"expect": "Deliverables ready"}])
        self.assertTrue(ok["all_passed"], ok)
        bad = self.go([{"open": "http://127.0.0.1:9/elsewhere"}, {"expect": "x1"}])
        self.assertFalse(bad["all_passed"])
        self.assertIn("same server", bad["steps"][0]["reason"])

    def test_one_deadline_covers_the_whole_run(self):
        started = time.monotonic()
        with mock.patch.object(screen_script, "STEP_WAIT_SECONDS", 30):
            run = self.run_with_timeout([{"expect": "text that never appears"}], timeout=4)
        self.assertFalse(run["all_passed"])
        self.assertLess(time.monotonic() - started, 40)

    def run_with_timeout(self, steps, timeout):
        import tempfile
        runtime = Path(tempfile.mkdtemp(prefix="screen-script-test-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(runtime, ignore_errors=True))
        try:
            return screen_script.run_script(self.page_url, screen_script.parse_steps(steps_bytes(*steps)), runtime, timeout=timeout)
        except (RuntimeError, OSError) as error:
            self.skipTest("no usable harness browser: " + str(error)[:120])

    def test_a_run_stops_when_the_agent_is_no_longer_allowed_and_says_so(self):
        calls = []
        def authorized():
            calls.append(1)
            return len(calls) < 2
        import tempfile
        runtime = Path(tempfile.mkdtemp(prefix="screen-script-test-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(runtime, ignore_errors=True))
        try:
            run = screen_script.run_script(self.page_url, screen_script.parse_steps(steps_bytes({"expect": "Todo list"}, {"click": {"selector": "#add"}}, {"expect": "Todo"})),
                                           runtime, authorized=authorized)
        except (RuntimeError, OSError) as error:
            self.skipTest("no usable harness browser: " + str(error)[:120])
        self.assertFalse(run["all_passed"])
        self.assertEqual(len(run["steps"]), 2)
        self.assertIn("no longer allowed", run["steps"][1]["reason"])

    def test_a_reviewer_cannot_satisfy_a_delivery_script_with_a_plain_screenshot(self):
        source = Path(board.__file__).read_text()
        self.assertIn("does not satisfy it", source)
        self.assertIn('item.get("kind") == "screen-script" and item.get("role") == "qa"', source)


if __name__ == "__main__":
    unittest.main()
