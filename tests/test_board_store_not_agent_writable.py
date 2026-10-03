# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""F-1: a managed agent cannot write the harness's own storage.

The board state, Completion Contracts, reviews, control records (including the
session-token verifiers) and evidence live under the project's data root. Their
gates run server-side, but until 2026-10-01 their INPUTS were files any agent
could edit: the data root was in every agent's write grant, and for a
scaffolded project it sits inside the checkout, which is always granted.

These tests prove the boundary by execution: the real runner, in the product's
launch mode (authenticated board surface), with a fake CLI that tries the
writes; and the real Codex sandbox driven with exactly the flags the runner
produced. Each one fails on the code before the fix.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from harness import agent_confinement, agent_grant, board, contract, control, platform_support, project_registry
from harness.board_client import ENDPOINT_ENV, PROTOCOL_ENV, TOKEN_ENV
from harness.board_surface import PROTOCOL_VERSION, SessionTokenAuthority
from harness.platform_support import defaults, linux
from harness.project_context import ProjectContext
from tests import test_board_surface as surface
from tests.environment_support import require_loopback

ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "scripts" / "run_managed_agent.sh"


def _real(path) -> str:
    return os.path.realpath(str(path))


class GrantTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)

    def _adopted(self):
        home = self.base / "home"
        code = self.base / "code"; code.mkdir()
        data = home / "projects" / "p1" / "data"; data.mkdir(parents=True)
        workspaces = home / "projects" / "p1" / "workspaces"; workspaces.mkdir(parents=True)
        project_registry.save(home, {"version": project_registry.REGISTRY_VERSION, "projects": [
            {"id": "p1", "name": "p", "code_root": str(code), "data_root": str(data), "workspace_root": str(workspaces)}]})
        return home, code, data, workspaces

    def test_with_the_board_surface_the_data_root_is_validated_but_not_granted(self):
        home, code, data, workspaces = self._adopted()
        legacy = agent_grant.agent_writable_roots(code, data, workspaces, code, home)
        self.assertIn(_real(data), legacy, "legacy launches (board run inside the agent) keep the old grant")
        granted = agent_grant.agent_writable_roots(code, data, workspaces, code, home, board_surface=True)
        self.assertEqual(granted, [_real(code), _real(workspaces)])

    def test_an_unassigned_data_root_still_refuses_with_the_board_surface(self):
        home, code, _, workspaces = self._adopted()
        elsewhere = self.base / "elsewhere"; elsewhere.mkdir()
        with self.assertRaises(agent_grant.GrantTooBroad):
            agent_grant.agent_writable_roots(code, elsewhere, workspaces, code, home, board_surface=True)

    def test_the_protected_roots_are_the_data_root_and_its_backups(self):
        # A scaffolded project: everything here sits INSIDE the granted checkout.
        # Board and memory backups are restored automatically when the live copy
        # is missing, so a planted backup would be a forged board.
        code = self.base / "code"; code.mkdir()
        data = code / ".harness"; data.mkdir()
        protected = agent_grant.agent_protected_roots(code, data, self.base / "workspaces")
        self.assertEqual(protected, [_real(data), _real(code / ".harness-backups"),
                                     _real(code / ".harness-memory-backups")])


