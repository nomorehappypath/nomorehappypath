# NoMoreHappyPath

**Build commercial software with AI agents, from a plain-English idea toward a release-ready product, with the governance built in.**

NoMoreHappyPath (nomorehappypath.com) is an open-source delivery system for AI-built software. You describe what you want. A team of AI agents plans it, builds it, tests it and reviews it, inside a governed project board and a local control room (Mission Control). Nothing moves forward without recorded evidence, and "done" stays your word. It runs real coding agents on your own accounts: Codex CLI, Claude Code CLI and, optionally, Gemini through Google's Antigravity CLI.

## What it is

- **A team of agents.** A builder, an independent reviewer from a different AI company, and a CTO that watches the board. Claude, Codex or Gemini can take any role, and the reviewer is never from the builder's company.
- **A contract before any code.** The agents propose the requirements for your approval (you press Go ahead, or change them), then plan the work and an evidence contract before anything is built.
- **A board that agents cannot directly edit.** Tasks, contracts, reviews and their backups are written only by the platform's own software; agents change them only through its governed commands. A task advances only with evidence: executed tests, failure paths, and a walk through the finished product (in a real browser when the app has a screen).
- **Security and quality by default.** Agents are required to build in, and the reviewer checks where they apply: secrets kept out of the code, user input checked, logins that hash passwords and slow down repeated guesses, rate-limited sending endpoints, no internal errors shown to users, and a dependency check before delivery. Every app also gets one central error catch-all. A per-task **Enhanced security** option adds a threat model, security tests and a controls register (security assessment and audit preparation, not a certification or a penetration test).
- **A write sandbox around every agent**, enforced by the operating system on macOS and Linux: agents can write only to the project, their own CLI state, caches and temporary folders, not to your other files.
- **Self-healing.** A stalled or unexpectedly ended agent session is restarted for you (up to three times an hour), sign-in problems are flagged in plain words, and agents resume their saved conversation after a pause or stop.
- **Mission Control.** A local web app to watch every project, preview the finished app, ask questions about your project (project chat uses your own OpenAI API key), accept the result, and update itself with one click.
- **Over 2,000 automated tests**, including headless-browser tests that verify what the owner actually sees rendered.

## Why

AI is excellent at the happy path: the demo works. The failed login, the empty screen and the raw error message appear in front of customers. NoMoreHappyPath exists so software built by AI is built, checked and accepted like a product, not shown like a demo.

Website: https://nomorehappypath.com . Releases: https://github.com/nomorehappypath/nomorehappypath/releases

## What you need

Be aware before you clone: the platform itself is free to evaluate, but the
AI agents it governs run on **your own accounts**.

| Requirement | Where to get it | Cost |
|---|---|---|
| **macOS 13+** with Python 3.9+, or **Linux** (tested on Ubuntu 24.04) with Python 3.9+, git, tmux and bubblewrap | `python3 --version` to check; on Ubuntu `sudo apt install git tmux bubblewrap` | free |
| **Codex CLI** (any role) | `npm install -g @openai/codex` — sign in with your OpenAI account | your OpenAI/ChatGPT plan |
| **Claude Code CLI** (any role) | https://claude.com/claude-code — sign in with your Anthropic account | your Anthropic plan |
| **Antigravity CLI** (`agy`, optional — Gemini for any role) | https://antigravity.google/cli — sign in with your Google account; the agent asks once more inside its own window | a free Google account has a weekly quota; Google AI Pro/Ultra raise it (billing: one.google.com) |
| **OpenAI API key** (project chat) | https://platform.openai.com/api-keys — entered under Settings, verified before storage, kept in a file only you can read | pay-per-use, cents |

You can start with just one CLI, but cross-vendor review, where an agent
from a different vendor verifies the work, needs **any two of Codex, Claude Code
and Gemini**, so the reviewer is never the builder's vendor. No other
dependencies: the platform is pure Python standard library. Windows is not
supported directly; on Windows, run it inside a Linux VM (see below).

