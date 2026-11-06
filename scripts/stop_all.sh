#!/usr/bin/env bash
# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
# Stop every NoMoreHappyPath process belonging to THIS installation - the
# manager, its private worker, and this install's auto-start service.
# Scoped by path on purpose: another installation on the same Mac (for
# example a development copy) is left completely alone.
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# pgrep -f takes a REGEX: every metacharacter in the path must be escaped or
# /tmp/harness.next would also match /tmp/harnessXnext - and kill it.
root_re="$(printf '%s' "$root" | sed -e 's/[][\.^$*+?(){}|/]/\\&/g')"
# The app's command line is "<interpreter> [flags] <root>/harness/project_manager.py ...", and the interpreter's name varies: on a
# Mac whose only Python is Xcode's it is ".../Python.app/Contents/MacOS/Python -E", with no "python3" in it. So match the script
# path under THIS root, which must be a whole argument (start of line or after a space), so /x/root never matches /y/x/root.
app_re="(^| )$root_re\/harness\/(project_manager|project_worker)\.py"
source "$root/scripts/platform_support.sh"
label="$(service_label)"
plist="$(service_unit_path)"

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
  echo "Usage: stop_all.sh          stop this installation's app processes"
  echo "       stop_all.sh --list   only show what would be stopped"
  exit 0
fi

echo "NoMoreHappyPath — stopping processes of: $root"
matches="$(pgrep -fl "$app_re" || true)"
if [[ "${1:-}" == "--list" ]]; then
  if [[ -n "$matches" ]]; then echo "$matches"; else echo "  (none running from this installation)"; fi
  [[ -f "$plist" ]] && echo "  auto-start service installed: $plist (a plain run also removes it for this stop)"
  exit 0
fi

# The service would instantly restart what we kill - take it out first.
if [[ -f "$plist" ]]; then
  autostart_stop
  echo "  ✔ auto-start service stopped (reinstall any time with: bash install.sh)"
fi

if [[ -n "$matches" ]]; then
  echo "$matches" | while read -r pid rest; do
    kill "$pid" 2>/dev/null || true
    echo "  ✔ stopped pid $pid"
  done
  sleep 1
  leftover="$(pgrep -f "$app_re" || true)"
  if [[ -n "$leftover" ]]; then
    echo "$leftover" | xargs kill -9 2>/dev/null || true
    echo "  ✔ force-stopped stubborn processes"
  fi
else
  echo "  (no app processes were running from this installation)"
fi
echo "Done. Your projects and settings in ~/.harness-home are untouched."
