# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Owner 2026-10-15 (Studio, task jls-brand-strategy-quality-diagnostic): the deliverable was a report;
the release card said "read the report", View app showed nothing, and the CTO repeated "awaiting owner"
for hours. Now: Delivery marks the document (contract-evidence --for-owner) or the CTO marks an attached
file (deliverable-file); the board links it on the task card, the release card, the owner card and History;
a card that tells the owner to read something unreachable is refused; the directive says to open the path
first. These tests fail on main (no link; the card is accepted).
"""
from __future__ import annotations

import io
import json
import os
from contextlib import redirect_stdout
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from harness import board, board_surface, board_viewer, claude_inbox, contract, control, cto as cto_module, gemini_runtime, review_brief
from tests.environment_support import require_loopback
from tests.test_requirements_contract import context, delivery_with_direction

ROOT = Path(__file__).resolve().parents[1]
STUDIO_TITLE = "Read the JLS output-quality diagnostic report, then Accept or Reject"
STUDIO_WHY = ("Open it in View app: it names real, quoted content misses in the existing Brand Strategy deck "
              "(unsupported buyer quotes, no buyer research per production notes, an unproven bilingual claim), plus recommendations.")
REPORT = "An owner-readable evidence report with the directive acceptance map"
FIX = "A four-line test-isolation fix"
REPORT_TEXT = "# JLS Brand Strategy quality diagnostic — content-focused update\n\n**Status: complete.**\n\ncommand: python3 -m unittest\nresult: ok\n"
EARLIER_TEXT = "# JLS Brand Strategy quality diagnostic — evidence update\n\nStatus: partial.\n"


def flat(path: str) -> str:
    return " ".join((ROOT / path).read_text(encoding="utf-8").split())


class _Project(unittest.TestCase):
    def setUp(self):
        require_loopback()
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = context(Path(self.temporary.name))
        self.delivery = delivery_with_direction(self.root, "TASK-DOC")
        session = control.create(self.root, "claude_cto")
        self.cto = board.register(self.root, "cto", "GLOBAL_MONITOR", vendor="Anthropic", session_id=session["id"])
        board.create_contract(self.root, self.delivery["id"], "Diagnose the existing deck", [REPORT, FIX])
        self.uploads = self.root / ".harness" / "evidence" / "uploads" / "session"
        self.uploads.mkdir(parents=True)

    def file(self, name: str, text: str) -> Path:
        path = self.uploads / name
        path.write_text(text, encoding="utf-8")
        return path

    def attach(self, name: str, text: str, for_owner: bool = False, deliverable: str = REPORT, document: str = "") -> Path:
        path = self.file(name, text)
        board.attach_contract_evidence(self.root, self.delivery["id"], deliverable, str(path), for_owner, document)
        return path

    def pin(self, title: str = STUDIO_TITLE, why: str = STUDIO_WHY, task: str = "TASK-DOC", **kw) -> dict:
        return board.record_owner_action(self.root, self.cto["id"], title, why=why, task=task, **kw)

    def links(self, task: str = "TASK-DOC") -> list[dict]:
        return board.document_deliverable_links(self.root, task)


class ReadableDocumentTests(unittest.TestCase):
    """One validator for discovery, the card guard and the route."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        self.path = self.base / "report.txt"
        self.path.write_text(REPORT_TEXT, encoding="utf-8")
        self.sha = contract.hashlib.sha256(REPORT_TEXT.encode()).hexdigest()

    def test_a_recorded_text_file_inside_the_roots_is_readable_and_its_bytes_are_returned(self):
        ok, reason, payload = contract.readable_document(str(self.path), self.sha, [self.base])
        self.assertTrue(ok, reason)
        self.assertEqual(payload, REPORT_TEXT.encode())

    def test_every_failure_is_a_plain_reason_never_an_exception(self):
        cases = {
            "outside": (str(self.path), self.sha, [self.base / "elsewhere"]),
            "gone": (str(self.base / "missing.txt"), self.sha, [self.base]),
            "changed": (str(self.path), "0" * 64, [self.base]),
            "empty": ("", self.sha, [self.base]),
        }
        for label, arguments in cases.items():
            ok, reason, payload = contract.readable_document(*arguments)
            self.assertFalse(ok, label)
            self.assertTrue(reason, label)
            self.assertEqual(payload, b"", label)
        self.assertIn("outside this project", contract.readable_document(str(self.path), self.sha, [self.base / "elsewhere"])[1])
        self.assertIn("gone", contract.readable_document(str(self.base / "missing.txt"), self.sha, [self.base])[1])
        self.assertIn("changed since", contract.readable_document(str(self.path), "0" * 64, [self.base])[1])

    def test_a_symlink_a_directory_a_binary_a_token_and_an_oversized_file_are_refused(self):
        link = self.base / "link.txt"
        os.symlink(self.path, link)
        self.assertIn("not a regular", contract.readable_document(str(link), self.sha, [self.base])[1])
        self.assertFalse(contract.readable_document(str(self.base), self.sha, [self.base])[0])
        binary = self.base / "bin.txt"; binary.write_bytes(b"\xff\xfe\x00binary")
        sha = contract.hashlib.sha256(binary.read_bytes()).hexdigest()
        self.assertIn("not a text document", contract.readable_document(str(binary), sha, [self.base])[1])
        token = self.base / "token.txt"; token.write_text("# Report\n\nsk-ant-oat01-" + "a" * 40 + "\n")
        sha = contract.hashlib.sha256(token.read_bytes()).hexdigest()
        self.assertIn("login token", contract.readable_document(str(token), sha, [self.base])[1])
        big = self.base / "big.txt"; big.write_bytes(b"x" * (contract.MAX_OWNER_DOCUMENT_BYTES + 1))
        sha = contract.hashlib.sha256(big.read_bytes()).hexdigest()
        self.assertIn("larger than", contract.readable_document(str(big), sha, [self.base])[1])

    def test_the_title_is_the_first_heading_else_the_first_line_and_never_a_token(self):
        self.assertEqual(contract.document_title(REPORT_TEXT.encode()), "JLS Brand Strategy quality diagnostic — content-focused update")
        self.assertEqual(contract.document_title(b"---\ntitle: x\n---\nPlain first line here\n"), "title: x")
        self.assertEqual(contract.document_title(b"\n\nPlain first line here\n# later\n"), "Plain first line here")
        self.assertEqual(contract.document_title(b"# sk-ant-oat01-" + b"a" * 40), contract.DOCUMENT_TITLE_WITHHELD)
        self.assertEqual(contract.document_title(b"\xff\xfe"), contract.DOCUMENT_TITLE_WITHHELD)
        self.assertLessEqual(len(contract.document_title(("# " + "w" * 300).encode())), contract.MAX_DOCUMENT_TITLE)


