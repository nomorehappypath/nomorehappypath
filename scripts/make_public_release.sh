#!/usr/bin/env bash
# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
# Gated assembly of the public NoMoreHappyPath release tree.
# Encodes the release gates of the governance directive: cut, scrub,
# marker stealth, provenance refusal, and the in-tree test suite.
# It assembles and verifies; pushing is a separate, human-ordered act.
set -euo pipefail

source_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
out="${1:?usage: make_public_release.sh <output-dir> [version] (dir must not exist)}"
version="${2:-}"
[[ -e "$out" ]] && { echo "REFUSED: output exists: $out" >&2; exit 1; }

echo "== Gate 0: assemble the standing cut"
mkdir -p "$out"
for item in harness tests scripts directives \
            LICENSE NOTICE DISCLAIMER.md CONTRIBUTING.md \
            README.md install.sh .gitignore; do
  cp -R "$source_root/$item" "$out/"
done
# docs/ stays private, except the Support install folder (guide, systemd unit, proxy config):
# scripts/check_support_install.sh and the public install guide need it (v0.19.0).
# (Guarded: tests run this script against small planted source trees that have no docs/support.)
if [[ -d "$source_root/docs/support" ]]; then
  mkdir -p "$out/docs" && cp -R "$source_root/docs/support" "$out/docs/"
fi
# The skill library (skills/) is read by the running app (harness/skill_library.py); without it a public
# install has an empty library and the Go ahead card offers no guides (v0.24.0 and v0.24.1 shipped without it).
# (Guarded like docs/support: tests run this script against small planted source trees that have no skills/.)
if [[ -d "$source_root/skills" ]]; then
  cp -R "$source_root/skills" "$out/"
fi
# The community code of conduct (Contributor Covenant) ships with the other community documents.
# (Guarded like skills/: tests run this script against small planted source trees that have no such file.)
if [[ -f "$source_root/CODE_OF_CONDUCT.md" ]]; then
  cp "$source_root/CODE_OF_CONDUCT.md" "$out/"
fi
find "$out" \( -name __pycache__ -o -name .DS_Store -o -name "*.pyc" \) -prune -exec rm -rf {} + 2>/dev/null || true

if [[ -n "$version" ]]; then
  printf '%s\n' "$version" > "$out/VERSION"
  echo "   stamped VERSION=$version"
fi

echo "== Gate 1: provenance/decoder refusal"
if find "$out" -iname "*provenance*" | grep -q .; then
  echo "REFUSED: provenance material in outgoing tree:" >&2
  find "$out" -iname "*provenance*" >&2
  exit 1
fi

echo "== Gate 2: secret and personal-data scrub"
# The cost center's private ledger (token counts per task) must never ship (task 2026-10-27_cost-center).
if find "$out" \( -name "cost_ledger*" \) | grep -q .; then
  echo "REFUSED: private cost ledger in outgoing tree:" >&2
  find "$out" -name "cost_ledger*" >&2
  exit 1
fi
# The trace pattern is assembled from pieces so this script's own shipped
# copy never contains the literal it hunts.
trace="aba""lgir"
if grep -rn "$trace" "$out" --exclude-dir=.git 2>/dev/null | grep -v "/Users/owner/" | grep -q .; then
  echo "REFUSED: personal traces in outgoing tree:" >&2
  grep -rn "$trace" "$out" --exclude-dir=.git | head >&2
  exit 1
fi
if grep -rInE "sk-[A-Za-z0-9]{40,}|sk-ant-[A-Za-z0-9_-]{20,}|AKIA[0-9A-Z]{16}|ghp_[A-Za-z0-9]{30,}|gho_[A-Za-z0-9]{30,}|-----BEGIN [A-Z ]*PRIVATE KEY" "$out" --exclude-dir=.git | grep -q .; then
  echo "REFUSED: secret-shaped strings in outgoing tree" >&2
  exit 1
fi

