# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Cost center (task 2026-10-27_cost-center): what each task and project cost in agent work. Information only.

The owner pays by subscription, so there is no bill per task. This module reads the token COUNTS the Claude and Codex programs already
write for sessions NMHP launched, and prices them as "API-equivalent": what the same tokens would cost at the vendors' published prices.
It is a yardstick, never a charge, and never a cut-off, a hold or a question (DEV_DIRECTIVE (ab)).

Privacy: a transcript line is parsed in memory and only the model name, the time, the reply id and four counts are kept. No prompt or reply
text is stored, logged, shown or put in an error message (errors are fixed phrases). The ledger holds ids, model, task, times and counts.

Accounting rules (spec section 12):
- Claude writes one reply several times: rows are de-duplicated by message.id, the last row wins, and the difference goes to the SAME bucket.
- Codex totals are cumulative: a stretch costs the difference between two readings; a counter that went down is a new counting run.
- Usage is keyed by (provider, store, vendor session id), never by managed session, so a hand-over or resume counts once.
- Money is computed when shown, from the current rate card, so an edited card reprices history.
- The CTO is project overhead; usage that fell outside any task is "not on a task". Project = tasks + overhead + not on a task.
- Gemini is never measured and is always said so; anything unmeasured is "not counted", never $0.
"""
from __future__ import annotations

import fcntl
import json
import math
import os
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

LEDGER_NAME = "cost_ledger.json"
LEDGER_VERSION = 1
SHIPPED_CARD = Path(__file__).with_name("rate_card.json")

# Defaults and bounds of the Settings fields (global_settings.PLUMBING_LIMITS routes them; nothing else holds a number).
MAX_BYTES_DEFAULT, MAX_BYTES_LOW, MAX_BYTES_HIGH = 64_000_000, 1_000_000, 2_000_000_000
SECONDS_DEFAULT, SECONDS_LOW, SECONDS_HIGH = 5, 1, 120
CARD_PATH_FIELD = "cost_rate_card_path"
_BLOCK = 1_000_000

SAFE_SESSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{5,127}$")
RATE_KINDS = ("input", "output", "cache_read", "cache_write")
ROLE_WORDS = {"delivery": "builder", "reviewer": "reviewer", "cto": "CTO"}
NOT_A_TASK_PREFIXES = ("REVIEW_", "AWAITING_")
GEMINI_WORDS = "not measured yet - the Gemini program does not record usage in a readable form"

_configured: dict[str, Any] = {}
_memo: dict[str, Any] = {}


def _clock() -> datetime:
    return datetime.now(timezone.utc)


def configure(plumbing: Any) -> None:
    """Install the owner's settings (the project worker passes them once); anything unusable keeps the default."""
    _configured.clear()
    _memo.clear()
    if not isinstance(plumbing, dict):
        return
    for key, name, low, high in (("cost_collect_max_bytes", "bytes", MAX_BYTES_LOW, MAX_BYTES_HIGH),
                                 ("cost_collect_seconds", "seconds", SECONDS_LOW, SECONDS_HIGH)):
        value = plumbing.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and low <= value <= high:
            _configured[name] = value
    path = plumbing.get(CARD_PATH_FIELD)
    if isinstance(path, str) and path.strip():
        _configured["card_path"] = path.strip()


def limits() -> tuple[int, int]:
    return _configured.get("bytes", MAX_BYTES_DEFAULT), _configured.get("seconds", SECONDS_DEFAULT)


# ---- the price list ----------------------------------------------------------------------------------------------------------

def _valid_card(value: Any) -> dict[str, Any]:
    """The card as a clean dict, or ValueError. Rates must be finite and not negative."""
    if not isinstance(value, dict) or not isinstance(value.get("models"), dict) or not value["models"]:
        raise ValueError("no models")
    models: dict[str, dict[str, float]] = {}
    for name, rates in value["models"].items():
        if not isinstance(name, str) or not name or not isinstance(rates, dict):
            raise ValueError("bad model entry")
        clean: dict[str, float] = {}
        for kind in RATE_KINDS:
            number = rates.get(kind, 0.0)
            if isinstance(number, bool) or not isinstance(number, (int, float)) or not math.isfinite(number) or number < 0:
                raise ValueError("bad rate")
            clean[kind] = float(number)
        if "input" not in rates or "output" not in rates:
            raise ValueError("input and output rates are required")
        models[name] = clean
    return {"as_of": str(value.get("as_of") or "unknown date")[:40], "models": models}


def load_card() -> tuple[dict[str, Any], str]:
    """(card, note). The owner's copy wins when it is valid; otherwise the shipped card and one plain line saying why."""
    note = ""
    owner = _configured.get("card_path")
    if owner:
        try:
            return {**_valid_card(json.loads(Path(owner).read_text(encoding="utf-8"))), "owner_copy": True}, ""
        except (OSError, ValueError, TypeError, RecursionError):
            note = "Your own price list could not be used (missing, unreadable or has an invalid price), so the built-in price list is used."
    return {**_valid_card(json.loads(SHIPPED_CARD.read_text(encoding="utf-8"))), "owner_copy": False}, note


def _card_key() -> tuple:
    owner = _configured.get("card_path")
    try:
        stat = Path(owner).stat() if owner else None
    except OSError:
        stat = None
    return (owner, stat.st_mtime_ns if stat else 0, stat.st_size if stat else 0)


def rates_for(card: dict[str, Any], model: str) -> tuple[dict[str, float], bool]:
    """(rates, estimated). Exact name, then the longest prefix, then the highest rate of each kind on the card (estimated)."""
    models = card["models"]
    name = str(model or "")
    if name in models:
        return models[name], False
    best = max((known for known in models if name.startswith(known)), key=len, default="")
    if best:
        return models[best], False
    return {kind: max(rates[kind] for rates in models.values()) for kind in RATE_KINDS}, True


def price(card: dict[str, Any], model: str, counts: list[int]) -> tuple[float, bool]:
    """counts = [input (not cached), output, cache read, cache write]."""
    rates, estimated = rates_for(card, model)
    usd = sum(count * rates[kind] for count, kind in zip(counts, RATE_KINDS)) / 1_000_000
    return usd, estimated


# ---- the ledger --------------------------------------------------------------------------------------------------------------

