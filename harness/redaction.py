# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""One shared place to strip a credential shape out of free text before it is stored or shown.

Used everywhere an agent's own free text (a status note, a transcript line, a captured command's
output, a git remote URL) is written to the board, the event log, or a log file. Never applied to
the owner's own typed direction or clarification text, which must stay byte-exact.

Deliberately broad: text that loses an opaque token is cheaper to read than text that leaks one.
"""
from __future__ import annotations

import re
from typing import Any

MARKER = "[redacted]"

_SECRET_SHAPES = re.compile(
    r"sk-ant-[A-Za-z0-9]{2,6}\d{2}-[A-Za-z0-9_-]{20,}"                       # Claude setup tokens and API keys
    r"|\b(?:sk|pk|rk)[-_][A-Za-z0-9_-]{16,}"                                  # other provider keys
    r"|\b(?:ghp|gho|ghs|ghu|ghr)_[A-Za-z0-9]{16,}|\bgithub_pat_[A-Za-z0-9_]{16,}"   # GitHub tokens, fine-grained included
    r"|\bxox[abprs]-[A-Za-z0-9-]{10,}|\bAKIA[0-9A-Z]{12,}|\bAIza[0-9A-Za-z_-]{20,}"  # Slack, AWS, Google
    r"|\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"           # JSON web tokens
    r"|HARNESS_[A-Z_]*TOKEN\S*"                                               # this harness's own token env vars
    r"|(?i:\bbearer\s+)[A-Za-z0-9._~+/=-]{8,}"                                 # a bearer credential, any case
    r"|(?i:\b(?:authorization|api[-_ ]?key|access[-_ ]?token|secret|password|passwd|token)\b\s*[:=]\s*)(?i:(?:bearer|basic|token)\s+)?\S+"   # a labelled secret
    r"|\b(?=[A-Za-z0-9_-]*[A-Za-z])(?=[A-Za-z0-9_-]*\d)[A-Za-z0-9_-]{32,}\b",   # any long opaque letters-and-digits run
)

_URL_USERINFO = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^/\s@]+@")


def redact(text: Any) -> str:
    """Return `text` with every credential-shaped substring, including a git URL's userinfo, replaced by a marker."""
    cleaned = _URL_USERINFO.sub(r"\1" + MARKER + "@", str(text or ""))
    return _SECRET_SHAPES.sub(MARKER, cleaned)
