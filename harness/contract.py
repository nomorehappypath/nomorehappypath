#!/usr/bin/env python3
# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Completion Contracts, evidence manifests, and claim linting."""
from __future__ import annotations
import argparse, hashlib, json, re, sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from harness import redaction
from harness.project_context import ProjectRoot, add_context_arguments, context_from_args, project_context

REQUIRED_CONTRACT = {"objective", "deliverables", "exclusions", "status", "remaining_work"}
COMPLETION_WORDS = re.compile(r"\b(done|ready|complete|finished|shipped)\b", re.I)
HANDOFF_STATUSES = {"COMPLETE", "PARTIAL", "BLOCKED"}
ANSI_DCS = re.compile(r"(?:\x1bP|\x90).*?(?:\x1b\\|\x9c)", re.S)
ANSI_OSC = re.compile(r"(?:\x1b\]|\x9d).*?(?:\x07|\x1b\\|\x9c)", re.S)
ANSI_CSI = re.compile(r"(?:\x1b\[|\x9b)[0-?]*[ -/]*[@-~]")
ANSI_SS3 = re.compile(r"(?:\x1bO|\x8f).")
TERMINAL_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1a\x1c-\x1f\x7f-\x9f]")
SCENARIO_ID = re.compile(r"^S-[A-Za-z0-9._-]+$|^S\d+$")   # S-001 is the documented form; S1 is accepted too (2026-10-28: ten refusals over the dash)
SECURITY_SCENARIO_PREFIX = "S-SEC-"


def is_security_scenario_id(scenario_id: Any) -> bool:
    """Owner 2026-10-12: Enhanced security rows carry ids S-SEC-<n>; the board never reuses their results."""
    return str(scenario_id or "").upper().startswith(SECURITY_SCENARIO_PREFIX)
# Environment-varied evidence (VAR=value cmd) is exactly what hostile-env and
# recovery scenarios must record; the prefix form is plain POSIX, carries no
# shell control operators, and is executed with the same safety as the bare
# command (issue row 6).
EXECUTABLE_TEST_COMMAND = re.compile(
    r"^(?:cd\s+(?![/~])[^\s;&|<>`$]+\s+&&\s+)?"          # the folder of the project the runner belongs to
    r"(?:[A-Za-z_][A-Za-z0-9_]*=[^\s;&|<>`$]*\s+)*"
    r"(?:python(?:3)?\s+-m\s+(?:unittest|pytest)\b|pytest\b|"
    r"(?:npm|yarn|pnpm|bun)\s+(?:test|run\s+test)\b|npx\s+(?:jest|vitest|playwright\s+test|mocha)\b|node\s+--test\b|deno\s+test\b|"
    r"flutter\s+test\b|dart\s+test\b|swift\s+test\b|xcodebuild\s+(?:\S+\s+)*test\b|"
    r"go\s+test\b|cargo\s+test\b|dotnet\s+test\b|mvn\s+(?:\S+\s+)*(?:test|verify)\b|ctest\b|rspec\b|(?:vendor/bin/)?phpunit\b|"
    r"make\s+test\b|(?:\./)?gradlew?\s+(?:\S+\s+)*(?:test|connectedAndroidTest)\b)",
    re.I,
)
SHELL_CONTROL = re.compile(r"(?:&&|\|\||[;&|<>]|\$\(|\r|\n)")


def split_folder(command: str) -> tuple[str, str]:
    """(folder, the command itself) of a command that starts with `cd <folder in the project> &&`."""
    match = re.match(r"^cd\s+([^\s;&|<>`$]+)\s+&&\s+(.*)$", command.strip(), re.S)
    return (match.group(1), match.group(2)) if match else ("", command)


def runnable_test_command(command: str) -> bool:
    """One test command the board can run and whose own exit says whether it passed (no pipe, no second command)."""
    folder, rest = split_folder(command)
    return bool(EXECUTABLE_TEST_COMMAND.match(command)) and ".." not in folder.split("/") and not SHELL_CONTROL.search(rest)
OWNER_DESCRIPTION_HEADER = "What was tested"
# Task 2026-11-24_tdd-examples-first: a ledger row may say which accepted requirements it tests, by number, in a column named
# "Requirement" ("3" or "3, 5"). The board matches numbers only, never prose; an older ledger without the column is unchanged.
REQUIREMENT_HEADER = "Requirement"
OWNER_DESCRIPTION_MAX_LENGTH = 500
OWNER_DESCRIPTION_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")

# Takes no arguments. Return: the current UTC time as an ISO-8601 string, used to timestamp contract records.
def _now(): return datetime.now(timezone.utc).isoformat()
# Takes root, a ProjectRoot. Return: root's code_root path, the project's checked-out source tree.
def _root(root: ProjectRoot): return project_context(root).code_root
# Takes root (a ProjectRoot) and task (a task id). Return: the storage path of task's Completion Contract JSON file under root's tasks folder.
def _task_path(root: ProjectRoot, task): return project_context(root).storage_path("tasks", f"{task}.json")
# Takes path, the file to read. Return: path's text, read as UTF-8 and parsed as JSON.
def _load(path): return json.loads(path.read_text(encoding="utf-8"))
def _save(path, value):
    """Write value to path as indented JSON, atomically via a same-directory temp file.

    Returns nothing; the effect is that path either holds the new content or
    is left untouched (the temp file is written and renamed into place, so a
    crash mid-write never leaves a partial file at path).
    """
    import os, secrets
    path.parent.mkdir(parents=True, exist_ok=True); temp=path.with_name(f"{path.name}.tmp-{os.getpid()}-{secrets.token_hex(4)}")
    temp.write_text(json.dumps(value, indent=2, sort_keys=True)+"\n", encoding="utf-8"); temp.replace(path)

