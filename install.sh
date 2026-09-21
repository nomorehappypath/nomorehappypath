#!/usr/bin/env bash
# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
# NoMoreHappyPath installer: check every prerequisite honestly, then install
# the auto-start service or run once. No silent happy path.
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# The one place that knows what a service, a log directory, and an opener mean
# on this platform. Sourced, not executed: these are functions, not a program.
source "$root/scripts/platform_support.sh"
label="$(service_label)"
plist="$(service_unit_path)"
log_dir="$(service_log_path)"
mode="${1:-}"

say()  { printf '%s\n' "$*"; }
ok()   { printf '  \033[32m✔\033[0m %s\n' "$*"; }
warn() { printf '  \033[33m✱\033[0m %s\n' "$*"; }
bad()  { printf '  \033[31m✘\033[0m %s\n' "$*"; }

if [[ "$mode" == "--uninstall" ]]; then
  if [[ -f "$plist" ]]; then
    autostart_remove
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
if platform_supported; then
  ok "$(platform_display_label)"
else
  bad "This platform is $(uname -s). NoMoreHappyPath runs on macOS and Linux."
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

# Agent CLIs — found the way the APP finds them, not the way this shell does.
#
# This used `command -v`, which respects the login PATH. The app resolves CLIs
# through the platform seam, because a service manager supplies a minimal
# environment and there is no login shell to fix it. On a real machine the two
# disagreed: Claude Code was installed at ~/.local/bin/claude, the app found it,
# and this check told the owner to install it again.
#
# That is the SAME class as public issue #1 — a CLI in ~/.local/bin the app
# could not find — fixed in the app and never fixed here. A prerequisite check
# that disagrees with the program it is checking for is worse than none: it
# sends the owner to fix something that is not broken.
provider_path() {
  # The interpreter the prerequisite check just validated. Bare `python3` would
  # be a second, unstated dependency in a script whose whole job is naming them.
  local interpreter; interpreter="$(command -v python3 || true)"
  [[ -n "$interpreter" ]] || return 1
  "$interpreter" -E -c '
import sys
sys.path.insert(0, sys.argv[1])
from harness import global_settings
try:
    print(global_settings.provider_executable(sys.argv[2]))
except Exception:
    raise SystemExit(1)
' "$root" "$1" 2>/dev/null
}
if codex_path="$(provider_path codex)" && [[ -n "$codex_path" ]]; then
  ok "Codex CLI found ($codex_path) — uses your OpenAI account"
else
  warn "Codex CLI not found. The Delivery agent needs it: 'npm install -g @openai/codex', then sign in with your OpenAI account (paid plan)."
  missing=1
fi
if claude_path="$(provider_path claude)" && [[ -n "$claude_path" ]]; then
  ok "Claude Code CLI found ($claude_path) — uses your Anthropic account"
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
    # Never pretend to install a service that cannot exist here. A container or
    # a chroot without systemd would otherwise "succeed" and start nothing.
    if ! service_manager_available; then
      bad "No service manager is available on this system ($(service_manager_label) not found)."
      say "Run it in the foreground instead:  bash $root/scripts/start_project_manager.sh"
      exit 1
    fi
    autostart_install "$root" "$log_dir"
    ok "Auto-start installed via $(service_manager_label)."
    say "  unit: $(service_unit_path)"
    say "  log:  $log_dir/nomorehappypath.log"
    # A systemd user service stops when the last session for this user ends.
    # On a headless box that means: install over SSH, log out, and it dies —
    # which the owner experiences as "it randomly stopped working".
    if [[ "$(platform_kind)" == "linux" ]]; then
      if linger_enabled; then
        ok "Lingering is on, so the service keeps running after you log out."
      else
        warn "Lingering is OFF. This service will STOP when you log out of this machine."
        say "  To keep it running after logout, run:  $(linger_command)"
        say "  Until then it runs only while you are logged in."
      fi
    fi
    say "Waiting for the app…"
    for _ in $(seq 1 40); do
      if curl --silent --fail --output /dev/null http://127.0.0.1:8740/; then
        ok "NoMoreHappyPath is running: http://127.0.0.1:8740/"
        # Through the seam, which tries `open`, falls back to `xdg-open`, and
        # PRINTS the URL where neither exists.
        #
        # This was a bare `open` under `set -euo pipefail` with no `|| true`.
        # Stage 0 deliberately left it that way, because routing it would have
        # changed the installer's exit behaviour on macOS and Stage 0 forbade
        # that. Once Linux was supported the calculation inverted: on a headless
        # box the opener fails, and the installer exited 3 immediately AFTER
        # telling the owner the app was running. A successful install reported
        # as a failure is worse than no message at all.
        owner_open_url "http://127.0.0.1:8740/"
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
