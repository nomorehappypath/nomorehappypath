# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Rendered proof that the Help page shows its guidance, not just carries it."""
from __future__ import annotations

import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

from harness import browser_acceptance, project_manager
from tests.test_branding_rendered import probe_proxy, require_loopback
from tests.environment_support import require_loopback

PROBE = r"""
<script>
(async () => {
  document.querySelector('[data-page="help"]').click();
  for (let attempt = 0; attempt < 50; attempt++) {
    const page = document.querySelector('#help-page');
    if (page && !page.hidden) break;
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  const titles = Array.from(document.querySelectorAll('#help-page h2'))
    .filter(node => node.getBoundingClientRect().width > 0)
    .map(node => node.textContent.trim());
  const tables = document.querySelectorAll('#help-page .help-table').length;
  await fetch('/__probe__', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({titles, tables, hidden: document.querySelector('#help-page').hidden}),
  });
})();
</script>
"""


SECURITY_PROBE = r"""
<script>
(async () => {
  document.querySelector('[data-page="help"]').click();
  for (let attempt = 0; attempt < 50; attempt++) {
    const page = document.querySelector('#help-page');
    if (page && !page.hidden) break;
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  const row = Array.from(document.querySelectorAll('#help-page .help-table tr'))
    .find(node => node.textContent.includes('Enhanced security'));
  const box = row ? row.getBoundingClientRect() : null;
  await fetch('/__probe__', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({text: row ? row.innerText : null, visible: !!box && box.width > 0 && box.height > 0}),
  });
})();
</script>
"""


LEGAL_PROBE = r"""
<script>
(async () => {
  document.querySelector('[data-page="legal"]').click();
  for (let attempt = 0; attempt < 50; attempt++) {
    const page = document.querySelector('#legal-page');
    if (page && !page.hidden) break;
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  const page = document.querySelector('#legal-page');
  const visible = node => node && node.getBoundingClientRect().width > 0;
  const hero = page.querySelector('.hero');
  const text = selector => { const node = hero.querySelector(selector); return visible(node) ? node.textContent.trim() : null; };
  await fetch('/__probe__', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({
      hidden: page.hidden,
      eyebrow: text('.eyebrow'), heading: text('h1'), copy: text('.hero-copy'),
      titles: Array.from(page.querySelectorAll('h2')).filter(visible).map(node => node.textContent.trim()),
      innerText: page.innerText,
    }),
  });
})();
</script>
"""


TRUST_PROBE = r"""
<script>
(async () => {
  document.querySelector('[data-page="help"]').click();
  for (let attempt = 0; attempt < 50; attempt++) {
    const page = document.querySelector('#help-page');
    if (page && !page.hidden) break;
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  const note = Array.from(document.querySelectorAll('#help-page .help-callout'))
    .find(node => node.textContent.includes('First time an agent opens'));
  const box = note ? note.getBoundingClientRect() : null;
  await fetch('/__probe__', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({text: note ? note.innerText : null, visible: !!box && box.width > 0 && box.height > 0}),
  });
})();
</script>
"""


