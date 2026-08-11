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

import json
import os
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


def _enrich_graphify(root: str, deps: list[Dep]) -> bool:
    """Measure blast radius from a graphify graph.json.

    Graphify emits the dependency's own symbols as nodes (`fluid_synth_noteon`
    is a node, not just a string in our source), and its call edges live under
    `links` with `relation: "calls"`. There is no synthetic `main` node, so
    depth-from-entry-point is not available. What *is* available, and is more
    useful for judging an upgrade, is the reverse question: how much of our
    code transitively reaches this dependency.

    Symbols consumed through a macro (`HEGEL_TEST(...)`) are nodes with no
    `calls` edges at all, so a calls-only walk matches the seed and then
    reports a blast radius of zero — the worst possible answer, since it reads
    as "nothing uses this". Those deps fall back to `references` edges, which
    is a weaker claim and is labelled as such in the note.
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
    if not nodes or not edges:
        return False

    by_id = {n.get("id"): n for n in nodes if n.get("id")}
    # label -> ids. Labels for our own methods look like ".foo()".
    by_label: dict[str, list[str]] = {}
    for n in nodes:
        label = str(n.get("label") or "").strip()
        if not label:
            continue
        by_label.setdefault(label, []).append(n["id"])
        bare = label.strip(".()")
        if bare and bare != label:
            by_label.setdefault(bare, []).append(n["id"])

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
        seeds: set[str] = set()
        for sym in dep.consumed:
            bare = sym.split("::")[-1].split(".")[-1]
            for cand in (sym, bare):
                for nid in by_label.get(cand, []):
                    seeds.add(nid)
        if not seeds:
            continue

        direct, reached = _reverse_bfs(seeds, callers)
        via_references = False
        if not direct:
            # Macro or header-only use: the seed exists but nothing "calls" it.
            direct, reached = _reverse_bfs(seeds, loose)
            via_references = bool(direct)

        our_reachers = {n for n in reached if n in by_id and n not in seeds}
        if not our_reachers and not direct:
            continue

        names = sorted(
            str(by_id[n].get("label", "")).strip(".()") for n in list(direct)[:6] if n in by_id
        )
        example = ", ".join(n for n in names if n)
        if via_references:
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

    NOTE: written against published documentation and exercised only through
    the unit tests' fakes — this path has not been run against a real install.
    """
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