def _immutable_projection(value):
    """Return the subset of value (objective, exclusions, deliverable name/proof, build) that must never change.

    This projection is stored as a contract's immutable_scope and compared
    against the current contract to detect any attempt to rewrite it.
    """
    projection={"objective":value.get("objective"),"exclusions":json.loads(json.dumps(value.get("exclusions",[]))),"deliverables":[{"name":item.get("name"),"acceptance_proof":item.get("acceptance_proof")} for item in value.get("deliverables",[])]}
    # A program build declaration is part of what a review certifies; contracts without one hash exactly as before.
    if value.get("build"): projection["build"]=json.loads(json.dumps(value["build"]))
    return projection

EXCLUSION_ACTIONS = ("lift", "replace", "add")


def _amendment_problem(item, original_count):
    """Why one recorded exclusion amendment is not a valid, owner-approved one ('' when it is)."""
    if not isinstance(item, dict): return "an exclusion amendment is malformed"
    label=f"exclusion amendment {item.get('id') or '?'}"
    if item.get("action") not in EXCLUSION_ACTIONS: return f"{label} has no valid action"
    if not str(item.get("owner_message_id") or "").strip() or not str(item.get("owner_words") or "").strip():
        return f"{label} has no owner approval reference"
    if not str(item.get("reason") or "").strip(): return f"{label} has no reason"
    if item["action"] in ("lift","replace"):
        index=item.get("index")
        if not isinstance(index,int) or isinstance(index,bool) or not 1 <= index <= original_count: return f"{label} names an exclusion that does not exist"
    if item["action"] in ("replace","add") and not str(item.get("text") or "").strip(): return f"{label} has no text"
    return ""

def effective_exclusions(value):
    """What is in force now: the original exclusions with each owner-approved amendment applied beside them.

    The original list (and ``immutable_scope``) is never rewritten; an amendment is a separate dated record.
    """
    originals=list((value.get("immutable_scope") or {}).get("exclusions", value.get("exclusions",[])))
    amendments=[a for a in value.get("exclusion_amendments",[]) if not _amendment_problem(a,len(originals))]
    result=[]
    for position, original in enumerate(originals, 1):
        change=next((a for a in amendments if a["action"] in ("lift","replace") and a["index"]==position),None)
        if change is None: result.append({"index":position,"exclusion":original,"status":"in force"})
        elif change["action"]=="lift": result.append({"index":position,"exclusion":original,"status":"lifted","amendment":change["id"]})
        else: result.append({"index":position,"exclusion":original,"status":"replaced","replaced_by":change["text"],"amendment":change["id"]})
    result.extend({"index":None,"exclusion":a["text"],"status":"added","amendment":a["id"]} for a in amendments if a["action"]=="add")
    return result

def _immutable_problems(value):
    """Return reasons value's immutable scope was violated, or amendments are malformed.

    Output is an empty list when value has no immutable_scope recorded yet,
    or when the current projection matches it and every exclusion amendment
    is valid; otherwise each problem is a human-readable string.
    """
    immutable=value.get("immutable_scope")
    if not immutable: return []
    current=_immutable_projection(value)
    expected={"objective":immutable.get("objective"),"exclusions":immutable.get("exclusions",[]),"deliverables":immutable.get("deliverables",[])}
    if immutable.get("build"): expected["build"]=immutable["build"]
    current["deliverables"]=current.get("deliverables",[])[:len(expected["deliverables"])]
    problems=["original contract objective, exclusions, and deliverable names/proofs/order are immutable"] if current != expected else []
    # The originals above must be untouched; every amendment beside them must carry the owner's approval.
    count=len(expected["exclusions"])
    problems.extend(problem for problem in (_amendment_problem(a,count) for a in value.get("exclusion_amendments",[])) if problem)
    return problems

def amend_exclusions(root, task, amendment):
    """Append one owner-approved, dated exclusion amendment beside the immutable originals (idempotent by id)."""
    path=_task_path(root,task); value=_load(path)
    problem=_amendment_problem(amendment,len((value.get("immutable_scope") or {}).get("exclusions",value.get("exclusions",[]))))
    if problem: raise ValueError(problem)
    existing=value.setdefault("exclusion_amendments",[])
    if any(a.get("id")==amendment["id"] for a in existing): return value
    if amendment["action"] in ("lift","replace") and any(a.get("action") in ("lift","replace") and a.get("index")==amendment["index"] for a in existing):
        raise ValueError(f"exclusion {amendment['index']} was already amended; amendments are recorded once per exclusion")
    existing.append(json.loads(json.dumps(amendment))); value["updated_at"]=_now(); _save(path,value); return value

def normalize_owner_direction(text):
    """Return only owner-authored direction, without terminal protocol bytes."""
    value = text.replace("\x1b[200~", "").replace("\x1b[201~", "")
    value = ANSI_DCS.sub("", value)
    value = ANSI_OSC.sub("", value)
    value = ANSI_CSI.sub("", value)
    value = ANSI_SS3.sub("", value)
    value = TERMINAL_CONTROL.sub("", value).replace("\x1b", "")
    return value.strip()

def contract_preflight(root, task):
    """Validate that a task has a usable contract before delivery work starts."""
    path = _task_path(root, task)
    if not path.is_file():
        return False, ["Completion Contract missing"]
    try:
        value = _load(path)
    except (OSError, json.JSONDecodeError):
        return False, ["Completion Contract is unreadable"]
    problems = []
    if value.get("task") != task: problems.append("contract task does not match board task")
    if not isinstance(value.get("objective"), str) or not value["objective"].strip(): problems.append("contract objective is missing")
    deliverables = value.get("deliverables")
    if not isinstance(deliverables, list) or not deliverables: problems.append("contract deliverables are missing")
    problems.extend(_immutable_problems(value))
    return not problems, problems

