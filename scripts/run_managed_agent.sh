#!/usr/bin/env bash
# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
# Internal runner launched in a visible Terminal window by the board control panel.
set -euo pipefail

# GNU screen (the macOS terminal host) resets HOME to the account's real home; the app passes the
# HOME it was started with as HARNESS_AGENT_HOME. Restore it before anything reads HOME.
if [[ -n "${HARNESS_AGENT_HOME:-}" ]]; then
  export HOME="$HARNESS_AGENT_HOME"
  unset HARNESS_AGENT_HOME
fi

# A runner that ends before it starts the CLI used to vanish: the terminal window closed with its message
# inside it, and the control panel only knew "exited" (2026-10-10, an idle reviewer's first restart launch).
# Every step names itself in runner_step; an EXIT trap reports a failed attempt to the session record with a
# bounded, redacted copy of the runner's last stderr; the CLI exec replaces the shell, so the trap never fires
# after a real launch. The report is best effort and bounded; the control registry also notices an attempt
# that died without reporting (SIGKILL, shutdown).
runner_step="checking the launch arguments"
runner_signal=""
launch_err_file=""
if launch_err_file="$(mktemp "${TMPDIR:-/tmp}/harness-launch.XXXXXX" 2>/dev/null)"; then
  exec 3>&2
  exec 2> >(tee -a "$launch_err_file" >&3)
else
  launch_err_file=""
fi
launch_retries="${HARNESS_LAUNCH_RETRIES:-4}"
[[ "$launch_retries" =~ ^[1-8]$ ]] || launch_retries=4
launch_retry_seconds="${HARNESS_LAUNCH_RETRY_SECONDS:-2}"
[[ "$launch_retry_seconds" =~ ^([0-9]|10)$ ]] || launch_retry_seconds=2

restore_stderr() {
  # The CLI must get the terminal's own stderr, not the capture pipe.
  if [[ -n "$launch_err_file" ]]; then
    exec 2>&3 3>&-
    rm -f "$launch_err_file" 2>/dev/null || true
    launch_err_file=""
  fi
}

report_launch_failure() {
  local code="$1" diagnostic="" signal_args=()
  [[ -n "${session_id:-}" && -n "${python_bin:-}" && -n "${target_root:-}" && -x "${python_bin:-}" ]] || return 0
  if [[ -n "$launch_err_file" && -r "$launch_err_file" ]]; then
    diagnostic="$(tail -c 600 "$launch_err_file" 2>/dev/null | tr -d '\000' || true)"
  fi
  [[ -n "$runner_signal" ]] && signal_args=(--signal="$runner_signal")
  local context=(--root "$target_root")
  if [[ -n "${data_root:-}" && -n "${workspace_root:-}" ]]; then context+=(--data-root "$data_root" --workspace-root "$workspace_root"); fi
  # If the registry cannot be reached in time nothing more is attempted here (a second write to the same
  # stalled storage could hang the closing terminal); the registry's own reconciliation records an unknown cause.
  "$python_bin" -E "${harness_root:-$(dirname "${BASH_SOURCE[0]}")/..}/harness/control.py" "${context[@]}" launch-failed \
      --session-id "$session_id" --pid "$$" --code "$code" --step="$runner_step" --diagnostic="$diagnostic" \
      ${signal_args[@]+"${signal_args[@]}"} >/dev/null 2>&1 || true
}

on_runner_exit() {
  local code="$1"
  trap - EXIT HUP INT TERM
  if [[ "$code" != "0" ]]; then
    report_launch_failure "$code"
    if [[ -n "$runner_signal" ]]; then
      echo "The terminal was closed by ${runner_signal} while ${runner_step}, before the CLI started." >&2
    else
      echo "The terminal stopped while ${runner_step}, before the CLI started (exit code ${code})." >&2
    fi
    # 2 and 3 are the runner's own refusals and keep their meaning; anything else unexpected is 75.
    if [[ "$code" != "2" && "$code" != "3" && -z "$runner_signal" ]]; then
      exit 75
    fi
  fi
}

on_runner_signal() {
  runner_signal="$1"
  exit "$((128 + $2))"
}

trap 'on_runner_exit $?' EXIT
trap 'on_runner_signal SIGHUP 1' HUP
trap 'on_runner_signal SIGINT 2' INT
trap 'on_runner_signal SIGTERM 15' TERM

target_root=""
data_root=""
workspace_root=""
manager_home=""
python_bin=""
session_id=""
kind=""
board_endpoint=""
board_bootstrap=""
claude_auth_bootstrap=""
close_terminal_on_exit="0"
launch_mode="fresh"
cli_session_id=""
transcript_path=""
launch_reason=""
codex_marker=""
predecessor_session=""
stage3_supervisor_args=()
claude_system_layer=""

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
    --claude-auth-bootstrap)
      [[ $# -ge 2 ]] || { echo "--claude-auth-bootstrap requires a socket" >&2; exit 2; }
      claude_auth_bootstrap="$2"; shift 2 ;;
    --close-terminal-on-exit)
      close_terminal_on_exit="1"; shift ;;
    *) echo "Unknown option: $1" >&2; exit 2 ;;
  esac
done

[[ -n "$target_root" && -n "$session_id" && -n "$kind" ]] || { echo "Missing managed-session arguments" >&2; exit 2; }
# Every process this session starts - the CLI, its tools, and anything they
# start in turn - inherits this non-secret marker, even after it is reparented.
# When the session ends, the harness stops whatever still carries it
# (2026-09-26: CLI probes started by an agent outlived it for hours).
export HARNESS_MANAGED_SESSION="$session_id"
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
unset BASH_ENV ENV CDPATH ZDOTDIR CLAUDE_CODE_OAUTH_TOKEN ANTHROPIC_API_KEY ANTHROPIC_AUTH_TOKEN ANTHROPIC_PROFILE CLAUDE_CODE_USE_BEDROCK CLAUDE_CODE_USE_VERTEX CLAUDE_CODE_USE_FOUNDRY HARNESS_REVIEWER_CLAUDE_TOKEN
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

runner_step="attaching to the control panel"
"$python_bin" -E "$harness_root/harness/control.py" "${context_args[@]}" attach --id "$session_id" --pid "$$" >/dev/null
runner_step="signing in to the board"

# Projects mode obtains the raw session credential once from an OS-authenticated
# Unix-socket peer after the control registry has attached this shell PID. The
# token travels only in response bytes and then the provider environment.
if [[ -n "$board_bootstrap" ]]; then
  board_environment_json="$("$python_bin" -E - "$board_bootstrap" "$session_id" "$launch_retries" "$launch_retry_seconds" <<'PY'
import json
import socket
import sys
import time

