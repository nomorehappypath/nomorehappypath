# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Tests that exist before the build for an app with screens (task 2026-11-24_tdd-walks).

A walk is the steps a user takes on the app's screens and what he must then see, written as data by the Reviewer before any
code exists. It is stored as one of the requirement's checks (its one file is the walk), so everything the board does with a
check — storage, digest, correction, coverage, the owner's card — applies to it. NMHP runs it itself: it starts the app, with a
free port in PORT and a new empty folder in DATA_DIR, and takes the steps in the harness browser (`screen_script`).
"""
from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import tempfile
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable

from harness import environment_check, owner_env, screen_script, test_env, workspace_settings

FILE = "nmhp-walk.json"
RUNS = "the app's own screens, in the browser"
NOTHING_BUILT = "nothing is built yet: there is no app here that NMHP knows how to start (start.sh, app.py, server.py, main.py or npm start)"
# The steps that use a control of the page, and the words that name it.
CONTROL_STEPS = {"click": "text", "fill": "field", "choose": "field", "upload": "field"}


def is_walk(check: dict[str, Any]) -> bool:
    """This stored check is a walk (read from its files, so it holds wherever the check was copied)."""
    return set(check.get("files") or {}) == {FILE}


def _quote(text: Any) -> str:
    return "“" + str(text) + "”"


def sentence(steps: list[dict[str, Any]]) -> str:
    """The walk in plain words for the owner, written from its steps."""
    parts = []
    for step in steps:
        kind, value = next(iter(step.items()))
        if kind == "open":
            parts.append("Open the app." if value == "/" else f"Open the page {value}.")
        elif kind == "click":
            parts.append(f"Click {_quote(value['text'])}.")
        elif kind == "fill":
            parts.append(f"Type {_quote(value['text'])} in {_quote(value['field'])}.")
        elif kind == "choose":
            parts.append(f"Choose {_quote(value['option'])} in {_quote(value['field'])}.")
        elif kind == "upload":
            parts.append("Upload " + ", ".join(value["files"]) + (f" in {_quote(value['field'])}." if value["field"] else "."))
        elif kind == "wait_for":
            parts.append(f"Within {value['seconds']} seconds, see {_quote(value['text'])}.")
        else:
            parts.append(f"See {_quote(value)}.")
    return " ".join(parts)


def parse(raw: Any, where: str) -> dict[str, Any]:
    """One walk of a submission as the check it is stored as; ValueError with the plain reason."""
    if isinstance(raw, list):
        raw = {"steps": raw}
    if not isinstance(raw, dict):
        raise ValueError(f"{where}: a walk is {{\"steps\": [...], \"files\": {{\"note.txt\": \"<text>\"}}}} (files optional)")
    files = raw.get("files") or {}
    if not isinstance(files, dict) or not all(
            isinstance(body, str) and isinstance(name, str) and name and Path(name).name == name and name not in {".", ".."}
            for name, body in files.items()):
        raise ValueError(f"{where}: a walk's files are plain file names, each with its text")
    try:
        steps = screen_script.parse_steps(json.dumps(raw.get("steps")))
    except screen_script.ScriptError as error:
        raise ValueError(f"{where}: {error}") from None
    for number, step in enumerate(steps, 1):
        kind, value = next(iter(step.items()))
        if kind == "type" or (kind == "click" and "selector" in value):
            raise ValueError(f"{where} step {number}: a walk names what the user sees, never a selector (click by text; fill a field by its label)")
    return {"coverage": "checked", "runs": RUNS, "ledger": sentence(steps), "walk": sentence(steps),
            "files": {FILE: json.dumps({"steps": steps, "files": files}, sort_keys=True, ensure_ascii=False)}}


def content(check: dict[str, Any]) -> dict[str, Any]:
    """The stored walk: its steps and the small text files it uploads."""
    return json.loads(check["files"][FILE])


def _normal(text: Any) -> str:
    return " ".join(str(text or "").split()).casefold()


class _Words(HTMLParser):
    """The words an agreed screen shows: every element's own text, placeholders, labels and the text of buttons."""

    VOID = {"input", "br", "img", "hr", "meta", "link", "source", "wbr", "col", "area"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.words: set[str] = set()
        self.open: list[tuple[str, list[str]]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        for name, value in attrs:
            if name in {"placeholder", "aria-label", "value"} and value:
                self.words.add(_normal(value))
        if tag not in self.VOID:
            self.open.append((tag, []))

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag not in self.VOID:
            self.handle_endtag(tag)

    def handle_data(self, data: str) -> None:
        if data.strip() and not any(tag in {"style", "title"} for tag, _ in self.open):
            self.words.add(_normal(data))
        for _tag, texts in self.open:
            texts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if not any(opened == tag for opened, _ in self.open):
            return
        while self.open:
            opened, texts = self.open.pop()
            if opened in {"button", "a", "label", "summary", "option", "legend", "span", "div", "li", "td", "th", "p", "h1", "h2", "h3", "h4"}:
                self.words.add(_normal("".join(texts)))
            if opened == tag:
                return


def screen_words(screens: list[bytes]) -> set[str]:
    """Every control word of the agreed screens."""
    words: set[str] = set()
    for data in screens:
        reader = _Words()
        reader.feed(data.decode("utf-8", "replace"))
        reader.close()
        words |= reader.words
    return words - {""}


def label_problem(check: dict[str, Any], words: set[str]) -> str:
    """The first control of the walk that no agreed screen shows, as a plain reason ("" when every control is there). What the
    walk itself typed or uploaded earlier may be clicked; an upload with no label means the page's only file field."""
    own: set[str] = set()
    for number, step in enumerate(content(check)["steps"], 1):
        kind, value = next(iter(step.items()))
        label = _normal(value.get(CONTROL_STEPS[kind])) if kind in CONTROL_STEPS else ""
        if label and label not in words and label not in own and not (kind != "click" and any(word.startswith(label) for word in words)):
            return (f"step {number} uses {_quote(value[CONTROL_STEPS[kind]])}, which no agreed screen shows. Use the exact words of the screens "
                    "(a button, a link, a field's label or placeholder), or ask for the screen to show it")
        if kind == "choose" and _normal(value["option"]) not in words:
            return f"step {number} chooses {_quote(value['option'])}, which no agreed screen shows as an option"
        if kind == "fill":
            own.add(_normal(value["text"]))
        if kind == "upload":
            own |= {_normal(name) for name in value["files"]}
    return ""


def budget(steps: list[dict[str, Any]]) -> float:
    """The time one walk may take: today's allowance plus its own waits, never more than a real run (Codex plan check R1-1)."""
    waits = sum(step["wait_for"]["seconds"] for step in steps if "wait_for" in step)
    return min(environment_check.REAL_RUN_SECONDS, screen_script.WALL_SECONDS + waits)


def upload_files(source: Path, needs: dict[str, Any] | None) -> dict[str, str]:
    """The owner's files a step may upload, by name: the setup list's real_data (a project file is read in this copy)."""
    found: dict[str, str] = {}
    for item in (needs or {}).get("real_data") or []:
        path = Path(os.path.expanduser(str(item)))
        path = path if path.is_absolute() else source / path
        for one in sorted(path.iterdir()) if path.is_dir() else [path]:
            if one.is_file():
                found.setdefault(one.name, str(one))
    return found


def run(root: Any, source: Path, check: dict[str, Any], needs: dict[str, Any] | None, evidence: Path,
        authorized: Callable[[], bool] = lambda: True) -> dict[str, Any]:
    """Start the app in `source`, take the walk's steps, stop the app. {"status": passed | failed | could_not_run, "reason",
    "steps", "picture"}: `failed` is about the app, `could_not_run` about this computer."""
    from harness import release_preview      # it imports the board, which imports this module
    walk = content(check)
    steps = walk["steps"]
    settings = workspace_settings.load(root).get("preview") or {}
    command = str(settings.get("command") or "").strip() or str((release_preview.detect_start(source) or {}).get("command") or "")
    if not command:
        return {"status": "failed", "reason": NOTHING_BUILT, "steps": [], "picture": ""}
    try:
        values = owner_env.read()
    except owner_env.OwnerEnvError as error:
        return {"status": "could_not_run", "reason": f"your own setup could not be read: {error}", "steps": [], "picture": ""}
    scrub = owner_env.redactor(values)
    folder = Path(tempfile.mkdtemp(prefix="nmhp-walk-"))
    preview = release_preview.Preview("walk", "", folder)
    try:
        (folder / "files").mkdir()
        for name, body in walk["files"].items():
            (folder / "files" / name).write_text(body, encoding="utf-8")
        uploads = {**upload_files(source, needs), **{name: str(folder / "files" / name) for name in walk["files"]}}
        data, shims = folder / "data", folder / "bin"
        data.mkdir()
        environment = release_preview.preview_environment(os.environ, {"BROWSER": "true"}, values)
        declared = test_env.resolve(root, source)
        environment = test_env.apply(environment, declared)
        hide = lambda text: scrub(test_env.scrub(text, declared))
        environment_check.write_recorders(shims, folder / "calls", list((needs or {}).get("ai_tools") or []), values, environment_check.test_models())
        preview.port = release_preview._free_port()
        preview.url = str(settings.get("url_template") or "http://127.0.0.1:{port}/").replace("{port}", str(preview.port))
        # applied after everything declared, so a walk never reaches the project's own port or the owner's own data
        environment.update({"PORT": str(preview.port), "DATA_DIR": str(data), "PATH": str(shims) + os.pathsep + environment.get("PATH", "")})
        rendered = command.replace("{port}", str(preview.port)).replace("{state_dir}", shlex.quote(str(data)))
        with preview.log_path.open("ab") as log:
            preview.process = subprocess.Popen(rendered, shell=True, cwd=source, env=environment, stdin=subprocess.DEVNULL,
                                               stdout=log, stderr=log, start_new_session=True)
        preview.watch_group()
        problem = release_preview.ReleasePreviewSupervisor._await_health(None, preview, settings, find_listener=True)  # type: ignore[arg-type]
        if problem:
            return {"status": "failed", "reason": hide(f"the app did not start with `{command}`: {problem}. {release_preview._log_tail(preview.log_path)}"),
                    "steps": [], "picture": ""}
        try:
            result = screen_script.run_script(preview.url, steps, folder / "browser", timeout=budget(steps), authorized=authorized, uploads=uploads)
        except (OSError, RuntimeError, ValueError) as error:
            return {"status": "could_not_run", "reason": f"the harness browser could not open the app: {str(error)[:300]}", "steps": [], "picture": ""}
        evidence.mkdir(parents=True, exist_ok=True)
        (evidence / "steps.json").write_text(hide(json.dumps([{key: item.get(key) for key in ("index", "kind", "step", "ok", "reason", "text")}
                                                             for item in result["steps"]], indent=2, ensure_ascii=False)), encoding="utf-8")
        picture = ""
        for item, data_bytes in zip(result["steps"], result["pictures"]):
            if data_bytes:
                path = evidence / f"step{item['index']:02d}.png"
                path.write_bytes(data_bytes)
                picture = str(path)
        failed = next((item for item in result["steps"] if not item["ok"]), None)
        reason = ""
        if not result["all_passed"]:
            reason = (f"step {failed['index']} ({sentence([failed['step']])}) did not work: {failed['reason']}. The page showed: {hide(failed.get('text') or '')[:600]}"
                      if failed else f"the walk did not finish: {result['error']}")
        return {"status": "passed" if result["all_passed"] else "failed", "reason": reason, "steps": len(result["steps"]), "picture": picture}
    except test_env.TestEnvError as error:
        return {"status": "could_not_run", "reason": str(error), "steps": [], "picture": ""}
    finally:
        preview.stop()
        shutil.rmtree(folder, ignore_errors=True)
