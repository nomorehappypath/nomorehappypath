#!/usr/bin/env python3
# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
"""Compose the active directive set from the portable engine + a filled profile.

Reads a filled profile (via profile_config.load_profile) and writes fully-resolved copies of
the engine directives + templates — every `{{PROFILE:key}}` token replaced with the profile
value — into an output dir (default: ./build/active/). Stdlib only; no third-party deps.

  bash compose.sh                         # uses auto-discovered profile/profile.config
  bash compose.sh --profile path.config --out build/active --strict

Set tokens are substituted inline (lists render comma-joined). Unset tokens render as a
visible `<unset:key>` marker and are reported as a warning (or fail with --strict), never a
silent blank.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from profile_config import load_profile

TOKEN = re.compile(r"\{\{PROFILE:([a-z_]+)\}\}")


def render_value(v) -> str:
    if isinstance(v, list):
        return ", ".join(str(x) for x in v)
    return str(v)


def substitute(text: str, profile: dict, unset: set) -> str:
    def repl(m: re.Match) -> str:
        key = m.group(1)
        val = profile.get(key)
        if val is None or val == "" or val == []:
            unset.add(key)
            return f"<unset:{key}>"
        return render_value(val)

    return TOKEN.sub(repl, text)


def engine_dir() -> Path:
    # scripts/ -> engine/
    return Path(__file__).resolve().parent.parent


def source_files(eng: Path) -> list[Path]:
    files: list[Path] = []
    for sub in ("directives", "templates"):
        d = eng / sub
        if d.is_dir():
            files += sorted(d.glob("*.md"))
    return files


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Compose active directives from engine/ + profile.config")
    ap.add_argument("--profile", help="Path to profile.config (else $DEV_HARNESS_PROFILE / auto-discovered)")
    ap.add_argument("--out", default="build/active", help="Output dir for resolved directives (default: build/active)")
    ap.add_argument("--strict", action="store_true", help="Exit nonzero if any token is unset")
    args = ap.parse_args(argv)

    profile = load_profile(args.profile)
    if not profile:
        print("No profile found. Provide --profile or create profile/profile.config.", file=sys.stderr)
        return 2

    eng = engine_dir()
    files = source_files(eng)
    if not files:
        print(f"No engine source files under {eng}/directives or /templates.", file=sys.stderr)
        return 2

    out_root = Path(args.out)
    unset: set = set()
    seen_tokens: set = set()
    written: list[Path] = []

    for f in files:
        text = f.read_text(encoding="utf-8")
        seen_tokens.update(m.group(1) for m in TOKEN.finditer(text))
        resolved = substitute(text, profile, unset)
        dest = out_root / f.relative_to(eng)  # preserves directives/ , templates/
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(resolved, encoding="utf-8")
        written.append(dest)

    # Safety net: no raw {{PROFILE:}} may remain in any output (set tokens fully resolved).
    residual = [d for d in written if "{{PROFILE:" in d.read_text(encoding="utf-8")]

    set_tokens = sorted(seen_tokens - unset)
    print(f"Composed {len(written)} file(s) -> {out_root}/")
    print(f"  tokens substituted: {', '.join(set_tokens) or '(none)'}")
    if unset:
        print(f"  WARNING unset token(s), rendered as <unset:...>: {', '.join(sorted(unset))}")
    if residual:
        names = ", ".join(str(r) for r in residual)
        print(f"  ERROR residual {{PROFILE:}} markers remain in: {names}", file=sys.stderr)
        return 2
    if unset and args.strict:
        print("  --strict: failing due to unset token(s).", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