**On Linux** the platform is built for a server with no desktop. The
installer sets up a per-user systemd service and tells you if lingering is
off (without it the service stops when you log out; `sudo loginctl
enable-linger <user>` fixes that). Each agent runs in its own named tmux
session. Click **View agent** beside Delivery, Reviewer or CTO to see live
output and type to that same session in your browser, including from Windows.
Closing the browser view leaves the agent running. On macOS, closing a CLI
window also leaves its agent running. **View agent** focuses the existing
Terminal window or reopens a window attached to that same session.

Bubblewrap is required. The installer and every agent launch run a real sandbox
probe; an installed but blocked sandbox refuses launch with setup guidance.
On Ubuntu, if Bubblewrap reports `setting up uid map: Permission denied`, install
and load the distribution's AppArmor profile:

```sh
sudo apt install apparmor-profiles
sudo install -m 644 /usr/share/apparmor/extra-profiles/bwrap-userns-restrict /etc/apparmor.d/bwrap-userns-restrict
sudo apparmor_parser -r /etc/apparmor.d/bwrap-userns-restrict
bash install.sh --check
```

Keep AppArmor and the system-wide user namespace restriction enabled. Other
Linux distributions should use their supported Bubblewrap policy. A failing
probe never permits agents to run without their required sandbox.

To do all of this in one step, including installing tmux and Bubblewrap, run
`sudo bash scripts/linux_enable_sandbox.sh` once in Linux. The app never runs it;
it uses Ubuntu's own profile and leaves the system-wide restriction on. If an
agent stops after it starts, its tmux window stays open until you press Enter,
so you can read why.

On Linux, **Choose folder** opens a folder dialog like the macOS one: places on the left (Home, Computer and any connected drive), the folders inside the current one, **New Folder**, **Cancel** and **Choose**. The box at the top also accepts a typed path.
From Windows, the dialog shows the folders of the Linux server. macOS retains its native
folder picker.

**First time an agent opens in a project:** stay at your computer. Claude may ask *Do you trust this folder?* (choose *Yes, I trust this folder*) and Codex may ask the same (choose *Trust and continue*). Use **View agent** to answer it. Mission Control shows that agent as waiting for you until you do. Gemini does not ask (the project you opened is recorded as trusted for it); it asks you to sign in once inside its own window, and Mission Control says so when it does.

**Linux CLI setup (including a VM on Windows):** install **the CLIs you use (Codex CLI, Claude Code CLI,
and optionally Antigravity CLI for Gemini) inside Linux** and sign in to each using the account that runs
NoMoreHappyPath. Make `codex`, `claude` and `agy` available in `PATH` for **all shells**
used by that account: login and non-interactive shells, tmux sessions, and the
app's systemd service. Shell aliases and a PATH exported in just one terminal
are insufficient. Check `codex --version`, `claude --version` and `agy --version` from a fresh
shell before installing the service. Run `bash install.sh --check` under the
same Linux account to confirm the app can find the CLIs for its service.

### Claude agent authentication

Harness-launched Claude agents require a dedicated one-year token created with
`claude setup-token`. A normal interactive `/login` is not enough for these
sandboxed agents. The harness checks the token with a real Claude request before
launch; a missing or rejected token produces an owner-action card.

On **macOS**, save the token as the password of a login Keychain generic password
item named `claude-cli-oauth`, using Keychain Access. Unlock the login Keychain.
The trusted manager reads that item before launch. Agents receive the token only
through an authenticated, one-use local socket and cannot access the Keychain or
shared Claude login files. If you already export `CLAUDE_CODE_OAUTH_TOKEN` into the
manager's environment, that explicit environment value takes precedence.

On **Linux**, provide the token to the user service in memory. Run these commands
in your own Bash terminal; the prompt hides your input and keeps it out of shell
history and files:

```bash
claude setup-token
read -rsp 'Claude setup-token: ' CLAUDE_CODE_OAUTH_TOKEN; printf '\n'
export CLAUDE_CODE_OAUTH_TOKEN
systemctl --user import-environment CLAUDE_CODE_OAUTH_TOKEN
unset CLAUDE_CODE_OAUTH_TOKEN
```

Safely pause your project before restarting the harness user service, then retry
the agent. Repeat the import after reboot. For foreground use, export the token
before starting the harness instead of importing it into systemd. Do not store
the value in unit files, shell startup files, project settings, or the board.
Never run `/login` inside a harness agent to repair a missing or expired setup-token;
generate a replacement in your own terminal and follow the owner-action card.

