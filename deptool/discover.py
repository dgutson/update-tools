"""Find every declared dependency in a repository, across ecosystems.

Everything here is deterministic. No LLM, no network.

Two things are worth knowing before changing this file. Manifests are searched
for by *walking*, not by testing the repository root — a project that keeps a
manifest per target platform one directory down was previously invisible, and
the failure mode was not a degraded answer but no answer at all. And the same
library is frequently declared more than once, under more than one name, so
declarations are *reconciled* rather than concatenated: see `reconcile`.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tomllib

from . import cmake
from .model import Declaration, Dep, Upstream, most_restrictive, parse_version

MANIFESTS = {
    "CMakeLists.txt": "cmake",
    "package.json": "npm",
    "Cargo.toml": "cargo",
    "pyproject.toml": "pypi",
    "requirements.txt": "pypi",
    "go.mod": "gomod",
    "conanfile.txt": "conan",
    "conanfile.py": "conan",
    "vcpkg.json": "vcpkg",
}

# Directories the manifest walk must not descend into. A manifest inside build
# output or a vendored copy of another project declares *its* dependencies, not
# ours. Kept beside the walk rather than shared with the source walk in
# backends.builtin, because the two answer different questions and will drift.
SKIP_DIRS = {
    ".git", "build", "_build", "out", "dist", "node_modules", "target",
    "__pycache__", ".venv", "venv", "third_party", "vendor", "_deps",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox", ".eggs",
    "cmake-build-debug", "cmake-build-release", "site-packages",
}


_SUBMODULE_PATH = re.compile(r"^\s*path\s*=\s*(.+?)\s*$", re.M)


def submodule_paths(root: str) -> set[str]:
    """Directories `.gitmodules` declares as other projects' checkouts.

    Walking for manifests widens what the tool can see, and one of the things it
    can now see is a vendored copy of somebody else's project — whose manifest
    declares *its* dependencies, not ours. `SKIP_DIRS` only knows the
    conventional container names (`third_party`, `vendor`), so a copy checked out
    under its own name (`tests/googletest`) slips through.

    This reads a declared fact rather than guessing at directory names, which is
    the difference between a mechanism that generalises and a table that has to
    be maintained. It does nothing when the repository has no submodules — the
    vendored-copy-without-a-submodule case is finding F in ROADMAP.md and is
    still open.
    """
    try:
        text = open(os.path.join(root, ".gitmodules"), encoding="utf-8", errors="replace").read()
    except OSError:
        return set()
    return {
        os.path.normpath(m.group(1))
        for m in _SUBMODULE_PATH.finditer(text)
        if m.group(1).strip()
    }


def detect_manifests(root: str) -> list[tuple[str, str]]:
    """Every manifest in the tree, as (path relative to root, ecosystem).

    Walks rather than testing `root` alone. A monorepo with per-platform Conan
    manifests in subdirectories declares real pins that the root does not
    mention, and reporting the version-less `find_package` calls instead is
    worse than reporting nothing: it reads as an answer.
    """
    skip_rel = submodule_paths(root)
    found: list[tuple[str, str]] = []
    for dirpath, dirnames, filenames in os.walk(root):
        here = os.path.relpath(dirpath, root)
        dirnames[:] = sorted(
            d
            for d in dirnames
            if d not in SKIP_DIRS
            and not d.startswith(".")
            and os.path.normpath(os.path.join(here, d)) not in skip_rel
        )
        for fname in sorted(filenames):
            eco = MANIFESTS.get(fname)
            if eco:
                rel = os.path.relpath(os.path.join(dirpath, fname), root)
                found.append((os.path.normpath(rel), eco))
    # Shallowest first, so the root manifest is the natural primary.
    found.sort(key=lambda p: (p[0].count(os.sep), p[0]))
    return found


# ------------------------------------------------------------------ ecosystems


def _npm(root: str, rel: str) -> list[Dep]:
    try:
        data = json.load(open(os.path.join(root, rel), encoding="utf-8"))
    except (OSError, ValueError):
        return []
    deps = []
    for field, scope in (
        ("dependencies", "runtime"),
        ("devDependencies", "test"),
        ("peerDependencies", "runtime"),
        ("optionalDependencies", "runtime"),
    ):
        for name, spec in (data.get(field) or {}).items():
            deps.append(
                Dep(
                    name=name,
                    kind="npm",
                    version=str(spec).lstrip("^~>=< "),
                    raw_pin=str(spec),
                    declared_in=f"{rel} ({field})",
                    scope=scope,
                    upstream=Upstream(kind="npm", ref=name),
                )
            )
    return deps


def _cargo(root: str, rel: str) -> list[Dep]:
    try:
        data = tomllib.load(open(os.path.join(root, rel), "rb"))
    except (OSError, ValueError):
        return []
    deps = []
    for field, scope in (
        ("dependencies", "runtime"),
        ("dev-dependencies", "test"),
        ("build-dependencies", "build"),
    ):
        for name, spec in (data.get(field) or {}).items():
            version = spec if isinstance(spec, str) else (spec or {}).get("version", "")
            deps.append(
                Dep(
                    name=name,
                    kind="cargo",
                    version=str(version).lstrip("^~>=< "),
                    raw_pin=str(version),
                    declared_in=f"{rel} ([{field}])",
                    scope=scope,
                    upstream=Upstream(kind="crates", ref=name),
                )
            )
    return deps


_PY_REQ = re.compile(r"^\s*([A-Za-z0-9._\-]+)\s*(?:\[[^\]]+\])?\s*([=<>!~]=?[^;#]*)?")


def _pyproject(root: str, rel: str) -> list[Dep]:
    try:
        data = tomllib.load(open(os.path.join(root, rel), "rb"))
    except (OSError, ValueError):
        return []
    deps: list[Dep] = []
    entries = [(s, "runtime", "project.dependencies")
               for s in (data.get("project", {}).get("dependencies") or [])]
    for group, specs in (data.get("project", {}).get("optional-dependencies") or {}).items():
        scope = "test" if re.search(r"test|dev|lint", group, re.I) else "runtime"
        entries += [(s, scope, f"project.optional-dependencies.{group}") for s in specs]
    for spec, scope, where in entries:
        m = _PY_REQ.match(spec)
        if not m:
            continue
        deps.append(
            Dep(
                name=m.group(1),
                kind="pypi",
                version=(m.group(2) or "").strip().lstrip("=<>!~ "),
                raw_pin=spec.strip(),
                declared_in=f"{rel} ({where})",
                scope=scope,
                upstream=Upstream(kind="pypi", ref=m.group(1)),
            )
        )
    return deps


def _requirements(root: str, rel: str) -> list[Dep]:
    deps: list[Dep] = []
    try:
        fh = open(os.path.join(root, rel), encoding="utf-8", errors="replace")
    except OSError:
        return []
    with fh:
        for i, line in enumerate(fh, 1):
            if not line.strip() or line.lstrip().startswith(("#", "-")):
                continue
            m = _PY_REQ.match(line)
            if not m:
                continue
            deps.append(
                Dep(
                    name=m.group(1),
                    kind="pypi",
                    version=(m.group(2) or "").strip().lstrip("=<>!~ "),
                    raw_pin=line.strip(),
                    declared_in=f"{rel}:{i}",
                    upstream=Upstream(kind="pypi", ref=m.group(1)),
                )
            )
    return deps


def _gomod(root: str, rel: str) -> list[Dep]:
    try:
        text = open(os.path.join(root, rel), encoding="utf-8", errors="replace").read()
    except OSError:
        return []
    deps = []
    for i, line in enumerate(text.splitlines(), 1):
        m = re.match(r"\s*(?:require\s+)?([\w.\-/]+\.[\w.\-/]+)\s+(v[\w.\-+]+)", line)
        if not m:
            continue
        deps.append(
            Dep(
                name=m.group(1),
                kind="gomod",
                version=m.group(2).lstrip("v"),
                raw_pin=m.group(2),
                declared_in=f"{rel}:{i}",
                scope="test" if "// indirect" in line else "runtime",
                upstream=Upstream(kind="gomod", ref=m.group(1)),
            )
        )
    return deps


def _vcpkg(root: str, rel: str) -> list[Dep]:
    try:
        data = json.load(open(os.path.join(root, rel), encoding="utf-8"))
    except (OSError, ValueError):
        return []
    deps = []
    for entry in data.get("dependencies") or []:
        name = entry if isinstance(entry, str) else entry.get("name", "")
        if not name:
            continue
        version = "" if isinstance(entry, str) else str(entry.get("version>=", ""))
        deps.append(
            Dep(
                name=name,
                kind="vcpkg",
                version=version,
                raw_pin=version,
                declared_in=rel,
                upstream=Upstream(kind="vcpkg", ref=name),
            )
        )
    ov = data.get("builtin-baseline")
    for d in deps:
        if ov:
            d.notes.append(f"vcpkg baseline {ov[:12]} governs the actual version")
    return deps


def _conan_txt(root: str, rel: str) -> list[Dep]:
    deps: list[Dep] = []
    try:
        fh = open(os.path.join(root, rel), encoding="utf-8", errors="replace")
    except OSError:
        return []
    section = ""
    with fh:
        for i, line in enumerate(fh, 1):
            s = line.strip()
            if s.startswith("["):
                section = s.strip("[]")
                continue
            if not s or s.startswith("#") or section not in (
                "requires", "tool_requires", "build_requires"
            ):
                continue
            name, _, version = s.partition("/")
            deps.append(
                Dep(
                    name=name,
                    kind="conan",
                    version=version.split("@")[0],
                    raw_pin=s,
                    declared_in=f"{rel}:{i}",
                    scope="build" if "tool" in section or "build" in section else "runtime",
                    upstream=Upstream(kind="conan", ref=name),
                )
            )
    return deps


def _conan_py(root: str, rel: str) -> list[Dep]:
    try:
        text = open(os.path.join(root, rel), encoding="utf-8", errors="replace").read()
    except OSError:
        return []
    deps = []
    for m in re.finditer(r'self\.(tool_)?requires\(\s*["\']([\w.\-+]+)/([^"\'@]+)', text):
        line = text.count("\n", 0, m.start()) + 1
        deps.append(
            Dep(
                name=m.group(2),
                kind="conan",
                version=m.group(3),
                raw_pin=f"{m.group(2)}/{m.group(3)}",
                declared_in=f"{rel}:{line}",
                scope="build" if m.group(1) else "runtime",
                upstream=Upstream(kind="conan", ref=m.group(2)),
            )
        )
    return deps


# Per-manifest parsers, keyed by basename. Each takes (root, path relative to
# root) so a manifest found anywhere in the tree is parsed the same way.
_PARSERS = {
    "package.json": _npm,
    "Cargo.toml": _cargo,
    "pyproject.toml": _pyproject,
    "requirements.txt": _requirements,
    "go.mod": _gomod,
    "conanfile.txt": _conan_txt,
    "conanfile.py": _conan_py,
    "vcpkg.json": _vcpkg,
}


def _cmake_entries(rels: list[str]) -> list[str]:
    """Entry points for the CMake reader.

    `cmake.parse_project` already follows `include()` and `add_subdirectory()`,
    so a nested `CMakeLists.txt` reachable from the top is not a separate entry
    and must not be parsed twice. Only when the root has none do the topmost
    nested ones become entries.

    A subdirectory the root never adds is therefore still missed — that is the
    several-products-in-one-repository case, recorded as finding F in ROADMAP.md
    and deliberately not solved here, because guessing which stray
    `CMakeLists.txt` is a product and which is a vendored copy is the hard half.
    """
    lists = [r for r in rels if os.path.basename(r) == "CMakeLists.txt"]
    if "CMakeLists.txt" in lists:
        return ["CMakeLists.txt"]
    entries: list[str] = []
    for rel in sorted(lists, key=lambda r: (r.count(os.sep), r)):
        here = os.path.dirname(rel)
        if any(
            here == os.path.dirname(e) or here.startswith(os.path.dirname(e) + os.sep)
            for e in entries
        ):
            continue
        entries.append(rel)
    return entries


# --------------------------------------------------------------- system state


# find_package() names rarely match the pkg-config module name.
PKGCONFIG_ALIAS = {
    "ALSA": "alsa",
    "Threads": "",
    "OpenSSL": "openssl",
    "ZLIB": "zlib",
    "SQLite3": "sqlite3",
    "PNG": "libpng",
    "JPEG": "libjpeg",
    "Freetype": "freetype2",
    "CURL": "libcurl",
}

# Kinds whose effective version comes from the machine, not the repo.
_SYSTEM_KINDS = ("pkg-config", "cmake-find-package", "cmake-system-or-fetch")


def probe_installed(deps: list[Dep]) -> None:
    """Record what is actually installed on this machine.

    A system dependency has no pinned version in the repo — the effective
    version is whatever the build box supplies. That is precisely what makes
    these invisible to Dependabot, so resolving it is the whole point.

    `find_package(ALSA)` and `pkg_check_modules(... alsa)` refer to the same
    library under different names, so try a few candidates.
    """
    if not shutil.which("pkg-config"):
        return
    for dep in deps:
        if dep.kind not in _SYSTEM_KINDS or dep.installed_version:
            continue
        alias = PKGCONFIG_ALIAS.get(dep.name, dep.name)
        if alias == "":
            continue
        candidates = [c for c in (alias, dep.name, dep.name.lower()) if c]
        for cand in dict.fromkeys(candidates):
            try:
                out = subprocess.run(
                    ["pkg-config", "--modversion", cand],
                    capture_output=True, text=True, timeout=10,
                )
            except (OSError, subprocess.SubprocessError):
                break
            if out.returncode == 0 and out.stdout.strip():
                dep.installed_version = out.stdout.strip()
                break


# ------------------------------------------------------------- reconciliation


# The generic rule below fails only where two names differ in substance rather
# than in spelling. Every entry here needs a real observed pair: a table of
# plausible-looking package-name equivalences is exactly the kind of constant
# that gets guessed once and inherited forever (see ROADMAP.md on `KNOWN`).
CANONICAL_ALIAS: dict[str, str] = {}

# Strip a leading `lib` only when something substantial is left, so `libc` does
# not become `c`.
_LIB_PREFIX = re.compile(r"^lib(?=[a-z0-9]{3,})")


def canonical_name(name: str) -> str:
    """A spelling-independent key for the same library under two names.

    `find_package(CURL)` versus Conan's `libcurl`; `LibArchive` versus
    `libarchive`; `OpenSSL` versus `openssl`; `ZLIB` versus `zlib`. Casefold,
    drop separators, drop a leading `lib` — which covers every alias observed on
    a real project with no table to maintain, and keeps the mechanism one that
    generalises instead of one that enumerates.
    """
    key = _LIB_PREFIX.sub("", re.sub(r"[^a-z0-9]", "", name.lower()))
    return CANONICAL_ALIAS.get(key, key)


# Which namespace a declaration's names live in. Only declarations in the same
# family are candidates for reconciliation: npm's `zlib` and Conan's `zlib` are
# not the same artefact, whereas `find_package(ZLIB)` and Conan's `zlib` are.
_FAMILY = {"npm": "npm", "cargo": "cargo", "pypi": "pypi", "gomod": "gomod"}


def _family(kind: str) -> str:
    return _FAMILY.get(kind, "native")


def _split_site(declared_in: str) -> tuple[str, int]:
    """Split `path:line`, `path (table)` or `path` into (path, line)."""
    text = (declared_in or "").strip()
    if not text:
        return "", 0
    head = text.split(" (")[0].strip()
    path, _, lpart = head.rpartition(":")
    if path and lpart.isdigit():
        return path, int(lpart)
    return head, 0


def _declaration_of(dep: Dep) -> Declaration:
    path, line = _split_site(dep.declared_in)
    return Declaration(
        path=path, line=line, kind=dep.kind, version=dep.version, raw_pin=dep.raw_pin
    )


def _pin_rank(d: Declaration) -> int:
    """How good a declaration is as *the* pin. `apply` needs version and line."""
    if d.is_editable():
        return 3
    if d.version:
        return 2
    if d.path and d.line:
        return 1
    return 0


# How useful an upstream is for "what versions exist?". Higher wins a merge.
_UPSTREAM_RANK = {
    "github": 4, "gitlab": 4,
    "pypi": 3, "npm": 3, "crates": 3, "conan": 3, "vcpkg": 3, "gomod": 3,
    "distro": 2,
    "unknown": 1, "": 0,
}


def reconcile(deps: list[Dep]) -> list[Dep]:
    """Fold declarations of the same library into one record, additively.

    Two things make this necessary rather than cosmetic.

    A manifest per target platform declares the same dependency several times,
    and the versions can *disagree*. That divergence is the finding — no
    upstream lookup produces it — so every declaration is kept and
    `Dep.diverges()` can see it.

    And a CMake `find_package` name sits beside a package-manager pin for the
    same library (`CURL` and `libcurl`). Left separate, the unpinned CMake
    record resolves to a distro lookup, so the tool compares the *machine's*
    system library against what distros ship and frames the fix as a CI-image
    change — when the real pin is one line in a manifest. That is not a missing
    feature, it is a wrong answer stated confidently.

    Nothing is dropped: the surviving record carries every declaration site and
    every name it was declared under, and the merge only ever adds facts.
    """
    for dep in deps:
        if not dep.declarations:
            dep.declarations = [_declaration_of(dep)]

    groups: dict[tuple[str, str], list[Dep]] = {}
    for dep in deps:
        groups.setdefault((_family(dep.kind), canonical_name(dep.name)), []).append(dep)

    out: list[Dep] = []
    for key in dict.fromkeys((_family(d.kind), canonical_name(d.name)) for d in deps):
        group = groups[key]
        out.append(group[0] if len(group) == 1 else _fold(group))
    return out


def _rank_of(dep: Dep) -> tuple[int, int]:
    return (
        max((_pin_rank(x) for x in dep.declarations), default=0),
        _UPSTREAM_RANK.get(dep.upstream.kind, 0),
    )


def _fold(group: list[Dep]) -> Dep:
    """Merge a group of declarations of one library into its best record.

    The surviving record carries the most editable pin, then the most
    informative upstream. Among equally good candidates the *oldest* version
    wins, which is the only defensible choice when the platforms disagree: it is
    the copy an advisory is most likely to match and the one that breaks first,
    so leading the report with the newest would understate the exposure on the
    platform that matters. `version` is then the worst we ship, and
    `declarations` says where the rest are.
    """
    best = max(_rank_of(d) for d in group)
    keep = min(
        (d for d in group if _rank_of(d) == best),
        key=lambda d: (parse_version(d.version) or (0,), d.declared_in),
    )
    others = [d for d in group if d is not keep]

    decls = list(keep.declarations)
    for d in others:
        for decl in d.declarations:
            if not any(x.where() == decl.where() and x.kind == decl.kind for x in decls):
                decls.append(decl)
    keep.declarations = sorted(decls, key=lambda x: (x.path, x.line, x.kind))

    keep.aliases = sorted({d.name for d in group if d.name != keep.name})
    keep.scope = most_restrictive(*[d.scope for d in group])
    keep.scope_evidence = list(dict.fromkeys(e for d in group for e in d.scope_evidence))
    keep.notes = list(dict.fromkeys(n for d in group for n in d.notes))

    for attr in ("version", "raw_pin", "integrity", "installed_version"):
        if getattr(keep, attr):
            continue
        for d in others:
            if getattr(d, attr):
                setattr(keep, attr, getattr(d, attr))
                break
    if not _UPSTREAM_RANK.get(keep.upstream.kind, 0):
        for d in sorted(others, key=lambda d: -_UPSTREAM_RANK.get(d.upstream.kind, 0)):
            if _UPSTREAM_RANK.get(d.upstream.kind, 0):
                keep.upstream = d.upstream
                break
    for d in others:
        for p in d.companion_pins:
            if not any(q.var == p.var and q.where() == p.where() for q in keep.companion_pins):
                keep.companion_pins.append(p)

    kinds = {x.kind for x in keep.declarations}
    if keep.aliases:
        keep.notes.append("also declared as " + ", ".join(keep.aliases))
    if any(k.startswith("cmake-") or k == "pkg-config" for k in kinds) and (
        kinds & {"conan", "vcpkg"}
    ):
        keep.notes.append(
            "declared to the build system under one name and pinned by the package "
            "manager under another — the package manager decides the version, so a "
            "system-library comparison would be the wrong answer here"
        )
    if keep.diverges():
        keep.notes.append(keep.divergence_note())
    editable = [x for x in keep.declarations if x.is_editable()]
    if len(editable) > 1:
        keep.notes.append(
            f"pinned in {len(editable)} places — a bump has to edit all of them: "
            + ", ".join(x.where() for x in editable)
        )
    return keep


def discover(root: str) -> tuple[list[Dep], list[str]]:
    """Return (deps, files_that_declare_them)."""
    deps: list[Dep] = []
    files: list[str] = []

    manifests = detect_manifests(root)
    rels = [rel for rel, _ in manifests]

    # A vendored tree is usually reached by `add_subdirectory`, so the CMake
    # reader needs the same exclusion the manifest walk applies.
    submodules = submodule_paths(root)
    for entry in _cmake_entries(rels):
        cdeps, cfiles = cmake.parse_project(root, entry, submodules)
        deps += cdeps
        files += cfiles

    parsed: dict[str, list[Dep]] = {}
    for rel, _eco in manifests:
        parser = _PARSERS.get(os.path.basename(rel))
        if parser is not None:
            parsed[rel] = parser(root, rel)

    # `requirements.txt` beside a pyproject that declares dependencies is
    # usually a generated or partial mirror of it, so it stays the fallback it
    # has always been — now decided per directory rather than per repository.
    pyproject_dirs = {
        os.path.dirname(rel)
        for rel, found in parsed.items()
        if os.path.basename(rel) == "pyproject.toml" and found
    }

    for rel, found in parsed.items():
        if os.path.basename(rel) == "requirements.txt" and os.path.dirname(rel) in pyproject_dirs:
            continue
        deps += found
        # Listed even when it yielded nothing: a manifest whose schema we cannot
        # read (a Poetry pyproject, today) must not look like a project with no
        # dependencies.
        files.append(rel)

    probe_installed(deps)

    # Drop CMake's own built-in find modules that are not really dependencies.
    noise = {
        "Threads", "PkgConfig", "Git", "Doxygen",
        "Python", "Python2", "Python3", "PythonInterp", "PythonLibs",
    }
    deps = [d for d in deps if d.name not in noise]

    deps = reconcile(deps)
    deps.sort(key=lambda d: (d.scope, d.name.lower()))
    return deps, sorted(set(files))
