#!/usr/bin/env python3
# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Prove the plumbing LIVE capabilities of the installed CLIs (spec PLUMBING_MODERNIZATION.md, Stage 0).

Re-run on every CLI upgrade. Each live item is recorded `true`, `false` or
`UNPROVEN` beside the binary's identity (`harness.cli_capabilities.record_live`),
and UNPROVEN gates a stage exactly like `false`.

Hermetic by construction: every CLI run gets scratch `CODEX_HOME` /
`CLAUDE_CONFIG_DIR`; the owner's `~/.codex/config.toml` and
`~/.claude/settings.json` are hashed before and after, and the spike FAILS if
either changed. It never reads or copies the owner's login files. Items that
need an authenticated model session stay UNPROVEN until the owner provides an
isolated credential (OPENAI key for a scratch Codex home; a `claude
setup-token` token as CLAUDE_CODE_OAUTH_TOKEN); items that need a later
stage's code stay UNPROVEN until that stage exists.

Usage: python3 scripts/plumbing_spike.py --home <manager home>
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import select
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import threading  # noqa: E402
from contextlib import contextmanager  # noqa: E402
from http.server import ThreadingHTTPServer  # noqa: E402

from harness import agent_confinement, board, cli_capabilities, control, global_settings, project_worker  # noqa: E402
from harness.board_client import ENDPOINT_ENV, PROTOCOL_ENV, TOKEN_ENV  # noqa: E402
from harness.board_surface import PROTOCOL_VERSION, SessionTokenAuthority  # noqa: E402
from harness.project_context import ProjectContext  # noqa: E402

UNPROVEN = cli_capabilities.UNPROVEN
OWNER_FILES = (Path.home() / ".codex" / "config.toml", Path.home() / ".claude" / "settings.json")


def _hash(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return "absent"


def _scratch_env(executable: str, scratch: Path) -> dict[str, str]:
    environment = global_settings.provider_environment(executable)
    for name, sub in (("CODEX_HOME", "codex-home"), ("CLAUDE_CONFIG_DIR", "claude-config")):
        (scratch / sub).mkdir(parents=True, exist_ok=True)
        environment[name] = str(scratch / sub)
    return environment


def confined_socket_bind(claude: str, scratch: Path) -> tuple[object, str]:
    """G-7: a Claude CLI inside the harness's own write confinement still binds its inbox socket."""
    project = scratch / "project"; project.mkdir()
    record = scratch / "socket.txt"
    hook = {"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command":
            f"s=\"$CLAUDE_CODE_MESSAGING_SOCKET\"; if [ -S \"$s\" ]; then echo BOUND \"$s\"; else echo NOT_BOUND \"$s\"; fi > '{record}'"}]}]}}
    command = agent_confinement.wrap(
        [claude, "-p", "spike", "--output-format", "json", "--settings", json.dumps(hook)],
        [str(project)], store=scratch / "store", home=Path.home(),
        claude_config_dir=str(scratch / "claude-config"))
    subprocess.run(command, env=_scratch_env(claude, scratch), cwd=project, capture_output=True, text=True, timeout=90)
    text = record.read_text(encoding="utf-8").strip() if record.is_file() else ""
    if text.startswith("BOUND"):
        return True, f"confined claude bound its inbox socket: {text}"
    return False, f"confined claude did not bind an inbox socket: {text or 'SessionStart hook did not run'}"


def codex_stdio_isolated(codex: str, scratch: Path) -> tuple[object, str]:
    """G-2/G-6 (harness side): an app-server on stdio creates nothing in the shared daemon folder."""
    daemon = Path(f"/tmp/codex-daemon-{os.getuid()}")  # macOS: /tmp is the /private/tmp link
    before = set(os.listdir(daemon)) if daemon.is_dir() else set()
    process = subprocess.Popen([codex, "app-server", "--listen", "stdio://"], env=_scratch_env(codex, scratch),
                               cwd=scratch, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    answered = set()
    try:
        for ident, method, params in ((1, "initialize", {"clientInfo": {"name": "harness-spike", "version": "0"}}),
                                      (2, "thread/start", {"cwd": str(scratch)})):
            process.stdin.write(json.dumps({"jsonrpc": "2.0", "id": ident, "method": method, "params": params}) + "\n")
            process.stdin.flush()
            if ident == 1:
                process.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "initialized"}) + "\n"); process.stdin.flush()
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline and ident not in answered:
                ready, _, _ = select.select([process.stdout], [], [], 0.5)
                if ready:
                    line = process.stdout.readline()
                    if not line:
                        break
                    try:
                        if json.loads(line).get("id") == ident:
                            answered.add(ident)
                    except ValueError:
                        pass
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
    after = set(os.listdir(daemon)) if daemon.is_dir() else set()
    if answered != {1, 2}:
        return False, f"stdio app-server did not answer initialize/thread/start (answered {sorted(answered)})"
    if after - before:
        return False, f"stdio app-server created daemon entries: {sorted(after - before)}"
    return True, "stdio app-server answered initialize and thread/start and created nothing in the shared daemon folder"


