#!/usr/bin/env bash
# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
# Live gates of the Antigravity CLI (`agy`) as a managed agent. Run by hand, signed in, on demand
# (spec board/tasks/2026-10-07_gemini-provider_SPEC.md §5 T0, results §13). Never run by the test suite.
#
#   scripts/agy_spike.sh [--record <manager-home>] [--work <dir>]
#
# Each gate runs INSIDE the real confinement wrapper (harness.agent_confinement), prints one line
# `GATE <name> true|false <evidence>`, and with --record stores it through
# cli_capabilities.record_live_item. It copies and links no credential: the CLI reads the owner's
# sign-in in place. Needs a prior sign-in made inside the box (the CLI's own URL + code flow).
set -u
HERE="$(cd "$(dirname "$0")/.." && pwd)"
RECORD=""
WORK="${TMPDIR:-/tmp}/agy-spike-$$"
while [ $# -gt 0 ]; do
  case "$1" in
    --record) RECORD="$2"; shift 2 ;;
    --work) WORK="$2"; shift 2 ;;
    *) echo "usage: $0 [--record <manager-home>] [--work <dir>]" >&2; exit 2 ;;
  esac
done
export PYTHONPATH="$HERE" PYTHONDONTWRITEBYTECODE=1
AGY="${HARNESS_AGY_BIN:-$(command -v agy || true)}"
[ -x "$AGY" ] || { echo "GATE agy_missing false no agy on PATH and HARNESS_AGY_BIN unset"; exit 1; }
MODEL="${AGY_SPIKE_MODEL:-gemini-3.8-flash-low}"
PROJ="$WORK/proj"; RUNTIME="$WORK/runtime"; STORE="$WORK/control"
mkdir -p "$PROJ" "$RUNTIME/.agents/rules" "$STORE"
cleanup() { rm -rf "$WORK"; }
trap cleanup EXIT

result() {   # result <item> <true|false> <evidence>
  echo "GATE $1 $2 $3"
  if [ -n "$RECORD" ]; then
    python3 - "$RECORD" "$1" "$2" "$3" <<'PY'
import sys
from pathlib import Path
from harness import cli_capabilities
home, item, value, evidence = sys.argv[1:5]
cli_capabilities.record_live_item(Path(home), "gemini", item, value == "true", evidence)
PY
  fi
}

# boxed <protected-write-path-or-empty> -- argv...   (the wrapper's own command line, as the product builds it)
boxed() {
  local protected="$1"; shift; [ "${1:-}" = "--" ] && shift
  local roots protectedjson wrapped
  roots="$(python3 -c 'import json,sys; print(json.dumps(sys.argv[1:]))' "$PROJ" "$RUNTIME")"
  protectedjson="$(python3 -c 'import json,sys; print(json.dumps([a for a in sys.argv[1:] if a]))' "$protected")"
  wrapped="$(python3 -m harness.agent_confinement --provider gemini --store "$STORE" --writable-roots "$roots" --protected-writes "$protectedjson" -- "$@")" || return 3
  python3 - "$wrapped" <<'PY'
import json, os, subprocess, sys
raise SystemExit(subprocess.run(json.loads(sys.argv[1]), cwd=os.environ.get("AGY_CWD") or None, stdin=subprocess.DEVNULL).returncode)
PY
}

# G-A: authentication under confinement (signed-in model list + one real request).
if AGY_CWD="$PROJ" boxed "" -- "$AGY" models >"$WORK/models.txt" 2>&1 && grep -q "gemini-" "$WORK/models.txt"; then
  ENVELOPE="$(AGY_CWD="$PROJ" boxed "" -- "$AGY" -p "Reply with exactly the word OK." --output-format json --model "$MODEL" 2>"$WORK/ga.err")"
  if printf '%s' "$ENVELOPE" | grep -q '"status": *"SUCCESS"'; then
    result gemini.auth_under_confinement true "boxed 'agy models' listed models and boxed 'agy -p' returned SUCCESS with $MODEL"
  else
    result gemini.auth_under_confinement false "boxed 'agy -p' did not succeed: $(tr '\n' ' ' <"$WORK/ga.err" | cut -c1-200)"
  fi
else
  result gemini.auth_under_confinement false "boxed 'agy models' failed: $(tr '\n' ' ' <"$WORK/models.txt" | cut -c1-200). Sign in once inside the box (agy's URL + code flow)."
