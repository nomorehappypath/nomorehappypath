# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
import json
import subprocess
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from harness import board, board_viewer
from tests.environment_support import require_loopback


class ReleaseFeedbackRejectionTests(unittest.TestCase):
    def setUp(self):
        require_loopback()
        super().setUp()

    def released(self, root: Path, task: str):
        cto = board.register(root, "cto", "GLOBAL_MONITOR", vendor="Anthropic")
        checks = {key: True for key in board.RELEASE_REQUIRED_CHECKS}
        return board.record_release_ready(root, cto["id"], task, checks | {"head_commit": "release-commit"})

    def multipart(self, reason: str, filename: str = "../proof.png", content_type: str = "image/png", data: bytes = b"PNG-BYTES"):
        boundary = "owner-feedback-boundary"
        parts = [
            f'--{boundary}\r\nContent-Disposition: form-data; name="decision"\r\n\r\nnot_accepted\r\n'.encode(),
            f'--{boundary}\r\nContent-Disposition: form-data; name="reason"\r\n\r\n{reason}\r\n'.encode(),
            (
                f'--{boundary}\r\nContent-Disposition: form-data; name="attachments"; filename="{filename}"\r\n'
                f"Content-Type: {content_type}\r\n\r\n"
            ).encode() + data + b"\r\n",
            f"--{boundary}--\r\n".encode(),
        ]
        return b"".join(parts), f"multipart/form-data; boundary={boundary}"

    def post(self, root: Path, task: str, body: bytes, content_type: str):
        server = board_viewer.ThreadingHTTPServer(("127.0.0.1", 0), board_viewer.make_handler(root))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            request = Request(
                f"http://127.0.0.1:{server.server_address[1]}/api/releases/{task}/decision",
                data=body,
                headers={"Content-Type": content_type},
                method="POST",
            )
            return urlopen(request, timeout=3).status, json.loads(urlopen(request, timeout=3).read())
        finally:
            server.shutdown()
            thread.join(timeout=3)
            server.server_close()

    def request(self, root: Path, task: str, body: bytes, content_type: str):
        server = board_viewer.ThreadingHTTPServer(("127.0.0.1", 0), board_viewer.make_handler(root))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            request = Request(
                f"http://127.0.0.1:{server.server_address[1]}/api/releases/{task}/decision",
                data=body,
                headers={"Content-Type": content_type},
                method="POST",
            )
            try:
                with urlopen(request, timeout=3) as response:
                    return response.status, json.loads(response.read())
            except HTTPError as error:
                return error.code, json.loads(error.read())
        finally:
            server.shutdown()
            thread.join(timeout=3)
            server.server_close()

    def node_view(self, state: dict, task: str):
        script = board_viewer.rendered_page().split("<script>", 1)[1].split("</script>", 1)[0].split("el('#status-dialog-close')", 1)[0]
        invocation = """
globalThis.document={querySelector(){return null;}};
const state=%s;
process.stdout.write(JSON.stringify({gate:taskGate(state,%s,{},null,[],0,0),panel:releaseResponseHtml(state,%s)}));
""" % (json.dumps(state), json.dumps(task), json.dumps(task))
        completed = subprocess.run(["node", "-e", script + "\n" + invocation], capture_output=True, text=True)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return json.loads(completed.stdout)

    def test_multipart_rejection_is_durable_and_routes_safe_attachment_to_repair(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = "TASK-REJECT"
            release = self.released(root, task)
            reason = "First paragraph explains the defect.\n\nSecond paragraph explains the expected correction."
            body, content_type = self.multipart(reason)
            status, response = self.request(root, task, body, content_type)
            state = board.snapshot(root)
            attachment = state["release_decisions"][task]["attachments"][0]
            stored = root / attachment["stored_path"]

            self.assertEqual(status, 201)
            self.assertEqual(response["decision"]["decision"], "not_accepted")
            self.assertEqual(state["release_decisions"][task]["reason"], reason)
            self.assertEqual(state["release_repairs"][task]["status"], "OWNER_REJECTED_REPAIR_REQUIRED")
            self.assertEqual(state["release_repairs"][task]["reason"], reason)
            self.assertEqual(state["releases"][task], release)
            self.assertEqual(attachment["display_name"], "proof.png")
            self.assertNotIn("..", attachment["stored_path"])
            self.assertEqual(stored.read_bytes(), b"PNG-BYTES")
            view = self.node_view(state, task)
            self.assertEqual(view["gate"]["status"], "OWNER REJECTED / REPAIR REQUIRED")
            self.assertIn("Delivery will use them", view["panel"])
            self.assertIn("First paragraph explains the defect", view["panel"])

    def test_acceptance_is_hidden_when_serving_runtime_does_not_match_release(self):
        page = board_viewer.rendered_page(
            project_name="Runtime project", project_id="runtime-project",
            chat_action_token="chat-token", runtime={"commit": "a" * 40},
        )
        script = page.split("<script>", 1)[1].split("</script>", 1)[0].split(
            "el('#status-dialog-close')", 1,
        )[0]
        state = {"releases": {"TASK": {
            "status": "VISUAL_TEST_REQUIRED", "head_commit": "b" * 40,
            # The deployment gate exists for releases whose product IS the
            # runtime serving this page; their checks prove it.
            "checks": {"deployed_runtime_verified": True, "deployed_chat_verified": True},
        }}}
        invocation = """
globalThis.document={querySelector(){return null;}};
const state=%s;
process.stdout.write(JSON.stringify({gate:taskGate(state,'TASK',{},null,[],0,0),panel:releaseResponseHtml(state,'TASK')}));
""" % json.dumps(state)
        completed = subprocess.run(
            ["node", "-e", script + "\n" + invocation], capture_output=True, text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        value = json.loads(completed.stdout)
        self.assertEqual(value["gate"]["status"], "DEPLOYMENT REFRESH REQUIRED")
        self.assertIn("Acceptance unavailable", value["panel"])
        self.assertNotIn("submitAccepted", value["panel"])

    def test_external_target_release_is_not_compared_to_control_plane_runtime(self):
        page = board_viewer.rendered_page(
            project_name="Runtime project", project_id="runtime-project",
            chat_action_token="chat-token", runtime={"commit": "a" * 40},
        )
        script = page.split("<script>", 1)[1].split("</script>", 1)[0].split(
            "el('#status-dialog-close')", 1,
        )[0]
        state = {"releases": {"TASK": {
            "status": "VISUAL_TEST_REQUIRED", "head_commit": "b" * 40,
            "runtime_verification_deferred_to_target_acceptance": True,
        }}}
        invocation = """
globalThis.document={querySelector(){return null;}};
const state=%s;
process.stdout.write(JSON.stringify({gate:taskGate(state,'TASK',{},null,[],0,0),panel:releaseResponseHtml(state,'TASK')}));
""" % json.dumps(state)
        completed = subprocess.run(
            ["node", "-e", script + "\n" + invocation], capture_output=True, text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        value = json.loads(completed.stdout)
        self.assertEqual(value["gate"]["status"], "READY FOR YOUR TEST")
        self.assertNotIn("Acceptance unavailable", value["panel"])
        self.assertIn("submitAccepted", value["panel"])

    def test_html_attachment_is_rejected_but_rejection_reason_remains_saved(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = "TASK-HTML"
            self.released(root, task)
            body, content_type = self.multipart("The page renders incorrectly.", "proof.html", "text/html", b"<script>alert(1)</script>")
            status, response = self.request(root, task, body, content_type)
            state = board.snapshot(root)
            self.assertEqual(status, 400)
            self.assertTrue(response["decision_recorded"])
            self.assertEqual(state["release_decisions"][task]["reason"], "The page renders incorrectly.")
            self.assertEqual(state["release_decisions"][task]["attachments"], [])
            self.assertIn("response was saved", response["message"])

    def test_storage_failure_is_visible_after_rejection_is_saved(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = "TASK-DISK"
            self.released(root, task)
            body, content_type = self.multipart("The result is not acceptable.")
            with patch("harness.board._write_attachment", side_effect=OSError("disk full")):
                status, response = self.request(root, task, body, content_type)
            state = board.snapshot(root)
            self.assertEqual(status, 500)
            self.assertTrue(response["decision_recorded"])
            self.assertEqual(state["release_decisions"][task]["decision"], "not_accepted")
            self.assertIn("could not be stored", response["message"])

    def test_accepted_and_rejected_owner_states_close_or_route_the_gate(self):
        accepted = self.node_view({"releases": {"ACCEPT": {"status": "VISUAL_TEST_REQUIRED"}}, "release_decisions": {"ACCEPT": {"decision": "accepted"}}}, "ACCEPT")
        rejected = self.node_view({"releases": {"REJECT": {"status": "VISUAL_TEST_REQUIRED"}}, "release_decisions": {"REJECT": {"decision": "not_accepted", "attachments": []}}}, "REJECT")
        self.assertEqual(accepted["gate"]["status"], "OWNER ACCEPTED")
        self.assertEqual(rejected["gate"]["status"], "OWNER REJECTED / REPAIR REQUIRED")

    def test_legacy_rejection_uses_repair_reason_or_honest_fallback(self):
        from_repair = self.node_view({
            "releases": {"REJECT": {"status": "VISUAL_TEST_REQUIRED"}},
            "release_decisions": {"REJECT": {"decision": "not_accepted", "attachments": []}},
            "release_repairs": {"REJECT": {"reason": "Persisted repair reason."}},
        }, "REJECT")
        unavailable = self.node_view({
            "releases": {"REJECT": {"status": "VISUAL_TEST_REQUIRED"}},
            "release_decisions": {"REJECT": {"decision": "not_accepted", "attachments": []}},
        }, "REJECT")
        self.assertIn("Persisted repair reason.", from_repair["panel"])
        self.assertIn("Reason unavailable for this older response.", unavailable["panel"])


if __name__ == "__main__":
    unittest.main()