@contextmanager
def _served_board(scratch: Path):
    """A throwaway project with a real authenticated worker on loopback, and one Delivery session's credentials."""
    code = scratch / "code"; code.mkdir()
    context = ProjectContext(code, code / ".harness", scratch / "workspaces")
    control.initialize(context)
    session = control.create(context, "codex_delivery")
    authority = SessionTokenAuthority(context)
    authority.prepare(session["id"])
    control.attach(context, session["id"], os.getpid())
    board.register(context, "engineering", board.AWAITING_OWNER_DIRECTION, session_id=session["id"])
    token = authority.claim(session["id"], os.getpid())
    box = {"endpoint": ""}
    server = ThreadingHTTPServer(("127.0.0.1", 0), project_worker.make_handler(context, authority=authority,
                                                                               endpoint=lambda: box["endpoint"]))
    box["endpoint"] = f"http://127.0.0.1:{server.server_address[1]}"
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    try:
        yield code, {TOKEN_ENV: token, ENDPOINT_ENV: box["endpoint"], PROTOCOL_ENV: PROTOCOL_VERSION}
    finally:
        server.shutdown(); thread.join(timeout=3); server.server_close()


def _selftest_result(path: Path, seconds: float) -> str:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if path.is_file() and path.read_text(encoding="utf-8").strip():
            return path.read_text(encoding="utf-8").strip()
        time.sleep(0.5)
    return ""


def claude_mcp_reaches_board(claude: str, scratch: Path) -> tuple[object, str]:
    """Stage 2 (Claude): the CLI, inside the agent confinement, starts board_mcp, which reaches the worker."""
    with _served_board(scratch) as (code, credentials):
        record = scratch / "mcp-selftest.txt"
        config = {"mcpServers": {"harness_board": {"type": "stdio", "command": sys.executable,
                  "args": ["-E", str(ROOT / "harness" / "board_mcp.py"), "--role", "engineering"]}}}
        command = agent_confinement.wrap(
            [claude, "-p", "spike", "--output-format", "json", "--mcp-config", json.dumps(config)],
            [str(code)], store=scratch / "store", home=Path.home(), claude_config_dir=str(scratch / "claude-config"))
        environment = {**_scratch_env(claude, scratch), **credentials, "HARNESS_BOARD_MCP_SELFTEST": str(record)}
        subprocess.run(command, env=environment, cwd=code, capture_output=True, text=True, timeout=120)
        outcome = _selftest_result(record, 5)
    if outcome.startswith("OK"):
        return True, f"confined claude started board_mcp, which reached the worker ({outcome})"
    return False, f"board_mcp under claude did not reach the worker: {outcome or 'no self-test record'}"


def codex_mcp_reaches_board(codex: str, scratch: Path) -> tuple[object, str]:
    """Stage 2 (Codex): the app-server starts board_mcp with the credentials forwarded BY NAME; it reaches the worker."""
    with _served_board(scratch) as (code, credentials):
        record = scratch / "mcp-selftest.txt"
        flags = ["-c", "mcp_servers.harness_board.command=" + json.dumps(sys.executable),
                 "-c", "mcp_servers.harness_board.args=" + json.dumps(["-E", str(ROOT / "harness" / "board_mcp.py"), "--role", "engineering"]),
                 "-c", "mcp_servers.harness_board.env_vars=" + json.dumps([TOKEN_ENV, ENDPOINT_ENV, PROTOCOL_ENV, "HARNESS_BOARD_MCP_SELFTEST"])]
        environment = {**_scratch_env(codex, scratch), **credentials, "HARNESS_BOARD_MCP_SELFTEST": str(record)}
        process = subprocess.Popen([codex, "app-server", "--listen", "stdio://", *flags], env=environment, cwd=code,
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        try:
            for ident, method, params in ((1, "initialize", {"clientInfo": {"name": "harness-spike", "version": "0"}}),
                                          (2, "thread/start", {"cwd": str(code)})):
                process.stdin.write(json.dumps({"jsonrpc": "2.0", "id": ident, "method": method, "params": params}) + "\n")
                if ident == 1:
                    process.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "initialized"}) + "\n")
                process.stdin.flush()
            outcome = _selftest_result(record, 30)
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
    if outcome.startswith("OK"):
        return True, f"codex app-server started board_mcp with credentials forwarded by name; it reached the worker ({outcome})"
    return False, f"board_mcp under codex did not reach the worker: {outcome or 'no self-test record'}"


