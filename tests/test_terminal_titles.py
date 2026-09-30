# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
import unittest
from harness.terminal_titles import TitlePrefix


class TitlePrefixTests(unittest.TestCase):
    def test_titles_survive_every_read_boundary_and_keep_all_other_bytes(self):
        for kind, role in (("codex_delivery", b"Developer"), ("claude_reviewer", b"Reviewer"), ("claude_cto", b"CTO")):
            for code in (b"0", b"1", b"2"):
                for end in (b"\x07", b"\x1b\\"):
                    header = b"\x1b]" + code + b";"
                    original = b"\x1b[31mhello\x1b[0m" + header + "working…".encode() + end + header + end + b"\x1b]8;;https://example.com\x07link\x1b]8;;\x07"
                    expected = original.replace(header, header + role + b" | ")
                    for split in range(len(original) + 1):
                        with self.subTest(kind=kind, code=code, split=split, end=end):
                            titles = TitlePrefix(kind + "-0123456789")
                            self.assertEqual(titles.feed(original[:split]) + titles.feed(original[split:]), expected)
                    titles = TitlePrefix(kind + "-0123456789")
                    self.assertEqual(b"".join(titles.feed(bytes([byte])) for byte in original), expected)

    def test_unknown_session_and_non_title_controls_pass_through_immediately(self):
        original = b"\x1b]0;keep\x07\x1b[31mtext\x1b[0m"
        self.assertEqual(TitlePrefix("other").feed(original), original)
        titles = TitlePrefix("codex_delivery-1")
        for data in (b"text", b"\x1b", b"]", b"8;;https://example.com\x07", b"\x1b]10;?\x07", b"\x1b[6n", b"\x1b]", b"2"):
            self.assertEqual(titles.feed(data), data)
        self.assertEqual(titles.feed(b";"), b";Developer | ")
