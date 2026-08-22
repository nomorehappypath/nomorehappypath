#!/usr/bin/env bash
# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
# Start harness_next as one public Projects app. Per-project workers remain on
# a private loopback port and are reached by the browser through the manager.
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
harness_root="$(cd "$script_dir/.." && pwd)"
home="${HARNESS_HOME:-$HOME/.harness-home}"
port="8740"
worker_port="8741"
migrate_root=""
open_browser=1

usage() {
  cat <<'EOF'
Usage: start_project_manager.sh [--home HARNESS_HOME] [--port PORT]
                                [--worker-port PORT] [--migrate-root ROOT]
                                [--no-open]

Starts harness_next at one stable browser address. The project worker binds a
separate private loopback port but is exposed only below /project/ on this app.
EOF
}

valid_port() {
  [[ "$1" =~ ^[0-9]+$ && "$1" -ge 1 && "$1" -le 65535 ]]
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --home) [[ $# -ge 2 ]] || { echo "--home requires a directory" >&2; exit 2; }; home="$2"; shift 2 ;;
    --port) [[ $# -ge 2 ]] && valid_port "$2" || { echo "--port requires a number from 1 to 65535" >&2; exit 2; }; port="$2"; shift 2 ;;
    --worker-port) [[ $# -ge 2 ]] && valid_port "$2" || { echo "--worker-port requires a number from 1 to 65535" >&2; exit 2; }; worker_port="$2"; shift 2 ;;
    --migrate-root) [[ $# -ge 2 ]] || { echo "--migrate-root requires a directory" >&2; exit 2; }; migrate_root="$2"; shift 2 ;;
    --no-open) open_browser=0; shift ;;
    --help|-h) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

if [[ "$port" == "$worker_port" ]]; then
  echo "--port and --worker-port must be different" >&2
  exit 2
fi
if [[ -n "$migrate_root" && ! -d "$migrate_root" ]]; then
  echo "Migration root does not exist: $migrate_root" >&2
  exit 2
fi

mkdir -p "$home"
chmod 700 "$home"
url="http://127.0.0.1:${port}/"
manager_pid=""

source_revision() {
  local source_digest git_commit
  source_digest="$(python3 -E - "$harness_root" <<'PY'
import hashlib
import pathlib
import sys

root = pathlib.Path(sys.argv[1])
digest = hashlib.sha256()
for path in sorted((root / "harness").glob("*.py")):
    digest.update(path.name.encode())
    digest.update(path.read_bytes())
print(digest.hexdigest(), end="")
PY
)"
  git_commit="$(git -C "$harness_root" rev-parse HEAD 2>/dev/null || true)"
  # Source bytes detect an edited installation before it is committed. The Git
  # identity is equally important: manager/worker runtime matching includes the
  # commit and tree, so a commit with unchanged Harness bytes must still replace
  # the already-running manager before it can launch a worker.
  printf '%s:%s' "$git_commit" "$source_digest"
}

start_manager() {
  local arguments=(--home "$home" --port "$port" --board-port "$worker_port")
  if [[ -n "$migrate_root" ]]; then
    arguments+=(--migrate-root "$migrate_root")
  fi
  python3 -E "$harness_root/harness/project_manager.py" "${arguments[@]}" &
  manager_pid=$!
}

cleanup() {
  trap - EXIT INT TERM
  if [[ -n "$manager_pid" ]]; then
    kill "$manager_pid" 2>/dev/null || true
    wait "$manager_pid" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

manager_ready() {
  local page
  page="$(curl --disable --noproxy '*' --silent --fail "$url" 2>/dev/null)" || return 1
  [[ "$page" == *"NoMoreHappyPath"* ]]
}

wait_for_manager() {
  for _ in {1..60}; do
    if manager_ready; then return 0; fi
    if ! kill -0 "$manager_pid" 2>/dev/null; then
      wait "$manager_pid"
      return 1
    fi
    sleep 0.1
  done
  return 1
}

start_manager
served_revision="$(source_revision)"
if ! wait_for_manager; then
  echo "harness_next did not become available at $url" >&2
  exit 1
fi

if [[ "$open_browser" == "1" ]]; then
  open "$url" 2>/dev/null || true
fi

echo "HARNESS NEXT ONLINE | url=$url | private_worker_port=$worker_port | pid=$manager_pid"
echo "Press Ctrl-C in this terminal to stop harness_next."

update_deferred=0
while true; do
  sleep 0.5
  if ! kill -0 "$manager_pid" 2>/dev/null; then
    wait "$manager_pid"
    exit 1
  fi
  current_revision="$(source_revision)"
  if [[ "$current_revision" == "$served_revision" ]]; then
    update_deferred=0
    continue
  fi
  # A source change never restarts the manager over an open project: the
  # bounce kills agent terminals, expires certified executions, and
  # invalidates saved evidence. Pause or Close the project to release it.
  if python3 -E "$harness_root/harness/update_gate.py" --manager-url "$url" >/dev/null 2>&1; then
    :
  else
    if [[ "$update_deferred" == "0" ]]; then
      echo "HARNESS NEXT UPDATE PENDING | source changed; restart deferred until the open project is paused or closed"
      update_deferred=1
    fi
    continue
  fi
  if [[ "$update_deferred" == "1" ]]; then
    # The project was just released; give its in-flight HTTP responses a
    # moment to drain before replacing the manager.
    sleep 2
  fi
  update_deferred=0
  echo "HARNESS NEXT REFRESH | released source changed; restarting manager and private worker"
  kill "$manager_pid" 2>/dev/null || true
  wait "$manager_pid" 2>/dev/null || true
  start_manager
  if ! wait_for_manager; then
    echo "Updated harness_next did not become available at $url" >&2
    exit 1
  fi
  served_revision="$current_revision"
done
