# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""A harness-run click/type script against a project's own loopback page (the `screen-script` board command).

Agents' own sandboxes refuse localhost, and a plain screen-check only renders one page. This module runs a tiny,
validated step list (open, click, type, expect) in the harness browser, outside the agent's sandbox. The agent never
supplies code: steps are data, every string reaches the page only as a JSON value inside harness-owned JavaScript
templates, and the browser itself is held to the page's own server:

* all non-loopback traffic is refused by the browser (proxy and resolver flags), and every request the page makes is
  paused and refused unless it goes to the page's own origin (DevTools Fetch interception);
* new windows are closed, downloads are denied, file choosers are intercepted, JavaScript dialogs are dismissed;
* the whole run - browser start, protocol calls and steps - shares one 90 second deadline.
"""
from __future__ import annotations

import base64
import hashlib
import http.client
import json
import select
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urljoin, urlsplit

from harness import browser_acceptance as ba

MAX_STEPS = 12
MAX_TEXT = 500
MAX_STEPS_BYTES = 50_000
WALL_SECONDS = 90
STEP_WAIT_SECONDS = 5.0
STEP_TEXT_LIMIT = 4_000
STEP_KINDS = ("open", "click", "type", "expect")
TEXT_INPUT_TYPES = {"", "text", "search", "email", "url", "tel", "password", "number"}
BROWSER_ARGS = [
    # Browser-wide: everything that is not loopback fails, for every window, frame and worker.
    "--proxy-server=http://127.0.0.1:9", "--proxy-bypass-list=127.0.0.1;localhost;[::1]",
    "--host-resolver-rules=MAP * ~NOTFOUND , EXCLUDE 127.0.0.1 , EXCLUDE localhost",
]

# Visible text without changing the page: the same rules as the screen-check collector (hidden, transparent, 1px and
# off-screen text does not count) but read-only, so a later step never meets a control this reader hid.
READ_TEXT = r"""(() => {
  if (!document.body) return '';
  const out = [];
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  const seen = (el) => {
    for (let e = el; e && e !== document.documentElement; e = e.parentElement) {
      const s = getComputedStyle(e);
      if (s.display === 'none' || s.visibility === 'hidden' || s.opacity === '0' || e.hidden) return false;
    }
    const b = el.getBoundingClientRect();
    return !((b.width <= 1 && b.height <= 1) || b.right <= 0 || b.bottom <= 0);
  };
  let node;
  while ((node = walker.nextNode())) {
    const text = node.nodeValue.trim();
    const el = node.parentElement;
    if (text && el && !['SCRIPT', 'STYLE', 'NOSCRIPT', 'TEMPLATE'].includes(el.tagName) && seen(el)) out.push(text);
  }
  return out.join(' ');
})()"""

# Finds the one control a step names, checks that a person could use it, and reports where to click.
LOCATE = r"""((spec) => {
  const visible = (el) => {
    for (let e = el; e && e !== document.documentElement; e = e.parentElement) {
      const s = getComputedStyle(e);
      if (s.display === 'none' || s.visibility === 'hidden' || s.opacity === '0' || e.hidden) return false;
    }
    const b = el.getBoundingClientRect();
    return b.width > 1 && b.height > 1;
  };
  const own = (el) => (el.tagName === 'INPUT' ? (el.value || '') : (el.textContent || '')).replace(/\s+/g, ' ').trim();
  let found = [];
  try {
    if (spec.selector) found = Array.from(document.querySelectorAll(spec.selector));
    else {
      const wanted = spec.text.replace(/\s+/g, ' ').trim().toLowerCase();
      const controls = Array.from(document.querySelectorAll(
        "button, a, summary, label, [role=button], [role=link], [role=tab], [role=menuitem], input[type=button], input[type=submit]"));
      found = controls.filter((el) => own(el).toLowerCase() === wanted);
      if (!found.length) {
        found = Array.from(document.querySelectorAll('body *')).filter(
          (el) => el.children.length === 0 && own(el).toLowerCase() === wanted);
      }
    }
  } catch (error) { return {ok: false, reason: 'the selector is not valid: ' + error.message}; }
  if (!found.length) return {ok: false, reason: 'no element matches ' + (spec.selector ? 'the selector' : 'that text')};
  const shown = found.filter(visible);
  if (!shown.length) return {ok: false, reason: 'the element exists but is not visible'};
  if (shown.length > 1) return {ok: false, reason: shown.length + ' visible elements match; use a more specific selector'};
  const el = shown[0];
  const fileInput = el.matches('input[type=file]') || (el.control && el.control.matches && el.control.matches('input[type=file]'))
    || (el.closest('label') && el.closest('label').control && el.closest('label').control.matches('input[type=file]'));
  if (fileInput) return {ok: false, reason: 'file upload is not allowed in a screen script', refused: true};
  if (el.disabled || el.getAttribute('aria-disabled') === 'true') return {ok: false, reason: 'the element is disabled'};
  el.scrollIntoView({block: 'center', inline: 'center'});
  const b = el.getBoundingClientRect();
  const x = b.left + b.width / 2, y = b.top + b.height / 2;
  const hit = document.elementFromPoint(x, y);
  if (!hit || !(hit === el || el.contains(hit) || hit.contains(el))) {
    return {ok: false, reason: 'the element is covered by another element and a person could not click it'};
  }
  return {ok: true, x, y, tag: el.tagName.toLowerCase(), type: (el.getAttribute('type') || '').toLowerCase()};
})"""


class ScriptError(ValueError):
    """A step list that is not allowed (shape, size, kinds)."""


def _text(value: Any, label: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        raise ScriptError(f"{label} must be a non-empty text")
    if len(value) > MAX_TEXT:
        raise ScriptError(f"{label} must be {MAX_TEXT} characters or fewer")
    return value


def parse_steps(payload: bytes | str) -> list[dict[str, Any]]:
    """Validate a step list; raises ScriptError with a plain message. No code is accepted, only data."""
    raw = payload if isinstance(payload, bytes) else payload.encode("utf-8")
    if len(raw) > MAX_STEPS_BYTES:
        raise ScriptError(f"the step list is larger than {MAX_STEPS_BYTES // 1000} KB")
    try:
        steps = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as error:
        raise ScriptError(f"the step list is not valid JSON: {error}") from None
    if not isinstance(steps, list) or not steps:
        raise ScriptError("the step list must be a JSON list with at least one step")
    if len(steps) > MAX_STEPS:
        raise ScriptError(f"a screen script may have at most {MAX_STEPS} steps; this one has {len(steps)}")
    clean: list[dict[str, Any]] = []
    for number, step in enumerate(steps, 1):
        label = f"step {number}"
        if not isinstance(step, dict) or len(step) != 1 or next(iter(step)) not in STEP_KINDS:
            raise ScriptError(f"{label} must be an object with exactly one of: " + ", ".join(STEP_KINDS))
        kind, value = next(iter(step.items()))
        if kind in {"open", "expect"}:
            clean.append({kind: _text(value, f"{label} {kind}")})
        elif kind == "click":
            if not isinstance(value, dict) or len(value) != 1 or next(iter(value)) not in {"text", "selector"}:
                raise ScriptError(f'{label} click must be {{"text": ...}} or {{"selector": ...}}')
            key = next(iter(value))
            clean.append({"click": {key: _text(value[key], f"{label} click {key}")}})
        else:
            if not isinstance(value, dict) or set(value) != {"selector", "text"}:
                raise ScriptError(f'{label} type must be {{"selector": ..., "text": ...}}')
            clean.append({"type": {
                "selector": _text(value["selector"], f"{label} type selector"),
                "text": _text(value["text"], f"{label} type text", allow_empty=True),
            }})
    if "expect" not in clean[-1]:
        raise ScriptError("the last step must be an expect step: a script that never checks what the page shows proves nothing")
    return clean


def _normal(text: str) -> str:
    return " ".join(str(text).split())


class _Driver(ba._DevToolsSocket):
    """The DevTools socket plus event handling: the page's own server is the only place it may reach."""

    def __init__(self, port: int, path: str, timeout: float, origin: str) -> None:
        super().__init__(port, path, timeout)
        self.origin = origin
        self.violations: list[str] = []
        self.notes: list[str] = []
        self.target_id = ""

    def _write(self, method: str, params: dict[str, Any] | None = None) -> int:
        self.next_id += 1
        body = json.dumps({"id": self.next_id, "method": method, "params": params or {}}).encode()
        mask = ba.os.urandom(4)
        size = len(body)
        header = bytes([0x81]) + (
            bytes([0x80 | size]) if size < 126
            else bytes([0x80 | 126]) + size.to_bytes(2, "big") if size < 65536
            else bytes([0x80 | 127]) + size.to_bytes(8, "big")
        )
        self.socket.sendall(header + mask + bytes(value ^ mask[index % 4] for index, value in enumerate(body)))
        return self.next_id

    def _allowed(self, url: str) -> bool:
        parts = urlsplit(url)
        if parts.scheme in {"data", "blob", "about"}:
            return True
        return parts.scheme in {"http", "ws"} and f"{parts.hostname}:{parts.port}" == self.origin

    def _event(self, message: dict[str, Any]) -> None:
        method, params = message.get("method"), message.get("params") or {}
        if method == "Fetch.requestPaused":
            url = str((params.get("request") or {}).get("url") or "")
            if self._allowed(url):
                self._write("Fetch.continueRequest", {"requestId": params["requestId"]})
            else:
                self.violations.append(f"the page tried to reach {urlsplit(url).netloc or url[:60]}, which is not its own server")
                self._write("Fetch.failRequest", {"requestId": params["requestId"], "errorReason": "BlockedByClient"})
        elif method == "Page.fileChooserOpened":
            self.violations.append("the page tried to open a file chooser; file upload is not allowed in a screen script")
        elif method == "Page.javascriptDialogOpening":
            self.notes.append("a page dialog was dismissed: " + str(params.get("message", ""))[:120])
            self._write("Page.handleJavaScriptDialog", {"accept": False})
        elif method == "Target.targetCreated":
            info = params.get("targetInfo") or {}
            if info.get("type") == "page" and info.get("targetId") != self.target_id:
                self.violations.append("the page tried to open a new window")
                self._write("Target.closeTarget", {"targetId": info.get("targetId")})
        elif method in {"Page.downloadWillBegin", "Browser.downloadWillBegin"}:
            self.violations.append("the page tried to download a file; downloads are not allowed in a screen script")

    def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        ident = self._write(method, params)
        while True:
            message = self._message()
            if message.get("id") == ident:
                if "error" in message:
                    raise RuntimeError(f"browser inspection failed: {message['error']}")
                return message.get("result") or {}
            self._event(message)

    def pump(self, seconds: float) -> None:
        """Handle the page's pending events (requests are paused until they are answered)."""
        end = time.monotonic() + max(0.0, seconds)
        while time.monotonic() < end:
            ready, _, _ = select.select([self.socket], [], [], min(0.1, max(0.0, end - time.monotonic())))
            if self.buffer or ready:
                self._event(self._message())

    def evaluate(self, expression: str) -> Any:
        result = self.call("Runtime.evaluate", {"expression": expression, "returnByValue": True, "awaitPromise": False})
        if result.get("exceptionDetails"):
            details = result["exceptionDetails"]
            raise RuntimeError("the page script failed: " + str((details.get("exception") or {}).get("description") or details.get("text"))[:200])
        return (result.get("result") or {}).get("value")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True)


