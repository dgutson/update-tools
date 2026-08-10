---
description: Check whether CLAUDE_DEPS.md has drifted from the repository, and refresh what changed
allowed-tools: Bash, Read, Edit, Write, Glob, Grep
---

Check whether `CLAUDE_DEPS.md` still matches this repository, and repair it if not.

Use the `dep-review` skill, `/deps:sync` workflow:

1. `python3 -m deptool --root . status --json`.
2. If the verdict is `current` and nothing is unassessed, say so in one line
   and stop — do not re-analyse an unchanged repository.
3. Otherwise run `python3 -m deptool --root . profile` (this preserves existing
   assessments), then rewrite the `### Assessment` blocks only for the
   dependencies reported as `added` or `drifted`.

Report what drifted and why — a changed pin, a changed declaration file, or
changed call sites are different situations and the user should know which.
