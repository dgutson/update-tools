# deps — an AI-powered dependabot that answers "is it worth it?"

Dependabot and Renovate tell you a new version exists. This tells you whether
you should care.

It builds a **usage profile** of your project — not a dependency list, but a
record of which symbols your code actually consumes from each library — then
intersects what changed upstream with that surface. A change only matters if it
touches something you call.

For C/C++ it does not take the changelog's word for what changed: it diffs the
library's **public headers** between the tag you are pinned to and the tag you
are considering, and intersects the result with your call sites.

```
^  hegel        0.7.4 -> 0.11.1  (6 behind, minor, scope=test)
   we call: HEGEL_TEST, hegel::TestCase, hegel::generators, hegel::stateful
^  fluidsynth   2.3.4 -> 2.6.0   (21 behind, minor, scope=runtime)
   we call: FLUID_FAILED, fluid_ramsfont_t, fluid_synth_noteon, new_fluid_synth …
   graphify: 8 direct callers, 10 functions transitively reach it
   api: v2.3.4 -> v2.6.0, 17/17 public header(s)
     !! fluid_ramsfont_t — removed (alias) at src/audio.cpp:4
=  libremidi    5.4.3 — current
```

That last finding is not from a changelog. FluidSynth dropped the
`fluid_ramsfont` API in 2.4.0; the tool established it by reading the headers at
both tags and matching the result against a type this project uses at a specific
line.

Note what kind of thing it is. `fluid_ramsfont_t` is a **type**, `FLUID_FAILED`
an **enum constant**, `new_fluid_synth` a constructor that does not carry the
library's prefix — none of them a `prefix_name(` call. Most of a C library's API
is not shaped like a function call, and a breaking change in the part you cannot
see is one you never hear about.

## Why this exists