NEEDS_AUTH = "needs an authenticated isolated session (owner decision pending: isolated credentials)"
NEEDS_STAGE = "needs the stage's own code (proven by that stage's task)"
DEFERRED = {
    "codex": {
        "codex.hooks_loaded_once": NEEDS_AUTH,
        "codex.tui_via_multiplexer": NEEDS_STAGE + " — Stage 3 multiplexer",
        "codex.remote_renders_harness_turns": NEEDS_AUTH,
        "codex.turn_receipt": NEEDS_AUTH,
        "codex.developer_instructions_on_resume": NEEDS_AUTH,
    },
    "claude": {
        "claude.inbox_receipt": NEEDS_AUTH,
        "claude.socket_authority_parity": NEEDS_AUTH,
        "claude.append_prompt_on_resume": NEEDS_AUTH,
    },
    "both": {
        "cross_agent_isolation": NEEDS_STAGE + " — Stage 3 sockets and peer-PID checks",
        "launch_surfaces_tamperproof": NEEDS_STAGE + " — Stages 1-3 runtime directory",
    },
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--home", required=True, help="manager home whose capability cache receives the results")
    parser.add_argument("--guard-file", action="append", default=None,
                        help="an owner file that must be byte-identical after the spike (repeatable; "
                             "default: ~/.codex/config.toml and ~/.claude/settings.json)")
    parser.add_argument("--record", action="append", default=[], metavar="NAME=true|false",
                        help="record one live result proven by a visible run (repeatable; needs --evidence)")
    parser.add_argument("--evidence", default="", help="where the proof of --record is (a transcript, a board event)")
    args = parser.parse_args(argv)
    home = Path(args.home)
    if args.record:
        for item in args.record:
            name, _, value = item.partition("=")
            provider = "claude" if name.startswith("claude.") else "codex"
            if value not in ("true", "false"):
                parser.error(f"--record {item}: the value must be true or false")
            for target in (("codex", "claude") if "." not in name else (provider,)):
                cli_capabilities.record_live_item(home, target, name, value == "true", args.evidence)
        print(json.dumps({"recorded": args.record, "evidence": args.evidence}))
        return 0
    guarded = [Path(item).expanduser() for item in args.guard_file] if args.guard_file else list(OWNER_FILES)
    before = {str(path): _hash(path) for path in guarded}
    report: dict[str, dict[str, dict[str, object]]] = {"codex": {}, "claude": {}}
    identities = {provider: cli_capabilities.binary_identity(provider) for provider in ("codex", "claude")}
    with tempfile.TemporaryDirectory(prefix="harness-plumbing-spike-") as tmp:
        scratch = Path(tmp)
        if identities["claude"]["path"]:
            (scratch / "g7").mkdir()
            value, note = confined_socket_bind(identities["claude"]["path"], scratch / "g7")
            report["claude"]["confined_socket_bind"] = {"value": value, "note": note}
        if identities["codex"]["path"]:
            (scratch / "g2").mkdir()
            value, note = codex_stdio_isolated(identities["codex"]["path"], scratch / "g2")
            report["codex"]["codex.isolated_from_daemon"] = {"value": value, "note": note}
            (scratch / "mcp-codex").mkdir()
            value, note = codex_mcp_reaches_board(identities["codex"]["path"], scratch / "mcp-codex")
            report["codex"]["codex.mcp_reaches_board"] = {"value": value, "note": note}
        if identities["claude"]["path"]:
            (scratch / "mcp-claude").mkdir()
            value, note = claude_mcp_reaches_board(identities["claude"]["path"], scratch / "mcp-claude")
            report["claude"]["claude.mcp_reaches_board"] = {"value": value, "note": note}
    for provider in ("codex", "claude"):
        for name, note in {**DEFERRED[provider], **DEFERRED["both"]}.items():
            report[provider].setdefault(name, {"value": UNPROVEN, "note": note})
        report[provider].setdefault("confined_socket_bind", {"value": UNPROVEN, "note": "probed for the Claude CLI only"})
        if identities[provider]["path"]:
            # A result a visible run proved, with its evidence, is never
            # overwritten by this spike's "unproven".
            proven = cli_capabilities.capabilities(home, provider).get("live", {})
            results = {name: item["value"] for name, item in report[provider].items()}
            results.update({name: value for name, value in proven.items()
                            if value in (True, False) and results.get(name) == UNPROVEN})
            cli_capabilities.record_live(home, provider, results, auth_mode="none")
    after = {str(path): _hash(path) for path in guarded}
    print(json.dumps({"identities": identities, "results": report,
                      "owner_files_sha256": {"before": before, "after": after}}, indent=2, sort_keys=True))
    if before != after:
        print("SPIKE FAILED: an owner file changed during the spike", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
