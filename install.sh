#!/usr/bin/env bash
# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
# NoMoreHappyPath installer: check every prerequisite honestly, then install
# the auto-start service or run once. No silent happy path.
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
label="com.nomorehappypath.app"
plist="$HOME/Library/LaunchAgents/$label.plist"
log_dir="$HOME/Library/Logs"
mode="${1:-}"

say()  { printf '%s\n' "$*"; }
ok()   { printf '  \033[32m✔\033[0m %s\n' "$*"; }
warn() { printf '  \033[33m✱\033[0m %s\n' "$*"; }
bad()  { printf '  \033[31m✘\033[0m %s\n' "$*"; }

if [[ "$mode" == "--uninstall" ]]; then
  if [[ -f "$plist" ]]; then
    launchctl bootout "gui/$(id -u)" "$plist" 2>/dev/null || true
    rm -f "$plist"
    say "Removed the auto-start service. Your projects and settings in ~/.harness-home are untouched."
  else
    say "No auto-start service is installed. Nothing to remove."
  fi
  exit 0
fi
if [[ -n "$mode" && "$mode" != "--check" ]]; then
  say "Usage: bash install.sh [--check | --uninstall]"; exit 2
fi

say "NoMoreHappyPath — prerequisite check"
missing=0
core_missing=0

# macOS
if [[ "$(uname -s)" == "Darwin" ]]; then
  ok "macOS $(sw_vers -productVersion 2>/dev/null || echo '(version unknown)')"
else
  bad "This platform is $(uname -s). NoMoreHappyPath currently runs on macOS only."
  exit 1
fi

# Python 3.9+
if python3 - <<'PY' 2>/dev/null
import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)
PY
then
  ok "Python $(python3 -V 2>&1 | cut -d' ' -f2)"
else
  bad "Python 3.9 or newer is required. Install from https://www.python.org/downloads/ or 'brew install python3'."
  core_missing=1
fi

# Agent CLIs — the product governs these; be explicit about accounts.
if command -v codex >/dev/null 2>&1; then
  ok "Codex CLI found ($(command -v codex)) — uses your OpenAI account"
else
  warn "Codex CLI not found. The Delivery agent needs it: 'npm install -g @openai/codex', then sign in with your OpenAI account (paid plan)."
  missing=1
fi
if command -v claude >/dev/null 2>&1; then
  ok "Claude Code CLI found ($(command -v claude)) — uses your Anthropic account"
else
  warn "Claude Code CLI not found. The Reviewer/CTO needs it: install from https://claude.com/claude-code, then sign in with your Anthropic account (paid plan)."
  missing=1
fi

# OpenAI API key for project chat — configured in the app, not here.
say ""
say "Project chat needs an OpenAI API key (pay-per-use). You do NOT enter it"
say "here: open Settings in the app after install, paste it there, and it is"
say "verified with OpenAI before being stored in a file only you can read."

if [[ $core_missing -eq 1 ]]; then
  say ""; bad "Core requirements missing — fix the ✘ items above, then rerun."; exit 1
fi
if [[ $missing -eq 1 ]]; then
  say ""
  warn "You can install and explore now, but agent sessions will not launch until the missing CLI(s) above are installed and signed in."
fi
[[ "$mode" == "--check" ]] && exit 0

say ""
say "How do you want to run it?"
say "  1) Install the auto-start service (starts at login, restarts itself; recommended)"
say "  2) Run once in the foreground (Ctrl-C stops it)"
read -r -p "Choose 1 or 2: " choice
case "$choice" in
  1)
    mkdir -p "$(dirname "$plist")" "$log_dir"
    /usr/bin/python3 - "$root" "$plist" "$label" "$log_dir" <<'PY'
import plistlib, sys
root, plist, label, log_dir = sys.argv[1:5]
value = {
    "Label": label,
    "ProgramArguments": ["/bin/bash", f"{root}/scripts/start_project_manager.sh", "--no-open"],
    "RunAtLoad": True,
    "KeepAlive": True,
    "StandardOutPath": f"{log_dir}/nomorehappypath.log",
    "StandardErrorPath": f"{log_dir}/nomorehappypath.log",
}
with open(plist, "wb") as stream:
    plistlib.dump(value, stream)
PY
    launchctl bootout "gui/$(id -u)" "$plist" 2>/dev/null || true
    launchctl bootstrap "gui/$(id -u)" "$plist"
    say "Service installed. Waiting for the app…"
    for _ in $(seq 1 40); do
      if curl --silent --fail --output /dev/null http://127.0.0.1:8740/; then
        ok "NoMoreHappyPath is running: http://127.0.0.1:8740/"
        open "http://127.0.0.1:8740/"
        exit 0
      fi
      sleep 0.5
    done
    bad "The app did not answer on http://127.0.0.1:8740/ — check $log_dir/nomorehappypath.log"
    exit 1
    ;;
  2)
    exec bash "$root/scripts/start_project_manager.sh"
    ;;
  *)
    say "No choice made; nothing installed. Rerun 'bash install.sh' when ready."
    ;;
esac
