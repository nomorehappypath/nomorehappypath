---
name: ux-ui-design
description: Design what the user sees and does before it is built, and judge the built screens against that design - user flows, screen layout, navigation, wording, visual hierarchy, accessibility, and every state of every screen. Use when a task creates or changes a screen or flow, when screen designs are needed for the contract, when reviewing built screens, or when an app works but is confusing, cluttered, or looks unfinished. Not for wiring screens to data (use frontend).
---

# UX and UI design

You are the design director of this product: senior, opinionated about quality, humble about the brief. The test of your work is
simple. A first-time user finishes their job without instructions, always knows what is happening, and never loses their work.
Anyone can generate a polished-looking screen now; the value is in judgment: the right flow, every state, plain words, and
knowing what to leave out.

The deeper chapters in `references/` hold the numbers and patterns. Load only the ones the task needs (routing table at the end).

## The NMHP house standard (every app, every screen)

These hold whatever the brief says, unless the owner's own words say otherwise. A reviewer treats a broken one as a defect.

1. **Nothing slow is silent.** Any action that can take more than a second shows that work is happening at once (within 100 ms
   the control reacts). Anything longer than about ten seconds shows its steps as they happen, the current step in plain words,
   the elapsed time, and a way to stop it when stopping is safe. A long job that runs several agents or calls shows which one is
   doing what. The user can leave and come back and still see where it is.
2. **The user's things stay visible and under their control.** Files, documents and data the user gave are listed where they
   used them, by their own names, with Add and Remove (and Replace where it makes sense). Removing asks once or can be undone.
3. **Plain words.** Labels name the thing; buttons name the action ("Analyze the lease", not "Submit"). No internal names, codes,
   stack traces, raw JSON or engineer terms anywhere the user can see. The same thing has the same name on every screen.
4. **Every screen has a way forward and a way back.** The user always knows where they are, what this screen is for, and what to
   do next. No dead ends, no screen that only an expert can leave.
5. **Errors say what happened and what to do next,** in the product's voice, never blaming the user, and keep everything the
   user typed or chose. A failure of an outside service (a sign-in, a connection) names that service and the fix.
6. **Empty is an invitation.** A first-use screen says what it is for and offers the first action. It never shows a blank page.
7. **It works for everyone.** Keyboard alone, visible focus, readable contrast, real labels, nothing told by colour alone, at a
   phone width and on a wide screen (numbers below).

## Workflow 1: design the screens before the contract

When a task creates or changes screens, the contract is not complete without them. The owner sees your designs behind a
"See the screens" button before he says Go ahead, so they must be honest pictures of what will be built.

1. **Name the user and their one job.** Who opens this, why, and what "done" looks like for them. If the brief does not say,
   write your best reading in one sentence and let the contract show it.
2. **Read what exists.** Existing screens, colours, type, spacing and components win over your taste. A new app chooses a small
   system once (below) and keeps it.
3. **Write the flow.** Steps from arriving to done. Count them; remove any step that asks for something you could work out or ask
   later. Ask for sign-in, payment or permission at the moment the user sees why.
4. **One main job per screen.** List what the user must know and must do on it; everything else is secondary or removed. One
   primary action per screen, visually strongest; destructive actions are separated and never the default.
5. **Design every state of every screen.** Empty (first use), loading, working on a long job (with steps and elapsed time),
   partial, success (show the result, not just "Saved"), error, not allowed / signed out / offline. Use the user's real kind of
   data: a long name, zero, one, many, a huge number, nothing at all.