class DesignationTests(_Project):
    def test_delivery_marks_the_document_at_attach_time_and_the_board_links_it_newest_first(self):
        self.attach("1-evidence-a.txt", EARLIER_TEXT, for_owner=True)
        self.attach("2-evidence-log.txt", "python3 -m unittest\nOK\n")           # proof, not a document
        self.attach("3-evidence-b.txt", REPORT_TEXT, for_owner=True)
        links = self.links()
        self.assertEqual([(l["label"], l["title"], l["available"]) for l in links], [
            ("Open report", "JLS Brand Strategy quality diagnostic — content-focused update", True),
            ("Open earlier version", "JLS Brand Strategy quality diagnostic — evidence update", True),
        ])
        self.assertTrue(links[0]["latest"] and not links[1]["latest"])
        self.assertTrue(links[0]["url"].startswith("/api/deliverables/" + board._project_ref(self.root) + "/TASK-DOC/0/"))
        event = next(e for e in reversed(board.snapshot(self.root)["events"]) if e["kind"] == "completion_contract_evidence")
        self.assertTrue(event["for_owner"]); self.assertIn("the document the owner reads", event["message"])

    def test_the_cto_marks_an_already_attached_file_the_newest_mark_wins_and_remarking_reselects(self):
        a = self.attach("1-evidence-a.txt", EARLIER_TEXT)
        b = self.attach("2-evidence-b.txt", REPORT_TEXT)
        self.assertEqual(self.links(), [], "nothing marked, nothing linked")
        first = board.mark_deliverable_file(self.root, self.cto["id"], "TASK-DOC", REPORT, str(a))
        self.assertTrue(first["changed"]); self.assertEqual(first["audit_event"]["kind"], "deliverable_file_marked")
        second = board.mark_deliverable_file(self.root, self.cto["id"], "TASK-DOC", REPORT, b.name)   # by file name
        self.assertEqual(self.links()[0]["path"], str(b))
        again = board.mark_deliverable_file(self.root, self.cto["id"], "TASK-DOC", REPORT, b.name)
        self.assertFalse(again["changed"]); self.assertIsNone(again["audit_event"], "marking the current latest is a no-op")
        back = board.mark_deliverable_file(self.root, self.cto["id"], "TASK-DOC", REPORT, a.name)
        self.assertTrue(back["changed"]); self.assertEqual(self.links()[0]["path"], str(a), "re-marking an earlier file re-selects it")

    def test_only_recorded_evidence_only_the_cto_and_only_through_the_surface_as_a_cto_operation(self):
        a = self.attach("1-evidence-a.txt", EARLIER_TEXT)
        with self.assertRaises(ValueError) as refused:
            board.mark_deliverable_file(self.root, self.cto["id"], "TASK-DOC", REPORT, "/elsewhere/report.md")
        self.assertIn("not an evidence file recorded", str(refused.exception))
        with self.assertRaises(ValueError) as refused:
            board.mark_deliverable_file(self.root, self.delivery["id"], "TASK-DOC", REPORT, str(a))
        self.assertIn("only the active CTO", str(refused.exception))
        with self.assertRaises(ValueError):
            board.mark_deliverable_file(self.root, self.cto["id"], "TASK-DOC", "no such deliverable", str(a))
        self.assertIn("deliverable-file", board_surface.ALL_BOARD_OPERATIONS)
        self.assertEqual(board_surface.AUTHORIZATION_MATRIX["deliverable-file"], frozenset({"cto"}))
        self.assertIn("deliverable-file", board_surface.AGENT_ARGUMENT_OPERATIONS)
        with redirect_stdout(io.StringIO()):
            code = board.main(["--root", str(self.root), "deliverable-file", "--agent", self.cto["id"], "--task", "TASK-DOC",
                               "--deliverable", REPORT, "--evidence", a.name])
        self.assertEqual(code, 0)
        self.assertEqual(self.links()[0]["path"], str(a))
        with redirect_stdout(io.StringIO()):
            code = board.main(["--root", str(self.root), "contract-evidence", "--agent", self.delivery["id"], "--deliverable", FIX,
                               "--evidence", str(self.file("fix.txt", "# Fix notes\n")), "--for-owner", "--document", "Fix notes"])
        self.assertEqual(code, 0)
        self.assertEqual([l["document"] for l in self.links()], [REPORT, "Fix notes"])

    def test_a_deliverable_with_two_documents_keeps_both_current_and_versions_within_each(self):
        self.attach("r1.txt", EARLIER_TEXT, for_owner=True, document="Report")
        self.attach("p1.txt", "# Implementation plan v1\n", for_owner=True, document="Plan")
        self.attach("r2.txt", REPORT_TEXT, for_owner=True, document="Report")
        links = self.links()
        self.assertEqual([(l["document"], l["latest"]) for l in links], [("Report", True), ("Plan", True), ("Report", False)])

    def test_the_cto_retires_a_mistaken_document_name_without_waiving_and_a_new_mark_revives_it(self):
        """Plan check round 10, finding 5: a mistaken name must not block for ever."""
        report = self.attach("r.txt", REPORT_TEXT, for_owner=True, document="Report")
        wrong = self.attach("w.txt", "# Mistaken\n", for_owner=True, document="Repport")
        wrong.write_text("tampered\n")
        ok, problems, _ = cto_module.owner_artifacts_readable(self.root, board.snapshot(self.root), "TASK-DOC", None)
        self.assertFalse(ok); self.assertIn("--retire", problems[0])
        with self.assertRaises(ValueError):
            board.retire_deliverable_document(self.root, self.delivery["id"], "TASK-DOC", REPORT, "Repport")
        with self.assertRaises(ValueError):
            board.retire_deliverable_document(self.root, self.cto["id"], "TASK-DOC", REPORT, "Nothing")
        out = board.retire_deliverable_document(self.root, self.cto["id"], "TASK-DOC", REPORT, "Repport")
        self.assertTrue(out["changed"]); self.assertEqual(out["audit_event"]["kind"], "deliverable_file_retired")
        self.assertEqual([l["document"] for l in self.links()], ["Report"])
        self.assertTrue(cto_module.owner_artifacts_readable(self.root, board.snapshot(self.root), "TASK-DOC", None)[0])
        self.assertFalse(board.retire_deliverable_document(self.root, self.cto["id"], "TASK-DOC", REPORT, "Repport")["changed"])
        with redirect_stdout(io.StringIO()):
            code = board.main(["--root", str(self.root), "deliverable-file", "--agent", self.cto["id"], "--task", "TASK-DOC",
                               "--deliverable", REPORT, "--document", "Repport", "--retire"])
        self.assertEqual(code, 0)
        wrong.write_text("# Mistaken\n")   # bytes restored, then marked again under the name: revived
        board.mark_deliverable_file(self.root, self.cto["id"], "TASK-DOC", REPORT, "w.txt", document="Repport")
        self.assertEqual(sorted(l["document"] for l in self.links()), ["Report", "Repport"])
        self.assertNotIn(REPORT, contract._load(contract._task_path(self.root, "TASK-DOC"))["deliverables"][0].get("owner_waiver") or {}, "no waiver")

    def test_a_waived_deliverable_shows_no_document_and_an_unavailable_latest_is_listed_not_replaced(self):
        a = self.attach("1-evidence-a.txt", EARLIER_TEXT, for_owner=True)
        b = self.attach("2-evidence-b.txt", REPORT_TEXT, for_owner=True)
        b.write_text("tampered\n")
        links = self.links()
        self.assertEqual([l["available"] for l in links], [False, True])
        self.assertTrue(links[0]["latest"], "the newest mark stays the latest even when unreadable")
        self.assertIn("changed since", links[0]["reason"]); self.assertIn("deliverable-file", links[0]["recovery"])
        words = "the report deliverable is not required"
        message = board.record_owner_message(self.root, self.cto["id"], f"CTO: {words}.", "cto_message")["message"]
        board.waive_deliverable(self.root, self.cto["id"], "TASK-DOC", REPORT, message["id"], words)
        self.assertEqual(self.links(), [], "a waived deliverable is not something the owner reads")


