# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
"""Session-connected developer agents stay visible on the board.

Folded forward from Codex's stranded live-session draft (it was sitting
uncommitted in the running tree — the very self-development hazard Fix #1
guards). The compact dashboard state normally shows only `active` agents. This
adds a narrow carve-out: a DEVELOPER-role agent that has gone idle (active=False)
but whose terminal session is still CONNECTED (in control.ACTIVE_STATUSES) stays
on the board, so a delivery agent between heartbeats does not vanish from the
user's view. The carve-out is deliberately tight — it must not leak the standby
(AWAITING_OWNER_DIRECTION) agent, non-developer roles, or agents whose session
has closed.

Run:  PYTHONPATH=. python3 -m unittest tests.test_dashboard_session_visibility -v
"""
import tempfile
import unittest
from pathlib import Path

from harness import board, board_viewer, control


class DashboardSessionVisibility(unittest.TestCase):
    def _agents(self, agents: dict, connected: set) -> dict:
        state = {"agents": agents}
        compact = board_viewer._compact_dashboard_state(state, list(), connected)
        return compact["agents"]

    # ---- S-SESS-001 (the point of the fix) ----
    def test_idle_developer_with_connected_session_stays_visible(self):
        agents = {"dev": {"id": "dev", "role": "development", "task": "T1",
                          "session_id": "s1", "active": False}}
        self.assertIn("dev", self._agents(agents, {"s1"}),
                      "an idle developer whose session is still connected must stay on the board")

    # ---- S-SESS-002 (guard: closed session drops the idle agent) ----
    def test_idle_developer_without_connected_session_is_hidden(self):
        agents = {"dev": {"id": "dev", "role": "development", "task": "T1",
                          "session_id": "s1", "active": False}}
        self.assertNotIn("dev", self._agents(agents, set()),
                         "an idle developer with no connected session must not linger")

    # ---- S-SESS-003 (guard: the standby agent never leaks in) ----
    def test_awaiting_direction_agent_never_leaks_even_when_connected(self):
        agents = {"standby": {"id": "standby", "role": "development",
                              "task": board.AWAITING_OWNER_DIRECTION,
                              "session_id": "s1", "active": False}}
        self.assertNotIn("standby", self._agents(agents, {"s1"}),
                         "the AWAITING_OWNER_DIRECTION standby must not appear as a live task agent")

    # ---- S-SESS-004 (guard: non-developer roles are not carved in) ----
    def test_non_developer_role_not_carved_in(self):
        agents = {"cto": {"id": "cto", "role": "cto", "task": "GLOBAL_MONITOR",
                          "session_id": "s1", "active": False}}
        self.assertNotIn("cto", self._agents(agents, {"s1"}),
                         "only developer roles get the connected-session carve-out")

    # ---- S-SESS-005 (no regression: active agents still always show) ----
    def test_active_agent_always_visible(self):
        agents = {"dev": {"id": "dev", "role": "engineering", "task": "T1",
                          "session_id": "s9", "active": True}}
        # Visible even though its session is not in the connected set.
        self.assertIn("dev", self._agents(agents, set()),
                      "an active agent must always be visible regardless of session state")

    # ---- S-SESS-006 (integration: real session drives live_session_ids) ----
    def test_dashboard_payload_wires_connected_sessions(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            session = control.create(root, "codex_delivery")  # status: launching (ACTIVE)
            agent = board.register(root, "development", board.AWAITING_OWNER_DIRECTION,
                                   vendor="OpenAI", session_id=session["id"])
            board.record_owner_direction(root, session["id"], "Directive.")
            board.begin_task(root, agent["id"], "SESS-INT")
            # The real payload computes live_session_ids from ACTIVE_STATUSES sessions
            # and projects compact state without error; the working agent is visible.
            payload = board_viewer.dashboard_payload(root)
            self.assertIn(agent["id"], payload["state"]["agents"],
                          "a working developer agent on a connected session must be on the board")


if __name__ == "__main__":
    unittest.main()
