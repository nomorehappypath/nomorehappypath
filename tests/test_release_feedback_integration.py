# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
from __future__ import annotations

import json
import subprocess
import threading
import unittest
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from harness import board, board_viewer, control
from tests.environment_support import require_loopback


class ReleaseFeedbackIntegrationTests(unittest.TestCase):
    def setUp(self):
        require_loopback()
        super().setUp()

    def released(self, root: Path, task: str, head_commit: str):
        cto = board.register(root, "cto", "GLOBAL_MONITOR", vendor="Anthropic")
        checks = {key: True for key in board.RELEASE_REQUIRED_CHECKS}
        return board.record_release_ready(root, cto["id"], task, checks | {"head_commit": head_commit})

    def multipart(self, reason: str, files: list[tuple[str, str, bytes]]):
        boundary = "served-owner-feedback-boundary"
        parts = [
            f'--{boundary}\r\nContent-Disposition: form-data; name="decision"\r\n\r\nnot_accepted\r\n'.encode(),
            f'--{boundary}\r\nContent-Disposition: form-data; name="reason"\r\n\r\n{reason}\r\n'.encode(),
        ]
        for filename, content_type, data in files:
            parts.append(
                (
                    f'--{boundary}\r\nContent-Disposition: form-data; name="attachments"; filename="{filename}"\r\n'
                    f"Content-Type: {content_type}\r\n\r\n"
                ).encode()
                + data
                + b"\r\n"
            )
        parts.append(f"--{boundary}--\r\n".encode())
        return b"".join(parts), f"multipart/form-data; boundary={boundary}"

    @contextmanager
    def served(self, root: Path):
        server = board_viewer.ThreadingHTTPServer(("127.0.0.1", 0), board_viewer.make_handler(root))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_address[1]}"
        finally:
            server.shutdown()
            thread.join(timeout=3)
            server.server_close()

    def request(self, base: str, path: str, body: bytes | None = None, content_type: str = "application/json"):
        request = Request(
            base + path,
            data=body,
            headers={"Content-Type": content_type} if body is not None else {},
            method="POST" if body is not None else "GET",
        )
        try:
            with urlopen(request, timeout=3) as response:
                return response.status, json.loads(response.read()), dict(response.headers)
        except HTTPError as error:
            return error.code, json.loads(error.read()), dict(error.headers)

    def raw_get(self, base: str, path: str):
        with urlopen(base + path, timeout=3) as response:
            return response.status, response.read().decode("utf-8"), dict(response.headers)

    def json_post(self, base: str, task: str, value: dict):
        return self.request(
            base,
            f"/api/releases/{task}/decision",
            json.dumps(value).encode(),
        )

    def node_owner_view(self, page: str, state: dict, task: str):
        script = page.split("<script>", 1)[1].split("</script>", 1)[0].split("el('#status-dialog-close')", 1)[0]
        invocation = """
globalThis.document={querySelector(){return null;}};
const state=%s;
process.stdout.write(JSON.stringify({gate:taskGate(state,%s,{},null,[],0,0),panel:releaseResponseHtml(state,%s)}));
""" % (json.dumps(state), json.dumps(task), json.dumps(task))
        completed = subprocess.run(["node", "-e", script + "\n" + invocation], capture_output=True, text=True)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return json.loads(completed.stdout)

    def test_served_viewer_acceptance_closes_gate_and_preserves_certification(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = "TASK-SERVED-ACCEPT"
            release = self.released(root, task, "accepted-commit")
            with self.served(root) as base:
                status, page, headers = self.raw_get(base, "/")
                self.assertEqual(status, 200)
                self.assertEqual(headers["Cache-Control"], "no-store")
                self.assertIn("Accepted", page)

                status, response, _ = self.json_post(base, task, {"decision": "accepted"})
                self.assertEqual(status, 201)
                self.assertEqual(response["decision"]["decision"], "accepted")
                status, dashboard, headers = self.request(base, "/api/dashboard")
                self.assertEqual(status, 200)
                self.assertEqual(headers["Cache-Control"], "no-store")
                state = dashboard["state"]
                self.assertNotIn(task, state["release_decisions"])
                persisted = board.snapshot(root)
                self.assertEqual(persisted["release_decisions"][task]["decision"], "accepted")
                self.assertEqual(persisted["releases"][task], release)
                self.assertNotIn(task, state["release_repairs"])
                status, history, _ = self.request(base, "/api/history")
                self.assertEqual(status, 200)
                item = next(value for value in history["task_history"] if value["task"] == task)
                self.assertEqual(item["result"], "OWNER ACCEPTED")

    def test_served_viewer_rejection_routes_reason_and_files_across_restart(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = "TASK-SERVED-REJECT"
            release = self.released(root, task, "rejected-commit")
            reason = "The first section is not usable.\n\nPlease correct the layout in the second section."
            body, content_type = self.multipart(
                reason,
                [
                    ("../screenshots/visual.png", "image/png", b"PNG-SCREENSHOT"),
                    ("notes.docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document", b"DOCX-NOTES"),
                ],
            )
            with self.served(root) as base:
                _, page, _ = self.raw_get(base, "/")
                status, response, _ = self.request(base, f"/api/releases/{task}/decision", body, content_type)
                self.assertEqual(status, 201)
                self.assertEqual(response["decision"]["decision"], "not_accepted")
                status, dashboard, _ = self.request(base, "/api/dashboard")
                self.assertEqual(status, 200)
                state = dashboard["state"]
                decision = state["release_decisions"][task]
                repair = state["release_repairs"][task]
                self.assertEqual(decision["reason"], reason)
                self.assertEqual(len(decision["attachments"]), 2)
                self.assertEqual(repair["status"], "OWNER_REJECTED_REPAIR_REQUIRED")
                self.assertEqual(repair["reason"], reason)
                self.assertEqual(repair["attachments"], decision["attachments"])
                self.assertEqual(state["releases"][task], release)
                for attachment in decision["attachments"]:
                    stored = root / attachment["stored_path"]
                    self.assertTrue(stored.is_file())
                    self.assertTrue(stored.resolve().is_relative_to((root / ".harness" / "board" / "owner-feedback").resolve()))
                    self.assertNotIn("..", Path(attachment["stored_path"]).parts)
                view = self.node_owner_view(page, state, task)
                self.assertEqual(view["gate"]["status"], "OWNER REJECTED / REPAIR REQUIRED")
                self.assertIn("2 attachments", view["panel"])
                self.assertIn("new repair, review, and release cycle", view["panel"])

            with self.served(root) as restarted_base:
                _, restarted_page, _ = self.raw_get(restarted_base, "/")
                status, restarted, _ = self.request(restarted_base, "/api/dashboard")
                self.assertEqual(status, 200)
                restarted_state = restarted["state"]
                self.assertEqual(restarted_state["release_decisions"][task], decision)
                self.assertEqual(restarted_state["release_repairs"][task], repair)
                self.assertEqual(self.node_owner_view(restarted_page, restarted_state, task)["gate"]["status"], "OWNER REJECTED / REPAIR REQUIRED")

    def test_served_endpoint_keeps_decisions_isolated_and_release_history_immutable(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            accepted = "TASK-ISOLATED-ACCEPT"
            rejected = "TASK-ISOLATED-REJECT"
            accepted_release = self.released(root, accepted, "commit-a")
            rejected_release = self.released(root, rejected, "commit-b")
            body, content_type = self.multipart("Only this task needs repair.", [("proof.txt", "text/plain", b"proof")])
            with self.served(root) as base:
                self.assertEqual(self.json_post(base, accepted, {"decision": "accepted"})[0], 201)
                self.assertEqual(self.request(base, f"/api/releases/{rejected}/decision", body, content_type)[0], 201)
                status, dashboard, _ = self.request(base, "/api/dashboard")
                self.assertEqual(status, 200)
                state = dashboard["state"]
                self.assertNotIn(accepted, state["release_decisions"])
                self.assertEqual(state["release_decisions"][rejected]["reason"], "Only this task needs repair.")
                persisted = board.snapshot(root)
                self.assertEqual(set(persisted["release_decisions"]), {accepted, rejected})
                self.assertEqual(persisted["release_decisions"][accepted]["decision"], "accepted")
                self.assertNotIn(rejected, persisted["release_decisions"][accepted]["reason"])
                self.assertNotIn(accepted, state["release_repairs"])
                self.assertEqual(persisted["releases"][accepted], accepted_release)
                self.assertEqual(state["releases"][rejected], rejected_release)

    def test_served_endpoint_concurrency_allows_one_decision_and_reports_storage_failure(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = "TASK-SERVED-CONCURRENT"
            self.released(root, task, "concurrent-commit")
            with self.served(root) as base:
                def submit(_):
                    return self.json_post(base, task, {"decision": "accepted"})[0]

                with ThreadPoolExecutor(max_workers=12) as pool:
                    outcomes = list(pool.map(submit, range(12)))
                self.assertEqual(outcomes.count(201), 1)
                self.assertEqual(outcomes.count(400), 11)

            failure_task = "TASK-SERVED-DISK"
            self.released(root, failure_task, "disk-commit")
            body, content_type = self.multipart("Please fix the broken result.", [("screen.png", "image/png", b"screen")])
            with patch("harness.board._write_attachment", side_effect=OSError("disk full")):
                with self.served(root) as base:
                    status, response, _ = self.request(base, f"/api/releases/{failure_task}/decision", body, content_type)
            self.assertEqual(status, 500)
            self.assertTrue(response["decision_recorded"])
            self.assertIn("could not be stored", response["message"])
            self.assertNotIn("disk full", response["message"])
            self.assertEqual(board.snapshot(root)["release_decisions"][failure_task]["decision"], "not_accepted")

    def test_new_delivery_agent_discovers_and_claims_saved_repair_without_owner_repeat(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = "TASK-DELIVERY-ROUTE"
            release = self.released(root, task, "repair-source-commit")
            reason = "The workflow cannot be completed from the release card."
            body, content_type = self.multipart(reason, [("owner-shot.png", "image/png", b"OWNER-SHOT")])
            with self.served(root) as base:
                self.assertEqual(self.request(base, f"/api/releases/{task}/decision", body, content_type)[0], 201)

            source_session = control.create(root, "codex_delivery")
            source = board.register(root, "engineering", board.AWAITING_OWNER_DIRECTION, vendor="OpenAI", session_id=source_session["id"])
            board.record_owner_direction(root, source_session["id"], "Deliver the release task and preserve its owner feedback.")
            board.begin_task(root, source["id"], task)
            board.offline(root, source["id"], "source Delivery session ended after the owner response was saved", transport_ended=True)
            replacement_session = control.create(root, "codex_delivery")
            replacement = board.register(root, "engineering", board.AWAITING_OWNER_DIRECTION, vendor="OpenAI", session_id=replacement_session["id"])
            # Registration is the material controller event that resumes the
            # saved repair; dashboard reads remain side-effect free.
            with self.served(root) as base:
                status, dashboard, _ = self.request(base, "/api/dashboard")
                self.assertEqual(status, 200)
                self.assertIn(task, dashboard["state"]["release_repairs"])
            discovered = board.release_repairs_for_delivery(root, task)
            self.assertEqual(len(discovered), 1)
            self.assertEqual(discovered[0]["reason"], reason)
            self.assertEqual(discovered[0]["attachments"][0]["display_name"], "owner-shot.png")
            claimed = board.snapshot(root)["release_repairs"][task]
            self.assertEqual(claimed["status"], "DELIVERY_REPAIR_IN_PROGRESS")
            self.assertEqual(claimed["reason"], reason)
            self.assertEqual(claimed["attachments"], discovered[0]["attachments"])
            self.assertEqual(claimed["repair_cycle"]["status"], "repairing")
            self.assertIn("new review and release cycle", claimed["next_action"])
            self.assertEqual(board.snapshot(root)["agents"][replacement["id"]]["task"], task)
            self.assertEqual(board.snapshot(root)["releases"][task], release)
            self.assertTrue(any(event["kind"] == "owner_release_repair_claimed" for event in board.snapshot(root)["events"]))


if __name__ == "__main__":
    unittest.main()
