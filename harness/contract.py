#!/usr/bin/env python3
# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
"""Completion Contracts, profile validation, evidence manifests, and claim linting."""
from __future__ import annotations
import argparse, hashlib, json, re, sys
from datetime import datetime, timezone
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from harness.project_context import ProjectRoot, add_context_arguments, context_from_args, project_context

REQUIRED_PROFILE = {"project_name", "default_branch", "test_command", "build_command", "health_command", "deployment_channels"}
REQUIRED_CONTRACT = {"objective", "deliverables", "exclusions", "status", "remaining_work"}
COMPLETION_WORDS = re.compile(r"\b(done|ready|complete|finished|shipped)\b", re.I)
APPROVED_EXCEPTION = re.compile(r"^(?:N/A|DEFERRED):\s*(?:CTO|PRODUCT OWNER)\b\s*[-:]\s*(.{8,})$", re.I)
HANDOFF_STATUSES = {"COMPLETE", "PARTIAL", "BLOCKED"}
ANSI_DCS = re.compile(r"(?:\x1bP|\x90).*?(?:\x1b\\|\x9c)", re.S)
ANSI_OSC = re.compile(r"(?:\x1b\]|\x9d).*?(?:\x07|\x1b\\|\x9c)", re.S)
ANSI_CSI = re.compile(r"(?:\x1b\[|\x9b)[0-?]*[ -/]*[@-~]")
ANSI_SS3 = re.compile(r"(?:\x1bO|\x8f).")
TERMINAL_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1a\x1c-\x1f\x7f-\x9f]")
SCENARIO_ID = re.compile(r"^S-[A-Za-z0-9._-]+$")
# Environment-varied evidence (VAR=value cmd) is exactly what hostile-env and
# recovery scenarios must record; the prefix form is plain POSIX, carries no
# shell control operators, and is executed with the same safety as the bare
# command (issue row 6).
EXECUTABLE_TEST_COMMAND = re.compile(
    r"^(?:[A-Za-z_][A-Za-z0-9_]*=[^\s;&|<>`$]*\s+)*"
    r"(?:python(?:3)?\s+-m\s+(?:unittest|pytest)\b|pytest\b|"
    r"npm\s+(?:test|run\s+test)\b|go\s+test\b|cargo\s+test\b|"
    r"make\s+test\b|gradle\s+test\b)",
    re.I,
)
SHELL_CONTROL = re.compile(r"(?:&&|\|\||[;&|<>]|\$\(|\r|\n)")
SCENARIO_HEADERS = {
    "simulation_command": "Simulation command",
    "expected_response": "Expected system response",
    "observed_response": "Observed system response",
    "result": "QA result",
}
OWNER_DESCRIPTION_HEADER = "What was tested"
OWNER_DESCRIPTION_MIN_LENGTH = 24
OWNER_DESCRIPTION_MAX_LENGTH = 500
OWNER_DESCRIPTION_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
OWNER_DESCRIPTION_HASH = re.compile(r"^(?:[0-9a-f]{7,64}|sha(?:1|256)\s*[:=]?\s*[0-9a-f]{7,64})$", re.I)
OWNER_DESCRIPTION_IDENTIFIER = re.compile(r"^(?:S-)?[A-Za-z0-9]+(?:[._:-][A-Za-z0-9]+)+$")
OWNER_DESCRIPTION_INTERNAL_STATE = re.compile(r"^[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+$")
OWNER_DESCRIPTION_PATH = re.compile(r"^(?:\.?\.?/|~?/|[A-Za-z]:[\\/]|[^\s]+[/\\][^\s]+)$")
OWNER_DESCRIPTION_JARGON_WORDS = {
    "cas", "certification", "certified", "challenge", "command", "digest",
    "executed", "execution", "hash", "identifier", "ledger", "pass", "passed",
    "qa", "regression", "reviewer", "scenario", "suite", "test", "tested",
    "testing", "validation",
}
OWNER_DESCRIPTION_STOP_WORDS = {
    "a", "an", "and", "as", "at", "by", "for", "from", "in", "is", "it",
    "of", "on", "or", "the", "this", "to", "was", "with",
}