class RouteTests(_Project):
    def serve(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), board_viewer.make_handler(self.root))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close); self.addCleanup(server.shutdown)
        return f"http://127.0.0.1:{server.server_address[1]}"

    def test_the_route_serves_the_marked_text_read_only_with_nosniff_and_refuses_everything_else(self):
        a = self.attach("1-evidence-a.txt", EARLIER_TEXT)
        b = self.attach("2-evidence-b.txt", REPORT_TEXT, for_owner=True)
        base = self.serve()
        link = self.links()[0]
        response = urlopen(base + link["url"], timeout=5)
        self.assertEqual(response.status, 200)
        self.assertEqual(response.headers["Content-Type"], "text/plain; charset=utf-8")
        self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(response.read(), REPORT_TEXT.encode())
        ref = board._project_ref(self.root)
        for path, expected in (
            (f"/api/deliverables/{ref}/TASK-DOC/0/0", "evidence for the reviewer"),           # attached, not marked
            (f"/api/deliverables/{ref}/TASK-DOC/0/9", "no such document"),
            (f"/api/deliverables/{ref}/TASK-NONE/0/0", "no such document"),
            (f"/api/deliverables/{ref}/TASK-DOC/x/0", "no such document"),
            (f"/api/deliverables/other-project/TASK-DOC/0/1", "belongs to another project"),
            (f"/api/deliverables/{ref}/TASK-DOC", "not found"),
        ):
            with self.assertRaises(HTTPError) as failure:
                urlopen(base + path, timeout=5)
            self.assertEqual(failure.exception.code, 404, path)
            self.assertIn(expected, json.loads(failure.exception.read())["error"], path)
        b.write_text("tampered\n")
        with self.assertRaises(HTTPError) as failure:
            urlopen(base + link["url"], timeout=5)
        self.assertIn("changed since it was attached", json.loads(failure.exception.read())["error"])
        with self.assertRaises(HTTPError) as failure:
            urlopen(Request(base + link["url"], data=b"{}", headers={"Content-Type": "application/json"}, method="POST"), timeout=5)
        self.assertNotEqual(failure.exception.code, 200)
        with self.assertRaises(HTTPError) as failure:
            urlopen(Request(base + link["url"], headers={"Host": "evil.example"}), timeout=5)
        self.assertEqual(failure.exception.code, 403, "the same request guard as every other route")

    def test_the_manager_proxy_forwards_the_nosniff_header(self):
        from harness import project_manager

        class Worker(BaseHTTPRequestHandler):
            def log_message(self, *_): return
            def do_GET(self):
                body = REPORT_TEXT.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers(); self.wfile.write(body)

        worker = ThreadingHTTPServer(("127.0.0.1", 0), Worker)
        threading.Thread(target=worker.serve_forever, daemon=True).start()
        self.addCleanup(worker.server_close); self.addCleanup(worker.shutdown)
        home = Path(self.temporary.name) / "manager"
        manager = project_manager.ProjectManager(home, board_port=worker.server_address[1])

        class Running:
            pid = 1
            def poll(self): return None
        manager.worker = Running()
        server = ThreadingHTTPServer(("127.0.0.1", 0), project_manager.make_handler(manager))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close); self.addCleanup(server.shutdown)
        manager.manager_url = f"http://127.0.0.1:{server.server_address[1]}/"
        response = urlopen(manager.manager_url.rstrip("/") + project_manager.PROJECT_ROUTE + "api/deliverables/x/T/0/0", timeout=5)
        self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(response.read(), REPORT_TEXT.encode())


