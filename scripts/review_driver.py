#!/usr/bin/env python3
# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Drive the whole real-agent path through the app's own HTTP API: no browser, no person.

Owner's order, 2026-10-03 21:10: "a human cannot be available any time, all the
time." A review runs every owner step through the same endpoints the page
calls - create and open a project, launch the agents, give the direction,
Go ahead, pause and resume, Accept, Stop all - and reads the result back,
including the rendered page (headless Chrome's DOM, no clicking). Agents still
open their own visible terminals, as for the owner; nothing is typed into them.

    python3 <app checkout>/scripts/review_driver.py run --app http://127.0.0.1:<port> --home <manager home> \\
        --parent <folder for the new project> --name review-p1 --evidence <evidence folder> --pause-resume

Every step also runs alone (create, open, launch, direct, go-ahead, pause,
resume, wait-release, accept, rendered, stop-all, check-clean, close, status);
each writes <step>.json into --evidence and prints one line. A step that cannot
finish says exactly why - an agent waiting for a person is reported by name
and reason, never waited on in silence.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DEFAULT_DIRECTION = (
    "Create salute.py with a function salute(name) that returns 'Salute, <name>!' and a unittest file "
    "test_salute.py that checks it. No other files. Keep it minimal."
)
KINDS = ("codex_delivery", "claude_cto", "claude_reviewer")
ATTENTION_GRACE_SECONDS = 90            # a screen that asks a person for longer than this fails the step


class StepFailed(RuntimeError):
    pass


class Driver:
    def __init__(self, app: str, evidence: Path, home: Path | None):
        self.app = app.rstrip("/")
        self.evidence = evidence
        self.evidence.mkdir(parents=True, exist_ok=True)
        self.home = home
        self.started = time.monotonic()

    # -- HTTP ------------------------------------------------------------------
    def call(self, method: str, path: str, body: dict | None = None, *, timeout: float = 120) -> Any:
        data = None if body is None else json.dumps(body).encode("utf-8")
        request = urllib.request.Request(self.app + path, data=data, method=method)
        if body is not None:
            request.add_header("Content-Type", "application/json")
        if method != "GET":
            request.add_header("Origin", self.app)            # the page's own origin; the app refuses any other
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as error:
            text = error.read().decode("utf-8", "replace")
            raise StepFailed(f"{method} {path} -> HTTP {error.code}: {text[:500]}") from None
        try:
            return json.loads(raw)
        except ValueError:
            return raw

    def board(self) -> dict:
        return self.call("GET", "/project/api/board")["state"]

    def control(self) -> dict:
        return self.call("GET", "/project/api/control")

    # -- evidence --------------------------------------------------------------
    def record(self, step: str, value: Any) -> Any:
        (self.evidence / f"{step}.json").write_text(json.dumps(value, indent=2, default=str) + "\n", encoding="utf-8")
        elapsed = time.monotonic() - self.started
        print(f"[{elapsed:7.1f}s] {step}: {summary(value)}", flush=True)
        return value

    def waiting_for_a_person(self) -> list[dict]:
        """Agents whose screen asks a person for something, and for how long."""
        try:
            rows = self.control().get("sessions", [])
        except StepFailed:
            return []
        return [{"session": row.get("id"), "kind": row.get("kind"), "reason": row.get("attention_reason"),
                 "since": row.get("attention_since")}
                for row in rows if row.get("attention_reason") and row.get("status") in {"launching", "running"}]

    def wait(self, step: str, check: Callable[[], Any], timeout: float, poll: float = 5) -> Any:
        """Poll until check() returns a value; fail on timeout or on an agent waiting for a person."""
        deadline = time.monotonic() + timeout
        asking_since: dict[str, float] = {}
        last_note = 0.0
        while True:
            value = check()
            if value:
                return value
            now = time.monotonic()
            for row in self.waiting_for_a_person():
                first = asking_since.setdefault(f"{row['session']}:{row['reason']}", now)
                if now - first > ATTENTION_GRACE_SECONDS:
                    self.record(step + "-blocked", {"waiting_for_a_person": row})
                    raise StepFailed(f"{step}: agent {row['session']} ({row['kind']}) is waiting for a person: {row['reason']}")
            if now > deadline:
                raise StepFailed(f"{step}: not reached within {int(timeout)} s")
            if now - last_note > 60:
                print(f"[{now - self.started:7.1f}s] {step}: waiting...", flush=True)
                last_note = now
            time.sleep(poll)

    # -- steps -----------------------------------------------------------------
    def create(self, parent: str, name: str, description: str) -> dict:
        entry = self.call("POST", "/api/projects", {"kind": "scaffold", "parent_root": os.path.expanduser(parent),
                                                    "name": name, "description": description})
        return self.record("create", entry)

    def open(self, project: str) -> dict:
        value = self.call("POST", f"/api/projects/{project}/open", {}, timeout=300)
        self.wait("open", lambda: self._ready(), 120, poll=1)
        return self.record("open", value)

    def _ready(self) -> bool:
        try:
            return bool(self.call("GET", "/project/api/ready").get("ready"))
        except (StepFailed, AttributeError, OSError):
            return False

    def launch(self, kinds: tuple[str, ...] = KINDS) -> dict:
        launched = {kind: self.call("POST", "/project/api/sessions", {"kind": kind})["session"]["id"] for kind in kinds}

        def attached():
            rows = {row.get("id"): row for row in self.control().get("sessions", [])}
            failed = [f"{session}: {rows[session].get('reason')}" for session in launched.values()
                      if rows.get(session, {}).get("status") in {"failed", "exited", "stopped"}]
            if failed:
                raise StepFailed(f"launch: an agent did not start - {failed}")
            return all(rows.get(session, {}).get("status") == "running" for session in launched.values())
        self.wait("launch", attached, 240)
        return self.record("launch", launched)

    def delivery_agent(self) -> str:
        def find():
            agents = self.board().get("agents", {})
            return next((agent_id for agent_id, agent in agents.items()
                         if agent.get("role") == "engineering" and agent.get("active")), "")
        return self.wait("delivery-agent", find, 300)

    def direct(self, text: str) -> dict:
        agent = self.delivery_agent()
        value = self.call("POST", f"/project/api/agents/{agent}/owner-message", {"text": text, "message_type": "direction"})
        return self.record("direct", {"agent": agent, "response": value})

    def go_ahead(self, task: str = "") -> dict:
        def awaiting():
            proposals = self.board().get("requirement_proposals", {})
            return next((name for name, proposal in proposals.items()
                         if proposal.get("status") == "awaiting_owner" and (not task or name == task)), "")
        name = self.wait("requirements-proposal", awaiting, 1800)
        value = self.call("POST", f"/project/api/tasks/{name}/requirements-decision", {"decision": "go_ahead"})
        return self.record("go-ahead", {"task": name, "response": value})

    def pause(self, project: str) -> dict:
        return self.record("pause", self.call("POST", f"/api/projects/{project}/pause", {}, timeout=600))

    def resume(self, project: str) -> dict:
        value = self.call("POST", f"/api/projects/{project}/resume", {}, timeout=600)
        self.wait("resume", lambda: self._ready(), 180, poll=2)
        return self.record("resume", value)

    def wait_release(self, task: str = "", timeout: float = 5400) -> dict:
        def released():
            releases = self.board().get("releases", {})
            return next(({"task": name, "release": release} for name, release in releases.items()
                         if release.get("status") == "VISUAL_TEST_REQUIRED" and (not task or name == task)), None)
        return self.record("release-ready", self.wait("release-ready", released, timeout, poll=15))

    def accept(self, task: str) -> dict:
        value = self.call("POST", f"/project/api/releases/{task}/decision", {"decision": "accepted"})

        def settled():
            state = self.board()
            accepted = (state.get("releases", {}).get(task, {}).get("status") == "ACCEPTED")
            stopped = any(event.get("kind") == "delivery_stopped_after_acceptance" and event.get("task") == task
                          for event in state.get("events", []))
            # The terminal stop is owed only while a Delivery agent still holds the task.
            delivery_live = any(agent.get("role") == "engineering" and agent.get("task") == task and agent.get("active")
                                for agent in state.get("agents", {}).values())
            if accepted and (stopped or not delivery_live):
                return {"accepted": True, "delivery_stopped_after_acceptance": stopped}
            return None
        return self.record("accept", {"response": value, "settled": self.wait("accept", settled, 300)})

    def rendered(self, task: str = "") -> dict:
        """What the owner sees after Accept, read by headless Chrome (visible text only, no clicking).

        The Delivery card reads "TASK ACCEPTED" only while its terminal is
        closing; once closed, the card leaves the live list and the task is in
        Task history as OWNER ACCEPTED. So the page must never show a Delivery
        card stuck starting, and the history must carry the owner's acceptance.
        """
        from harness import browser_acceptance
        with tempfile.TemporaryDirectory(prefix="review-driver-page-") as runtime:
            page = browser_acceptance.render_page(self.app + "/project/", Path(runtime), timeout=90)
        (self.evidence / "rendered.html").write_text(page["html"], encoding="utf-8")
        (self.evidence / "rendered-visible.txt").write_text(page["text"], encoding="utf-8")
        (self.evidence / "rendered.png").write_bytes(page["png"])
        stuck = [phrase for phrase in ("Waiting to attach", "Terminal is starting") if phrase in page["text"]]
        history = self.call("GET", "/project/api/history").get("task_history", [])
        accepted = [row.get("task") for row in history if row.get("result") == "OWNER ACCEPTED" and (not task or row.get("task") == task)]
        value = self.record("rendered", {"stuck_cards": stuck, "accepted_in_history": accepted,
                                         "task_accepted_card_visible": "TASK ACCEPTED" in page["text"]})
        if stuck or not accepted:
            raise StepFailed(f"rendered page: stuck cards {stuck}; accepted in task history {accepted}")
        return value

    def directives(self, moment: str) -> dict:
        """Stage 4: each live agent's rules are in its CLI's system layer, read from the processes themselves.

        Claude: `--append-system-prompt <rules>` and `--system-prompt-snapshot off`.
        Codex: the app-server carries `developer_instructions=<rules>`, and the
        window's own line carries none of it (its first message is the short
        kickoff). Run after launch and again after a resume: a resumed agent
        must have its full rules back, not only the recovery note.
        """
        titles = {"codex_delivery": "# Agent Directive", "claude_reviewer": "# Agent Directive", "claude_cto": "# CTO Directive"}

        def running():
            rows = [row for row in self.control().get("sessions", []) if row.get("kind") in titles]
            return rows if rows and all(row.get("status") == "running" and row.get("pid") for row in rows) else None
        rows = self.wait(f"directives-{moment}", running, 180, poll=2)
        time.sleep(5)                                    # the CLI (and the Codex app-server) start after the supervisor
        tree = {}
        for line in subprocess.run(["ps", "-axo", "pid=,ppid="], capture_output=True, text=True).stdout.splitlines():
            pid, ppid = line.split()
            tree.setdefault(int(ppid), []).append(int(pid))
        report, missing = {}, []
        for row in rows:
            pids, stack = [], [int(row["pid"])]
            while stack:
                pid = stack.pop()
                pids.append(pid)
                stack.extend(tree.get(pid, []))
            argv = {pid: subprocess.run(["ps", "-ww", "-o", "args=", "-p", str(pid)], capture_output=True, text=True).stdout
                    for pid in pids}
            title = titles[row["kind"]]
            if row["kind"].startswith("claude"):
                carrier = [pid for pid, text in argv.items() if f"--append-system-prompt {title}" in text]
                ok = bool(carrier) and all("--system-prompt-snapshot off" in argv[pid] for pid in carrier)
                detail = {"system_prompt_on_cli": bool(carrier), "snapshot_off": ok}
            else:
                server = [pid for pid, text in argv.items() if "app-server" in text and "developer_instructions=" in text and title in text]
                window = [pid for pid, text in argv.items() if "--remote" in text]
                window_clean = bool(window) and not any(title in argv[pid] for pid in window)
                ok = bool(server) and window_clean
                detail = {"developer_instructions_on_app_server": bool(server), "window_line_without_rules": window_clean}
            report[row["id"]] = {"kind": row["kind"], "launch_mode": "resume" if row.get("cli_last_launch_resumed") else "fresh",
                                 "rules_in_system_layer": ok, **detail}
            if not ok:
                missing.append(row["id"])
        value = self.record(f"directives-{moment}", report)
        if missing:
            raise StepFailed(f"directives-{moment}: rules not in the system layer for {missing}")
        return value

    def stop_all(self) -> dict:
        sessions = self.session_ids()
        started = time.monotonic()
        value = self.call("POST", "/project/api/sessions/stop-all", {}, timeout=300)
        deadline = time.monotonic() + 30
        reading = self._clean(sessions)
        while reading["left"] and time.monotonic() < deadline:
            time.sleep(1)
            reading = self._clean(sessions)
        reading["seconds"] = round(time.monotonic() - started, 1)
        self.record("stop-all", {"response": value, "after": reading, "manual_cleanup": False})
        if reading["left"]:
            raise StepFailed(f"Stop all left processes or runtime folders after 30 s: {reading['left']}")
        return reading

    def session_ids(self) -> list[str]:
        try:
            return [str(row.get("id")) for row in self.control().get("sessions", []) if row.get("id")]
        except StepFailed:
            return []

    def _clean(self, sessions: list[str] | None = None) -> dict:
        """What of this app's agent sessions still runs, and its runtime folders.

        Every process of a session - supervisor, CLI, inbox relay, Codex
        app-server - carries HARNESS_MANAGED_SESSION=<id> in its environment,
        so it is counted by that, never by a command line that merely names a
        path (a shell running `grep` would match that).
        """
        sessions = self.session_ids() if sessions is None else sessions
        marks = [f"HARNESS_MANAGED_SESSION={session}" for session in sessions]
        listing = subprocess.run(["ps", "-Eww", "-axo", "pid=,args="], capture_output=True, text=True).stdout.splitlines()
        left = []
        for line in listing:
            mark = next((mark for mark in marks if re.search(re.escape(mark) + r"(\s|$)", line)), None)
            if mark and str(os.getpid()) != line.split(None, 1)[0]:
                command = line.split(None, 1)[1] if " " in line.strip() else line
                left.append(f"{line.split(None, 1)[0]} {mark.split('=', 1)[1]} {command.split(' HARNESS_')[0].split(' TERM=')[0][:120]}")
        if self.home:
            runtime = Path(self.home) / "rt"
            left += [f"runtime folder {path}" for path in (runtime.iterdir() if runtime.is_dir() else [])]
        return {"sessions_checked": len(sessions), "left": left}

    def close(self, project: str) -> dict:
        return self.record("close", self.call("POST", f"/api/projects/{project}/close", {}, timeout=300))


def summary(value: Any) -> str:
    text = json.dumps(value, default=str)
    return text if len(text) <= 220 else text[:217] + "..."


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("step", choices=["run", "create", "open", "launch", "direct", "go-ahead", "pause", "resume",
                                         "wait-release", "accept", "rendered", "stop-all", "check-clean", "close", "status", "directives"])
    parser.add_argument("--app", default="http://127.0.0.1:8750")
    parser.add_argument("--home", required=True, help="the app's manager home (runtime folders are checked under <home>/rt)")
    parser.add_argument("--evidence", required=True)
    parser.add_argument("--project", default="", help="project id (from create)")
    parser.add_argument("--parent", default="", help="folder the new project is created in")
    parser.add_argument("--name", default="")
    parser.add_argument("--description", default="Headless review run")
    parser.add_argument("--direction", default=DEFAULT_DIRECTION)
    parser.add_argument("--task", default="")
    parser.add_argument("--pause-resume", action="store_true", help="run: pause and resume the project after Go ahead")
    parser.add_argument("--release-timeout", type=float, default=5400)
    args = parser.parse_args(argv)
    driver = Driver(args.app, Path(os.path.expanduser(args.evidence)), Path(os.path.expanduser(args.home)))
    try:
        if args.step == "run":
            project = driver.create(args.parent, args.name, args.description)["id"]
            try:
                driver.open(project)
                driver.launch()
                driver.directives("launch")
                driver.direct(args.direction)
                task = driver.go_ahead(args.task)["task"]
                if args.pause_resume:
                    time.sleep(60)
                    driver.pause(project)
                    driver.resume(project)
                    driver.directives("resume")
                driver.wait_release(task, args.release_timeout)
                driver.accept(task)
                driver.rendered(task)
            finally:
                # Whatever happened, nothing is left running: Stop all, the leftover check, close.
                try:
                    driver.stop_all()
                finally:
                    driver.close(project)
            driver.record("result", {"task": task, "project": project, "result": "TASK DONE: the whole path ran with no person and no browser clicks"})
        elif args.step == "create":
            driver.create(args.parent, args.name, args.description)
        elif args.step in {"open", "pause", "resume", "close"}:
            getattr(driver, args.step)(args.project)
        elif args.step == "launch":
            driver.launch()
        elif args.step == "direct":
            driver.direct(args.direction)
        elif args.step == "go-ahead":
            driver.go_ahead(args.task)
        elif args.step == "wait-release":
            driver.wait_release(args.task, args.release_timeout)
        elif args.step == "accept":
            driver.accept(args.task)
        elif args.step == "rendered":
            driver.rendered(args.task)
        elif args.step == "directives":
            driver.directives("now")
        elif args.step == "stop-all":
            driver.stop_all()
        elif args.step == "check-clean":
            driver.record("check-clean", driver._clean())
        elif args.step == "status":
            driver.record("status", {"waiting_for_a_person": driver.waiting_for_a_person(),
                                     "releases": {k: v.get("status") for k, v in driver.board().get("releases", {}).items()}})
    except StepFailed as error:
        driver.record("failed", {"step": args.step, "error": str(error)})
        print(f"FAILED: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
