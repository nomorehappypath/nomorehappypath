# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
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
    # like the page: the decision names the version and card state the owner was shown
    proposal = board.snapshot(root)["requirement_proposals"][event["task"]]
    board.record_requirements_decision(
        root, event["task"], "go_ahead", version=proposal["version"], token=(proposal.get("contract_check") or {}).get("token", ""),
    )
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


NO_SETUP = '{"ai_tools": []}'


def setup_checked(root, task: str, needs: str = NO_SETUP) -> None:
    """The task's setup list (by default nothing outside the project; `needs` gives another) recorded and NMHP's contract-stage
    setup check passed, as after a real check (owner 2026-11-21): fixtures that file a contract or request a review through the
    agent command use it."""
    from harness import environment_check
    with board.locked_state(root) as state:
        environment_check.record_needs(state, task, environment_check.parse_needs(needs), "fixture", board.now())
        run = environment_check.request(state, task, "contract", board.now())
        run.update(status="passed", finished_at=board.now(), identity=environment_check.TEST_SUITE_IDENTITY)
