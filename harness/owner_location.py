# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Where an owner card sends the owner, and whether that place is the owner's.

Owner 2026-10-15 (Studio, 15:46-15:56Z): Delivery started a private Studio copy
under /private/tmp on the owner's own port 8900, got "Claude needs sign-in",
and the CTO pinned "Sign in to Claude inside Studio". The owner tried the copy,
could not sign in, lost ten minutes; the card was withdrawn as wrong. A card
that asks the owner to sign in or act inside an app must say WHERE, and the
board checks that place before the card exists:

  owner          the location answers and nothing says it is an agent's copy
  private_copy   the listener's folder is a temp folder or a task workspace, or
                 the listener descends from a running agent session
  unanswering    nothing answers at that address
  cannot_verify  the board could not look (no tools, no permission, TLS, a
                 remote host): the CTO states how it knows the place is the
                 owner's (--location-verified "<how>"), which is recorded on the card

Nothing here follows a redirect, reads a body beyond a few bytes, or runs for
more than the deadline; every failure is a reason, never an exception.
"""
from __future__ import annotations

import ipaddress
import os
import re
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

PROBE_TIMEOUT_SECONDS = 3.0
TOTAL_DEADLINE_SECONDS = 8.0
MAX_CARD_LOCATIONS = 4
SUBPROCESS_TIMEOUT_SECONDS = 2.0

SIGN_IN_WORDS = re.compile(r"\b(?:sign(?:ed|ing)?[ -]?in(?:to)?|log(?:ged|ging)?[ -]?in(?:to)?|/login|authenticate|authentication)\b", re.I)
# An action the owner performs somewhere: a place word ("inside the app", "in Studio", "at the portal",
# "within the dashboard") or a verb that opens a place ("open the app", "visit the site", "go to Studio").
# Review 2026-10-15 round 3: "Open http://... and approve", "Complete the setup in Studio" must count.
PLACE_WORDS = r"(?:app|application|site|website|page|portal|dashboard|console|copy|instance|studio|interface|ui|browser tab)"
_DETERMINER = r"(?:(?:the|your|its|that|this)\s+)?"
ACT_AT_PLACE = re.compile(r"\b(?:inside|into|within|in|at|on)\s+" + _DETERMINER + PLACE_WORDS + r"\b", re.I)
ACT_AT_NAMED = re.compile(r"\b(?:inside|into|within|in|[Ii]n)\s+(?:the\s+)?(?P<name>[A-Z][\w-]+)\b")
OPEN_PLACE = re.compile(r"\b(?:open|use|visit|go to|launch|load|reach)\s+" + _DETERMINER + r"(?P<name>" + PLACE_WORDS + r"|[A-Z][\w-]+)\b", re.I)
# Names that are never an app the owner is sent to: roles, products, machines, the board's own words.
NOT_PLACES = {
    "mission", "control", "terminal", "finder", "keychain", "settings", "chrome", "chromium", "safari", "firefox", "xcode",
    "mac", "macos", "linux", "windows", "ubuntu", "delivery", "reviewer", "cto", "codex", "claude", "gemini", "python",
    "board", "git", "github", "main", "view", "accept", "accepted", "recover", "agent", "project", "projects", "task",
    "history", "help", "mission control", "desktop", "downloads", "documents", "library", "home", "october", "november", "december", "january", "february", "march", "april",
    "may", "june", "july", "august", "september", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
}
# Places the owner already has and reaches without any agent's copy: Mission Control's own controls, and the
# owner's own machine apps. The existing sign-in flows ("open its terminal and run /login", "clear the saved
# site permission in Chrome", "In Terminal run ...") stay accepted.
HARNESS_CONTROLS = re.compile(
    r"mission control|(?:its|the|that|this|your|delivery|reviewer|cto|agent|codex|claude|gemini)[\w' -]{0,24}terminal"
    r"|view agent|recover agent|view app|release card|task card|claude authentication card|authentication card|setup-token"
    r"|(?<!'s )(?<!\u2019s )(?<!app )(?<!its )settings(?:\s*(?:page|panel))?\b"
    r"|\b(?:accepted?|go ahead|modify|respond to delivery|give direction|send clarification|not accepted)\b"
    r"|\b(?:in|open|use)\s+(?:the\s+)?(?:terminal|finder|keychain(?: access)?|system settings|chrome|chromium|safari|firefox|xcode|browser)\b",
    re.I,
)
URL = re.compile(r"https?://[^\s<>\"')\]]+", re.I)


# The destinations the owner already has: Mission Control's own controls and the owner's own machine apps.
# A destination is judged by its own name (one or two words), never by other words near it (review round 5:
# "inside Studio using Settings" names Studio, and "Settings" beside it does not make Studio a known place).
KNOWN_DESTINATIONS = {
    "mission control", "terminal", "finder", "keychain", "keychain access", "system settings", "settings", "settings page",
    "settings panel", "chrome", "chromium", "safari", "firefox", "xcode", "browser", "view app", "release card", "task card",
    "authentication card", "claude authentication card", "help", "task history", "history",
}


def destination_is_known(name: str, following: str) -> bool:
    first = name.lower().strip("'’s ")
    second = f"{first} {following.lower()}".strip() if following else first
    if first in KNOWN_DESTINATIONS or second in KNOWN_DESTINATIONS:
        return True
    return first == "terminal" or second.endswith(" terminal")


def place_phrases(text: str, known_names: set[str] | frozenset[str] = frozenset()) -> list[tuple[str, str, str]]:
    """(phrase, destination name, the word after it) for every phrase that sends the owner somewhere;
    task names, agent ids and role/product/machine words are not places."""
    excluded = {name.lower() for name in known_names} | NOT_PLACES
    found: list[tuple[str, str, str]] = []

    def following(end: int) -> str:
        rest = text[end:].lstrip()
        return re.match(r"[\w-]*", rest).group(0) if rest else ""

    for match in ACT_AT_PLACE.finditer(text):
        name = match.group(0).split()[-1]
        found.append((match.group(0), name, following(match.end())))
    for pattern in (ACT_AT_NAMED, OPEN_PLACE):
        for match in pattern.finditer(text):
            name = match.group("name")
            if name.lower() in excluded or any(name.lower() == known.lower() or known.lower().startswith(name.lower() + "-") for known in known_names):
                continue
            if pattern is OPEN_PLACE and not (re.fullmatch(PLACE_WORDS, name, re.I) or name[:1].isupper()):
                continue
            found.append((match.group(0), name, following(match.end())))
    return found


def unresolved_places(title: str, why: str, known_names: set[str] | frozenset[str] = frozenset()) -> list[str]:
    """Place phrases whose destination is neither a Mission Control control nor the owner's own machine app."""
    text = f"{title} {why}"
    return [phrase for phrase, name, nxt in place_phrases(text, known_names) if not destination_is_known(name, nxt)]


