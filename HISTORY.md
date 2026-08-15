# History

> Completed roadmap items, newest first.

The closed record for `deps` — what shipped, what was measured, and what was
decided against. Split out of `ROADMAP.md` so that file can stay a list of work
with instructions.

**Two layers.** *Completed roadmap items* below is the running log a retired
`R-nnn` lands in — one line, dated, recording what was **actually** achieved. The
long-form sections after it are the reasoning: what was measured, what was tried
and rejected, and why. When an item's story is worth more than a line, put the
line in the log and the reasoning in a section, and link them.

Read this before proposing something that looks obvious. Most entries are here
because a plausible idea turned out to be wrong when it met real data, and the
reasoning is the part worth keeping — the rejections are as load-bearing as the
features.

**Conventions.** Items marked **(observed)** came out of running the tool against
a real project rather than from speculation. `zeta-daw` was the development case;
a second, larger C++ project produced findings A–F, which are referenced by letter
throughout the code comments.

---

## Completed roadmap items

Newest first. Retired from ROADMAP.md; the ID is never reused.

### 2026-08-15

- **R-003** `pyproject.toml` dependencies carry a `path:line` and are editable.
  A line-recording scan runs beside the `tomllib` parse and hands back every
  string in every array with its line; `_pyproject` then takes the line **by
  index and checks the scanned text against the parsed value**, because
  agreeing on the count is not agreeing on the string and a line holding a
  *different* requirement is worse than no line — `apply` would bump whichever
  dependency happened to share the version. A spec the scan cannot reproduce
  (a `\uXXXX` escape) falls back to an unambiguous match by value and then to
  0, which leaves that one declaration report-only with a note while its
  neighbours on the same line are still located. Measured against **24 real
  `pyproject.toml` files on this machine, 112 dependencies: 112 located**, and
  every recorded line verifiably contains its requirement. Through `plan`:
  90 bump cleanly, 21 are declared with no constraint to move, 1 is refused
  (below). Before this, *every* pyproject dependency raised `report-only — the
  only record of it is pyproject.toml`. **Two findings beyond the item**, both
  from running it:
  - **`declaration_span` was CMake-shaped and would have made the bump edit the
    wrong lines.** It scanned forward for the first `(` *anywhere after* the
    recorded line, so on a line-oriented file the span ran from the dependency
    to some unrelated parenthesis further down: bumping `requests` also
    rewrote `attrs` on the next line and a `2.0` inside a `[tool.mypy]` string.
    It now decides from what the recorded line *starts with* — a command
    invocation gets the balanced span, everything else gets its own line, and
    an unbalanced paren falls back to the line rather than to the end of file.
    Latent for `requirements.txt`, `go.mod` and `conanfile.txt` too, and it
    only ever bit where a stray `(` sat below the declaration. It also gives
    Python dependencies a *meaningful* `decl` fingerprint for the first time —
    the old one hashed the whole file for want of a line — so an existing
    `CLAUDE_DEPS.md` for a Python project reports drift once, and regenerating
    it is the whole fix.
  - **A range is refused rather than flattened.** `httpx>=0.27,<1` reaches
    `apply` as the version `0.27,<1` — the roadmap said `2.0`, which is wrong,
    it is the whole tail — and swapping that produced `httpx>=9.9.9`,
    *silently dropping the upper bound*. Observed on a real project, and only
    reachable because this item made pyproject pins editable at all. `plan`
    now refuses any version still carrying an operator, in any ecosystem
    (npm's `lstrip` leaves the same shape). Carrying ranges through as ranges
    is R-006; refusing is what can be done honestly until then.
  Also: the unpinned-dependency error told a Python user to "update it through
  the OS package manager", which is right for `find_package(CURL)` and a
  confidently wrong answer to a different question for a `pypi` record.
- **R-002** Fixed the codebase-memory backend and turned it back on. The invocation
  now uses `--repo-path`/`--name` and **checks the return code** — the old flags exit
  1 with `unknown flag --path`, which was discarded, so a failed index was
  indistinguishable from an absent backend. The enricher is re-seeded from call
  sites, and its response parsing reads the documented `cols`/`groups` envelope by
  column instead of scraping any `name`-ish key, which is what turned the table's own
  headers into `zlib: 3 — Function, Method, compress`. On `endpoint/appv2` it now
  reports libcurl 77, zlib 4, libarchive 2, in 8.5s. Two corrections beyond the
  item: the module docstring's claim that graphify "cannot see namespaced C++" is
  false and was repeated in a test docstring, and a dependency that locates *no*
  site now says so rather than silently producing nothing. **The item's premise was
  partly wrong** — codebase-memory does not simply beat graphify at site location;
  it is worse on four of five dependencies. Measured in
  [Both backends compared on the same tree](#both-backends-compared-on-the-same-tree--2026-08-15).

### 2026-08-14

- **R-001** Re-seed blast radius from the enclosing function — `_enrich_graphify` now
  seeds from the callable containing each located `Site`, and accepts a label match
  only on a node with an empty `source_file` (a symbol graphify referenced but never
  defined, which is what an external symbol looks like). `blast_radius` counts only
  nodes that have a source file, and unlocated sites are reported as a lower bound.
  On `endpoint/appv2`: libcurl 7 → 31, libarchive 3 → 7, openssl 2 → 6, and zlib's
  1 now comes from `kaitai::kstream::process_zlib` instead of our own
  `ZStream::compress`. **Wider than planned:** `_enrich_codebase_memory` seeded the
  same way and was corrupting the merge — it reported `zlib: 3 — Function, Method,
  compress`, two of which are table headers scraped by `_collect_names`, and
  `_record`'s largest-wins rule let that override graphify's correct answer. Since
  codebase-memory indexes no external symbols at all, *every* symbol-name match it
  can make is one of our own homonyms, so that path is held off behind
  `CODEBASE_MEMORY_ENABLED = False` until R-002. Also found: graphify emits no
  callable nodes for out-of-line method definitions — `archive/z_stream.cpp` has only
  a file node, its methods being attributed to the header — which is why 5 of zlib's
  8 sites cannot be located, and a concrete reason to prefer codebase-memory once it
  works.
- **Pre-roadmap** — Graph backends generated and run for the first time against
  `endpoint/appv2` and this repo; `codebase-memory-mcp` 0.10.4 installed. Outcome
  diverged sharply from the plan: the intent was to start using `blast_radius`, and
  the measurement instead established that the existing seeding produces wrong
  numbers. Recorded in
  [Graph backends measured](#graph-backends-measured-for-the-first-time--2026-08-14);
  the resulting work is R-001, R-002, R-007, R-008, R-014, R-015.

---

## Shipped, by version

### Shipped in 0.1.0

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

### Shipped in 0.2.0 — how the header diff behaves

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

### Shipped in 0.3.0 — the consumed side of the intersection

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

### Shipped in 0.4.0 — discovery is variant-aware

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

#### Conan Center as a version source — the consequence of D

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

#### The recipe is not the repository — *shipped*

For as long as `dep.upstream.ref` held a recipe name, **the header diff could not
run on a Conan dependency at all** — the tool's central mechanism was unavailable
to exactly the project class that motivated findings A–F. `upstream.conan_source_repo`
now reads the recipe's own `conandata.yml` and takes the repository its sources
come from, which turns the whole API-surface diff on for Conan-pinned C/C++.

Measured on the project that motivated A–F: **0 of 17 Conan dependencies were
diffable before, 8 after** — 8 recipes resolve to a repository, and all 8 pinned
refs resolve to a readable tree. Two of those needed the tag fix below; the first
measurement counted repositories resolved and was 6 end-to-end until then. The
nine that still are not resolve honestly rather
than silently — they genuinely build from elsewhere (the GNU autotools chain,
pkgconf from distfiles.ariadne.space, xz_utils from tukaani.org, cmake as a
prebuilt archive), and a recipe with no GitHub source reports that as its
`api_diff.reason` instead of going quiet.

Two things the live index taught, both of which a naive version gets wrong:

- **The first URL is not the answer.** zlib lists zlib.net ahead of
  `madler/zlib`, libcurl lists curl.se ahead of `curl/curl`. The rule is the
  first URL that is a GitHub *repository*.
- **`patches:` carries URLs too**, and a patch is not where the library's code
  lives, so the read stops at the end of the `sources:` mapping.

The recipe stays the version source. Conan Center is what knows whether a pin is
still installable (`pin_unavailable`), so `upstream.ref` is untouched and the
repository is kept beside it as `upstream.source_repo` — two different facts.
It is deliberately **not** written to `CLAUDE_DEPS.md`: everything the profile
records is read from the repository itself, and `status` must keep working with
no network.

Because the catalogue is pruned, the pinned version is usually already gone from
`config.yml`. The repository is a property of the recipe rather than of the
version, so the lookup falls back to the rest of the mapping and says which it
used — `for 3.9.1` versus `for another version of the recipe; 1.2.11 is not in it`.

##### The tag is declared too — *shipped*

Resolving the repository is not enough: `tag_forms` guessed `v<version>` and
`<version>`, which is wrong for any project that prefixes its tags. openssl
publishes `openssl-3.6.3` and curl publishes `curl-8_21_0`, so both resolved to a
repository and then failed at the tree listing — and a wrong guess does not
degrade the answer, it **removes** it, falling back to release notes with no sign
that a factual diff was available.

The recipe declares the tag as well, inside the source URL it already parses
(`/releases/download/<tag>/…`, `/archive/refs/tags/<tag>.tar.gz`), so `tag` in the
version list is now read rather than assumed. Two consequences worth recording:

- **A pruned pin still needs a tag**, and it is one end of every diff. The naming
  pattern is derived from upstream's own `(version, tag)` pairs — if openssl
  publishes 3.6.3 as `openssl-3.6.3` then `openssl-3.2.1` follows a declared
  convention rather than a guess. The separator is derived too, because curl
  rewrites the dots. Candidates stay ordered read → inferred → guessed, since each
  miss costs a tree listing.
- **A parser bug this exposed, and it predated the mapping work.** openssl
  annotates its LTS and FIPS entries with inline comments (`3.5.7: # LTS: …`), and
  `_CONAN_VERSION_KEY` required end-of-line after the colon — so those versions
  were skipped entirely, and `conan_recipe_patches` reported them as *absent from
  the recipe* when they were present. A wrong answer rather than a missing one.
  Fixing it also recovered `1.1.1w` → `OpenSSL_1_1_1w`, upstream's older naming,
  which no guess would ever have produced.

After both, all **8 of 8** repo-resolved Conan dependencies have a pinned ref that
resolves to a readable tree.

#### What A, D and B did not settle

A bump across variants is atomic only when they already agree, and *resolving* a
disagreement is still the user's edit. Ranking divergence by whether the diverging
versions differ in a symbol we consume — B's stated goal — needs the API-surface
diff run per variant, which meets **E** (build options decide what the API is) and
should be done there rather than twice. And the OSV lookup for a Conan dependency
still falls back to a bare-name query, which returns distro-flavoured advisories
in bulk; the sentinel guard keeps them honestly labelled but the list is long.

---

### Shipped in 0.5.0 — the lockfile, and an ingested scanner

Finding C, and item 7's first source. Both answer "what is actually built"
rather than "is there something newer", which is the shift
[the second project forced](#what-this-changes-about-the-strategy).

#### The lockfile is a declaration site of its own

`conan.lock` is parsed natively — Conan 2's flat `requires` / `build_requires` /
`python_requires` / `config_requires` lists and Conan 1's `graph_lock.nodes`
object. Line numbers are recovered by scanning the raw text in document order,
because `json.load` discards positions and a resolved version nobody can go and
look at is half a fact. The format permits the same package twice at two
versions, so nothing assumes uniqueness.

What it produces, none of it from a network call:

- **Manifest-versus-lock divergence.** `Dep.lock_drift()` reports versions the
  lock resolved that no manifest asks for. On the second project that is one
  library **two minor versions behind** every manifest — either the lock is
  stale or the build is not using it.
- **The transitive set.** Three libraries ship that no conanfile names. They are
  marked transitive, because their usage profile is empty *by construction* and
  an empty profile read as "unused" is exactly the confidently-wrong answer this
  tool exists to avoid.
- **Build-only requirements separated**, so eight build tools are not reported
  as runtime risk.
- **One package locked at two versions at once** — a build requirement resolved
  differently for two profiles — reported rather than silently deduplicated.

Two consequences were bigger than the parser:

- **A lockfile pin is not editable.** It has a version, a path and a line, so it
  looks exactly like a declaration `apply` may rewrite — but the version sits
  beside the recipe revision it was resolved with, and hand-editing one
  desynchronises the pair. `GENERATED_KINDS` marks the kind, `is_editable()`
  excludes it, and `apply` refuses any dependency whose every declaration is
  uneditable. What it does instead is report `regenerate`: the generated files
  that still record the old version, because a bump nobody regenerates the lock
  for changes the declaration and not the build.
- **`diverges()` now means the *hand-written* declarations disagree.** It is the
  predicate `apply` refuses on, and a stale lock must not block a bump: two
  manifests contradicting each other means the edit has no defined starting
  point, whereas a lock contradicting the manifests is a fact about the past.
  Left as one predicate, the lockfile parser would have blocked every legitimate
  bump on the project it was written for.

#### Trivy as a discovery source

`sources.py` is the analogue of `backends/`: auto-detected, never required,
merged additively. Trivy only — one static Go binary, no runtime to depend on
(ORT needs a JVM, dependabot-core is Ruby in Docker).

- **Report-only is enforced by the model, not by a special case.** An ingested
  package with no line number has no editable declaration, and the gate `apply`
  already needs for lockfiles refuses it.
- **Merged, not preferred.** An ingested `conan.lock` line is recorded as the
  same kind our own parser records, so the two collapse to one declaration; the
  native site keeps the line `apply` edits.
- **Families are not merged across ecosystems.** An ingested ecosystem with no
  native parser is its own namespace, so a NuGet package and a `find_package`
  result that share a name stay separate rather than inventing a fact.
- **The invocation was measured, not assumed.** `--list-all-pkgs` with the native
  JSON format is the only shape carrying `Locations`; CycloneDX and SPDX drop
  line numbers. Package analysis needs *some* scanner enabled, and the choice is
  not free: `vuln` against a cached database took 0.8s on a real repository where
  `secret` took 5.1s. So the fast path runs first and offline
  (`--skip-db-update --offline-scan`), with `secret` as the fallback for a fresh
  install whose database has never been downloaded — never a silent 50MB fetch.

Measured against the second real project:

| | native only | with the ingest |
|---|---|---|
| dependencies found | 18 | **32** |
| ecosystems | C/C++, Python | + NuGet (15 packages, 5 manifests, **no native parser**) |
| conan packages | 18 | 18 — Trivy's 9 are a strict subset and collapse into ours |
| cost | 0.10s | 0.65s |

Trivy reads `conan.lock` but not `conanfile.txt`, and takes only `requires` from
the lock — so for Conan the native parser is strictly better and the ingest adds
nothing but confirmation. Its value is the ecosystem nobody here parses at all,
which is precisely the split item 7 predicted.

Ingest is on by default and `--no-ingest` turns it off; the profile header
records which sources ran, because "no dependencies" means something different
when only the native parsers were used. The test suite disables it globally
(`tests/conftest.py`) and switches it back on against a fake binary — otherwise
every discovery test would depend on what is installed on the machine running it.

Still not done here: Trivy's own vulnerability findings are ignored (the OSV path
already covers that, and reconciling two advisory sources is its own decision),
and an ingested NuGet package has no upstream resolver, so it is discovered and
reported but "is there something newer" is answered honestly with "no resolver".

---

---

## The second real project — findings A to F

Kept in full because the A–F labels are referenced from code comments and commit messages.

### Measured against a second real project **(observed)**

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

#### A, D and the core of B — *shipped, see [0.4.0](#shipped-in-040--discovery-is-variant-aware)*

The three were shipped as one change, because A alone makes things worse: it
surfaces the pins *and* leaves every such library appearing twice, once pinned
from the manifest and once unpinned from `find_package`, with the unpinned one
still winning. The findings below are kept as written, since they are the record
of what the second project actually showed.

#### A. Manifests are only discovered at the repository root

`detect_manifests` tests `isfile(join(root, name))` for each manifest, so a
manifest one directory down does not exist as far as the tool is concerned. This
project keeps its Conan manifests in per-platform subdirectories — the parser for
them already works, and was never handed a file. The result is not a degraded
answer, it is *no* answer: the pins are invisible and what gets reported instead
is the `find_package` calls, which carry no version.

This is a discovery-walk bug, not a parser gap, and it is the cheapest
high-value fix on this list. It also picks up nested manifests in other
ecosystems for free.

#### B. One pin per dependency is the wrong data model

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

#### C. The lockfile holds a different truth from the manifest — *shipped, see [0.5.0](#shipped-in-050--the-lockfile-and-an-ingested-scanner)*

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

#### D. The CMake name is not the package name

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

#### E. Build options decide what the API even is

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

##### "Locally patched" lowers confidence — *shipped*

E's third part, and the one the Conan upstream mapping made urgent: once the
header diff started running on Conan dependencies it began producing confident,
factual-looking output about libraries that are **not** what gets built. `Dep.patched`
now records why, from three declared facts, and the judgement layer is told to
treat it as a confidence question rather than a ranking one:

- **`PATCH_COMMAND` / `UPDATE_COMMAND`** on a `FetchContent_Declare`, which
  forwards both to `ExternalProject_Add`. One `_kv()` call on arguments the CMake
  parser already had.
- **A tracked diff header naming a recipe path** (`--- a/recipes/<name>/…`), found
  with `git grep` — ~30ms on a 1200-file tree, and tracked-only on purpose,
  because a patch a fresh checkout does not get is not what the build applies.
  The observed case applies a `patch` heredoc from a CI script, so there is no
  local `recipes/` directory to find; the diff header is the whole fact.
- **Conan Center's own `patches:`**, which apply even when nobody here has
  touched anything. zlib 1.3.2 carries one, libarchive 3.8.7 carries three,
  openssl and libcurl carry none.

Three things this got right that a simpler version would not:

- **The weighting is read, not guessed.** Conan declares `patch_type: conan` for
  build-system plumbing, so "three patches, all plumbing" and "one patch, untyped"
  produce different confidence language from the same mechanism.
- **"Cannot be established" is not "none".** When the pin is gone from the recipe
  the patch set is unrecoverable, which the pruned catalogue makes common — zlib
  1.2.11 and openssl 3.2.1 both hit it.
- **The caveat rides on the diff itself**, not only on the finding. The diff is
  the output that reads as factual, so that is where it has to admit it read a
  repository that is not what ships.

Not gated on `behind_by`: libarchive 3.8.7 is *current* and still not upstream's
release, and one read of `conandata.yml` answers both this and the source
repository, so gating would hide a finding to save nothing.

**Still open in E:** using the declared build options to resolve `#if` in the
header diff, and recording static-versus-shared to reweight ABI findings. Both
are about what the surface *is*; this part was about how far to trust a reading
of it.

#### F. Vendored trees and multiple products in one repository

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

#### What this changes about the strategy

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

---

## Items that closed after their version section

#### 2. Trust the changelog less — *shipped, see above*

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

#### 3. Verify in a sandbox, not in place — *shipped*

The premise of this tool is that it advises and, when explicitly asked, edits
exactly the pin. A verification that edited the working tree and *then*
discovered the build was broken had already done the thing it was run to prevent,
and it left a `build/` directory and a `.deptool.bak` behind either way.

- `apply --verify` now copies the tree somewhere disposable, applies the edits
  **there**, builds and tests **there**, and writes to the real checkout only if
  that passed. A bump that does not compile is a no-op on the user's tree, and
  exits 3 so the caller knows there is nothing to revert.
- A **copy rather than a `git worktree`**, deliberately. A worktree holds `HEAD`,
  so it would verify the bump against code the user has not got — and creating one
  writes into their `.git`. The cost is missing VCS metadata, so a build deriving
  its version from `git describe` can fail in the sandbox and be fine in place;
  `SANDBOX_NOTE` says so and `--in-place` restores the old behaviour.
- **A skipped step is not a pass.** `established` is false when the toolchain is
  absent, which is reported as "nothing was proved" rather than green.
- **Backups moved out of the checkout** to
  `${XDG_CACHE_HOME:-~/.cache}/deptool/backups/<hash of root>/`, so an apply adds
  nothing to `git status`. A legacy in-tree `.deptool.bak` is still honoured by
  `revert`, and restoring it removes the litter.

Not done, and the reason item 4 is still open: the second half of this item was
"several candidate versions can be tried in parallel". The sandbox makes that
possible — `make_sandbox` is independent per call — but nothing yet drives it, so
"does 0.9.0 build even though 0.11.1 doesn't?" still has to be asked one version
at a time.

#### 6. Symbol-level breaking-change matching — *largely shipped with item 2*

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

#### 7. Ingest an SCA tool as a discovery source — *Trivy shipped, see [0.5.0](#shipped-in-050--the-lockfile-and-an-ingested-scanner)*

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

#### 8. Transitive dependencies — *mostly item 7's dividend*

Only direct dependencies are profiled. A CVE usually lands in a transitive one.
This needed "lockfile parsing per ecosystem", which is exactly what item 7
ingests instead — Trivy and ORT both emit a resolved tree.

**Partly shipped in 0.5.0 for Conan**, and by the native parser rather than the
ingest: `conan.lock` names the transitive set, so those dependencies are now
discovered, scoped and marked transitive. What is *not* shipped is the useful
half below — nothing yet says which direct dependency pulled one in. A Conan 2
lockfile is a flat list with no edges, so that needs either the Conan 1 node
graph, Trivy's `DependsOn` (absent from the version measured), or a resolve.

What stays ours, because no SCA tool answers it: **"we do not call this, but our
dependency does."** A transitive dependency has no call sites of ours by
definition, so the usage profile is empty and the intersection is vacuous. The
useful framing is one hop up — *which direct dependency pulls it in, and do we
call the part of that dependency which uses it* — and that needs the reverse
reachability a graph backend gives. Without that, a transitive list is just a
longer list.

---

---

## Decisions that closed

### Prior art — what not to build

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
   `commits_finder`. Writing more Python parsers is duplicated work;
   [the Python items](ROADMAP.md#1-python-is-producing-wrong-answers-highest-priority)
   and the Trivy ingest below are scoped around that.
3. **Only Trivy and dependabot know `uv`.** ORT, Dependency-Check and OpenSCA do
   not. That picks the ingest target.
4. **Almost nothing records the declaration line** — Trivy does for
   pip/pom/conan/cargo but *not* for uv/poetry/pyproject; dependabot, ORT and DC
   are file-level only. Hence the report-only rule in item 7.
5. **No tool answers "is it worth upgrading."** The usage profile, the
   API-surface diff, coupled pins and the judgement are the whole of what is
   ours.

#### Steps that follow

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
5. ~~**Add a Trivy discovery source**~~ — *shipped in 0.5.0.* Single static binary, keeps
   `[project.dependencies]` empty, brings transitive resolution and `uv`
   coverage. Merge additively with the native parsers, exactly as the analysis
   backends merge. (Item 7.)
6. ~~**Mark ingested dependencies report-only**~~ — *shipped in 0.5.0*, and it fell
   out of the model rather than needing a special case: no line number means no
   editable declaration, and `apply` refuses those. (Item 7.)
7. **Consider dependabot-cli later, and only for update-checking** — its
   `update_checker` per ecosystem is better than anything reasonable to write
   here, but it is Ruby in Docker, so it stays optional and never a requirement.
   Do not adopt its `file_updater`; ours is line-precise and theirs is not.

Explicitly **not** doing: writing Gradle, Maven, NuGet, SwiftPM, Bazel or Nix
parsers (was item 7), or lockfile parsers per ecosystem for transitive
dependencies (was item 8). Both are ingest, not implementation.

#### `CLAUDE_DEPS.md` stays markdown — measured, do not revisit

Asked whether a denser format would cut context cost. Measured on a real 18-dep
set with assessment prose attached, `cl100k` as tokenizer proxy:

| format | tokens | vs markdown |
|---|---|---|
| **markdown (current)** | **7,174** | **1.00×** |
| SQLite `.dump` | 7,481 | 1.04× |
| JSONL compact | 8,211 | 1.14× |
| YAML | 8,234 | 1.15× |
| JSON compact | 8,464 | 1.18× |
| TOON v4.1.1 (tab/comma/pipe) | 8,502 / 8,569 / 8,641 | 1.19× / 1.20× |
| JSON `indent=2` | 10,799 | 1.51× |

Markdown wins for three reasons that are properties of *this* data, not of
markdown: it omits empty fields, it never quotes, and it embeds prose as prose.
Every alternative pays for at least one of those. Specifically:

- **JSON** re-states every key per dependency and escapes the assessment
  newlines, which also collapses the reviewable part of the diff to one line.
- **TOON** cannot tabularise `deps[]` at all — tabular form needs uniform
  primitive-valued keys, and every `Dep` carries variable-length `declarations`,
  `sites`, `consumed` and `notes` — so it degrades to list form and the `[N]`
  markers become pure overhead. It must also quote every `file:line` (contains
  `:`) and every numeric-looking version, and it has no block-string form. All
  three are conditions TOON's own README names as bad fits.
- **SQLite** is disqualified before tokens: the file is committed and reviewed in
  a PR, so a binary blob has no diff and no resolvable merge conflict. Its text
  dump also measured *larger* than the markdown, at 57KB on disk versus 24KB.

Two things this did surface, both parked deliberately:

- **A write-side verb (`assess --dep N`) would cut the profile read entirely.**
  Assessments are only 25% of the file, and there is no verb that writes one, so
  the skill's "update the `### Assessment` block" forces a full read to satisfy
  `Edit`'s precondition — ~7.2k tokens read to write ~100. Not done, because it
  is 10–15% of a `/deps:check` turn once the `check --json` payload is counted,
  and `carry_over` already means there is no correctness bug to fix. **Revisit at
  ~50 deps**, where the read alone passes 20k, or on a real context exhaustion.
- **TOON does win on uniform primitives** — 586 tokens versus 789 compact JSON
  and 1,190 pretty, for the same 18 rows flattened to six scalar fields. That is
  not this file, but it is arguably the shape of the `check --json` payload, which
  no human reviews. Untested: `change_evidence`/`api_diff` may be no more uniform
  than the profile. Measure before believing it.

#### What in 0.3.0 is fitted to one project, and should be cut back

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

## Both backends compared on the same tree — 2026-08-15

R-002 predicted that fixing codebase-memory would make it the better backend,
because 2026-08-14 found graphify emitting no callable node for the out-of-line
methods in `archive/z_stream.cpp`. Running both against `endpoint/appv2` with
identical site input shows that prediction holds **only for that one shape**.

Sites located, and the resulting radius (non-`include` sites only):

| dep | graphify | codebase-memory |
|---|---|---|
| libarchive | **12/12** → 7 | 2/12 → 2 |
| libcurl | **12/12** → 31 | 7/12 → **77** |
| libiconv | **3/3** → 1 | 0/3 → unmeasured |
| openssl | **11/12** → 6 | 0/12 → unmeasured |
| zlib | 3/8 → 1 | **5/8** → **4** |

**Neither dominates, and they fail differently.** codebase-memory wins exactly
where 2026-08-14 predicted — `zlib`, whose sites sit in `ZStream::compress`, an
out-of-line method of a *top-level* class, which it records at
`archive/z_stream.cpp:72-147` and graphify does not record at all. But the same
gap reappears one level down: for a **nested** class it attributes the member to
the header *declaration* and emits nothing spanning the definition body, so
`LibarchiveBasedArchive::ArchiveEntry::ArchiveEntry` exists only at
`libarchive_based_archive.h`, and all ten sites in `archive/archive_entry.cpp`
land in no callable. That file's entire graph contribution is three macros and a
module node. graphify locates all twelve.

openssl locates nothing for a third reason: the index records
`Class SignatureVerifier` (38-96) with no method nodes inside it, so its members
are not in the graph at any location.

Where it does locate, its radius is larger and better founded — libcurl 77
against graphify's 31, from 3× the edges (100370 vs 32612). So the two answer
different fractions of the same question, which is the case for keeping the
additive merge rather than picking a winner.

**One caveat on the comparison.** graphify's higher location rate is partly an
artefact of a looser rule: with no end line it takes the nearest declaration
*above* the site, so it will attribute a site in a gap to whatever precedes it.
codebase-memory reports an explicit range and is tested for real containment, so
it returns nothing rather than the function above. Some of graphify's 12/12 is
therefore confidence rather than coverage — untested either way, and worth
checking before treating its location rate as strictly better.

### Cost, and why this reads the graph in bulk

Every CLI call pays a daemon start-up: **3.7s cold, 1.25s warm**. Seeding
per site via `trace_path` — the shape the old code implied — costs one call per
seed and would have run to minutes on appv2. Instead the whole graph is pulled in
a fixed handful of calls (`search_graph` returned all 3712 methods in 1.3s;
`query_graph` all 10046 CALLS edges in 2.9s) and walked in memory with the same
`_reverse_bfs` graphify uses. **Whole-tree analysis: 8.5s**, independent of
dependency count.

`query_graph` has no JSON mode — the global `--json` flag wraps its *text* table
in an MCP envelope rather than structuring it — so its rows are parsed with
`shlex` against the declared column header. C++ makes this necessary: qualified
names like `operator std::string` contain spaces and come back quoted.

Two smaller findings: `trace_path` refuses a bare name (`compress` returns
`status: ambiguous` with both candidates) so seeds must be qualified, which the
enclosing-function lookup supplies for free; and graphify's own 21 MB
`graphify-out/` is itself indexed by codebase-memory when both are used on the
same checkout, putting `GRAPH_REPORT.md` sections into appv2's graph.

## Graph backends measured for the first time — 2026-08-14

Roadmap §4.1 said "no graph has ever been generated for either project". That is
now false: both backends have been generated and run against **endpoint/appv2**
(C++, 1234 source files) and **update-tools** itself (Python). `codebase-memory`
was not installed on this machine at all, so §4.3 was not merely unverified — it
was unstartable. It is installed now (`uv tool install codebase-memory-mcp`,
0.10.4, native runtime downloaded from GitHub Releases and checksum-verified).

### What each tool produced

| | graphify | codebase-memory 0.10.4 |
|---|---|---|
| appv2 (C++, 1234 files) | 30s — 17185 nodes, 32612 edges | 20s — 60070 nodes, 100370 edges |
| update-tools (Python) | 1.6s — 795 nodes, 1671 edges | 1454 nodes, 4087 edges |
| parse failures on appv2 | 447 files (36%) | 368 files, mostly `third-party/armadillo` header-only templates |
| artefact | 21 MB `graphify-out/` **in the checkout**, not gitignored | `~/.cache/codebase-memory-mcp`, tree untouched |
| install | pure Python via uv | downloads a per-platform native binary on first run |

### C++ support: both real, one better

**graphify parses C++ properly.** `extract.py:858` defines a `_CPP_CONFIG` with
`class_specifier`/`struct_specifier`, `qualified_identifier` in
`call_accessor_node_types`, a `_cpp_collect_type_refs` type-reference collector
and ~8 C++-specific branches in `engine.py`.

Two claims in `backends/__init__.py` are **wrong** and were written from
assumption rather than measurement:

1. Line 9, *"Graphify resolves the C ABI but cannot see namespaced C++"* —
   contradicted by `qualified_identifier` above.
2. A concern raised and then disproved in the same session: `.h` maps to
   `extract_c` in the static dispatch table (`extract.py:4671`), which looked
   fatal here because 387 of appv2's 474 `.h` files contain C++-only constructs.
   It is not fatal — content-sniffing reroutes at runtime. Measured on `tod.h`:
   26 nodes under the C grammar, 118 under the C++ grammar, **97 in the real
   graph**. The sniffing works.

**codebase-memory adds type resolution.** 158 vendored tree-sitter grammars,
plus "Hybrid LSP" semantic type resolution for C, C++, Python, TS/JS, PHP, C#,
Go, Java, Kotlin, Rust and Perl — parameter binding, return-type inference,
generic substitution. That is precisely the machinery §4.2's three `consumed`
gaps need.

Two behaviours make it more honest than graphify for our purposes:

- **It refuses to guess.** Asked to trace `compress`, it returned
  `status: ambiguous` with both candidates (`ZStream.compress` and armadillo's)
  and demanded a qualified name. graphify silently picked one.
- **It grades its own edges.** `trace_path --include-evidence` returns a
  strategy class per hop — `lsp | language_rule | heuristic | unresolved` — and a
  confidence. That maps directly onto standing rule 4; graphify offers no
  equivalent.

### The finding that matters: neither models the dependency boundary

**Both index only your own repository.** Third-party library symbols are not
nodes in either graph, because the library's source is not in the tree.

Of the 74 symbols deptool records as `consumed` across appv2's dependencies,
**5 matched a graphify node**, and every one of those five was a false match:

- `archive_entry`, `curl_slist`, `EVP_MD_CTX` — nodes with an **empty
  `source_file`**: dangling reference stubs, not definitions.
- `compress` — resolves to `archive/z_stream.h:40`, **our own** `ZStream`
  method, which merely shares a name with zlib's.

codebase-memory is identical: `curl_easy_setopt`, `deflate`, `BIO_free`,
`archive_entry_new`, `iconv_open` all return `function not found`, and
`search_graph` for `curl_easy.*`, `.*deflate.*`, `BIO_.*`, `archive_entry.*`
returns 0 rows each. A control query for our own `doAction` returns 2 rows, so
the queries are correct and the zeroes are real. Python behaves the same way:
neither graph contains `tomllib`, `urlopen` or `subprocess`, and
codebase-memory's only `load` hit is our own `profile.load`.

**So `blast_radius` as currently seeded is not merely thin, it is wrong.** zlib
was reported with `blast_radius=1, call_depth=1, "e.g. postprocess"` — a number
derived entirely from our own `ZStream::compress`. It was right by coincidence
of naming, and found nothing for the other seven zlib symbols. This is standing
rule 3 violated by our own code: `_enrich_graphify` seeds by matching a
dependency's symbol names against graph labels, which is guessing, not reading a
declared fact.

### The fix is backend-agnostic: seed from the enclosing function

deptool already holds the right seed. The builtin extractor records
`Site(path, line, symbol, context)` per call — e.g.
`archive/z_stream.cpp:109 deflate call`. The correct query is therefore not
"find a node named `deflate`" but "find the function *containing*
`z_stream.cpp:109`, then walk inbound from it".

Verified end to end: that site sits inside `ZStream::compress`
(`archive/z_stream.cpp:72-147`), and codebase-memory's inbound trace from it
returns `callers_total: 1` — `postprocess`, strategy `lsp`, confidence `0.95`.
Both backends can answer this shape of question, because both model our own code
well. Neither can answer the shape we currently ask.

### Integration bugs found by running it

- `backends/__init__.py:264` invokes `index_repository --path R --project P`.
  The real flags are `--repo-path` and `--name`; `--project` is rejected outright
  with `unknown flag`. The return code is never checked, so this fails silently,
  every `trace_path` then queries an unindexed project, and the enricher returns
  `False` having contributed nothing. **codebase-memory could never have worked**,
  even once installed.
- `graphify-out/` is not gitignored, and is 21 MB on appv2.

### Criteria — which backend, when

The rules, in order:

1. **Neither backend is consulted for "what do we consume".** That is the
   builtin extractor's job and stays that way. Graph backends answer exactly one
   question: *how much of our own code reaches a call site we already located.*
   Any use that seeds from a dependency's symbol name is a bug, not a
   configuration choice.
2. **Prefer codebase-memory wherever it is installed.** It resolves types on
   C, C++ and Python — our two first-class targets plus one — grades every edge
   it returns, refuses ambiguous matches instead of guessing, produced 3.5× the
   nodes in two-thirds the time on appv2, and leaves the checkout untouched.
   **Superseded 2026-08-15:** measured against graphify on the same tree it
   locates fewer sites on four dependencies of five, so prefer it for *radius*
   and edge quality, not for coverage — and run both where coverage matters. See
   [Both backends compared](#both-backends-compared-on-the-same-tree--2026-08-15).
3. **Use graphify when a native binary is unacceptable or unavailable** — a
   locked-down machine, an air-gapped build, an unsupported platform. It is pure
   Python, installs via uv, and emits a portable `graph.json` that deptool reads
   with no daemon and no subprocess round-trip. That artefact property also makes
   it the better choice if a graph is ever to be **committed or shared in CI**.
4. **Run both only when the two disagree about a dependency worth arguing
   over.** The additive `_record` merge (largest-wins) is defensible precisely
   because the two count different things, but a second backend costs a second
   index of the same tree for a lower-bound that rarely changes the judgement.
   Both is a diagnostic mode, not the default.
5. **Neither is worth running on a repository under ~500 files.** The builtin
   extractor already reports the call sites, and a blast radius over a tree that
   small is something the reader can see unaided.
6. **Never let a backend downgrade an answer to a confident wrong one.** If
   seeding cannot locate the enclosing function, the honest output is an unset
   `blast_radius`, not a number derived from a name collision.

