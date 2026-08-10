"""A small, tolerant CMake reader.

Not a CMake interpreter. It tokenises command invocations, tracks `if()`
nesting so we can tell a test-only dependency from a shipped one, and follows
`include()` / `add_subdirectory()` so a dep declared three files deep is still
found.

This is the part Renovate does not do for URL-pinned `FetchContent_Declare`.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

from .model import Dep, Upstream, most_restrictive

# Conditions that mean "this block only matters for tests / examples / docs".
_TEST_COND = re.compile(r"\b(BUILD_TESTING|BUILD_TESTS?|ENABLE_TESTS?|\w*_TESTS?)\b", re.I)
_EXAMPLE_COND = re.compile(r"\b(BUILD_EXAMPLES?|BUILD_DOCS?|BUILD_BENCH\w*)\b", re.I)
_TEST_TARGET = re.compile(r"(^|[_\-])(tests?|gtest|catch|bench)([_\-]|$)", re.I)


@dataclass
class Command:
    name: str
    args: list[str]
    line: int
    file: str
    scope: str  # runtime | test | example


def _strip_comments(text: str) -> str:
    """Remove # comments and #[[ ]] bracket comments, respecting quotes."""
    out = []
    i, n = 0, len(text)
    in_str = False
    while i < n:
        c = text[i]
        if in_str:
            out.append(c)
            if c == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if c == '"':
                in_str = False
            i += 1
            continue
        if c == '"':
            in_str = True
            out.append(c)
            i += 1
            continue
        if c == "#":
            # Bracket comment #[=*[ ... ]=*]
            m = re.match(r"#\[(=*)\[", text[i:])
            if m:
                closer = "]" + m.group(1) + "]"
                end = text.find(closer, i)
                i = n if end == -1 else end + len(closer)
                continue
            # Line comment: keep the newline so line numbers survive.
            end = text.find("\n", i)
            i = n if end == -1 else end
            continue
        out.append(c)
        i += 1
    return "".join(out)


def _split_args(blob: str) -> list[str]:
    """Split a command's argument blob, honouring quotes."""
    args: list[str] = []
    cur: list[str] = []
    in_str = False
    i, n = 0, len(blob)
    while i < n:
        c = blob[i]
        if in_str:
            if c == "\\" and i + 1 < n:
                cur.append(blob[i + 1])
                i += 2
                continue
            if c == '"':
                in_str = False
                i += 1
                continue
            cur.append(c)
            i += 1
            continue
        if c == '"':
            in_str = True
            i += 1
            continue
        if c.isspace():
            if cur:
                args.append("".join(cur))
                cur = []
            i += 1
            continue
        cur.append(c)
        i += 1
    if cur:
        args.append("".join(cur))
    return args