def _now(): return datetime.now(timezone.utc).isoformat()
def _root(root: ProjectRoot): return project_context(root).code_root
def _task_path(root: ProjectRoot, task): return project_context(root).storage_path("tasks", f"{task}.json")
def _load(path): return json.loads(path.read_text(encoding="utf-8"))
def _save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True); temp=path.with_suffix(".tmp")
    temp.write_text(json.dumps(value, indent=2, sort_keys=True)+"\n", encoding="utf-8"); temp.replace(path)

def _immutable_projection(value):
    return {"objective":value.get("objective"),"exclusions":json.loads(json.dumps(value.get("exclusions",[]))),"deliverables":[{"name":item.get("name"),"acceptance_proof":item.get("acceptance_proof")} for item in value.get("deliverables",[])]}

def _immutable_problems(value):
    immutable=value.get("immutable_scope")
    if not immutable: return []
    current=_immutable_projection(value)
    expected={"objective":immutable.get("objective"),"exclusions":immutable.get("exclusions",[]),"deliverables":immutable.get("deliverables",[])}
    current["deliverables"]=current.get("deliverables",[])[:len(expected["deliverables"])]
    return ["original contract objective, exclusions, and deliverable names/proofs/order are immutable"] if current != expected else []

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
    if value.get("status") not in {"open", "partial", "blocked", "complete"}: problems.append("contract status is invalid")
    if not isinstance(value.get("remaining_work"), list): problems.append("contract remaining work is missing")
    if not isinstance(value.get("exclusions"), list): problems.append("contract exclusions are missing")
    deliverables = value.get("deliverables")
    if not isinstance(deliverables, list) or not deliverables: problems.append("contract deliverables are missing")
    else:
        for item in deliverables:
            if not isinstance(item, dict) or not item.get("name") or not item.get("acceptance_proof"):
                problems.append("every deliverable needs a name and executable acceptance proof")
                break
    problems.extend(_immutable_problems(value))
    return not problems, problems

def validate_profile(path: Path):
    profile=_load(path); missing=sorted(k for k in REQUIRED_PROFILE if not profile.get(k))
    return {"valid": not missing, "missing": missing, "profile": profile}

def create_contract(root, task, objective, deliverables, exclusions=None):
    if not objective.strip() or not deliverables: raise ValueError("objective and at least one deliverable are required")
    path=_task_path(root, task)
    if path.exists(): raise ValueError("task contract already exists")
    original=[{"name":x,"acceptance_proof":f"Executable evidence verifies: {x}","evidence":[],"verified":False} for x in deliverables]
    value={"task":task,"objective":objective,"deliverables":original,"exclusions":exclusions or [],"status":"open","remaining_work":list(deliverables),"created_at":_now(),"updated_at":_now(),"immutable_scope":_immutable_projection({"objective":objective,"exclusions":exclusions or [],"deliverables":original})}
    _save(path,value); return value

def migrate_legacy_scope(root, task):
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
    immutable=value.get("immutable_scope")
    if not immutable:
        raise ValueError("legacy contract requires audited immutable-scope migration before expansion")
    existing={item["name"] for item in value["deliverables"]}
    added=[]
    for name, proof in additions:
        name, proof=name.strip(), proof.strip()
        if not name or not proof: raise ValueError("expanded deliverables require a name and acceptance proof")
        if name in existing: raise ValueError(f"deliverable already exists: {name}")
        item={"name":name,"acceptance_proof":proof,"evidence":[],"verified":False,"added_by":actor,"added_at":_now()}
        value["deliverables"].append(item); existing.add(name); added.append(item)
    value["remaining_work"]=[item["name"] for item in value["deliverables"] if not item.get("verified")]
    value["status"]="partial" if value["remaining_work"] else "complete"; value["updated_at"]=_now(); _save(path,value)
    return {"contract":value,"added":added}

