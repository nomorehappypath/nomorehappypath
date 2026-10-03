# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""The command an agent's own CLI runs for each lifecycle hook (plumbing Stage 1).

    python3 -E <harness>/harness/hook_gate.py <provider> <Event>   (hook payload on stdin)

It relays the event to the project's worker over the session's existing
authenticated board channel (`hook-event`) and prints the decision in the
vendor's hook format.

FAIL CLOSED / FAIL OPEN. A PreToolUse decision is a guard: if the worker cannot
be reached within the deadline, or anything at all goes wrong, the tool call is
DENIED with "harness gate unreachable - retry". Every other event only reports,
so a failure there is silent and never blocks the agent.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from harness import board_client  # noqa: E402

DEADLINE_ENV = "HARNESS_HOOK_GATE_TIMEOUT"
UNREACHABLE = "harness gate unreachable - retry"
INPUT_LIMIT = 48 * 1024


def _deny(provider: str, reason: str) -> str:
    # Claude Code's documented PreToolUse contract. Stage 1 wires Claude only;
    # the Codex half waits for its live proof (cli_capabilities: hooks_loaded_once).
    return json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                              "permissionDecisionReason": reason}})


def _compact(payload: dict) -> str:
    """Only what the decision needs; oversized tool input is cut, never sent whole."""
    value = {"tool_name": payload.get("tool_name", ""), "tool_input": payload.get("tool_input") or {}}
    text = json.dumps(value, separators=(",", ":"))
    if len(text.encode("utf-8")) > INPUT_LIMIT:
        tool_input = value["tool_input"] if isinstance(value["tool_input"], dict) else {}
        value["tool_input"] = {key: (item[:4096] if isinstance(item, str) else item)
                               for key, item in tool_input.items() if key in {"command", "file_path", "path",
                                                                               "notebook_path", "run_in_background"}}
        text = json.dumps(value, separators=(",", ":"))
    return text


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    provider, event = (argv + ["", ""])[:2]
    guard = event == "PreToolUse"
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
        deadline = float(os.environ.get(DEADLINE_ENV, "5"))
        arguments = ["hook-event", "--event", event, "--payload", _compact(payload) if guard else "{}"]
        result = board_client.call(arguments, timeout=deadline)
        if guard and result.get("decision") == "deny":
            print(_deny(provider, f"Blocked by the harness ({result.get('rule')}): {result.get('reason')}"))
        return 0
    except Exception:  # noqa: BLE001 - the whole point: any failure has a defined outcome
        if guard:
            print(_deny(provider, UNREACHABLE))
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
