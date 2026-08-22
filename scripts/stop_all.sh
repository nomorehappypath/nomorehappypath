#!/usr/bin/env bash
# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
# Stop every NoMoreHappyPath process belonging to THIS installation - the
# manager, its private worker, and this install's auto-start service.
# Scoped by path on purpose: another installation on the same Mac (for
# example a development copy) is left completely alone.
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
label="com.nomorehappypath.app"
plist="$HOME/Library/LaunchAgents/$label.plist"

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
  echo "Usage: stop_all.sh          stop this installation's app processes"
  echo "       stop_all.sh --list   only show what would be stopped"
  exit 0
fi

echo "NoMoreHappyPath — stopping processes of: $root"
matches="$(pgrep -fl "python3.*$root/harness/(project_manager|project_worker)\.py" || true)"
if [[ "${1:-}" == "--list" ]]; then
  if [[ -n "$matches" ]]; then echo "$matches"; else echo "  (none running from this installation)"; fi
  [[ -f "$plist" ]] && echo "  auto-start service installed: $plist (a plain run also removes it for this stop)"
  exit 0
fi

# The service would instantly restart what we kill - take it out first.
if [[ -f "$plist" ]]; then
  launchctl bootout "gui/$(id -u)" "$plist" 2>/dev/null || true
  echo "  ✔ auto-start service stopped (reinstall any time with: bash install.sh)"
fi

if [[ -n "$matches" ]]; then
  echo "$matches" | while read -r pid rest; do
    kill "$pid" 2>/dev/null || true
    echo "  ✔ stopped pid $pid"
  done
  sleep 1
  leftover="$(pgrep -f "python3.*$root/harness/(project_manager|project_worker)\.py" || true)"
  if [[ -n "$leftover" ]]; then
    echo "$leftover" | xargs kill -9 2>/dev/null || true
    echo "  ✔ force-stopped stubborn processes"
  fi
else
  echo "  (no app processes were running from this installation)"
fi
echo "Done. Your projects and settings in ~/.harness-home are untouched."
