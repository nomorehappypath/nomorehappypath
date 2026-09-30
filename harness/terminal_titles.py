# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Keep the managed role in titles emitted by a CLI."""
import re


def role_title(session_id: str) -> str:
    return {
        "codex_delivery": "Developer",
        "claude_reviewer": "Reviewer",
        "claude_cto": "CTO",
    }.get(session_id.split("-", 1)[0], "")


class TitlePrefix:
    """Insert the role after an OSC 0/1/2 title header, even across reads.

    Every original byte is forwarded immediately. Only the last three bytes
    are remembered to recognize a four-byte header split over PTY reads; no
    output is delayed, decoded, discarded, or used to modify the transcript.
    """
    def __init__(self, session_id: str):
        title = role_title(session_id)
        self.prefix = (title + " | ").encode() if title else b""
        self.tail = b""

    def feed(self, data: bytes) -> bytes:
        if not self.prefix:
            return data
        joined = self.tail + data
        offset = len(self.tail)
        self.tail = joined[-3:]
        output, start = [], 0
        for match in re.finditer(rb"\x1b\][012];", joined):
            end = match.end() - offset
            output.extend((data[start:end], self.prefix))
            start = end
        output.append(data[start:])
        return b"".join(output)
