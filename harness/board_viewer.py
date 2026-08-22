#!/usr/bin/env python3
# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
"""Zero-config local Mission Control for the visible development harness."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from email.parser import BytesParser
from email.policy import default as email_default
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, unquote, urlparse

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from harness import board, board_surface, child_process, contract, control, git_process, global_settings, workspace_settings, release_coordinator, lifecycle_metrics, runtime_identity
from harness.project_context import ProjectRoot, add_context_arguments, context_cli_arguments, context_from_args, project_context


OWNER_MESSAGE_MAX_BYTES = 20_000
OWNER_DIRECTIVE_EXTENSIONS = {".md", ".txt"}
DASHBOARD_REVIEW_LIMIT_PER_TASK = 40


PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="harness-viewer-version" content="__VIEWER_VERSION__">
<meta name="harness-runtime-commit" content="__RUNTIME_COMMIT__">
<title>NoMoreHappyPath</title>
<link rel="icon" type="image/png" href="favicon.png?v=2">
<style>
:root{--ink:#18212b;--muted:#687583;--line:#dce3e8;--paper:#f7f9fb;--card:#fff;--nav:#0d1728;--blue:#155eef;--blue-soft:#eaf1ff;--green:#067647;--green-soft:#e7f6ee;--amber:#b54708;--amber-soft:#fff1db;--red:#b42318;--red-soft:#ffebe9}
*{box-sizing:border-box}body{margin:0;background:var(--paper);color:var(--ink);font:15px/1.45 Inter,ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}.wrap{max-width:1360px;margin:auto;padding:34px 28px 56px}.top,.launch,.row,.task-head{display:flex;justify-content:space-between;gap:18px;align-items:flex-start}.top{border-bottom:1px solid var(--line);padding-bottom:24px}.eyebrow{font-size:12px;font-weight:750;letter-spacing:.09em;color:var(--blue);text-transform:uppercase}.back-link{display:inline-block;font-size:13px;font-weight:700;color:var(--blue);text-decoration:none;margin-bottom:10px}.back-link:hover{text-decoration:underline}#board-offline{position:fixed;inset:0;background:rgba(247,249,251,.97);display:none;align-items:center;justify-content:center;z-index:50}#board-offline.show{display:flex}#board-offline .panel{max-width:440px;text-align:center;padding:30px}#board-offline h2{margin:0 0 8px}h1{font-size:30px;line-height:1.12;letter-spacing:-.04em;margin:5px 0 8px}h2{font-size:17px;margin:0}h3{font-size:16px;margin:0}.sub,#updated,.section>p,.launch p,small{color:var(--muted)}.live{display:inline-flex;gap:8px;align-items:center;background:var(--green-soft);color:var(--green);border-radius:999px;padding:7px 11px;font-size:13px;font-weight:700}.dot{width:7px;height:7px;border-radius:50%;background:currentColor}.panel{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:20px;box-shadow:0 1px 2px #1018280a}.launch{align-items:center;margin-top:24px}.launch p,.section>p{margin:4px 0 0}.actions{display:flex;gap:9px;flex-wrap:wrap}button{appearance:none;border:0;border-radius:8px;background:var(--blue);color:#fff;padding:10px 13px;font:inherit;font-weight:700;cursor:pointer}button:hover{filter:brightness(.94)}button.secondary{background:var(--blue-soft);color:#174ea6}button.stop{background:var(--red-soft);color:var(--red)}button:disabled{background:#e4e8ec;color:#8994a0;cursor:not-allowed}.notice{margin-top:13px;color:var(--muted);min-height:22px}.stats{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin:20px 0}.stat{padding:16px}.stat strong{display:block;font-size:29px;letter-spacing:-.05em}.stat span{color:var(--muted);font-size:13px}.layout{display:grid;grid-template-columns:minmax(0,1.5fr) minmax(360px,1fr);gap:18px}.section{margin-top:18px}.empty{border:1px dashed #c7d1db;border-radius:10px;padding:24px;color:var(--muted);text-align:center}.task,.queue,.session,.agent-row{border-top:1px solid var(--line);padding:16px 0}.task:first-of-type,.queue:first-of-type,.session:first-of-type,.agent-row:first-of-type{border-top:0}.task-key{font-size:12px;font-weight:700;color:var(--muted);margin-top:4px}.task-static{display:block}.task-dynamic{margin-top:14px;border:1px solid var(--line);border-radius:10px;background:#f7f9fc;padding:11px 14px 14px}.task-dynamic .progress{margin-top:10px}.live-label{display:inline-flex;align-items:center;gap:7px;font-size:11px;font-weight:800;letter-spacing:.08em;text-transform:uppercase;color:var(--blue)}.live-label::before{content:"";width:7px;height:7px;border-radius:50%;background:var(--green);box-shadow:0 0 0 3px var(--green-soft)}.badge{display:inline-block;border-radius:999px;padding:4px 8px;font-size:11px;font-weight:800;white-space:nowrap}.tone-ready{background:var(--green-soft);color:var(--green)}.tone-repair{background:var(--red-soft);color:var(--red)}.tone-active{background:var(--amber-soft);color:var(--amber)}.tone-muted{background:#edf1f4;color:#596775}.progress{height:8px;background:#e8edf1;border-radius:8px;overflow:hidden;margin:13px 0 7px}.progress i{display:block;height:8px;border-radius:8px;background:var(--blue)}.progress i.repair{background:var(--red)}.progress i.ready{background:var(--green)}.meta{display:flex;gap:14px;flex-wrap:wrap;color:var(--muted);font-size:13px}.cto-chip{display:inline-flex;align-items:center;border:1px solid #b8c9e8;border-radius:999px;background:var(--blue-soft);color:#174ea6;padding:4px 9px;font-size:12px;font-weight:800}.next{margin-top:13px;background:#f5f8ff;border-left:3px solid var(--blue);padding:10px 12px;border-radius:0 7px 7px 0;color:#27476f;font-size:13px}.delivery-brief{margin-top:12px;background:#f8fafc;border:1px solid var(--line);border-radius:8px;padding:10px 12px;font-size:13px}.delivery-brief strong{color:#27476f}.delivery-brief p{margin:3px 0 8px;color:var(--ink)}.directive{margin:12px 0;border:1px solid var(--line);border-radius:8px;background:#fbfcfe}.directive-title{padding:9px 11px;color:#27476f;font-size:13px;font-weight:800;border-bottom:1px solid var(--line)}.directive-body{height:180px;max-height:180px;overflow-y:scroll;overscroll-behavior:contain;padding:10px 14px 14px;font-size:13px}.directive-body h4{font-size:14px;margin:12px 0 5px}.directive-body p{margin:6px 0;color:var(--ink)}.directive-body ul,.directive-body ol{margin:5px 0 8px;padding-left:22px}.directive-body li{margin:3px 0}.directive-body code{background:#edf1f4;border-radius:3px;padding:1px 3px}.agent-row .meta-line{color:var(--muted);font-size:12px;margin-top:3px}.status-button{padding:7px 10px;font-size:13px}.session{display:grid;grid-template-columns:minmax(0,1fr) auto;align-items:center;gap:12px}.session strong{display:block}.session small{display:block;margin-top:4px}.audit{margin-top:18px}.audit summary{cursor:pointer;color:var(--muted);font-weight:700}.audit pre{margin:12px 0 0;padding:14px;border-radius:8px;background:#111827;color:#e5edf6;overflow:auto;white-space:pre-wrap;font-size:12px}dialog{border:0;border-radius:14px;box-shadow:0 24px 64px #10182844;width:min(560px,calc(100% - 32px));padding:0}dialog::backdrop{background:#10182866}.modal{padding:24px}.modal h2{margin:0 0 6px;font-size:20px}.modal dl{margin:0}.modal dt{font-size:12px;font-weight:750;color:var(--muted);text-transform:uppercase;letter-spacing:.06em;margin-top:12px}.modal dd{margin:3px 0 0}.modal-actions{display:flex;justify-content:flex-end;gap:9px;margin-top:18px}@media(max-width:900px){.top,.launch{display:block}.actions{margin-top:15px}.stats,.layout{grid-template-columns:1fr}.wrap{padding:24px 16px}}
.release-response{margin-top:14px;padding:14px;border:1px solid #b8c9e8;border-radius:9px;background:#f7faff}.release-response h4{margin:0 0 4px;font-size:14px}.release-response p{margin:4px 0 10px;color:var(--muted)}.release-response .actions{margin-top:8px}.release-response.recorded{border-color:#b6dfc8;background:var(--green-soft)}.release-response.recorded strong{color:var(--green)}.owner-test-plan{margin:10px 0 12px;padding:10px 12px;border-left:3px solid var(--blue);background:#fff}.owner-test-plan strong{color:#174ea6}.owner-test-plan ol{margin:7px 0 0;padding-left:22px}.owner-test-plan li{margin:5px 0;color:var(--ink)}.release-reason{max-height:180px;margin:10px 0;padding-left:11px;overflow:auto;border-left:3px solid #d58b82;scrollbar-gutter:stable}.release-reason span{display:block;color:var(--muted);font-size:11px;font-weight:800;text-transform:uppercase}.release-reason p{margin:3px 0;color:var(--ink);white-space:pre-wrap;overflow-wrap:anywhere}
.release-preview{margin:0 0 12px;padding:12px 14px;border:1px solid #b8c9e8;border-radius:9px;background:#fff}.release-preview strong{color:#174ea6}.release-preview.ready{border-color:#8fbfa4;background:var(--green-soft)}.release-preview.ready strong{color:var(--green)}.release-preview.failed{border-color:#d58b82;background:var(--red-soft)}.release-preview.failed strong{color:var(--red)}.release-preview p{margin:5px 0 8px;color:var(--muted)}.release-preview .preview-link{display:inline-block;padding:9px 16px;border-radius:8px;background:var(--green);color:#fff;font-weight:700;text-decoration:none}.release-preview .preview-link:focus-visible{outline:3px solid #174ea6;outline-offset:2px}.preview-log{max-height:140px;margin:8px 0;padding:8px 10px;overflow:auto;background:#fff;border:1px solid var(--line);border-radius:7px;font-size:11px;white-space:pre-wrap;overflow-wrap:anywhere}.preview-setup{display:flex;gap:8px;flex-wrap:wrap}.preview-setup input{flex:1;min-width:240px;border:1px solid #b8c4cf;border-radius:8px;padding:9px 11px;font:inherit}.preview-hint{font-size:12px}
.modal label{display:block;margin-top:14px;font-size:13px}.modal textarea{display:block;width:100%;min-height:150px;margin-top:6px;border:1px solid var(--line);border-radius:8px;padding:10px;font:inherit;resize:vertical}.modal input[type=file]{display:block;width:100%;margin-top:6px}.modal small{display:block;margin-top:6px}.directive-file-panel{margin-top:14px;padding:11px 12px;border:1px solid var(--line);border-radius:8px;background:#f8fafc}.directive-file-panel label{margin-top:0}.input-valid{color:var(--green)}.input-error{color:var(--red)}
.terminal-color{display:inline-flex;align-items:center;gap:5px;margin-top:5px;color:var(--muted);font-size:12px}.terminal-color-swatch{display:inline-block;width:12px;height:12px;border-radius:3px;border:1px solid #8994a0;vertical-align:-1px}
.history-panel{margin-top:18px}.history-details>summary{display:flex;justify-content:space-between;align-items:center;gap:16px;cursor:pointer}.history-count{color:var(--muted);font-size:13px;font-weight:600}.history-intro{margin:8px 0 0;color:var(--muted);font-size:13px}.history-search{width:min(100%,560px);margin-top:12px;border:1px solid #b8c4cf;border-radius:8px;padding:9px 11px;font:inherit}.history-search-status{margin:6px 0;color:var(--muted);font-size:13px}.history-list{height:520px;max-height:60vh;overflow-y:scroll;overscroll-behavior:contain;margin-top:10px;border:1px solid var(--line);border-radius:9px;padding:0 12px;background:#fbfcfe}.history-date-group{border-top:1px solid var(--line)}.history-date-group:first-child{border-top:0}.history-date-group>summary{display:flex;justify-content:space-between;gap:12px;cursor:pointer;padding:13px 2px;color:var(--blue);font-size:12px;font-weight:800;text-transform:uppercase}.history-date-count{color:var(--muted);text-transform:none}.history-date-items{padding-left:16px}.history-item{border-top:1px solid var(--line);padding:13px 0}.history-item:first-child{border-top:0}.history-item-head{display:flex;justify-content:space-between;gap:12px;align-items:flex-start}.history-item h3{font-size:15px}.history-item .task-key{margin-top:2px}.history-time{color:var(--muted);font-size:12px;white-space:nowrap}.history-meta{display:flex;gap:12px;flex-wrap:wrap;margin-top:7px;color:var(--muted);font-size:13px}.history-empty{border:1px dashed #c7d1db;border-radius:10px;padding:18px;color:var(--muted);text-align:center;margin:12px 0}.history-match{margin-top:9px;padding:8px 10px;border-radius:7px;background:var(--amber-soft);font-size:13px}.history-match mark{background:#ffd98b}.history-directive{margin-top:10px;border:1px solid var(--line);border-radius:8px;background:#fff}.history-directive-title{padding:8px 11px;color:#27476f;font-size:12px;font-weight:800;border-bottom:1px solid var(--line)}.history-directive-body{height:150px;max-height:150px;overflow-y:scroll;overscroll-behavior:contain;padding:10px 14px 14px;font-size:13px}.history-directive-body h4{font-size:14px;margin:10px 0 5px}.history-directive-body p{margin:5px 0;color:var(--ink)}.history-directive-body ul,.history-directive-body ol{margin:5px 0 8px;padding-left:22px}.history-directive-body li{margin:3px 0}.history-directive-body code{background:#edf1f4;border-radius:3px;padding:1px 3px}.findings-panel{margin-top:18px}.findings-intro{margin:8px 0 0;color:var(--muted);font-size:13px}.finding-card{border:1px solid var(--line);border-radius:10px;padding:14px;margin-top:12px;background:#fff}.finding-card h3{font-size:15px;margin:0 0 4px}.finding-card p{margin:6px 0;color:var(--ink)}.finding-meta{display:flex;gap:10px;flex-wrap:wrap;color:var(--muted);font-size:12px}.finding-evidence{margin-top:8px;padding:8px 10px;border-radius:7px;background:#f8fafc;color:var(--muted);font-size:12px}.finding-card .actions{margin-top:11px}.finding-card.queued{border-color:#b8c9e8;background:#f7faff}.finding-card.dismissed{border-color:#b6dfc8;background:var(--green-soft)}.finding-limit{margin-top:10px;color:var(--muted);font-size:12px}
.settings-panel{margin-top:18px}.settings-details>summary{display:flex;justify-content:space-between;gap:16px;align-items:center;cursor:pointer}
/* A flex summary drops the browser's disclosure triangle, so a collapsible
   panel looks like a fixed window. Draw the caret back explicitly. */
.settings-details>summary,.history-details>summary{list-style:none}.settings-details>summary::-webkit-details-marker,.history-details>summary::-webkit-details-marker{display:none}.summary-end{display:inline-flex;align-items:center;gap:10px}.summary-caret{flex:none;width:7px;height:7px;border-right:2px solid var(--muted);border-bottom:2px solid var(--muted);transform:rotate(-45deg);transition:transform .15s ease}.settings-details[open]>summary .summary-caret,.history-details[open]>summary .summary-caret{transform:rotate(45deg)}@media(prefers-reduced-motion:reduce){.summary-caret{transition:none}}.settings-grid{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:10px;margin-top:14px}.settings-grid input{width:100%;border:1px solid #b8c4cf;border-radius:8px;padding:9px 11px;font:inherit}.settings-provider{margin-top:14px;border-top:1px solid var(--line);padding-top:12px}.settings-provider strong{display:block}.settings-provider code{display:block;margin-top:4px;color:var(--muted);font-size:12px;overflow-wrap:anywhere}.settings-warning{margin-top:8px;color:#7a3b00;background:var(--amber-soft);border-radius:7px;padding:8px 10px;font-size:13px}.settings-actions{margin-top:12px;display:flex;gap:9px;flex-wrap:wrap}
</style>
<style>.progress-heading{display:flex;justify-content:space-between;gap:12px;margin-top:12px;font-size:13px}.progress-heading span{color:var(--muted);text-align:right}.task-dynamic .progress{margin-top:6px}</style>
<style>.color-options{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-top:16px}.color-choice{display:flex;align-items:center;gap:9px;border:1px solid var(--line);border-radius:9px;padding:10px;background:#fff;color:var(--ink);cursor:pointer}.color-choice:hover{border-color:var(--blue)}.color-choice input{accent-color:var(--blue)}.color-swatch{width:24px;height:24px;border-radius:6px;border:1px solid #9aa6b2;display:inline-block}</style>
<style>.requirements-confirmation{margin:12px 0;border:1px solid #b6dfc8;border-radius:8px;background:var(--green-soft);padding:10px 12px}.requirements-title{color:var(--green);font-weight:800;font-size:13px}.requirements-body{margin-top:5px;font-size:13px}.requirements-body p{margin:4px 0;color:var(--ink)}.requirements-confirmation small{display:block;margin-top:8px;color:var(--muted)}.requirements-pending{border-left-color:var(--amber);background:var(--amber-soft);color:#6b3a08}</style>
<style>.requirements-section{border-top:1px solid #cce6d7;padding-top:8px;margin-top:8px}.requirements-section:first-child{border-top:0;padding-top:0;margin-top:0}.requirements-section h4{font-size:12px;color:#286344;margin:0 0 3px;text-transform:uppercase}.requirements-section ul,.requirements-section ol{margin:5px 0 8px;padding-left:22px}.requirements-section li{margin:5px 0;overflow-wrap:anywhere}.scope-change{border-left:3px solid var(--amber);padding:7px 10px;margin-top:8px;background:var(--amber-soft)}.scope-change small{display:block}.cto-task-list{max-height:min(52vh,520px);overflow:auto;overscroll-behavior:contain;border-top:1px solid var(--line);margin-top:14px}.cto-task{padding:12px 0;border-bottom:1px solid var(--line)}.cto-task:last-child{border-bottom:0}.cto-task h3{font-size:14px}.cto-task p{margin:5px 0}.cto-task .row{align-items:center}.task-counts{font-weight:750;color:#27476f}</style>
<style>.history-requirements{margin-top:10px;border:1px solid #b6dfc8;border-radius:8px;background:var(--green-soft);padding:9px 11px}.history-requirements-title{color:var(--green);font-size:12px;font-weight:800}.history-requirements-body{margin-top:5px;font-size:13px}.history-requirements-body p{margin:4px 0}.history-requirements small{display:block;margin-top:6px;color:var(--muted)}</style>
<style>.test-ledger{margin-top:12px;border:1px solid var(--line);border-radius:8px;background:#fbfcfe;padding:10px 12px;min-width:0;max-width:100%}.test-ledger+.test-ledger{margin-top:8px}.test-ledger h3{font-size:13px;color:#27476f;margin:0}.test-ledger-list{list-style:none;margin:7px 0 0;padding:0}.test-ledger-list li{display:grid;grid-template-columns:1.35em minmax(0,1fr);gap:2px 7px;margin:7px 0;align-items:start}.test-ledger-description{grid-column:2;min-width:0;overflow-wrap:anywhere;word-break:break-word}.test-ledger-check{grid-row:1 / span 2;display:inline-block;width:1.35em;font-weight:900;line-height:1.45}.test-ledger-check.passed{color:var(--green)}.test-ledger-check.failed{color:var(--red)}.test-ledger-check.pending{color:var(--muted)}.test-ledger-check.exception{color:var(--amber)}.test-ledger-state{grid-column:2;color:var(--muted);font-size:12px;font-weight:700}.test-ledger-check.failed+.test-ledger-description+.test-ledger-state{color:var(--red)}.test-ledger-check.exception+.test-ledger-description+.test-ledger-state{color:var(--amber)}.test-ledger-empty{margin:7px 0 0;color:var(--muted);font-size:13px;overflow-wrap:anywhere}.evidence-attempt{margin-top:12px;padding-top:10px;border-top:1px solid var(--line)}.evidence-attempt:first-of-type{border-top:0}.evidence-attempt-label{font-size:12px;font-weight:800;color:var(--muted)}.evidence-attempt-status{margin:3px 0 0;color:var(--muted);font-size:12px}@media(max-width:480px){dialog{width:calc(100% - 16px)}.modal{padding:18px}.test-ledger{padding:9px}.history-date-items{padding-left:6px}.history-item-head{display:block}.history-time{display:block;margin-top:4px;white-space:normal}}</style>
<style>.owner-clarifications{margin:12px 0;border:1px solid #b8c9e8;border-radius:8px;background:#f7faff;padding:10px 12px}.clarification-item{border-top:1px solid #dce3e8;margin-top:8px;padding-top:8px;font-size:13px}.clarification-item:first-of-type{border-top:0;margin-top:4px;padding-top:0}.clarification-item p{margin:4px 0}.owner-clarifications small{display:block;margin-top:5px;color:var(--muted)}</style>
<style>.agent-meta{display:block;margin-top:4px;color:var(--muted);font-size:12px}.session-starting{border-left:4px solid #b8c9e8;padding-left:10px}</style>
<style>.left-column{min-width:0}.delivery-progress-panel{margin-top:18px}.active-panel{min-width:0}.left-column>.panel:first-child{margin-top:18px}#tasks{height:auto;min-height:0;max-height:none;overflow-y:auto;overscroll-behavior:contain;padding-right:10px}@media(max-width:900px){#tasks{max-height:none;overflow-y:auto}}</style>
<style>.paused-banner{display:flex;align-items:center;justify-content:space-between;gap:18px;margin-top:18px;border:1px solid #c9b7eb;background:#f5f0ff}.paused-banner[hidden]{display:none}.paused-banner h2{color:#533183}.paused-banner p{margin:5px 0 0;color:#5d5270}.resume-link{display:inline-block;white-space:nowrap;border-radius:8px;background:var(--blue);color:#fff;padding:10px 13px;font-weight:700;text-decoration:none}.project-paused .live{background:#f0eaff;color:#5a398e}@media(max-width:700px){.paused-banner{display:block}.resume-link{margin-top:14px}}</style>
<style>html,body{max-width:100%;overflow-x:hidden}.project-controls{display:flex;justify-content:flex-end;margin-top:10px}.project-controls button{padding:7px 10px;font-size:13px}.project-chat-panel,.project-chat-panel form,.project-chat-head,.project-chat-history,.project-chat-actions{min-width:0;max-width:100%}.project-chat-panel{margin-bottom:18px}.project-chat-head{display:flex;align-items:flex-start;justify-content:space-between;gap:12px}.project-chat-head p{margin:4px 0 0;color:var(--muted);font-size:13px}.project-chat-history{min-height:72px;max-height:240px;overflow-y:auto;overscroll-behavior:contain;margin:14px 0 10px;padding:8px;border:1px solid var(--line);border-radius:9px;background:#fbfcfe}.chat-empty{color:var(--muted);font-size:13px;padding:12px;text-align:center}.chat-turn{margin:7px 0;padding:9px 11px;border-radius:9px;font-size:13px;white-space:pre-wrap;overflow-wrap:anywhere}.chat-question{margin-left:12%;background:var(--blue-soft);color:#174ea6}.chat-answer{margin-right:12%;background:#edf1f4}.project-chat-panel label{display:block;font-size:13px;font-weight:700}.project-chat-panel textarea{display:block;width:100%;min-width:0;max-width:100%;min-height:72px;max-height:150px;margin-top:6px;border:1px solid #b8c4cf;border-radius:8px;padding:9px 11px;font:inherit;resize:vertical}.project-chat-actions{display:flex;align-items:center;gap:8px;margin-top:9px;flex-wrap:wrap}.project-chat-status{color:var(--muted);font-size:13px;min-height:20px;flex:1 1 220px;overflow-wrap:anywhere}.project-chat-error{color:var(--red)}.project-chat-locked{margin:0 0 10px;padding:9px 11px;border:1px solid #f0d9a8;border-radius:9px;background:#fff8e8;color:#7a5410;font-size:13px}.project-chat-panel textarea:disabled,.project-chat-panel button:disabled{opacity:.55;cursor:not-allowed;background:#f1f3f6}@media(max-width:900px){.project-controls{justify-content:flex-start}.project-chat-history{max-height:200px}}@media(max-height:700px){.project-chat-history{max-height:150px}.project-chat-panel textarea{min-height:58px;max-height:90px}}</style>
<style>.project-topbar{position:fixed;inset:0 0 auto;z-index:60;height:68px;display:flex;align-items:center;padding:0 34px;background:rgba(13,23,40,.97);color:#fff;box-shadow:0 1px 0 rgba(255,255,255,.08);backdrop-filter:blur(14px)}.project-topbar+.wrap{margin-top:68px}.project-brand{display:flex;align-items:center;gap:11px;min-width:230px;color:#fff;font-size:16px;font-weight:780;text-decoration:none}.project-brand-mark{width:30px;height:30px;display:grid;place-items:center;border-radius:8px;color:#fff;background:linear-gradient(145deg,#5576ff,#7a52f5);box-shadow:0 8px 20px rgba(83,91,245,.35)}.project-nav{display:flex;align-items:center;gap:5px;height:100%}.project-nav a{height:40px;display:flex;align-items:center;border-radius:8px;padding:0 15px;color:#9eabc0;font-weight:680;text-decoration:none}.project-nav a:hover{color:#fff;background:rgba(255,255,255,.07)}.project-nav a[aria-current=page]{color:#fff;background:rgba(255,255,255,.1)}.project-nav-status{margin-left:auto;display:inline-flex;align-items:center;gap:8px;color:#cbd5e5;font-size:13px;font-weight:650}.project-nav-status .dot{width:8px;height:8px;background:#42d69a;box-shadow:0 0 0 4px rgba(66,214,154,.12)}@media(max-width:760px){.project-topbar{height:102px;padding:12px 18px;flex-wrap:wrap;gap:8px}.project-topbar+.wrap{margin-top:102px}.project-brand{min-width:0;margin-right:auto}.project-nav{order:3;width:100%;height:40px;overflow-x:auto}.project-nav a{flex:1;justify-content:center;white-space:nowrap;padding:0 10px}.project-nav-status{font-size:0}}@media(max-width:420px){.project-nav a{font-size:12px;padding:0 7px}}</style>
</head>
<body>__PROJECT_NAV__<main class="wrap">
<header class="top"><div style="display:flex;gap:14px;align-items:flex-start"><img src="favicon.png" alt="" aria-hidden="true" style="width:44px;height:44px;border-radius:10px;object-fit:contain;margin-top:3px"><div><div class="eyebrow">Local delivery system</div><h1>NoMoreHappyPath Mission Control</h1>__PROJECT_DESC__<div class="sub" id="project">Loading project…</div></div></div><div><div class="live"><i class="dot"></i>Board connected</div><div id="updated"></div>__PROJECT_CONTROLS__</div></header>__PROJECT_CLOSE_DIALOG__<div id="board-offline"><div class="panel"><h2>This project is closed</h2><p class="sub">Its board is not running. Return to Projects to open it again.</p><p><a id="board-offline-link" class="back-link" href="#">Projects</a></p></div></div>
<section class="panel paused-banner" id="paused-banner" hidden role="status"><div><h2>Project paused</h2><p>This board is read-only. Saved work, review ownership, and evidence are preserved exactly where they stopped.</p></div><a class="resume-link" id="resume-project" href="__RESUME_LINK__">Resume project</a></section>

<section class="panel launch"><div><h2>Start visible work</h2><p>Open a role now. Give Delivery direction through its safe composer; its Product Manager designs the objective and plan. Choose a terminal color to identify it; Cancel uses standard black.</p></div><div class="actions"><button id="codex">CODEX CLI · Delivery Agent</button><button class="secondary" id="claude">CLAUDE CLI · Reviewer</button><button class="secondary" id="cto">CTO (CLAUDE)</button><button class="stop" id="stop-all" disabled>Stop all agents</button><button class="secondary" id="relaunch-preserved" hidden>Relaunch preserved agents</button></div></section>
<section class="panel settings-panel"><details class="settings-details" id="access-details"><summary><span><strong>AI access for this project</strong></span><span class="summary-end"><span class="history-count">Where this project’s provider permissions live</span><i class="summary-caret" aria-hidden="true"></i></span></summary><div id="access-notice" class="notice" role="status" aria-live="polite"></div><div class="settings-provider"><strong>Claude — project permissions file</strong><code id="access-claude-path">Loading…</code><div>Applies only to this project folder. Bypass mode retains the deny guardrails for destructive commands and force-pushes.</div></div><div class="settings-provider"><strong>Codex — project trust entry</strong><code id="access-codex-path">Loading…</code><div>The global file carries one trust entry per project — this project’s is shown. Approval and sandbox access are passed per launch to this project’s agents and are never written globally, so they cannot leak into other projects or your own codex sessions.</div></div><p class="history-intro">Access is configured automatically every time this project opens — nothing to click. This panel only shows where it lives.</p></details></section>
<div id="notice" class="notice" aria-live="polite"></div>
<section id="attention" class="panel section" aria-live="polite"></section>
<section class="stats"><div class="panel stat"><strong id="active">0</strong><span>active board agents</span></div><div class="panel stat"><strong id="open">0</strong><span>reviews waiting</span></div><div class="panel stat"><strong id="claimed">0</strong><span>reviews in QA</span></div><div class="panel stat"><strong id="passed">0</strong><span>review passes</span></div></section>
<section class="layout"><div class="left-column">__PROJECT_CHAT_PANEL__<section class="panel delivery-progress-panel"><h2>Delivery progress</h2><p>Only current work appears here. Component counts are supporting evidence, not a completion claim.</p><div id="tasks"></div></section><section class="panel history-panel"><details class="history-details" id="history"><summary><span><strong>Task history</strong></span><span class="summary-end"><span class="history-count" id="history-count"></span><i class="summary-caret" aria-hidden="true"></i></span></summary><p class="history-intro">Completed tasks are grouped under collapsible dates.</p><input class="history-search" id="history-search" type="search" placeholder="Search history by word or sentence…" aria-label="Search task history"><div class="history-search-status" id="history-search-status" aria-live="polite"></div><div class="history-list" id="history-list"></div></details></section></div><aside class="panel active-panel"><h2>Active agents and terminals</h2><p>Each agent appears once with its exact task, board status, terminal color, and controls.</p><div id="agents"></div><h2 style="margin-top:24px">Review queue</h2><p>Independent reviewers claim these items.</p><div id="queue"></div></aside></section>
<dialog id="status-dialog" aria-labelledby="status-dialog-title"><div class="modal"><h2 id="status-dialog-title" tabindex="-1">Agent status</h2><div id="status-dialog-body"></div><div class="modal-actions"><button class="secondary" id="status-dialog-close">Close</button></div></div></dialog>
<dialog id="color-dialog" aria-labelledby="color-dialog-title"><form class="modal" id="color-form"><h2 id="color-dialog-title">Choose terminal color</h2><p>This color identifies this CLI window in Mission Control. Cancel launches with the standard black background.</p><div id="color-options" class="color-options"></div><div class="modal-actions"><button type="button" class="secondary" id="color-cancel">Cancel — use black</button><button type="submit" id="color-launch">Launch selected color</button></div></form></dialog>
<dialog id="decision-dialog" aria-labelledby="decision-dialog-title"><form class="modal" id="decision-form"><h2 id="decision-dialog-title">Tell us what needs changing</h2><p>Explain why you did not accept this release. Your explanation is saved with the release record and sent to Delivery.</p><label for="decision-reason"><strong>What should be changed?</strong></label><textarea id="decision-reason" rows="9" required placeholder="Add as much detail as you need."></textarea><label for="decision-attachments"><strong>Documents or screenshots</strong></label><input id="decision-attachments" type="file" multiple accept=".pdf,.md,.txt,.rtf,.doc,.docx,.png,.jpg,.jpeg,.gif,.webp,text/markdown"><small>Up to 5 files, 10 MB each. Files are stored safely and are not displayed as web pages.</small><p id="decision-error" class="notice" role="alert" aria-live="polite"></p><div class="modal-actions"><button type="button" class="secondary" id="decision-cancel">Cancel</button><button type="submit" id="decision-submit">Send response</button></div></form></dialog>
<dialog id="push-dialog" aria-labelledby="push-dialog-title"><form class="modal" id="push-form"><h2 id="push-dialog-title">Push the accepted commit</h2><p id="push-help">This is separate from accepting the local release. Choose an existing approved remote and branch. Nothing contacts the remote until you confirm in the next step.</p><label for="push-remote"><strong>Configured remote</strong></label><input id="push-remote" value="origin" required><label for="push-branch"><strong>Branch</strong></label><input id="push-branch" value="main" required><p id="push-error" class="notice" role="alert" aria-live="polite"></p><div class="modal-actions"><button type="button" class="secondary" id="push-cancel">Cancel</button><button type="submit" id="push-submit">Record push instruction</button></div></form></dialog>
<dialog id="owner-message-dialog" aria-labelledby="owner-message-title"><form class="modal" id="owner-message-form"><h2 id="owner-message-title">Give direction</h2><p id="owner-message-help">This message will be sent as one complete owner instruction. It will not be submitted by pressing Enter inside the paragraph box.</p><label for="owner-message-text"><strong>What should Delivery do?</strong></label><textarea id="owner-message-text" rows="12" required placeholder="Write the full direction or clarification here…"></textarea><small id="owner-message-count">0 of 20,000 stored UTF-8 bytes. Line endings become line feeds and boundary whitespace is removed once; all interior text is preserved exactly.</small><div class="directive-file-panel"><label for="owner-message-directive-file"><strong>Or use a .md or .txt file as the complete message</strong></label><input id="owner-message-directive-file" type="file" accept=".md,.txt,text/markdown,text/plain"><small id="owner-message-file-status">The browser reads the file as strict UTF-8. Its path is never sent or opened by the worker.</small></div><label for="owner-message-attachments"><strong>Separate supporting documents or screenshots</strong></label><input id="owner-message-attachments" type="file" multiple accept=".pdf,.md,.txt,.rtf,.doc,.docx,.png,.jpg,.jpeg,.gif,.webp,text/markdown"><small>Attachments stay separate from the message. Up to 5 files, 10 MB each. Nothing is sent until you press Send.</small><p id="owner-message-error" class="notice" role="alert" aria-live="polite"></p><div class="modal-actions"><button type="button" class="secondary" id="owner-message-cancel">Cancel</button><button type="submit" id="owner-message-submit">Send direction</button></div></form></dialog>
</main>
<script>
const loadedViewerVersion='__VIEWER_VERSION__';
const loadedRuntimeCommit='__RUNTIME_COMMIT__';
const loadedRuntimeManaged=__RUNTIME_MANAGED__;
const apiPrefix=__API_PREFIX__;
let boardFailures=0;
const activeStates=['launching','running','stopping','pausing'];
const preservedSessionStates=['paused'];
const terminalColors=[
  {id:'black',label:'Standard black',hex:'#000000'},
  {id:'blue',label:'Ocean blue',hex:'#123B5D'},
  {id:'purple',label:'Plum purple',hex:'#42275A'},
  {id:'green',label:'Forest green',hex:'#164A35'},
  {id:'red',label:'Brick red',hex:'#5A2525'},
  {id:'amber',label:'Dark amber',hex:'#5A4314'},
];
const el=selector=>document.querySelector(selector);
let refreshing=false;
let lastBoard=null;
let historyEntries=[];
let historyLoaded=false;
let historyLoadedAtVersion='';
let currentHistoryVersion='';
let historyLoading=false;
let pendingLaunchKind='';
let ownerMessageAgentId='';
let ownerMessageType='direction';
const ownerMessageMaxBytes=20000;
let ownerDirectiveLoading=false;

const esc=value=>String(value??'').replace(/[&<>"']/g,character=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[character]));
const apiPath=path=>`${apiPrefix}${path}`;

async function call(path,body){
  const response=await fetch(apiPath(path),{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body||{})});
  const data=await response.json();
  if(!response.ok)throw Error(data.error||'request failed');
  return data;
}

async function callMultipart(path,form){
  const response=await fetch(apiPath(path),{method:'POST',body:form});
  const data=await response.json();
  if(!response.ok){const error=Error(data.error||data.message||'We could not save your response. Please try again.');error.decisionRecorded=Boolean(data.decision_recorded);throw error;}
  return data;
}

function normalizeOwnerMessageText(value){return String(value??'').replace(/\r\n?/g,'\n');}
function ownerMessageBytes(value){return new TextEncoder().encode(value).byteLength;}
function validateOwnerMessageText(value){
  const text=normalizeOwnerMessageText(value).trim(),bytes=ownerMessageBytes(text);
  if(!text)throw Error('Write the complete message before sending it.');
  if(bytes>ownerMessageMaxBytes)throw Error(`The message is ${bytes.toLocaleString()} UTF-8 bytes; the limit is ${ownerMessageMaxBytes.toLocaleString()}.`);
  return{text,bytes};
}
function updateOwnerMessageCount(){
  const count=el('#owner-message-count'),bytes=ownerMessageBytes(normalizeOwnerMessageText(el('#owner-message-text').value).trim());
  count.textContent=`${bytes.toLocaleString()} of ${ownerMessageMaxBytes.toLocaleString()} stored UTF-8 bytes. Line endings become line feeds and boundary whitespace is removed once; all interior text is preserved exactly.`;
  count.className=bytes>ownerMessageMaxBytes?'input-error':'';
}
async function loadOwnerDirectiveFile(){
  const input=el('#owner-message-directive-file'),status=el('#owner-message-file-status'),error=el('#owner-message-error'),file=input.files?.[0];
  error.textContent=''; status.className='';
  if(!file){status.textContent='The browser reads the file as strict UTF-8. Its path is never sent or opened by the worker.';return;}
  ownerDirectiveLoading=true;el('#owner-message-submit').disabled=true;
  try{
    if(!/\.(?:md|txt)$/i.test(file.name)){input.value='';status.className='input-error';status.textContent='Choose a .md or .txt directive file.';return;}
    if(file.size===0){input.value='';status.className='input-error';status.textContent='The directive file is empty.';return;}
    if(file.size>128*1024){input.value='';status.className='input-error';status.textContent='The directive file is too large to read. The stored message limit is 20,000 UTF-8 bytes.';return;}
    const decoded=new TextDecoder('utf-8',{fatal:true}).decode(await file.arrayBuffer()),validated=validateOwnerMessageText(decoded);
    el('#owner-message-text').value=validated.text; updateOwnerMessageCount();
    status.className='input-valid';status.textContent=`Loaded ${file.name} as the complete message · ${validated.bytes.toLocaleString()} stored UTF-8 bytes after newline and boundary normalization.`;
  }catch(failure){input.value='';status.className='input-error';status.textContent=failure instanceof TypeError?'The directive file is not valid UTF-8.':failure.message;}
  finally{ownerDirectiveLoading=false;el('#owner-message-submit').disabled=false;}
}
function editOwnerMessageText(){
  const input=el('#owner-message-directive-file');
  if(input.files?.length){input.value='';el('#owner-message-file-status').className='';el('#owner-message-file-status').textContent='The loaded file was edited in the text box; the edited text will be sent as the message.';}
  updateOwnerMessageCount();
}

function badge(status){
  const value=String(status||'');
  const key=value.toLowerCase();
  const tone=key==='ready for your test'?'tone-ready':key.includes('repair')||key.includes('failed')||key.includes('blocked')?'tone-repair':key.includes('progress')||key.includes('review')||key.includes('checks')||key.includes('waiting')||key.includes('recover')||key.includes('monitor')||key.includes('start')?'tone-active':'tone-muted';
  return `<span class="badge ${tone}">${esc(value)}</span>`;
}

function inline(text){
  return esc(text).replace(/`([^`]+)`/g,'<code>$1</code>').replace(/\*\*([^*]+)\*\*/g,'<strong>$1</strong>');
}

function displayMultilineText(value){
  const text=String(value||'');
  // Some CLI callers preserve pasted line breaks as the two visible characters
  // "\\n". Decode only text that has no real line breaks and clearly represents
  // multiple lines, so an inline code example containing one "\\n" stays exact.
  const escapedBreaks=text.match(/\\r\\n|\\n/g)||[];
  if(!text.includes('\n')&&escapedBreaks.length>=2)return text.replace(/\\r\\n|\\n/g,'\n');
  return text;
}

function directionHtml(text){
  let output='',list='';
  const close=()=>{if(list){output+=`</${list}>`;list='';}};
  for(const raw of displayMultilineText(text).replace(/\x1b\[200~/g,'').replace(/\x1b\[201~/g,'').split(/\r?\n/)){
    const line=raw.trim();
    if(!line||/^```/.test(line))continue;
    const heading=line.match(/^#{1,6}\s+(.+)/),bullet=line.match(/^[-*]\s+(.+)/),numbered=line.match(/^\d+\.\s+(.+)/);
    if(heading){close();output+=`<h4>${inline(heading[1])}</h4>`;}
    else if(bullet||numbered){const wanted=numbered?'ol':'ul';if(list&&list!==wanted)close();if(!list){list=wanted;output+=`<${list}>`;}output+=`<li>${inline((bullet||numbered)[1])}</li>`;}
    else{close();output+=`<p>${inline(line)}</p>`;}
  }
  close();
  return output||'<p>No readable owner direction was recorded.</p>';
}

function requirementsHtml(text){
  const value=displayMultilineText(text).replace(/\x1b\[200~/g,'').replace(/\x1b\[201~/g,'');
  const pattern=/(^|(?:[\n.!?]\s+))(Objective|Requirements?|Deliverables|Acceptance|Approved exclusions|Status|Remaining work):\s*/gim;
  const matches=[...value.matchAll(pattern)];
  if(matches.length<2)return directionHtml(value);
  const labels={requirement:'Requirements',status:'Status when requirements were confirmed','remaining work':'Remaining work when requirements were confirmed'};
  return matches.map((match,index)=>{
    const start=(match.index||0)+match[0].length;
    const next=matches[index+1];
    const end=next?(next.index||value.length)+String(next[1]||'').length:value.length;
    const raw=String(match[2]||''),label=labels[raw.toLowerCase()]||raw;
    const body=value.slice(start,end).trim();
    const listSection=/^(requirements?|deliverables|acceptance|approved exclusions|remaining work)$/i.test(raw);
    const items=listSection?body.split(/\s*;\s*/).map(item=>item.trim()).filter(Boolean):[];
    const content=items.length>1?`<ul class="requirements-list">${items.map(item=>`<li>${inline(item)}</li>`).join('')}</ul>`:directionHtml(body);
    return `<section class="requirements-section"><h4>${esc(label)}</h4>${content}</section>`;
  }).join('');
}

function objectiveSummary(fallback){
  return String(fallback||'Delivery task').replace(/[_-]+/g,' ').toLowerCase().replace(/\b\w/g,character=>character.toUpperCase());
}

function orderedReviews(reviews){
  return [...reviews].sort((left,right)=>String(left.completed_at||left.claimed_at||left.reserved_at||left.requested_at||'').localeCompare(String(right.completed_at||right.claimed_at||right.reserved_at||right.requested_at||'')));
}

function reviewScope(review){
  if(!review)return'current work';
  if(review.phase==='final_acceptance')return'the complete task';
  const scope=review.subtask&&review.subtask!=='subtask-final'?review.subtask:review.chunk&&review.chunk!=='subtask-final'?review.chunk:'current chunk';
  return objectiveSummary(scope);
}

function taskGate(state,name,contract,agent,reviews,total,done){
  const release=(state.releases||{})[name];
  const decision=(state.release_decisions||{})[name]?.decision;
  const released=release?.status==='VISUAL_TEST_REQUIRED';
  const externalTarget=release?.runtime_verification_deferred_to_target_acceptance===true;
  const runtimeGated=Boolean(release?.checks?.deployed_runtime_verified||release?.checks?.deployed_chat_verified);
  const deployedRelease=externalTarget||!runtimeGated||!released||!loadedRuntimeManaged||(Boolean(loadedRuntimeCommit)&&release?.head_commit===loadedRuntimeCommit);
  const ordered=orderedReviews(reviews);
  const structureRevision=Number((state.delivery_plans||{})[name]?.structure_revision||0);
  const latest=ordered.at(-1);
  // Only the newest open/claimed request is the current gate. Looking for any
  // open and any claimed request could report an older review as the next step.
  const currentReview=['authoring','open','reserved','claimed'].includes(latest?.status)?latest:null;
  const finalPassed=ordered.some(item=>item.phase==='final_acceptance'&&item.status==='passed'&&(!structureRevision||Number(item.structure_revision||0)===structureRevision));
  const ratio=total?Math.min(1,done/total):0;
  // Reviews are progress within the current leaf of work. Give that leaf
  // provisional credit while it is reviewed, then replace it with full
  // credit on PASS. This prevents review -> next-chunk and FAIL -> repair
  // transitions from making the percentage move backwards.
  let leaf=total?1/total:1;
  const creditReview=currentReview||latest;
  const plan=(state.delivery_plans||{})[name]||{};
  if(plan.mode==='application'&&total&&creditReview?.subtask){
    const subtask=(plan.subtasks||{})[creditReview.subtask]||{};
    const nested=Object.keys(subtask.chunks||{}).length;
    if(creditReview.phase==='chunk'&&nested)leaf=.9/(total*nested);
    else if(creditReview.phase==='subtask_acceptance'&&nested)leaf=.1/total;
  }
  const staged=fraction=>Math.min(79,Math.round(10+Math.min(1,ratio+leaf*fraction)*60));
  const deliveryProgress=Math.min(70,Math.round(10+ratio*60));
  if(decision==='accepted')return{status:'OWNER ACCEPTED',progress:100,progressTone:'ready',ctoAction:'Your response is saved',next:'You accepted this release. Your visual-test response is closed.'};
  if(decision==='not_accepted')return{status:'OWNER REJECTED / REPAIR REQUIRED',progress:100,progressTone:'repair',ctoAction:'Delivery: repair required',next:'Your reason and attachments are saved. Delivery will use them in a new repair, review, and release cycle.'};
  if(released&&!deployedRelease)return{status:'DEPLOYMENT REFRESH REQUIRED',progress:96,progressTone:'repair',ctoAction:'CTO: loading the reviewed release',next:'The reviewed release is not the version serving this page. Acceptance is disabled until Mission Control is running the exact reviewed commit. Your action: none.'};
  if(agent?.task===name&&!((state.requirement_confirmations||{})[name]?.text))return{status:'AWAITING FINAL REQUIREMENTS',progress:0,progressTone:'active',ctoAction:'CTO: monitoring requirements capture',next:'Delivery is clarifying the request. No implementation or review may begin until you say go ahead and the final requirements are recorded.'};
  if(released)return{status:'READY FOR YOUR TEST',progress:100,progressTone:'ready',ctoAction:'CTO: release approved',next:'The exact tested version is clean and pushed to main. Your visual test is now required.'};
  if(latest?.status==='failed'){const final=latest.phase==='final_acceptance';return{status:'REPAIR IN PROGRESS',progress:final?86:staged(.85),progressTone:'repair',ctoAction:'CTO: blocking release and routing repair',next:'Independent review found a defect. Delivery must repair it and submit a new review cycle. Your action: none.'};}
  if(agent?.liveness==='stalled'&&recentOutputActive(agent))return{status:'STATUS UPDATE OVERDUE',progress:deliveryProgress,progressTone:'active',ctoAction:'CTO: requesting a short status update',next:'The Delivery Agent is actively producing terminal output, but its board update is overdue. The harness has requested a short update. Your action: none.'};
  if(agent?.liveness==='stalled')return{status:'REPAIR IN PROGRESS',progress:deliveryProgress,progressTone:'repair',ctoAction:'CTO: recovering automation',next:'The Delivery Agent stopped checking the board. The system must resume its saved work. Your action: none.'};
  if(currentReview?.status==='claimed'){const final=currentReview.phase==='final_acceptance';return{status:'INDEPENDENT REVIEW IN PROGRESS',progress:final?86:staged(.85),progressTone:'active',ctoAction:'CTO: monitoring independent review',next:`An Independent Reviewer is testing ${reviewScope(currentReview)} with a different scenario ledger. Your action: none.`};}
  if(currentReview?.status==='reserved'&&currentReview.delivery_state==='executing'){return{status:'DELIVERY TESTING / REVIEWER AUTHORING',progress:staged(.65),progressTone:'active',ctoAction:'CTO: monitoring bounded parallel work',next:`Delivery is executing its checks while the Independent Reviewer authors separate test intentions for ${reviewScope(currentReview)}. Reviewer execution remains blocked until Delivery succeeds. Your action: none.`};}
  if(currentReview?.status==='reserved'){const final=currentReview.phase==='final_acceptance';return{status:'REVIEWER PREPARING CHALLENGE LEDGER',progress:final?84:staged(.7),progressTone:'active',ctoAction:'CTO: reviewer is preparing independent scenarios',next:`An Independent Reviewer reserved ${reviewScope(currentReview)} and is preparing a different Challenge Ledger before execution. Your action: none.`};}
  if(currentReview?.status==='authoring'){return{status:'DELIVERY TESTING / REVIEW READY',progress:staged(.55),progressTone:'active',ctoAction:'CTO: routing independent authoring',next:`Delivery is executing its checks for ${reviewScope(currentReview)}. The frozen candidate is ready for a Reviewer to author independent test intentions, but review execution cannot begin yet. Your action: none.`};}
  if(currentReview?.status==='open'){const final=currentReview.phase==='final_acceptance';return{status:'INDEPENDENT REVIEW IN PROGRESS',progress:final?82:staged(.55),progressTone:'active',ctoAction:'CTO: monitoring the review queue',next:`The review for ${reviewScope(currentReview)} is waiting for an Independent Reviewer. Your action: none.`};}
  if(finalPassed||agent?.status==='release_wait'||agent?.status==='done'||agent?.status==='final_review_passed')return{status:'FINAL RELEASE CHECKS',progress:92,progressTone:'active',ctoAction:'CTO: verifying tested commit, push, clean main, and health',next:'Development and independent review have passed. The CTO must verify the exact pushed version before asking you to test. Your action: none.'};
  if(total>0&&done===total){const application=(state.delivery_plans||{})[name]?.mode==='application';return{status:'INDEPENDENT REVIEW IN PROGRESS',progress:78,progressTone:'active',ctoAction:'CTO: requiring final acceptance',next:`All ${application?'product subtasks':'chunks'} passed separately. Delivery must now request full end-to-end QA and independent review of the complete ${application?'application':'task'}. Your action: none.`};}
  if(agent?.status==='repairing'||agent?.status==='CONSOLIDATED_REPAIR')return{status:'REPAIR IN PROGRESS',progress:staged(.85),progressTone:'repair',ctoAction:'CTO: monitoring the repair cycle',next:'Delivery is repairing a failed review and must submit the repair for a new independent review. Your action: none.'};
  const mode=(state.delivery_plans||{})[name]?.mode||((state.task_chunks||{})[name]?'chunked':'atomic');
  const next=mode==='application'?'Delivery is implementing and testing the next product subtask or its next reviewable chunk. Your action: none.':mode==='atomic'?'Delivery is implementing the cohesive task, running unit tests, and preparing independent acceptance. Your action: none.':'Delivery is implementing and testing the next small, reviewable chunk. Your action: none.';
  return{status:'DEVELOPMENT IN PROGRESS',progress:deliveryProgress,progressTone:'active',ctoAction:'CTO: monitoring delivery gates',next};
}

function taskProgress(state,name,facts,gate,confirmation={}){
  const reviews=orderedReviews(facts.reviews),latest=reviews.at(-1),plan=(state.delivery_plans||{})[name],brief=(state.task_briefs||{})[name];
  const confirmed=Boolean(confirmation.text||(state.requirement_confirmations||{})[name]?.text);
  const deliveryCertified=reviews.some(item=>item.delivery_state==='passed'&&Boolean(item.delivery_evidence));
  const finalReviewed=reviews.some(item=>item.phase==='final_acceptance'&&item.status==='passed');
  const released=(state.releases||{})[name]?.status==='VISUAL_TEST_REQUIRED';
  const accepted=(state.release_decisions||{})[name]?.decision==='accepted';
  const overallChecks=[confirmed,deliveryCertified,finalReviewed,released,accepted];
  const paused=['draining','paused'].includes(state.project_pause?.status);
  let label='Development',checks=[Boolean(plan),Boolean(brief?.update),Boolean(latest),deliveryCertified];
  if(paused){label='Paused safely';checks=[true];}
  else if(gate.status==='AWAITING FINAL REQUIREMENTS'){label='Requirements';checks=[confirmed];}
  else if(gate.status.includes('DELIVERY TESTING')){label='Delivery testing';checks=[Boolean(latest),latest?.delivery_state==='executing'||deliveryCertified,deliveryCertified];}
  else if(gate.status==='REVIEWER PREPARING CHALLENGE LEDGER'){label='Review preparation';checks=[Boolean(latest),Boolean(latest?.reviewer_initial_intents?.length),Boolean(latest?.challenge_ledger)];}
  else if(gate.status==='INDEPENDENT REVIEW IN PROGRESS'){label='Independent review';checks=[Boolean(latest?.challenge_ledger),Boolean(latest?.claimed_by),Boolean(latest?.evidence),['passed','failed'].includes(latest?.status)];}
  else if(gate.status==='OWNER REJECTED / REPAIR REQUIRED'){
    const repair=(state.release_repairs||{})[name]||{},created=String(repair.created_at||'');
    label='Owner-requested repair';checks=[Boolean(repair.reason),repair.status==='DELIVERY_REPAIR_IN_PROGRESS',reviews.some(item=>String(item.requested_at||'')>created)];
  }
  else if(gate.status==='REPAIR IN PROGRESS'){label='Repair and re-review';checks=[reviews.some(item=>item.status==='failed'),Boolean(latest?.repair_package_id),Boolean(latest&&latest.status!=='failed')];}
  else if(gate.status==='FINAL RELEASE CHECKS'||gate.status==='DEPLOYMENT REFRESH REQUIRED'){label='Release checks';checks=[finalReviewed,facts.agent?.status==='done',released];}
  else if(gate.status==='READY FOR YOUR TEST'){label='Owner testing';checks=[released];}
  else if(gate.status==='OWNER ACCEPTED'){label='Completed';checks=[accepted];}
  const count=list=>list.filter(Boolean).length,total=Math.max(1,checks.length);
  return{overall:{completed:count(overallChecks),total:overallChecks.length},current:{label,completed:count(checks),total}};
}

function taskFacts(state,contracts,name){
  const chunks=Object.values((state.task_chunks||{})[name]||{});
  const plan=(state.delivery_plans||{})[name]||{};
  const mode=plan.mode||(chunks.length?'chunked':'atomic');
  const subtasks=Object.values(plan.subtasks||{});
  const reviews=Object.values(state.qa_requests||{}).filter(item=>item.task===name);
  const agents=Object.values(state.agents||{});
  const agent=agents.find(item=>item.task===name&&['engineering','development'].includes(item.role))||agents.find(item=>item.task===name);
  const passedSubtasks=subtasks.filter(item=>item.status==='passed').length;
  const done=mode==='application'?subtasks.reduce((credit,item)=>{
    if(item.status==='passed')return credit+1;
    const leaves=Object.values(item.chunks||{});
    // Nested chunks can earn most of their subtask's credit, but the last 10%
    // belongs to the independent subtask-acceptance gate itself.
    return credit+(leaves.length ? .9*leaves.filter(chunk=>chunk.status==='passed').length/leaves.length : 0);
  },0):chunks.filter(item=>item.status==='passed').length;
  const total=mode==='application'?subtasks.length:chunks.length;
  const nestedChunks=subtasks.flatMap(item=>Object.values(item.chunks||{}));
  const progressText=mode==='application'?`${passedSubtasks} product ${passedSubtasks===1?'subtask':'subtasks'} independently accepted · ${Math.max(0,total-passedSubtasks)} remaining`:mode==='atomic'?'One cohesive task · final independent acceptance still controls release':`${done} ${done===1?'change':'changes'} independently passed · ${Math.max(0,total-done)} remaining`;
  return{mode,plan,subtasks,chunks,reviews,agent,done,total,nestedChunks,progressText,contract:(contracts||{})[name]||{}};
}

function releasePreviewHtml(release){
  const preview=release?.preview||{};
  const commit=String(release?.head_commit||'').slice(0,10);
  const task=esc(JSON.stringify(release?.task||''));
  if(preview.status==='ready')return `<div class="release-preview ready"><strong>See it running before you decide</strong><p>The exact reviewed candidate (commit <code>${esc(commit)}</code>) is running on this computer for your inspection.</p><div class="actions"><a class="preview-link" href="${esc(preview.url||'')}" target="_blank" rel="noopener">Open the candidate preview</a></div></div>`;
  if(preview.status==='app_bundle')return `<div class="release-preview ready"><strong>The app is built and ready to test</strong><p>${esc(preview.app_name||'The application')} was built from the reviewed candidate (commit <code>${esc(commit)}</code>)${preview.built_at?` at ${esc(preview.built_at)}`:''}.</p><div class="actions"><button type="button" class="preview-link" onclick="openAppPreview(${task})">Open the app</button></div><p class="preview-hint" id="preview-hint-${esc(release?.task||'')}"></p></div>`;
  if(preview.status==='starting')return `<div class="release-preview"><strong>Candidate preview is starting…</strong><p>The reviewed candidate is being launched on this computer. This page updates automatically.</p></div>`;
  if(preview.status==='failed')return `<div class="release-preview failed"><strong>The candidate preview could not start</strong><p>${esc(preview.error||'Unknown failure')}</p>${preview.log_tail?`<pre class="preview-log">${esc(preview.log_tail)}</pre>`:''}<div class="actions"><button type="button" class="secondary" onclick="retryPreview(${task})">Try again</button></div></div>`;
  const location=[preview.branch?`branch <code>${esc(preview.branch)}</code>`:'',commit?`commit <code>${esc(commit)}</code>`:''].filter(Boolean).join(' · ');
  return `<div class="release-preview"><strong>Set up a candidate preview for this project</strong><p>The reviewed work lives on ${location||'the task branch'}${preview.workspace?` in <code>${esc(preview.workspace)}</code>`:''}. Enter the command that starts this project so you can see the candidate running before you accept. Use {port} for the local port and {state_dir} for a scratch folder.</p><div class="preview-setup"><input id="preview-command" placeholder="Example: scripts/start_app.sh --port {port}" autocomplete="off"><button type="button" class="secondary" onclick="savePreviewCommand()">Save and start preview</button></div><p class="preview-hint" id="preview-hint">The command runs from a clean copy of the reviewed commit and serves only this computer.</p></div>`;
}
async function savePreviewCommand(){
  const input=el('#preview-command'), hint=el('#preview-hint');
  if(!input.value.trim()){hint.textContent='Enter the command that starts this project.';return;}
  try{await call('/api/settings/preview',{command:input.value});hint.textContent='Saved. The preview starts in a few seconds; this page updates automatically.';}
  catch(error){hint.textContent='Could not save the preview command: '+error.message;}
}
async function openAppPreview(task){
  const hint=el('#preview-hint-'+CSS.escape(task));
  try{const result=await call('/api/releases/'+encodeURIComponent(task)+'/open-app',{});if(hint)hint.textContent=`${result.app_name||'The app'} is opening on your screen.`;}
  catch(error){if(hint)hint.textContent='Could not open the app: '+error.message;else el('#notice').textContent='Could not open the app: '+error.message;}
}
async function loadAccessPanel(){
  try{
    const response=await fetch(apiPath('/api/settings'),{cache:'no-store'});
    if(!response.ok)return;
    const data=await response.json();
    el('#access-claude-path').textContent=data?.claude?.settings_path||'Unavailable';
    el('#access-codex-path').textContent=(data?.codex?.config_path||'Unavailable')+'  →  [projects."'+(data?.workspace_root||'')+'"]';
  }catch(error){}
}
async function retryPreview(task){
  try{await call('/api/releases/'+encodeURIComponent(task)+'/preview-retry',{});el('#notice').textContent='Preview retry requested. It starts again in a few seconds.';await refresh();}
  catch(error){el('#notice').textContent='Could not retry the preview: '+error.message;}
}
function ownerTestPlanHtml(release){
  const fallback='Verify the released result against the final agreed requirements shown above.';
  const steps=Array.isArray(release?.owner_test_steps)&&release.owner_test_steps.length?release.owner_test_steps.slice(0,8):[fallback];
  return `<div class="owner-test-plan"><strong>What to test before accepting</strong><ol>${steps.map(step=>`<li>${esc(step)}</li>`).join('')}</ol></div>`;
}
function releaseResponseHtml(state,name){
  const release=(state.releases||{})[name];
  if(release?.status!=='VISUAL_TEST_REQUIRED')return'';
  const acceptRuntimeGated=Boolean(release?.checks?.deployed_runtime_verified||release?.checks?.deployed_chat_verified);
  if(loadedRuntimeManaged&&acceptRuntimeGated&&release?.runtime_verification_deferred_to_target_acceptance!==true&&(!loadedRuntimeCommit||release?.head_commit!==loadedRuntimeCommit))return `<div class="release-response recorded" aria-live="polite"><strong>Acceptance unavailable</strong><p>Mission Control is serving a different runtime than this reviewed release. The CTO must deploy and verify the exact commit before you test it.</p></div>`;
  const response=(state.release_decisions||{})[name];
  if(response){
    if(response.decision==='accepted'){
      const acceptance=(state.git_acceptances||{})[name],instruction=(state.remote_push_instructions||{})[name],outcome=(state.remote_push_outcomes||{})[name],task=esc(JSON.stringify(name));
      if(!acceptance)return `<div class="release-response recorded" aria-live="polite"><strong>Your response: Accepted</strong><p>The local Git transaction is waiting for reintegration or recovery. No remote push occurred.</p></div>`;
      if(outcome?.outcome==='pushed')return `<div class="release-response recorded" aria-live="polite"><strong>Accepted locally and pushed</strong><p>The exact accepted commit was pushed to ${esc(outcome.remote)} ${esc(outcome.branch)} after your separate confirmation.</p></div>`;
      if(instruction&&!instruction.used_at)return `<div class="release-response recorded" aria-live="polite"><strong>Accepted locally</strong><p>Your separate push instruction is saved. Confirm now to allow one remote contact; drift will abort rather than overwrite.</p><div class="actions"><button type="button" onclick="confirmPush(${task},${esc(JSON.stringify(instruction.id))})">Confirm push now</button></div></div>`;
      return `<div class="release-response recorded" aria-live="polite"><strong>Accepted locally</strong><p>Main advanced to the certified tree. Nothing has been pushed remotely.</p><div class="actions"><button type="button" onclick="openPushDialog(${task})">Push accepted commit…</button></div></div>`;
    }
    const count=Number(response.attachments?.length||0);
    const files=count===1?'1 attachment':`${count} attachments`;
    const repair=(state.release_repairs||{})[name]||{};
    const reason=String(response.reason||repair.reason||'').trim();
    const reasonText=reason?esc(reason):'Reason unavailable for this older response.';
    return `<div class="release-response recorded" aria-live="polite"><strong>OWNER REJECTED / REPAIR REQUIRED</strong><div class="release-reason"><span>Reason</span><p>${reasonText}</p></div><p>Your explanation and ${files} are saved. Delivery will use them in a new repair, review, and release cycle.</p></div>`;
  }
  const task=esc(JSON.stringify(name));
  return `<div class="release-response">${releasePreviewHtml(release)}<h4>When you finish your test</h4>${ownerTestPlanHtml(release)}<p>Tell us whether you accept this release.</p><div class="actions"><button type="button" class="secondary" onclick="submitAccepted(${task})">Accepted</button><button type="button" onclick="openDecisionDialog(${task})">Not accepted</button></div></div>`;
}

function taskLatestTimestamp(state,name){
  const indexed=state.latest_event_at_by_task?.[name];
  if(indexed)return Date.parse(String(indexed))||0;
  const values=[];
  for(const item of Object.values(state.qa_requests||{})){if(item.task===name)values.push(item.requested_at,item.claimed_at,item.completed_at,item.updated_at);}
  for(const item of Object.values(state.agents||{})){if(item.task===name)values.push(item.last_status_at,item.spawned_at);}
  for(const item of state.events||[]){if(item.task===name)values.push(item.at);}
  for(const item of Object.values(state.releases||{})){if(item.task===name)values.push(item.recorded_at);}
  for(const item of Object.values(state.release_decisions||{})){if(item.task===name)values.push(item.recorded_at);}
  for(const item of Object.values(state.release_repairs||{})){if(item.task===name)values.push(item.updated_at,item.created_at);}
  for(const item of Object.values(state.owner_clarifications||{}).flat()){if(item.task===name)values.push(item.created_at);}
  const timestamps=values.map(value=>Date.parse(String(value||''))).filter(value=>Number.isFinite(value));
  return timestamps.length?Math.max(...timestamps):0;
}

function orderTasksByLatest(state,names){
  return [...new Set(names)].filter(Boolean).sort((left,right)=>taskLatestTimestamp(state,right)-taskLatestTimestamp(state,left)||String(left).localeCompare(String(right)));
}

function taskNames(state,contracts,directions,liveTasks){
  if(Array.isArray(liveTasks))return orderTasksByLatest(state,liveTasks);
  const names=new Set([...Object.keys(state.task_chunks||{}),...Object.keys(state.delivery_plans||{}),...Object.values(state.qa_requests||{}).map(item=>item.task),...Object.keys(contracts||{}),...Object.keys(directions||{})]);
  names.delete('AWAITING_OWNER_DIRECTION');
  names.delete('GLOBAL_MONITOR');
  names.delete('REVIEW_QUEUE');
  return orderTasksByLatest(state,[...names]);
}

// A task card is split into two independently-refreshed regions so the window
// stops jumping.  The STATIC region (title, the full user directive, the final
// agreed requirements, owner clarifications) is the settled agreement: it is
// written once and only rewritten on the rare occasions it genuinely changes, so
// a long directive you are reading is never rebuilt underneath you.  The DYNAMIC
// region — the "Live delivery status" sub-window — carries the frequently-moving
// values (progress, plain-language update, next step, release response) and is
// the only part that refreshes on the two-second tick.  Each region is guarded by
// its own content signature (see tasks()), so an unchanged region is never
// touched at all.
function _taskCardParts(state,name,facts,gate,brief,directive,confirmation,clarifications,inScopeFindings){
  const blockers=(Array.isArray(inScopeFindings)?inScopeFindings:[]).filter(finding=>finding.task===name&&finding.status==='in_scope');
  const scopeNotice=blockers.length?`<div class="next" style="border-left-color:var(--red);background:var(--red-soft);color:#7a271a"><strong>Required before this task can finish:</strong> ${esc(blockers.map(finding=>finding.title).join('; '))}. Delivery is fixing and re-testing this as part of the current task.</div>`:'';
  const structureLabel=facts.mode==='application'?'Full application with product subtasks':facts.mode==='chunked'?'One task split into logical chunks':'One cohesive task with no artificial chunks';
  const structureChanges=Array.isArray(facts.plan.structure_changes)?facts.plan.structure_changes.slice(-3):[];
  const changeHistory=structureChanges.map(change=>`<div class="scope-change"><strong>Work added after planning</strong><p>${esc(change.reason||'Reason unavailable.')}</p><small>${esc(change.at||'Time unavailable')} · ${esc((change.added||[]).join(', '))}</small></div>`).join('');
  const structurePlan=`<div class="delivery-brief"><strong>Product Management structure</strong><p>${esc(structureLabel)}${facts.plan.rationale?` — ${esc(facts.plan.rationale)}`:''}</p>${facts.mode==='application'?`<strong>Product subtasks</strong>${facts.subtasks.map(item=>`<p>${item.status==='passed'?'✓':'○'} ${esc(item.title)}${item.dependencies?.length?` · after ${esc(item.dependencies.join(', '))}`:''}</p>`).join('')}`:''}${changeHistory}</div>`;
  const confirmationBlock=confirmation.text?`<div class="requirements-confirmation"><div class="requirements-title">Final agreed requirements</div><div class="requirements-body">${requirementsHtml(confirmation.text)}</div><small>Confirmed ${esc(confirmation.confirmed_at||'Time unavailable')} · version ${esc(confirmation.version||1)}. Status and remaining-work statements above describe this confirmation moment, not the live task.</small></div>`:`<div class="next requirements-pending"><strong>Final requirements confirmation:</strong> Delivery is clarifying the request. No implementation or review may begin until you say go ahead and the final requirements are recorded.</div>`;
  const clarificationBlock=clarifications.length?`<div class="owner-clarifications"><div class="requirements-title">Owner clarifications</div>${clarifications.slice(-3).map(item=>`<div class="clarification-item"><div>${directionHtml(item.text)}</div><small>${esc(item.created_at||'')} · ${Number(item.attachments?.length||0)} attachment(s)</small></div>`).join('')}</div>`:'';
  // TITLE-FIRST: the task-head (title + key + status badge) leads the card, then
  // the static directive/requirements, then the live progress region.
  const head=`<div class="task-head"><div><h3>${esc(objectiveSummary(name))}</h3><div class="task-key">Task: ${esc(name)} · ${esc(facts.mode)}</div></div>${badge(gate.status)}</div>`;
  const staticRegion=`${head}<div class="directive"><div class="directive-title">Full user directive</div><div class="directive-body">${directive?directionHtml(directive):'<p>No user directive has been recorded for this task.</p>'}</div></div>${confirmationBlock}${clarificationBlock}`;
  const progress=taskProgress(state,name,facts,gate,confirmation),overallWidth=Math.round(100*progress.overall.completed/progress.overall.total),currentWidth=Math.round(100*progress.current.completed/progress.current.total);
  const accepted=progress.overall.completed===progress.overall.total;
  const dynamicRegion=`<div class="live-label">Live delivery status</div><div class="progress-heading"><strong>Whole task</strong><span>${progress.overall.completed} of ${progress.overall.total} durable gates complete</span></div><div class="progress" role="progressbar" aria-label="Whole task progress" aria-valuemin="0" aria-valuemax="${progress.overall.total}" aria-valuenow="${progress.overall.completed}"><i class="${accepted?'ready':''}" style="width:${overallWidth}%"></i></div><div class="progress-heading"><strong>Current stage: ${esc(progress.current.label)}</strong><span>${progress.current.completed} of ${progress.current.total} checks complete</span></div><div class="progress" role="progressbar" aria-label="Current stage progress" aria-valuemin="0" aria-valuemax="${progress.current.total}" aria-valuenow="${progress.current.completed}"><i class="${esc(gate.progressTone)}" style="width:${currentWidth}%"></i></div><div class="meta"><span class="task-counts">${esc(facts.progressText)}</span><span>${facts.reviews.filter(item=>item.status==='passed').length} independent review passes recorded</span><span class="cto-chip">${esc(gate.ctoAction)}</span></div><div class="delivery-brief"><strong>What Delivery will do</strong><p>${esc(brief.plan||'Delivery has not yet published its plain-language plan.')}</p><strong>Current update</strong><p>${esc(brief.update||'Waiting for the next plain-language Delivery update.')}</p></div>${structurePlan}${scopeNotice}<div class="next"><strong>What happens next:</strong> ${esc(gate.next)}</div>${releaseResponseHtml(state,name)}`;
  return {static:staticRegion,dynamic:dynamicRegion};
}

function _cardSignature(html){let hash=5381;for(let i=0;i<html.length;i++){hash=((hash<<5)+hash+html.charCodeAt(i))|0;}return String(hash);}

function tasks(state,contracts,directions,liveTasks,inScopeFindings,requirements){
  const out=el('#tasks');
  const previousScrollTop=out.scrollTop;
  const existing=new Map((typeof out.querySelectorAll==='function'?[...out.querySelectorAll('.task')]:[]).map(node=>[node.dataset.task,node]));
  const names=taskNames(state,contracts,directions,liveTasks);
  if(!names.length){
    const waiting=Object.values(state.agents||{}).some(agent=>agent.active&&agent.task==='AWAITING_OWNER_DIRECTION');
    out.replaceChildren();
    out.innerHTML=waiting?'<div class="empty"><strong>Delivery Agent is standing by.</strong><br>Use Give direction beside the waiting agent.</div>':'<div class="empty"><strong>No active delivery task.</strong><br>Completed work is preserved in Task history below.</div>';
    out.scrollTop=0;
    return;
  }
  const seen=new Set();
  for(const name of names){
    const facts=taskFacts(state,contracts,name);
    const gate=taskGate({...state,requirement_confirmations:requirements||state.requirement_confirmations||{}},name,facts.contract,facts.agent,facts.reviews,facts.total,facts.done);
    const brief=(state.task_briefs||{})[name]||{};
    const directive=(directions||{})[name]||'';
    const confirmation=(requirements||{})[name]||{};
    const clarifications=(state.owner_clarifications||{})[name]||[];
    const parts=_taskCardParts(state,name,facts,gate,brief,directive,confirmation,clarifications,inScopeFindings);
    const staticSig=_cardSignature(parts.static);
    const dynamicSig=_cardSignature(parts.dynamic);
    let node=existing.get(name);
    let staticNode,dynamicNode;
    if(!node){
      node=document.createElement('article');
      node.className='task';
      node.dataset.task=name;
      staticNode=document.createElement('div');
      staticNode.className='task-static';
      dynamicNode=document.createElement('div');
      dynamicNode.className='task-dynamic';
      if(typeof node.appendChild==='function'){node.appendChild(staticNode);node.appendChild(dynamicNode);}
    }else{
      staticNode=typeof node.querySelector==='function'?node.querySelector('.task-static'):null;
      dynamicNode=typeof node.querySelector==='function'?node.querySelector('.task-dynamic'):null;
    }
    // The settled agreement (title + directive + agreed requirements) is rewritten
    // ONLY when it actually changes — never on the two-second tick — so a long
    // directive you are reading is never rebuilt underneath you.  Its scroll
    // position is carried across the rare rewrite.
    if(staticNode&&staticNode.dataset.sig!==staticSig){
      const priorDirectiveScroll=(typeof staticNode.querySelector==='function'?staticNode.querySelector('.directive-body')?.scrollTop:0)||0;
      staticNode.innerHTML=parts.static;
      staticNode.dataset.sig=staticSig;
      const directiveNode=typeof staticNode.querySelector==='function'?staticNode.querySelector('.directive-body'):null;
      if(directiveNode)directiveNode.scrollTop=priorDirectiveScroll;
    }
    // The Live delivery status sub-window is the only region that moves with the
    // refresh, and even it is rewritten only when its own content changed.
    if(dynamicNode&&dynamicNode.dataset.sig!==dynamicSig){
      dynamicNode.innerHTML=parts.dynamic;
      dynamicNode.dataset.sig=dynamicSig;
    }
    out.append(node);
    seen.add(name);
  }
  for(const[cardName,node]of existing){if(!seen.has(cardName)&&typeof node.remove==='function')node.remove();}
  // Keep the user's place unless the refreshed content became shorter.
  out.scrollTop=Math.min(previousScrollTop,Math.max(0,out.scrollHeight-out.clientHeight));
}

// Keep the Delivery list scrollable while using all of the viewport below its
// heading.  A fixed height cuts a card at an arbitrary point on different
// screens; measuring its actual position keeps the bottom edge predictable.
function fitTaskScroller(){
  const tasksNode=el('#tasks');
  if(!tasksNode||typeof window==='undefined'||typeof tasksNode.getBoundingClientRect!=='function')return;
  const previousScrollTop=tasksNode.scrollTop;
  const top=tasksNode.getBoundingClientRect().top;
  if(!Number.isFinite(top))return;
  tasksNode.style.height='auto';
  const rightPanel=el('.active-panel');
  const rightBottom=rightPanel?.getBoundingClientRect?.().bottom;
  const bottom=Math.min(window.innerHeight-28,Number.isFinite(rightBottom)?rightBottom:window.innerHeight-28);
  const available=Math.max(220,Math.floor(bottom-Math.max(0,top)-28));
  if(tasksNode.scrollHeight>available)tasksNode.style.height=`${available}px`;
  tasksNode.scrollTop=Math.min(previousScrollTop,Math.max(0,tasksNode.scrollHeight-tasksNode.clientHeight));
}

function historyTimestamp(value){
  const date=new Date(value);
  return Number.isNaN(date.getTime())?'Timestamp unavailable':date.toLocaleString([], {dateStyle:'medium',timeStyle:'short'});
}

function historyDateKey(value){
  const date=new Date(value);
  if(Number.isNaN(date.getTime()))return'undated';
  return [date.getFullYear(),String(date.getMonth()+1).padStart(2,'0'),String(date.getDate()).padStart(2,'0')].join('-');
}

function historyDateLabel(key){
  if(key==='undated')return'Date unavailable';
  const date=new Date(`${key}T12:00:00`);
  return date.toLocaleDateString([], {weekday:'long',month:'long',day:'numeric',year:'numeric'});
}

function historyMatch(item,query){
  const needle=String(query||'').trim().toLowerCase();
  if(!needle)return'';
  const ledgerLines=(Array.isArray(item.test_ledgers)&&item.test_ledgers.length?item.test_ledgers:[item.test_ledger]).flatMap(ledger=>['delivery','reviewer'].flatMap(role=>Array.isArray(ledger?.[role]?.scenarios)?ledger[role].scenarios:role==='delivery'&&Array.isArray(ledger?.scenarios)?ledger.scenarios:[])).map(scenario=>`${scenario.what_was_tested||scenario.scenario||''} ${scenario.label||''}`.trim());
  const lines=[`Task: ${item.task||''}`,`Result: ${historyResultLabel(item.result)}`,...String(item.owner_direction||'').split(/\r?\n/).map(line=>line.trim()).filter(Boolean),...String(item.requirements_confirmation?.text||'').split(/\r?\n/).map(line=>line.trim()).filter(Boolean),...ledgerLines];
  return lines.find(line=>line.toLowerCase().includes(needle))||'';
}

function highlightHistory(text,query){
  const value=String(text||''),needle=String(query||'').trim(),index=value.toLowerCase().indexOf(needle.toLowerCase());
  if(!needle||index<0)return esc(value);
  return esc(value.slice(0,index))+`<mark>${esc(value.slice(index,index+needle.length))}</mark>`+esc(value.slice(index+needle.length));
}

function requirementsHistoryHtml(item){
  const confirmation=item.requirements_confirmation||{};
  if(!confirmation.text)return'';
  return`<div class="history-requirements"><div class="history-requirements-title">Final agreed requirements</div><div class="history-requirements-body">${requirementsHtml(confirmation.text)}</div><small>Confirmed ${esc(confirmation.confirmed_at||'Time unavailable')} · version ${esc(confirmation.version||1)}. Historical status wording describes that confirmation moment.</small></div>`;
}

function historyResultLabel(value){
  const key=String(value||'').toLowerCase();
  if(key.includes('owner accepted'))return'Accepted';
  if(key.includes('ready for your test'))return'Ready for your test';
  if(key.includes('failed')||key.includes('repair'))return'Needs repair';
  if(key.includes('passed'))return'Checks complete';
  if(key.includes('completed'))return'Completed';
  return'Recorded';
}

function checklistSectionHtml(section,role){
  const reviewer=role==='reviewer',heading=reviewer?'What the independent reviewer tested':'What Delivery tested';
  const scenarios=Array.isArray(section?.scenarios)?section.scenarios:[];
  if(!scenarios.length){
    const message=section?.message||(reviewer?'The independent reviewer has not submitted checks for this attempt.':'Delivery has not submitted checks for this attempt.');
    return`<section class="test-ledger" aria-label="${esc(heading)}"><h3>${esc(heading)}</h3><p class="test-ledger-empty">${esc(message)}</p></section>`;
  }
  const items=scenarios.map(item=>{
    const status=['passed','failed','exception'].includes(item.status)?item.status:'pending';
    const symbol=status==='passed'?'☑':status==='failed'?'☒':status==='exception'?'◇':'☐';
    const defaultLabel=status==='passed'?'Passed':status==='failed'?'Needs attention':status==='exception'?'Not required for this change':'Not tested yet';
    const label=String(item.label||defaultLabel),description=String(item.what_was_tested||item.scenario||'A recorded check has no plain-language summary.');
    return`<li><span class="test-ledger-check ${status}" role="img" aria-label="${esc(label)}">${symbol}</span><span class="test-ledger-description">${esc(description)}</span><span class="test-ledger-state">${esc(label)}</span></li>`;
  }).join('');
  return`<section class="test-ledger" aria-label="${esc(heading)}"><h3>${esc(heading)}</h3><ul class="test-ledger-list">${items}</ul></section>`;
}

function testLedgerHtml(ledger){
  const delivery=ledger?.delivery||{state:ledger?.state,scenarios:Array.isArray(ledger?.scenarios)?ledger.scenarios:[]};
  const reviewer=ledger?.reviewer||{state:'absent',scenarios:[]};
  const label=ledger?.attempt_label?`<div class="evidence-attempt-label">${esc(ledger.attempt_label)}</div>`:'';
  const status=ledger?.attempt_status?`<p class="evidence-attempt-status">${esc(ledger.attempt_status)}</p>`:'';
  return`<div class="evidence-attempt">${label}${status}${checklistSectionHtml(delivery,'delivery')}${checklistSectionHtml(reviewer,'reviewer')}</div>`;
}

function agentChecklistHtml(checklist){
  if(!checklist)return'';
  if(checklist.state==='idle')return checklistSectionHtml({state:'idle',message:checklist.message,scenarios:[]},'reviewer');
  return checklistSectionHtml(checklist.section||{},checklist.role==='reviewer'?'reviewer':'delivery');
}

function renderHistory(entries,queryValue){
  const out=el('#history-list'),count=el('#history-count'),search=el('#history-search'),status=el('#history-search-status');
  if(!out||!count)return;
  const previousScrollTop=out.scrollTop;
  const historyDirectiveScrollTops=new Map((typeof out.querySelectorAll==='function'?[...out.querySelectorAll('.history-item')]:[]).map(item=>[item.dataset.historyKey,(typeof item.querySelector==='function'?item.querySelector('.history-directive-body')?.scrollTop:0)||0]));
  const openDates=new Set(out.querySelectorAll?[...out.querySelectorAll('.history-date-group[open]')].map(group=>group.dataset.historyDate):[]);
  const items=(Array.isArray(entries)?entries:[]).slice().sort((left,right)=>String(right.completed_at||right.started_at||'').localeCompare(String(left.completed_at||left.started_at||'')));
  const query=String(queryValue===undefined?(search?.value||''):queryValue).trim();
  const visible=query?items.filter(item=>historyMatch(item,query)):items;
  count.textContent=items.length?`${items.length} ${items.length===1?'completed task':'completed tasks'}`:'No completed tasks yet';
  if(!items.length){out.innerHTML='<div class="history-empty">Completed task results will appear here after the first delivery finishes.</div>';return;}
  if(status)status.textContent=query?`${visible.length} of ${items.length} tasks match “${query}”`:`${items.length} tasks grouped by date`;
  if(!visible.length){out.innerHTML='<div class="history-empty">No history item matches this search.</div>';return;}
  const groups=new Map();
  for(const item of visible){const key=historyDateKey(item.completed_at||item.started_at);if(!groups.has(key))groups.set(key,[]);groups.get(key).push(item);}
  out.innerHTML=[...groups.entries()].map(([key,group])=>`<details class="history-date-group" data-history-date="${esc(key)}"${query||openDates.has(key)?' open':''}><summary><span>${esc(historyDateLabel(key))}</span><span class="history-date-count">${group.length} ${group.length===1?'task':'tasks'}</span></summary><div class="history-date-items">${group.map(item=>{const directive=String(item.owner_direction||''),match=historyMatch(item,query),structure=item.delivery_mode==='application'?`${Number(item.subtasks_passed||0)}/${Number(item.subtasks_total||0)} work areas completed`:item.delivery_mode==='atomic'?'Single focused change':`${Number(item.chunks_passed||0)}/${Number(item.chunks_total||0)} focused changes completed`,historyKey=`${item.task}|${item.completed_at||item.started_at||''}`;return`<article class="history-item" data-history-key="${esc(historyKey)}"><div class="history-item-head"><div><h3>${esc(objectiveSummary(item.task))}</h3><div class="task-key">Task: ${esc(item.task)}</div></div><span class="history-time">${esc(historyTimestamp(item.completed_at||item.started_at))}</span></div>${match?`<div class="history-match"><strong>Matching line:</strong> ${highlightHistory(match,query)}</div>`:''}<div class="history-directive"><div class="history-directive-title">Full user directive</div><div class="history-directive-body">${directive?directionHtml(directive):'<p>No user directive was recorded for this older task.</p>'}</div></div>${requirementsHistoryHtml(item)}${(Array.isArray(item.test_ledgers)&&item.test_ledgers.length?item.test_ledgers:[item.test_ledger]).map(testLedgerHtml).join('')}<div class="history-meta"><span>${esc(historyResultLabel(item.result))}</span><span>${esc(structure)}</span><span>${Number(item.review_passes||0)} independent checks completed</span>${item.owner_decision?`<span>Owner response recorded</span>`:''}</div></article>`;}).join('')}</div></details>`).join('');
  if(typeof out.querySelectorAll==='function')out.querySelectorAll('.history-item').forEach(item=>{const directive=typeof item.querySelector==='function'?item.querySelector('.history-directive-body'):null;if(directive)directive.scrollTop=historyDirectiveScrollTops.get(item.dataset.historyKey)||0;});
  out.scrollTop=Math.min(previousScrollTop,Math.max(0,out.scrollHeight-out.clientHeight));
}

function reviewerAssignment(agent,state){
  return orderedReviews(Object.values(state?.qa_requests||{}).filter(item=>((item.status==='claimed'||item.status==='suspended')&&item.claimed_by===agent.id)||(item.status==='reserved'&&item.reserved_by===agent.id)||((item.status==='authoring'||item.status==='open')&&item.routed_to===agent.id))).at(-1)||null;
}

function reviewExecutionActive(agent){
  const execution=agent?.review_execution;
  if(!execution?.active||!execution.last_heartbeat_at)return false;
  const heartbeat=Date.parse(execution.last_heartbeat_at);
  return Number.isFinite(heartbeat)&&Date.now()-heartbeat<240000;
}

function recentOutputActive(agent){
  if(!agent?.recent_output_at)return false;
  const output=Date.parse(agent.recent_output_at);
  return Number.isFinite(output)&&Date.now()-output<240000;
}

function humanTask(agent,state={}){
  if(agent.role==='cto')return'All project tasks';
  if(agent.role==='qa'){
    const assignment=reviewerAssignment(agent,state);
    return assignment?objectiveSummary(assignment.task):'Independent review queue';
  }
  if(agent.task==='AWAITING_OWNER_DIRECTION')return'Waiting for your next task';
  return objectiveSummary(agent.task||'Current task');
}

function humanStage(agent,state,contracts){
  if(agent.status==='paused')return'PAUSED';
  if(agent.role==='cto'){
    if(agent.liveness==='stalled')return'CTO RECOVERY REQUIRED';
    const rows=ctoTaskRows(state,contracts);
    if(rows.some(row=>row.ownerAction!=='None.'))return'OWNER TEST READY';
    if(rows.some(row=>row.stage.includes('REPAIR')||row.blocker))return'MONITORING REQUIRED REPAIRS';
    return`MONITORING ${rows.length} ACTIVE ${rows.length===1?'TASK':'TASKS'}`;
  }
  if(agent.task==='AWAITING_OWNER_DIRECTION')return'WAITING FOR YOUR TASK';
  if(agent.role==='qa'){
    const assignment=reviewerAssignment(agent,state);
    return reviewExecutionActive(agent)?'INDEPENDENT REVIEW EXECUTION IN PROGRESS':agent.liveness==='stalled'&&recentOutputActive(agent)?'STATUS UPDATE OVERDUE':agent.liveness==='stalled'?'REPAIR IN PROGRESS':assignment?.status==='authoring'?'INDEPENDENT TEST AUTHORING READY':assignment?.status==='open'?'REVIEW ROUTED — RESERVE NOW':assignment?.status==='reserved'&&assignment.delivery_state==='executing'?'AUTHORING INDEPENDENT TESTS':assignment?.status==='reserved'?'PREPARING CHALLENGE LEDGER':assignment?'INDEPENDENT REVIEW IN PROGRESS':'MONITORING REVIEW QUEUE';
  }
  const facts=taskFacts(state,contracts,agent.task);
  return taskGate(state,agent.task,facts.contract,agent,facts.reviews,facts.total,facts.done).status;
}

function relativeUpdate(value){
  if(!value)return'No update recorded yet';
  const seconds=Math.max(0,Math.round((Date.now()-new Date(value).getTime())/1000));
  if(seconds<60)return'Updated less than a minute ago';
  if(seconds<3600)return`Updated ${Math.floor(seconds/60)} minutes ago`;
  return`Updated ${Math.floor(seconds/3600)} hours ago`;
}

function ctoTaskRows(state,contracts){
  const names=Array.isArray(state.live_tasks)?state.live_tasks:taskNames(state,contracts,{});
  const findings=Array.isArray(state.in_scope_findings)?state.in_scope_findings:[];
  return names.slice(0,20).map(name=>{
    const facts=taskFacts(state,contracts,name),gate=taskGate(state,name,facts.contract,facts.agent,facts.reviews,facts.total,facts.done);
    const failed=orderedReviews(facts.reviews).filter(item=>item.status==='failed').at(-1);
    const finding=findings.find(item=>item.task===name&&item.status==='in_scope');
    const repair=(state.release_repairs||{})[name]||{};
    const blocker=String(repair.reason||finding?.description||failed?.result_summary||failed?.summary||'').trim();
    const ready=(state.releases||{})[name]?.status==='VISUAL_TEST_REQUIRED'&&!(state.release_decisions||{})[name];
    const stamp=taskLatestTimestamp(state,name);
    return{
      task:name,stage:gate.status,counts:facts.progressText,
      blocker:blocker||(gate.status.includes('REPAIR')?'A required repair is recorded; its older record has no plain-language reason.':''),
      next:gate.next.replace(/\s*(USER|Your) ACTION:\s*None\.?/i,''),
      updated:stamp?relativeUpdate(new Date(stamp).toISOString()):'No task update recorded yet',
      ownerAction:ready?'Open this task, test the released version, then record your decision.':'None.',
    };
  });
}

function ctoTaskRowsHtml(state,contracts){
  const rows=ctoTaskRows(state,contracts),total=(Array.isArray(state.live_tasks)?state.live_tasks:taskNames(state,contracts,{})).length;
  if(!rows.length)return'<div class="empty">No project task is active.</div>';
  const items=rows.map(row=>`<article class="cto-task"><div class="row"><h3>${esc(objectiveSummary(row.task))}</h3>${badge(row.stage)}</div><p>${esc(row.counts)}</p>${row.blocker?`<p><strong>Current blocker or repair reason:</strong> ${esc(row.blocker)}</p>`:''}<p><strong>Next:</strong> ${esc(row.next)}</p><small>${esc(row.updated)} · Your action: ${esc(row.ownerAction)}</small></article>`).join('');
  const remainder=total>rows.length?`<p><strong>${total-rows.length} additional active tasks are omitted from this bounded view.</strong></p>`:'';
  return`<section aria-label="Tasks monitored by the CTO"><h3>Tasks under CTO monitoring</h3><div class="cto-task-list">${items}</div>${remainder}</section>`;
}

function agentStatusSummary(agent,state,contracts){
  if(agent.status==='paused')return{summary:'This agent and its exact next action are intentionally paused. The terminal is stopped and the board is read-only.',next:'Resume the project to continue from the saved gate. No work has been reset or re-queued.'};
  if(agent.role==='qa'&&reviewExecutionActive(agent))return{summary:'The Independent Reviewer is actively running a long executable check. Execution heartbeats are current while board polling is temporarily deferred; this is not an abandoned agent. You do not need to do anything.',next:'Wait for the executable check to finish; the reviewer will post PASS or FAIL. Your action: none.'};
  if(agent.liveness==='stalled'&&recentOutputActive(agent))return{summary:`The ${agent.role==='qa'?'Independent Reviewer':'Delivery Agent'} for ${humanTask(agent,state)} is producing recent terminal output, but its board status update is overdue. This is not enough to satisfy the board heartbeat or release gates; the harness has routed a short internal update request and will not show a Recover action.`,next:'Post a short board status update. Owner action is not required.'};
  if(agent.liveness==='stalled')return{summary:`The ${agent.role==='qa'?'Independent Reviewer':agent.role==='cto'?'CTO':'Delivery Agent'} for ${humanTask(agent,state)} stopped checking the board. The harness must recover it; you do not need to intervene.`,next:'Resume the saved work and report a plain-language update. Your action: none.'};
  if(agent.task==='AWAITING_OWNER_DIRECTION')return{summary:'This Delivery Agent is open and waiting for your development direction.',next:'Use Give direction in Mission Control when you are ready.'};
  if(agent.role==='cto'){
    const names=Array.isArray(state.live_tasks)?state.live_tasks:taskNames(state,contracts,{}),releases=state.releases||{},decisions=state.release_decisions||{};
    const ready=names.filter(name=>releases[name]?.status==='VISUAL_TEST_REQUIRED'&&!decisions[name]);
    const taskCount=`${names.length} current ${names.length===1?'task':'tasks'}`;
    if(ready.length){
      const readyCount=`${ready.length} ${ready.length===1?'task is':'tasks are'}`;
      return{summary:`The CTO is monitoring ${taskCount}. ${readyCount} ready for your test.`,next:'The tested version is on main and is waiting for your decision.',ownerAction:`Test ${ready.length===1?'the ready task':'the ready tasks'}, then choose Accepted or Send feedback.`};
    }
    return{summary:`The CTO is monitoring ${taskCount}. No current task is ready for your test yet. You do not need to do anything.`,next:'Keep work moving and release only the exact independently tested commit on clean pushed main.',ownerAction:'None.'};
  }
  if(agent.role==='qa'){
    const assignment=reviewerAssignment(agent,state);
    if(assignment?.status==='authoring')return{summary:`This Independent Reviewer was notified about ${objectiveSummary(assignment.task)} while Delivery checks are still running.`,next:'Reserve the exact review and record independent test intentions. Do not execute tests or post a verdict until Delivery succeeds. Owner action is not required.'};
    if(assignment?.status==='open')return{summary:`This Independent Reviewer was actively notified about ${objectiveSummary(assignment.task)} and the review is waiting for an immediate reservation.`,next:'Reserve the review now, then prepare and attach the distinct Challenge Ledger. No owner action is required.'};
    if(assignment?.status==='reserved'&&assignment.delivery_state==='executing')return{summary:`The Independent Reviewer reserved ${objectiveSummary(assignment.task)} while Delivery checks continue and is authoring independent test intentions.`,next:'Record the independent intentions and prepare the distinct Challenge Ledger. Wait for Delivery success before attaching or executing it. Your action: none.'};
    if(assignment?.status==='reserved')return{summary:`The Independent Reviewer reserved ${objectiveSummary(assignment.task)} and is preparing a different Challenge Ledger. The review is owned and has not begun execution yet.`,next:'Attach the validated distinct Challenge Ledger, then execute the review. Your action: none.'};
    return assignment?{summary:`The Independent Reviewer is checking ${objectiveSummary(assignment.task)} with a different scenario ledger.`,next:'Post executable PASS or FAIL evidence, then claim the next waiting review. Your action: none.'}:{summary:'The Independent Reviewer has no claimed review and is waiting at its interactive prompt for the harness to route the next eligible request.',next:'The harness will wake this reviewer automatically when a review opens. Your action: none.'};
  }
  const facts=taskFacts(state,contracts,agent.task);
  const gate=taskGate(state,agent.task,facts.contract,agent,facts.reviews,facts.total,facts.done);
  const brief=(state.task_briefs||{})[agent.task];
  return{summary:brief?.update||gate.next,next:gate.next};
}

let decisionTask='';
let pushTask='';

async function submitAccepted(task){
  try{
    await call('/api/releases/'+encodeURIComponent(task)+'/decision',{decision:'accepted'});
    el('#notice').textContent='Your response was saved with this release.';
    await refresh();
  }catch(error){el('#notice').textContent='We could not save your response. Please try again.';}
}

function openDecisionDialog(task){
  decisionTask=task;
  el('#decision-reason').value='';
  el('#decision-attachments').value='';
  el('#decision-error').textContent='';
  el('#decision-submit').disabled=false;
  el('#decision-dialog').showModal();
}

function openPushDialog(task){
  pushTask=task;el('#push-remote').value='origin';el('#push-branch').value='main';el('#push-error').textContent='';el('#push-submit').disabled=false;el('#push-dialog').showModal();
}

async function confirmPush(task,instruction){
  try{await call('/api/releases/'+encodeURIComponent(task)+'/push-confirm',{instruction_id:instruction});el('#notice').textContent='The accepted commit was pushed after your confirmation.';await refresh();}
  catch(error){el('#notice').textContent=error.message||'Push stopped safely; local acceptance is unchanged.';await refresh();}
}

async function submitPushInstruction(event){
  event.preventDefault();const error=el('#push-error'),submit=el('#push-submit');submit.disabled=true;error.textContent='';
  try{await call('/api/releases/'+encodeURIComponent(pushTask)+'/push-instruction',{remote:el('#push-remote').value,branch:el('#push-branch').value});el('#push-dialog').close();el('#notice').textContent='Push instruction saved. Review it, then use Confirm push now for the separate network-contact confirmation.';await refresh();}
  catch(failure){error.textContent=failure.message;}finally{submit.disabled=false;}return false;
}

async function submitRejection(event){
  event.preventDefault();
  const reason=el('#decision-reason').value;
  const error=el('#decision-error');
  if(!reason.trim()){error.textContent='Please explain what should be changed before sending your response.';return false;}
  const form=new FormData();
  form.append('decision','not_accepted');
  form.append('reason',reason);
  for(const file of Array.from(el('#decision-attachments').files||[]))form.append('attachments',file,file.name);
  const submit=el('#decision-submit');
  submit.disabled=true;
  error.textContent='';
  try{
    await callMultipart('/api/releases/'+encodeURIComponent(decisionTask)+'/decision',form);
    el('#decision-dialog').close();
    el('#notice').textContent='Your response was saved. Delivery has your explanation and attachments for the repair cycle.';
    await refresh();
  }catch(failure){error.textContent=failure.message;}
  finally{submit.disabled=false;}
  return false;
}

function sessionColorHtml(session){
  const color=session?.color_hex||'#000000', label=session?.color_label||'Standard black';
  return `<span class="terminal-color"><i class="terminal-color-swatch" style="background:${esc(color)}" aria-hidden="true"></i>Terminal color: ${esc(label)}</span>`;
}

function sessionCanRepresentAgent(session){
  return Boolean(session&&(
    activeStates.includes(session.status)||
    preservedSessionStates.includes(session.status)
  ));
}

function sessionIsVisibleTerminal(session){
  return Boolean(session&&activeStates.includes(session.status));
}

function unmatchedSessionStage(session){
  if(session.status==='stopping')return'STOPPING';
  if(session.status==='pausing')return'PAUSING';
  return'STARTING';
}

function unmatchedSessionNote(session){
  if(session.status==='pausing')return'Terminal is pausing and its board state is being preserved.';
  return'Terminal is starting; board agent registration is pending.';
}

function agentOrderKey(agent){
  const role=String(agent.role||'').toLowerCase();
  const roleRank=role==='cto'?0:role==='engineering'||role==='development'?1:role==='qa'?2:3;
  return `${roleRank}|${String(agent.task||'').toLowerCase()}|${String(agent.id||'').toLowerCase()}`;
}

function openAgents(state,contracts,sessionItems=[]){
  const out=el('#agents');
  const attachableSessions=(sessionItems||[]).filter(sessionCanRepresentAgent);
  const visibleSessions=attachableSessions.filter(sessionIsVisibleTerminal);
  const sessionsById=new Map(attachableSessions.map(session=>[session.id,session]));
  const items=Object.values(state.agents||{}).filter(agent=>{
    if(agent.active)return true;
    const session=sessionsById.get(agent.session_id);
    return Boolean(session&&(agent.status==='paused'||(['engineering','development'].includes(agent.role)&&agent.task!=='AWAITING_OWNER_DIRECTION')));
  });
  const representedSessions=new Set();
  out.replaceChildren();
  if(!items.length&&!visibleSessions.length){out.innerHTML='<div class="empty">No active agents or terminals.</div>';return;}
  for(const agent of items.sort((left,right)=>agentOrderKey(left).localeCompare(agentOrderKey(right)))){
    const session=sessionsById.get(agent.session_id); if(session)representedSessions.add(session.id);
    const task=humanTask(agent,state), stage=humanStage(agent,state,contracts), wording=agentStatusSummary(agent,state,contracts);
    const row=document.createElement('div');
    row.className='agent-row';
    row.dataset.agentId=agent.id; row.dataset.sessionId=session?.id||''; row.dataset.task=task;
    const provider=agent.vendor||session?.vendor||'Provider not recorded', model=session?.model||'Model not recorded';
    row.innerHTML=`<div class="row"><div><strong>${esc(agent.display_name||agent.role)} — ${esc(task)}</strong><small class="agent-meta">Role: ${esc(agent.role)} · Provider: ${esc(provider)} · Model: ${esc(model)} · Board stage: ${esc(stage)}</small>${sessionColorHtml(session)}</div>${badge(stage)}</div><small class="agent-meta">${esc(wording.summary)}</small>`;
    const actions=document.createElement('div');actions.className='actions';
    if(agent.role==='engineering'||agent.role==='development'){
      const waiting=agent.task==='AWAITING_OWNER_DIRECTION', directionSent=Boolean((state.owner_directions||{})[agent.session_id]?.text);
      const releaseReady=(state.releases||{})[agent.task]?.status==='VISUAL_TEST_REQUIRED',decision=(state.release_decisions||{})[agent.task];
      if(decision?.decision!=='accepted'){
        const ownerButton=document.createElement('button');ownerButton.className='secondary';ownerButton.type='button';ownerButton.disabled=false;
        if(releaseReady&&!decision){
          ownerButton.textContent='Send feedback';ownerButton.title='Tell Delivery what must change before you accept this task.';ownerButton.onclick=()=>openDecisionDialog(agent.task);
        }else{
          ownerButton.textContent=waiting?(directionSent?'Respond to Delivery':'Give direction'):'Send clarification';ownerButton.title=directionSent?'Reply to the Product Manager, approve the proposed requirements, or request changes.':'Send a complete owner message through the safe composer.';ownerButton.onclick=()=>openOwnerMessageDialog(agent.id,waiting&&!directionSent?'direction':'clarification');
        }
        actions.append(ownerButton);
      }
    }
    const button=document.createElement('button');
    button.className='secondary status-button';button.type='button';button.textContent='View status';button.onclick=()=>showAgentStatus(agent.id);
    actions.append(button);
    if((agent.liveness==='stalled'&&!reviewExecutionActive(agent)&&!recentOutputActive(agent))||agent.status==='blocked'){
      const recover=document.createElement('button');recover.type='button';recover.textContent='Recover agent';recover.onclick=()=>recoverAgent(agent.id,humanTask(agent,state));actions.append(recover);
    }
    if(session){
      const stop=document.createElement('button');stop.className='stop';stop.type='button';stop.textContent='Stop terminal';stop.disabled=session.status==='stopping';stop.onclick=()=>confirmStopSession(session.id,session.label,task,stage);actions.append(stop);
    }else{
      const disconnected=document.createElement('small');disconnected.className='agent-meta';disconnected.textContent='Terminal is not currently connected; task memory remains on the board.';actions.append(disconnected);
    }
    row.append(actions);out.append(row);
  }
  for(const session of visibleSessions.sort((left,right)=>String(left.id||'').localeCompare(String(right.id||'')) )){
    if(representedSessions.has(session.id))continue;
    const row=document.createElement('div');row.className='agent-row session-starting';row.dataset.sessionId=session.id;row.dataset.task=session.task||'';
    const superseded=Boolean(session.superseded_by_agent_id||session.read_only);
    const task=objectiveSummary(session.superseded_task||session.task||(superseded?'Recovered task':'Waiting to attach to a board agent'));
    const stage=superseded?'STOPPING — SUPERSEDED':unmatchedSessionStage(session);
    const note=superseded?`Read-only predecessor superseded by ${session.superseded_by_agent_id||'the replacement Delivery Agent'}. Its terminal is stopping and cannot change the task.`:unmatchedSessionNote(session);
    row.innerHTML=`<div class="row"><div><strong>${esc(session.label)} — ${esc(task)}</strong><small class="agent-meta">${esc(note)}</small>${sessionColorHtml(session)}</div>${badge(stage)}</div>`;
    const actions=document.createElement('div');actions.className='actions';
    const stop=document.createElement('button');stop.className='stop';stop.type='button';stop.textContent='Stop terminal';stop.disabled=session.status==='stopping';stop.onclick=()=>confirmStopSession(session.id,session.label,task,stage);actions.append(stop);
    row.append(actions);out.append(row);
  }
}

function openOwnerMessageDialog(agentId,type){
  ownerMessageAgentId=agentId; ownerMessageType=type;
  el('#owner-message-title').textContent=type==='direction'?'Give direction to Delivery':'Send clarification to Delivery';
  el('#owner-message-help').textContent=type==='direction'?'This message will be sent as one complete owner instruction. Delivery will clarify it before asking you to say go ahead.':'This response is appended to the current or pending task and sent to its Delivery Agent. You can approve the proposal or request changes; the original directive is never replaced.';
  el('#owner-message-submit').textContent=type==='direction'?'Send direction':'Send clarification';
  el('#owner-message-text').value=''; el('#owner-message-directive-file').value=''; el('#owner-message-attachments').value=''; el('#owner-message-error').textContent=''; el('#owner-message-file-status').className=''; el('#owner-message-file-status').textContent='The browser reads the file as strict UTF-8. Its path is never sent or opened by the worker.'; el('#owner-message-submit').disabled=false; updateOwnerMessageCount();
  el('#owner-message-dialog').showModal();
}

async function submitOwnerMessage(event){
  event.preventDefault();
  const error=el('#owner-message-error'),directiveFile=el('#owner-message-directive-file').files?.[0];
  if(ownerDirectiveLoading){error.textContent='Wait for the directive file to finish loading.';return false;}
  let validated;try{validated=validateOwnerMessageText(el('#owner-message-text').value);}catch(failure){error.textContent=failure.message;return false;}
  const form=new FormData(); form.append('message_type',ownerMessageType); form.append('text',validated.text); form.append('directive_source',directiveFile?'file':'text'); form.append('directive_filename',directiveFile?.name||'');
  for(const file of Array.from(el('#owner-message-attachments').files||[]))form.append('attachments',file,file.name);
  const submit=el('#owner-message-submit'); submit.disabled=true; error.textContent='';
  try{
    await callMultipart('/api/agents/'+encodeURIComponent(ownerMessageAgentId)+'/owner-message',form);
    el('#owner-message-dialog').close(); el('#notice').textContent=ownerMessageType==='direction'?'Your complete direction was sent to Delivery.':'Your clarification and attachments were sent to the current Delivery Agent.'; await refresh();
  }catch(failure){error.textContent=failure.message;}
  finally{submit.disabled=false;}
  return false;
}

function showAgentStatus(agentId){
  const agent=lastBoard?.state?.agents?.[agentId];
  if(!agent)return;
  const state=lastBoard.state||{},contracts=lastBoard.contracts||{},wording=agentStatusSummary(agent,state,contracts),needsOwner=agent.task==='AWAITING_OWNER_DIRECTION',directionSent=Boolean((state.owner_directions||{})[agent.session_id]?.text);
  el('#status-dialog-title').textContent=(agent.display_name||agent.role)+' status';
  const ledger=agentChecklistHtml(lastBoard?.agent_checklists?.[agent.id]);
  const ownerAction=needsOwner?(directionSent?'Use Respond to Delivery to approve the proposal or request changes.':'Use Give direction beside this agent in Mission Control.'):(wording.ownerAction||'None.');
  el('#status-dialog-body').innerHTML=`<dl><dt>Current situation</dt><dd>${esc(wording.summary)}</dd><dt>Task</dt><dd>${esc(humanTask(agent,state))}</dd><dt>Current stage</dt><dd>${esc(humanStage(agent,state,contracts))}</dd><dt>Last update</dt><dd>${esc(relativeUpdate(agent.last_status_at))}</dd><dt>What happens next</dt><dd>${esc(wording.next.replace(/\s*(USER|Your) ACTION:\s*None\.?/i,''))}</dd><dt>Your action</dt><dd>${esc(ownerAction)}</dd></dl>${agent.role==='cto'?ctoTaskRowsHtml(state,contracts):ledger}`;
  const dialog=el('#status-dialog'),title=el('#status-dialog-title');
  dialog.showModal();
  title.focus?.({preventScroll:true});
  dialog.scrollTop=0;
}

function queue(state){
  const out=el('#queue');
  const items=Object.values(state.qa_requests||{}).filter(item=>['authoring','open','reserved','claimed','suspended'].includes(item.status));
  out.replaceChildren();
  if(!items.length){out.innerHTML='<div class="empty">No review is waiting right now.</div>';return;}
  for(const item of items.sort((left,right)=>String(left.requested_at).localeCompare(String(right.requested_at)))){
    const row=document.createElement('div');row.className='queue';
    const label=item.phase==='final_acceptance'?'Final acceptance':item.phase==='subtask_acceptance'?'Subtask acceptance':'Chunk review';
    const scope=item.subtask?`${objectiveSummary(item.subtask)}${item.phase==='chunk'?' · '+objectiveSummary(item.chunk):''}`:(item.chunk||'Complete task');
    const queueStatus=item.status==='suspended'?'PAUSED — REVIEWER PRESERVED':item.status==='claimed'?'REVIEW EXECUTING':item.status==='reserved'&&item.delivery_state==='executing'?'REVIEWER AUTHORING WHILE DELIVERY TESTS':item.status==='reserved'?'REVIEWER PREPARING CHALLENGE LEDGER':item.status==='authoring'&&item.routed_to?'AUTHORING ROUTED — RESERVE NOW':item.status==='authoring'?'DELIVERY TESTING — REVIEW READY':item.routed_to?'ROUTED — RESERVE NOW':'WAITING FOR REVIEWER';
    row.innerHTML=`<div class="row"><strong>${esc(label+' — '+objectiveSummary(item.task))}</strong>${badge(queueStatus)}</div><small>${esc(scope)}</small>`;
    out.append(row);
  }
}

function terminalDetails(session,state,contracts){
  if(session.superseded_by_agent_id||session.read_only)return{task:objectiveSummary(session.superseded_task||session.task||'Recovered task'),stage:'STOPPING — SUPERSEDED',summary:`Read-only predecessor superseded by ${session.superseded_by_agent_id||'the replacement Delivery Agent'}. It cannot change the task and is being stopped.`};
  const agents=Object.values(state.agents||{});
  const agent=agents.find(item=>item.session_id===session.id);
  if(!agent)return{task:session.task?objectiveSummary(session.task):'Waiting to attach',stage:unmatchedSessionStage(session),summary:session.reason||unmatchedSessionNote(session)};
  const stage=humanStage(agent,state,contracts);
  const summary=agentStatusSummary(agent,state,contracts).summary;
  return{task:humanTask(agent,state),stage,summary};
}

function sessions(items,state,contracts){
  const out=el('#sessions');
  const live=items.filter(sessionIsVisibleTerminal);
  out.replaceChildren();
  if(!live.length){out.innerHTML='<div class="empty">No live CLI sessions.</div>';return;}
  for(const session of live){
    const details=terminalDetails(session,state,contracts);
    const row=document.createElement('div');row.className='session';row.dataset.sessionId=session.id;row.dataset.task=details.task;
    if(session.color_hex){row.style=row.style||{};row.style.borderLeft=`6px solid ${session.color_hex}`;row.style.paddingLeft='10px';}
    const colorNote=session.color_label?`<span class="terminal-color"><i class="terminal-color-swatch" style="background:${esc(session.color_hex||'#000000')}" aria-hidden="true"></i>Terminal color: ${esc(session.color_label)}</span>`:'<span class="terminal-color"><i class="terminal-color-swatch" style="background:#000000" aria-hidden="true"></i>Terminal color: Standard black</span>';
    const label=document.createElement('div');label.innerHTML=`<strong>${esc(session.label)} — ${esc(details.task)}</strong>${badge(details.stage)}<small>${esc(details.summary)}</small>${colorNote}`;
    const stop=document.createElement('button');stop.className='stop';stop.textContent='Stop';stop.disabled=session.status==='stopping';stop.dataset.sessionId=session.id;stop.dataset.task=details.task;stop.onclick=()=>confirmStopSession(session.id,session.label,details.task,details.stage);
    row.append(label,stop);out.append(row);
  }
}

async function confirmStopSession(id,label,task,stage){
  const approved=window.confirm(`Stop ${label} for “${task}”?\n\nCurrent stage: ${stage}\n\nIf this is an unfinished Delivery task, its board records and isolated workspace will be removed. Unexpected terminal crashes still preserve recovery memory.`);
  if(!approved)return false;
  try{
    await call('/api/sessions/'+encodeURIComponent(id)+'/stop');
    el('#notice').textContent=`Stopped ${label} — ${task}. Unfinished Delivery work was cleaned from the board.`;
    await refresh();
    return true;
  }catch(error){el('#notice').textContent='Could not stop session: '+error.message;return false;}
}

async function stopAllAgents(){
  const approved=window.confirm('Stop all agents?\n\nEvery live terminal will stop. All unfinished Delivery tasks, review requests, and isolated task workspaces will be removed from the board. Released work waiting for your acceptance is preserved.');
  if(!approved)return false;
  try{
    const result=await call('/api/sessions/stop-all',{});
    el('#notice').textContent=`Stopped ${result.stopped_sessions||0} terminals and removed ${result.cancelled_tasks?.length||0} unfinished tasks.`;
    await refresh();
    return true;
  }catch(error){el('#notice').textContent='Could not stop all agents: '+error.message;return false;}
}

async function recoverAgent(agentId,task){
  try{
    await call('/api/agents/'+encodeURIComponent(agentId)+'/recover');
    el('#notice').textContent=`Recovery requested for ${task}. The task memory and next action were preserved.`;
    await refresh();
  }catch(error){el('#notice').textContent='Could not recover agent: '+error.message;}
}

function render(data,managed){
  const state=data.state||{},contracts=data.contracts||{},agents=Object.values(state.agents||{}),reviews=Object.values(state.qa_requests||{});
  const projectPaused=['paused','resuming'].includes(state.project_pause?.status);
  state.live_tasks=Array.isArray(data.live_tasks)?[...data.live_tasks]:[];
  state.in_scope_findings=Array.isArray(data.in_scope_findings)?[...data.in_scope_findings]:[];
  lastBoard=data;
  el('#project').textContent=data.path||'Local project';
  el('#updated').textContent='Updated '+new Date(data.updated).toLocaleTimeString();
  el('#active').textContent=agents.filter(agent=>agent.active).length;
  el('#open').textContent=reviews.filter(review=>['authoring','open'].includes(review.status)).length;
  el('#claimed').textContent=reviews.filter(review=>review.status==='claimed').length;
  const liveTasks=new Set(data.live_tasks||[]);
  el('#passed').textContent=reviews.filter(review=>review.status==='passed'&&liveTasks.has(review.task)).length;
  const stalled=agents.filter(agent=>agent.active&&agent.liveness==='stalled'&&!reviewExecutionActive(agent)&&!recentOutputActive(agent)&&agent.recovery_state!=='reset_requested');
  if(stalled.length)el('#attention').innerHTML=`<h2>Automation recovery in progress</h2><p><strong>Your action: none.</strong> ${esc(stalled.map(agent=>`${agent.display_name||agent.role} for ${humanTask(agent,state)}`).join(', '))} stopped checking the board. The harness must recover the saved work.</p>`;
  else el('#attention').innerHTML='<h2>What you need to do</h2><p>Nothing while Delivery, independent review, or CTO release checks are in progress. The viewer will explicitly say <strong>READY FOR YOUR TEST</strong> when the exact tested version is clean and pushed to main.</p>';
  tasks(state,contracts,data.owner_directions||{},data.live_tasks,data.in_scope_findings||[],data.requirement_confirmations||{});
  currentHistoryVersion=String(data.history_version||'');
  if(historyLoaded&&historyLoadedAtVersion!==currentHistoryVersion)loadHistory(true);
  openAgents(state,contracts,managed.sessions||[]);
  queue(state);
  fitTaskScroller();
  const counts=managed.active_counts||{};
  const limits=managed.limits||{codex_delivery:2,claude_reviewer:2,claude_cto:1};
  const configured=managed.agent_settings||{};
  const launchButtons=[
    ['#codex','codex_delivery','CODEX CLI · Delivery Agent'],
    ['#claude','claude_reviewer','CLAUDE CLI · Reviewer'],
    ['#cto','claude_cto','CTO (CLAUDE)'],
  ];
  for(const [selector,kind,label] of launchButtons){
    const button=el(selector), active=Number(counts[kind]||0), limit=Number(limits[kind]||0);
    const role=kind==='codex_delivery'?'delivery':kind==='claude_reviewer'?'reviewer':'cto', setting=configured[role]||{}, provider=setting.provider==='claude'?'Claude':'Codex', model=setting.model||'default model', effort=setting.effort==='xhigh'?'extra high':(setting.effort||'high');
    button.textContent=`${provider} · ${model} · ${role==='reviewer'?'Reviewer':role==='cto'?'CTO':'Delivery'} · ${effort} (${active}/${limit})`;
    button.disabled=active>=limit;
    button.title=active>=limit?`Limit reached: ${active} of ${limit} active. Stop one before starting another.`:`Start a visible ${label} session (${active} of ${limit} active).`;
  }
  const staged=(managed.sessions||[]).filter(item=>item.status==='launching'&&!item.resume_launch_requested_at&&item.resume_offer==='relaunch');
  const relaunch=el('#relaunch-preserved');
  if(relaunch){
    relaunch.hidden=staged.length===0;
    relaunch.textContent=`Relaunch ${staged.length} preserved agent${staged.length===1?'':'s'}`;
    relaunch.dataset.sessions=staged.map(item=>item.id).join(',');
  }
  const activeTotal=Object.values(counts).reduce((total,value)=>total+Number(value||0),0);
  el('#stop-all').disabled=activeTotal===0;
  el('#stop-all').title=activeTotal?`Stop ${activeTotal} live terminal${activeTotal===1?'':'s'} and clean unfinished work.`:'No live terminals to stop.';
  document.body.classList.toggle('project-paused',projectPaused);
  el('#paused-banner').hidden=!projectPaused;
  if(projectPaused){
    document.querySelectorAll('button').forEach(button=>{
      button.disabled=true;
      button.title='Unavailable while this project is paused. Resume the project to make changes.';
    });
  }
  applyChatAvailability(managed.project_chat,projectPaused);
}

function applyChatAvailability(state,projectPaused){
  const input=el('#project-chat-input');
  if(!input)return;
  if(!state)return;
  const locked=el('#project-chat-locked'),send=el('#project-chat-send');
  const available=Boolean(state.available);
  const reason=state.reason?String(state.reason):'';
  chatAvailable=available;
  input.disabled=projectPaused||!available;
  if(send)send.disabled=projectPaused||!available;
  if(locked){
    locked.hidden=available;
    locked.textContent=available?'':reason;
  }
}

function showColorDialog(kind){
  pendingLaunchKind=kind;
  el('#color-options').innerHTML=terminalColors.map((color,index)=>`<label class="color-choice"><input type="radio" name="terminal-color" value="${color.id}"${index===0?' checked':''}><span class="color-swatch" style="background:${color.hex}"></span><span>${color.label}</span></label>`).join('');
  el('#color-dialog').showModal();
}

async function start(kind,color='black'){
  try{await call('/api/sessions',{kind,color});el('#notice').textContent=`${kind==='codex_delivery'?'CODEX Delivery Agent':kind==='claude_reviewer'?'CLAUDE Independent Reviewer':'CTO'} launch requested with ${terminalColors.find(item=>item.id===color)?.label||'standard black'} terminal.`;await refresh();}
  catch(error){el('#notice').textContent='Could not launch session: '+error.message;}
}

async function refresh(){
  if(refreshing)return;
  refreshing=true;
  try{
    const [dashboard,managed]=await Promise.all([fetch(apiPath('/api/dashboard'),{cache:'no-store'}).then(response=>response.json()),fetch(apiPath('/api/control'),{cache:'no-store'}).then(response=>response.json())]);
    if(dashboard.viewer_version&&dashboard.viewer_version!==loadedViewerVersion){window.location.reload();return;}
    render(dashboard,managed);
    boardFailures=0;
    el('#board-offline')?.classList.remove('show');
  }catch(error){
    el('#notice').textContent='Board unavailable: '+error.message;
    boardFailures+=1;
    if(boardFailures>=3){
      const overlay=el('#board-offline');
      if(overlay){
        const back=el('#project-nav-projects');
        const link=el('#board-offline-link');
        if(back&&link){link.href=back.href;link.hidden=false;}
        else if(link){link.hidden=true;}
        overlay.classList.add('show');
      }
    }
  }
  finally{refreshing=false;}
}

async function loadHistory(force=false){
  if(historyLoading||(!force&&historyLoaded))return;
  historyLoading=true;
  const status=el('#history-search-status');
  if(status)status.textContent='Loading complete task history…';
  try{
    const response=await fetch(apiPath('/api/history'),{cache:'no-store'});
    if(!response.ok)throw Error('history unavailable');
    const data=await response.json();
    historyEntries=Array.isArray(data.task_history)?data.task_history:[];
    historyLoadedAtVersion=currentHistoryVersion;
    historyLoaded=true;
    renderHistory(historyEntries,el('#history-search')?.value||'');
  }catch(error){if(status)status.textContent='Task history could not be loaded: '+error.message;}
  finally{historyLoading=false;}
}

el('#status-dialog-close').onclick=()=>el('#status-dialog').close();
el('#color-cancel').onclick=()=>{el('#color-dialog').close();const kind=pendingLaunchKind;pendingLaunchKind='';if(kind)start(kind,'black');};
el('#color-form').onsubmit=event=>{event.preventDefault();const selected=el('#color-options').querySelector('input[name="terminal-color"]:checked')?.value||'black';const kind=pendingLaunchKind;pendingLaunchKind='';el('#color-dialog').close();if(kind)start(kind,selected);};
el('#decision-cancel').onclick=()=>el('#decision-dialog').close();
el('#decision-form').onsubmit=submitRejection;
el('#push-cancel').onclick=()=>el('#push-dialog').close();
el('#push-form').onsubmit=submitPushInstruction;
el('#owner-message-cancel').onclick=()=>el('#owner-message-dialog').close();
el('#owner-message-form').onsubmit=submitOwnerMessage;
const closeProjectButton=el('#close-project');
if(closeProjectButton){
  closeProjectButton.onclick=()=>el('#close-project-dialog').showModal();
  el('#close-project-cancel').onclick=()=>el('#close-project-dialog').close();
}
el('#owner-message-text').oninput=editOwnerMessageText;
el('#owner-message-directive-file').onchange=loadOwnerDirectiveFile;
el('#history-search').addEventListener('input',async event=>{if(!historyLoaded)await loadHistory();renderHistory(historyEntries,event.target.value);});
el('#history').addEventListener('toggle',event=>{if(event.target.open)loadHistory();});
window.addEventListener('resize',fitTaskScroller);
el('#codex').onclick=()=>showColorDialog('codex_delivery');
el('#claude').onclick=()=>showColorDialog('claude_reviewer');
el('#cto').onclick=()=>showColorDialog('claude_cto');
el('#stop-all').onclick=stopAllAgents;
el('#relaunch-preserved').onclick=async()=>{
  const ids=(el('#relaunch-preserved').dataset.sessions||'').split(',').filter(Boolean);
  for(const id of ids){
    try{await call('/api/sessions/'+encodeURIComponent(id)+'/resume-launch',{});}
    catch(error){el('#notice').textContent='Could not relaunch a preserved agent: '+error.message;return;}
  }
  el('#notice').textContent=`Relaunch requested for ${ids.length} preserved agent${ids.length===1?'':'s'}.`;
  await refresh();
};
loadAccessPanel();
// The viewer force-reloads whenever its version changes, so an open/closed
// panel must survive a reload or the owner has to re-collapse it every time.
function rememberPanel(id){
  const node=el('#'+id);
  if(!node||typeof node.addEventListener!=='function')return;
  try{const saved=window.localStorage.getItem('panel-open:'+id);if(saved!==null)node.open=saved==='1';}catch(error){}
  node.addEventListener('toggle',()=>{try{window.localStorage.setItem('panel-open:'+id,node.open?'1':'0');}catch(error){}});
}
rememberPanel('settings-details');
rememberPanel('history');
refresh();
setInterval(refresh,2000);
</script>
<script>
let chatAvailable=!el('#project-chat-input')?.disabled;
let chatController=null;
let chatRequestId='';
const chatTurns=[];
const chatQuestionMaxBytes=2048;
function renderChat(){
  const history=el('#project-chat-history');
  if(!history)return;
  history.innerHTML=chatTurns.length?chatTurns.map(turn=>`<div class="chat-turn chat-question"><strong>You</strong>\n${esc(turn.question)}</div><div class="chat-turn chat-answer"><strong>Project assistant</strong>\n${esc(turn.answer)}</div>`).join(''):'<div class="chat-empty">Ask anything about this project — status, reviews, releases, blockers, timing, or what you should do next. Chat is read-only and cannot make changes.</div>';
  history.scrollTop=history.scrollHeight;
}
function chatToken(){return el('#project-chat')?.dataset.actionToken||'';}
function chatBytes(value){return new TextEncoder().encode(String(value||'')).byteLength;}
function setChatBusy(busy){
  const send=el('#project-chat-send'),cancel=el('#project-chat-cancel'),input=el('#project-chat-input');
  if(!send)return;
  send.disabled=busy;cancel.hidden=!busy;input.setAttribute('aria-busy',busy?'true':'false');
}
async function sendChat(event){
  event?.preventDefault();
  if(chatController)return;
  const input=el('#project-chat-input'),status=el('#project-chat-status'),question=input.value.trim(),bytes=chatBytes(question);
  status.className='project-chat-status';
  if(!chatAvailable){status.className='project-chat-status project-chat-error';status.textContent=el('#project-chat-locked')?.textContent||'Project chat needs your OpenAI API key. Add it in Settings to switch chat on.';return;}
  if(!question){status.textContent='Write a project question first.';input.focus();return;}
  if(bytes>chatQuestionMaxBytes){status.className='project-chat-status project-chat-error';status.textContent=`Question is ${bytes.toLocaleString()} bytes; the limit is ${chatQuestionMaxBytes.toLocaleString()}.`;input.focus();return;}
  chatRequestId=crypto.randomUUID();chatController=new AbortController();setChatBusy(true);status.textContent='Checking current project facts…';
  try{
    const response=await fetch(apiPath('/api/project-chat'),{method:'POST',headers:{'Content-Type':'application/json','X-Harness-Chat-Action':chatToken()},body:JSON.stringify({request_id:chatRequestId,question}),signal:chatController.signal});
    const data=await response.json();
    if(!response.ok)throw Error(data.error||'Project chat failed.');
    chatTurns.push({question,answer:String(data.answer||'')});while(chatTurns.length>20)chatTurns.shift();
    input.value='';renderChat();status.textContent=data.unknown?'No supported project fact was available.':'Answered from the current project snapshot.';
  }catch(error){
    status.className='project-chat-status project-chat-error';
    status.textContent=error.name==='AbortError'?'Request cancelled.':`Chat error: ${error.message}`;
  }finally{chatController=null;chatRequestId='';setChatBusy(false);input.focus();}
}
async function cancelChat(){
  const requestId=chatRequestId,controller=chatController;
  if(!requestId||!controller)return;
  controller.abort();
  try{await fetch(apiPath('/api/project-chat/cancel'),{method:'POST',headers:{'Content-Type':'application/json','X-Harness-Chat-Action':chatToken()},body:JSON.stringify({request_id:requestId})});}catch(error){}
}
function clearChat(){
  if(chatController)return;
  chatTurns.splice(0);renderChat();el('#project-chat-status').textContent='Conversation cleared. Project facts were not changed.';el('#project-chat-input').focus();
}
const chatForm=el('#project-chat-form');
if(chatForm){
  chatForm.onsubmit=sendChat;
  el('#project-chat-cancel').onclick=cancelChat;
  el('#project-chat-clear').onclick=clearChat;
  el('#project-chat-input').addEventListener('keydown',event=>{if((event.metaKey||event.ctrlKey)&&event.key==='Enter'){event.preventDefault();sendChat(event);}});
  renderChat();
}
</script>
</body></html>"""


