#!/usr/bin/env bash
# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
# Spawn-runner CLI — launch a real agent process for a work item. System python3.
#   bash runner.sh dispatch --item <item-id> --signature <sig>
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "$SCRIPT_DIR/runner.py" "$@"