brand="kpi""mind"
echo "== Gate 3: marker stealth (code carries only headers; legal docs may name the company)"
if grep -rin "$brand" "$out" --exclude-dir=.git \
    --exclude=LICENSE --exclude=NOTICE --exclude=DISCLAIMER.md \
    --exclude=CONTRIBUTING.md --exclude=README.md \
    | grep -viE "Copyright \(c\) 2026 KpiMinds LLC|license@kpiminds\.com|KpiMinds LLC|KPIMINDS LLC|KPIMinds LLC" | grep -q .; then
  echo "REFUSED: unexpected company-name material in code:" >&2
  grep -rin "$brand" "$out" --exclude-dir=.git --exclude=LICENSE --exclude=NOTICE --exclude=DISCLAIMER.md --exclude=CONTRIBUTING.md --exclude=README.md | grep -viE "Copyright \(c\) 2026 KpiMinds LLC|license@kpiminds\.com|KpiMinds LLC|KPIMINDS LLC|KPIMinds LLC" | head >&2
  exit 1
fi

echo "== Gate 4: full test suite inside the assembled tree"
# Gate 4 proves, among other things, WHICH processes an execution owned, and it
# learns that by reading the OS process table. A release must never be cut from
# an environment that cannot verify it, so an unreadable process table REFUSES
# here rather than letting the suite report 145 errors, or - worse - letting it
# report a pass built on 145 skips. The probe runs inside the assembled tree
# because that is where the suite runs: sandboxes have been observed permitting
# `ps` in the source checkout and denying it in the assembled tree, which sits
# outside the approved path.
if ! ( cd "$out" && PYTHONPATH=. PYTHONDONTWRITEBYTECODE=1 python3 -c \
        "from harness import browser_acceptance; browser_acceptance._process_table()" ) 2>/dev/null; then
  echo "REFUSED: this environment cannot read the OS process table ('ps') in $out." >&2
  echo "         Certified execution proves process ownership by reading it, so the" >&2
  echo "         release suite cannot verify this tree here and a pass would be false." >&2
  echo "         Re-run the release from a shell that permits reading the process" >&2
  echo "         table for the output directory." >&2
  exit 1
fi
# Owner 2026-11-24: only a material bug stops a release. A failing test stops it until the dev has named it as not material, with the
# review's agreement, in the file NOT_MATERIAL points at: one line per test, its complete id first (module.Class.test), then the reason.
# The complete id is compared, so naming one test never covers another; a run that ends any other way always stops.
log="$(mktemp)"
( cd "$out" && PYTHONPATH=. PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests ) 2>&1 | tee "$log" && suite=0 || suite=$?
if [[ $suite -ne 0 ]]; then
  python3 - "$log" "${NOT_MATERIAL:-}" <<'PY' || exit 1
import re, sys
text = open(sys.argv[1], errors="replace").read()
failed = set()
for name, place in re.findall(r"(?m)^(?:FAIL|ERROR): (\S+) \(([^)\s]+)\)", text):
    failed.add(place if place.split(".")[-1] == name else f"{place}.{name}")
summary = re.findall(r"(?m)^FAILED \((.*)\)\s*$", text)
named = set()
if sys.argv[2]:
    named = {line.split()[0] for line in open(sys.argv[2], encoding="utf-8") if line.strip() and not line.startswith("#")}
open_ones = sorted(failed - named)
headers = len(re.findall(r"(?m)^(?:FAIL|ERROR): ", text))
read = len(re.findall(r"(?m)^(?:FAIL|ERROR): \S+ \([^)\s]+\)", text))
if open_ones or not failed or not summary or headers != read or "unexpected" in summary[-1]:
    print("REFUSED: tests failed: " + (", ".join(open_ones) or "the run did not say which"), file=sys.stderr)
    sys.exit(1)
print("   failing tests named as not material: " + ", ".join(sorted(failed)))
PY
fi

echo "== All gates passed: $out is ready for release commit"
