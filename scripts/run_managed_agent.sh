#!/usr/bin/env bash
# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
# Internal runner launched in a visible Terminal window by the board control panel.
set -euo pipefail

target_root=""
data_root=""
workspace_root=""
manager_home=""
python_bin=""
session_id=""
kind=""
board_endpoint=""
board_bootstrap=""
close_terminal_on_exit="0"
launch_mode="fresh"
cli_session_id=""
transcript_path=""
launch_reason=""
codex_marker=""
predecessor_session=""

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
    --manager-home)
      # Where the project registry lives. The write grant is validated against
      # the storage the registry ASSIGNED this project, and without the home it
      # would fall back to the default location and refuse an adopted project's
      # legitimate storage - which is exactly how a reviewer failed this.
      [[ $# -ge 2 ]] || { echo "--manager-home requires a directory" >&2; exit 2; }
      manager_home="$2"; shift 2 ;;
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
    supervisor_args=("${context_args[@]}" --session-id "$session_id" --agent-id "$agent_id" --provider "$provider" --execution-root "$execution_root")
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

# The CLI keeps its own memory of a conversation, and the harness keeps the
# vendor's session id so a relaunch RESUMES that memory instead of starting a
# stranger. On 2026-09-22 an afternoon of CTO design work was lost this way.
# plan-cli-launch decides FRESH or RESUME (resume only when the vendor store
# still holds the session; otherwise fresh, and the reason goes into the
# transcript so nobody has to guess). The recovery message replaces the full
# directive on a resume: the agent already has the directive in its memory.
plan_cli_launch() {
  local plan_json
  plan_json="$("$python_bin" -E "$harness_root/harness/control.py" "${context_args[@]}" plan-cli-launch --session-id "$session_id" --provider "$provider")" || {
    echo "REFUSED: could not plan the CLI launch for $session_id" >&2; exit 2; }
  launch_mode="$("$python_bin" -E -c 'import json,sys; print(json.load(sys.stdin)["mode"])' <<<"$plan_json")"
  cli_session_id="$("$python_bin" -E -c 'import json,sys; print(json.load(sys.stdin)["cli_session_id"])' <<<"$plan_json")"
  transcript_path="$("$python_bin" -E -c 'import json,sys; print(json.load(sys.stdin)["transcript"])' <<<"$plan_json")"
  launch_reason="$("$python_bin" -E -c 'import json,sys; print(json.load(sys.stdin)["reason"])' <<<"$plan_json")"
  codex_marker="$("$python_bin" -E -c 'import json,sys; print(json.load(sys.stdin).get("codex_marker",""))' <<<"$plan_json")"
  predecessor_session="$("$python_bin" -E -c 'import json,sys; print(json.load(sys.stdin).get("predecessor",""))' <<<"$plan_json")"
  mkdir -p "$(dirname "$transcript_path")"
  printf '%s -- launch: mode=%s provider=%s cli_session_id=%s (%s)\n' "$(date -u +%Y-%m-%dT%H:%M:%S+00:00)" "$launch_mode" "$provider" "${cli_session_id:-none}" "$launch_reason" >> "$transcript_path"
  if [[ "$launch_mode" == "resume" ]]; then
    "$python_bin" -E "$harness_root/harness/control.py" "${context_args[@]}" note-cli-launch --session-id "$session_id" --resumed >/dev/null
  else
    "$python_bin" -E "$harness_root/harness/control.py" "${context_args[@]}" note-cli-launch --session-id "$session_id" >/dev/null
  fi
}

conversation_command_for() {
  printf '%q -E %q --root %q --data-root %q --workspace-root %q conversation --session-id %q' \
    "$python_bin" "$harness_root/harness/control.py" "$target_root" "$data_root" "$workspace_root" "$1"
}

earlier_conversation_note() {
  "$python_bin" -E - "$harness_root" "$predecessor_session" "$(conversation_command_for "$predecessor_session")" <<'PY'
import sys
sys.path.insert(0, sys.argv[1])
from harness.conversation import earlier_conversation_note
print(earlier_conversation_note(sys.argv[2], sys.argv[3]))
PY
}

recovery_prompt() {
  local conversation_command
  conversation_command="$(conversation_command_for "$session_id")"
  "$python_bin" -E - "$harness_root" "$cli_session_id" "$transcript_path" "$agent_id" "$board_command_prefix" "$conversation_command" <<'PY'
import sys
sys.path.insert(0, sys.argv[1])
from harness.conversation import recovery_message
print(recovery_message(sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5], sys.argv[6]))
PY
}

launch_agent_cli() {
  local prompt="$1"
  plan_cli_launch
  if [[ "$launch_mode" == "resume" ]]; then
    prompt="$(recovery_prompt)"
  else
    if [[ -n "$predecessor_session" ]]; then
      # A previous session of this role existed and could not be resumed:
      # point the new agent at its readable conversation before it starts.
      prompt="${prompt}

$(earlier_conversation_note)"
    fi
    if [[ "$provider" == "codex" && -n "$codex_marker" ]]; then
      # Codex records this prompt as the first message of its rollout; the
      # marker is how the supervisor finds THIS launch's rollout and no other.
      prompt="${prompt}

${codex_marker}"
    fi
  fi
  if [[ "$provider" == "codex" ]]; then
    # Approval and sandbox scope are supplied PER LAUNCH, bound to this
    # project's execution root. Nothing dangerous is ever written to the
    # owner's global ~/.codex/config.toml, so one project's access can never
    # leak into another project or into the owner's own codex sessions.
    #
    # WRITES ARE CONFINED. This used to pass the sandbox mode that disables
    # Codex's sandbox entirely (the literal is not written here: a test scans
    # this file for it, as it should). A reviewer proved by
    # execution that a managed agent could write to a sibling directory outside
    # the project, while the app's own Help text claimed writes were held to the
    # project folder. The claim was false, and this is what makes it true.
    #
    # workspace-write alone is not enough: the harness's own task workspaces sit
    # in a SIBLING directory of the project, and its data root may too, so a
    # bare workspace-write blocks legitimate work. Both are named explicitly
    # instead, which is the whole point - the grant is the exact set of paths
    # this agent needs, and nothing else. The owner's home, ssh keys and other
    # projects are outside it.
    #
    # READS ARE NOT CONFINED. Codex has no read-scoping mode, so an agent can
    # still read anything the owner can. The Help text says so plainly; do not
    # let this comment or that text drift into implying otherwise.
    # The grant is only as narrow as the paths handed to it. For an ADOPTED
    # project these roots are owner-supplied, so one can name a broad ancestor -
    # or a symlink resolving to one - and passing it through would grant every
    # sibling of the project. A reviewer proved exactly that. The roots are
    # validated (symlinks resolved FIRST) and the launch REFUSES rather than
    # narrowing silently, because a silently narrowed grant is a surprise the
    # owner never sees.
    # The harness root arrives as argv, not PYTHONPATH: this interpreter runs
    # with -E, which IGNORES the environment on purpose, so an exported
    # PYTHONPATH is invisible here. Setting one made every launch refuse - the
    # import failed, the helper exited non-zero, and 13 suites went red.
    if ! writable_roots_json="$("$python_bin" -E -c '
import json, sys
sys.path.insert(0, sys.argv[1])
from harness.agent_grant import agent_writable_roots, GrantTooBroad
try:
    print(json.dumps(agent_writable_roots(sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5], sys.argv[6] or None)))
except GrantTooBroad as error:
    print(error, file=sys.stderr)
    raise SystemExit(3)
' "$harness_root" "$execution_root" "$data_root" "$workspace_root" "$target_root" "$manager_home")"; then
      echo "REFUSED: this project's storage layout would grant the agent more than it needs." >&2
      echo "         Fix the project's data or workspace root, then relaunch." >&2
      exit 3
    fi
    # NETWORK STAYS ON INSIDE THE SANDBOX. Codex's workspace-write sandbox
    # disables network for the agent's shell commands by default, and the
    # board client is a shell command that talks HTTP to the private worker
    # on 127.0.0.1. With the default, every board poll of every Delivery
    # agent was refused at the socket ("authenticated board worker is
    # unavailable or temporarily busy") from the day write confinement went
    # live. Proven with `codex exec` under these exact settings: curl to the
    # worker → exit 7 without this line, 403 with it. Codex 0.156.0 offers
    # no loopback-only option; the Help text has never claimed network
    # confinement, only write confinement, and that is unchanged.
    if [[ "$launch_mode" == "resume" ]]; then
      # `codex resume` takes the same -c settings; it has no --cd, and the
      # runner already runs from the execution root.
      launch_visible_cli "${HARNESS_CODEX_BIN:-codex}" resume --model "$model" -c "model_reasoning_effort=${effort}" -c "approval_policy=never" -c "sandbox_mode=workspace-write" -c "sandbox_workspace_write.writable_roots=${writable_roots_json}" -c "sandbox_workspace_write.network_access=true" "$cli_session_id" "$prompt"
    else
      launch_visible_cli "${HARNESS_CODEX_BIN:-codex}" --cd "$execution_root" --model "$model" -c "model_reasoning_effort=${effort}" -c "approval_policy=never" -c "sandbox_mode=workspace-write" -c "sandbox_workspace_write.writable_roots=${writable_roots_json}" -c "sandbox_workspace_write.network_access=true" "$prompt"
    fi
  else
    if [[ "$launch_mode" == "resume" ]]; then
      launch_visible_cli "${HARNESS_CLAUDE_BIN:-claude}" --model "$model" --effort "$effort" --resume "$cli_session_id" "$prompt"
    else
      launch_visible_cli "${HARNESS_CLAUDE_BIN:-claude}" --model "$model" --effort "$effort" --session-id "$cli_session_id" "$prompt"
    fi
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