### Gemini agent setup (optional)

Gemini runs through Google's Antigravity CLI (`agy`) on your own Google account. No API key is used or accepted.

1. **Install** (macOS or Linux, same command): `curl -fsSL https://antigravity.google/cli | bash`. It puts `agy` in `~/.local/bin`; make sure that folder is in `PATH`
   for every shell (see the Linux CLI setup above). Check with `agy --version`.
2. **Sign in once, in your own terminal:** run `agy`, choose *Google OAuth*, open the link it prints, approve with your Google account and paste the code it shows back
   into the terminal. This also works over SSH or in a Linux VM. Then it asks you to pick a colour scheme and shows **Terms of Service & Data Use** with one
   checkbox about letting Google use your interactions data. That is your own choice: the app never answers it for you, and leaving it ticked or unticked both work.
   Quit (`Ctrl+C`) after the folder-trust question; the app records trust for the projects it opens, so agents never ask.
3. **Plan:** a free Google account has a small weekly quota and agents run out of it fast; a Google AI Pro or Ultra plan is recommended
   (billing: one.google.com). When the quota is used up, Mission Control says so in plain words and for how long.
4. **In the app:** open **Settings**, choose *Gemini (Antigravity CLI)* for any role, then click **Test Gemini connection**. It sends one real word to Gemini from inside
   the agents' sandbox, on your account, and says what it found. Gemini is greyed out in Settings, with the reason, until `agy` is found and proven.
5. **First launch:** each agent window may ask you to sign in once more. The agent's sandbox keeps its own copy of the sign-in and does not use your Keychain or your
   normal sign-in files. Mission Control shows the agent as waiting for you until you do. On Linux, do this under the same account that runs the app.

Agents run with `--dangerously-skip-permissions`, like the other providers, but inside the same write sandbox: they can write only to the project, the task workspace and
Gemini's own state and cache folders; your `~/.gemini/antigravity-cli/settings.json` and `~/.gemini/config` stay protected. Antigravity CLI 1.x is what was tested;
`bash install.sh --check` reports whether `agy` is found.

## Install

```bash
git clone https://github.com/nomorehappypath/nomorehappypath.git
cd nomorehappypath && bash install.sh
```

The installer checks every requirement above, tells you plainly what is
missing and where to get it, then either installs the auto-start service or
runs once in the foreground — your choice. `bash install.sh --uninstall`
removes the service; `bash scripts/stop_all.sh` stops every process of this
installation (and only this one) — `--list` shows what it would stop. Or skip the installer entirely:

```bash
python3 harness/project_manager.py   # opens Mission Control on 127.0.0.1:8740
```

**First run:** open **Settings** and add your OpenAI key under *Project chat
key* (Save and connect). Then **New project** or **Adopt existing** to point
the agents at a folder — read *Help → Your Responsibility* before choosing
it. Start all three agent roles from Mission Control and give your first
direction in the Delivery agent's terminal window.

## Status

Active development. The concept — governance as the product, agents as
interchangeable labor — is the point; the code is how we prove it works.

## FAQ

**Is this for real, serious products, or for toys?**
Serious products - that is the design center. This platform is for
building software that might carry revenue, clients, and your name -
everything the agents build in your folder is yours.

**Why not just Claude Code?**
Claude Code is a superb builder — this platform launches it as one. But when
the same assistant builds and then verifies, one context window is grading
its own plan; models agree with their own prior reasoning. Here verification
is a separate Codex process with its own directive and a clean context, and
its verdict is recorded on the board as written — the builder cannot alter
or soften it.

**Why not just Codex?**
Same argument, mirrored. Codex builds fast; asking Codex (or any sibling of
the model that wrote the code) to certify that work is the same model family
grading itself. Here Codex's work is verified by a Claude process — a
reviewer whose maker has no stake in the builder looking good.

**Claude Code can call Codex — isn't that cross-vendor review?**
No. In that setup the builder writes the judge's brief, runs the call,
interprets the output, and summarizes the verdict back to you: the defendant
hires the judge. Here neither agent manages the other. Each runs in its own
process with its own instructions, and verdicts land on the board through a
separate reviewer channel; the builder cannot alter or soften them through
the app.

