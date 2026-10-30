# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""R-3 must not refuse the text a file-writing heredoc carries (2026-10-28: "Concurrency & Maintainability" was read as a background job).

Run:  PYTHONPATH=. python3 -m unittest tests.test_hook_rules_heredoc -v
"""
from __future__ import annotations

import unittest

from harness import hook_rules


def verdict(command):
    return hook_rules.evaluate("engineering", "Bash", {"command": command}, project_roots=["/p"])[0]


class FileHeredocTests(unittest.TestCase):
    def test_an_ampersand_in_a_report_written_with_cat_is_allowed(self):
        self.assertEqual(verdict("mkdir -p /tmp/x\ncat > /tmp/x/f.md << 'EOF'\n# Backend - Concurrency & Maintainability Audit\nQ&A and a & b\nEOF"), "")

    def test_the_word_nohup_in_a_written_report_is_allowed(self):
        self.assertEqual(verdict("cat > f.md <<EOF\nthe worker is started with nohup in the deploy notes\nEOF"), "")

    def test_tee_heredoc_is_also_data(self):
        self.assertEqual(verdict("tee out.md <<'EOT'\nA & B\nEOT"), "")

    def test_a_real_background_job_is_still_refused(self):
        self.assertEqual(verdict("sleep 100 &"), "R-3")
        self.assertEqual(verdict("nohup python3 serve.py"), "R-3")

    def test_a_background_job_after_the_heredoc_is_still_refused(self):
        self.assertEqual(verdict("cat > f.md << 'EOF'\nhello\nEOF\nsleep 100 &"), "R-3")

    def test_a_heredoc_fed_to_a_shell_or_interpreter_is_still_checked(self):
        self.assertEqual(verdict("bash << 'EOF'\nsleep 100 &\nEOF"), "R-3")
        self.assertEqual(verdict("python3 - << 'EOF'\nprint(1)\nEOF\nsleep 5 &"), "R-3")

    def test_a_heredoc_piped_or_chained_into_something_is_still_checked(self):
        self.assertEqual(verdict("cat << 'EOF' | bash\nsleep 100 &\nEOF"), "R-3")
        self.assertEqual(verdict("cat <<EOF | sh\nnohup x\nEOF"), "R-3")
        self.assertEqual(verdict("cat << 'EOF' > f.sh ; bash f.sh &\nhello\nEOF"), "R-3")
        self.assertEqual(verdict("x=1 && cat <<EOF | bash\nsleep 5 &\nEOF"), "R-3")

    def test_redirects_to_a_file_are_still_set_aside(self):
        self.assertEqual(verdict("cat <<'EOF' >> notes.md\nA & B\nEOF"), "")
        self.assertEqual(verdict("cat > out.md <<EOF\nA & B\nEOF"), "")

    def test_an_unquoted_heredoc_that_still_runs_a_substitution_is_checked(self):
        self.assertEqual(verdict("cat > f.md <<EOF\nnote $(sleep 100 &)\nEOF"), "R-3")
        self.assertEqual(verdict("cat > f.md <<EOF\nnote `sleep 100 &`\nEOF"), "R-3")
        self.assertEqual(verdict("tee f.md <<\"EOF\"\nA & B\nEOF"), "")           # quoted delimiter: no expansion, plain data
        self.assertEqual(verdict("cat > f.md <<EOF\nA & B and 5 dollars\nEOF"), "")   # unquoted but nothing to expand

    def test_a_quoted_heredoc_with_substitution_text_is_data(self):
        self.assertEqual(verdict("cat > f.md <<'EOF'\nuse $(sleep 100 &) in the docs\nEOF"), "")

    def test_whole_tree_staging_is_still_refused(self):
        self.assertEqual(verdict("git add -A"), "R-2")

    def test_credential_paths_are_still_refused_even_inside_a_heredoc(self):
        self.assertEqual(verdict("cat > f.sh << 'EOF'\ncat ~/.claude/.credentials.json\nEOF"), "R-4")


if __name__ == "__main__":
    unittest.main()
