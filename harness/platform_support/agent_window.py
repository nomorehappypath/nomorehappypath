# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Closes the macOS Terminal window an agent's view ran in, once the view has ended.

The agent runs in a detached `screen` session; its Terminal window only runs
`screen -r` to show it. When that returns (the agent ended, was stopped, the
project was paused or closed, or the owner detached) nothing runs in the window
any more, and the window used to stay open on "[Process completed]". This is the
last thing the window runs, so it is the one place that knows which window it is.

Safety, in order of importance:
* only the window this process itself sits in: its tab is found while still alive
  (same tty, busy, exact title; a finished window with a recycled tty is not busy),
  never by position or by a name pattern over other windows;
* just before closing, the window is checked again: one tab, the recorded title
  and tty, nothing running. Anything else, including any error or timeout, leaves
  the window open and writes one line saying why;
* never forced, never `saving no`, so Terminal never raises a dialog nobody answers.
"""
from __future__ import annotations

import argparse
import datetime
import os
import subprocess
import sys

LOOKUP_SECONDS = 10
CLOSER_SECONDS = 30
CLOSING_SUFFIX = " (closing)"

# While still alive: find this tab (tty + busy + exact title), rename it so View agent
# never mistakes a departing window for a live one, and answer its window id.
LOOKUP_SCRIPT = r'''on run argv
 tell application "Terminal"
  repeat with terminalWindow in windows
   try
    repeat with terminalTab in tabs of terminalWindow
     try
      if (tty of terminalTab is item 1 of argv) and (custom title of terminalTab is item 2 of argv) and (busy of terminalTab) then
       set custom title of terminalTab to item 3 of argv
       return (id of terminalWindow) as string
      end if
     end try
    end repeat
   end try
  end repeat
 end tell
 return "not found"
end run'''

# Detached, after this process has exited. Checked again right before closing; any
# doubt leaves the window open. Windows are addressed by id only.
CLOSE_SCRIPT = r'''on run argv
 set targetId to (item 1 of argv) as integer
 tell application "Terminal"
  repeat 40 times
   try
    if not (exists window id targetId) then return "already closed"
    if not (visible of window id targetId) then return "already closed"
    if (count of tabs of window id targetId) is not 1 then return "left open: the window holds more than the agent's own tab"
    set agentTab to tab 1 of window id targetId
    if (custom title of agentTab) is not (item 3 of argv) then return "left open: the tab is no longer the agent's"
    if (tty of agentTab) is not (item 2 of argv) then return "left open: the tab is no longer the agent's"
    if not (busy of agentTab) then
     close window id targetId
     delay 0.5
     if (exists window id targetId) and (visible of window id targetId) then return "left open: Terminal asked for confirmation"
     return "closed"
    end if
   on error problem
    return "left open: " & problem
   end try
   delay 0.25
  end repeat
 end tell
 return "left open: something is still running in it"
end run'''


def _log(path: str, title: str, window: str, reason: str) -> None:
    if not path:
        return
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(f"{stamp} | {title} | window {window or 'unknown'} | {reason} | close this window by hand\n")
    except OSError:
        pass


def _osascript(script: str, arguments: list[str], timeout: float) -> str:
    result = subprocess.run(
        ["/usr/bin/osascript", "-e", script, *arguments],
        capture_output=True, text=True, check=False, timeout=timeout, stdin=subprocess.DEVNULL,
    )
    if result.returncode != 0:
        raise OSError((result.stderr or "osascript failed").strip().splitlines()[-1][:200])
    return result.stdout.strip()


def close_after_exit(log: str, title: str, exit_code: int, stdin_fd: int = 0) -> None:
    """Called in the window once `screen -r` has returned."""
    if sys.platform != "darwin":
        return
    if exit_code:
        _log(log, title, "", f"screen -r ended with code {exit_code}; the window is closed anyway but the session may not have been shown")
        # The window is still closed: it runs nothing.
    try:
        tty = os.ttyname(stdin_fd)
    except OSError:
        _log(log, title, "", "this window has no terminal to identify it by")
        return
    closing = title + CLOSING_SUFFIX
    try:
        found = _osascript(LOOKUP_SCRIPT, [tty, title, closing], LOOKUP_SECONDS)
    except (OSError, subprocess.TimeoutExpired) as error:
        _log(log, title, "", f"could not look the window up: {error}")
        return
    if not found.isdigit():
        _log(log, title, "", "the window was not found while it was still running")
        return
    try:
        subprocess.Popen(
            [sys.executable, os.path.abspath(__file__), "--close", found, tty, closing, "--log", log, "--title", title],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
        )
    except OSError as error:
        _log(log, title, found, f"could not start the closer: {error}")


def _closer(window: str, tty: str, closing: str, log: str, title: str) -> None:
    try:
        answer = _osascript(CLOSE_SCRIPT, [window, tty, closing], CLOSER_SECONDS)
    except (OSError, subprocess.TimeoutExpired) as error:
        _log(log, title, window, f"the close request failed: {error}")
        return
    if not answer.startswith(("closed", "already closed")):
        _log(log, title, window, answer)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Close this agent view's Terminal window after the view ended")
    parser.add_argument("--log", default="")
    parser.add_argument("--title", default="")
    parser.add_argument("--exit-code", type=int, default=0)
    parser.add_argument("--close", nargs=3, metavar=("WINDOW", "TTY", "CLOSING_TITLE"))
    args = parser.parse_args(argv)
    if args.close:
        _closer(args.close[0], args.close[1], args.close[2], args.log, args.title)
    else:
        close_after_exit(args.log, args.title, args.exit_code)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
