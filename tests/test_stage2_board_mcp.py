# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Plumbing Stage 2: the board as typed MCP tools, alongside the board CLI.

Parity is the contract: a tool call produces exactly the argv the CLI would
send, so the worker and every gate see identical requests. The schemas are
derived from the CLI's own parser; the role sees only its authorized
operations; the token reaches a Codex MCP server by NAME, never as a value.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests.claude_auth_support import auth_arguments

from harness import board, board_mcp, board_surface, cli_capabilities, contract, control, global_settings, project_registry
from harness.board_client import ENDPOINT_ENV, PROTOCOL_ENV, TOKEN_ENV
from harness.board_surface import PROTOCOL_VERSION, SessionTokenAuthority
from harness.project_context import ProjectContext
from tests import test_board_surface as surface
from tests.environment_support import require_loopback

ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "scripts" / "run_managed_agent.sh"
SERVER = ROOT / "harness" / "board_mcp.py"


def sample(schema: dict) -> object:
    kind = schema.get("type")
    if "enum" in schema:
        return schema["enum"][0]
    if kind == "boolean":
        return True
    if kind == "integer":
        return 1
    if kind == "array":
        return [sample(schema["items"])]
    if kind == "object":
        return {name: sample(child) for name, child in schema["properties"].items()}
    return "x"


class ParityTests(unittest.TestCase):
    def test_every_tool_call_is_an_argv_the_board_cli_accepts(self):
        parser = board.build_parser()
        for role in ("engineering", "qa", "cto"):
            for tool in board_mcp.tool_definitions(role):
                arguments = {name: sample(schema) for name, schema in tool["inputSchema"]["properties"].items()}
                argv = board_mcp.argv_for(tool["name"], arguments)
                # The worker adds --agent from the token; the CLI parser must then accept the whole argv.
                if "--agent" in board_mcp._subcommands()[tool["name"]]._option_string_actions:  # noqa: SLF001
                    argv = [*argv, "--agent", "a-1"]
                with self.subTest(role=role, tool=tool["name"]):
                    self.assertEqual(parser.parse_args(["--root", "/x", *argv]).command, tool["name"])

    def test_structured_fields_join_exactly_as_the_cli_splits_them(self):
        self.assertEqual(board_mcp.argv_for("declare-subtasks", {"subtask": [
            {"id": "auth", "title": "Auth", "acceptance_proof": "Users can log in", "dependencies": ["db"],
             "owned_paths": ["src/auth", "tests/auth"], "owned_surfaces": ["api:auth"]}]}),
            ["declare-subtasks", "--subtask", "auth|Auth|Users can log in|db|src/auth,tests/auth|api:auth"])
        self.assertEqual(board_mcp.argv_for("qa-result", {"request": "r", "result": "failed", "summary": "s", "failure": [
            {"id": "F1", "category": "c", "summary": "s", "affected_paths": ["a.py"], "surface": "cli", "regression_check": "t"}]}),
            ["qa-result", "--request", "r", "--result", "failed", "--summary", "s",
             "--failure", "F1|c|s|a.py|cli|t"])
        self.assertEqual(board_mcp.argv_for("declare-chunks", {"chunk": [{"name": "api", "description": "API behavior"}]}),
                         ["declare-chunks", "--chunk", "api:API behavior"])

    def test_a_field_containing_its_separator_is_refused_not_mis_split(self):
        for operation, arguments, message in (
                ("declare-chunks", {"chunk": [{"name": "a:b", "description": "d"}]}, "may not contain ':'"),
                ("expand-contract", {"deliverable": [{"name": "x|y", "acceptance_proof": "p"}]}, r"may not contain '\|'"),
                ("declare-subtasks", {"subtask": [{"id": "a", "title": "t", "acceptance_proof": "p", "owned_paths": ["a,b"]}]},
                 "may not contain ','")):
            with self.subTest(operation=operation), self.assertRaisesRegex(ValueError, message):
                board_mcp.argv_for(operation, arguments)

    def test_each_role_sees_exactly_its_authorized_operations(self):
        for role in ("engineering", "qa", "cto"):
            names = {tool["name"] for tool in board_mcp.tool_definitions(role)}
            expected = {operation for operation, roles in board_surface.AUTHORIZATION_MATRIX.items()
                        if role in roles} - board_mcp.NOT_TOOLS
            self.assertEqual(names, expected, role)
            for tool in board_mcp.tool_definitions(role):
                self.assertNotIn("agent", tool["inputSchema"]["properties"], "identity is never the caller's")