bootstrap, session_id, attempts, pause = sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4])
last = None
for attempt in range(1, attempts + 1):
    try:
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.settimeout(5)
        connection.connect(bootstrap)
        connection.sendall(json.dumps({"session_id": session_id, "protocol": "1"}, separators=(",", ":")).encode("utf-8") + b"\n")
        line = connection.makefile("rb").readline()
        connection.close()
        if not line:
            raise ConnectionResetError("the board closed the connection without answering")
    except (OSError, ConnectionError) as error:
        # The board is starting, busy or restarting: the only failures worth waiting for.
        last = error
        if attempt < attempts:
            time.sleep(pause)
            continue
        print(f"authenticated board bootstrap failed after {attempt} attempts: {error}", file=sys.stderr)
        raise SystemExit(2)
    try:
        value = json.loads(line)
        if "error" in value:
            raise ValueError(value["error"])
        environment = value["environment"]
        required = {"HARNESS_BOARD_TOKEN", "HARNESS_BOARD_ENDPOINT", "HARNESS_BOARD_PROTOCOL"}
        if set(environment) != required or not all(isinstance(environment[key], str) and environment[key] for key in required):
            raise ValueError("worker returned an invalid session environment")
        print(json.dumps(environment, separators=(",", ":")))
        raise SystemExit(0)
    except SystemExit:
        raise
    except Exception as error:
        # A definite answer (a refusal, an invalid reply) is never retried.
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
  local board_role="$1" board_task="$2" board_name="$3" board_vendor="$4" registered attempt=1 register_err register_status
  register_err="$(mktemp "${TMPDIR:-/tmp}/harness-register.XXXXXX")"
  while true; do
    if registered="$("$python_bin" -E "$harness_root/harness/board.py" "${context_args[@]}" register --role "$board_role" --task "$board_task" --name "$board_name" --vendor "$board_vendor" --session-id "$session_id" 2>"$register_err")"; then
      break
    else
      register_status=$?
    fi
    # Only a worker that is busy or not reachable yet is worth waiting for; any other answer is final.
    if (( attempt < launch_retries )) && grep -q "unavailable or temporarily busy" "$register_err"; then
      attempt=$((attempt + 1))
      sleep "$launch_retry_seconds"
      continue
    fi
    cat "$register_err" >&2
    rm -f "$register_err"
    return "$register_status"
  done
  rm -f "$register_err"
  "$python_bin" -E -c 'import json,sys; print(json.loads(sys.stdin.read())["id"])' <<<"$registered"
}

require_runnable() {
  # A failed exec ends a bash 3.2 shell without running its EXIT trap, so a missing CLI is refused here, where it is reported.
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "REFUSED: the CLI program '$1' was not found or is not executable." >&2
    exit 127
  fi
}

launch_visible_cli() {
  runner_step="starting the CLI"
  if [[ "$provider" == "gemini" ]]; then require_runnable "${HARNESS_AGY_BIN:-agy}"; else require_runnable "$1"; fi
  # From the next line on, a death is the CLI's own; until the registry hears this, an ended runner is a failed launch.
  "$python_bin" -E "$harness_root/harness/control.py" "${context_args[@]}" note-cli-exec --session-id "$session_id" >/dev/null 2>&1 || true
  # What the harness itself delivers to this agent, once per launch and after the carrier is chosen (task 2026-10-25_directive-diet).
  # A recording problem is said out loud and never holds the launch.
  if [[ -n "${instruction_file:-}" ]]; then
    printf '%s' "${instruction_text:-}" | "$python_bin" -E "$harness_root/harness/control.py" "${context_args[@]}" note-instruction \
      --session-id "$session_id" --file "$instruction_file" --carrier "${instruction_carrier:-first_message}" --launch-mode "$launch_mode" >/dev/null 2>>"${launch_err_file:-/dev/null}" \
      || echo "NOTE: the size of the rules delivered to this agent could not be recorded; the agent starts normally." >&2
  fi
  credential_prefix=()
  if [[ "$provider" == "claude" ]]; then
    if [[ -z "$claude_auth_bootstrap" ]]; then
      echo "REFUSED: Claude needs a validated setup-token. Launch from Mission Control." >&2
      exit 2
    fi
    credential_prefix=("$python_bin" -E "$harness_root/harness/claude_auth.py" --socket "$claude_auth_bootstrap" --session "$session_id")
    # Only the Reviewer: its end-to-end test copy of the app must start a real Claude (owner's order 2026-10-07).
    if [[ "$kind" == "claude_reviewer" ]]; then
      credential_prefix+=(--share-with-commands)
    fi
    credential_prefix+=(--)
  fi
  if [[ -t 0 && -t 1 ]]; then
    supervisor_args=("${context_args[@]}" --session-id "$session_id" --agent-id "$agent_id" --provider "$provider" --execution-root "$execution_root")
    if [[ "$close_terminal_on_exit" == "1" ]]; then
      supervisor_args+=(--close-terminal-on-exit)
    fi
    supervisor_args+=(${stage3_supervisor_args[@]+"${stage3_supervisor_args[@]}"})
    restore_stderr
    exec ${credential_prefix[@]+"${credential_prefix[@]}"} "$python_bin" -E "$harness_root/harness/interactive_supervisor.py" "${supervisor_args[@]}" -- "$@"
  fi
  restore_stderr
  exec ${credential_prefix[@]+"${credential_prefix[@]}"} "$@"
}

runner_step="reading the role's launch settings"
launch_settings_json="$("$python_bin" -E "$harness_root/harness/control.py" "${context_args[@]}" resolve --kind "$kind" --session-id "$session_id")"
provider="$("$python_bin" -E -c 'import json,sys; print(json.load(sys.stdin)["provider"])' <<<"$launch_settings_json")"
model="$("$python_bin" -E -c 'import json,sys; print(json.load(sys.stdin)["model"])' <<<"$launch_settings_json")"
effort="$("$python_bin" -E -c 'import json,sys; print(json.load(sys.stdin)["effort"])' <<<"$launch_settings_json")"
vendor="$("$python_bin" -E -c 'import sys; sys.path.insert(0, sys.argv[1]); from harness import control; print(control.PROVIDERS[sys.argv[2]]["vendor"])' "$harness_root" "$provider")"

# ONE CLI, CHOSEN DETERMINISTICALLY. This Terminal's own PATH decides nothing:
# on 2026-09-26 a months-old Homebrew `claude` sat first on it and the agents
# ran that. The harness resolves the CLI (HARNESS_*_BIN if set, otherwise the
# newest of the copies it can find), launches exactly that file, and puts its
# folder first on PATH so every `claude`/`codex` the agent itself runs is the
# same one. The launch line in the transcript names the path and version.
runner_step="finding the CLI program"
resolved_cli_json="$("$python_bin" -E -c '
import json, sys
sys.path.insert(0, sys.argv[1])
from harness import global_settings
print(json.dumps(global_settings.resolved_cli(sys.argv[2])))
' "$harness_root" "$provider")" || resolved_cli_json='{}'
cli_path="$("$python_bin" -E -c 'import json,sys; print(json.load(sys.stdin).get("path",""))' <<<"$resolved_cli_json")"
cli_version="$("$python_bin" -E -c 'import json,sys; d=json.load(sys.stdin); print(d.get("version") or ("set by HARNESS_%s_BIN" % sys.argv[1].upper() if d.get("source")=="configured" else "unknown"))' "$provider" <<<"$resolved_cli_json")"
if [[ -n "$cli_path" ]]; then
  case "$provider" in
    codex) export HARNESS_CODEX_BIN="$cli_path" ;;
    gemini) export HARNESS_AGY_BIN="$cli_path" ;;
    *) export HARNESS_CLAUDE_BIN="$cli_path" ;;
  esac
  export PATH="$(dirname "$cli_path"):$PATH"
fi