def viewer_version() -> str:
    """Version the exact page so an already-open browser reloads after a change."""
    return hashlib.sha256(PAGE.encode("utf-8")).hexdigest()[:12]


def rendered_page(project_name: str = "", project_description: str = "", manager_url: str = "",
                  project_id: str = "", manager_action_token: str = "",
                  chat_action_token: str = "", runtime: dict | None = None,
                  api_prefix: str = "", chat_availability: dict | None = None) -> str:
    """The served page; project identity is shown when a manager opened us.

    Defaults reproduce the historical standalone page exactly, so a viewer
    launched directly (no manager) is unchanged.
    """
    import html as _html
    name = _html.escape(project_name.strip()) if project_name and project_name.strip() else ""
    description = _html.escape(project_description.strip()) if project_description and project_description.strip() else ""
    manager_root = manager_url.strip().rstrip("/") if manager_url and manager_url.strip() else ""
    url = _html.escape(manager_root + "/", quote=True) if manager_root else ""
    close_url = ""
    if (manager_url and manager_url.strip() and project_id and project_id.strip()
            and manager_action_token and manager_action_token.strip()):
        close_url = _html.escape(
            manager_url.strip().rstrip("/") + "/projects/" + quote(project_id.strip(), safe="") + "/close",
            quote=True,
        )
    runtime_commit = str((runtime or {}).get("commit", ""))
    api_prefix = str(api_prefix or "").rstrip("/")
    if api_prefix and (not api_prefix.startswith("/") or not re.fullmatch(r"/[A-Za-z0-9/_-]+", api_prefix)):
        raise ValueError("browser API prefix is invalid")
    page = PAGE.replace("__VIEWER_VERSION__", viewer_version()).replace(
        "__RUNTIME_COMMIT__", _html.escape(runtime_commit, quote=True),
    ).replace("__RUNTIME_MANAGED__", "true" if project_id else "false").replace(
        "__API_PREFIX__", json.dumps(api_prefix),
    )
    chat_panel = ""
    if project_id and chat_action_token:
        token = _html.escape(chat_action_token, quote=True)
        availability = chat_availability or {"available": True, "reason": ""}
        chat_ready = bool(availability.get("available"))
        # Rendered disabled, not styled disabled: a page that loads without a
        # working key must never offer a composer that cannot answer.
        locked = "" if chat_ready else " disabled"
        notice = _html.escape(str(availability.get("reason", "")))
        chat_panel = (
            f'<section class="panel project-chat-panel" id="project-chat" data-action-token="{token}" aria-labelledby="project-chat-title">'
            '<div class="project-chat-head"><div><h2 id="project-chat-title">Ask about this project</h2><p>Answers use verified facts for this opened project only.</p></div><button class="secondary" type="button" id="project-chat-clear">Clear</button></div>'
            '<div class="project-chat-history" id="project-chat-history" role="log" aria-live="polite" aria-label="Project chat conversation"></div>'
            f'<p class="project-chat-locked" id="project-chat-locked" role="status"{"" if not chat_ready else " hidden"}>{notice}</p>'
            f'<form id="project-chat-form"><label for="project-chat-input">Question</label><textarea id="project-chat-input" rows="3" maxlength="2048" placeholder="What is left?" aria-describedby="project-chat-status"{locked}></textarea>'
            f'<div class="project-chat-actions"><button type="submit" id="project-chat-send"{locked}>Send</button><button class="secondary" type="button" id="project-chat-cancel" hidden>Cancel</button><span class="project-chat-status" id="project-chat-status" role="status" aria-live="polite"></span></div></form></section>'
        )
    page = page.replace("__PROJECT_CHAT_PANEL__", chat_panel)
    project_nav = ""
    if manager_root and project_id and project_id.strip():
        project_path = (api_prefix or "/project") + "/"
        projects_url = _html.escape(manager_root + "/", quote=True)
        mission_url = _html.escape(manager_root + project_path, quote=True)
        settings_url = _html.escape(manager_root + "/?page=settings", quote=True)
        help_url = _html.escape(manager_root + "/?page=help", quote=True)
        project_nav = (
            '<header class="project-topbar" id="project-navigation">'
            f'<a class="project-brand" href="{projects_url}" aria-label="NoMoreHappyPath projects"><img src="favicon.png?v=2" alt="" aria-hidden="true" style="width:30px;height:30px;border-radius:8px;object-fit:contain"><span>NoMoreHappyPath</span></a>'
            '<nav class="project-nav" aria-label="Primary navigation">'
            f'<a id="project-nav-projects" href="{projects_url}">Projects</a>'
            f'<a href="{mission_url}" aria-current="page">Mission Control</a>'
            f'<a href="{settings_url}">Settings</a>'
            f'<a href="{help_url}">Help</a>'
            '</nav><div class="project-nav-status"><i class="dot" aria-hidden="true"></i><span>Project open</span></div></header>'
        )
    page = page.replace("__PROJECT_NAV__", project_nav)
    if name:
        # Whole-tag substitution keeps the literal defaults inside PAGE itself,
        # so viewer_version() (the PAGE digest that drives browser reload and
        # the launcher's restart contract) still covers the template text.

        page = page.replace("<h1>NoMoreHappyPath Mission Control</h1>", f"<h1>{name}</h1>", 1)
        page = page.replace('<div class="eyebrow">Local delivery system</div>',
                            '<div class="eyebrow">NoMoreHappyPath Mission Control</div>', 1)
    if close_url:
        action_token = _html.escape(manager_action_token.strip(), quote=True)
        page = page.replace(
            "__PROJECT_CONTROLS__",
            '<div class="project-controls"><button class="stop" id="close-project" type="button">Close project</button></div>',
        )
        page = page.replace(
            "__PROJECT_CLOSE_DIALOG__",
            f'<dialog id="close-project-dialog" aria-labelledby="close-project-title"><form class="modal" method="post" action="{close_url}"><input type="hidden" name="action_token" value="{action_token}"><h2 id="close-project-title">Close project?</h2><p class="sub">Mission Control will stop. Saved tasks, reviews, evidence, and project files remain unchanged.</p><div class="modal-actions"><button class="secondary" id="close-project-cancel" type="button">Cancel</button><button class="stop" type="submit">Close project</button></div></form></dialog>',
        )
    else:
        page = page.replace("__PROJECT_CONTROLS__", "").replace("__PROJECT_CLOSE_DIALOG__", "")
    page = page.replace("__RESUME_LINK__", url or "#")
    page = page.replace("__PROJECT_DESC__", f'<div class="sub">{description}</div>' if description else "")
    return page


