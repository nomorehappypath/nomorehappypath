#!/usr/bin/env python3
# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
"""Stdlib-only loader for the harness profile (profile.config).

The profile is a small, comment-friendly YAML subset:
  - `key: value`            scalar (quotes optional, inline `  # comment` stripped)
  - `key: []`               empty list
  - `key:` then `- item`    block list

No external dependencies (no pyyaml): the harness must run on a bare Python 3.
This loader is intentionally limited to the subset profile.config actually uses.

Discovery order when no explicit path is given:
  1. $DEV_HARNESS_PROFILE
  2. nearest `profile/profile.config` searching upward from cwd
  3. nearest `profile.config` searching upward from cwd
Returns {} when nothing is found, so callers fall back to safe defaults.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Optional


def find_profile(explicit: Optional[str] = None) -> Optional[Path]:
    if explicit:
        p = Path(explicit).expanduser()
        return p if p.is_file() else None
    env = os.environ.get("DEV_HARNESS_PROFILE")
    if env:
        p = Path(env).expanduser()
        if p.is_file():
            return p
    here = Path.cwd().resolve()
    for base in [here, *here.parents]:
        for rel in ("profile/profile.config", "profile.config"):
            cand = base / rel
            if cand.is_file():
                return cand
    return None


def _strip_inline_comment(value: str) -> str:
    # Remove a trailing `  # comment`. Only whitespace-preceded '#' counts, so a
    # value like a URL fragment without a leading space is preserved.
    m = re.search(r"\s#", value)
    return value[: m.start()] if m else value


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value


def parse(text: str) -> dict[str, Any]:
    result: dict[str, Any] = {}
    current_list_key: Optional[str] = None
    for raw in text.splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("- ") and current_list_key is not None:
            item = _unquote(_strip_inline_comment(stripped[2:]).strip())
            if item:
                result[current_list_key].append(item)
            continue
        if ":" not in raw:
            current_list_key = None
            continue
        key, _, rest = raw.partition(":")
        key = key.strip()
        val = _strip_inline_comment(rest).strip()
        if val == "" :
            result[key] = []           # may stay empty, or fill from block items
            current_list_key = key
        elif val == "[]":
            result[key] = []
            current_list_key = None
        else:
            result[key] = _unquote(val)
            current_list_key = None
    return result


def load_profile(explicit: Optional[str] = None) -> dict[str, Any]:
    path = find_profile(explicit)
    if not path:
        return {}
    try:
        return parse(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


if __name__ == "__main__":
    import json
    import sys

    arg = sys.argv[1] if len(sys.argv) > 1 else None
    p = find_profile(arg)
    print(f"# profile: {p or 'NOT FOUND'}")
    print(json.dumps(load_profile(arg), indent=2, sort_keys=True))
