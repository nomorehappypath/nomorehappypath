# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
import json
import os
import tempfile
import unittest
from pathlib import Path

from harness import board, certified_execution, execution_identity
from tests import environment_support


class CertifiedProcessObservationTests(unittest.TestCase):
    def setUp(self):
        # This suite's whole subject is proving which processes an execution
        # owned, read from the OS process table. Where the environment forbids
        # that, the assertions cannot run and saying so is honest.
        environment_support.require_process_table()
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.candidate = execution_identity.candidate_evidence_identity(
            "c" * 40, "t" * 40, "contract", {"ledger": "l" * 64},
        )

    def execute(self, command: str, retry_reason: str = ""):
        return certified_execution.run(
            self.root, self.root, command, candidate=self.candidate,
            environment_sha256="e" * 64, environment=os.environ,
            lockfile_digests={}, role="reviewer", gate="final",
            retry_reason=retry_reason,
        )

    def test_success_records_content_addressed_process_audit(self):
        result = self.execute("python3 -c \"print('Ran 1 test in 0.001s'); print('OK')\"")
        manifest = result["measurement"]["process_audit"]
        path = Path(manifest["path"])
        self.assertTrue(path.is_file())
        audit = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(audit["problems"], [])
        records = [json.loads(line) for line in (board.board_dir(self.root) / execution_identity.STORE_NAME).read_text().splitlines()]
        self.assertEqual(records[-1]["metadata"]["process_audit"]["sha256"], manifest["sha256"])

    def test_nested_owner_browser_launch_is_observed_and_certified_as_failure(self):
        browser = self.root / "OwnerChrome.app" / "Contents" / "MacOS" / "Chrome"
        browser.parent.mkdir(parents=True)
        browser.write_text("#!/bin/sh\nsleep .3\n", encoding="utf-8")
        browser.chmod(0o755)
        command = (
            "python3 -c \"import subprocess; "
            f"subprocess.run([r'{browser}']); "
            "print('Ran 1 test in 0.001s'); print('OK')\""
        )
        with self.assertRaisesRegex(ValueError, "owner-browser application bundle"):
            self.execute(command)
        records = [json.loads(line) for line in (board.board_dir(self.root) / execution_identity.STORE_NAME).read_text().splitlines()]
        self.assertNotEqual(records[-1]["exit_code"], 0)
        audit_path = Path(records[-1]["metadata"]["process_audit"]["path"])
        audit = json.loads(audit_path.read_text(encoding="utf-8"))
        self.assertTrue(audit["forbidden_owner_browser_descendants"])


if __name__ == "__main__":
    unittest.main()
