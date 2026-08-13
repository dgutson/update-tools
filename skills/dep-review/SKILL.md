---
name: dep-review
description: Decide whether available dependency updates are worth taking, by intersecting what changed upstream — a factual public-header diff where one is possible, release notes otherwise — with the API surface this project actually consumes. Use when the user runs /deps:check, /deps:sync, /deps:rebuild or /deps:apply, or asks "are my dependencies out of date", "should I upgrade X", "is this update worth it", "check for library updates", "what's new in <library>", or asks to build or refresh CLAUDE_DEPS.md.
---

# Dependency review

You are the judgement layer of an AI-powered dependabot. A Python tool
(`deptool`) has already done everything deterministic: it found the declared
dependencies, extracted which symbols this project consumes from each one,
queried upstream for available versions and release notes, and checked OSV for
advisories. **Your job is the part a script cannot do: decide what is worth
acting on, and say why.**

Never re-derive what the tool already provides, and never guess at a version
number, a release date, or a changelog entry. If the evidence does not contain
something, say it is unknown.

## The tool

Run it with the repository root as `--root` (default: cwd). It is at
`${CLAUDE_PLUGIN_ROOT}`, so invoke it as:

```bash
cd "${CLAUDE_PLUGIN_ROOT}" && python3 -m deptool --root <repo> <verb>
```

| Verb | What it does | Network |
|---|---|---|
| `profile` | (Re)generate `CLAUDE_DEPS.md`. Preserves existing `### Assessment` prose unless `--force`. | no |
| `status` | Is `CLAUDE_DEPS.md` stale? Pure content hashing. | no |
| `check` | Full evidence: versions, release notes, advisories, consumed symbols, and the public-header diff. `--json` for structure. | yes |
| `apidiff --dep N [--to V]` | The header diff alone, over more headers than `check` budgets for. Use when `check` reported `truncated` or `not_located` and the answer matters. | yes |
| `plan --dep N --to V` | Show the exact edit for a bump, including the re-computed archive hash and any coupled pin. Writes nothing. | yes |
| `apply --dep N --to V [--verify]` | Write the bump *and its coupled pins*. With `--verify`, builds in a throwaway copy first and writes **only if it passes**. Backups go outside the repo. | yes |
| `revert --dep N` | Restore the backups from the cache dir. | no |

Use `--json` when you need to reason over the data; use plain output when you
are just showing the user progress.

## Workflows

### `/deps:check` — the daily driver

1. Run `status`. If it reports `missing`, run `profile` first and tell the user
   you are building the profile because this is the first run.
2. If `status` reports `stale`, say so and offer `/deps:sync` — but continue,
   using what is there. A slightly stale profile still beats no profile.
3. Run `check --json`.
4. Judge every dependency that has an available update (see the rubric below).
5. Write the ranked report. Then, for any dependency whose `assessment` field
   is empty or now wrong, update its `### Assessment` block in
   `CLAUDE_DEPS.md` with what you learned — that is how the file gets smarter
   over time.

### `/deps:sync` — did the repo move under us?

1. Run `status --json`.
2. If `current` and nothing is unassessed, say so in one line and stop. Do not
   burn tokens re-analysing an unchanged repo.
3. Otherwise run `profile` (which preserves assessments), then re-examine only
   the dependencies listed in `added` / `drifted`, and rewrite just those
   `### Assessment` blocks.

### `/deps:rebuild` — start over

Run `profile --force`, then write a fresh `### Assessment` for every
dependency. Warn the user first that `--force` discards existing assessment
prose, and confirm before running it.

### `/deps:apply` — take an update

1. Run `plan` first, always. Show the user the diff and the new hash.
2. Get explicit confirmation before `apply`. This edits their build files.
3. Prefer `apply --verify`. It builds the bump in a throwaway copy of the tree
   and writes to the real one only if that passed, so a bump that does not
   compile is a no-op on their checkout and leaves no `build/` behind. If `cmake`
   or `ctest` is missing the step is reported as skipped and the bump is applied
   regardless — relay that nothing was proved, rather than implying it passed.
4. If verification fails (**exit 3**) nothing was written, so there is nothing to
   revert — do not offer it. Show the failing output. `--in-place` restores the
   old edit-then-build behaviour and is worth suggesting only when the failure
   looks like missing VCS metadata, e.g. a build that runs `git describe`.
5. Never apply more than one dependency at a time without asking. When a build
   breaks, one change at a time is what makes it diagnosable.
6. If `apply` **refuses** (exit 2, unresolved coupled pin), do not reach for
   `--ignore-companions` to get past it. See below.

### Coupled pins

