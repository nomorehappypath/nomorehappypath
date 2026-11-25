"""2026-11-25 (owner: "A slide is not an app it should not fail the review"): a crowded slide is one note. It never fails a
review, blocks or holds a deck; Delivery still aims for 3 items and about 50 words a slide."""
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FULL = ("directives/AGENT.md",)


def flat(path):
    return " ".join((ROOT / path).read_text(encoding="utf-8").split())


class DeckDensity(unittest.TestCase):
    def test_a_crowded_slide_never_fails_or_blocks_anywhere(self):
        for path, phrase in (
            ("directives/AGENT.md", "a crowded slide never fails a review, never blocks anything and never needs a new judgment"),
            ("directives/CTO.md", "A crowded slide never fails a review, never blocks and is never a reason to hold or send back a deck"),
            ("directives/cards/CTO.md", "a crowded slide is only a note and never holds a deck"),
            ("directives/cards/REVIEWER.md", "never fails a review: a slide is not an app"),
        ):
            with self.subTest(path=path):
                self.assertIn(phrase, flat(path))
        for path in ("directives/AGENT.md", "directives/CTO.md", "directives/cards/CTO.md", "directives/cards/DELIVERY.md", "directives/cards/REVIEWER.md"):
            with self.subTest(path=path):
                self.assertNotIn("slide density", flat(path))
                self.assertNotIn("density is content", flat(path))
                self.assertNotRegex(flat(path), r"(?i)50 words[^.]{0,60} (is|are) (a )?material")

    def test_the_build_guidance_and_the_substance_are_kept(self):
        text = flat("directives/AGENT.md")
        self.assertIn("Delivery still aims for at most 3 items and about 50 words of body text per slide: one idea per slide", text)
        self.assertIn("the substance must stay in the package the client receives", text)

    def test_the_text_content_rule_is_kept_whole(self):
        self.assertIn("Text deliverables (deck, copy, strategy, report): reading the text is the evidence", flat("directives/CTO.md"))
        for path in FULL:
            self.assertIn("Text deliverables: for text a human reads (deck, copy, strategy, report) the substance is the product", flat(path))

    def test_the_cards_send_delivery_and_the_reviewer_to_the_deck_chapter(self):
        # Directive diet: the card is what is always loaded; the counting rules sit in the `text-deliverables` chapter it names.
        from harness import reference_chapters
        for role in ("engineering", "qa"):
            rules = [sentence for _n, sentence, targets in reference_chapters.card_rules(role) if "text-deliverables" in targets and "50 words" in sentence]
            self.assertTrue(rules, role)
            self.assertIn("3 items", rules[0])
        chapter = reference_chapters.role_chapters("engineering")["text-deliverables"]
        self.assertIn("Slide decks (client-facing): a slide is not an app", chapter)


if __name__ == "__main__":
    unittest.main()
