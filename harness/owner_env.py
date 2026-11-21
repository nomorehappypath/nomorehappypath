# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""The owner's own environment: what a program gets when he starts it from his Terminal.

Owner 2026-11-21 (Lease): his Claude sign-in lives in his ~/.zshrc. NMHP started his app with PATH only, so the app's `claude`
fell back to an expired saved login and the owner's test failed while `claude` worked in his Terminal. The environment checks,
the app's real run and the release preview all use the environment read here, so what was checked is what the owner gets.

Read by running the account's own login shell (from the password database, never the manager's $SHELL or HOME) as an
interactive login shell with nothing of NMHP's own environment, then `env -0` after a marker.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
import hashlib
import os
from pathlib import Path
import pwd
import re
import signal
import subprocess
import sys
import time

READ_SECONDS = 20
OUTPUT_CAP = 64 * 1024
MARKER = "\0NMHP-OWNER-ENV\0"
DROP_PREFIXES = ("HARNESS_", "NMHP_")
DROP_NAMES = frozenset({"SHLVL", "PWD", "OLDPWD", "_", "ZDOTDIR", "BASH_ENV", "ENV", "PS1", "PS2", "TERM_SESSION_ID"})
LIVE_TEST_ENV = "NMHP_LIVE_OWNER_ENV"
_CACHE_SECONDS = 60.0
_cache: dict[tuple[str, str], tuple[float, dict[str, str]]] = {}


class OwnerEnvError(ValueError):
    """The owner's environment could not be read; the message is plain words for the card."""


class OwnerEnvNotInTests(OwnerEnvError):
    """The test suite never reads the real account (the gates then use a fixed identity, see environment_check.identity)."""


def _real_account() -> tuple[str, str]:
    """(login shell, home folder) of the account NMHP runs as, from the password database."""
    if ("unittest" in sys.modules or os.environ.get("HARNESS_TESTS_RUN_ROOT")) and not os.environ.get(LIVE_TEST_ENV):
        # the test suite (and every program a test starts, which inherits HARNESS_TESTS_RUN_ROOT) never starts the developer's
        # real login shell (Codex plan check R2-3); a test injects ACCOUNT
        raise OwnerEnvNotInTests("the owner's environment is not read during the test suite (set ACCOUNT in the test)")
    entry = pwd.getpwuid(os.getuid())
    return entry.pw_shell or "/bin/sh", entry.pw_dir


ACCOUNT: Callable[[], tuple[str, str]] = _real_account


def read(fresh: bool = False) -> dict[str, str]:
    """The owner's login-shell environment, without NMHP's own variables. Raises OwnerEnvError with plain words."""
    shell, home = ACCOUNT()
    key = (shell, home)
    cached = _cache.get(key)
    if cached and not fresh and time.monotonic() - cached[0] < _CACHE_SECONDS:
        return dict(cached[1])
    if not shell or not os.access(shell, os.X_OK):
        raise OwnerEnvError(f"your login shell {shell!r} could not be started")
    base = {"HOME": home, "USER": Path(home).name, "LOGNAME": Path(home).name, "SHELL": shell, "TERM": "dumb",
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"}
    script = f"printf '{MARKER.replace(chr(0), chr(92) + '0')}'; env -0"
    code, output, timed_out = run([shell, "-l", "-i", "-c", script], cwd=home, env=base, timeout=READ_SECONDS)
    if timed_out:
        raise OwnerEnvError(f"your login shell ({Path(shell).name}) did not finish starting within {READ_SECONDS} seconds")
    if MARKER not in output:
        tail = output.strip().splitlines()[-1:] or [f"it exited with code {code}"]
        raise OwnerEnvError(f"your login shell ({Path(shell).name}) did not start cleanly: {tail[0][:200]}")
    values: dict[str, str] = {}
    for item in output.split(MARKER, 1)[1].split("\0"):
        name, sep, value = item.partition("=")
        if sep and name and not name.startswith(DROP_PREFIXES) and name not in DROP_NAMES and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            values[name] = value
    if not values.get("PATH"):
        raise OwnerEnvError(f"your login shell ({Path(shell).name}) gave no PATH")
    values.setdefault("HOME", home)
    _cache[key] = (time.monotonic(), dict(values))
    return values


def fingerprint(values: Mapping[str, str]) -> str:
    """A hash of the environment (names and values), never the values themselves: any change makes a different fingerprint."""
    digest = hashlib.sha256()
    for name in sorted(values):
        digest.update(f"{name}\0{values[name]}\0".encode("utf-8", "replace"))
    return digest.hexdigest()[:16]


def _plain(value: str) -> bool:
    """A value that is only folder paths (PATH-like) or one existing path: shown as it is."""
    parts = [part for part in value.split(os.pathsep) if part]
    if parts and all(part.startswith("/") for part in parts):
        return True
    try:
        return value.startswith("/") and Path(value).exists()
    except OSError:
        return False


URL_SECRET = re.compile(r"(?P<scheme>[A-Za-z][A-Za-z0-9+.-]*://)[^/@\s:]+:[^/@\s]+@")


def redactor(values: Mapping[str, str], base: Mapping[str, str] | None = None) -> Callable[[str], str]:
    """Hide the owner environment's values (Codex plan check R2-2): by VALUE, not by name, so a DATABASE_URL or SERVICE_URL
    with a password is hidden too. Every value of 8+ characters (except folder lists and existing paths) is replaced by its
    variable's name — also when NMHP's own environment happens to hold the same value (Codex review R1-1) — and a URL's
    user:password part is always hidden. `base` is kept for callers; it no longer exempts anything."""
    hidden = sorted(
        ((name, value) for name, value in values.items() if len(value) >= 8 and not _plain(value)),
        key=lambda item: len(item[1]), reverse=True,
    )

    def scrub(text: str) -> str:
        """Return text with the owner environment's added values hidden."""
        result = str(text or "")
        for name, value in hidden:
            result = result.replace(value, f"<{name}>")
        return URL_SECRET.sub(lambda match: f"{match.group('scheme')}<hidden>@", result)
    return scrub


def run(argv, *, cwd, env: Mapping[str, str], timeout: float) -> tuple[int, str, bool]:
    """Run argv in its own process group, stdin closed, output capped, with a hard limit: (exit code, output, timed out).
    Whatever the command left running (a server it started) is stopped afterwards too."""
    process = subprocess.Popen(
        list(argv), cwd=str(cwd), env=dict(env), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    timed_out = False
    try:
        raw, _ = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        _stop_group(process.pid)
        try:
            raw, _ = process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            raw = b""
    _stop_group(process.pid)
    text = (raw or b"").decode("utf-8", "replace")
    if len(text) > OUTPUT_CAP:
        text = text[: OUTPUT_CAP // 2] + "\n…\n" + text[-OUTPUT_CAP // 2:]
    return (-1 if timed_out else int(process.returncode or 0)), text, timed_out


def _stop_group(pid: int) -> None:
    """Stop every process left in the group pid leads (a helper or server the command started)."""
    for signal_number in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(pid, signal_number)
        except (ProcessLookupError, PermissionError):
            return
        time.sleep(0.2)