class ProfileShapeTests(unittest.TestCase):
    def test_seatbelt_denies_the_protected_paths_after_every_allow(self):
        profile = defaults._AgentConfinement().profile(
            ["/Users/owner/project"], [], ["/Users/owner/project/.harness"])
        lines = profile.splitlines()
        deny = '(deny file-write* (subpath "/Users/owner/project/.harness"))'
        self.assertIn(deny, lines)
        last_allow = max(index for index, line in enumerate(lines) if line.startswith("(allow file-write*"))
        self.assertGreater(lines.index(deny), last_allow, "Seatbelt applies the last matching rule")

    def test_bubblewrap_rebinds_the_protected_paths_read_only_after_the_grant(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"; project.mkdir()
            protected = project / ".harness"
            bubblewrap = linux._BwrapAgentConfinement(which=lambda name: "/usr/bin/bwrap")
            with mock.patch.dict(os.environ, {"HARNESS_BWRAP_BIN": ""}):
                command = agent_confinement.wrap(
                    ["claude"], [str(project)], store=Path(tmp) / "store", home=Path(tmp) / "home",
                    implementation=bubblewrap, protected_writes=[str(protected)])
            real_project, real_protected = _real(project), _real(protected)
            triples = [command[index:index + 3] for index in range(len(command) - 2)]
            bind = triples.index(["--bind", real_project, real_project])
            ro = triples.index(["--ro-bind", real_protected, real_protected])
            self.assertGreater(ro, bind, "bwrap applies binds in order; the read-only bind must come last")
            self.assertTrue(protected.is_dir(), "created first, so the agent cannot create it inside the grant")


class ExecutedConfinementTests(unittest.TestCase):
    """The platform primitive, executed: a granted root stays writable, the protected path inside it does not."""

    def setUp(self):
        if not platform_support.agent_confinement().available():
            raise unittest.SkipTest("no write-confinement primitive on this platform")
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.project = Path(self._tmp.name) / "project"
        (self.project / ".harness" / "board").mkdir(parents=True)

    def test_a_write_inside_the_protected_path_is_refused_by_the_os(self):
        script = (
            f"( echo x > '{self.project}/work.txt' && echo WROTE_PROJECT ) 2>/dev/null || echo DENIED_PROJECT; "
            f"( echo x > '{self.project}/.harness/board/state.json' && echo WROTE_BOARD ) 2>/dev/null || echo DENIED_BOARD; "
            f"( mkdir '{self.project}/.harness/forged' && echo MADE_DIR ) 2>/dev/null || echo DENIED_DIR; "
            f"( mv '{self.project}/.harness' '{self.project}/moved' && echo MOVED ) 2>/dev/null || echo DENIED_MOVE"
        )
        command = agent_confinement.wrap(
            ["/bin/sh", "-c", script], [str(self.project)], store=Path(self._tmp.name) / "store",
            home=Path.home(), protected_writes=[str(self.project / ".harness")])
        completed = subprocess.run(command, capture_output=True, text=True, timeout=30)
        out = completed.stdout
        self.assertIn("WROTE_PROJECT", out, out + completed.stderr)
        for refused in ("DENIED_BOARD", "DENIED_DIR", "DENIED_MOVE"):
            self.assertIn(refused, out, out + completed.stderr)
        self.assertFalse((self.project / ".harness" / "board" / "state.json").exists())


    def test_the_conversation_reader_agents_are_given_works_inside_the_sandbox(self):
        """Recovery, pause and predecessor prompts hand agents `control.py conversation`.

        It used to take the control document's write lock - a write the
        protected data root now refuses - so it must read without one.
        """
        data = self.project / ".harness"
        context = ProjectContext(self.project, data, Path(self._tmp.name) / "workspaces")
        session = control.create(context, "codex_delivery")
        control.record_cli_session(context, session["id"], "01a0fad0-0000-0000-0000-000000000000", "codex")
        transcript = data / "control" / "transcripts" / f"{session['id']}.log"
        transcript.parent.mkdir(parents=True, exist_ok=True)
        transcript.write_text("2026-10-02T04:12:44+00:00 -- supervisor started\n", encoding="utf-8")
        command = agent_confinement.wrap(
            [os.path.realpath(os.sys.executable), "-E", str(ROOT / "harness" / "control.py"),
             "--root", str(self.project), "--data-root", str(data),
             "--workspace-root", str(context.workspace_root), "conversation", "--session-id", session["id"]],
            [str(self.project)], store=Path(self._tmp.name) / "store", home=Path.home(),
            protected_writes=[str(data)])
        completed = subprocess.run(command, capture_output=True, text=True, timeout=30,
                                   env={**os.environ, "CODEX_HOME": str(Path(self._tmp.name) / "codex-home")})
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("supervisor started", completed.stdout)


class RunnerTests(unittest.TestCase):
    """The real runner in the product's launch mode (authenticated board surface)."""

    served = surface.BoardSurfaceAuthenticationTests.served
    bootstrap_served = surface.BoardSurfaceAuthenticationTests.bootstrap_served

    def setUp(self):
        require_loopback()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)

    def _project(self, scaffold: bool):
        code = self.base / "code"; code.mkdir()
        data = code / ".harness" if scaffold else self.base / "home" / "projects" / "p1" / "data"
        workspaces = self.base / "home" / "workspaces" / "p1"
        self.context = ProjectContext(code, data, workspaces)
        control.initialize(self.context)
        home = self.base / "home"; home.mkdir(exist_ok=True)
        project_registry.save(home, {"version": project_registry.REGISTRY_VERSION, "projects": [
            {"id": "p1", "name": "project", "code_root": str(code), "data_root": str(data),
             "workspace_root": str(workspaces)}]})
        return home

    def _run(self, kind: str, home: Path, environment: dict) -> subprocess.CompletedProcess:
        session = control.create(self.context, kind)
        authority = SessionTokenAuthority(self.context)
        authority.prepare(session["id"])
        with self.served(authority) as endpoint, self.bootstrap_served(authority, endpoint) as bootstrap:
            command = [
                "/bin/bash", str(RUNNER),
                "--root", str(self.context.code_root), "--data-root", str(self.context.data_root),
                "--workspace-root", str(self.context.workspace_root),
                "--python", os.path.realpath(os.sys.executable),
                "--session-id", session["id"], "--kind", session["kind"],
                "--manager-home", str(home), "--board-bootstrap", bootstrap,
            ]
            env = {**os.environ, "HARNESS_EXECUTION_ROOT": str(self.context.code_root), **environment}
            return subprocess.run(command, cwd=self.context.code_root, env=env,
                                  capture_output=True, text=True, timeout=60)

    def test_a_confined_claude_agent_cannot_write_the_board_store_of_a_scaffolded_project(self):
        if not platform_support.agent_confinement().available():
            raise unittest.SkipTest("no write-confinement primitive on this platform")
        home = self._project(scaffold=True)
        data = self.context.data_root
        probe = self.base / "fake-claude"
        probe.write_text(
            "#!/bin/sh\n"
            f"( echo x > '{self.context.code_root}/work.txt' && echo WROTE_PROJECT ) 2>/dev/null || echo DENIED_PROJECT\n"
            f"( echo x > '{self.context.workspace_root}/work.txt' && echo WROTE_WORKSPACE ) 2>/dev/null || echo DENIED_WORKSPACE\n"
            f"( echo '{{}}' > '{data}/board/state.json' && echo WROTE_BOARD ) 2>/dev/null || echo DENIED_BOARD\n"
            f"( echo '{{}}' > '{data}/control/session-token-verifiers.json' && echo WROTE_VERIFIERS ) 2>/dev/null || echo DENIED_VERIFIERS\n"
            f"( mkdir -p '{data}/tasks' && echo '{{}}' > '{data}/tasks/forged.json' && echo WROTE_CONTRACT ) 2>/dev/null || echo DENIED_CONTRACT\n"
            f"( mkdir -p '{self.context.code_root}/.harness-backups/e999999999' && echo WROTE_BOARD_BACKUP ) 2>/dev/null || echo DENIED_BOARD_BACKUP\n"
            f"( mkdir -p '{self.context.code_root}/.harness-memory-backups/forged' && echo WROTE_MEMORY_BACKUP ) 2>/dev/null || echo DENIED_MEMORY_BACKUP\n"
            f"( for f in '{data}'/control/agent-sandbox-*.sb; do echo '(allow default)' > \"$f\"; done && echo WROTE_PROFILE ) 2>/dev/null || echo DENIED_PROFILE\n",
            encoding="utf-8")
        probe.chmod(0o755)
        completed = self._run("claude_cto", home, {
            "HARNESS_CLAUDE_BIN": str(probe),
            "CLAUDE_CONFIG_DIR": str(self.base / "claude-config"),
        })
        out = completed.stdout + completed.stderr
        self.assertEqual(completed.returncode, 0, out)
        self.assertIn("WROTE_PROJECT", out)
        self.assertIn("WROTE_WORKSPACE", out)
        for refused in ("DENIED_BOARD", "DENIED_VERIFIERS", "DENIED_CONTRACT", "DENIED_PROFILE",
                        "DENIED_BOARD_BACKUP", "DENIED_MEMORY_BACKUP"):
            self.assertIn(refused, out)
        state = json.loads((data / "board" / "state.json").read_text(encoding="utf-8"))
        self.assertTrue(state.get("agents"), "the worker's registration survives; the agent's '{}' overwrite did not land")
        self.assertFalse((data / "tasks" / "forged.json").exists())

    def _codex_flags(self, scaffold: bool) -> list[str]:
        home = self._project(scaffold=scaffold)
        capture = self.base / "codex-argv.json"
        fake = self.base / "fake-codex"
        fake.write_text(
            "#!/usr/bin/env python3\nimport json, sys\n"
            f"json.dump(sys.argv[1:], open({str(capture)!r}, 'w'))\n", encoding="utf-8")
        fake.chmod(0o755)
        completed = self._run("codex_delivery", home, {"HARNESS_CODEX_BIN": str(fake),
                                                        "CODEX_HOME": str(self.base / "codex-home")})
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        argv = json.loads(capture.read_text(encoding="utf-8"))
        return [argv[index + 1] for index, item in enumerate(argv) if item == "-c"]

    def test_codex_on_an_adopted_project_keeps_workspace_write_without_the_data_root(self):
        flags = self._codex_flags(scaffold=False)
        self.assertIn("sandbox_mode=workspace-write", flags)
        roots = json.loads(next(f for f in flags if f.startswith("sandbox_workspace_write.writable_roots=")).split("=", 1)[1])
        self.assertNotIn(_real(self.context.data_root), roots)
        self.assertEqual(roots, [_real(self.context.code_root), _real(self.context.workspace_root)])
        self.assertIn("sandbox_workspace_write.network_access=true", flags)

    def test_codex_on_a_scaffolded_project_gets_a_profile_and_the_real_sandbox_refuses_the_board_store(self):
        flags = self._codex_flags(scaffold=True)
        self.assertNotIn("sandbox_mode=workspace-write", flags, "workspace-write cannot deny a subpath")
        self.assertIn('default_permissions="harness_agent"', flags)
        self.assertIn("permissions.harness_agent.network.enabled=true", flags)
        codex = shutil.which("codex")
        if not codex or not platform_support.agent_confinement().available():
            self.skipTest("the real Codex CLI is not installed here; flag shape asserted above")
        data = self.context.data_root
        script = (
            f"( echo x > '{self.context.code_root}/work.txt' && echo WROTE_PROJECT ) 2>/dev/null || echo DENIED_PROJECT; "
            f"( echo x > '{data}/board/state.json' && echo WROTE_BOARD ) 2>/dev/null || echo DENIED_BOARD; "
            f"( echo x > '{data}/control/session-token-verifiers.json' && echo WROTE_VERIFIERS ) 2>/dev/null || echo DENIED_VERIFIERS"
        )
        sandbox = [codex, "sandbox"]
        for flag in flags:
            if flag.startswith(("default_permissions", "permissions.")):
                sandbox += ["-c", flag]
        codex_home = self.base / "codex-sandbox-home"; codex_home.mkdir()
        completed = subprocess.run(sandbox + ["--", "/bin/sh", "-c", script], cwd=self.context.code_root,
                                   env={**os.environ, "CODEX_HOME": str(codex_home)},
                                   capture_output=True, text=True, timeout=60)
        out = completed.stdout + completed.stderr
        self.assertIn("WROTE_PROJECT", out)
        self.assertIn("DENIED_BOARD", out)
        self.assertIn("DENIED_VERIFIERS", out)