def run_script(
    url: str, steps: list[dict[str, Any]], runtime: Path, *, timeout: float = WALL_SECONDS,
    authorized: Callable[[], bool] = lambda: True,
) -> dict[str, Any]:
    """Run validated steps; returns the step results and evidence bytes. Raises only when no step could run."""
    ba.validate_url(url)
    start = time.monotonic()
    deadline = start + timeout
    parts = urlsplit(url)
    origin = f"{parts.hostname}:{parts.port}"
    results: list[dict[str, Any]] = []
    pictures: list[bytes] = []
    session = ba.launch(url, runtime, extra_args=list(BROWSER_ARGS))
    driver: _Driver | None = None
    error = ""
    final_text = ""
    try:
        port, path = ba._devtools(runtime, deadline, session.process)
        driver = _Driver(port, path, timeout=max(1.0, deadline - time.monotonic()), origin=origin)
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        connection.request("GET", "/json/list")
        targets = json.loads(connection.getresponse().read())
        connection.close()
        driver.target_id = next((item.get("id", "") for item in targets if item.get("type") == "page"), "")
        for method, params in (
            ("Page.enable", {}), ("Fetch.enable", {"patterns": [{"urlPattern": "*"}]}),
            ("Page.setInterceptFileChooserDialog", {"enabled": True}),
            ("Target.setDiscoverTargets", {"discover": True}),
        ):
            driver.call(method, params)
        try:
            driver.call("Browser.setDownloadBehavior", {"behavior": "deny"})
        except RuntimeError:
            pass   # older browsers: downloads are still reported by the events above
        driver.call("Page.navigate", {"url": url})   # the first load runs under the same interception
        _wait_ready(driver, deadline)
        for number, step in enumerate(steps, 1):
            kind, value = next(iter(step.items()))
            outcome = {"index": number, "kind": kind, "step": step, "ok": False, "reason": ""}
            results.append(outcome)
            if not authorized():
                outcome["reason"] = "stopped: the agent is no longer allowed to run board commands (paused or replaced)"
                break
            if time.monotonic() >= deadline:
                outcome["reason"] = f"the script ran longer than {int(timeout)} seconds"
                break
            try:
                driver.socket.settimeout(max(1.0, deadline - time.monotonic()))
                ok, reason = _run_step(driver, kind, value, url, deadline)
            except (RuntimeError, OSError, ValueError) as step_error:
                ok, reason = False, str(step_error)[:300]
            driver.pump(0.2)
            if driver.violations:
                ok, reason = False, driver.violations[0]
            outcome["ok"], outcome["reason"] = ok, reason
            try:
                final_text = _normal(driver.evaluate(READ_TEXT) or "")
                pictures.append(base64.b64decode(driver.call("Page.captureScreenshot", {"format": "png"})["data"]))
            except (RuntimeError, OSError, ValueError) as capture_error:
                final_text = final_text or ""
                if ok:
                    outcome["ok"], outcome["reason"] = False, "could not read the page after the step: " + str(capture_error)[:200]
                pictures.append(b"")
            outcome["text"] = final_text[:STEP_TEXT_LIMIT]
            outcome["text_sha256"] = hashlib.sha256(final_text.encode("utf-8")).hexdigest()
            if not outcome["ok"]:
                break
    except (RuntimeError, OSError, ValueError) as failure:
        if not results:
            ba._report_blocked(str(failure)[:600])
            raise
        error = str(failure)[:300]
    finally:
        if driver is not None:
            driver.close()
        session.close(validate=False)
    passed = len(results) == len(steps) and all(item["ok"] for item in results) and not error
    return {
        "steps": results, "all_passed": passed, "final_text": final_text, "pictures": pictures,
        "notes": list(driver.notes) if driver else [], "error": error,
        "violations": list(driver.violations) if driver else [],
        "seconds": round(time.monotonic() - start, 1),
    }


