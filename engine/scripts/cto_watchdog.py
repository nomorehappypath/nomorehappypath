#!/usr/bin/env python3
# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
"""CTO Watchdog for a multi-agent development workflow (vendor/project-neutral).

This script performs deterministic governance checks over the multi-agent workflow.
It does not modify application code. It writes optional reports under .agents/cto/reports.

Project specifics (which repos to scan, the deployment channels a task record must
classify, the product-owner label) are read from the harness profile (profile.config)
via profile_config.py. With no profile present it falls back to safe defaults
(scan the current repo; require a "Local dev" environment row).

Checks include:
- git branch discipline
- dirty main
- active task branch has matching task record
- task record has acceptance criteria and environment classification
- REVIEW_REQUESTED tasks have evidence package sections
- REVIEW_PASSED / ACCEPTANCE_READY tasks have merge verification evidence
- unresolved CTO HOLD files

Exit codes:
0 = no blocking failures
1 = blocking failures found in one-shot mode
2 = script/config error or interrupted

Continuous mode:
Use --watch-interval 600 --only-changes to run every 10 minutes and print only when
the warning/failure/hold state changes.

Baseline:
Task ids listed in .agents/cto/baseline.txt are grandfathered: their record-quality
auto-checks are skipped so historical pre-Watchtower debt does not keep the watchdog
permanently red. Open CTO HOLD files, dirty main, and branch discipline still fire for
baselined tasks. Use --include-baseline to audit the full debt, or pass --task <id> to
inspect one baselined task in full.
"""
from __future__ import annotations

import argparse
import datetime as dt
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple

from profile_config import load_profile

STATUSES_REQUIRING_EVIDENCE = {
    "SELF_TESTED",
    "REVIEW_REQUESTED",
    "REVIEW_FAILED",
    "REVIEW_PASSED",
    "ACCEPTANCE_READY",
    "ACCEPTED_BY_OWNER",
    "DONE",
}

STATUSES_REQUIRING_MERGE = {
    "ACCEPTANCE_READY",
    "ACCEPTED_BY_OWNER",
    "DONE",
}

# Statuses at/after review where evidence placeholders and merge proof are enforced.
POST_REVIEW_STATUSES = {
    "REVIEW_REQUESTED",
    "REVIEW_PASSED",
    "ACCEPTANCE_READY",
    "ACCEPTED_BY_OWNER",
    "DONE",
}

DEFAULT_ENV_LABELS = ["Local dev"]

PLACEHOLDER_PATTERNS = [
    r"<paste",
    r"<exact command>",
    r"<command/API/UI/DB proof>",
    r"PASS/FAIL",
    r"YES/NO",
    r"<item>",
    r"<path>",
]

@dataclass
class Finding:
    level: str  # PASS, WARN, FAIL
    repo: str
    message: str
    task_id: Optional[str] = None

@dataclass
class RepoReport:
    repo_path: Path
    branch: str = "unknown"
    git_status: str = ""
    findings: List[Finding] = field(default_factory=list)

    @property
    def has_failures(self) -> bool:
        return any(f.level == "FAIL" for f in self.findings)

    @property
    def has_warnings(self) -> bool:
        return any(f.level == "WARN" for f in self.findings)


def run(cmd: List[str], cwd: Path) -> Tuple[int, str, str]:
    proc = subprocess.run(cmd, cwd=str(cwd), text=True, capture_output=True)
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


def is_git_repo(path: Path) -> bool:
    code, out, _ = run(["git", "rev-parse", "--is-inside-work-tree"], path)
    return code == 0 and out.strip() == "true"


def git_branch(path: Path) -> str:
    code, out, err = run(["git", "branch", "--show-current"], path)
    if code != 0:
        return f"unknown ({err or out})"
    return out or "DETACHED"


def git_status(path: Path) -> str:
    code, out, err = run(["git", "status", "--short"], path)
    if code != 0:
        return f"ERROR: {err or out}"
    return out


def normalize_task_id(value: str) -> str:
    value = value.strip()
    value = value.removeprefix("task/")
    value = value.removesuffix(".md")
    return value


def task_id_from_branch(branch: str) -> Optional[str]:
    if branch.startswith("task/"):
        return normalize_task_id(branch)
    return None


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return path.read_text(errors="replace")


