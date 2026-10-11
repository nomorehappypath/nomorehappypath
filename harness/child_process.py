# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Explicit environment boundaries for programs launched by the harness."""
from __future__ import annotations

from collections.abc import Mapping
import os
from pathlib import Path
import sys

from harness import platform_support


SHELL_AMBIENT_KEYS = frozenset({
    "BASH_ENV", "ENV", "CDPATH", "IFS", "SHELLOPTS", "BASHOPTS", "ZDOTDIR",
})

EXECUTION_ENVIRONMENT_KEYS = frozenset({
    "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE",
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy",
    "no_proxy", "VIRTUAL_ENV", "CONDA_PREFIX",
})


def _execution_path(source: Mapping[str, str]) -> str:
    """Return one stable tool path independent of an agent's interactive shell."""
    return platform_support.discovery().governed_execution_path(source)


def environment(
    source: Mapping[str, str] | None = None,
    *,
    git: bool = False,
    python: bool = False,
    shell: bool = False,
) -> dict[str, str]:
    """Copy the parent environment while neutralizing selected child contracts.

    Provider credentials, proxy configuration, HOME, PATH, and harness settings
    remain available. Tool-specific variables that can redirect repository,
    import, startup-script, or shell resolution are removed at the boundary.
    """
    values = dict(os.environ if source is None else source)
    if git:
        values = {key: value for key, value in values.items() if not key.startswith("GIT_")}
        # Absence would let Git fall back to ambient system/global config files.
        # Pin both sources explicitly for harness-owned evidence operations.
        values["GIT_CONFIG_NOSYSTEM"] = "1"
        values["GIT_CONFIG_GLOBAL"] = os.devnull
    if python:
        values = {key: value for key, value in values.items() if not key.startswith("PYTHON")}
    if shell:
        values = {key: value for key, value in values.items() if key not in SHELL_AMBIENT_KEYS}
    return values


def execution_environment(source: Mapping[str, str] | None = None) -> dict[str, str]:
    """Build the reproducible, non-secret environment for governed commands.

    Provider credentials and arbitrary shell variables are deliberately absent.
    A test that requires configuration must declare it in its command or fixture,
    so the same bytes execute the same way for Delivery, Reviewer, and release.
    """
    parent = dict(os.environ if source is None else source)
    values = {
        key: value for key, value in parent.items()
        if key in EXECUTION_ENVIRONMENT_KEYS and isinstance(value, str)
    }
    values.update({
        "PATH": _execution_path(parent),
        "LANG": values.get("LANG") or "C.UTF-8",
        "TZ": "UTC",
        "PYTHONHASHSEED": "0",
        "PYTHONDONTWRITEBYTECODE": "1",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "CI": "1",
    })
    from harness import platform_support
    host = platform_support.browser_host()
    if host.confined_by_harness() and not host.inside_os_sandbox():
        # A confined process that starts governed execution again (a fixture-board test, for example) keeps telling
        # its browser helper that the outer confinement is in place.
        values[host.CONFINED_MARKER] = "1"
    return values


# Set for every governed test, scenario or health command (and so for everything it starts): the canonical data
# root of the LIVE board it must not touch. `board.main` refuses a call aimed at that board with a plain message
# instead of failing on the protected lock file. A diagnostic only: OS confinement is the protection.
LIVE_BOARD_ENV = "HARNESS_CERTIFIED_BOARD"


def with_live_board(environment: Mapping[str, str], live_root: object) -> dict[str, str]:
    from harness.project_context import project_context
    values = dict(environment)
    values[LIVE_BOARD_ENV] = str(Path(project_context(live_root).data_root).resolve())
    return values


def run_bounded(argv, *, cwd, env, timeout: float):
    """Run a command in its own process group with a hard time limit and return
    (exit_code, output, timed_out). On timeout the whole group is stopped, so a server or
    helper the command started cannot outlive it and hold a port or a pipe."""
    import os
    import signal
    import subprocess
    process = subprocess.Popen(
        list(argv), cwd=cwd, env=dict(env), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, start_new_session=True,
    )
    try:
        output, _ = process.communicate(timeout=timeout)
        return int(process.returncode or 0), output or "", False
    except subprocess.TimeoutExpired:
        for signal_number in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(process.pid, signal_number)
            except ProcessLookupError:
                break
            try:
                output, _ = process.communicate(timeout=5)
                return -1, output or "", True
            except subprocess.TimeoutExpired:
                continue
        try:
            output, _ = process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            output = ""
        return -1, output or "", True
