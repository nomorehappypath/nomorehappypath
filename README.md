# NoMoreHappyPath

**Agents that prove it. An AI development-governance platform: AI agents do the
work — the platform refuses every happy-path claim until the evidence exists.**

NoMoreHappyPath (nomorehappypath.com) runs real AI coding agents (Codex CLI, Claude CLI) as a governed
delivery team — a Delivery Agent that builds, an Independent Reviewer from a
*different vendor* that verifies by running the code, and a CTO watchtower
that audits every claim against evidence. A human owner stays in charge of one
thing only: saying what they want, and accepting the result.

## Why

AI agents are excellent at producing code and terrible at being believed.
"Done" from an agent means nothing until someone runs the tests, renders the
page, and checks the claim. NoMoreHappyPath turns that skepticism into
infrastructure:

- **Cross-vendor review** — the agent that wrote the code never reviews it,
  and the reviewer is from a competing vendor with no incentive to agree.
- **Evidence-gated status** — a task cannot become "done" by assertion. The
  board accepts state transitions only with attached proof: test output,
  rendered-page checks, executed failure paths.
- **The owner speaks product, never plumbing** — plain-language directives in,
  plain-language "ready for your test" out.

## What you get

- **Mission Control** — a local web app that shows every project, launches
  visible agent terminal sessions with the correct role directives preloaded,
  and tracks the delivery gate in real time.
- **A per-project governance board** — tasks, review queue, holds, release
  gates, all evidence-linked.
- **Project chat** — ask "what is left?" and get answers grounded in board
  facts, never model improvisation.
- ~1,000 tests, including headless-browser tests that verify what the owner
  actually sees rendered.

## What you need

Be aware before you clone: the platform itself is free to evaluate, but the
AI agents it governs run on **your own accounts**.

| Requirement | Where to get it | Cost |
|---|---|---|
| macOS 13+ with Python 3.9+ | `python3 --version` to check | free |
| **Codex CLI** (the Delivery agent) | `npm install -g @openai/codex` — sign in with your OpenAI account | your OpenAI/ChatGPT plan |
| **Claude Code CLI** (the Reviewer/CTO) | https://claude.com/claude-code — sign in with your Anthropic account | your Anthropic plan |
| **OpenAI API key** (project chat) | https://platform.openai.com/api-keys — entered under Settings, verified before storage, kept in a file only you can read | pay-per-use, cents |

You can start with just one CLI, but the platform's core guarantee —
**cross-vendor review, where a competing vendor's agent verifies the work** —
needs both. No other dependencies: the platform is pure Python standard
library. Windows and Linux are not supported yet (the visible-terminal
workflow is built on macOS machinery); Linux support is on the roadmap.

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
agents work on your own OpenAI and Anthropic plans, and a serious build -
with adversarial reviews, scenario tests, and failure-path evidence - can
run into the hundreds or a few thousand dollars of model spend. Measure
that against what the software is for: a commercial product that will
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

## Your responsibility

Using this software is entirely at your own risk and responsibility; it is
provided as-is with no warranty (see LICENSE). The CLI agents can do whatever
they need to do inside the project folder you choose — create, edit, delete,
and run code — so choose that folder carefully, keep it under version control,
and never point the agents at data you cannot afford to lose. Agent usage is
billed to your own OpenAI/Anthropic accounts.

The complete, binding terms — no warranty, assumption of AI-agent risk, and
KpiMinds LLC's limitation of liability (zero for free use) — are in
[DISCLAIMER.md](DISCLAIMER.md). Using the software means accepting them.

## License

Source-available under the **Business Source License 1.1** (see LICENSE).
Free for evaluation, development, testing, and personal non-commercial use.
**Any production or commercial use requires a commercial license from
KpiMinds LLC** — contact **license@kpiminds.com**. On 2030-08-21 this version
converts to Apache 2.0.

Copyright (c) 2026 KpiMinds LLC. All rights reserved.
