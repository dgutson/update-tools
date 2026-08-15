# Roadmap

> Pending work only — finished items move to HISTORY.md.
> This is the durable record of what's outstanding. Read it instead of reconstructing
> the state of play from git history, old conversations, or a sweep of the code.
> Next thing to work on: the first item under the earliest horizon whose **Blocked-by**
> entries are no longer present in this file.

Format: 1
Next ID: R-035

**Targets.** C/C++ (CMake) and Python are both first-class. npm, Cargo and Go are
supported but not a priority.

**Read [HISTORY.md](HISTORY.md) before starting an item.** Most items below have a
rejected design or a measurement behind them, and the reasoning is the load-bearing
part.

---

## Standing rules

Not aspirations — the constraints every item is judged against. Each exists because
breaking it produced a wrong answer.

1. **The tool never touches project files except on an explicit `apply`.**
   `apply_mod.write` must stay reachable only from `cmd_apply`.
2. **A generated file is never edited.** Lockfile pins are marked, excluded from
   `is_editable()`, and reported as `regenerate`.
3. **Read the declared fact; do not tabulate guesses.** Mechanisms that read what
   upstream declares generalise. Tables of constants fitted to one project do not.
   `CANONICAL_ALIAS` ships empty on purpose. No new entry without a cited case.
4. **Unmeasured is not unaffected.** Every partial read must say so —
   `truncated`, `not_located`, `consumed_count`, `patches_known`. A missing answer
   must never render as a clean one.
5. **Only cut something if there is a benefit in runtime, accuracy, or context.**
   Measure first, and give a straight accounting of the diff when asked.
6. **Before adding a parser, read Prior art in HISTORY.** Discovery is solved
   everywhere except CMake; ingest it rather than reimplement it.

---

## Now

### R-002 — Fix the codebase-memory invocation and the false backend docstrings

