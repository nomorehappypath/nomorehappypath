# Safety Notice

**This notice is informational.** It does not modify, and adds no conditions
to, the Apache License, Version 2.0 under which NoMoreHappyPath is licensed
(see LICENSE). The license's own disclaimer of warranty (section 7) and
limitation of liability (section 8) are the only terms that apply.

## 1. License

The Software is licensed under the Apache License, Version 2.0, copyright
holder KpiMinds LLC. You may use, modify and distribute it, including
commercially, under that license's terms. It is provided "AS IS", without
warranties or conditions of any kind, as section 7 of the license states.

The Software, and any code or other output that AI agents produce while using it, is provided as is. As sections 7 and 8 of the license state, and to the extent applicable law allows, unless a law or a written agreement says otherwise, KpiMinds LLC gives no warranty for and is not liable for bugs, defects, security flaws or lost data in the Software or in what the agents produce. The license covers the Software, not the projects you build with it: you review, own and are responsible for what you build and deploy.

## 2. AI agents act autonomously — know what that means

The Software orchestrates third-party artificial-intelligence agents that are
non-deterministic and act autonomously. Inside the folders you designate they
can create, modify, delete and execute files and commands, and they can incur
charges or use up quota on the third-party accounts you connect (including
OpenAI, Anthropic and Google accounts and API keys). Things that have happened with tools like
this include loss or deletion of data or code, defective or insecure output,
unintended command execution, and unexpected charges.

Practical advice: choose the folder you point the agents at carefully, keep
it in version control, keep backups of anything you cannot afford to lose,
review output before you rely on it, and decide for yourself what to accept
or deploy.

## 3. Third-party services

The Codex CLI, Claude Code CLI, the OpenAI API and, optionally, Google's
Antigravity CLI (which runs Gemini models) are third-party services governed
by their own terms and billed or quota-limited on your own accounts. KpiMinds
LLC is not a party to those relationships and does not operate those services.

Gemini through the Antigravity CLI uses your own Google account. The CLI
shows Google's Terms of Service and Data Use choice (including whether Google
may use your interactions) when you first sign in; that choice is yours, and
the Software never makes it for you. Whatever a Gemini agent can reach in any
role (Delivery, Reviewer or CTO): its prompts, the files it reads, command
output and other content its sandbox lets it read, not only your project
folder, can be sent to Google by that CLI under Google's terms and your choice. A free Google account has a small usage quota; Google's plans and
billing are between you and Google. The Software accepts no API key for Gemini.

NoMoreHappyPath is an independent project. It is not affiliated with,
sponsored by or endorsed by OpenAI, Anthropic or Google. OpenAI, Codex,
Anthropic, Claude, Google, Gemini and Antigravity are names or trademarks of
their respective owners and are used here only to say which tools the Software
can work with.

## 4. Build attribution

Projects created ("scaffolded") by the Software include a visible
BUILT_WITH.md file identifying NoMoreHappyPath as the build tool, with the
creation time and a non-personal installation identifier. Adopted
repositories are never written to. The project's code belongs to its owner;
the stamp records only the tool.

Contact: license@kpiminds.com
