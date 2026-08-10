"""Pluggable analysis backends.

The built-in extractor always works. If a richer code-graph tool is present we
use it instead, because it can answer things grep cannot — how deep a
dependency sits below main(), whether a call is reachable from a real-time
thread, which of our functions transitively depend on it.

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
        return f"{rich[0]} (+ builtin fallback)"
    return "builtin (no code-graph backend detected)"


def analyse(root: str, deps: list[Dep], prefer: str = "") -> str:
    """Fill dep.consumed / dep.sites. Returns the backend actually used."""
    order = detect(root)
    if prefer:
        order = [prefer] + [b for b in order if b != prefer]

    # The builtin pass always runs: it is what produces the file:line sites
    # that make the profile auditable. Graph backends then enrich.
    builtin.analyse(root, deps)
    used = "builtin"

    for name in order:
        if name == "graphify":
            if _enrich_graphify(root, deps):
                used = "graphify+builtin"
                break
        elif name == "codebase-memory":
            if _enrich_codebase_memory(root, deps):
                used = "codebase-memory+builtin"
                break
    return used


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

    # Reverse adjacency over genuine call edges only.
    callers: dict[str, set[str]] = {}
    for e in edges:
        if e.get("relation") not in ("calls", "method"):
            continue
        src, tgt = e.get("source"), e.get("target")
        if src and tgt:
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

        direct = set()
        for s in seeds:
            direct |= callers.get(s, set())

        # Reverse BFS: everything of ours that transitively reaches the dep.
        reached: set[str] = set()
        frontier, hops = set(direct), 1
        depth_of_first = 1 if direct else None
        while frontier and hops < 64:
            reached |= frontier
            nxt: set[str] = set()
            for nid in frontier:
                nxt |= callers.get(nid, set()) - reached
            frontier = nxt
            hops += 1

        our_reachers = {n for n in reached if n in by_id and n not in seeds}
        if not our_reachers and not direct:
            continue

        dep.blast_radius = len(our_reachers)
        dep.call_depth = depth_of_first
        dep.backend = "graphify+builtin"
        names = sorted(
            str(by_id[n].get("label", "")).strip(".()") for n in list(direct)[:6] if n in by_id
        )
        if names:
            dep.notes.append(
                f"graphify: {len(direct)} direct caller(s), {len(our_reachers)} "
                f"function(s) transitively reach it — e.g. {', '.join(n for n in names if n)}"
            )
        touched = True
    return touched


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
            dep.blast_radius = len(callers)
            dep.backend = "codebase-memory+builtin"
            dep.notes.append(
                f"codebase-memory: {len(callers)} function(s) reach it — "
                + ", ".join(sorted(callers)[:6])
            )
            touched = True
    return touched


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