class RenderedHelpTests(unittest.TestCase):
    def setUp(self):
        try:
            browser_acceptance.resolve_binary()
        except (FileNotFoundError, ValueError) as error:
            raise unittest.SkipTest(str(error)) from error
        require_loopback()

    def _render(self, probe: str) -> dict:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / "home"
            manager = project_manager.ProjectManager(home, board_port=0)
            server = ThreadingHTTPServer(
                ("127.0.0.1", 0), project_manager.make_handler(manager),
            )
            threading.Thread(target=server.serve_forever, daemon=True).start()
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)
            sink: dict = {}
            proxy = ThreadingHTTPServer(("127.0.0.1", 0), probe_proxy(
                f"http://127.0.0.1:{server.server_address[1]}", sink, probe,
            ))
            threading.Thread(target=proxy.serve_forever, daemon=True).start()
            self.addCleanup(proxy.server_close)
            self.addCleanup(proxy.shutdown)
            profile = tempfile.TemporaryDirectory()
            self.addCleanup(profile.cleanup)
            process = browser_acceptance.launch(
                f"http://127.0.0.1:{proxy.server_address[1]}/",
                Path(profile.name), width=1280, height=900,
            )
            try:
                deadline = time.monotonic() + 45
                while time.monotonic() < deadline and "value" not in sink:
                    time.sleep(0.1)
            finally:
                process.close()
        return sink.get("value") or {}

    def test_legal_page_visibly_shows_the_informational_hero_and_no_acceptance_language(self):
        """Relicensed 2026-09-23: the Legal page informs; it does not make the reader accept anything."""
        reading = self._render(LEGAL_PROBE)
        self.assertTrue(reading, "Chrome reported nothing for the Legal page")
        self.assertFalse(reading["hidden"], "Legal page did not open")
        self.assertEqual(reading["eyebrow"], "Open-source licensing and safety")
        self.assertEqual(reading["heading"], "Apache 2.0 and Operational Safety")
        self.assertEqual(reading["copy"], (
            "NoMoreHappyPath is licensed under Apache License 2.0. This page summarizes the license and provides "
            "informational safety guidance about autonomous agents; it does not modify the license or add conditions."
        ))
        for title in ("1. License — Apache License, Version 2.0", "2. Safety Notice (informational)",
                      "3. Third-Party Services", "4. Build Attribution"):
            self.assertIn(title, reading["titles"], f"section not visibly rendered: {title}")
        shown = " ".join(reading["innerText"].split()).lower()
        for phrase in ("legal terms", "disclaimer & limitation of liability", "accepting these terms",
                       "accept these terms", "do not accept them", "accept them in full", "complete, binding",
                       "indemnify", "hold harmless", "travis county"):
            self.assertNotIn(phrase, shown, f"the rendered Legal page still shows: {phrase!r}")

    def test_help_tells_the_owner_to_answer_the_first_time_trust_question(self):
        reading = self._render(TRUST_PROBE)
        self.assertTrue(reading, "Chrome reported nothing for the Help page")
        self.assertTrue(reading["visible"], "the first-time trust note is not visibly rendered in Help")
        shown = " ".join(reading["text"].split())
        for words in ("stay at your computer", "Do you trust this folder?", "Yes, I trust this folder", "Trust and continue", "View agent"):
            self.assertIn(words, shown)

    def test_help_explains_the_enhanced_security_checkbox_and_its_lock(self):
        """Owner 2026-10-12: a checkbox per task, locked at the first review request."""
        reading = self._render(SECURITY_PROBE)
        self.assertTrue(reading, "Chrome reported nothing for the Help page")
        self.assertTrue(reading["visible"], "the Enhanced security row is not visibly rendered in Help")
        shown = " ".join(reading["text"].split())
        for words in ("Enhanced security", "Off by default", "threat model", "security test pass", "controls register",
                      "before Delivery's first review request", "locked at review", "error catch-all"):
            self.assertIn(words, shown)

    def test_readme_carries_the_same_first_time_note(self):
        readme = (Path(__file__).resolve().parents[1] / "README.md").read_text()
        for words in ("First time an agent opens in a project", "Yes, I trust this folder", "Trust and continue"):
            self.assertIn(words, readme)

    def test_help_page_visibly_renders_every_guide_section(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / "home"
            manager = project_manager.ProjectManager(home, board_port=0)
            server = ThreadingHTTPServer(
                ("127.0.0.1", 0), project_manager.make_handler(manager),
            )
            threading.Thread(target=server.serve_forever, daemon=True).start()
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)
            sink: dict = {}
            proxy = ThreadingHTTPServer(("127.0.0.1", 0), probe_proxy(
                f"http://127.0.0.1:{server.server_address[1]}", sink, PROBE,
            ))
            threading.Thread(target=proxy.serve_forever, daemon=True).start()
            self.addCleanup(proxy.server_close)
            self.addCleanup(proxy.shutdown)
            profile = tempfile.TemporaryDirectory()
            self.addCleanup(profile.cleanup)
            process = browser_acceptance.launch(
                f"http://127.0.0.1:{proxy.server_address[1]}/",
                Path(profile.name), width=1280, height=900,
            )
            try:
                deadline = time.monotonic() + 45
                while time.monotonic() < deadline and "value" not in sink:
                    time.sleep(0.1)
            finally:
                process.close()
        reading = sink.get("value") or {}
        self.assertTrue(reading, "Chrome reported nothing for the Help page")
        self.assertFalse(reading["hidden"], "Help page did not open")
        for title in (
            "What This App Needs To Run",
            "When Claude Or Codex Runs Out Of Allowance",
            "Switch On Project Chat (OpenAI Key)",
            "Configure The Agents (Settings)",
            "Frame A Good Task",
            "Talking To The Agents Directly",
            "Check Your Version And Update",
            "Why It Takes Its Time (No Happy Path)",
            "Pause, Close, Or Remove A Project",
            "Your Responsibility, And What The Agents Can Do",
            "When Something Looks Wrong",
            "Ask About This Project",
            "Legal — Apache 2.0, and a Safety Notice",
        ):
            self.assertIn(title, reading["titles"], f"section not visibly rendered: {title}")
        self.assertGreaterEqual(reading["tables"], 3, "help tables missing from render")

    def test_the_usage_limit_help_and_faq_say_what_the_landed_code_does(self):
        from harness import attention, project_manager_page
        page = project_manager_page.PAGE
        start = page.index('id="help-limit-title"')
        text = page[start:page.index('id="help-version-title"')]
        for words in ("WAITING: USAGE LIMIT", "amber note", "(estimated)", "one</strong> short line", "Please continue where you stopped",
                      "check the board for its current assignment", "type any message into that agent", "never hands a review",
                      "does not switch an agent to another company", "two minutes"):
            self.assertIn(words, text)
        # the numbers in the text are the code's own
        self.assertEqual(attention.LIMIT_RESUME_GRACE_SECONDS, 120)
        self.assertEqual(attention.LIMIT_NUDGE_AFTER_SECONDS, 60)
        self.assertEqual(attention.LIMIT_ESTIMATED_MINUTES, 60)
        self.assertEqual(attention.nudge_text("delivery"), "Please continue where you stopped.")
        self.assertIn("check the board for your current assignment", attention.nudge_text("reviewer"))
        readme = (Path(__file__).resolve().parents[1] / "README.md").read_text(encoding="utf-8")
        for words in ("What happens when Claude or Codex runs out of my usage allowance?", "WAITING: USAGE LIMIT", "never handed to the company that wrote the work",
                      "does not switch vendors mid-task"):
            self.assertIn(" ".join(words.split()), " ".join(readme.split()))

    def test_the_launch_choice_help_and_faq_say_what_the_code_does(self):
        """Task 2026-10-17_launch-model-choice: one Help item, one FAQ entry, every sentence true of the code."""
        from harness import control, project_manager_page
        page = project_manager_page.PAGE
        start = page.index('id="help-settings-title"')
        text = page[start:page.index("</section>", start)]
        for words in ("Choosing a model for one launch", "that launch only", "Settings is not changed",
                      "The Independent Reviewer and the CTO always use their Settings values",
                      "never the Reviewer's provider", "keep the choice", "<code>/model</code>",
                      "does not yet explain it"):
            self.assertIn(words, text)
        # the sentences describe the code: the server refuses other roles with that very sentence, and a per-launch
        # choice is a stored flag the replacement and the pause capture carry
        self.assertEqual(control.LAUNCH_CHOICE_ROLE_SENTENCE, "the Independent Reviewer and the CTO always use their Settings values")
        import inspect
        self.assertIn('"launch_choice"', inspect.getsource(control.create))
        from harness import board
        self.assertIn('"launch_choice"', inspect.getsource(board))
        readme = (Path(__file__).resolve().parents[1] / "README.md").read_text(encoding="utf-8")
        for words in ("Can I use a strong model to write the spec and a cheaper one to code it?", "that launch only",
                      "Settings stays the default", "the Reviewer and the CTO always use Settings", "keep review independent",
                      "not yet explained by the harness"):
            self.assertIn(" ".join(words.split()), " ".join(readme.split()))


if __name__ == "__main__":
    unittest.main()
