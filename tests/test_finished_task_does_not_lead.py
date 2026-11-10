"""An accepted task that needs nothing never leads the page, so the way to start the next task is always there (owner, 2026-11-10)."""
import unittest

from harness import story


class FinishedTaskDoesNotLead(unittest.TestCase):
    def page(self, tasks):
        built = {name: dict(task, task=name) for name, task in tasks.items()}
        original = story.build
        story.build = lambda state, contracts, task, *a, **k: built[task]
        try:
            return story.for_page({}, {}, list(built))
        finally:
            story.build = original

    def test_an_accepted_task_alone_leaves_nothing_leading(self):
        page = self.page({"done-task": {"accepted": True, "needs_owner": False, "exception": None, "step": 26, "title": "Done Task"}})
        self.assertIsNone(page["current"])
        self.assertEqual(page["last_accepted"]["title"], "Done Task")

    def test_a_running_task_leads_even_when_another_was_accepted(self):
        page = self.page({"done-task": {"accepted": True, "needs_owner": False, "exception": None, "step": 26, "title": "Done"},
                          "live-task": {"accepted": False, "needs_owner": False, "exception": None, "step": 8, "title": "Live"}})
        self.assertEqual(page["current"]["task"], "live-task")

    def test_an_accepted_task_with_a_problem_still_leads(self):
        page = self.page({"done-task": {"accepted": True, "needs_owner": True, "exception": None, "step": 26, "title": "Done"}})
        self.assertEqual(page["current"]["task"], "done-task")


if __name__ == "__main__":
    unittest.main()
