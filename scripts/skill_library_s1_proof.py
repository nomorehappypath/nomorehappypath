#!/usr/bin/env python3
# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""S1 real-program proof: show that the real, installed Claude and Codex
programs, launched exactly the way NMHP's own confinement wraps a live
builder/reviewer (harness.agent_confinement.wrap), can each read every one
of the 12 skill library files at the path NMHP names.

Reads are never confined (only writes are — see agent_confinement.py's own
docstring), so this needs no special grant beyond a normal launch; it is a
genuine, minimal real agent turn per vendor, not a simulation.

Prints one line per (vendor, role, platform) combination: "shown" with the
slugs it proved reading, or "not proven: <reason>" when this run's own
environment cannot show it (wrong platform, the CLI or its sign-in is
unavailable here) — "not proven" is never counted as passed by the caller.

Usage: python3 scripts/skill_library_s1_proof.py
Exit code is always 0; the printed lines are the record.
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from harness import agent_confinement, skill_library  # noqa: E402

ROLES = ("builder", "reviewer")
VENDORS = ("claude", "codex")
CURRENT_PLATFORM = "macOS" if platform.system() == "Darwin" else ("Linux" if platform.system() == "Linux" else platform.system())


def _real_hashes(slugs: list[str], paths: list[str]) -> dict[str, str]:
    """The SHA256 of each file's real bytes, computed independently of the agent.

    This is the token the reviewer asked for: a value present only inside the
    guide's own exact bytes, never in the prompt, so an agent cannot produce it
    by echoing the prompt or by guessing from the slug/path alone — only by
    actually reading that exact file.
    """
    return {slug: hashlib.sha256(Path(path).read_bytes()).hexdigest() for slug, path in zip(slugs, paths)}


def _prompt_for(slugs: list[str], paths: list[str]) -> str:
    lines = "\n".join(f"- {slug}: {path}" for slug, path in zip(slugs, paths))
    return (
        "For each file below, read its exact bytes and compute its SHA256 hex digest "
        "(the whole file, not a part of it). Print exactly one line per file, in the form "
        "`<name>: <hexdigest>`, nothing else:\n" + lines
    )


def _run_one(vendor: str, role: str, slugs: list[str], paths: list[str]) -> tuple[bool, str]:
    """Launch vendor confined the way NMHP starts role, ask it to read every
    skill file and print its real SHA256 hex digest, and check the program's
    own output against the hash this process independently computed from disk.

    Returns (shown, detail): shown is True only when every slug's real,
    independently-computed hash appears in the program's real output and the
    run exited 0 — never from text that could be echoed without reading the
    file (reviewer finding: printing a guide's `name:` line could be produced
    from the prompt itself, proving nothing about whether the file was read).
    """
    search_path = f"/opt/homebrew/bin:{os.environ.get('PATH', '')}"
    located = shutil.which(vendor, path=search_path)
    if not located:
        return False, f"the {vendor} program is not installed/found on PATH in this environment"
    with tempfile.TemporaryDirectory(prefix=f"s1-{vendor}-{role}-") as tmp:
        workspace = Path(tmp) / "workspace"
        workspace.mkdir()
        store = Path(tmp) / "store"
        try:
            wrapped = agent_confinement.wrap(
                [located], [str(workspace)], store=store, home=Path(os.path.expanduser("~")), provider=vendor,
            )
        except agent_confinement.ConfinementUnavailable as error:
            return False, f"confinement unavailable on this platform: {error}"
        prompt = _prompt_for(slugs, paths)
        argv = list(wrapped)
        env = dict(os.environ)
        env["PATH"] = f"/opt/homebrew/bin:{env.get('PATH', '')}"
        try:
            if vendor == "claude":
                result = subprocess.run(argv[1:] + ["-p", prompt], executable=argv[0], cwd=str(workspace),
                                        capture_output=True, text=True, timeout=180, env=env)
            else:
                result = subprocess.run(argv[1:] + ["exec", prompt], executable=argv[0], cwd=str(workspace),
                                        capture_output=True, text=True, timeout=180, env=env)
        except subprocess.TimeoutExpired:
            return False, f"the {vendor} run timed out"
        except OSError as error:
            return False, f"could not launch {vendor}: {error}"
        output = result.stdout + "\n" + result.stderr
        expected = _real_hashes(slugs, paths)
        missing = [slug for slug in slugs if expected[slug] not in output]
        if missing:
            return False, f"missing the real hash for {', '.join(missing)} in the {vendor} {role} output (exit {result.returncode})"
        if result.returncode != 0:
            # The expected hashes appearing in stdout/stderr is not proof the run actually
            # succeeded - a failing run can still echo its own prompt or partial output.
            # Only a clean exit, together with every real hash present, counts as the
            # program having really read the files (not echoed a guess).
            return False, f"the {vendor} {role} run exited {result.returncode} even though the expected hashes were present"
        return True, f"all {len(slugs)} skills' real file hashes shown"


def main() -> int:
    skills = skill_library.usable_skills()
    slugs = [skill.slug for skill in skills]
    paths = [str(skill.path) for skill in skills]
    results = []
    for vendor in VENDORS:
        for role in ROLES:
            for platform_name in ("macOS", "Linux"):
                if platform_name != CURRENT_PLATFORM:
                    results.append({"vendor": vendor, "role": role, "platform": platform_name,
                                    "shown": False, "detail": f"not proven: this run is on {CURRENT_PLATFORM}, not {platform_name}"})
                    continue
                shown, detail = _run_one(vendor, role, slugs, paths)
                results.append({"vendor": vendor, "role": role, "platform": platform_name,
                                "shown": shown, "detail": detail if shown else f"not proven: {detail}"})
    for row in results:
        print(f"{row['vendor']} {row['role']} {row['platform']}: {'shown' if row['shown'] else row['detail']}")
    print(json.dumps(results))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