def payload(root: Path) -> dict:
    state = board.snapshot(root)
    return {"updated": board.now(), "state": state}


def _compact_dashboard_state(
    state: dict,
    live_tasks: list[str] | None = None,
    live_session_ids: set[str] | None = None,
) -> dict:
    """Project only live UI state; history is served separately on demand."""
    live = set(live_tasks or [])
    connected = set(live_session_ids or [])
    compact = json.loads(json.dumps({
        key: value for key, value in state.items()
        if key not in {"archive", "events", "qa_requests", "qa_request_index"}
    }))
    events = list(state.get("events", []))
    latest: dict[str, str] = {}
    for event in events:
        task = event.get("task")
        if task:
            latest[task] = event.get("at", "")
    compact["events"] = events[-50:]
    compact["latest_event_at_by_task"] = latest
    compact["agents"] = {
        key: value for key, value in (state.get("agents") or {}).items()
        if value.get("status") == "paused" or value.get("active") or (
            value.get("role") in board.DEVELOPER_ROLES
            and value.get("task") != board.AWAITING_OWNER_DIRECTION
            and value.get("session_id") in connected
        )
    }
    live_requests: dict[str, dict] = {}
    for task in live:
        candidates = [
            (key, value) for key, value in (state.get("qa_requests") or {}).items()
            if value.get("task") == task
        ]
        candidates.extend(
            (key, value) for key, value in (state.get("qa_request_index") or {}).items()
            if value.get("task") == task and key not in {item[0] for item in candidates}
        )
        candidates.sort(key=lambda item: _ledger_sort_key(item[1]), reverse=True)
        active = [item for item in candidates if item[1].get("status") in {"authoring", "open", "reserved", "claimed", "suspended"}]
        selected = (active + [item for item in candidates if item not in active])[:DASHBOARD_REVIEW_LIMIT_PER_TASK]
        for key, value in selected:
            live_requests[key] = value
    compact["qa_requests"] = json.loads(json.dumps(live_requests))
    compact["qa_request_index"] = {}
    compact["archive"] = []
    for key in (
        "task_chunks", "delivery_plans", "task_briefs", "task_baselines",
        "task_workspaces", "releases", "release_decisions", "release_repairs",
        "requirement_confirmations", "owner_clarifications",
    ):
        compact[key] = {
            task: value for task, value in (state.get(key) or {}).items()
            if task in live
        }
    active_sessions = {value.get("session_id") for value in compact["agents"].values() if value.get("session_id")}
    compact["owner_directions"] = {
        session: value for session, value in (state.get("owner_directions") or {}).items()
        if session in active_sessions
    }
    compact["owner_messages"] = [
        value for value in (state.get("owner_messages") or [])
        if not value.get("task") or value.get("task") in live
    ]
    compact["archive"] = []
    compact.pop("qa_request_index", None)
    compact["deferred_findings"] = {}
    return compact


