# deps — an AI-powered dependabot that answers "is it worth it?"

Dependabot and Renovate tell you a new version exists. This tells you whether
you should care.

It builds a **usage profile** of your project — not a dependency list, but a
record of which symbols your code actually consumes from each library — then
intersects upstream release notes with that surface. A changelog entry only
matters if it touches something you call.

```
^  hegel        0.7.4 -> 0.11.1  (6 behind, minor, scope=test)
   we call: HEGEL_TEST, hegel::TestCase, hegel::generators, hegel::stateful
^  fluidsynth   2.3.4 -> 2.5.7   (20 behind, minor, scope=runtime)
   we call: fluid_synth_noteon, fluid_synth_cc, fluid_synth_sfload …  (19 total)
   graphify: 8 direct callers, 10 functions transitively reach it
=  libremidi    5.4.3 — current
```

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

Four things make this more than a copy of the manifest:

- **`consumed` / `sites`** — the API surface, which is what turns a changelog
  into a decision.
- **`scope-evidence`** — *why* it concluded test-only, so you can disagree.
- **`companion-pins`** — coupled versions that must move together (see below).
- **`### Assessment`** — prose written by Claude and **preserved across
  regenerations**. This is the accumulated understanding of what each
  dependency means to your project, and the reason the file gets more useful
  the longer you keep it.

The `fingerprint` lines hash each dependency's *own declaration block*, not its
file — so editing one dep in a shared `CMakeLists.txt` marks only that dep as
drifted. `/deps:sync` detects drift by hashing alone; no model call is needed
to notice that nothing changed.

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

## Analysis backends

Consumed-symbol extraction is pluggable and auto-detected:

| Backend | Detected via | Adds |
|---|---|---|
| `builtin` | always available | includes/imports → symbols → `file:line` sites |
| [`graphify`](https://github.com/Graphify-Labs/graphify) | `graphify-out/graph.json` | blast radius: direct and transitive callers |
| [`codebase-memory`](https://github.com/DeusData/codebase-memory-mcp) | binary on `PATH` | caller counts via `trace_path` — **untested, see [roadmap 10](ROADMAP.md)** |

`builtin` always runs — it produces the auditable `file:line` sites. A graph
backend enriches rather than replaces. Nothing breaks if you have neither.

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
python3 -m deptool --root ~/src/zeta-daw plan  --dep hegel --to 0.11.1
python3 -m deptool --root ~/src/zeta-daw apply --dep hegel --to 0.11.1 --verify
python3 -m deptool --root ~/src/zeta-daw revert --dep hegel
```

`--json` on `profile`, `status` and `check` gives machine-readable output.
`plan` writes nothing; `apply` leaves a `.deptool.bak` beside the edited file.

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
