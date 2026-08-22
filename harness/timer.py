#!/usr/bin/env python3
# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
"""Render durable scheduler timers; installation is always an explicit step."""
from __future__ import annotations

import argparse
import html
import shlex
import shutil
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from harness.project_context import ProjectRoot, add_context_arguments, context_cli_arguments, context_from_args, project_context


SCHEDULER_SCRIPT = Path(__file__).with_name("scheduler.py").resolve()


def _python_executable(value: str | None) -> str:
    candidate = value or sys.executable
    located = candidate if Path(candidate).is_absolute() else shutil.which(candidate)
    if not located:
        raise ValueError(f"Python interpreter is not executable: {candidate}")
    path = Path(located).resolve()
    if not path.is_file():
        raise ValueError(f"Python interpreter is not executable: {candidate}")
    return str(path)


def command(root: ProjectRoot, profile: Path, python: str | None = None) -> str:
    context = project_context(root)
    root_arg = shlex.quote(str(context.code_root))
    profile_arg = shlex.quote(str(Path(profile).resolve()))
    context_args = " ".join(shlex.quote(value) for value in context_cli_arguments(context))
    scheduler_arg = shlex.quote(str(SCHEDULER_SCRIPT))
    return f"cd {root_arg} && {shlex.quote(_python_executable(python))} -E {scheduler_arg} {context_args} --profile {profile_arg} --execute"


def cron(root: ProjectRoot, profile: Path, interval_minutes: int, python: str | None = None) -> str:
    if interval_minutes <= 0 or 60 % interval_minutes:
        raise ValueError("interval_minutes must be a positive divisor of 60")
    log = shlex.quote(str(project_context(root).storage_path("board", "watch.log")))
    return f"*/{interval_minutes} * * * * {command(root, profile, python)} >> {log} 2>&1"


def launchd(root: ProjectRoot, profile: Path, interval_seconds: int, label: str, python: str | None = None) -> str:
    if interval_seconds <= 0:
        raise ValueError("interval_seconds must be positive")
    context = project_context(root)
    args = [_python_executable(python), "-E", str(SCHEDULER_SCRIPT), *context_cli_arguments(context), "--profile", str(Path(profile).resolve()), "--execute"]
    items = "".join(f"\n      <string>{html.escape(arg)}</string>" for arg in args)
    return f'''<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>{html.escape(label)}</string>
  <key>WorkingDirectory</key><string>{html.escape(str(context.code_root))}</string>
  <key>ProgramArguments</key><array>{items}
  </array>
  <key>StartInterval</key><integer>{interval_seconds}</integer>
  <key>StandardOutPath</key><string>{html.escape(str(context.storage_path('board', 'watch.log')))}</string>
  <key>StandardErrorPath</key><string>{html.escape(str(context.storage_path('board', 'watch.log')))}</string>
</dict></plist>
'''


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Render Dev Harness board-watch timers")
    add_context_arguments(parser)
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--profile", required=True, help="validated project profile that authorizes agent launch commands")
    sub = parser.add_subparsers(dest="kind", required=True)
    c = sub.add_parser("cron"); c.add_argument("--interval-minutes", type=int, default=5)
    l = sub.add_parser("launchd"); l.add_argument("--interval-seconds", type=int, default=300); l.add_argument("--label", default="com.dev-harness.board-watch")
    args = parser.parse_args(argv)
    root = context_from_args(args)
    profile = Path(args.profile).resolve()
    try:
        out = cron(root, profile, args.interval_minutes, args.python) if args.kind == "cron" else launchd(root, profile, args.interval_seconds, args.label, args.python)
    except ValueError as error:
        parser.error(str(error))
    print(out, end="" if out.endswith("\n") else "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
