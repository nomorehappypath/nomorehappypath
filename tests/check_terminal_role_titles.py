# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Native macOS proof: opens only disposable test windows, then closes them.

Run from the repository root with PYTHONPATH=. python3 tests/check_terminal_role_titles.py.
No agent, project, login, or Terminal preference is changed.
"""
from pathlib import Path
import subprocess
import sys
import tempfile
import time

from harness.platform_support.defaults import TERMINAL_HOST
from harness import board, control

CHILD = r'''
import os, pathlib, sys, time
root = pathlib.Path(sys.argv[1])

for code in ("0", "1", "2"):
    for piece in ("\033", "]", code, ";", "CLI dynamic title", "\007"):
        print(piece, end="", flush=True)
        time.sleep(.03)
(root / "ready").touch()
print("Disposable role-title check; no agent is running.", flush=True)
for _ in range(600):
    if (root / "stop").exists():
        break
    time.sleep(.1)
'''
INSPECT = '''on run argv
 tell application "Terminal"
  repeat with w in windows
   repeat with t in tabs of w
    if tty of t is item 1 of argv then
     return (custom title of t) & linefeed & (name of w) & linefeed & (id of w)
    end if
   end repeat
  end repeat
 end tell
 error "Test terminal not found"
end run'''
CLOSE = '''on run argv
 tell application "Terminal"
  repeat with w in windows
   repeat with t in tabs of w
    if tty of t is item 1 of argv then
     if (count of tabs of w) is not 1 then error "Refusing to close a window with other tabs"
     if busy of t then error "Test child has not exited"
     close w
     return
    end if
   end repeat
  end repeat
 end tell
end run'''


def apple(script, tty):
    return subprocess.run(["/usr/bin/osascript", "-e", script, tty],
                          check=True, capture_output=True, text=True).stdout.strip()


def main():
    if sys.platform != "darwin":
        raise SystemExit("Native title check requires macOS Terminal")
    for kind, expected in (("codex_delivery", "Developer"),
                           ("claude_reviewer", "Reviewer"), ("claude_cto", "CTO")):
        with tempfile.TemporaryDirectory(prefix="terminal-title-", dir=Path.cwd()) as temp:
            root = Path(temp) / "project"
            root.mkdir()
            tty = ""
            try:
                session = control.create(root, kind)
                role = {"codex_delivery": "engineering", "claude_reviewer": "qa", "claude_cto": "cto"}[kind]
                task = {"engineering": board.AWAITING_OWNER_DIRECTION, "qa": "REVIEW_QUEUE", "cto": "GLOBAL_MONITOR"}[role]
                agent = board.register(root, role, task, session_id=session["id"])
                # Capture the OUTER tty, then execute the real supervisor, whose
                # nested PTY is deliberately a different device.
                wrapper = "import os,pathlib,sys; pathlib.Path(sys.argv[1]).write_text(os.ttyname(0)); os.execv(sys.argv[2],sys.argv[2:])"
                command = [sys.executable, "-c", wrapper, str(root / "tty"), sys.executable,
                           str(Path(__file__).resolve().parents[1] / "harness/interactive_supervisor.py"),
                           "--root", str(root), "--session-id", session["id"], "--agent-id", agent["id"],
                           "--", sys.executable, "-c", CHILD, str(root)]
                TERMINAL_HOST.open_session(session["id"], command, color_rgb=(0, 0, 0))
                for _ in range(100):
                    if (root / "tty").exists():
                        tty = (root / "tty").read_text()
                        break
                    time.sleep(.1)
                assert tty, "Test child did not start"
                for _ in range(100):
                    if (root / "ready").exists():
                        break
                    time.sleep(.1)
                assert (root / "ready").exists(), "CLI title updates did not finish"
                observed = apple(INSPECT, tty)
                custom, name, window_id = observed.split("\n", 2)
                assert custom == expected + " | CLI dynamic title" and expected in name, (expected, observed)
                print(f"PASS {expected}: custom title={custom!r}; native frame contains role", flush=True)
            finally:
                (root / "stop").touch()
                if tty:
                    for _ in range(30):
                        try:
                            apple(CLOSE, tty)
                            break
                        except subprocess.CalledProcessError:
                            time.sleep(.1)
                    else:
                        raise RuntimeError("Could not close disposable test window " + tty)
    print("PASS: all three native frame titles survive CLI title output; test windows closed.")


if __name__ == "__main__":
    main()
