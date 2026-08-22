#!/usr/bin/env bash
# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
# Start the visible Harness board for the current project. No Python command,
# profile, or manual browser URL is required from the product owner.
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
harness_root="$(cd "$script_dir/.." && pwd)"
target_root="$PWD"
data_root=""
workspace_root=""
port="8742"
open_browser=1
root_was_supplied=0

usage() {
  cat <<'EOF'
Usage: start_board_viewer.sh [--root PROJECT_ROOT] [--data-root DATA_ROOT]
                             [--workspace-root WORKSPACE_ROOT] [--port PORT] [--no-open]

Starts the live Harness board for PROJECT_ROOT (default: current directory).
It creates the board if necessary and opens the local display automatically.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --root)
      [[ $# -ge 2 ]] || { echo "--root requires a directory" >&2; exit 2; }
      target_root="$2"; root_was_supplied=1; shift 2 ;;
    --data-root)
      [[ $# -ge 2 ]] || { echo "--data-root requires a directory" >&2; exit 2; }
      data_root="$2"; shift 2 ;;
    --workspace-root)
      [[ $# -ge 2 ]] || { echo "--workspace-root requires a directory" >&2; exit 2; }
      workspace_root="$2"; shift 2 ;;
    --port)
      [[ $# -ge 2 && "$2" =~ ^[0-9]+$ && "$2" -ge 1 && "$2" -le 65535 ]] || {
        echo "--port requires a number from 1 to 65535" >&2; exit 2;
      }
      port="$2"; shift 2 ;;
    --no-open) open_browser=0; shift ;;
    --help|-h) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

if [[ ( -n "$data_root" && -z "$workspace_root" ) || ( -z "$data_root" && -n "$workspace_root" ) ]]; then
  echo "--data-root and --workspace-root are required together" >&2
  exit 2
fi

if [[ ! -d "$target_root" ]]; then
  echo "Project root does not exist: $target_root" >&2
  exit 2
fi

if [[ "$root_was_supplied" == "0" && "$target_root" == "$script_dir" ]]; then
  # A common natural invocation is `cd .../scripts && bash start_board_viewer.sh`.
  # In that case the project is the harness root, never the implementation folder.
  target_root="$harness_root"
fi
target_root="$(cd "$target_root" && pwd)"
if [[ -z "$data_root" ]]; then
  data_root="$target_root/.harness"
  workspace_root="$(dirname "$target_root")/.harness-task-workspaces"
fi
context_args=(--root "$target_root" --data-root "$data_root" --workspace-root "$workspace_root")
url="http://127.0.0.1:${port}/"

# Bootstrap the visible files before the browser opens.
python3 -E "$harness_root/harness/board.py" "${context_args[@]}" view >/dev/null
viewer_pid=""

viewer_revision() {
  # Supervise executable sources directly instead of consulting Git. Besides
  # supporting installed copies, this keeps inherited GIT_* routing state out
  # of the long-running viewer boundary.
  python3 -E - "$harness_root" <<'PY'
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
}

start_viewer() {
  python3 -E "$harness_root/harness/board_viewer.py" "${context_args[@]}" --port "$port" &
  viewer_pid=$!
}

start_viewer
served_revision="$(viewer_revision)"
watchdog_interval=15  # Keep synchronized with board.WATCHDOG_INTERVAL_SECONDS.
agent_stale_after=240 # Keep synchronized with board.AGENT_STALE_SECONDS.
python3 -E "$harness_root/harness/watchdog.py" "${context_args[@]}" --interval "$watchdog_interval" --stale-after "$agent_stale_after" &
watchdog_pid=$!

cleanup() {
  trap - EXIT INT TERM
  kill "$viewer_pid" 2>/dev/null || true
  kill "$watchdog_pid" 2>/dev/null || true
  wait "$viewer_pid" 2>/dev/null || true
  wait "$watchdog_pid" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

viewer_ready() {
  # Do not pipe curl into grep under `set -o pipefail`: grep may exit after a
  # match while curl is still writing, producing a false SIGPIPE failure.
  local page
  # Ignore ~/.curlrc and proxy routing for this fixed loopback readiness probe.
  page="$(curl --disable --noproxy '*' --silent --fail "$url" 2>/dev/null)" || return 1
  [[ "$page" == *"NoMoreHappyPath Mission Control"* ]]
}

wait_for_viewer() {
  for _ in {1..50}; do
    if viewer_ready; then
      return 0
    fi
    if ! kill -0 "$viewer_pid" 2>/dev/null; then
      wait "$viewer_pid"
      return 1
    fi
    sleep 0.1
  done
  return 1
}

if ! wait_for_viewer; then
  echo "Board viewer did not become available at $url" >&2
  exit 1
fi

if [[ "$open_browser" == "1" ]]; then
  if command -v open >/dev/null 2>&1; then
    open "$url" || true
  elif command -v xdg-open >/dev/null 2>&1; then
    xdg-open "$url" || true
  fi
fi

echo "BOARD VIEWER ONLINE | project=$target_root | url=$url | pid=$viewer_pid"
echo "Press Ctrl-C in this terminal to stop the viewer."

# A long-running Python process keeps imported code in memory. Supervise the
# exact harness revision and replace only the viewer child when that revision
# changes. The page's version check then reloads the browser automatically.
while true; do
  sleep 0.5
  if ! kill -0 "$viewer_pid" 2>/dev/null; then
    wait "$viewer_pid"
    exit 1
  fi
  current_revision="$(viewer_revision)"
  if [[ "$current_revision" == "$served_revision" ]]; then
    continue
  fi
  echo "BOARD VIEWER REFRESH | revision changed; loading the released Mission Control code"
  kill "$viewer_pid" 2>/dev/null || true
  wait "$viewer_pid" 2>/dev/null || true
  start_viewer
  if ! wait_for_viewer; then
    echo "Updated board viewer did not become available at $url" >&2
    exit 1
  fi
  served_revision="$current_revision"
done
