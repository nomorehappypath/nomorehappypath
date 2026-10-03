# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""The board as typed MCP tools (plumbing Stage 2, docs/specs/PLUMBING_MODERNIZATION.md).

    python3 -E <harness>/harness/board_mcp.py --role engineering|qa|cto --agent <id>    (MCP over stdio)

The agent's CLI starts this as its own child. It is a CLIENT of the worker,
exactly like `board_client`: same token, same nonce allocator, same
authenticated endpoint, same server-side gates. Nothing moves; the agent stops
assembling shell strings and gets JSON-Schema inputs instead.

The tool schemas are DERIVED from `board.build_parser()` - the command line is
the single source - and the arguments a tool call produces are the same argv
the CLI would send, so the gateway sees identical requests. The few hand-split
arguments (`ID|TITLE|...`, `NAME:DESCRIPTION`) become named fields; a field
containing its separator is refused instead of silently mis-split.

The board CLI stays; agents may use either.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any
from urllib.error import HTTPError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from harness import board, board_client, board_surface  # noqa: E402

SERVER_NAME = "harness_board"
PROTOCOL_VERSION = "2025-06-18"
HIDDEN = {"--agent", "--session-id", "--root", "--data-root", "--workspace-root", "-h", "--help"}
LONG_RUNNING = {"request-review", "execute-challenge", "record-release"}
# The runner registers the agent; hooks report lifecycle. Neither is the agent's tool.
NOT_TOOLS = {"register", "hook-event"}

# Hand-split CLI arguments, as named fields: (separator, [(field, is_list)]).
STRUCTURED: dict[tuple[str, str], tuple[str, list[tuple[str, bool]]]] = {
    ("declare-subtasks", "--subtask"): ("|", [("id", False), ("title", False), ("acceptance_proof", False),
                                              ("dependencies", True), ("owned_paths", True), ("owned_surfaces", True)]),
    ("qa-result", "--failure"): ("|", [("id", False), ("category", False), ("summary", False),
                                       ("affected_paths", True), ("surface", False), ("regression_check", False)]),
    ("resolve-repair-package", "--resolution"): ("|", [("id", False), ("resolution", False), ("regression_check", False)]),
    ("expand-contract", "--deliverable"): ("|", [("name", False), ("acceptance_proof", False)]),
    ("declare-chunks", "--chunk"): (":", [("name", False), ("description", False)]),
    ("declare-subtask-chunks", "--chunk"): (":", [("name", False), ("description", False)]),
}
LIST_OF_IDS = {("split-repair-package", "--group")}   # each entry: comma-joined member ids


def _subcommands() -> dict[str, argparse.ArgumentParser]:
    parser = board.build_parser()
    action = next(item for item in parser._actions if isinstance(item, argparse._SubParsersAction))  # noqa: SLF001
    return dict(action.choices)


def _property(operation: str, action: argparse.Action) -> dict[str, Any]:
    option = action.option_strings[0]
    repeated = isinstance(action, argparse._AppendAction)  # noqa: SLF001
    description = action.help or (action.metavar if isinstance(action.metavar, str) else "") or ""
    if isinstance(action, argparse._StoreTrueAction):  # noqa: SLF001
        return {"type": "boolean", "description": description}
    if (operation, option) in STRUCTURED:
        _separator, fields = STRUCTURED[(operation, option)]
        item = {"type": "object", "additionalProperties": False,
                "properties": {name: ({"type": "array", "items": {"type": "string"}} if is_list else {"type": "string"})
                               for name, is_list in fields},
                "required": [name for name, is_list in fields if not is_list]}
        return {"type": "array", "items": item} if repeated else item
    if (operation, option) in LIST_OF_IDS:
        return {"type": "array", "items": {"type": "array", "items": {"type": "string"}}}
    scalar: dict[str, Any] = {"type": "integer" if action.type is int else "string"}
    if action.choices:
        scalar["enum"] = list(action.choices)
    if description:
        scalar["description"] = description
    return {"type": "array", "items": scalar} if repeated else scalar


def tool_definitions(role: str) -> list[dict[str, Any]]:
    """Exactly the operations the role is authorized for (the surface's matrix decides; this only mirrors it)."""
    tools = []
    for operation, parser in sorted(_subcommands().items()):
        if operation in NOT_TOOLS or role not in board_surface.AUTHORIZATION_MATRIX.get(operation, frozenset()):
            continue
        properties, required = {}, []
        for action in parser._actions:  # noqa: SLF001
            if not action.option_strings or action.option_strings[0] in HIDDEN:
                continue
            name = action.option_strings[0].lstrip("-").replace("-", "_")
            properties[name] = _property(operation, action)
            if action.required:
                required.append(name)
        tools.append({"name": operation, "description": (parser.description or f"board {operation}"),
                      "inputSchema": {"type": "object", "properties": properties, "required": required,
                                      "additionalProperties": False}})
    return tools


def _join(value: dict[str, Any], separator: str, fields: list[tuple[str, bool]]) -> str:
    parts = []
    for name, is_list in fields:
        item = value.get(name, [] if is_list else "")
        text = ",".join(str(entry) for entry in item) if is_list else str(item)
        if is_list and any("," in str(entry) for entry in item):
            raise ValueError(f"{name} entries may not contain ','")
        if separator in text:
            raise ValueError(f"{name} may not contain '{separator}'")
        parts.append(text)
    return separator.join(parts)