class ServedTests(unittest.TestCase):
    """The server process, over stdio, against a real authenticated worker."""

    served = surface.BoardSurfaceCommandTests.served
    session = surface.BoardSurfaceCommandTests.session

    def setUp(self):
        require_loopback()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name)
        code = base / "code"; code.mkdir()
        self.context = ProjectContext(code, code / ".harness", base / "workspaces")
        control.initialize(self.context)

    def exchange(self, token, endpoint, role, messages):
        environment = {**os.environ, TOKEN_ENV: token, ENDPOINT_ENV: endpoint, PROTOCOL_ENV: PROTOCOL_VERSION}
        payload = "".join(json.dumps(message) + "\n" for message in messages)
        completed = subprocess.run([os.path.realpath(os.sys.executable), "-E", str(SERVER), "--role", role, "--agent", self.agent_id],
                                   input=payload, env=environment, capture_output=True, text=True, timeout=60)
        return {item["id"]: item for item in map(json.loads, completed.stdout.splitlines())}

    def test_a_tool_call_reaches_the_board_and_a_foreign_tool_is_refused(self):
        session, agent, authority, token, _ = self.session()
        self.agent_id = agent["id"]
        board.record_owner_direction(self.context, session["id"], "OWNER DIRECTION — a tiny greeter")
        board.begin_task(self.context, agent["id"], "cli-greeter")
        evidence = Path(self._tmp.name) / "agent-scratch.txt"
        evidence.write_text("Ran 2 tests\n\nOK\n", encoding="utf-8")
        with self.served(authority) as endpoint:
            replies = self.exchange(token, endpoint, "engineering", [
                {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "create-contract", "arguments": {
                    "objective": "A tiny greeter", "deliverable": ["greeting"]}}},
                {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "contract-evidence", "arguments": {
                    "deliverable": "greeting", "evidence": str(evidence)}}},
                {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "qa-result", "arguments": {
                    "request": "r", "result": "passed", "summary": "s"}}},
            ])
        self.assertEqual(replies[1]["result"]["serverInfo"]["name"], "harness_board")
        self.assertIn("create-contract", {tool["name"] for tool in replies[2]["result"]["tools"]})
        self.assertFalse(replies[3]["result"]["isError"], replies[3])
        self.assertFalse(replies[4]["result"]["isError"], replies[4])
        complete, problems, value = contract.contract_complete(self.context, "cli-greeter")
        self.assertTrue(complete, problems)
        self.assertTrue(value["deliverables"][0]["evidence"][0]["path"].startswith(str(self.context.data_root)),
                        "the evidence file was uploaded through the same ingestion as the CLI")
        self.assertTrue(replies[5]["result"]["isError"])
        self.assertIn("not a board tool for this role", replies[5]["result"]["content"][0]["text"])


