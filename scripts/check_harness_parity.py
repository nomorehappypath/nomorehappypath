#!/usr/bin/env python3
# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Fail when shared Dev behavior has no audited harness_next counterpart."""
from __future__ import annotations

import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from harness.parity_audit import audit_roots, production_files, test_methods


def main() -> int:
    """Run the harness parity audit from command-line arguments --dev-root and --next-root.

    Takes no parameters (reads sys.argv via argparse). Compares the Dev tree against the
    harness_next tree, printing PASSED or FAILED with the problems found.

    Return: 0 when the audit passes (no problems), 1 when it fails.
    """
    parser = argparse.ArgumentParser()
    parser.add_argument("--dev-root", type=Path, required=True)
    parser.add_argument("--next-root", type=Path, required=True)
    args = parser.parse_args()
    problems = audit_roots(args.dev_root, args.next_root)
    if problems:
        print("Harness parity audit: FAILED")
        for problem in problems:
            print(f"- {problem}")
        return 1
    print(
        "Harness parity audit: PASSED "
        f"({len(production_files(args.dev_root))} Dev production files; "
        f"{len(test_methods(args.dev_root))} Dev regression tests covered)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
