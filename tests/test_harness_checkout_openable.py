"""A task that changes NoMoreHappyPath itself has an app to open (owner, 2026-11-09)."""
import tempfile
import unittest
from pathlib import Path

from harness import board, board_viewer, release_preview


def make_checkout(base: Path) -> Path:
    root = base / "nmhp"
    (root / "scripts").mkdir(parents=True)
    (root / "harness").mkdir()
    (root / "scripts" / "start_project_manager.sh").write_text("#!/usr/bin/env bash\n")
    (root / "harness" / "project_manager.py").write_text("")
    return root


class HarnessCheckoutOpenable(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)

    def test_start_command_uses_its_own_port_and_data_folder(self):
        start = release_preview.detect_start(make_checkout(self.base))
        command = start["command"]
        self.assertIn("scripts/start_project_manager.sh", command)
        self.assertIn("--home {state_dir}/home", command)
        self.assertIn("--port {port}", command)
        self.assertIn("--worker-port", command)
        self.assertIn("--no-open", command)
        self.assertTrue(command.startswith("HOME=~ "), "the preview environment has no HOME; the script needs one")

    def test_a_folder_that_is_not_nmhp_is_not_mistaken_for_it(self):
        plain = self.base / "plain"
        (plain / "scripts").mkdir(parents=True)
        (plain / "scripts" / "start_project_manager.sh").write_text("")
        self.assertFalse(release_preview.is_harness_checkout(plain))
        self.assertFalse(release_preview.is_harness_checkout(""))

    def test_the_old_frozen_not_an_app_label_is_corrected_on_the_page_only(self):
        checkout = make_checkout(self.base)
        other = self.base / "docs-only"
        other.mkdir()
        state = {
            "task_workspaces": {"nmhp-task": str(checkout), "doc-task": str(other)},
            "releases": {"nmhp-task": {"status": "VISUAL_TEST_REQUIRED", "app_openable": False},
                         "doc-task": {"status": "VISUAL_TEST_REQUIRED", "app_openable": False}},
        }
        compact = board_viewer._compact_dashboard_state(state, ["nmhp-task", "doc-task"])
        self.assertTrue(compact["releases"]["nmhp-task"]["app_openable"])
        self.assertFalse(compact["releases"]["doc-task"]["app_openable"])
        self.assertFalse(state["releases"]["nmhp-task"]["app_openable"], "the stored decision is untouched")


if __name__ == "__main__":
    unittest.main()