PROJECT_RECORD_WORDS = "from Claude's project record (the session file was not written; the total is Claude's own end-of-session figure)"


def _empty() -> dict[str, Any]:
    return {"version": LEDGER_VERSION, "sessions": {}, "windows": {}, "replies": {}, "buckets": {}, "limits": {}, "notes": []}


def ledger_path(ledger_dir: Path) -> Path:
    return Path(ledger_dir) / LEDGER_NAME


def _load(ledger_dir: Path) -> tuple[dict[str, Any], str]:
    """(ledger, note). A missing ledger is empty; a corrupt one is moved aside and rebuilt from the files."""
    path = ledger_path(ledger_dir)
    if not path.is_file():
        return _empty(), ""
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or value.get("version") != LEDGER_VERSION:
            raise ValueError("not a ledger")
        for name, kind in (("sessions", dict), ("windows", dict), ("replies", dict), ("buckets", dict), ("limits", dict)):
            if not isinstance(value.get(name), kind):
                raise ValueError("bad section")
        value.setdefault("notes", [])
        return value, ""
    except (OSError, ValueError, RecursionError):
        try:
            os.replace(path, path.with_name(f"cost_ledger.corrupt-{int(time.time())}.json"))
        except OSError:
            pass
        return _empty(), "The cost record was damaged and has been rebuilt from the program files; earlier task splits may be approximate."


