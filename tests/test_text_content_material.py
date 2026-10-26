"""2026-10-15: a text deliverable (a deck) passed review with "BLOCKING: none" although it presented founder
inference as buyer research, repeated its thesis and carried an unclear line, because every directive listed
wording and style as never material. The CTO and Reviewer directives must now treat weak, ungrounded or
off-brief content of a text deliverable as material, proven by quoting it, while punctuation and cosmetic polish
stay non-material and the CTO payload stays inside the Gemini rules budget."""
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FILES = (
    "directives/CTO.md",
    "directives/AGENT.md",
    "harness/directives/CTO_COMPLETION_DIRECTIVE.md",
    "harness/directives/AUTONOMOUS_COMPLETION_DIRECTIVE.md",
)


def flat(path):
    return " ".join((ROOT / path).read_text(encoding="utf-8").split())


class TextContentIsMaterial(unittest.TestCase):
    def test_every_role_directive_judges_text_substance_by_quoting_it(self):
        for path in FILES:
            text = flat(path)
            cto = path == "directives/CTO.md"
            with self.subTest(path=path):
                self.assertIn("Text deliverables (deck, copy, strategy, report): reading the text is the evidence" if cto
                              else "Text deliverables: for text a human reads (deck, copy, strategy, report) the substance is the product", text)
                self.assertIn("ungrounded (stated as fact or research with no source), generic, off-brief or unclear, or a missing requested part" if cto
                              else "ungrounded (a claim presented as fact or research with no source), generic, off-brief or unclear, or a requested part that is missing", text)
                self.assertIn("when it misleads the reader or fails the brief's purpose", text)
                self.assertIn("This overrides the", text)
                self.assertIn("executable-proof rule", text)
                self.assertIn("punctuation and polish stay non-material" if cto else "punctuation and cosmetic polish stay non-material", text)
                self.assertIn("Labeled hypotheses and recaps are fine" if cto else "Labeled hypotheses and deliberate recaps are fine", text)

    def test_required_text_fixes_block_and_optional_ones_do_not(self):
        for path in FILES:
            text = flat(path)
            with self.subTest(path=path):
                self.assertTrue("required fixes block completion, optional ones are notes" in text
                                or "Required fixes are BLOCKING and need a fresh judgment after correction; optional ones are summary notes" in text)

    def test_software_style_exclusions_are_kept(self):
        self.assertIn("A misplaced comma, naming preference, cosmetic defect", flat("directives/CTO.md"))
        self.assertIn("test housekeeping, wording and style are NON-BLOCKING notes", flat("directives/AGENT.md"))

if __name__ == "__main__":
    unittest.main()
