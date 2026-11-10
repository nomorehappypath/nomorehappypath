"""The task says what kind of work it is: Write code, Analyze code or Write specs (owner, 2026-11-10)."""
import unittest

from harness import story


class TaskKindLabel(unittest.TestCase):
    def test_the_three_kinds_have_the_words_of_the_start_screen(self):
        state = {"task_kinds": {"a": "development", "b": "analysis", "c": "spec"}}
        self.assertEqual([story.kind_label(state, name) for name in "abc"], ["Write code", "Analyze code", "Write specs"])

    def test_an_unknown_or_missing_kind_reads_as_write_code(self):
        self.assertEqual(story.kind_label({}, "x"), "Write code")
        self.assertEqual(story.kind_label({"task_kinds": {"x": "odd"}}, "x"), "Write code")


if __name__ == "__main__":
    unittest.main()