# The CLI keeps its own memory of a conversation, and the harness keeps the
# vendor's session id so a relaunch RESUMES that memory instead of starting a
# stranger. On 2026-09-22 an afternoon of CTO design work was lost this way.
# plan-cli-launch decides FRESH or RESUME (resume only when the vendor store
# still holds the session; otherwise fresh, and the reason goes into the
# transcript so nobody has to guess). The recovery message replaces the full
# directive on a resume: the agent already has the directive in its memory.
plan_cli_launch() {
  runner_step="planning the CLI launch"
  local plan_json
  plan_json="$("$python_bin" -E "$harness_root/harness/control.py" "${context_args[@]}" plan-cli-launch --session-id "$session_id" --provider "$provider")" || {
    echo "REFUSED: could not plan the CLI launch for $session_id" >&2; exit 2; }
  launch_mode="$("$python_bin" -E -c 'import json,sys; print(json.load(sys.stdin)["mode"])' <<<"$plan_json")"
  cli_session_id="$("$python_bin" -E -c 'import json,sys; print(json.load(sys.stdin)["cli_session_id"])' <<<"$plan_json")"
  transcript_path="$("$python_bin" -E -c 'import json,sys; print(json.load(sys.stdin)["transcript"])' <<<"$plan_json")"
  launch_reason="$("$python_bin" -E -c 'import json,sys; print(json.load(sys.stdin)["reason"])' <<<"$plan_json")"
  codex_marker="$("$python_bin" -E -c 'import json,sys; print(json.load(sys.stdin).get("codex_marker",""))' <<<"$plan_json")"
  predecessor_session="$("$python_bin" -E -c 'import json,sys; print(json.load(sys.stdin).get("predecessor",""))' <<<"$plan_json")"
  after_pause="$("$python_bin" -E -c 'import json,sys; print("1" if json.load(sys.stdin).get("after_pause") else "")' <<<"$plan_json")"
  mkdir -p "$(dirname "$transcript_path")"
  printf '%s -- launch: mode=%s provider=%s cli_session_id=%s (%s) cli=%s version=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%S+00:00)" "$launch_mode" "$provider" "${cli_session_id:-none}" "$launch_reason" "${cli_path:-not-found}" "$cli_version" >> "$transcript_path"
  if [[ "$launch_mode" == "resume" ]]; then
    "$python_bin" -E "$harness_root/harness/control.py" "${context_args[@]}" note-cli-launch --session-id "$session_id" --resumed --reason "$launch_reason" >/dev/null
  else
    "$python_bin" -E "$harness_root/harness/control.py" "${context_args[@]}" note-cli-launch --session-id "$session_id" --reason "$launch_reason" >/dev/null
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
  "$python_bin" -E - "$harness_root" "$cli_session_id" "$transcript_path" "$agent_id" "$board_command_prefix" "$conversation_command" "$after_pause" <<'PY'
import sys
sys.path.insert(0, sys.argv[1])
from harness.conversation import recovery_message
print(recovery_message(sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5], sys.argv[6], after_pause=bool(sys.argv[7])))
PY
}

machine_note() {
  # The CTO has no test-machine chapter; Delivery and the Reviewer are pointed at it (directive diet, 2026-10-25).
  "$python_bin" -E - "$harness_root" "$target_root" "$data_root" "$workspace_root" "$kind" <<'PY'
import sys
sys.path.insert(0, sys.argv[1])
from harness import test_machine
from harness.project_context import context_from_roots
print(test_machine.launch_note(context_from_roots(sys.argv[2], sys.argv[3], sys.argv[4]), open_reference=sys.argv[5] != "claude_cto"))
PY
}

pause_resume_note() {
  "$python_bin" -E - "$harness_root" "$(conversation_command_for "$session_id")" <<'PY'
import sys
sys.path.insert(0, sys.argv[1])
from harness.conversation import pause_resume_note
print(pause_resume_note(sys.argv[2]))
PY
}

