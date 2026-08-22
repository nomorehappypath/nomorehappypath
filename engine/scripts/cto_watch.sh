#!/usr/bin/env bash
# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
# Vendor-neutral forwarder to cto_watchdog.py. Standalone: system python3.
#   bash cto_watch.sh --repo .                       # one-shot
#   bash cto_watch.sh --watch-interval 600 --only-changes   # 10-min monitor
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "$SCRIPT_DIR/cto_watchdog.py" "$@"