def extract_header_value(text: str, key: str) -> Optional[str]:
    # Handles lines like "Task ID: xyz" or "Status: `REVIEW_REQUESTED`"
    pat = re.compile(rf"^\s*{re.escape(key)}\s*:\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE)
    match = pat.search(text)
    if not match:
        return None
    value = match.group(1).strip()
    value = value.strip("` ")
    # If a template line lists many allowed statuses, do not treat it as actual status.
    if " | " in value:
        return None
    return value


def section(text: str, heading_regex: str) -> str:
    match = re.search(heading_regex, text, flags=re.IGNORECASE | re.MULTILINE)
    if not match:
        return ""
    start = match.start()
    next_match = re.search(r"^##\s+", text[match.end():], flags=re.MULTILINE)
    if not next_match:
        return text[start:]
    return text[start: match.end() + next_match.start()]


def contains_placeholder(text: str) -> bool:
    return any(re.search(p, text, flags=re.IGNORECASE) for p in PLACEHOLDER_PATTERNS)


def task_records(repo: Path) -> List[Path]:
    tasks_dir = repo / ".agents" / "tasks"
    if not tasks_dir.exists():
        return []
    return sorted(tasks_dir.glob("*.md"))


def record_id_candidates(task_id: str) -> List[str]:
    """One feature may run several branches off one canonical record
    (task/<record-id>_impl, _ui, _spec ...). Try the full id, then strip
    trailing _segments, never past the date prefix plus one name segment."""
    parts = task_id.split("_")
    out = []
    while len(parts) >= 2:
        out.append("_".join(parts))
        parts = parts[:-1]
    return out


def record_declaring_branch(branch: str, repo: Path, all_repos: Optional[List[Path]]) -> Optional[str]:
    """Last-resort resolution: a record may DECLARE extra branches it covers
    (Branch:/coordination lines naming the full task/<id> ref). Scan records
    in every profiled working tree for the literal branch ref."""
    repos = [repo] + [r.resolve() for r in (all_repos or []) if r.resolve() != repo.resolve()]
    for r in repos:
        for rec in task_records(r):
            try:
                if branch in rec.read_text(encoding="utf-8", errors="ignore"):
                    return f"{rec.name} ({r.name})"
            except OSError:
                continue
    return None


def cross_filed_record(task_id: str, repo: Path, all_repos: Optional[List[Path]]) -> Optional[str]:
    """A multi-repo task keeps one canonical record on the governance-home board;
    resolve across every profiled repo before calling a record missing. The record
    may sit on the sibling's working tree OR still be on that repo's unmerged
    task/<task-id> branch (the sibling checkout is often on main) — check both.
    Returns a human-readable location, or None."""
    rel = f".agents/tasks/{task_id}.md"
    for other in all_repos or []:
        other = other.resolve()
        if other == repo:
            continue
        if (other / rel).exists():
            return f"{other.name} board"
        for ref in (f"task/{task_id}", f"origin/task/{task_id}"):
            code, _out, _err = run(["git", "cat-file", "-e", f"{ref}:{rel}"], other)
            if code == 0:
                return f"{other.name} branch {ref}"
    return None


def load_baseline(repo: Path) -> set:
    """Read grandfathered pre-Watchtower task ids from .agents/cto/baseline.txt.

    Baselined tasks have their record-quality auto-checks (acceptance criteria,
    environment classification, evidence package, merge evidence, docs
    obligation) suppressed so historical debt does not keep the watchdog
    permanently red. Open CTO HOLD files, dirty main, and branch discipline are
    NOT suppressed. Returns an empty set when the file is absent (pre-baseline
    behaviour: everything is checked).
    """
    f = repo / ".agents" / "cto" / "baseline.txt"
    if not f.exists():
        return set()
    ids = set()
    for line in read_text(f).splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        ids.add(normalize_task_id(line))
    return ids


def record_task_id(path: Path) -> str:
    """Resolve a task record's id the same way check_task_record does."""
    text = read_text(path)
    tid = extract_header_value(text, "Task ID") or normalize_task_id(path.name)
    return normalize_task_id(tid)


def hold_files(repo: Path, task_id: Optional[str] = None) -> List[Path]:
    holds_dir = repo / ".agents" / "cto" / "holds"
    if not holds_dir.exists():
        return []
    # A hold file is named <task-id>.md. README.md (and any other docs) in this
    # directory are not holds.
    files = [p for p in sorted(holds_dir.glob("*.md")) if p.name.lower() != "readme.md"]
    if task_id:
        wanted = normalize_task_id(task_id)
        files = [p for p in files if normalize_task_id(p.name) == wanted]
    return files


def hold_is_open(path: Path) -> bool:
    text = read_text(path)
    status = extract_header_value(text, "Status")
    if status:
        return status.upper() == "OPEN"
    # Conservative: if a hold file exists and does not explicitly say RESOLVED, treat as open.
    return "Status: RESOLVED" not in text


def add(report: RepoReport, level: str, message: str, task_id: Optional[str] = None) -> None:
    report.findings.append(Finding(level=level, repo=report.repo_path.name, message=message, task_id=task_id))


def check_task_record(
    path: Path,
    report: RepoReport,
    mode: str,
    requested_task: Optional[str],
    env_labels: List[str],
) -> None:
    text = read_text(path)
    task_id = extract_header_value(text, "Task ID") or normalize_task_id(path.name)
    task_id = normalize_task_id(task_id)

    if requested_task and normalize_task_id(requested_task) != task_id:
        return

    status = extract_header_value(text, "Status") or "UNKNOWN"
    status = status.upper()

    completion = section(text, r"^##\s+0\.\s+Completion Contract")
    if not completion:
        level = "FAIL" if status in POST_REVIEW_STATUSES else "WARN"
        add(report, level, f"Task record {path.name} has no Completion Contract section.", task_id)
    else:
        required_contract_terms = [
            "User objective", "Required deliverable", "Acceptance proof", "Exclusions",
            "Current contract status", "Remaining work",
        ]
        missing_contract_terms = [term for term in required_contract_terms if term.lower() not in completion.lower()]
        if missing_contract_terms:
            add(report, "FAIL", f"Task {task_id} Completion Contract missing: {', '.join(missing_contract_terms)}.", task_id)
        elif status in STATUSES_REQUIRING_MERGE:
            contract_complete = "current contract status: `complete`" in completion.lower()
            remaining_clear = bool(re.search(r"remaining work[^\n]*\n\s*(?:[-*]\s*)?(?:none|nothing)\b", completion, flags=re.IGNORECASE))
            if not contract_complete or not remaining_clear:
                add(report, "FAIL", f"Task {task_id} is {status} but its Completion Contract is not complete with empty remaining work.", task_id)
            else:
                add(report, "PASS", f"Task {task_id} Completion Contract is complete.", task_id)

    ac = section(text, r"^##\s+5\.\s+Acceptance criteria") or section(text, r"^##\s+Acceptance criteria")
    # Case-insensitive: the intent is "has Given/When/Then acceptance criteria".
    # Records legitimately write "**Given** … when … then …" in Title/lower case;
    # requiring literal uppercase GIVEN/WHEN/THEN manufactured false "lacks
    # Given/When/Then" findings and forced authors to SHOUT their criteria. Casing
    # is irrelevant to whether the three clauses are present.
    ac_upper = ac.upper()
    has_ac = "GIVEN" in ac_upper and "WHEN" in ac_upper and "THEN" in ac_upper
    if not has_ac:
        add(report, "FAIL", f"Task record {path.name} lacks real Given/When/Then acceptance criteria.", task_id)
    elif contains_placeholder(ac):
        add(report, "WARN", f"Task record {path.name} acceptance criteria may still contain template placeholders.", task_id)
    else:
        add(report, "PASS", f"Task record {path.name} has acceptance criteria.", task_id)

    env = section(text, r"^##\s+6\.\s+Environment classification") or section(text, r"^##\s+Environment classification")
    missing_env = [label for label in env_labels if label.lower() not in env.lower()]
    if missing_env:
        add(report, "FAIL", f"Task record {path.name} missing environment classification rows: {', '.join(missing_env)}.", task_id)
    elif contains_placeholder(env):
        add(report, "WARN", f"Task record {path.name} environment classification still contains placeholders; confirm YES/NO and reasons are real.", task_id)
    else:
        add(report, "PASS", f"Task record {path.name} has environment classification.", task_id)

    cto = section(text, r"^###\s+8\.2\s+CTO Watchtower status") or section(text, r"CTO Watchtower")
    if not cto:
        add(report, "WARN", f"Task record {path.name} has no CTO Watchtower status section; use the updated template for new work.", task_id)

    if status in STATUSES_REQUIRING_EVIDENCE:
        evidence = section(text, r"^##\s+(?:\d+\.\s+)?Evidence package")
        if not evidence:
            add(report, "FAIL", f"Task {task_id} is {status} but has no evidence package section.", task_id)
        else:
            required_terms = ["Changed files", "Acceptance criteria results", "Command/test output"]
            missing_terms = [term for term in required_terms if term.lower() not in evidence.lower()]
            if missing_terms:
                add(report, "FAIL", f"Task {task_id} evidence package missing: {', '.join(missing_terms)}.", task_id)
            elif contains_placeholder(evidence) and status in POST_REVIEW_STATUSES:
                add(report, "FAIL", f"Task {task_id} is {status} but evidence package still contains placeholders.", task_id)
            else:
                add(report, "PASS", f"Task {task_id} has evidence package for status {status}.", task_id)

    if status in POST_REVIEW_STATUSES:
        # Product behavior docs/help update or explicit N/A must be addressed.
        lower = text.lower()
        docs_words = ["context/spec/help", "docs/help", "help docs", "context/spec", "documentation updates", "updated context"]
        if not any(w in lower for w in docs_words):
            add(report, "WARN", f"Task {task_id} does not visibly address context/spec/help doc obligation.", task_id)

    if status in STATUSES_REQUIRING_MERGE:
        # Records title this section "14.1 Merge + SHA" (template convention) or
        # "14.1 Merge to main" — accept any "14.1 Merge..." heading (§19).
        merge_section = section(text, r"^##\s+14\.1\s+Merge") or section(text, r"Merge to main")
        lower_merge = merge_section.lower()
        has_sha = bool(re.search(r"[a-f0-9]{7,40}", merge_section, flags=re.IGNORECASE))
        has_verified = "verified" in lower_merge and "not merged" not in lower_merge
        if not has_sha or not has_verified:
            add(report, "FAIL", f"Task {task_id} is {status} but merge SHA / verify-merge VERIFIED evidence is missing.", task_id)
        else:
            add(report, "PASS", f"Task {task_id} has merge verification evidence.", task_id)


def check_repo(
    repo: Path,
    mode: str,
    requested_task: Optional[str],
    include_baseline: bool,
    env_labels: List[str],
    all_repos: Optional[List[Path]] = None,
) -> RepoReport:
    repo = repo.resolve()
    report = RepoReport(repo_path=repo)
    if not repo.exists():
        add(report, "FAIL", f"Repo path does not exist: {repo}")
        return report
    if not is_git_repo(repo):
        add(report, "FAIL", f"Not a git repo: {repo}")
        return report

    branch = git_branch(repo)
    status = git_status(repo)
    report.branch = branch
    report.git_status = status

    if branch == "main":
        # The watchdog's own not-yet-committed reports/holds must not dirty main —
        # they are written by this tool between CTO commit passes (§13). Expand
        # untracked dirs (git compresses them to "?? dir/") so those artifacts
        # are matchable while any other untracked file still flags.
        code_all, status_all, _ = run(["git", "status", "--short", "--untracked-files=all"], repo)
        dirt = [
            line for line in (status_all if code_all == 0 else status).splitlines()
            if line.strip() and not line.startswith("?? .agents/cto/")
        ]
        if dirt:
            add(report, "FAIL", "Dirty worktree on main. Agents must not leave uncommitted changes directly on main.")
        else:
            add(report, "PASS", "main branch is clean.")
    elif branch.startswith("task/"):
        task_id = task_id_from_branch(branch)
        task_path = repo / ".agents" / "tasks" / f"{task_id}.md"
        for cand in record_id_candidates(task_id):
            cand_path = repo / ".agents" / "tasks" / f"{cand}.md"
            if cand_path.exists():
                if cand == task_id:
                    add(report, "PASS", f"Active task branch {branch} has matching task record.", task_id)
                else:
                    add(report, "PASS", f"Active task branch {branch} covered by task record {cand}.md.", task_id)
                break
            cross = cross_filed_record(cand, repo, all_repos)
            if cross:
                add(report, "PASS", f"Active task branch {branch} record ({cand}.md) cross-filed on {cross}.", task_id)
                break
        else:
            declared = record_declaring_branch(branch, repo, all_repos)
            if declared:
                add(report, "PASS", f"Active task branch {branch} declared by task record {declared}.", task_id)
            else:
                add(report, "FAIL", f"Active task branch {branch} has no matching task record {task_path.relative_to(repo)} on any profiled board.", task_id)
    else:
        add(report, "FAIL", f"Unauthorized branch {branch}. Use main or short-lived task/<task-id> branches.")

    # Holds
    holds = hold_files(repo, requested_task)
    for hold in holds:
        task_id = normalize_task_id(hold.name)
        if hold_is_open(hold):
            add(report, "FAIL", f"Open CTO HOLD: {hold.relative_to(repo)}", task_id)
        else:
            add(report, "PASS", f"Resolved CTO HOLD: {hold.relative_to(repo)}", task_id)

    # Task records
    records = task_records(repo)
    if requested_task:
        requested = normalize_task_id(requested_task)
        records = [p for p in records if normalize_task_id(p.name) == requested]
        if not records:
            if cross_filed_record(requested, repo, all_repos):
                add(report, "PASS", f"Requested task record {requested}.md cross-filed on a sibling board.", requested)
            else:
                add(report, "FAIL", f"Requested task record not found: .agents/tasks/{requested}.md", requested)
    elif not records:
        if any(task_records(other) for other in (all_repos or []) if other.resolve() != repo):
            add(report, "PASS", "No local task records; canonical records live on a sibling repo's board.")
        else:
            add(report, "WARN", "No .agents/tasks/*.md records found in this repo.")

    # Grandfather pre-Watchtower task records so historical debt does not keep
    # the watchdog permanently red. A specific --task request or --include-baseline
    # bypasses the baseline so the full debt is always auditable on demand.
    baseline = set() if (include_baseline or requested_task) else load_baseline(repo)
    skipped: List[str] = []
    for record in records:
        if baseline and record_task_id(record) in baseline:
            skipped.append(record_task_id(record))
            continue
        check_task_record(record, report, mode, requested_task, env_labels)
    if skipped:
        add(report, "PASS", f"{len(skipped)} pre-Watchtower task record(s) baselined; record-quality checks skipped (see .agents/cto/baseline.txt). Run --include-baseline to audit them.")

    return report


def candidate_repos(args: argparse.Namespace) -> List[Path]:
    if args.repo:
        return [Path(p).expanduser() for p in args.repo]
    profile = getattr(args, "profile_data", {}) or {}
    repos = profile.get("repos") or []
    root = profile.get("project_root") or ""
    found: List[Path] = []
    if root and repos:
        base = Path(str(root)).expanduser()
        for entry in repos:
            name = str(entry).split(":", 1)[0].strip()
            if not name:
                continue
            p = base / name
            if p.exists():
                found.append(p)
    if found:
        return found
    return [Path.cwd()]


def render(reports: List[RepoReport], mode: str, requested_task: Optional[str], product_owner: str) -> str:
    all_findings = [f for r in reports for f in r.findings]
    failures = [f for f in all_findings if f.level == "FAIL"]
    warnings = [f for f in all_findings if f.level == "WARN"]
    passes = [f for f in all_findings if f.level == "PASS"]

    status = "HOLD" if failures else ("WARN" if warnings else "PASS")
    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines = [
        "# CTO Watchtower Report",
        "",
        f"Generated: {now}",
        f"Mode: {mode}",
        f"Task: {requested_task or 'all-active-tasks'}",
        f"Status: {status}",
        "",
        "## Repo state",
        "",
    ]
    for r in reports:
        lines += [
            f"### {r.repo_path}",
            "",
            f"- Branch: `{r.branch}`",
            "- Git status:",
            "",
            "```text",
            r.git_status or "<clean>",
            "```",
            "",
        ]

    lines += ["## Blocking holds / failures", ""]
    if failures:
        for f in failures:
            prefix = f"{f.repo}"
            if f.task_id:
                prefix += f" / {f.task_id}"
            lines.append(f"- **{prefix}** — {f.message}")
    else:
        lines.append("- None")
    lines.append("")

    lines += ["## Warnings", ""]
    if warnings:
        for f in warnings:
            prefix = f"{f.repo}"
            if f.task_id:
                prefix += f" / {f.task_id}"
            lines.append(f"- **{prefix}** — {f.message}")
    else:
        lines.append("- None")
    lines.append("")

    lines += ["## Clean checks", ""]
    if passes:
        for f in passes:
            prefix = f"{f.repo}"
            if f.task_id:
                prefix += f" / {f.task_id}"
            lines.append(f"- {prefix} — {f.message}")
    else:
        lines.append("- No passing checks recorded")
    lines.append("")

    lines += ["## Required next action", ""]
    if failures:
        lines.append("- Responsible implementer/reviewer must correct the blocking finding, update the task record with evidence, and rerun the CTO watchdog before review/merge/acceptance-ready.")
    elif warnings:
        lines.append("- No blocking hold found, but warnings should be resolved or explicitly documented before review.")
    else:
        lines.append(f"- No governance blocker found. This does not mean the task is done; normal QA, independent review, merge verification, and {product_owner} acceptance still apply.")

    return "\n".join(lines) + "\n"


def write_report(reports: List[RepoReport], content: str) -> Optional[Path]:
    # Write to the first valid git repo's .agents/cto/reports.
    for r in reports:
        if r.repo_path.exists() and is_git_repo(r.repo_path):
            reports_dir = r.repo_path / ".agents" / "cto" / "reports"
            reports_dir.mkdir(parents=True, exist_ok=True)
            ts = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
            path = reports_dir / f"{ts}_cto_watch.md"
            path.write_text(content, encoding="utf-8")
            return path
    return None


def finding_signature(reports: List[RepoReport]) -> Tuple[Tuple[str, str, Optional[str], str], ...]:
    """Return a stable signature for warnings/failures/holds.

    PASS findings are intentionally excluded so continuous mode does not keep
    repeating clean-check noise. If there are no warnings/failures, return a
    clean signature so the first clean state can still be reported once.
    """
    sig = []
    for r in reports:
        for f in r.findings:
            if f.level in {"WARN", "FAIL"}:
                sig.append((f.level, f.repo, f.task_id, f.message))
    if not sig:
        return (("PASS", "all", None, "no warnings or blocking holds"),)
    # task_id is Optional[str]: repo-level findings (dirty main, branch
    # discipline) carry None while task-level findings carry a str. Sorting the
    # raw tuples makes Python compare None < str and raise TypeError, which
    # crashed the default sweep whenever both kinds were present. Coerce None to
    # "" in the sort key only.
    return tuple(sorted(sig, key=lambda t: (t[0], t[1], t[2] or "", t[3])))


def run_once(args: argparse.Namespace) -> Tuple[int, str, Tuple[Tuple[str, str, Optional[str], str], ...], List[RepoReport]]:
    profile = getattr(args, "profile_data", {}) or {}
    env_labels = profile.get("deployment_channels") or DEFAULT_ENV_LABELS
    product_owner = profile.get("product_owner") or "owner"
    repos = candidate_repos(args)
    reports = [check_repo(repo, args.mode, args.task, args.include_baseline, env_labels, all_repos=repos) for repo in repos]
    content = render(reports, args.mode, args.task, product_owner)
    has_failures = any(r.has_failures for r in reports)
    return (1 if has_failures else 0), content, finding_signature(reports), reports


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="CTO Watchdog for a multi-agent development workflow")
    parser.add_argument("--repo", action="append", help="Repo path to inspect. Can be provided multiple times. Defaults to the repos in the profile, else the current repo.")
    parser.add_argument("--task", help="Specific task id to inspect")
    parser.add_argument("--mode", default="status", choices=["status", "preflight", "wip", "review", "merge"], help="Conceptual watch mode")
    parser.add_argument("--profile", help="Path to profile.config (else $DEV_HARNESS_PROFILE, else auto-discovered).")
    parser.add_argument("--no-write-report", action="store_true", help="Print only; do not write .agents/cto/reports file")
    parser.add_argument("--watch-interval", type=int, default=0, help="Run continuously every N seconds. Use 600 for a 10-minute CTO cadence.")
    parser.add_argument("--only-changes", action="store_true", help="In continuous mode, print/write only when WARN/FAIL/HOLD state changes.")
    parser.add_argument("--include-baseline", action="store_true", help="Ignore .agents/cto/baseline.txt and run full record-quality checks on grandfathered pre-Watchtower tasks (audit/cleanup pass).")
    args = parser.parse_args(argv)

    args.profile_data = load_profile(getattr(args, "profile", None))

    if args.watch_interval and args.watch_interval < 60:
        print("Refusing watch intervals below 60 seconds. Use 600 for the default 10-minute cadence.", file=sys.stderr)
        return 2

    if not args.watch_interval:
        code, content, _sig, reports = run_once(args)
        print(content)
        if not args.no_write_report:
            path = write_report(reports, content)
            if path:
                print(f"Report written: {path}")
        return code

    last_sig = None
    print(f"CTO Watchtower continuous monitor started. Interval: {args.watch_interval} seconds. Press Ctrl-C to stop.")
    while True:
        code, content, sig, reports = run_once(args)
        should_print = (not args.only_changes) or (sig != last_sig)
        if should_print:
            print(content)
            if not args.no_write_report:
                path = write_report(reports, content)
                if path:
                    print(f"Report written: {path}")
        last_sig = sig
        time.sleep(args.watch_interval)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("Interrupted", file=sys.stderr)
        raise SystemExit(2)
