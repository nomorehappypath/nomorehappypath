# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Shared helpers for the Mission Control rendered tests (headless Chrome against the real manager and board servers)."""
from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

from harness import board_viewer, browser_acceptance, project_manager, project_registry as registry
from tests.environment_support import require_loopback
from tests.test_branding_rendered import probe_proxy

PREAMBLE = r"""
const wait = ms => new Promise(resolve => setTimeout(resolve, ms));
const until = async (test, tries = 200) => { for (let i = 0; i < tries; i++) { try { const value = test(); if (value) return value; } catch (error) {} await wait(100); } return null; };
const out = {errors: []};
window.addEventListener('error', event => out.errors.push(String(event.message)));
const lum = ([r, g, b]) => { const f = v => { v /= 255; return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); }; return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b); };
const parseColor = c => { const p = c.match(/rgba?\(([^)]+)\)/)[1].split(',').map(Number); return {rgb: p.slice(0, 3), a: p.length > 3 ? p[3] : 1}; };
const backgroundOf = node => { for (let n = node; n; n = n.parentElement) { const c = parseColor(getComputedStyle(n).backgroundColor); if (c.a > 0.5) return c.rgb; } return [255, 255, 255]; };
const contrastOf = node => { const a = lum(parseColor(getComputedStyle(node).color).rgb), b = lum(backgroundOf(node)); return (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05); };
const lowContrast = selectors => { const bad = []; for (const selector of selectors) for (const node of document.querySelectorAll(selector)) { if (node.disabled || !node.textContent.trim() || node.getBoundingClientRect().width === 0) continue; const ratio = contrastOf(node); if (ratio < 4.5) bad.push([selector, node.textContent.trim().slice(0, 30), Math.round(ratio * 100) / 100]); } return bad; };
const send = () => fetch('/__probe__', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(out)});
"""


def probe_script(body: str) -> str:
    return "<script>(async () => {" + PREAMBLE + "try {" + body + "} catch (error) { out.error = String(error && error.stack || error); } await send(); })();</script>"


class RenderedCase(unittest.TestCase):
    """setUp skips when there is no acceptance browser; `run` serves a handler behind the probe proxy and returns what the page reported."""

    def setUp(self):
        try:
            browser_acceptance.resolve_binary()
        except (FileNotFoundError, ValueError) as error:
            raise unittest.SkipTest(str(error)) from error
        require_loopback()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)

    def serve(self, handler) -> str:
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return f"http://127.0.0.1:{server.server_address[1]}"

    def run_probe(self, target: str, body: str, *, width=1280, height=1000, wait=90, path="/") -> dict:
        sink: dict = {}
        proxy = self.serve(probe_proxy(target, sink, probe_script(body)))
        profile = tempfile.TemporaryDirectory()
        self.addCleanup(profile.cleanup)
        process = browser_acceptance.launch(proxy + path, Path(profile.name), width=width, height=height)
        try:
            deadline = time.monotonic() + wait
            while time.monotonic() < deadline and "value" not in sink:
                time.sleep(0.1)
        finally:
            process.close()
        result = sink.get("value")
        self.assertIsNotNone(result, "Chrome reported nothing")
        self.assertNotIn("error", result, result.get("error"))
        self.assertEqual(result["errors"], [], "the page raised errors")
        return result

    # ---- the manager (Projects, Settings) ----
    def manager_url(self, home: Path) -> str:
        manager = project_manager.ProjectManager(home, board_port=0)
        self.manager = manager
        return self.serve(project_manager.make_handler(manager))

    def project(self, home: Path, name: str, state: dict | None, *, description: str = "") -> dict:
        code = self.base / "code" / name.replace(" ", "-").lower()
        code.mkdir(parents=True, exist_ok=True)
        entry = registry.register(home, name, code, kind="scaffold", description=description)
        if state is not None:
            directory = Path(entry["data_root"]) / "board"
            directory.mkdir(parents=True, exist_ok=True)
            (directory / "state.json").write_text(json.dumps(state), encoding="utf-8")
        return entry


def dev(task: str, **extra) -> dict:
    return {"id": "dev-1", "role": "engineering", "vendor": "OpenAI", "active": True, "task": task, "session_id": "s1", **extra}


QA = {"id": "qa-1", "role": "qa", "vendor": "Anthropic", "active": True, "task": "REVIEW_QUEUE", "session_id": "s2"}


def confirmation(task: str, count: int = 6) -> dict:
    return {task: {"text": "agreed", "contract_check": {"checks": [{"criterion_index": index} for index in range(1, count + 1)]}}}


def ready_project_state(task: str = "WEATHER") -> dict:
    return {"task_owner_directions": {task: "x"}, "release_decisions": {}, "agents": {"dev-1": dev(task), "qa-1": QA},
            "events": [{"kind": "task_begun", "task": task, "at": "2026-10-27T09:00:00+00:00"}],
            "task_briefs": {task: {"update": "Done.", "updated_at": "2026-10-27T09:50:00+00:00"}}, "requirement_confirmations": confirmation(task),
            "qa_requests": {"r9": {"id": "r9", "task": task, "status": "passed", "phase": "final_acceptance", "stage": "independent_review", "cycle": 1,
                                   "claimed_by": "qa-1", "requested_at": "2026-10-27T09:40:00+00:00"}},
            "releases": {task: {"status": "VISUAL_TEST_REQUIRED", "head_commit": "abc", "recorded_at": "2026-10-27T09:55:00+00:00"}}}


def repair_project_state(task: str = "SKY") -> dict:
    return {"task_owner_directions": {task: "x", "OLD": "y"}, "release_decisions": {"OLD": {"decision": "accepted"}},
            "events": [{"kind": "task_begun", "task": task, "at": "2026-10-27T08:00:00+00:00"}],
            "agents": {"dev-1": dev(task), "qa-1": QA}, "task_briefs": {task: {"update": "Fixing planets below the horizon.", "updated_at": "2026-10-27T10:00:00+00:00"}},
            "requirement_confirmations": confirmation(task),
            "qa_requests": {"r1": {"id": "r1", "task": task, "status": "failed", "phase": "chunk", "stage": "independent_review", "cycle": 1, "review_round": 1,
                                   "structured_findings": [{"id": "B1"}, {"id": "B2"}], "requested_at": "2026-10-27T09:00:00+00:00", "completed_at": "2026-10-27T09:30:00+00:00"}}}


def empty_project_state() -> dict:
    return {"task_owner_directions": {}, "release_decisions": {}, "agents": {}, "events": []}


def paused_project_state() -> dict:
    return {"task_owner_directions": {"M": "x"}, "release_decisions": {}, "agents": {}, "events": [], "project_pause": {"status": "paused"}}
