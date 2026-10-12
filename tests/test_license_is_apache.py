# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""The licence is one thing, said the same way everywhere it is said.

Relicensed to Apache 2.0 on 2026-09-23. Every surface that names the licence
— the LICENSE and NOTICE files, README, CONTRIBUTING, DISCLAIMER, the app's
Legal page and 226 source headers — must agree, and the old licence must not
survive anywhere in the shipped tree. A grep at the time found the previous
text in exactly these places; this test keeps them from drifting apart. The
old licence's name is assembled from parts below so that this file, which is
itself in the tree, never carries the phrase it hunts.
"""
from __future__ import annotations

import re
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APACHE = "Apache License, Version 2.0"
HEADER = "Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0"
CANONICAL_SHA256 = "cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30"   # sha256 of https://www.apache.org/licenses/LICENSE-2.0.txt, 2026-09-23
OLD_LICENSE = "Business" + " Source"      # the previous licence, never written whole here
OLD_ABBREVIATION = "BU" + "SL"
# Wording that would turn the informational safety notice into a binding term. Matched
# case-insensitively against the whole document, so a change of grammatical form
# ("accept" vs "accepting") cannot slip past the guard again (reviewer finding, 2026-09-23).
BINDING_PHRASES = (
    "indemnify", "hold harmless", "travis county", "is zero", "you consent to that jurisdiction",
    "binding text", "complete, binding",
    "accept these terms", "accepting these terms", "do not accept them", "accept them in full",
    "legal terms", "disclaimer & limitation of liability",
)


SKIP_DIRS = {".git", "node_modules", "__pycache__", ".harness", "docs"}


def _shipped_names() -> list[str]:
    """Tracked files in the source tree; every file in the assembled public tree.

    The public tree the release script builds carries no git metadata, and the
    suite runs inside it as the release gate, so the guard walks the tree when
    git cannot list it.
    """
    listed = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True)
    if listed.returncode == 0 and listed.stdout.strip():
        return listed.stdout.split()
    names = []
    for path in ROOT.rglob("*"):
        if path.is_file() and not (set(path.relative_to(ROOT).parts[:-1]) & SKIP_DIRS):
            names.append(str(path.relative_to(ROOT)))
    return names


def tracked_text_files() -> list[Path]:
    files = []
    for name in _shipped_names():
        path = ROOT / name
        if not path.is_file() or name.startswith("docs/"):
            continue
        try:
            path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        files.append(path)
    return files


class LicenseIsApacheTests(unittest.TestCase):
    def test_the_license_file_is_the_canonical_apache_text_applied_to_this_product(self):
        raw = (ROOT / "LICENSE").read_bytes()
        import hashlib
        self.assertEqual(hashlib.sha256(raw).hexdigest(), CANONICAL_SHA256,
                         "LICENSE must be the canonical Apache 2.0 text, byte for byte; attribution belongs in NOTICE")
        notice = (ROOT / "NOTICE").read_text(encoding="utf-8")
        self.assertIn("Copyright 2026 KpiMinds LLC", notice)
        self.assertIn(APACHE, notice)

    def test_notice_readme_contributing_disclaimer_and_the_legal_page_agree(self):
        for name in ("NOTICE", "README.md", "CONTRIBUTING.md", "DISCLAIMER.md"):
            body = " ".join((ROOT / name).read_text(encoding="utf-8").split())
            self.assertIn(APACHE, body, name)
            self.assertNotIn(OLD_LICENSE, body, name)
            self.assertNotIn("commercial license from", body, name)
        page = " ".join((ROOT / "harness" / "project_manager_page.py").read_text(encoding="utf-8").split())
        self.assertIn("1. License — Apache License, Version 2.0", page)
        self.assertIn("open source under the <strong>Apache License, Version 2.0</strong>", page)
        self.assertNotIn(OLD_LICENSE, page)
        self.assertNotIn("requires a commercial license", page)

    def test_the_safety_notice_is_informational_and_adds_no_conditions(self):
        """The owner asked for Apache 2.0 plus an informational safety notice, not extra binding terms."""
        disclaimer = " ".join((ROOT / "DISCLAIMER.md").read_text(encoding="utf-8").split())
        page = " ".join((ROOT / "harness" / "project_manager_page.py").read_text(encoding="utf-8").split())
        readme = " ".join((ROOT / "README.md").read_text(encoding="utf-8").split())
        page_plain = page.replace("&amp;", "&")
        for text, name in ((disclaimer, "DISCLAIMER.md"), (page_plain, "Legal page")):
            self.assertIn("This notice is informational", text, name)
            self.assertIn("adds no conditions to", text, name)
            for binding in BINDING_PHRASES:
                self.assertNotIn(binding, text.lower(), f"{name} still imposes a term: {binding!r}")
        self.assertNotIn("Using the software means accepting", readme)
        self.assertIn("adds no conditions", readme)

    def test_the_legal_page_hero_is_informational(self):
        """The reviewer found the hero above the numbered sections still demanded acceptance (2026-09-23)."""
        page = " ".join((ROOT / "harness" / "project_manager_page.py").read_text(encoding="utf-8").split())
        hero = page[page.index('<section id="legal-page"'):page.index('<div class="help-shell">', page.index('<section id="legal-page"'))]
        self.assertIn('<div class="eyebrow">Open-source licensing and safety</div>', hero)
        self.assertIn("<h1>Apache 2.0 and Operational Safety</h1>", hero)
        self.assertIn("NoMoreHappyPath is licensed under Apache License 2.0. This page summarizes the license and provides "
                      "informational safety guidance about autonomous agents; it does not modify the license or add conditions.", hero)

    def test_every_source_header_names_the_same_license_and_no_file_names_the_old_one(self):
        header_re = re.compile(r"Copyright \(c\) 2026 KpiMinds LLC\. Licensed under ([^;]+); see LICENSE\. SPDX-License-Identifier: Apache-2\.0")
        wrong_headers, old_license = [], []
        for path in tracked_text_files():
            text = path.read_text(encoding="utf-8")
            if OLD_LICENSE in text or OLD_ABBREVIATION in text:
                old_license.append(str(path.relative_to(ROOT)))
            for match in header_re.finditer(text):
                if match.group(1) != "the Apache License, Version 2.0":
                    wrong_headers.append(f"{path.relative_to(ROOT)}: {match.group(0)}")
        self.assertEqual(old_license, [], "the previous licence survives in: " + ", ".join(old_license))
        self.assertEqual(wrong_headers, [], "\n".join(wrong_headers))
        headers = sum(1 for path in tracked_text_files() if HEADER in path.read_text(encoding="utf-8"))
        self.assertGreaterEqual(headers, 200, f"only {headers} files carry the header; the sweep missed some")




class GeminiLegalCoverageTests(unittest.TestCase):
    """Owner 2026-10-12: Gemini and its legal protections must be in the notice, the Legal page, the README and Help."""

    @staticmethod
    def plain(text):
        return " ".join(text.replace("&amp;", "&").replace("<strong>", "").replace("</strong>", "").replace("<code>", "").replace("</code>", "").split())

    def legal_page(self):
        page = (ROOT / "harness" / "project_manager_page.py").read_text(encoding="utf-8")
        start = page.index('<section id="legal-page"')
        return self.plain(page[start:page.index("</main>", start)])

    def help_page(self):
        page = (ROOT / "harness" / "project_manager_page.py").read_text(encoding="utf-8")
        start = page.index('<section id="help-page"')
        return self.plain(page[start:page.index('<section id="legal-page"', start)])

    SENTENCES = (
        "Antigravity CLI (which runs Gemini models)",
        "Google accounts and API keys",
        "that choice is yours, and {sw} never makes it for you",
        "its prompts, the files it reads, command output and other content its sandbox lets it read, not only your project folder, can be sent to Google by that CLI under Google's terms and your choice",
        "A free Google account has a small usage quota; Google's plans and billing are between you and Google",
        "accepts no API key for Gemini",
        "not affiliated with, sponsored by or endorsed by OpenAI, Anthropic or Google",
        "names or trademarks of their respective owners",
        "As sections 7 and 8 of the license state, and to the extent applicable law allows, unless a law or a written agreement says otherwise,",
        "is not liable for bugs, defects, security flaws or lost data in {sw} or in what the agents produce",
        "The license covers {sw}, not the projects you build with it",
    )

    def test_notice_and_legal_page_carry_the_same_google_and_liability_statements(self):
        disclaimer = self.plain((ROOT / "DISCLAIMER.md").read_text(encoding="utf-8"))
        legal = self.legal_page()
        for text, name, sw in ((disclaimer, "DISCLAIMER.md", "the Software"), (legal, "Legal page", "this software")):
            for sentence in self.SENTENCES:
                wanted = sentence.format(sw=sw)
                if wanted.startswith("that choice") and name == "DISCLAIMER.md":
                    wanted = wanted.replace("{sw}", sw)
                self.assertIn(wanted.replace("the Software never", "the Software never").replace("this software never", "this software never"), text, f"{name} is missing: {wanted}")
            for binding in BINDING_PHRASES:
                self.assertNotIn(binding, text.lower(), name)
            self.assertNotIn("any other outcome", text, name)

    def test_the_notice_and_the_legal_page_say_the_same_in_sections_two_and_three(self):
        def norm(text):
            return self.plain(text).lower().replace("this software", "the software").replace(",", "").replace("</p> <p>", " ")

        def between(text, start, end):
            text = norm(text)
            start, end = norm(start), norm(end)
            return text[text.index(start):text.index(end) + len(end)]

        disclaimer = (ROOT / "DISCLAIMER.md").read_text(encoding="utf-8")
        legal = self.legal_page()
        two = ("inside the folders you designate", "unexpected charges.")
        three = ("The Codex CLI, Claude Code CLI, the OpenAI API", "can work with.")
        for start, end in (two, three):
            self.assertEqual(between(disclaimer, start, end), between(legal, start, end), f"notice and Legal page differ at: {start}")

    def test_readme_and_help_tell_the_owner_where_a_gemini_agents_content_can_go(self):
        readme = self.plain((ROOT / "README.md").read_text(encoding="utf-8"))
        self.assertIn("can send what the agent reaches (not only the project folder) to Google", readme)
        self.assertIn("not affiliated with or endorsed by OpenAI, Anthropic or Google", readme)
        self.assertIn("not the responsibility of KpiMinds LLC to the extent applicable law allows", readme)
        self.assertIn("travel to OpenAI, Anthropic or Google (for a Gemini agent)", self.help_page())
        self.assertIn("are not the responsibility of KpiMinds LLC to the extent applicable law allows", self.help_page())
        self.assertIn("can send what the agent reaches (prompts, files it reads, command output, not only your project folder) to Google", self.help_page())

    def test_help_does_not_promise_gemini_appears_as_soon_as_it_is_installed(self):
        help_text = self.help_page()
        self.assertNotIn("as soon as agy is installed", help_text)
        self.assertIn("once agy is installed and the app's own checks of that CLI pass", help_text)
        self.assertIn("only confirms the sign-in and the model, it does not switch Gemini on", help_text)


if __name__ == "__main__":
    unittest.main()
