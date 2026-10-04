# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Plumbing Stage 3, Codex, end to end through the real supervisor.

The supervisor runs in a real PTY. Its child is a stand-in Codex TUI that
attaches with `--remote unix://...` (the flag the supervisor adds to today's
launch line) and reports anything typed into its terminal. A harness message
must reach the app-server as a `turn/start` with its delivery id and be
acknowledged only on the receipt - never typed. Without receipts the session
goes back to typing after two tries and says so.
"""
from __future__ import annotations

import json
import os
import pty
import select
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from harness import board, control
from tests import stage3_stubs

ROOT = Path(__file__).resolve().parents[1]

FAKE_TUI = r'''#!PYTHON
import os, select, sys, tty
sys.path.insert(0, ROOT)
from tests.stage3_stubs import WebSocketClient
arguments = sys.argv[1:]
address = arguments[arguments.index("--remote") + 1]
tty.setraw(0)
tui = WebSocketClient(address[len("unix://"):])
tui.handshake()
tui.send({"id": "initialize", "method": "initialize", "params": {}})
tui.receive_until(lambda m: m.get("id") == "initialize")
tui.send({"method": "initialized"})
tui.send({"id": 1, "method": "thread/start", "params": {}})
tui.receive_until(lambda m: m.get("method") == "thread/started")
if os.environ.get("STUB_MODE") == "busy":
    # The agent's own long turn (it works, and waits on the board, inside it).
    tui.send({"id": 2, "method": "turn/start", "params": {"threadId": "th-1", "clientUserMessageId": "owner-turn",
              "input": [{"type": "text", "text": "work", "text_elements": []}]}})
    tui.receive_until(lambda m: m.get("method") == "turn/started")
os.write(1, b"TUI_READY\r\n")
tui.socket.settimeout(None)
typed = b""
while True:
    readable, _, _ = select.select([0, tui.socket], [], [], 0.2)
    if 0 in readable:
        data = os.read(0, 4096)
        typed += data
        if b"\r" in typed:
            os.write(1, b"TYPED:" + typed.replace(b"\r", b"<ENTER>") + b"\r\n")
            typed = b""
    if tui.socket in readable:
        message = tui.receive()
        item = (message.get("params") or {}).get("item") or {}
        if message.get("method") == "item/started" and item.get("type") == "userMessage":
            os.write(1, ("TURN:" + item.get("clientId", "") + "\r\n").encode())
'''


class Stage3SupervisorFixture(unittest.TestCase):
    READ_CEILING = 30
    steer = "consume"

    _extra: list = []

    def launch(self, mode: str, receipt_timeout: float = 5.0, *, wait_ready: bool = True):
        self._tmp = tempfile.TemporaryDirectory(dir="/tmp" if os.path.isdir("/tmp") else None, prefix="hn3s-")
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name)
        self.root = base / "p"
        self.root.mkdir()
        (base / "stub.py").write_text(stage3_stubs.APP_SERVER)
        tui = base / "codex"
        tui.write_text(FAKE_TUI.replace("#!PYTHON", "#!" + sys.executable)
                       .replace("sys.path.insert(0, ROOT)", f"sys.path.insert(0, {str(ROOT)!r})"))
        tui.chmod(0o755)
        self.server_log = base / "server.log"
        self.session = control.create(self.root, "codex_delivery")
        self.agent = board.register(self.root, "engineering", "AWAITING_OWNER_DIRECTION", vendor="OpenAI",
                                    session_id=self.session["id"])
        self.runtime = base / "rt"
        master, slave = pty.openpty()
        command = [
            sys.executable, str(ROOT / "harness" / "interactive_supervisor.py"), "--root", str(self.root),
            "--session-id", self.session["id"], "--agent-id", self.agent["id"], "--provider", "codex",
            "--codex-app-server-json", json.dumps([sys.executable, str(base / "stub.py")]),
            "--runtime-dir", str(self.runtime), "--receipt-timeout", str(receipt_timeout), "--app-server-timeout", "5",
            *self._extra, "--", str(tui),
        ]
        environment = {**os.environ, "STUB_MODE": mode, "STUB_STEER": self.steer, "STUB_LOG": str(self.server_log)}
        process = subprocess.Popen(command, stdin=slave, stdout=slave, stderr=slave, close_fds=True, env=environment)
        os.close(slave)
        self.addCleanup(self._stop, process, master)
        self.output = b""
        if wait_ready:
            self.read_until(master, b"TUI_READY")
        return process, master

    def launch_expecting_stop(self, mode: str):
        process, master = self.launch(mode, wait_ready=False)
        deadline = time.monotonic() + self.READ_CEILING
        while process.poll() is None and time.monotonic() < deadline:
            if select.select([master], [], [], .1)[0]:
                try:
                    self.output += os.read(master, 65536)
                except OSError:
                    break
        self.assertIsNotNone(process.poll(), "the session stopped")
        return process, master

    def _stop(self, process, master):
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
        os.close(master)

    def read_until(self, master, needle):
        deadline = time.monotonic() + self.READ_CEILING
        while time.monotonic() < deadline:
            readable, _, _ = select.select([master], [], [], .1)
            if master in readable:
                try:
                    self.output += os.read(master, 65536)
                except OSError:
                    break
            if needle in self.output:
                return self.output
        self.fail(f"did not see {needle!r}; got {self.output[-800:]!r}")

    def wait_for(self, predicate):
        deadline = time.monotonic() + self.READ_CEILING
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(.05)
        self.fail("timed out")


class Stage3SupervisorTests(Stage3SupervisorFixture):
    def test_a_harness_message_arrives_as_a_turn_with_a_receipt_and_is_never_typed(self):
        process, master = self.launch("normal")
        queued = control.enqueue_instruction(self.root, self.session["id"], "Poll the board now.", "test-controller")
        self.read_until(master, f"TURN:harness-{queued['id']}".encode())
        self.wait_for(lambda: control.instruction_receipt(self.root, queued["id"])["status"] == "delivered")
        starts = [json.loads(line) for line in self.server_log.read_text().splitlines() if '"turn/start"' in line]
        self.assertEqual(len(starts), 1)
        text = starts[0]["params"]["input"][0]["text"]
        self.assertEqual(text, "[SYSTEM CONTROL — test-controller] Poll the board now.", "today's label, unchanged")
        self.assertNotIn(b"TYPED:", self.output, "nothing was typed into the terminal")
        self.assertEqual(control.cli_session(self.root, self.session["id"]).get("cli_session_id"), "th-1")
        self.assertEqual(os.stat(self.runtime).st_mode & 0o777, 0o700)

    def test_without_receipts_the_session_goes_back_to_typing_and_says_so(self):
        process, master = self.launch("no_receipt", receipt_timeout=1.0)
        queued = control.enqueue_instruction(self.root, self.session["id"], "Poll the board now.", "test-controller")
        output = self.read_until(master, b"TYPED:")
        self.assertIn(b"SYSTEM CONTROL", output)
        self.wait_for(lambda: control.instruction_receipt(self.root, queued["id"])["status"] == "delivered")
        events = [event for event in board.snapshot(self.root)["events"] if event["kind"] == "plumbing_fallback"]
        self.assertEqual(len(events), 1, "the fallback is a board event the CTO sees")
        starts = [line for line in self.server_log.read_text().splitlines() if '"turn/start"' in line]
        self.assertEqual(len(starts), 2, "two tries, then typing")

    def test_the_runtime_directory_is_removed_when_the_session_ends(self):
        process, master = self.launch("normal")
        self.assertTrue(self.runtime.is_dir())
        process.terminate()
        process.wait(timeout=10)
        self.assertFalse(self.runtime.exists())


if __name__ == "__main__":
    unittest.main()


class RunnerTests(unittest.TestCase):
    """The real runner under a real terminal: Stage 3 only when switched on and proven."""

    from tests import test_board_surface as _surface
    served = _surface.BoardSurfaceAuthenticationTests.served
    bootstrap_served = _surface.BoardSurfaceAuthenticationTests.bootstrap_served

    def setUp(self):
        from harness import global_settings, project_registry
        from harness.project_context import ProjectContext
        from tests.environment_support import require_loopback
        require_loopback()
        self._tmp = tempfile.TemporaryDirectory(dir="/tmp" if os.path.isdir("/tmp") else None, prefix="hn3r-")
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(os.path.realpath(self._tmp.name))
        code = self.base / "code"; code.mkdir()
        # A manager home OUTSIDE temp space, as in production; the runtime
        # directory under a temp-space home is refused by design.
        from tests.environment_support import home_outside_temp_space
        self.home = home_outside_temp_space(self, ".hn3home-")
        self.context = ProjectContext(code, self.home / "projects" / "p1" / "data", self.home / "projects" / "p1" / "workspaces")
        control.initialize(self.context)
        project_registry.save(self.home, {"version": project_registry.REGISTRY_VERSION, "projects": [
            {"id": "p1", "name": "project", "code_root": str(code), "data_root": str(self.context.data_root),
             "workspace_root": str(self.context.workspace_root)}]})
        self.capture = self.base / "argv.jsonl"
        self.global_settings = global_settings

    def fake_codex(self) -> Path:
        path = self.base / "fake-codex"
        path.write_text("#!/usr/bin/env python3\nimport json, sys\n"
                        f"open({str(self.capture)!r}, 'a').write(json.dumps(sys.argv[1:]) + '\\n')\n", encoding="utf-8")
        path.chmod(0o755)
        return path

    def launch(self, on: bool) -> list[list[str]]:
        from harness import cli_capabilities
        from harness.board_surface import SessionTokenAuthority
        environment = {"HARNESS_CODEX_BIN": str(self.fake_codex()), "CODEX_HOME": str(self.base / "codex-home")}
        settings = self.global_settings.load(self.home)
        settings["plumbing"]["stage1_hooks_enabled"] = on
        settings["plumbing"]["stage3_codex_app_server_enabled"] = on
        self.global_settings._write(self.home, settings)
        source = {**os.environ, **environment}
        identity = cli_capabilities.binary_identity("codex", source_environment=source)
        needed = cli_capabilities.STAGE_REQUIREMENTS["stage3_codex_app_server"]["codex"]
        live = [name for name in needed if name not in cli_capabilities.STATIC_ITEMS["codex"]]
        path = cli_capabilities.cache_path(self.home, identity)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"identity": identity, "probed_at": "test", "probe_version": cli_capabilities.PROBE_VERSION,
                                    "static": {name: True for name in needed if name not in live}}), encoding="utf-8")
        cli_capabilities.record_live(self.home, "codex", {name: True for name in live}, auth_mode="test", source_environment=source)
        session = control.create(self.context, "codex_delivery")
        authority = SessionTokenAuthority(self.context)
        authority.prepare(session["id"])
        master, slave = pty.openpty()
        with self.served(authority) as endpoint, self.bootstrap_served(authority, endpoint) as bootstrap:
            process = subprocess.Popen([
                "/bin/bash", str(ROOT / "scripts" / "run_managed_agent.sh"), "--root", str(self.context.code_root),
                "--data-root", str(self.context.data_root), "--workspace-root", str(self.context.workspace_root),
                "--python", os.path.realpath(sys.executable), "--session-id", session["id"], "--kind", "codex_delivery",
                "--manager-home", str(self.home), "--board-bootstrap", bootstrap,
            ], cwd=self.context.code_root, stdin=slave, stdout=slave, stderr=slave, close_fds=True,
                env={**os.environ, "HARNESS_EXECUTION_ROOT": str(self.context.code_root), **environment})
            os.close(slave)
            output = b""
            deadline = time.monotonic() + 60
            while process.poll() is None and time.monotonic() < deadline:
                if select.select([master], [], [], .1)[0]:
                    try:
                        output += os.read(master, 65536)
                    except OSError:
                        break
            if process.poll() is None:
                process.kill()
            os.close(master)
        self.assertEqual(process.wait(timeout=10), 0, output[-1500:])
        launches = [json.loads(line) for line in self.capture.read_text().splitlines()]
        return [argv for argv in launches if argv != ["--version"]]   # version probes, not launches

    def test_switched_on_and_proven_the_tui_attaches_to_a_private_stdio_app_server(self):
        launches = self.launch(on=True)
        server = next(argv for argv in launches if argv[:1] == ["app-server"])
        tui = next(argv for argv in launches if argv[:1] != ["app-server"])
        self.assertEqual(server[:3], ["app-server", "--listen", "stdio://"], "stdio: no socket for any agent to reach")
        self.assertIn("approval_policy=never", server)
        self.assertIn("sandbox_mode=workspace-write", server, "the same sandbox flags as today's launch")
        self.assertEqual(tui[0], "--remote")
        self.assertTrue(tui[1].startswith(f"unix://{os.path.realpath(self.home)}/rt/"), tui[1])
        # The owner's first launch died on overrides in this line ("... not
        # supported with --remote"): the TUI carries none; the server carries all.
        self.assertEqual(tui[2:4], ["--cd", str(self.context.code_root)])
        self.assertNotIn("-c", tui)
        self.assertNotIn("--model", tui)
        self.assertTrue(any(flag.startswith("model=") for flag in server), "the model is set on the server")


    def test_switched_off_the_launch_is_todays(self):
        launches = self.launch(on=False)
        self.assertEqual(len(launches), 1, "no app-server")
        self.assertNotIn("--remote", launches[0])
        self.assertEqual(launches[0][0], "--cd")


class LaunchLineTests(unittest.TestCase):
    """The owner's first Stage 3 launch: "Error: ... overrides are not supported with --remote"."""

    def test_the_remote_tui_line_carries_no_override_fresh_or_resume(self):
        from harness.interactive_supervisor import _remote_tui_command
        flags = ["-c", "model_reasoning_effort=high", "-c", "approval_policy=never", "-c", "sandbox_mode=workspace-write",
                 "-c", 'sandbox_workspace_write.writable_roots=["/a"]', "-c", 'default_permissions="harness_agent"']
        fresh = _remote_tui_command(["codex", "--cd", "/p", "--model", "gpt-5", *flags, "PROMPT"], "/rt/t.sock")
        self.assertEqual(fresh, ["codex", "--remote", "unix:///rt/t.sock", "--cd", "/p", "PROMPT"])
        resume = _remote_tui_command(["codex", "resume", "--model", "gpt-5", *flags, "SESSION", "PROMPT"], "/rt/t.sock")
        self.assertEqual(resume, ["codex", "resume", "--remote", "unix:///rt/t.sock", "SESSION", "PROMPT"])

    def test_the_terminal_cannot_override_the_sessions_policy(self):
        from harness.codex_app_server import pin_policy
        start = pin_policy({"id": 1, "method": "thread/start", "params": {
            "cwd": "/p", "approvalPolicy": "on-request", "sandbox": "danger-full-access", "model": "m",
            "config": {"sandbox_mode": "danger-full-access", "approval_policy": "untrusted", "model_reasoning_effort": "low"}}})
        self.assertEqual(start["params"], {"cwd": "/p", "model": "m", "config": {"model_reasoning_effort": "low"}})
        turn = pin_policy({"id": 2, "method": "turn/start", "params": {
            "threadId": "t", "input": [], "sandboxPolicy": {"type": "dangerFullAccess"}, "approvalPolicy": "never", "cwd": "/"}})
        self.assertEqual(turn["params"], {"threadId": "t", "input": []})

    def test_the_threads_reported_policy_must_be_the_sessions(self):
        from harness.codex_app_server import policy_mismatch
        expected = {"approvalPolicy": "never", "writableRoots": ["/w", "/x"], "networkAccess": True}
        good = {"approvalPolicy": "never", "cwd": "/w", "sandbox": {"type": "workspaceWrite", "writableRoots": ["/x"], "networkAccess": True}}
        self.assertEqual(policy_mismatch(expected, good), "", "the cwd is writable implicitly")
        self.assertIn("approval", policy_mismatch(expected, {**good, "approvalPolicy": "on-request"}))
        self.assertIn("sandbox", policy_mismatch(expected, {**good, "sandbox": {"type": "dangerFullAccess"}}))
        self.assertIn("/x", policy_mismatch(expected, {**good, "sandbox": {**good["sandbox"], "writableRoots": []}}))
        profile = {"approvalPolicy": "never", "profile": "harness_agent"}
        self.assertEqual(policy_mismatch(profile, {"approvalPolicy": "never", "activePermissionProfile": {"id": "harness_agent"}}), "")
        self.assertIn("profile", policy_mismatch(profile, {"approvalPolicy": "never", "activePermissionProfile": None}))

    def test_the_gate_runs_the_real_line_and_refuses_the_old_one(self):
        import shutil as _shutil
        from unittest import mock
        from harness import cli_capabilities, global_settings, interactive_supervisor
        codex = global_settings.resolved_cli("codex").get("path", "")
        if not codex or not _shutil.which(codex):
            self.skipTest("codex CLI not installed")
        def scratch_dir() -> Path:
            # A killed Codex can still be writing its plugin cache for a moment.
            path = Path(tempfile.mkdtemp(prefix="hn3gate-"))
            self.addCleanup(_shutil.rmtree, path, True)
            return path
        if True:
            scratch = scratch_dir()
            environment = cli_capabilities._scratch_environment(codex, scratch)
            now = cli_capabilities._probe_codex_stage3(codex, scratch, environment)
            self.assertEqual(now, {"codex.remote_tui_launch_line": True, "codex.server_policy_applies": True})

            def old_line(command, socket_path):                 # 81250ab: overrides kept on the TUI
                position = 2 if command[1:2] == ["resume"] else 1
                return command[:position] + ["--remote", f"unix://{socket_path}"] + command[position:]
            other = scratch_dir()
            with mock.patch.object(interactive_supervisor, "_remote_tui_command", old_line):
                old = cli_capabilities._probe_codex_stage3(codex, other, cli_capabilities._scratch_environment(codex, other))
            self.assertFalse(old["codex.remote_tui_launch_line"], "the line that killed the owner's launch is refused by the gate")


