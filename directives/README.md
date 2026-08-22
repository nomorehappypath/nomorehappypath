# Centralized visible CLI sessions — no owner setup

The recommended workflow is one local command from the target project root:

```bash
bash <harness-root>/scripts/start_board_viewer.sh
```

It opens **NoMoreHappyPath Mission Control** automatically: a clear delivery-progress,
review-queue, terminal-status, and next-action display. Use **CODEX CLI** to
open a Delivery Agent role only. Give it your direction later in its terminal;
its Product Manager hat turns that direction into the internal objective and
delivery plan. Use **CLAUDE CLI** to open one or more Independent Reviewers.
Use **CTO (CLAUDE)** to open the single global CTO. The buttons inject the
matching directive and role into a visible interactive terminal; the owner
never copies a directive or fills a profile/configuration.

The panel shows every session and has a Stop control. A session closed from its
own terminal is detected automatically, and the CTO button becomes available
again. The sessions start in your projects root to inherit the existing
full-bypass permission context, while their directive identifies the chosen
target project and board. It opens only local macOS Terminal sessions and is
bound to localhost.

Manual directive pasting is still supported when the control panel is not in
use: `AGENT.md` for Delivery/Independent Reviewer and `CTO.md` for CTO.

All work is visible in the three CLIs and in auto-created
`.harness/board/BOARD.md` and `.harness/board/events.jsonl`.

The panel creates the board if necessary and refreshes managed sessions, active
agents, review queue, and the human-readable board every two seconds.

Managed Delivery terminals are pre-registered with a session-bound ID. The
first real instruction typed into that terminal is recorded as owner direction;
the board rejects task creation before it exists. The controller blocks a
pre-direction source edit, routes a failed independent review back into the
same terminal, and the watchdog labels a missing heartbeat as **STALLED**.