# 2026-11-15 (PropDesk Public): the owner's requirements said "(SPEC 1.1, 1.7)" and no deliverable of the contract cited 1.7, so the
# rename it required was never built, checked or released against. A spec section the accepted requirements cite must be cited by a
# deliverable. Citations look like "SPEC 1.1, 1.7", "SPEC 1.1-1.5" (a range of one parent), "(SPEC 11)" or "SPEC §3.4".
_SECTION = r"\d+(?:\.\d+)*"
_SPEC_CITATION = re.compile(r"\bSPEC\b\s*§?\s*(" + _SECTION + r"(?:\s*[-\u2013\u2014]\s*" + _SECTION + r")?"
                            r"(?:\s*(?:,|and|&|/)\s*§?\s*" + _SECTION + r"(?:\s*[-\u2013\u2014]\s*" + _SECTION + r")?)*)", re.I)
_SECTION_OR_RANGE = re.compile(r"(" + _SECTION + r")(?:\s*[-\u2013\u2014]\s*(" + _SECTION + r"))?")


def cited_spec_sections(text):
    """Return {section: the line that cites it} for every spec section cited in text; a range of one parent ("1.1-1.5") is expanded."""
    found = {}
    for line in str(text or "").splitlines():
        for citation in _SPEC_CITATION.finditer(line):
            for start, end in _SECTION_OR_RANGE.findall(citation.group(1)):
                sections = [start]
                head, _, first = start.rpartition(".")
                last = end.rpartition(".")[2] if end else ""
                # a same-parent, ascending range of at most 50 sections is expanded; anything else counts its two ends only
                if end and end.rpartition(".")[0] == head and first.isdigit() and last.isdigit() and 0 <= int(last) - int(first) <= 50:
                    sections = [(head + "." if head else "") + str(n) for n in range(int(first), int(last) + 1)]
                elif end:
                    sections.append(end)
                for section in sections:
                    found.setdefault(section, line.strip())
    return found


def uncovered_spec_sections(requirements_text, deliverables):
    """The spec sections the accepted requirements cite that no deliverable cites, as {section: requirement line}."""
    wanted = cited_spec_sections(requirements_text)
    covered = cited_spec_sections("\n".join(str(item) for item in deliverables or []))
    return {section: line for section, line in wanted.items() if section not in covered}


def create_contract(root, task, objective, deliverables, exclusions=None):
    """Create and save a brand-new Completion Contract for task under root.

    objective must be non-empty and deliverables must be a non-empty list of
    names; each becomes a deliverable with a generated acceptance_proof.
    exclusions, when given, are stored as-is. Raises ValueError if objective
    or deliverables is missing, or if a contract already exists for task.
    Return: the newly created contract dict.
    """
    objective = objective.strip() or "The agreed requirements"
    deliverables = list(deliverables or ["Everything in the agreed requirements"])
    path=_task_path(root, task)
    if path.exists(): raise ValueError("task contract already exists")
    original=[{"name":x,"acceptance_proof":f"Executable evidence verifies: {x}","evidence":[],"verified":False} for x in deliverables]
    value={"task":task,"objective":objective,"deliverables":original,"exclusions":exclusions or [],"status":"open","remaining_work":list(deliverables),"created_at":_now(),"updated_at":_now(),"immutable_scope":_immutable_projection({"objective":objective,"exclusions":exclusions or [],"deliverables":original})}
    _save(path,value); return value

def build_declaration(root, task):
    """The task's declared program build ({} when it declares none: a web app, a document or a legacy contract)."""
    try: return dict(_load(_task_path(root, task)).get("build") or {})
    except (OSError, ValueError): return {}

def declare_build(root, task, declaration):
    """Record the program build the task will deliver, in the contract AND in its immutable scope (so a review certifies it)."""
    path=_task_path(root,task)
    if not path.is_file(): raise ValueError("the task has no Completion Contract yet; create it first")
    value=_load(path)
    if not value.get("immutable_scope"): value["immutable_scope"]=_immutable_projection(value); value["immutable_scope_migrated_at"]=_now()
    value["build"]=json.loads(json.dumps(declaration)); value["immutable_scope"]["build"]=json.loads(json.dumps(declaration))
    value["updated_at"]=_now(); _save(path,value); return value

def migrate_legacy_scope(root, task):
    """Give task's contract an immutable_scope projection if it lacks one.

    root locates the project and task names the contract file. When the
    contract already has an immutable_scope, it is given back unchanged;
    otherwise one is computed from the current contract, stamped with a
    migration timestamp, and saved. Return: the resulting contract dict.
    """
    path=_task_path(root,task); value=_load(path)
    if value.get("immutable_scope"):
        return value
    value["immutable_scope"]=_immutable_projection(value); value["immutable_scope_migrated_at"]=_now(); _save(path,value); return value


def expand_contract(root, task, additions, actor="Product Management"):
    """Append necessary deliverables without allowing scope erasure or replacement."""
    if not additions:
        raise ValueError("at least one contract deliverable expansion is required")
    path=_task_path(root,task); value=_load(path)
    valid, problems=contract_preflight(root,task)
    if not valid: raise ValueError("cannot expand an invalid Completion Contract: "+"; ".join(problems))
    if not value.get("immutable_scope"):
        value = migrate_legacy_scope(root, task)
    existing={item["name"] for item in value["deliverables"]}
    added=[]
    for name, proof in additions:
        name, proof=name.strip(), proof.strip()
        if not name or name in existing: continue
        proof = proof or f"Executable evidence verifies: {name}"
        item={"name":name,"acceptance_proof":proof,"evidence":[],"verified":False,"added_by":actor,"added_at":_now()}
        value["deliverables"].append(item); existing.add(name); added.append(item)
    value["remaining_work"]=_remaining(value)
    value["status"]="partial" if value["remaining_work"] else "complete"; value["updated_at"]=_now(); _save(path,value)
    return {"contract":value,"added":added}

