# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
"""Walk the full requirements contract for test fixtures.

The confirmation gate requires an accepted proposal (the owner's recorded
Go ahead); fixtures use this helper so they exercise the real protocol
instead of bypassing it. The [OWNER DECISION] instruction the go-ahead queues
for the terminal is drained here (and only it), so fixtures that assert on
their own queued instructions keep their original expectations.
"""
from harness import board, control


def agreed_requirements(root, agent_id: str, text: str):
    event = board.record_requirement_proposal(root, agent_id, text)
    board.record_requirements_decision(root, event["task"], "go_ahead")
    confirmation = board.record_requirement_confirmation(root, agent_id, text)
    state = board.snapshot(root)
    agents = state.get("agents") or {}
    agent = agents.get(agent_id) if isinstance(agents, dict) else next(
        (a for a in agents if a.get("id") == agent_id), None,
    )
    session_id = str((agent or {}).get("session_id", "")).strip()
    if session_id:
        items = control.take_instructions(root, session_id)
        for item in items:
            if item.get("source") != "owner-requirements-go_ahead":
                control.enqueue_instruction(
                    root, session_id, item.get("text", ""),
                    source=item.get("source", "requeued"),
                )
    return confirmation
