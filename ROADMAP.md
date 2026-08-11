# Roadmap

Status of `deps` v0.1.0, and where it goes next.

Priorities below are ordered by how much they change the quality of the
*answer*, not by how much code they need. Items marked **(observed)** came out
of running the tool against a real project (`dgutson/zeta-daw`) rather than
from speculation.

---

## Shipped in 0.1.0

- CMake dependency extraction: `FetchContent_Declare` (URL and `GIT_TAG`),
  `CPMAddPackage`, `find_package`, `pkg_check_modules`, Conan, vcpkg.
  Follows `include()` / `add_subdirectory()`.
- Scope inference from `if()` nesting, `FetchContent_MakeAvailable` placement,
  and `target_link_libraries` edges — separates test-only from shipped.
- npm, Cargo, PyPI, Go module manifests.
- Consumed-symbol extraction with `file:line` sites (builtin backend).
- Graphify backend: blast radius via reverse BFS over `calls` edges, falling
  back to `references` edges for symbols consumed through a macro (which
  graphify emits with no call edges at all).
- Additive backends: every detected backend runs and their findings merge, so
  a backend that covers only part of the dependency set cannot mask another.
- Upstream resolution: GitHub releases/tags, PyPI, npm, crates.io, Repology
  (for distro-shipped system libraries).
- OSV.dev advisory lookup.
- `CLAUDE_DEPS.md` with per-declaration fingerprints and preserved
  `### Assessment` prose.
- Bump application with archive re-hashing, declaration-scoped edits,
  build/test verification, and revert.
- Coupled-pin detection **and resolution** **(observed)** — reads the
  dependency's own build files at the target tag to find what its companion
  version must become, cross-checks the extraction against the currently pinned
  version, and bumps both in one atomic edit. `apply` refuses when a companion
  cannot be resolved.

---

## Near term

### 1. Companion pins upstream declares nowhere **(observed)**

Resolution now ships (see above) and works when upstream states the requirement
in a build file at the tag, or names it in release-note prose. Verified against
real repositories: `grpc/grpc` moves `gRPC_CORE_VERSION` from `37.0.0` to
`44.1.0` between `v1.60.0` and `v1.68.0`, and that is resolved, cross-checked
and applied as one edit.

What is still unsolved is the case that motivated the feature. zeta-daw's
`HEGEL_LIBHEGEL_VERSION` corresponds to a **prebuilt binary release**, and if
Hegel does not declare the pairing in a file we read, there is nothing to
extract — resolution correctly reports `unresolved` rather than guessing, and
`apply` correctly refuses, but the upgrade is still blocked. Ways in, roughly by
expected value:

- **Release assets.** A prebuilt engine ships as a named artefact
  (`libhegel-0.31.0-linux-x86_64.tar.gz`); the GitHub release asset list for the
  target tag often *is* the answer.
- **Lockfiles and CI config** in the dependency's own repo — the version its own
  CI installs is the version it was tested against.
- **Try-and-see**, once verification runs in a sandbox (item 3): with several
  candidate companion versions, "which pair builds?" is answerable mechanically.

Two smaller gaps in what shipped: only GitHub upstreams can be read (a
`distro:`/`pypi:` dependency reports `unresolved` immediately), and a pin
attached by proximity alone is flagged but never confirmed.

### 2. Trust the changelog less

Release notes are the weakest evidence in the pipeline — often absent, often
marketing. Add, in order of preference:

- **Tag-to-tag diff of public headers.** For a C++ dependency, the API surface
  is in the headers; diffing them between two tags gives a *factual* breaking
  change list instead of a prose summary. Intersect that with `consumed` and
  the report stops depending on whether upstream writes good notes.
- Commit-log fallback between tags when notes are empty.
- Upstream migration guides (`MIGRATING.md`, `UPGRADING.md`) when present.

### 3. Verify in a sandbox, not in place

`apply --verify` currently edits the working tree and leaves a `.bak`. It
should build in a git worktree or a copy, so a failed verification never
touches the user's checkout and several candidate versions can be tried in
parallel — "does 0.9.0 build even though 0.11.1 doesn't?" is usually the more
useful question than a binary pass/fail on latest.

### 4. Bisect to the last good version

When latest fails to build, walk backwards to find the newest version that
does. For zeta-daw's Hegel that turns "blocked" into "0.x works today, and
here's what it would take to reach 0.11.1".

---

## Medium term

### 5. Better system-dependency handling

`pkg-config` deps have no version in the repo — the build box decides. Current
output reports installed-vs-packaged. Missing:

- Read CI workflows (`.github/workflows/*.yml`, Dockerfiles) to find the
  versions CI actually installs, which is the version that matters for
  reproducibility.
- Flag divergence between developer machines and CI.
- Distinguish "distro ships 2.5.7" from "your LTS will never ship 2.5.7",
  since the latter makes the upgrade a packaging decision, not a code one.

### 6. Symbol-level breaking-change matching

Today the intersection of "what changed" and "what we call" is done by the
model reading both lists. With header diffs (item 2) it can become mechanical:
removed/renamed/re-signatured symbols matched directly against `consumed`, with
`file:line` sites for each affected call. The model then explains and
prioritises rather than pattern-matching.

### 7. More ecosystems

Gradle/Maven, NuGet, Swift Package Manager, Bazel `MODULE.bazel`, Nix. Each is
a parser plus an upstream resolver; the judgement layer is unchanged.

### 8. Transitive dependencies

Only direct dependencies are profiled. A CVE usually lands in a transitive one.
Needs lockfile parsing per ecosystem, plus a way to say "we do not call this,
but our dependency does" without drowning the report.

---

## Longer term / open questions

### 9. Fleet mode

Explicitly cut from 0.1.0 — the design is per-repo. If it returns, the shape
is a `repos.yaml`, one `CLAUDE_DEPS.md` per repo (still committed), and a
cross-repo report that ranks by aggregate exposure: *"fluidsynth is behind in
four of your projects and one of them calls the affected function."*

### 10. Untested code paths

Honest list of what has **not** been run against reality:

- **`codebase-memory` backend** — written against published docs, exercised
  only by unit-test fakes. The CLI shape (`cli trace_path --direction
  inbound`) is documented, not verified. Consequently the additive merge has
  never run with two *live* backends: only graphify has ever contributed to a
  real profile, so the largest-wins rule in `_record` is untested in anger.
- **Non-C++ extraction** — the npm/Cargo/PyPI/Go parsers have unit tests but
  have not been run against a substantial real project.
- **Conan and vcpkg** — parsed, never exercised end-to-end.
- **Companion resolution** has been run end-to-end against `grpc/grpc` and
  `FluidSynth/fluidsynth`, covering the resolved, unchanged, diverged and
  unresolved paths — but only for CMake `set()` pins on GitHub-hosted
  dependencies. The release-notes fallback is unit-tested only; no real release
  note has ever been the source of a companion version.

### 11. Should this also open PRs?

Currently it edits the working tree and stops. Opening a branch + PR with the
reasoning as the description is a small step, but it changes the tool from
advisory to actor. Worth doing only once the recommendations have proven
trustworthy over some real upgrades.

### 12. Calibration

There is no feedback loop. If a "WORTH IT" recommendation gets rejected, or a
"LOW VALUE" one turns out to have mattered, nothing learns from it. The
`### Assessment` blocks are the natural place to record outcomes — a dependency
whose last three recommendations were ignored should probably stop being
recommended.