def _wholly_waived(item):
    """An owner waiver without a named part removes the whole deliverable's obligation (owner 2026-10-14)."""
    waiver=item.get("owner_waiver")
    return isinstance(waiver,dict) and bool(waiver.get("owner_words")) and not str(waiver.get("part") or "").strip()

def _remaining(value):
    """Return the names of value's deliverables that are neither verified nor wholly waived.

    Output is the list stored as a contract's remaining_work field.
    """
    return [d["name"] for d in value.get("deliverables",[]) if not d.get("verified") and not _wholly_waived(d)]

def waive_deliverable(root, task, deliverable, waiver):
    """Record (or reconcile, on an identical retry) the owner's waiver on one deliverable.

    The board is the authority for the waiver record; this is its projection onto the
    contract so completeness reads it. Name and acceptance proof are untouched, so the
    immutable scope (what a PASS certified) is unchanged.
    """
    path=_task_path(root,task); value=_load(path)
    target=next((d for d in value["deliverables"] if d["name"]==deliverable),None)
    if not target: raise ValueError("unknown deliverable")
    existing=target.get("owner_waiver")
    if existing and existing.get("id")!=waiver.get("id"): raise ValueError("deliverable already waived")
    target["owner_waiver"]=json.loads(json.dumps(waiver))
    value["remaining_work"]=_remaining(value)
    value["status"]="complete" if not value["remaining_work"] else "partial"; value["updated_at"]=_now(); _save(path,value); return value

def add_evidence(root, task, deliverable, files, for_owner=False, document=""):
    """Attach evidence files; ``for_owner`` marks each as the document the owner reads (owner 2026-10-15).

    ``document`` names the document within the deliverable (default: the
    deliverable itself); a deliverable that is several documents (a report and
    a plan) marks each under its own name, and files marked under one name are
    versions of that document.
    """
    path=_task_path(root,task); value=_load(path)
    target=next((d for d in value["deliverables"] if d["name"]==deliverable),None)
    if not target: raise ValueError("unknown deliverable")
    explicit_document=" ".join(str(document or "").split())[:MAX_DOCUMENT_TITLE]
    document=explicit_document or deliverable
    manifest=[]
    for raw in files:
        file=Path(raw); file = file if file.is_absolute() else _root(root)/file
        if not file.is_file(): raise ValueError(f"evidence missing: {file}")
        payload=file.read_bytes()
        entry={"path":str(file),"sha256":hashlib.sha256(payload).hexdigest(),"attached_at":_now()}
        if for_owner:
            entry.update({"for_owner":True,"marked_at":entry["attached_at"],"title":document_title(payload),"document":document})
        elif explicit_document:
            entry["document"]=explicit_document     # an independent named proof (the evidence gate for tasks without product code)
        manifest.append(entry)
    target["evidence"].extend(manifest); target["verified"]=True
    value["remaining_work"]=_remaining(value)
    value["status"]="complete" if not value["remaining_work"] else "partial"; value["updated_at"]=_now(); _save(path,value); return value


# ---------------------------------------------------------------------------
# The document the owner reads (owner 2026-10-15, Studio: a diagnostic's only
# deliverable was a report; the release card said "read the report" and no
# surface led to it). Delivery marks the document at attach time
# (contract-evidence --for-owner); the CTO can mark an already attached file
# (deliverable-file). The NEWEST mark per deliverable is the owner's current
# document; earlier marks are versions. Nothing here guesses from names.
# ---------------------------------------------------------------------------
MAX_OWNER_DOCUMENT_BYTES = 1024 * 1024          # the governed upload limit (board_surface.MAX_ARTIFACT_BYTES)
MAX_DOCUMENT_TITLE = 80
DOCUMENT_TITLE_WITHHELD = "Document (title withheld)"
_CLAUDE_TOKEN_SHAPE = re.compile(r"sk-ant-[A-Za-z0-9]{2,6}\d{2}-[A-Za-z0-9_-]{20,}")
_CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def document_title(payload):
    """The first Markdown heading, else the first non-empty line, of a text document; safe to display."""
    try:
        text=payload.decode("utf-8")
    except (UnicodeDecodeError, AttributeError):
        return DOCUMENT_TITLE_WITHHELD
    first=""
    for line in text.splitlines():
        stripped=line.strip()
        if not stripped or stripped in {"---","+++"}:
            continue
        first=stripped.lstrip("#").strip() if stripped.startswith("#") else stripped
        break
    first=_CONTROL_CHARACTERS.sub("", " ".join(first.split()))
    if not first or _CLAUDE_TOKEN_SHAPE.search(first):
        return DOCUMENT_TITLE_WITHHELD
    return first[:MAX_DOCUMENT_TITLE].rstrip()


