#!/usr/bin/env python3
# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Phone apps, step 1 of 4: the real run (owner direction 2026-11-25).

`python3 scripts/phone_proof.py ready` calls `harness.phone_setup.check()` directly - the exact code the
contract-stage setup check also calls, never a copy or a stand-in - and writes one page,
`build/phone-proof/ready/result.html`, naming the simulated phone's name and version, every step and how
long it took, and the starter app's picture embedded in the page itself. That one file is the only thing
kept; it is not committed.
"""
from __future__ import annotations

import html
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from harness import agent_confinement, phone_setup  # noqa: E402  (after sys.path is set up)


def role_flutter_lines(root: Path) -> list[dict[str, Any]]:
    """Owner release repair 2026-11-26 (round 2): the per-role setup check's own "<role> can use flutter"
    item (environment_check.check_roles -> check_role_program), run for real for Builder, Reviewer and CTO
    inside each role's exact launch confinement - Claude's and Codex's alike (the project's own defaults:
    Builder and CTO on Claude, Reviewer on Codex), widened the same way a real Yes-iPhone agent launch is.
    `root` is a throwaway board root (its settings decide each role's provider; a fresh one falls back to
    the defaults). Returns one {"role", "ok", "detail"} dict per role; raises nothing of its own, each
    role's own failure is carried in its "ok"/"detail" like any other setup-check item."""
    from harness import environment_check, owner_env
    values = phone_setup.host_env()
    scrub = owner_env.redactor(values)
    # min_version (trivially low) makes check_role_program actually run `flutter --version` inside the
    # role's confinement, rather than only checking the binary is executable, so the recorded detail
    # carries flutter's own version line - proof it really answered, not just that it was found.
    needs = {"ai_tools": [], "programs": [{"name": "flutter", "min_version": "1.0.0"}], "folders": [], "libraries": []}
    items = environment_check.check_roles(root, needs, values, scrub, signed_in=True, mobile=True)
    return [{"role": item["label"], "ok": item["ok"], "detail": item["detail"]} for item in items]


def _render(result: dict[str, Any], role_lines: list[dict[str, Any]]) -> str:
    """The one HTML page for `result` (phone_setup.check()'s return value) and `role_lines`
    (role_flutter_lines()'s return value): each of the six steps, pass or fail, the phone's name/version
    and starter-app picture on success or the failing label/detail on failure, then each role's own real
    "<role> can use flutter" line from inside its exact launch confinement."""
    steps_html = "".join(
        f"<li>{'Passed' if step['ok'] else 'Failed'}: {html.escape(step['name'])} ({step['seconds']}s)</li>"
        for step in result.get("steps", [])
    )
    roles_html = "<ul>" + "".join(
        f"<li>{'Passed' if role['ok'] else 'Failed'}: {html.escape(role['role'])}: {html.escape(role['detail'])}</li>"
        for role in role_lines
    ) + "</ul>" if role_lines else ""
    if result["ok"]:
        picture = (
            f'<img alt="The starter app open on the simulated iPhone" '
            f'src="data:image/png;base64,{result.get("picture_png_base64", "")}">'
        ) if result.get("picture_png_base64") else "<p>No picture was kept.</p>"
        body = (
            f"<h1>Phone setup proof: passed</h1>"
            f"<p>Phone: {html.escape(result.get('phone_name', ''))} ({html.escape(result.get('phone_version', ''))})</p>"
            f"<p>{html.escape(result['detail'])}.</p>"
            f"<ol>{steps_html}</ol>{picture}"
            f"<h2>Each role's own real launch sandbox running flutter</h2>{roles_html}"
        )
    else:
        body = (
            f"<h1>Phone setup proof: failed at &ldquo;{html.escape(result['label'])}&rdquo;</h1>"
            f"<pre>{html.escape(result['detail'])}</pre>"
            f"<ol>{steps_html}</ol>"
            f"<h2>Each role's own real launch sandbox running flutter</h2>{roles_html}"
        )
    return f"<!doctype html><html><head><meta charset='utf-8'><title>Phone setup proof</title></head><body>{body}</body></html>"


def main(argv: list[str]) -> int:
    """Run the real phone-setup check for `argv[0] == "ready"`, write its result page, print the outcome,
    and return 0 on a passed check or 1 otherwise (2 for a bad argument)."""
    if not argv or argv[0] != "ready":
        print("usage: phone_proof.py ready", file=sys.stderr)
        return 2
    # CTO finding, owner release repair 2026-11-26: this proof must run Flutter the way agents do, not outside
    # their protected area, so a confinement problem (like Flutter's own cache being unwritable there) shows up
    # here instead of only in real agent use.
    mobile_env = phone_setup.host_env()
    extra_writable = phone_setup.mobile_extra_writable(mobile_env)
    confine = lambda argv: agent_confinement.confine_agent_command(
        argv, ROOT, extra_writable=extra_writable, path_env=mobile_env.get("PATH", ""))
    result = phone_setup.check(progress=lambda text: print(text, flush=True), confine=confine)
    # CTO finding, owner release repair 2026-11-26 (round 2): the owner's "setup check passes the
    # per-role can use flutter line" proof, run for real by NMHP (never asked of the owner, never
    # attempted from an agent's own sandbox, which cannot start a real agent CLI at all).
    with tempfile.TemporaryDirectory(prefix="nmhp-phone-proof-roles-") as roles_root:
        role_lines = role_flutter_lines(Path(roles_root))
    for role in role_lines:
        print(f"{role['role']}: {'PASSED' if role['ok'] else 'FAILED'} - {role['detail']}", flush=True)
    out = ROOT / "build" / "phone-proof" / "ready" / "result.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(_render(result, role_lines), encoding="utf-8")
    roles_ok = all(role["ok"] for role in role_lines)
    print(f"Phone setup proof: {'PASSED' if result['ok'] and roles_ok else 'FAILED'}"
          + ("" if result["ok"] else f" - {result['label']}")
          + ("" if roles_ok else " - a role cannot use flutter"))
    return 0 if result["ok"] and roles_ok else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
