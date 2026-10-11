# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Two fixes found in the first live cycles on the owner's weather project (2026-10-10).

1. Gemini's model id carries the thinking level and the CLI rejects a disagreeing --model/--effort pair, yet Settings allowed
   `gemini-3.8-flash-low` with effort `medium` and the window then showed "high".
2. The refusal telling an agent not to call the live board sat earlier than the last 500 characters of a long Node trace, so
   the agent never saw why its certified checks failed.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from harness import browser_acceptance, control, execution_identity, execution_preflight, global_settings, project_manager
from tests.environment_support import require_loopback
from tests.test_gemini_capabilities import write_agy
from tests.test_project_manager_rendered import proxy_handler

OWNERS_PAIR = {
    "cto": {"provider": "gemini", "model": "gemini-3.8-flash-low", "effort": "medium"},
    "delivery": {"provider": "codex", "model": "gpt-6-luna", "effort": "high"},
    "reviewer": {"provider": "claude", "model": "sonnet", "effort": "medium"},
}


class PairRuleTests(unittest.TestCase):
    def test_the_chosen_effort_selects_the_level_of_the_model(self):
        for model, effort, expected in (
            ("gemini-3.8-flash-low", "medium", "gemini-3.8-flash-medium"),
            ("gemini-3.8-flash-high", "low", "gemini-3.8-flash-low"),
            ("gemini-3.1-pro-low", "high", "gemini-3.1-pro-high"),
            ("gemini-3.8-flash-medium", "medium", "gemini-3.8-flash-medium"),
        ):
            with self.subTest(model=model, effort=effort):
                pair = control.gemini_pair(model, effort, repair=False)
                self.assertEqual(pair[:2], (expected, effort))
                self.assertEqual(bool(pair[2]), model != expected)

    def test_a_level_missing_from_the_catalog_is_not_refused_but_the_notice_says_to_confirm_it(self):
        model, effort, notice = control.gemini_pair("gemini-3.1-pro-low", "medium", repair=False)
        self.assertEqual((model, effort), ("gemini-3.1-pro-medium", "medium"))
        self.assertIn("Test connection", notice)

    def test_custom_ids_follow_the_suffix_rule_and_unsuffixed_ids_are_untouched(self):
        self.assertEqual(control.gemini_pair("gemini-new-low", "high", repair=False)[:2], ("gemini-new-high", "high"))
        self.assertEqual(control.gemini_pair("gemini-custom", "xhigh", repair=False), ("gemini-custom", "xhigh", ""))

    def test_xhigh_and_max_are_refused_on_save_and_repaired_when_they_are_old_stored_values(self):
        with self.assertRaisesRegex(ValueError, "thinking level is part of the model name"):
            control.gemini_pair("gemini-3.8-flash-low", "max", repair=False)
        model, effort, notice = control.gemini_pair("gemini-3.8-flash-low", "xhigh", repair=True)
        self.assertEqual((model, effort), ("gemini-3.8-flash-low", "low"))
        self.assertIn("was changed to low", notice)

    def test_claude_and_codex_settings_are_unchanged(self):
        settings = {
            "cto": {"provider": "claude", "model": "opus", "effort": "xhigh"},
            "delivery": {"provider": "codex", "model": "gpt-6-luna", "effort": "max"},
            "reviewer": {"provider": "claude", "model": "sonnet", "effort": "low"},
        }
        notices: list[str] = []
        self.assertEqual(control._validated_agent_settings(settings, notices=notices, repair=True), settings)
        self.assertEqual(notices, [])

    def test_a_repair_never_replaces_the_other_roles_with_defaults(self):
        stored = json.loads(json.dumps(OWNERS_PAIR))
        stored["cto"]["effort"] = "xhigh"
        notices: list[str] = []
        repaired = control._validated_agent_settings(stored, notices=notices, repair=True)
        self.assertEqual(repaired["delivery"], OWNERS_PAIR["delivery"])
        self.assertEqual(repaired["reviewer"], OWNERS_PAIR["reviewer"])
        self.assertEqual(repaired["cto"]["effort"], "low")
        self.assertTrue(notices and notices[0].startswith("CTO: "))


class StoredSettingsTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name) / "home"
        self.home.mkdir()

    def store(self, agent_settings: dict) -> None:
        document = global_settings._default()
        document["agent_settings"] = agent_settings
        document.pop("notices", None)             # an older build wrote no notices
        (self.home / global_settings.SETTINGS_FILENAME).write_text(json.dumps(document), encoding="utf-8")

    def test_the_owners_stored_pair_is_repaired_on_first_read_written_back_and_announced(self):
        self.store(OWNERS_PAIR)
        value = global_settings.initialize(self.home)
        self.assertEqual(value["agent_settings"]["cto"]["model"], "gemini-3.8-flash-medium")
        self.assertEqual(value["agent_settings"]["delivery"], OWNERS_PAIR["delivery"])
        self.assertTrue(any("CTO" in notice and "medium" in notice for notice in value["notices"]))
        on_disk = json.loads((self.home / global_settings.SETTINGS_FILENAME).read_text())
        self.assertEqual(on_disk["agent_settings"]["cto"]["model"], "gemini-3.8-flash-medium", "what launches matches what Settings shows")
        self.assertEqual(on_disk["notices"], value["notices"])
        self.assertEqual(global_settings.load(self.home)["notices"], value["notices"], "the notice stays until the next explicit save")

    def test_an_explicit_save_clears_old_notices_and_reports_its_own_change(self):
        self.store(OWNERS_PAIR)
        global_settings.initialize(self.home)
        with mock.patch("harness.cli_capabilities.provider_availability", return_value={"available": True, "reason": ""}):
            clean = global_settings.update_agent_settings(self.home, {**OWNERS_PAIR, "cto": {"provider": "gemini", "model": "gemini-3.8-flash-medium", "effort": "medium"}})
            self.assertEqual(clean["notices"], [])
            mismatched = global_settings.update_agent_settings(self.home, {**OWNERS_PAIR, "cto": {"provider": "gemini", "model": "gemini-3.8-flash-medium", "effort": "high"}})
        self.assertEqual(mismatched["agent_settings"]["cto"], {"provider": "gemini", "model": "gemini-3.8-flash-high", "effort": "high"})
        self.assertTrue(mismatched["notices"])

    def test_a_pair_that_cannot_exist_is_refused_on_save_and_nothing_is_stored(self):
        self.store(OWNERS_PAIR)
        global_settings.initialize(self.home)
        before = (self.home / global_settings.SETTINGS_FILENAME).read_text()
        with mock.patch("harness.cli_capabilities.provider_availability", return_value={"available": True, "reason": ""}):
            with self.assertRaisesRegex(ValueError, "thinking level is part of the model name"):
                global_settings.update_agent_settings(self.home, {**OWNERS_PAIR, "cto": {"provider": "gemini", "model": "gemini-3.8-flash-medium", "effort": "max"}})
        self.assertEqual((self.home / global_settings.SETTINGS_FILENAME).read_text(), before)


class SessionTests(unittest.TestCase):
    def test_a_session_captured_with_the_bad_pair_relaunches_consistently_for_every_role(self):
        for kind, role in (("claude_cto", "cto"), ("codex_delivery", "delivery"), ("claude_reviewer", "reviewer")):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                settings = json.loads(json.dumps(OWNERS_PAIR))
                settings[role] = {"provider": "gemini", "model": "gemini-3.8-flash-high", "effort": "high"}
                session = control.create(root, kind, settings_override=settings)
                with control.locked_state(root) as state:        # as captured by an older build
                    state["sessions"][session["id"]].update({"model": "gemini-3.8-flash-low", "effort": "medium"})
                launch = control.session_launch_settings(root, session["id"], kind)
                self.assertEqual((launch["model"], launch["effort"]), ("gemini-3.8-flash-medium", "medium"))
                stored = control.snapshot(root)["sessions"][0]
                self.assertIn("setting_repaired", stored)
                self.assertEqual(control.session_launch_settings(root, session["id"], kind)["model"], "gemini-3.8-flash-medium")

    def test_a_different_google_model_in_the_window_is_recorded_not_only_a_non_google_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = json.loads(json.dumps(OWNERS_PAIR))
            settings["cto"] = {"provider": "gemini", "model": "gemini-3.8-flash-medium", "effort": "medium"}
            session = control.create(root, "claude_cto", settings_override=settings)
            control.record_observed_model(root, session["id"], "gemini-3.8-flash-high")
            record = control.snapshot(root)["sessions"][0]
            self.assertIn("asked for gemini-3.8-flash-medium", record["selection_differs"])
            control.record_observed_model(root, session["id"], "gemini-3.8-flash-medium")
            self.assertEqual(control.snapshot(root)["sessions"][0]["selection_differs"], "")


