# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Recognise a managed terminal that has stopped to wait for the owner.

A managed agent is supposed to run unattended. When its CLI still stops at a
permission prompt ("Do you want to proceed?"), or the agent itself prints a
line asking the owner to act, all progress halts until a person looks at the
Terminal window. The supervisor already sees every byte the child prints;
this module watches that stream for the forms a waiting terminal takes and
reports the transitions, so Mission Control can say so loudly instead of the
owner discovering it hours later. The recogniser is a fallback: the launch
flags are what stop the prompts from appearing in the first place.

What the owner sees is the SCREEN, not the byte stream. A full-screen CLI
redraws by moving the cursor and erasing lines; a prompt that was answered
is overwritten or cleared, not scrolled away. The first version kept a
rolling window of the stripped output and so kept alarming after the screen
was cleared (reviewer finding, 2026-09-23). `Screen` is a small model of the
terminal's visible rows - cursor movement, absolute positioning, erase line/
display, clear, alternate screen - and the watch asks whether a prompt is on
the screen NOW.
"""
from __future__ import annotations

import re

from harness.conversation import strip_terminal_sequences

DEFAULT_HEIGHT = 40       # rows on screen when the real terminal size is unknown
MAX_COLS = 4000           # a row longer than this is a redraw artefact, not text

# A control sequence the last read cut in half: held back until the rest arrives.
_INCOMPLETE_ESCAPE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*|\][^\x07\x1b]*)?$")

# Each pattern is matched against the visible screen text.
# The label is what the owner reads on the banner.
PROMPT_FORMS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"Do you want to (?:proceed|make this edit|create|run|allow)", re.IGNORECASE),
     "is asking permission to continue ('Do you want to proceed?')"),
    (re.compile(r"switch to auto mode", re.IGNORECASE),
     "is asking permission to continue ('Do you want to proceed?')"),
    (re.compile(r"USER ACTION:\s*Needed", re.IGNORECASE),
     "says it needs you to act ('USER ACTION: Needed')"),
    # 2026-09-25 defect #15: an expired login stalled the CTO for three hours
    # and only the event log knew. The login prompt is its own cause.
    (re.compile(r"(?:run|use|type|try)\s+/login|not logged in|please log ?in|login required|"
                r"authentication (?:failed|expired|required|error)|session expired|token expired", re.IGNORECASE),
     "appears to be logged out (open its terminal and run /login)"),
)

# One token of the stream: an escape sequence, a control character, or text.
_TOKEN = re.compile(
    r"\x1b\[(?P<csi_params>[0-?]*)(?P<csi_inter>[ -/]*)(?P<csi_final>[@-~])"   # CSI
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"                                       # OSC (titles, links)
    r"|\x1b[@-Z\\-_]"                                                           # two-byte escapes
    r"|(?P<ctrl>[\r\n\x08\x07\x0c])"                                            # controls we model
    r"|(?P<text>[^\x1b\r\n\x08\x07\x0c]+)"                                      # printable run
)


def detect(text: str) -> str | None:
    """The reason a terminal is waiting, or None when this text shows none."""
    for pattern, reason in PROMPT_FORMS:
        if pattern.search(text):
            return reason
    return None


class Screen:
    """The visible rows of a terminal, updated from its raw output.

    Not a full emulator: it models what CLIs use to redraw — newline, carriage
    return, backspace, cursor up/down/forward/back, absolute positioning
    (CUP/HVP/CHA), erase in line (EL), erase in display (ED), clear, and the
    alternate screen — and ignores colours and everything else. Text written
    at the cursor overwrites what was there, as on a real terminal. The
    screen has a HEIGHT (the real terminal's, from the supervisor; a default
    otherwise): a newline on the bottom row scrolls the top row off, and what
    scrolled off is gone — a prompt forty lines up is no longer on screen. A
    control sequence cut in half by the read boundary is held back until the
    rest arrives (reviewer finding, round 2).
    """

    def __init__(self, height: int = DEFAULT_HEIGHT) -> None:
        self.rows: list[str] = [""]
        self.row = 0
        self.col = 0
        self.height = max(2, int(height or DEFAULT_HEIGHT))
        self._partial = b""
        self._pending = ""

    def resize(self, height: int | None) -> None:
        """The terminal changed size; keep the bottom rows, as a terminal does."""
        if not height or int(height) < 2:
            return
        self.height = int(height)
        self._ensure_row()

    # -- geometry helpers -------------------------------------------------
    def _ensure_row(self) -> None:
        # Clamp BEFORE allocating: a cursor sent a million rows down is at the
        # bottom of the screen, not a million rows away (reviewer finding,
        # round 3: CSI 1000000 L allocated 16 MB before the height applied).
        if self.row >= self.height:
            del self.rows[:self.row - (self.height - 1)]
            self.row = self.height - 1
        while self.row >= len(self.rows):
            self.rows.append("")
        if len(self.rows) > self.height:
            overflow = len(self.rows) - self.height
            del self.rows[:overflow]
            self.row = max(0, self.row - overflow)

    def _write(self, text: str) -> None:
        self._ensure_row()
        line = self.rows[self.row]
        if self.col > len(line):
            line = line.ljust(self.col)
        line = line[:self.col] + text + line[self.col + len(text):]
        self.rows[self.row] = line[:MAX_COLS]
        self.col = min(self.col + len(text), MAX_COLS)

    def _erase_line(self, mode: int) -> None:
        self._ensure_row()
        line = self.rows[self.row]
        if mode == 0:
            self.rows[self.row] = line[:self.col]
        elif mode == 1:
            self.rows[self.row] = " " * min(self.col, len(line)) + line[self.col:]
        else:
            self.rows[self.row] = ""

    def _erase_display(self, mode: int) -> None:
        self._ensure_row()
        if mode == 0:
            self._erase_line(0)
            del self.rows[self.row + 1:]
        elif mode == 1:
            for index in range(self.row):
                self.rows[index] = ""
            self._erase_line(1)
        else:
            self.clear()

    def clear(self) -> None:
        self.rows = [""]
        self.row = 0
        self.col = 0

    # -- input ------------------------------------------------------------
    @staticmethod
    def _param(params: str, index: int, default: int) -> int:
        parts = params.split(";")
        try:
            value = int(parts[index]) if index < len(parts) and parts[index] else default
        except ValueError:
            value = default
        # No terminal parameter needs more than this; it bounds every loop
        # and allocation below regardless of what the CLI sends.
        return min(max(value, 0), 1_000_000)

    def _rows(self, params: str, index: int = 0) -> int:
        """A row count clamped to the screen: what a real terminal would move by."""
        return min(max(1, self._param(params, index, 1)), self.height)

    def _cols(self, params: str, index: int = 0) -> int:
        return min(max(1, self._param(params, index, 1)), MAX_COLS)

    def feed(self, data: bytes) -> None:
        data = self._partial + data
        text = data.decode("utf-8", errors="replace")
        # A multibyte character split across reads would decode as U+FFFD;
        # hold back an incomplete trailing sequence instead.
        self._partial = b""
        if data and data[-1] & 0x80:
            tail = 0
            while tail < min(4, len(data)) and (data[-1 - tail] & 0xC0) == 0x80:
                tail += 1
            if tail < len(data) and (data[-1 - tail] & 0xC0) == 0xC0:
                self._partial = data[-1 - tail:]
                text = data[:-1 - tail].decode("utf-8", errors="replace")
        text = self._pending + text
        self._pending = ""
        cut = _INCOMPLETE_ESCAPE.search(text)
        if cut and len(text) - cut.start() < 4096:   # a split title (OSC) can be long; junk longer than this is not a sequence
            self._pending = text[cut.start():]
            text = text[:cut.start()]
        for match in _TOKEN.finditer(text):
            if match.group("text") is not None:
                self._write(match.group("text"))
            elif match.group("ctrl") is not None:
                control = match.group("ctrl")
                if control == "\n":
                    self.row += 1
                    self.col = 0
                    self._ensure_row()
                elif control == "\r":
                    self.col = 0
                elif control == "\x08":
                    self.col = max(0, self.col - 1)
                elif control == "\x0c":
                    self.clear()
            elif match.group("csi_final") is not None:
                self._csi(match.group("csi_params"), match.group("csi_final"))

    def _csi(self, params: str, final: str) -> None:
        if params.startswith("?"):
            # Alternate screen on/off: a new, empty screen either way.
            if final in "hl" and params[1:] in {"1049", "47", "1047"}:
                self.clear()
            return
        if final == "A":
            self.row = max(0, self.row - self._rows(params))
        elif final == "B" or final == "e":
            self.row += self._rows(params)
            self._ensure_row()
        elif final == "C" or final == "a":
            self.col = min(MAX_COLS, self.col + self._cols(params))
        elif final == "D":
            self.col = max(0, self.col - self._cols(params))
        elif final in "Hf":
            self.row = min(self.height - 1, max(0, self._param(params, 0, 1) - 1))
            self.col = min(MAX_COLS, max(0, self._param(params, 1, 1) - 1))
            self._ensure_row()
        elif final == "G" or final == "`":
            self.col = min(MAX_COLS, max(0, self._param(params, 0, 1) - 1))
        elif final == "d":
            self.row = min(self.height - 1, max(0, self._param(params, 0, 1) - 1))
            self._ensure_row()
        elif final == "E":
            self.row += self._rows(params); self.col = 0
            self._ensure_row()
        elif final == "F":
            self.row = max(0, self.row - self._rows(params)); self.col = 0
        elif final == "K":
            self._erase_line(self._param(params, 0, 0))
        elif final == "J":
            self._erase_display(self._param(params, 0, 0))
        elif final == "M":
            # Delete lines at the cursor; at most what is below it.
            self._ensure_row()
            del self.rows[self.row:self.row + self._rows(params)]
            self._ensure_row()
        elif final == "L":
            # Insert blank lines at the cursor; the screen never grows past its height.
            self._ensure_row()
            count = min(self._rows(params), self.height - self.row)
            self.rows[self.row:self.row] = [""] * count
            del self.rows[self.height:]
        # Colours, cursor save/restore, scroll regions and the rest: ignored.

    def text(self) -> str:
        return "\n".join(strip_terminal_sequences(row) for row in self.rows)


class PromptWatch:
    """Watches one terminal's screen; reports when waiting starts and ends.

    Two facts are kept apart: whether a prompt is VISIBLE on the screen, and
    whether the alarm is raised. The alarm rises when a prompt appears and
    drops when it disappears or when the owner types. After the owner has
    typed, a prompt still on screen does not re-raise the alarm; only a prompt
    that goes away and comes back does.
    """

    def __init__(self, height: int | None = None) -> None:
        self.screen = Screen(height or DEFAULT_HEIGHT)
        self.visible: str | None = None
        self.reason: str | None = None

    def resize(self, height: int | None) -> tuple[str, str | None] | None:
        self.screen.resize(height)
        return self._reconcile()

    def _reconcile(self) -> tuple[str, str | None] | None:
        now = detect(self.screen.text())
        was = self.visible
        self.visible = now
        if now and not was:
            self.reason = now
            return ("waiting", now)
        if was and not now and self.reason:
            self.reason = None
            return ("cleared", None)
        return None

    def feed(self, data: bytes) -> tuple[str, str | None] | None:
        """Absorb child output. Returns ("waiting", reason), ("cleared", None) or None."""
        self.screen.feed(data)
        return self._reconcile()

    def owner_typed(self) -> tuple[str, str | None] | None:
        """The owner answered in the terminal: whatever was waiting is now theirs."""
        if self.reason:
            self.reason = None
            return ("cleared", None)
        return None