def latest_owner_direction(state: dict) -> str:
    """Return the preserved, owner-authored direction for the current view."""
    for event in reversed(state.get("events", [])):
        if event.get("kind") == "task_begun" and event.get("owner_direction"):
            return contract.normalize_owner_direction(event["owner_direction"])
    for value in reversed(list(state.get("owner_directions", {}).values())):
        if value.get("text"):
            return contract.normalize_owner_direction(value["text"])
    return ""


def owner_directions_by_task(state: dict) -> dict[str, str]:
    """Preserve every in-flight task's own owner direction for the viewer."""
    directions: dict[str, str] = {}
    for agent in state.get("agents", {}).values():
        task = agent.get("task", "")
        if not task or task == board.AWAITING_OWNER_DIRECTION:
            continue
        value = board.owner_direction_for_task(state, agent["id"], task)
        if value:
            directions[task] = value
    for event in state.get("events", []):
        if event.get("kind") != "task_begun" or not event.get("task") or not event.get("owner_direction"):
            continue
        directions.setdefault(event["task"], contract.normalize_owner_direction(event["owner_direction"]))
    return directions


def requirement_confirmations_by_task(state: dict) -> dict[str, dict]:
    """Return the final clarified requirements without altering the original direction."""
    return {
        task: value
        for task, value in (state.get("requirement_confirmations") or {}).items()
        if isinstance(value, dict) and value.get("text")
    }


