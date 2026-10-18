"""Choose the Delivery agent's provider, model and effort when you press Start (task 2026-10-17_launch-model-choice).

Settings stays the default and is never written by a launch; the choice is validated the way Settings is; it survives a
self-heal replacement and a pause; both `/api/sessions` handlers (the standalone viewer's and the project worker's,
which the running Projects app uses) accept it and refuse with 400 and a plain sentence.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from harness import board, board_viewer, control, conversation, global_settings, project_memory, project_worker
from harness.board_surface import SessionTokenAuthority
from harness.project_context import ProjectContext
from tests.environment_support import require_loopback
from tests.test_gemini_capabilities import write_agy

SETTINGS = {
    "delivery": {"provider": "codex", "model": "gpt-6-sol", "effort": "high"},
    "cto": {"provider": "claude", "model": "opus", "effort": "high"},
    "reviewer": {"provider": "claude", "model": "opus", "effort": "max"},
}
AVAILABLE = lambda provider: {"available": True, "reason": ""}  # noqa: E731


def _write_settings_document(home: Path, agent_settings: dict) -> Path:
    """Write a settings file directly (no availability gate, no repair), as an older build or a hand edit would."""
    document = global_settings._default()
    document["agent_settings"] = agent_settings
    global_settings._write(home, document)
    return global_settings._path(home)


def _post(url: str, body: dict) -> tuple[int, dict]:
    request = Request(url, data=json.dumps(body).encode("utf-8"), headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read())
    except HTTPError as error:
        return error.code, json.loads(error.read())


class ReadOnlyLoaderTests(unittest.TestCase):
    """`global_settings.load` writes nothing; `initialize` (manager start, the Settings page) keeps repairing."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name) / "home"
        self.home.mkdir()

    def test_a_missing_file_is_read_as_defaults_and_stays_missing(self):
        value = global_settings.load(self.home)
        self.assertEqual(value["agent_settings"], control.default_agent_settings())
        self.assertNotIn("error", value)
        self.assertFalse(global_settings._path(self.home).exists())
        global_settings.initialize(self.home)
        self.assertTrue(global_settings._path(self.home).exists(), "initialize still creates the file")

    def test_a_legacy_gemini_pair_is_repaired_in_memory_only(self):
        legacy = {**SETTINGS, "cto": {"provider": "gemini", "model": "gemini-3.8-flash-low", "effort": "medium"}}
        path = _write_settings_document(self.home, legacy)
        before = path.read_bytes()
        value = global_settings.load(self.home)
        self.assertEqual(value["agent_settings"]["cto"]["model"], "gemini-3.8-flash-medium", "the reader sees the repaired value")
        self.assertTrue(any("CTO" in note for note in value["notices"]))
        self.assertEqual(path.read_bytes(), before, "load must not rewrite the file")
        for _ in range(3):
            global_settings.load(self.home)
            global_settings.openai_status(self.home)
            global_settings.chat_availability(self.home)
        self.assertEqual(path.read_bytes(), before, "neither openai_status nor chat_availability writes")
        global_settings.initialize(self.home)
        self.assertNotEqual(path.read_bytes(), before, "initialize (the Settings page) still repairs the file")
        self.assertEqual(json.loads(path.read_text())["agent_settings"]["cto"]["model"], "gemini-3.8-flash-medium")

    def test_a_damaged_file_reads_as_defaults_plus_error_and_is_left_alone(self):
        path = global_settings._path(self.home)
        path.write_text("{not json", encoding="utf-8")
        value = global_settings.load(self.home)
        self.assertIn("saved global settings need attention", value["error"])
        self.assertEqual(value["agent_settings"], control.default_agent_settings())
        self.assertEqual(path.read_text(encoding="utf-8"), "{not json")

    def test_the_settings_payload_carries_the_error_and_the_static_catalog(self):
        root = Path(self._tmp.name) / "project"
        root.mkdir()
        global_settings._path(self.home).write_text("{not json", encoding="utf-8")
        payload = board_viewer.settings_payload(root, self.home)
        self.assertIn("saved global settings need attention", payload["error"])
        self.assertEqual(payload["provider_models_catalog"], control.PROVIDER_MODELS)