def mark_owner_file(root, task, deliverable, evidence_path, document=""):
    """The CTO marks an already attached evidence file as the document the owner reads.

    Ordering is by ``marked_at``: marking the current latest is a no-op (idempotent);
    marking an earlier file again re-selects it as the latest (Codex plan check 2026-10-15
    round 4, finding 3). ``document`` names the document within the deliverable
    (default: the deliverable). Returns (contract, entry, changed).
    """
    path=_task_path(root,task); value=_load(path)
    target=next((d for d in value["deliverables"] if d["name"]==deliverable),None)
    if not target: raise ValueError("unknown deliverable")
    document=" ".join(str(document or "").split())[:MAX_DOCUMENT_TITLE] or deliverable
    wanted=str(evidence_path or "").strip()
    candidates=[e for e in target.get("evidence",[]) if isinstance(e,dict) and (e.get("path")==wanted or Path(str(e.get("path") or "")).name==wanted)]
    if not candidates:
        raise ValueError(f"{wanted!r} is not an evidence file recorded on deliverable {deliverable!r}; Delivery attaches it first with contract-evidence")
    if len(candidates)>1 and not any(e.get("path")==wanted for e in candidates):
        raise ValueError(f"{wanted!r} names more than one recorded evidence file; give the full recorded path")
    entry=next((e for e in candidates if e.get("path")==wanted), candidates[0])
    latest=current_owner_document(target, document)
    if latest is entry:
        return value, entry, False
    entry["for_owner"]=True; entry["marked_at"]=_now(); entry["document"]=document
    if document in retired_documents(target):
        target["retired_documents"]=sorted(retired_documents(target)-{document})
    if not entry.get("title"):
        try:
            entry["title"]=document_title(Path(entry["path"]).read_bytes())
        except OSError:
            entry["title"]=DOCUMENT_TITLE_WITHHELD
    value["updated_at"]=_now(); _save(path,value)
    return value, entry, True


def _document_name(item, entry):
    """Return entry's document name, falling back to item's deliverable name.

    Output is entry's "document" field when set, else item's "name", else
    an empty string.
    """
    return str(entry.get("document") or item.get("name") or "")


def retired_documents(item):
    """Return the set of document names item has retired.

    item is a deliverable dict; its "retired_documents" list (or none) is
    read. Output: that list as a set.
    """
    return set(item.get("retired_documents") or [])


def retire_owner_document(root, task, deliverable, document):
    """The CTO retires a mistaken document name: its files stop counting as the owner's current document.

    The deliverable and its evidence are untouched (no waiver); marking a file
    under that name again revives it. Returns (contract, changed).
    """
    path=_task_path(root,task); value=_load(path)
    target=next((d for d in value["deliverables"] if d["name"]==deliverable),None)
    if not target: raise ValueError("unknown deliverable")
    document=" ".join(str(document or "").split())[:MAX_DOCUMENT_TITLE] or deliverable
    names={_document_name(target,e) for e in (target.get("evidence") or []) if isinstance(e,dict) and e.get("for_owner")}
    if document not in names:
        raise ValueError(f"{document!r} is not a document marked for the owner on deliverable {deliverable!r}")
    if document in retired_documents(target):
        return value, False
    target["retired_documents"]=sorted(retired_documents(target)|{document})
    value["updated_at"]=_now(); _save(path,value)
    return value, True


def owner_documents(item, document=None):
    """A deliverable's marked files, newest mark first; ``document`` narrows to one named document; retired names are left out."""
    retired=retired_documents(item)
    marked=[e for e in (item.get("evidence") or []) if isinstance(e,dict) and e.get("for_owner")
            and _document_name(item,e) not in retired
            and (document is None or _document_name(item,e)==document)]
    return sorted(marked, key=lambda e: (str(e.get("marked_at") or e.get("attached_at") or ""), ), reverse=True)


def current_owner_document(item, document=None):
    """Return item's newest owner-marked file, optionally narrowed to document.

    document, when given, selects only entries under that document name.
    Output is the newest matching entry, or None if there is none.
    """
    documents=owner_documents(item, document)
    return documents[0] if documents else None


def owner_document_manifest(value):
    """Every deliverable's documents (newest mark per document name first), waived deliverables excluded."""
    manifest=[]
    for index,item in enumerate(value.get("deliverables") or []):
        if not isinstance(item,dict) or _wholly_waived(item):
            continue
        seen_documents=set()
        for entry in owner_documents(item):
            name=_document_name(item,entry)
            manifest.append({
                "deliverable": item.get("name"), "deliverable_index": index, "document": name,
                "evidence_index": (item.get("evidence") or []).index(entry),
                "path": entry.get("path"), "sha256": entry.get("sha256"),
                "title": entry.get("title") or DOCUMENT_TITLE_WITHHELD,
                "marked_at": entry.get("marked_at") or entry.get("attached_at") or "",
                "attached_at": entry.get("attached_at") or "",
                "latest": name not in seen_documents,
            })
            seen_documents.add(name)
    return manifest


