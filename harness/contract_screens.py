# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""The screens are part of the contract (owner 2026-11-21: "I like that the UX is part of the contract and I will have a button
to see the different UX screens").

Delivery designs each screen and important state as one self-contained HTML file and uploads it with `contract-screen` before
filing the contract; the owner opens them with "See the screens" on the contract card; the agreed set is frozen with the
contract and the final review compares the real app with it. A design is shown to the owner, so it must be inert: it is
parsed (never string-matched) and refused if it could run code or load anything from outside; the page serves it with a
no-script content policy inside a sandboxed frame.
"""
from __future__ import annotations

from html.parser import HTMLParser
import hashlib
import json
import re
from pathlib import Path
from typing import Any

MAX_BYTES = 8 * 1024 * 1024      # what the browser can still show at once
UX_SKILL = "ux-ui-design"
CONTENT_POLICY = ("default-src 'none'; style-src 'unsafe-inline'; img-src data:; font-src data:; form-action 'none'; "
                  "frame-ancestors 'self'; base-uri 'none'; sandbox")
REFUSED_TAGS = {"script", "iframe", "frame", "frameset", "object", "embed", "applet", "link", "base", "portal", "noscript"}
URL_ATTRIBUTES = {"href", "src", "srcset", "action", "formaction", "data", "poster", "background", "xlink:href", "cite",
                  "longdesc", "manifest", "ping", "lowsrc", "dynsrc", "icon", "codebase", "archive", "profile"}
CSS_URL = re.compile(r"url\s*\(\s*(['\"]?)(.*?)\1\s*\)", re.IGNORECASE | re.DOTALL)
CSS_IMPORT = re.compile(r"@import|expression\s*\(|behavior\s*:|-moz-binding", re.IGNORECASE)
FORMAT = ("contract-screen --agent <id> --name \"<screen - state>\" --file <design.html>: one self-contained HTML file per screen "
          "and important state (inline CSS, no scripts, no outside links or fonts, images only as data:image, at most 512 KB)")


def _safe_url(value: str) -> bool:
    """Only an in-page anchor or an inline image can be named by a design."""
    value = re.sub(r"[\s\x00-\x1f]", "", value or "").lower()
    return value == "" or value.startswith("#") or value.startswith("data:image/") and not value.startswith("data:image/svg")


class _Inspector(HTMLParser):
    """Collects the first reason a design is not inert."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.problem = ""
        self.in_style = False

    def _refuse(self, text: str) -> None:
        if not self.problem:
            self.problem = text

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in REFUSED_TAGS:
            self._refuse(f"a <{tag}> element is not allowed")
        if tag == "meta" and any((name or "").lower() == "http-equiv" for name, _ in attrs):
            self._refuse("a <meta http-equiv> element is not allowed")
        if tag == "style":
            self.in_style = True
        for name, value in attrs:
            name = (name or "").lower()
            if name.startswith("on"):
                self._refuse(f"an event handler ({name}) is not allowed")
            elif name in URL_ATTRIBUTES:
                parts = [part.strip().split(" ")[0] for part in (value or "").split(",")] if name == "srcset" else [value or ""]
                if not all(_safe_url(part) for part in parts):
                    self._refuse(f"{name}=\"{(value or '')[:60]}\" points outside the design (only #anchors and data:image are allowed)")
            elif name == "style":
                self._css(value or "")

    handle_startendtag = handle_starttag

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "style":
            self.in_style = False

    def handle_data(self, data: str) -> None:
        if self.in_style:
            self._css(data)

    def _css(self, text: str) -> None:
        if CSS_IMPORT.search(text):
            self._refuse("CSS @import, expression() or bindings are not allowed")
        for match in CSS_URL.finditer(text):
            if not _safe_url(match.group(2)):
                self._refuse(f"CSS url({match.group(2)[:60]}) points outside the design")


def validate(data: bytes) -> str:
    """The plain reason a design cannot be shown to the owner, or ""."""
    if len(data) > MAX_BYTES:
        return f"the design is {len(data) // 1024} KB; at most {MAX_BYTES // 1024} KB"
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return "the design is not UTF-8 text"
    if "<" not in text:
        return "the design is not HTML"
    inspector = _Inspector()
    try:
        inspector.feed(text)
        inspector.close()
    except Exception as error:  # a parser failure is a refusal, never a pass
        return f"the design could not be read as HTML ({error})"
    return inspector.problem


def clean_name(name: str) -> str:
    """A design's name as the owner reads it ("Projects - first visit")."""
    name = " ".join(str(name or "").split())
    return name or "Screen"


def store(folder: Path, data: bytes) -> dict[str, Any]:
    """Store one design under its content hash; returns its file name, hash and size."""
    digest = hashlib.sha256(data).hexdigest()
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{digest}.html"
    if not path.exists():
        temporary = path.with_suffix(".tmp")
        temporary.write_bytes(data)
        temporary.replace(path)
    return {"file": path.name, "sha256": digest, "bytes": len(data)}


def manifest_id(manifest: list[dict[str, Any]]) -> str:
    """The identity of one design set (names, order and contents)."""
    return hashlib.sha256(json.dumps([[item["name"], item["sha256"]] for item in manifest]).encode()).hexdigest()[:16]


def read(folder: Path, item: dict[str, Any]) -> bytes:
    """The stored bytes of one design, checked against its recorded hash (Codex plan check R1-4); ValueError when altered."""
    if not re.fullmatch(r"[0-9a-f]{64}\.html", str(item.get("file") or "")):
        raise ValueError("this design's record is not valid")
    path = folder / item["file"]
    try:
        data = path.read_bytes()
    except OSError as error:
        raise ValueError("this design is missing from the board's storage") from error
    if hashlib.sha256(data).hexdigest() != item.get("sha256"):
        raise ValueError("this design was changed after it was agreed")
    return data


def ui_task(state: dict[str, Any], task: str, small_change: bool = False) -> bool:
    """A task that builds or changes screens by default: development work, not document-only, analysis, spec or a small change."""
    if small_change or (state.get("task_document_only") or {}).get(task):
        return False
    return str((state.get("task_kinds") or {}).get(task) or "development") not in {"spec", "analysis"}


def screens_block_present(text: str) -> bool:
    """The contract carries its plain "Screens:" block."""
    return bool(re.search(r"(?im)^\s*screens\s*:", text or ""))


def coverage_problems(ledger_text: str, manifest: list[dict[str, Any]]) -> list[str]:
    """Every agreed design needs a ledger row naming it with a rendered screen id (Codex plan check R1-6, R2-3, R2-4)."""
    rows = [line for line in (ledger_text or "").splitlines() if line.strip().startswith("|")]
    missing = []
    for item in manifest:
        name = item["name"].lower()
        if not any(name in row.lower() and re.search(r"\bscreen-[0-9A-Za-z]", row) for row in rows):
            missing.append(item["name"])
    return missing