- **Category:** Graph backends
- **What:** In `deptool/backends/__init__.py:264`, change `index_repository --path R
  --project P` to `--repo-path R --name P`, and check the return code instead of
  discarding it. Correct two claims in the module docstring: line 9 ("Graphify
  resolves the C ABI but cannot see namespaced C++") and lines 116-118 ("Graphify
  emits the dependency's own symbols as nodes"), and the same claim repeated in the
  `test_backends_merge_additively` docstring. Then give the enricher a site-based
  seed as `_enrich_graphify` now has, fix `_collect_names` (it scrapes the table
  headers `Function` and `Method` out of the tool's output and reports them as
  function names), and flip `CODEBASE_MEMORY_ENABLED` back on.
- **Why:** `--project` is rejected outright with `unknown flag`, and because the
  return code is discarded the failure is silent: every subsequent `trace_path` then
  queries an unindexed project, so the enricher returns `False` having contributed
  nothing. codebase-memory could never have worked, even once installed. Both
  docstring claims were written from published docs rather than measurement and are
  contradicted by `extract.py:858` and by the seed measurement in R-001.
- **Outcome:** `codebase-memory` indexes successfully when installed, a failed index
  is reported rather than swallowed, the module docstring describes what the backends
  actually do, and the enricher contributes again — seeded from sites, never from
  symbol names.
- **Blocked-by:** —
- **Enables:** R-014

### R-003 — Record declaration lines for pyproject dependencies

- **Category:** Python
- **What:** Add a line-recording scan of `pyproject.toml` beside the `tomllib` parse
  in `deptool/discover._pyproject`, so each `Dep.declared_in` carries `path:line`
  rather than a table name.
- **Why:** `declared_in` is currently `"pyproject.toml (project.dependencies)"` — a
  table name with no line — so by the report-only rule every pyproject dependency is
  unappliable and `apply` cannot touch it. `tomllib` discards positions, so the parse
  alone cannot supply this.
- **Outcome:** Every dependency declared in a `pyproject.toml` has a line number and
  is appliable by `plan`/`apply`.
- **Blocked-by:** —
- **Enables:** —

### R-004 — Read Poetry manifests

- **Category:** Python
- **What:** Teach `deptool/discover._pyproject` to read `[tool.poetry.dependencies]`
  and `[tool.poetry.group.*.dependencies]`. The schema is name → constraint where the
  value is either a string or a `{version, extras, markers, optional}` dict; the
  `python` key is a marker, not a dependency. `_PY_REQ` also cannot parse Poetry's
  caret/tilde operators (`^2.0`, `~1.4`) — `_cargo` already strips these at
  `discover.py:200`.
- **Why:** Poetry's tables are not read at all, so a Poetry project reports **zero
  dependencies**. That is a wrong answer, not a coverage gap, and it is the loudest
  failure in the tool.
- **Outcome:** A Poetry project reports its real dependency set with scopes, and
  caret/tilde pins resolve to versions rather than being dropped.
- **Blocked-by:** —
- **Enables:** —

## Next

### R-005 — Read PEP 735 `[dependency-groups]`

- **Category:** Python
- **What:** Read the `[dependency-groups]` table in `deptool/discover._pyproject`.
- **Why:** It is the modern standard, what `uv` uses, and what this repo's own
  `pyproject.toml` uses — so the tool currently reports zero dependencies for itself.
- **Outcome:** Projects using PEP 735 groups, including this one, report their
  dependencies with scopes.
- **Blocked-by:** —
- **Enables:** —

### R-006 — Keep Python constraints as ranges, not floors

- **Category:** Python
- **What:** Stop `_PY_REQ` (`deptool/discover.py:210`) taking the first specifier and
  stripping its operator, and carry the full constraint through `Dep.version` /
  `Dep.raw_pin`.
- **Why:** `>=2.0,<3` becomes `2.0` — a floor read as a pin. Ranges are the norm in
  Python, so this mismeasures "how far behind" far more often than in CMake.
- **Outcome:** A ranged Python constraint is reported as a range, and "how far behind"
  is measured against the range's ceiling rather than its floor.
- **Blocked-by:** —
- **Enables:** —

### R-007 — Close the three structural `consumed` gaps

- **Category:** Graph backends
- **What:** Use codebase-memory's Hybrid LSP type resolution to catch the three cases
  the builtin extractor structurally cannot: a method called on a dependency's type
  (`node.as<int>()`, where the receiver's type is not tracked), a symbol reached only
  through one of our own aliases (`using Cfg = YAML::Node;`), and a symbol used only
  inside a macro we define.
- **Why:** These are why `nlohmann_json` reports `consumed: 2` on `endpoint/appv2`.
  Each needs type information no regex-based extractor can have. codebase-memory
  resolves types on C, C++ and Python — measured 2026-08-14 — so it is the backend to
  attempt them against.
- **Outcome:** `consumed` counts reflect method calls, aliased references and
  macro-mediated uses, and `nlohmann_json` on `endpoint/appv2` reports more than 2.
- **Blocked-by:** —
- **Enables:** R-020, R-023

### R-008 — Decide and implement the backend install check

- **Category:** Graph backends
- **What:** Add a one-time backend probe. Preferred shape: a lazy check inside
  `backends.detect()` that runs on the first `profile`/`check`, writes a marker
  (e.g. `~/.cache/deptool/backend-check`), and never asks again. The alternative is a
  `SessionStart` hook in `.claude-plugin/plugin.json`, which fires per session rather
  than once and needs the same marker anyway. Do **not** auto-install: print one line
  naming the install command and move on.
- **Why:** The plugin has no `install.py`, no hooks, and nothing that runs once, so a
  missing backend is silently absent forever and the user is never told the capability
  exists. Auto-installing is the wrong default in the other direction —
  codebase-memory downloads a per-platform native binary from GitHub Releases and
  indexes the whole repository, which is more than a dependency-advisory tool should
  do unprompted and is the kind of behaviour that gets a tool banned from a locked-down
  build machine.
- **Outcome:** A user without a graph backend is told once that installing one enables
  blast radius, and is never nagged again; no binary is ever fetched without an
  explicit action.
- **Blocked-by:** —
- **Enables:** —

### R-009 — Cut `_CTOR_AFFIXES`

- **Category:** Extraction
- **What:** Cut the nine-entry `_CTOR_AFFIXES` list to `new_`/`delete_`, or replace it
  with the general rule `\b\w+_PREFIX\w+\b`. Re-run the FluidSynth probe afterwards.
- **Why:** Decided but never coded. Only `new_`/`delete_` have a cited case;
  `free_`/`destroy_` are aimed at the wrong end (they are suffixes in real C APIs,
  which the prefix rule already catches). The padding is not free: `init_`, `open_`,
  `close_` and `make_` are common in *our own* code, so our wrappers get harvested as
  the library's symbols, dilute `not_located`, and can spend `CONFIRM_BUDGET` fetches
  hunting a header for a symbol that was never upstream's. This file's own asymmetry
  argument favours the general rule.
- **Outcome:** Our own wrapper functions stop being attributed to dependencies, and the
  FluidSynth probe's symbol count is re-measured against the change.
- **Blocked-by:** —
- **Enables:** —

### R-010 — Produce `scope-evidence` for Python

- **Category:** Python
- **What:** Emit an evidence line for Python scope inference, matching what the CMake
  path already produces.
- **Why:** Scope is inferred by regex-matching `test|dev|lint` against the group name,
  with no evidence line the user can disagree with. Standing rule 4 requires the
  inference be visible.
- **Outcome:** Every Python dependency's scope carries a citable reason the user can
  contest.
- **Blocked-by:** —
- **Enables:** —

### R-011 — Read `uv.lock` / `poetry.lock` / `pdm.lock` as declaration sites

- **Category:** Python
- **What:** Read the three Python lockfiles as declaration sites, the way `conan.lock`
  already is — marked as generated, excluded from `is_editable()`, reported as
  `regenerate`.
- **Why:** The lockfile holds a different truth from the manifest (finding C), and
  that divergence is already surfaced for Conan but invisible for Python.
- **Outcome:** A Python project reports manifest-versus-lock divergence the same way a
  Conan project does.
- **Blocked-by:** —
- **Enables:** —

### R-012 — Resolve `#if` from declared build options

- **Category:** API diff
- **What:** Feed the build options the manifests declare (TLS backend, compression,
  iconv, per platform) into the header diff, so conditional branches are resolved
  rather than all read unconditionally.
- **Why:** Finding E. The diff currently reads every `#if` branch and prints an
  unavoidable-limitation caveat on every run. Those options are declared facts, not
  guesses, so the caveat is avoidable.
- **Outcome:** The header diff reads only the branches the project actually compiles,
  and the blanket caveat is removed from its output.
- **Blocked-by:** —
- **Enables:** R-018

### R-013 — Add an audit verb

- **Category:** Product
- **What:** Add a verb that answers consistency questions rather than upgrade
  questions: do our platforms ship the same versions as each other, does the lockfile
  agree with the manifest, is anything we ship built from a locally modified recipe,
  what is in the transitive set we never declared.
- **Why:** For a mature project already close to current on everything, "is there
  something newer" is the wrong question. All four are consistency findings the
  deterministic layer already produces with no model call, so `check` should not be the
  only entry point. Every input exists; what is missing is the verb and its output
  shape.
- **Outcome:** A project can be audited for internal consistency without asking for
  upgrade advice, and the answer costs no model call.
- **Blocked-by:** —
- **Enables:** —

## Later

### R-014 — Exercise the additive merge with two live backends

- **Category:** Graph backends
- **What:** Run `backends.analyse` with both graphify and codebase-memory present and
  confirm the largest-wins rule in `_record` behaves.
- **Why:** The merge has never run with two live backends — until 2026-08-14 only one
  was installed on the dev machine — so the largest-wins rule is untested in anger. It
  cannot be meaningfully tested until R-001 makes either backend produce a correct
  number.
- **Outcome:** Two backends run together against one tree and the merged
  `blast_radius` is defensible against both sources of evidence.
- **Blocked-by:** R-002
- **Enables:** —

### R-015 — Evaluate the graph backends against zeta-daw

- **Category:** Verification
- **What:** Run both backends against `zeta-daw` and compare against the
  `endpoint/appv2` results recorded in HISTORY.
- **Why:** HISTORY names `zeta-daw` as the original development case, but no copy
  exists anywhere under `/home/daniel` as of 2026-08-14, so the 2026-08-14 backend
  measurement rests on `endpoint/appv2` plus this repo only. A second C++ project is
  what would show whether the seed findings generalise or are one project's shape.
  **Locate or re-clone the tree first.**
- **Outcome:** The backend criteria in HISTORY are confirmed or corrected against a
  second real C++ project.
- **Blocked-by:** —
- **Enables:** —

### R-016 — Ship the Python API-surface diff

- **Category:** Python
- **What:** Run `ast` over sdists/wheels fetched from PyPI to diff a dependency's
  public surface between two versions — the direct analogue of the header diff.
  Fetching is `_pypi_versions` plus a zip; extraction is stdlib; `ast.arguments` makes
  "a parameter became keyword-only" and "a default was removed" precisely detectable.
  Carry both honesty rules over unchanged, and note in the output that dynamic
  re-export (`globals().update`, module `__getattr__`) is invisible to `ast`.
- **Why:** It is the reason the judgement layer has anything factual to say about a
  Python upgrade rather than relying on upstream having written good release notes.
- **Outcome:** A Python dependency's breaking changes are established from its own
  code, with "absent" explicitly marked as a weaker claim than in C++.
- **Blocked-by:** —
- **Enables:** —

### R-017 — Record static-versus-shared and weight ABI accordingly

- **Category:** API diff
- **What:** Detect whether the project links its dependencies statically or against
  prebuilt shared objects, and pass that to the judgement layer.
- **Why:** For a project that links everything statically and rebuilds together, ABI
  breaks are nearly irrelevant and API breaks matter in full — the exact opposite of
  the prebuilt-binary case. ABI-versus-API relevance is a property of the project, not
  a constant.
- **Outcome:** The judgement layer knows which kind of project it is looking at, and
  stops weighting ABI breaks identically in both.
- **Blocked-by:** —
- **Enables:** R-019

### R-018 — Run the header diff per variant

- **Category:** API diff
- **What:** Run the diff once per declared variant and rank a divergence by whether the
  diverging versions differ in a symbol we actually consume.
- **Why:** Finishes finding B. All three known divergences (zlib, openssl,
  nlohmann_json) now resolve to readable repositories, so the input exists — but the
  ranking is only meaningful once conditionals are resolved per variant.
- **Outcome:** A version divergence between platforms is ranked by whether it changes
  a symbol we use, rather than merely reported as a divergence.
- **Blocked-by:** R-012
- **Enables:** —

### R-019 — Track enum values and struct layout

- **Category:** API diff
- **What:** Track enumerator *values* and struct field layout in the header diff, not
  just enumerator names.
- **Why:** A reordered enum, a changed explicit value, or a struct that gained a field
  all currently read as unchanged — silent ABI breaks. Only worth the cost once R-017
  establishes that ABI matters for the project in hand.
- **Outcome:** Layout and value changes are reported as ABI breaks for projects where
  ABI is relevant.
- **Blocked-by:** R-017
- **Enables:** —

### R-020 — Per-call-site diagnosis

- **Category:** API diff
- **What:** Report which of *our* calls would fail to compile after a signature change,
  rather than which overloads moved. Needs argument types per site.
- **Why:** "Three overloads changed" is not actionable; "this call at `foo.cpp:112`
  no longer compiles" is. Requires type information only a graph backend can supply.
- **Outcome:** A breaking signature change names the call sites that break.
- **Blocked-by:** R-007
- **Enables:** —

### R-021 — Support several products in one repository

- **Category:** Discovery
- **What:** Let the CMake reader cover products the root entry point never adds, so a
  repository holding several products with shared manifests above them can be read
  correctly from one invocation.
- **Why:** The manifest walk finds per-product manifests, but the CMake reader starts
  at the root entry and follows only what it reaches. Pointed at the root the tool
  mixes products; pointed at one product it cannot see the manifests — **neither root
  is correct**. This is cut fleet mode reappearing inside one repository, which makes
  it more urgent than the cross-repo version. `endpoint` is exactly this shape: `app`,
  `appv2`, `driver`, `common`, `demo` and `tests` under one root.
- **Outcome:** A multi-product repository reports per-product dependency sets without
  mixing them, from a single invocation.
- **Blocked-by:** —
- **Enables:** —

### R-022 — Detect a committed vendored tree with no submodule entry

- **Category:** Discovery
- **What:** Find a design for detecting a vendored third-party tree that was committed
  directly rather than added as a submodule. **No design yet — propose one before
  coding.**
- **Why:** A submodule path is excluded from both the manifest walk and the CMake
  reader, and that mechanism generalises because it reads a declared fact. A committed
  copy has no such signal, and guessing at directory names is what `SKIP_DIRS` already
  does badly (standing rule 3). `endpoint/appv2/third-party/armadillo` is a live
  instance.
- **Outcome:** A committed vendored tree is identified from evidence rather than a
  name table, and excluded from the project's own dependency set.
- **Blocked-by:** —
- **Enables:** —

### R-023 — Attribute transitive dependencies to what pulls them in

- **Category:** Discovery
- **What:** Attribute each transitive dependency to the direct dependency that pulls it
  in, and report whether we call the part of that dependency which uses it.
- **Why:** A Conan 2 lock is a flat list with no edges, so this needs the Conan 1 node
  graph, Trivy's `DependsOn` (absent in 0.52.2), or a real resolve. Without edges a
  transitive list is just a longer list.
- **Outcome:** A transitive dependency is reported with the direct dependency
  responsible for it, so the reader knows who to talk to about removing it.
- **Blocked-by:** R-007
- **Enables:** —

### R-024 — Challenge the remaining invented constants

- **Category:** Extraction
- **What:** Justify or cut `MAX_CONSUMED = 150`, `MIN_DECLARED_LEN = 4` (chosen so
  `Node` scrapes through), `_CONTEXT_RANK` (call > type > constant), and `KNOWN` (18
  entries).
- **Why:** Listed so they are not silently inherited. `_CONTEXT_RANK` is probably
  wrong — a removed enum constant is exactly as fatal at compile time as a removed
  function, so ranking constants last is a guess dressed as a judgement. `KNOWN` is
  mostly the development case's own dependency set, so every library outside it gets a
  prefix derived from its *package* name, which is frequently not its API's.
- **Outcome:** Each surviving constant has a cited case behind it, or is gone.
- **Blocked-by:** —
- **Enables:** —

### R-025 — Bisect to the last good version

- **Category:** Product
- **What:** When the latest version fails to build, walk back to the newest that does.
  `make_sandbox` is independent per call, so candidates can be built in parallel.
- **Why:** Unblocked by sandbox verification, but nothing drives it yet, so a failing
  latest currently ends the recommendation rather than producing a usable target.
- **Outcome:** A dependency whose latest release fails to build still yields a
  recommended version.
- **Blocked-by:** —
- **Enables:** —

### R-026 — Resolve companion pins that upstream declares nowhere

- **Category:** Upstream
- **What:** Resolve a companion version when upstream declares it nowhere, in expected-
  value order: the GitHub **release asset list** at the target tag
  (`libhegel-0.31.0-linux-x86_64.tar.gz` often *is* the answer); **lockfiles and CI
  config in the dependency's own repo** (the version its CI installs is the version it
  was tested against); **try-and-see**, now that sandbox verification exists.
- **Why:** The motivating case is a prebuilt binary release. Also: only GitHub
  upstreams can be read, and a pin attached by proximity alone is flagged but never
  confirmed.
- **Outcome:** A companion pin is resolved from evidence for the common prebuilt-binary
  case, instead of blocking `apply`.
- **Blocked-by:** —
- **Enables:** —

### R-027 — Filter or map the bulk distro advisories

- **Category:** Upstream
- **What:** Filter or map the distro advisories OSV returns for a bare-name Conan
  query.
- **Why:** The sentinel guard labels them honestly, but the list is long enough to bury
  the findings that matter.
- **Outcome:** A Conan dependency's advisory list is short enough to read.
- **Blocked-by:** —
- **Enables:** —

### R-028 — Add a NuGet version resolver

- **Category:** Upstream
- **What:** Add a NuGet resolver so ingested packages can answer "is there something
  newer".
- **Why:** `check` currently prints `no upstream resolver for unknown:Newtonsoft.Json`.
- **Outcome:** Ingested NuGet packages report their latest available version.
- **Blocked-by:** —
- **Enables:** —

### R-029 — Add an `assess --dep N` verb

- **Category:** Product
- **What:** Add a verb that assesses one dependency, so the whole profile need not be
  read to write one paragraph.
- **Why:** Deferred deliberately — it is 10-15% of a `/deps:check` turn, and
  `carry_over` means there is no correctness bug. **Revisit at ~50 deps** or on a real
  context exhaustion. See the format measurement in HISTORY.
- **Outcome:** Assessing one dependency costs one dependency's worth of context.
- **Blocked-by:** —
- **Enables:** —

### R-030 — Better system-dependency handling

- **Category:** Product
- **What:** Read CI workflows and Dockerfiles for the versions CI actually installs,
  flag developer-versus-CI divergence, and distinguish "the distro ships 2.5.7" from
  "your LTS never will".
- **Why:** The CI-installed versions are the ones that matter for reproducibility, and
  the LTS distinction turns an upgrade into a packaging decision rather than a version
  bump.
- **Outcome:** System dependencies are reported against the versions CI installs, with
  packaging consequences called out.
- **Blocked-by:** —
- **Enables:** —

### R-031 — Verify non-C++ extraction against a real project

- **Category:** Verification
- **What:** Run the npm/Cargo/PyPI/Go parsers against a substantial real project. Fix
  `_harvest_generic`, which records only *imported* names, so attribute access on an
  imported module (`np.frombuffer`) is missed and there is no declared-name match
  equivalent.
- **Why:** These parsers have unit tests but have never run against real code, so their
  `consumed` counts are unvalidated in exactly the way the C++ path's were before
  2026-08-14.
- **Outcome:** Non-C++ extraction has a measured accuracy figure against a real
  project, and attribute access is counted.
- **Blocked-by:** —
- **Enables:** —

### R-032 — Verify the header diff's prose fallbacks

- **Category:** Verification
- **What:** Find a real C/C++ dependency shipping a migration guide and confirm the
  prose fallback contributes to a recommendation.
- **Why:** Migration-guide discovery is verified against `symfony/symfony` and the
  commit log against `FluidSynth/fluidsynth`, but no C/C++ dependency encountered so
  far ships a migration guide, so that path has never contributed to a real
  recommendation.
- **Outcome:** The migration-guide path has produced at least one real recommendation,
  or is cut as unreachable for C/C++.
- **Blocked-by:** —
- **Enables:** —

### R-033 — Exercise vcpkg end to end

- **Category:** Verification
- **What:** Run the vcpkg path against a real vcpkg project, discovery through `apply`.
- **Why:** Parsed but never exercised end to end. Conan now has been, and doing so is
  what surfaced findings C through F.
- **Outcome:** A vcpkg project can be profiled, checked and bumped, with any gaps found
  recorded.
- **Blocked-by:** —
- **Enables:** —

### R-034 — Verify the companion release-notes fallback

- **Category:** Verification
- **What:** Find a real release note that supplies a companion version and confirm the
  fallback fires.
- **Why:** Unit-tested only; no real release note has ever been the source of a
  companion version. The `set()`-pin path is verified against `grpc/grpc` and
  `FluidSynth/fluidsynth`, so only the prose fallback is unproven.
- **Outcome:** The release-notes fallback has resolved at least one real companion
  version, or is cut.
- **Blocked-by:** —
- **Enables:** —

---

## Deliberately not now

Not roadmap items — decisions, recorded so they are not rediscovered as ideas. The
reasoning is in [HISTORY.md](HISTORY.md).

- **Fleet mode.** Cut from 0.1.0; the design is per-repo. If it returns, the shape is a
  `repos.yaml`, one committed `CLAUDE_DEPS.md` per repo, and a cross-repo report ranked
  by aggregate exposure. R-021 is the same problem *inside* one repository, and that is
  the version worth solving first.
- **Opening PRs.** A small step that changes the tool from advisory to actor. Worth
  doing only once recommendations have proven trustworthy over some real upgrades.
- **Calibration.** There is no feedback loop: a rejected "WORTH IT" or a consequential
  "LOW VALUE" teaches nothing. The `### Assessment` blocks are the natural place to
  record outcomes.
- **Writing Gradle, Maven, NuGet, SwiftPM, Bazel or Nix parsers**, or per-ecosystem
  lockfile parsers for transitive dependencies. Both are ingest, not implementation.
  **`dependabot-core`'s `file_updater` is never adopted** — ours is line-precise and
  theirs is not; its `update_checker` may be, optionally, and only for update-checking.
