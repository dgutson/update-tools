"""Zero-dependency extractor for the consumed API surface.

The question that decides "is this upgrade worth it" is not *what changed
upstream* but *what changed upstream that we actually call*. This backend
answers the second half without any external tool:

  1. attribute every #include / import in our sources to a dependency,
  2. inside the files that import it, harvest the symbols that belong to it,
  3. report both, with file:line sites.

It is a heuristic, not a compiler. The LLM layer is expected to sanity-check
what comes out of here, which is why every symbol carries a site.

What this list feeds matters for how wide it should be: the API-surface diff can
only report a break in a symbol recorded here, so anything missed becomes a
break that is never reported. That asymmetry is why the C/C++ harvest is *not*
keyed on a trailing `(`. It was, and the consequence was that only functions
were ever recorded — types, enum and macro constants and callbacks passed by
name were all invisible, and a library whose API is mostly types read as
consuming nothing at all. Over-harvesting produces a symbol the diff then finds
nothing to say about; under-harvesting produces silence that reads as safety.
"""

from __future__ import annotations

import os
import re

from ..model import Dep, Site

SOURCE_EXT = {
    ".c", ".cc", ".cpp", ".cxx", ".h", ".hh", ".hpp", ".hxx", ".ipp", ".inl",
    ".py", ".rs", ".go", ".js", ".jsx", ".ts", ".tsx", ".mjs",
}
SKIP_DIRS = {
    ".git", "build", "_build", "out", "dist", "node_modules", "target",
    "__pycache__", ".venv", "venv", "third_party", "vendor", "_deps",
    ".mypy_cache", ".pytest_cache", "cmake-build-debug", "cmake-build-release",
}

# Libraries whose include path / namespace / C prefix does not follow from the
# package name. Everything else is derived generically below.
KNOWN: dict[str, dict[str, list[str]]] = {
    "googletest": {
        "includes": ["gtest/", "gmock/"],
        "namespaces": ["testing"],
        "prefixes": ["TEST", "TEST_F", "TEST_P", "EXPECT_", "ASSERT_", "MOCK_METHOD", "INSTANTIATE_"],
    },
    "gtest": {"includes": ["gtest/"], "namespaces": ["testing"], "prefixes": ["TEST", "EXPECT_", "ASSERT_"]},
    "hegel": {
        "includes": ["hegel/"],
        "namespaces": ["hegel"],
        "prefixes": ["HEGEL_"],
    },
    "hegel-cpp": {"includes": ["hegel/"], "namespaces": ["hegel"], "prefixes": ["HEGEL_"]},
    "fluidsynth": {
        "includes": ["fluidsynth.h", "fluidsynth/"],
        "namespaces": [],
        "prefixes": ["fluid_", "FLUID_"],
    },
    "ALSA": {
        "includes": ["alsa/"],
        "namespaces": [],
        "prefixes": ["snd_", "SND_"],
    },
    "alsa": {"includes": ["alsa/"], "namespaces": [], "prefixes": ["snd_", "SND_"]},
    "yaml-cpp": {
        "includes": ["yaml-cpp/"],
        "namespaces": ["YAML"],
        "prefixes": [],
    },
    "libremidi": {
        "includes": ["libremidi/"],
        "namespaces": ["libremidi"],
        "prefixes": [],
    },
    "boost": {"includes": ["boost/"], "namespaces": ["boost"], "prefixes": ["BOOST_"]},
    "fmt": {"includes": ["fmt/"], "namespaces": ["fmt"], "prefixes": ["FMT_"]},
    "spdlog": {"includes": ["spdlog/"], "namespaces": ["spdlog"], "prefixes": ["SPDLOG_"]},
    "nlohmann_json": {"includes": ["nlohmann/"], "namespaces": ["nlohmann"], "prefixes": []},
    "openssl": {"includes": ["openssl/"], "namespaces": [], "prefixes": ["SSL_", "EVP_", "X509_", "BIO_"]},
    "sqlite3": {"includes": ["sqlite3.h"], "namespaces": [], "prefixes": ["sqlite3_"]},
    "zlib": {"includes": ["zlib.h"], "namespaces": [], "prefixes": ["gz", "deflate", "inflate", "compress"]},
}