def _owner_direction_for_history(state: dict, task: str, contract_value: dict) -> str:
    """Return the exact persisted owner direction belonging to one task."""
    task_agent_ids = {
        event.get("agent_id")
        for event in state.get("events", [])
        if event.get("task") == task
        and event.get("kind") in {"task_begun", "task_resumed"}
        and event.get("agent_id")
    }
    task_agent_ids.update(
        agent.get("id")
        for agent in state.get("agents", {}).values()
        if agent.get("task") == task and agent.get("id")
    )
    original = next(
        (
            message.get("text", "")
            for message in state.get("owner_messages", [])
            if message.get("type") == "direction"
            and message.get("agent_id") in task_agent_ids
            and message.get("text")
        ),
        "",
    )
    if original:
        return contract.normalize_owner_direction(original)
    for agent_id in task_agent_ids:
        value = board.owner_direction_for_task(state, agent_id, task)
        if value:
            return value
    for event in reversed(state.get("events", [])):
        if event.get("kind") == "task_begun" and event.get("task") == task and event.get("owner_direction"):
            return contract.normalize_owner_direction(event["owner_direction"])
    # Contracts created before task_begun stored the owner's exact request as
    # their objective. This is the durable fallback for older history records.
    return contract.normalize_owner_direction(contract_value.get("objective", ""))


