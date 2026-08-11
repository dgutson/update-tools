---
description: Apply a dependency version bump, re-hash the archive, and verify the build
argument-hint: "<dependency> [target version]"
allowed-tools: Bash, Read, Edit, Write, Glob, Grep, WebFetch
---

Apply a version bump for dependency `$1`$2.

Use the `dep-review` skill, `/deps:apply` workflow. In order:

1. If no target version was given, run `check --only $1` and propose one, with
   the reasoning for that specific version rather than simply "latest".
2. `python3 -m deptool --root . plan --dep $1 --to <version>` — show the user
   the diff, the recomputed archive hash, and any coupled pin the plan moves
   with it. This writes nothing.
3. **Get explicit confirmation.** This edits their build files.
4. `python3 -m deptool --root . apply --dep $1 --to <version> --verify`
5. Report each verification step honestly. A step reported as `skipped`
   (missing `cmake`/`ctest`) is not a pass — say so.
6. If verification fails, show the failing output and offer
   `python3 -m deptool --root . revert --dep $1`.

If `apply` exits 2, it refused because a coupled pin could not be resolved.
Relay what it could not establish and stop there. Do not retry with
`--ignore-companions` unless the user decides the pins are independent — that
flag reinstates exactly the silent link-time failure the check exists to catch.

Do not bundle several dependencies into one run. One bump at a time is what
makes a broken build diagnosable.