_CMD_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)\s*\(", re.M)


def iter_commands(text: str, rel_file: str) -> list[Command]:
    """Tokenise every command invocation, tracking if()-scope."""
    clean = _strip_comments(text)
    cmds: list[Command] = []
    # Stack of scopes contributed by enclosing if() blocks.
    cond_stack: list[str] = []

    pos = 0
    while True:
        m = _CMD_RE.search(clean, pos)
        if not m:
            break
        name = m.group(1)
        # Find the matching close paren.
        depth = 1
        i = m.end()
        in_str = False
        while i < len(clean) and depth:
            c = clean[i]
            if in_str:
                if c == "\\":
                    i += 2
                    continue
                if c == '"':
                    in_str = False
            elif c == '"':
                in_str = True
            elif c == "(":
                depth += 1
            elif c == ")":
                depth -= 1
            i += 1
        blob = clean[m.end() : i - 1]
        line = clean.count("\n", 0, m.start()) + 1
        args = _split_args(blob)
        low = name.lower()

        if low == "if":
            cond = " ".join(args)
            if _TEST_COND.search(cond):
                cond_stack.append("test")
            elif _EXAMPLE_COND.search(cond):
                cond_stack.append("example")
            else:
                cond_stack.append("runtime")
        elif low == "endif":
            if cond_stack:
                cond_stack.pop()
        elif low in ("else", "elseif"):
            # An else branch is not governed by the positive condition.
            if cond_stack:
                cond_stack[-1] = "runtime"

        scope = most_restrictive("runtime", *cond_stack)
        cmds.append(Command(name=low, args=args, line=line, file=rel_file, scope=scope))
        pos = i
    return cmds


def collect_cmake_files(root: str, entry: str = "CMakeLists.txt") -> list[str]:
    """Follow include() / add_subdirectory() from the entry point."""
    seen: list[str] = []
    queue = [entry]
    guard = set()
    while queue:
        rel = queue.pop(0)
        if rel in guard:
            continue
        guard.add(rel)
        full = os.path.join(root, rel)
        if not os.path.isfile(full):
            continue
        seen.append(rel)
        try:
            text = open(full, encoding="utf-8", errors="replace").read()
        except OSError:
            continue
        base = os.path.dirname(rel)
        for cmd in iter_commands(text, rel):
            if cmd.name == "add_subdirectory" and cmd.args:
                sub = cmd.args[0]
                if "${" in sub:
                    continue
                queue.append(os.path.normpath(os.path.join(base, sub, "CMakeLists.txt")))
            elif cmd.name == "include" and cmd.args:
                inc = cmd.args[0]
                if "${" in inc or not inc.endswith(".cmake"):
                    continue
                queue.append(os.path.normpath(os.path.join(base, inc)))
    return seen


# ---------------------------------------------------------------- extraction

_GH_ARCHIVE = re.compile(
    r"https?://github\.com/(?P<owner>[\w.\-]+)/(?P<repo>[\w.\-]+)/archive/"
    r"(?:refs/tags/)?(?P<tag>[^/\s]+?)(?:\.tar\.gz|\.zip|\.tgz)$",
    re.I,
)
_GH_RELEASE = re.compile(
    r"https?://github\.com/(?P<owner>[\w.\-]+)/(?P<repo>[\w.\-]+)/releases/download/"
    r"(?P<tag>[^/\s]+)/",
    re.I,
)
_GH_REPO = re.compile(
    r"(?:https?://github\.com/|git@github\.com:)(?P<owner>[\w.\-]+)/(?P<repo>[\w.\-]+?)(?:\.git)?/?$",
    re.I,
)


def _version_from_tag(tag: str) -> str:
    return tag.lstrip("vV") if re.match(r"^v?\d", tag) else tag


def _kv(args: list[str], key: str) -> str:
    """Value following a keyword argument, e.g. GIT_TAG <value>."""
    for i, a in enumerate(args):
        if a.upper() == key and i + 1 < len(args):
            return args[i + 1]
    return ""


def _parse_fetchcontent(cmd: Command) -> Dep | None:
    if not cmd.args:
        return None
    name = cmd.args[0]
    url = _kv(cmd.args, "URL")
    git_repo = _kv(cmd.args, "GIT_REPOSITORY")
    git_tag = _kv(cmd.args, "GIT_TAG")
    url_hash = _kv(cmd.args, "URL_HASH") or _kv(cmd.args, "URL_MD5")
    where = f"{cmd.file}:{cmd.line}"

    if url:
        up = Upstream()
        version = ""
        m = _GH_ARCHIVE.search(url) or _GH_RELEASE.search(url)
        if m:
            up = Upstream(kind="github", ref=f"{m.group('owner')}/{m.group('repo')}")
            version = _version_from_tag(m.group("tag"))
        else:
            vm = re.search(r"[-/]v?(\d+\.\d+(?:\.\d+)?)", url)
            version = vm.group(1) if vm else ""
            up = Upstream(kind="unknown", ref=url, note="non-GitHub archive URL")
        return Dep(
            name=name,
            kind="cmake-fetchcontent-url",
            version=version,
            raw_pin=url,
            integrity=url_hash,
            declared_in=where,
            scope=cmd.scope,
            scope_evidence=[f"declared under {cmd.scope} guard at {where}"]
            if cmd.scope != "runtime"
            else [],
            upstream=up,
        )

    if git_repo:
        m = _GH_REPO.search(git_repo)
        up = (
            Upstream(kind="github", ref=f"{m.group('owner')}/{m.group('repo')}")
            if m
            else Upstream(kind="unknown", ref=git_repo)
        )
        return Dep(
            name=name,
            kind="cmake-fetchcontent-git",
            version=_version_from_tag(git_tag) if git_tag else "",
            raw_pin=git_tag,
            declared_in=where,
            scope=cmd.scope,
            upstream=up,
            notes=[
                "GIT_TAG is a commit SHA, not a tag — frozen at a point in "
                "time with no version to compare against; check what has "
                "landed upstream since"
            ]
            if re.fullmatch(r"[0-9a-f]{7,40}", git_tag or "")
            else [],
        )
    return None


def _parse_cpm(cmd: Command) -> Dep | None:
    """CPMAddPackage(NAME x VERSION y GITHUB_REPOSITORY o/r) or shorthand."""
    if not cmd.args:
        return None
    args = cmd.args
    name = _kv(args, "NAME")
    gh = _kv(args, "GITHUB_REPOSITORY") or _kv(args, "GITLAB_REPOSITORY")
    version = _kv(args, "VERSION") or _version_from_tag(_kv(args, "GIT_TAG"))
    if not name and args:
        # Shorthand: CPMAddPackage("gh:owner/repo@1.2.3")
        m = re.match(r"gh:([\w.\-]+/[\w.\-]+)@(.+)", args[0])
        if m:
            gh, version = m.group(1), m.group(2)
            name = gh.split("/")[-1]
    if not name:
        return None
    return Dep(
        name=name,
        kind="cmake-cpm",
        version=version,
        raw_pin=version,
        declared_in=f"{cmd.file}:{cmd.line}",
        scope=cmd.scope,
        upstream=Upstream(kind="github", ref=gh) if gh else Upstream(),
    )


def _parse_find_package(cmd: Command) -> Dep | None:
    if not cmd.args:
        return None
    name = cmd.args[0]
    if name.startswith("${"):
        return None
    version = ""
    if len(cmd.args) > 1 and re.match(r"^\d+(\.\d+)*$", cmd.args[1]):
        version = cmd.args[1]
    exact = any(a.upper() == "EXACT" for a in cmd.args)
    required = any(a.upper() == "REQUIRED" for a in cmd.args)
    notes = []
    if exact:
        notes.append("EXACT — a newer system version will not satisfy this")
    return Dep(
        name=name,
        kind="cmake-find-package",
        version=version,
        raw_pin=f"{version}{' EXACT' if exact else ''}".strip(),
        declared_in=f"{cmd.file}:{cmd.line}",
        scope=cmd.scope,
        upstream=Upstream(kind="distro", ref=name, note="provided by the system/toolchain"),
        notes=notes + ([] if required else ["optional (not REQUIRED)"]),
    )


def _parse_pkg_check(cmd: Command) -> list[Dep]:
    """pkg_check_modules(PREFIX [REQUIRED] [IMPORTED_TARGET] mod[>=ver] ...)"""
    if len(cmd.args) < 2:
        return []
    keywords = {
        "REQUIRED", "QUIET", "IMPORTED_TARGET", "GLOBAL", "NO_CMAKE_PATH",
        "NO_CMAKE_ENVIRONMENT_PATH",
    }
    required = any(a.upper() == "REQUIRED" for a in cmd.args)
    mods = [a for a in cmd.args[1:] if a.upper() not in keywords and not a.startswith("${")]
    deps = []
    for mod in mods:
        m = re.match(r"^([\w.+\-]+)\s*(>=|=|>|<=|<)?\s*([\d.]+)?$", mod)
        if not m:
            continue
        modname, op, ver = m.group(1), m.group(2) or "", m.group(3) or ""
        deps.append(
            Dep(
                name=modname,
                kind="pkg-config",
                version=ver,
                raw_pin=f"{op}{ver}" if ver else "",
                declared_in=f"{cmd.file}:{cmd.line}",
                scope=cmd.scope,
                upstream=Upstream(kind="distro", ref=modname),
                notes=[] if required else ["optional (not REQUIRED)"],
            )
        )
    return deps


# How useful an upstream is for "what versions exist?". Higher wins a merge.
_UPSTREAM_RANK = {
    "github": 4, "gitlab": 4,
    "pypi": 3, "npm": 3, "crates": 3, "gomod": 3, "conan": 3, "vcpkg": 3,
    "distro": 2,
    "unknown": 1, "": 0,
}


def _merge(a: Dep, b: Dep) -> Dep:
    """Fold two declarations of the same dependency into one record.

    The common C++ shape is `find_package(... QUIET)` with a
    `FetchContent_Declare` fallback. Both matter: find_package says a system
    copy is acceptable, FetchContent says which version we vendor — and it
    carries the GitHub URL, which is the only thing that tells us what
    releases exist. Keep the record with the more informative upstream and
    graft the rest on.
    """
    keep, drop = (a, b) if _UPSTREAM_RANK.get(a.upstream.kind, 0) >= _UPSTREAM_RANK.get(
        b.upstream.kind, 0
    ) else (b, a)

    keep.scope = most_restrictive(a.scope, b.scope)
    keep.scope_evidence = list(dict.fromkeys(a.scope_evidence + b.scope_evidence))
    for attr in ("version", "raw_pin", "integrity", "installed_version"):
        if not getattr(keep, attr) and getattr(drop, attr):
            setattr(keep, attr, getattr(drop, attr))
    keep.notes = list(dict.fromkeys(keep.notes + drop.notes))
    if drop.declared_in and drop.declared_in != keep.declared_in:
        keep.notes.append(f"also declared at {drop.declared_in} ({drop.kind})")

    kinds = {a.kind, b.kind}
    if any(k.startswith("cmake-fetchcontent") for k in kinds) and "cmake-find-package" in kinds:
        keep.kind = "cmake-system-or-fetch"
        keep.notes.append(
            "system copy used when present, otherwise the pinned archive is fetched — "
            "the effective version differs between build machines"
        )
    return keep


def parse_project(root: str, entry: str = "CMakeLists.txt") -> tuple[list[Dep], list[str]]:
    """Return (deps, cmake_files_read)."""
    files = collect_cmake_files(root, entry)
    deps: dict[str, Dep] = {}
    # name -> scope, learned from FetchContent_MakeAvailable / link edges
    scope_hint: dict[str, str] = {}
    targets: dict[str, str] = {}  # target -> scope it was defined in
    link_edges: list[tuple[str, list[str], str]] = []
    cache_vars: list[dict] = []

    for rel in files:
        try:
            text = open(os.path.join(root, rel), encoding="utf-8", errors="replace").read()
        except OSError:
            continue
        for cmd in iter_commands(text, rel):
            new: list[Dep] = []
            if cmd.name == "fetchcontent_declare":
                d = _parse_fetchcontent(cmd)
                if d:
                    new = [d]
            elif cmd.name == "cpmaddpackage":
                d = _parse_cpm(cmd)
                if d:
                    new = [d]
            elif cmd.name == "find_package":
                d = _parse_find_package(cmd)
                if d:
                    new = [d]
            elif cmd.name == "pkg_check_modules":
                new = _parse_pkg_check(cmd)
            elif cmd.name == "fetchcontent_makeavailable":
                for a in cmd.args:
                    scope_hint[a.lower()] = most_restrictive(
                        scope_hint.get(a.lower(), "runtime"), cmd.scope
                    )
            elif cmd.name == "set" and len(cmd.args) >= 2:
                # A version-valued CACHE variable sitting beside a dependency
                # is almost always a coupled pin — a prebuilt engine, ABI
                # level, or protocol version that must move with the source.
                # Bumping one and not the other produces a link error that
                # looks nothing like a dependency problem.
                var, val = cmd.args[0], cmd.args[1]
                if re.fullmatch(r"v?\d+\.\d+(\.\d+)?", val) and "VERSION" in var.upper():
                    # set(<var> <value> CACHE <type> <docstring> [FORCE])
                    doc = ""
                    for i, a in enumerate(cmd.args):
                        if a.upper() == "CACHE" and i + 2 < len(cmd.args):
                            doc = cmd.args[i + 2]
                            break
                    cache_vars.append(
                        {"var": var, "value": val, "line": cmd.line,
                         "file": cmd.file, "doc": doc, "scope": cmd.scope}
                    )
            elif cmd.name in ("add_executable", "add_library"):
                if cmd.args:
                    targets[cmd.args[0]] = cmd.scope
            elif cmd.name == "target_link_libraries" and cmd.args:
                libs = [
                    a for a in cmd.args[1:]
                    if a.upper() not in ("PUBLIC", "PRIVATE", "INTERFACE") and not a.startswith("${")
                ]
                link_edges.append((cmd.args[0], libs, cmd.scope))

            for d in new:
                prev = deps.get(d.name)
                deps[d.name] = d if prev is None else _merge(prev, d)

    # A dep whose targets are only ever linked into test executables is
    # test-only even when declared at top level.
    for name, dep in deps.items():
        hint = scope_hint.get(name.lower())
        if hint and hint != "runtime":
            dep.scope = most_restrictive(dep.scope, hint)
            dep.scope_evidence.append(f"FetchContent_MakeAvailable under {hint} guard")

        consumer_scopes = []
        for target, libs, edge_scope in link_edges:
            if not any(_matches_dep(name, lib) for lib in libs):
                continue
            s = edge_scope
            if _TEST_TARGET.search(target) or targets.get(target) == "test":
                s = "test"
            consumer_scopes.append((target, s))
        if consumer_scopes and all(s != "runtime" for _, s in consumer_scopes):
            worst = most_restrictive(*[s for _, s in consumer_scopes])
            if worst != dep.scope:
                dep.scope = most_restrictive(dep.scope, worst)
            dep.scope_evidence.append(
                "linked only into: " + ", ".join(sorted(t for t, _ in consumer_scopes))
            )

        _attach_companions(dep, cache_vars)

    return list(deps.values()), files


def _attach_companions(dep: Dep, cache_vars: list[dict]) -> None:
    """Link version-valued CACHE variables to the dependency they belong to.

    Matched by name (`HEGEL_LIBHEGEL_VERSION` mentions `hegel`), by the CACHE
    docstring naming the dependency, or by sitting within a few lines of its
    declaration.
    """
    try:
        decl_line = int(dep.declared_in.split(":")[1])
    except (IndexError, ValueError):
        decl_line = None
    decl_file = dep.declared_in.split(":")[0] if dep.declared_in else ""
    key = dep.name.lower().replace("-", "").replace("_", "")

    for cv in cache_vars:
        if cv["var"].upper() == f"{dep.name.upper()}_VERSION":
            continue  # the dep's own version, not a companion
        by_name = key and key in cv["var"].lower().replace("_", "")
        by_doc = key and key in (cv["doc"] or "").lower().replace("-", "").replace("_", "")
        by_place = (
            decl_line is not None
            and cv["file"] == decl_file
            and 0 <= cv["line"] - decl_line <= 20
        )
        if not (by_name or by_doc or by_place):
            continue
        entry = f"{cv['var']}={cv['value']} — {cv['file']}:{cv['line']}"
        if cv["doc"]:
            entry += f" ({cv['doc']})"
        if entry not in dep.companion_pins:
            dep.companion_pins.append(entry)

    if dep.companion_pins:
        dep.notes.append(
            "has coupled pin(s) that must be bumped together — changing this "
            "dependency alone can produce an ABI/link failure"
        )


def _matches_dep(dep_name: str, lib: str) -> bool:
    """Loosely match a target name against a dependency name."""
    a = dep_name.lower().replace("-", "").replace("_", "")
    b = lib.lower().split("::")[0].replace("-", "").replace("_", "")
    if not a or not b:
        return False
    return a == b or a.startswith(b) or b.startswith(a)
