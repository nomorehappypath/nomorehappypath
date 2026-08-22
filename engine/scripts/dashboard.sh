#!/usr/bin/env bash
# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
# Live dashboard CLI — observability over the orchestration substrate. System python3.
#   bash dashboard.sh show                 # text view
#   bash dashboard.sh html --out build/dashboard.html   # browsable HTML
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "$SCRIPT_DIR/dashboard.py" "$@"
