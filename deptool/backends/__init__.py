"""Pluggable analysis backends.

The built-in extractor always works. If richer code-graph tools are present we
run them too, because they can answer things grep cannot — how deep a
dependency sits below main(), whether a call is reachable from a real-time
thread, which of our functions transitively depend on it.

Every available backend runs; they are complementary, not ranked alternatives.
Both parse C and C++ properly — graphify carries a C++ tree-sitter config with
`qualified_identifier` in its call accessors, so the long-standing claim here
that it "cannot see namespaced C++" was written from assumption and is false.
What codebase-memory adds is type resolution: it binds parameters and infers
return types, and it grades every edge it returns. Stopping at the first backend
that returned anything would let a partial result mask a dependency another
could have covered.

Detection is automatic and non-fatal: a missing backend is never an error.
"""

from __future__ import annotations

import bisect
import json
import os
import re
import shlex
import shutil
import subprocess
import sys

from ..model import Dep
from . import builtin

# Where graphify drops its artefacts, in preference order. `graphify update`
# writes graphify-out/graph.json by default.
GRAPHIFY_PATHS = [
    "graphify-out/graph.json",
    ".graphify/graph.json",
    "graph.json",
]


def detect(root: str) -> list[str]:
    """Which backends are usable here, best first."""
    available = []
    for rel in GRAPHIFY_PATHS:
        if os.path.isfile(os.path.join(root, rel)):
            available.append("graphify")
            break
    if _codebase_memory_exe():
        available.append("codebase-memory")
    available.append("builtin")
    return available


def describe(root: str) -> str:
    found = detect(root)
    rich = [b for b in found if b != "builtin"]
    if rich:
        return f"{' + '.join(rich)} (+ builtin fallback)"
    return "builtin (no code-graph backend detected)"


def analyse(root: str, deps: list[Dep], force: str = "") -> str:
    """Fill dep.consumed / dep.sites. Returns the backends that contributed.

    `force` restricts the run to a single named backend — the escape hatch for
    when one is wrong or slow. Without it every detected backend runs and
    their findings merge (see `_record`).
    """
    order = detect(root)
    if force:
        # Honour the name even if detection missed it — the enrichers each
        # re-check their own availability and no-op when absent. `builtin` is
        # not in _ENRICHERS, so forcing it is how you turn the rich pass off.
        order = [force]

    # The builtin pass always runs: it is what produces the file:line sites
    # that make the profile auditable. Graph backends then enrich.
    builtin.analyse(root, deps)
    contributed: list[str] = []

    for name in order:
        enrich = _ENRICHERS.get(name)
        if enrich and enrich(root, deps) and name not in contributed:
            contributed.append(name)
    return "+".join(contributed + ["builtin"])


def _record(dep: Dep, backend: str, radius: int, note: str,
            depth: int | None = None) -> None:
    """Merge one backend's finding into a dep without discarding another's.

    Backends see overlapping slices of the same graph, and by an unknown
    amount — graphify counts nodes, codebase-memory counts names, and nothing
    reliably maps between them. Summing would double-count, so the largest
    single-backend count is kept. That reads as a lower bound: *at least* this
    much of our code reaches the dependency. Every backend still appends its
    own note, so the evidence stays separable.
    """
    prior = [b for b in dep.backend.split("+") if b and b != "builtin"]
    if backend not in prior:
        prior.append(backend)
    dep.backend = "+".join(prior + ["builtin"])
    if dep.blast_radius is None or radius > dep.blast_radius:
        dep.blast_radius = radius
    if depth is not None and (dep.call_depth is None or depth < dep.call_depth):
        dep.call_depth = depth
    dep.notes.append(note)


def _graphify_path(root: str) -> str | None:
    for p in GRAPHIFY_PATHS:
        full = os.path.join(root, p)
        if os.path.isfile(full):
            return full
    return None