def _task_names_from_state(state: dict, contracts: dict) -> set[str]:
    """Collect every durable task identity without treating it as live work."""
    names = set((state.get("task_chunks") or {}).keys())
    names.update((state.get("delivery_plans") or {}).keys())
    names.update(item.get("task", "") for item in (state.get("qa_requests") or {}).values())
    names.update(item.get("task", "") for item in (state.get("qa_request_index") or {}).values())
    names.update(
        entry.get("value", {}).get("task", "")
        for entry in (state.get("archive") or [])
        if entry.get("kind") == "qa_request"
    )
    names.update((state.get("task_briefs") or {}).keys())
    names.update((state.get("releases") or {}).keys())
    names.update((state.get("release_decisions") or {}).keys())
    names.update((state.get("release_repairs") or {}).keys())
    names.update((state.get("requirement_confirmations") or {}).keys())
    names.update(value.get("task", "") for value in (state.get("agents") or {}).values())
    names.update(
        event.get("task", "")
        for event in (state.get("events") or [])
        if event.get("kind") in {
            "task_begun", "development_complete", "visual_test_required",
            "owner_release_decision_recorded", "release_repair_claimed",
        }
    )
    names.update(contracts.keys())
    cancelled = set((state.get("cancelled_tasks") or {}).keys())
    cancelled.update(
        event.get("task", "") for event in (state.get("events") or [])
        if event.get("kind") == "task_cancelled"
    )
    return {
        name for name in names
        if name and name not in {board.AWAITING_OWNER_DIRECTION, "GLOBAL_MONITOR", "REVIEW_QUEUE"} and name not in cancelled
    }


def _task_requests(state: dict, task: str) -> list[dict]:
    current = [value for value in (state.get("qa_requests") or {}).values() if value.get("task") == task]
    archived = [
        entry.get("value", {})
        for entry in (state.get("archive") or [])
        if entry.get("kind") == "qa_request" and entry.get("value", {}).get("task") == task
    ]
    current_ids = {value.get("id") for value in current}
    archived_ids = {value.get("id") for value in archived}
    indexed = [
        value for value in (state.get("qa_request_index") or {}).values()
        if value.get("task") == task and value.get("id") not in current_ids | archived_ids
    ]
    return current + archived + indexed


OWNER_DESCRIPTION_FALLBACK = "A recorded check has no plain-language summary."
OWNER_DESCRIPTION_LEGACY_HEADERS = (
    "description", "scenario", "action or induced fault", "dimension",
    "expected system response",
)
OWNER_DESCRIPTION_COMMAND = re.compile(
    r"(?:^|\s)(?:python\d*|pytest|npm|pnpm|yarn|node|curl|git|bash|zsh|sh)\b|`|--[a-z]",
    re.I,
)
OWNER_DESCRIPTION_PATH_OR_HASH = re.compile(
    r"(?:^|\s)(?:\.?\.?/|~?/|[A-Za-z]:[\\/])\S+|\b[0-9a-f]{32,64}\b",
    re.I,
)
OWNER_DESCRIPTION_ID_OR_STATE = re.compile(
    r"^(?:S-[A-Z0-9._:-]+|[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+|[a-z0-9]+(?:-[a-z0-9]+){2,})$"
)


def _safe_owner_description(value: object) -> str:
    """Return display-safe authored prose, never a technical fallback field."""
    text = " ".join(str(value or "").split())
    if not text or len(text) > contract.OWNER_DESCRIPTION_MAX_LENGTH:
        return ""
    if contract.OWNER_DESCRIPTION_CONTROL.search(str(value or "")):
        return ""
    if OWNER_DESCRIPTION_COMMAND.search(text) or OWNER_DESCRIPTION_PATH_OR_HASH.search(text):
        return ""
    if OWNER_DESCRIPTION_ID_OR_STATE.fullmatch(text):
        return ""
    words = re.findall(r"[A-Za-z][A-Za-z'-]+", text)
    if len(words) < 3 or not any(len(word) >= 5 for word in words):
        return ""
    return text


def _path_within(path: Path, roots: list[Path]) -> bool:
    try:
        resolved = path.resolve(strict=False)
    except OSError:
        return False
    for root in roots:
        try:
            resolved.relative_to(root.resolve(strict=False))
            return True
        except (OSError, ValueError):
            continue
    return False


def _task_evidence_roots(root: Path, state: dict, task: str) -> list[Path]:
    context = project_context(root)
    values = [context.code_root, context.data_root]
    workspace = (state.get("task_workspaces") or {}).get(task)
    if workspace:
        values.append(Path(str(workspace)))
    for subtask_workspace in ((state.get("subtask_workspaces") or {}).get(task) or {}).values():
        if subtask_workspace:
            values.append(Path(str(subtask_workspace)))
    return values


def _verified_scenario_outcomes(bundle: object, evidence_root: Path | None = None) -> tuple[dict[str, str], set[str]]:
    """Derive row truth from intact scenario-linked execution output only."""
    if not isinstance(bundle, dict):
        return {}, set()
    scenario_ids = [value for value in bundle.get("scenario_ids", []) if isinstance(value, str)]
    exception_ids = {
        value for value in bundle.get("approved_exception_ids", [])
        if isinstance(value, str) and value in scenario_ids
    }
    if not scenario_ids:
        return {}, set()
    evidence_value = bundle.get("certified_evidence") or bundle.get("evidence")
    expected_digest = bundle.get("certified_evidence_sha256") or bundle.get("evidence_sha256")
    if not evidence_value or not expected_digest:
        return {}, set()
    evidence_path = Path(str(evidence_value))
    if evidence_root is not None and not _path_within(evidence_path, [project_context(evidence_root).data_root]):
        return {}, set()
    try:
        payload = evidence_path.read_bytes()
    except OSError:
        return {}, set()
    if hashlib.sha256(payload).hexdigest() != str(expected_digest):
        return {}, set()
    try:
        evidence_text = payload.decode("utf-8")
    except UnicodeError:
        return {}, set()
    outcomes: dict[str, str] = {}
    current = ""
    allowed = set(scenario_ids) - exception_ids
    for line in evidence_text.splitlines():
        scenario = re.fullmatch(r"\s*scenario:\s*(\S+)\s*", line, re.I)
        if scenario:
            current = scenario.group(1) if scenario.group(1) in allowed else ""
            continue
        result = re.fullmatch(r"\s*result:\s*(PASS(?:ED)?|FAIL(?:ED)?|ERROR)\s*", line, re.I)
        if current and result:
            outcomes[current] = "passed" if result.group(1).upper().startswith("PASS") else "failed"
            current = ""
    if set(outcomes) != allowed:
        return {}, set()
    executed = len(outcomes)
    recorded_count = bundle.get("executed_count")
    if recorded_count not in {executed, sum(value == "passed" for value in outcomes.values())}:
        return {}, set()
    return outcomes, exception_ids


def _verified_delivery_scenario_ids(bundle: object) -> set[str]:
    """Backward-compatible helper used by older callers and tests."""
    outcomes, _ = _verified_scenario_outcomes(bundle)
    return {scenario_id for scenario_id, outcome in outcomes.items() if outcome == "passed"}