fi

# G-C: hooks and rules from an --add-dir runtime directory; the runtime directory is tamper-proof.
cat >"$RUNTIME/.agents/hooks.json" <<JSON
{"harness-spike":{"PreInvocation":[{"type":"command","command":"/bin/sh -c 'cat > \"$WORK/hook-payload.json\"; echo {}'","timeout":10}]}}
JSON
echo 'Begin every reply with the word ZEBRA.' | tee "$RUNTIME/.agents/rules/harness.md" >"$RUNTIME/GEMINI.md"
AGY_CWD="$PROJ" boxed "$RUNTIME/.agents" -- "$AGY" -p "Say hello." --output-format json --model "$MODEL" --add-dir "$RUNTIME" >"$WORK/gc.out" 2>/dev/null
FIRED=false; grep -q '"conversationId"' "$WORK/hook-payload.json" 2>/dev/null && FIRED=true
RULED=false; grep -q 'ZEBRA' "$WORK/gc.out" && RULED=true
TAMPER=true
AGY_CWD="$PROJ" boxed "$RUNTIME/.agents" -- /bin/sh -c "echo x > '$RUNTIME/.agents/hooks.json'" 2>/dev/null && TAMPER=false
AGY_CWD="$PROJ" boxed "$RUNTIME/.agents" -- /bin/sh -c "rm -f '$RUNTIME/.agents/hooks.json'" 2>/dev/null; [ -f "$RUNTIME/.agents/hooks.json" ] || TAMPER=false
# A PreToolUse hook's answer decides the tool call: an explicit allow lets it run, a deny stops it.
# (Measured on 1.3.1: an EMPTY object `{}` also denies - the harness gate must answer allow explicitly.)
tool_ran() {   # tool_ran <hook answer, as JSON text> -> prints ran|denied|unknown
  local answer="$1" conversation
  python3 - "$RUNTIME/.agents/hooks.json" "$answer" <<'PY'
import json, shlex, sys
command = "/bin/sh -c " + shlex.quote("cat >/dev/null; echo " + shlex.quote(sys.argv[2]))
json.dump({"harness-spike": {"PreToolUse": [{"matcher": ".*", "hooks": [{"type": "command", "command": command, "timeout": 10}]}]}},
          open(sys.argv[1], "w"))
PY
  conversation="$(AGY_CWD="$PROJ" boxed "$RUNTIME/.agents" -- "$AGY" -p "Run the shell command: echo spike-tool-ran. Then say finished." --output-format json --model "$MODEL" --dangerously-skip-permissions --add-dir "$RUNTIME" 2>/dev/null | python3 -c 'import json,sys; print(json.loads(sys.stdin.read().strip().splitlines()[-1]).get("conversation_id",""))' 2>/dev/null)"
  python3 - "$HOME/.gemini/antigravity-cli/brain/$conversation/.system_generated/logs/transcript.jsonl" <<'PY'
import json, sys
state = "unknown"
try:
    for line in open(sys.argv[1], encoding="utf-8"):
        entry = json.loads(line)
        if entry.get("type") == "GENERIC":
            state = "denied" if "denied by pre-tool hook" in str(entry.get("content")) else ("ran" if "spike-tool-ran" in str(entry.get("content")) else state)
except OSError:
    pass
print(state)
PY
}
ALLOWED="$(tool_ran '{"decision":"allow"}')"
DENIED="$(tool_ran '{"decision":"deny","reason":"spike"}')"
EMPTYOBJ="$(tool_ran '{}')"
if [ "$ALLOWED" = ran ] && [ "$DENIED" = denied ]; then PRETOOL=true; else PRETOOL=false; fi
if [ "$FIRED" = true ] && [ "$RULED" = true ] && [ "$TAMPER" = true ] && [ "$PRETOOL" = true ]; then
  result gemini.session_scoped_controls true "hook fired with conversationId, rules applied from an --add-dir runtime dir, boxed write/remove of .agents denied, PreToolUse allow runs the tool and deny stops it (an empty {} answer: $EMPTYOBJ)"
else
  result gemini.session_scoped_controls false "hook_fired=$FIRED rules_applied=$RULED tamper_proof=$TAMPER pretool_allow=$ALLOWED pretool_deny=$DENIED"
fi