class ContractThroughTheBoardTests(unittest.TestCase):
    """The real gap the end-to-end run found: Delivery creates its Completion Contract.

    Before F-1 an agent ran `contract.py create`, which wrote harness storage
    directly. With that storage protected, the board does it: a real thin-client
    `board.py` call, authenticated, through the worker.
    """

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

    def _board(self, token, endpoint, *arguments):
        environment = {**os.environ, TOKEN_ENV: token, ENDPOINT_ENV: endpoint, PROTOCOL_ENV: PROTOCOL_VERSION}
        return subprocess.run(
            [os.path.realpath(os.sys.executable), "-E", str(ROOT / "harness" / "board.py"),
             "--root", str(self.context.code_root), *arguments],
            env=environment, capture_output=True, text=True, timeout=30)

    def test_delivery_creates_and_evidences_its_contract_through_the_board(self):
        session, agent, authority, token, _ = self.session()
        board.record_owner_direction(self.context, session["id"], "OWNER DIRECTION — a tiny greeter")
        board.begin_task(self.context, agent["id"], "cli-greeter")
        evidence = Path(self._tmp.name) / "agent-scratch" / "unittest.txt"
        evidence.parent.mkdir()
        evidence.write_text("Ran 2 tests in 0.001s\n\nOK\n", encoding="utf-8")
        with self.served(authority) as endpoint:
            created = self._board(token, endpoint, "create-contract", "--agent", agent["id"],
                                  "--objective", "A tiny greeter", "--deliverable", "greeting")
            self.assertEqual(created.returncode, 0, created.stderr)
            attached = self._board(token, endpoint, "contract-evidence", "--agent", agent["id"],
                                   "--deliverable", "greeting", "--evidence", str(evidence))
            self.assertEqual(attached.returncode, 0, attached.stderr)
        complete, problems, value = contract.contract_complete(self.context, "cli-greeter")
        self.assertTrue(complete, problems)
        stored = value["deliverables"][0]["evidence"][0]
        # The contract points at the worker's stored copy, not the agent's file:
        # editing the agent's file afterwards cannot change the evidence.
        self.assertTrue(stored["path"].startswith(str(self.context.data_root)), stored["path"])
        self.assertNotEqual(Path(stored["path"]).resolve(), evidence.resolve())
        self.assertEqual(Path(stored["path"]).read_text(encoding="utf-8"), evidence.read_text(encoding="utf-8"))
        kinds = [event["kind"] for event in board.snapshot(self.context)["events"]]
        self.assertIn("completion_contract_created", kinds)
        self.assertIn("completion_contract_evidence", kinds)

    def test_the_refusal_names_the_board_remedy_for_an_agent_on_older_rules(self):
        """Found live: an agent resumed on its pre-F-1 conversation kept the old
        rules, never saw `create-contract`, and stalled on "contract missing".
        The refusal itself must name the remedy, so in-flight agents recover."""
        session, agent, authority, token, _ = self.session()
        board.record_owner_direction(self.context, session["id"], "OWNER DIRECTION — a tiny greeter")
        board.begin_task(self.context, agent["id"], "cli-greeter")
        with self.served(authority) as endpoint:
            refused = self._board(token, endpoint, "define-plan", "--agent", agent["id"],
                                  "--mode", "atomic", "--rationale", "one cohesive change")
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("Completion Contract missing", refused.stderr)
        self.assertIn("create-contract", refused.stderr)

    def test_only_an_active_delivery_agent_may_create_a_contract(self):
        _, reviewer, authority, token, _ = self.session("claude_reviewer", "qa", "REVIEW_QUEUE")
        with self.served(authority) as endpoint:
            refused = self._board(token, endpoint, "create-contract", "--agent", reviewer["id"],
                                  "--objective", "x", "--deliverable", "y")
        self.assertNotEqual(refused.returncode, 0)
        self.assertFalse((self.context.data_root / "tasks" / "REVIEW_QUEUE.json").exists())

    def test_delivery_without_a_task_cannot_create_one(self):
        _, agent, authority, token, _ = self.session()
        with self.served(authority) as endpoint:
            refused = self._board(token, endpoint, "create-contract", "--agent", agent["id"],
                                  "--objective", "x", "--deliverable", "y")
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("only an active Delivery Agent", refused.stderr)


if __name__ == "__main__":
    unittest.main()
