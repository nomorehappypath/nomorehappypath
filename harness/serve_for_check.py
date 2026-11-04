# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Run the app in the foreground for exactly one check, then stop it.

Usage: python3 serve_for_check.py --start "<command that starts the app>" --port <port> -- <check command...>

Why it exists (2026-11-03 review R1-1): a Claude Delivery agent may not leave a background process behind (hook rule R-3),
so it had no allowed way to keep its web app listening while `board.py ... screen-check` captured it, and the task stalled.
This helper is one foreground command: it starts the app as its own child, waits until the port answers, runs the check
command, and always stops the app (its whole process group) before it returns, so nothing outlives the command.
It runs inside the agent's own sandbox with the agent's own rights; it grants nothing new.
"""
from __future__ import annotations

import argparse
import os
import shlex
import signal
import socket
import subprocess
import sys
import time

READY_TIMEOUT_SECONDS = 60.0
STOP_GRACE_SECONDS = 5.0


def port_answers(port: int) -> bool:
    """Take a loopback port; return True when something accepts a connection on it."""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


def stop(app: subprocess.Popen) -> None:
    """Take the started app; end its whole process group (a server's workers included), forcefully after a short grace."""
    if app.poll() is not None:
        return
    try:
        os.killpg(app.pid, signal.SIGTERM)
        app.wait(timeout=STOP_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        os.killpg(app.pid, signal.SIGKILL)
        app.wait()
    except ProcessLookupError:
        pass


def run(start: str, port: int, check: list[str], ready_timeout: float = READY_TIMEOUT_SECONDS) -> int:
    """Take the start command, the app's port and the check command; return the check's exit code (2 if the app never answered).

    Refuses (exit 2) when the port is already taken, because the check would then capture some other server.
    """
    if port_answers(port):
        print(f"serve_for_check: port {port} is already in use; stop that server or pick a free port", file=sys.stderr)
        return 2
    # Its own session, so stop() can end the server and any workers it spawns in one signal.
    app = subprocess.Popen(shlex.split(start), start_new_session=True)
    try:
        deadline = time.monotonic() + ready_timeout
        while not port_answers(port):
            if app.poll() is not None:
                print(f"serve_for_check: the app exited with code {app.returncode} before port {port} answered", file=sys.stderr)
                return 2
            if time.monotonic() > deadline:
                print(f"serve_for_check: port {port} did not answer within {int(ready_timeout)} seconds", file=sys.stderr)
                return 2
            time.sleep(0.2)
        return subprocess.run(check).returncode
    finally:
        stop(app)


def main(argv: list[str] | None = None) -> int:
    """Parse the command line (see the module note) and run it; return the exit code."""
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--" not in argv:
        print("serve_for_check: put the check command after --", file=sys.stderr)
        return 2
    split = argv.index("--")
    parser = argparse.ArgumentParser(prog="serve_for_check.py")
    parser.add_argument("--start", required=True)
    parser.add_argument("--port", required=True, type=int)
    args = parser.parse_args(argv[:split])
    check = argv[split + 1:]
    if not check:
        print("serve_for_check: the check command after -- is empty", file=sys.stderr)
        return 2
    return run(args.start, args.port, check)


if __name__ == "__main__":
    sys.exit(main())