def names_a_location_need(title: str, why: str, known_names: set[str] | frozenset[str] = frozenset()) -> str:
    """'sign_in', 'act_inside' or '' — does this card send the owner somewhere?"""
    text = f"{title} {why}"
    if SIGN_IN_WORDS.search(text):
        return "sign_in"
    places = place_phrases(text, known_names)
    if places and any(not destination_is_known(name, nxt) for _, name, nxt in places):
        return "act_inside"
    return ""


def names_a_harness_control(title: str, why: str, command: str) -> bool:
    return bool(HARNESS_CONTROLS.search(f"{title} {why} {command}"))



def urls_in(*texts: str) -> list[str]:
    found: list[str] = []
    for text in texts:
        for match in URL.finditer(text or ""):
            value = match.group(0).rstrip(".,;:")
            if value not in found:
                found.append(value)
    return found


def classify_host(url: str) -> tuple[str, str, int]:
    """('loopback' | 'bind_address' | 'remote' | 'invalid', host, port)."""
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        port = parts.port or (443 if parts.scheme == "https" else 80)
    except ValueError:
        return "invalid", "", 0
    if not host:
        return "invalid", "", 0
    if host in {"0.0.0.0", "::", "[::]"}:
        return "bind_address", host, port
    if host == "localhost":
        return "loopback", host, port
    try:
        address = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return "remote", host, port
    return ("loopback" if address.is_loopback else "remote"), host, port


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        return None


def probe(url: str, timeout: float = PROBE_TIMEOUT_SECONDS) -> tuple[str, str]:
    """('answers' | 'unanswering' | 'cannot_verify', detail). A HEAD, no redirects, no body."""
    opener = urllib.request.build_opener(_NoRedirect, urllib.request.ProxyHandler({}))
    request = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "nomorehappypath-board/location-check"})
    try:
        with opener.open(request, timeout=timeout) as response:
            return "answers", f"HTTP {response.status}"
    except urllib.error.HTTPError as error:
        return "answers", f"HTTP {error.code}"        # a 4xx/5xx page is still something answering there
    except urllib.error.URLError as error:
        reason = getattr(error, "reason", error)
        if "CERTIFICATE" in str(reason).upper() or "SSL" in str(reason).upper():
            return "cannot_verify", f"TLS could not be checked from the board ({reason})"
        return "unanswering", f"nothing answers ({reason})"
    except (socket.timeout, TimeoutError):
        return "unanswering", "nothing answered within the timeout"
    except (OSError, ValueError) as error:
        return "cannot_verify", f"the board could not probe it ({error.__class__.__name__})"


def _run(arguments: list[str]) -> str:
    try:
        return subprocess.run(arguments, capture_output=True, text=True, timeout=SUBPROCESS_TIMEOUT_SECONDS, stdin=subprocess.DEVNULL).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def listeners(port: int) -> tuple[list[int], str]:
    """PIDs listening on the port in every address family, or ([], reason) when the board cannot look."""
    output = _run(["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-Fp"])
    pids = sorted({int(line[1:]) for line in output.splitlines() if line.startswith("p") and line[1:].isdigit()})
    if pids:
        return pids, ""
    if os.path.exists("/proc/net/tcp"):
        pids = _proc_listeners(port)
        if pids:
            return pids, ""
        return [], "no process could be seen listening on that port (the board may lack permission to look)"
    return [], "the board has no tool to see which process listens on that port"


