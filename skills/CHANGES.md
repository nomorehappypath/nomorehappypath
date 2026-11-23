# Edits made to the delivered skill text

Per `SKILL_LIBRARY_SPEC.md` section 4, only two kinds of edit to a skill's text
are allowed, and every one must be listed here, old text beside new text.
Nothing else in the twelve skills was edited, shortened or merged; the other
eleven skills, and every other section of `qa-testing`, are word for word the
text delivered in `SKILL_LIBRARY_SKILLS.md`.

## `qa-testing`: browser steps rewritten to NMHP's real browser route

NMHP's own agents can exercise a running app in a browser only through two
board commands (the `rendered-proof` chapter of `directives/AGENT.md`):
`screen-check --url ... --expect "<text>"` (one rendered page, one moment,
text only) and `screen-script` (a bounded list of at most 12 open/click/
type/expect steps). Neither takes a screen width, neither gives access to the
browser's own console, and neither can run a general-purpose automated
accessibility checker. The original skill assumed a researcher's own browser
with all of that, so its steps are rewritten to what NMHP's route can
actually do, and the rest is removed rather than left as an instruction the
agent cannot follow.

### Section "4. Run the real thing"

Old:
> - Start the app and walk the main journeys yourself. Tests passing does not show the app starts, the screen renders, or the pieces are wired.
> - For screens: check a phone width and a desktop width; check loading, empty, error; check the console for errors.
> - Start from a clean state, following the README exactly, as a new person would.

New:
> - Start the app and prove the main journeys through NMHP's own browser route: `screen-check --expect "<text>"` for one rendered page, or a bounded `screen-script` (at most 12 steps: open, click, type, expect) for a sequence of clicks and typing. Tests passing does not show the app starts, the screen renders, or the pieces are wired.
> - Check loading, empty and error states this way. The browser route takes no size, so a phone-width versus desktop-width check is not possible through it; where the project has its own rendered test suite that starts an isolated browser at a given width, use that instead, and otherwise say plainly that the two widths were not checked. The route gives no access to the browser's console, so console errors are not checked by it; say so plainly rather than claim it.
> - If the browser route is unavailable, say so plainly instead of claiming it was used.
> - Start from a clean state, following the README exactly, as a new person would.

### Section "5. Check the quiet qualities" (accessibility bullet)

Old:
> - **Accessibility:** keyboard-only walk of the main journey; visible focus; labels on inputs; sufficient contrast; an automated accessibility check where the project has one.

New:
> - **Accessibility:** labels on inputs, sufficient contrast, and a focus order you can reason about from the markup. NMHP's browser route clicks and types by text or selector, not by real keyboard events, so a true keyboard-only walk and any automated accessibility check are not something it can prove; say so plainly rather than claim them.

### "Proof to leave"

Old:
> - Evidence of the real run: what was walked, on which screen sizes.

New:
> - Evidence of the real run: the `screen-check`/`screen-script` id for what was walked, and which screen sizes a separate rendered test suite (not NMHP's browser route) actually checked, if any.

## No other edit

The other eleven skills were scanned against NMHP's `logins`, `owner-actions`,
`own-tool-limits`, `hermetic-commands` and `security-baseline` chapters. None
of their sentences instructs an agent to do something those chapters
forbid (for example, `frontend`'s and `ux-ui-design`'s own phone/desktop-width
and browser-console lines are ordinary good practice, not an instruction
that conflicts with a stated NMHP rule, so they are left exactly as written;
an agent following them reports what it could and could not check, the same
way `qa-testing` now says to). No correction was needed outside `qa-testing`.

## `ux-ui-design`: replaced at the owner's order (2026-11-21)

The owner ordered the screens to be part of the contract and "let the skill be one of the best UX skills out there for the time
being". The whole of `ux-ui-design/SKILL.md` is replaced by a guide built from the strongest public UX skills of November 2026
(see its Credits section), adapted to NMHP's own browser route (`screen-check`, `screen-script`) and to the contract's screen
designs, with NMHP's house standard. The reference chapters in `ux-ui-design/references/` are the UX Designer skill's, kept word
for word under their MIT license (`references/LICENSE`, `references/SOURCE`).

Old: the 99-line guide delivered in `SKILL_LIBRARY_SKILLS.md` (sections: Use when; First, read what exists; Size it to the task;
How to work 1-8; Rules an expert does not break; Common failures; Proof to leave; Hand-offs), kept in git history at main 5c69909.

New: `ux-ui-design/SKILL.md` as it stands now (sections: The NMHP house standard; Workflow 1 design the screens before the
contract; Workflow 2 build to the design; Workflow 3 judge built screens; A small visual system; Generated-looking defaults to
refuse; Words; Forms; Apps that use AI; Numbers that matter; Rules an expert does not break; Proof to leave; Hand-offs; Reference
routing; Credits).
