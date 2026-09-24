#!/usr/bin/env bash
# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
# Intake + planning CLI — turn a need into a plan and cross-vendor work items. System python3.
#   bash intake.sh questions --idea "..."
#   bash intake.sh plan --idea "..." --intent commercial --features "log in" "see invoices"
#   bash intake.sh decompose --plan .agents/intake/<plan_id>.json
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "$SCRIPT_DIR/intake.py" "$@"
