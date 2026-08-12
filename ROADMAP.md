# Roadmap

Status of `deps` v0.1.0, and where it goes next.

Priorities below are ordered by how much they change the quality of the
*answer*, not by how much code they need. Items marked **(observed)** came out
of running the tool against a real project (`dgutson/zeta-daw`) rather than
from speculation.

**Targets.** C/C++ (CMake) and **Python** are both first-class; zeta-daw was the
development case, not the scope. npm, Cargo and Go are supported but not a
priority. See [2c](#2c-python-dependency-discovery-is-too-thin) for how far short
the Python side currently falls, and [2b](#2b-the-same-diff-for-python--the-next-item)
for the Python half of the thesis.

**Before adding any parser, read [Prior art](#prior-art--what-not-to-build).**
Dependency discovery is a solved problem in every ecosystem except CMake, and
this tool should ingest it rather than reimplement it. The unique value is the
usage profile and the judgement, not the parsing.

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
- **Tag-to-tag public-header diff** **(observed)** — for a C/C++ dependency on a
  readable GitHub upstream, the breaking-change list no longer comes from prose.
  `deptool apidiff` (and `check`, automatically) reads the public headers at both
  tags, extracts declarations, and intersects removals and signature changes with
  `consumed`, reporting our own `file:line` for each hit. Plus the two prose
  fallbacks: migration guides discovered from the target's file listing and
  narrowed to the versions being crossed, and the commit log between tags when
  the release notes are empty.

---

## Shipped in 0.2.0 — how the header diff behaves

Verified against real repositories, and worth recording because most of the
design is scar tissue from false positives found there:

- **FluidSynth 2.3.4 → 2.5.7** — finds exactly two public removals
  (`fluid_ramsfont_t`, `fluid_rampreset_t`, the RAM SoundFont API dropped in
  2.4.0) out of 437 declarations, and correctly reports nothing affecting a
  project consuming the common `fluid_synth_*` surface.
- **yaml-cpp 0.6.3 → 0.8.0** — pass-by-value → const-ref on six emitter
  manipulators, `std::string_view` / `std::unordered_map` / `std::valarray`
  overloads added to `convert`, allocator template parameters added to the
  container specialisations. All real, none of it in the release notes.
- **grpc 1.60.0 → 1.68.0** — 155 public headers, budget bites at 24, and the
  truncation is reported rather than hidden: 88 removals found, `grpc::Status`
  listed as `not_located` instead of silently counted as unaffected.

Three things the diff must never do, each of which it did once:

- Report a declaration that **moved between headers** as removed.
- Let an **incomplete read** read as a clean bill of health. Observed with
  FluidSynth: `ramsfont.h` sorted outside a 12-header budget, so a genuine
  removal of a type the project used came back as "nothing we consume was
  removed". Fixed by ranking headers whose name matches a consumed symbol's
  subsystem, by a targeted second search of skipped headers, and by reporting
  `not_located` explicitly.
- Report **cosmetic edits** as breaks: a renamed parameter, an added `override`,
  a `FLUID_RESTRICT` annotation, a renamed include guard, or churn in `src/`
  when the project publishes an `include/` tree.

Two adjacent fixes fell out of this, both pre-existing:

- **Tags carrying a project name** (`yaml-cpp-0.6.3`, `release-1.11.0`) parsed
  to `None`, which silently deleted every such release from `newer_than`. Those
  projects reported nothing to upgrade to, and neither companion resolution nor
  the header diff could find a ref to read. `upstream.version_from_tag` now
  extracts the version and keeps the tag verbatim.
- `_first_readable_ref` listed each ref's tree twice, doubling the API calls per
  dependency.

---

---

## Shipped in 0.3.0 — the consumed side of the intersection

Item 2's last open bullet and item 6's stated prerequisite were the same thing:
the header diff was factual about what upstream removed, but could only flag what
the extractor had recorded as consumed, and that extractor required a `(` after a
symbol to harvest it. The measured effect, on a translation unit using eleven
distinct FluidSynth names: **two were recorded.** The nine missed were types
(`fluid_synth_t`, `fluid_ramsfont_t`), enum constants (`FLUID_FAILED`) and the
`new_`/`delete_` constructor pair, which carries the library's prefix in the
middle of the name rather than at the start.

Worse than the recall was the silence. A dependency the guess missed entirely
reported *zero* consumed symbols, which made the header diff skip it, which made
its report indistinguishable from a dependency with nothing to worry about.

What changed:

- **The C/C++ harvest is no longer keyed on a trailing `(`.** A prefix ending in
  `_` is treated as a namespace marker, so any identifier carrying it counts;
  conventional constructor affixes (`new_`, `delete_`, `free_`, `create_`, …) are
  recognised in front of it. A prefix that is a bare stem instead (zlib's
  `deflate`, googletest's `TEST`) still has to look like a call — such a token is
  about as likely to be an English word. Each symbol records what its use site
  looked like (`call` / `type` / `constant`), which is what the truncation rule
  drops last.
- **Comments, string literals and `#include` paths are blanked before
  harvesting** — necessary once the `(` requirement went, or a symbol named in a
  log message would read as a use of it.
- **Upstream's own declared names replace the guess.** The prefix is derived from
  the *package* name, which is frequently not the API's: libsndfile yields
  `sndfile_` and its API is `sf_open`/`SF_INFO`. The header diff already reads
  upstream's declarations, so it now feeds them back — a name upstream declares
  that our sources mention is consumed, at no extra fetches. Names *we* declare
  are excluded, so our `Node` does not become yaml-cpp's.
- **Enum constants are part of the extracted surface.** Reading only an enum's
  tag meant every constant a project consumed came back `not_located` — unchecked
  however many headers were read. Values and ordering are still out of scope, so
  a renumbered enum still reads as unchanged (that half of item 6 stands).
- **A generated public header is a public header.** libsndfile 1.0.28 keeps its
  entire C API in `src/sndfile.h.in`; only literal suffixes were recognised, so
  the diff read 20 *internal* headers, never saw `sf_open`, and drew its removals
  from internal churn. `.in` / `.cmake` / `.meson` templates now count, and the
  include hint resolves through them so `#include <sndfile.h>` reaches the
  template first instead of alphabetically.
- **One false positive the wider surface exposed, fixed.** `SNDFILE` is a typedef
  of an opaque struct whose *tag* libsndfile renamed in 1.2.2; the alias comparison
  read that as a signature change on a symbol every caller uses and none can see
  inside. Alias targets now erase an incomplete type's tag while keeping pointer
  and `const` decoration, so `struct A` → `struct A *` is still the change it is.
  Latent before this item — nothing had ever put `SNDFILE` in a `consumed` list.
- **Three guardrails against a vacuous all-clear**, matching the existing rule
  about incomplete reads. An attributed dependency that harvests nothing says so
  in its profile; `check` states that it did not diff a dependency no file of ours
  includes rather than skipping it in silence; and a diff whose `consumed_count`
  is zero reports *unmeasured, not unaffected* instead of "nothing we consume
  changed".

Verified against real repositories, and the previously recorded results are
unchanged, which was the point of checking:

- **FluidSynth 2.3.4 → 2.5.7** — still exactly two removals (`fluid_ramsfont_t`,
  `fluid_rampreset_t`), now out of 626 declarations rather than 437, the extra 189
  being enumerators. No new false positive came with them.
- **FluidSynth 2.3.4 → 2.6.0**, against a project consuming the surface above —
  `fluid_ramsfont_t` reported at our own `file:line`, and `not_located` now empty
  where the enum constant used to sit in it. This is the finding this tool's
  README leads with, and until now the extractor could not actually reach it.
- **libsndfile 1.0.28 → 1.2.2** — from zero consumed symbols and no diff at all,
  to `SF_INFO`, `SNDFILE`, `sf_open`, `sf_close` matched out of the generated
  header, and a correct report that none of them changed.
- **grpc 1.60.0 → 1.68.0** — unchanged: 155 headers, budget bites at 24, 88
  removals, `grpc::Status` still reported as `not_located`.

Cost, since this runs per dependency: the declared-name match adjudicates only
the names that actually matched, and parses only the files mentioning one of them.
Extracting every declaration in the repository first is the obvious
implementation and takes ~14s per dependency on a 6k-file tree; the current one
takes ~1.7s worst case.

### What in 0.3.0 is fitted to one project, and should be cut back

Recorded because it is the failure mode this whole tool is exposed to: every
heuristic here was tuned against one codebase, and a constant that looks
principled is usually a constant that was guessed once and never challenged.

**The constructor-affix list is the worst of it.** `_CTOR_AFFIXES` has nine
entries. Two — `new_`, `delete_` — come from the development case. The other
seven (`free_`, `create_`, `destroy_`, `make_`, `init_`, `open_`, `close_`) have
no cited example of any library putting them *in front of* its own prefix, and
`free_`/`destroy_` are aimed at the wrong end: in real C APIs they are suffixes
(`sqlite3_free`, `curl_free`), which the prefix rule already catches. So a
category was generalised from one instance and then padded with plausible-sounding
words.

The padding is not free. `init_`, `open_`, `close_` and `make_` are common in
*our own* code, so `init_fluid_state()` — a wrapper of ours — is harvested as the
library's symbol, lands in `not_located`, dilutes the genuine entries there, and
can spend up to `CONFIRM_BUDGET` network fetches hunting a header for a symbol
that was never upstream's.

Worse, the rule is mostly redundant. The declared-name match finds
`new_fluid_synth` with no heuristic at all, because upstream declares it. The
affix rule only does non-redundant work where that match cannot run — an offline
`profile`, or a dependency whose upstream cannot be read at all (`distro:`,
`pypi:`, non-GitHub), which is a real case but not the one it was built for. And
its value was measured on the same example that motivated it.

Three designs, and the shipped one is arguably the weakest:

1. **No affix rule** — lean on the declared-name match, accept the gap for
   offline runs and unreadable upstreams.
2. **One general rule** — allow any leading token before the prefix
   (`\b\w+_fluid_\w+\b`). No list to get wrong, catches conventions nobody
   thought of, and its false positives are our own wrappers: noisy in
   `not_located`, but never a false *finding*, since a name upstream does not
   declare cannot match a removal.
3. **A bounded list** — what shipped, with a boundary drawn by imagination.

By this file's own asymmetry argument — a missed symbol is a missed break, an
extra symbol is noise — (2) beats (3). **Decision to take: cut the list to
`new_`/`delete_`, or replace it with (2).**

Other constants with no evidence behind them, listed so they can be challenged
rather than inherited:

- `MAX_CONSUMED = 150` — invented.
- `MIN_DECLARED_LEN = 4` — invented; chosen so `Node` scrapes through.
- `_CONTEXT_RANK` (call > type > constant) — plausible and probably wrong. A
  removed enum constant is exactly as fatal at compile time as a removed
  function, so ranking constants last is a guess dressed as a judgement.
- `KNOWN` — 18 entries, most of them the development case's own dependency set.
  Every library outside it gets a prefix derived from its *package* name, which
  is frequently not its API's.

What is *not* fitted to one project, for calibration: the generated-header rule
and the opaque-tag rule both came from a library deliberately chosen as a
counter-example; enum members are a language-level fact about C, not one
library's habit; and the declared-name match uses no library-specific knowledge
at all. That last point is the strategic one — **the mechanisms that generalise
are the ones that read upstream instead of guessing about it**, and effort is
better spent widening those than adding entries to a table.

---

## Measured against a second real project **(observed)**

Everything above was developed against one codebase. Running the tool against an
unrelated one — a large multi-platform C++ product, ~1,200 sources, Conan for
dependencies, CMake for the build, several products in one repository — produced
the most useful result so far, which is that **the extraction works and the
discovery does not**.

What held up: the consumed surface came out right, in 0.5s over 1,200 files, with
plausible symbol sets per library (~26 for the HTTP client, ~22 for the archive
library, ~13 for the crypto library). 0.3.0 earned its place there — the crypto
library's harvested surface is mostly types and a size constant, none of them
followed by a `(`, so the old extractor would have recorded a fraction of it. And
the "no direct usage found" note fired correctly on a library whose real header
name does not follow from its package name.

What did not hold up: **the tool found none of the project's actual pinned
versions.** Every dependency came back unpinned, under its CMake name, resolving
to a distro lookup. Six findings, in the order they need fixing.

### A, D and the core of B — *shipped, see [0.4.0](#shipped-in-040--discovery-is-variant-aware)*

The three were shipped as one change, because A alone makes things worse: it
surfaces the pins *and* leaves every such library appearing twice, once pinned
from the manifest and once unpinned from `find_package`, with the unpinned one
still winning. The findings below are kept as written, since they are the record
of what the second project actually showed.

### A. Manifests are only discovered at the repository root

`detect_manifests` tests `isfile(join(root, name))` for each manifest, so a
manifest one directory down does not exist as far as the tool is concerned. This
project keeps its Conan manifests in per-platform subdirectories — the parser for
them already works, and was never handed a file. The result is not a degraded
answer, it is *no* answer: the pins are invisible and what gets reported instead
is the `find_package` calls, which carry no version.

This is a discovery-walk bug, not a parser gap, and it is the cheapest
high-value fix on this list. It also picks up nested manifests in other
ecosystems for free.

### B. One pin per dependency is the wrong data model

The same libraries are pinned in several manifests, one per target platform, and
**the versions disagree** — including on the TLS library. `Dep` has `version`,
`raw_pin` and `declared_in` as single strings, so there is no way to say "this
version on three platforms, that version on the fourth". The tool would pick one
arbitrarily or emit duplicates.

And the divergence *is the finding*. No upstream lookup produces it, no SCA tool
reports it, and it is worth more to this project than "you are one minor version
behind", because the project is not meaningfully behind on anything. **A
dependency needs a set of (variant → declaration) pairs, and "the variants
disagree" needs to be a first-class finding** with its own severity, ranked by
whether the diverging versions differ in a symbol the project consumes.

This is a data-model change, so it should land before more parsers are written
against the current shape.

### C. The lockfile holds a different truth from the manifest

The project has a Conan lockfile, and it pins one library **two minor versions
behind what every manifest requires**. Either the lock is stale or the build is
not using it; both are findings, and neither is visible today. The lockfile is
also the only place naming the transitive dependencies — a compression library,
an XZ implementation, an iconv implementation — which are real attack surface
that no conanfile mentions, and it separates build-only requirements (build
systems, autotools, pkg-config) that must not be reported as runtime risk.

This reprioritises item 8. A Conan lockfile is flat JSON with `requires` /
`build_requires` / `python_requires`; parsing it is perhaps twenty lines and it
yields resolved transitive versions *with scope*, plus manifest-versus-lock
divergence. **Worth doing natively, before any SCA ingest** — the tool has to
read it anyway to know what is actually built. Note the format permits the same
package twice at two versions, so the parser cannot assume uniqueness.

### D. The CMake name is not the package name

`find_package(CURL)` and `libcurl`; `find_package(ZLIB)` and `zlib`;
`find_package(OpenSSL)` and `openssl`. Once (A) is fixed, every such library
yields **two** dependencies: one pinned from the package manager, one unpinned
from CMake. Today the unpinned one is what survives, and it resolves to a distro
lookup — so the tool compares the machine's system library against what distros
ship, and frames the fix as a CI-image or documented-minimum change, when the
real fix is a one-line edit in a manifest and the library is statically linked
from the package manager anyway. That is not a missing feature, it is a **wrong
answer stated confidently**, which is worse.

Needed: an alias map from `find_package` name to package name, and additive
reconciliation so the two declarations merge into one dependency with two
declaration sites and the source of each fact still visible. The roadmap already
states that rule for ingested sources (item 7); it turns out to be needed
*between the native parsers* first.

### E. Build options decide what the API even is

The manifests set per-platform options: a different TLS backend on each platform,
compression and iconv support switched off on some, static linking everywhere.
So the *same version* of a library has a different surface per platform, and the
header diff — which reads declarations behind `#if` unconditionally, its
best-known blind spot — is being handed the information that would resolve those
conditionals and is throwing it away. **Build options are declared facts, and the
diff should use them** rather than list `#if` as an unavoidable limitation.

Static linking everywhere also reweights item 6: for this project ABI changes are
nearly irrelevant (everything is rebuilt together) while API changes matter in
full — the exact opposite of the prebuilt-binary case that motivated the ABI work.
So **ABI-versus-API relevance is a property of the project, not a constant**, and
the judgement layer should be told which one it is looking at.

### F. Vendored trees and multiple products in one repository

A vendored copy of a test framework sits in-tree. Run from the repository root,
the tool reports a Python interpreter as a runtime dependency of the product
(picked out of that framework's internal CMake), and picks a *test* CMakeLists as
the declaration site for libraries the shipped application also declares — while
still calling the scope `runtime`. `SKIP_DIRS` knows `third_party` and `vendor`
but has no notion of a vendored copy under another name, and the declaration-site
dedup has no notion that a shipped-application declaration outranks a test one.

**Partly addressed in 0.4.0, and worth being precise about which parts.** Both
observed symptoms are gone: CMake's own find modules are filtered, so no
interpreter is reported; and the declaration site is now chosen by which
declaration carries an editable pin, so a manifest outranks a test
`find_package` without needing a rule about test directories at all — the test
declaration is kept in `declarations`, correctly labelled, rather than promoted or
discarded.

The underlying problem is *not* fixed. A vendored tree declared in `.gitmodules`
is excluded from both the manifest walk and the CMake reader, but the tree that
motivated the finding is a **committed copy with no submodule entry**, so nothing
excludes it and its internal `CMakeLists.txt` files are still read. The
submodule path is the mechanism that generalises; identifying a committed
vendored copy still needs a signal nobody has proposed yet, and guessing at
directory names is what `SKIP_DIRS` already does badly.

Also untouched: several products in one repository with shared manifests above
them. The manifest half is now fine from the repository root — the walk finds
per-product manifests wherever they sit — but the CMake half still starts at the
root entry and follows only what it reaches, so a product the root never adds is
invisible. "One repository, one profile" is still the assumption that fails.

Underneath that is a scoping problem: this repository holds several products, a
kernel driver among them, each with its own dependency set, and the shared
manifests live above the product directories. Pointed at the repository root the
tool mixes products together; pointed at one product it cannot see the manifests.
**Neither root is correct, which means "one repository, one profile" is the
assumption that fails.** This is the cut fleet mode (item 9) reappearing *inside*
a single repository, which makes it more urgent than a cross-repo feature.

### What this changes about the strategy

The thesis — profile what we consume, intersect it with what changed upstream —
survives intact, and the extraction half of it is in better shape than expected.
What does not survive is the assumption that **the interesting question is always
"is there something newer"**. For a mature project on a lockfile-based ecosystem,
already close to current on everything, the valuable questions are:

- do our platforms ship the same versions as each other?
- does the lockfile agree with the manifest?
- is anything we ship built from a locally modified recipe? (This project patches
  one dependency's build recipe to add an option. Every upstream-derived claim
  about that dependency — advisory match, header diff — is therefore approximate,
  and the tool has no way to say so. **"Locally patched" should be a recorded
  property that lowers the confidence of every finding about that dependency.**)
- what is in the transitive set we never declared?

None of those are upgrade recommendations, and all of them are consistency
findings the deterministic layer can produce without a model call. **So `check`
should not be the only entry point.** A project like this wants an audit verb
first — one that reports divergence, staleness, local patches and undeclared
transitive surface — and reaches for upgrade advice second. That is a genuinely
different product shape from the one this tool was designed around, and it is the
shape the second real project asked for.

Ordering, by expected value: **A** (nothing works without it), then **D** (it
prevents a confidently wrong answer), then **C** (cheap, and unlocks transitive
plus scope), then **B** (the data-model change, before more parsers accumulate),
then **F**, then **E**. A, D and B's data model shipped together in 0.4.0; **C is
next**, then F, then E.

---

## Shipped in 0.4.0 — discovery is variant-aware

Findings A and D, plus the data model B asked for. One change rather than three,
because A on its own is a regression dressed as a fix: it makes the pins visible
while leaving each library declared twice, and the unpinned `find_package` record
still wins.

- **Manifests are found by walking, not by testing the root.** `detect_manifests`
  returns `(path relative to root, ecosystem)` and every parser takes a path, so
  a manifest at any depth is parsed the same way. `declared_in` carries the
  relative path, which is what `apply` and the fingerprints already resolve
  against, so editing a nested manifest needed no further change.
- **`Declaration` is a first-class record** — path, line, kind, version, raw pin —
  and `Dep.declarations` holds every one of them. `declared_in` remains the single
  site `apply` edits, so nothing downstream had to change; `Dep.pin_variants()`
  and `Dep.diverges()` read the list.
- **"The variants disagree" is a finding.** It is reported by `profile` and
  `check` (a `divergence` field, and a `!=` line in the human output) without a
  network call or a model call, which for a project already current on everything
  is the only finding of real size.
- **The reported version is the *oldest* of the declared ones.** Deliberate: it is
  the copy an advisory is most likely to match and the one that breaks first, so
  leading with the newest would understate the exposure on the platform that
  matters. `declarations` says where the others are.
- **CMake and package-manager names reconcile by one generic rule**, not a table:
  casefold, drop separators, drop a leading `lib` when something substantial is
  left. That resolves `CURL`↔`libcurl`, `ZLIB`↔`zlib`, `OpenSSL`↔`openssl`,
  `LibArchive`↔`libarchive` and `nlohmann_json` with **zero** entries in the alias
  table, which is left empty on purpose — by the argument in
  [what is fitted to one project](#what-in-030-is-fitted-to-one-project-and-should-be-cut-back),
  a table of plausible-looking name pairs is a constant that gets guessed once
  and inherited forever. Reconciliation is additive and per ecosystem family, so
  npm's `zlib` and Conan's `zlib` stay separate.
- **The surviving record keeps every name.** `Dep.aliases` carries the others, so
  `--dep CURL` still resolves after the fold, and symbol attribution derives its
  include/prefix candidates from *all* the names — otherwise renaming the record
  to the package-manager spelling would have silently changed what the extractor
  looks for.
- **`apply` refuses a divergent dependency** rather than editing one site and
  deepening the disagreement, and when the sites agree it **bumps all of them in
  one plan** (`also_pinned_in`), the same atomicity rule the coupled-pin work
  established. A sibling manifest that is missing, or that does not contain the
  version it claims, lands in `blocked_on` instead of being skipped quietly.
- **A manifest that yields nothing is still listed.** This repo's own
  `pyproject.toml` uses PEP 735 and reports zero dependencies (item 2c), and that
  must not look like a project with no dependencies.
- **A declared submodule is not our dependency.** Walking newly exposes vendored
  checkouts under their own names, which `SKIP_DIRS` does not know; `.gitmodules`
  is read and those paths are skipped. This reads a declared fact rather than
  guessing at directory names — the vendored-copy-*without*-a-submodule case is
  still open, see F.

Measured against the second real project, from the repository root:

| | before | after |
|---|---|---|
| dependencies found | 6, **all unpinned** | 7, **7/7 pinned** |
| upstream for the C/C++ set | `distro:<CMakeName>` | the package manager |
| version disagreements found | 0 (invisible) | 3, matching the manifests exactly |
| spurious interpreter dependency | reported | gone |
| consumed symbols per dependency | 26 / 22 / 13 / 8 / 2 / 0 | **unchanged** |

The last row is the one that needed checking: the records are now named after
their packages rather than their CMake spelling, and attribution had to survive
that. Walk cost is 0.01s for 34 manifests on that tree.

### Conan Center as a version source — the consequence of D

Reconciliation turned this from optional into required, and it is worth recording
as a lesson about the shape of these changes. Once a Conan-pinned library stops
being reported under its CMake name, `distro:` is no longer its upstream — so
`check` had **no resolver at all** for the primary upstream of every dependency in
that project, and went silent about all of them. Fixing the wrong answer had
replaced it with no answer.

`recipes/<name>/config.yml` in `conan-io/conan-center-index` is the authoritative
list of what a project can pin, and it is a file in a GitHub repo, which this tool
already reads. One fetch per dependency, usually off `raw.githubusercontent` at no
API cost.

Two honesty rules came out of it, both verified against the live index:

- **The catalogue is pruned, not a history.** `zlib` lists exactly one version.
  So `behind_by` is a *floor*, reported as `behind_by_is_floor` and printed as
  `1+ behind`; presenting it as a release count would be a confidently wrong
  number.
- **A pin absent from the catalogue is a finding of its own** —
  `pin_unavailable`. Verified against the live index on a real dependency set:
  three of five pins are no longer offered, so a fresh `conan install` cannot
  reproduce that build, and the two still listed correctly do not fire. Nothing
  else the tool does would have surfaced this. Restricted to catalogues that are
  authoritative about installability — Repology is a survey of what distros ship,
  so a version missing from it says nothing.

Still unresolved for Conan dependencies: **the header diff cannot run on them**,
because `dep.upstream.ref` is a recipe name rather than a repository. The recipe's
own `conandata.yml` records the source URL per version, which would map a recipe
to its GitHub upstream and make the whole API-surface diff apply to
Conan-pinned C/C++ — the tool's central mechanism, currently unavailable to
exactly the project class that motivated findings A–F. That is the highest-value
item this work uncovered and it is not on the A–F list.

### What A, D and B did not settle

A bump across variants is atomic only when they already agree, and *resolving* a
disagreement is still the user's edit. Ranking divergence by whether the diverging
versions differ in a symbol we consume — B's stated goal — needs the API-surface
diff run per variant, which meets **E** (build options decide what the API is) and
should be done there rather than twice. And the OSV lookup for a Conan dependency
still falls back to a bare-name query, which returns distro-flavoured advisories
in bulk; the sentinel guard keeps them honestly labelled but the list is long.

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

The header diff added one thing here: when a companion pin blocks an upgrade,
`deptool apidiff --dep hegel --to 0.11.1` still answers "what would break if we
got there", so the cost of the upgrade is knowable before the pairing is solved.

Two smaller gaps in what shipped: only GitHub upstreams can be read (a
`distro:`/`pypi:` dependency reports `unresolved` immediately), and a pin
attached by proximity alone is flagged but never confirmed.

### 2. Trust the changelog less — *shipped, see above*

What is left of it, now that the header diff and the consumed-side fix exist:

- **The remaining `consumed` gaps are structural rather than a matter of
  patterns.** Three specific ones, each needing type information the builtin
  backend does not have: a method called on an object of a dependency's type
  (`node.as<int>()` attributes to nothing, since the receiver's type is not
  tracked); a symbol reached only through one of our own aliases (`using Cfg =
  YAML::Node;` then `Cfg` everywhere); and a symbol consumed only inside a macro
  *we* define. All three are jobs for a graph backend, which is where item 6's
  per-call-site diagnosis also lands — so they belong together, not here.
- **Non-header ecosystems have no factual equivalent yet.** See item 2b — for
  Python this is the next thing to build, and it is easier than the C++ case.
- **Only GitHub upstreams can be diffed.** A `distro:` or `pypi:` dependency
  reports `resolved: false` with a reason.

### 2b. The same diff for Python — *the next item*

The header diff is not a C++ feature, it is an **API-surface diff**: fetch the
published artefact at both versions, extract the public surface, intersect the
difference with what we consume. Python is the same shape and less work:

- **Fetching is trivial.** PyPI's JSON API already gives the file list per
  release, and `deptool` already speaks it (`_pypi_versions`). A wheel is a zip.
- **Extraction is stdlib.** `ast` parses the sources without importing them,
  which matters — importing a package to inspect it runs its code and needs its
  dependencies installed. `ast` needs neither. Public surface = module-level
  `def`/`class`/assignments not prefixed with `_`, plus `__all__` when present.
- **Signatures are richer than C++.** `ast.arguments` gives positional-only,
  keyword-only, defaults and `*args`/`**kwargs` exactly, so "a parameter became
  keyword-only" and "a default was removed" are detectable precisely — both are
  real breaks that no changelog reliably mentions.
- **Deprecation is visible.** A `DeprecationWarning` raised in a function body,
  or a `@deprecated` decorator, is a fact in the AST and a stronger signal than
  a changelog line.

The two honesty rules carry over unchanged and are the hard part, not the
parsing: a name must be absent from the **whole** target surface before it is
called removed (re-exports move between modules constantly, and `__init__.py`
re-export chains make this more common in Python than in C++), and an incomplete
read must never read as a clean bill of health.

What is genuinely harder than C++: dynamic re-export (`globals().update`,
`__getattr__` module hooks, conditional imports) is invisible to `ast`, so
"absent" is a weaker claim. Say so in the notes, as the C++ side does about
`#if`.

The consumed side already exists — `builtin._harvest_generic` reads Python
imports — though it shares the weakness in item 2: it harvests imported names,
so attribute access on an imported module (`np.frombuffer`) is missed.

### 2c. Python dependency discovery is too thin

Python is a first-class target, not one of the "others" the README lists it
under. What `_pypi()` reads today is PEP 621 `[project.dependencies]` and
`[project.optional-dependencies]`, plus `requirements.txt` *only when pyproject
yielded nothing*. Concretely missing:

- **Poetry** — `[tool.poetry.dependencies]` and `[tool.poetry.group.*]` are a
  different schema (a table of name → constraint, with `{version, extras,
  markers}` dicts) and are not read at all. A Poetry project currently reports
  zero dependencies.
- **PEP 735 `[dependency-groups]`** — the modern standard, and what `uv` uses for
  dev dependencies. Not read. This repo's own `pyproject.toml` uses it.
- **`uv.lock`** — the resolved, transitive, exact-version truth for a uv project.
  Not read. Same for `poetry.lock`, `Pipfile.lock`, `pdm.lock`.
- **No line numbers for pyproject deps.** `declared_in` is
  `"pyproject.toml (project.dependencies)"` — a file and a table name, no line.
  `requirements.txt` records a line; pyproject does not. So by the rule in item 7
  a pyproject dependency is currently **report-only** and `apply` cannot safely
  edit it. Fixing this means a line-recording TOML scan, since `tomllib` discards
  positions.
- **Version constraints are not ranges.** `_PY_REQ` takes the first specifier and
  strips the operators, so `>=2.0,<3` becomes `2.0`. For "how far behind are we"
  that reads a floor as a pin. Python constraints are ranges far more often than
  CMake pins are, so this matters more here than elsewhere.

The scope inference is also weaker than the C++ side: it regex-matches
`test|dev|lint` in the extra/group name. That is a reasonable default but it is a
guess, and unlike the CMake path it produces no `scope-evidence` the user can
disagree with.

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

### 6. Symbol-level breaking-change matching — *largely shipped with item 2*

The mechanical matching exists: `api_diff.affects_us` pairs each removed or
re-signatured symbol with our `file:line` sites, and the model explains and
prioritises rather than pattern-matching two prose lists. The `consumed` side is
fixed as of 0.3.0 (above), which was the stated prerequisite. What remains:

- **Per-call-site diagnosis.** A signature change reports which overloads moved,
  but not which of *our* calls would fail to compile. Answering that needs the
  argument types at each site, which is a job for a graph backend, not a regex —
  and the same backend answers the three structural `consumed` gaps left in item
  2, so this is now the largest single C/C++ item left.
- **Enum *values* and struct layout are not tracked.** Enumerator *names* are, as
  of 0.3.0, so a removed or renamed constant is reported; but a reordered enum, a
  changed explicit value, or a struct that gained a field is an ABI break the diff
  still sees as unchanged. Deliberate for now — reporting a renumbering as an API
  break would fire on every enum that gained a member in the middle, which is
  most of them — but for a dependency consumed as a prebuilt binary (item 1's
  motivating case) ABI is exactly what matters, so the two items meet here.

### 7. Ingest an SCA tool as a discovery source — *replaces "more ecosystems"*

This item used to read "write parsers for Gradle/Maven, NuGet, SwiftPM, Bazel,
Nix." That was wrong, and the survey in [Prior art](#prior-art--what-not-to-build)
is why: those parsers already exist, several times over, maintained by people
whose whole job is the edge cases. Writing a sixth Gradle parser buys nothing
the judgement layer needs.

The right shape is the one the analysis backends already use: **discovery
sources, auto-detected, additive**. `discover()` becomes one source among
several rather than the only one.

| Source | Gives | Cost |
|---|---|---|
| native CMake parser | the primary case; nothing else parses `FetchContent`/`pkg_check_modules` | already built |
| native manifest parsers | direct deps *with a line number*, so `apply` can edit them | already built, keep thin |
| Trivy | ~19 ecosystems, transitive, SBOM, single Go binary, and line spans for some ecosystems | one binary |
| ORT analyzer | broadest ecosystem coverage, resolved transitive tree | JVM + build tools |
| dependabot-cli | best-in-class update checking *and* file updating per ecosystem | Ruby in Docker |

Two constraints fall out of the data model and must be enforced, not papered
over:

- **A dependency with no line number is report-only.** It can be profiled,
  judged and reported; `apply` must refuse it rather than guess at an edit.
  This is the real reason to keep the small native parsers — they are what makes
  an ecosystem *editable*.
- **Merge additively, like the analysis backends.** An ingested dependency and a
  natively-parsed one must reconcile rather than one silently winning, and the
  source of each fact must stay visible.

Trivy is the first one to do: single static binary, no runtime for `deptool` to
depend on, and it covers the Python managers below.

### 8. Transitive dependencies — *mostly item 7's dividend*

Only direct dependencies are profiled. A CVE usually lands in a transitive one.
This needed "lockfile parsing per ecosystem", which is exactly what item 7
ingests instead — Trivy and ORT both emit a resolved tree.

What stays ours, because no SCA tool answers it: **"we do not call this, but our
dependency does."** A transitive dependency has no call sites of ours by
definition, so the usage profile is empty and the intersection is vacuous. The
useful framing is one hop up — *which direct dependency pulls it in, and do we
call the part of that dependency which uses it* — and that needs the reverse
reachability a graph backend gives. Without that, a transitive list is just a
longer list.

---

## Prior art — what not to build

The full analysis lives in the README under
[**"Isn't this an SCA tool?"**](README.md#isnt-this-an-sca-tool): a
(tool x language) coverage matrix for ORT, Trivy, OWASP Dependency-Check,
dependabot-core and OpenSCA against C, C++ and Python, each cell read from source
in 2026-08. Read it before adding a parser. The conclusions that constrain this
roadmap:

1. **Nothing parses CMake dependencies** — not one of the five. Same for C
   autotools. So C/C++ discovery stays ours, and the header diff has no
   competitor.
2. **Python discovery is a solved problem** — dependabot-core alone covers pip,
   pip-compile, pipenv, poetry and uv, with a `file_updater` and a
   `commits_finder`. Writing more Python parsers is duplicated work; items
   [2c](#2c-python-dependency-discovery-is-too-thin) and [7](#7-ingest-an-sca-tool-as-a-discovery-source--replaces-more-ecosystems)
   are scoped around that.
3. **Only Trivy and dependabot know `uv`.** ORT, Dependency-Check and OpenSCA do
   not. That picks the ingest target.
4. **Almost nothing records the declaration line** — Trivy does for
   pip/pom/conan/cargo but *not* for uv/poetry/pyproject; dependabot, ORT and DC
   are file-level only. Hence the report-only rule in item 7.
5. **No tool answers "is it worth upgrading."** The usage profile, the
   API-surface diff, coupled pins and the judgement are the whole of what is
   ours.

### Steps that follow

Ordered; each is independently shippable.

1. **Record declaration lines for Python** — prerequisite for everything else,
   because without it every Python dependency is report-only and `apply` cannot
   touch it. `tomllib` discards positions, so this needs a line-recording scan of
   `pyproject.toml` alongside the parse. (Part of item 2c.)
2. **Read the Python manifests that exist in the wild** — Poetry's
   `[tool.poetry.dependencies]` and `[tool.poetry.group.*]`, PEP 735
   `[dependency-groups]`. A Poetry project currently reports **zero**
   dependencies, which is a correctness hole rather than a coverage gap. (Item
   2c.)
3. **Keep constraints as ranges, not floors** — `>=2.0,<3` currently becomes
   `2.0`, which reads a floor as a pin and mismeasures "how far behind". Matters
   more in Python than in CMake because ranges are the norm there. (Item 2c.)
4. **Ship the Python API-surface diff** — `ast` over sdists/wheels from PyPI, the
   direct analogue of the header diff and the reason the judgement layer has
   anything factual to say about Python. (Item 2b.)
5. **Add a Trivy discovery source** — single static binary, keeps
   `[project.dependencies]` empty, brings transitive resolution and `uv`
   coverage. Merge additively with the native parsers, exactly as the analysis
   backends merge. (Item 7.)
6. **Mark ingested dependencies report-only** and make `apply` refuse them rather
   than guess an edit. Ship with step 5, not after it. (Item 7.)
7. **Consider dependabot-cli later, and only for update-checking** — its
   `update_checker` per ecosystem is better than anything reasonable to write
   here, but it is Ruby in Docker, so it stays optional and never a requirement.
   Do not adopt its `file_updater`; ours is line-precise and theirs is not.

Explicitly **not** doing: writing Gradle, Maven, NuGet, SwiftPM, Bazel or Nix
parsers (was item 7), or lockfile parsers per ecosystem for transitive
dependencies (was item 8). Both are ingest, not implementation.

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
  have not been run against a substantial real project. The 0.3.0 harvest fix is
  C/C++ only: `_harvest_generic` still records only *imported* names, so
  attribute access on an imported module (`np.frombuffer`) is missed, and the
  declared-name match has no equivalent there yet (see item 2b — the Python
  API-surface diff is what would supply the declared side).
- **The header diff's prose fallbacks** — migration-guide discovery is verified
  against `symfony/symfony` (which is where the versioned-filename case came
  from) and the commit log against `FluidSynth/fluidsynth`, but no *C/C++*
  dependency encountered so far actually ships a migration guide, so that path
  has never contributed to a real recommendation.
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
