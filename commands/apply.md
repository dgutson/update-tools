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
   This builds the bump in a **throwaway copy** and writes to their tree only if
   it passes, so a bump that does not compile leaves the checkout untouched and
   drops no `build/` directory into it.
5. Report each verification step honestly. A step reported as `skipped`
   (missing `cmake`/`ctest`) is not a pass — say so. The tool applies anyway in
   that case, because nothing was disproved; make clear that nothing was proved
   either.
6. If it exits 3, verification failed and **nothing was written**. Show the
   failing output. Do not reach for `revert` — there is nothing to revert. Only
   suggest `--in-place` if the failure looks like missing VCS metadata (a build
   that needs `git describe`), and say that it edits their tree before building.
7. Backups go to `${XDG_CACHE_HOME:-~/.cache}/deptool/backups/`, not beside the
   file, so an apply adds nothing to `git status`.
   `python3 -m deptool --root . revert --dep $1` restores from there.

If `apply` exits 2, it refused because a coupled pin could not be resolved.
Relay what it could not establish and stop there. Do not retry with
`--ignore-companions` unless the user decides the pins are independent — that
flag reinstates exactly the silent link-time failure the check exists to catch.

Do not bundle several dependencies into one run. One bump at a time is what
makes a broken build diagnosable.
