# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""A Windows program prints in the console's code page (the degree sign is the single byte 0xF8). The harness must record that, not crash."""
import os
import sys
import tempfile
import unittest
from pathlib import Path

from harness import certified_execution, child_process
from tests import environment_support

PRINT_F8 = "import sys; sys.stdout.buffer.write(b'Austin, TX: 24 \\xf8C\\n'); sys.stdout.flush(); sys.stderr.buffer.write(b'warn \\xf8\\n'); sys.exit({code})"


class WindowsOutputDecodingTests(unittest.TestCase):
    def setUp(self):
        environment_support.require_process_table()
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def observed(self, code: str):
        command = f"{sys.executable} -c \"{code}\""
        return certified_execution._run_observed(command, self.root, dict(os.environ), 60)

    def test_certified_command_printing_byte_f8_is_recorded_not_crashed(self):
        exit_code, output, _ = self.observed(PRINT_F8.format(code=0))
        self.assertEqual(exit_code, 0)
        self.assertIn("Austin, TX: 24 �C", output)
        self.assertIn("warn �", output)

    def test_replacing_bytes_never_turns_a_failure_into_a_pass(self):
        exit_code, output, _ = self.observed(PRINT_F8.format(code=3))
        self.assertEqual(exit_code, 3)

    def test_valid_utf8_is_recorded_exactly_as_before(self):
        _, output, _ = self.observed("print('24 \\u00b0C \\u2713')")
        self.assertIn("24 °C ✓", output)

    def test_bounded_command_printing_byte_f8_is_read(self):
        code, output, timed_out = child_process.run_bounded(
            [sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'24 \\xf8C\\n')"],
            cwd=self.root, env=dict(os.environ), timeout=30)
        self.assertEqual((code, timed_out), (0, False))
        self.assertIn("24 �C", output)


if __name__ == "__main__":
    unittest.main()
