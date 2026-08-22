# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
"""The requirements contract: declared proposal, recorded owner decision, gate.

The owner's go-ahead is a contract signature. It must be a structured board
event - terminal prose is not authorization - and the confirmation gate must
make that impossible to bypass.
"""
from __future__ import annotations

import json
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from harness import board, board_viewer, control
from tests.environment_support import require_loopback


def context(base: Path) -> Path:
    """A plain board root, exactly as the suite's own BoardTests use it.

    No ProjectContext and therefore no git broker and no sandbox-exec: the
    protocol under test is the requirements handshake, and this fixture must
    reproduce from a fresh clone in ANY environment - review finding r1-r3.
    """
    root = base / "root"
    root.mkdir(exist_ok=True)
    control.initialize(root)
    board.snapshot(root)
    return root


def delivery_with_direction(root, task: str = "TASK-CONTRACT"):
    session = control.create(root, "codex_delivery")
    agent = board.register(root, "development", board.AWAITING_OWNER_DIRECTION,
                           vendor="OpenAI", session_id=session["id"])
    board.record_owner_direction(root, session["id"], f"OWNER DIRECTION — {task}: build the thing.")
    board.begin_task(root, agent["id"], task)
    agent["session_id"] = session["id"]
    return agent


class RequirementsProtocolTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = context(Path(self._tmp.name))

    def test_terminal_prose_alone_can_no_longer_confirm(self):
        agent = delivery_with_direction(self.root)
        with self.assertRaises(ValueError) as raised:
            board.record_requirement_confirmation(self.root, agent["id"], "Final agreed requirements: build the thing.")
        self.assertIn("terminal prose is not authorization", str(raised.exception))

    def test_full_handshake_propose_go_ahead_confirm(self):
        agent = delivery_with_direction(self.root)
        text = "Final agreed requirements: build the thing end to end."
        event = board.record_requirement_proposal(self.root, agent["id"], text)
        self.assertEqual(event["task"], "TASK-CONTRACT")
        board.record_requirements_decision(self.root, "TASK-CONTRACT", "go_ahead")
        # the go-ahead reaches the delivery terminal
        state = board.snapshot(self.root)
        session_id = state["agents"][agent["id"]]["session_id"] if isinstance(state.get("agents"), dict) else agent.get("session_id")
        confirmation = board.record_requirement_confirmation(self.root, agent["id"], text)
        proposal = board.snapshot(self.root)["requirement_proposals"]["TASK-CONTRACT"]
        self.assertEqual(proposal["status"], "accepted")
        self.assertTrue(proposal["decided_at"])

    def test_confirmation_must_match_the_accepted_text_verbatim(self):
        agent = delivery_with_direction(self.root)
        board.record_requirement_proposal(self.root, agent["id"], "Final agreed requirements: exactly this.")
        board.record_requirements_decision(self.root, "TASK-CONTRACT", "go_ahead")
        with self.assertRaises(ValueError) as raised:
            board.record_requirement_confirmation(self.root, agent["id"], "Final agreed requirements: something else.")
        self.assertIn("verbatim", str(raised.exception))

    def test_modify_records_the_change_request_and_allows_a_new_proposal(self):
        agent = delivery_with_direction(self.root)
        board.record_requirement_proposal(self.root, agent["id"], "Final agreed requirements: v1.")
        board.record_requirements_decision(self.root, "TASK-CONTRACT", "modify", "Drop the export feature.")
        proposal = board.snapshot(self.root)["requirement_proposals"]["TASK-CONTRACT"]
        self.assertEqual(proposal["status"], "modify_requested")
        self.assertEqual(proposal["owner_change_request"], "Drop the export feature.")
        board.record_requirement_proposal(self.root, agent["id"], "Final agreed requirements: v2, no export.")
        revised = board.snapshot(self.root)["requirement_proposals"]["TASK-CONTRACT"]
        self.assertEqual(revised["version"], 2)
        self.assertEqual(revised["status"], "awaiting_owner")

    def test_double_proposal_and_wrong_decisions_are_refused(self):
        agent = delivery_with_direction(self.root)
        board.record_requirement_proposal(self.root, agent["id"], "Final agreed requirements: v1.")
        with self.assertRaises(ValueError):
            board.record_requirement_proposal(self.root, agent["id"], "Final agreed requirements: v1 again.")
        with self.assertRaises(ValueError):
            board.record_requirements_decision(self.root, "TASK-CONTRACT", "modify", "")  # modify needs text
        with self.assertRaises(ValueError):
            board.record_requirements_decision(self.root, "NO-SUCH-TASK", "go_ahead")
        board.record_requirements_decision(self.root, "TASK-CONTRACT", "go_ahead")
        with self.assertRaises(ValueError):
            board.record_requirements_decision(self.root, "TASK-CONTRACT", "go_ahead")  # already decided

    def test_decision_routes_an_owner_decision_message_to_the_terminal(self):
        agent = delivery_with_direction(self.root)
        board.record_requirement_proposal(self.root, agent["id"], "Final agreed requirements: v1.")
        state = board.snapshot(self.root)
        agents = state.get("agents") or {}
        record = agents.get(agent["id"]) if isinstance(agents, dict) else agent
        session_id = record["session_id"]
        control.take_instructions(self.root, session_id)  # clear anything prior
        board.record_requirements_decision(self.root, "TASK-CONTRACT", "go_ahead")
        queued = control.take_instructions(self.root, session_id)
        self.assertEqual(len(queued), 1)
        self.assertIn("[OWNER DECISION] GO AHEAD", queued[0]["text"])
        self.assertIn("confirm-requirements", queued[0]["text"])


class RequirementsEndpointTests(unittest.TestCase):
    def setUp(self):
        require_loopback()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = context(Path(self._tmp.name))
        server = ThreadingHTTPServer(("127.0.0.1", 0), board_viewer.make_handler(self.root))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.url = f"http://127.0.0.1:{server.server_address[1]}"

    def post(self, path, body):
        request = Request(self.url + path, data=json.dumps(body).encode(),
                          headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read())
        except HTTPError as error:
            return error.code, json.loads(error.read() or b"{}")

    def test_endpoint_records_go_ahead_and_refuses_nonsense(self):
        agent = delivery_with_direction(self.root)
        board.record_requirement_proposal(self.root, agent["id"], "Final agreed requirements: endpoint test.")
        status, payload = self.post("/api/tasks/TASK-CONTRACT/requirements-decision", {"decision": "go_ahead"})
        self.assertEqual(status, 201, payload)
        self.assertEqual(payload["decision"]["task"], "TASK-CONTRACT")
        status, payload = self.post("/api/tasks/TASK-CONTRACT/requirements-decision", {"decision": "go_ahead"})
        self.assertEqual(status, 400)
        status, payload = self.post("/api/tasks/TASK-CONTRACT/requirements-decision", {"decision": "sideways"})
        self.assertEqual(status, 400)

    def test_modify_via_endpoint_requires_text(self):
        agent = delivery_with_direction(self.root)
        board.record_requirement_proposal(self.root, agent["id"], "Final agreed requirements: endpoint modify.")
        status, _ = self.post("/api/tasks/TASK-CONTRACT/requirements-decision", {"decision": "modify"})
        self.assertEqual(status, 400)
        status, _ = self.post("/api/tasks/TASK-CONTRACT/requirements-decision", {"decision": "modify", "text": "Change X."})
        self.assertEqual(status, 201)


if __name__ == "__main__":
    unittest.main()