def _line_of(node: dict) -> int | None:
    """Start line from graphify's `source_location` ("L40"), or None."""
    m = re.search(r"\d+", str(node.get("source_location") or ""))
    return int(m.group()) if m else None


def _defs_by_file(nodes: list[dict]) -> dict[str, list[tuple[int, str]]]:
    """path -> sorted [(start_line, node_id)] for nodes that can enclose a site.

    Only callable nodes qualify. A file or class node would also "contain" the
    line, but `blast_radius` counts *functions*, so admitting them would inflate
    it with things that cannot call anything.
    """
    out: dict[str, list[tuple[int, str]]] = {}
    for n in nodes:
        if not n.get("_callable") or not n.get("id"):
            continue
        path = str(n.get("source_file") or "").strip()
        line = _line_of(n)
        if not path or line is None:
            continue
        out.setdefault(os.path.normpath(path), []).append((line, n["id"]))
    for entries in out.values():
        entries.sort()
    return out


def _enclosing(defs: dict[str, list[tuple[int, str]]], path: str, line: int) -> str | None:
    """The innermost callable whose declaration precedes `line` in `path`."""
    entries = defs.get(os.path.normpath(path))
    if not entries:
        return None
    i = bisect.bisect_right(entries, (line, "￿")) - 1
    return entries[i][1] if i >= 0 else None


