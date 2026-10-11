# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Which login the Antigravity CLI would use: the owner's Google subscription, or an API key.

The harness runs Gemini on the owner's subscription sign-in, read in place by
the CLI (spec decision D-1, M-2). The CLI can instead be switched to a pay-per-use
API key or a custom endpoint, by `"modelProvider": "gemini"` in its own
`~/.gemini/antigravity-cli/settings.json` or by `GOOGLE_GEMINI_BASE_URL` in the
shell. That is not the subscription the owner chose, and agents would bill a
Google Cloud project without anyone having decided so: the launch and the
connection test REFUSE, in plain words. The harness never edits that file.
"""
from __future__ import annotations

import fcntl
import json
import os
import sys
from pathlib import Path

SETTINGS = Path(".gemini") / "antigravity-cli" / "settings.json"
# Credentials the CLI also reads from the environment; the runner removes them from a managed agent.
STRAY_ENVIRONMENT = ("GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_GEMINI_BASE_URL")
REFUSAL = ("Gemini is set to an API key or a custom endpoint, and the harness uses your Google subscription "
           "sign-in. Remove it from ~/.gemini/antigravity-cli/settings.json{where} and start the agent again.")


def api_key_problem(home: str | Path | None = None, environment: dict[str, str] | None = None) -> str:
    """The plain refusal when the CLI is not on the subscription sign-in, else an empty string."""
    environment = os.environ if environment is None else environment
    path = Path(home or Path.home()) / SETTINGS
    try:
        document = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    except (OSError, ValueError):
        document = {}
    settings = document if isinstance(document, dict) else {}
    if str(settings.get("modelProvider", "")).lower() == "gemini":
        return REFUSAL.format(where="")
    if environment.get("GOOGLE_GEMINI_BASE_URL"):
        return REFUSAL.format(where=" or your shell (GOOGLE_GEMINI_BASE_URL)")
    return ""


def trust_workspaces(paths: list[str], home: str | Path | None = None) -> list[str]:
    """Record the project's folders as trusted by the Antigravity CLI, the way a project's open does for Codex.

    Owner's order 2026-10-03: no human at any folder-trust question. The CLI keeps its answer in
    `trustedWorkspaces` of its own `settings.json`, which an AGENT cannot write (protected); so the
    harness, outside the agent's sandbox, adds the folders the owner opened. Every other key is kept, and a
    file that is not a JSON object is left untouched. Returns the folders newly added.
    """
    path = Path(home or Path.home()) / SETTINGS
    path.parent.mkdir(parents=True, exist_ok=True)
    # Agents of one project start together (CTO, Reviewer, Delivery): the read-modify-write is one locked step,
    # or the last writer would drop the others' folders and a human trust question would come back.
    # Locked on the folder itself: no lock file is ever left in the owner's Gemini state.
    descriptor = os.open(path.parent, os.O_RDONLY)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        return _trust_locked(path, paths)
    finally:
        os.close(descriptor)


def _trust_locked(path: Path, paths: list[str]) -> list[str]:
    try:
        document = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    except (OSError, ValueError):
        return []
    if not isinstance(document, dict):
        return []
    trusted = document.get("trustedWorkspaces")
    trusted = list(trusted) if isinstance(trusted, list) else []
    added = list(dict.fromkeys(str(Path(folder).resolve()) for folder in paths if folder))
    added = [folder for folder in added if folder not in trusted]
    if not added:
        return []
    document["trustedWorkspaces"] = trusted + added
    temporary = path.with_name(f".{path.name}.harness.tmp")
    temporary.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    if path.is_file():
        os.chmod(temporary, path.stat().st_mode & 0o777)
    os.replace(temporary, path)
    return added


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    problem = api_key_problem()
    if problem:
        print(problem, file=sys.stderr)
        return 3
    if argv[:1] == ["--trust"]:
        trust_workspaces(argv[1:])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