def _profile_for(dep: Dep) -> dict[str, list[str]]:
    """Include prefixes / namespaces / symbol prefixes to look for.

    Every name the dependency is declared under contributes candidates.
    Reconciliation leaves the package-manager spelling as the record's name
    while the build system may declare it under another (`libcurl` beside
    `CURL`), and either spelling can be the one that resolves. By this file's
    asymmetry rule an extra candidate costs a little noise, while a missing one
    costs the whole consumed surface for that dependency.
    """
    names = [dep.name] + [a for a in dep.aliases if a]
    for name in names:
        for key in (name, name.lower(), name.replace("-", "_")):
            if key in KNOWN:
                return KNOWN[key]
    includes: list[str] = []
    namespaces: list[str] = []
    prefixes: list[str] = []
    for name in names:
        stem = re.sub(r"^(lib|python-|py|node-|rust-|go-)", "", name.lower())
        stem = stem.replace("-", "_")
        alt = stem.replace("_", "")
        includes += [f"{name}/", f"{stem}/", f"{stem}.h", f"{alt}/", f"{alt}.h"]
        namespaces += [stem, alt, name.lower()]
        prefixes += [f"{stem}_", f"{alt}_"]
    return {
        "includes": list(dict.fromkeys(includes)),
        "namespaces": list(dict.fromkeys(namespaces)),
        "prefixes": list(dict.fromkeys(prefixes)),
    }


# Leading words C libraries put *in front of* their own prefix, so the symbol
# does not start with it: `new_fluid_synth`, `delete_fluid_settings`. A bounded
# conventional list rather than "any leading word", because `my_fluid_helper` is
# our wrapper and not their API.
_CTOR_AFFIXES = (
    "new", "delete", "free", "create", "destroy", "make", "init", "open", "close",
)

# Most symbols we consume are worth listing, but a prefix shared with a large
# library (`BOOST_`) can harvest hundreds, and CLAUDE_DEPS.md is meant to be
# read. Truncation is reported rather than silent, and calls survive it first.
MAX_CONSUMED = 150
_CONTEXT_RANK = {"call": 0, "type": 1, "constant": 2, "import": 0}

_INCLUDE_RE = re.compile(r'^\s*#\s*include\s*[<"]([^>"]+)[>"]', re.M)
# Comments, string and char literals, and #include paths. A dependency's symbol
# named in a log message or a doc comment is not a use of it. Ordered so that a
# `'` inside a comment cannot open a char literal.
_NON_CODE = re.compile(
    r"""//[^\n]*
      | /\*.*?\*/
      | ^[ \t]*\#[ \t]*include[^\n]*
      | "(?:\\.|[^"\\\n])*"
      | '(?:\\.|[^'\\\n])*'""",
    re.S | re.M | re.X,
)
_PY_IMPORT_RE = re.compile(r"^\s*(?:from\s+([\w.]+)\s+import\s+(.+)|import\s+([\w.,\s]+))", re.M)
_JS_IMPORT_RE = re.compile(
    r"""(?:import\s+(?:(?P<what>[^'"]+?)\s+from\s+)?['"](?P<mod>[^'"]+)['"]"""
    r"""|require\(\s*['"](?P<mod2>[^'"]+)['"]\s*\))""",
    re.M,
)
_RS_USE_RE = re.compile(r"^\s*use\s+([\w:]+)(?:::\{([^}]*)\})?", re.M)
_GO_IMPORT_RE = re.compile(r'^\s*(?:[\w.]+\s+)?"([\w./\-]+)"', re.M)


def _iter_sources(root: str) -> list[str]:
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        for fn in filenames:
            if os.path.splitext(fn)[1] in SOURCE_EXT:
                out.append(os.path.relpath(os.path.join(dirpath, fn), root))
    return sorted(out)


def _read(root: str, rel: str) -> str:
    try:
        return open(os.path.join(root, rel), encoding="utf-8", errors="replace").read()
    except OSError:
        return ""


def _line_of(text: str, idx: int) -> int:
    return text.count("\n", 0, idx) + 1


def _blank_non_code(text: str) -> str:
    """Comments, literals and include paths replaced by same-length whitespace.

    Same length because every site's line number is derived from an offset into
    this string, so the replacement has to be positionally transparent.
    """
    return _NON_CODE.sub(lambda m: re.sub(r"[^\n]", " ", m.group(0)), text)