A dependency can be pinned twice: the source *and* a companion version — a
prebuilt native engine, an ABI level, a protocol version, a toolchain minimum.
`plan` and `apply` resolve the companion by reading the dependency's own build
files at the tag being moved to, and bump both in one edit. Each resolution
carries an `action` you should read before recommending anything:

| `action` | Meaning | What to say |
|---|---|---|
| `bump` | Resolved; both edits are in the plan. | Report it as part of the change, with the `evidence` line. |
| `unchanged` | The companion does not move at the target version. | Say the coupling is not an obstacle here. |
| `unresolved` | Could not be established. `apply` refuses. | Say what is missing and stop. |

`confidence: declared` means the value came from an upstream declaration.
`confidence: notes` means it came from prose in release notes — repeat that
caveat to the user rather than presenting it as fact.

`self_check` is the field that tells you whether to trust any of it. It reports
whether the same extraction reproduces the value **already** in the repo at the
**currently pinned** version:

- `reproduced` — the mechanism demonstrably works for this dependency.
- `diverged` — upstream at the pinned version declares something *different*
  from what the repo pins. That is a finding in its own right, and worth
  raising even when no upgrade is due: either the pin was set deliberately (so
  there is a reason nobody wrote down) or the wrong variable is being read.
- `unavailable` — no cross-check was possible, so the value is unverified.

**`--ignore-companions` is not a way to make a refusal go away.** It bumps the
dependency alone, which is the exact failure this detection exists to prevent:
the build configures cleanly and dies at link time with a missing symbol, which
reads like a compiler problem. Offer it only when the user has established the
pins are genuinely independent, and say what it disables.

## The rubric

For each update, the central question is:

> **Does anything that changed upstream touch what we actually call?**

`consumed` and `sites` in the evidence tell you what the project uses. The
intersection with what changed upstream is the finding. An update with a huge
changelog that touches nothing we call is *low value and low risk*; an update
with a one-line changelog that renames a function we call in twelve places is
*high effort*.

### `divergence` outranks "something newer exists"

A finding with a non-empty `divergence` field says the repository declares the
same dependency at **two different versions** — typically one manifest per target
platform, disagreeing. Lead with these, ahead of any upgrade advice, for three
reasons:

- It needs no upstream lookup and no judgement about whether an upgrade is
  worthwhile. It is a fact about the repository, and it is certain.
- What ships depends on which manifest the build used, so every other claim
  about that dependency — the advisory match, the header diff — is really a
  claim about one platform. Say which.
- For a mature project already close to current on everything, this is the only
  finding of real size. A report that leads with "you are one minor version
  behind" and buries "your platforms ship different TLS versions" has the
  priorities backwards.

`declarations` lists every site with the version each asserts, and `aliases`
gives the other names the dependency is declared under (a CMake package name
beside the package-manager one). Note that **`pinned` and the version compared
against upstream are the *oldest* of the declared versions** — the one an
advisory is most likely to match. So on a divergent dependency, "behind by N" is
the gap for the worst platform, not for all of them.

`/deps:apply` **refuses** a divergent dependency rather than editing one site
and deepening the disagreement. The fix is to reconcile the manifests first;
that is the recommendation to make, and it is usually a one-line edit rather
than an upgrade.

