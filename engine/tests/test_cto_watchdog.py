# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))
import cto_watchdog as watchdog  # noqa: E402


def record(contract: str) -> str:
    return f"""# Agent Task Record
Task ID: `TASK-1`
Status: `ACCEPTANCE_READY`
{contract}
## 5. Acceptance criteria
GIVEN a user WHEN they use the harness THEN the release is gated.
## 6. Environment classification
| Local dev | YES | tested |
## Evidence package
Changed files: x
Acceptance criteria results: pass
Command/test output: pass
## 14.1 Merge + SHA
Merged: abcdef1
verify-merge: VERIFIED
"""


class CompletionContractWatchdogTests(unittest.TestCase):
    def check(self, text):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "TASK-1.md"
            path.write_text(text)
            report = watchdog.RepoReport(repo_path=Path(tmp))
            watchdog.check_task_record(path, report, "review", None, ["Local dev"])
            return [finding.message for finding in report.findings if finding.level == "FAIL"]

    def test_acceptance_ready_without_contract_is_blocked(self):
        failures = self.check(record(""))
        self.assertTrue(any("Completion Contract" in failure for failure in failures))

    def test_acceptance_ready_requires_complete_contract_and_no_remaining_work(self):
        incomplete = """## 0. Completion Contract
User objective: ship
| Required deliverable | Acceptance proof |
| x | command |
Exclusions: none
Current contract status: `PARTIAL`
Remaining work:
- x
"""
        failures = self.check(record(incomplete))
        self.assertTrue(any("not complete" in failure for failure in failures))
        complete = incomplete.replace("`PARTIAL`", "`COMPLETE`").replace("- x", "- none")
        failures = self.check(record(complete))
        self.assertFalse(any("Completion Contract" in failure for failure in failures))


if __name__ == "__main__":
    unittest.main()