def argv_for(operation: str, arguments: dict[str, Any]) -> list[str]:
    """The exact argv the board CLI would send for this tool call."""
    parser = _subcommands().get(operation)
    if parser is None:
        raise ValueError(f"unknown board operation: {operation}")
    actions = {action.option_strings[0].lstrip("-").replace("-", "_"): action
               for action in parser._actions if action.option_strings and action.option_strings[0] not in HIDDEN}  # noqa: SLF001
    unknown = set(arguments) - set(actions)
    if unknown:
        raise ValueError(f"unknown arguments for {operation}: {', '.join(sorted(unknown))}")
    argv = [operation]
    for name, action in actions.items():
        if name not in arguments:
            continue
        option, value = action.option_strings[0], arguments[name]
        if isinstance(action, argparse._StoreTrueAction):  # noqa: SLF001
            if value is True:
                argv.append(option)
            continue
        values = value if isinstance(action, argparse._AppendAction) else [value]  # noqa: SLF001
        if not isinstance(values, list):
            raise ValueError(f"{name} must be a list")
        for item in values:
            if (operation, option) in STRUCTURED:
                separator, fields = STRUCTURED[(operation, option)]
                if not isinstance(item, dict):
                    raise ValueError(f"{name} entries must be objects")
                item = _join(item, separator, fields)
            elif (operation, option) in LIST_OF_IDS:
                if any("," in str(member) for member in item):
                    raise ValueError(f"{name} member ids may not contain ','")
                item = ",".join(str(member) for member in item)
            argv.extend((option, str(item)))
    return argv


def _call(operation: str, arguments: dict[str, Any], agent: str = "") -> dict[str, Any]:
    argv = argv_for(operation, arguments)
    if agent and "--agent" in _subcommands()[operation]._option_string_actions:  # noqa: SLF001
        # The registered id the runner was given; the surface still checks it
        # against the token's own session and refuses any other.
        argv += ["--agent", agent]
    timeout = 360 if operation in LONG_RUNNING else 30
    try:
        result = board_client.call(argv, timeout=timeout, uploads=True)
        return {"content": [{"type": "text", "text": json.dumps(result, indent=2, sort_keys=True)}], "isError": False}
    except HTTPError as error:
        try:
            detail = json.loads(error.read() or b"{}").get("error") or str(error)
        except (ValueError, OSError):
            detail = str(error)
        return {"content": [{"type": "text", "text": f"board refused {operation}: {detail}"}], "isError": True}
    except (OSError, ValueError) as error:
        return {"content": [{"type": "text", "text": f"board call {operation} failed: {error}"}], "isError": True}


SELFTEST_ENV = "HARNESS_BOARD_MCP_SELFTEST"


def _selftest() -> None:
    """Opt-in proof that this server, as started by the CLI, reaches the board.

    Set only by the capability spike and the tests: after `initialize`, make one
    read-only call (`findings`) with the session's own credentials and write the
    outcome to the named file. Proves spawn, inherited credentials and loopback
    reach from wherever the vendor runs its MCP servers.
    """
    path = os.environ.get(SELFTEST_ENV)
    if not path:
        return
    try:
        result = board_client.call(["findings"], timeout=15)
        outcome = f"OK {len(result) if isinstance(result, (list, dict)) else 0}"
    except Exception as error:  # noqa: BLE001 - the outcome IS the report
        outcome = f"ERR {type(error).__name__}: {error}"[:300]
    Path(path).write_text(outcome + "\n", encoding="utf-8")


def serve(role: str, stdin=None, stdout=None, agent: str = "") -> int:
    stdin, stdout = stdin or sys.stdin, stdout or sys.stdout
    tools = tool_definitions(role)
    names = {tool["name"] for tool in tools}

    def reply(ident, result=None, error=None):
        message = {"jsonrpc": "2.0", "id": ident}
        message["error" if error else "result"] = error or result
        stdout.write(json.dumps(message, separators=(",", ":")) + "\n")
        stdout.flush()

    for line in stdin:
        if not line.strip():
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        method, ident, params = message.get("method"), message.get("id"), message.get("params") or {}
        if ident is None:
            continue  # notifications (initialized, cancelled) need no answer
        if method == "initialize":
            reply(ident, {"protocolVersion": params.get("protocolVersion") or PROTOCOL_VERSION,
                          "capabilities": {"tools": {}},
                          "serverInfo": {"name": SERVER_NAME, "version": "1"}})
            _selftest()
        elif method == "ping":
            reply(ident, {})
        elif method == "tools/list":
            reply(ident, {"tools": tools})
        elif method == "tools/call":
            name = params.get("name", "")
            if name not in names:
                reply(ident, {"content": [{"type": "text", "text": f"{name} is not a board tool for this role"}],
                              "isError": True})
                continue
            try:
                reply(ident, _call(name, params.get("arguments") or {}, agent))
            except ValueError as error:
                reply(ident, {"content": [{"type": "text", "text": str(error)}], "isError": True})
        else:
            reply(ident, error={"code": -32601, "message": f"method not found: {method}"})
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="The board as MCP tools, for one agent role")
    parser.add_argument("--role", required=True, choices=["engineering", "qa", "cto"])
    parser.add_argument("--agent", default="", help="this session's registered board agent id")
    args = parser.parse_args(argv)
    return serve(args.role, agent=args.agent)


if __name__ == "__main__":
    raise SystemExit(main())
