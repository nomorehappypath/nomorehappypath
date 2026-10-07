# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Explicit memory-only credentials for fake CLI fixtures, never owner login."""
from harness import claude_auth


def auth_arguments(root, session):
    if session.get('provider') == 'claude':
        return ['--claude-auth-bootstrap', claude_auth._handoff(root, session['id'], 'test-setup-token')]
    return []
