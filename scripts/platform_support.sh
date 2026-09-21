#!/usr/bin/env bash
# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
#
# The one place that knows what an auto-start service, a log directory, and a
# URL opener mean on this platform. A sourced library, because every caller is
# bash: install.sh, scripts/stop_all.sh, and the two start_ scripts.
#
# Stage 0 rule: this MOVES the macOS behaviour, it does not change it. Every
# function below renders exactly what its call site rendered before, including
# the parts that look like duplication and are not.

# --- which platform is this -----------------------------------------------------

platform_kind() {
  case "$(uname -s)" in
    Darwin) printf '%s' "darwin" ;;
    Linux)  printf '%s' "linux" ;;
    *)      printf '%s' "unsupported" ;;
  esac
}

# --- where this platform keeps things -----------------------------------------

service_label() { printf '%s' "com.nomorehappypath.app"; }

service_unit_path() {
  if [[ "$(platform_kind)" == "darwin" ]]; then
    printf '%s' "$HOME/Library/LaunchAgents/$(service_label).plist"
  else
    printf '%s' "${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/$(service_label).service"
  fi
}

service_log_path() {
  if [[ "$(platform_kind)" == "darwin" ]]; then
    printf '%s' "$HOME/Library/Logs"
  else
    # XDG puts logs under state, not cache: cache may be cleared at any time and
    # a log the owner is asked to read must survive that.
    printf '%s' "${XDG_STATE_HOME:-$HOME/.local/state}/nomorehappypath"
  fi
}

# Is there a service manager to install into at all?
service_manager_available() {
  if [[ "$(platform_kind)" == "darwin" ]]; then
    command -v launchctl >/dev/null 2>&1
  else
    command -v systemctl >/dev/null 2>&1 && [[ -d /run/systemd/system ]]
  fi
}

service_manager_label() {
  if [[ "$(platform_kind)" == "darwin" ]]; then printf '%s' "launchd (per-user LaunchAgent)"
  else printf '%s' "systemd (per-user service)"; fi
}

# A systemd USER service runs only while that user has a session, unless
# lingering is on. On a headless box the owner installs over SSH, logs out, and
# the service stops — which presents as "it randomly stopped working". We report
# it; we do NOT enable it silently, because that changes system state beyond
# this app.
# $USER is NOT reliably set: a service manager supplies a minimal environment,
# and under `set -euo pipefail` an unbound USER aborts the installer outright.
# `id -un` always answers.
service_user() { printf '%s' "${USER:-$(id -un)}"; }

linger_enabled() {
  [[ "$(platform_kind)" == "linux" ]] || return 0
  [[ "$(loginctl show-user "$(service_user)" --property=Linger --value 2>/dev/null)" == "yes" ]]
}

linger_command() { printf '%s' "sudo loginctl enable-linger $(service_user)"; }

# --- the platform gate --------------------------------------------------------
#
# Stage 1 lifted the refusal here, in one function, exactly as Stage 0 intended.
# Everything else in this file branches on platform_kind rather than re-deriving
# the platform from `uname` at each site.

platform_supported() { [[ "$(platform_kind)" != "unsupported" ]]; }

platform_display_label() {
  if [[ "$(platform_kind)" == "darwin" ]]; then
    printf 'macOS %s' "$(sw_vers -productVersion 2>/dev/null || echo '(version unknown)')"
  else
    # PRETTY_NAME is the line a Linux owner recognises as "their" distro.
    local name=""
    [[ -r /etc/os-release ]] && name="$(. /etc/os-release 2>/dev/null && printf '%s' "${PRETTY_NAME:-}")"
    printf '%s' "${name:-Linux ($(uname -r))}"
  fi
}

# --- the auto-start service ---------------------------------------------------

autostart_installed() { [[ -f "$(service_unit_path)" ]]; }

# Deactivate and LEAVE the unit in place, so the owner need not rerun the
# installer after a plain stop.
autostart_stop() {
  local unit; unit="$(service_unit_path)"
  [[ -f "$unit" ]] || return 0
  if [[ "$(platform_kind)" == "darwin" ]]; then
    launchctl bootout "gui/$(id -u)" "$unit" 2>/dev/null || true
  else
    # stop, NOT disable: the unit stays installed and enabled, so the owner
    # need not rerun the installer after a plain stop. Same contract as macOS.
    systemctl --user stop "$(service_label).service" 2>/dev/null || true
  fi
}

# Deactivate AND remove. This differs from autostart_stop by exactly one line,
# and the difference is deliberate: collapsing them into one uninstall() would
# make a plain stop silently uninstall the service.
autostart_remove() {
  local unit; unit="$(service_unit_path)"
  [[ -f "$unit" ]] || return 0
  if [[ "$(platform_kind)" == "darwin" ]]; then
    launchctl bootout "gui/$(id -u)" "$unit" 2>/dev/null || true
  else
    systemctl --user disable --now "$(service_label).service" 2>/dev/null || true
  fi
  rm -f "$unit"
  [[ "$(platform_kind)" == "darwin" ]] || systemctl --user daemon-reload 2>/dev/null || true
}

autostart_install() {
  local root="$1" log_dir="$2"
  local unit; unit="$(service_unit_path)"
  mkdir -p "$(dirname "$unit")" "$log_dir"
  if [[ "$(platform_kind)" != "darwin" ]]; then
    # systemd, per-user. Restart=always mirrors launchd KeepAlive; the log goes
    # to the same file the owner is told to read, not only the journal, so the
    # troubleshooting instructions are identical on both platforms.
    cat > "$unit" <<UNIT
[Unit]
Description=NoMoreHappyPath
After=network-online.target

[Service]
Type=simple
ExecStart=/bin/bash ${root}/scripts/start_project_manager.sh --no-open
Restart=always
RestartSec=2
StandardOutput=append:${log_dir}/nomorehappypath.log
StandardError=append:${log_dir}/nomorehappypath.log

[Install]
WantedBy=default.target
UNIT
    systemctl --user daemon-reload
    systemctl --user enable --now "$(service_label).service"
    return
  fi
  /usr/bin/python3 - "$root" "$unit" "$(service_label)" "$log_dir" <<'PY'
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
  launchctl bootout "gui/$(id -u)" "$unit" 2>/dev/null || true
  launchctl bootstrap "gui/$(id -u)" "$unit"
}

# --- opening a URL for the owner ----------------------------------------------
#
# Adopts the behaviour start_board_viewer.sh already had: try, fall back, and
# where neither opener exists print the URL and SUCCEED. A machine with no
# opener is not a failed start.
#
# install.sh DOES call this now, for the URL it opens at the end of a
# successful install. During Stage 0 it deliberately did not:
# routing a bare `open` here would have made a failing opener succeed, changing
# installer exit behaviour, which Stage 0 forbade. The follow-up task that was
# promised here has since made that change on purpose, so a machine with no
# opener now prints the URL and the installer still reaches `exit 0`.
owner_open_url() {
  local url="$1"
  if command -v open >/dev/null 2>&1; then
    open "$url" || true
  elif command -v xdg-open >/dev/null 2>&1; then
    xdg-open "$url" || true
  else
    printf 'Open this in your browser: %s\n' "$url"
  fi
}