class ConnectionTestTests(unittest.TestCase):
    def test_the_test_button_uses_the_same_rule_on_unsaved_values_and_returns_the_effective_pair(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            capture = base / "args.txt"
            agy = base / "agy"
            agy.write_text("#!/bin/sh\ncase \"$1\" in --version) echo 1.3.1 ;; *) printf '%s\\n' \"$@\" > " + str(capture)
                           + "; echo '{\"status\": \"SUCCESS\", \"response\": \"ready\"}' ;; esac\n")
            agy.chmod(0o755)
            workspace = base / "ws"
            workspace.mkdir()
            with mock.patch.dict(os.environ, {"HARNESS_AGY_BIN": str(agy)}), \
                    mock.patch("harness.gemini_auth.api_key_problem", return_value=""), \
                    mock.patch("harness.agent_confinement.wrap", side_effect=lambda argv, *a, **k: argv):
                result = global_settings.test_connection(base / "home", "gemini", "gemini-3.8-flash-low", "medium", workspace)
            arguments = capture.read_text().split()
            self.assertEqual(arguments[arguments.index("--model") + 1], "gemini-3.8-flash-medium")
            self.assertEqual(arguments[arguments.index("--effort") + 1], "medium")
            self.assertEqual((result["effective_model"], result["effective_effort"]), ("gemini-3.8-flash-medium", "medium"))
            self.assertIn("thinking level is part of the model name", result["message"])


def long_trace_with(refusal_line: str) -> str:
    trace = "\n".join(f"    at Object.<anonymous> (/work/tests/ui.test.mjs:{n}:11)" for n in range(60))
    return f"Error: command failed\n{refusal_line}\n{trace}\n{trace}\n"


class RefusalFirstTests(unittest.TestCase):
    REFUSAL = "error: " + execution_preflight.BOARD_CALL_REFUSAL

    def test_the_refusal_leads_the_message_in_full_even_when_the_tail_would_cut_it_off(self):
        output = long_trace_with(self.REFUSAL)
        self.assertNotIn(execution_preflight.BOARD_CALL_MARKER, output[-500:], "this is the weather project's situation")
        message = browser_acceptance.with_blocked_note(f"internal-QA test command failed with exit code 1: {output[-500:]}", output)
        self.assertTrue(message.startswith(execution_preflight.BOARD_CALL_REFUSAL), message[:200])
        self.assertIn("screen-... evidence id", message)
        self.assertIn("fixture", message)

    def test_a_quoted_log_or_an_assertion_that_merely_contains_the_words_is_not_taken_for_the_refusal(self):
        for output in (
            f'assert "{execution_preflight.BOARD_CALL_MARKER}" in stderr\n',
            f"expected: error: {execution_preflight.BOARD_CALL_MARKER} (a test about the message)\n",
            f"    # error: {execution_preflight.BOARD_CALL_MARKER}\n",
        ):
            self.assertEqual(browser_acceptance.board_call_note(output), "")
            self.assertEqual(browser_acceptance.with_blocked_note("failed", output), "failed")

    def test_the_blocked_browser_note_is_kept_alongside_it(self):
        output = long_trace_with(self.REFUSAL) + f"{browser_acceptance.SANDBOX_BLOCKED}: no display\n"
        message = browser_acceptance.with_blocked_note("failed", output)
        self.assertTrue(message.startswith(execution_preflight.BOARD_CALL_REFUSAL))
        self.assertIn(browser_acceptance.SANDBOX_BLOCKED, message)

    def test_the_runtime_guard_prints_exactly_the_line_the_extractor_looks_for(self):
        self.assertTrue(self.REFUSAL.startswith("error: " + execution_preflight.BOARD_CALL_MARKER))

    def test_every_failure_branch_of_both_governed_paths_goes_through_the_helper(self):
        from pathlib import Path as P
        root = P(__file__).resolve().parents[1] / "harness"
        board_source = (root / "board.py").read_text(encoding="utf-8")
        certified_source = (root / "certified_execution.py").read_text(encoding="utf-8")
        self.assertIn('with_blocked_note("internal-QA test command could not run its screen check", output)', board_source)
        self.assertEqual(certified_source.count("with_blocked_note("), 4)       # timeout, zero tests, failure, retry
        self.assertNotIn("could not run its screen check: {browser_acceptance.blocked_note(output)}", board_source)

    def test_a_retry_without_a_repair_reason_shows_why_the_earlier_attempt_failed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            identity = {"kind": "command_run", "sha256": "a" * 64, "fields": {"argv": ["npm", "test"]}}
            output = long_trace_with(self.REFUSAL)
            execution_identity.certify(root, identity, exit_code=1, output_sha256=hashlib.sha256(output.encode()).hexdigest(), duration_seconds=1.0, output=output)
            self.assertIn(execution_preflight.BOARD_CALL_MARKER, execution_identity.latest_failed_output(root, identity))
            self.assertEqual(execution_identity.latest_failed_output(root, {**identity, "sha256": "c" * 64}), "")


SETTINGS_SCRIPT = r"""
<script>
(async () => {
  document.querySelector('button[data-page="settings"]').click();
  for (let i = 0; i < 100 && !document.querySelector('[data-setting-provider]'); i++) await new Promise(r => setTimeout(r, 100));
  const notice = document.querySelector('#settings-notices');
  const cto = document.querySelector('[data-setting-model-choice="cto"]');
  const before = document.querySelector('[data-settings-result="cto"]').textContent;
  const effort = document.querySelector('[data-setting-effort="cto"]');
  effort.value = 'high'; effort.dispatchEvent(new Event('change'));
  await new Promise(r => setTimeout(r, 100));
  await fetch('/__layout_result__', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({
    noticeHidden: notice.hidden, noticeText: notice.textContent, ctoModel: cto.value,
    resultBefore: before, resultAfterChange: document.querySelector('[data-settings-result="cto"]').textContent,
  })});
})();
</script>
"""


class RenderedSettingsTests(unittest.TestCase):
    def setUp(self):
        require_loopback()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)

    def test_the_owner_sees_the_repair_and_a_stale_test_asks_for_a_retest(self):
        reading = self.render_settings({"gemini": {"ok": True, "message": "Gemini answered.", "model": "gemini-3.8-flash-low",
                                                   "effort": "medium", "tested_at": "2026-10-09T10:00:00+00:00", "cli_version": "1.3.1"}})
        self.assertFalse(reading["noticeHidden"])
        self.assertIn("gemini-3.8-flash-medium", reading["noticeText"])
        self.assertEqual(reading["ctoModel"], "gemini-3.8-flash-medium")
        self.assertIn("another model or effort", reading["resultBefore"], "a test made with the old pair is history, not a promise")
        self.assertIn("Settings changed", reading["resultAfterChange"])

    def test_a_result_without_its_pair_is_unverified_history_and_a_changed_cli_asks_for_a_retest(self):
        # An older build stored no model/effort with its result: it must not be shown as a match.
        legacy = self.render_settings({"gemini": {"ok": True, "message": "Gemini answered.", "tested_at": "2026-10-01T10:00:00+00:00"}})
        self.assertIn("Press Test", legacy["resultBefore"])
        self.assertNotIn("Gemini answered.", legacy["resultBefore"])
        # Same pair, but made with another agy version than the one now in use.
        moved = self.render_settings({"gemini": {"ok": True, "message": "Gemini answered.", "model": "gemini-3.8-flash-medium",
                                                 "effort": "medium", "tested_at": "2026-10-09T10:00:00+00:00",
                                                 "cli_version": "0.0.1"}})
        self.assertIn("another CLI version", moved["resultBefore"])

    def render_settings(self, connectivity: dict) -> dict:
        home = self.base / f"home-{len(list(self.base.iterdir()))}"
        home.mkdir()
        document = global_settings._default()
        document["agent_settings"] = json.loads(json.dumps(OWNERS_PAIR))
        document["connectivity"] = connectivity
        document.pop("notices", None)
        (home / global_settings.SETTINGS_FILENAME).write_text(json.dumps(document), encoding="utf-8")
        agy = write_agy(self.base / "agy")
        with mock.patch.dict(os.environ, {"HARNESS_AGY_BIN": str(agy)}):
            manager = project_manager.ProjectManager(home, board_port=0)
            server = ThreadingHTTPServer(("127.0.0.1", 0), project_manager.make_handler(manager))
            threading.Thread(target=server.serve_forever, daemon=True).start()
            sink: dict = {}
            proxy = ThreadingHTTPServer(("127.0.0.1", 0), proxy_handler(f"http://127.0.0.1:{server.server_address[1]}", sink, SETTINGS_SCRIPT))
            threading.Thread(target=proxy.serve_forever, daemon=True).start()
            try:
                with tempfile.TemporaryDirectory() as profile:
                    session = browser_acceptance.launch(f"http://127.0.0.1:{proxy.server_address[1]}/", Path(profile), width=1280, height=900)
                    try:
                        deadline = time.monotonic() + 45
                        while "value" not in sink and time.monotonic() < deadline:
                            time.sleep(0.1)
                    finally:
                        session.close()
            finally:
                proxy.shutdown(); proxy.server_close(); server.shutdown(); server.server_close()
        self.assertIn("value", sink, "Chrome never reported the Settings page")
        return sink["value"]


