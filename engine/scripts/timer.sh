#!/usr/bin/env bash
# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
# OS-timer generator — render a launchd/cron unit to run `scheduler tick` durably. System python3.
#   bash timer.sh gen --kind launchd --interval 120
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "$SCRIPT_DIR/timer.py" "$@"
