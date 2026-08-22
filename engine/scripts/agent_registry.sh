#!/usr/bin/env bash
# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
# Agent registry CLI — identity layer of the orchestration substrate. Standalone: system python3.
#   bash agent_registry.sh register --vendor "Claude (Anthropic)" --role implementer
#   bash agent_registry.sh list
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "$SCRIPT_DIR/agent_registry.py" "$@"