def _context_at(text: str, end: int, name: str) -> str:
    """What the use site looks like: call | constant | type.

    Recorded per symbol because the three are not equally strong evidence — a
    call is a hard dependency on a signature, a type mention may be a forward
    declaration, a constant may be tested in a `#if` — and because a caller
    truncating the list should drop the weakest last.
    """
    if text[end:end + 64].lstrip().startswith("("):
        return "call"
    if re.fullmatch(r"[A-Z][A-Z0-9_]*", name):
        return "constant"
    return "type"


def _harvest_cxx(text: str, prof: dict[str, list[str]]) -> dict[str, tuple[int, str]]:
    """symbol -> (first offset, context), for C/C++ sources.

    Three ways a symbol is attributed to the dependency:

      * `ns::Name`, for a C++ library with a namespace;
      * the library's own prefix, when that prefix ends in `_` and so acts as a
        namespace marker — any identifier carrying it belongs to the library
        whether or not it is being called;
      * a conventional constructor affix in front of that prefix
        (`new_fluid_synth`), which carries none of the prefix at its start and
        so was previously invisible even though it is a plain function call.

    A prefix that is a bare stem rather than a marker (zlib's `deflate`,
    `compress`; googletest's `TEST`) still has to look like a call, because such
    a token is about as likely to be an English word or an identifier of ours.
    """
    found: dict[str, tuple[int, str]] = {}

    def note(name: str, start: int, end: int) -> None:
        if name not in found:
            found[name] = (start, _context_at(text, end, name))

    for ns in prof["namespaces"]:
        if not ns:
            continue
        for m in re.finditer(rf"\b{re.escape(ns)}::([A-Za-z_]\w*(?:<[^;<>()]{{0,60}}>)?)", text):
            note(f"{ns}::{m.group(1)}", m.start(), m.end())

    affix = "|".join(_CTOR_AFFIXES)
    for pre in prof["prefixes"]:
        if not pre:
            continue
        esc = re.escape(pre)
        if pre.endswith("_"):
            pattern = rf"\b(?P<sym>(?:(?:{affix})_)?{esc}\w+)\b"
        else:
            pattern = rf"\b(?P<sym>{esc}\w*)\s*\("
        for m in re.finditer(pattern, text):
            note(m.group("sym"), m.start("sym"), m.end("sym"))
    return found


def _harvest_generic(text: str, modname: str, kind: str) -> dict[str, int]:
    found: dict[str, int] = {}
    if kind == "py":
        for m in _PY_IMPORT_RE.finditer(text):
            mod = m.group(1) or (m.group(3) or "")
            if not mod.split(".")[0].strip().lower().startswith(modname[:6].lower()):
                continue
            names = m.group(2) or mod
            for n in re.split(r"[,\s]+", names):
                n = n.strip().strip("()")
                if n and n not in ("as", "import"):
                    found.setdefault(f"{mod.split('.')[0]}.{n}" if m.group(1) else n, m.start())
    elif kind == "rs":
        for m in _RS_USE_RE.finditer(text):
            path = m.group(1)
            if not path.lower().startswith(modname.replace("-", "_").lower()):
                continue
            if m.group(2):
                for n in re.split(r"\s*,\s*", m.group(2)):
                    if n.strip():
                        found.setdefault(f"{path}::{n.strip()}", m.start())
            else:
                found.setdefault(path, m.start())
    elif kind == "js":
        for m in _JS_IMPORT_RE.finditer(text):
            mod = m.group("mod") or m.group("mod2") or ""
            if not mod.startswith(modname):
                continue
            what = (m.group("what") or mod).strip()
            for n in re.split(r"[{},\s]+", what):
                if n and n not in ("as", "type"):
                    found.setdefault(n, m.start())
    return found


CXX_EXT = {".c", ".cc", ".cpp", ".cxx", ".h", ".hh", ".hpp", ".hxx", ".ipp", ".inl"}