def _proc_listeners(port: int) -> list[int]:
    inodes: set[str] = set()
    for table in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            lines = Path(table).read_text(encoding="utf-8").splitlines()[1:]
        except OSError:
            continue
        for line in lines:
            fields = line.split()
            if len(fields) > 9 and fields[3] == "0A" and int(fields[1].rsplit(":", 1)[1], 16) == port:
                inodes.add(fields[9])
    if not inodes:
        return []
    pids: list[int] = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            for fd in (entry / "fd").iterdir():
                if os.readlink(fd).strip("socket:[]") in inodes:
                    pids.append(int(entry.name)); break
        except OSError:
            continue
    return sorted(set(pids))


def process_cwd(pid: int) -> str:
    try:
        return os.readlink(f"/proc/{pid}/cwd")
    except OSError:
        pass
    output = _run(["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"])
    for line in output.splitlines():
        if line.startswith("n"):
            return line[1:]
    return ""


def ancestors(pid: int, limit: int = 32) -> list[int]:
    chain: list[int] = []
    current = pid
    for _ in range(limit):
        try:
            parent = int(Path(f"/proc/{current}/stat").read_text().rsplit(")", 1)[1].split()[1])
        except (OSError, ValueError, IndexError):
            parent_text = _run(["ps", "-o", "ppid=", "-p", str(current)]).strip()
            parent = int(parent_text) if parent_text.isdigit() else 0
        if parent <= 1 or parent in chain:
            break
        chain.append(parent); current = parent
    return chain


def temp_folders() -> list[Path]:
    folders = [Path("/tmp"), Path("/private/tmp"), Path(tempfile.gettempdir())]
    return [Path(os.path.realpath(str(folder))) for folder in folders]


def _under(path: str, bases: list[Path]) -> bool:
    if not path:
        return False
    resolved = Path(os.path.realpath(path))
    return any(resolved == base or base in resolved.parents for base in bases)


def check(url: str, *, project_roots: list[Path], agent_copy_roots: list[Path], agent_session_pids: set[int], deadline: float) -> dict[str, Any]:
    """One location's verdict: {'url', 'verdict', 'reason', 'checked_at'}."""
    kind, host, port = classify_host(url)
    if kind == "invalid":
        return _verdict(url, "cannot_verify", "the URL could not be read")
    if kind == "bind_address":
        return _verdict(url, "unanswering", f"{host} is a bind address, not a place the owner can open; name 127.0.0.1 or the host name")
    if kind == "remote":
        # the board never probes a host outside this computer (a one-use link or an action endpoint
        # must not be consumed by a check), and a remote answer would not show which instance it is
        return _verdict(url, "cannot_verify", f"{host} is outside this computer; the board does not probe it and cannot see which instance runs there")
    if time.monotonic() > deadline:
        return _verdict(url, "cannot_verify", "the board ran out of time checking the card's locations")
    answered, detail = probe(url, min(PROBE_TIMEOUT_SECONDS, max(0.5, deadline - time.monotonic())))
    if answered == "unanswering":
        return _verdict(url, "unanswering", detail)
    if answered == "cannot_verify":
        return _verdict(url, "cannot_verify", detail)
    pids, why_not = listeners(port)
    if not pids:
        return _verdict(url, "cannot_verify", why_not)
    for pid in pids:
        cwd = process_cwd(pid)
        hit = next((p for p in ancestors(pid) if p in agent_session_pids), None)
        if hit is not None:
            return _verdict(url, "private_copy", f"the process on port {port} (pid {pid}) was started by a running agent session (pid {hit}): not the owner's instance")
        if _under(cwd, agent_copy_roots):
            return _verdict(url, "private_copy", f"the process on port {port} (pid {pid}) runs from a task workspace ({cwd}): an agent's candidate, not the owner's instance")
        if _under(cwd, project_roots):
            continue  # the project's own folder: the owner's instance unless an agent started it (checked above)
        if _under(cwd, temp_folders()):
            return _verdict(url, "private_copy", f"the process on port {port} (pid {pid}) runs from a temp folder ({cwd}): a private copy an agent started for its own test, not the owner's instance")
        return _verdict(url, "cannot_verify", f"the process on port {port} (pid {pid}) runs from {cwd or 'a folder the board cannot read'}, which is not this project's folder; say how you know it is the owner's instance")
    return _verdict(url, "owner", f"answers ({detail}); the listener runs from the project's own folder")


def _verdict(url: str, verdict: str, reason: str) -> dict[str, Any]:
    from harness.board import now
    return {"url": url, "verdict": verdict, "reason": reason, "checked_at": now()}
