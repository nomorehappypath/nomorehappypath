# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Keep the managed role in titles emitted by a CLI."""
import re


def role_title(session_id: str) -> str:
    """Map a session_id's prefix to its managed role label.

    Args:
        session_id: The session identifier, whose prefix before the first
            "-" selects the role.

    Return:
        The role label, or an empty string when the prefix is unrecognized.
    """
    return {
        "codex_delivery": "Developer",
        "claude_reviewer": "Reviewer",
        "claude_cto": "CTO",
    }.get(session_id.split("-", 1)[0], "")


def window_title(session_id: str) -> str:
    """The agent Terminal window's title, e.g. "Reviewer [38fe3154b1]": role plus the session's unique part.

    Takes a session id such as "claude_reviewer-38fe3154b1"; returns the title. The id's first part is a
    fixed slot name from when each role had one vendor, so it is left out: the window would otherwise say
    "claude_reviewer" while Codex runs in it (owner 2026-11-04).
    """
    return role_title(session_id) + " [" + session_id.split("-", 1)[-1] + "]"


class TitlePrefix:
    """Insert the role after an OSC 0/1/2 title header, even across reads.

    Every original byte is forwarded immediately. Only the last three bytes
    are remembered to recognize a four-byte header split over PTY reads; no
    output is delayed, decoded, discarded, or used to modify the transcript.
    """
    def __init__(self, session_id: str):
        """Set up the prefix for session_id and clear the pending-byte tail.

        Args:
            session_id: The session identifier used to look up the role
                title to insert.

        Returns:
            None.
        """
        title = role_title(session_id)
        self.prefix = (title + " | ").encode() if title else b""
        self.tail = b""

    def feed(self, data: bytes) -> bytes:
        """Insert the role prefix after any OSC 0/1/2 title header in data.

        Args:
            data: The next chunk of raw PTY bytes to forward.

        Returns:
            The bytes to output, with the role prefix inserted right after
            each title header, including one split across feed calls.
        """
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