def _wait_ready(driver: _Driver, deadline: float, limit: float = STEP_WAIT_SECONDS) -> None:
    end = min(deadline, time.monotonic() + limit)
    while time.monotonic() < end:
        driver.pump(0.15)
        try:
            if driver.evaluate("document.readyState") == "complete":
                driver.pump(0.15)
                return
        except RuntimeError:
            pass   # the page is mid-navigation; the next poll sees the new one


def _run_step(driver: _Driver, kind: str, value: Any, url: str, deadline: float) -> tuple[bool, str]:
    if kind == "open":
        target = urljoin(url, value)
        parts = urlsplit(target)
        if f"{parts.hostname}:{parts.port}" != driver.origin or parts.scheme != "http":
            return False, "open may only go to a page of the same server (same host and port) as --url"
        driver.call("Page.navigate", {"url": target})
        _wait_ready(driver, deadline)
        return True, ""
    if kind == "expect":
        wanted = _normal(value).casefold()
        end = min(deadline, time.monotonic() + STEP_WAIT_SECONDS)
        while True:
            driver.pump(0.2)
            text = _normal(driver.evaluate(READ_TEXT) or "")
            if wanted in text.casefold():
                return True, ""
            if time.monotonic() >= end or driver.violations:
                return False, f"the text was not visible within {int(STEP_WAIT_SECONDS)} seconds: {value[:80]}"
    spec = value if kind == "click" else {"selector": value["selector"]}
    located = driver.evaluate(f"({LOCATE})({_json(spec)})") or {}
    if not located.get("ok"):
        return False, str(located.get("reason") or "the element could not be used")
    x, y = float(located["x"]), float(located["y"])
    if kind == "type":
        if located["tag"] == "input" and located["type"] not in TEXT_INPUT_TYPES or located["tag"] not in {"input", "textarea"}:
            return False, f"typing is supported in text inputs and textareas, not in <{located['tag']}{' type=' + located['type'] if located['type'] else ''}>"
        driver.evaluate(
            "(() => { const el = document.querySelector(%s); el.focus();"
            " const proto = el.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;"
            " Object.getOwnPropertyDescriptor(proto, 'value').set.call(el, '');"
            " el.dispatchEvent(new Event('input', {bubbles: true})); })()" % _json(value["selector"])
        )
        if value["text"]:
            driver.call("Input.insertText", {"text": value["text"]})
        driver.pump(0.2)
        return True, ""
    for event in ("mouseMoved", "mousePressed", "mouseReleased"):
        driver.call("Input.dispatchMouseEvent", {
            "type": event, "x": x, "y": y, "button": "none" if event == "mouseMoved" else "left", "clickCount": 0 if event == "mouseMoved" else 1,
        })
    driver.pump(0.3)
    _wait_ready(driver, deadline, limit=2.0)
    return True, ""
