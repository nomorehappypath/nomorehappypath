# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Ask project chat real questions against a COPY of a registered project.

The project's board, task contracts and memory index are copied into a
temporary folder, so the owner's project is only read.  The OpenAI key saved in
Settings is read into this process's environment and never written anywhere.

    python3 scripts/project_chat_probe.py --project studio \
        "what have we solved as of now?" "did we upgrade the brand identity agent?"
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from harness import global_settings, project_chat  # noqa: E402
from harness.project_context import context_from_roots  # noqa: E402


def _project(home: Path, name: str) -> dict:
    registry = json.loads((home / "registry.json").read_text(encoding="utf-8"))
    for project in registry.get("projects", []):
        if name in {project.get("name"), project.get("id")}:
            return project
    raise SystemExit(f"no registered project named {name!r}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--home", default=str(Path.home() / ".harness-home"))
    parser.add_argument("--project", required=True, help="registered project name or id")
    parser.add_argument("questions", nargs="+")
    args = parser.parse_args()

    home = Path(args.home).expanduser()
    source = Path(_project(home, args.project)["data_root"])
    key = global_settings.openai_api_key(home)
    with tempfile.TemporaryDirectory(prefix="chat-probe-") as scratch:
        scratch_path = Path(scratch)
        data = scratch_path / "data"
        (data / "board").mkdir(parents=True)
        shutil.copy2(source / "board" / "state.json", data / "board" / "state.json")
        for folder in ("tasks", "memory"):
            if (source / folder).is_dir():
                shutil.copytree(
                    source / folder, data / folder,
                    ignore=shutil.ignore_patterns("*.lock"), dirs_exist_ok=True,
                )
        code = scratch_path / "code"
        code.mkdir()
        context = context_from_roots(code, data, scratch_path / "workspaces")
        settings_home = scratch_path / "home"
        os.environ[global_settings.OPENAI_API_KEY_ENV] = key
        for question in args.questions:
            result = project_chat.answer_question(context, question, settings_home=settings_home)
            print(f"Q: {question}")
            print(result["answer"])
            print(f"   [cited: {', '.join(item['fact_id'] for item in result['claims']) or '-'}"
                  f"; composed: {result.get('composed', False)}]\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