def _enrich_graphify(root: str, deps: list[Dep]) -> bool:
    """Measure blast radius from a graphify graph.json.

    **Seeded from call sites, not from symbol names.** Graphify indexes only the
    repository's own code, so a dependency's functions are almost never nodes:
    measured against a 1234-file C++ project, 5 of 74 consumed symbols matched a
    node, and every match was wrong — three were dangling stubs and `compress`
    resolved to *our own* `ZStream::compress`, which merely shares zlib's name.
    Matching labels against a dependency's symbols is guessing (standing rule 3),
    so the seed is instead the function *containing* a site the extractor already
    located, which is a fact we read rather than infer.

    The one label match that is trustworthy is a node with an **empty
    `source_file`**: graphify knows of the symbol but has no definition for it,
    which is exactly what an external symbol looks like. A label match on a node
    that does have a source file is our own code and is rejected.

    `blast_radius` counts only nodes that have a source file — our functions —
    so an external stub is never counted as part of our own blast radius.
    """
    path = _graphify_path(root)
    if not path:
        return False
    try:
        graph = json.load(open(path, encoding="utf-8"))
    except (OSError, ValueError):
        return False

    nodes = [n for n in (graph.get("nodes") or []) if isinstance(n, dict)]
    # Graphify uses `links`; some exporters use `edges`.
    raw_edges = graph.get("links")
    if not isinstance(raw_edges, list):
        raw_edges = graph.get("edges") or []
    edges = [e for e in raw_edges if isinstance(e, dict)]
    # Edges are no longer required: a located site makes its enclosing function
    # a reacher on its own, so a leaf that nothing calls still has a radius of 1.
    if not nodes:
        return False

    by_id = {n.get("id"): n for n in nodes if n.get("id")}
    # Nodes graphify has a definition for — our own code. `blast_radius` counts
    # these and nothing else.
    ours = {
        nid for nid, n in by_id.items()
        if str(n.get("source_file") or "").strip()
    }
    # label -> ids, restricted to nodes with *no* source file. Those are symbols
    # graphify saw referenced but never defined, which is what a dependency's
    # own symbols look like. Anything with a source file is ours.
    external: dict[str, list[str]] = {}
    for n in nodes:
        if n.get("id") in ours:
            continue
        label = str(n.get("label") or "").strip()
        if not label:
            continue
        external.setdefault(label, []).append(n["id"])
        bare = label.strip(".()")
        if bare and bare != label:
            external.setdefault(bare, []).append(n["id"])
    defs = _defs_by_file(nodes)

    # Two reverse adjacencies: genuine call edges, and the same plus the much
    # looser `references` relation used only as a fallback.
    callers: dict[str, set[str]] = {}
    loose: dict[str, set[str]] = {}
    for e in edges:
        relation = e.get("relation")
        if relation not in ("calls", "method", "references"):
            continue
        src, tgt = e.get("source"), e.get("target")
        if not (src and tgt):
            continue
        loose.setdefault(tgt, set()).add(src)
        if relation != "references":
            callers.setdefault(tgt, set()).add(src)

    touched = False
    for dep in deps:
        # Primary seed: the function containing each site we already located.
        site_seeds: set[str] = set()
        located = unlocated = 0
        calls_directly = False
        for site in dep.sites:
            if site.context == "include":
                continue  # an #include is a file fact, not a function's use
            nid = _enclosing(defs, site.path, site.line)
            if nid:
                site_seeds.add(nid)
                located += 1
                calls_directly = calls_directly or site.context == "call"
            else:
                unlocated += 1

        # Secondary seed: the dependency's own symbols, when graphify emitted
        # them as definition-less stubs.
        ext_seeds: set[str] = set()
        for sym in dep.consumed:
            bare = sym.split("::")[-1].split(".")[-1]
            for cand in (sym, bare):
                ext_seeds.update(external.get(cand, []))

        seeds = site_seeds | ext_seeds
        if not seeds:
            continue

        direct, reached = _reverse_bfs(seeds, callers)
        via_references = False
        if not direct and not site_seeds:
            # An external stub nothing "calls" — macro or header-only use.
            direct, reached = _reverse_bfs(seeds, loose)
            via_references = bool(direct)

        # Only our own functions count, and a seed we located is itself one of
        # them: it uses the dependency directly.
        our_reachers = (seeds | reached) & ours
        if not our_reachers:
            continue

        names = sorted(
            str(by_id[n].get("label", "")).strip(".()")
            for n in sorted(site_seeds or direct)[:6] if n in by_id
        )
        example = ", ".join(n for n in names if n)
        if located:
            note = (
                f"graphify: {len(our_reachers)} function(s) reach it, seeded from "
                f"{located} of {located + unlocated} located site(s)"
            )
            # The extractor already knows whether the site was a call; inferring
            # it from edges would be a weaker claim about the same fact.
            depth = 1 if calls_directly else None
        elif via_references:
            # No call edge was ever traversed, so claiming a call depth of 1
            # would be a fabrication — leave it unset.
            note = (
                f"graphify: no call edges (macro or header-only use); "
                f"{len(direct)} direct reference(s), {len(our_reachers)} "
                f"function(s) transitively reach it"
            )
            depth = None
        else:
            note = (
                f"graphify: {len(direct)} direct caller(s), {len(our_reachers)} "
                f"function(s) transitively reach it"
            )
            depth = 1
        if example:
            note += f" — e.g. {example}"
        if unlocated:
            # Standing rule 4: a partial read has to say so.
            note += (
                f"; {unlocated} site(s) not located in the graph, so this is a "
                f"lower bound"
            )
        _record(dep, "graphify", len(our_reachers), note, depth)
        touched = True
    return touched


def _reverse_bfs(seeds: set[str], callers: dict[str, set[str]]) -> tuple[set, set]:
    """Everything that transitively reaches `seeds`. Returns (direct, reached)."""
    direct: set[str] = set()
    for s in seeds:
        direct |= callers.get(s, set())

    reached: set[str] = set()
    frontier, hops = set(direct), 1
    while frontier and hops < 64:
        reached |= frontier
        nxt: set[str] = set()
        for nid in frontier:
            nxt |= callers.get(nid, set()) - reached
        frontier = nxt
        hops += 1
    return direct, reached


# Whether the codebase-memory enricher may contribute. Its invocation, its
# seeding and its response parsing were all wrong and were fixed together; see
# `_enrich_codebase_memory`.
CODEBASE_MEMORY_ENABLED = True