class ValidatedLaunchChoiceTests(unittest.TestCase):
    def test_a_good_choice_passes_and_a_gemini_effort_adjusts_the_model_with_launch_wording(self):
        picked = control.validated_launch_choice(SETTINGS, "delivery", {"provider": "codex", "model": "gpt-6-luna", "effort": "low"}, AVAILABLE)
        self.assertEqual(picked, {"provider": "codex", "model": "gpt-6-luna", "effort": "low", "note": ""})
        picked = control.validated_launch_choice(SETTINGS, "delivery", {"provider": "gemini", "model": "gemini-3.8-flash-low", "effort": "high"}, AVAILABLE)
        self.assertEqual(picked["model"], "gemini-3.8-flash-high")
        self.assertIn("your high effort selects gemini-3.8-flash-high", picked["note"])
        self.assertNotIn("press Test connection", picked["note"])

    def test_every_refusal_is_a_plain_sentence(self):
        cases = [
            ("reviewer", {"provider": "codex", "model": "gpt-6-sol", "effort": "high"}, "always use their Settings values"),
            ("delivery", "not an object", "must be an object"),
            ("delivery", {"provider": "openai", "model": "x", "effort": "high"}, "unsupported provider"),
            ("delivery", {"provider": "codex", "model": "gpt-6-sol", "effort": "turbo"}, "unsupported effort for codex"),
            ("delivery", {"provider": "gemini", "model": "claude-opus-5-5", "effort": "high"}, "Gemini sessions run only Google's models"),
            ("delivery", {"provider": "codex", "model": "has space", "effort": "high"}, "without spaces"),
            ("delivery", {"provider": "claude", "model": "opus", "effort": "high"}, "Claude is your Independent Reviewer's provider"),
            ("delivery", {"provider": "gemini", "model": "gemini-3.1-pro-high", "effort": "xhigh"}, "xhigh and max do not exist for Gemini"),
        ]
        for role, choice, words in cases:
            with self.subTest(choice=choice):
                with self.assertRaises(ValueError) as refused:
                    control.validated_launch_choice(SETTINGS, role, choice, AVAILABLE)
                self.assertIn(words, str(refused.exception))

    def test_an_unavailable_gemini_is_refused_with_the_stored_reason(self):
        unavailable = lambda provider: {"available": provider != "gemini", "reason": "Gemini (Antigravity CLI) — not yet available on this machine: the Antigravity CLI (agy) is not installed."}  # noqa: E731
        with self.assertRaises(ValueError) as refused:
            control.validated_launch_choice(SETTINGS, "delivery", {"provider": "gemini", "model": "gemini-3.1-pro-high", "effort": "high"}, unavailable)
        self.assertIn("agy) is not installed", str(refused.exception))


class CreateWithChoiceTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / "project"
        self.root.mkdir()

    def test_the_choice_is_stored_with_its_flag_and_settings_are_untouched(self):
        session = control.create(self.root, "codex_delivery", settings_override=SETTINGS,
                                 launch_choice={"provider": "gemini", "model": "gemini-3.8-flash-low", "effort": "high"}, availability=AVAILABLE)
        self.assertEqual((session["provider"], session["model"], session["effort"]), ("gemini", "gemini-3.8-flash-high", "high"))
        self.assertTrue(session["launch_choice"])
        self.assertIn("selects gemini-3.8-flash-high", session["launch_choice_note"])
        self.assertEqual(session["label"], "GEMINI CLI")
        self.assertEqual(session["vendor"], "Google")
        self.assertIsNone(session["cli_launch_reason"])
        plain = control.create(self.root, "claude_cto", settings_override=SETTINGS)
        self.assertFalse(plain["launch_choice"])

    def test_a_refused_choice_creates_nothing_even_when_saved_settings_are_unreadable(self):
        with self.assertRaises(ValueError) as refused:
            control.create(self.root, "codex_delivery", settings_override={"broken": True},
                           launch_choice={"provider": "codex", "model": "gpt-6-sol", "effort": "turbo"})
        self.assertIn("unsupported effort", str(refused.exception))
        self.assertEqual(control.snapshot(self.root)["sessions"], [], "the defaults fallback never applies to an explicit choice")
        with self.assertRaises(ValueError) as refused:
            control.create(self.root, "claude_reviewer", settings_override=SETTINGS, launch_choice={"provider": "codex", "model": "gpt-6-sol", "effort": "high"})
        self.assertIn("always use their Settings values", str(refused.exception))
        self.assertEqual(control.snapshot(self.root)["sessions"], [])

    def test_the_settings_default_passes_the_same_availability_gate(self):
        gemini_default = {**SETTINGS, "delivery": {"provider": "gemini", "model": "gemini-3.1-pro-high", "effort": "high"}}
        unavailable = lambda provider: {"available": provider != "gemini", "reason": "Gemini is not proven on this machine."}  # noqa: E731
        with self.assertRaises(ValueError) as refused:
            control.create(self.root, "codex_delivery", settings_override=gemini_default, availability=unavailable)
        self.assertIn("not proven", str(refused.exception))
        self.assertEqual(control.snapshot(self.root)["sessions"], [])
        okay = control.create(self.root, "codex_delivery", settings_override=SETTINGS, availability=unavailable)
        self.assertEqual(okay["provider"], "codex")

    def test_a_replacement_continues_the_owners_choice_not_todays_settings(self):
        first = control.create(self.root, "codex_delivery", settings_override=SETTINGS,
                               launch_choice={"provider": "gemini", "model": "gemini-3.1-pro-high", "effort": "high"}, availability=AVAILABLE)
        control.record_cli_session(self.root, first["id"], "conv-1", "gemini")
        with control.locked_state(self.root) as state:
            state["sessions"][first["id"]].update({"status": "exited", "ended_at": control.now()})
        replacement = control.create(self.root, "codex_delivery", settings_override=SETTINGS, continues=first["id"], availability=AVAILABLE)
        self.assertEqual((replacement["provider"], replacement["model"], replacement["effort"]), ("gemini", "gemini-3.1-pro-high", "high"))
        self.assertTrue(replacement["launch_choice"])
        self.assertEqual(replacement["cli_session_id"], "conv-1", "the conversation continues too")
        # a Settings-launched predecessor replaces from Settings, as today
        plain = control.create(self.root, "codex_delivery", settings_override=SETTINGS)
        with control.locked_state(self.root) as state:
            state["sessions"][plain["id"]].update({"status": "exited", "ended_at": control.now()})
            state["sessions"][replacement["id"]].update({"status": "exited", "ended_at": control.now()})
        again = control.create(self.root, "codex_delivery", settings_override=SETTINGS, continues=plain["id"])
        self.assertEqual(again["provider"], "codex")
        self.assertFalse(again["launch_choice"])

    def test_a_replacement_whose_choice_is_now_refused_is_refused_not_rerouted(self):
        first = control.create(self.root, "codex_delivery", settings_override=SETTINGS,
                               launch_choice={"provider": "gemini", "model": "gemini-3.1-pro-high", "effort": "high"}, availability=AVAILABLE)
        with control.locked_state(self.root) as state:
            state["sessions"][first["id"]].update({"status": "exited", "ended_at": control.now()})
        reviewer_on_gemini = {**SETTINGS, "reviewer": {"provider": "gemini", "model": "gemini-3.1-pro-high", "effort": "high"}}
        with self.assertRaises(ValueError) as refused:
            control.create(self.root, "codex_delivery", settings_override=reviewer_on_gemini, continues=first["id"], availability=AVAILABLE)
        self.assertIn("Gemini is your Independent Reviewer's provider", str(refused.exception))
        self.assertEqual(len([s for s in control.snapshot(self.root)["sessions"] if s["id"] != first["id"]]), 0)

    def test_the_failure_sentence_points_a_chosen_launch_to_the_delivery_button(self):
        chosen = control.create(self.root, "codex_delivery", settings_override=SETTINGS,
                                launch_choice={"provider": "codex", "model": "gpt-6-luna", "effort": "low"}, availability=AVAILABLE)
        sentence, setup = control.launch_failure_text(chosen, "starting the CLI", 3, "REFUSED: no such model")
        self.assertTrue(setup)
        self.assertIn("Press the Delivery button in Mission Control and correct the provider, model or effort for this launch", sentence)
        plain = control.create(self.root, "claude_cto", settings_override=SETTINGS)
        sentence, _ = control.launch_failure_text(plain, "starting the CLI", 3, "REFUSED")
        self.assertIn("Check the role's provider, model and sign-in in Settings", sentence)

    def test_the_launch_plan_names_a_provider_change_and_the_card_field_follows_the_attempt(self):
        first = control.create(self.root, "codex_delivery", settings_override=SETTINGS)
        control.record_cli_session(self.root, first["id"], "codex-conv", "codex")
        control.note_cli_launch(self.root, first["id"], False, "first launch of this managed session")
        with control.locked_state(self.root) as state:
            state["sessions"][first["id"]].update({"status": "exited", "ended_at": control.now()})
        second = control.create(self.root, "codex_delivery", settings_override=SETTINGS,
                                launch_choice={"provider": "gemini", "model": "gemini-3.1-pro-high", "effort": "high"}, availability=AVAILABLE)
        self.assertEqual(second["cli_session_id"], "codex-conv", "today's inheritance hands the previous id on")
        self.assertEqual(second["cli_session_provider"], "codex")
        plan = conversation.plan_cli_launch(self.root, second["id"], "gemini")
        self.assertEqual(plan["mode"], "fresh")
        self.assertEqual(plan["reason"], "the previous conversation was with Codex; starting fresh with Gemini")
        self.assertIsNone(control.cli_session(self.root, second["id"])["cli_session_id"], "the foreign id is cleared, as today")
        noted = control.note_cli_launch(self.root, second["id"], False, plan["reason"])
        self.assertEqual(control.snapshot(self.root)["sessions"][0]["cli_launch_reason"], plan["reason"])
        self.assertEqual(noted["cli_launches"], 2)
        # a new attempt clears the previous attempt's plan
        with control.locked_state(self.root) as state:
            state["sessions"][second["id"]].update({"status": "launching", "pid": None})
        control.attach(self.root, second["id"], os.getpid())
        self.assertIsNone(next(s for s in control.snapshot(self.root)["sessions"] if s["id"] == second["id"])["cli_launch_reason"])

    def test_pause_capture_and_reconstruction_keep_the_flag(self):
        """The pause capture lists `launch_choice` and a reconstructed session carries it (so a later replacement keeps the choice)."""
        import inspect
        self.assertIn('"launch_choice"', inspect.getsource(board), "board.py's pause capture names the key")
        chosen = control.create(self.root, "codex_delivery", settings_override=SETTINGS,
                                launch_choice={"provider": "gemini", "model": "gemini-3.1-pro-high", "effort": "high"}, availability=AVAILABLE)
        capture = {key: chosen[key] for key in ("kind", "provider", "model", "effort", "launch_choice")}
        with control.locked_state(self.root) as state:
            state["sessions"].pop(chosen["id"])          # the control record went missing while paused
        with mock.patch("harness.control._paused_session_capture", return_value=capture):
            rebuilt = control.restore_missing_resume_session(self.root, chosen["id"], "codex_delivery", SETTINGS)
        self.assertEqual((rebuilt["provider"], rebuilt["model"], rebuilt["effort"]), ("gemini", "gemini-3.1-pro-high", "high"))
        self.assertTrue(rebuilt["launch_choice"])
        self.assertIsNone(rebuilt["cli_launch_reason"])
        plain_capture = {"kind": "codex_delivery", "provider": "codex", "model": "gpt-6-sol", "effort": "high"}
        with mock.patch("harness.control._paused_session_capture", return_value=plain_capture):
            plain = control.restore_missing_resume_session(self.root, "codex_delivery-plain", "codex_delivery", SETTINGS)
        self.assertFalse(plain["launch_choice"])


