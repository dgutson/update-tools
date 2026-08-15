"""Pluggable analysis backends.

The built-in extractor always works. If richer code-graph tools are present we
run them too, because they can answer things grep cannot — how deep a
dependency sits below main(), whether a call is reachable from a real-time
thread, which of our functions transitively depend on it.

Every available backend runs; they are complementary, not ranked alternatives.
Graphify resolves the C ABI but cannot see namespaced C++; codebase-memory
claims LSP-grade type resolution. Stopping at the first backend that returned
anything would let a partial result mask a dependency another could have
covered.

Detection is automatic and non-fatal: a missing backend is never an error.
"""

from __future__ import annotations

import bisect
import json
import os
import re
import shutil
import subprocess

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


# Whether the codebase-memory enricher may contribute. Off until its seeding and
# its invocation are fixed together — see `_enrich_codebase_memory`. Detection is
# unaffected: `detect` still reports the backend as present.
CODEBASE_MEMORY_ENABLED = False


def _codebase_memory_exe() -> str | None:
    for name in ("codebase-memory-mcp", "codebase-memory", "cbmem"):
        found = shutil.which(name)
        if found:
            return found
    return None


def _enrich_codebase_memory(root: str, deps: list[Dep]) -> bool:
    """Count inbound callers of each consumed symbol via codebase-memory-mcp.

    Its CLI has no `callers` verb; the documented shape is
    `cli trace_path --project P --function-name F --direction inbound --json`,
    against a project that has been indexed first.

    HELD OFF pending the invocation fix. Run against a real install, this path
    seeds the same way `_enrich_graphify` used to — by looking a dependency's
    symbol names up in the graph — and codebase-memory indexes *only* the
    repository's own code. Every external symbol queried against a real 1234-file
    project (`curl_easy_setopt`, `deflate`, `BIO_free`, `archive_entry_new`,
    `iconv_open`) returned `function not found`, and `search_graph` for their
    patterns returned zero rows, so there is no case in which a symbol-name match
    here is the dependency's symbol rather than one of our own homonyms. It
    reported `zlib: 3 function(s) reach it — Function, Method, compress`: our own
    `ZStream::compress`, plus two column headers `_collect_names` scraped out of
    the tool's table output. Because `_record` keeps the largest count, that
    overrode graphify's correct answer.

    Re-enable it with a site-based seed once the invocation is fixed — the flags
    below are also wrong (`--repo-path`/`--name`, not `--path`/`--project`) and
    the return code is discarded, so a failed index is silent.
    """
    if not CODEBASE_MEMORY_ENABLED:
        return False

    exe = _codebase_memory_exe()
    if not exe:
        return False

    project = os.path.basename(os.path.abspath(root))
    try:
        subprocess.run(
            [exe, "cli", "index_repository", "--path", root, "--project", project],
            cwd=root, capture_output=True, text=True, timeout=300,
        )
    except (OSError, subprocess.SubprocessError):
        return False

    touched = False
    for dep in deps:
        callers: set[str] = set()
        for sym in dep.consumed[:8]:
            bare = sym.split("::")[-1].split(".")[-1]
            try:
                out = subprocess.run(
                    [exe, "cli", "trace_path", "--project", project,
                     "--function-name", bare, "--direction", "inbound", "--json"],
                    cwd=root, capture_output=True, text=True, timeout=30,
                )
            except (OSError, subprocess.SubprocessError):
                return touched
            if out.returncode != 0 or not out.stdout.strip():
                continue
            try:
                data = json.loads(out.stdout)
            except ValueError:
                continue
            callers |= _collect_names(data)
        if callers:
            _record(
                dep, "codebase-memory", len(callers),
                f"codebase-memory: {len(callers)} function(s) reach it — "
                + ", ".join(sorted(callers)[:6]),
            )
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


def _collect_names(data) -> set[str]:
    """Pull symbol names out of a trace_path result of unknown exact shape."""
    names: set[str] = set()
    stack = [data]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            for key in ("name", "function_name", "symbol", "label"):
                val = cur.get(key)
                if isinstance(val, str) and val:
                    names.add(val)
            stack.extend(cur.values())
        elif isinstance(cur, list):
            stack.extend(cur)
    return names
