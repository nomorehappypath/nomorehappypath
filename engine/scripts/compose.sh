#!/usr/bin/env bash
# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
# Compose the active directive set from engine/ + profile.config. Standalone: system python3.
#   bash compose.sh                                  # auto-discovered profile
#   bash compose.sh --profile p.config --out build/active --strict
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "$SCRIPT_DIR/compose.py" "$@"