def readable_document(path_value, sha256, roots):
    """Can this recorded document be shown to the owner right now? -> (ok, reason, bytes).

    The read is the one ``board_client._read_artifact`` already does for uploads:
    lstat, no-follow non-blocking open, fstat agreement, bounded read from the
    descriptor; then containment under ``roots``, the recorded SHA-256, UTF-8 and
    the Claude-token shape. Every failure is a plain reason; nothing here blocks.
    """
    import os, stat as _stat
    path=Path(str(path_value or ""))
    if not str(path_value or "").strip():
        return False, "no file is recorded", b""
    try:
        resolved=Path(os.path.realpath(path))
    except OSError as error:
        return False, f"the document's storage is not reachable ({error.__class__.__name__})", b""
    allowed=False
    for base in roots or []:
        try:
            base_resolved=Path(os.path.realpath(str(base)))
        except OSError:
            continue
        if resolved==base_resolved or base_resolved in resolved.parents:
            allowed=True; break
    if not allowed:
        return False, "the file is outside this project's folders", b""
    try:
        before=os.lstat(path)
    except FileNotFoundError:
        return False, "the file is gone", b""
    except OSError as error:
        return False, f"the document's storage is not reachable ({error.__class__.__name__})", b""
    if not _stat.S_ISREG(before.st_mode) or before.st_nlink!=1:
        return False, "the file is not a regular single-link file", b""
    if before.st_size>MAX_OWNER_DOCUMENT_BYTES:
        return False, f"the file is larger than {MAX_OWNER_DOCUMENT_BYTES//1024} KiB, more than Mission Control shows", b""
    flags=os.O_RDONLY|getattr(os,"O_CLOEXEC",0)|getattr(os,"O_NOFOLLOW",0)|getattr(os,"O_NONBLOCK",0)
    try:
        descriptor=os.open(path, flags)
    except OSError as error:
        return False, f"the file could not be opened ({error.__class__.__name__})", b""
    try:
        opened=os.fstat(descriptor)
        if (not _stat.S_ISREG(opened.st_mode) or opened.st_nlink!=1
                or (opened.st_dev,opened.st_ino)!=(before.st_dev,before.st_ino) or opened.st_size!=before.st_size):
            return False, "the file changed while it was being opened", b""
        chunks=[]; remaining=MAX_OWNER_DOCUMENT_BYTES+1
        while remaining:
            chunk=os.read(descriptor, min(64*1024, remaining))
            if not chunk: break
            chunks.append(chunk); remaining-=len(chunk)
    except OSError as error:
        return False, f"the file could not be read ({error.__class__.__name__})", b""
    finally:
        os.close(descriptor)
    payload=b"".join(chunks)
    if len(payload)>MAX_OWNER_DOCUMENT_BYTES:
        return False, f"the file is larger than {MAX_OWNER_DOCUMENT_BYTES//1024} KiB, more than Mission Control shows", b""
    if hashlib.sha256(payload).hexdigest()!=str(sha256 or ""):
        return False, "the file changed since it was attached (its content no longer matches the record)", b""
    try:
        text=payload.decode("utf-8")
    except UnicodeDecodeError:
        return False, "the file is not a text document", b""
    if _CLAUDE_TOKEN_SHAPE.search(text):
        return False, "the file contains what looks like a Claude login token and is not shown", b""
    return True, "", payload

def contract_complete(root, task):
    """Check whether task's contract under root is fully complete.

    Verifies required fields are present, remaining_work is empty, every
    non-waived deliverable has verified evidence, every exclusion has an
    approver and reason, and the immutable scope is intact. Return: a tuple
    of (whether it is complete, the list of problems found, the loaded
    contract value).
    """
    value=_load(_task_path(root,task)); problems=[]
    if not REQUIRED_CONTRACT.issubset(value): problems.append("contract fields missing")
    if value.get("remaining_work"): problems.append("remaining work is not empty")
    if any((not d.get("verified") or not d.get("evidence")) and not _wholly_waived(d) for d in value.get("deliverables",[])): problems.append("deliverables lack verified evidence")
    if any(not e.get("approved_by") or not e.get("reason") for e in value.get("exclusions",[])): problems.append("exclusion lacks approval/reason")
    problems.extend(_immutable_problems(value))
    return (not problems, problems, value)


def scope_problems(root, task):
    """The product part of the contract check (owner 2026-11-20: only it may hold a release): the agreed scope is unchanged and
    every exclusion carries the owner's approval. No contract recorded: nothing to violate. A contract that cannot be read or
    has the wrong shape cannot prove its scope, so it is a problem. Return: the list of problems."""
    path = _task_path(root, task)
    if not Path(path).exists():
        return []
    try:
        value = _load(path)
        return list(_immutable_problems(value))
    except (OSError, ValueError, TypeError, AttributeError, KeyError):
        return ["the Completion Contract could not be read, so its agreed scope cannot be checked"]


# Owner (2026-10-23): "the owner never runs or verifies a test". A deliverable that is about the agents' own tests is their job, not a step.
AGENT_TEST_DELIVERABLE = re.compile(
    r"\b(automated|unit|integration|regression|e2e|end-to-end)\b.*\btests?\b|\btests?\b.*\b(pass|passing|suite|coverage)\b|\btest suite\b|\bpytest\b|\bunittest\b|\blint", re.I)


def owner_test_steps(root, task, limit=8):
    """Derive a bounded owner checklist from the immutable deliverable names."""
    try:
        value = _load(_task_path(root, task))
    except (OSError, TypeError, json.JSONDecodeError):
        value = {}
    steps = []
    waived = 0
    deliverables = value.get("deliverables") if isinstance(value, dict) else []
    if isinstance(deliverables, list):
        for item in deliverables:
            if not isinstance(item, dict):
                continue
            if _wholly_waived(item):
                # Owner 2026-10-14: never ask the owner to verify what they waived.
                waived += 1
                continue
            name = " ".join(str(item.get("name", "")).split())[:500].strip(" .")
            if AGENT_TEST_DELIVERABLE.search(name):
                continue
            if name and name not in steps:
                steps.append(name)
            if len(steps) >= max(1, min(int(limit), 8)):
                break
    if not steps and waived:
        return ["Every deliverable of this task was waived by you; nothing is left to verify."]
    if not steps:
        return ["Verify the released result against the final agreed requirements shown above."]
    return [f"Verify {step}." for step in steps]

def _ledger_cells(line):
    """Split one Markdown table line into its trimmed cell values.

    Strips a leading and trailing "|" from line, then splits on "|"
    characters that are not inside a backtick-quoted code span. Return: the
    list of cell strings.
    """
    value = line.strip()
    if value.startswith("|"):
        value = value[1:]
    if value.endswith("|"):
        value = value[:-1]
    cells = []
    current = []
    in_code = False
    for character in value:
        if character == "`":
            in_code = not in_code
        if character == "|" and not in_code:
            cells.append("".join(current).strip())
            current = []
        else:
            current.append(character)
    cells.append("".join(current).strip())
    return cells