class OwnerCardGuardTests(_Project):
    def test_the_studio_card_is_refused_while_nothing_leads_to_the_report_and_accepted_with_the_link_once_it_does(self):
        self.attach("1-evidence-a.txt", REPORT_TEXT)   # attached as proof only, like Studio
        with self.assertRaises(ValueError) as refused:
            self.pin()
        text = str(refused.exception)
        self.assertTrue(text.startswith("give the owner a link they can open"), text)
        self.assertIn("deliverable-file", text); self.assertIn("contract-evidence --for-owner", text)
        self.assertEqual(list(board.snapshot(self.root).get("owner_actions", {}).values()), [], "nothing recorded")
        board.mark_deliverable_file(self.root, self.cto["id"], "TASK-DOC", REPORT, "1-evidence-a.txt")
        card = self.pin()
        self.assertEqual([(l["label"], l["title"]) for l in card["links"]],
                         [("Open report", "JLS Brand Strategy quality diagnostic — content-focused update")])
        self.assertEqual(card["links"][0]["reference"], "generic")

    def test_the_owner_action_status_note_takes_the_same_guard(self):
        with self.assertRaises(ValueError) as refused:
            board.status(self.root, self.cto["id"], "OWNER ACTION: read the TASK-DOC report, then Accept/Reject", "waiting")
        self.assertIn("give the owner a link they can open", str(refused.exception))
        self.assertFalse(board.snapshot(self.root).get("owner_actions"))
        self.attach("1-evidence-a.txt", REPORT_TEXT, for_owner=True)
        board.status(self.root, self.cto["id"], "OWNER ACTION: read the TASK-DOC report, then Accept/Reject", "waiting")
        card = next(iter(board.snapshot(self.root)["owner_actions"].values()))
        self.assertEqual(card["links"][0]["label"], "Open report")

    def test_cards_that_do_not_ask_the_owner_to_read_a_board_document_are_untouched(self):
        for title, kw in (
            ("Run the release script in Terminal", {"command": "bash /x/scripts/release.sh", "why": "Output lands in results.txt"}),
            ("Open Settings and upgrade your Google plan", {"why": "The quota is out."}),
            ("Review audit settings in Mission Control", {"why": ""}),
            ("Read the report at https://example.com/report", {"why": "external", "location_verified": "the owner's own published site"}),
            ("Open the Delivery terminal and run /login", {"why": "Delivery is signed out."}),
            ("Run the GPU check on your Mac", {"why": "Only your machine has the GPU; the output lands in gpu.log afterwards."}),
        ):
            card = board.record_owner_action(self.root, self.cto["id"], title, **kw)
            self.assertNotIn("links", card, title)

    def test_explicit_names_must_resolve_themselves_and_ambiguity_refuses(self):
        self.attach("1-evidence-a.txt", REPORT_TEXT, for_owner=True)
        with self.assertRaises(ValueError) as refused:
            self.pin("Read /missing/final-report.md before you decide", why="")
        self.assertIn("/missing/final-report.md", str(refused.exception))
        with self.assertRaises(ValueError) as refused:
            self.pin("Read the missing report and sign in at https://example.com/login", why="", task="TASK-NONE")
        self.assertIn("give the owner a link", str(refused.exception))
        card = self.pin("Read 1-evidence-a.txt, then Accept", why="")
        self.assertEqual(card["links"][0]["reference"], "name")
        with self.assertRaises(ValueError) as refused:
            self.pin("Read strategy.md, then Accept", why="")
        self.assertIn("strategy.md", str(refused.exception)); self.assertIn("the marked documents are", str(refused.exception))
        other = delivery_with_direction(self.root, "TASK-TWO")
        board.create_contract(self.root, other["id"], "Second task", ["A second report"])
        twin = self.uploads / "other" / "1-evidence-a.txt"; twin.parent.mkdir(); twin.write_text("# Second\n", encoding="utf-8")
        board.attach_contract_evidence(self.root, other["id"], "A second report", str(twin), True)   # same file name on another task
        with self.assertRaises(ValueError) as refused:
            self.pin("Read 1-evidence-a.txt, then Accept", why="", task="")
        self.assertIn("more than one", str(refused.exception))
        with self.assertRaises(ValueError) as refused:
            self.pin("Read the report, then Accept", why="", task="")
        self.assertIn("more than one task has a document", str(refused.exception))

    def test_a_repost_revalidates_and_a_reworded_repost_replaces_the_card_in_place(self):
        a = self.attach("1-evidence-a.txt", REPORT_TEXT, for_owner=True)
        card = self.pin()
        self.assertIs(self.pin()["id"] == card["id"], True)
        reworded = self.pin("Read the diagnostic report (final), then Accept", why="It names the misses.")
        self.assertEqual(reworded["id"], card["id"], "the same request about the same task is one card")
        self.assertEqual(reworded["title"], "Read the diagnostic report (final), then Accept")
        event = next(e for e in reversed(board.snapshot(self.root)["events"]) if e["kind"] == "owner_action_refreshed")
        self.assertEqual(event["previous"]["title"], STUDIO_TITLE)
        a.write_text("tampered\n")
        with self.assertRaises(ValueError) as refused:
            self.pin("Read the diagnostic report (final), then Accept", why="It names the misses.")
        self.assertIn("changed since", str(refused.exception))
        self.assertEqual(len([c for c in board.snapshot(self.root)["owner_actions"].values() if c["status"] == "open"]), 1)

    def test_a_card_recorded_before_this_change_gets_its_link_projected_read_only_with_the_view_app_note(self):
        self.attach("1-evidence-a.txt", REPORT_TEXT, for_owner=True)
        with board.locked_state(self.root) as state:
            state.setdefault("owner_actions", {})["action-a238f910ab1b"] = {
                "id": "action-a238f910ab1b", "title": STUDIO_TITLE, "command": "", "why": STUDIO_WHY,
                "task": "TASK-DOC", "for_agent": "", "kind": "decision", "agent_id": self.cto["id"],
                "status": "open", "recorded_at": board.now(), "outcome": "", "done_at": "",
            }
        before = (self.root / ".harness" / "board" / "state.json").read_bytes()
        payload = board_viewer.dashboard_payload(self.root)
        card = payload["state"]["owner_actions"]["action-a238f910ab1b"]
        self.assertEqual(card["links"][0]["label"], "Open report")
        self.assertTrue(card["links"][0]["projected"])
        self.assertIn("View app starts the app and does not show a document", card["links_note"])
        self.assertEqual((self.root / ".harness" / "board" / "state.json").read_bytes(), before, "nothing written on GET")
        self.assertEqual(len(payload["deliverable_links"]["TASK-DOC"]), 1)

    def test_a_card_about_a_deliverable_with_two_documents_links_both_and_each_follows_its_own_versions(self):
        """Review 2026-10-15 round 1, blocking 1."""
        report = self.attach("strategy.txt", "# Strategy report\nResult for owner\n", True, document="Report")
        plan = self.attach("plan.txt", "# Implementation plan\nNext steps\n", True, document="Plan")
        card = self.pin("Read the report and review the plan, then Accept or Reject", why="Both are delivered documents.")
        self.assertEqual({(l["document"], l["path"]) for l in card["links"]}, {("Report", str(report)), ("Plan", str(plan))})
        self.assertEqual(sorted(l["label"] for l in card["links"]), ["Open report: Plan", "Open report: Report"])
        shown = board_viewer.dashboard_payload(self.root)["state"]["owner_actions"][card["id"]]["links"]
        self.assertEqual({l["path"] for l in shown}, {str(report), str(plan)}, "two distinct documents, never duplicate links to one")
        self.assertEqual(len({l["url"] for l in shown}), 2)
        plan2 = self.attach("plan-v2.txt", "# Implementation plan v2\n", True, document="Plan")
        shown = board_viewer.dashboard_payload(self.root)["state"]["owner_actions"][card["id"]]["links"]
        by_document = {l["document"]: l for l in shown}
        self.assertEqual(by_document["Plan"]["path"], str(plan2), "the plan link follows the plan's new version")
        self.assertIn("updated to the latest version", by_document["Plan"]["label"])
        self.assertEqual(by_document["Report"]["path"], str(report), "the report link stays on the report")

    def test_every_file_named_after_one_read_verb_must_resolve_across_commas_and_and(self):
        """Review 2026-10-15 round 1, blocking 2."""
        a = self.attach("strategy.txt", "# Strategy report\n", True, document="Report")
        with self.assertRaises(ValueError) as refused:
            self.pin("Read strategy.txt and missing.txt, then Accept or Reject", why="Read both before accepting.")
        self.assertIn("missing.txt", str(refused.exception))
        self.assertFalse(board.snapshot(self.root).get("owner_actions"), "the whole card is refused, nothing stored")
        b = self.attach("plan.txt", "# Plan\n", True, document="Plan")
        c = self.attach("notes.txt", "# Notes\n", True, document="Notes")
        card = self.pin("Read strategy.txt, plan.txt and notes.txt, then Accept", why="")
        self.assertEqual({l["path"] for l in card["links"]}, {str(a), str(b), str(c)})
        with self.assertRaises(ValueError):
            self.pin("Open plan.txt; also read gone.txt", why="")
        refs = board._document_references("Run the check and read results.txt afterwards", "output lands in results.txt")
        self.assertEqual([r["text"] for r in refs], ["results.txt"], "a file after a read verb is a reference; the bare mention is not")

    def test_a_generic_link_on_a_stored_card_follows_the_latest_document_and_says_so(self):
        self.attach("1-evidence-a.txt", EARLIER_TEXT, for_owner=True)
        card = self.pin()
        self.attach("2-evidence-b.txt", REPORT_TEXT, for_owner=True)
        shown = board_viewer.dashboard_payload(self.root)["state"]["owner_actions"][card["id"]]["links"][0]
        self.assertEqual(shown["label"], "Open report (updated to the latest version)")
        self.assertEqual(shown["title"], "JLS Brand Strategy quality diagnostic — content-focused update")