# Rows per `search_graph` page. The node table is pulled in as few calls as
# possible because the cost here is per *invocation*, not per row: each one pays
# a daemon start-up, measured at ~3.7s cold and ~1.25s warm, while a single call
# returned all 3712 methods of a 1234-file C++ project in 1.3s. That is why this
# backend reads the graph in bulk and walks it in memory rather than asking
# `trace_path` one question per call site.
CBMEM_PAGE = 5000
CBMEM_INDEX_TIMEOUT = 300
CBMEM_QUERY_TIMEOUT = 180

# Node labels that can enclose a call site. Same rule as `_defs_by_file`:
# `blast_radius` counts functions, so a Class or File node would "contain" the
# line without being able to call anything.
CBMEM_CALLABLE = ("Function", "Method")


def _codebase_memory_exe() -> str | None:
    for name in ("codebase-memory-mcp", "codebase-memory", "cbmem"):
        found = shutil.which(name)
        if found:
            return found
    return None


def _cbmem_run(exe: str, root: str, tool: str, args: list[str],
               timeout: int) -> str | None:
    """One codebase-memory CLI call. Returns stdout, or None if it failed.

    The return code is checked. It was previously discarded, which is what let a
    rejected flag pass for a successful index.

    Progress and allocator logging go to stderr, so stdout is the payload alone.
    """
    try:
        out = subprocess.run(
            [exe, "cli", tool, *args],
            cwd=root, capture_output=True, text=True, timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return out.stdout


def _cbmem_index(exe: str, root: str) -> str | None:
    """Index the tree; return the project name it was stored under, or None.

    The name is read back out of the response rather than assumed. `--name` is
    documented to encode non-ASCII bytes and normalise unsafe path characters,
    so the name we ask for is not always the name `--project` will need.
    """
    asked = os.path.basename(os.path.abspath(root))
    raw = _cbmem_run(exe, root, "index_repository",
                     ["--repo-path", root, "--name", asked], CBMEM_INDEX_TIMEOUT)
    if raw is None:
        # Standing rule 4: a failed index must not read as an absent backend.
        # Silently swallowing this is precisely why the enricher appeared to
        # work while contributing nothing.
        print(
            "codebase-memory: indexing failed, so blast radius is unmeasured "
            f"(tried: {exe} cli index_repository --repo-path {root})",
            file=sys.stderr,
        )
        return None
    try:
        return str(json.loads(raw).get("project") or asked)
    except ValueError:
        return asked


def _cbmem_rows(table: dict) -> list[tuple[str, str, dict]]:
    """(qualified_name, file, column->value) per row of a tree-format response.

    `search_graph` and `trace_path` share one envelope::

        {"cols": [...],
         "groups": [{"qn_prefix": ..., "file": ..., "rows": [[...], ...]}]}

    Row values are positional; their names live in `cols`, and the qualified name
    is `qn_prefix` joined to the row's `name`. A row only means anything read
    against that header.

    Reading it by column is the whole point. The previous version walked the
    response blindly for any `name`/`function_name`/`symbol`/`label` key and
    harvested the table's own column headers as though they were symbols — it
    reported `zlib: 3 function(s) reach it — Function, Method, compress`, of
    which two are the words at the top of a column and the third is our own
    `ZStream::compress`.
    """
    cols = [str(c) for c in (table.get("cols") or [])]
    if "name" not in cols:
        return []
    out: list[tuple[str, str, dict]] = []
    for group in table.get("groups") or []:
        if not isinstance(group, dict):
            continue
        prefix = str(group.get("qn_prefix") or "")
        path = str(group.get("file") or "")
        for row in group.get("rows") or []:
            if not isinstance(row, list) or len(row) != len(cols):
                continue  # not the declared shape; refuse to guess at it
            values = dict(zip(cols, row))
            name = str(values.get("name") or "")
            if not name:
                continue
            out.append((f"{prefix}.{name}" if prefix else name, path, values))
    return out


def _cbmem_defs(exe: str, root: str, project: str) -> dict[str, list[tuple[int, int, str]]]:
    """path -> [(start, end, qualified_name)] for every callable in the graph.

    Unlike graphify, codebase-memory reports an explicit end line (`lines` is
    `"72-147"`), so enclosure is a real containment test rather than "the nearest
    declaration above". A site in a gap between two functions is therefore
    correctly reported as unlocated instead of being attributed to the one above
    it.
    """
    defs: dict[str, list[tuple[int, int, str]]] = {}
    for label in CBMEM_CALLABLE:
        offset = 0
        while True:
            raw = _cbmem_run(
                exe, root, "search_graph",
                ["--project", project, "--label", label, "--format", "json",
                 "--limit", str(CBMEM_PAGE), "--offset", str(offset)],
                CBMEM_QUERY_TIMEOUT,
            )
            if raw is None:
                break
            try:
                page = json.loads(raw)
            except ValueError:
                break
            for qn, path, values in _cbmem_rows(page):
                span = str(values.get("lines") or "")
                m = re.fullmatch(r"(\d+)-(\d+)", span)
                if not (m and path):
                    continue
                defs.setdefault(os.path.normpath(path), []).append(
                    (int(m.group(1)), int(m.group(2)), qn)
                )
            if not page.get("has_more"):
                break
            offset += CBMEM_PAGE
    for entries in defs.values():
        entries.sort()
    return defs


def _cbmem_calls(exe: str, root: str, project: str) -> tuple[dict[str, set[str]], dict[tuple[str, str], str]]:
    """Reverse call adjacency, plus how each edge was resolved.

    One `query_graph` call returns the whole CALLS relation — 10046 edges in 2.9s
    on a 1234-file C++ project — which is cheaper than one `trace_path` per seed
    and gives the same answer, since both walk the same edges.

    `query_graph` has no JSON mode: it answers with a header naming its columns,
    then one indented row per result, quoting any field containing a space (C++
    yields plenty, e.g. `operator std::string`). Rows are parsed with `shlex` and
    checked against the header's column count; a row that does not match the
    declared shape is dropped rather than guessed at.
    """
    raw = _cbmem_run(
        exe, root, "query_graph",
        ["--project", project, "--query",
         "MATCH (a)-[r:CALLS]->(b) RETURN a.qualified_name AS src, "
         "b.qualified_name AS dst, r.strategy AS strat"],
        CBMEM_QUERY_TIMEOUT,
    )
    callers: dict[str, set[str]] = {}
    how: dict[tuple[str, str], str] = {}
    if raw is None:
        return callers, how

    header = re.search(r"^rows:\s*\d+\s*\(cols:\s*([^)]*)\)", raw, re.M)
    if not header:
        return callers, how
    cols = header.group(1).split()
    for line in raw.splitlines():
        if not line.startswith("  ") or line.startswith("total:"):
            continue
        try:
            fields = shlex.split(line)
        except ValueError:
            continue
        if len(fields) != len(cols):
            continue
        row = dict(zip(cols, fields))
        src, dst = row.get("src"), row.get("dst")
        if not (src and dst):
            continue
        callers.setdefault(dst, set()).add(src)
        how[(src, dst)] = row.get("strat") or ""
    return callers, how


def _enclosing_cbmem(defs: dict[str, list[tuple[int, int, str]]],
                     path: str, line: int) -> str | None:
    """The innermost callable whose line range contains `line` in `path`."""
    best: tuple[int, str] | None = None
    for start, end, qn in defs.get(os.path.normpath(path), []):
        if start <= line <= end and (best is None or start >= best[0]):
            best = (start, qn)
    return best[1] if best else None


def _enrich_codebase_memory(root: str, deps: list[Dep]) -> bool:
    """Measure blast radius from a codebase-memory index.

    **Seeded from call sites, not from symbol names** — the same correction
    `_enrich_graphify` carries, for the same reason. codebase-memory indexes only
    the repository's own code, so a dependency's own functions are never nodes:
    `curl_easy_setopt`, `deflate`, `BIO_free`, `archive_entry_new` and
    `iconv_open` each returned `function not found` against a real 1234-file
    project, and `search_graph` for their patterns returned zero rows, against a
    control query that returned rows normally. Seeding by symbol name therefore
    cannot match the dependency's symbol — only one of our own homonyms.

    The seed is instead the function *containing* a site the builtin extractor
    already located, which is a fact we read rather than infer. Verified against
    the case in HISTORY: `archive/z_stream.cpp:109` resolves to
    `ZStream::compress` (lines 72-147) and its inbound trace returns exactly one
    caller, `postprocess`.

    Every node here is our own code by construction, so unlike the graphify path
    there is no external-stub category to exclude.
    """
    if not CODEBASE_MEMORY_ENABLED:
        return False

    exe = _codebase_memory_exe()
    if not exe:
        return False

    project = _cbmem_index(exe, root)
    if not project:
        return False

    defs = _cbmem_defs(exe, root, project)
    if not defs:
        return False
    callers, how = _cbmem_calls(exe, root, project)

    touched = False
    for dep in deps:
        seeds: set[str] = set()
        located = unlocated = 0
        calls_directly = False
        for site in dep.sites:
            if site.context == "include":
                continue  # an #include is a file fact, not a function's use
            qn = _enclosing_cbmem(defs, site.path, site.line)
            if qn:
                seeds.add(qn)
                located += 1
                calls_directly = calls_directly or site.context == "call"
            else:
                unlocated += 1
        if not seeds:
            if unlocated:
                # Standing rule 4. Saying nothing here is what makes an
                # unmeasured dependency read like an unaffected one: openssl has
                # 14 sites on the development case and located none of them,
                # because its members are declared inside a class the index
                # recorded without method nodes. `blast_radius` stays unset —
                # criterion 6 — but the reason is now on the page.
                dep.notes.append(
                    f"codebase-memory: none of {unlocated} site(s) fall inside a "
                    f"function the index knows, so blast radius is unmeasured "
                    f"here, not zero"
                )
            continue

        direct, reached = _reverse_bfs(seeds, callers)
        our_reachers = seeds | reached

        names = sorted(qn.split(".")[-1] for qn in sorted(seeds)[:6])
        note = (
            f"codebase-memory: {len(our_reachers)} function(s) reach it, seeded "
            f"from {located} of {located + unlocated} located site(s)"
        )
        example = ", ".join(n for n in names if n)
        if example:
            note += f" — e.g. {example}"
        if unlocated:
            # Standing rule 4: a partial read has to say so.
            note += (
                f"; {unlocated} site(s) not located in the graph, so this is a "
                f"lower bound"
            )
        # codebase-memory grades its own edges, which is the thing it offers that
        # graphify does not. An edge resolved by matching a name is a weaker
        # claim than one resolved from a type, so say when the answer rests on
        # them rather than presenting one number for both.
        # Counted over edges, not callers: one caller reaching two seeds is two
        # edges, so measuring the numerator in pairs and the denominator in
        # callers produced "19 of 17".
        graded = [(s, t) for s in direct for t in seeds if (s, t) in how]
        guessed = sum(1 for e in graded if not how[e].startswith("lsp"))
        if guessed:
            note += (
                f"; {guessed} of {len(graded)} direct edge(s) resolved by name "
                f"rather than by type"
            )
        _record(dep, "codebase-memory", len(our_reachers), note,
                1 if calls_directly else None)
        touched = True
    return touched


# Declared after the enrichers so the names resolve; `analyse` walks this in
# whatever order `detect` returned.
_ENRICHERS = {
    "graphify": _enrich_graphify,
    "codebase-memory": _enrich_codebase_memory,
}

# What `--backend` accepts. Since that flag now *restricts* the run rather than
# merely reordering it, a typo would silently disable every rich backend.
NAMES = sorted(_ENRICHERS) + ["builtin"]
