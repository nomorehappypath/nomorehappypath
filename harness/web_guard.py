# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Which web requests the local app accepts, and the headers every page carries.

Security scan 2026-10-04 (OpenAI Codex Security on v0.7.2), findings 3 and 4:
the board worker accepted cross-origin loopback mutations with no Origin,
Host or media-type check, and no page refused to be framed. Shared by the
manager (Projects) and the board worker (Mission Control).
"""
from __future__ import annotations

import ipaddress

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "[::1]"})
FRAME_HEADERS = (("X-Frame-Options", "DENY"), ("Content-Security-Policy", "frame-ancestors 'none'"))
BODY_MEDIA_TYPES = ("application/json", "multipart/form-data")


def _host_name(value: str) -> str:
    """Normalize a Host header value into a bare host name, dropping any port.

    Lowercases and strips the input value first. A bracketed IPv6 literal
    (e.g. "[::1]:8000") keeps its brackets and loses the trailing port; any
    other value loses a single trailing ":<port>" segment.

    Args:
        value: the raw Host header text (or host string) to normalize.

    Return:
        The lowercased host name, with any port suffix removed.
    """
    value = str(value or "").strip().lower()
    if value.startswith("["):
        return value[: value.find("]") + 1] if "]" in value else value
    return value.rsplit(":", 1)[0] if value.count(":") == 1 else value


def _is_ip_literal(name: str) -> bool:
    """Check whether a host name is an IP address literal rather than a DNS name.

    Strips any surrounding "[" "]" brackets (as used for IPv6 host names)
    before attempting to parse the result as an IPv4 or IPv6 address.

    Args:
        name: the host name text to check.

    Returns:
        True if name parses as an IP address literal, False otherwise.
    """
    try:
        ipaddress.ip_address(name.strip("[]"))
        return True
    except ValueError:
        return False


def host_allowed(host: str, bound: str = "127.0.0.1") -> bool:
    """Loopback names, the address this server was started on, or - when it
    listens on every address - any IP literal (never another name: a rebinding
    page always arrives with a name)."""
    name = _host_name(host)
    if name in LOOPBACK_HOSTS or (bound and name == bound.lower()):
        return True
    return bound in {"0.0.0.0", "::", ""} and _is_ip_literal(name)


def request_refusal(method: str, headers, bound: str = "127.0.0.1") -> str:
    """Why a request must be refused, or "" (security scan 2026-10-04, findings 3 and 4).

    - Only loopback host names: a page that re-points its own name at
      127.0.0.1 (DNS rebinding) arrives with ITS name in Host and is refused.
    - A mutation from a browser must come from this same origin: browsers
      always send Origin on a cross-origin POST, and Sec-Fetch-Site says
      cross-site. Local tools that send neither are unaffected.
    - A request body must be JSON or a multipart form; anything else is not a
      request this app makes (a cross-site "text/plain" post needs no preflight).
    """
    host = str(headers.get("Host") or "")
    if not host_allowed(host, bound):
        return "requests must address this app by its own address (127.0.0.1 or localhost)"
    if method in {"GET", "HEAD", "OPTIONS"}:
        return ""
    origin = str(headers.get("Origin") or "")
    if origin and origin.rstrip("/").lower() != f"http://{host}".lower():
        return "same-origin requests only: cross-origin requests are not accepted"
    if str(headers.get("Sec-Fetch-Site") or "same-origin").lower() not in {"same-origin", "none"}:
        return "same-origin requests only: cross-site requests are not accepted"
    try:
        length = int(headers.get("Content-Length") or 0)
    except ValueError:
        return "request length is invalid"
    media = str(headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
    if length and media not in BODY_MEDIA_TYPES:
        return "requests must send JSON"
    return ""