class ReleaseAndHistoryTests(_Project):
    def test_release_readiness_requires_the_current_document_to_be_readable_and_reviewed(self):
        self.assertIn("owner_artifacts_readable", board.RELEASE_REQUIRED_CHECKS)
        self.assertIn("owner_artifacts_readable", board.BROKER_RELEASE_REQUIRED_CHECKS)
        ok, problems, manifest = cto_module.owner_artifacts_readable(self.root, board.snapshot(self.root), "TASK-DOC", None)
        self.assertTrue(ok and not problems and manifest == [], "nothing marked passes as today")
        a = self.attach("1-evidence-a.txt", REPORT_TEXT, for_owner=True)
        ok, problems, manifest = cto_module.owner_artifacts_readable(self.root, board.snapshot(self.root), "TASK-DOC", None)
        self.assertTrue(ok); self.assertEqual(manifest[0]["path"], str(a))
        a.write_text("tampered\n")
        ok, problems, _ = cto_module.owner_artifacts_readable(self.root, board.snapshot(self.root), "TASK-DOC", None)
        self.assertFalse(ok); self.assertIn("1-evidence-a.txt", problems[0]); self.assertIn("deliverable-file", problems[0])
        with self.assertRaises(ValueError) as refused:
            board.record_release_ready(self.root, self.cto["id"], "TASK-DOC", {
                **{key: True for key in board.RELEASE_REQUIRED_CHECKS}, "owner_artifacts_readable": False, "owner_artifact_problems": problems,
            })
        self.assertIn("owner_artifacts_readable", str(refused.exception)); self.assertIn("1-evidence-a.txt", str(refused.exception))
        a.write_text(REPORT_TEXT)
        reviewed = {"status": "passed", "requested_at": board.now(), "owner_documents": [{"deliverable": REPORT, "sha256": "f" * 64}]}
        ok, problems, _ = cto_module.owner_artifacts_readable(self.root, board.snapshot(self.root), "TASK-DOC", reviewed)
        self.assertTrue(ok, "a file attached before the review was requested is what the reviewer could see")
        later = {"status": "passed", "requested_at": "2000-01-01T00:00:00+00:00", "owner_documents": []}
        ok, problems, _ = cto_module.owner_artifacts_readable(self.root, board.snapshot(self.root), "TASK-DOC", later)
        self.assertFalse(ok); self.assertIn("after the final review was requested", problems[0])
        legacy = {"status": "passed", "requested_at": "2000-01-01T00:00:00+00:00"}
        self.assertTrue(cto_module.owner_artifacts_readable(self.root, board.snapshot(self.root), "TASK-DOC", legacy)[0])

    def test_the_final_review_request_and_its_brief_carry_the_document_manifest(self):
        self.attach("1-evidence-a.txt", REPORT_TEXT, for_owner=True)
        self.assertEqual(board._owner_documents_now(self.root, "TASK-DOC")[0]["deliverable"], REPORT)
        with board.locked_state(self.root) as state:
            state["requirement_confirmations"]["TASK-DOC"] = {"text": "Final agreed requirements: the report.", "confirmed_at": board.now(), "version": 1}
        request = {"id": "review-1", "task": "TASK-DOC", "test_scope": "full", "phase": "final_acceptance",
                   "owner_documents": board._owner_documents_now(self.root, "TASK-DOC")}
        brief = review_brief.build(self.root, board.snapshot(self.root), request)
        self.assertEqual(brief["risk_and_scope"]["owner_documents"][0]["deliverable"], REPORT)

    def test_history_rows_carry_the_links_and_the_version_changes_when_the_document_changes(self):
        a = self.attach("1-evidence-a.txt", EARLIER_TEXT, for_owner=True)
        b = self.attach("2-evidence-b.txt", REPORT_TEXT)
        with board.locked_state(self.root) as state:
            state["releases"]["TASK-DOC"] = {"task": "TASK-DOC", "status": "RELEASE_ACCEPTED", "cto_id": self.cto["id"], "recorded_at": board.now(), "head_commit": "c0ffee"}
            state["release_decisions"]["TASK-DOC"] = {"task": "TASK-DOC", "decision": "accepted", "recorded_at": board.now()}
        first = board_viewer.dashboard_payload(self.root)
        self.assertNotIn("TASK-DOC", first["deliverable_links"], "accepted work is history, not a live task")
        rows = board_viewer.history_payload(self.root)["task_history"]
        row = next(r for r in rows if r["task"] == "TASK-DOC")
        self.assertEqual(row["deliverable_links"][0]["title"], "JLS Brand Strategy quality diagnostic — evidence update")
        board.mark_deliverable_file(self.root, self.cto["id"], "TASK-DOC", REPORT, b.name)
        second = board_viewer.dashboard_payload(self.root)
        self.assertNotEqual(first["history_version"], second["history_version"], "an open History panel reloads after a correction")
        a.write_text("changed\n")
        self.assertNotEqual(second["history_version"], board_viewer.dashboard_payload(self.root)["history_version"])


