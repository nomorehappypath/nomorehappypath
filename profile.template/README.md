# Profile — where you add your own project's requirements

The **engine** (`../engine/`) is generic and you never edit it. The **profile** is where
*your* project lives. To adopt the harness on a new project:

## 1. Copy this template

```
cp -r profile.template profile
```

## 2. Fill `profile/profile.config`

Set every value (product name, owner, repos, the two AI vendors, DB, tooling, deployment
channels, …). Each key carries a CRE example showing the shape of a real profile.

## 3. Write your own domain QA appendices

Replace `profile/qa/EXAMPLE_domain_qa_appendices.md` with your project's own
domain-specific QA scenarios — the equivalent of the CRE cancel/continue matrix and
Project-Analysis checklist in the example. The engine ships the *method* (how to model a
state machine, how to build a coverage matrix); you supply the *content* for your domain.

## 4. Compose

A composition step substitutes your `profile.config` values into the engine's
`{{PROFILE:key}}` tokens, producing the active directive set your agents read.
*(The composition tooling is the next build step — see `../docs/decisions.md` O4.)*

## The one rule that makes this work

- **Edit the profile, never the engine.** Engine files are upgradeable; the moment you
  edit one per-project, you fork off future improvements. Anything project-specific
  belongs here in the profile.

## The non-negotiable you inherit from the engine

Whatever you put in `implementer_vendor` / `reviewer_vendor`, they must be **different
vendors**, and the one that did NOT write a change is the one that reviews it. That
cross-vendor review gate is the heart of the harness — don't collapse it to one vendor.