def analyse(root: str, deps: list[Dep], max_sites: int = 12) -> None:
    """Fill in dep.consumed / dep.sites in place."""
    sources = _iter_sources(root)
    texts = {rel: _read(root, rel) for rel in sources}
    # Harvested from the code with comments, literals and include paths blanked
    # out; includes are still detected in the original, which is where they are.
    code = {
        rel: _blank_non_code(text)
        for rel, text in texts.items()
        if os.path.splitext(rel)[1] in CXX_EXT
    }

    for dep in deps:
        prof = _profile_for(dep)
        sites: list[Site] = []
        symbols: dict[str, Site] = {}

        for rel, text in texts.items():
            ext = os.path.splitext(rel)[1]
            hit_include = None

            if ext in CXX_EXT:
                for m in _INCLUDE_RE.finditer(text):
                    header = m.group(1)
                    if any(header.startswith(p) or header == p.rstrip("/") for p in prof["includes"]):
                        hit_include = Site(
                            path=rel, line=_line_of(text, m.start()),
                            symbol=f"#include <{header}>", context="include",
                        )
                        break
                if hit_include:
                    sites.append(hit_include)
                    for sym, (off, ctx) in _harvest_cxx(code[rel], prof).items():
                        symbols.setdefault(
                            sym, Site(path=rel, line=_line_of(text, off), symbol=sym, context=ctx)
                        )
            else:
                kind = {".py": "py", ".rs": "rs"}.get(ext, "js")
                if ext == ".go":
                    kind = "go"
                    for m in _GO_IMPORT_RE.finditer(text):
                        if m.group(1).startswith(dep.name):
                            hit = Site(path=rel, line=_line_of(text, m.start()),
                                       symbol=f'import "{m.group(1)}"', context="import")
                            sites.append(hit)
                            break
                    continue
                got = _harvest_generic(text, dep.name, kind)
                if got:
                    first = min(got.values())
                    sites.append(Site(path=rel, line=_line_of(text, first), context="import"))
                    for sym, off in got.items():
                        symbols.setdefault(
                            sym, Site(path=rel, line=_line_of(text, off), symbol=sym, context="use")
                        )

        dep.backend = "builtin"
        # Truncate by strength of evidence, then present alphabetically: a call
        # outranks a type mention, which outranks a constant.
        ranked = sorted(
            symbols, key=lambda s: (_CONTEXT_RANK.get(symbols[s].context, 3), s)
        )
        dropped = len(ranked) - MAX_CONSUMED
        dep.consumed = sorted(ranked[:MAX_CONSUMED])
        # Keep include sites plus a bounded sample of use sites.
        use_sites = [symbols[s] for s in dep.consumed][:max_sites]
        dep.sites = sites + [s for s in use_sites if s.key() not in {x.key() for x in sites}]
        if dropped > 0:
            dep.notes.append(
                f"{dropped} further consumed symbol(s) beyond the first "
                f"{MAX_CONSUMED} are not listed — anything upstream removes from "
                f"those will not be reported as affecting us"
            )
        if not dep.sites and dep.scope != "test":
            dep.notes.append(
                "no direct usage found in our sources — may be a transitive or "
                "link-only dependency, or the extractor missed its include style"
            )
        elif dep.sites and not dep.consumed:
            # The API-surface diff intersects against `consumed`, so an empty
            # list makes every upgrade look harmless. Say so instead: this is a
            # gap in our extraction, not a clean bill of health.
            dep.notes.append(
                "attributed to our sources by include/import but no symbols were "
                "extracted, so an API-surface diff has nothing to intersect "
                "against — unmeasured rather than unaffected"
            )


# ------------------------------------------------- matching upstream's own names


_IDENT = re.compile(r"[A-Za-z_]\w*")
# Below this, a bare declared name collides with ordinary identifiers too often
# to attribute to a library on the strength of the name alone.
MIN_DECLARED_LEN = 4


def _cxx_sources(root: str) -> list[str]:
    return [r for r in _iter_sources(root) if os.path.splitext(r)[1] in CXX_EXT]


def _attributed_files(root: str, dep: Dep) -> dict[str, str]:
    """Our C/C++ files that include this dependency -> their blanked code.

    Re-derived rather than read off `dep.sites`, because that list is capped for
    readability and loses its per-site context when round-tripped through
    CLAUDE_DEPS.md.
    """
    prof = _profile_for(dep)
    out: dict[str, str] = {}
    for rel in _cxx_sources(root):
        text = _read(root, rel)
        if any(
            any(h.startswith(p) or h == p.rstrip("/") for p in prof["includes"])
            for h in _INCLUDE_RE.findall(text)
        ):
            out[rel] = _blank_non_code(text)
    return out


