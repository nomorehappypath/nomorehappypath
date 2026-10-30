# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""The Help section "Create The Windows Test Machine" says what the code does, and is reachable from the card.

Run:  PYTHONPATH=. python3 -m unittest tests.test_windows_vm_help -v
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

from harness import test_machine
from harness.project_manager_page import PAGE
from tests import test_test_machine_rendered as rendered

ROOT = Path(__file__).resolve().parent.parent
LINES = ("Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0",
         "Set-Service -Name sshd -StartupType Automatic", "Start-Service sshd")


def section() -> str:
    block = PAGE[PAGE.index('id="help-windows-vm"'):]
    return block[:block.index("</section>")]


def plain(html: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))


class HelpTextTests(unittest.TestCase):
    def test_the_three_user_steps_and_the_owners_host_agnostic_wording(self):
        text = plain(section())
        for needle in ("Why.", "Where it runs does not matter", "Create it and size it.", "Turn on the ssh server", "Make sure this computer can reach it", "Give NoMoreHappyPath access",
                       "Everything else is the agents", "ARM or the x86-64", "Administrator account", "Snapshot", "restore a snapshot",
                       "If the address changes", "Release this machine", "never creates, resets or deletes", "Permission denied", "and Connect again", "wipe and reinstall"):
            self.assertIn(needle, text)
        for line in LINES:
            self.assertIn(line, section())
        self.assertNotIn("never harm", text)
        self.assertIn(r"C:\ProgramData\ssh\ssh_host_ed25519_key.pub", section())
        for hypervisor in ("VMware", "Hyper-V"):  # per-hypervisor detail stays in the one short optional note
            self.assertEqual(plain(section()).count(hypervisor), 1)
        self.assertLessEqual(plain(section()).count("UTM"), 2)  # the optional note and the one creation link

    def test_the_numbers_match_the_connect_dialogs_prerequisites(self):
        joined, text = " ".join(test_machine.PREREQUISITES), plain(section())
        for number in ("4 GB", "64 GB", "8 GB", "100 GB"):
            self.assertIn(number, joined)
            self.assertIn(number, text)
        self.assertIn("2 cores", joined)
        self.assertIn("2 processor cores", text)
        self.assertIn("Release this machine", PAGE[PAGE.index('id="machine-dialog"'):] + PAGE)

    def test_the_card_the_dialog_and_the_readme_point_to_it(self):
        self.assertIn('href="?page=help#help-windows-vm" data-help-link', PAGE)
        self.assertIn("helpLink", PAGE)  # the card on the project row carries it too
        viewer = (ROOT / "harness" / "board_viewer.py").read_text(encoding="utf-8")
        self.assertIn("?page=help#help-windows-vm", viewer)
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        for line in LINES:
            self.assertIn(line, readme)
        self.assertIn("Create The Windows Test Machine", readme)


class HelpRenderedTests(rendered.TestMachineRenderedTests):
    """Rendered: from the connect dialog the owner follows the link and lands on the section."""
    def test_the_dialog_link_opens_help_at_the_section(self):
        url, _manager = self.serve()
        run = self.walk(url, [
            {"click": {"text": "Advanced"}},
            {"click": {"text": "Test machine"}},
            {"expect": "How to create the Windows test machine"},
            {"click": {"selector": "a[data-help-link]"}},
            {"expect": "Create The Windows Test Machine"},
            {"expect": "Start-Service sshd"},
            {"expect": "Everything else is the agents"},
        ])
        self.assert_passed(run)


for _name in [n for n in dir(HelpRenderedTests) if n.startswith("test_") and n != "test_the_dialog_link_opens_help_at_the_section"]:
    setattr(HelpRenderedTests, _name, None)  # the parent's walks are not re-run here

if __name__ == "__main__":
    unittest.main()
