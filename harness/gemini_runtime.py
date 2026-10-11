# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""The harness-owned directory that gives ONE Antigravity session its hooks, board tools and rules.

Spec board/tasks/2026-10-07_gemini-provider_SPEC.md G-C, M-6, M-14, M-15, M-16 (measured in T0 on macOS and Linux):

- The CLI loads `.agents/hooks.json`, `.agents/plugins/<name>/{plugin.json,mcp_config.json}` and `GEMINI.md`
  from every directory passed with `--add-dir`.
- That directory is the session's runtime directory (`runtime_dir`): under the manager home, outside every write
  grant (validated), created 0700 and removed with the session. No agent can write it, and no file of the
  owner's (`~/.gemini/config/...`) is touched (Q-3, Q-5b).

Each piece sits behind its own stage flag and capability record, exactly like Claude and Codex: a switched-off or
unproven stage writes nothing and says why (`PLUMBING FALLBACK <stage> ...`), and the launch is today's.
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from harness import agent_confinement, claude_inbox, cli_capabilities, global_settings, runtime_dir

GATE_EVENTS = ("PreToolUse", "PreInvocation", "Stop")
# The Antigravity CLI loads at most this many bytes of a rules file and drops the rest (measured on 1.3.1: "Rule
# file ... truncated by 13389 bytes (original 37387 bytes, limit 24000 bytes)"). A directive that does not fit
# would reach the agent cut in the middle, so it stays in the first message instead (spec M-15: complete loading).
RULES_LIMIT_BYTES = 24000


def _inside_any(path: str, roots: list[str]) -> bool:
    real = os.path.realpath(path)
    return any(real == os.path.realpath(root) or real.startswith(os.path.realpath(root).rstrip("/") + "/") for root in roots)


def hooks_document(python: str, harness_root: str, timeout: int, *, guard: bool) -> dict[str, Any]:
    """`.agents/hooks.json`: the gate for tool calls (guard), and the turn signals / delivery for every invocation."""
    gate = " ".join(shlex.quote(part) for part in (python, "-E", os.path.join(harness_root, "harness", "hook_gate.py"), "gemini"))

    def command(event: str) -> dict[str, Any]:
        return {"type": "command", "command": f"{gate} {event}", "timeout": timeout + 5}
    group: dict[str, Any] = {"PreInvocation": [command("PreInvocation")], "Stop": [command("Stop")]}
    if guard:
        group["PreToolUse"] = [{"matcher": ".*", "hooks": [command("PreToolUse")]}]
    return {"harness-gate": group}


def mcp_document(python: str, harness_root: str, role: str, agent: str) -> dict[str, Any]:
    server = os.path.join(harness_root, "harness", "board_mcp.py")
    return {"mcpServers": {"harness_board": {"command": python, "args": ["-E", server, "--role", role, "--agent", agent]}}}