def _scenario_rows_for_view(path: Path) -> list[dict[str, str]]:
    """Read only safe owner prose and row identity from a ledger."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return []
    parsed = [contract._ledger_cells(line) for line in lines]
    header = next((cells for cells in parsed if cells and cells[0].casefold() == "id"), None)
    if not header:
        return []
    indexes = {cell.casefold(): index for index, cell in enumerate(header)}
    result_index = indexes.get("qa result")
    if result_index is None:
        return []
    rows = []
    for cells in parsed:
        if not cells or not contract.SCENARIO_ID.fullmatch(cells[0]):
            continue
        if result_index >= len(cells):
            continue
        description = ""
        owner_index = indexes.get(contract.OWNER_DESCRIPTION_HEADER.casefold())
        if owner_index is not None and owner_index < len(cells):
            description = _safe_owner_description(cells[owner_index])
        if not description:
            for name in OWNER_DESCRIPTION_LEGACY_HEADERS:
                index = indexes.get(name)
                if index is not None and index < len(cells):
                    description = _safe_owner_description(cells[index])
                    if description:
                        break
        description = description or OWNER_DESCRIPTION_FALLBACK
        rows.append({
            "id": cells[0],
            "what_was_tested": description,
            # Compatibility alias for older local API consumers. The browser
            # renders only what_was_tested and never exposes the row ID.
            "scenario": description,
            "recorded_result": cells[result_index],
        })
    return rows


def _review_cycle(request: dict) -> int:
    try:
        return max(0, int(request.get("cycle", 0)))
    except (TypeError, ValueError):
        return 0


def _read_verified_ledger_rows(path: Path, expected_digest: object = "") -> list[dict[str, str]]:
    """Reject changed certified/reviewed bytes while tolerating unhashed legacy rows."""
    try:
        if expected_digest and hashlib.sha256(path.read_bytes()).hexdigest() != str(expected_digest):
            return []
    except OSError:
        return []
    return _scenario_rows_for_view(path)


def _ledger_sort_key(request):
    return (
        str(request.get("requested_at") or request.get("completed_at") or ""),
        request.get("phase") == "final_acceptance",
        _review_cycle(request),
    )


def _ledger_rows_for_request(root: Path, state: dict, task: str, request: dict, kind: str) -> tuple[list[dict[str, str]], str, bool]:
    """Read one exact request ledger and report whether its bytes are certified."""
    artifact_name = "challenge_ledger" if kind == "reviewer" else "delivery_ledger"
    field_name = "challenge_ledger" if kind == "reviewer" else "ledger"
    digest_name = "challenge_ledger_sha256" if kind == "reviewer" else "ledger_sha256"
    certified = request.get("certified_artifacts", {}).get(artifact_name, {})
    certified_required = request.get("status") == "passed"
    candidates = [(Path(str(certified.get("path", ""))), certified.get("sha256"), True)] if certified.get("path") else []
    ledger_value = str(request.get(field_name, ""))
    if ledger_value:
        candidates.append((board._task_path_from_state(root, state, task, ledger_value), request.get(digest_name), False))
    allowed_roots = _task_evidence_roots(root, state, task)
    for candidate, expected_digest, is_certified in candidates:
        if not _path_within(candidate, allowed_roots):
            continue
        if candidate.is_file():
            rows = _read_verified_ledger_rows(candidate, expected_digest)
            if rows:
                actual = hashlib.sha256(candidate.read_bytes()).hexdigest()
                intact = bool(expected_digest and actual == str(expected_digest))
                return rows, actual, intact and (is_certified or not certified_required)
    # Never invoke Git (or any process) from a status/history projection. New
    # requests have immutable certified copies; legacy rows with missing bytes
    # fail closed instead of reconstructing a checkout during a refresh.
    return [], "", False


def _checklist_section(rows: list[dict[str, str]], bundle: object, *, evidence_allowed: bool = True, root: Path | None = None) -> dict:
    outcomes, exception_ids = _verified_scenario_outcomes(bundle, root) if evidence_allowed else ({}, set())
    scenarios: list[dict[str, str]] = []
    for row in rows:
        outcome = outcomes.get(row["id"])
        if outcome == "passed":
            status, label = "passed", "Passed"
        elif outcome == "failed":
            status, label = "failed", "Needs attention"
        elif row["id"] in exception_ids:
            status, label = "exception", "Not required for this change"
        else:
            status, label = "pending", "Not tested yet"
        scenarios.append({
            "id": row["id"],
            "what_was_tested": row["what_was_tested"],
            "scenario": row["what_was_tested"],
            "status": status,
            "label": label,
        })
    return {"state": "available" if rows else "absent", "scenarios": scenarios}


def _request_ledger_view(root: Path, state: dict, task: str, request: dict):
    """Project both independently authored ledgers for one exact request."""
    delivery_rows, delivery_digest, delivery_intact = _ledger_rows_for_request(
        root, state, task, request, "delivery",
    )
    delivery_bundle = request.get("delivery_simulations") or request.get("qa_simulations")
    delivery = _checklist_section(
        delivery_rows,
        delivery_bundle,
        evidence_allowed=delivery_intact or not request.get("ledger_sha256"),
        root=root,
    )
    if request.get("ledger") and not delivery_rows:
        delivery.update({
            "state": "unavailable",
            "message": "The recorded checks are not available, so none are shown as passed.",
        })
    challenge_value = str(request.get("challenge_ledger") or "")
    reviewer_rows, challenge_digest, challenge_intact = _ledger_rows_for_request(
        root, state, task, request, "reviewer",
    ) if challenge_value else ([], "", False)
    execution = request.get("challenge_execution") or {}
    reviewer_bundle = (
        request.get("reviewer_simulations")
        if request.get("status") == "passed"
        else request.get("reviewer_simulations") or execution.get("bundle")
    )
    execution_digest = str(execution.get("ledger_sha256") or request.get("challenge_ledger_sha256") or "")
    reviewer_allowed = bool(
        reviewer_rows and challenge_intact and execution_digest
        and execution_digest == challenge_digest
    )
    reviewer = _checklist_section(reviewer_rows, reviewer_bundle, evidence_allowed=reviewer_allowed, root=root)
    if not challenge_value:
        reviewer.update({"state": "preparing", "message": "The reviewer is preparing independent checks."})
    elif not reviewer_rows:
        reviewer.update({"state": "unavailable", "message": "The recorded checks are not available, so none are shown as passed."})

    phase = str(request.get("phase", "")).replace("_", " ").strip().title() or "Delivery review"
    verdict = str(request.get("status", "")).replace("_", " ").strip().title()
    attempt_status = (
        "This attempt found a problem and was followed by a repair."
        if request.get("status") == "failed"
        else "This attempt was accepted."
        if request.get("status") == "passed"
        else "This attempt is still in progress."
    )
    return {
        "request_id": request.get("id", ""),
        "source": f"{phase} · cycle {_review_cycle(request) or 1}" + (f" · {verdict}" if verdict else ""),
        "requested_at": request.get("requested_at", ""),
        "attempt_status": attempt_status,
        "state": delivery["state"],
        "scenarios": delivery["scenarios"],
        "delivery": delivery,
        "reviewer": reviewer,
    }


def _task_test_ledger(root: Path, state: dict, task: str) -> dict:
    """Project the newest readable Delivery ledger without mutating review state."""
    requests = [request for request in _task_requests(state, task) if request.get("ledger")]
    requests.sort(key=_ledger_sort_key, reverse=True)
    for request in requests:
        return _request_ledger_view(root, state, task, request)
    if requests:
        latest = requests[0]
        phase = str(latest.get("phase", "")).replace("_", " ").strip().title() or "Delivery review"
        newer_exists = any(_review_cycle(value) > _review_cycle(latest) for value in requests)
        return {
            "request_id": latest.get("id", ""),
            "source": f"{phase} · cycle {_review_cycle(latest) or 1}",
            "state": "superseded" if latest.get("status") == "failed" and newer_exists else "unreadable",
            "scenarios": [],
        }
    return {"request_id": "", "source": "", "state": "absent", "scenarios": []}


def _live_task_test_ledger(root: Path, state: dict, task: str) -> dict:
    """Read a bounded newest request set during ordinary dashboard refresh."""
    candidates = [
        value for value in (state.get("qa_requests") or {}).values()
        if value.get("task") == task and value.get("ledger")
    ]
    current_ids = {value.get("id") for value in candidates}
    candidates.extend(
        value for value in (state.get("qa_request_index") or {}).values()
        if value.get("task") == task and value.get("ledger") and value.get("id") not in current_ids
    )
    candidates.sort(key=_ledger_sort_key, reverse=True)
    if candidates:
        return _request_ledger_view(root, state, task, candidates[0])
    return {"request_id": "", "source": "", "state": "absent", "scenarios": []}


def _task_test_ledgers(root: Path, state: dict, task: str) -> list:
    """EVERY readable Delivery ledger for the task, oldest first.

    History must preserve the whole record - each subtask review and every
    final-acceptance cycle, including failed ones - not only the newest ledger.
    """
    requests = [
        request for request in _task_requests(state, task)
        if request.get("ledger") or request.get("challenge_ledger")
    ]
    requests.sort(key=_ledger_sort_key)
    views = []
    for request in requests:
        views.append(_request_ledger_view(root, state, task, request))
    for index, view in enumerate(views):
        view["attempt_label"] = (
            "First recorded attempt" if index == 0
            else "Latest recorded attempt" if index == len(views) - 1
            else "Earlier recorded attempt"
        )
    return views


def _reviewer_request_for_agent(state: dict, agent: dict) -> dict | None:
    eligible = [
        request for request in (state.get("qa_requests") or {}).values()
        if (
            request.get("status") in {"claimed", "suspended"}
            and request.get("claimed_by") == agent.get("id")
        ) or (
            request.get("status") == "reserved"
            and request.get("reserved_by") == agent.get("id")
        ) or (
            request.get("status") in {"authoring", "open"}
            and request.get("routed_to") == agent.get("id")
        )
    ]
    return sorted(eligible, key=_ledger_sort_key)[-1] if eligible else None


def _agent_checklists(root: Path, state: dict, agents: dict) -> dict[str, dict]:
    """Bound live checklists to authoritative agent/request ownership."""
    result: dict[str, dict] = {}
    for agent_id, agent in agents.items():
        if agent.get("role") == "qa":
            request = _reviewer_request_for_agent(state, agent)
            if request is None:
                result[agent_id] = {"role": "reviewer", "state": "idle", "message": "No review is currently assigned."}
                continue
            view = _request_ledger_view(root, state, str(request.get("task", "")), request)
            result[agent_id] = {"role": "reviewer", "state": "assigned", "section": view["reviewer"]}
        elif agent.get("role") in board.DEVELOPER_ROLES:
            requests = [
                request for request in (state.get("qa_requests") or {}).values()
                if request.get("task") == agent.get("task")
                and request.get("developer_id") == agent_id
                and request.get("ledger")
            ]
            request = sorted(requests, key=_ledger_sort_key)[-1] if requests else None
            section = (
                _request_ledger_view(root, state, str(agent.get("task", "")), request)["delivery"]
                if request else {"state": "absent", "scenarios": []}
            )
            result[agent_id] = {"role": "delivery", "state": "assigned", "section": section}
    return result
def _live_task_names(state: dict, contracts: dict) -> list[str]:
    """Return only work that still belongs in the live Delivery progress area.

    Completed tasks remain in the durable history projection. A release awaiting
    the owner's response or a routed repair is intentionally still live because
    hiding it would hide an action the owner or Delivery must take.
    """
    names = _task_names_from_state(state, contracts)
    live: set[str] = set()
    accepted = {
        str(task) for task, decision in (state.get("release_decisions") or {}).items()
        if decision.get("decision") == "accepted"
    }
    for agent in (state.get("agents") or {}).values():
        if (agent.get("active") or agent.get("status") == "paused") and agent.get("role") in board.DEVELOPER_ROLES and agent.get("task") in names and agent.get("task") not in accepted:
            live.add(agent["task"])
    for request in (state.get("qa_requests") or {}).values():
        if request.get("status") in {"authoring", "open", "reserved", "claimed", "suspended"} and request.get("task") in names and request.get("task") not in accepted:
            live.add(request["task"])
    for task in names:
        if task in accepted:
            continue
        latest_by_scope: dict[tuple[str, str, str, int], dict] = {}
        for request in _task_requests(state, task):
            key = (
                str(request.get("phase") or "legacy"),
                str(request.get("subtask") or ""), str(request.get("chunk") or ""),
                int(request.get("structure_revision", 0)),
            )
            previous = latest_by_scope.get(key)
            if previous is None or int(request.get("cycle", 0)) > int(previous.get("cycle", 0)):
                latest_by_scope[key] = request
        if any(request.get("status") == "failed" for request in latest_by_scope.values()):
            live.add(task)
        release = (state.get("releases") or {}).get(task)
        final_passed = any(
            request.get("phase") == "final_acceptance" and request.get("status") == "passed"
            for request in _task_requests(state, task)
        )
        if final_passed and not release:
            live.add(task)
    for task, release in (state.get("releases") or {}).items():
        if release.get("status") == "VISUAL_TEST_REQUIRED" and not (state.get("release_decisions") or {}).get(task):
            live.add(task)
    for task, repair in (state.get("release_repairs") or {}).items():
        if repair.get("status") in {"OWNER_REJECTED_REPAIR_REQUIRED", "DELIVERY_REPAIR_IN_PROGRESS"}:
            live.add(task)
    live.update(
        str(finding.get("task") or "")
        for finding in (state.get("deferred_findings") or {}).values()
        if finding.get("status") == "in_scope"
        and str(finding.get("task") or "") in names
        and str(finding.get("task") or "") not in accepted
    )
    return sorted(live)


def _task_history(state: dict, contracts: dict, live_tasks: list[str], test_ledgers: dict[str, dict] | None = None, test_ledgers_all: dict[str, list] | None = None) -> list[dict]:
    """Build an unbounded, restart-safe history view from persisted board data."""
    live = set(live_tasks)
    history: list[dict] = []
    relevant_events = {
        "task_begun", "development_complete", "visual_test_required",
        "owner_release_decision_recorded", "release_repair_claimed", "agent_offline",
    }
    for task in _task_names_from_state(state, contracts) - live:
        events = [
            event for event in (state.get("events") or [])
            if event.get("task") == task and event.get("kind") in relevant_events
        ]
        contract_value = contracts.get(task, {})
        started_at = next((event.get("at", "") for event in events if event.get("kind") == "task_begun"), "")
        if not started_at:
            started_at = contract_value.get("created_at", "")
        release = (state.get("releases") or {}).get(task, {})
        decision = (state.get("release_decisions") or {}).get(task, {})
        final_reviews = [
            request for request in _task_requests(state, task)
            if request.get("phase") == "final_acceptance" and request.get("status") == "passed"
        ]
        failed_reviews = [request for request in _task_requests(state, task) if request.get("status") == "failed"]
        agent_statuses = [agent.get("status") for agent in (state.get("agents") or {}).values() if agent.get("task") == task]
        if decision.get("decision") == "accepted":
            result = "OWNER ACCEPTED"
        elif decision.get("decision") == "not_accepted":
            result = "OWNER REJECTED / REPAIR REQUIRED"
        elif release.get("status") == "VISUAL_TEST_REQUIRED":
            result = "READY FOR YOUR TEST"
        elif final_reviews:
            result = "FINAL REVIEW PASSED"
        elif "done" in agent_statuses:
            result = "COMPLETED"
        elif failed_reviews:
            result = "REVIEW FAILED / REPAIR REQUIRED"
        else:
            result = "TASK HISTORY"
        completion_events = [event for event in events if event.get("kind") != "task_begun"]
        completed_at = completion_events[-1].get("at", "") if completion_events else ""
        completed_at = max(completed_at, release.get("recorded_at", ""), decision.get("recorded_at", ""), contract_value.get("updated_at", ""))
        chunks = (state.get("task_chunks") or {}).get(task, {})
        plan = (state.get("delivery_plans") or {}).get(task, {})
        subtasks = plan.get("subtasks", {})
        requests = _task_requests(state, task)
        history.append({
            "task": task,
            "started_at": started_at,
            "completed_at": completed_at or started_at,
            "result": result,
            "chunks_passed": sum(value.get("status") == "passed" for value in chunks.values()),
            "chunks_total": len(chunks),
            "delivery_mode": plan.get("mode") or ("chunked" if chunks else "atomic"),
            "subtasks_passed": sum(value.get("status") == "passed" for value in subtasks.values()),
            "subtasks_total": len(subtasks),
            "review_passes": sum(request.get("status") == "passed" for request in requests),
            "release_status": release.get("status", ""),
            "owner_decision": decision.get("decision", ""),
            "requirements_confirmation": (state.get("requirement_confirmations") or {}).get(task, {}),
            "owner_clarifications": (state.get("owner_clarifications") or {}).get(task, []),
            "owner_direction": _owner_direction_for_history(state, task, contract_value),
            "test_ledger": (test_ledgers or {}).get(task, {"request_id": "", "source": "", "state": "absent", "scenarios": []}),
            "test_ledgers": (test_ledgers_all or {}).get(task, []),
        })
    return sorted(history, key=lambda value: (value.get("completed_at", ""), value.get("started_at", ""), value.get("task", "")), reverse=True)


# A freshly launched terminal needs time to register its agent on the board;
# beyond this window an agentless session is an orphan (its task concluded and
# its agent record was archived), not a terminal mid-registration.
ORPHAN_SESSION_GRACE_SECONDS = 900


def _session_age_seconds(session: dict) -> float:
    try:
        created = datetime.fromisoformat(str(session.get("created_at", "")))
    except ValueError:
        return 0.0
    now_value = datetime.now(created.tzinfo) if created.tzinfo else datetime.now()
    return max(0.0, (now_value - created).total_seconds())


def retire_orphan_sessions(root: Path, grace_seconds: float | None = None) -> list[str]:
    """Stop active sessions no board agent references, after the launch grace.

    A finished task's terminal used to sit open forever ("waiting to be
    stopped") once its agent record was archived — and the follow-up
    dispatcher, seeing an active session it did not recognize, waited
    indefinitely for a registration that could never come. The whole approved
    follow-up queue deadlocked behind one ghost terminal (observed live
    2026-08-15 on the governing harness). The controller retires such orphans
    on every poll.
    """
    grace = ORPHAN_SESSION_GRACE_SECONDS if grace_seconds is None else grace_seconds
    state = board.snapshot(root)
    known = {agent.get("session_id") for agent in (state.get("agents") or {}).values()}
    stopped: list[str] = []
    for session in control.snapshot(root)["sessions"]:
        if session.get("status") not in control.ACTIVE_STATUSES:
            continue
        if session.get("id") in known:
            continue
        if _session_age_seconds(session) <= grace:
            continue
        try:
            control.stop(root, session["id"])
        except (ValueError, OSError):
            continue
        stopped.append(session["id"])
    if stopped:
        with board.locked_state(root) as state:
            board._event(state, "orphan_sessions_stopped", None, {
                "sessions": stopped,
                "message": "controller stopped finished terminals no agent references; queued follow-ups can dispatch",
            })
    return stopped


CTO_ROTATION_POLLS = 120


def rotate_reviewer_sessions(root: Path) -> list[str]:
    """Compatibility no-op: board compaction preserves reviewer continuity."""
    return []


def ensure_reviewer_available(root: Path) -> dict[str, object]:
    """Record that the review queue needs a reviewer. NEVER auto-spawns.

    An earlier version launched a terminal from this controller helper. When
    called from dashboard refresh, any registration delay became a terminal-
    spawn loop that overwhelmed the owner's machine on 2026-08-16. Spawning
    belongs to an explicit, rate-limited, owner-visible action.
    """
    state = board.snapshot(root)
    open_reviews = [r for r in (state.get("qa_requests") or {}).values()
                    if r.get("status") in {"authoring", "open"}]
    if not open_reviews:
        return {"status": "no_open_reviews"}
    managed = control.snapshot(root)
    sessions = {s["id"]: s for s in managed["sessions"]
                if s.get("status") in control.ACTIVE_STATUSES}
    live_reviewer = any(
        agent.get("active") and agent.get("role") == "qa"
        and agent.get("session_id") in sessions
        for agent in (state.get("agents") or {}).values())
    if live_reviewer:
        return {"status": "reviewer_present"}
    if any(s.get("kind") == "claude_reviewer" for s in sessions.values()):
        return {"status": "reviewer_registering"}
    # Signal only. The board records the need ONCE (no event spam, no
    # spawning); the owner or an explicit controller action starts the
    # terminal.
    with board.locked_state(root) as mutable:
        if not mutable.get("reviewer_needed"):
            mutable["reviewer_needed"] = {
                "requested_at": board.now(),
                "request_id": open_reviews[0].get("id", ""),
            }
            board._event(mutable, "reviewer_needed", None, {
                "task": open_reviews[0].get("task", ""),
                "request_id": open_reviews[0].get("id", ""),
                "message": "open review queue has no live reviewer; start one from Mission Control",
            })
    return {"status": "reviewer_needed"}


_SPAWN_BUDGET_WINDOW_SECONDS = 900
_SPAWN_BUDGET_MAX = 2


def _spawn_budget_exhausted(root: Path) -> bool:
    """Hard cap on controller-initiated terminal launches per window.

    A backstop independent of any single call site's logic: whatever a
    controller path believes it needs, it cannot exceed this budget.
    """
    state = board.snapshot(root)
    recent = 0
    for event in reversed(state.get("events", [])):
        if event.get("kind") not in {"cto_rotated", "reviewer_spawned", "terminal_launched"}:
            continue
        age = _session_age_seconds({"created_at": event.get("at", "")})
        if age > _SPAWN_BUDGET_WINDOW_SECONDS:
            break
        recent += 1
    return recent >= _SPAWN_BUDGET_MAX


def rotate_cto_session(root: Path) -> dict[str, object]:
    """Compatibility no-op: the CTO is one long-lived global monitor."""
    return {"status": "preserved", "strategy": "board_context_compaction"}


def dispatch_approved_findings(
    root: Path, settings_home: Path | None = None,
) -> dict[str, object]:
    """Start at most one owner-approved follow-up when the project is idle."""
    state = board.snapshot(root)
    findings = list((state.get("deferred_findings") or {}).values())
    if any(finding.get("status") == "fix_in_progress" for finding in findings):
        return {"status": "in_progress"}
    queued = [finding for finding in findings if finding.get("status") == "fix_requested"]
    if not queued:
        return {"status": "empty"}
    contracts: dict[str, dict] = {}
    for path in project_context(root).storage_path("tasks").glob("*.json"):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            if value.get("task"):
                contracts[value["task"]] = value
        except (OSError, json.JSONDecodeError):
            continue
    if _live_task_names(state, contracts):
        return {"status": "waiting_for_current_work"}

    managed = control.snapshot(root)
    sessions = {
        session["id"]: session for session in managed["sessions"]
        if session.get("status") in control.ACTIVE_STATUSES
    }
    waiting = sorted(
        (
            agent for agent in state.get("agents", {}).values()
            if agent.get("active")
            and agent.get("role") in board.DEVELOPER_ROLES
            and agent.get("task") == board.AWAITING_OWNER_DIRECTION
            and sessions.get(agent.get("session_id"))
        ),
        key=lambda agent: (agent.get("spawned_at", ""), agent.get("id", "")),
    )
    if waiting:
        try:
            return {"status": "dispatched", **board.dispatch_approved_finding(root, waiting[0]["id"])}
        except ValueError as error:
            return {"status": "waiting", "reason": str(error)}

    known_session_ids = {agent.get("session_id") for agent in state.get("agents", {}).values()}
    # Only a RECENTLY launched unknown session means a terminal is still
    # registering. An old agentless session is an orphan (retired by
    # retire_orphan_sessions) and must never deadlock the queue.
    if any(
        session_id not in known_session_ids
        and _session_age_seconds(session) <= ORPHAN_SESSION_GRACE_SECONDS
        for session_id, session in sessions.items()
    ):
        return {"status": "terminal_registering"}
    if int(managed.get("active_counts", {}).get("codex_delivery", 0)) >= int(managed.get("limits", {}).get("codex_delivery", 2)):
        return {"status": "waiting_for_delivery_capacity"}
    try:
        settings_override = (
            global_settings.load(settings_home)["agent_settings"]
            if settings_home else None
        )
        session = control.create(
            root, "codex_delivery", settings_override=settings_override,
        )
        launch_terminal(root, session)
        return {"status": "terminal_started", "session_id": session["id"]}
    except Exception as error:
        if "session" in locals():
            control.fail_launch(root, session["id"], f"unable to open Terminal: {error}")
        return {"status": "launch_failed", "reason": str(error)}


def dashboard_payload(
    root: Path, settings_home: Path | None = None, *, expose_path: bool = True,
) -> dict:
    """Build a read-only dashboard projection.

    A browser refresh is never a controller clock. Material board events,
    explicit owner actions, startup recovery, and the watchdog own lifecycle
    work; GET requests must not launch, stop, route, execute, or persist.
    """
    paused = board.pause_state(root).get("status") in {"paused", "resuming"}
    data = payload(root)
    sessions_by_id = {item["id"]: item for item in control.snapshot(root)["sessions"]}
    for agent_id, agent in list(data["state"].get("agents", {}).items()):
        session = sessions_by_id.get(agent.get("session_id"))
        if (
            not paused and agent.get("active") and agent.get("session_id")
            and (not session or session.get("status") not in control.ACTIVE_STATUSES)
        ):
            # Reconcile only the returned copy. The watchdog owns the durable
            # offline transition; a GET must not mutate board state.
            data["state"]["agents"].pop(agent_id, None)
            continue
        if not session or not session.get("last_output_at"):
            continue
        agent["recent_output_at"] = session["last_output_at"]
        agent["output_bytes"] = session.get("output_bytes", 0)
    contracts = {}
    for path in project_context(root).storage_path("tasks").glob("*.json"):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            if value.get("task"):
                contracts[value["task"]] = value
        except (OSError, json.JSONDecodeError):
            continue
    live_tasks = _live_task_names(data["state"], contracts)
    live_contracts = {task: value for task, value in contracts.items() if task in live_tasks}
    all_findings = board.list_findings(root)
    in_scope_findings = [finding for finding in all_findings if finding.get("status") == "in_scope"]
    test_ledgers = {task: _live_task_test_ledger(root, data["state"], task) for task in live_tasks}
    visible_agents = {
        key: value for key, value in (data["state"].get("agents") or {}).items()
        if value.get("status") == "paused" or value.get("active") or value.get("session_id") in sessions_by_id
    }
    agent_checklists = _agent_checklists(root, data["state"], visible_agents)
    result = {
        **data,
        "viewer_version": viewer_version(),
        **({"path": str(project_context(root).code_root)} if expose_path else {}),
        "contracts": live_contracts,
        "owner_direction": latest_owner_direction(data["state"]) if live_tasks or any(agent.get("active") for agent in data["state"].get("agents", {}).values()) else "",
        "owner_directions": owner_directions_by_task(data["state"]),
        "requirement_confirmations": {task: value for task, value in requirement_confirmations_by_task(data["state"]).items() if task in live_tasks},
        "owner_clarifications": {task: value for task, value in data["state"].get("owner_clarifications", {}).items() if task in live_tasks},
        "owner_messages": [value for value in data["state"].get("owner_messages", []) if value.get("task") in live_tasks],
        "live_tasks": live_tasks,
        "test_ledgers": test_ledgers,
        "agent_checklists": agent_checklists,
        "in_scope_findings": in_scope_findings,
        # Cheap change signature for the history panel: an OPEN panel must
        # refresh when a task concludes, not serve yesterday's groups forever
        # (owner report 2026-08-16).
        "history_version": f"{len(data['state'].get('release_decisions', {}))+len(data['state'].get('cancelled_tasks', {}))}:"
                           + max([value.get("recorded_at", "") for value in data["state"].get("release_decisions", {}).values()]
                                 + [value.get("cancelled_at", "") for value in data["state"].get("cancelled_tasks", {}).values()]
                                 + [""]),
    }
    live_session_ids = {
        session_id for session_id, session in sessions_by_id.items()
        if session.get("status") in control.ACTIVE_STATUSES
    }
    result["state"] = _compact_dashboard_state(data["state"], live_tasks, live_session_ids)
    return result


def history_payload(root: Path) -> dict:
    """Load complete history only when the owner opens or searches History."""
    state = board.historical_snapshot(root)
    contracts = {}
    for path in project_context(root).storage_path("tasks").glob("*.json"):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            if value.get("task"):
                contracts[value["task"]] = value
        except (OSError, json.JSONDecodeError):
            continue
    live_tasks = _live_task_names(state, contracts)
    historical_tasks = _task_names_from_state(state, contracts) - set(live_tasks)
    test_ledgers = {task: _task_test_ledger(root, state, task) for task in historical_tasks}
    test_ledgers_all = {task: _task_test_ledgers(root, state, task) for task in historical_tasks}
    return {
        "updated": board.now(),
        "task_history": _task_history(state, contracts, live_tasks, test_ledgers, test_ledgers_all=test_ledgers_all),
    }


def settings_payload(root: Path, settings_home: Path | None = None) -> dict:
    """Return one stable settings document for both settings panels."""
    value = workspace_settings.load(root)
    global_value = global_settings.load(settings_home) if settings_home else None
    agent_value = global_value["agent_settings"] if global_value else control.agent_settings(root)
    value["settings"] = agent_value
    value["agent_settings"] = agent_value
    value["providers"] = control.PROVIDERS
    value["efforts"] = control.EFFORTS
    value["provider_efforts"] = control.PROVIDER_EFFORTS
    value["provider_models"] = available_provider_models()
    if global_value:
        value["connectivity"] = global_value.get("connectivity", {})
        value["settings_scope"] = "global"
    return value


def available_provider_models() -> dict[str, list[str]]:
    """Discover local model choices and retain safe aliases as fallbacks."""
    choices = {provider: list(models) for provider, models in control.PROVIDER_MODELS.items()}
    codex_cache = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))) / "models_cache.json"
    try:
        cached = json.loads(codex_cache.read_text(encoding="utf-8"))
        discovered = [str(item.get("slug", "")).strip() for item in cached.get("models", []) if item.get("slug")]
        if discovered:
            choices["codex"] = list(dict.fromkeys(discovered + choices["codex"]))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        pass
    claude_settings = Path.home() / ".claude" / "settings.json"
    try:
        configured = str(json.loads(claude_settings.read_text(encoding="utf-8")).get("model", "")).strip()
        if configured:
            choices["claude"] = list(dict.fromkeys([configured] + choices["claude"]))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        pass
    return choices


def test_provider_connection(root: Path, provider: str, effort: str, model: str = "") -> dict[str, str]:
    """Dry-run a provider CLI with its selected model and effort flags.

    This deliberately invokes the parser/help path only: it proves the binary
    is available and accepts the selected flags without spending tokens or
    opening an interactive agent session.
    """
    provider = str(provider).strip().lower()
    if provider not in control.PROVIDERS:
        raise ValueError("choose Codex or Claude before testing the connection")
    model = control.normalize_provider_model(provider, model)
    effort = control.normalize_provider_effort(provider, effort)
    if effort not in control.PROVIDER_EFFORTS[provider]:
        choices = ", ".join(control.PROVIDER_EFFORTS[provider])
        raise ValueError(f"{provider.title()} does not support '{effort}'. Choose {choices}.")
    configured = os.environ.get(control.PROVIDERS[provider]["binary_env"], provider)
    executable = configured if os.path.isabs(configured) else shutil.which(configured)
    if not executable:
        raise ValueError(f"{provider.title()} CLI was not found ({configured})")
    workspace = project_context(root).code_root
    if not workspace.is_dir():
        raise ValueError("the registered project folder is unavailable")
    command = ([executable, "--model", model, "-c", f"model_reasoning_effort={effort}", "--help"]
               if provider == "codex" else [executable, "--model", model, "--effort", effort, "--help"])
    try:
        result = subprocess.run(
            command, cwd=str(workspace), capture_output=True, text=True, timeout=10,
            env=child_process.environment(git=True, shell=True),
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ValueError(f"{provider.title()} CLI could not be started: {error}") from error
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "the CLI rejected its startup options").strip().splitlines()[-1]
        raise ValueError(f"{provider.title()} CLI rejected model {model} or effort {effort}: {detail}")
    return {
        "provider": provider,
        "model": model,
        "effort": effort,
        "message": f"{provider.title()} is installed and accepts the launch flags for model {model} with effort {effort}. No task was launched or billed.",
    }


def launch_terminal(root: Path, session: dict) -> None:
    """Open exactly one visible macOS Terminal session for a hard-coded agent role."""
    if sys.platform != "darwin":
        raise RuntimeError("central CLI launch currently requires macOS Terminal")
    runner = Path(__file__).resolve().parents[1] / "scripts" / "run_managed_agent.sh"
    arguments = [
        "/usr/bin/env", "-u", "BASH_ENV", "-u", "ENV",
        "/bin/bash", "--noprofile", "--norc", str(runner),
        *context_cli_arguments(root),
        "--python", sys.executable,
        "--session-id", session["id"],
        "--kind", session["kind"],
        "--close-terminal-on-exit",
    ]
    if session["task"]:
        arguments += ["--task", session["task"]]
    command = "exec " + shlex.join(arguments)
    color = control.SESSION_COLORS.get(session.get("color", "black"), control.SESSION_COLORS["black"])
    rgb = "{" + ", ".join(str(round(channel * 65535 / 255)) for channel in color["rgb"]) + "}"
    applescript = f'''on run argv
 tell application "Terminal"
 activate
 set newTab to do script (item 1 of argv)
 tell newTab
  set background color to {rgb}
  set normal text color to {{65535, 65535, 65535}}
 end tell
 end tell
end run'''
    subprocess.run(["/usr/bin/osascript", "-e", applescript, command], check=True, capture_output=True, text=True)


def parse_release_multipart(raw: bytes, content_type: str) -> tuple[dict[str, str], list[dict[str, object]]]:
    """Parse only the fields needed by the rejection form; files stay bytes."""
    if "\r" in content_type or "\n" in content_type:
        raise ValueError("invalid attachment form")
    envelope = (
        f"Content-Type: {content_type}\r\n"
        "MIME-Version: 1.0\r\n"
        "\r\n"
    ).encode("utf-8") + raw
    message = BytesParser(policy=email_default).parsebytes(envelope)
    if not message.is_multipart():
        raise ValueError("invalid attachment form")
    fields: dict[str, str] = {}
    attachments: list[dict[str, object]] = []
    for part in message.iter_parts():
        disposition = part.get("Content-Disposition", "")
        name = part.get_param("name", header="content-disposition")
        if not name:
            continue
        filename = part.get_filename()
        data = part.get_payload(decode=True) or b""
        if filename is not None:
            attachments.append({
                "filename": filename,
                "content_type": part.get_content_type(),
                "data": data,
            })
        elif name in {
            "decision", "reason", "text", "message_type",
            "directive_source", "directive_filename",
        }:
            try:
                fields[name] = data.decode("utf-8", errors="strict")
            except UnicodeDecodeError as error:
                raise ValueError("form text must be valid UTF-8") from error
    return fields, attachments


def normalize_owner_message_text(text: str) -> tuple[str, int]:
    """Normalize line endings and boundary whitespace before durable intake."""
    normalized = str(text).replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized:
        raise ValueError("a direction or clarification is required")
    if contract.normalize_owner_direction(normalized) != normalized:
        raise ValueError("the message contains unsupported terminal control characters")
    size = len(normalized.encode("utf-8"))
    if size > OWNER_MESSAGE_MAX_BYTES:
        raise ValueError(
            f"the message is {size} UTF-8 bytes; the limit is {OWNER_MESSAGE_MAX_BYTES}"
        )
    return normalized, size


def owner_message_input(fields: dict[str, str]) -> tuple[str, dict[str, object]]:
    text, size = normalize_owner_message_text(fields.get("text", ""))
    source = str(fields.get("directive_source", "text") or "text").strip().lower()
    filename = str(fields.get("directive_filename", "") or "")
    if source not in {"text", "file"}:
        raise ValueError("message source must be text or file")
    if source == "file":
        if (
            not filename or Path(filename).suffix.lower() not in OWNER_DIRECTIVE_EXTENSIONS
            or Path(filename).name != filename or "\\" in filename
        ):
            raise ValueError("directive files must be a single .md or .txt filename")
    elif filename:
        raise ValueError("a text message cannot claim a directive filename")
    return text, {
        "source": source,
        "filename": filename if source == "file" else "",
        "normalized_bytes": size,
        "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "newline_normalization": "CRLF and CR become LF; leading and trailing whitespace is removed",
    }


def make_handler(root: Path, project_name: str = "", project_description: str = "", manager_url: str = "",
                 settings_home: Path | None = None, ready_token: str = "", project_id: str = "",
                 chat_action_token: str = "", runtime: dict | None = None,
                 api_prefix: str = "", worker_health=None):
    runtime = runtime or runtime_identity.PROCESS
    class Handler(BaseHTTPRequestHandler):
        def send_json(self, status: int, value: dict):
            body = json.dumps(value).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = urlparse(self.path).path
            if path == "/api/ready":
                ready = {
                    "ready": True,
                    "ready_token": ready_token,
                    "project_name": project_name,
                    "project_ref": board_surface.project_id(root),
                    "runtime": runtime_identity.public(runtime),
                    "surface": {"project_chat": bool(project_id and chat_action_token)},
                }
                if worker_health is not None:
                    ready["watchdog"] = worker_health()
                self.send_json(200, ready); return
            if path == "/favicon.png":
                icon = Path(__file__).resolve().parent / "assets" / "nomorehappypath.png"
                try:
                    body = icon.read_bytes()
                except OSError:
                    body = b"not found\n"
                    self.send_response(404)
                    self.send_header("Content-Type", "text/plain; charset=utf-8")
                else:
                    self.send_response(200)
                    self.send_header("Content-Type", "image/png")
                    self.send_header("Cache-Control", "public, max-age=86400")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if path == "/api/board":
                self.send_json(200, payload(root)); return
            if path == "/api/dashboard":
                # A managed Projects worker is already identified by its
                # worker-derived project context. Its browser has no need for
                # a trusted filesystem path, so do not put one on the wire.
                self.send_json(
                    200, dashboard_payload(root, settings_home, expose_path=not bool(project_id)),
                ); return
            if path == "/api/history":
                self.send_json(200, history_payload(root)); return
            if path.startswith("/api/metrics/"):
                task = path.split("/api/metrics/", 1)[1]
                self.send_json(200, {
                    "metrics": lifecycle_metrics.task_metrics(root, task),
                    "duplicates": lifecycle_metrics.duplicate_execution_count(root, task),
                }); return
            if path == "/api/repairs":
                self.send_json(200, {"repairs": board.release_repairs_for_delivery(root)}); return
            if path == "/api/findings":
                self.send_json(200, {"findings": board.list_findings(root)}); return
            if path == "/api/control":
                value = control.snapshot(root)
                value["project_chat"] = global_settings.chat_availability(settings_home)
                if settings_home:
                    value["agent_settings"] = global_settings.load(settings_home)["agent_settings"]
                self.send_json(200, value); return
            if path == "/api/settings":
                try:
                    self.send_json(200, settings_payload(root, settings_home))
                except ValueError as error:
                    self.send_json(400, {"error": str(error)})
                return
            if path == "/":
                body = rendered_page(
                    project_name, project_description, manager_url, project_id, ready_token,
                    chat_action_token, runtime, api_prefix,
                    global_settings.chat_availability(settings_home),
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
            else:
                body = b"not found\n"
                self.send_response(404)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            path = urlparse(self.path).path
            try:
                open_app_suffix = "/open-app"
                if path.startswith("/api/releases/") and path.endswith(open_app_suffix):
                    task = unquote(path[len("/api/releases/"):-len(open_app_suffix)])
                    from harness import release_preview as _release_preview
                    result = _release_preview.open_app_bundle(root, task)
                    self.send_json(200, result); return
                if path == "/api/settings/preview":
                    length = int(self.headers.get("Content-Length", "0"))
                    if length > 8 * 1024:
                        raise ValueError("preview settings are too large")
                    data = json.loads(self.rfile.read(length) or b"{}")
                    section = dict(workspace_settings.load(root).get("preview") or {})
                    for key in ("command", "url_template", "startup_timeout_seconds"):
                        if key in data:
                            section[key] = data[key]
                    preview = workspace_settings.update_preview(root, section)
                    self.send_json(200, {"preview": preview}); return
                if board.pause_state(root).get("status") in {"paused", "resuming"}:
                    self.send_json(409, {
                        "error": "This project is paused and read-only. Resume it from Projects before making changes."
                    }); return
                release_prefix = "/api/releases/"
                if path.startswith(release_prefix) and path.endswith("/preview-retry"):
                    task = unquote(path[len(release_prefix):-len("/preview-retry")])
                    board.clear_release_preview(root, task)
                    self.send_json(200, {"cleared": task}); return
                if path.startswith(release_prefix) and path.endswith("/push-instruction"):
                    task = unquote(path[len(release_prefix):-len("/push-instruction")])
                    length = int(self.headers.get("Content-Length", "0"))
                    if length > 64 * 1024:
                        raise ValueError("push instruction is too large")
                    data = json.loads(self.rfile.read(length) or b"{}")
                    response = board.record_remote_push_instruction(
                        root, task, str(data.get("remote", "")), str(data.get("branch", "")),
                        str(data.get("expected_remote_tip", "")),
                    )
                    self.send_json(201, response); return
                if path.startswith(release_prefix) and path.endswith("/push-confirm"):
                    task = unquote(path[len(release_prefix):-len("/push-confirm")])
                    length = int(self.headers.get("Content-Length", "0"))
                    if length > 64 * 1024:
                        raise ValueError("push confirmation is too large")
                    data = json.loads(self.rfile.read(length) or b"{}")
                    response = board.confirm_remote_push(root, task, str(data.get("instruction_id", "")))
                    self.send_json(201, response); return
                if path.startswith(release_prefix) and path.endswith("/decision"):
                    task = unquote(path[len(release_prefix):-len("/decision")])
                    length = int(self.headers.get("Content-Length", "0"))
                    content_type = self.headers.get("Content-Type", "")
                    if length > board.MAX_ATTACHMENTS * board.MAX_ATTACHMENT_BYTES + 512 * 1024:
                        raise ValueError("response and attachments are too large")
                    raw = self.rfile.read(length)
                    if content_type.lower().startswith("multipart/form-data"):
                        fields, attachments = parse_release_multipart(raw, content_type)
                        response = board.record_release_decision(root, task, fields.get("decision", ""), fields.get("reason", ""))
                        try:
                            response = board.add_release_attachments(root, task, attachments)
                        except ValueError as error:
                            self.send_json(400, {
                                "message": f"Your response was saved, but one or more attachments were not stored. {error}",
                                "decision_recorded": True,
                            }); return
                        except OSError:
                            self.send_json(500, {
                                "message": "Your response was saved, but one or more attachments could not be stored. Please try again without those files.",
                                "decision_recorded": True,
                            }); return
                        self.send_json(201, {"decision": response, "message": "Your response was saved with its attachments."}); return
                    if length > 128 * 1024:
                        raise ValueError("response is too large")
                    data = json.loads(raw or b"{}")
                    response = board.record_release_decision(root, task, str(data.get("decision", "")), str(data.get("reason", "")))
                    self.send_json(201, {"decision": response}); return
                owner_prefix, owner_suffix = "/api/agents/", "/owner-message"
                if path.startswith(owner_prefix) and path.endswith(owner_suffix):
                    agent_id = unquote(path[len(owner_prefix):-len(owner_suffix)])
                    length = int(self.headers.get("Content-Length", "0"))
                    content_type = self.headers.get("Content-Type", "")
                    if length < 0 or length > board.MAX_TOTAL_ATTACHMENT_BYTES + 512 * 1024:
                        raise ValueError("message and attachments are too large")
                    raw = self.rfile.read(length)
                    if len(raw) != length:
                        raise ValueError("message upload is incomplete")
                    if content_type.lower().startswith("multipart/form-data"):
                        fields, attachments = parse_release_multipart(raw, content_type)
                        text, input_metadata = owner_message_input(fields)
                        response = board.record_owner_message(
                            root, agent_id, text,
                            fields.get("message_type", "direction"), attachments,
                        )
                    else:
                        if length > 128 * 1024:
                            raise ValueError("message is too large")
                        data = json.loads(raw or b"{}")
                        text, input_metadata = owner_message_input({
                            "text": str(data.get("text", "")),
                            "directive_source": "text",
                        })
                        response = board.record_owner_message(
                            root, agent_id, text,
                            str(data.get("message_type", "direction")), [],
                        )
                    response["input"] = input_metadata
                    self.send_json(201, response); return
                length = int(self.headers.get("Content-Length", "0"))
                data = json.loads(self.rfile.read(length) or b"{}")
                if path == "/api/settings/connect":
                    result = (
                        global_settings.test_connection(
                            settings_home, data.get("provider", ""), data.get("model", ""),
                            data.get("effort", ""), project_context(root).code_root,
                        )
                        if settings_home else
                        test_provider_connection(root, data.get("provider", ""), data.get("effort", ""), data.get("model", ""))
                    )
                    self.send_json(200, result); return
                if path == "/api/settings":
                    if "workspace_root" in data:
                        raise ValueError("the project folder is managed from Projects and cannot be changed here")
                    selected = data.get("settings", data)
                    if settings_home:
                        global_settings.update_agent_settings(settings_home, selected)
                    else:
                        control.update_agent_settings(root, selected)
                    self.send_json(200, settings_payload(root, settings_home)); return
                if path == "/api/sessions":
                    settings_override = (
                        global_settings.load(settings_home)["agent_settings"]
                        if settings_home else None
                    )
                    session = control.create(
                        root, data.get("kind", ""), data.get("task", ""),
                        data.get("color", "black"), settings_override=settings_override,
                    )
                    try:
                        launch_terminal(root, session)
                    except Exception as error:
                        session = control.fail_launch(root, session["id"], f"unable to open Terminal: {error}")
                        self.send_json(500, {"error": session["reason"], "session": session}); return
                    self.send_json(201, {"session": session}); return
                if path == "/api/settings/browse":
                    raise ValueError("the project folder is managed from Projects and cannot be changed here")
                if path == "/api/settings/apply":
                    settings = workspace_settings.load(root)
                    provider = str(data.get("provider", ""))
                    result = workspace_settings.apply_provider_files(settings, provider)
                    self.send_json(200, {"result": result}); return
                if path == "/api/findings":
                    finding = board.record_finding(
                        root,
                        str(data.get("task", "")),
                        str(data.get("title", "")),
                        str(data.get("description", "")),
                        bool(data.get("affects_current_task", False)),
                        str(data.get("evidence", "")),
                    )
                    self.send_json(201, {"finding": finding}); return
                if path == "/api/sessions/stop-all":
                    cleanup = board.cancel_all_unfinished_work(root)
                    active = [
                        session for session in control.snapshot(root).get("sessions", [])
                        if session.get("status") in control.ACTIVE_STATUSES
                    ]
                    stopped = [control.stop(root, session["id"]) for session in active]
                    self.send_json(200, {
                        **cleanup,
                        "stopped_sessions": len(stopped),
                        "sessions": stopped,
                    }); return
                prefix, suffix = "/api/sessions/", "/stop"
                if path.startswith(prefix) and path.endswith(suffix):
                    session_id = path[len(prefix):-len(suffix)]
                    cleanup = board.cancel_session_work(root, session_id)
                    stopped = [control.stop(root, value) for value in cleanup.get("related_session_ids", [session_id])]
                    primary = next((value for value in stopped if value.get("id") == session_id), stopped[0] if stopped else {})
                    self.send_json(200, {"session": primary, "stopped_sessions": stopped, "cleanup": cleanup}); return
                agent_prefix, recovery_suffix = "/api/agents/", "/recover"
                if path.startswith(agent_prefix) and path.endswith(recovery_suffix):
                    agent_id = path[len(agent_prefix):-len(recovery_suffix)]
                    self.send_json(200, {"recovery": board.request_recovery(root, agent_id)}); return
                finding_prefix, decision_suffix = "/api/findings/", "/decision"
                if path.startswith(finding_prefix) and path.endswith(decision_suffix):
                    finding_id = unquote(path[len(finding_prefix):-len(decision_suffix)])
                    finding = board.record_finding_decision(root, finding_id, str(data.get("decision", "")))
                    dispatch = dispatch_approved_findings(root, settings_home) if finding.get("decision") == "fix" else {"status": "not_requested"}
                    self.send_json(200, {"finding": finding, "dispatch": dispatch}); return
                self.send_json(404, {"error": "not found"})
            except (ValueError, json.JSONDecodeError) as error:
                self.send_json(400, {"error": str(error)})
            except Exception as error:
                self.send_json(500, {"error": str(error)})

        def log_message(self, *_):
            return

    return Handler


def serve(root: Path, host: str = "127.0.0.1", port: int = 8742,
          project_name: str = "", project_description: str = "", manager_url: str = "",
          settings_home: Path | None = None, ready_token: str = "", project_id: str = "") -> None:
    board.recover_git_transactions(root)
    board.certify_legacy_review_ledgers(root)
    release_coordinator.coordinate(root)
    server = ThreadingHTTPServer(
        (host, port), make_handler(
            root, project_name, project_description, manager_url, settings_home, ready_token, project_id,
        ),
    )
    print(f"Live NoMoreHappyPath Board: http://{host}:{server.server_address[1]}/", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Live zero-config NoMoreHappyPath board display")
    add_context_arguments(parser)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8742)
    parser.add_argument("--project-name", default="", help="display name when opened from the projects manager")
    parser.add_argument("--project-description", default="")
    parser.add_argument("--manager-url", default="", help="the projects landing page to link back to")
    parser.add_argument("--project-id", default="", help=argparse.SUPPRESS)
    parser.add_argument("--settings-home", default="", help="manager-owned global settings directory")
    parser.add_argument("--ready-token", default="", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    serve(context_from_args(args), args.host, args.port,
          project_name=args.project_name, project_description=args.project_description,
          manager_url=args.manager_url,
          settings_home=Path(args.settings_home).resolve() if args.settings_home else None,
          ready_token=args.ready_token, project_id=args.project_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
