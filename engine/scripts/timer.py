#!/usr/bin/env python3
# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
"""OS-timer generator — run `scheduler tick` durably as a local background service.

Renders an OS scheduler unit (macOS launchd plist, or a cron line) that runs the scheduler tick
on an interval, and prints the install command. It does NOT auto-install: loading a system timer
is a deliberate, side-effecting step the user runs (the harness only generates the unit). This
keeps the durable, local scheduler (D11/D13) explicit and safe. Stdlib only.

  bash timer.sh gen --kind launchd --interval 120 [--out ~/Library/LaunchAgents/<label>.plist]
  bash timer.sh gen --kind cron --interval 120
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

LABEL = "com.dev_harness.scheduler"


def _scheduler_sh() -> str:
    return str(Path(__file__).resolve().parent / "scheduler.sh")


def _repo_root() -> str:
    here = Path.cwd().resolve()
    for c in [here, *here.parents]:
        if (c / ".agents").is_dir():
            return str(c)
    return str(here)


def render_launchd(interval: int, repo_root: str, label: str = LABEL) -> str:
    sh = _scheduler_sh()
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{label}</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>{sh}</string>
    <string>tick</string>
  </array>
  <key>WorkingDirectory</key><string>{repo_root}</string>
  <key>StartInterval</key><integer>{interval}</integer>
  <key>RunAtLoad</key><true/>
  <key>StandardOutPath</key><string>{repo_root}/.agents/dispatches/logs/scheduler.out.log</string>
  <key>StandardErrorPath</key><string>{repo_root}/.agents/dispatches/logs/scheduler.err.log</string>
</dict>
</plist>
"""


def render_cron(interval: int, repo_root: str) -> str:
    sh = _scheduler_sh()
    return (
        f"# dev_harness scheduler — runs `scheduler tick` every minute "
        f"(cron's finest granularity; requested interval {interval}s)\n"
        f"* * * * * cd {repo_root} && /bin/bash {sh} tick "
        f">> {repo_root}/.agents/dispatches/logs/scheduler.cron.log 2>&1\n"
    )


def _cmd_gen(a) -> int:
    root = a.repo or _repo_root()
    if a.kind == "launchd":
        content = render_launchd(a.interval, root)
        install = (f"cp <the-file> ~/Library/LaunchAgents/{LABEL}.plist && "
                   f"launchctl load ~/Library/LaunchAgents/{LABEL}.plist")
    else:
        content = render_cron(a.interval, root)
        install = "append the line to your crontab:  crontab -e"
    if a.out:
        Path(a.out).expanduser().write_text(content, encoding="utf-8")
        print(f"wrote {a.out}")
    else:
        print(content)
    # Installing is a deliberate manual step — we only generate the unit, never load it.
    print(f"# To install (deliberate manual step): {install}", file=sys.stderr)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="OS-timer generator for the scheduler tick")
    sub = p.add_subparsers(dest="command", required=True)
    g = sub.add_parser("gen", help="Render an OS-timer unit (launchd plist or cron line).")
    g.add_argument("--kind", choices=["launchd", "cron"], default="launchd")
    g.add_argument("--interval", type=int, default=120)
    g.add_argument("--repo")
    g.add_argument("--out")
    g.set_defaults(func=_cmd_gen)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