def add_evidence(root, task, deliverable, files):
    path=_task_path(root,task); value=_load(path)
    target=next((d for d in value["deliverables"] if d["name"]==deliverable),None)
    if not target: raise ValueError("unknown deliverable")
    manifest=[]
    for raw in files:
        file=Path(raw); file = file if file.is_absolute() else _root(root)/file
        if not file.is_file(): raise ValueError(f"evidence missing: {file}")
        manifest.append({"path":str(file),"sha256":hashlib.sha256(file.read_bytes()).hexdigest()})
    target["evidence"].extend(manifest); target["verified"]=True
    value["remaining_work"]=[d["name"] for d in value["deliverables"] if not d["verified"]]
    value["status"]="complete" if not value["remaining_work"] else "partial"; value["updated_at"]=_now(); _save(path,value); return value

def contract_complete(root, task):
    value=_load(_task_path(root,task)); problems=[]
    if not REQUIRED_CONTRACT.issubset(value): problems.append("contract fields missing")
    if value.get("remaining_work"): problems.append("remaining work is not empty")
    if any(not d.get("verified") or not d.get("evidence") for d in value.get("deliverables",[])): problems.append("deliverables lack verified evidence")
    if any(not e.get("approved_by") or not e.get("reason") for e in value.get("exclusions",[])): problems.append("exclusion lacks approval/reason")
    problems.extend(_immutable_problems(value))
    return (not problems, problems, value)


def owner_test_steps(root, task, limit=8):
    """Derive a bounded owner checklist from the immutable deliverable names."""
    try:
        value = _load(_task_path(root, task))
    except (OSError, TypeError, json.JSONDecodeError):
        value = {}
    steps = []
    deliverables = value.get("deliverables") if isinstance(value, dict) else []
    if isinstance(deliverables, list):
        for item in deliverables:
            if not isinstance(item, dict):
                continue
            name = " ".join(str(item.get("name", "")).split())[:500].strip(" .")
            if name and name not in steps:
                steps.append(name)
            if len(steps) >= max(1, min(int(limit), 8)):
                break
    if not steps:
        return ["Verify the released result against the final agreed requirements shown above."]
    return [f"Verify {step}." for step in steps]

def _ledger_cells(line):
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


def _owner_description_problem(value: str) -> str:
    """Return a stable reason when owner-facing scenario wording is unsafe."""
    text = value.strip()
    if not text:
        return f"requires a non-empty {OWNER_DESCRIPTION_HEADER} value"
    if len(text) < OWNER_DESCRIPTION_MIN_LENGTH or len(text) > OWNER_DESCRIPTION_MAX_LENGTH:
        return (
            f"{OWNER_DESCRIPTION_HEADER} must be between "
            f"{OWNER_DESCRIPTION_MIN_LENGTH} and {OWNER_DESCRIPTION_MAX_LENGTH} characters"
        )
    if OWNER_DESCRIPTION_CONTROL.search(value):
        return f"{OWNER_DESCRIPTION_HEADER} must not contain control characters"
    unquoted = text.strip("`").strip()
    if EXECUTABLE_TEST_COMMAND.match(unquoted) or SHELL_CONTROL.search(unquoted):
        return f"{OWNER_DESCRIPTION_HEADER} must describe behavior, not a command"
    if (
        OWNER_DESCRIPTION_HASH.fullmatch(unquoted)
        or OWNER_DESCRIPTION_IDENTIFIER.fullmatch(unquoted)
        or OWNER_DESCRIPTION_INTERNAL_STATE.fullmatch(unquoted)
        or OWNER_DESCRIPTION_PATH.fullmatch(unquoted)
    ):
        return f"{OWNER_DESCRIPTION_HEADER} must describe behavior, not a technical identifier"
    words = re.findall(r"[A-Za-z][A-Za-z'-]*", text)
    if len(words) < 5:
        return f"{OWNER_DESCRIPTION_HEADER} must be meaningful in ordinary language"
    owner_words = [
        word.casefold() for word in words
        if word.casefold() not in OWNER_DESCRIPTION_JARGON_WORDS
        and word.casefold() not in OWNER_DESCRIPTION_STOP_WORDS
    ]
    if len(owner_words) < 3:
        return f"{OWNER_DESCRIPTION_HEADER} must describe observable behavior, not testing jargon"
    return ""