**What does the CTO role add?**
The builder and the reviewer argue about the code; the CTO watches the
process. It is a third agent whose only job is management: it reconciles the
board against git and the running services, catches stalled work and claims
that lack evidence, keeps the review queue honest, and tells you — in plain
language — the one thing that actually needs you. Coding assistants give you
labor; a reviewer gives you judgment; the CTO is the layer that makes the
whole thing run unattended without drifting into fiction.

**Who decides something is "done"?**
Not a model. The board is a state machine in ordinary software: a task
advances only with recorded evidence — executed tests, failure paths,
rendered-screen checks, a review verdict, and finally your acceptance.
Prompts are suggestions; gates are law.

**Where does my code go?**
Nowhere new. Everything runs on your Mac; the agents talk to OpenAI and
Anthropic exactly as those CLIs always do, on your accounts. Nothing is sent
to us — there is no us-server at all.

**What does it cost to run?**
Think of it as hiring a governed engineering team, not buying an app. The
agents work on your own OpenAI, Anthropic and Google plans, and a serious build -
with adversarial reviews, scenario tests, and failure-path evidence - can
run into the hundreds or a few thousand dollars of model spend. Measure
that against what the software is for: a product that will
generate revenue, or a contract build that would cost tens of thousands
with an agency. The reviews are where the money goes, and they are the
point - you are paying for the right to trust the result.

**What if the two vendors disagree?**
That is the product working. A FAIL verdict arrives with executed evidence
and named causes; the builder agent must fix the whole class and resubmit.

**What happens when my account runs out of credit?**
The app does not crash. The agent's terminal shows the provider's error,
Settings → Test connection names the account that needs topping up, and the
board keeps every record; top up and continue the same task.

**When should I NOT use it?**
Quick scripts, exploration, one-off experiments — use a plain assistant
there; governance would be overhead without a payoff. This platform is
built for the other case: a product, a client deliverable, revenue
software — anything where being wrong costs real money and the result has
to hold up under someone else's scrutiny. Rule of thumb: use an assistant
when being wrong costs a shrug; use NoMoreHappyPath when being wrong costs
a client, a weekend, or your reputation.

**Can I run it inside a virtual machine?**
Yes, and a Linux VM or server is the simplest way to do it. Install on a
headless Ubuntu guest (or a cloud box), keep the projects inside it, and take
a snapshot before the first run; that snapshot lets you revert the whole
environment, not just a Git commit. On an Apple Silicon Mac you can instead
run the agents inside a macOS virtual machine: tools such as VirtualBuddy,
UTM, and Parallels can create macOS guests on Apple Silicon Macs. This is
optional: running directly on your Mac is normal too. Either way, point the
agents at the exact project folder and keep it in version control. Practical
limit: Apple allows macOS guests only on Apple hardware, so a macOS VM is not
a workaround for running on a Windows PC; a Linux VM is.

## Your responsibility

Using this software is entirely at your own risk and responsibility; it is
provided as-is with no warranty (see LICENSE). The CLI agents can do whatever
they need to do inside the project folder you choose — create, edit, delete,
and run code — so choose that folder carefully, keep it under version control,
and never point the agents at data you cannot afford to lose. Agent usage is
billed to your own OpenAI/Anthropic/Google accounts. A Gemini agent's CLI can send what the agent
reaches (not only the project folder) to Google, under Google's terms and the data-use choice you made when
you signed in. NoMoreHappyPath is independent and not affiliated with or endorsed by OpenAI,
Anthropic or Google; their names are trademarks of their owners.

Bugs and defects in the app, and in what its agents build for you, are not the
responsibility of KpiMinds LLC to the extent applicable law allows: the license's own no-warranty and
limitation-of-liability terms apply (see LICENSE, sections 7 and 8). The license covers this software,
not the projects you build with it. [DISCLAIMER.md](DISCLAIMER.md) is an informational
safety notice about what autonomous agents can do; it adds no conditions.

## License

Open source under the **Apache License, Version 2.0** (see LICENSE). You may
use, modify and distribute it, including commercially, under the terms of
that license; it comes with no warranty (LICENSE, section 7). Contact:
**license@kpiminds.com**.

Copyright 2026 KpiMinds LLC.