# Owner 2026-11-24: "if an issue is not a material bug in the app it should not stop the delivery of the product, should apply everywhere,
# everytime". A ledger is read for what it says: names of columns, row ids and wording never stop a step. Only a ledger nothing can be run
# from is refused. A row that cannot be run is left out of the run with its reason and is never counted as passed.
_COLUMN_WORDS = {
    "simulation_command": ("simulation command", "command", "simulation", "test command"),
    "expected_response": ("expected system response", "expected response", "expected result", "expected"),
    "observed_response": ("observed system response", "observed response", "observed result", "observed", "actual"),
    "result": ("qa result", "result", "status", "verdict", "outcome"),
    "what": ("what was tested", "scenario", "description", "what", "test"),
    "requirement": ("requirement", "requirements"),
}


def _column(header: list[str], names: tuple[str, ...]) -> int | None:
    """The column of header whose name is one of names, exactly first, else starting with one; None when there is none."""
    cells = [cell.strip("*` ").casefold() for cell in header]
    for match in (lambda cell, name: cell == name, lambda cell, name: cell.startswith(name)):
        for name in names:
            for index, cell in enumerate(cells):
                if match(cell, name):
                    return index
    return None


def _is_rule_line(cells: list[str]) -> bool:
    """A markdown table's separator line."""
    return bool(cells) and all(not cell.strip("-: ") for cell in cells)


def scenario_table(path: Path) -> tuple[dict[str, int], list[dict[str, Any]], list[str]]:
    """The ledger's table read once for everybody: (columns, rows, notes). A row keeps its own id when it is a scenario id and gets the
    next free S-<n> otherwise, so every reader of the same file sees the same ids."""
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    tables = [(number, _ledger_cells(line)) for number, line in enumerate(lines) if line.strip().startswith("|") or "|" in line.strip()]
    start = next((position for position, (_n, cells) in enumerate(tables) if cells and cells[0].strip("*` ").casefold() == "id"), None)
    if start is None:
        start = next((position for position, (_n, cells) in enumerate(tables)
                      if len(cells) > 1 and _column(cells, _COLUMN_WORDS["simulation_command"]) is not None), None)
    if start is None:
        return {}, [], ["ledger has no scenario table"]
    header = tables[start][1]
    columns = {field: index for field, names in _COLUMN_WORDS.items() if (index := _column(header, names)) is not None}
    has_id = header[0].strip("*` ").casefold() == "id"
    rows: list[dict[str, Any]] = []
    for _number, cells in tables[start + 1:]:
        if _is_rule_line(cells) or cells == header or (has_id and cells[0].strip("*` ").casefold() == "id"):
            continue
        rows.append({"cells": cells, "id": cells[0].strip("*` ") if has_id else ""})
    taken = {row["id"] for row in rows if SCENARIO_ID.fullmatch(row["id"])}
    seen: set[str] = set()
    notes: list[str] = []
    number = 0
    for row in rows:
        if SCENARIO_ID.fullmatch(row["id"]) and row["id"] not in seen:
            seen.add(row["id"])
            continue
        number += 1
        while f"S-{number:03d}" in taken:
            number += 1
        notes.append(f"row {row['id'] or len(seen) + 1!r} was numbered S-{number:03d}")
        row["id"] = f"S-{number:03d}"
        taken.add(row["id"])
        seen.add(row["id"])
    return columns, rows, notes


def _scenario_rows(path: Path, *, require_owner_description: bool = False):
    """The ledger's rows that can be run, and why there is none ([] when there is at least one). Form is never a reason."""
    rows, problems, _notes = scenario_plan(path)
    return rows, problems


def scenario_plan(path: Path) -> tuple[list[dict[str, Any]], list[str], list[str]]:
    """(rows the board can run, problems, notes). problems is non-empty only when nothing can be run; notes name every row left out."""
    if not path.is_file():
        return [], [f"ledger missing: {path}"], []
    columns, table, notes = scenario_table(path)
    if "simulation_command" not in columns:
        return [], ["ledger has no scenario table with a command column"], notes
    rows = []
    for row in table:
        cells = row["cells"]

        def cell(field: str) -> str:
            index = columns.get(field)
            return cells[index].strip() if index is not None and index < len(cells) else ""

        command = cell("simulation_command").strip("`").strip()
        result = cell("result") or "PASS"          # an empty cell says nothing; the board runs the row and its own run decides
        if result.upper().startswith(("N/A", "DEFERRED")):
            notes.append(f"{row['id']} was not run: the ledger says {result}")
            continue
        if not command or command.casefold() in {"n/a", "none", "tbd", "todo", "<command>"}:
            notes.append(f"{row['id']} was not run: it names no command")
            continue
        if not runnable_test_command(command):
            notes.append(f"{row['id']} was not run: the board cannot run its command as one test ({command[:80]})")
            continue
        rows.append({
            "id": row["id"],
            "command": command,
            "expected_response": cell("expected_response") or "the command passes",
            "observed_response": cell("observed_response") or "not written in the ledger",
            "result": result,
            "what_was_tested": cell("what"),
            # the accepted requirement numbers this row says it tests (empty without the column)
            "requirements": sorted({int(number) for number in re.findall(r"\d+", cell("requirement"))}),
        })
    notes = [redaction.redact(note) for note in notes]         # a cell may hold anything; a credential never reaches a note or a message
    problems = [] if rows else ["nothing in the ledger can be run: " + ("; ".join(notes) if notes else "it has no rows")]
    return rows, problems, notes


