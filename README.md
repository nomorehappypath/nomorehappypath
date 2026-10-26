# NoMoreHappyPath

**Build commercial software with AI agents, from a plain-English idea toward a release-ready product, with the governance built in.**

NoMoreHappyPath (nomorehappypath.com) is an open-source delivery system for AI-built software. You describe what you want. A team of AI agents plans it, builds it, tests it and reviews it, inside a governed project board and a local control room (Mission Control). Nothing moves forward without recorded evidence, and "done" stays your word. It runs real coding agents on your own accounts: Codex CLI, Claude Code CLI and, optionally, Gemini through Google's Antigravity CLI.

## See it working

![Mission Control: the project list](https://nomorehappypath.com/assets/readme-projects.png)
*Mission Control lists every project. This one is a commercial marketing app built and maintained with NoMoreHappyPath.*

![Every step the studio ran, including cross-vendor review](https://nomorehappypath.com/assets/readme-studio-steps.png)
*One task, step by step: checking the brief, experts working, a synthesized strategy, and an independent review by the other AI company.*

![Mission Control: a task ready for the owner's test](https://nomorehappypath.com/assets/shot-ready.png)
*A small weather app built from one request: review passes recorded, then "ready for your test".*

![How a task becomes accepted work: twenty steps in six phases](https://nomorehappypath.com/assets/process-infographic.png)
*Twenty steps in six phases. You describe the task and approve a plain-words contract; the reviewer from the other AI company writes the tests first; the board locks them and checks they fail on the empty project; the builder builds and proves its scenarios; the board and reviewer prove it works in bounded rounds; two safety nets retry once on a model you choose in Settings, otherwise "could not finish"; you review and accept.*

![Why every test must fail first](https://nomorehappypath.com/assets/red-proof.png)
*A test that passes on nothing proves nothing, so it is flagged "did not distinguish" and never counts. This shows a test can fail on an empty project, not that it covers every requirement.*

![Three roles, each filled by Codex, Claude or Gemini](https://nomorehappypath.com/assets/readme-roles.png)
*Builder, independent reviewer and CTO can each be Codex, Claude or Gemini. Only the platform software writes the project board; agents change it through governed commands, and the reviewer is never from the builder's company.*

![A commercial marketing app built with it: expert chats](https://nomorehappypath.com/assets/readme-experts.png)
*A commercial marketing app built with it: chatting with the expert agents.*

![Choosing which AI company builds and which reviews](https://nomorehappypath.com/assets/readme-vendors.png)
*Settings: choose which AI company builds and which one reviews.*

Demo videos and a longer walkthrough: https://nomorehappypath.com

## Quick start

1. **Check you have** macOS 13+ or Linux, Python 3.9+, git, and **any two** of the Codex CLI, Claude Code CLI and Gemini (Antigravity) CLI, installed and signed in (details in [What you need](#what-you-need)). On Linux you also need tmux and bubblewrap.
2. **Install:**

   ```bash
   git clone https://github.com/nomorehappypath/nomorehappypath.git
   cd nomorehappypath && bash install.sh
   ```

   The installer checks your prerequisites (it cannot check that your accounts and sign-ins work; Settings does that), tells you plainly what is missing, then installs the auto-start service or runs once in the foreground.
3. **Open Mission Control** at http://127.0.0.1:8740/ (the installer prints the address), or run `python3 harness/project_manager.py`. On a Linux server, that address is on the server itself: forward it first, for example `ssh -L 8740:127.0.0.1:8740 you@your-server`, then open http://127.0.0.1:8740/ on your own computer.
4. **In Settings:** choose a provider for each of the three roles (Delivery, Reviewer, CTO) from the CLIs you installed, keeping Delivery and Reviewer on different vendors, and use the connection test to check each one. Settings apply to agents you start afterwards. If you use Claude, set up its agent token as described in [Claude agent authentication](#claude-agent-authentication); if you use Gemini, follow [Gemini agent setup](#gemini-agent-setup-optional). For the optional project chat, add your OpenAI API key under *Project chat key*.
5. **New project** (or **Adopt existing**): choose the folder (read *Help → Your Responsibility* first: agents can edit, delete and run code in that folder, so choose it deliberately and back it up), then start the three agent roles from Mission Control. If Claude or Codex asks *Do you trust this folder?*, answer it in the agent's window (**View agent**).
6. **Give your first direction** in plain English in the Delivery agent's window. Review the requirements the agents propose and press **Go ahead** (or change them).
7. **When the task shows "ready for your test"**, follow its test checklist: press **View app** (or **Open report** for a document), try it, then choose **Accepted** or **Not accepted**.

The first run needs a few sign-ins; the sections below explain each one.

**Costs tip:** the agents run on your own plans. For small tasks, choose a medium effort level in Settings before you start the agents; the highest effort settings use noticeably more of your allowance.

**On a fresh Linux server, also:**

- **Install the CLIs without sudo.** `curl -fsSL https://claude.ai/install.sh | bash` and `curl -fsSL https://chatgpt.com/codex/install.sh | sh` put `claude` and `codex` in `~/.local/bin`. Open a new terminal window afterwards; `bash install.sh --check` then finds them. With no browser on the server, sign in with `codex login --device-auth` and `claude auth login`.
- **Claude's one-time warning.** The first time Claude runs in agent mode it shows a *Bypass Permissions* warning that defaults to "No, exit". Clear it with the same account and Claude installation the harness uses: press **View agent** and answer it there, or in your own terminal run `claude --dangerously-skip-permissions`, press Down to choose "Yes, I accept", press Enter, then type `/exit`. Until then a Claude agent looks stuck on "waiting for the harness" in Mission Control, and **Test Claude connection** does not catch it.
- **Claude token, simplest way.** Run `claude setup-token`, then from the repository folder `python3 -m harness.claude_auth store-token` and paste the token only at its hidden prompt. Never put it on a command line or in a chat. If an older token was imported into the service earlier (see above), it overrides the saved one: remove that import, safely pause the project, restart the service, press **Test Claude connection**, then restart the affected agent.
- **A native browser, only if you need screen checks** (rendered evidence for app work; not needed for documents or backend work). Snap Chromium does not work. For example, on Ubuntu: download `https://dl.google.com/linux/direct/google-chrome-stable_current_arm64.deb` (or `..._amd64.deb` on an Intel/AMD server) and install it with `sudo apt install ./google-chrome-stable_current_arm64.deb`. Then tell the app where it is: `export HARNESS_BROWSER_BIN=/usr/bin/google-chrome` before a foreground start, or for the auto-start service `systemctl --user set-environment HARNESS_BROWSER_BIN=/usr/bin/google-chrome` and restart the service (repeat after a reboot). On the Ubuntu 26.04 machine we tried, the Google package's AppArmor profile blocked Chrome's network inside the agent sandbox, so fully rendered tests there could not run; the screen check worked.
- **Reaching Mission Control through an SSH tunnel.** Forward the same port on both sides, for example `ssh -L 8740:127.0.0.1:8740 user@server`. If you start the manager with `--port N`, forward `N:N`. A different local port is refused with "same-origin project request required".

**Where this was tried:** install, sandbox, Mission Control and the start of Claude and Codex agents were verified on a clean Ubuntu 26.04 ARM64 server (Gemini was not part of that test). A full run to Accepted was not completed there.

## What it is

- **A team of agents.** A builder, an independent reviewer from a different AI company, and a CTO that watches the board. Each role has a one-page card of its rules; every other rule is still there as a named chapter the agent can open. Claude, Codex or Gemini can take any role, and the reviewer is never from the builder's company.
- **A contract before any code.** The agents propose the requirements for your approval (you press Go ahead, or change them), then plan the work and an evidence contract before anything is built. The reviewer from the other AI company writes the tests first, before the builder starts, and the board proves those tests can fail (see below).
- **A board that agents cannot directly edit.** Tasks, contracts, reviews and their backups are written only by the platform's own software; agents change them only through its governed commands. A task advances only with evidence: executed tests, failure paths, and a walk through the finished product (in a real browser when the app has a screen).
- **Security and quality by default.** Agents are required to build in, and the reviewer checks where they apply: secrets kept out of the code, user input checked, logins that hash passwords and slow down repeated guesses, rate-limited sending endpoints, no internal errors shown to users, and a dependency check before delivery. Every app also gets one central error catch-all. A per-task **Enhanced security** option adds a threat model, security tests and a controls register (security assessment and audit preparation, not a certification or a penetration test).
- **A write sandbox around every agent**, enforced by the operating system on macOS and Linux: agents can write only to the project, their own CLI state, caches and temporary folders, not to your other files.
- **Self-healing.** A stalled or unexpectedly ended agent session is restarted for you (up to three times an hour), sign-in problems are flagged in plain words, and agents resume their saved conversation after a pause or stop.
- **Mission Control.** A local web app to watch every project, preview the finished app, ask questions about your project (project chat uses your own OpenAI API key), accept the result, and update itself with one click.
- **Over 3,400 automated tests**, including headless-browser tests that verify what the owner actually sees rendered.

## Before work starts: the reviewer reads your requirements

When Delivery writes down the final requirements, the Reviewer reads them first and writes tests for them before any work starts. If it cannot write a test without guessing, it asks you at most four questions, once, on a single card in Mission Control, each with a recommended answer and an Other box; your answers are added to the requirements under "Decided before coding". You can always go ahead without waiting. Later, each requirement is marked checked, partly checked or not checked in advance. Agents ask you product questions only in Mission Control (a banner at the top counts them), never in their own terminal windows.

**A passing flag is not proof.** When a project runs its own final check before it is called done, it prints one `FINAL CHECK` line for each check: passed, failed, not run, warnings only or skipped. The board reads those lines and does not accept "done" on anything but passed, even when the program exited cleanly, and the Reviewer reads the same lines instead of trusting a status flag.

## Windows programs, end to end

A Windows program is built and proven on a real Windows machine you give the agents, and you get the finished file from Mission Control. In plain words:

1. **Make a throwaway Windows machine, anywhere** (a virtual machine on a Mac, Linux or PC, or a spare computer): a local account, at least 2 cores, 4 GB of memory and 64 GB of disk, and the OpenSSH Server turned on (three PowerShell lines; the Help page shows them). Note its address.
2. **Connect it once** from Mission Control: press *Test machine* on the project, enter the address, the account name and the password, and press *Connect*. The password is used for that one sign-in and forgotten.
3. **The agents do the rest.** After the one-time setup they install their own build and test tools on the machine over ssh, build the program, and run its tests there; the independent reviewer, from the other AI company, runs them again on the same machine. After any stop (pause, close, restart, or a key that stops working) the card asks for the password again.
4. **If part of the work could not be tested, you are told.** The project says *Not fully tested* and the agents settle it themselves; you are not asked to sit and wait. If it still cannot be completed, the task says *could not finish* and nothing ships. Security and the independent review cannot be waived.
5. **You get the program.** The acceptance card shows *Get the build*: each file with its size and SHA-256, the machine and version it was built and tested on, what Windows it is for, what you need, the steps to try it and what you should see. Compare the SHA-256 (`Get-FileHash` on Windows, `shasum -a 256` on a Mac, `sha256sum` on Linux) before you run it, and do not run the file if it differs. Programs are not code-signed, so Windows may warn you.

What it does not do: it never creates, resets or deletes the machine, one machine serves one project, the harness itself does not run on Windows, and a test only proves what that one machine can show.

**The first Windows tool built this way: `why.exe`.** `why --port <N>` tells you which program is listening on a local TCP port. It was built by the agents, tested on a real Windows virtual machine, and reviewed by an independent reviewer from the other AI company who ran it there. The proof trail is the board of that project, and it is not a clean sweep: six reviews ran. The first three also found real defects, including that the error report could send a bearer token over plain HTTP to another computer, which the agents fixed and the reviewer re-ran on Windows. Five of the six ended *NOT TESTED* because the case where Windows refuses access to a protected process could not happen on the test machine (its account was an administrator). The owner then waived only that part, in writing, and the sixth review passed on fresh Windows runs of everything else. Accepting an untested part does not bypass security or defect findings. Those rounds are why *Not fully tested* exists.

Existing projects keep their old acceptance card: *Get the build* appears only for a new task whose Delivery declared the build, so `why.exe` itself would need a new task to be downloadable this way. A task that declares a build and also a web app keeps *View app*.

## What's new in v0.13.0

- **The tests come first, and they must be able to fail.** The reviewer from the other AI company writes the tests before any building starts. The board then runs those locked tests on the empty project. A test that passes on nothing is flagged *did not distinguish* and never counts as proof. This shows a test can fail on an empty project; it does not show the tests cover every requirement.
- **A repair that edits a test must say why.** The reason is written down and shown to the reviewer.
- **Reviews now end.** Round 1 lists every real blocker, with the harm it does and the steps to reproduce it. Later rounds block only on four things: a blocker not fixed yet, something the fix broke, an agreed requirement still unmet, or a risk to safety or data. New nitpicks become notes and do not block.
- **Two safety nets, chosen up front in Settings.** If a review cannot be completed, the reviewer retries once on a model you pick. After the third failed review, a fresh builder retries once on a model you pick. If it still fails, the task says *could not finish* and nothing ships. There is no owner card and no question for you.
- **One-page role cards.** Every agent starts from a one-page card for its role. Every rule is still reachable as a named chapter.
- **Usage at a glance, and big changes stay readable.** Each task shows a plain usage line, and a very large change gets a bounded reading list instead of the whole diff.

## What's new in v0.10.0

- **Windows programs, end to end:** *Get the build* (download the tested program with its SHA-256 and steps), a *Test machine* button in the project's own header, and agents that install their own tools on the machine over ssh.
- **You are told when work could not be fully tested:** *Not fully tested* and *Waiting for you*, with three choices, instead of a silent loop.
- **Safer machine handling:** one OS-held lock per machine, the password asked again after any stop, and Resume keeps a pending Connect.
- **Plain words:** the app's messages, cards and Help were rewritten to say what happened, what it means and what to do, without technical terms.
- **Windows closes itself:** finished agent terminal windows close on their own, and the AI reviewer's browser tools no longer open on your screen.
- **Clearer evidence rules for reviewers and agents,** and a repeated-failure alert for the CTO.

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

**Linux, simplest way:** the harness can keep the setup-token in an owner-only file so it
survives a reboot and needs no systemd import. In your own terminal (the prompt is hidden
and the value never goes to shell history):

```bash
claude setup-token
python3 -m harness.claude_auth store-token
```

The token is saved in `~/.config/nmhp-harness/claude-setup-token` (folder `0700`, file `0600`).
Then press **Test Claude connection** in Settings; it tests the same path the agents use and
tells a rejected token apart from a missing Claude program, a refusing sandbox, a usage limit
or an unavailable model. On Linux the agents (Claude and Gemini) are read-confined: they can
read the project, their own CLI state, the system and tool folders, not the rest of your home.

**Ubuntu with Chromium installed as a snap:** a snap launcher cannot run the harness's headless
screen checks. Point the app at a native browser, for example
`export HARNESS_BROWSER_BIN=/usr/bin/google-chrome` before starting the app.

**A project is slow to open:** the app waits while the board is still making progress. To
change the limit, set `HARNESS_WORKER_START_TIMEOUT` (seconds) or start the manager with
`--worker-start-timeout`.

**A red "Needs repair" hold you know is stale:** with the project resumed, run in your own
terminal (roots are the project's code, data and workspace folders shown in Settings):

```bash
python3 -m harness.board --root PROJECT --data-root DATA --workspace-root WORKSPACE \
  clear-hold --task TASK --source SOURCE
```

It refuses if the task has no open hold or the source is not the hold's source, and it never
changes a certified result.

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

**Can it build and test a Windows program?**
Build on this machine, test on one you name. Make a throwaway Windows VM with
OpenSSH Server turned on, then tell Delivery: "build here, test on the VM".
When the agents need it, a *Test machine* card appears on the project in
Mission Control; you enter the VM's address, account name and password there
once and press Connect. Mission Control reads and saves the VM's identity,
makes a key for the project, signs in once with the password (used for that
one connection and forgotten, never logged or shown to an agent), installs
the key and checks that it works. Nothing is pasted and nothing is installed
by you: Delivery tells you what size to make the VM, the agents install the
tools themselves, add a `make test` target that copies the code there and
runs the tests over ssh, and the board certifies that output; the reviewer,
from the other AI company, reruns it on the same VM in its own folder. The
agents clean up what their runs leave behind before every test, and say so if
they cannot. Whenever the project stops for any reason, or the key stops
working, the card asks for the password again and machine-dependent results
wait for it. If you rebuild or change the VM, the card and the agents
re-run what depended on it. Use a password that exists only for that VM and
keep the VM away from your files and your other machines: anything the agents
write can run there. The harness itself does not run on Windows. See
[Windows programs, end to end](#windows-programs-end-to-end) for what you see
from the first request to the downloaded program.

**How do I create the Windows test machine?** (Mission Control's Help page has the same
steps: *Create The Windows Test Machine*.) Some programs only prove themselves on real
Windows, so the agents build (here or there) and test on a Windows machine you give them,
one per project. Make it disposable, with none of your personal files, no shared folders
or attached disks, and no access to anything on your network you care about: the agents run
commands there with the account you give them. You create it and destroy it; the app never
creates, resets or deletes it. Where it runs does not matter (a virtual machine on a Mac,
Linux or PC, or a spare Windows computer). You do four things: (1) create it from
Microsoft's installer with a local account and a password used nowhere else, at least 2
cores, 4 GB of memory and 64 GB of disk, comfortably 4 cores, 8 GB, 100 GB (Delivery
restates the exact needs for the project); (2) in PowerShell as Administrator, run once
`Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0`,
`Set-Service -Name sshd -StartupType Automatic`, `Start-Service sshd`; (3) find its address
with `ipconfig` and check that `ssh -F /dev/null -o PubkeyAuthentication=no account@address` works from
the Terminal of the computer that runs Mission Control; (4) in Mission Control's Projects
page press *Test machine* on the project, enter the address, account name and password, and
press *Connect*. Everything else (which Windows it is, ARM or x86-64, the toolchain, the
tools) is the agents' job; some projects need an Administrator account or extra tools and
Delivery tells you before the first test. Take a snapshot of the clean machine if your
software offers it. After you restore a snapshot, rebuild or change the machine, press
Connect again and tell Delivery (also after a Windows update and restart): that makes earlier results stale, Delivery sends the work for review again and the reviewer runs the tests again; a
pause, close, restart or a failing key makes the card ask for the password by itself. If
the address changes, press Connect with the new one. When the project is finished press
*Release this machine* (the installed key stays on the machine) and destroy the machine, or
wipe a spare computer.

**Is this for real, serious products, or for toys?**
Serious products - that is the design center. This platform is for
building software that might carry revenue, clients, and your name -
everything the agents build in your folder is yours.

**My task delivered a report, not a screen. Where do I read it?**
On the task card and on its release card, under **Read what was delivered**:
the **Open report** link opens the document Delivery attached for you (and
the CTO's card carries the same link). View app starts the app itself; it
does not show a document. Accepted work keeps its link in Task history.

**Can I use a strong model to write the spec and a cheaper one to code it?**
Yes, without touching Settings. The Delivery button in Mission Control opens a
small dialog pre-filled with the Delivery card's provider, model and effort;
change them there (the model list is the Settings card's, plus a custom id) and the choice applies to that launch only. Settings stays
the default, the Reviewer and the CTO always use Settings, and the provider list
offers only providers that keep review independent. A replacement the harness
opens for a stopped agent, and a project resume, keep the choice. A model the
CLI rejects is not yet explained by the harness: read its terminal, Stop it and
start again with a corrected model.

**What happens when Claude or Codex runs out of my usage allowance?**
The agent that ran out is held, not restarted: the project shows one amber note
("... shows Claude's usage-limit message; it announced 9:43 PM ...") and that
agent's row reads WAITING: USAGE LIMIT. Nothing is typed into it, the other
agents keep working, and its messages wait. Claude Code continues by itself at
the announced time; if the message is still on the screen a minute later the
harness types one short line, once. A waiting Reviewer is never replaced by a
second one, and a review is never handed to the company that wrote the work.
It does not switch vendors mid-task, use another account, or use an API key to
get around a limit. If the note is wrong, type any message into that agent's
terminal and the harness continues when the program answers.

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