| Tool | Gap |
|---|---|
| Dependabot / Renovate | No judgement. Renovate covers Conan and CPM, but not URL-pinned `FetchContent` archives or `pkg_check_modules` system libraries. |
| [llm-dependency-bot](https://github.com/SeanZoR/llm-dependency-bot) | Reactive — reviews a dependency PR something else had to open first, so it inherits Renovate's blind spots. |
| [DepAdvisor](https://github.com/chaubes/depadvisor) | Closest prior art: LLM risk reports from manifests. Python/npm/Maven only; C++ and system packages explicitly out of scope. |
| [fossabot](https://fossa.com/blog/fossabot-dependency-upgrade-ai-agent/) | Proprietary SaaS. |
| Dependabot → AI agent handoff | Security alerts only; never answers "is this feature release worth taking". |

None of them read *your* code to see how you use the library, and none handle
the three worlds a C++ project lives in at once: fetched archives, pkg-config
system libraries, and `find_package`.

## "Isn't this an SCA tool?"

Reasonable question, and the answer decides how much of this repo should exist.
Software Composition Analysis tools are very good at *finding* dependencies, so
`deps` should not be rebuilding that. Below is what five of them actually cover
across the two languages this targets, read from their source in **2026-08**
rather than from their marketing.

The short version: **dependency discovery is a solved problem in every ecosystem
except CMake, and no SCA tool answers "is it worth upgrading".** So `deps` should
ingest them for discovery and spend its own effort on the judgement.

### Coverage matrix

`extract` = produces a usable dependency list. `fingerprint` = identifies the
project itself for CVE matching, which is not the same thing and is easy to
mistake for it.

| | C (autotools, pkg-config) | C++ (CMake, Conan, vcpkg) | Python |
|---|---|---|---|
| **[ORT](https://oss-review-toolkit.org/ort/docs/tools/analyzer)** | — | Conan 1.x/2.x, Bazel (docs say limited). No CMake. | Pip, Pipenv, Poetry. **No uv.** |
| **[Trivy](https://github.com/aquasecurity/trivy)** | — | Conan only (`parser/c/` has exactly one entry) | pip, pipenv, poetry, pyproject, pylock, **uv** |
| **[OWASP Dependency-Check](https://owasp.org/www-project-dependency-check/)** | `AutoconfAnalyzer` — *fingerprint* | `CMakeAnalyzer` — *fingerprint* | requirements.txt, Pipfile(.lock), poetry.lock — all `@Experimental`. **No uv.** |
| **[dependabot-core](https://github.com/dependabot/dependabot-core)** | — | **vcpkg**, bazel. No CMake, no Conan. | pip, pip-compile, pipenv, poetry, **uv** (own top-level ecosystem) |
| **[OpenSCA](https://github.com/xmirrorsecurity/opensca-cli)** | — | — | Pipfile(.lock), setup.py, requirements.txt/.in. **No poetry, no uv.** |

### Where that leaves each language

**C++ — nothing parses CMake dependencies.** Not one of the five. ORT's
documented fallback for an unsupported build system is to hand-write an ORT
project file or supply an SPDX document. Dependency-Check *has* a `CMakeAnalyzer`,
which looks like a counterexample until you read it: it is `@Experimental`, runs
in phase `INFORMATION_COLLECTION`, and matches only `project(NAME)`,
`set(VERSION …)` and `set(<name>_version …)`, calling
`addEvidence(VENDOR/PRODUCT/VERSION)`. Occurrences in that file of
`FetchContent`, `find_package`, `pkg_check_modules`, `CPMAddPackage`, `URL_HASH`,
`GIT_TAG`, `ExternalProject` and `target_link_libraries`: **zero each**. It
identifies the directory; it does not read what the directory depends on.

One detail makes the category difference exactly: Dependency-Check's
`set(<name>_version "…")` pattern is the same syntactic shape `deps` detects as a
[coupled pin](#coupled-pins). DC reads it as *"a product — look up its CVEs."*
`deps` reads it as *"a pin bound to another declaration — and here is the value
it must take at the target tag."* Same bytes, different question.

**C — same gap, same reason.** Dependency-Check's `AutoconfAnalyzer` is the
autotools twin of its CMake one: `@Experimental`, matches `AC_INIT` and package
variables in `configure.ac`, emits `addEvidence(PRODUCT/VENDOR/VERSION)`. It
fingerprints the project. A C project built with autotools and `pkg-config`
therefore has no dependency extractor in any of the five.

**Python — well covered, and `deps` is the weak one.** This is where the
challenge lands. dependabot-core ships, *per ecosystem*, a `file_fetcher`,
`file_parser`, `update_checker`, **`file_updater`** and a `metadata_finder` with
`changelog_finder` / `release_finder` / **`commits_finder`**. That is: find the
manifests, parse them, resolve the newest allowed version, **edit the manifest**,
and fetch the changelog, the release and the commits between two versions — four
of this tool's five deterministic layers, across ~35 ecosystems. Anything `deps`
adds by hand here is duplicated work, which is why the roadmap's "write more
parsers" item was deleted.

### Two findings that shaped the design

**Only Trivy and dependabot know about `uv`.** ORT, Dependency-Check and OpenSCA
have no uv support at all. If uv matters to you, those three cannot see your
dependencies.

**Almost nothing records *where* a dependency is declared.** This is the
structural reason `deps` keeps its own parsers rather than becoming a pure
frontend. To bump a pin you need the line, not the package name:

| Tool | Declaration location |
|---|---|
| Trivy | `Package.Locations[]{StartLine, EndLine}` — **populated** for `pip` (requirements.txt), `pom`, `conan`, `cargo`; **not** for `uv`, `poetry`, `pyproject`, `package.json`, `go.mod` |
| dependabot-core | file only — a requirement is `{requirement, file, groups, source}`; `dependency.rb` contains no line concept. Its `file_updater` re-parses content instead. |
| ORT | file only — `Project.definitionFilePath`. `Project.kt`, `Package.kt`, `PackageReference.kt` and `Scope.kt` contain no line concept at all. |
| Dependency-Check | file only, as CPE evidence |

So a dependency ingested from an SCA tool is **report-only** in `deps`: it can be
profiled, judged and reported, but `apply` refuses to edit it rather than
guessing. That, not ecosystem coverage, is why the small native parsers stay.

### What is left that is genuinely this tool's job

Deliberately a short list:

1. **CMake / `pkg-config` / `FetchContent` discovery**, with the declaration site
   and raw pin needed to rewrite and re-hash. Nothing else does it.
2. **The usage profile** — which symbols of a dependency your code consumes, with
   `file:line`. Not a question SCA asks.
3. **The API-surface diff** and its intersection with that profile. Dependabot
   fetches the changelog; it does not check the changelog against the headers.
4. **Coupled pins** — detection *and* resolution against upstream's own build
   files at the target tag.
5. **The judgement.** The whole point, and the one part that is not a parsing
   problem.

That Dependabot and Renovate have excellent parsers is an argument for consuming
them, not for competing with them. See [ROADMAP item 7](ROADMAP.md) for the
ingest plan.

## Install

```bash
claude plugin marketplace add dgutson/update-tools
claude plugin install deps@update-tools-marketplace
```

No Python packages to install — `deptool` is pure standard library.

## Commands

| Command | What it does |
|---|---|
| `/deps:check` | The daily driver. Builds `CLAUDE_DEPS.md` on first run, then reports which updates are worth taking. |
| `/deps:sync` | Has the repo drifted from the profile? Refreshes only what changed, preserving assessments. |
| `/deps:rebuild` | Regenerate from scratch (discards assessments — asks first). |
| `/deps:apply <dep> [version]` | Bump a pin, re-hash the archive, build, and test. |

## CLAUDE_DEPS.md

A committed, reviewable markdown file — one section per dependency. Real output
from zeta-daw:

```markdown
## hegel

- kind: cmake-fetchcontent-url
- pinned: https://github.com/hegeldev/hegel-cpp/archive/refs/tags/v0.7.4.tar.gz — CMakeLists.txt:115
- version: 0.7.4
- integrity: SHA256=690d5dfb…
- scope: test
- upstream: github:hegeldev/hegel-cpp
- consumed:
  - HEGEL_TEST
  - hegel::TestCase
  - hegel::generators
  - hegel::stateful
- sites:
  - tests/configuration_test.cpp:4 — #include <hegel/hegel.h>
  - tests/loop_slot_fsm_test.cpp:49 — hegel::stateful
- companion-pins:
  - HEGEL_LIBHEGEL_VERSION=0.29.0 — CMakeLists.txt:123 (libhegel version required by Hegel C++ v0.7.4) [matched by name]
- scope-evidence:
  - declared under test guard at CMakeLists.txt:115
  - linked only into: configuration_tests, loop_timing_tests, midi_tests, …
- notes:
  - has coupled pin(s) that must be bumped together — changing this
    dependency alone can produce an ABI/link failure
- fingerprint: decl=a3f1e9c72b04 sites=7b22c4d19e60 pin=1f0e4a9c3d55

### Assessment

Test-only; never linked into the shipped binary, so the blast radius of a
bump is confined to CI. …
```

Five things make this more than a copy of the manifest:

- **`consumed` / `sites`** — the API surface, which is what turns a changelog
  into a decision.
- **`scope-evidence`** — *why* it concluded test-only, so you can disagree.
- **`companion-pins`** — coupled versions that must move together (see below).
- **`declarations`** — every place the dependency is pinned, when there is more
  than one (see [Manifests that disagree](#manifests-that-disagree)).
- **`### Assessment`** — prose written by Claude and **preserved across
  regenerations**. This is the accumulated understanding of what each
  dependency means to your project, and the reason the file gets more useful
  the longer you keep it.

The `fingerprint` lines hash each dependency's *own declaration block*, not its
file — so editing one dep in a shared `CMakeLists.txt` marks only that dep as
drifted. `/deps:sync` detects drift by hashing alone; no model call is needed
to notice that nothing changed.

## Manifests that disagree

A cross-platform project often keeps one manifest per target — a Conan file for
each of Linux, macOS and two Windows variants — and over time they drift apart.
Nothing about that is visible upstream: every version involved may be perfectly
current, so an update checker has nothing to say.

`deps` looks for manifests **anywhere in the tree**, keeps every declaration it
finds, and reports the disagreement as a finding of its own:

```
!= openssl          declarations disagree on the version — 3.2.1 in
   deps/linux/conanfile.txt:4, deps/macos/conanfile.txt:4; 3.5.0 in
   deps/windows/conanfile.txt:4 — so what ships depends on which manifest
   the build used
```

Two consequences worth knowing:

- The version compared against upstream is the **oldest** of the declared ones —
  the copy an advisory is most likely to match. "Two versions behind" is then the
  gap for the worst platform, and `declarations` says where the others are.
- `/deps:apply` **refuses** a dependency whose manifests disagree, rather than
  editing one and deepening the split. When they agree it bumps all of them in a
  single plan.

The same reconciliation fixes a quieter wrong answer. `find_package(CURL)` and a
`libcurl/8.9.0` pin are the same library under two names; left separate, the
version-less CMake declaration wins and the tool ends up comparing *your machine's*
system library against what distros ship — recommending a CI-image change for a
library the project statically links from its own manifest. The two declarations
are now folded into one dependency, with both names and both sites kept.

## Coupled pins

Some dependencies are pinned twice. zeta-daw pins Hegel as a source tarball
*and* as `HEGEL_LIBHEGEL_VERSION` for its prebuilt native engine. Bump the
source alone and the build configures cleanly, then dies at link time:

```
undefined reference to `hegel_settings_set_stateful_step_count'
```

That reads like a compiler problem, not a dependency problem. `deps` detects the
coupling, then works out what the companion has to become by reading the
dependency's *own* build files at the tag you are moving to — and bumps both in
one edit:

```
$ deptool plan --dep grpc --to 1.68.0
grpc: 1.60.0 -> 1.68.0 in CMakeLists.txt
coupled pins:
  +> GRPC_CORE_VERSION 37.0.0 -> 44.1.0  [declared]
     grpc/grpc@v1.68.0 CMakeLists.txt: set(gRPC_CORE_VERSION "44.1.0")
     ! the CACHE docstring still says 1.60.0 and will be stale after the bump
--- a/CMakeLists.txt
+++ b/CMakeLists.txt
@@ -5,11 +5,11 @@
 FetchContent_Declare(
     grpc
-    URL https://github.com/grpc/grpc/archive/refs/tags/v1.60.0.tar.gz
+    URL https://github.com/grpc/grpc/archive/refs/tags/v1.68.0.tar.gz
 )
 set(
     GRPC_CORE_VERSION
-    37.0.0
+    44.1.0
```

The value is evidence, not inference: it is quoted from a declaration upstream,
and it is cross-checked before being used. `deps` resolves the same variable at
the version **currently** pinned and compares it with what the repo already
says. If our reading reproduces the existing pin, the mechanism demonstrably
works for this dependency. If it disagrees, that is reported as a finding and
nothing is written — either the pin was set deliberately, or the wrong variable
is being read, and both need a human.

When the companion cannot be resolved at all, `apply` **refuses**:

```
$ deptool apply --dep grpc --to 1.68.0
refusing to bump grpc: unresolved coupled pin(s) — GRPC_NO_SUCH_PIN_VERSION (pinned 37.0.0, unresolved)
  !? GRPC_NO_SUCH_PIN_VERSION 37.0.0 -> unknown
     ! not declared in any build file read at 1.68.0 and not mentioned in its
       release notes — bumping grpc alone risks the link-time failure this pin
       exists to prevent
  set them by hand, or re-run with --ignore-companions to bump only grpc.
```

Refusing is the useful behaviour here: half the bump configures cleanly and
fails after a full build, having told you nothing. `--ignore-companions` is
available for the case where you have established the pins really are
independent.

Companion resolution reads the root `CMakeLists.txt` first, then `cmake/`
modules and other build metadata at that tag, and falls back to release-note
prose — marked as such, because prose is the weakest evidence in this tool.

## Not trusting the changelog

Release notes are the weakest evidence in a tool like this — often absent, often
marketing, never written with *your* call sites in mind. For a C/C++ dependency
the API surface is not a matter of opinion, so `deps` reads it directly:

```
$ deptool apidiff --dep fluidsynth --to 2.5.7
fluidsynth 2.3.4 -> 2.5.7 (FluidSynth/fluidsynth v2.3.4...v2.5.7)
  626 public declaration(s) -> 641, read 17/17 header(s)
    !! fluid_ramsfont_t — removed (alias) at src/audio.cpp:4
```

The surface is functions, types, aliases, macros **and enum constants** — a
renamed enumerator is as hard a compile break as a deleted function, and reading
only the enum's own tag left every constant you consume permanently unchecked.
A public header that exists in the repository only as a build-time template
(`src/sndfile.h.in`) counts too: libsndfile keeps its entire C API there, so
skipping it meant reading twenty *internal* headers and never seeing `sf_open`.

Three rules keep it from crying wolf, which matters more than coverage:

- **A removal must be absent from every header read at the target**, not just
  from the one it used to live in. Upstream moving a declaration between headers
  is routine and breaks nobody.
- **An incomplete read never reports a clean bill of health.** Big libraries
  exceed the per-dependency header budget, so headers naming a subsystem you
  consume are read first, and any symbol the diff never saw is listed separately
  as `not_located` — *unchecked, not unaffected*. Symbols you consume that look
  removed get a second, targeted search of the skipped headers before anything
  is reported.
- **An empty intersection with an empty input is not a result.** If nothing is
  known about what you consume, then "nothing you consume changed" is a fact
  about the extractor and not about the upgrade, and it is reported as
  *unmeasured, not unaffected*.

Noise suppression is deliberate and each rule came from a real false positive:
renaming a parameter is not a signature change, adding `override` to a virtual
is not a signature change, `FLUID_RESTRICT` on a parameter is not a type change,
renaming the opaque struct behind a handle typedef is not a change to the handle
(libsndfile did exactly that to `SNDFILE`, which every caller uses and none can
see inside), and an include guard is not API. Where a project has an `include/`
tree, nothing
outside it counts — FluidSynth's real surface is 17 headers, and counting `src/`
turned internal churn into 33 "removals" no consumer could have called.

Renames are reported as `[inferred]` — a removed and an added symbol sharing a
signature and a similar name — because a rename cannot be proved from two
snapshots.

Two prose fallbacks fill the gap for everything that is not a C/C++ header:
upstream's own `UPGRADING.md` / `UPGRADE-6.4.md` at the target tag (discovered
from the repo listing, and narrowed to the guides covering the versions you are
actually crossing), and the commit log between the two tags — used **only** when
the release notes are empty, with the subjects that announce a break flagged.

## Analysis backends

Consumed-symbol extraction is pluggable and auto-detected:

| Backend | Detected via | Adds |
|---|---|---|
| `builtin` | always available | includes/imports → symbols → `file:line` sites |
| [`graphify`](https://github.com/Graphify-Labs/graphify) | `graphify-out/graph.json` | blast radius: direct and transitive callers |
| [`codebase-memory`](https://github.com/DeusData/codebase-memory-mcp) | binary on `PATH` | caller counts via `trace_path` — **untested, see [roadmap 10](ROADMAP.md)** |

`builtin` always runs — it produces the auditable `file:line` sites. A graph
backend enriches rather than replaces. Nothing breaks if you have neither.

### How a symbol gets attributed to a library

Everything above depends on this list, so it is worth knowing how it is built and
where it stops. `builtin` attributes a file to a dependency by its `#include`,
then harvests from that file in three ways:

- **`ns::Name`**, for a C++ library with a namespace.
- **the library's own prefix**, when that prefix ends in `_` and so acts as a
  namespace marker — `fluid_synth_t` counts whether or not it is being called,
  and so does `new_fluid_synth`, which carries the prefix in the middle. A prefix
  that is a bare stem instead (zlib's `deflate`, `compress`) still has to look
  like a call, because such a token is about as likely to be an English word.
- **upstream's own declared names.** The prefix comes from the *package* name,
  which is often not the API's: libsndfile yields `sndfile_`, its API is `sf_open`
  and `SF_INFO`, and the guess matches nothing at all. So the header diff feeds
  its own reading back — a name upstream declares that your sources mention is
  consumed, no guess required. It costs no extra fetches, since those headers
  were read anyway, and names *your* code declares are excluded so your `Node`
  stays yours rather than becoming yaml-cpp's.

Comments, string literals and include paths are not uses, so a symbol named in a
log message does not count. Where the harvest comes back empty on a dependency
your code demonstrably includes, that is recorded as a gap in the profile rather
than left to look like an unused dependency.

What it still misses, all of it on the [roadmap](ROADMAP.md): a method called on
an object of a dependency's type (`node.as<int>()`), a symbol reached only
through one of your own `typedef`s, and enum *values* — a reordered enum is an
ABI break that reads as unchanged.

To use graphify:

```bash
uv tool install graphifyy
cd your-repo && graphify update .    # tree-sitter only, no LLM, no API key
```

## What it understands

**C/C++** — `FetchContent_Declare` (URL and `GIT_TAG`), `CPMAddPackage`,
`find_package` (including `EXACT`), `pkg_check_modules`, Conan, vcpkg. Follows
`include()` and `add_subdirectory()`. Tracks `if()` nesting and
`target_link_libraries` edges to separate test-only dependencies from shipped
ones.

**Others** — npm, Cargo, PyPI (`pyproject.toml` / `requirements.txt`), Go
modules.

**Manifests anywhere in the tree**, not just at the repository root, so a project
keeping one manifest per target platform in subdirectories is read properly —
including when those manifests contradict each other. Build output, package
caches and trees declared as submodules in `.gitmodules` are skipped, since a
vendored project's manifest declares *its* dependencies rather than yours.

**Unpinned system libraries** get honest treatment: there is no version in the
repo to bump, so it reports what is installed locally versus what distros ship
(via Repology), and frames the action as a CI-image or documented-minimum
change rather than a code edit.

### Pinned dependencies are the point

Pinned-ness determines *where the fix goes*, never *whether to recommend it*.

An unpinned system library drifts forward for free every time the distro or CI
image updates. A pin does not: it is a snapshot of one moment, and nothing will
ever move it on its own. **Pinned dependencies are the only ones that rot
silently** — which is precisely why they are the primary target here, not a
special case to tiptoe around.

So `deps` never softens a recommendation because something is pinned,
deliberately pinned, hash-pinned, or `EXACT`-pinned. A raw commit SHA is
treated as a *rot signal* rather than a keep-out sign: it usually means someone
froze the dependency to dodge a specific problem and never revisited it, and
the reason is often years stale and undocumented. A comment explaining *why*
a pin exists is real evidence and gets weighed; the absence of one is evidence
too, in the other direction.

## Using the CLI directly

The plugin is a wrapper; the tool stands alone.

```bash
python3 -m deptool --root ~/src/zeta-daw profile      # write CLAUDE_DEPS.md
python3 -m deptool --root ~/src/zeta-daw status       # drifted? (no network)
python3 -m deptool --root ~/src/zeta-daw check        # upgrade evidence
python3 -m deptool --root ~/src/zeta-daw apidiff --dep fluidsynth --to 2.5.7
python3 -m deptool --root ~/src/zeta-daw plan  --dep hegel --to 0.11.1
python3 -m deptool --root ~/src/zeta-daw apply --dep hegel --to 0.11.1 --verify
python3 -m deptool --root ~/src/zeta-daw revert --dep hegel
```

`--json` on `profile`, `status`, `check` and `apidiff` gives machine-readable
output. `check` runs the header diff automatically for C/C++ dependencies with a
readable GitHub upstream; `--no-api-diff` skips it and `--max-headers N` widens
the budget.

### What touches your files, and when

Everything except `apply` is read-only. `profile` writes `CLAUDE_DEPS.md` and
nothing else; `check`, `status`, `apidiff` and `plan` write nothing at all —
`plan` exists precisely so you can see the edit before agreeing to it.

`apply` edits your manifest, and only when you run it. Two things keep that
contained:

- **`--verify` builds in a throwaway copy of your tree, and applies only if the
  build passes.** A bump that does not compile leaves your checkout exactly as it
  was, and no `build/` directory appears in it. VCS metadata is not copied, so a
  build that derives its version from `git describe` may need `--in-place`, which
  restores the old edit-then-build behaviour.
- **Backups live outside your checkout**, under
  `${XDG_CACHE_HOME:-~/.cache}/deptool/backups/`, so an apply adds nothing to
  `git status`. `revert` restores from there.

## Development

```bash
uv sync          # installs the dev group from uv.lock
uv run pytest -q
```

`pytest` is a dev-group dependency. `[project.dependencies]` is empty **and
must stay empty** — the plugin runs `deptool` as `python3 -m deptool` on
whatever interpreter it finds, with no install step, so anything added there
becomes a runtime requirement for every repository the plugin is pointed at.
The test suite runs under `uv run`; the tool itself is exercised with a bare
`python3`, precisely to keep that contract honest.

## Status

v0.1.0. Developed and verified against [zeta-daw](https://github.com/dgutson/zeta-daw)
(C++20/CMake). See [ROADMAP.md](ROADMAP.md) — including
[item 10](ROADMAP.md), an explicit list of code paths not yet run against
reality.

## Licence

MIT
