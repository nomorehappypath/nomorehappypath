#!/usr/bin/env bash
# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
# Functional check for the Linux Support install (docs/support/SUPPORT_LINUX_INSTALL.md):
# starts Support mode briefly on scratch ports, confirms the public receiver answers
# /healthz, confirms the admin page refuses a non-loopback Host, and confirms the
# systemd and nginx templates are present. Not a provisioning system: it does not
# create the systemd unit or edit a real proxy config.
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
harness_root="$(cd "$script_dir/.." && pwd)"
support_doc_dir="$harness_root/docs/support"

fail() { echo "FINAL CHECK: FAIL support-install — $1" >&2; exit 1; }

[[ -f "$support_doc_dir/nmhp-support.service" ]] || fail "systemd unit template is missing"
[[ -f "$support_doc_dir/nmhp-support-nginx.conf" ]] || fail "nginx template is missing"
grep -q "^ExecStart=" "$support_doc_dir/nmhp-support.service" || fail "systemd unit template has no ExecStart"
grep -q "^WantedBy=" "$support_doc_dir/nmhp-support.service" || fail "systemd unit template has no WantedBy"
grep -q "proxy_pass" "$support_doc_dir/nmhp-support-nginx.conf" || fail "nginx template has no proxy_pass"

home="$(mktemp -d)"
trap 'kill "$pid" 2>/dev/null || true; wait "$pid" 2>/dev/null || true; rm -rf "$home"' EXIT

python3 -E "$harness_root/harness/project_manager.py" --support \
  --home "$home" --host 127.0.0.1 --port 0 --support-admin-port 0 \
  > "$home/support.log" 2>&1 &
pid=$!

for _ in $(seq 1 40); do
  sleep 0.1
  [[ -s "$home/support.log" ]] && break
done
line="$(head -n1 "$home/support.log" 2>/dev/null || true)"
public_port="$(sed -n 's/.*http:\/\/127\.0\.0\.1:\([0-9]*\)\/ (admin.*/\1/p' <<<"$line")"
admin_port="$(sed -n 's/.*admin: http:\/\/127\.0\.0\.1:\([0-9]*\)\/).*/\1/p' <<<"$line")"
[[ -n "$public_port" && -n "$admin_port" ]] || fail "Support mode did not print its ports: $line"

health="$(curl --silent --fail --max-time 5 "http://127.0.0.1:$public_port/healthz")" \
  || fail "the public receiver did not answer /healthz"
[[ "$health" == *'"status": "ok"'* ]] || fail "unexpected /healthz response: $health"

refused_status="$(curl --silent --output /dev/null --write-out '%{http_code}' --max-time 5 \
  -H 'Host: attacker.example' "http://127.0.0.1:$admin_port/")"
[[ "$refused_status" == "403" ]] || fail "the admin page did not refuse a non-loopback Host (got $refused_status)"

echo "FINAL CHECK: PASS support-install"