# G-D busy: a PreInvocation hook injects a queued message into a running TUI turn; receipt = SYSTEM_SDK transcript entry.
QUEUE="$PROJ/queue.txt"
cat >"$RUNTIME/inject.sh" <<SH
#!/bin/sh
Q="$QUEUE"
cat >/dev/null
if [ -s "\$Q" ]; then M=\$(cat "\$Q"); : > "\$Q"; /usr/bin/python3 -c 'import json,sys; print(json.dumps({"injectSteps":[{"userMessage": sys.argv[1]}]}))' "\$M"; else echo '{}'; fi
SH
cat >"$RUNTIME/.agents/hooks.json" <<JSON
{"harness-delivery":{"PreInvocation":[{"type":"command","command":"/bin/sh $RUNTIME/inject.sh","timeout":10}]}}
JSON
rm -f "$RUNTIME/.agents/rules/harness.md" "$RUNTIME/GEMINI.md"
echo '[SYSTEM CONTROL — spike] Reply with exactly the single word BANANA. (harness delivery d1)' >"$QUEUE"
BRAIN="$HOME/.gemini/antigravity-cli/brain"
export AGY_SPIKE_BOOT="${AGY_SPIKE_BOOT:-15}" AGY_SPIKE_TURN="${AGY_SPIKE_TURN:-40}"   # seconds the TUI gets to start and to finish a turn
BUSY="$(python3 - "$AGY" "$PROJ" "$RUNTIME" "$STORE" "$MODEL" "$BRAIN" "$HOME" <<'PY'
import json, os, pty, select, signal, subprocess, sys, time
from pathlib import Path
agy, proj, runtime, store, model, brain, home = sys.argv[1:8]
from harness import agent_confinement
roots = [proj, runtime]
argv = agent_confinement.wrap([agy, "--model", model, "--add-dir", runtime], roots, store=Path(store), home=home,
                              protected_writes=[str(Path(runtime) / ".agents")], provider="gemini")
before = set(os.listdir(brain)) if os.path.isdir(brain) else set()
pid, fd = pty.fork()
if pid == 0:
    os.chdir(proj)
    os.execvp(argv[0], argv)
def drain(seconds):
    end = time.time() + seconds
    while time.time() < end:
        if select.select([fd], [], [], 0.2)[0]:
            try:
                os.read(fd, 65536)
            except OSError:
                return
drain(float(os.environ["AGY_SPIKE_BOOT"]))
try:
    os.write(fd, b"\r")             # a folder the CLI has not seen asks "Do you trust this project?": Enter accepts
    drain(3)
    os.write(fd, b"Say exactly the word hello and nothing else.\r")
except OSError:                     # the session ended on its own: no turn, so no receipt
    pass
drain(float(os.environ["AGY_SPIKE_TURN"]))
after = set(os.listdir(brain)) if os.path.isdir(brain) else set()
receipt = False
for name in sorted(after - before):
    transcript = Path(brain) / name / ".system_generated/logs/transcript.jsonl"
    text = transcript.read_text(errors="replace") if transcript.is_file() else ""
    receipt = receipt or ('"SYSTEM_SDK"' in text and "harness delivery d1" in text)
group = pid                         # pty.fork makes the child a session leader: its group id is its pid
try:
    os.killpg(group, signal.SIGKILL)
    os.waitpid(pid, 0)
except OSError:
    pass
gone = False
for _ in range(15):                 # bounded: the group has no member left once the kernel reaps them
    try:
        os.killpg(group, 0)
    except OSError:
        gone = True
        break
    time.sleep(0.2)
print(("true" if receipt else "false"), ("true" if gone else "false"))
PY
)"
GONE="${BUSY#* }"; BUSY="${BUSY%% *}"
# G-B: one process group per session; SIGKILL of the group leaves nothing of the session behind.
if [ "$GONE" = true ]; then
  result gemini.process_group_cleanup true "a boxed interactive session was one process group; killpg(SIGKILL) left no process of it"
else
  result gemini.process_group_cleanup false "a process of the session survived killpg(SIGKILL)"
fi
if [ "$BUSY" = true ]; then
  result gemini.busy_delivery_receipt true "PreInvocation injectSteps delivered a queued message to a running TUI turn; transcript.jsonl holds it as SYSTEM_SDK with its delivery id"
else
  result gemini.busy_delivery_receipt false "no SYSTEM_SDK transcript entry with the delivery id (a first-run trust prompt in a new folder can swallow the first line: rerun)"
fi