class SessionApiTests(unittest.TestCase):
    """Both handlers: the standalone viewer's and the project worker's (the running Projects app)."""

    def setUp(self):
        require_loopback()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / "project"
        self.root.mkdir()
        project_memory.initialize(self.root, project_name="Launch choice", description="Facts.")
        self.home = Path(self._tmp.name) / "home"
        self.home.mkdir()
        global_settings.update_agent_settings(self.home, SETTINGS)
        # Gemini is the only provider that can fail the availability gate; a fake `agy` makes it available here
        # (DEV_DIRECTIVE (s): no live Gemini in tests). The Gemini-unavailable test below removes it again.
        agy = write_agy(Path(self._tmp.name) / "agy")
        patcher = mock.patch.dict(os.environ, {"HARNESS_AGY_BIN": str(agy)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def _viewer(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), board_viewer.make_handler(self.root, settings_home=self.home))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close); self.addCleanup(server.shutdown)
        return f"http://127.0.0.1:{server.server_address[1]}"

    def _worker(self):
        base = Path(self._tmp.name)
        context = ProjectContext(self.root, base / "data", base / "workspaces")
        handler = project_worker.make_handler(
            context, authority=SessionTokenAuthority(context), endpoint=lambda: "http://127.0.0.1:1/",
            project_name="Launch choice", project_id="launch-choice", settings_home=self.home, chat_action_token="token",
        )
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close); self.addCleanup(server.shutdown)
        return f"http://127.0.0.1:{server.server_address[1]}"

    def test_the_viewer_accepts_a_choice_and_refuses_with_400(self):
        before = global_settings._path(self.home).read_bytes()
        with mock.patch("harness.board_viewer.launch_terminal") as launch:
            origin = self._viewer()
            status, payload = _post(f"{origin}/api/sessions", {"kind": "codex_delivery", "color": "blue",
                                                               "launch_settings": {"provider": "gemini", "model": "gemini-3.1-pro-high", "effort": "high"}})
            self.assertEqual(status, 201, payload)
            self.assertEqual((payload["session"]["provider"], payload["session"]["model"], payload["session"]["effort"]), ("gemini", "gemini-3.1-pro-high", "high"))
            self.assertTrue(payload["session"]["launch_choice"])
            launch.assert_called_once()
            for body, words in [
                ({"kind": "codex_delivery", "launch_settings": {"provider": "claude", "model": "opus", "effort": "high"}}, "Independent Reviewer's provider"),
                ({"kind": "codex_delivery", "launch_settings": {"provider": "codex", "model": "gpt-6-sol", "effort": "turbo"}}, "unsupported effort"),
                ({"kind": "codex_delivery", "launch_settings": "gpt-6-sol"}, "must be an object"),
                ({"kind": "claude_cto", "launch_settings": {"provider": "claude", "model": "opus", "effort": "low"}}, "always use their Settings values"),
            ]:
                status, payload = _post(f"{origin}/api/sessions", body)
                self.assertEqual(status, 400, payload)
                self.assertIn(words, payload["error"])
            self.assertEqual(len(control.snapshot(self.root)["sessions"]), 1, "refused requests create no session")
            self.assertEqual(launch.call_count, 1)
        self.assertEqual(global_settings._path(self.home).read_bytes(), before, "launches never write Settings")

    def test_the_worker_handler_accepts_the_same_choice_and_answers_400_not_500(self):
        with mock.patch("harness.project_worker.launch_terminal") as launch:
            launch.return_value = mock.Mock(attach_hint="tmux attach -t nmhp-x")
            origin = self._worker()
            status, payload = _post(f"{origin}/api/sessions", {"kind": "codex_delivery", "color": "black",
                                                               "launch_settings": {"provider": "gemini", "model": "gemini-3.1-pro-high", "effort": "high"}})
            self.assertEqual(status, 201, payload)
            self.assertEqual(payload["session"]["provider"], "gemini")
            self.assertEqual(payload["attach_hint"], "tmux attach -t nmhp-x", "the Linux attach hint is no longer dropped")
            status, payload = _post(f"{origin}/api/sessions", {"kind": "codex_delivery",
                                                               "launch_settings": {"provider": "claude", "model": "opus", "effort": "high"}})
            self.assertEqual(status, 400, payload)
            self.assertIn("Independent Reviewer's provider", payload["error"])
            self.assertEqual(launch.call_count, 1)

    def test_damaged_settings_refuse_every_new_session_and_the_payload_says_why(self):
        global_settings._path(self.home).write_text("{not json", encoding="utf-8")
        with mock.patch("harness.board_viewer.launch_terminal") as launch:
            origin = self._viewer()
            status, payload = _post(f"{origin}/api/sessions", {"kind": "codex_delivery"})
            self.assertEqual(status, 400, payload)
            self.assertIn("saved global settings need attention", payload["error"])
            self.assertIn("nothing was launched", payload["error"])
            launch.assert_not_called()
            with urlopen(f"{origin}/api/settings", timeout=5) as response:
                settings = json.loads(response.read())
            self.assertIn("saved global settings need attention", settings["error"])
        with self.assertRaises(ValueError):
            board_viewer.read_agent_settings_for_launch(self.home)
        self.assertEqual(global_settings._path(self.home).read_text(encoding="utf-8"), "{not json")

    def test_cancel_with_a_gemini_default_is_refused_when_gemini_is_unavailable(self):
        gemini_default = {**SETTINGS, "delivery": {"provider": "gemini", "model": "gemini-3.1-pro-high", "effort": "high"}}
        _write_settings_document(self.home, gemini_default)
        with mock.patch.dict(os.environ, {"HARNESS_AGY_BIN": str(Path(self._tmp.name) / "no-such-agy")}), \
                mock.patch("harness.board_viewer.launch_terminal") as launch:
            origin = self._viewer()
            status, payload = _post(f"{origin}/api/sessions", {"kind": "codex_delivery", "color": "black"})
            self.assertEqual(status, 400, payload)
            self.assertIn("Gemini (Antigravity CLI)", payload["error"])
            launch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
