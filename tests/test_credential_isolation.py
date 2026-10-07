# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""2026-09-26 incident: a delivery copied the owner's Claude login file into
temporary config folders to run the CLI "isolated"; the copies shared one
session and broke every Claude login on the machine.

These tests prove, by execution and against a FIXTURE home (never the owner's
real login), that:
- a managed Claude agent on macOS cannot read or copy the login file, while
  its own login keeps working (it authenticates through the Keychain service);
- a ledger command the harness runs in its worker cannot read or copy it, and
  loses nothing else;
- Linux keeps the agent's own login readable (no Keychain there) and masks it
  for harness-run commands.
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from harness import agent_confinement, certified_execution
from harness.platform_support import defaults, linux

FIXTURE_LOGIN = '{"claudeAiOauth":{"accessToken":"FIXTURE-NOT-A-REAL-TOKEN"}}'


def _fixture_home(root: Path) -> Path:
    home = root / "home"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / ".credentials.json").write_text(FIXTURE_LOGIN, encoding="utf-8")
    return home


class ProfileShapeTests(unittest.TestCase):
    def test_the_macos_agent_profile_denies_shared_login_and_keychain(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = _fixture_home(Path(tmp))
            seatbelt = defaults._AgentConfinement()
            protected = seatbelt.protected_read_paths(home, str(Path(tmp) / "relocated"))
            profile = seatbelt.profile(["/Users/owner/project"], protected)
            login = (home / ".claude" / ".credentials.json").resolve()
            self.assertIn(f'(deny file-read* (literal "{login}")', profile)
            self.assertIn(str((Path(tmp) / "relocated" / ".credentials.json").resolve()), profile,
                          "a relocated CLAUDE_CONFIG_DIR's login is protected too")
            self.assertIn("Keychains", profile, "setup-token agents must not read the shared Keychain")
            self.assertIn("(deny mach-lookup", profile)
            self.assertIn("(deny file-write*)", profile, "the write boundary is unchanged")

    def test_linux_keeps_the_agents_own_login_and_masks_it_for_harness_commands(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = _fixture_home(Path(tmp))
            bubblewrap = linux._BwrapAgentConfinement(which=lambda name: "/usr/bin/bwrap")
            with patch.dict(os.environ, {"HARNESS_BWRAP_BIN": ""}):
                self.assertEqual(bubblewrap.protected_read_paths(home), [str(home / ".claude" / ".credentials.json")],
                                 "setup-token agents must not refresh the shared Linux login")
                command = agent_confinement.read_guard(
                    ["/bin/sh", "-c", "true"], home=home, store=Path(tmp) / "store", implementation=bubblewrap)
            login = str(home / ".claude" / ".credentials.json")
            index = command.index("--ro-bind")
            self.assertEqual(command[index + 1:index + 3], ["/dev/null", login])
            self.assertEqual(command[command.index("--bind") + 1:command.index("--bind") + 3], ["/", "/"],
                             "everything else stays as writable as before")

    def test_linux_without_bubblewrap_runs_the_command_as_before(self):
        bubblewrap = linux._BwrapAgentConfinement(which=lambda name: None)
        with patch.dict(os.environ, {"HARNESS_BWRAP_BIN": ""}):
            self.assertEqual(bubblewrap.read_guard(["/bin/true"], [], store=Path("/nonexistent")), ["/bin/true"])


@unittest.skipUnless(sys.platform == "darwin" and Path("/usr/bin/sandbox-exec").is_file(), "macOS Seatbelt only")
class MacosExecutionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.home = _fixture_home(self.root)
        self.login = self.home / ".claude" / ".credentials.json"
        self.granted = self.root / "granted"
        self.granted.mkdir()

    def probe(self) -> str:
        return (
            f"cat '{self.login}' >/dev/null 2>&1 && echo LOGIN_READ || echo LOGIN_DENIED; "
            f"mkdir -p '{self.granted}/copy'; "
            f"cp '{self.login}' '{self.granted}/copy/' 2>/dev/null && echo COPY_MADE || echo COPY_DENIED; "
            f"echo x > '{self.granted}/w.txt' && echo WRITE_OK"
        )

    def test_a_managed_claude_agent_cannot_read_or_copy_the_login_file(self):
        import subprocess
        command = agent_confinement.wrap(
            ["/bin/sh", "-c", self.probe()], [str(self.granted)], store=self.root / "store", home=self.home)
        completed = subprocess.run(command, capture_output=True, text=True, timeout=30)
        lines = completed.stdout.split()
        self.assertIn("LOGIN_DENIED", lines, completed.stdout + completed.stderr)
        self.assertIn("COPY_DENIED", lines)
        self.assertIn("WRITE_OK", lines, "the agent's own grant still works")
        self.assertFalse((self.granted / "copy" / ".credentials.json").exists())

    def test_a_ledger_command_in_the_worker_cannot_read_or_copy_the_login_file(self):
        with patch.object(certified_execution, "_owner_home", return_value=self.home):
            code, output, _ = certified_execution._run_observed(self.probe(), self.root, dict(os.environ), 30)
        lines = output.split()
        self.assertEqual(code, 0, output)
        self.assertIn("LOGIN_DENIED", lines, output)
        self.assertIn("COPY_DENIED", lines)
        self.assertIn("WRITE_OK", lines, "the command loses nothing but the login")
        self.assertFalse((self.granted / "copy" / ".credentials.json").exists())

    def test_the_ledger_guard_also_covers_a_relocated_config_folder(self):
        relocated = self.root / "relocated"
        relocated.mkdir()
        (relocated / ".credentials.json").write_text(FIXTURE_LOGIN, encoding="utf-8")
        environment = {**os.environ, "CLAUDE_CONFIG_DIR": str(relocated)}
        with patch.object(certified_execution, "_owner_home", return_value=self.home):
            _, output, _ = certified_execution._run_observed(
                f"cat '{relocated}/.credentials.json' >/dev/null 2>&1 && echo READ || echo DENIED",
                self.root, environment, 30)
        self.assertIn("DENIED", output.split(), output)

    def test_the_guard_refuses_rather_than_runs_open_without_sandbox_exec(self):
        seatbelt = defaults._AgentConfinement()
        with patch.object(defaults._AgentConfinement, "available", return_value=False):
            with self.assertRaises(defaults.AgentConfinementUnavailable):
                seatbelt.read_guard(["/bin/true"], [str(self.login)], store=self.root / "store")


if __name__ == "__main__":
    unittest.main()


class SessionSweepTests(unittest.TestCase):
    """A managed session's leftover processes are stopped when it ends; nothing else is."""

    def setUp(self):
        import subprocess
        self.subprocess = subprocess
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.spawned: list = []
        self.addCleanup(self._reap)

    def _reap(self):
        for process in self.spawned:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)

    def detached(self, marker: str | None) -> "object":
        """A long-lived process in its own session, as an agent's escaped CLI probe is."""
        environment = {key: value for key, value in os.environ.items() if key != "HARNESS_MANAGED_SESSION"}
        if marker is not None:
            environment["HARNESS_MANAGED_SESSION"] = marker
        # A non-platform program, like the `claude` CLI: macOS hides the
        # environment of Apple platform binaries (/bin/sh, /bin/sleep) from
        # `ps`, so a survivor built on one would "survive" trivially.
        process = self.subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(300)"], env=environment, start_new_session=True)
        self.spawned.append(process)
        return process

    def test_an_ended_session_stops_what_it_left_behind_and_nothing_else(self):
        from harness import control
        session = control.create(self.root, "codex_delivery")
        stand_in = self.subprocess.Popen(["/bin/sleep", "300"], start_new_session=True)
        self.spawned.append(stand_in)
        control.attach(self.root, session["id"], stand_in.pid)
        leftover = self.detached(session["id"])
        other_session = self.detached("codex_delivery-some-other-session")
        prefix_collision = self.detached(session["id"] + "0")
        owners_own = self.detached(None)
        stand_in.kill(); stand_in.wait(timeout=5)  # the agent's terminal ends

        ended = next(item for item in control.snapshot(self.root)["sessions"] if item["id"] == session["id"])

        self.assertNotIn(ended["status"], control.ACTIVE_STATUSES)
        self.assertEqual(ended.get("swept_descendants"), [leftover.pid])
        self.assertIsNotNone(leftover.wait(timeout=5), "the leftover process was stopped")
        for survivor in (other_session, prefix_collision, owners_own):
            self.assertIsNone(survivor.poll(), "only this session's marker is swept")

    def test_a_session_that_left_nothing_behind_records_no_sweep(self):
        from harness import control
        session = control.create(self.root, "claude_reviewer")
        stand_in = self.subprocess.Popen(["/bin/sleep", "300"], start_new_session=True)
        self.spawned.append(stand_in)
        control.attach(self.root, session["id"], stand_in.pid)
        stand_in.kill(); stand_in.wait(timeout=5)
        ended = next(item for item in control.snapshot(self.root)["sessions"] if item["id"] == session["id"])
        self.assertNotIn("swept_descendants", ended)

    def test_the_launcher_exports_the_marker_before_the_cli_starts(self):
        script = (Path(__file__).resolve().parents[1] / "scripts" / "run_managed_agent.sh").read_text(encoding="utf-8")
        export = script.index('export HARNESS_MANAGED_SESSION="$session_id"')
        self.assertLess(export, script.index("launch_visible_cli()"), "exported before any CLI can be launched")


def _versioned_fake(path: Path, version: str) -> Path:
    """A fake `claude` that answers --version and otherwise records who ran and with what marker."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "#!/usr/bin/env bash\n"
        f"if [[ \"$1\" == \"--version\" ]]; then echo '{version} (Claude Code)'; exit 0; fi\n"
        f"printf 'ran=%s\\nmarker=%s\\n' '{version}' \"$HARNESS_MANAGED_SESSION\" > \"$HARNESS_CAPTURE\"\n",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


class DeterministicCliTests(unittest.TestCase):
    """The agents run ONE claude, chosen by the harness, not by the Terminal's PATH."""

    def setUp(self):
        from tests.environment_support import require_loopback
        require_loopback()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve() / "project"
        self.root.mkdir()
        self.capture = self.root / "captured.txt"
        base = Path(self.tmp.name).resolve()
        # Versions no real install can outrank; the owner's own tool folders are
        # never searched (blanked in-process, a temporary HOME for the runner).
        self.old = _versioned_fake(base / "homebrew" / "claude", "1.0.0")
        self.new = _versioned_fake(base / "local" / "claude", "99.0.0")
        from harness import global_settings
        blank = patch.object(global_settings, "PROVIDER_SEARCH_DIRECTORIES", ())
        blank.start()
        self.addCleanup(blank.stop)
        self.home = base / "home"
        self.home.mkdir()
        self.environment = {
            key: value for key, value in os.environ.items() if key not in {"HARNESS_CLAUDE_BIN", "HARNESS_MANAGED_SESSION"}
        }
        self.environment.update({
            "HARNESS_CAPTURE": str(self.capture),
            # the stale copy FIRST on PATH, exactly as on 2026-09-26
            "PATH": os.pathsep.join([str(self.old.parent), str(self.new.parent), os.environ.get("PATH", "")]),
            "CLAUDE_CONFIG_DIR": str(base / "claude-config"),
            "CODEX_HOME": str(base / "codex-home"),
        })

    def test_the_newest_copy_wins_over_path_order_and_ties_keep_path_order(self):
        from harness import global_settings
        chosen = global_settings.provider_executable("claude", source_environment=self.environment)
        self.assertEqual(Path(chosen).resolve(), self.new.resolve())
        same = _versioned_fake(Path(self.tmp.name).resolve() / "twin" / "claude", "99.0.0")
        environment = {**self.environment, "PATH": os.pathsep.join([str(same.parent), str(self.new.parent)])}
        self.assertEqual(Path(global_settings.provider_executable("claude", source_environment=environment)).resolve(),
                         same.resolve(), "a tie keeps PATH order")

    def test_an_explicit_binary_is_used_as_given_and_never_run_for_its_version(self):
        from harness import global_settings
        environment = {**self.environment, "HARNESS_CLAUDE_BIN": str(self.old)}
        with patch.object(global_settings.subprocess, "run", side_effect=AssertionError("must not execute")):
            resolved = global_settings.resolved_cli("claude", source_environment=environment)
        self.assertEqual(resolved, {"path": str(self.old), "version": "", "source": "configured"})

    def test_a_hanging_version_probe_is_bounded_and_ranks_last(self):
        from harness import global_settings
        hang = Path(self.tmp.name).resolve() / "hang" / "claude"
        hang.parent.mkdir()
        hang.write_text("#!/usr/bin/env bash\nexec /bin/sleep 60\n", encoding="utf-8")
        hang.chmod(0o755)
        environment = {**self.environment, "PATH": os.pathsep.join([str(hang.parent), str(self.old.parent)])}
        with patch.object(global_settings, "CLI_VERSION_PROBE_SECONDS", 1):
            import time
            started = time.monotonic()
            chosen = global_settings.provider_executable("claude", source_environment=environment)
            self.assertLess(time.monotonic() - started, 10, "a hung CLI cannot stall resolution")
        self.assertEqual(Path(chosen).resolve(), self.old.resolve())

    def test_the_runner_launches_the_resolved_cli_and_records_its_path_and_version(self):
        from harness import control, conversation
        from tests.test_conversation_memory import run_runner
        session = control.create(self.root, "claude_cto")
        completed = run_runner(self.root, session, {**self.environment, "HOME": str(self.home)})
        self.assertEqual(completed.returncode, 0, completed.stderr)
        recorded = dict(line.split("=", 1) for line in self.capture.read_text(encoding="utf-8").splitlines())
        self.assertEqual(recorded["ran"], "99.0.0", "the stale copy first on PATH did not run")
        self.assertEqual(recorded["marker"], session["id"], "the CLI inherits the session marker")
        transcript = conversation.transcript_path(self.root, session["id"]).read_text(encoding="utf-8")
        self.assertIn(f"cli={self.new} version=99.0.0", transcript)


SETTINGS_PROBE = r"""
<script>
(async () => {
  await showPage('settings');
  const card = () => document.querySelector('[data-settings-result="cto"]');
  for (let attempt = 0; attempt < 150 && !(card() && /agents use/.test(card().textContent)); attempt++) {
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  const node = card();
  const box = node ? node.getBoundingClientRect() : {width: 0, height: 0};
  await fetch('/__layout_result__', {method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({text: node ? node.textContent.trim() : '', visible: box.width > 0 && box.height > 0,
                          ok: Boolean(node && node.classList.contains('ok'))})});
})();
</script>
"""


class RenderedSettingsTests(unittest.TestCase):
    """The owner sees which CLI and version the agents run, on the real Settings page."""

    def setUp(self):
        from harness import browser_acceptance
        from tests.environment_support import require_loopback
        try:
            browser_acceptance.resolve_binary()
        except (FileNotFoundError, ValueError) as error:
            raise unittest.SkipTest(str(error)) from error
        require_loopback()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()

    def test_the_settings_card_names_the_cli_path_and_version_the_agents_use(self):
        import threading, time, json
        from http.server import ThreadingHTTPServer
        from harness import browser_acceptance, global_settings, project_manager
        from tests.test_project_manager_rendered import proxy_handler
        fake = self.base / "bin" / "claude"
        fake.parent.mkdir()
        fake.write_text(
            "#!/usr/bin/env bash\n"
            "if [[ \"$1\" == \"--version\" ]]; then echo '99.0.0 (Claude Code)'; exit 0; fi\n"
            "echo OK\n", encoding="utf-8")
        fake.chmod(0o755)
        home = self.base / "home"
        workspace = self.base / "workspace"
        workspace.mkdir()
        global_settings.initialize(home)
        environment = {key: value for key, value in os.environ.items() if key != "HARNESS_CLAUDE_BIN"}
        environment["PATH"] = os.pathsep.join([str(fake.parent), "/usr/bin", "/bin"])
        with patch.dict(os.environ, environment, clear=True), \
                patch.object(global_settings, "PROVIDER_SEARCH_DIRECTORIES", ()):
            result = global_settings.test_connection(home, "claude", "opus", "high", workspace)
        self.assertEqual(result["cli_version"], "99.0.0")
        self.assertEqual(Path(result["cli_path"]).resolve(), fake.resolve())

        manager = project_manager.ProjectManager(home, board_port=0)
        server = ThreadingHTTPServer(("127.0.0.1", 0), project_manager.make_handler(manager))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        sink: dict = {}
        proxy = ThreadingHTTPServer(("127.0.0.1", 0), proxy_handler(
            f"http://127.0.0.1:{server.server_address[1]}", sink, SETTINGS_PROBE))
        threading.Thread(target=proxy.serve_forever, daemon=True).start()
        self.addCleanup(proxy.server_close)
        self.addCleanup(proxy.shutdown)
        profile = tempfile.TemporaryDirectory()
        self.addCleanup(profile.cleanup)
        process = browser_acceptance.launch(f"http://127.0.0.1:{proxy.server_address[1]}/", Path(profile.name),
                                            width=1280, height=1000)
        try:
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline and "value" not in sink:
                time.sleep(0.1)
        finally:
            process.close()
        reading = sink.get("value")
        self.assertIsNotNone(reading, "Chrome reported nothing")
        self.assertTrue(reading["visible"], json.dumps(reading))
        self.assertTrue(reading["ok"], "a passed test is shown as passed")
        self.assertIn(f"The agents use Claude 99.0.0 at {result['cli_path']}.", reading["text"], json.dumps(reading))
