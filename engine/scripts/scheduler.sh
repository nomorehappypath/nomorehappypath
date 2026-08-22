#!/usr/bin/env bash
# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
# Durable scheduler CLI — dispatch core of the orchestration substrate. System python3.
#   bash scheduler.sh tick                 # one pass
#   bash scheduler.sh run --interval 120   # loop (stop-gap; OS-timer install is next)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "$SCRIPT_DIR/scheduler.py" "$@"
