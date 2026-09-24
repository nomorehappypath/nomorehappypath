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
    return values
