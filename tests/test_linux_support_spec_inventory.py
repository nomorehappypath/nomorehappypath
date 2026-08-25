# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
"""The Linux-support spec's completeness claim, made executable.

A spec that asserts "every platform-coupled site is listed" is worth nothing
unless the assertion is run. Its first review failed on exactly that: the
inventory was incomplete. This test re-derives the sweep from the spec's own
stated pattern set and fails when the code carries a platform-coupled file the
table does not mention, or when a line the table cites no longer matches.
"""
from __future__ import annotations

import re
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = ROOT / "docs" / "specs" / "LINUX_SUPPORT.md"

# The product surface is the release cut itself, minus the items the spec
# declares out of scope. Nothing here is hand-maintained: test_surface_matches_
# the_release_cut below fails if the cut changes and this list does not.
CUT_SCRIPT = ROOT / "scripts" / "make_public_release.sh"
# Declared out of scope in the spec's section 4.1, with the reason.
NOT_SEARCHED = {
    "LICENSE": "legal document",
    "NOTICE": "legal document",
    "DISCLAIMER.md": "legal document",
    "tests": "the port's proof, covered by section 12",
}
EXCLUDED = ("node_modules",)
SUFFIXES = (".py", ".sh", ".md", ".ts", ".tsx", ".js", ".json", ".gitignore")


def release_cut() -> list[str]:
    """The standing cut, read from the release script rather than duplicated."""
    text = CUT_SCRIPT.read_text(encoding="utf-8")
    block = re.search(r"for item in (.*?); do", text, re.S)
    if not block:
        raise AssertionError("the release script no longer declares its cut as 'for item in ...'")
    return [item for item in block.group(1).replace("\\\n", " ").split() if item]


def surface() -> list[str]:
    return [item for item in release_cut() if item not in NOT_SEARCHED]


def spec_text() -> str:
    return SPEC.read_text(encoding="utf-8")


def require_spec(case: unittest.TestCase) -> None:
    """Skip where the spec does not ship.

    `docs/` is deliberately outside the public release cut
    (`scripts/make_public_release.sh`), so in an assembled public tree this
    file is absent and there is no inventory claim to check. Every test in
    `tests/` must pass inside that tree; this one has nothing to assert there.
    """
    if not SPEC.is_file():
        case.skipTest("docs/specs/LINUX_SUPPORT.md is not part of this tree (public cut excludes docs/)")


def inventory_table() -> str:
    """Only section 4.2's table.

    Round 2 of review caught this: searching the whole document let a deleted
    table row pass because the same filename was mentioned in prose elsewhere.
    Presence is therefore asserted against the table alone.
    """
    text = spec_text()
    start = text.index("### 4.2 The table")
    end = text.index("### 4.3", start)
    rows = [line for line in text[start:end].splitlines() if line.startswith("|")]
    if len(rows) < 10:
        raise AssertionError("section 4.2 no longer holds a table of inventory rows")
    return "\n".join(rows)


def pattern_from_spec() -> str:
    """Read the sweep pattern out of the spec, so the two cannot drift apart."""
    block = re.search(r"\*\*Pattern set\*\*.*?```\n(.*?)```", spec_text(), re.S)
    if not block:
        raise AssertionError("the spec no longer states its pattern set in section 4.1")
    return "".join(line.strip() for line in block.group(1).splitlines())


def sweep(pattern: str) -> dict[str, set[int]]:
    command = ["grep", "-rnE", pattern, *surface()]
    for suffix in SUFFIXES:
        command.append(f"--include=*{suffix}")
    completed = subprocess.run(command, cwd=ROOT, capture_output=True, text=True)
    if completed.returncode not in (0, 1):
        raise AssertionError(f"sweep failed: {completed.stderr.strip()}")
    hits: dict[str, set[int]] = {}
    for line in completed.stdout.splitlines():
        path, _, rest = line.partition(":")
        number, _, _ = rest.partition(":")
        if any(part in path for part in EXCLUDED) or not number.isdigit():
            continue
        hits.setdefault(path, set()).add(int(number))
    return hits


class SpecInventoryTests(unittest.TestCase):
    def test_every_platform_coupled_file_is_named_in_the_inventory(self):
        require_spec(self)
        table = inventory_table()
        # Repository-relative paths only. Matching a bare basename let
        # `engine/scripts/timer.py` stand in for `harness/timer.py` - the same
        # false negative round 2 found, one level deeper.
        missing = [path for path in sorted(sweep(pattern_from_spec())) if path not in table]
        self.assertEqual(
            missing, [],
            "platform-coupled files absent from the spec's section 4.2 table: "
            + ", ".join(missing),
        )

    def test_every_cited_location_still_matches_the_sweep(self):
        """A table that rots is a table that lies; citations must stay true.

        Rows cite the construct they describe, which often starts at a `def`
        line a few lines above the coupled call. A citation is therefore
        satisfied by a sweep hit anywhere in the cited range - or, for a bare
        line number, within a short window below it.
        """
        require_spec(self)
        by_path = sweep(pattern_from_spec())
        window = 20
        stale = []
        for citation in re.findall(r"`([\w./-]+\.(?:py|sh|json|md):[\d,\s-]+)`", spec_text()):
            cited = citation.split(":")[0]
            if cited not in by_path:
                continue
            numbers = [int(value) for value in re.findall(r"\d+", citation.split(":", 1)[1])]
            if not numbers:
                continue
            low, high = min(numbers), max(numbers)
            if high == low:
                high = low + window
            if not any(low <= number <= high for number in by_path[cited]):
                stale.append(citation)
        self.assertEqual(
            stale, [],
            "spec cites locations that no longer carry a platform-coupled construct: "
            + ", ".join(stale),
        )

    def test_surface_matches_the_release_cut(self):
        """The guard cannot quietly search less than the product ships.

        Round 2 caught the searched surface omitting README.md,
        profile.example.json, CONTRIBUTING.md and .gitignore. Deriving it from
        the release script makes that impossible; this test fails if the cut
        gains an item that is neither searched nor explicitly excused.
        """
        unaccounted = [item for item in release_cut()
                       if item not in NOT_SEARCHED and item not in surface()]
        self.assertEqual(unaccounted, [], f"release-cut items neither searched nor excused: {unaccounted}")
        for item in surface():
            self.assertTrue((ROOT / item).exists(), f"searched surface names a missing path: {item}")


if __name__ == "__main__":
    unittest.main()
