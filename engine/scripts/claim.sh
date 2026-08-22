#!/usr/bin/env bash
# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
# Claim/assignment protocol CLI — routing layer of the orchestration substrate. System python3.
#   bash claim.sh post --task T1 --role reviewer --forbid-vendor "Claude (Anthropic)"
#   bash claim.sh claim --item <item-id> --signature <sig>
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "$SCRIPT_DIR/claim.py" "$@"