class PolicyMismatchTests(Stage3SupervisorTests):
    def test_a_thread_with_a_looser_policy_stops_the_session_and_its_next_launch_types(self):
        from harness import interactive_supervisor
        self._extra = ["--codex-expected-policy", json.dumps({"approvalPolicy": "never", "writableRoots": ["/w2"], "networkAccess": True})]
        process, master = self.launch_expecting_stop("loose_policy")
        events = [event for event in board.snapshot(self.root)["events"] if event["kind"] == "plumbing_policy_mismatch"]
        self.assertEqual(len(events), 1)
        self.assertIn("approval policy 'on-request'", events[0]["message"])
        self.assertTrue(interactive_supervisor.stage3_refused_marker(self.root, self.session["id"]).is_file())


class RuntimeDirectoryCleanupTests(unittest.TestCase):
    """Review r1: a stale runtime directory outlived its session."""

    def test_a_channel_that_never_started_still_leaves_no_runtime_directory(self):
        base = Path(tempfile.mkdtemp(dir="/tmp" if os.path.isdir("/tmp") else None, prefix="hn3d-"))
        self.addCleanup(__import__("shutil").rmtree, base, True)
        root = base / "p"; root.mkdir()
        session = control.create(root, "codex_delivery")
        agent = board.register(root, "engineering", "AWAITING_OWNER_DIRECTION", vendor="OpenAI", session_id=session["id"])
        runtime = base / "rt"
        master, slave = pty.openpty()
        command = [sys.executable, str(ROOT / "harness" / "interactive_supervisor.py"), "--root", str(root),
                   "--session-id", session["id"], "--agent-id", agent["id"], "--provider", "codex",
                   "--codex-app-server-json", json.dumps([str(base / "no-such-codex"), "app-server"]),
                   "--runtime-dir", str(runtime), "--", sys.executable, "-c", "import time; print('UP', flush=True); time.sleep(30)"]
        process = subprocess.Popen(command, stdin=slave, stdout=slave, stderr=slave, close_fds=True)
        os.close(slave)
        output = b""
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and b"UP" not in output:
            if select.select([master], [], [], .1)[0]:
                output += os.read(master, 65536)
        self.assertTrue(runtime.is_dir(), "created before the channel failed")
        process.terminate()
        process.wait(timeout=15)
        os.close(master)
        self.assertFalse(runtime.exists())
