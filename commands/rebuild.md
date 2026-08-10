---
description: Regenerate CLAUDE_DEPS.md from scratch, discarding existing assessments
allowed-tools: Bash, Read, Edit, Write, Glob, Grep
---

Rebuild `CLAUDE_DEPS.md` from scratch for this repository.

Use the `dep-review` skill, `/deps:rebuild` workflow.

**Confirm with the user before running.** `profile --force` discards every
`### Assessment` block — the accumulated judgement about what each dependency
means to this project. If they only want the machine fields refreshed, that is
`/deps:sync`, which preserves the prose. Make sure that is not what they meant.

After confirmation:

1. `python3 -m deptool --root . profile --force`
2. Write a fresh `### Assessment` for every dependency, grounded in the
   consumed-symbol evidence in the regenerated file.
3. Report which backend was used, and how many symbols and sites were found
   per dependency.