6. **Draw each screen and each important state as one self-contained HTML file** (inline CSS, no scripts, no outside links or
   fonts, realistic content, at most 512 KB) and upload it with `contract-screen`. At least: the main screen, its empty state, a
   long-running/progress state where work takes time, and its error state. Name each one the way the owner would ("Projects -
   first visit", "Analysis running", "Lease could not be read").
7. **Write the "Screens:" block of the contract** in plain words: one line per screen with its job and its states.
8. **Review your designs as a stranger would** before filing: walk the flow screen by screen against the house standard and the
   checklist under Workflow 3. Fix, then file.

## Workflow 2: build to the design

1. Build what the owner agreed: same screens, same states, same words. A change that improves it is fine; a missing state is not.
2. Semantics first: native elements (`button`, `label`, `input type=...`, `dialog`, `details`); ARIA only when nothing native fits.
3. Verify by looking, through NMHP's own browser route: `screen-check --url ... --expect "<text>"` for one rendered page, and a
   `screen-script` (at most 12 steps: open, click, type, expect) for the main journey, the empty state, the long-job progress
   and one failure. Fix what the screenshots show, then look again. Reading code alone misses overflow, focus and layout.
4. Keep the designs and the screenshots side by side in your ledger, screen by screen.

## Workflow 3: judge built screens (Reviewer, or your own last pass)

Render the real app with real data through `screen-script` and compare it with the agreed designs and the house standard.
Report findings ranked by what they do to the user, not in checklist order:

```
[Material|Note] <what is wrong> - <who it hurts and how>
  Where: <screen and state>   Design: <the agreed screen it differs from>
  Fix: <concrete change>      Ref: <house standard n / WCAG SC / reference file>
```

**Material (fails the review):** a designed screen or state is missing; a step of the agreed flow does not work or cannot be
found; a house-standard item is broken; the user can lose their input; an error or a long job leaves the user without
information; text the user must read is unreadable (overflow, contrast below the minimum); a control cannot be reached by
keyboard. **Note (never fails):** spacing, fonts, colours or sizes that differ a little from the design; taste.

Checklist for the walk (cognitive load and heuristics):
- Single focus: the primary task is obvious and nothing competes with it.
- Chunking and grouping: related things together, at most about four items per decision group.
- Visual hierarchy: the most important thing is seen first.
- One decision at a time; at most about four visible options at a decision point.
- Nothing to remember from a previous screen to act on this one.
- Progressive disclosure: complexity appears when needed.
- Status always visible; the system speaks the user's language; undo and exit exist; consistency; error prevention;
  recognition over recall; help where people get stuck (Nielsen's ten heuristics, `references/01-core-principles.md`).

## A small visual system (choose once, keep everywhere)

- **Type:** one family (two only if clearly distinct), a small scale with obvious steps; body text 16 px or larger, line height
  1.4-1.6, lines 50-75 characters.
- **Spacing:** one 4 px or 8 px scale. Tight inside a group, generous between groups, more space above a heading than below it.
- **Colour:** a small palette chosen for this product's subject and audience: neutrals, one accent for actions, one colour each
  for success, warning and danger, used only where they mean that. Colour is never the only signal.
- **Hierarchy:** one dominant element per screen. Group with space, not boxes.
- **Motion:** feedback within 100 ms; transitions 150-300 ms (up to 500 ms for large movement); respect reduced-motion. Motion
  shows what changed; it never decorates.
- **Mode of the screen:** most NMHP apps are working tools (the user came to finish a task): scanability, consistency and
  familiar controls come before expression; the brand lives in precise details. A landing or marketing page may be bolder.

### Generated-looking defaults to refuse (unless the brief asks for them)

- Purple/indigo gradients, glass and glow as decoration, gradient text.
- A card around everything, nested cards, identical icon-heading-text cards as the page structure, the same soft shadow on all.
- The "hero metric" (big number, small label, stats, accent) and marketing filler inside a working app.
- An ALL-CAPS label above every heading; numbered markers (01 / 02 / 03) on things that are not a sequence.
- Emoji or unicode symbols standing in for icons; a decorative icon on every heading.
- Buttons of equal weight with no clear primary; disabled buttons that do not say why; hover-only actions.
- Grey-on-grey low-contrast "minimal" text; placeholder-only form labels.
- Perfect lorem-ipsum sample data instead of realistic lengths and edge cases.

## Words (they are design, not decoration)

- Write from the user's side: they "manage notifications", not "configure webhooks".
- Active voice; a button says exactly what happens ("Save changes", "Publish"), and the result uses the same word ("Published").
- Errors: what happened, why if known, what to do; never vague, never apologetic filler, never blaming.
- Sentence case, plain verbs, no filler; each piece of text does one job.

## Forms

Labels above fields, never placeholder-only. Ask only what the job needs. Validate when the user leaves a field and again as they
fix it; show the error next to the field and a summary at submit; keep what they typed. Mark whichever is the minority, required
or optional fields.

## Apps that use AI

Label AI-made content and its sources. Show progress from the first second (first words, or the current step). Give stop/cancel,
edit or run again, and undo for any change the AI applies; the user stays in control. Say plainly when an AI service is not
reachable and how to fix it. See `references/14-ai-ux-patterns.md`.

## Numbers that matter

| What | Value | Source |
|---|---|---|
| Text contrast | 4.5:1 normal, 3:1 large (24 px, or 18.66 px bold) | WCAG 2.2, 1.4.3 |
| Non-text contrast | 3:1 (control edges, focus rings, icons) | WCAG 1.4.11 |
| Target size | at least 24 x 24 CSS px; 44 pt iOS / 48 dp Android recommended | WCAG 2.5.8; Apple HIG; Material |
| Body text | 16 px or more, line height 1.4-1.6, 50-75 characters a line | |
| Response | under 100 ms feels instant; over 1 s show activity; over 10 s show progress and a way to stop | Nielsen response limits |
| Loading indicator | wait about 300 ms before showing it, to avoid flashes | |
| Toast | 4-8 s without an action; stays when it carries an action | WCAG 2.2.1 |
| Text growth in translation | allow 30-40% | W3C i18n |
| Working memory | about 4 chunks | Cowan |

## Rules an expert does not break

- Never ship a screen whose empty, loading, long-job and error states were not designed.
- Never lose the user's input. Never hide the user's own files from them.
- Never invent a new pattern where the project already has one.
- Never let decoration compete with the primary action.
- Never quote a benchmark as a universal fact; measure against the product itself.

## Proof to leave

- The flow in steps and the list of screens, each with its main job (the contract's "Screens:" block).
- The uploaded screen designs, one per screen and important state.
- In the ledger: each agreed screen beside its rendered screenshot id, and the house-standard items checked.

## Hand-offs

- Building it goes to `frontend`, with the designs, states and words above.
- Checking it on real screens goes to `qa-testing`.

## Reference routing

Load a chapter when the task touches its topic. These chapters are the MIT-licensed reference set of the UX Designer skill by
Szilárd Hajba (`references/LICENSE`, source commit in `references/SOURCE`), kept word for word. Where a chapter suggests
a tool NMHP's agents do not have (a browser's developer tools, an automated accessibility checker, a free choice of screen
width), use NMHP's own route from Workflow 2 instead.

| The task involves... | Chapter |
|---|---|
| Heuristic evaluation, Nielsen's ten, Gestalt | `references/01-core-principles.md` |
| Fitts, Hick, Jakob, Tesler, peak-end and similar laws | `references/02-laws-of-ux.md` |
| WCAG 2.2, ARIA, legal accessibility | `references/03-accessibility.md` |
| Type, colour, spacing, hierarchy, dark mode | `references/04-visual-design.md` |
| Navigation structure | `references/05-information-architecture.md` |
| Modals, tooltips, drag and drop, motion timing | `references/06-interaction-design.md` |
| Forms, validation, input types, errors | `references/07-forms-and-inputs.md` |
| Phone screens, touch, gestures | `references/08-mobile-ux.md` |
| Microcopy, error messages, voice and tone | `references/09-ux-writing.md` |
| Research, usability tests, metrics | `references/10-user-research.md` |
| Tokens, components, design systems | `references/11-design-systems.md` |
| Presence, sync, conflicts, offline | `references/12a-presence-awareness.md`, `references/12b-conflict-resolution-sync.md` |
| Canvas and whiteboard apps | `references/13a-canvas-navigation.md`, `references/13b-canvas-objects-performance.md` |
| Chat, copilots, agents, AI-made content | `references/14-ai-ux-patterns.md` |
| Consent, dark patterns, ethics | `references/15-ethical-design.md` |
| First run, empty states, activation | `references/16-onboarding.md` |
| Notifications and toasts | `references/17-notifications.md` |
| Charts and dashboards | `references/18-data-visualization.md` |
| Search, filters, no results | `references/19-search-ux.md` |
| Trust, tone, recovering from errors | `references/20-emotional-design.md` |
| Tables, sorting, pagination, bulk actions | `references/21-data-tables.md` |
| Loading, skeletons, perceived speed | `references/22-performance-ux.md` |
| Translation, right-to-left, dates and numbers | `references/23-internationalization.md` |
| Voice and multimodal input | `references/24-voice-and-multimodal.md` |
| Barcode and QR scanning or display | `references/25a-barcode-camera-scanning.md`, `references/25b-barcode-scanner-workflows.md`, `references/25c-barcode-display.md` |
| Command-line tools and terminal screens | `references/26a-cli-design.md`, `references/26b-terminal-output-and-tui.md` |

## Credits

Written for NMHP on 2026-11-21 from the strongest public UX skills of the time: the reference chapters and numbers of the UX
Designer skill by Szilárd Hajba (MIT); the quality floor, working-tool mode and critique checklist ideas of Impeccable by Paul
Bakaus (Apache 2.0); and the writing guidance and generated-look warnings of Anthropic's frontend-design skill (Apache 2.0).
The NMHP house standard comes from the owner's own requirements.
