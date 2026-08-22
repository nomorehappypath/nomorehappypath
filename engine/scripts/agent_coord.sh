#!/usr/bin/env bash
# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
# ─────────────────────────────────────────────────────────────────
# Vendor-neutral forwarder to agent_coord.py (the coordination CLI).
# Standalone: uses system python3 — no project backend venv required.
#
#   bash agent_coord.sh status
#   bash agent_coord.sh lock --agent <impl> --task "X" --files A B C
#   bash agent_coord.sh note --to <reviewer> --from <impl> --message "…"
#   bash agent_coord.sh merge --agent <impl> --task <task-id>
#   bash agent_coord.sh verify-merge --task <task-id>
#   bash agent_coord.sh unlock --agent <impl>
# ─────────────────────────────────────────────────────────────────
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY_SCRIPT="$SCRIPT_DIR/agent_coord.py"

[ -f "$PY_SCRIPT" ] || { echo "✗  agent_coord.py not found at $PY_SCRIPT" >&2; exit 2; }

exec python3 "$PY_SCRIPT" "$@"