def _scenario_rows(path: Path, *, require_owner_description: bool = False):
    """Parse executable scenario rows and report structural simulation gaps."""
    if not path.is_file():
        return [], [f"ledger missing: {path}"]
    lines = path.read_text(encoding="utf-8").splitlines()
    problems = []
    header = next((cells for cells in map(_ledger_cells, lines) if cells and cells[0].lower() == "id"), None)
    if header is None:
        return [], ["ledger is missing a scenario table header beginning with ID"]
    normalized = {cell.casefold(): index for index, cell in enumerate(header)}
    owner_description_index = normalized.get(OWNER_DESCRIPTION_HEADER.casefold())
    if require_owner_description and owner_description_index is None:
        problems.append(f"ledger header is missing required column: {OWNER_DESCRIPTION_HEADER}")
    indexes = {}
    for field, label in SCENARIO_HEADERS.items():
        index = normalized.get(label.casefold())
        if index is None:
            problems.append(f"ledger header is missing required column: {label}")
        else:
            indexes[field] = index
    rows = []
    for line in lines:
        cells = _ledger_cells(line)
        if not cells or not SCENARIO_ID.fullmatch(cells[0]):
            continue
        scenario_id = cells[0]
        required_indexes = list(indexes.values())
        if owner_description_index is not None:
            required_indexes.append(owner_description_index)
        if required_indexes and max(required_indexes, default=0) >= len(cells):
            problems.append(f"{scenario_id} has fewer cells than the scenario table header")
            continue
        if len(indexes) != len(SCENARIO_HEADERS):
            continue
        command = cells[indexes["simulation_command"]].strip().strip("`").strip()
        expected = cells[indexes["expected_response"]].strip()
        observed = cells[indexes["observed_response"]].strip()
        result = cells[indexes["result"]].strip()
        owner_description = (
            cells[owner_description_index].strip()
            if owner_description_index is not None and owner_description_index < len(cells)
            else ""
        )
        if require_owner_description:
            description_problem = _owner_description_problem(owner_description)
            if description_problem:
                problems.append(f"{scenario_id} {description_problem}")
        exception = result.upper().startswith(("N/A:", "DEFERRED:"))
        if not exception:
            if not command or command.casefold() in {"n/a", "none", "tbd", "todo", "<command>"}:
                problems.append(f"{scenario_id} requires an executable Simulation command")
            elif not EXECUTABLE_TEST_COMMAND.match(command):
                problems.append(f"{scenario_id} Simulation command must invoke a recognized test runner")
            elif SHELL_CONTROL.search(command):
                problems.append(f"{scenario_id} Simulation command must not contain shell control operators")
            if not expected or expected.casefold() in {"tbd", "todo", "<expected>", "pass"}:
                problems.append(f"{scenario_id} requires a substantive Expected system response")
        rows.append({
            "id": scenario_id,
            "command": command,
            "expected_response": expected,
            "observed_response": observed,
            "result": result,
            "what_was_tested": owner_description,
        })
    if not rows:
        problems.append("ledger has no concrete scenario rows")
    return rows, problems


def scenario_simulations(path: Path):
    """Return a validated per-scenario executable simulation plan."""
    rows, problems = _scenario_rows(path)
    return not problems, problems, rows


def scenario_submission_simulations(path: Path):
    """Validate a newly submitted ledger, including owner-readable wording."""
    rows, problems = _scenario_rows(path, require_owner_description=True)
    return not problems, problems, rows