class RunnerTests(unittest.TestCase):
    """The real runner: the MCP server is configured inline, only when switched on and proven."""

    served = surface.BoardSurfaceAuthenticationTests.served
    bootstrap_served = surface.BoardSurfaceAuthenticationTests.bootstrap_served

    def setUp(self):
        require_loopback()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)
        code = self.base / "code"; code.mkdir()
        self.context = ProjectContext(code, self.base / "home" / "projects" / "p1" / "data",
                                      self.base / "home" / "projects" / "p1" / "workspaces")
        control.initialize(self.context)
        self.home = self.base / "home"
        project_registry.save(self.home, {"version": project_registry.REGISTRY_VERSION, "projects": [
            {"id": "p1", "name": "project", "code_root": str(code), "data_root": str(self.context.data_root),
             "workspace_root": str(self.context.workspace_root)}]})
        self.capture = self.base / "argv.json"

    def fake(self, name: str) -> Path:
        path = self.base / name
        path.write_text("#!/usr/bin/env python3\nimport json, sys\n"
                        f"json.dump(sys.argv[1:], open({str(self.capture)!r}, 'w'))\n", encoding="utf-8")
        path.chmod(0o755)
        return path

    def launch(self, kind: str, provider: str, on: bool) -> list[str]:
        environment = {"HARNESS_CLAUDE_BIN": str(self.fake("fake-claude")), "HARNESS_CODEX_BIN": str(self.fake("fake-codex")),
                       "CLAUDE_CONFIG_DIR": str(self.base / "claude-config"), "CODEX_HOME": str(self.base / "codex-home")}
        settings = global_settings.load(self.home)
        settings["plumbing"]["stage2_board_mcp_enabled"] = on
        global_settings._write(self.home, settings)
        identity = cli_capabilities.binary_identity(provider, source_environment={**os.environ, **environment})
        path = cli_capabilities.cache_path(self.home, identity)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"identity": identity, "probed_at": "test", "probe_version": cli_capabilities.PROBE_VERSION,
                                    "static": {f"{provider}.mcp_config" if provider == "claude" else "codex.mcp_command": True}}),
                        encoding="utf-8")
        cli_capabilities.record_live(self.home, provider, {f"{provider}.mcp_reaches_board": True}, auth_mode="test",
                                     source_environment={**os.environ, **environment})
        session = control.create(self.context, kind)
        authority = SessionTokenAuthority(self.context)
        authority.prepare(session["id"])
        with self.served(authority) as endpoint, self.bootstrap_served(authority, endpoint) as bootstrap:
            completed = subprocess.run([
                "/bin/bash", str(RUNNER), "--root", str(self.context.code_root),
                "--data-root", str(self.context.data_root), "--workspace-root", str(self.context.workspace_root),
                "--python", os.path.realpath(os.sys.executable), "--session-id", session["id"], "--kind", kind,
                "--manager-home", str(self.home), "--board-bootstrap", bootstrap,
                *auth_arguments(self.context, session),
            ], cwd=self.context.code_root, env={**os.environ, "HARNESS_EXECUTION_ROOT": str(self.context.code_root), **environment},
                capture_output=True, text=True, timeout=60)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return json.loads(self.capture.read_text(encoding="utf-8"))

    def test_claude_gets_the_server_inline_with_its_role(self):
        if not Path("/usr/bin/sandbox-exec").exists() and not shutil.which("bwrap"):
            self.skipTest("no write-confinement primitive on this platform")
        argv = self.launch("claude_reviewer", "claude", on=True)
        config = json.loads(argv[argv.index("--mcp-config") + 1])["mcpServers"]["harness_board"]
        self.assertEqual(config["args"][1:4], [str(SERVER), "--role", "qa"])
        self.assertEqual(config["args"][4], "--agent")

    def test_codex_gets_the_server_with_the_token_forwarded_by_name_only(self):
        argv = self.launch("codex_delivery", "codex", on=True)
        flags = [argv[index + 1] for index, item in enumerate(argv) if item == "-c"]
        args = json.loads(next(flag for flag in flags if flag.startswith("mcp_servers.harness_board.args=")).split("=", 1)[1])
        self.assertEqual(args[:5], ["-E", str(SERVER), "--role", "engineering", "--agent"])
        self.assertIn('mcp_servers.harness_board.env_vars=["HARNESS_BOARD_TOKEN", "HARNESS_BOARD_ENDPOINT", "HARNESS_BOARD_PROTOCOL"]', flags)
        # Found live: with approval_policy=never, an MCP call that needs approval is refused.
        self.assertIn('mcp_servers.harness_board.default_tools_approval_mode="approve"', flags)
        self.assertFalse(any("mcp_servers" in flag and "env=" in flag for flag in flags), "no token value on the command line")

    def test_switched_off_neither_vendor_gets_a_server(self):
        self.assertNotIn("--mcp-config", self.launch("codex_delivery", "codex", on=False))
        argv = self.launch("codex_delivery", "codex", on=False)
        self.assertFalse(any(item.startswith("mcp_servers.") for item in argv))


if __name__ == "__main__":
    unittest.main()
