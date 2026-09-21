# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
"""The Help disclosure must match what the code actually does.

The reviewer proved by execution that the previous wording — "Changes are held
to your project folder. Writing is what the harness confines today." — was
FALSE: the managed Codex agent launches with sandbox_mode=danger-full-access and
wrote to a sibling directory outside the project root.

That claim predated the move into Help; the move carried it forward and gave it
a more prominent home. A false safety claim in user-facing copy is the exact
failure this product is named after, so it is pinned here rather than trusted to
stay true.
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

from harness.project_manager_page import PAGE

ROOT = Path(__file__).resolve().parent.parent
RUNNER = (ROOT / "scripts" / "run_managed_agent.sh").read_text(encoding="utf-8")


def help_text() -> str:
    block = PAGE[PAGE.index("help-reach-title"):]
    block = block[:block.index("</section>")]
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", block))


class ClaimTruthTests(unittest.TestCase):
    def test_writes_really_are_confined_at_the_launch_site(self):
        """The premise of the Help copy, pinned to the code that provides it."""
        self.assertIn("sandbox_mode=workspace-write", RUNNER)
        self.assertNotIn("danger-full-access", RUNNER,
                         "the sandbox is switched off again; the Help copy now "
                         "claims protection that does not exist")

    def test_the_harness_own_paths_are_granted_through_a_VALIDATED_list(self):
        """workspace-write alone blocks the sibling task workspace.

        The roots used to be interpolated raw. A reviewer showed that an adopted
        project can name a broad ancestor, or a symlink resolving to one, so they
        are now validated first and the launch refuses a grant that is too wide.
        """
        self.assertIn("sandbox_workspace_write.writable_roots", RUNNER)
        self.assertIn("agent_writable_roots", RUNNER,
                      "the roots must be validated, not interpolated raw")
        self.assertIn("writable_roots=${writable_roots_json}", RUNNER)
        self.assertNotIn('writable_roots=[\\"${data_root}\\"', RUNNER,
                         "the raw owner-supplied roots must not reach the sandbox flag")

    def test_help_claims_write_confinement_only_because_it_is_enforced(self):
        text = help_text().lower()
        self.assertIn("cannot change files outside your project", text)
        self.assertIn("refused by the operating system", text)

    def test_help_STILL_says_reads_are_not_confined(self):
        """The half that is not fixed must not quietly disappear from the copy.

        Enforcing writes makes it tempting to write reassuring text. Codex has
        no read-scoping mode, so a reader who takes 'confined' to mean 'private'
        would be misled in the other direction.
        """
        text = help_text().lower()
        self.assertIn("can still read anything you can", text)
        self.assertIn("read confinement is not available today", text)

    def test_help_still_credits_the_protection_that_DOES_exist(self):
        """Honest means accurate in both directions, not merely alarming."""
        text = help_text().lower()
        self.assertIn("own git work is sandboxed", text)
        self.assertIn("sandbox", text)

    def test_help_does_not_invite_discounting_the_present_risk(self):
        text = help_text().lower()
        self.assertIn("not available today", text)


class NoStaleNoticeTests(unittest.TestCase):
    def test_nothing_still_describes_a_mandatory_project_view_warning(self):
        """The reviewer's non-blocking finding: docs outliving the thing."""
        offenders = []
        for path in list(ROOT.glob("harness/*.py")) + list(ROOT.glob("docs/**/*.md")):
            body = path.read_text(encoding="utf-8", errors="ignore")
            if "disclosed on every" in body and "project view" in body:
                offenders.append(path.relative_to(ROOT).as_posix())
        self.assertEqual(offenders, [], f"stale description of the removed notice: {offenders}")


if __name__ == "__main__":
    unittest.main()
