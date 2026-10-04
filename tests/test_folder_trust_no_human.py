# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""No agent waits for a person to trust its project folder.

Owner's order, 2026-10-03 21:10: "a human cannot be available any time, all
the time." The Stage 3 round-3 review stopped on a resumed Claude QA agent
asking "Yes, I trust this folder" - in every new project, after an agent's
`git init`, Claude's own trust check stops at the git root and finds nothing.
Opening a project in the app is the owner's trust decision: Codex gets it as
the project's trust entry on every open (already so, measured below); Claude
now gets it as CLAUDE_CODE_SANDBOXED, which is true - the launch line wraps
every Claude agent in the write confinement or refuses it.

The real-binary cases use scratch homes, a dummy key and a local stub API:
no sign-in, no model turn, nothing outside the test's own folder.
"""
from __future__ import annotations

import fcntl
import json
import os
import pty
import re
import select
import shutil
import struct
import subprocess
import tempfile
import termios
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from harness import codex_app_server, control, global_settings, workspace_settings
from harness.interactive_supervisor import _remote_tui_command
from tests.test_stage4_system_layer import Stage4RunnerTests, stage_relaunch

TERMINAL_REPLIES = ((b"\x1b[6n", b"\x1b[1;1R"), (b"\x1b[c", b"\x1b[?62;22c"), (b"\x1b[?u", b"\x1b[?0u"),
                    (b"\x1b]10;?", b"\x1b]10;rgb:ffff/ffff/ffff\x1b\\"), (b"\x1b]11;?", b"\x1b]11;rgb:0000/0000/0000\x1b\\"))
TRUST_QUESTION = re.compile(r"trust\s*this\s*folder|Trust\s*and\s*continue|Do you trust", re.IGNORECASE)


def visible_screen(argv: list[str], environment: dict[str, str], cwd: str, seconds: float = 12) -> str:
    """What a person would see in the terminal in the first seconds."""
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 140, 0, 0))
    process = subprocess.Popen(argv, stdin=slave, stdout=slave, stderr=slave, env=environment, cwd=cwd, start_new_session=True)
    os.close(slave)
    screen = b""
    try:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline and process.poll() is None:
            if select.select([master], [], [], 0.2)[0]:
                try:
                    chunk = os.read(master, 65536)
                except OSError:
                    break
                screen += chunk
                for query, reply in TERMINAL_REPLIES:
                    if query in chunk:
                        os.write(master, reply)
    finally:
        codex_app_server.end_process_group(process)
        os.close(master)
    text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)", "", screen.decode("utf-8", "replace"))
    return re.sub(r"\s+", " ", text)


def fresh_git_project(parent: Path) -> Path:
    project = parent / "projects" / "fresh-project"
    project.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(project)], check=True)
    return project


class LaunchLineTests(unittest.TestCase):
    """The launch script tells every confined Claude agent it runs sandboxed."""
    served = Stage4RunnerTests.served
    bootstrap_served = Stage4RunnerTests.bootstrap_served
    setUp = Stage4RunnerTests.setUp
    switch = Stage4RunnerTests.switch
    launch = Stage4RunnerTests.launch
    require_confinement = Stage4RunnerTests.require_confinement

    def fake(self, name: str) -> Path:
        path = self.base / name
        path.write_text("#!/usr/bin/env python3\nimport json, os, sys\n"
                        f"open({str(self.capture)!r}, 'a').write(json.dumps(sys.argv[1:]) + '\\n')\n"
                        "if sys.argv[1:] != ['--version']:\n"
                        f"    open({str(self.capture) + '.env'!r}, 'a').write(os.environ.get('CLAUDE_CODE_SANDBOXED', '<unset>') + '\\n')\n",
                        encoding="utf-8")
        path.chmod(0o755)
        return path

    def sandboxed_values(self) -> list[str]:
        values = Path(str(self.capture) + ".env").read_text().split()
        Path(str(self.capture) + ".env").unlink()
        return values

    def test_claude_fresh_and_resumed_both_launch_trusted(self):
        self.require_confinement()
        self.switch("claude", stage4=False)
        session = control.create(self.context, "claude_reviewer")
        argv = self.launch(session, "claude_reviewer")
        self.assertEqual(self.sandboxed_values(), ["1"], "a fresh agent never stops on the folder-trust question")
        minted = argv[argv.index("--session-id") + 1]
        store = self.claude_dir / "projects" / "-code"
        store.mkdir(parents=True)
        (store / f"{minted}.jsonl").write_text("{}\n", encoding="utf-8")
        stage_relaunch(self.context, session["id"])
        argv = self.launch(session, "claude_reviewer")
        self.assertIn("--resume", argv)
        self.assertEqual(self.sandboxed_values(), ["1"], "round 3's blocker: the RESUMED QA agent asked")


class _StubAPI(BaseHTTPRequestHandler):
    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", "0")))
        self.send_response(500)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"{}")

    do_GET = do_POST

    def log_message(self, *_):
        return


class RealClaudeTests(unittest.TestCase):
    """Claude Code 2.1.x: a fresh git project under a trusted parent asks; told it is sandboxed, it does not."""

    def setUp(self):
        from tests.environment_support import require_loopback
        require_loopback()
        self.claude = global_settings.resolved_cli("claude").get("path", "")
        if not self.claude or not shutil.which(self.claude):
            self.skipTest("claude CLI not installed")
        self.scratch = Path(tempfile.mkdtemp(prefix="hntrust-"))
        self.addCleanup(shutil.rmtree, self.scratch, True)
        self.api = ThreadingHTTPServer(("127.0.0.1", 0), _StubAPI)
        threading.Thread(target=self.api.serve_forever, daemon=True).start()
        self.addCleanup(self.api.server_close)
        self.addCleanup(self.api.shutdown)

    def screen(self, sandboxed: bool) -> str:
        case = self.scratch / ("sandboxed" if sandboxed else "plain")
        config = case / "claude-config"
        config.mkdir(parents=True)
        project = fresh_git_project(case)
        key = "sk-ant-api03-" + "x" * 80
        # The parent is trusted, as ~/Projects is for the owner; the new repository under it is not.
        (config / ".claude.json").write_text(json.dumps({
            "hasCompletedOnboarding": True, "bypassPermissionsModeAccepted": True, "skipDangerousModePermissionPrompt": True,
            "theme": "dark", "customApiKeyResponses": {"approved": [key[-20:]], "rejected": []},
            "projects": {os.path.realpath(project.parent): {"hasTrustDialogAccepted": True}}}), encoding="utf-8")
        environment = {name: value for name, value in os.environ.items() if not name.startswith(("CLAUDE", "ANTHROPIC"))}
        environment.update(CLAUDE_CONFIG_DIR=str(config), ANTHROPIC_API_KEY=key, TERM="xterm-256color",
                           ANTHROPIC_BASE_URL=f"http://127.0.0.1:{self.api.server_address[1]}")
        if sandboxed:
            environment["CLAUDE_CODE_SANDBOXED"] = "1"
        return visible_screen([self.claude, "--permission-mode", "bypassPermissions"], environment, str(project))

    def test_the_trust_question_appears_without_it_and_never_with_it(self):
        self.assertRegex(self.screen(False), TRUST_QUESTION, "the measured failure: a fresh git project asks")
        launched = self.screen(True)
        self.assertNotRegex(launched, TRUST_QUESTION)
        # Redraws can drop the spaces once escapes are stripped (2.1.289: "bypasspermissionson").
        self.assertRegex(launched, r"bypass\s*permissions\s*on", "the agent reaches its prompt")


class RealCodexTests(unittest.TestCase):
    """Codex 0.160: the trust entry the app writes on every project open covers both launch paths."""

    def setUp(self):
        self.codex = global_settings.resolved_cli("codex").get("path", "")
        if not self.codex or not shutil.which(self.codex):
            self.skipTest("codex CLI not installed")
        self.scratch = Path(tempfile.mkdtemp(prefix="hncodextrust-"))
        self.addCleanup(shutil.rmtree, self.scratch, True)
        self.flags = ["-c", 'model_provider="stub"', "-c", 'model="gpt-5"',
                      "-c", 'model_providers.stub={name="stub", base_url="http://127.0.0.1:9/v1", wire_api="responses", requires_openai_auth=false}',
                      "-c", "approval_policy=never", "-c", "sandbox_mode=workspace-write"]

    def screen(self, *, opened: bool, remote: bool) -> str:
        case = self.scratch / f"{'opened' if opened else 'new'}-{'remote' if remote else 'typing'}"
        home = case / "codex-home"
        home.mkdir(parents=True)
        project = fresh_git_project(case)
        if opened:
            workspace_settings.apply_provider_files({"workspace_root": str(project), "codex": {"config_path": str(home / "config.toml")}}, "codex")
        environment = {**os.environ, "CODEX_HOME": str(home), "TERM": "xterm-256color"}
        today = [self.codex, "--cd", str(project), *self.flags]
        if not remote:
            return visible_screen(today, environment, str(project))
        runtime = Path(tempfile.mkdtemp(prefix="hnrt-", dir="/tmp" if os.path.isdir("/tmp") else None))
        self.addCleanup(shutil.rmtree, runtime, True)
        mux = codex_app_server.CodexMultiplexer(
            str(runtime / "t.sock"), [self.codex, "app-server", "--listen", "stdio://", *self.flags],
            environment=environment, cwd=str(project), admit=lambda pid: True, peer_pid=lambda connection: 1,
            expected_policy={"approvalPolicy": "never", "writableRoots": [], "networkAccess": False})
        mux.start()
        try:
            return visible_screen(_remote_tui_command(today, mux.socket_path), environment, str(project))
        finally:
            mux.stop()

    def test_an_opened_project_never_asks_on_either_path(self):
        for remote in (False, True):
            with self.subTest(path="stage3 --remote" if remote else "typing"):
                self.assertRegex(self.screen(opened=False, remote=remote), TRUST_QUESTION, "a never-opened folder asks")
                self.assertNotRegex(self.screen(opened=True, remote=remote), TRUST_QUESTION)


if __name__ == "__main__":
    unittest.main()
