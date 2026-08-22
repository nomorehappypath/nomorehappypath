#!/usr/bin/env python3
# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
"""Independent board-heartbeat watchdog for the local Harness viewer."""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from harness import board
from harness.project_context import add_context_arguments, context_from_args


def tick(root: Path, stale_after: int) -> list[dict]:
    return board.mark_stalled(root, stale_after)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Mark agents stale when their board heartbeat stops")
    add_context_arguments(parser)
    parser.add_argument("--interval", type=int, default=board.WATCHDOG_INTERVAL_SECONDS)
    parser.add_argument("--stale-after", type=int, default=board.AGENT_STALE_SECONDS)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    if args.interval < 1 or args.stale_after < 1:
        parser.error("--interval and --stale-after must be positive")
    root = context_from_args(args)
    while True:
        for event in tick(root, args.stale_after):
            print(f"WATCHDOG | agent={event['agent_id']} | status=STALLED | next=agent must resume and process board work", flush=True)
        if args.once:
            return 0
        time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
