#!/usr/bin/env bash
# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
# One-time Linux setup for starting agents: installs tmux (agents open as tmux
# windows) and bubblewrap and, where the kernel blocks unprivileged user
# namespaces (Ubuntu 24.04 does by default), loads Ubuntu's own AppArmor profile
# for bubblewrap. The system-wide restriction stays on. Run it once, in Linux:
#     sudo bash scripts/linux_enable_sandbox.sh
set -euo pipefail

if [[ "$(uname -s)" != "Linux" ]]; then echo "This is for Linux only."; exit 1; fi
if [[ $EUID -ne 0 ]]; then echo "Run it with sudo:  sudo bash $0"; exit 1; fi
owner="${SUDO_USER:-}"

works() {  # the same bubblewrap call the app makes, as the person who runs the app
  local run=(bwrap --ro-bind / / --dev-bind /dev /dev --proc /proc -- /bin/true)
  if [[ -n "$owner" ]]; then sudo -u "$owner" "${run[@]}" >/dev/null 2>&1; else "${run[@]}" >/dev/null 2>&1; fi
}

install_packages() {
  if   command -v apt-get >/dev/null 2>&1; then apt-get install -y "$@"
  elif command -v dnf     >/dev/null 2>&1; then dnf install -y "$@"
  elif command -v pacman  >/dev/null 2>&1; then pacman -S --noconfirm "$@"
  elif command -v zypper  >/dev/null 2>&1; then zypper --non-interactive install "$@"
  else echo "Install the '$*' package(s) with your package manager, then run this again."; exit 1; fi
}
wanted=()
command -v tmux >/dev/null 2>&1 || wanted+=(tmux)
command -v bwrap >/dev/null 2>&1 || wanted+=(bubblewrap)
if [[ ${#wanted[@]} -gt 0 ]]; then
  echo "Installing ${wanted[*]}…"
  install_packages "${wanted[@]}"
fi

bwrap_path="$(readlink -f "$(command -v bwrap)")"
if works; then echo "The agent sandbox already works. Nothing to change."; exit 0; fi

if [[ -d /etc/apparmor.d ]] && command -v apparmor_parser >/dev/null 2>&1; then
  # Ubuntu's own profile for bubblewrap: it allows user namespaces for bwrap only
  # and its children keep no capabilities. The system-wide restriction stays on.
  stock=/usr/share/apparmor/extra-profiles/bwrap-userns-restrict
  if [[ ! -f "$stock" ]] && command -v apt-get >/dev/null 2>&1; then
    echo "Installing apparmor-profiles (Ubuntu's profile for bubblewrap)…"
    apt-get install -y apparmor-profiles
  fi
  if [[ -f "$stock" ]]; then
    echo "Allowing bubblewrap through AppArmor (/etc/apparmor.d/bwrap-userns-restrict)…"
    install -m 644 "$stock" /etc/apparmor.d/bwrap-userns-restrict
    apparmor_parser -r /etc/apparmor.d/bwrap-userns-restrict
  else
    echo "This system has no stock AppArmor profile for bubblewrap, and this script will not write a broader one."
    echo "Allow user namespaces for $bwrap_path in your AppArmor policy, then run this again."
    exit 1
  fi
fi

if works; then
  echo "Done: the agent sandbox works. Start the agents again from the app."
else
  echo "bubblewrap still cannot start. Run this as the person who runs the app to see why:"
  echo "    bwrap --ro-bind / / --dev-bind /dev /dev --proc /proc -- /bin/true"
  exit 1
fi