def scenario_ledger_complete(path: Path):
    """Require executable simulations, observations, and PASS/approved exceptions."""
    rows, problems = _scenario_rows(path)
    for row in rows:
        result = row["result"].upper()
        if result == "PASS":
            observed = row["observed_response"].strip()
            if not observed or observed.casefold() in {"pass", "passed", "ok", "tbd", "todo", "<observed>"}:
                problems.append(f"{row['id']} PASS requires a substantive Observed system response")
            continue
        if result.startswith("N/A:") or result.startswith("DEFERRED:"):
            if APPROVED_EXCEPTION.fullmatch(result):
                continue
            problems.append(f"{row['id']} exception requires CTO or Product Owner approval and a substantive reason")
            continue
        problems.append(f"{row['id']} has non-passing QA result: {result or '<empty>'}")
    return not problems, problems


def scenario_submission_complete(path: Path):
    """Require complete executable results and owner-readable wording."""
    rows, problems = _scenario_rows(path, require_owner_description=True)
    for row in rows:
        result = row["result"].upper()
        if result == "PASS":
            observed = row["observed_response"].strip()
            if not observed or observed.casefold() in {"pass", "passed", "ok", "tbd", "todo", "<observed>"}:
                problems.append(f"{row['id']} PASS requires a substantive Observed system response")
            continue
        if result.startswith("N/A:") or result.startswith("DEFERRED:"):
            if APPROVED_EXCEPTION.fullmatch(result):
                continue
            problems.append(f"{row['id']} exception requires CTO or Product Owner approval and a substantive reason")
            continue
        problems.append(f"{row['id']} has non-passing QA result: {result or '<empty>'}")
    return not problems, problems

def scenario_ledger_exists(path: Path):
    """Require a concrete executable simulation plan before review begins."""
    rows, problems = _scenario_rows(path)
    return bool(rows) and not problems, problems


def scenario_submission_exists(path: Path):
    """Require concrete scenarios and owner-readable wording for new intake."""
    rows, problems = _scenario_rows(path, require_owner_description=True)
    return bool(rows) and not problems, problems

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
    p=argparse.ArgumentParser(); add_context_arguments(p); s=p.add_subparsers(dest="cmd",required=True)
    q=s.add_parser("validate-profile"); q.add_argument("--profile",required=True)
    q=s.add_parser("create"); q.add_argument("--task",required=True); q.add_argument("--objective",required=True); q.add_argument("--deliverable",action="append",required=True)
    q=s.add_parser("evidence"); q.add_argument("--task",required=True); q.add_argument("--deliverable",required=True); q.add_argument("--file",action="append",required=True)
    q=s.add_parser("expand"); q.add_argument("--task",required=True); q.add_argument("--deliverable",action="append",required=True, metavar="NAME|ACCEPTANCE_PROOF")
    q=s.add_parser("migrate-scope"); q.add_argument("--task",required=True)
    q=s.add_parser("lint"); q.add_argument("--task",required=True); q.add_argument("--file",required=True)
    a=p.parse_args(argv); root=context_from_args(a)
    try:
        if a.cmd=="validate-profile": out=validate_profile(Path(a.profile))
        elif a.cmd=="create": out=create_contract(root,a.task,a.objective,a.deliverable)
        elif a.cmd=="evidence": out=add_evidence(root,a.task,a.deliverable,a.file)
        elif a.cmd=="expand": out=expand_contract(root,a.task,[tuple(item.split("|",1)) if "|" in item else ("","") for item in a.deliverable])
        elif a.cmd=="migrate-scope": out=migrate_legacy_scope(root,a.task)
        else: out=lint_handoff(root,a.task,Path(a.file).read_text(encoding="utf-8"))
    except (ValueError,FileNotFoundError,json.JSONDecodeError) as e: print(f"error: {e}",file=sys.stderr); return 2
    print(json.dumps(out,indent=2)); return 0 if out.get("valid",True) else 1
if __name__=="__main__": raise SystemExit(main())
