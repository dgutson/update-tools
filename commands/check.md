---
description: Check dependencies for updates worth taking, building CLAUDE_DEPS.md on first run
argument-hint: "[dependency name to focus on]"
allowed-tools: Bash, Read, Edit, Write, Glob, Grep, WebFetch
---

Review this repository's dependencies for updates worth taking.

Use the `dep-review` skill. Follow its `/deps:check` workflow:

1. `python3 -m deptool --root . status` — if the profile is missing, build it
   first with `profile` and tell the user this is the first run.
2. `python3 -m deptool --root . check --json` for the evidence.
3. Judge each dependency with the skill's rubric and write the ranked report.
4. Update any empty or now-wrong `### Assessment` block in `CLAUDE_DEPS.md`.

$1 — if a dependency name was given, focus the report on it (pass
`--only <name>` to `check`), but still mention anything in ACT NOW.