launch_agent_cli() {
  local directive_text="$1" kickoff_text="$2" prompt
  plan_cli_launch
  runner_step="preparing the launch (rules, permissions and confinement)"
  # PLUMBING STAGE 4 (docs/specs/PLUMBING_MODERNIZATION.md): the rules move to
  # the system layer (Claude --append-system-prompt, Codex
  # developer_instructions) on EVERY launch, fresh or resume, so compaction or
  # a resume can no longer drop them; the visible first message is the short
  # role kickoff. Off, the prompt is exactly today's: directive, blank line,
  # kickoff.
  system_directive=""
  if [[ -n "$board_bootstrap" && -n "$manager_home" ]]; then
    if [[ -n "$("$python_bin" -E -c '
import sys
from datetime import datetime, timezone
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from harness import cli_capabilities
status = cli_capabilities.stage_status(Path(sys.argv[2]), "stage4_system_layer_directives", sys.argv[3])
if status["reason"].startswith(("PLUMBING FALLBACK", "PLUMBING PROVING")):
    print(datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00"), "--", status["reason"], file=sys.stderr)
print("1" if status["enabled"] else "")
' "$harness_root" "$manager_home" "$provider" 2>>"${transcript_path:-/dev/null}")" ]]; then
      system_directive="$directive_text"
    fi
  fi
  # DIRECTIVE DIET (2026-10-25): what is delivered is the role's one-page card (directives/cards/), and the carrier below is recorded
  # once per launch. No launch path may leave the agent without its card: when the card is not in the system layer it rides the
  # first message, on a resume too (a resume used to replace it with the recovery note alone).
  instruction_text="$directive_text"
  if [[ -n "$system_directive" ]]; then
    instruction_carrier="system_layer"
    prompt="$kickoff_text"
  else
    instruction_carrier="first_message"
    prompt="${directive_text}

${kickoff_text}"
  fi
  if [[ "$launch_mode" == "resume" ]]; then
    prompt="$(recovery_prompt)"
    if [[ -z "$system_directive" ]]; then
      prompt="${directive_text}

${prompt}"
    fi
  else
    if [[ -n "$predecessor_session" ]]; then
      # A previous session of this role existed and could not be resumed:
      # point the new agent at its readable conversation before it starts.
      prompt="${prompt}

$(earlier_conversation_note)"
    fi
    if [[ -n "$after_pause" ]]; then
      # The pause closed this terminal and the CLI's store no longer holds
      # its conversation: the fresh agent is told what happened and where
      # the record is, and to continue the task rather than start over.
      prompt="${prompt}

$(pause_resume_note)"
    fi
    if [[ ( "$provider" == "codex" || "$provider" == "gemini" ) && -n "$codex_marker" ]]; then
      # Codex records this prompt as the first message of its rollout, and the
      # Antigravity CLI as the first request of its transcript; the marker is how
      # the supervisor finds THIS launch's conversation and no other.
      prompt="${prompt}

${codex_marker}"
    fi
  fi
  # Every fresh launch, restart and resume gets the test machine's connection and the current rule (2026-10-20); a project
  # that never used a machine prints nothing here.
  machine_text="$(machine_note 2>/dev/null || true)"
  if [[ -n "$machine_text" ]]; then
    prompt="${prompt}

${machine_text}"
  fi
  # THE SAME WRITE GRANT FOR BOTH VENDORS. Codex confines writes to these
  # roots (sandbox below); Claude is told the same roots with --add-dir so a
  # read or write inside them never stops the agent to ask. Computed once,
  # before the vendor branch, so the two can never drift apart.
  #
  # THE BOARD STORE IS NOT THE AGENT'S (F-1, 2026-10-01). With the
  # authenticated board surface (--board-bootstrap), every write to the
  # project's harness data - board state, contracts, reviews, control records,
  # evidence, backups - is made by the worker, this runner or the supervisor,
  # none of which runs inside the agent's confinement. So the data root is not
  # granted, and because a scaffolded or legacy project keeps it INSIDE the
  # checkout (which is granted), it is also denied explicitly for both vendors.
  # A legacy launch without the surface still runs board commands inside the
  # agent and keeps the old grant; it is not the product's launch path.
  board_surface=""
  [[ -n "$board_bootstrap" ]] && board_surface="1"
  if ! writable_roots_json="$("$python_bin" -E -c '
import json, sys
sys.path.insert(0, sys.argv[1])
from harness.agent_grant import agent_writable_roots, GrantTooBroad
try:
  print(json.dumps(agent_writable_roots(sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5], sys.argv[6] or None,
                                        board_surface=bool(sys.argv[7]))))
except GrantTooBroad as error:
  print(error, file=sys.stderr)
  raise SystemExit(3)
' "$harness_root" "$execution_root" "$data_root" "$workspace_root" "$target_root" "$manager_home" "$board_surface")"; then
    echo "REFUSED: this project's storage layout would grant the agent more than it needs." >&2
    echo "         Fix the project's data or workspace root, then relaunch." >&2
    exit 3
  fi
  protected_writes_json="[]"
  if [[ -n "$board_surface" ]]; then
    protected_writes_json="$("$python_bin" -E -c '
import json, sys
sys.path.insert(0, sys.argv[1])
from harness.agent_grant import agent_protected_roots
print(json.dumps(agent_protected_roots(sys.argv[2], sys.argv[3], sys.argv[4])))
' "$harness_root" "$target_root" "$data_root" "$workspace_root")"
  fi
  # Codex's workspace-write sandbox cannot deny a subpath of a writable root.
  # When a protected path lies inside the grant (scaffolded/legacy layout), the
  # same grant is expressed as a Codex permission profile instead: the whole
  # disk readable, the granted roots and temp space writable, the protected
  # paths read-only, network on. Proven live with `codex sandbox` (0.159.3):
  # workspace write OK, data-root write/mkdir refused, loopback HTTP 200, /tmp
  # writable, .git still protected as under workspace-write. Otherwise the
  # launch flags are exactly today's, minus the data root.
  codex_access=(-c "sandbox_mode=workspace-write" -c "sandbox_workspace_write.writable_roots=${writable_roots_json}" -c "sandbox_workspace_write.network_access=true")
  if [[ "$provider" == "codex" ]]; then
    # Linux: the saved Claude setup-token folder is invisible to Codex (access "none"), even one saved after launch.
    # Codex does not run inside the harness's bubblewrap, so its own permission profile must carry the denial.
    claude_token_dir=""
    if [[ "$(uname -s)" == "Linux" && -n "${HOME:-}" ]]; then
      claude_token_dir="$HOME/.config/nmhp-harness"
      mkdir -p -m 700 "$claude_token_dir" 2>/dev/null || {
        echo "REFUSED: cannot create $claude_token_dir, the folder that keeps the Claude sign-in hidden from this agent. Fix its permissions and retry." >&2; exit 3; }
    fi
    codex_profile_table="$("$python_bin" -E -c '
import json, sys
writable = json.loads(sys.argv[1]); protected = json.loads(sys.argv[2]); hidden = sys.argv[3]
def inside(path, root):
    return path == root or path.startswith(root.rstrip("/") + "/")
if hidden or any(inside(p, w) for p in protected for w in writable):
    entries = {":root": "read", ":slash_tmp": "write", ":tmpdir": "write"}
    entries.update({root: "write" for root in writable})
    entries.update({path: "read" for path in protected})
    if hidden:
        entries[hidden] = "none"
    print("{" + ", ".join(json.dumps(k) + " = " + json.dumps(v) for k, v in entries.items()) + "}")
' "$writable_roots_json" "$protected_writes_json" "$claude_token_dir")"
    if [[ -n "$codex_profile_table" ]]; then
      codex_access=(-c 'default_permissions="harness_agent"' -c "permissions.harness_agent.filesystem=${codex_profile_table}" -c "permissions.harness_agent.network.enabled=true")
    fi
  fi
  if [[ "$provider" == "codex" ]]; then
    # Folder trust travels with the launch, never only in a config file: a private test or review copy
    # of the app must not have to write its throwaway folder into the owner's Codex configuration
    # (2026-10-15). Verified with the real Codex: without it a fresh git project asks, with it it never does.
    case "$execution_root" in *\"*|*\\*) ;; *) codex_access+=(-c "projects.\"${execution_root}\".trust_level=\"trusted\"") ;; esac
  fi
  if [[ "$provider" == "codex" ]]; then
    # Codex's structured question tool is off by default and is switched off explicitly, so an owner-enabled setting cannot put a
    # question in the agent's terminal where the board and the owner never see it.
    codex_access+=(--disable default_mode_request_user_input)
  fi
  if [[ "$provider" == "codex" ]]; then
    # Owner 2026-10-21: a managed agent never gets Codex's bundled browser / computer-use tools (they start the owner's real
    # Chrome and raise a keychain dialog on his Mac). The harness renders pages itself. See harness/codex_isolation.py.
    # Run BY PATH (python -E ignores PYTHONPATH, and this script is inside the project's folder): a failure refuses the launch.
    if ! isolation_out="$("$python_bin" -E "$harness_root/harness/codex_isolation.py")"; then
      echo "Codex tool isolation could not be prepared; the agent was not started." >&2
      exit 2
    fi
    while IFS= read -r isolation_arg; do [[ -n "$isolation_arg" ]] && codex_access+=("$isolation_arg"); done <<< "$isolation_out"
  fi
  if [[ "$provider" == "codex" && -n "$system_directive" ]]; then
    # Stage 4, Codex: the rules as developer instructions, on every launch.
    # Also on the Stage 3 app-server below, which shares these flags (the
    # TUI, not the harness, creates the thread).
    codex_access+=(-c "developer_instructions=$("$python_bin" -E -c 'import json,sys; print(json.dumps(sys.argv[1]))' "$system_directive")")
  fi
  # PLUMBING STAGE 2 (docs/specs/PLUMBING_MODERNIZATION.md): the board as
  # typed MCP tools, started by the CLI itself, alongside the board CLI. A
  # client like board_client - same token, same gates. Inline config only; the
  # token reaches a Codex MCP server by NAME (env_vars), never as a value on
  # the command line where another agent could read it.
  board_mcp_args=()
  mcp_role=""
  case "$kind" in
    codex_delivery) mcp_role="engineering" ;;
    claude_reviewer) mcp_role="qa" ;;
    claude_cto) mcp_role="cto" ;;
  esac
  if [[ -n "$board_surface" && -n "$manager_home" && -n "$mcp_role" ]]; then
    while IFS= read -r -d '' part; do board_mcp_args+=("$part"); done < <("$python_bin" -E -c '
import json, os, sys
from datetime import datetime, timezone
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from harness import cli_capabilities
harness_root, home, provider, role, python, writable, agent = sys.argv[1], Path(sys.argv[2]), sys.argv[3], sys.argv[4], sys.argv[5], json.loads(sys.argv[6]), sys.argv[7]
if provider == "gemini":
    raise SystemExit(0)          # the board tools of a Gemini session are a plugin in its runtime directory (harness/gemini_runtime.py)
status = cli_capabilities.stage_status(home, "stage2_board_mcp", provider)
real = os.path.realpath(harness_root)
if status["enabled"] and any(real == os.path.realpath(root) or real.startswith(os.path.realpath(root).rstrip("/") + "/") for root in writable):
    status = {"enabled": False, "reason": "PLUMBING FALLBACK stage2_board_mcp harness install is inside an agent write grant"}
if not status["enabled"]:
    if status["reason"].startswith("PLUMBING FALLBACK"):
        print(datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00"), "--", status["reason"], file=sys.stderr)
    raise SystemExit(0)
server = os.path.join(harness_root, "harness", "board_mcp.py")
if provider == "claude":
    args = ["--mcp-config", json.dumps({"mcpServers": {"harness_board": {"type": "stdio", "command": python,
            "args": ["-E", server, "--role", role, "--agent", agent]}}}, separators=(",", ":"))]
else:
    args = ["-c", "mcp_servers.harness_board.command=" + json.dumps(python),
            "-c", "mcp_servers.harness_board.args=" + json.dumps(["-E", server, "--role", role, "--agent", agent]),
            "-c", "mcp_servers.harness_board.env_vars=" + json.dumps(["HARNESS_BOARD_TOKEN", "HARNESS_BOARD_ENDPOINT", "HARNESS_BOARD_PROTOCOL"]),
            # approval_policy=never refuses MCP calls that need approval (found
            # live: "MCP tool call requires approval, but approval policy is
            # never"). This server is harness code behind the same gates as
            # the board CLI, which needs no approval either.
            "-c", "mcp_servers.harness_board.default_tools_approval_mode=\"approve\""]
sys.stdout.write("\0".join(args) + "\0")
' "$harness_root" "$manager_home" "$provider" "$mcp_role" "$python_bin" "$writable_roots_json" "${agent_id:-}" 2>>"${transcript_path:-/dev/null}")
  fi
  # PLUMBING STAGE 3, Codex (docs/specs/PLUMBING_MODERNIZATION.md): harness
  # messages without typing. The supervisor runs this session's `codex
  # app-server` on stdio with EXACTLY today's -c flags and attaches the TUI to
  # it through a private socket in the harness runtime directory, which must
  # lie outside every write grant (temp space included). Any missing piece -
  # switch, capability, runtime directory - means today's launch, and a switch
  # that is on but refused says why in the session log.
  if [[ "$provider" == "codex" && -n "$board_surface" && -n "$manager_home" ]]; then
    while IFS= read -r -d '' part; do stage3_supervisor_args+=("$part"); done < <("$python_bin" -E -c '
import json, os, sys
from datetime import datetime, timezone
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from harness import agent_confinement, cli_capabilities, global_settings, runtime_dir
harness_root, home, session, project, writable, codex = sys.argv[1], Path(sys.argv[2]), sys.argv[3], sys.argv[4], json.loads(sys.argv[5]), sys.argv[6]
flags = json.loads(sys.argv[7])
transcript = sys.argv[8]
def refuse(reason):
    if reason.startswith("PLUMBING FALLBACK"):
        print(datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00"), "--", reason, file=sys.stderr)
    raise SystemExit(0)
if transcript and os.path.exists(os.path.join(os.path.dirname(transcript), session + ".stage3-refused")):
    refuse("PLUMBING FALLBACK stage3_codex_app_server the sandbox of this session could not be confirmed on an earlier launch")
status = cli_capabilities.stage_status(home, "stage3_codex_app_server", "codex")
if not status["enabled"]:
    refuse(status["reason"])
if status["reason"]:
    print(datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00"), "--", status["reason"], file=sys.stderr)
directory = runtime_dir.session_directory(home, project, session)
try:
    grant = agent_confinement.writable_paths(writable, home=os.path.expanduser("~"))
    runtime_dir.validate(directory, grant)
except (runtime_dir.RuntimeDirectoryRefused, agent_confinement.ConfinementUnavailable) as error:
    refuse(f"PLUMBING FALLBACK stage3_codex_app_server {error}")
plumbing = global_settings.load(home)["plumbing"]
argv = [codex, "app-server", "--listen", "stdio://", *flags]
# What the thread must report (checked by the multiplexer): the policy of today.
values = dict(flag.split("=", 1) for flag in flags if "=" in flag)
expected = {"approvalPolicy": "never"}
if values.get("default_permissions") == "\"harness_agent\"":
    expected["profile"] = "harness_agent"
else:
    expected["writableRoots"] = json.loads(values.get("sandbox_workspace_write.writable_roots", "[]"))
    expected["networkAccess"] = values.get("sandbox_workspace_write.network_access") == "true"
launch = {"model": values.get("model", "").strip().strip(chr(34)), "effort": values.get("model_reasoning_effort", "").strip()}
sys.stdout.write("\0".join(["--codex-app-server-json", json.dumps(argv), "--codex-expected-policy", json.dumps(expected),
                            "--codex-launch-json", json.dumps(launch),
                            "--runtime-dir", str(directory),
                            "--receipt-timeout", str(plumbing["delivery_receipt_timeout_seconds"]),
                            "--app-server-timeout", str(plumbing["app_server_start_timeout_seconds"])]) + "\0")
' "$harness_root" "$manager_home" "$session_id" "$data_root" "$writable_roots_json" "${HARNESS_CODEX_BIN:-codex}" \
      "$("$python_bin" -E -c 'import json,sys; print(json.dumps(sys.argv[1:]))' -c "model=\"${model}\"" -c "model_reasoning_effort=${effort}" -c "approval_policy=never" "${codex_access[@]}" ${board_mcp_args[@]+"${board_mcp_args[@]}"})" "${transcript_path:-}" \
      2>>"${transcript_path:-/dev/null}")
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
      launch_visible_cli "${HARNESS_CODEX_BIN:-codex}" resume --model "$model" -c "model_reasoning_effort=${effort}" -c "approval_policy=never" "${codex_access[@]}" ${board_mcp_args[@]+"${board_mcp_args[@]}"} "$cli_session_id" "$prompt"
    else
      launch_visible_cli "${HARNESS_CODEX_BIN:-codex}" --cd "$execution_root" --model "$model" -c "model_reasoning_effort=${effort}" -c "approval_policy=never" "${codex_access[@]}" ${board_mcp_args[@]+"${board_mcp_args[@]}"} "$prompt"
    fi
  elif [[ "$provider" == "gemini" ]]; then
    # GEMINI (Antigravity CLI, `agy`), spec board/tasks/2026-10-07_gemini-provider_SPEC.md.
    # Same unattended-and-confined contract as Claude: every permission request is
    # auto-approved (--dangerously-skip-permissions) BECAUSE the whole process runs
    # inside the harness's OS write confinement, limited to the same roots Codex and
    # Claude get plus the CLI's own state (provider-aware: ~/.gemini/antigravity-cli
    # and the cache, never ~/.gemini/config, and the owner's settings.json is a
    # protected write). The CLI reads the owner's subscription sign-in IN PLACE; an
    # API-key or custom-endpoint setup is refused, never silently used (M-2).
    # Folder trust is the owner's decision, made by opening the project (owner's order 2026-10-03: no human at
    # any trust question): the folders go into the CLI's own trustedWorkspaces, written here, outside the sandbox.
    trust_folders=("$execution_root")
    while IFS= read -r granted_root; do [[ -n "$granted_root" ]] && trust_folders+=("$granted_root"); done < <("$python_bin" -E -c 'import json,sys; print("\n".join(json.loads(sys.argv[1])))' "$writable_roots_json")
    "$python_bin" -E -c 'import sys; sys.path.insert(0, sys.argv[1]); from harness import gemini_auth; raise SystemExit(gemini_auth.main(sys.argv[2:]))' "$harness_root" --trust "${trust_folders[@]}" || {
      echo "REFUSED: Gemini is not on your Google subscription sign-in (see the message above)." >&2; exit 3; }
    # A key in the owner's shell must not switch the CLI off the subscription sign-in (it is unset for Gemini
    # only: a Claude or Codex agent's own project may legitimately use one).
    unset GEMINI_API_KEY GOOGLE_API_KEY GOOGLE_GEMINI_BASE_URL
    gemini_access=(--dangerously-skip-permissions)
    # PLUMBING STAGES 1, 2, 3, 4 for Gemini (harness/gemini_runtime.py): hooks, the board as MCP tools and the
    # rules, written by the harness into this session's runtime directory (outside every write grant) and
    # handed to the CLI with --add-dir. Each stage is behind its own switch and proof; a refused stage says
    # why in the session log and the launch is exactly today's.
    gemini_runtime_json='{}'
    if [[ -n "$board_surface" && -n "$manager_home" ]]; then
      gemini_runtime_json="$(printf '%s' "$system_directive" | "$python_bin" -E "$harness_root/harness/gemini_runtime.py" \
        --manager-home "$manager_home" --project "$data_root" --session "$session_id" --harness-root "$harness_root" \
        --python "$python_bin" --writable-json "$writable_roots_json" --role "$mcp_role" --agent "${agent_id:-}" \
        2>>"${transcript_path:-/dev/null}")" || gemini_runtime_json='{}'
    fi
    if [[ -n "$manager_home" ]]; then
      # How long the supervisor looks for this session's conversation: an owner-settable limit, not a number here.
      stage3_supervisor_args+=(--conversation-wait "$("$python_bin" -E -c 'import sys; sys.path.insert(0, sys.argv[1]); from pathlib import Path; from harness import global_settings; print(global_settings.load(Path(sys.argv[2]))["plumbing"]["gemini_conversation_wait_seconds"])' "$harness_root" "$manager_home")")
    fi
    gemini_add_dir="$("$python_bin" -E -c 'import json,sys; print(json.load(sys.stdin).get("add_dir",""))' <<<"$gemini_runtime_json")"
    if [[ -n "$gemini_add_dir" ]]; then
      gemini_access+=(--add-dir "$gemini_add_dir")
      while IFS= read -r -d '' part; do stage3_supervisor_args+=("$part"); done < <("$python_bin" -E -c 'import json,sys; sys.stdout.write("\0".join(json.load(sys.stdin).get("supervisor_args",[]))+"\0")' <<<"$gemini_runtime_json")
      export HARNESS_HOOK_GATE_TIMEOUT="$("$python_bin" -E -c 'import json,sys; print(json.load(sys.stdin).get("gate_timeout",5))' <<<"$gemini_runtime_json")"
      if [[ -n "$system_directive" && "$("$python_bin" -E -c 'import json,sys; print("1" if json.load(sys.stdin).get("stage4") else "")' <<<"$gemini_runtime_json")" == "" ]]; then
        # The rules could not be placed in the system layer after all: the first message carries them, on a resume too.
        instruction_carrier="first_message"
        prompt="${system_directive}

${prompt}"
      fi
    elif [[ -n "$system_directive" ]]; then
      # No runtime preparation at all (it failed): the first message carries the card, on a resume too.
      instruction_carrier="first_message"
      prompt="${system_directive}

${prompt}"
    fi
    execution_root_real="$(cd "$execution_root" && pwd -P)"
    while IFS= read -r granted_root; do
      if [[ -n "$granted_root" && "$granted_root" != "$execution_root_real" ]]; then gemini_access+=(--add-dir "$granted_root"); fi
    done < <("$python_bin" -E -c 'import json,sys; print("\n".join(json.loads(sys.argv[1])))' "$writable_roots_json")
    gemini_confined=()
    while IFS= read -r -d '' part; do gemini_confined+=("$part"); done < <(
      "$python_bin" -E -c '
import json, os, sys
sys.path.insert(0, sys.argv[1])
from harness import agent_confinement
try:
    wrapped = agent_confinement.wrap([], json.loads(sys.argv[2]), store=sys.argv[3], home=os.path.expanduser("~"),
                                     protected_writes=json.loads(sys.argv[4]), provider="gemini", read_confined=True,
                                     readable_roots=agent_confinement.tool_read_roots(
                                         [os.environ.get("HARNESS_AGY_BIN", "agy")], home=os.path.expanduser("~"),
                                         extra=[sys.argv[1], sys.argv[5]]) +
                                     agent_confinement.session_runtime_roots(sys.argv[6], sys.argv[7], sys.argv[8]))
except agent_confinement.ConfinementUnavailable as error:
    print(error, file=sys.stderr)
    raise SystemExit(3)
sys.stdout.write("\0".join(wrapped) + "\0")
' "$harness_root" "$writable_roots_json" "$data_root/control" "$protected_writes_json" "$python_bin" "$manager_home" "$data_root" "$session_id"
    )
    if [[ ${#gemini_confined[@]} -eq 0 ]]; then
      echo "REFUSED: this computer cannot confine the agent's writes (see the message above); the agent is not launched open." >&2
      exit 3
    fi
    if [[ "$launch_mode" == "resume" ]]; then
      launch_visible_cli "${gemini_confined[@]}" "${HARNESS_AGY_BIN:-agy}" --model "$model" --effort "$effort" "${gemini_access[@]}" --conversation "$cli_session_id" -i "$prompt"
    else
      launch_visible_cli "${gemini_confined[@]}" "${HARNESS_AGY_BIN:-agy}" --model "$model" --effort "$effort" "${gemini_access[@]}" -i "$prompt"
    fi
  else
    # NO PERMISSION PROMPTS, AND A REAL BOUNDARY. A managed Claude terminal
    # runs unattended; a "Do you want to proceed?" menu halts all progress
    # until a person looks at the window. The project's own .claude/settings
    # asks for bypass mode, but the CLI ignores that setting at project level
    # ("the session starts in Manual mode", docs/permission-modes) and never
    # restores bypass on --resume (docs/sessions). So bypass is passed on every
    # launch - and BECAUSE bypass removes the CLI's own boundary, the whole CLI
    # process is run inside the harness's OS write confinement (Seatbelt on
    # macOS, bubblewrap on Linux; harness/agent_confinement.py), limited to
    # exactly the roots Codex is granted plus the CLI's own state. A write
    # anywhere else is refused by the operating system, which is what the Help
    # text promises. Reads and network stay open, as for Codex. The launch
    # REFUSES rather than run open when the platform lacks the primitive.
    # The granted roots are also added as working directories so the CLI's
    # own path checks never stop it either.
    claude_access=(--permission-mode bypassPermissions)
    # NO QUESTIONS IN THE TERMINAL (owner, live test 2026-10-23): the CLI's own question widget ("press Tab to accept") shows only
    # in the agent's window, which the owner is not watching, and the board never hears the question. Every managed Claude agent
    # (Delivery, Reviewer, CTO; fresh or resumed - they all pass this line) has that tool denied; questions go through the board.
    # The option is variadic, so it always sits directly before another option in the assembled line.
    claude_access+=(--disallowedTools AskUserQuestion)
    claude_access+=(${board_mcp_args[@]+"${board_mcp_args[@]}"})
    # PLUMBING STAGE 1 (docs/specs/PLUMBING_MODERNIZATION.md): session-scoped
    # hooks, passed INLINE so no file exists for any agent to alter. Only when
    # the owner's switch is on, the authenticated board surface is in use, the
    # CLI's capabilities are proven, and the harness install is outside every
    # agent's write grant (an agent must not be able to edit the gate it is
    # checked by). Otherwise the launch line is exactly as before; a switch
    # that is on but refused says why in the session log.
    hook_settings_json=""
    if [[ -n "$board_surface" && -n "$manager_home" ]]; then
      hook_settings_json="$("$python_bin" -E -c '
import json, os, shlex, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from harness import cli_capabilities, global_settings
harness_root, home, writable, python = sys.argv[1], Path(sys.argv[2]), json.loads(sys.argv[3]), sys.argv[4]
status = cli_capabilities.stage_status(home, "stage1_hooks", "claude")
if status["enabled"]:
    real = os.path.realpath(harness_root)
    if any(real == os.path.realpath(root) or real.startswith(os.path.realpath(root).rstrip("/") + "/") for root in writable):
        status = {"enabled": False, "reason": "PLUMBING FALLBACK stage1_hooks harness install is inside an agent write grant"}
if not status["enabled"]:
    if status["reason"].startswith("PLUMBING FALLBACK"):
        from datetime import datetime, timezone
        print(datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00"), "--", status["reason"], file=sys.stderr)
    raise SystemExit(0)
timeout = global_settings.load(home)["plumbing"]["hook_gate_timeout_seconds"]
gate = " ".join(shlex.quote(part) for part in (python, "-E", os.path.join(harness_root, "harness", "hook_gate.py"), "claude"))
def hook(event, matcher=None):
    entry = {"hooks": [{"type": "command", "command": f"{gate} {event}", "timeout": timeout + 5}]}
    if matcher:
        entry["matcher"] = matcher
    return [entry]
print(json.dumps({"hooks": {
    "PreToolUse": hook("PreToolUse", "Bash|Write|Edit|MultiEdit|NotebookEdit|Read|NotebookRead|Grep|Glob"),
    "SessionStart": hook("SessionStart"), "UserPromptSubmit": hook("UserPromptSubmit"),
    "Stop": hook("Stop"), "SessionEnd": hook("SessionEnd"),
}}, separators=(",", ":")))
' "$harness_root" "$manager_home" "$writable_roots_json" "$python_bin" 2>>"${transcript_path:-/dev/null}")"
      # PLUMBING STAGE 3, Claude (spec §4.3, amended by measurement): harness
      # messages through the session's own inbox. Needs Stage 1's hooks (the
      # SessionStart hook starts the relay). Adds: the supervisor's handover
      # socket in the harness runtime directory, the authority note in the
      # system layer, and a deny on the inter-agent tools (agents talk only
      # through the board). Any missing piece: today's launch, reason logged.
      if [[ -n "$hook_settings_json" ]]; then
        stage3_claude=()
        while IFS= read -r -d '' part; do stage3_claude+=("$part"); done < <("$python_bin" -E -c '
import json, os, sys
from datetime import datetime, timezone
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from harness import agent_confinement, claude_inbox, cli_capabilities, global_settings, runtime_dir
harness_root, home, session, project, writable, settings = sys.argv[1], Path(sys.argv[2]), sys.argv[3], sys.argv[4], json.loads(sys.argv[5]), json.loads(sys.argv[6])
def refuse(reason):
    if reason.startswith("PLUMBING FALLBACK"):
        print(datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00"), "--", reason, file=sys.stderr)
    raise SystemExit(0)
status = cli_capabilities.stage_status(home, "stage3_claude_socket_delivery", "claude")
if not status["enabled"]:
    refuse(status["reason"])
if status["reason"]:
    print(datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00"), "--", status["reason"], file=sys.stderr)
directory = runtime_dir.session_directory(home, project, session)
try:
    grant = agent_confinement.writable_paths(writable, home=os.path.expanduser("~"), claude_config_dir=os.environ.get("CLAUDE_CONFIG_DIR") or None)
    runtime_dir.validate(directory, grant)
except (runtime_dir.RuntimeDirectoryRefused, agent_confinement.ConfinementUnavailable) as error:
    refuse(f"PLUMBING FALLBACK stage3_claude_socket_delivery {error}")
plumbing = global_settings.load(home)["plumbing"]
settings.setdefault("permissions", {}).setdefault("deny", []).extend(claude_inbox.DENIED_TOOLS)
sys.stdout.write("\0".join([json.dumps(settings, separators=(",", ":")), claude_inbox.AUTHORITY_NOTE,
                            "--claude-inbox", "--runtime-dir", str(directory),
                            "--busy-wait", str(plumbing["delivery_busy_wait_seconds"]),
                            "--receipt-timeout", str(plumbing["delivery_receipt_timeout_seconds"]),
                            "--app-server-timeout", str(plumbing["app_server_start_timeout_seconds"])]) + "\0")
' "$harness_root" "$manager_home" "$session_id" "$data_root" "$writable_roots_json" "$hook_settings_json" 2>>"${transcript_path:-/dev/null}")
        if [[ ${#stage3_claude[@]} -gt 2 ]]; then
          hook_settings_json="${stage3_claude[0]}"
          claude_system_layer="${stage3_claude[1]}"
          stage3_supervisor_args=("${stage3_claude[@]:2}")
        fi
        claude_access+=(--settings "$hook_settings_json")
        export HARNESS_HOOK_GATE_TIMEOUT="$("$python_bin" -E -c 'import sys; sys.path.insert(0, sys.argv[1]); from pathlib import Path; from harness import global_settings; print(global_settings.load(Path(sys.argv[2]))["plumbing"]["hook_gate_timeout_seconds"])' "$harness_root" "$manager_home")"
      fi
    fi
    execution_root_real="$(cd "$execution_root" && pwd -P)"
    while IFS= read -r granted_root; do
      # The grant lists resolved paths; the execution root is already the
      # CLI's working directory and needs no --add-dir.
      if [[ -n "$granted_root" && "$granted_root" != "$execution_root_real" ]]; then claude_access+=(--add-dir "$granted_root"); fi
    done < <("$python_bin" -E -c 'import json,sys; print("\n".join(json.loads(sys.argv[1])))' "$writable_roots_json")
    claude_confined=()
    while IFS= read -r -d '' part; do claude_confined+=("$part"); done < <(
      "$python_bin" -E -c '
import json, os, sys
sys.path.insert(0, sys.argv[1])
from harness import agent_confinement
try:
    wrapped = agent_confinement.wrap([], json.loads(sys.argv[2]), store=sys.argv[3], home=os.path.expanduser("~"),
                                     claude_config_dir=os.environ.get("CLAUDE_CONFIG_DIR") or None,
                                     protected_writes=json.loads(sys.argv[4]), read_confined=True,
                                     readable_roots=agent_confinement.tool_read_roots(
                                         [os.environ.get("HARNESS_CLAUDE_BIN", "claude")], home=os.path.expanduser("~"),
                                         extra=[sys.argv[1], sys.argv[5]]) +
                                     agent_confinement.session_runtime_roots(sys.argv[6], sys.argv[7], sys.argv[8]))
except agent_confinement.ConfinementUnavailable as error:
    print(error, file=sys.stderr)
    raise SystemExit(3)
sys.stdout.write("\0".join(wrapped) + "\0")
' "$harness_root" "$writable_roots_json" "$data_root/control" "$protected_writes_json" "$python_bin" "$manager_home" "$data_root" "$session_id"
    )
    if [[ ${#claude_confined[@]} -eq 0 ]]; then
      echo "REFUSED: this computer cannot confine the agent's writes (see the message above); the agent is not launched open." >&2
      exit 3
    fi
    # Folder trust (owner's order 2026-10-03: no human at any time). Opening a
    # project in the app is the owner's trust decision; Codex gets it as the
    # project's trust entry on every open. Claude's own check stops at the git
    # root, so after an agent's `git init` every new project asked again and
    # the agent waited for a person. Claude Code treats a session it is told
    # runs sandboxed as trusted - and this one is: it was just wrapped in the
    # write confinement above, or refused. Measured on 2.1.288: without it a
    # fresh git project asks "Yes, I trust this folder"; with it, the prompt.
    export CLAUDE_CODE_SANDBOXED=1
    # Select and copy (owner's order 2026-10-07): Claude Code's full-screen mode
    # captures the mouse, so in the CTO and Reviewer windows the owner could not
    # select or copy text (the Codex window could). The documented switch turns
    # the capture off so the terminal's own selection works; Page Up/Down still scroll.
    export CLAUDE_CODE_DISABLE_MOUSE=1
    # The Reviewer's end-to-end walk of a whole task takes about 20 minutes, but Claude Code caps each Bash command at
    # BASH_MAX_TIMEOUT_MS = 10 minutes by default, so the Reviewer split the walk into pieces and said so (owner's order
    # 2026-10-07: "take the time it requires ... not be limited to 10 minutes"). The Reviewer only; the value is a setting.
    if [[ "$kind" == "claude_reviewer" ]]; then
      export BASH_MAX_TIMEOUT_MS="${HARNESS_REVIEWER_BASH_MAX_TIMEOUT_MS:-7200000}"
    fi
    if [[ -n "$system_directive" ]]; then
      # Stage 4, Claude: the rules in the system layer, with Stage 3's
      # authority note (when on) folded into the same text.
      if [[ -n "$claude_system_layer" ]]; then
        claude_system_layer="${system_directive}

${claude_system_layer}"
      else
        claude_system_layer="$system_directive"
      fi
    fi
    if [[ -n "$claude_system_layer" ]]; then
      # Every launch, fresh or resume: the system layer is never recorded once
      # and replayed stale (spec Stage 4: snapshot off).
      claude_access+=(--append-system-prompt "$claude_system_layer" --system-prompt-snapshot off)
    fi
    if [[ "$launch_mode" == "resume" ]]; then
      launch_visible_cli "${claude_confined[@]}" "${HARNESS_CLAUDE_BIN:-claude}" --model "$model" --effort "$effort" "${claude_access[@]}" --resume "$cli_session_id" "$prompt"
    else
      launch_visible_cli "${claude_confined[@]}" "${HARNESS_CLAUDE_BIN:-claude}" --model "$model" --effort "$effort" "${claude_access[@]}" --session-id "$cli_session_id" "$prompt"
    fi
  fi
}

runner_step="registering with the board"
case "$kind" in
  codex_delivery)
    agent_id="$(register_agent engineering AWAITING_OWNER_DIRECTION 'Delivery Agent' "$vendor")"
    instruction_file="directives/cards/DELIVERY.md"
    directive="$(<"$harness_root/$instruction_file")"
    kickoff="MODE: Delivery Agent.
Start from ${execution_root}, which supplies the established CLI permissions.
    The target project is ${target_root}; perform all task work and board actions there.
    You are already registered by the visible supervisor as agent ${agent_id}; use that ID for every board command and do not register a second agent.
For every board command, start with: ${board_command_prefix}
No owner direction has been supplied yet. Register your role on the visible
board as standing by, then wait for the owner to describe what they want. Do
not invent an objective, task, Completion Contract, scenario ledger, chunk,
QA request, or bootstrap work before that direction arrives. When it does,
your Product Manager hat converts it into the internal objective and plan.
Before implementation, ask clarifying questions as needed (with the board
command ask-owner, never in the terminal). When the requirements are agreed,
FILE them with propose-requirements --agent ${agent_id}: the text starts with
an 'In plain words:' block of at most three short lines for the owner (what
will be built, what changes for them, whether they must do anything), then a
blank line, then the full requirements. Wait for the owner's Go ahead on the
board; then record the accepted text verbatim with confirm-requirements
--agent ${agent_id}. The original owner direction remains unchanged; this
confirmation is an additional archived section immediately after it. Do not define a delivery
plan or begin implementation until that confirmation is recorded.
If this agent ID is already attached to a recovered task by the board, poll
immediately and resume that preserved task and next action; do not return to
standby or ask the owner to repeat the direction."
    launch_agent_cli "$directive" "$kickoff"
    ;;
  claude_reviewer)
    agent_id="$(register_agent qa REVIEW_QUEUE 'Independent Reviewer' "$vendor")"
    instruction_file="directives/cards/REVIEWER.md"
    directive="$(<"$harness_root/$instruction_file")"
    kickoff="MODE: Independent Reviewer.
There is no implementation task for you. Start from ${execution_root}, which
supplies the established CLI permissions. The target project and visible board
    are ${target_root}; you are already registered by the visible supervisor as agent ${agent_id}; use that ID for every board command and do not register a second agent. Continuously poll that board, claim eligible review
requests, and execute independent QA there.
For every board command, start with: ${board_command_prefix}"
    launch_agent_cli "$directive" "$kickoff"
    ;;
  claude_cto)
    agent_id="$(register_agent cto GLOBAL_MONITOR CTO "$vendor")"
    instruction_file="directives/cards/CTO.md"
    directive="$(<"$harness_root/$instruction_file")"
    kickoff="Start from ${execution_root}, which supplies the established CLI permissions.
    You are the global CTO for the target project ${target_root}. You are already registered by the visible supervisor as agent ${agent_id}; use that ID for every board command and do not register a second agent. Start visible
monitoring of that project's board now.
For every board command, start with: ${board_command_prefix}"
    launch_agent_cli "$directive" "$kickoff"
    ;;
  *) echo "Unknown managed session kind: $kind" >&2; exit 2 ;;
esac
