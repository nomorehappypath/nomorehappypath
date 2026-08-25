# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
"""Human-facing Projects landing page for the stable manager process."""

PAGE = r'''<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>NoMoreHappyPath</title>
  <link rel="icon" type="image/png" href="/favicon.png?v=2">
  <style>
    :root {
      color-scheme: light;
      --ink: #142033;
      --muted: #65738a;
      --subtle: #8b97aa;
      --line: #dfe5ec;
      --paper: #f3f6fa;
      --card: #ffffff;
      --nav: #0d1728;
      --blue: #365cf5;
      --blue-strong: #2847d7;
      --blue-soft: #edf1ff;
      --green: #087a55;
      --green-soft: #e9f8f1;
      --amber: #9a5b0a;
      --amber-soft: #fff6e5;
      --red: #b42318;
      --red-soft: #fff0ee;
      --shadow: 0 18px 48px rgba(23, 36, 58, .08);
    }
    * { box-sizing: border-box; }
    html, body { height: 100%; overflow: hidden; }
    html { background: var(--paper); }
    body { margin: 0; display: flex; flex-direction: column; color: var(--ink); background: var(--paper); font: 15px/1.5 Inter, ui-sans-serif, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
    button, input, textarea, select { font: inherit; }
    button { cursor: pointer; }
    button:focus-visible, input:focus-visible, textarea:focus-visible, select:focus-visible { outline: 3px solid rgba(54, 92, 245, .25); outline-offset: 2px; }
    [hidden] { display: none !important; }

    .topbar { position: relative; z-index: 10; flex: none; height: 68px; display: flex; align-items: center; padding: 0 34px; background: rgba(13, 23, 40, .97); color: white; box-shadow: 0 1px 0 rgba(255,255,255,.08); backdrop-filter: blur(14px); }
    .brand { display: flex; align-items: center; gap: 11px; min-width: 230px; font-size: 16px; font-weight: 780; letter-spacing: -.01em; }
    .brand-mark { width: 30px; height: 30px; display: grid; place-items: center; border-radius: 9px; color: white; background: linear-gradient(145deg, #5576ff, #7a52f5); box-shadow: 0 8px 20px rgba(83, 91, 245, .35); }
    .brand-logo { width: 30px; height: 30px; border-radius: 8px; object-fit: contain; }
    .nav { display: flex; align-items: center; gap: 5px; height: 100%; }
    .nav button { height: 40px; border: 0; border-radius: 9px; padding: 0 15px; color: #9eabc0; background: transparent; font-weight: 680; }
    .nav button:hover { color: white; background: rgba(255,255,255,.07); }
    .nav button[aria-current="page"] { color: white; background: rgba(255,255,255,.1); }
    .connection { margin-left: auto; display: inline-flex; align-items: center; gap: 8px; color: #cbd5e5; font-size: 13px; font-weight: 650; }
    .connection-dot { width: 8px; height: 8px; border-radius: 50%; background: #42d69a; box-shadow: 0 0 0 4px rgba(66,214,154,.12); }

    .page { flex: 1; min-height: 0; width: min(1180px, calc(100% - 48px)); margin: 0 auto; padding: 24px 0 18px; overflow: hidden; }
    #projects-page { height: 100%; min-height: 0; display: flex; flex-direction: column; }
    #settings-page, #help-page, #legal-page { height: 100%; min-height: 0; overflow-y: auto; }
    .hero { display: flex; align-items: center; justify-content: space-between; gap: 32px; margin-bottom: 16px; }
    .eyebrow { margin-bottom: 4px; color: var(--blue); font-size: 12px; font-weight: 820; letter-spacing: .1em; text-transform: uppercase; }
    h1 { margin: 0; font-size: 38px; line-height: 1.08; letter-spacing: -.045em; }
    .hero-copy { max-width: 650px; margin: 5px 0 0; color: var(--muted); font-size: 14px; }
    .hero-actions, .actions, .dialog-actions { display: flex; align-items: center; gap: 10px; }
    .button { appearance: none; border: 1px solid transparent; border-radius: 10px; min-height: 42px; padding: 9px 15px; color: white; background: var(--blue); font-weight: 740; box-shadow: 0 5px 14px rgba(54,92,245,.18); transition: transform .12s ease, background .12s ease, border-color .12s ease; }
    .button:hover { background: var(--blue-strong); transform: translateY(-1px); }
    .button.secondary { color: #33415a; background: white; border-color: var(--line); box-shadow: 0 2px 5px rgba(23,36,58,.04); }
    .button.secondary:hover { color: var(--blue); border-color: #b9c5e8; background: #fafbff; }
    .button.ghost { color: var(--muted); background: transparent; box-shadow: none; }
    .button.danger { color: var(--red); background: var(--red-soft); border-color: #ffd5d0; box-shadow: none; }
    .button:disabled { cursor: not-allowed; opacity: .52; transform: none; }

    .summary { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 10px; margin-bottom: 8px; }
    .summary-card { min-height: 58px; padding: 9px 14px; border: 1px solid var(--line); border-radius: 14px; background: rgba(255,255,255,.72); }
    .summary-label { color: var(--muted); font-size: 12px; font-weight: 760; text-transform: uppercase; letter-spacing: .06em; }
    .summary-value { margin-top: 0; font-size: 22px; line-height: 1.2; font-weight: 790; letter-spacing: -.03em; }
    .summary-note { color: var(--subtle); font-size: 12px; }

    .list-head { display: flex; align-items: center; justify-content: space-between; margin: 10px 0 8px; }
    .list-head h2 { margin: 0; font-size: 17px; letter-spacing: -.015em; }
    .list-note { color: var(--subtle); font-size: 13px; }
    #projects { flex: 1; min-height: 0; display: grid; grid-auto-rows: max-content; align-content: start; gap: 10px; overflow-y: auto; overscroll-behavior: contain; scrollbar-gutter: stable; padding: 8px 10px 12px 8px; border: 1px solid var(--line); border-radius: 18px; background: rgba(255,255,255,.38); box-shadow: inset 0 1px 2px rgba(23,36,58,.035); }
    #projects::-webkit-scrollbar { width: 10px; }
    #projects::-webkit-scrollbar-track { background: transparent; }
    #projects::-webkit-scrollbar-thumb { border: 3px solid var(--paper); border-radius: 999px; background: #c2ccd9; }
    .project { position: relative; display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 16px; padding: 16px; overflow: hidden; border: 1px solid var(--line); border-radius: 16px; background: var(--card); box-shadow: 0 2px 4px rgba(23,36,58,.025); transition: border-color .16s ease, box-shadow .16s ease, transform .16s ease; }
    .project::before { content: ""; position: absolute; inset: 0 auto 0 0; width: 4px; background: #cad3df; }
    .project.active::before { background: var(--blue); }
    .project.running::before { background: #18a873; }
    .project.stopped::before { background: #e39a2d; }
    .project.paused::before { background: #7756b7; }
    .project.unhealthy::before { background: #e06457; }
    .project:hover { border-color: #c8d2df; box-shadow: var(--shadow); transform: translateY(-1px); }
    .project-top { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
    .project h3 { margin: 0; font-size: 19px; line-height: 1.25; letter-spacing: -.02em; }
    .badges { display: inline-flex; gap: 6px; flex-wrap: wrap; }
    .badge { display: inline-flex; align-items: center; min-height: 24px; border-radius: 999px; padding: 3px 9px; font-size: 10px; font-weight: 820; letter-spacing: .04em; text-transform: uppercase; }
    .badge.open { color: #2847d7; background: var(--blue-soft); }
    .badge.running { color: var(--green); background: var(--green-soft); }
    .badge.idle { color: #637084; background: #eef2f6; }
    .badge.stopped { color: #85510a; background: #fff0d5; }
    .badge.paused { color: #5a398e; background: #f0eaff; }
    .badge.bad { color: var(--red); background: var(--red-soft); }
    .badge.kind { color: #765314; background: #fff7df; }
    .description { display: -webkit-box; max-width: 760px; min-height: 0; margin: 5px 0 9px; overflow: hidden; color: #4f5e74; font-size: 14px; white-space: pre-line; -webkit-box-orient: vertical; -webkit-line-clamp: 2; }
    .description.empty-copy { color: var(--subtle); font-style: italic; }
    .metrics { display: flex; flex-wrap: wrap; gap: 6px 14px; margin-bottom: 8px; }
    .metric { color: var(--muted); font-size: 13px; }
    .metric strong { color: var(--ink); font-weight: 760; }
    .progress { display: flex; gap: 10px; align-items: flex-start; max-width: 780px; padding: 8px 10px; border-radius: 10px; color: #44536a; background: #f7f9fc; font-size: 13px; }
    .progress-icon { flex: none; width: 20px; height: 20px; display: grid; place-items: center; border-radius: 6px; color: var(--blue); background: var(--blue-soft); font-size: 11px; font-weight: 850; }
    .path { display: flex; align-items: center; gap: 7px; min-width: 0; margin-top: 8px; color: var(--subtle); font: 12px/1.35 ui-monospace, SFMono-Regular, Menlo, monospace; }
    .path span:last-child { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .project-side { min-width: 150px; display: flex; flex-direction: column; align-items: flex-end; justify-content: space-between; gap: 9px; }
    .last-active { color: var(--subtle); font-size: 12px; text-align: right; }
    .actions { justify-content: flex-end; flex-wrap: wrap; }
    .actions .button { min-height: 38px; padding: 7px 12px; font-size: 13px; }

    .empty, .skeleton { border: 1px dashed #cbd4df; border-radius: 16px; padding: 52px 24px; text-align: center; color: var(--muted); background: rgba(255,255,255,.55); }
    .empty strong { display: block; margin-bottom: 5px; color: var(--ink); font-size: 18px; }
    .skeleton { min-height: 150px; display: grid; place-items: center; }
    .spinner { width: 24px; height: 24px; border: 3px solid #dce3ed; border-top-color: var(--blue); border-radius: 50%; animation: spin .8s linear infinite; }
    @keyframes spin { to { transform: rotate(360deg); } }

    .notice { position: fixed; right: 24px; bottom: 24px; z-index: 20; width: min(420px, calc(100% - 48px)); padding: 13px 15px; border: 1px solid #d9e1ed; border-radius: 12px; color: #33415a; background: white; box-shadow: 0 18px 50px rgba(23,36,58,.18); }
    .notice.error { color: #8a241d; border-color: #facfc9; background: #fff8f7; }
    .confidentiality { margin-top: 8px; padding: 8px 12px; border: 1px solid #f2ddad; border-radius: 12px; color: #765014; background: var(--amber-soft); font-size: 12px; line-height: 1.3; }
    .settings-shell { display: grid; gap: 18px; padding-bottom: 30px; }
    .settings-intro { display: flex; justify-content: space-between; gap: 24px; align-items: center; padding: 18px 20px; border: 1px solid #d8e1fb; border-radius: 14px; color: #34425a; background: var(--blue-soft); }
    .settings-intro strong { display: block; margin-bottom: 3px; color: var(--ink); }
    .settings-intro p { margin: 0; color: var(--muted); font-size: 13px; }
    .settings-cards { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 14px; }
    .settings-card { display: grid; align-content: start; gap: 14px; padding: 20px; border: 1px solid var(--line); border-radius: 16px; background: white; box-shadow: 0 2px 5px rgba(23,36,58,.035); }
    .settings-card h2 { margin: 0; font-size: 18px; letter-spacing: -.02em; }
    .settings-card-copy { min-height: 40px; margin: -8px 0 0; color: var(--muted); font-size: 12px; }
    .settings-card .field { margin: 0; }
    .settings-card select, .settings-card input { width: 100%; min-height: 42px; border: 1px solid #cfd7e2; border-radius: 10px; padding: 8px 10px; color: var(--ink); background: white; }
    .connection-result { min-height: 58px; padding: 10px 11px; border-radius: 10px; color: var(--muted); background: #f6f8fb; font-size: 12px; }
    .connection-result.ok { color: var(--green); background: var(--green-soft); }
    .connection-result.bad { color: var(--red); background: var(--red-soft); }
    .openai-card { max-width: 620px; }
    .openai-state { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; color: var(--muted); font-size: 12px; }
    .openai-actions { display: flex; gap: 10px; flex-wrap: wrap; }
    .topbar-version { margin-right: 14px; border: 1px solid rgba(255,255,255,.22); border-radius: 999px; background: transparent; color: #cbd5e5; padding: 5px 12px; font-size: 12px; font-weight: 700; }
    .topbar-version:hover { color: white; border-color: rgba(255,255,255,.45); }
    .settings-footer { display: flex; justify-content: flex-end; align-items: center; gap: 14px; }
    .settings-error { flex: 1; margin: 0; color: var(--red); font-size: 13px; }
    .help-shell { display: grid; gap: 14px; padding-bottom: 30px; }
    .help-section { min-width: 0; padding: 18px 20px; border: 1px solid var(--line); border-radius: 16px; background: white; box-shadow: 0 2px 5px rgba(23,36,58,.035); overflow-wrap: anywhere; }
    .help-section h2 { margin: 0 0 8px; font-size: 18px; letter-spacing: -.02em; }
    .help-section p { margin: 6px 0; color: #4f5e74; }
    .help-section strong { color: var(--ink); }
    .help-steps, .help-list { margin: 8px 0 0; padding-left: 22px; color: #4f5e74; }
    .help-steps li, .help-list li { margin: 7px 0; }
    .help-table { width: 100%; border-collapse: collapse; margin-top: 9px; font-size: 14px; }
    .help-table th, .help-table td { padding: 9px 10px; border-top: 1px solid var(--line); text-align: left; vertical-align: top; }
    .help-table th { color: var(--muted); font-size: 12px; font-weight: 820; letter-spacing: .06em; text-transform: uppercase; }
    .help-callout { margin-top: 10px; padding: 10px 12px; border-left: 3px solid var(--blue); border-radius: 0 9px 9px 0; color: #27476f; background: var(--blue-soft); font-size: 13px; }

    dialog { width: min(560px, calc(100% - 32px)); padding: 0; border: 0; border-radius: 18px; color: var(--ink); background: white; box-shadow: 0 30px 90px rgba(8,18,35,.34); }
    dialog::backdrop { background: rgba(9,17,30,.58); backdrop-filter: blur(3px); }
    .modal { padding: 25px; }
    .modal h2 { margin: 0; font-size: 22px; letter-spacing: -.025em; }
    .modal-copy { margin: 5px 0 18px; color: var(--muted); font-size: 14px; }
    .field { display: grid; gap: 6px; margin-top: 13px; }
    .field label { color: #435168; font-size: 13px; font-weight: 710; }
    .field input, .field textarea { width: 100%; border: 1px solid #cfd7e2; border-radius: 10px; padding: 10px 12px; color: var(--ink); background: white; }
    .field textarea { min-height: 104px; resize: vertical; line-height: 1.5; }
    .field-hint { margin: 0; color: var(--subtle); font-size: 12px; }
    .folder-control { display: grid; grid-template-columns: auto minmax(0, 1fr); gap: 9px; align-items: stretch; }
    .folder-control .button { min-height: 44px; box-shadow: none; }
    .selected-path { min-width: 0; display: flex; align-items: center; padding: 10px 12px; overflow: hidden; border: 1px solid #cfd7e2; border-radius: 10px; color: #526078; background: #f8fafc; font: 12px/1.35 ui-monospace, SFMono-Regular, Menlo, monospace; text-overflow: ellipsis; white-space: nowrap; }
    .selected-path.empty { color: var(--subtle); font-family: inherit; font-style: italic; }
    .folder-preview { margin-top: 7px; padding: 9px 11px; border-radius: 9px; color: #41506a; background: var(--blue-soft); font-size: 12px; overflow-wrap: anywhere; }
    .form-error { min-height: 21px; margin: 12px 0 0; color: var(--red); font-size: 13px; }
    .danger-copy { padding: 12px 14px; border-radius: 10px; color: #7e2b24; background: var(--red-soft); font-size: 13px; }
    .dialog-actions { justify-content: flex-end; margin-top: 21px; }

    /* Keep the first project readable on laptop-height windows while the
       project list remains the only scrolling region. */
    @media (max-height: 700px) and (min-width: 761px) {
      .topbar { height: 58px; }
      .page { padding: 10px 0; }
      .hero { margin-bottom: 8px; }
      .eyebrow { display: none; }
      h1 { font-size: 30px; }
      .hero-copy { margin-top: 2px; font-size: 13px; line-height: 1.25; }
      .summary { gap: 8px; margin-bottom: 4px; }
      .summary-card { min-height: 42px; padding: 5px 10px; }
      .summary-label { font-size: 10px; }
      .summary-value { font-size: 18px; }
      .summary-note { display: none; }
      .list-head { margin: 6px 0 4px; }
      .list-note { font-size: 12px; }
      #projects { gap: 8px; padding: 6px 8px 8px 6px; }
      .project { gap: 10px; padding: 10px 12px; }
      .project h3 { font-size: 16px; }
      .badge { min-height: 20px; padding: 2px 7px; font-size: 9px; }
      .description { margin: 3px 0 5px; font-size: 13px; -webkit-line-clamp: 1; }
      .metrics { gap: 4px 10px; margin-bottom: 5px; }
      .metric { font-size: 12px; }
      .progress { padding: 6px 8px; font-size: 12px; }
      .progress > span:last-child { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
      .path { margin-top: 5px; font-size: 11px; }
      .project-side { min-width: 135px; gap: 6px; }
      .last-active { font-size: 11px; }
      .actions .button { min-height: 32px; padding: 5px 9px; font-size: 12px; }
      .confidentiality { margin-top: 4px; padding: 5px 8px; font-size: 11px; }
    }

    @media (max-width: 760px) {
      .topbar { height: auto; min-height: 64px; padding: 12px 18px; flex-wrap: wrap; gap: 8px; }
      .brand { min-width: 0; margin-right: auto; }
      .nav { order: 3; flex: 0 0 100%; width: 100%; height: auto; display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); }
      .nav button { min-width: 0; width: 100%; padding: 0 8px; }
      .connection { font-size: 0; }
      .page { width: calc(100% - 30px); max-width: 1180px; padding: 26px 0 18px; }
      .hero { align-items: flex-start; flex-direction: column; gap: 20px; }
      .hero-actions { width: 100%; }
      .hero-actions .button { flex: 1; }
      .summary { grid-template-columns: 1fr; }
      .settings-cards { grid-template-columns: 1fr; }
      .settings-intro { align-items: flex-start; flex-direction: column; }
      .help-table, .help-table tbody, .help-table tr, .help-table th, .help-table td { display: block; width: 100%; }
      .help-table thead { display: none; }
      .help-table tr { border-top: 1px solid var(--line); padding: 8px 0; }
      .help-table td { border-top: 0; padding: 4px 0; }
      .help-table td:first-child { color: var(--ink); font-weight: 760; }
      .project { grid-template-columns: 1fr; padding: 19px; }
      .project-side { min-width: 0; align-items: stretch; }
      .last-active { text-align: left; }
      .actions { justify-content: flex-start; }
      .folder-control { grid-template-columns: 1fr; }
    }
    @media (prefers-reduced-motion: reduce) {
      *, *::before, *::after { scroll-behavior: auto !important; transition: none !important; animation-duration: .01ms !important; animation-iteration-count: 1 !important; }
    }
  </style>
</head>
<body>
  <header class="topbar">
    <div class="brand"><img class="brand-logo" src="/favicon.png" alt="" aria-hidden="true"><span>NoMoreHappyPath</span></div>
    <nav class="nav" aria-label="Primary navigation">
      <button type="button" data-page="projects" aria-current="page">Projects</button>
      <button type="button" data-page="settings">Settings</button>
      <button type="button" data-page="help">Help</button>
      <button type="button" data-page="legal">Legal</button>
    </nav>
    <button type="button" class="topbar-version" id="update-open" title="Version and updates"><span id="app-version">…</span></button>
    <div class="connection"><span class="connection-dot" aria-hidden="true"></span><span>Manager connected</span></div>
  </header>

  <main class="page">
    <section id="projects-page">
      <div class="hero">
        <div>
          <div class="eyebrow">Your workspace</div>
          <h1>Projects</h1>
          <p class="hero-copy">Open a project to continue exactly where you left off, or create a focused workspace for something new.</p>
        </div>
        <div class="hero-actions">
          <button class="button secondary" id="adopt-btn" type="button">Adopt existing</button>
          <button class="button" id="new-btn" type="button">New project</button>
        </div>
      </div>

      <section class="summary" aria-label="Project summary">
        <div class="summary-card"><div class="summary-label">Projects</div><div class="summary-value" id="summary-total">—</div><div class="summary-note">Available workspaces</div></div>
        <div class="summary-card"><div class="summary-label">Active work</div><div class="summary-value" id="summary-active">—</div><div class="summary-note">Open projects and running tasks</div></div>
        <div class="summary-card"><div class="summary-label">Needs attention</div><div class="summary-value" id="summary-health">—</div><div class="summary-note">Folder or board health issues</div></div>
      </section>

      <div class="list-head"><h2>All projects</h2><span class="list-note">One project can be open at a time</span></div>
      <div id="projects" aria-busy="true"><div class="skeleton"><div class="spinner" aria-label="Loading projects"></div></div></div>
      <div class="confidentiality" id="codex-notice" hidden></div>

    </section>

    <section id="settings-page" hidden>
      <div class="hero"><div><div class="eyebrow">NoMoreHappyPath configuration</div><h1>Settings</h1><p class="hero-copy">Agent models, effort, and connectivity in one place.</p></div></div>
      <form class="settings-shell" id="settings-form">
        <div class="settings-intro"><div><strong>Global agent defaults</strong><p>These choices apply to new agent sessions in every project. Running sessions keep the model and effort they started with.</p></div><span class="badge open">Manager-wide</span></div>
        <section class="settings-card openai-card" id="openai-card" aria-labelledby="openai-title">
          <div><h2 id="openai-title">Project chat key</h2><p class="settings-card-copy">Project chat answers questions about the project you have open, using your own OpenAI account. Chat stays switched off until this key connects.</p></div>
          <div class="openai-state"><span class="badge idle" id="openai-badge">Checking</span><span id="openai-detail"></span></div>
          <div class="field"><label for="openai-key">OpenAI API key</label><input id="openai-key" type="password" autocomplete="off" spellcheck="false" placeholder="sk-…" aria-describedby="openai-result"></div>
          <div class="openai-actions"><button class="button" type="button" id="openai-save">Save and connect</button><button class="button secondary" type="button" id="openai-test" hidden>Test connection</button><button class="button secondary" type="button" id="openai-remove" hidden>Remove key</button></div>
          <div class="connection-result" id="openai-result" role="status" aria-live="polite"></div>
        </section>
        <div class="settings-cards" id="settings-fields"><div class="skeleton"><div class="spinner" aria-label="Loading settings"></div></div></div>
        <div class="settings-footer"><p class="settings-error" id="settings-error" role="alert" aria-live="polite"></p><button class="button" type="submit" id="settings-save">Save agent settings</button></div>
      </form>
    </section>

    <section id="help-page" hidden>
      <div class="hero"><div><div class="eyebrow">Owner guide</div><h1>Help</h1><p class="hero-copy">A practical walkthrough for creating or adopting a project, giving useful direction, reading progress, and accepting completed work.</p></div></div>
      <div class="help-shell">
        <section class="help-section" aria-labelledby="help-start-title">
          <h2 id="help-start-title">Create Or Adopt A Project</h2>
          <ol class="help-steps">
            <li><strong>Use New project</strong> when NoMoreHappyPath should create a fresh project folder for new work. Choose a parent folder, name the project, and add a short description that explains the purpose.</li>
            <li><strong>Use Adopt existing</strong> when the code already exists. Choose the exact repository or project folder you want agents to work on. NoMoreHappyPath keeps its board, evidence, and workspaces outside the adopted repository.</li>
            <li><strong>Open project</strong> restores Mission Control for that project. A paused project opens from the saved board state instead of starting over.</li>
            <li><strong>Repair project folder</strong> is only for a moved folder. It reconnects NoMoreHappyPath history to the new location and does not delete the old folder.</li>
          </ol>
          <p class="help-callout">Best practice: one NoMoreHappyPath project should map to one real code folder and one clear product objective. Create a separate project when the code, audience, or release path is different.</p>
        </section>

        <section class="help-section" aria-labelledby="help-setup-title">
          <h2 id="help-setup-title">What This App Needs To Run</h2>
          <ul class="help-list">
            <li>The app runs on this Mac only and lives at <strong>http://127.0.0.1:8740</strong>. Nothing is sent to a server of ours; projects, boards, and settings stay in your home folder.</li>
            <li>The AI agents run on <strong>your own accounts</strong> through two CLIs: the Codex CLI (OpenAI account) and the Claude Code CLI (Anthropic account). In <strong>Settings you choose which vendor plays each role</strong> — Delivery, Reviewer, and CTO can each use either CLI. Install both and sign each in once from a terminal; the platform's core guarantee — a competing vendor reviews the work — needs the builder and the reviewer on different vendors, so both CLIs are required.</li>
            <li>Project chat uses your own <strong>OpenAI API key</strong> (pay-per-use, fractions of a cent per question) — see the next section.</li>
            <li>Updating is built in — see <strong>Check Your Version And Update</strong> just below.</li>
          </ul>
        </section>

        <section class="help-section" aria-labelledby="help-version-title">
          <h2 id="help-version-title">Check Your Version And Update</h2>
          <ol class="help-steps">
            <li>Your installed version is always visible in the <strong>top bar</strong> — the small pill next to "Manager connected" (for example <strong>v0.1.7</strong>).</li>
            <li><strong>Click the version pill.</strong> The Software Update dialog opens and checks automatically — it asks only this installation's own GitHub origin; nothing is sent anywhere else. It answers plainly: you are up to date, or a newer version exists, with a <strong>What changed</strong> link to the release notes.</li>
            <li>When a newer version exists and no project is running (<strong>paused is fine</strong>), click <strong>Update now</strong>. The update is fast-forward only, never touches your projects, boards, or settings, and the app restarts itself — the page reloads on the new version.</li>
          </ol>
          <p class="help-callout">If <strong>Update now</strong> does not appear: a project is running — pause or close it and check again. If the dialog reports local changes, your installation folder was edited by hand; the update refuses to overwrite anything and shows the manual command instead.</p>
        </section>

        <section class="help-section" aria-labelledby="help-nohappy-title">
          <h2 id="help-nohappy-title">Why It Takes Its Time (No Happy Path)</h2>
          <p>The happy path is the demo where everything goes right on the first try. This app is built on the opposite assumption: an agent saying "done" proves nothing. So it deliberately spends time other tools skip:</p>
          <ul class="help-list">
            <li><strong>Cross-checking by a competing vendor.</strong> The agent that wrote the code never judges it; a reviewer from the other company runs the work itself and can fail it.</li>
            <li><strong>Scenario tests, not just the sunny case.</strong> Agents are required to build and run tests for the failure paths — wrong input, missing files, things breaking mid-way — before claiming success.</li>
            <li><strong>Ledgers.</strong> Every claim is written down with the command that proves it and its output, so "it works" is always traceable to evidence you can reread later.</li>
            <li><strong>Simulations.</strong> Risky changes are rehearsed against realistic conditions before they are allowed to land.</li>
          </ul>
          <p class="help-callout">This is why a task can look slow while the bars barely move: the time is going into checks designed to catch what a quick demo would hide. Slower to say done — far less likely to be wrong when it does.</p>
        </section>

        <section class="help-section" aria-labelledby="help-key-title">
          <h2 id="help-key-title">Switch On Project Chat (OpenAI Key)</h2>
          <ol class="help-steps">
            <li>The chat box on every project board stays <strong>greyed off until a working key is saved</strong>. The notice above the box says exactly this.</li>
            <li>Open <strong>Settings</strong> and find the <strong>Project chat key</strong> card. Paste the key from your OpenAI account (platform.openai.com → API keys) and click <strong>Save and connect</strong>.</li>
            <li>The key is checked with OpenAI <strong>before</strong> it is stored. A key OpenAI rejects is never saved, and the card tells you why in plain words.</li>
            <li>When the badge shows <strong>Connected</strong>, chat is on everywhere. The key is kept on this Mac in a file only you can read, shown on screen only as its last 4 characters.</li>
            <li>Use <strong>Test connection</strong> any time to re-check the saved key, <strong>Replace and connect</strong> to change it, and <strong>Remove key</strong> to switch chat off.</li>
          </ol>
        </section>

        <section class="help-section" aria-labelledby="help-settings-title">
          <h2 id="help-settings-title">Configure The Agents (Settings)</h2>
          <ul class="help-list">
            <li>Settings has one card per role — <strong>Delivery Agent</strong>, <strong>Independent Reviewer</strong>, and <strong>CTO</strong>. Each picks a CLI provider, a model, and a reasoning effort.</li>
            <li>Keep Delivery and Reviewer on <strong>different providers</strong>; that separation is the point of independent review.</li>
            <li>Bigger models and higher effort cost more on your provider plans and think longer. A common setup: strong models for Delivery and Reviewer, a medium setting for the CTO.</li>
            <li><strong>Test connection</strong> on a card checks that the CLI is installed and accepts the chosen model — it launches nothing and bills nothing.</li>
            <li>Saving applies to <strong>new agent sessions only</strong>; running terminals keep the model and effort they started with.</li>
          </ul>
        </section>

        <section class="help-section" aria-labelledby="help-direction-title">
          <h2 id="help-direction-title">Frame A Good Task</h2>
          <p>Give Delivery enough context to make the right tradeoffs before it starts writing code.</p>
          <ul class="help-list">
            <li>State the outcome you want in plain language: what the user should be able to do when the task is done.</li>
            <li>Name the target app, screen, folder, platform, or command when it matters.</li>
            <li>List constraints: what must stay unchanged, what is out of scope, and what quality checks are mandatory.</li>
            <li>Include visual acceptance checks for UI work: screen sizes, buttons to click, data to enter, and what should appear.</li>
            <li>Keep unrelated goals separate. A smaller task with clear acceptance usually finishes faster and reviews more cleanly.</li>
          </ul>
        </section>

        <section class="help-section" aria-labelledby="help-go-title">
          <h2 id="help-go-title">Agree The Requirements And Say Go Ahead</h2>
          <ol class="help-steps">
            <li><strong>The first time an agent opens a project folder, its CLI asks whether you trust that folder.</strong> This comes from Codex and Claude themselves, not from this app — read it and answer deliberately: it is your confirmation that these agents may work in that exact folder. Each role (Delivery, Reviewer, CTO) asks once.</li>
            <li>Send the initial direction from the Delivery agent row with <strong>Give direction</strong>, or upload a .md or .txt file when the instruction is long.</li>
            <li>Delivery may ask clarifying questions in its terminal window — answer there; that conversation shapes the requirements.</li>
            <li>When the requirements are agreed, Delivery <strong>files them on the board</strong>. The task card then shows <strong>"Final agreed requirements — your decision"</strong> with the full text and two buttons. Nothing is built before your decision.</li>
            <li>Read the text as the task contract it is. Click <strong>Go ahead — this is the contract</strong> when it matches what you want built; click <strong>Modify…</strong> to describe changes — Delivery revises and files a new version for your decision. Every click is recorded with a timestamp; typing "go ahead" in the terminal is not authorization.</li>
            <li>After work starts, use <strong>Send clarification</strong> for necessary corrections. New feature ideas should usually become a new task after the current one is accepted.</li>
          </ol>
        </section>

        <section class="help-section" aria-labelledby="help-direct-title">
          <h2 id="help-direct-title">Talking To The Agents Directly</h2>
          <p>Every agent runs in a visible terminal window, and you can type to any of them at any time — ask what they are doing, why a decision was made, or for a plain-language summary.</p>
          <p class="help-callout"><strong>Be careful while work is running.</strong> A message typed into a busy agent is not a side conversation — the agent stops to handle it. Interrupting a build, a review, or a release check inserts your request into the middle of that work and can slow it down or derail it. If it can wait, let the current stage finish and read the agent's status first; if you must redirect running work, use <strong>Send clarification</strong> so the change is recorded on the task, not lost in scrollback. New ideas belong in a new task after this one is accepted.</p>
        </section>

        <section class="help-section" aria-labelledby="help-buttons-title">
          <h2 id="help-buttons-title">Main Buttons And What They Show</h2>
          <p class="help-callout"><strong>A task needs all three roles running: at least one Delivery agent, one Independent Reviewer, and the CTO.</strong> Delivery builds, the Reviewer verifies, and the CTO routes work and watches the gates. With any of them missing, the task stalls at that role's stage — direction with no builder, a finished build with no verdict, or a passed review that never reaches release. Start all three from Mission Control before expecting a task to move end to end.</p>
          <table class="help-table">
            <thead><tr><th>Button or area</th><th>What it does</th></tr></thead>
            <tbody>
              <tr><td>Open Mission Control</td><td>Opens the live board for the selected project.</td></tr>
              <tr><td>Pause project</td><td>Stops active work after board writes finish and preserves tasks, reviews, and evidence.</td></tr>
              <tr><td>CODEX CLI - Delivery Agent</td><td>Starts or attaches the implementation agent for owner direction, planning, coding, and Delivery evidence.</td></tr>
              <tr><td>CLAUDE CLI - Reviewer</td><td>Starts or attaches the independent reviewer. It should test the candidate through its own checks, not repeat Delivery's wording.</td></tr>
              <tr><td>CTO</td><td>Starts or attaches the monitoring role that keeps tasks routed, watches release gates, and reports owner action.</td></tr>
              <tr><td>View status</td><td>Shows the selected agent's current task, stage, last update, next step, and whether you need to act.</td></tr>
              <tr><td>Stop terminal / Stop all agents</td><td>Stops visible terminals. Use it when you intentionally want work to stop; saved board records remain.</td></tr>
              <tr><td>Settings</td><td>Changes default provider, model, effort, and connection tests for new agent sessions.</td></tr>
              <tr><td>Help</td><td>Opens this guide.</td></tr>
            </tbody>
          </table>
        </section>

        <section class="help-section" aria-labelledby="help-progress-title">
          <h2 id="help-progress-title">Read The Progress Bars</h2>
          <ul class="help-list">
            <li><strong>Whole task</strong> shows durable gates completed across the task: direction, implementation evidence, independent review, release checks, owner decision, and acceptance.</li>
            <li><strong>Current stage</strong> shows progress inside the active phase. It can move while the whole-task bar stays flat.</li>
            <li>The bars are not a timer. Review, repair, release checks, or owner validation can add time without meaning the code is being rewritten.</li>
            <li>Red or repair-colored progress means a gate needs attention or a repair cycle. Open the task or agent status and read <strong>What happens next</strong>.</li>
            <li>If progress appears stuck, check the CTO status first. It should say whether work is active, waiting for review, waiting for release checks, or waiting for you.</li>
          </ul>
        </section>

        <section class="help-section" aria-labelledby="help-accept-title">
          <h2 id="help-accept-title">Accept Or Reject Completed Work</h2>
          <ol class="help-steps">
            <li>Wait until Mission Control explicitly says the reviewed release is ready for your test. Delivery saying it is done is not enough.</li>
            <li>Open the candidate app or preview from the release panel when one is provided.</li>
            <li>Run the visible checklist shown under <strong>What to test before accepting</strong>. Add your own checks when the task needs them.</li>
            <li>Click <strong>Accepted</strong> only when the exact reviewed candidate works for you. Click <strong>Not accepted</strong> when something is wrong and write the reason in ordinary language.</li>
            <li>A local acceptance records the exact reviewed commit. Any remote push is a separate confirmation.</li>
          </ol>
        </section>

        <section class="help-section" aria-labelledby="help-history-title">
          <h2 id="help-history-title">Use Task History</h2>
          <p>Completed tasks move into Task history after release and owner response. History keeps the full user directive, final agreed requirements, owner-readable Delivery checks, reviewer checks, failed attempts, repairs, and final outcomes.</p>
          <ul class="help-list">
            <li>Use search with normal words from the task, requirement, or test description.</li>
            <li>Open older entries to see what was accepted, what was rejected, and what was tested.</li>
            <li>History is evidence for the project assistant. If the board has no data for a question, the assistant should say it does not know.</li>
          </ul>
        </section>

        <section class="help-section" aria-labelledby="help-lifecycle-title">
          <h2 id="help-lifecycle-title">Pause, Close, Or Remove A Project</h2>
          <table class="help-table">
            <thead><tr><th>Action</th><th>What it means</th></tr></thead>
            <tbody>
              <tr><td>Pause project</td><td>Stops agent work safely and freezes the board. Open it later and everything resumes from the saved state.</td></tr>
              <tr><td>Close project (on the board)</td><td>Parks the board and returns to Projects. Nothing is lost; Open brings it right back. Closing also lets a waiting app update apply.</td></tr>
              <tr><td>Remove</td><td>Takes the project off this list only. Your code folder on disk is never deleted.</td></tr>
            </tbody>
          </table>
        </section>

        <section class="help-section" aria-labelledby="help-limits-title">
          <h2 id="help-limits-title">Your Responsibility, And What The Agents Can Do</h2>
          <p class="help-callout"><strong>Using this software is entirely your own responsibility.</strong> It is provided as-is, with no warranty of any kind — see the LICENSE. You decide what the agents work on, you review the results, and you own what you accept and where you deploy it.</p>
          <ul class="help-list">
            <li><strong>The Codex and Claude CLI agents can do whatever they need to do inside the project folder you choose</strong> — create, edit, delete, and run code there. That power is what makes them useful, and it is real: <strong>be careful which folder you point them at.</strong></li>
            <li>Never choose a folder holding things you cannot afford to lose without a safety net. Keep the project in version control (git) or back it up first; adopt the exact project folder, not a parent folder that contains unrelated work.</li>
            <li>Agents spend money on <strong>your</strong> OpenAI and Anthropic accounts while they work. Their usage is your bill; the Stop buttons exist for a reason.</li>
            <li>Everything runs and stays on your Mac. Nothing is sent to us — which also means we cannot see, restore, or recover anything for you.</li>
            <li>Clicking <strong>Accepted</strong> is your decision. The platform gates and collects evidence, but the final judgment — especially before anything reaches real users — is yours.</li>
          </ul>
        </section>

        <section class="help-section" aria-labelledby="help-trouble-title">
          <h2 id="help-trouble-title">When Something Looks Wrong</h2>
          <table class="help-table">
            <thead><tr><th>What you see</th><th>What it means and what to do</th></tr></thead>
            <tbody>
              <tr><td>Chat box is greyed with a notice</td><td>No working OpenAI key. Open Settings → Project chat key and save one; the notice names the exact problem when a key exists but cannot be used.</td></tr>
              <tr><td>An agent launch button is greyed</td><td>That role is at its session limit. Stop one of its running terminals first.</td></tr>
              <tr><td>"This project is closed" over the board</td><td>The board is not running. Go to Projects and click Open.</td></tr>
              <tr><td>Buttons disabled and a paused banner</td><td>The project is paused. Resume it from the Projects page to make changes.</td></tr>
              <tr><td>A change you expected has not appeared</td><td>An update is waiting for the open project to close. Close or pause it and the app restarts itself within seconds.</td></tr>
              <tr><td>An agent stops with quota, credit, or billing errors — or just stalls</td><td>Your OpenAI or Anthropic account ran out of credit or hit its usage limit. <strong>The app does not crash</strong> — the agent's terminal shows the provider's error, and Settings → Test connection names the account that needs attention. Top up or upgrade on the provider's site (OpenAI: platform.openai.com/billing · Anthropic: your Claude plan), then continue the task; nothing on the board is lost.</td></tr>
              <tr><td>An agent dies with "requires a newer version" or a 400 error about the model</td><td>The installed CLI is older than the model you chose, or your account has no access to it. Update that CLI (Codex: <strong>npm install -g @openai/codex</strong>; Claude: reinstall from claude.com/claude-code), or pick a different model in Settings. <strong>Test connection</strong> now makes a real one-word request, so it catches this before a task starts.</td></tr>
              <tr><td>The console runs a CLI but the app says it is not found</td><td>The app does not read your shell profile. It searches the standard install locations itself (including ~/.local/bin); click Test connection again after installing. If the CLI lives somewhere unusual, move or link it into ~/.local/bin.</td></tr>
              <tr><td>"Update now" does not appear although a newer version exists</td><td>A project is running. Pause or close it and check again — the app never restarts under running work. If the dialog reports local changes instead, your installation folder was edited by hand; the update refuses to overwrite anything and shows the manual command.</td></tr>
              <tr><td>Leftover app processes ("ghost shells")</td><td>Run <strong>bash scripts/stop_all.sh</strong> from the installation folder — it stops only that installation's processes and its auto-start service; --list previews first.</td></tr>
              <tr><td>Something else</td><td>The app's log is at ~/Library/Logs/harness-next.log — its last lines usually name the problem.</td></tr>
            </tbody>
          </table>
        </section>

        <section class="help-section" aria-labelledby="help-legal-title">
          <h2 id="help-legal-title">Legal — No Warranty, No Liability</h2>
          <p class="help-callout"><strong>By using this software you accept its full disclaimer:</strong> it is provided as-is with no warranty of any kind; the AI agents act autonomously and you assume the entire risk of what they create, change, delete, execute, and spend on your accounts; and to the maximum extent permitted by law, KpiMinds LLC is not liable for any damages arising from this software — for free use, its total liability is zero.</p>
          <p>The complete, binding text is on the <strong>Legal</strong> page in the top menu and in the <strong>DISCLAIMER</strong> file that ships with the software, next to the LICENSE (Business Source License 1.1). If you do not accept those terms, do not use the software.</p>
        </section>

        <section class="help-section" aria-labelledby="help-chat-title">
          <h2 id="help-chat-title">Ask About This Project</h2>
          <p>The project assistant is read-only. Ask about current status, completed tasks, remaining work, blockers, reviews, releases, timing, or what you should do next. It answers from project records only and refuses unrelated general questions or requests to build code. It needs your OpenAI key from Settings (see "Switch On Project Chat") and costs a fraction of a cent per question.</p>
        </section>
      </div>
    </section>

    <section id="legal-page" hidden>
      <div class="hero"><div><div class="eyebrow">Legal terms</div><h1>Disclaimer &amp; Limitation of Liability</h1><p class="hero-copy">Using NoMoreHappyPath means accepting these terms in full. If you do not accept them, do not use the software.</p></div></div>
      <div class="help-shell">
        <section class="help-section" aria-labelledby="legal-license-title">
          <h2 id="legal-license-title">1. License — Business Source License 1.1</h2>
          <p>This software is distributed under the <strong>Business Source License 1.1</strong>, licensor <strong>KpiMinds LLC</strong>. You may use it freely for evaluation, development, testing, and personal, non-commercial projects. <strong>Any production or commercial use — including offering this software, modified or unmodified, as a product or service to others — requires a commercial license from KpiMinds LLC</strong> (license@kpiminds.com). On 2030-08-21 this version converts to the Apache 2.0 open-source license. The full text ships as the LICENSE file with the software.</p>
        </section>

        <section class="help-section" aria-labelledby="legal-warranty-title">
          <h2 id="legal-warranty-title">2. No Warranty</h2>
          <p>THE SOFTWARE IS PROVIDED "AS IS" AND "AS AVAILABLE", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE, TITLE, NON-INFRINGEMENT, ACCURACY, OR THAT THE SOFTWARE WILL BE ERROR-FREE, UNINTERRUPTED, OR SECURE. NO INFORMATION OR ADVICE GIVEN BY KPIMINDS LLC CREATES ANY WARRANTY.</p>
        </section>
        <section class="help-section" aria-labelledby="legal-risk-title">
          <h2 id="legal-risk-title">3. AI Agents Act Autonomously — You Assume That Risk</h2>
          <p>This software orchestrates third-party AI agents that are non-deterministic and act autonomously: they can create, modify, delete, and execute files and commands inside the folders you designate, and they can incur charges on the third-party accounts you connect (including OpenAI and Anthropic accounts and API keys). YOU ACCEPT THE ENTIRE RISK of agent behavior — including loss, corruption, or deletion of data or code; defective, insecure, or non-functional output; unintended execution of commands; and all charges billed to your accounts. You are solely responsible for folder selection, backups, version control, reviewing all output before use, and every decision to accept or deploy anything the software or its agents produce.</p>
        </section>
        <section class="help-section" aria-labelledby="legal-liability-title">
          <h2 id="legal-liability-title">4. Limitation of Liability</h2>
          <p>TO THE MAXIMUM EXTENT PERMITTED BY LAW, IN NO EVENT SHALL KPIMINDS LLC, ITS MEMBERS, OFFICERS, EMPLOYEES, CONTRACTORS, OR AGENTS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, CONSEQUENTIAL, EXEMPLARY, OR PUNITIVE DAMAGES WHATSOEVER — INCLUDING WITHOUT LIMITATION LOSS OF DATA, LOSS OF PROFITS, BUSINESS INTERRUPTION, COST OF SUBSTITUTE GOODS OR SERVICES, OR THIRD-PARTY API OR SUBSCRIPTION CHARGES — ARISING OUT OF OR RELATING TO THE SOFTWARE OR ITS USE OR INABILITY TO USE, UNDER ANY THEORY OF LIABILITY, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGES. KPIMINDS LLC'S TOTAL AGGREGATE LIABILITY SHALL NOT EXCEED THE AMOUNT YOU PAID KPIMINDS LLC FOR THE SOFTWARE IN THE TWELVE MONTHS PRECEDING THE CLAIM — WHICH, FOR FREE USE, IS ZERO. Where a jurisdiction does not allow certain exclusions, liability is limited to the minimum extent permitted by law.</p>
        </section>
        <section class="help-section" aria-labelledby="legal-indemnity-title">
          <h2 id="legal-indemnity-title">5. Your Indemnity</h2>
          <p>You agree to defend, indemnify, and hold harmless KpiMinds LLC from any claims, damages, and expenses (including reasonable attorneys' fees) brought by a third party arising out of your use of the software, the actions of agents operating under your accounts and direction, or your violation of these terms or of the LICENSE.</p>
        </section>
        <section class="help-section" aria-labelledby="legal-thirdparty-title">
          <h2 id="legal-thirdparty-title">6. Third-Party Services</h2>
          <p>The Codex CLI, Claude Code CLI, and the OpenAI API are third-party services governed by their own terms and billed to your own accounts. KpiMinds LLC is not a party to those relationships and has no responsibility for those services, their output, their availability, or their charges.</p>
        </section>
        <section class="help-section" aria-labelledby="legal-general-title">
          <h2 id="legal-general-title">7. General</h2>
          <p><strong>Build attribution:</strong> projects created ("scaffolded") by this software include a visible BUILT_WITH.md file identifying NoMoreHappyPath as the build tool, with the creation time and a non-personal installation identifier. Adopted repositories are never written to. The project's code belongs to its owner; the stamp records only the tool.</p>
          <p>These terms are governed by the laws of the State of Texas, USA, and any dispute shall be brought exclusively in the state or federal courts located in Austin, Travis County, Texas — you consent to that jurisdiction and venue. They supplement the LICENSE (Business Source License 1.1); for license matters the LICENSE governs. If any provision is unenforceable, the remainder stands. The same text ships as the DISCLAIMER file beside the LICENSE. Contact: license@kpiminds.com.</p>
        </section>
      </div>
    </section>
  </main>

  <div id="status" class="notice" role="status" aria-live="polite" hidden></div>

  <dialog id="update-dialog" aria-labelledby="update-dialog-title"><div class="modal"><h2 id="update-dialog-title">Software update</h2><p>Version <strong id="update-installed">…</strong>. The check asks only this installation's own GitHub origin; nothing is sent anywhere else.</p><p class="notice" id="update-status" role="status" aria-live="polite"></p><p id="update-consent" hidden>Updating fast-forwards this installation and restarts the app. Your projects, boards, and settings are not touched.</p><div class="modal-actions"><a id="update-notes" class="button secondary" href="https://github.com/nomorehappypath/nomorehappypath/releases" target="_blank" rel="noopener" hidden>What changed</a><button type="button" class="button secondary" id="update-check">Check again</button><button type="button" class="button" id="update-apply" hidden>Update now</button><button type="button" class="button secondary" id="update-close">Close</button></div></div></dialog>

  <dialog id="create-dialog" aria-labelledby="create-title">
    <form class="modal" id="create-form" novalidate>
      <h2 id="create-title">New project</h2>
      <p class="modal-copy" id="create-copy">Create a dedicated workspace for your next idea.</p>
      <div class="field"><label for="project-name">Name</label><input id="project-name" name="name" required autocomplete="off"></div>
      <div class="field"><label for="project-description">Description</label><textarea id="project-description" name="description" rows="4" autocomplete="off" placeholder="Describe what this project is for and what matters about it."></textarea><p class="field-hint">This appears on the project card, so a short paragraph works well.</p></div>
      <div class="field">
        <label id="project-folder-label">Parent folder</label>
        <div class="folder-control"><button class="button secondary" type="button" id="project-folder-browse">Choose folder</button><output class="selected-path empty" id="project-folder-path">No folder selected</output></div>
        <p class="field-hint" id="project-folder-hint">Your project folder will be created inside the selected location.</p>
        <div class="folder-preview" id="project-folder-preview" hidden></div>
      </div>
      <p class="form-error" id="create-error" role="alert" aria-live="polite"></p>
      <div class="dialog-actions"><button class="button secondary" type="button" id="create-cancel">Cancel</button><button class="button" type="submit" id="create-save">Save project</button></div>
    </form>
  </dialog>

  <dialog id="repair-dialog" aria-labelledby="repair-title">
    <form class="modal" id="repair-form" novalidate>
      <h2 id="repair-title">Repair project folder</h2>
      <p class="modal-copy">Choose the folder where this project lives now. Saved harness data and project history will be preserved.</p>
      <div class="field"><label>Current folder</label><div class="selected-path" id="repair-current-path"></div></div>
      <div class="field"><label>New folder</label><div class="folder-control"><button class="button secondary" type="button" id="repair-folder-browse">Choose folder</button><output class="selected-path empty" id="repair-folder-path">No folder selected</output></div></div>
      <p class="form-error" id="repair-error" role="alert" aria-live="polite"></p>
      <div class="dialog-actions"><button class="button secondary" type="button" id="repair-cancel">Cancel</button><button class="button" type="submit" id="repair-save">Save folder</button></div>
    </form>
  </dialog>

  <dialog id="remove-dialog" aria-labelledby="remove-title">
    <form class="modal" id="remove-form">
      <h2 id="remove-title">Remove project?</h2>
      <p class="modal-copy">Remove <strong id="remove-project-name"></strong> from this list?</p>
      <p class="danger-copy">Only the registry entry is removed. The project folder, source files, harness data, and workspaces are never deleted.</p>
      <p class="form-error" id="remove-error" role="alert" aria-live="polite"></p>
      <div class="dialog-actions"><button class="button secondary" type="button" id="remove-cancel">Cancel</button><button class="button danger" type="submit" id="remove-confirm">Remove from list</button></div>
    </form>
  </dialog>

  <script>
    const q = (selector) => document.querySelector(selector);
    const esc = (value = '') => String(value).replace(/[&<>'"]/g, (char) => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[char]));
    const status = (message, tone = '') => {
      const node = q('#status');
      node.textContent = message || '';
      node.className = `notice ${tone}`.trim();
      node.hidden = !message;
    };
    const api = async (path, options) => {
      const response = await fetch(path, options);
      const value = await response.json();
      if (!response.ok) throw new Error(value.error || response.statusText);
      return value;
    };
    const postAction = (path) => api(path, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: '{}',
    });
    const when = (value) => {
      if (!value) return 'No activity yet';
      const date = new Date(value);
      if (Number.isNaN(date.getTime())) return 'Activity recorded';
      return new Intl.DateTimeFormat(undefined, {month:'short', day:'numeric', hour:'numeric', minute:'2-digit'}).format(date);
    };
    const row = (project) => {
      const workerStopped = Boolean(project.worker_error);
      const held = Boolean(project.control_plane_hold);
      const state = !project.health.ok || held ? 'unhealthy' : project.paused ? 'paused' : workerStopped ? 'stopped' : project.active ? 'active' : project.running ? 'running' : 'idle';
      const stateLabel = !project.health.ok ? 'Needs repair' : held ? 'Needs repair' : project.paused ? 'Paused' : workerStopped ? 'Board stopped' : project.active ? 'Open' : project.running ? 'Running' : 'Idle';
      const health = project.health.ok ? '' : `<span class="badge bad" title="${esc(project.health.reasons.join('; '))}">Needs repair</span>`;
      const stateTitle = workerStopped ? ` title="${esc(project.worker_error)}"` : '';
      const counts = project.task_counts;
      const agents = project.agent_counts || {total: 0};
      const description = project.description
        ? `<p class="description">${esc(project.description)}</p>`
        : '<p class="description empty-copy">No description added yet.</p>';
      // The owner's rule: ANY project opens at ANY time - paused, stopped,
      // active, or unhealthy - unless it was deleted. One button, one verb.
      // A paused project's Open resumes it; an active unpaused project's
      // Open goes straight to its board.
      const primary = project.active && !project.paused
        ? `<button class="button" data-act="view" data-id="${esc(project.id)}">Open Mission Control</button><button class="button secondary" data-act="pause" data-id="${esc(project.id)}">Pause project</button>`
        : `<button class="button" data-act="open" data-id="${esc(project.id)}">Open project</button>`;
      const remove = project.active ? '' : `<button class="button ghost" data-act="remove" data-id="${esc(project.id)}">Remove</button>`;
      return `<article class="project ${state}" data-project-id="${esc(project.id)}">
        <div>
          <div class="project-top"><h3>${esc(project.name)}</h3><span class="badges"><span class="badge ${state}"${stateTitle}>${stateLabel}</span><span class="badge kind">${esc(project.kind)}</span>${health}</span></div>
          ${description}
          <div class="metrics">
            <span class="metric"><strong>${counts.total}</strong> tasks</span>
            <span class="metric"><strong>${counts.passed}</strong> accepted</span>
            <span class="metric"><strong>${counts.open}</strong> in progress</span>
            <span class="metric"><strong>${agents.total || 0}</strong> active agents</span>
          </div>
          <div class="progress"><span class="progress-icon" aria-hidden="true">↗</span><span><strong>${project.latest_task ? esc(project.latest_task) + ' · ' : ''}</strong>${esc(project.control_plane_hold ? 'Needs repair: ' + project.control_plane_hold : project.latest_progress)}</span></div>
          <div class="path" title="${esc(project.code_root)}"><span aria-hidden="true">⌁</span><span>${esc(project.code_root)}</span></div>
        </div>
        <div class="project-side">
          <div class="last-active">Last activity<br><strong>${when(project.last_board_activity || project.last_active_at)}</strong></div>
          <div class="actions">${remove}${primary}</div>
        </div>
      </article>`;
    };
    const renderSummary = (projects) => {
      q('#summary-total').textContent = projects.length;
      q('#summary-active').textContent = projects.filter((project) => project.active || project.running).length;
      q('#summary-health').textContent = projects.filter((project) => !project.health.ok).length;
    };
    const settingRoles = [
      {id: 'delivery', label: 'Delivery Agent', copy: 'Builds, designs, tests, and prepares the owner’s work.'},
      {id: 'reviewer', label: 'Independent Reviewer', copy: 'Challenges completed work from a different CLI provider.'},
      {id: 'cto', label: 'CTO', copy: 'Monitors evidence, review gates, main health, and release.'},
    ];
    let settingsCatalog = null;
    const settingValue = (role) => {
      const choice = q(`[data-setting-model-choice="${role}"]`).value;
      return {
        provider: q(`[data-setting-provider="${role}"]`).value,
        model: choice === '__custom__' ? q(`[data-setting-model="${role}"]`).value.trim() : choice,
        effort: q(`[data-setting-effort="${role}"]`).value,
      };
    };
    const modelOptions = (provider, selected = '') => {
      const models = settingsCatalog?.provider_models?.[provider] || [];
      const current = models.includes(selected) ? selected : '__custom__';
      return models.map((model) => `<option value="${esc(model)}"${current === model ? ' selected' : ''}>${esc(model)}</option>`).join('')
        + `<option value="__custom__"${current === '__custom__' ? ' selected' : ''}>Custom model ID…</option>`;
    };
    const syncCustomModel = (role) => {
      const custom = q(`[data-setting-model-choice="${role}"]`).value === '__custom__';
      q(`[data-setting-custom="${role}"]`).hidden = !custom;
      if (!custom) q(`[data-setting-model="${role}"]`).value = '';
    };
    const effortOptions = (provider, selected = '') => Object.entries(settingsCatalog?.provider_efforts?.[provider] || {})
      .map(([value, label]) => `<option value="${esc(value)}"${value === selected ? ' selected' : ''}>${esc(label)}</option>`).join('');
    function renderOpenAI(value) {
      const state = value.openai || {};
      const badge = q('#openai-badge'), detail = q('#openai-detail'), result = q('#openai-result');
      const connected = Boolean(state.connected);
      badge.className = `badge ${connected ? 'running' : 'bad'}`;
      badge.textContent = connected ? 'Connected' : 'Not connected';
      detail.textContent = state.unusable
        ? 'The key on this computer cannot be used. Save it again below to replace it.'
        : (state.configured
            ? `Key ${state.masked}${state.source === 'environment' ? ' (from this computer’s environment)' : ''}${connected && state.verified_at ? ` · checked ${new Date(state.verified_at).toLocaleString()}` : ''}`
            : 'No key saved yet. Chat is switched off until you add one.');
      q('#openai-test').hidden = !state.configured;
      q('#openai-remove').hidden = !(state.source === 'manager_secret' || state.unusable);
      q('#openai-save').textContent = state.configured || state.unusable ? 'Replace and connect' : 'Save and connect';
      result.className = `connection-result ${connected ? 'ok' : (state.configured || state.unusable ? 'bad' : '')}`;
      result.textContent = connected
        ? state.message
        : (state.configured || state.unusable
            ? `${state.message} Chat stays switched off until this key connects.`
            : `Paste the key from your OpenAI account. It is checked against ${value.chat_model || 'the chat model'} before it is saved, and is stored on this computer only, readable by you alone.`);
    }
    async function openaiAction(button, work) {
      const buttons = ['#openai-save', '#openai-test', '#openai-remove'].map(q);
      const result = q('#openai-result');
      buttons.forEach((item) => item.disabled = true);
      result.className = 'connection-result';
      result.textContent = button === 'remove' ? 'Removing the key…' : 'Checking this key with OpenAI…';
      try {
        renderSettings(await work());
        if (button === 'remove') { status('OpenAI API key removed. Project chat is switched off.'); }
      } catch (error) {
        q('#openai-result').className = 'connection-result bad';
        q('#openai-result').textContent = error.message;
      } finally {
        ['#openai-save', '#openai-test', '#openai-remove'].forEach((selector) => q(selector).disabled = false);
      }
    }
    function renderSettings(value) {
      settingsCatalog = value;
      renderOpenAI(value);
      const providers = Object.entries(value.providers || {});
      q('#settings-error').textContent = value.error || '';
      q('#settings-fields').innerHTML = settingRoles.map((role) => {
        const selected = value.agent_settings?.[role.id] || {};
        const result = value.connectivity?.[selected.provider];
        const resultClass = result ? (result.ok ? 'ok' : 'bad') : '';
        const resultText = result?.message || 'Not tested yet. This checks the local CLI and launch flags without starting an agent.';
        return `<section class="settings-card" data-settings-role="${role.id}">
          <div><h2>${esc(role.label)}</h2><p class="settings-card-copy">${esc(role.copy)}</p></div>
          <div class="field"><label for="provider-${role.id}">CLI provider</label><select id="provider-${role.id}" data-setting-provider="${role.id}">${providers.map(([id, provider]) => `<option value="${esc(id)}"${id === selected.provider ? ' selected' : ''}>${esc(provider.label)}</option>`).join('')}</select></div>
          <div class="field"><label for="model-${role.id}">Model</label><select id="model-${role.id}" data-setting-model-choice="${role.id}">${modelOptions(selected.provider, selected.model)}</select></div>
          <div class="field" data-setting-custom="${role.id}"${(value.provider_models?.[selected.provider] || []).includes(selected.model) ? ' hidden' : ''}><label for="custom-model-${role.id}">Custom model ID</label><input id="custom-model-${role.id}" data-setting-model="${role.id}" value="${(value.provider_models?.[selected.provider] || []).includes(selected.model) ? '' : esc(selected.model || '')}" autocomplete="off" placeholder="Provider model ID"></div>
          <div class="field"><label for="effort-${role.id}">Reasoning effort</label><select id="effort-${role.id}" data-setting-effort="${role.id}">${effortOptions(selected.provider, selected.effort)}</select></div>
          <button class="button secondary" type="button" data-settings-test="${role.id}">Test ${esc(selected.provider === 'claude' ? 'Claude' : 'Codex')} connection</button>
          <div class="connection-result ${resultClass}" data-settings-result="${role.id}" role="status" aria-live="polite">${esc(resultText)}</div>
        </section>`;
      }).join('');
      document.querySelectorAll('[data-setting-model-choice]').forEach((select) => select.onchange = () => syncCustomModel(select.dataset.settingModelChoice));
      document.querySelectorAll('[data-setting-provider]').forEach((select) => select.onchange = () => {
        const role = select.dataset.settingProvider;
        const models = settingsCatalog.provider_models?.[select.value] || [];
        q(`[data-setting-model-choice="${role}"]`).innerHTML = modelOptions(select.value, models[0] || '');
        syncCustomModel(role);
        q(`[data-setting-effort="${role}"]`).innerHTML = effortOptions(select.value);
        q(`[data-settings-test="${role}"]`).textContent = `Test ${select.value === 'claude' ? 'Claude' : 'Codex'} connection`;
        const result = q(`[data-settings-result="${role}"]`);
        result.className = 'connection-result';
        result.textContent = 'Settings changed. Test this provider before saving if you want to verify local access.';
      });
      document.querySelectorAll('[data-settings-test]').forEach((button) => button.onclick = async () => {
        const role = button.dataset.settingsTest;
        const result = q(`[data-settings-result="${role}"]`);
        button.disabled = true;
        result.className = 'connection-result';
        result.textContent = 'Testing the local CLI and flags…';
        try {
          const tested = await api('/api/settings/connect', {method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(settingValue(role))});
          result.className = 'connection-result ok';
          result.textContent = tested.message;
        } catch (error) {
          result.className = 'connection-result bad';
          result.textContent = error.message;
        } finally {
          button.disabled = false;
        }
      });
    }
    async function loadSettings() {
      try { renderSettings(await api('/api/settings')); }
      catch (error) { q('#settings-error').textContent = `Could not load settings: ${error.message}`; }
    }
    q('#settings-form').addEventListener('submit', async (event) => {
      event.preventDefault();
      const save = q('#settings-save');
      const agentSettings = Object.fromEntries(settingRoles.map((role) => [role.id, settingValue(role.id)]));
      q('#settings-error').textContent = '';
      save.disabled = true;
      try {
        renderSettings(await api('/api/settings', {method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({agent_settings: agentSettings})}));
        status('Global agent settings saved. They will apply to new sessions in every project.');
      } catch (error) {
        q('#settings-error').textContent = error.message;
      } finally {
        save.disabled = false;
      }
    });
    if (q('#openai-card')) {
      q('#openai-save').onclick = () => openaiAction('save', async () => {
        const input = q('#openai-key'), key = input.value.trim();
        if (!key) throw Error('Paste your OpenAI API key first.');
        const updated = await api('/api/settings/openai-key', {method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({key})});
        input.value = '';
        status('OpenAI API key saved and connected. Project chat is available.');
        return updated;
      });
      q('#openai-test').onclick = () => openaiAction('test', () => api('/api/settings/openai-key/test', {method: 'POST', headers: {'Content-Type':'application/json'}, body: '{}'}));
      q('#openai-remove').onclick = () => openaiAction('remove', () => api('/api/settings/openai-key', {method: 'DELETE'}));
    }
    async function loadVersion() {
      try {
        const version = (await api('/api/version')).version;
        q('#app-version').textContent = version;
        q('#update-installed').textContent = version;
      } catch (error) { q('#app-version').textContent = 'version'; }
    }
    loadVersion();
    let pendingUpdate = null;
    async function runUpdateCheck() {
      const status = q('#update-status'), apply = q('#update-apply'), notes = q('#update-notes'), consent = q('#update-consent');
      status.textContent = 'Checking this installation\u2019s origin\u2026';
      apply.hidden = true; notes.hidden = true; consent.hidden = true; pendingUpdate = null;
      try {
        const result = await api('/api/update/check', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}'});
        status.textContent = result.message;
        if (result.update_available) {
          pendingUpdate = result;
          notes.href = result.release_notes_url; notes.hidden = false;
          const anyOpen = [...projectsById.values()].some((project) => project.active && !project.paused);
          if (anyOpen) status.textContent = result.message + ' Close or pause your open project to apply it.';
          else { apply.hidden = false; consent.hidden = false; }
        }
      } catch (error) { status.textContent = error.message; }
    }
    q('#update-open').onclick = () => { q('#update-dialog').showModal(); runUpdateCheck(); };
    q('#update-close').onclick = () => q('#update-dialog').close();
    q('#update-check').onclick = runUpdateCheck;
    q('#update-apply').onclick = async () => {
      if (!pendingUpdate) return;
      const status = q('#update-status');
      q('#update-apply').disabled = true;
      status.textContent = 'Updating and restarting\u2026 this page reloads when the app is back.';
      try {
        await api('/api/update/apply', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}'});
        const poll = setInterval(async () => {
          try { await api('/api/version'); clearInterval(poll); window.location.reload(); } catch (error) {}
        }, 1500);
      } catch (error) { status.textContent = error.message; q('#update-apply').disabled = false; }
    };
    let projectsById = new Map();
    const loadedPageVersion = '__PAGE_VERSION__';
    async function refresh() {
      try {
        const value = await api('/api/projects');
        if (value.page_version && value.page_version !== loadedPageVersion) { window.location.reload(); return; }
        projectsById = new Map(value.projects.map((project) => [project.id, project]));
        const container = q('#projects');
        container.setAttribute('aria-busy', 'false');
        container.innerHTML = value.projects.length
          ? value.projects.map(row).join('')
          : '<div class="empty"><strong>No projects yet</strong>Create a new project or adopt an existing folder to get started.</div>';
        renderSummary(value.projects);
        const notice = q('#codex-notice');
        notice.hidden = !value.codex_notice;
        notice.textContent = value.codex_notice || '';
      } catch (error) {
        q('#projects').innerHTML = '<div class="empty"><strong>Projects could not be loaded</strong>The manager will keep trying. Check the connection and try again.</div>';
        status(`Could not load projects: ${error.message}`, 'error');
      }
    }

    async function showPage(requestedPage, updateUrl = false) {
      const page = ['projects', 'settings', 'help', 'legal'].includes(requestedPage) ? requestedPage : 'projects';
      document.querySelectorAll('button[data-page]').forEach((button) => {
        if (button.dataset.page === page) button.setAttribute('aria-current', 'page');
        else button.removeAttribute('aria-current');
      });
      q('#projects-page').hidden = page !== 'projects';
      q('#settings-page').hidden = page !== 'settings';
      q('#help-page').hidden = page !== 'help';
      q('#legal-page').hidden = page !== 'legal';
      if (updateUrl) {
        const target = page === 'projects' ? window.location.pathname : `${window.location.pathname}?page=${page}`;
        window.history.replaceState({page}, '', target);
      }
      if (page === 'settings') await loadSettings();
    }

    document.addEventListener('click', async (event) => {
      const nav = event.target.closest('button[data-page]');
      if (nav) {
        await showPage(nav.dataset.page, true);
        return;
      }
      const button = event.target.closest('button[data-act]');
      if (!button) return;
      const id = button.dataset.id;
      try {
        button.disabled = true;
        if (button.dataset.act === 'open') {
          status('Opening the project and restoring its saved board…');
          const value = await postAction(`/api/projects/${id}/open`);
          status('Project ready. Opening Mission Control…');
          setTimeout(() => { window.location = value.board_url; }, 800);
        }
        if (button.dataset.act === 'view') {
          const boardUrl = projectsById.get(id)?.board_url;
          if (!boardUrl) throw new Error('The open project does not have a verified Mission Control route.');
          window.location = boardUrl;
        }
        if (button.dataset.act === 'resume') {
          status('Resuming from the exact saved gate…');
          const value = await postAction(`/api/projects/${id}/resume`);
          status('Project resumed. Opening Mission Control…');
          setTimeout(() => { window.location = value.board_url; }, 800);
        }
        if (button.dataset.act === 'pause') {
          status('Pausing agents after their in-flight board writes finish…');
          await postAction(`/api/projects/${id}/pause`);
          status('Project paused safely. Saved work is now read-only.');
          await refresh();
        }
        if (button.dataset.act === 'repair') {
          openRepair(projectsById.get(id));
        }
        if (button.dataset.act === 'remove') {
          openRemove(projectsById.get(id));
        }
      } catch (error) {
        status(error.message, 'error');
      } finally {
        button.disabled = false;
      }
    });

    const createDialog = q('#create-dialog');
    const repairDialog = q('#repair-dialog');
    const removeDialog = q('#remove-dialog');
    let adopting = false;
    let createFolder = '';
    let repairProject = null;
    let repairFolder = '';
    let removeProject = null;

    const folderName = (name) => String(name).trim().replace(/[^A-Za-z0-9._-]+/g, '-').replace(/^[-._]+|[-._]+$/g, '').toLowerCase();
    const setPath = (selector, value) => {
      const output = q(selector);
      output.textContent = value || 'No folder selected';
      output.classList.toggle('empty', !value);
      output.title = value || '';
    };
    const updateFolderPreview = () => {
      const preview = q('#project-folder-preview');
      if (adopting || !createFolder || !folderName(q('#project-name').value)) {
        preview.hidden = true;
        preview.textContent = '';
        return;
      }
      preview.hidden = false;
      preview.textContent = `New project folder: ${createFolder.replace(/\/$/, '')}/${folderName(q('#project-name').value)}`;
    };
    async function browseFolder(purpose, errorSelector) {
      const error = q(errorSelector);
      error.textContent = '';
      try {
        const value = await api('/api/folders/browse', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({purpose})});
        return value.path || '';
      } catch (problem) {
        error.textContent = problem.message;
        return '';
      }
    }
    function openCreate(mode) {
      adopting = mode === 'adopted';
      createFolder = '';
      q('#create-form').reset();
      q('#create-error').textContent = '';
      setPath('#project-folder-path', '');
      q('#create-title').textContent = adopting ? 'Adopt existing project' : 'New project';
      q('#create-copy').textContent = adopting
        ? 'Connect an existing repository while keeping all harness data safely outside it.'
        : 'Create a self-contained project with its own saved board and history.';
      q('#project-folder-label').textContent = adopting ? 'Existing project folder' : 'Parent folder';
      q('#project-folder-hint').textContent = adopting
        ? 'The repository stays untouched; NoMoreHappyPath stores its board and workspaces in manager-owned folders.'
        : 'NoMoreHappyPath creates a folder from the project name inside the selected location.';
      q('#project-folder-browse').textContent = adopting ? 'Choose project folder' : 'Choose parent folder';
      updateFolderPreview();
      createDialog.showModal();
      q('#project-name').focus();
    }
    q('#new-btn').onclick = () => openCreate('scaffold');
    q('#adopt-btn').onclick = () => openCreate('adopted');
    q('#create-cancel').onclick = () => createDialog.close();
    q('#project-name').addEventListener('input', updateFolderPreview);
    q('#project-folder-browse').onclick = async () => {
      const selected = await browseFolder(adopting ? 'adopt-project' : 'new-parent', '#create-error');
      if (!selected) return;
      createFolder = selected;
      setPath('#project-folder-path', selected);
      if (adopting && !q('#project-name').value.trim()) {
        q('#project-name').value = selected.split('/').filter(Boolean).pop() || '';
      }
      updateFolderPreview();
    };
    q('#create-form').addEventListener('submit', async (event) => {
      event.preventDefault();
      const error = q('#create-error');
      const save = q('#create-save');
      const name = q('#project-name').value.trim();
      error.textContent = '';
      if (!name) { error.textContent = 'Add a project name.'; q('#project-name').focus(); return; }
      if (!createFolder) { error.textContent = adopting ? 'Choose the existing project folder.' : 'Choose a parent folder.'; q('#project-folder-browse').focus(); return; }
      const data = {name, description: q('#project-description').value.trim(), kind: adopting ? 'adopted' : 'scaffold'};
      if (adopting) data.code_root = createFolder;
      else data.parent_root = createFolder;
      save.disabled = true;
      try {
        await api('/api/projects', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(data)});
        createDialog.close();
        status(adopting ? 'Existing project adopted. Its repository was not changed.' : 'New project saved.');
        await refresh();
      } catch (problem) {
        error.textContent = problem.message;
      } finally {
        save.disabled = false;
      }
    });

    function openRepair(project) {
      if (!project) return;
      repairProject = project;
      repairFolder = '';
      q('#repair-error').textContent = '';
      q('#repair-current-path').textContent = project.code_root;
      q('#repair-current-path').title = project.code_root;
      setPath('#repair-folder-path', '');
      repairDialog.showModal();
      q('#repair-folder-browse').focus();
    }
    q('#repair-cancel').onclick = () => repairDialog.close();
    q('#repair-folder-browse').onclick = async () => {
      const selected = await browseFolder('repair-project', '#repair-error');
      if (!selected) return;
      repairFolder = selected;
      setPath('#repair-folder-path', selected);
    };
    q('#repair-form').addEventListener('submit', async (event) => {
      event.preventDefault();
      const error = q('#repair-error');
      if (!repairFolder) { error.textContent = 'Choose the project folder.'; return; }
      q('#repair-save').disabled = true;
      try {
        await api(`/api/projects/${repairProject.id}/repair`, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({code_root: repairFolder})});
        repairDialog.close();
        status('Project folder repaired. Saved work is still connected.');
        await refresh();
      } catch (problem) {
        error.textContent = problem.message;
      } finally {
        q('#repair-save').disabled = false;
      }
    });

    function openRemove(project) {
      if (!project) return;
      removeProject = project;
      q('#remove-error').textContent = '';
      q('#remove-project-name').textContent = project.name;
      removeDialog.showModal();
      q('#remove-cancel').focus();
    }
    q('#remove-cancel').onclick = () => removeDialog.close();
    q('#remove-form').addEventListener('submit', async (event) => {
      event.preventDefault();
      q('#remove-confirm').disabled = true;
      q('#remove-error').textContent = '';
      try {
        await api(`/api/projects/${removeProject.id}`, {method: 'DELETE'});
        removeDialog.close();
        status('Project removed from the list. Its folders were not changed.');
        await refresh();
      } catch (problem) {
        q('#remove-error').textContent = problem.message;
      } finally {
        q('#remove-confirm').disabled = false;
      }
    });

    showPage(new URLSearchParams(window.location.search).get('page'));
    refresh();
    setInterval(refresh, 4000);
  </script>
</body>
</html>'''