**A lockfile disagreeing with the manifest is a different finding, and does not
block a bump.** The `divergence` text says so ("either the lock is stale or the
build is not using it"). Treat it as a fact about what is *actually built*: every
other claim about that dependency — the advisory match, the header diff — is
about the version the manifest asks for, while the locked one is what ships.
Recommend regenerating the lock, never editing it; `apply` will not touch a
generated file, and after a bump it reports which ones still record the old
version.

### Two notes that change what a finding means

- **`transitive`** — only a lockfile or an ingested scanner records this
  dependency; no manifest declares it. It has no call sites of yours by
  definition, so an empty `consumed` list here means "we do not call it
  directly", **not** "it is unused". Never recommend dropping one on that basis.
  The actionable advice is about whatever pulls it in.
- **`report-only`** — the dependency was found without a line number, so
  `/deps:apply` will refuse it and say so. Judge and report it exactly like any
  other; just make the recommended action a hand edit rather than an apply.

### Read `change_evidence` before the release notes

For a C/C++ dependency with a readable GitHub upstream, the tool has already
done that intersection *mechanically*, by diffing the public headers between the
pinned tag and the target. Prefer it over prose in every case, because it is
evidence rather than summary. It sits under `change_evidence` in the `check`
JSON, in descending order of trustworthiness:

| Field | What it is | How to treat it |
|---|---|---|
| `api_diff.affects_us` | Symbols **we consume** that were removed or re-signatured, each with our own `file:line` sites. | The load-bearing finding. Quote the symbol and the sites. |
| `api_diff.removed` / `.changed` | Everything that changed upstream, whether we use it or not. | Context. Do not report as risk to us. |
| `api_diff.likely_renames` | `confidence: inferred` — a removed and an added symbol share a signature and a similar name. | Offer as a probable migration path, always labelled as a guess. |
| `migration_docs` | Upstream's own `UPGRADING.md` / `UPGRADE-x.y.md` at the target. | Stronger than release notes: this is upstream saying what it broke. |
| `commits` | Commit subjects, fetched **only when the release notes were empty**. `breaking` holds the ones whose message announces a break. | Weakest. A subject is not a summary. |

Three fields decide how much the diff is worth, and you must read them before
saying anything reassuring:

- **`consumed_count` is the size of the input to the intersection.** Zero means
  the extractor recorded nothing we consume, so `affects_us` is empty for want of
  an input — it says nothing whatsoever about the upgrade. Report it as
  **unmeasured**, never as unaffected, and treat the release notes as the only
  evidence you have.
- **`affects_us` empty is only good news if coverage was complete.** Check
  `truncated`. When true, the read hit its header budget.
- **`not_located`** lists symbols we consume that appear in *no* header the diff
  read, at either version. These are **unchecked, not unaffected.** Never fold
  them into "nothing we call was touched". Say they could not be checked, and if
  the decision hinges on them, run `apidiff --dep N --max-headers 80`.

`consumed_added` lists symbols the diff added to `consumed` by matching the
dependency's *own* declarations against our sources, which is how a library whose
symbols do not carry its package name gets a usage profile at all. They are
evidence like any other, with one caveat worth stating if you lean on one: they
were matched against a declaration in a header, not observed being called.

Removals carry `confirmed`. `true` means the symbol is established absent from
the target's public headers. `false` means the budget cut the read short and the
symbol may simply live somewhere unread — report it as a suspected removal.

When `api_diff.resolved` is false, its `reason` says why (not a GitHub upstream,
tag not readable, no public headers). Fall back to the release notes and **say
that you are doing so** — the confidence in your conclusion is lower.

Rank each dependency into exactly one bucket:

- **ACT NOW** — an advisory affects the pinned version, or a fixed bug is one
  we are demonstrably exposed to. Say which CVE, and which of our call sites is
  reachable.
- **WORTH IT** — a real benefit that touches our surface: a bug fix in a
  function we call, a performance win on a path we use, a deprecation we should
  get ahead of. State the concrete benefit, not "stay current".
- **LOW VALUE** — nothing in the diff touches our surface. Safe to take, no
  reason to hurry. Grouping several of these into one PR is usually right.
- **HIGH EFFORT** — the update requires code changes. Name the symbols we would
  have to change and roughly how many sites.
- **SKIP / BLOCKED** — do not take it, and why: a hard incompatibility, an
  `EXACT` version constraint, a platform we do not build for, an abandoned
  upstream.

Weigh these factors, in roughly this order:

1. **Security.** An advisory outranks everything. But check whether the
   vulnerable code path is one we reach — say so either way.
2. **Scope.** `scope: test` cannot break production. A test-only dependency
   being six releases behind is a maintenance-comfort question, not a risk
   question. Say that plainly instead of inflating it.
3. **Surface intersection.** As above. This is the core of the judgement.
4. **Bump kind.** `major` means read the migration notes and expect work.
   `patch` on a dependency we barely touch is nearly free.
5. **Compounding staleness.** Being many releases behind raises the cost of the
   *next* upgrade too. Mention it when the gap is large, but do not use it as
   the sole justification.

**Pinned-ness changes the action, never the priority.** This is the rule most
easily got backwards, so state it explicitly to yourself before ranking:

A pin is not a standing decision to stay on that version. It is a snapshot of
one moment, and nothing will ever move it on its own. Unpinned system libraries
drift forward for free whenever the distro or CI image updates; **pinned
dependencies are the only ones that rot silently**, which makes them the
primary reason this tool exists. Never soften a recommendation because a
dependency is pinned, deliberately pinned, hash-pinned, or `EXACT`-pinned.
If the evidence says an upgrade is worth taking, say so with the same force
you would for anything else, and let the user decide.

What pinned-ness legitimately determines:

- **Where the fix goes** — a repo edit (`/deps:apply` can do it) versus a CI
  image or documented-minimum change (it cannot).
- **What else must move with it** — see `companion-pins` below.
- **Whether a pin has an attached reason.** A comment next to the pin
  explaining *why* is real evidence; treat it seriously and check whether it
  still holds. The absence of one is also evidence — in the other direction.
6. **Effort vs. benefit.** Be explicit when the benefit does not justify the
   work. Recommending "no" is a valid and useful answer — a report where
   everything is worth doing is a report nobody will act on.

### Special cases you will hit

- **Unpinned system dependencies** (`kind: pkg-config`, `cmake-find-package`).
  There is no version to bump *in the repo*, but there is still an effective
  version: whatever the build machine installs, reported as `installed_here`.
  When that is known, judge the gap exactly as you would a pin — it is a real
  version difference with real consequences. What changes is only the
  **action**: the fix is a CI image, container base, or documented minimum
  version, not a source edit. Only when `installed_here` is empty *and* there
  is no pin is the current version genuinely unknown (`unpinned: true` in the
  evidence) — say so plainly rather than inventing a baseline.

  A second, separate finding worth raising here: an unpinned dependency means
  different developers may be building against different versions. That is a
  reproducibility problem independent of whether an upgrade is due.
- **`behind_by_is_floor: true`** — the source lists only the versions still
  offered, not a history. Conan Center deletes old recipes, so "1 behind" can
  span many releases. Say "at least N" and never present the count as a release
  count. The human output writes it as `1+ behind`.
- **`pin_unavailable: true`** — the version the repo pins is **gone from the
  index it comes from**. Raise this on its own merits, ahead of upgrade advice:
  a fresh checkout cannot reproduce the build, and whoever runs `conan install`
  next gets either a failure or a different version. It is common for a project
  that has not been rebuilt from scratch in a while, and it is invisible until
  someone tries.
- **`cmake-system-or-fetch`** — declared as `find_package(... QUIET)` with a
  `FetchContent` fallback. Two versions are in play: what a system copy might
  provide, and what the pinned archive vendors. Flag when they can diverge.
- **A dependency declared to the build system under one name and pinned under
  another** (`aliases` is non-empty, and the notes say so). The package manager
  decides the version, so `installed_here` and any distro comparison are beside
  the point here — recommending a CI-image change for a library the project
  statically links from its own manifest is a wrong answer, not a cautious one.
  The actionable site is the manifest.
- **`EXACT` version constraints** — a newer system library will silently fail
  to satisfy `find_package(x 1.2.3 EXACT)`. Call this out; it is a common
  source of "works on my machine".
- **SHA-pinned `GIT_TAG`** — the tool notes when a pin is a commit SHA rather
  than a tag. Treat this as a *rot signal*, not a keep-out sign. A raw SHA
  usually means someone froze the dependency to dodge a specific problem and
  never revisited it; the reason is often years stale and undocumented. Look
  for what has landed upstream since that commit, and if you cannot find why
  it was frozen, say so — "pinned to a SHA with no recorded reason, N releases
  have landed since" is a finding, not a reason to stay quiet.
- **Release notes that are empty.** Some projects tag without notes. The tool
  already falls back to the commit log in that case (`change_evidence.commits`),
  and for a C/C++ dependency the header diff does not depend on notes existing at
  all. Say the notes are unavailable, then use what is there. Do not invent a
  summary.
- **A header diff that contradicts the release notes.** The diff wins. Notes are
  written by hand and go stale; a declaration removed at the target tag is a
  fact. Say both, and say which you are acting on. Notes claiming a removal the
  diff cannot find is the more suspicious direction — check `truncated` and
  `not_located` before concluding the notes are wrong.

## Output

Lead with the ranked list, most actionable first. For each entry:

```
<BUCKET>  <name>  <current> → <proposed>   (<scope>, <bump>)
  why:     one or two sentences grounded in the evidence
  touches: the specific symbols of ours that are affected, or "nothing we call"
  action:  the concrete next step
```

Then a one-paragraph summary. Then, if anything is in ACT NOW or WORTH IT,
offer `/deps:apply` for the specific dependency.

Keep it short. The user wants a decision, not a changelog reprint. Quote at
most a line or two of upstream release notes, and only when it is the
load-bearing evidence for your recommendation.

## Honesty rules

- If `upstream.resolved` is false, say the dependency could not be checked and
  why. Do not quietly drop it from the report.
- If the builtin backend found no usage for a runtime dependency, say the
  surface is unknown rather than concluding the dependency is unused — it may
  be a transitive or link-only dependency, or the extractor may have missed its
  include style.
- Distinguish what you verified from what you inferred. "The changelog says X"
  and "this probably means X for us" are different claims. The header diff is
  the one place the tool gives you a verified answer about breakage — say so
  when you are relying on it, and say when you are not.
- The header diff is a regex extractor, not a compiler. It reads declarations
  behind `#if` unconditionally and does not track constructors or out-of-line
  definitions. Its `notes` field lists what applied to that run. A finding from
  it is strong evidence, not proof of a compile failure.
- The backend in use is reported by `profile`. If it is `builtin`, the symbol
  list is a heuristic; a code-graph backend (graphify, codebase-memory) would
  give call depth and reachability. Mention this once, if the user seems to
  want deeper analysis — not in every report.
