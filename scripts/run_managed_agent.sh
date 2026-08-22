#!/usr/bin/env bash
# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
# Internal runner launched in a visible Terminal window by the board control panel.
set -euo pipefail

target_root=""
data_root=""
workspace_root=""
python_bin=""
session_id=""
kind=""
board_endpoint=""
board_bootstrap=""
close_terminal_on_exit="0"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --root)
      [[ $# -ge 2 ]] || { echo "--root requires a directory" >&2; exit 2; }
      target_root="$2"; shift 2 ;;
    --data-root)
      [[ $# -ge 2 ]] || { echo "--data-root requires a directory" >&2; exit 2; }
      data_root="$2"; shift 2 ;;
    --workspace-root)
      [[ $# -ge 2 ]] || { echo "--workspace-root requires a directory" >&2; exit 2; }
      workspace_root="$2"; shift 2 ;;
    --python)
      [[ $# -ge 2 ]] || { echo "--python requires an executable" >&2; exit 2; }
      python_bin="$2"; shift 2 ;;
    --session-id)
      [[ $# -ge 2 ]] || { echo "--session-id requires an ID" >&2; exit 2; }
      session_id="$2"; shift 2 ;;
    --kind)
      [[ $# -ge 2 ]] || { echo "--kind requires a role" >&2; exit 2; }
      kind="$2"; shift 2 ;;
    --board-endpoint)
      [[ $# -ge 2 ]] || { echo "--board-endpoint requires a URL" >&2; exit 2; }
      board_endpoint="$2"; shift 2 ;;
    --board-bootstrap)
      [[ $# -ge 2 ]] || { echo "--board-bootstrap requires a socket" >&2; exit 2; }
      board_bootstrap="$2"; shift 2 ;;
    --close-terminal-on-exit)
      close_terminal_on_exit="1"; shift ;;
    *) echo "Unknown option: $1" >&2; exit 2 ;;
  esac
done

[[ -n "$target_root" && -n "$session_id" && -n "$kind" ]] || { echo "Missing managed-session arguments" >&2; exit 2; }
if [[ -n "$board_endpoint" && -z "$board_bootstrap" ]]; then
  echo "--board-endpoint bootstrap is no longer accepted; use --board-bootstrap" >&2
  exit 2
fi
if [[ ( -n "$data_root" && -z "$workspace_root" ) || ( -z "$data_root" && -n "$workspace_root" ) ]]; then
  echo "--data-root and --workspace-root are required together" >&2
  exit 2
fi
[[ -d "$target_root" ]] || { echo "Project root does not exist: $target_root" >&2; exit 2; }
# Child programs must not reinterpret ambient repository or shell-startup
# contracts. Harness-owned Python uses -E; provider PYTHON* configuration is
# retained because it belongs to the explicitly selected provider executable.
for ambient_name in ${!GIT_@}; do
  unset "$ambient_name"
done
unset BASH_ENV ENV CDPATH ZDOTDIR
if [[ -z "$python_bin" ]]; then
  python_bin="$(command -v python3 || true)"
fi
[[ -n "$python_bin" && -x "$python_bin" ]] || { echo "Python interpreter does not exist or is not executable: $python_bin" >&2; exit 2; }
python_bin="$(cd "$(dirname "$python_bin")" && pwd)/$(basename "$python_bin")"

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
harness_root="$(cd "$script_dir/.." && pwd)"
target_root="$(cd "$target_root" && pwd)"
if [[ -z "$data_root" ]]; then
  data_root="$target_root/.harness"
  workspace_root="$(dirname "$target_root")/.harness-task-workspaces"
fi
context_args=(--root "$target_root" --data-root "$data_root" --workspace-root "$workspace_root")
printf -v board_command_prefix '%q -E %q --root %q --data-root %q --workspace-root %q' "$python_bin" "$harness_root/harness/board.py" "$target_root" "$data_root" "$workspace_root"
if [[ -n "$board_bootstrap" ]]; then
  printf -v board_command_prefix '%q -E %q --root %q' "$python_bin" "$harness_root/harness/board.py" "$target_root"
fi
# Project registration is the only execution-root authority. The environment
# override remains available solely for isolated tests and controlled launches.
execution_root="${HARNESS_EXECUTION_ROOT:-$target_root}"
[[ -d "$execution_root" ]] || { echo "Execution root does not exist: $execution_root" >&2; exit 2; }
execution_root="$(cd "$execution_root" && pwd)"
cd "$execution_root"

"$python_bin" -E "$harness_root/harness/control.py" "${context_args[@]}" attach --id "$session_id" --pid "$$" >/dev/null

# Projects mode obtains the raw session credential once from an OS-authenticated
# Unix-socket peer after the control registry has attached this shell PID. The
# token travels only in response bytes and then the provider environment.
if [[ -n "$board_bootstrap" ]]; then
  board_environment_json="$("$python_bin" -E - "$board_bootstrap" "$session_id" <<'PY'
import json
import socket
import sys

bootstrap, session_id = sys.argv[1:]
try:
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    connection.settimeout(5)
    connection.connect(bootstrap)
    connection.sendall(json.dumps({"session_id": session_id, "protocol": "1"}, separators=(",", ":")).encode("utf-8") + b"\n")
    value = json.loads(connection.makefile("rb").readline())
    connection.close()
    if "error" in value:
        raise ValueError(value["error"])
    environment = value["environment"]
    required = {"HARNESS_BOARD_TOKEN", "HARNESS_BOARD_ENDPOINT", "HARNESS_BOARD_PROTOCOL"}
    if set(environment) != required or not all(isinstance(environment[key], str) and environment[key] for key in required):
        raise ValueError("worker returned an invalid session environment")
    print(json.dumps(environment, separators=(",", ":")))
except Exception as error:
    print(f"authenticated board bootstrap failed: {error}", file=sys.stderr)
    raise SystemExit(2)
PY
)" || exit 2
  export HARNESS_BOARD_TOKEN="$("$python_bin" -E -c 'import json,sys; print(json.load(sys.stdin)["HARNESS_BOARD_TOKEN"])' <<<"$board_environment_json")"
  export HARNESS_BOARD_ENDPOINT="$("$python_bin" -E -c 'import json,sys; print(json.load(sys.stdin)["HARNESS_BOARD_ENDPOINT"])' <<<"$board_environment_json")"
  export HARNESS_BOARD_PROTOCOL="$("$python_bin" -E -c 'import json,sys; print(json.load(sys.stdin)["HARNESS_BOARD_PROTOCOL"])' <<<"$board_environment_json")"
  unset board_environment_json
fi

register_agent() {
  local board_role="$1" board_task="$2" board_name="$3" board_vendor="$4" registered
  registered="$("$python_bin" -E "$harness_root/harness/board.py" "${context_args[@]}" register --role "$board_role" --task "$board_task" --name "$board_name" --vendor "$board_vendor" --session-id "$session_id")"
  "$python_bin" -E -c 'import json,sys; print(json.loads(sys.stdin.read())["id"])' <<<"$registered"
}

launch_visible_cli() {
  if [[ -t 0 && -t 1 ]]; then
    supervisor_args=("${context_args[@]}" --session-id "$session_id" --agent-id "$agent_id")
    if [[ "$close_terminal_on_exit" == "1" ]]; then
      supervisor_args+=(--close-terminal-on-exit)
    fi
    exec "$python_bin" -E "$harness_root/harness/interactive_supervisor.py" "${supervisor_args[@]}" -- "$@"
  fi
  exec "$@"
}

launch_settings_json="$("$python_bin" -E "$harness_root/harness/control.py" "${context_args[@]}" resolve --kind "$kind" --session-id "$session_id")"
provider="$("$python_bin" -E -c 'import json,sys; print(json.load(sys.stdin)["provider"])' <<<"$launch_settings_json")"
model="$("$python_bin" -E -c 'import json,sys; print(json.load(sys.stdin)["model"])' <<<"$launch_settings_json")"
effort="$("$python_bin" -E -c 'import json,sys; print(json.load(sys.stdin)["effort"])' <<<"$launch_settings_json")"
vendor="OpenAI"
if [[ "$provider" == "claude" ]]; then vendor="Anthropic"; fi

launch_agent_cli() {
  local prompt="$1"
  if [[ "$provider" == "codex" ]]; then
    # Approval and sandbox scope are supplied PER LAUNCH, bound to this
    # project's execution root. Nothing dangerous is ever written to the
    # owner's global ~/.codex/config.toml, so one project's access can never
    # leak into another project or into the owner's own codex sessions.
    launch_visible_cli "${HARNESS_CODEX_BIN:-codex}" --cd "$execution_root" --model "$model" -c "model_reasoning_effort=${effort}" -c "approval_policy=never" -c "sandbox_mode=danger-full-access" "$prompt"
  else
    launch_visible_cli "${HARNESS_CLAUDE_BIN:-claude}" --model "$model" --effort "$effort" "$prompt"
  fi
}

case "$kind" in
  codex_delivery)
    agent_id="$(register_agent engineering AWAITING_OWNER_DIRECTION 'Delivery Agent' "$vendor")"
    directive="$(<"$harness_root/directives/AGENT.md")"
    prompt="${directive}

MODE: Delivery Agent.
Start from ${execution_root}, which supplies the established CLI permissions.
    The target project is ${target_root}; perform all task work and board actions there.
    You are already registered by the visible supervisor as agent ${agent_id}; use that ID for every board command and do not register a second agent.
For every board command, start with: ${board_command_prefix}
No owner direction has been supplied yet. Register your role on the visible
board as standing by, then wait for the owner to describe what they want. Do
not invent an objective, task, Completion Contract, scenario ledger, chunk,
QA request, or bootstrap work before that direction arrives. When it does,
your Product Manager hat converts it into the internal objective and plan.
Before implementation, ask clarifying questions as needed. After the owner
agrees and says go ahead, write a structured final requirements confirmation
and record it with the board command: confirm-requirements --agent ${agent_id}.
The original owner direction remains unchanged; this confirmation is an
additional archived section immediately after it. Do not define a delivery
plan or begin implementation until that confirmation is recorded.
If this agent ID is already attached to a recovered task by the board, poll
immediately and resume that preserved task and next action; do not return to
standby or ask the owner to repeat the direction."
    launch_agent_cli "$prompt"
    ;;
  claude_reviewer)
    agent_id="$(register_agent qa REVIEW_QUEUE 'Independent Reviewer' "$vendor")"
    directive="$(<"$harness_root/directives/AGENT.md")"
    prompt="${directive}

MODE: Independent Reviewer.
There is no implementation task for you. Start from ${execution_root}, which
supplies the established CLI permissions. The target project and visible board
    are ${target_root}; you are already registered by the visible supervisor as agent ${agent_id}; use that ID for every board command and do not register a second agent. Continuously poll that board, claim eligible review
requests, and execute independent QA there.
For every board command, start with: ${board_command_prefix}"
    launch_agent_cli "$prompt"
    ;;
  claude_cto)
    agent_id="$(register_agent cto GLOBAL_MONITOR CTO "$vendor")"
    directive="$(<"$harness_root/directives/CTO.md")"
    prompt="${directive}

Start from ${execution_root}, which supplies the established CLI permissions.
    You are the global CTO for the target project ${target_root}. You are already registered by the visible supervisor as agent ${agent_id}; use that ID for every board command and do not register a second agent. Start visible
monitoring of that project's board now.
For every board command, start with: ${board_command_prefix}"
    launch_agent_cli "$prompt"
    ;;
  *) echo "Unknown managed session kind: $kind" >&2; exit 2 ;;
esac
