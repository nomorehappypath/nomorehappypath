"""The Delivery Start dialog in a real browser (task 2026-10-17_launch-model-choice §3.1, §3.7, §6).

Pressing the Delivery button opens the dialog pre-filled from Settings; the Reviewer's provider is disabled with its
reason; choosing another provider, model and effort and pressing Launch sends the choice, and the notice names what the
server STORED. Cancel launches with Settings' values. The Reviewer button keeps the colour-only dialog. At 390 px the
dialog is usable and the page does not overflow.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from harness import board_viewer, browser_acceptance, control, global_settings, project_memory
from tests.environment_support import require_loopback
from tests.test_branding_rendered import probe_proxy
from tests.test_gemini_capabilities import write_agy

PROBE = r"""
<script>
(async () => {
  const wait = async (test, tries = 600) => { for (let i = 0; i < tries && !test(); i++) await new Promise(r => setTimeout(r, 100)); return test(); };  // 60 s: /api/settings runs CLI capability probes, slow under full-suite load
  const q = s => document.querySelector(s);
  const visible = node => Boolean(node) && node.getBoundingClientRect().width > 0 && node.getBoundingClientRect().height > 0;
  const result = {steps: []};
  const post = () => fetch('/__probe__', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(result)});
  try {
    await wait(() => /Delivery/.test(q('#codex')?.textContent || '') && !q('#codex').disabled);
    // 1. the Reviewer button keeps the colour-only dialog
    q('#claude').click();
    await wait(() => q('#color-dialog').open);
    result.reviewerDialog = {choiceHidden: q('#launch-choice').hidden, title: q('#color-dialog-title').textContent, cancel: q('#color-cancel').textContent};
    q('#color-close').click();
    await wait(() => !q('#color-dialog').open);
    // 2. the Delivery button: loading state, then pre-filled from Settings
    q('#codex').click();
    await wait(() => q('#color-dialog').open);
    result.loading = {origin: q('#launch-choice-origin').textContent, launchDisabled: q('#color-launch').disabled, providerDisabled: q('#launch-provider').disabled};
    await wait(() => !q('#launch-provider').disabled);
    const options = [...q('#launch-provider').options].map(o => ({value: o.value, disabled: o.disabled, selected: o.selected, text: o.textContent}));
    result.prefilled = {
      origin: q('#launch-choice-origin').textContent, title: q('#color-dialog-title').textContent,
      provider: q('#launch-provider').value, model: q('#launch-model-select').value, effort: q('#launch-effort').value,
      modelOptions: [...q('#launch-model-select').options].map(o => o.value), customHidden: q('#launch-model').hidden,
      options, note: q('#launch-choice-note').textContent, cancel: q('#color-cancel').textContent,
      closeVisible: visible(q('#color-close')), settingsLink: q('#launch-settings-link').hidden ? '' : q('#launch-settings-link').getAttribute('href'),
      dialogRight: q('#color-dialog').getBoundingClientRect().right, viewport: innerWidth,
      overflowX: document.scrollingElement.scrollWidth > document.scrollingElement.clientWidth,
      efforts: [...q('#launch-effort').options].map(o => o.value),
    };
    // 3. choose the Reviewer's provider: disabled with its reason (the option is disabled, so force it to read the note)
    q('#launch-provider').value = 'claude'; q('#launch-provider').dispatchEvent(new Event('change'));
    await new Promise(r => setTimeout(r, 100));
    result.claudeNote = {note: q('#launch-choice-note').textContent, launchDisabled: q('#color-launch').disabled};
    // 4. choose Gemini, a model and an effort, and launch
    q('#launch-provider').value = 'gemini'; q('#launch-provider').dispatchEvent(new Event('change'));
    await new Promise(r => setTimeout(r, 100));
    result.geminiModels = [...q('#launch-model-select').options].map(o => o.value);
    q('#launch-model-select').value = 'gemini-3.8-flash-high'; q('#launch-model-select').dispatchEvent(new Event('change'));
    q('#launch-effort').value = 'low'; q('#launch-effort').dispatchEvent(new Event('change'));
    await new Promise(r => setTimeout(r, 100));
    result.geminiNote = q('#launch-choice-note').textContent;
    q('#color-launch').click();
    await wait(() => /launch requested/.test(q('#notice').textContent) || /Could not/.test(q('#notice').textContent));
    result.notice = q('#notice').textContent;
    // 5. Reopen: every opening starts from Settings again (batch review F1: the last opening's Low must not leak)
    await wait(() => !q('#color-dialog').open);
    q('#codex').click();
    await wait(() => q('#color-dialog').open && !q('#launch-provider').disabled);
    result.reopened = {provider: q('#launch-provider').value, model: q('#launch-model-select').value, effort: q('#launch-effort').value};
    // edit, Close (nothing launched), reopen: the edit is gone too
    q('#launch-effort').value = 'max'; q('#launch-effort').dispatchEvent(new Event('change'));
    q('#color-close').click();
    await wait(() => !q('#color-dialog').open);
    q('#codex').click();
    await wait(() => q('#color-dialog').open && !q('#launch-provider').disabled);
    result.reopenedAfterClose = {provider: q('#launch-provider').value, model: q('#launch-model-select').value, effort: q('#launch-effort').value};
    // Launch untouched: Settings' values
    const noticeBefore = q('#notice').textContent;
    q('#color-launch').click();
    await wait(() => q('#notice').textContent !== noticeBefore && /launch requested/.test(q('#notice').textContent));
    result.untouchedLaunchNotice = q('#notice').textContent;
    // two stubbed sessions are now "active" (the page allows two Delivery agents): stop them so the button is enabled again
    await fetch('/api/sessions/stop-all', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}'});
    await wait(() => !q('#color-dialog').open);
    await wait(() => { const b = q('#codex'); return b && !b.disabled && /\(0\/2\)/.test(b.textContent); });
    // 6. Cancel launches with Settings' values
    q('#codex').click();
    await wait(() => q('#color-dialog').open && !q('#launch-provider').disabled);
    q('#color-cancel').click();
    await wait(() => /CODEX Delivery Agent launch requested/.test(q('#notice').textContent) && q('#notice').textContent !== result.untouchedLaunchNotice);
    result.cancelNotice = q('#notice').textContent;
  } catch (error) { result.error = String(error && error.stack || error); }
  await post();
})();
</script>
"""


class LaunchChoiceDialogRenderedTests(unittest.TestCase):
    def setUp(self):
        try:
            browser_acceptance.resolve_binary()
        except (FileNotFoundError, ValueError) as error:
            raise unittest.SkipTest(str(error)) from error
        require_loopback()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve() / "project"
        self.root.mkdir()
        project_memory.initialize(self.root, project_name="Launch choice", description="Facts.")
        self.home = Path(self._tmp.name).resolve() / "home"
        agy = write_agy(Path(self._tmp.name).resolve() / "agy")
        patcher = mock.patch.dict(os.environ, {"HARNESS_AGY_BIN": str(agy)})
        patcher.start()
        self.addCleanup(patcher.stop)
        global_settings.update_agent_settings(self.home, {
            "delivery": {"provider": "codex", "model": "gpt-6-sol", "effort": "high"},
            "cto": {"provider": "claude", "model": "opus", "effort": "high"},
            "reviewer": {"provider": "claude", "model": "opus", "effort": "max"},
        })
        self.settings_bytes = global_settings._path(self.home).read_bytes()

    def render(self, width: int) -> dict:
        launched: list[dict] = []

        def fake_launch(root, session, *, manager_home=None):
            launched.append(dict(session))
            return None

        with mock.patch("harness.board_viewer.launch_terminal", side_effect=fake_launch):
            server = ThreadingHTTPServer(("127.0.0.1", 0), board_viewer.make_handler(
                self.root, project_name="Launch choice", manager_url="http://127.0.0.1:1/", settings_home=self.home,
                project_id="launch-choice", chat_action_token="owner-token"))
            threading.Thread(target=server.serve_forever, daemon=True).start()
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)
            sink: dict = {}
            proxy = ThreadingHTTPServer(("127.0.0.1", 0), probe_proxy(f"http://127.0.0.1:{server.server_address[1]}", sink, PROBE))
            threading.Thread(target=proxy.serve_forever, daemon=True).start()
            self.addCleanup(proxy.server_close)
            self.addCleanup(proxy.shutdown)
            with tempfile.TemporaryDirectory() as profile:
                process = browser_acceptance.launch(f"http://127.0.0.1:{proxy.server_address[1]}/", Path(profile), width=width, height=1100)
                try:
                    deadline = time.monotonic() + 120
                    while time.monotonic() < deadline and "value" not in sink:
                        time.sleep(0.1)
                finally:
                    process.close()
        self.assertIn("value", sink, "Chrome reported nothing: a blank render is not a pass")
        reading = sink["value"]
        reading["launched"] = launched
        return reading

    def _check(self, reading: dict) -> None:
        dump = json.dumps(reading, indent=2, ensure_ascii=False, default=str)
        self.assertNotIn("error", reading, dump)
        self.assertTrue(reading["reviewerDialog"]["choiceHidden"], dump)
        self.assertEqual(reading["reviewerDialog"]["title"], "Choose terminal color", dump)
        self.assertEqual(reading["loading"]["origin"], "Loading Settings…", dump)
        self.assertTrue(reading["loading"]["launchDisabled"] and reading["loading"]["providerDisabled"], dump)
        pre = reading["prefilled"]
        self.assertEqual(pre["title"], "Start a Delivery Agent", dump)
        self.assertIn("Pre-filled from Settings. Your choice here is for this launch only; Settings is not changed.", pre["origin"], dump)
        self.assertEqual((pre["provider"], pre["model"], pre["effort"]), ("codex", "gpt-6-sol", "high"), dump)
        claude = next(option for option in pre["options"] if option["value"] == "claude")
        self.assertTrue(claude["disabled"], dump)
        gemini = next(option for option in pre["options"] if option["value"] == "gemini")
        self.assertFalse(gemini["disabled"], dump)
        self.assertEqual(pre["efforts"], list(control.PROVIDER_EFFORTS["codex"]), dump)
        self.assertEqual(pre["modelOptions"], list(control.PROVIDER_MODELS["codex"]) + ["__custom__"], "every catalog model is offered, plus Custom")
        self.assertTrue(pre["customHidden"], dump)
        self.assertEqual(reading["geminiModels"], list(control.PROVIDER_MODELS["gemini"]) + ["__custom__"], dump)
        self.assertIn("Not tested with this model and effort. To test it, open Settings.", pre["note"], dump)
        self.assertNotIn("Press Test", pre["note"], dump)
        self.assertEqual(pre["cancel"], "Cancel — use Settings and black", dump)
        self.assertTrue(pre["closeVisible"], dump)
        self.assertIn("page=settings", pre["settingsLink"], dump)
        self.assertFalse(pre["overflowX"], dump)
        self.assertLessEqual(pre["dialogRight"], pre["viewport"] + 1, dump)
        self.assertIn("Claude is your Independent Reviewer's provider", reading["claudeNote"]["note"], dump)
        self.assertTrue(reading["claudeNote"]["launchDisabled"], dump)
        self.assertIn("GEMINI Delivery Agent launch requested with gemini-3.8-flash-low at low effort", reading["notice"], dump)
        self.assertIn("your low effort selects gemini-3.8-flash-low", reading["notice"], dump)
        self.assertNotIn("press Test connection", reading["notice"], dump)
        self.assertEqual(reading["reopened"], {"provider": "codex", "model": "gpt-6-sol", "effort": "high"}, "a reopened dialog starts from Settings: " + dump)
        self.assertEqual(reading["reopenedAfterClose"], {"provider": "codex", "model": "gpt-6-sol", "effort": "high"}, "an edit before Close is gone: " + dump)
        self.assertIn("CODEX Delivery Agent launch requested with gpt-6-sol at high effort", reading["untouchedLaunchNotice"], dump)
        self.assertIn("CODEX Delivery Agent launch requested with gpt-6-sol at high effort with Standard black terminal", reading["cancelNotice"], dump)
        self.assertEqual([(s["provider"], s["model"], s["effort"], s["launch_choice"]) for s in reading["launched"]],
                         [("gemini", "gemini-3.8-flash-low", "low", True), ("codex", "gpt-6-sol", "high", True), ("codex", "gpt-6-sol", "high", False)], dump)
        self.assertEqual(global_settings._path(self.home).read_bytes(), self.settings_bytes, "the dialog and two launches wrote nothing to Settings")

    def test_desktop_dialog_prefills_refuses_the_reviewers_provider_and_launches_the_choice(self):
        self._check(self.render(1280))

    def test_phone_width_dialog_is_usable_and_does_not_overflow(self):
        self._check(self.render(390))


PAUSE_PROBE = r"""
<script>
(async () => {
  const wait = async (test, tries = 900) => { for (let i = 0; i < tries && !test(); i++) await new Promise(r => setTimeout(r, 100)); return test(); };
  const q = s => document.querySelector(s);
  const post = value => fetch('/__probe__', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(value)});
  const result = {};
  try {
    await wait(() => /Delivery/.test(q('#codex')?.textContent || '') && !q('#codex').disabled);
    await post({phase: 1});
    // the test pauses the project now; the page learns it through its own refresh
    await wait(() => !q('#paused-banner').hidden);
    result.pausedCloseDisabled = q('#color-close').disabled;
    await post({phase: 'paused', pausedCloseDisabled: result.pausedCloseDisabled});
    // the test resumes the project; no reload
    await wait(() => q('#paused-banner').hidden && !q('#codex').disabled);
    q('#codex').click();
    await wait(() => q('#color-dialog').open && !q('#launch-provider').disabled);
    result.afterResume = {closeDisabled: q('#color-close').disabled, closeTitle: q('#color-close').title,
      cancelDisabled: q('#color-cancel').disabled, launchDisabled: q('#color-launch').disabled,
      cancelTitle: q('#color-cancel').title, provider: q('#launch-provider').value, effort: q('#launch-effort').value};
    q('#color-close').click();
    await wait(() => !q('#color-dialog').open, 50);
    result.dialogClosed = !q('#color-dialog').open;
    const c = await (await fetch('/api/control', {cache: 'no-store'})).json();
    result.sessions = c.sessions.length;
    result.phase = 2;
  } catch (error) { result.error = String(error && error.stack || error); result.phase = 2; }
  await post(result);
})();
</script>
"""


class LaunchChoiceDialogAfterPauseTests(unittest.TestCase):
    """Batch review F2 (2026-10-17): after Pause -> Resume on a page kept open, the dialog's Close must work again."""

    def setUp(self):
        try:
            browser_acceptance.resolve_binary()
        except (FileNotFoundError, ValueError) as error:
            raise unittest.SkipTest(str(error)) from error
        require_loopback()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve() / "project"
        self.root.mkdir()
        project_memory.initialize(self.root, project_name="Pause then close", description="Facts.")
        self.home = Path(self._tmp.name).resolve() / "home"
        global_settings.update_agent_settings(self.home, {
            "delivery": {"provider": "codex", "model": "gpt-6-sol", "effort": "high"},
            "cto": {"provider": "claude", "model": "opus", "effort": "high"},
            "reviewer": {"provider": "claude", "model": "opus", "effort": "max"},
        })

    def test_close_dismisses_the_dialog_after_pause_and_resume_without_reload(self):
        from harness import board
        server = ThreadingHTTPServer(("127.0.0.1", 0), board_viewer.make_handler(
            self.root, project_name="Pause then close", manager_url="http://127.0.0.1:1/", settings_home=self.home,
            project_id="pause-close", chat_action_token="owner-token"))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        sink: dict = {}
        proxy = ThreadingHTTPServer(("127.0.0.1", 0), probe_proxy(f"http://127.0.0.1:{server.server_address[1]}", sink, PAUSE_PROBE))
        threading.Thread(target=proxy.serve_forever, daemon=True).start()
        self.addCleanup(proxy.server_close)
        self.addCleanup(proxy.shutdown)

        def wait_phase(expected, seconds):
            deadline = time.monotonic() + seconds
            while time.monotonic() < deadline:
                if sink.get("value", {}).get("phase") == expected:
                    return dict(sink["value"])
                time.sleep(0.1)
            self.fail(f"the page never reported phase {expected!r}; last: {sink.get('value')}")

        with tempfile.TemporaryDirectory() as profile:
            process = browser_acceptance.launch(f"http://127.0.0.1:{proxy.server_address[1]}/", Path(profile), width=1280, height=1000)
            try:
                wait_phase(1, 90)
                board.begin_project_pause(self.root, drain_seconds=0)
                board.finish_project_pause(self.root)
                paused = wait_phase("paused", 60)
                self.assertTrue(paused["pausedCloseDisabled"], "while paused every button is disabled, Close included")
                resume = board.begin_project_resume(self.root)
                board.finish_project_resume(self.root, resume["resume_id"])
                reading = wait_phase(2, 120)
            finally:
                process.close()
        dump = json.dumps(reading, indent=2, ensure_ascii=False)
        self.assertNotIn("error", reading, dump)
        after = reading["afterResume"]
        self.assertFalse(after["closeDisabled"], "Close must be usable again after Resume: " + dump)
        self.assertFalse(after["cancelDisabled"], dump)
        self.assertFalse(after["launchDisabled"], dump)
        self.assertEqual(after["closeTitle"], "Close this dialog; nothing is launched.", dump)
        self.assertNotIn("paused", after["cancelTitle"].lower(), "the paused tooltip is gone: " + dump)
        self.assertEqual((after["provider"], after["effort"]), ("codex", "high"), dump)
        self.assertTrue(reading["dialogClosed"], dump)
        self.assertEqual(reading["sessions"], 0, "Close launched nothing: " + dump)


if __name__ == "__main__":
    unittest.main()