class DirectiveTests(unittest.TestCase):
    def test_the_cto_directive_says_open_the_path_yourself_before_pinning_and_stays_under_the_gemini_budget(self):
        text = (ROOT / "directives/CTO.md").read_text(encoding="utf-8")
        section = text[text.index("## What must never block the work"):text.index("### Findings outside the current objective")]
        self.assertIn("Open the path yourself before pinning or repeating an owner card", section)
        cto = flat("directives/CTO.md")
        self.assertIn("A card the owner cannot act on is a defect", cto)
        self.assertIn("a delivered document reaches the owner only through the Open report link Mission Control shows", cto)
        self.assertIn("attached with `--for-owner` or marked with `deliverable-file`", cto)
        self.assertIn('never "read the report" or "View app" for a task with no screen', cto)
        self.assertIn("for any other card verify the control or command it names exists", cto)
        self.assertIn('the first "awaiting owner" status names exactly where the thing is', cto)
        size = len(text.encode("utf-8")) + len(claude_inbox.AUTHORITY_NOTE.encode("utf-8")) + 2
        self.assertLessEqual(size, gemini_runtime.RULES_LIMIT_BYTES, f"CTO.md payload {size} bytes exceeds the Gemini rules budget")

    def test_the_mirror_and_delivery_directives_carry_the_rule(self):
        mirror = flat("harness/directives/CTO_COMPLETION_DIRECTIVE.md")
        self.assertIn("Before pinning or repeating an owner card, open the path yourself", mirror)
        self.assertIn("a card the owner cannot act on is a defect", mirror)
        self.assertIn('the first "awaiting owner" status names exactly where the thing is', mirror)
        agent = flat("directives/AGENT.md")
        self.assertIn("attach that document with `--for-owner`", agent)
        self.assertIn('attaches each under its own `--document "<name>"`', agent)
        self.assertIn("one current document per name", agent)
        self.assertIn("Read what was delivered", board_viewer.rendered_page())


if __name__ == "__main__":
    unittest.main()
