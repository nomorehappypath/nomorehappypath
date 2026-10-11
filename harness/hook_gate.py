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
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from harness import board_client  # noqa: E402

DEADLINE_ENV = "HARNESS_HOOK_GATE_TIMEOUT"
HANDOVER_ENV = "HARNESS_INBOX_HANDOVER"
GEMINI_HANDOVER_ENV = "HARNESS_GEMINI_HANDOVER"
UNREACHABLE = "harness gate unreachable - retry"
INPUT_LIMIT = 48 * 1024
RETRY_PAUSE_SECONDS = 0.3


# Antigravity CLI tools (measured on 1.3.1, hook payload `toolCall.name` / `toolCall.args`). The rules in
# `hook_rules` speak Claude's vocabulary, so a Gemini call is translated, not re-implemented (spec M-14).
GEMINI_SHELL = {"run_command"}
GEMINI_PATH_KEYS = ("TargetFile", "AbsolutePath", "FilePath", "File", "Path", "SearchPath", "DirectoryPath", "SearchDirectory")
GEMINI_WRITE_WORDS = ("write", "replace", "edit", "delete", "create", "rename", "move")


def gemini_call(payload: dict) -> dict:
    """The hook payload of an Antigravity `PreToolUse` in the vocabulary `hook_rules.evaluate` knows.

    Unknown tools are never waved through: any tool carrying a path argument is judged as a read (or a
    write, by its name), so the credential rule and the Reviewer-does-not-edit rule apply to it too.
    """
    call = payload.get("toolCall") if isinstance(payload.get("toolCall"), dict) else {}
    name, args = str(call.get("name") or ""), call.get("args") if isinstance(call.get("args"), dict) else {}

    def value(key):
        item = args.get(key)
        if isinstance(item, str):
            try:
                decoded = json.loads(item)
                return decoded if isinstance(decoded, str) else item
            except ValueError:
                return item
        return item
    if name in GEMINI_SHELL or value("CommandLine"):
        return {"tool_name": "Bash", "tool_input": {"command": value("CommandLine") or "", "run_in_background": value("IsDaemon") is True}}
    path = next((str(value(key)) for key in GEMINI_PATH_KEYS if value(key)), "")
    if path:
        writes = any(word in name.lower() for word in GEMINI_WRITE_WORDS)
        return {"tool_name": "Write" if writes else "Read", "tool_input": {"file_path": path}}
    return {"tool_name": name, "tool_input": {key: item for key, item in args.items() if isinstance(item, (str, bool, int))}}


def _deny(provider: str, reason: str) -> str:
    if provider == "gemini":
        # Antigravity's documented PreToolUse reply.
        return json.dumps({"decision": "deny", "reason": reason})
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
        if provider == "gemini":
            return _gemini(event, payload)
        if event == "SessionStart" and os.environ.get(HANDOVER_ENV):
            # Plumbing Stage 3 (Claude): this session's own-child inbox relay.
            # Fails open like every observation hook: no relay means the
            # supervisor keeps typing, as today.
            try:
                from harness import inbox_relay
                inbox_relay.start_detached(os.environ[HANDOVER_ENV], str(payload.get("transcript_path") or ""))
            except Exception:  # noqa: BLE001
                pass
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


def _gemini(event: str, payload: dict) -> int:
    """The Antigravity events: PreToolUse guards; PreInvocation reports a working turn (and may carry a
    harness message into it); Stop reports idle only when the CLI says it is fully idle."""
    guard = event == "PreToolUse"
    try:
        deadline = float(os.environ.get(DEADLINE_ENV, "5"))
        if guard:
            call = gemini_call(payload)
            arguments = ["hook-event", "--event", "PreToolUse", "--payload", _compact(call)]
            try:
                result = board_client.call(arguments, timeout=deadline)
            except Exception:  # noqa: BLE001 - one immediate retry: Antigravity gives up after a few denials in a row
                time.sleep(RETRY_PAUSE_SECONDS)
                result = board_client.call(arguments, timeout=deadline)
            if result.get("decision") == "deny":
                print(_deny("gemini", f"Blocked by the harness ({result.get('rule')}): {result.get('reason')}"))
            else:
                # Measured on agy 1.3.1 (macOS, inside the wrapper): an empty object `{}` DENIES the tool call
                # ("tool call denied by pre-tool hook:"); printing nothing or an explicit allow permits it.
                print(json.dumps({"decision": "allow"}))
            return 0
        board_event = {"PreInvocation": "UserPromptSubmit", "Stop": "Stop"}.get(event)
        injected = ""
        if os.environ.get(GEMINI_HANDOVER_ENV) and event in {"PreInvocation", "Stop"}:
            try:
                from harness import gemini_delivery
                injected = gemini_delivery.ask(os.environ[GEMINI_HANDOVER_ENV], payload, event)
            except Exception:  # noqa: BLE001 - observation and delivery fail open: no relay means the supervisor types
                injected = ""
        if board_event and not (event == "Stop" and payload.get("fullyIdle") is not True):
            report = {"model": str(payload.get("modelName") or "")} if event == "PreInvocation" else {}
            try:
                board_client.call(["hook-event", "--event", board_event, "--payload", json.dumps(report)], timeout=deadline)
            except Exception:  # noqa: BLE001 - a lost turn report must never lose a message already handed over
                pass
        print(json.dumps({"injectSteps": [{"userMessage": injected}]}) if injected else "{}")
        return 0
    except Exception as error:  # noqa: BLE001 - the whole point: any failure has a defined outcome
        # The cause goes into the denial text: the CLI shows it to the agent and the owner, and a gate that
        # says only "unreachable" cannot be diagnosed afterwards.
        print(_deny("gemini", f"{UNREACHABLE} ({type(error).__name__}: {str(error)[:100]})") if guard else "{}")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
