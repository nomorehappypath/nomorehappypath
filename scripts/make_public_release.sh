#!/usr/bin/env bash
# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
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
for item in harness web engine tests scripts directives profile.template \
            profile.example.json LICENSE NOTICE DISCLAIMER.md CONTRIBUTING.md \
            README.md install.sh .gitignore; do
  cp -R "$source_root/$item" "$out/"
done
find "$out" \( -name __pycache__ -o -name .DS_Store -o -name "*.pyc" \) -prune -exec rm -rf {} + 2>/dev/null || true
rm -rf "$out/web/node_modules" 2>/dev/null || true

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
# The trace pattern is assembled from pieces so this script's own shipped
# copy never contains the literal it hunts.
trace="aba""lgir"
if grep -rn "$trace" "$out" --exclude-dir=.git 2>/dev/null | grep -v "/Users/owner/" | grep -q .; then
  echo "REFUSED: personal traces in outgoing tree:" >&2
  grep -rn "$trace" "$out" --exclude-dir=.git | head >&2
  exit 1
fi
if grep -rInE "sk-[A-Za-z0-9]{40,}|AKIA[0-9A-Z]{16}|ghp_[A-Za-z0-9]{30,}|gho_[A-Za-z0-9]{30,}|-----BEGIN [A-Z ]*PRIVATE KEY" "$out" --exclude-dir=.git | grep -q .; then
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
( cd "$out" && PYTHONPATH=. PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests )

echo "== All gates passed: $out is ready for release commit"
