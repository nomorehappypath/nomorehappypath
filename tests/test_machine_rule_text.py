# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""The agents' test-machine rule, revision 2 (windows-walk items 1, 2, 6, 7): the text is there, and it reaches every agent.

Machine inputs removed: none are read; this tests strings and the launch/announcement builders.

Run:  PYTHONPATH=. python3 -m unittest tests.test_machine_rule_text -v
"""
from __future__ import annotations

import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from harness import board, test_machine, test_machine_followup
from tests.test_test_machine import connected_descriptor, make_context

ROOT = Path(__file__).resolve().parents[1]


def flat(text: str) -> str:
    return " ".join(text.split())


class AgentDirectiveTests(unittest.TestCase):
    def setUp(self):
        self.agent = flat((ROOT / "directives" / "AGENT.md").read_text())

    def test_the_lock_is_an_os_held_file_never_freed_by_age_or_an_agent_id(self):
        for needle in ("The lock (version 2) is a folder named like the project's older lock folder with `-v2` appended",
                       "[System.IO.File]::Open(<path>,'OpenOrCreate', 'ReadWrite','None')".replace("', '", "','"),
                       "Python `fcntl.flock`", "children started with Python `subprocess`, never `os.fork` without exec",
                       "`holder.txt` (`v2 <agent id> <role> pid=<the lock script's pid> start=<start time> since=<time>`) by writing a temporary file and renaming it",
                       "releases the lock the instant the script ends or dies, so a dead holder, your own earlier run included, never blocks anyone",
                       "retries only that contention, for a bounded time", "any other error fails at once with its real cause",
                       "never deleted, renamed or replaced by cleanup; age alone never frees a lock, and an agent id is never proof of anything"):
            self.assertIn(flat(needle).replace("', '", "','"), self.agent.replace("', '", "','"), needle)
        self.assertNotIn("a lock is taken over only after the target proves that holder is gone", self.agent, "the old takeover rule is gone")

    def test_an_old_lock_folder_has_one_disposition_with_positive_reboot_evidence(self):
        for needle in ("If the project's OLD lock folder still exists (any content)", "the CTO asks the owner once to approve a restart of the machine",
                       "requires a CHANGED boot identity", "macOS `sysctl -n kern.boottime`", "Linux `cat /proc/sys/kernel/random/boot_id` or `uptime -s`",
                       "(Get-CimInstance Win32_OperatingSystem).LastBootUpTime", "a denied restart, unreachable ssh or changed host identity fails and names the cause for the CTO",
                       "removes the old folder and takes the version 2 lock",
                       "A script from before this revision is updated to this lock by Delivery after the owner's reconnect"):
            self.assertIn(needle, self.agent)

    def test_the_boot_identity_commands_exist_on_the_platform_they_are_prescribed_for(self):
        # executed here, not assumed: the reviewer found the Linux commands failing on macOS (illegal option -s / no /proc)
        import shutil
        import subprocess
        import sys
        if sys.platform == "darwin":
            result = subprocess.run(["sysctl", "-n", "kern.boottime"], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("sec", result.stdout)
            self.assertNotEqual(subprocess.run(["uptime", "-s"], capture_output=True, text=True).returncode, 0, "uptime -s is not a macOS option")
        elif sys.platform.startswith("linux"):
            self.assertTrue(Path("/proc/sys/kernel/random/boot_id").read_text().strip())
            if shutil.which("uptime"):
                self.assertEqual(subprocess.run(["uptime", "-s"], capture_output=True, text=True).returncode, 0)

    def test_the_windows_recipe_has_its_seven_facts_and_the_exit_status_rule(self):
        for needle in ("copy files with `scp` and never embed source in the ssh command",
                       "powershell -NoProfile -ExecutionPolicy Bypass -File <script>", "`& <cmd>; $LASTEXITCODE`, never `Start-Process`",
                       "a native command that writes to stderr is not an error", "a local user name is at most 20 characters",
                       "`Remove-LocalUser` throws on a user that is not there", "the default ssh shell is `cmd.exe`",
                       "prints the runner's own `Ran <n> tests` summary line",
                       "exits non-zero when the tests, the setup or the cleanup failed (tests passing while the cleanup fails is a failure)"):
            self.assertIn(needle, self.agent)

    def test_the_reviewer_reuses_deliverys_route_and_does_not_force_unreproducible_scenarios(self):
        for needle in ("your first machine row is Delivery's certified command text", "so you do not write a transfer, lock or clean protocol of your own",
                       "`test-<name>` targets or arguments of the same `make test` route", "not reproducible here: <reason>",
                       "supplements, and never replaces, a required real run", "never an `N/A` row",
                       "Nobody creates accounts, services, scheduled tasks or registry entries on the owner's machine to force a scenario"):
            self.assertIn(needle, self.agent)

    def test_cleanup_names_leftovers_and_restores_only_what_the_run_created(self):
        for needle in ("local accounts), recording the owner and the prior state of each", "removing or restoring only what the run created, never something that was there before",
                       "a leftover it cannot remove fails the run and names it"):
            self.assertIn(needle, self.agent)

    def test_one_bootstrap_owner_and_the_stale_message_names_who_resubmits(self):
        self.assertIn("Delivery alone does the one step allowed before the lock", self.agent)
        self.assertIn("the Reviewer does no machine work until `--done`", self.agent)
        self.assertIn("Delivery resubmits and a fresh review request is made", self.agent)


class DeliveryTests(unittest.TestCase):
    """RULE_TEXT is the one place that reaches every role, provider and resumed prompt."""

    NEEDLES = ("Test machine rule (revision 2)", "<your lock folder>-v2", "frees the lock when the script dies", "never free a lock by age or by an agent id",
               "requires a changed boot identity", "the script exits non-zero if the tests, the setup or the cleanup failed",
               "powershell -NoProfile -ExecutionPolicy Bypass -File", "& cmd; $LASTEXITCODE", "Remove-LocalUser throws on a missing user",
               "your first machine row is Delivery's certified command text", "not reproducible here: <reason>",
               "never create accounts, services, tasks or registry entries on the owner's machine to force it")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.context = make_context(Path(self.tmp.name))

    def test_the_rule_text_and_revision_carry_the_new_rules(self):
        self.assertEqual(test_machine.RULE_REVISION, 2)
        for needle in self.NEEDLES:
            self.assertIn(needle, test_machine.RULE_TEXT)

    def test_every_launch_note_and_the_connect_announcement_and_machine_status_carry_them(self):
        connected_descriptor(self.context)
        for text in (test_machine.launch_note(self.context), test_machine_followup.connection_message(self.context),
                     test_machine.status_report(self.context)["rule"]):
            for needle in self.NEEDLES:
                self.assertIn(needle, text)
        self.assertEqual(test_machine.status_report(self.context)["rule_revision"], 2)

    def test_the_rule_is_short_enough_to_read_in_a_first_message(self):
        self.assertLess(len(test_machine.RULE_TEXT), 3200)

    def test_the_baseline_begin_response_names_the_bootstrap_exception(self):
        source = (ROOT / "harness" / "board.py").read_text()
        self.assertIn("the one exception is installing Python 3 on a macOS or Linux machine that lacks it, which is Delivery's one first step", source)

if __name__ == "__main__":
    unittest.main()