def scenario_simulations(path: Path):
    """The executable plan of a ledger: (it can be run, why not, rows)."""
    rows, problems, _notes = scenario_plan(path)
    return not problems, problems, rows


def scenario_submission_simulations(path: Path):
    """The executable plan of a newly submitted ledger; the same reading as scenario_simulations."""
    return scenario_simulations(path)


def scenario_ledger_complete(path: Path):
    """The ledger can be run and no row says it failed (what its own author wrote as failing is not complete)."""
    rows, problems, _notes = scenario_plan(path)
    problems = list(problems)
    for row in rows:
        if re.match(r"\W*(fail|failed|blocked|error)\b", row["result"], re.I):
            problems.append(f"{row['id']} has non-passing QA result: {row['result']}")
    return not problems, problems


def scenario_submission_complete(path: Path):
    """The same reading as scenario_ledger_complete."""
    return scenario_ledger_complete(path)


def scenario_ledger_exists(path: Path):
    """The ledger has at least one scenario the board can run."""
    valid, problems, _rows = scenario_simulations(path)
    return valid, problems


def scenario_submission_exists(path: Path):
    """The same reading as scenario_ledger_exists."""
    return scenario_ledger_exists(path)


def scenario_fingerprints(path: Path):
    """Return executable scenario commands for independent-ledger checks."""
    rows, _ = _scenario_rows(path)
    fingerprints = {row["command"] for row in rows if row.get("command")}
    if fingerprints:
        return fingerprints

    # Valid ledgers are resolved by their named header above. Retain only a
    # narrow compatibility path for historical headerless tables; if a header
    # exists but cannot be parsed, fail closed instead of guessing a column.
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return set()
    if any(cells and cells[0].casefold() == "id" for cells in map(_ledger_cells, lines)):
        return set()
    for line in lines:
        cells = _ledger_cells(line)
        if not cells or not SCENARIO_ID.fullmatch(cells[0]):
            continue
        if len(cells) >= 3:
            fingerprints.add(cells[2].strip().strip("`").strip())
    return fingerprints

def lint_handoff(root, task, text):
    """Validate a handoff message's text against task's contract under root.

    Checks that text's first four lines are the required OBJECTIVE
    STATUS/Completed/Remaining/Evidence headers, that the status is one of
    HANDOFF_STATUSES, that a COMPLETE claim matches an actually complete
    contract and states no remaining work, and that completion language is
    not used before the contract is complete. Return: a dict with "valid",
    "problems", and "contract_complete".
    """
    complete, problems, _ = contract_complete(root,task)
    first=text.splitlines()[:4]
    required=["OBJECTIVE STATUS:","Completed:","Remaining:","Evidence:"]
    if len(first)<4 or any(not first[i].startswith(required[i]) for i in range(4)): problems.append("handoff must start with required four lines")
    status=first[0].split(":",1)[1].strip() if first else ""
    if status not in HANDOFF_STATUSES: problems.append("handoff status must be COMPLETE, PARTIAL, or BLOCKED")
    if status=="COMPLETE" and not complete: problems.append("COMPLETE claim with incomplete contract")
    if status=="COMPLETE" and (len(first) < 3 or first[2].split(":", 1)[1].strip().lower() not in {"none", "none."}): problems.append("COMPLETE handoff requires Remaining: none")
    if status in {"PARTIAL", "BLOCKED"} and (len(first) < 3 or not first[2].split(":", 1)[1].strip()): problems.append("PARTIAL/BLOCKED handoff must name remaining work")
    if status!="COMPLETE" and COMPLETION_WORDS.search(text): problems.append("completion language is forbidden before contract is complete")
    return {"valid":not problems,"problems":problems,"contract_complete":complete}

def main(argv=None):
    """Run the contract CLI: parse argv and dispatch to the matching subcommand.

    Supports create, evidence, expand, migrate-scope, and
    lint subcommands, printing the resulting JSON output. Return: 0 on
    success (or when the command's own "valid" field is true), 1 when it is
    false, or 2 if a ValueError/FileNotFoundError/JSONDecodeError is raised.
    """
    p=argparse.ArgumentParser(); add_context_arguments(p); s=p.add_subparsers(dest="cmd",required=True)
    q=s.add_parser("create"); q.add_argument("--task",required=True); q.add_argument("--objective",required=True); q.add_argument("--deliverable",action="append",required=True)
    q=s.add_parser("evidence"); q.add_argument("--task",required=True); q.add_argument("--deliverable",required=True); q.add_argument("--file",action="append",required=True)
    q=s.add_parser("expand"); q.add_argument("--task",required=True); q.add_argument("--deliverable",action="append",required=True, metavar="NAME|ACCEPTANCE_PROOF")
    q=s.add_parser("migrate-scope"); q.add_argument("--task",required=True)
    q=s.add_parser("lint"); q.add_argument("--task",required=True); q.add_argument("--file",required=True)
    a=p.parse_args(argv); root=context_from_args(a)
    try:
        if a.cmd=="create": out=create_contract(root,a.task,a.objective,a.deliverable)
        elif a.cmd=="evidence": out=add_evidence(root,a.task,a.deliverable,a.file)
        elif a.cmd=="expand": out=expand_contract(root,a.task,[tuple(item.split("|",1)) if "|" in item else ("","") for item in a.deliverable])
        elif a.cmd=="migrate-scope": out=migrate_legacy_scope(root,a.task)
        else: out=lint_handoff(root,a.task,Path(a.file).read_text(encoding="utf-8"))
    except (ValueError,FileNotFoundError,json.JSONDecodeError) as e: print(f"error: {e}",file=sys.stderr); return 2
    print(json.dumps(out,indent=2)); return 0 if out.get("valid",True) else 1
if __name__=="__main__": raise SystemExit(main())