def _save(ledger_dir: Path, ledger: dict[str, Any]) -> None:
    path = ledger_path(ledger_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(ledger, handle, separators=(",", ":"), sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except OSError:
            pass


def _moment(value: Any) -> datetime | None:
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _bucket_key(role: str, task: str, model: str) -> str:
    return f"{role}\t{task}\t{model}"


def _add(ledger: dict[str, Any], role: str, task: str, model: str, counts: list[int], sign: int = 1) -> None:
    key = _bucket_key(role, task, model)
    bucket = ledger["buckets"].setdefault(key, [0, 0, 0, 0])
    for index, count in enumerate(counts):
        bucket[index] = max(0, bucket[index] + sign * count)


# ---- who held which task ------------------------------------------------------------------------------------------------------

def session_key(provider: str, store: str, vendor_id: str, managed: str = "") -> str:
    if vendor_id:
        return f"{provider}|{store}|{vendor_id}"
    return f"{provider}|pending|{managed}"


def _sample_windows(ledger: dict[str, Any], key: str, holder: dict[str, Any] | None, role: str, now: datetime) -> None:
    """Write the task the agent holds NOW into the session's durable windows (the board's own event window is compacted)."""
    if not holder:
        return
    task = "" if role == "cto" else str(holder.get("task") or "")
    if task.startswith(NOT_A_TASK_PREFIXES):
        task = ""
    windows = ledger["windows"].setdefault(key, [])
    if windows and windows[-1]["task"] == task and windows[-1]["role"] == role:
        return
    start = now
    spawned = _moment(holder.get("spawned_at"))
    if not windows and spawned and spawned < now:
        start = spawned
    first_task = bool(task) and role == "reviewer" and bool(windows) and all(not window["task"] for window in windows)
    windows.append({"task": task, "role": role, "from": start.isoformat()})
    if first_task:
        _reattribute_idle_spend(ledger, key, role, task)


def _reattribute_idle_spend(ledger: dict[str, Any], key: str, role: str, task: str) -> None:
    """A reviewer session that has held no task until now and takes a task (a Code analysis reviewer, which before was counted as unallocated):
    what it already spent belongs to that task. Only this session's own counted usage moves, and only from "no task"."""
    for reply_key, old in ledger["replies"].items():
        if reply_key.startswith(f"{key}|") and old["r"] == role and not old["t"]:
            _add(ledger, role, "", old["m"], old["c"], -1)
            _add(ledger, role, task, old["m"], old["c"])
            old["t"] = task
    entry = ledger["sessions"].get(key) or {}
    last = entry.get("last")
    if entry.get("provider") == "codex" and last:
        counts = [max(0, last[0] - last[1]), last[3], last[1], last[2]]
        _add(ledger, role, "", entry.get("model", ""), counts, -1)
        _add(ledger, role, task, entry.get("model", ""), counts)


def _attribution(windows: list[dict[str, Any]], when: datetime | None, fallback_role: str) -> tuple[str, str]:
    """(role, task) the session held at `when`; a reading outside every window is "not on a task"."""
    role, task = fallback_role, ""
    if when is None:
        return role, task
    for window in windows:
        start = _moment(window.get("from"))
        if start is not None and start <= when:
            role, task = window.get("role") or fallback_role, window.get("task") or ""
        else:
            break
    if role == "cto":
        task = ""
    return role, task


# ---- finding and reading the program files ---------------------------------------------------------------------------------------

def default_store(provider: str) -> str:
    if provider == "claude":
        return str(Path(os.environ.get("CLAUDE_CONFIG_DIR") or (Path.home() / ".claude")))
    if provider == "codex":
        return str(Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex")))
    return ""


def find_file(provider: str, store: str, vendor_id: str) -> Path | None:
    """The session's own file inside its store. The id must be a plain token and the result must stay inside the store."""
    if not store or not SAFE_SESSION_ID.match(vendor_id or ""):
        return None
    root = Path(store)
    try:
        if provider == "claude":
            candidates = (root / "projects").glob(f"*/{vendor_id}.jsonl")
        elif provider == "codex":
            candidates = (root / "sessions").rglob(f"rollout-*-{vendor_id}.jsonl")
        else:
            return None
        base = root.resolve()
        for candidate in candidates:
            resolved = candidate.resolve()
            if resolved.is_file() and base in resolved.parents:
                return resolved
    except OSError:
        return None
    return None


def _project_record(store: str, vendor_id: str) -> dict[str, Any] | None:
    """Claude's own per-project record (lastModelUsage) for a session whose transcript file is missing, or None.

    Claude Code writes it into .claude.json when a session ends: one record per project folder, the last session wins,
    so it only answers for a session that was the last to end in its folder."""
    if not store or not SAFE_SESSION_ID.match(vendor_id or ""):
        return None
    root = Path(store)
    for candidate in (root.parent / ".claude.json", root / ".claude.json"):
        try:
            projects = json.loads(candidate.read_text(encoding="utf-8")).get("projects")
        except (OSError, ValueError, RecursionError, AttributeError):
            continue
        for record in (projects.values() if isinstance(projects, dict) else ()):
            if isinstance(record, dict) and record.get("lastSessionId") == vendor_id and isinstance(record.get("lastModelUsage"), dict):
                return record
    return None


def _apply_project_record(ledger: dict[str, Any], key: str, entry: dict[str, Any], record: dict[str, Any], now: datetime) -> None:
    role, task = _attribution(ledger["windows"].get(key, []), now, entry.get("role", ""))
    for model, usage in record["lastModelUsage"].items():
        if not isinstance(usage, dict):
            continue
        counts = [_count(usage.get("inputTokens")), _count(usage.get("outputTokens")),
                  _count(usage.get("cacheReadInputTokens")), _count(usage.get("cacheCreationInputTokens"))]
        reply_key = f"{key}|record|{model}"
        old = ledger["replies"].get(reply_key)
        if old:
            _add(ledger, old["r"], old["t"], old["m"], old["c"], -1)
        _add(ledger, role, task, str(model)[:120], counts)
        ledger["replies"][reply_key] = {"r": role, "t": task, "m": str(model)[:120], "c": counts}
    entry.update(status="ok", reason="", source="project_record", counted_through=now.isoformat())


def _read_complete_lines(path: Path, offset: int, budget: int) -> tuple[list[bytes], int, str]:
    """(complete lines, new offset, problem). A half-written last line is left for the next time."""
    chunks: list[bytes] = []
    taken = 0
    with path.open("rb") as handle:
        handle.seek(offset)
        while taken < budget:
            block = handle.read(min(_BLOCK, budget - taken))
            if not block:
                break
            chunks.append(block)
            taken += len(block)
    data = b"".join(chunks)
    cut = data.rfind(b"\n")
    more = "more to read than one check allows; it continues on the next check"
    if cut < 0:
        return [], offset, ("a single line is larger than the per-check reading limit" if taken >= budget else "")
    return data[:cut].split(b"\n"), offset + cut + 1, (more if taken >= budget else "")


def _count(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _claude_row(line: bytes) -> tuple[str, str, str, list[int]] | None:
    """(message id, model, time, counts) of one assistant row, or None. Everything else on the line is dropped here."""
    if b'"usage"' not in line:
        return None
    try:
        row = json.loads(line)
    except (ValueError, RecursionError):
        return None
    if not isinstance(row, dict) or row.get("type") != "assistant":
        return None
    message = row.get("message")
    if not isinstance(message, dict) or not isinstance(message.get("id"), str) or not isinstance(message.get("usage"), dict):
        return None
    usage = message["usage"]
    counts = [_count(usage.get("input_tokens")), _count(usage.get("output_tokens")),
              _count(usage.get("cache_read_input_tokens")), _count(usage.get("cache_creation_input_tokens"))]
    return message["id"][:200], str(message.get("model") or "")[:120], str(row.get("timestamp") or ""), counts


def _codex_row(line: bytes) -> dict[str, Any] | None:
    """{"model"} for a turn_context row, {"totals","time","limit"} for a token_count row, else None."""
    if b"token_count" not in line and b"turn_context" not in line:
        return None
    try:
        row = json.loads(line)
    except (ValueError, RecursionError):
        return None
    payload = row.get("payload") if isinstance(row, dict) else None
    if not isinstance(payload, dict):
        return None
    if row.get("type") == "turn_context":
        return {"model": str(payload.get("model") or "")[:120]} if payload.get("model") else None
    if row.get("type") != "event_msg" or payload.get("type") != "token_count":
        return None
    info = payload.get("info")
    usage = info.get("total_token_usage") if isinstance(info, dict) else None
    if not isinstance(usage, dict):
        return None
    totals = [_count(usage.get("input_tokens")), _count(usage.get("cached_input_tokens")),
              _count(usage.get("cache_write_input_tokens")), _count(usage.get("output_tokens"))]
    limits = payload.get("rate_limits") if isinstance(payload.get("rate_limits"), dict) else row.get("rate_limits")
    primary = limits.get("primary") if isinstance(limits, dict) else None
    limit = None
    if isinstance(primary, dict) and isinstance(primary.get("used_percent"), (int, float)) and not isinstance(primary.get("used_percent"), bool) \
            and math.isfinite(primary["used_percent"]):
        limit = {"pct": float(primary["used_percent"]), "minutes": _count(primary.get("window_minutes")),
                 "resets": primary.get("resets_at") if isinstance(primary.get("resets_at"), (int, float)) and not isinstance(primary.get("resets_at"), bool) else None}
    return {"totals": totals, "time": str(row.get("timestamp") or ""), "limit": limit}


def _note_limit(ledger: dict[str, Any], task: str, when: datetime | None, limit: dict[str, Any]) -> None:
    """Keep the first and last subscription reading seen on a task (the percentage is the account's, so the same task is merged across sessions)."""
    if not task or when is None:
        return
    stamp = when.timestamp()
    entry = ledger["limits"].setdefault(task, {"first": None, "last": None, "reset": False})
    reading = {"pct": limit["pct"], "at": stamp, "minutes": limit["minutes"], "resets": limit["resets"]}
    if entry["first"] is None or stamp < entry["first"]["at"]:
        entry["first"] = reading
    if entry["last"] is None or stamp >= entry["last"]["at"]:
        entry["last"] = reading
    first, last = entry["first"], entry["last"]
    if last["pct"] < first["pct"] or (first["resets"] is not None and last["resets"] is not None and last["resets"] != first["resets"]):
        entry["reset"] = True


def _apply_claude(ledger: dict[str, Any], key: str, entry: dict[str, Any], lines: list[bytes]) -> None:
    windows = ledger["windows"].get(key, [])
    for line in lines:
        row = _claude_row(line)
        if row is None:
            continue
        message_id, model, stamp, counts = row
        reply_key = f"{key}|{message_id}"
        old = ledger["replies"].get(reply_key)
        if old:
            role, task = old["r"], old["t"]
            _add(ledger, role, task, old["m"], old["c"], -1)
        else:
            role, task = _attribution(windows, _moment(stamp), entry.get("role", ""))
        _add(ledger, role, task, model, counts)
        ledger["replies"][reply_key] = {"r": role, "t": task, "m": model, "c": counts}


def _apply_codex(ledger: dict[str, Any], key: str, entry: dict[str, Any], lines: list[bytes]) -> None:
    windows = ledger["windows"].get(key, [])
    for line in lines:
        row = _codex_row(line)
        if row is None:
            continue
        if "model" in row:
            entry["model"] = row["model"]
            continue
        current, last = row["totals"], entry.get("last")
        when = _moment(row["time"])
        if last is None:
            delta = current
        elif entry.get("replay"):
            # Re-reading a file that shrank: skip what was already counted, resume at the first reading beyond it.
            if all(now_ <= before for now_, before in zip(current, last)):
                continue
            entry["replay"] = False
            delta = [max(0, now_ - before) for now_, before in zip(current, last)]
        elif any(now_ < before for now_, before in zip(current, last)):
            delta = current   # the counters went down: a new counting run starts from zero
        else:
            delta = [now_ - before for now_, before in zip(current, last)]
        entry["last"] = current
        role, task = _attribution(windows, when, entry.get("role", ""))
        if any(delta):
            counts = [max(0, delta[0] - delta[1]), delta[3], delta[1], delta[2]]
            _add(ledger, role, task, entry.get("model", ""), counts)
        if row["limit"] is not None and role != "cto":
            _note_limit(ledger, task, when, row["limit"])


def _read_session(ledger: dict[str, Any], key: str, entry: dict[str, Any], budget: int, deadline: float, now: datetime) -> int:
    """Read what is new in one session's file. Returns the bytes used. Every problem becomes a plain label, never an exception."""
    provider = entry["provider"]
    if provider not in ("claude", "codex"):
        entry.update(status="not_counted", reason=GEMINI_WORDS if provider == "gemini" else "this program is not measured")
        return 0
    if entry["id"] == "":
        entry.update(status="not_counted", reason="the program has not reported its session yet")
        return 0
    path = Path(entry["file"]) if entry.get("file") else None
    if path is None or not path.is_file():
        path = find_file(provider, entry["store"], entry["id"])
    if path is not None and entry.get("source") == "project_record":   # the real file appeared: it replaces the end-of-session figure
        for reply_key in [k for k in ledger["replies"] if k.startswith(f"{key}|record|")]:
            old = ledger["replies"].pop(reply_key)
            _add(ledger, old["r"], old["t"], old["m"], old["c"], -1)
        entry.pop("source", None)
    if path is None and provider == "claude":
        record = _project_record(entry["store"], entry["id"])
        if record is not None:
            _apply_project_record(ledger, key, entry, record, now)
            return 0
    if path is None:
        entry.update(status="not_counted", reason="the program's session file was not found in its folder")
        return 0
    try:
        size = path.stat().st_size
        if entry.get("file") != str(path):
            entry["file"] = str(path)
            entry["offset"] = 0 if entry.get("offset") is None else entry["offset"]
        if size < int(entry.get("offset") or 0):
            entry["offset"] = 0   # the file shrank: read it again from the start (reply ids and the replay rule make this safe)
            entry["replay"] = provider == "codex"
        if time.monotonic() >= deadline:
            entry.update(status="partial", reason="the time limit for one check was reached")
            return 0
        lines, new_offset, problem = _read_complete_lines(path, int(entry.get("offset") or 0), budget)
        used = new_offset - int(entry.get("offset") or 0)
        (_apply_claude if provider == "claude" else _apply_codex)(ledger, key, entry, lines)
        entry["offset"], entry["size"] = new_offset, size
        if problem:
            entry.update(status="partial", reason=problem)
        else:
            entry.update(status="ok", reason="", counted_through=now.isoformat())
        return max(used, 0)
    except (OSError, ValueError, TypeError, KeyError, MemoryError) as error:
        entry.update(status="partial", reason=f"the file could not be read ({type(error).__name__})")
        return 0


# ---- collecting ----------------------------------------------------------------------------------------------------------------

def collect(ledger_dir: Path, observations: list[dict[str, Any]], *, now: datetime | None = None,
            max_bytes: int | None = None, max_seconds: float | None = None) -> dict[str, Any]:
    """One bounded pass. `observations` = [{managed, provider, id, store, role, holder: {task, spawned_at} | None}].

    Never raises: the result says what happened ("ok", or "partial" with a plain reason). A cost problem never touches recovery."""
    now = now or _clock()
    byte_limit, second_limit = limits()
    budget = max_bytes if max_bytes is not None else byte_limit
    deadline = time.monotonic() + (max_seconds if max_seconds is not None else second_limit)
    lock_file = None
    try:
        Path(ledger_dir).mkdir(parents=True, exist_ok=True)
        lock_file = (Path(ledger_dir) / "cost_ledger.lock").open("a+")
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return {"status": "busy", "reason": "another check is running"}
        ledger, note = _load(ledger_dir)
        if note and note not in ledger["notes"]:
            ledger["notes"].append(note)
        keys: dict[str, dict[str, Any]] = {}
        for item in observations:
            provider = str(item.get("provider") or "")
            vendor = str(item.get("id") or "")
            if vendor and not SAFE_SESSION_ID.match(vendor):
                vendor = ""
            store = str(item.get("store") or "") if provider in ("claude", "codex") else ""
            if provider in ("claude", "codex") and vendor and not store:
                store = default_store(provider)
            key = session_key(provider, store, vendor, str(item.get("managed") or ""))
            entry = ledger["sessions"].setdefault(key, {"provider": provider, "store": store, "id": vendor, "role": item.get("role") or "",
                                                         "offset": 0, "status": "not_counted", "reason": "", "first_seen": now.isoformat()})
            if item.get("role"):
                entry["role"] = item["role"]
            if vendor:
                ledger["sessions"].pop(session_key(provider, "pending", "", str(item.get("managed") or "")), None)
            keys[key] = entry
            _sample_windows(ledger, key, item.get("holder"), entry["role"], now)
        remaining = budget
        for key, entry in sorted(ledger["sessions"].items()):
            if key not in keys and not entry.get("id"):
                continue
            remaining -= _read_session(ledger, key, entry, max(remaining, 0), deadline, now) if remaining > 0 or entry["provider"] not in ("claude", "codex") \
                else _mark_waiting(entry)
        ledger["updated_at"] = now.isoformat()
        try:
            _save(ledger_dir, ledger)
        except OSError as error:
            return {"status": "partial", "reason": f"the cost record could not be saved ({type(error).__name__})"}
        problems = [e["reason"] for e in ledger["sessions"].values() if e.get("status") == "partial"]
        return {"status": "partial" if problems else "ok", "reason": problems[0] if problems else ""}
    except Exception as error:   # noqa: BLE001 - a cost failure must never reach recovery or the board lock
        return {"status": "partial", "reason": f"cost counting stopped ({type(error).__name__})"}
    finally:
        if lock_file is not None:
            lock_file.close()


def _mark_waiting(entry: dict[str, Any]) -> int:
    entry.update(status="partial", reason="more to read than one check allows; it continues on the next check")
    return 0


# ---- what is shown ---------------------------------------------------------------------------------------------------------------

def _tokens_text(count: int) -> str:
    if count >= 1_000_000:
        return f"{count / 1_000_000:.1f}M"
    if count >= 10_000:
        return f"{count / 1000:.0f}K"
    if count >= 1000:
        return f"{count / 1000:.1f}K"
    return str(count)


def _money(value: float) -> str:
    return f"${value:,.2f}" if value >= 0.10 or value == 0 else f"${value:.4f}"


def _amount(part: dict[str, Any]) -> str:
    """The money for a part, or "not measured" when nothing in it was counted (a counted zero is still $0.00)."""
    return _money(part["usd"]) if part.get("counted") else "not measured"


money = _money              # the screens that show a figure use these two (the Projects card and the task page)
tokens_text = _tokens_text


def _zero() -> dict[str, Any]:
    return {"usd": 0.0, "tokens": 0, "estimated": False, "roles": {}, "counted": 0}   # counted = ledger buckets in it; 0 means never measured, not "zero cost"


def summarize(ledger_dir: Path) -> dict[str, Any]:
    """Everything the screens need, priced NOW from the current card. {"available": False} when nothing has been counted yet."""
    path = ledger_path(ledger_dir)
    try:
        stamp = (path.stat().st_mtime_ns, path.stat().st_size, _card_key())
    except OSError:
        return {"available": False}
    memo = _memo.get(str(path))
    if memo and memo[0] == stamp:
        return memo[1]
    ledger, _note = _load(Path(ledger_dir))
    try:
        card, card_note = load_card()
    except (OSError, ValueError):
        return {"available": False}
    tasks: dict[str, dict[str, Any]] = {}
    overhead, unallocated, total = _zero(), _zero(), _zero()
    for bucket_key, counts in ledger["buckets"].items():
        role, task, model = bucket_key.split("\t", 2)
        usd, estimated = price(card, model, counts)
        tokens = sum(counts)
        target = overhead if role == "cto" else (tasks.setdefault(task, {**_zero(), "problems": [], "gemini": [], "codex_limit": None}) if task else unallocated)
        for part in (target, total):
            part["usd"] += usd
            part["tokens"] += tokens
            part["counted"] += 1
            part["estimated"] = part["estimated"] or estimated
        role_part = target["roles"].setdefault(role, {"usd": 0.0, "tokens": 0, "counted": 0})
        role_part["counted"] += 1
        role_part["usd"] += usd
        role_part["tokens"] += tokens
    counted_through, gemini_seen = "", False
    for key, entry in ledger["sessions"].items():
        served = {w["task"] for w in ledger["windows"].get(key, []) if w.get("task")}
        for window in ledger["windows"].get(key, []):
            if window.get("task") in tasks:
                tasks[window["task"]]["last_at"] = max(tasks[window["task"]].get("last_at", ""), str(window.get("from") or ""))
        if entry.get("provider") == "gemini":
            gemini_seen = True
            for task in served:
                row = tasks.setdefault(task, {**_zero(), "problems": [], "gemini": [], "codex_limit": None})
                row["gemini"].append(entry.get("role") or "delivery")
            continue
        if entry.get("counted_through"):
            counted_through = max(counted_through, entry["counted_through"])
        if entry.get("source") == "project_record" and entry.get("status") == "ok":
            for task in served:
                row = tasks.setdefault(task, {**_zero(), "problems": [], "gemini": [], "codex_limit": None})
                if PROJECT_RECORD_WORDS not in row["problems"]:
                    row["problems"].append(PROJECT_RECORD_WORDS)
        if entry.get("status") in ("partial", "not_counted"):
            label = ("partial because " if entry["status"] == "partial" else "not counted: ") + str(entry.get("reason") or "unknown")
            for task in served:
                row = tasks.setdefault(task, {**_zero(), "problems": [], "gemini": [], "codex_limit": None})
                if label not in row["problems"]:
                    row["problems"].append(label)
            if not served and label not in unallocated.setdefault("problems", []):
                unallocated["problems"].append(label)
    for windows in ledger["windows"].values():   # a task the Gemini or a partial session served has a last-active time too
        for window in windows:
            if window.get("task") in tasks:
                tasks[window["task"]]["last_at"] = max(tasks[window["task"]].get("last_at", ""), str(window.get("from") or ""))
    for task, entry in ledger["limits"].items():
        first, last = entry.get("first"), entry.get("last")
        if first and last and task in tasks:
            minutes = last.get("minutes") or first.get("minutes") or 0
            tasks[task]["codex_limit"] = {"reset": bool(entry.get("reset")), "minutes": minutes, "increase": round(max(0.0, last["pct"] - first["pct"]), 1)}
    result = {"available": True, "as_of": card["as_of"], "card_note": card_note, "counted_through": counted_through, "tasks": tasks,
              "overhead": overhead, "unallocated": unallocated, "total": total, "gemini": gemini_seen,
              "notes": list(ledger.get("notes") or [])[-3:]}
    _memo[str(path)] = (stamp, result)
    return result


def _window_words(minutes: int) -> str:
    if minutes and minutes % 1440 == 0:
        return f"{minutes // 1440}-day"
    if minutes and minutes % 60 == 0:
        return f"{minutes // 60}-hour"
    return f"{minutes}-minute" if minutes else "usage"


def limit_sentence(limit: dict[str, Any]) -> str:
    """The Codex subscription line for a task: the share of the window used while it ran, or that the window reset (no wrong number)."""
    if limit["reset"]:
        return "Codex subscription: window reset during this task."
    return f"Codex subscription: about +{limit['increase']:g}% of your {_window_words(limit['minutes'])} window."


def _through(summary: dict[str, Any]) -> str:
    moment = _moment(summary.get("counted_through"))
    return moment.astimezone().strftime("%H:%M") if moment else ""


def task_line(summary: dict[str, Any], task: str) -> str:
    """One plain sentence (plus the Codex, Gemini and partial notes) for a task card or the history. "" when there is nothing to say."""
    if not summary.get("available"):
        return ""
    row = (summary.get("tasks") or {}).get(task)
    if not row:
        return ""
    parts: list[str] = []
    if row["counted"]:
        roles = " / ".join(f"{ROLE_WORDS.get(role, role)} {_amount(part)}" for role, part in sorted(row["roles"].items()))
        parts.append(f"Cost so far: about {_money(row['usd'])} API-equivalent{' (estimated)' if row['estimated'] else ''} · "
                     f"{_tokens_text(row['tokens'])} tokens ({roles}). Prices as of {summary['as_of']}; this is a yardstick, not a bill.")
    elif not row["gemini"]:
        parts.append("Cost so far: not counted yet.")
    if row["problems"]:
        through = _through(summary)
        parts.append((f"Counted through {through}; " if through else "") + "; ".join(row["problems"]) + ".")
    if row.get("codex_limit"):
        parts.append(limit_sentence(row["codex_limit"]))
    for role in sorted(set(row["gemini"])):
        parts.append(f"Gemini ({ROLE_WORDS.get(role, role)}): {GEMINI_WORDS}.")
    return " ".join(parts)


def project_line(summary: dict[str, Any]) -> str:
    if not summary.get("available"):
        return ""
    total, overhead, loose = summary["total"], summary["overhead"], summary["unallocated"]
    if not total["counted"]:
        text = "Project so far: not measured yet - no usage has been counted."
        if summary.get("gemini"):
            text += " Excludes Gemini: it is not measured."
        return text + ((" " + summary["card_note"]) if summary.get("card_note") else "")
    tasks_part = {"usd": max(0.0, total["usd"] - overhead["usd"] - loose["usd"]), "counted": total["counted"] - overhead["counted"] - loose["counted"]}
    seen = len(summary["tasks"])
    measured = sum(1 for row in summary["tasks"].values() if row["counted"])
    coverage = f" for {measured} of {seen} tasks measured" if measured < seen else ""
    text = (f"Project so far: about {_money(total['usd'])} API-equivalent{coverage}{' (estimated)' if total['estimated'] else ''} · {_tokens_text(total['tokens'])} tokens "
            f"(tasks {_amount(tasks_part)} + CTO overhead {_amount(overhead)} + not on a task {_amount(loose)}). "
            f"Prices as of {summary['as_of']}.")
    if summary.get("gemini"):
        text += " Excludes Gemini: it is not measured."
    if summary.get("card_note"):
        text += " " + summary["card_note"]
    return text


NOT_MEASURED = "not measured yet"
WEEK_DAYS = 7


def _sum_parts(parts: list[dict[str, Any]]) -> dict[str, Any]:
    return {"usd": sum(part["usd"] for part in parts), "counted": sum(part["counted"] for part in parts)}


def _role_total(row: dict[str, Any], role: str) -> dict[str, Any]:
    return row["roles"].get(role) or {"usd": 0.0, "counted": 0}


def _window_text(limit: dict[str, Any] | None) -> str:
    if not limit:
        return ""
    if limit["reset"]:
        return "window reset during this task"
    return f"about +{limit['increase']:g}% of your {_window_words(limit['minutes'])} window"


def _task_notes(row: dict[str, Any]) -> list[str]:
    notes = list(row["problems"])
    notes += [f"Gemini ({ROLE_WORDS.get(role, role)}): {GEMINI_WORDS}" for role in sorted(set(row["gemini"]))]
    return notes


def _project_rows(summary: dict[str, Any]) -> dict[str, Any]:
    tasks = summary["tasks"]
    builder = _sum_parts([_role_total(row, "delivery") for row in tasks.values()])
    reviewer = _sum_parts([_role_total(row, "reviewer") for row in tasks.values()])
    overhead, loose, total = summary["overhead"], summary["unallocated"], summary["total"]
    seen, measured = len(tasks), sum(1 for row in tasks.values() if row["counted"])
    notes: list[str] = []
    if total["counted"] and measured < seen:
        notes.append(f"Counted for {measured} of {seen} tasks measured.")
    if loose["counted"]:
        notes.append(f"{_amount(loose)} more was used outside any task; it is in the total.")
    if summary.get("gemini"):
        notes.append("Excludes Gemini: it is not measured yet.")
    if total["estimated"]:
        notes.append("Some figures are estimated: a model is missing from the price list.")
    if summary.get("card_note"):
        notes.append(summary["card_note"])
    return {"builder": _amount(builder), "reviewer": _amount(reviewer), "cto": _amount(overhead), "total": _amount(total), "notes": notes}


def overview(ledger_dir: Path) -> dict[str, Any]:
    """One project for the Costs view: the project sentence, the table figures (every unmeasured cell says so) and a task list, the most recently active task first."""
    summary = summarize(ledger_dir)
    if not summary.get("available"):
        return {"available": False}
    rows = sorted(summary["tasks"].items(), key=lambda item: item[1].get("last_at", ""), reverse=True)
    tasks = []
    for task, row in rows:
        tasks.append({"task": task, "line": task_line(summary, task), "builder": _amount(_role_total(row, "delivery")),
                      "reviewer": _amount(_role_total(row, "reviewer")), "total": _amount(row), "window": _window_text(row.get("codex_limit")),
                      "notes": _task_notes(row)})
    return {"available": True, "line": project_line(summary), "as_of": summary["as_of"], **_project_rows(summary), "tasks": tasks}


def prices_line() -> str:
    """Where the prices on the Costs page come from, in one plain sentence."""
    try:
        card, _note = load_card()
    except (OSError, ValueError, TypeError, RecursionError):
        return "The price list could not be read, so no money figure can be shown."
    whose = "your own price list" if card.get("owner_copy") else "the providers' official pricing pages"
    return (f"Prices come from {whose}, dated {card['as_of']}. A price list of your own can be set in the settings file on this computer.")


_ROLE_LABEL = {"delivery": "Builder", "reviewer": "Reviewer", "cto": "CTO"}


def rollup(summaries: list[dict[str, Any]], now: datetime | None = None) -> dict[str, Any]:
    """The three tiles above the Costs table, from the projects' summaries. Only what was counted is added up; an empty tile says "not measured yet", never $0.00."""
    now = now or _clock()
    since = now - timedelta(days=WEEK_DAYS)
    week_seen = week_measured = seen_tasks = 0
    week_usd = 0.0
    task_usd: list[float] = []
    role_usd: dict[str, float] = {}
    estimated = False
    for summary in summaries:
        if not summary.get("available"):
            continue
        seen_tasks += len(summary["tasks"])
        for row in summary["tasks"].values():
            last = _moment(row.get("last_at"))
            recent = last is not None and last >= since
            week_seen += recent
            if not row["counted"]:
                continue
            estimated = estimated or row["estimated"]
            task_usd.append(row["usd"])
            if recent:
                week_measured += 1
                week_usd += row["usd"]
            for role, part in row["roles"].items():
                role_usd[role] = role_usd.get(role, 0.0) + part["usd"]
        overhead = summary["overhead"]
        if overhead["counted"]:
            estimated = estimated or overhead["estimated"]
            role_usd["cto"] = role_usd.get("cto", 0.0) + overhead["usd"]
    flag = " (estimated)" if estimated else ""
    plural = lambda n: "" if n == 1 else "s"   # noqa: E731
    if week_measured:
        week = {"value": _money(week_usd) + flag, "note": f"tasks worked on in the last {WEEK_DAYS} days" + (f"; {week_measured} of {week_seen} measured" if week_measured < week_seen else "")}
    elif week_seen:
        week = {"value": NOT_MEASURED, "note": f"{week_seen} task{plural(week_seen)} worked on in the last {WEEK_DAYS} days, none counted"}
    else:
        week = {"value": NOT_MEASURED, "note": f"no task was worked on in the last {WEEK_DAYS} days"}
    if task_usd:
        average = {"value": _money(sum(task_usd) / len(task_usd)) + flag,
                   "note": f"over {len(task_usd)} measured task{plural(len(task_usd))}" + (f" of {seen_tasks}" if len(task_usd) < seen_tasks else "")}
    else:
        average = {"value": NOT_MEASURED, "note": f"{seen_tasks} task{plural(seen_tasks)}, none counted" if seen_tasks else "no task yet"}
    whole = sum(role_usd.values())
    if role_usd and whole > 0:
        shown = [role for role in _ROLE_LABEL if role in role_usd]
        missing = [_ROLE_LABEL[role] for role in _ROLE_LABEL if role not in role_usd]
        split = {"value": " · ".join(f"{_ROLE_LABEL[role]} {round(100 * role_usd[role] / whole)}%" for role in shown),
                 "note": "of what was counted" + (f"; {', '.join(missing)} not measured yet" if missing else "")}
    else:
        split = {"value": NOT_MEASURED, "note": "nothing counted to split"}
    return {"week": week, "average": average, "split": split}


def figures(ledger_dir: Path, task: str | None = None) -> dict[str, Any]:
    """The compact numbers for the evals and screens: usd, tokens, estimated, as-of and counted-through, or {"unavailable": True}."""
    summary = summarize(ledger_dir)
    if not summary.get("available"):
        return {"unavailable": True}
    part = summary["total"] if task is None else (summary["tasks"].get(task) or _zero())
    if not part["counted"]:
        return {"unavailable": True, "reason": "not measured"}   # nothing counted is "not measured", never $0
    return {"usd": round(part["usd"], 4), "tokens": part["tokens"], "estimated": part["estimated"],
            "as_of": summary["as_of"], "counted_through": summary["counted_through"], "gemini": summary.get("gemini", False)}


# ---- gathering the project's sessions (the only part that knows the control and board files) ----------------------------------

def gather(root: Any) -> list[dict[str, Any]]:
    """One observation per launched session: its vendor id and store (from the session's own record) and the task its agent holds now."""
    from harness import board, control
    sessions = (control._read_state(root).get("sessions") or {})
    board_state = board._read_state(root)
    agents = (board_state.get("agents") or {})
    holders: dict[str, dict[str, Any]] = {}
    # A Code analysis reviewer works from Go ahead without a review request, so its agent holds no task: it holds the analysis task.
    analysis_tasks = {str(rec.get("reviewer_id") or ""): task for task, rec in (board_state.get("analysis_passes") or {}).items() if isinstance(rec, dict)}
    for agent in agents.values():
        session_id = str(agent.get("session_id") or "")
        if agent.get("active") and (not agent.get("task") or str(agent.get("task")).startswith(NOT_A_TASK_PREFIXES)) and str(agent.get("id") or "") in analysis_tasks:
            agent = {**agent, "task": analysis_tasks[str(agent.get("id"))]}
        if session_id and agent.get("active") and (session_id not in holders or str(agent.get("spawned_at") or "") >= str(holders[session_id].get("spawned_at") or "")):
            holders[session_id] = agent
    observed = []
    for session_id, session in sessions.items():
        provider = str(session.get("cli_session_provider") or session.get("provider") or "")
        role = control.KIND_TO_ROLE.get(str(session.get("kind") or ""), "")
        observed.append({"managed": session_id, "provider": provider, "id": str(session.get("cli_session_id") or ""),
                         "store": str(session.get("cli_session_store") or ""), "role": role, "holder": holders.get(session_id)})
    return observed


def collect_project(root: Any, ledger_dir: Path | None = None) -> dict[str, Any]:
    """The worker's background pass: never raises."""
    try:
        from harness import board
        return collect(ledger_dir or board.board_dir(root), gather(root))
    except Exception as error:   # noqa: BLE001
        return {"status": "partial", "reason": f"cost counting stopped ({type(error).__name__})"}


# ---- an eval run: costs from the run's own isolated homes, for any NMHP version ----------------------------------------------------

RUN_BYTES, RUN_SECONDS = 2_000_000_000, 90


def _in_run(provider: str, path: Path, roots: list[str]) -> bool:
    """True only for a session whose working folder is inside the run folder (other sessions in the same homes are not the run's)."""
    try:
        if provider == "claude":
            slugs = [re.sub(r"[^A-Za-z0-9]", "-", root) for root in roots]
            return any(path.parent.name.startswith(slug) for slug in slugs)
        with path.open("rb") as handle:
            for _ in range(40):
                line = handle.readline(4_000_000)
                if not line:
                    break
                if b"session_meta" not in line:
                    continue
                cwd = (json.loads(line).get("payload") or {}).get("cwd")
                return isinstance(cwd, str) and any(cwd == root or cwd.startswith(root + os.sep) for root in roots)
    except (OSError, ValueError, AttributeError):
        return False
    return False


def _run_figures(observed: list[dict[str, Any]], skipped: int) -> dict[str, Any]:
    """One bounded collection over the given sessions and the figures of an eval run (shared by the NMHP arm and the plain-Claude arm)."""
    import tempfile
    with tempfile.TemporaryDirectory(prefix="nmhp-cost-") as scratch:
        result = collect(Path(scratch), observed, max_bytes=RUN_BYTES, max_seconds=RUN_SECONDS)
        _memo.clear()
        summary = summarize(Path(scratch))
        _memo.clear()
    if not summary.get("available"):
        return {"available": False, "reason": "nothing could be counted"}
    if not summary["total"]["counted"]:
        return {"available": False, "reason": "no usage could be measured" + (" (Gemini is not measured)" if summary.get("gemini") else "")}
    roles: dict[str, dict[str, float]] = {}
    parts = [row for row in summary["tasks"].values()] + [summary["overhead"], summary["unallocated"]]
    for part in parts:
        for role, figures_ in part["roles"].items():
            slot = roles.setdefault(role, {"tokens": 0, "usd": 0.0})
            slot["tokens"] += figures_["tokens"]
            slot["usd"] += figures_["usd"]
    windows = {task: limit_sentence(row["codex_limit"]) for task, row in summary["tasks"].items() if row.get("codex_limit")}
    problems = sorted({text for row in summary["tasks"].values() for text in row["problems"]} | set(summary["unallocated"].get("problems", [])))
    task_rows = list(summary["tasks"].values())
    return {"available": True, "roles": roles, "tasks_measured": sum(1 for row in task_rows if row["counted"]), "tasks_total": len(task_rows), "tokens": summary["total"]["tokens"], "usd": summary["total"]["usd"],
            "estimated": summary["total"]["estimated"], "as_of": summary["as_of"], "codex_windows": windows,
            "gemini": bool(summary.get("gemini")), "status": result.get("status", ""), "problems": problems,
            "sessions_outside_the_run": skipped}


def collect_run(run: Path, record: dict[str, Any]) -> dict[str, Any]:
    """One bounded collection over an eval run, from the run's recorded sign-in homes and its own board and session records.

    Never raises. {"available": False, "reason"} when the run cannot be read (an older run, no recorded homes): shown as "unavailable", never as zero."""
    try:
        isolation = record.get("isolation") or {}
        if not isolation.get("codex_home") or not isolation.get("claude_home") or not record.get("project_id") or not record.get("home"):
            return {"available": False, "reason": "this run did not record its sign-in folders"}
        stores = {"codex": str(isolation["codex_home"]), "claude": str(Path(isolation["claude_home"]) / ".claude")}
        data = Path(record["home"]) / "projects" / str(record["project_id"]) / "data"
        sessions = (json.loads((data / "control" / "sessions.json").read_text(encoding="utf-8")).get("sessions") or {})
        try:
            agents = json.loads((data / "board" / "state.json").read_text(encoding="utf-8")).get("agents") or {}
        except (OSError, ValueError):
            agents = {}
        roots = sorted({str(Path(run)), str(Path(run).resolve())})
        from harness import control
        observed, skipped = [], 0
        for session_id, session in sessions.items():
            provider = str(session.get("cli_session_provider") or session.get("provider") or "")
            vendor_id = str(session.get("cli_session_id") or "")
            role = control.KIND_TO_ROLE.get(str(session.get("kind") or ""), "")
            holders = [a for a in agents.values() if isinstance(a, dict) and a.get("session_id") == session_id]
            holder = max(holders, key=lambda a: str(a.get("spawned_at") or ""), default=None)
            if provider in ("claude", "codex") and vendor_id:
                path = find_file(provider, stores[provider], vendor_id)
                if path is None or not _in_run(provider, path, roots):
                    skipped += 1
                    continue
            observed.append({"managed": session_id, "provider": provider, "id": vendor_id, "store": stores.get(provider, ""), "role": role, "holder": holder})
        return _run_figures(observed, skipped)
    except Exception as error:   # noqa: BLE001 - scoring must never fail because of cost
        return {"available": False, "reason": f"costs could not be counted ({type(error).__name__})"}


def collect_plain_run(claude_home: Path, run: Path, roles: dict[str, str] | None = None) -> dict[str, Any]:
    """Costs of a plain-Claude eval run (no NMHP): the Claude session files in the isolated home whose working folder is the run folder.

    roles maps a Claude session id to builder ("delivery") or reviewer ("reviewer"); any other session of the run counts as builder.
    Never raises. {"available": False, "reason"} when nothing can be read: shown as "unavailable", never as zero."""
    try:
        store = Path(claude_home) / ".claude"
        roots = sorted({str(Path(run)), str(Path(run).resolve())})
        slugs = [re.sub(r"[^A-Za-z0-9]", "-", root) for root in roots]
        observed = []
        for folder in sorted((store / "projects").glob("*")):
            if not folder.is_dir() or not any(folder.name.startswith(slug) for slug in slugs):
                continue
            for path in sorted(folder.glob("*.jsonl")):
                session_id = path.stem
                observed.append({"managed": session_id, "provider": "claude", "id": session_id, "store": str(store),
                                 "role": (roles or {}).get(session_id, "delivery"), "holder": None})
        if not observed:
            return {"available": False, "reason": "no Claude session of this run was found in the sign-in home"}
        return _run_figures(observed, 0)
    except Exception as error:   # noqa: BLE001 - scoring must never fail because of cost
        return {"available": False, "reason": f"costs could not be counted ({type(error).__name__})"}
