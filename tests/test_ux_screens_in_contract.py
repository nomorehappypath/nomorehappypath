# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""The screens are part of the contract (task 2026-11-21_ux-screens-in-contract).

Owner 2026-11-21: "I like that the UX is part of the contract and I will have a button to see the different UX screens" and "let
the skill be one of the best UX skills out there". Delivery uploads one inert HTML design per screen and state; the contract
carries them; the owner opens them with "See the screens"; the UX guide is agreed for every such task; screens found later go to
the owner first; the final review needs a rendered row for every design.

Run:  PYTHONPATH=. python3 -m unittest tests.test_ux_screens_in_contract -v
"""
from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from harness import board, board_viewer, contract_screens, review_brief, skill_library
from tests.test_requirements_contract import context, delivery_with_direction

TASK = "TASK-SCREENS"
GOOD = (b"<!doctype html><html><head><style>body{font:16px system-ui;margin:24px} .bar{background:#eef}</style></head>"
        b"<body><h1>Your leases</h1><p class=bar>Analysis running - step 2 of 4 - 0:42</p><a href=\"#top\">Back</a>"
        b"<img alt=\"\" src=\"data:image/png;base64,iVBORw0KGgo=\"></body></html>")
TEXT = ("In plain words:\nYou upload a lease and see what it means.\nNothing for you to do.\n\nAlready in the project:\nNothing related exists yet.\n\n"
        "1. Upload a lease.\n\nScreens:\n- Leases - the list of your documents, with Add and Remove; empty, error\n- Analysis running - steps and elapsed time\n")


class Fixture(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)
        self.root = context(self.base)
        self.agent = delivery_with_direction(self.root, TASK)
        patch = mock.patch("harness.control.enqueue_instruction", return_value={"id": "wake"})
        self.enqueue = patch.start()
        self.addCleanup(patch.stop)

    def design(self, name: str, data: bytes = GOOD) -> dict:
        path = self.base / (name.replace(" ", "-") + ".html")
        path.write_bytes(data)
        return board.contract_screen(self.root, self.agent["id"], name, str(path))

    def propose(self, text: str = TEXT, **kwargs):
        return board.record_requirement_proposal(self.root, self.agent["id"], text, screens_required=True, **kwargs)

    def state(self) -> dict:
        return board.snapshot(self.root)

    def proposal(self) -> dict:
        return self.state()["requirement_proposals"][TASK]

    def go_ahead_and_confirm(self, skills=None):
        proposal = self.proposal()
        board.record_requirements_decision(self.root, TASK, "go_ahead", version=proposal["version"],
                                           token=(proposal.get("contract_check") or {}).get("token", ""), skills=skills or [])
        board.record_requirement_confirmation(self.root, self.agent["id"], proposal["text"])


class DesignsAreInert(unittest.TestCase):
    def test_a_plain_design_with_inline_css_anchors_and_inline_images_is_accepted(self):
        self.assertEqual(contract_screens.validate(GOOD), "")

    def test_anything_that_could_run_or_load_from_outside_is_refused(self):
        cases = {
            b"<script>alert(1)</script>": "script",
            b"<p onclick=\"x()\">a</p>": "event handler",
            b"<iframe src=\"#\"></iframe>": "iframe",
            b"<link rel=stylesheet href=\"https://x.example/a.css\">": "link",
            b"<img src=\"https://x.example/a.png\">": "points outside",
            b"<img src=\"  https://x.example/a.png\">": "points outside",
            b"<img src=\"//x.example/a.png\">": "points outside",
            b"<a href=\"/other\">x</a>": "points outside",
            b"<a href=\"javascript:alert(1)\">x</a>": "points outside",
            b"<img srcset=\"data:image/png;base64,AA 1x, https://x.example/b.png 2x\">": "points outside",
            b"<img src=\"data:image/svg+xml,<svg onload=alert(1)>\">": "points outside",
            b"<div style=\"background:url(https://x.example/a.png)\">x</div>": "CSS url",
            b"<style>@import 'https://x.example/a.css';</style>": "@import",
            b"<style>p{background:url( 'http://x.example' )}</style>": "CSS url",
            b"<meta http-equiv=\"refresh\" content=\"0;url=https://x.example\">": "meta",
            b"<form action=\"https://x.example/send\"><input></form>": "points outside",
            b"<form><button formaction=\"https://x.example\">Go</button></form>": "points outside",
            b"<base href=\"https://x.example/\">": "base",
            b"<object data=\"#\"></object>": "object",
        }
        for data, word in cases.items():
            with self.subTest(design=data[:40]):
                self.assertIn(word, contract_screens.validate(b"<html><body>" + data + b"</body></html>"))
        # a form is a normal part of a screen design; the sandbox and form-action 'none' keep it from sending anything (real run 2026-11-22)
        self.assertEqual("", contract_screens.validate(b"<html><body><form action=\"#\"><label>Bill <input name=b></label>"
                                                     b"<button type=submit>Split</button></form></body></html>"))
        self.assertIn("KB", contract_screens.validate(b"<p>" + b"x" * (contract_screens.MAX_BYTES + 1)))
        self.assertIn("UTF-8", contract_screens.validate(b"<p>\xff\xfe</p>"))


class ContractCarriesTheScreens(Fixture):
    def test_uploads_replace_by_name_remove_and_list(self):
        self.design("Leases - empty")
        self.design("Analysis running")
        out = self.design("Leases - empty", GOOD.replace(b"Your leases", b"No lease yet"))
        self.assertEqual(out["pending"], ["Leases - empty", "Analysis running"])
        self.assertEqual(board.contract_screen(self.root, self.agent["id"], list_only=True)["pending"], ["Leases - empty", "Analysis running"])
        self.assertEqual(board.contract_screen(self.root, self.agent["id"], "Analysis running", remove=True)["pending"], ["Leases - empty"])
        with self.assertRaisesRegex(ValueError, "cannot be shown to the owner: a <script>"):
            self.design("Bad", b"<script>x</script>")
        self.assertEqual(board.contract_screen(self.root, self.agent["id"], list_only=True)["pending"], ["Leases - empty"])

    def test_a_task_that_builds_screens_cannot_file_its_contract_without_designs_or_a_reason(self):
        with self.assertRaisesRegex(ValueError, "contract-screen.*--no-screens"):
            self.propose()
        self.propose(no_screens="a command-line tool; nothing visible changes")
        self.assertEqual(self.proposal()["screens"], {"manifest": [], "no_screens": "a command-line tool; nothing visible changes"})

    def test_spec_document_only_and_small_change_tasks_are_unchanged(self):
        for field, value in (("task_kinds", "spec"), ("task_kinds", "analysis"), ("task_document_only", True)):
            with self.subTest(field=field, value=value):
                with board.locked_state(self.root) as state:
                    state.setdefault("task_kinds", {}).pop(TASK, None)
                    state.setdefault("task_document_only", {}).pop(TASK, None)
                    state[field][TASK] = value
                    state.get("requirement_proposals", {}).pop(TASK, None)
                state = self.state()
                self.assertFalse(contract_screens.ui_task(state, TASK))
        self.assertFalse(contract_screens.ui_task({}, TASK, small_change=True))

    def test_keep_screens_carries_the_earlier_set_and_otherwise_new_designs_are_needed(self):
        self.design("Leases - list")
        self.propose()
        first = self.proposal()["screens"]
        with board.locked_state(self.root) as state:             # the owner asked for changes: Delivery files again
            state["requirement_proposals"][TASK]["status"] = "modify_requested"
        with self.assertRaisesRegex(ValueError, "contract-screen"):
            self.propose()
        self.propose(keep_screens=True)
        self.assertEqual(self.proposal()["screens"], first)

    def test_go_ahead_and_confirmation_agree_the_ux_guide_whatever_was_ticked(self):
        self.design("Leases - list")
        self.propose()
        with board.locked_state(self.root) as state:
            state["requirement_proposals"][TASK]["skills_unavailable"] = "the skill library is switched off"
        self.go_ahead_and_confirm(skills=[])
        state = self.state()
        self.assertIn(contract_screens.UX_SKILL, [item["slug"] for item in state["task_skills_agreed"][TASK]])
        confirmation = state["requirement_confirmations"][TASK]
        self.assertEqual([item["name"] for item in confirmation["screens"]["manifest"]], ["Leases - list"])
        guide = next(item for item in state["task_skills_agreed"][TASK] if item["slug"] == contract_screens.UX_SKILL)
        self.assertTrue(Path(guide["path"]).is_file())

    def test_screens_found_later_wait_for_the_owner_and_hold_every_review(self):
        self.propose(no_screens="nothing visible at first")
        self.go_ahead_and_confirm()
        self.design("Settings - new screen")
        board.propose_screens(self.root, self.agent["id"])
        card = next(c for c in self.state()["owner_actions"].values() if c.get("choice_kind") == "screen_approval")
        self.assertEqual(card["choices"], ["Approve the screens", "Ask for changes"])
        with self.assertRaisesRegex(ValueError, "wait for the owner's approval"):
            board.screens_review_gate(self.root, self.agent["id"])
        board.owner_action_choose(self.root, card["id"], "Approve the screens")
        board.screens_review_gate(self.root, self.agent["id"])
        state = self.state()
        self.assertEqual([item["name"] for item in board.agreed_screens(state, TASK)], ["Settings - new screen"])
        self.assertIn(contract_screens.UX_SKILL, [item["slug"] for item in state["task_skills_agreed"][TASK]])

    def test_every_review_command_waits_for_new_screens(self):
        source = Path(board.__file__).read_text()
        for command in ("request-review", "request-qa", "request-independent-review"):
            block = source.split(f'elif args.command == "{command}":', 1)[1].split("elif args.command", 1)[0]
            self.assertIn("screens_review_gate(root, args.agent)", block, command)


class FinalReviewCoversEveryDesign(Fixture):
    def test_a_pass_needs_a_rendered_row_for_every_design_and_the_brief_names_them(self):
        self.design("Leases - list")
        self.design("Analysis running")
        self.propose()
        self.go_ahead_and_confirm()
        state = self.state()
        ledger = self.base / "challenge.md"
        request = {"task": TASK, "challenge_ledger": str(ledger)}
        ledger.write_text("| R-1 | Leases - list | screen-ab12cd shows the list with Add and Remove |\n")
        problem = board._screens_coverage_problem(self.root, state, request, TASK)
        self.assertIn("Analysis running", problem)
        self.assertNotIn("Leases - list,", problem)
        ledger.write_text(ledger.read_text() + "| R-2 | Analysis running | Not run: the job is long |\n")
        self.assertIn("Analysis running", board._screens_coverage_problem(self.root, state, request, TASK))  # Not run never completes a row
        ledger.write_text(ledger.read_text() + "| R-3 | Analysis running | screen-ff00aa shows step 2 of 4 and 0:42 |\n")
        self.assertEqual(board._screens_coverage_problem(self.root, state, request, TASK), "")
        brief_screens = [{"name": item["name"], "file": str(board.screens_folder(self.root, TASK) / item["file"]), "sha256": item["sha256"]}
                         for item in board.agreed_screens(state, TASK)]
        self.assertEqual([item["name"] for item in brief_screens], ["Leases - list", "Analysis running"])
        self.assertTrue(all(Path(item["file"]).is_file() for item in brief_screens))
        self.assertIn('"screens": [{"name": item["name"]', Path(review_brief.__file__).read_text())


class OwnerSeesTheScreens(Fixture):
    def setUp(self):
        from tests.environment_support import require_loopback
        require_loopback()
        super().setUp()

    def serve(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), board_viewer.make_handler(self.root))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return f"http://127.0.0.1:{server.server_address[1]}"

    def get(self, url):
        request = urllib.request.Request(url, headers={"Host": url.split("/")[2]})
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, dict(response.headers), response.read()
        except urllib.error.HTTPError as error:
            return error.code, dict(error.headers), error.read()

    def test_the_viewer_serves_each_design_inert_and_only_framed_by_this_page(self):
        self.design("Leases - list")
        self.propose()
        origin = self.serve()
        status, _, body = self.get(f"{origin}/api/tasks/{TASK}/screens/proposal")
        self.assertEqual((status, json.loads(body)["names"]), (200, ["Leases - list"]))
        status, headers, body = self.get(f"{origin}/api/tasks/{TASK}/screens/proposal/0")
        self.assertEqual(status, 200)
        self.assertEqual(body, GOOD)
        self.assertIn("default-src 'none'", headers["Content-Security-Policy"])
        self.assertIn("sandbox", headers["Content-Security-Policy"])
        self.assertEqual(headers["X-Frame-Options"], "SAMEORIGIN")
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        status, headers, _ = self.get(f"{origin}/api/tasks/{TASK}/screens/proposal")
        self.assertEqual(headers["X-Frame-Options"], "DENY")       # everything else stays unframeable
        self.assertEqual(self.get(f"{origin}/api/tasks/{TASK}/screens/proposal/7")[0], 404)
        stored = board.screens_folder(self.root, TASK) / self.proposal()["screens"]["manifest"][0]["file"]
        stored.write_bytes(GOOD.replace(b"leases", b"LEASES"))
        status, _, body = self.get(f"{origin}/api/tasks/{TASK}/screens/proposal/0")
        self.assertEqual(status, 409)
        self.assertIn("changed after it was agreed", json.loads(body)["error"])

    def test_through_the_projects_app_a_design_keeps_its_frame_and_sandbox_policy(self):
        # Real run 2026-11-22: the owner opens the board through the Projects app, whose proxy put its own DENY headers on the
        # design and dropped its sandbox policy, so the owner saw a blank frame. The proxy now passes the board's policy through.
        from harness import project_manager
        self.design("Leases - list")
        self.propose()
        board_origin = self.serve()
        manager = project_manager.ProjectManager(Path(self._tmp.name) / "manager", board_port=int(board_origin.rsplit(":", 1)[1]))

        class Running:
            pid = 1
            def poll(self): return None
        manager.worker = Running()
        server = ThreadingHTTPServer(("127.0.0.1", 0), project_manager.make_handler(manager))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        manager.manager_url = f"http://127.0.0.1:{server.server_address[1]}/"
        route = manager.manager_url.rstrip("/") + project_manager.PROJECT_ROUTE + f"api/tasks/{TASK}/screens/proposal"
        status, headers, body = self.get(route + "/0")
        self.assertEqual((status, body), (200, GOOD))
        self.assertEqual(headers["X-Frame-Options"], "SAMEORIGIN")
        self.assertEqual(headers["Content-Security-Policy"], contract_screens.CONTENT_POLICY)
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        status, headers, _ = self.get(route)
        self.assertEqual((status, headers["X-Frame-Options"]), (200, "DENY"))      # everything else stays unframeable
        self.assertEqual(headers["Content-Security-Policy"], "frame-ancestors 'none'")

    def test_the_card_has_the_button_and_the_page_has_the_sandboxed_viewer(self):
        page = board_viewer.PAGE if hasattr(board_viewer, "PAGE") else Path(board_viewer.__file__).read_text()
        self.assertIn('<iframe id="screens-frame" sandbox=""', page)
        self.assertIn("See the screens (${list.length})", page)
        self.assertIn("${screensButtonHtml(name,proposal.screens,'proposal')}", page)
        self.assertIn("No screens:", page)


class TheGuide(unittest.TestCase):
    def test_the_ux_guide_is_the_new_one_with_its_references_license_and_credits(self):
        folder = Path(skill_library.__file__).resolve().parent.parent / "skills" / "ux-ui-design"
        text = (folder / "SKILL.md").read_text()
        for words in ("The NMHP house standard", "Workflow 1: design the screens before the contract", "contract-screen",
                      "screen-script", "Generated-looking defaults to refuse", "Reference routing", "Credits"):
            self.assertIn(words, text)
        self.assertEqual(len(list((folder / "references").glob("*.md"))), 31)
        self.assertIn("MIT License", (folder / "references" / "LICENSE").read_text())
        for line in text.splitlines():
            for ref in [part for part in line.split("`") if part.startswith("references/") and not part.endswith("/")]:
                self.assertTrue((folder / ref).is_file(), ref)
        notice = (folder.parent.parent / "NOTICE").read_text()
        self.assertIn("ux-designer-skill", notice)
        self.assertIn("ux-ui-design", [item.slug for item in skill_library.usable_skills()] if hasattr(skill_library, "usable_skills")
                      else [item.slug for item in board._usable_library_skills()])


if __name__ == "__main__":
    unittest.main()


PROBE = r"""
<script>
(async () => {
  const wait = ms => new Promise(resolve => setTimeout(resolve, ms));
  const visible = node => Boolean(node) && node.getBoundingClientRect().width > 0 && node.getBoundingClientRect().height > 0;
  let button = null;
  for (let attempt = 0; attempt < 150 && !(button = document.querySelector('[data-screens-task]')); attempt++) await wait(100);
  const reading = {buttonText: button ? button.textContent.trim() : '', buttonVisible: visible(button)};
  if (button) {
    button.click();
    await wait(1500);
    const dialog = document.querySelector('#screens-dialog');
    const frame = document.querySelector('#screens-frame');
    reading.dialogOpen = Boolean(dialog && dialog.open);
    reading.name = (document.querySelector('#screens-dialog-name')?.textContent || '').trim();
    reading.position = (document.querySelector('#screens-pos')?.textContent || '').trim();
    reading.frameSrc = frame ? frame.getAttribute('src') : '';
    reading.frameSandbox = frame ? frame.getAttribute('sandbox') : null;
    reading.frameVisible = visible(frame);
    document.querySelector('#screens-next')?.click();
    await wait(1200);
    reading.secondName = (document.querySelector('#screens-dialog-name')?.textContent || '').trim();
  }
  await fetch('/__probe__', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(reading)});
})();
</script>
"""


class RenderedScreensButton(Fixture):
    """In headless Chrome: the contract card's button opens each design; a design that slipped past the upload checks still
    cannot load anything from outside (the page's sandboxed frame and the design's content policy)."""

    def setUp(self):
        from harness import browser_acceptance
        from tests.environment_support import require_loopback
        try:
            browser_acceptance.resolve_binary()
        except (FileNotFoundError, ValueError) as error:
            raise unittest.SkipTest(str(error)) from error
        require_loopback()
        super().setUp()

    def test_see_the_screens_opens_each_design_and_nothing_leaves_the_page(self):
        import time
        from http.server import BaseHTTPRequestHandler
        from harness import browser_acceptance
        sink: dict = {"leaks": 0}
        viewer = ThreadingHTTPServer(("127.0.0.1", 0), board_viewer.make_handler(self.root))
        threading.Thread(target=viewer.serve_forever, daemon=True).start()
        self.addCleanup(viewer.server_close)
        self.addCleanup(viewer.shutdown)
        target = f"http://127.0.0.1:{viewer.server_address[1]}"

        class Proxy(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def log_message(self, *_args):
                return

            def forward(self, data=None):
                if self.path.startswith("/__leak__"):
                    sink["leaks"] += 1
                    self.send_response(200); self.send_header("Content-Length", "0"); self.end_headers(); return
                if self.path == "/__probe__":
                    sink["value"] = json.loads(data or b"{}")
                    self.send_response(200); self.send_header("Content-Length", "2"); self.end_headers(); self.wfile.write(b"{}"); return
                request = urllib.request.Request(target + self.path, data=data, method=self.command, headers={"Content-Type": "application/json"})
                try:
                    response = urllib.request.urlopen(request, timeout=15)
                    status, headers, payload = response.status, response.headers, response.read()
                except urllib.error.HTTPError as error:
                    status, headers, payload = error.code, error.headers, error.read()
                if self.path == "/" and headers.get("Content-Type", "").startswith("text/html"):
                    payload = payload.decode().replace("</body>", PROBE + "</body>", 1).encode()
                self.send_response(status)
                for name in ("Content-Type", "Content-Security-Policy", "X-Frame-Options", "X-Content-Type-Options", "Referrer-Policy"):
                    for value in headers.get_all(name) or []:
                        self.send_header(name, value)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def do_GET(self):
                self.forward()

            def do_POST(self):
                self.forward(self.rfile.read(int(self.headers.get("Content-Length") or 0)))

        proxy = ThreadingHTTPServer(("127.0.0.1", 0), Proxy)
        threading.Thread(target=proxy.serve_forever, daemon=True).start()
        self.addCleanup(proxy.server_close)
        self.addCleanup(proxy.shutdown)
        origin = f"http://127.0.0.1:{proxy.server_address[1]}"
        self.design("Leases - list")
        self.design("Analysis running")
        self.propose()
        # a design the upload checks missed (simulated: written straight to storage with a matching record) tries to load
        # an image and a stylesheet from outside; the page must not let it
        leak = (f'<html><body><img src="{origin}/__leak__/img"><link rel=stylesheet href="{origin}/__leak__/css">'
                f'<div style="background:url({origin}/__leak__/bg)">x</div></body></html>').encode()
        stored = contract_screens.store(board.screens_folder(self.root, TASK), leak)
        with board.locked_state(self.root) as state:
            state["requirement_proposals"][TASK]["screens"]["manifest"][0].update(stored)
        profile = tempfile.TemporaryDirectory()
        self.addCleanup(profile.cleanup)
        process = browser_acceptance.launch(f"{origin}/", Path(profile.name), width=1280, height=1000)
        try:
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline and "value" not in sink:
                time.sleep(0.1)
            time.sleep(1)
        finally:
            process.close()
        self.assertIn("value", sink, "Chrome reported no probe")
        reading = sink["value"]
        self.assertEqual(reading["buttonText"], "See the screens (2)", reading)
        self.assertTrue(reading["buttonVisible"])
        self.assertTrue(reading["dialogOpen"], reading)
        self.assertEqual((reading["name"], reading["position"]), ("Leases - list", "1 of 2"))
        self.assertEqual(reading["frameSandbox"], "")
        self.assertTrue(reading["frameVisible"])
        self.assertIn(f"/api/tasks/{TASK}/screens/proposal/", reading["frameSrc"])
        self.assertEqual(reading["secondName"], "Analysis running")
        self.assertEqual(sink["leaks"], 0, "a design reached outside the page")