class BrowserConfinementTests(unittest.TestCase):
    """The reviewer's blocking finding: on Linux a rendered test that starts Chrome inside the harness's own confinement aborts
    (the SUID sandbox error). The fix only selects Chrome flags; no security decision may read what it reads."""

    def host(self):
        from harness import platform_support
        return platform_support.browser_host()

    def linux(self, marker: bool, no_new_privs: str | None):
        from harness.platform_support import defaults
        status = "Name:\tx\nNoNewPrivs:\t" + no_new_privs + "\n" if no_new_privs is not None else "Name:\tx\n"
        environment = {"HARNESS_OS_SANDBOX": "1"} if marker else {}
        return (mock.patch.object(defaults.sys, "platform", "linux"),
                mock.patch.dict(defaults.os.environ, environment),
                mock.patch.object(defaults.Path, "read_text", return_value=status))

    def test_linux_trusts_the_marker_only_together_with_the_kernels_no_new_privs_flag(self):
        cases = ((True, "1", True), (True, "0", False), (True, None, False), (False, "1", False), (False, None, False))
        for marker, flag, expected in cases:
            with self.subTest(marker=marker, no_new_privs=flag):
                patches = self.linux(marker, flag)
                with patches[0], patches[1], patches[2]:
                    self.assertEqual(self.host().confined_by_harness(), expected)

    def test_a_forged_marker_never_changes_what_the_security_predicate_says(self):
        patches = self.linux(True, "1")
        with patches[0], patches[1], patches[2]:
            self.assertFalse(self.host().inside_os_sandbox(), "confine_agent_command must still wrap an unconfined process")
        source = (Path(__file__).resolve().parents[1] / "harness" / "agent_confinement.py").read_text(encoding="utf-8")
        self.assertIn("platform_support.browser_host().inside_os_sandbox()", source)
        self.assertNotIn("confined_by_harness", source)

    def test_chrome_flags_per_platform(self):
        from harness.platform_support import defaults
        with mock.patch.object(defaults.sys, "platform", "darwin"):
            self.assertEqual(self.host().confined_browser_args(), ["--no-sandbox", "--single-process"])
        with mock.patch.object(defaults.sys, "platform", "linux"):
            self.assertEqual(self.host().confined_browser_args(), ["--no-sandbox"])

    def test_the_bubblewrap_builder_sets_the_marker(self):
        from harness import agent_confinement
        from harness.platform_support import linux
        with tempfile.TemporaryDirectory() as tmp:
            command = linux._BwrapAgentConfinement(which=lambda name: "/usr/bin/bwrap").wrap(
                ["true"], [str(Path(tmp) / "p")], store=Path(tmp) / "s")
        self.assertEqual(command[command.index("--setenv") + 1:command.index("--setenv") + 3], ["HARNESS_OS_SANDBOX", "1"])
        self.assertLess(command.index("--setenv"), command.index("--"))

    def test_a_confined_process_passes_the_marker_to_nested_governed_execution_and_an_unconfined_one_does_not(self):
        from harness import child_process
        host = self.host()
        with mock.patch.object(type(host), "confined_by_harness", return_value=True), \
                mock.patch.object(type(host), "inside_os_sandbox", return_value=False):
            self.assertEqual(child_process.execution_environment({"PATH": "/usr/bin"}).get("HARNESS_OS_SANDBOX"), "1")
        with mock.patch.object(type(host), "confined_by_harness", return_value=False):
            self.assertNotIn("HARNESS_OS_SANDBOX", child_process.execution_environment({"PATH": "/usr/bin", "HARNESS_OS_SANDBOX": "1"}))

    def test_the_launch_leaves_chromes_inner_sandbox_off_only_when_confined(self):
        captured = {}

        real_popen = subprocess.Popen

        def popen(args, **kwargs):
            if args and args[0] == __file__:               # only the browser launch; the process-table reads run normally
                captured["args"] = list(args)
                raise RuntimeError("stop after the arguments are known")
            return real_popen(args, **kwargs)

        host = self.host()
        for confined, expect_flag in ((True, True), (False, False)):
            with self.subTest(confined=confined), tempfile.TemporaryDirectory() as tmp, \
                    mock.patch.object(browser_acceptance, "resolve_binary", return_value=__file__), \
                    mock.patch.object(browser_acceptance, "browser_identity", return_value={"sha256": "x"}), \
                    mock.patch.object(browser_acceptance, "_binary_digest", return_value="x"), \
                    mock.patch.object(type(host), "confined_by_harness", return_value=confined), \
                    mock.patch.object(browser_acceptance.subprocess, "Popen", side_effect=popen):
                with self.assertRaisesRegex(RuntimeError, "stop after"):
                    browser_acceptance.launch("http://127.0.0.1:9/", Path(tmp))
            self.assertEqual("--no-sandbox" in captured["args"], expect_flag)

    def test_a_browser_that_dies_on_start_up_is_explained_instead_of_timing_out(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Path(tmp)
            (runtime / "output").mkdir()
            (runtime / "output" / "browser-stderr.txt").write_text("The SUID sandbox helper binary was found, but is not configured correctly.")

            class Dead:
                returncode = -6

                def poll(self):
                    return -6

            with self.assertRaisesRegex(RuntimeError, r"exited with code -6 before (opening|finishing) its page: .*SUID sandbox"):
                browser_acceptance._devtools(runtime, time.monotonic() + 5, Dead())


class ErrorPageTests(unittest.TestCase):
    """Found with the Linux helper: inside the harness confinement on a host whose AppArmor names the installed Chrome, the
    browser's network sockets are denied and the 'rendered' page is Chrome's own error page. That is never evidence."""

    class Inspector:
        def __init__(self, port, path, timeout, error_page=True):
            self.error_page = error_page

        def evaluate(self, expression):
            if expression == "document.readyState":
                return "complete"
            if expression == browser_acceptance.ERROR_PAGE_EXPRESSION:
                return self.error_page
            return "This site can't be reached" if self.error_page else "Tide Times"

        def call(self, method, params=None):
            import base64
            return {"data": base64.b64encode(b"png").decode()}

        def close(self):
            pass

    class Session:
        process = None

        def close(self, validate=True):
            pass

    def render(self, error_page: bool, confined: bool = False, stderr: str = ""):
        host = browser_acceptance.platform_support.browser_host()
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(browser_acceptance, "launch", return_value=self.Session()), \
                mock.patch.object(browser_acceptance, "_devtools", return_value=(1, "/x")), \
                mock.patch.object(browser_acceptance, "_report_blocked"), \
                mock.patch.object(browser_acceptance, "_DevToolsSocket", lambda p, q, timeout=0: self.Inspector(p, q, timeout, error_page)), \
                mock.patch.object(type(host), "confined_by_harness", return_value=confined):
            (Path(tmp) / "output").mkdir()
            (Path(tmp) / "output" / "browser-stderr.txt").write_text(stderr)
            return browser_acceptance.render_page("http://127.0.0.1:9/", Path(tmp), timeout=5)

    def test_a_browser_error_page_is_refused_not_returned_as_the_page(self):
        with self.assertRaisesRegex(RuntimeError, "could not load http://127.0.0.1:9/ and showed its own error page"):
            self.render(error_page=True)

    def test_a_stopped_server_is_not_blamed_on_the_host_policy(self):
        with self.assertRaises(RuntimeError) as caught:
            self.render(error_page=True, confined=True)
        self.assertIn("still running on that port", str(caught.exception))
        self.assertNotIn("AppArmor", str(caught.exception))

    def test_socket_denial_evidence_gets_the_host_policy_explanation_and_points_to_screen_check(self):
        with self.assertRaisesRegex(RuntimeError, "AppArmor.*screen-check board command"):
            self.render(error_page=True, confined=True, stderr="socket_posix.cc:121 CreatePlatformSocket() failed: Permission denied (13)")

    def test_the_error_page_is_recognised_by_its_scheme_not_a_class_name(self):
        self.assertEqual(browser_acceptance.ERROR_PAGE_EXPRESSION, "location.protocol === 'chrome-error:'")

    def test_a_real_page_is_still_returned(self):
        page = self.render(error_page=False, confined=False)
        self.assertEqual(page["text"], "Tide Times")

    def test_a_browser_that_is_alive_but_silent_with_denied_sockets_gets_the_same_explanation(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Path(tmp)
            (runtime / "output").mkdir()
            (runtime / "output" / "browser-stderr.txt").write_text("socket_posix.cc:121 CreatePlatformSocket() failed: Permission denied (13)")
            with self.assertRaisesRegex(RuntimeError, "did not open its page in time: .*AppArmor"):
                browser_acceptance._devtools(runtime, time.monotonic() - 1)


class AvailabilityIdentityTests(unittest.TestCase):
    def test_availability_carries_the_resolved_cli_identity_for_the_history_comparison(self):
        from harness import cli_capabilities
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            agy = write_agy(base / "agy")
            with mock.patch.dict(os.environ, {"HARNESS_AGY_BIN": str(agy)}):
                gemini = cli_capabilities.provider_availability(base / "home", "gemini")
                codex = cli_capabilities.provider_availability(base / "home", "codex")
        self.assertTrue(gemini["available"])
        self.assertTrue(gemini["version"])
        self.assertTrue(gemini["executable"].endswith("agy"))
        self.assertEqual(set(codex), {"available", "reason", "executable"})


def tearDownModule():
    from tests.environment_support import assert_agy_pinned
    assert_agy_pinned()


if __name__ == "__main__":
    unittest.main()


class ForgedConfinementMarkerTests(unittest.TestCase):
    def test_an_unconfined_process_with_the_marker_and_no_new_privs_is_not_treated_as_confined(self):
        from harness.platform_support import defaults
        host = defaults._BrowserHost()
        status = "Name:\tx\nNoNewPrivs:\t1\n"
        initial_map = "         0          0 4294967295\n"
        reads = {"/proc/self/status": status, "/proc/self/uid_map": initial_map}
        with mock.patch.object(defaults.sys, "platform", "linux"), \
                mock.patch.dict(os.environ, {"HARNESS_OS_SANDBOX": "1"}), \
                mock.patch.object(type(host), "inside_os_sandbox", return_value=False), \
                mock.patch.object(defaults.Path, "read_text", lambda self, **kw: reads[str(self)]):
            self.assertFalse(host.confined_by_harness())
            reads["/proc/self/uid_map"] = "         0       1000          1\n"
            self.assertTrue(host.confined_by_harness())