def prepare(home: Path, project: str, session: str, *, harness_root: str, python: str, writable: list[str],
            role: str, agent: str, directive: str, timeout: int | None = None) -> dict[str, Any]:
    """Decide which stages run for this Gemini session and write their files. Returns the launch additions."""
    notes: list[str] = []
    stages: dict[str, bool] = {}
    for stage in ("stage1_hooks", "stage2_board_mcp", "stage3_gemini_delivery", "stage4_system_layer_directives"):
        status = cli_capabilities.stage_status(home, stage, "gemini")
        stages[stage] = bool(status["enabled"])
        if status["reason"].startswith(("PLUMBING FALLBACK", "PLUMBING PROVING")):
            notes.append(status["reason"])
    result: dict[str, Any] = {"add_dir": "", "delivery": False, "stage4": False, "notes": notes}
    if not any(stages.values()):
        return result
    # An agent must not be able to edit the gate it is checked by, the tools it uses or the rules it follows.
    if _inside_any(harness_root, writable):
        for stage in [name for name, on in stages.items() if on and name != "stage4_system_layer_directives"]:
            notes.append(f"PLUMBING FALLBACK {stage} harness install is inside an agent write grant")
            stages[stage] = False
    directory = runtime_dir.session_directory(home, project, session)
    try:
        grant = agent_confinement.writable_paths(writable, home=os.path.expanduser("~"), provider="gemini")
        runtime_dir.validate(directory, grant, socket_names=("g.sock",))
    except (runtime_dir.RuntimeDirectoryRefused, agent_confinement.ConfinementUnavailable) as error:
        for stage, on in stages.items():
            if on:
                notes.append(f"PLUMBING FALLBACK {stage} {error}")
        return result
    if not any(stages.values()):
        return result
    plumbing = global_settings.load(home)["plumbing"]
    # The CLI kills a hook that runs longer than the deadline written here: it is the owner's Settings value (plus
    # the margin the gate itself needs), never a number of this file.
    timeout = plumbing["hook_gate_timeout_seconds"] if timeout is None else timeout
    runtime_dir.create(directory)
    agents = directory / ".agents"
    shutil.rmtree(agents, ignore_errors=True)
    for leftover in (directory / "GEMINI.md",):
        leftover.unlink(missing_ok=True)
    agents.mkdir(mode=0o700)
    if stages["stage1_hooks"] or stages["stage3_gemini_delivery"]:
        (agents / "hooks.json").write_text(
            json.dumps(hooks_document(python, harness_root, timeout, guard=stages["stage1_hooks"]), indent=2) + "\n", encoding="utf-8")
    if stages["stage2_board_mcp"]:
        plugin = agents / "plugins" / "harness_board"
        plugin.mkdir(parents=True, mode=0o700)
        (plugin / "plugin.json").write_text(json.dumps({"name": "harness_board"}) + "\n", encoding="utf-8")
        (plugin / "mcp_config.json").write_text(json.dumps(mcp_document(python, harness_root, role, agent), indent=2) + "\n", encoding="utf-8")
    rules = []
    if stages["stage4_system_layer_directives"] and directive.strip():
        size = len(directive.encode("utf-8")) + len(claude_inbox.AUTHORITY_NOTE.encode("utf-8")) + 2
        if size > RULES_LIMIT_BYTES:
            notes.append(f"PLUMBING FALLBACK stage4_system_layer_directives the directive is {size} bytes and Gemini loads at most "
                         f"{RULES_LIMIT_BYTES} bytes of a rules file; it stays in the first message")
            stages["stage4_system_layer_directives"] = False
        else:
            rules.append(directive)
    if stages["stage3_gemini_delivery"]:
        rules.append(claude_inbox.AUTHORITY_NOTE)
    if rules:
        (directory / "GEMINI.md").write_text("\n\n".join(rules) + "\n", encoding="utf-8")
    supervisor = ["--runtime-dir", str(directory)]
    if stages["stage3_gemini_delivery"]:
        supervisor += ["--gemini-delivery", "--busy-wait", str(plumbing["delivery_busy_wait_seconds"]),
                       "--receipt-timeout", str(plumbing["delivery_receipt_timeout_seconds"]),
                       "--app-server-timeout", str(plumbing["app_server_start_timeout_seconds"])]
    result.update(add_dir=str(directory), delivery=stages["stage3_gemini_delivery"], supervisor_args=supervisor,
                  gate_timeout=plumbing["hook_gate_timeout_seconds"],
                  stage4=stages["stage4_system_layer_directives"] and bool(directive.strip()))
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Prepare one Gemini session's harness runtime directory; prints JSON")
    parser.add_argument("--manager-home", required=True)
    parser.add_argument("--project", required=True)
    parser.add_argument("--session", required=True)
    parser.add_argument("--harness-root", required=True)
    parser.add_argument("--python", required=True)
    parser.add_argument("--writable-json", required=True)
    parser.add_argument("--role", default="")
    parser.add_argument("--agent", default="")
    parser.add_argument("--timeout", type=int, default=None)
    args = parser.parse_args(argv)
    result = prepare(Path(args.manager_home), args.project, args.session, harness_root=args.harness_root, python=args.python,
                     writable=json.loads(args.writable_json), role=args.role, agent=args.agent,
                     directive=sys.stdin.read(), timeout=args.timeout)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    for note in result.pop("notes"):
        print(stamp, "--", note, file=sys.stderr)
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
