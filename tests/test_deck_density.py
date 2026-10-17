"""2026-10-16: the Stage 2 Customer Research deck had 23 of 25 slides over 60 words and one of 229, and no directive
let the CTO or the Reviewer fail it or told Delivery how to build slides. Client-facing decks must be readable at a glance:
more than 3 items or about 50 words of body text on a slide is a material finding, proven by slide numbers and counts."""
import unittest
from pathlib import Path

from harness import claude_inbox, gemini_runtime

ROOT = Path(__file__).resolve().parents[1]
FULL = ("directives/AGENT.md", "harness/directives/CTO_COMPLETION_DIRECTIVE.md",
        "harness/directives/AUTONOMOUS_COMPLETION_DIRECTIVE.md")


def flat(path):
    return " ".join((ROOT / path).read_text(encoding="utf-8").split())


class DeckDensity(unittest.TestCase):
    def test_cto_rule_is_short_and_material(self):
        text = flat("directives/CTO.md")
        for phrase in ("Slide decks: density is content, not polish.",
                       "more than 3 items (a bullet with its explanation is one) or about 50 words of body text is material",
                       "counted on the delivered rendered slides (title, notes, sources excluded)",
                       "prove it by listing slide numbers with counts",
                       "Delivery splits it, one idea per slide, moves detail to speaker notes or a delivered companion document, never drops substance",
                       "This overrides the style exclusions and the executable-proof rule; punctuation and polish stay non-material"):
            self.assertIn(phrase, text)

    def test_full_rule_defines_the_counting_boundary_and_the_delivery_build_rule(self):
        for path in FULL:
            text = flat(path)
            with self.subTest(path=path):
                self.assertIn("Slide decks (client-facing): density is content, not polish.", text)
                self.assertIn("(open the file; text inside an image counts)", text)          # image text counts
                self.assertIn("The title, speaker notes, source line and chart or table labels are not body text", text)  # notes excluded
                self.assertIn("A bullet with its explanation, and its nested sub-bullets, is ONE item", text)  # 3 explained bullets are fine
                self.assertIn("A slide with more than 3 items, or more than about 50 words of body text, is a MATERIAL finding", text)
                self.assertIn("prove it by listing the slide numbers with their item and word counts for the exact inspected file", text)
                self.assertIn("one idea per slide, detail moved to speaker notes or a companion document that is delivered with the deck", text)
                self.assertIn("the substance must stay in the package the client receives", text)
                self.assertIn("the corrected deck gets a fresh judgment", text)
                self.assertIn("layout polish (alignment, spacing, fonts) stays non-material", text)

    def test_the_text_content_rule_is_kept_whole(self):
        self.assertIn("Text deliverables (deck, copy, strategy, report): reading the text is the evidence", flat("directives/CTO.md"))
        for path in FULL:
            self.assertIn("Text deliverables: for text a human reads (deck, copy, strategy, report) the substance is the product", flat(path))

    def test_cto_payload_stays_in_the_gemini_budget(self):
        size = len((ROOT / "directives/CTO.md").read_bytes()) + len(claude_inbox.AUTHORITY_NOTE.encode("utf-8")) + 2
        self.assertLessEqual(size, gemini_runtime.RULES_LIMIT_BYTES)


if __name__ == "__main__":
    unittest.main()