def _also_declared_by_us(root: str, names: set[str]) -> set[str]:
    """Which of `names` our own sources also declare.

    Only the names that actually matched are adjudicated, and only the files
    that mention one of them are parsed. Extracting every declaration in the
    repository first is the obvious implementation and the wrong one: it costs
    ~14s on a 6k-file tree, per dependency, to answer a question about a few
    dozen names — nearly all of which no file of ours mentions at all.
    """
    from .. import apidiff

    probe = re.compile(r"\b(?:" + "|".join(sorted(re.escape(n) for n in names)) + r")\b")
    found: set[str] = set()
    for rel in _cxx_sources(root):
        if found == names:
            break
        text = _read(root, rel)
        if not probe.search(text):
            continue
        for decls in apidiff.declarations(text, rel).values():
            found |= {d.name for d in decls} & names
    return found


def absorb_declared(
    root: str, dep: Dep, declared: set[str], max_new_sites: int = 6
) -> list[str]:
    """Extend `dep.consumed` using the names the dependency itself declares.

    `_harvest_cxx` has to *guess* which identifiers belong to a library, from
    its package name. When the guess is wrong the harvest is empty even though
    the dependency is plainly used: libsndfile's package name yields the prefix
    `sndfile_`, its API is `sf_open` and `SF_INFO`, and nothing matches at all.

    Given the names the dependency actually declares, no guess is needed — and
    the API-surface diff has already read them out of its headers, so this costs
    no extra fetches. A name is consumed when upstream declares it and one of
    our files that includes the dependency mentions it.

    Two restrictions keep it from claiming our code as theirs. The caller passes
    only the **unqualified** part of upstream's surface — free functions, macros,
    C types (`apidiff.unqualified_names`) — because a namespaced C++ surface is
    already matched by `_harvest_cxx` and a bare `Node` is far too
    collision-prone to attribute on the strength of the name alone. And **names
    our own sources declare are excluded** here, so our `Node` stays ours even
    when upstream has one too.

    Returns the names added, and records where they came from — the provenance
    matters, because these are matched against a *declaration* rather than
    observed being called.
    """
    candidates = {
        name for name in declared
        if len(name) >= MIN_DECLARED_LEN and name not in set(dep.consumed)
    }
    if not candidates:
        return []

    attributed = _attributed_files(root, dep)
    if not attributed:
        return []

    mentioned: dict[str, str] = {}  # name -> the file it was first seen in
    for rel, code in sorted(attributed.items()):
        for name in sorted(set(_IDENT.findall(code)) & candidates):
            mentioned.setdefault(name, rel)
    if not mentioned:
        return []

    for name in _also_declared_by_us(root, set(mentioned)):
        mentioned.pop(name, None)

    added: dict[str, Site] = {}
    for name, rel in sorted(mentioned.items()):
        code = attributed[rel]
        m = re.search(rf"\b{re.escape(name)}\b", code)
        if not m:
            continue
        added[name] = Site(
            path=rel, line=_line_of(code, m.start()), symbol=name,
            context=_context_at(code, m.end(), name),
        )
    if not added:
        return []

    by_strength = sorted(added, key=lambda n: (_CONTEXT_RANK.get(added[n].context, 3), n))
    room = MAX_CONSUMED - len(dep.consumed)
    if len(by_strength) > max(room, 0):
        dropped = by_strength[max(room, 0):]
        by_strength = by_strength[:max(room, 0)]
        added = {n: added[n] for n in by_strength}
        dep.notes.append(
            f"{len(dropped)} symbol(s) matched in {dep.name}'s headers are not "
            f"listed — the profile caps a dependency at {MAX_CONSUMED} symbols, so "
            f"removals affecting those will not be reported"
        )
    if not added:
        return []

    dep.consumed = sorted(set(dep.consumed) | set(added))
    seen = {s.key() for s in dep.sites}
    room = max_new_sites
    for name in by_strength:
        if room <= 0:
            break
        if added[name].key() not in seen:
            dep.sites.append(added[name])
            seen.add(added[name].key())
            room -= 1
    dep.notes.append(
        f"{len(added)} symbol(s) matched against the declarations in "
        f"{dep.name}'s own headers rather than by name prefix: "
        + ", ".join(sorted(added)[:8])
        + (f" (+{len(added) - 8})" if len(added) > 8 else "")
    )
    return sorted(added)
