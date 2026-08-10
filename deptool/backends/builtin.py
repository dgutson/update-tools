"""Zero-dependency extractor for the consumed API surface.

The question that decides "is this upgrade worth it" is not *what changed
upstream* but *what changed upstream that we actually call*. This backend
answers the second half without any external tool:

  1. attribute every #include / import in our sources to a dependency,
  2. inside the files that import it, harvest the symbols that belong to it,
  3. report both, with file:line sites.

It is a heuristic, not a compiler. The LLM layer is expected to sanity-check
what comes out of here, which is why every symbol carries a site.
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
    """Include prefixes / namespaces / symbol prefixes to look for."""
    for key in (dep.name, dep.name.lower(), dep.name.replace("-", "_")):
        if key in KNOWN:
            return KNOWN[key]
    stem = re.sub(r"^(lib|python-|py|node-|rust-|go-)", "", dep.name.lower())
    stem = stem.replace("-", "_")
    alt = stem.replace("_", "")
    return {
        "includes": [f"{dep.name}/", f"{stem}/", f"{stem}.h", f"{alt}/", f"{alt}.h"],
        "namespaces": [stem, alt, dep.name.lower()],
        "prefixes": [f"{stem}_", f"{alt}_"],
    }


_INCLUDE_RE = re.compile(r'^\s*#\s*include\s*[<"]([^>"]+)[>"]', re.M)
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


def _harvest_cxx(text: str, prof: dict[str, list[str]]) -> dict[str, int]:
    """symbol -> first offset, for C/C++ sources."""
    found: dict[str, int] = {}
    for ns in prof["namespaces"]:
        if not ns:
            continue
        for m in re.finditer(rf"\b{re.escape(ns)}::([A-Za-z_]\w*(?:<[^;<>()]{{0,60}}>)?)", text):
            found.setdefault(f"{ns}::{m.group(1)}", m.start())
    for pre in prof["prefixes"]:
        if not pre:
            continue
        for m in re.finditer(rf"\b({re.escape(pre)}\w*)\s*\(", text):
            found.setdefault(m.group(1), m.start())
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


def analyse(root: str, deps: list[Dep], max_sites: int = 12) -> None:
    """Fill in dep.consumed / dep.sites in place."""
    sources = _iter_sources(root)
    texts = {rel: _read(root, rel) for rel in sources}

    for dep in deps:
        prof = _profile_for(dep)
        sites: list[Site] = []
        symbols: dict[str, Site] = {}

        for rel, text in texts.items():
            ext = os.path.splitext(rel)[1]
            hit_include = None

            if ext in {".c", ".cc", ".cpp", ".cxx", ".h", ".hh", ".hpp", ".hxx", ".ipp", ".inl"}:
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
                    for sym, off in _harvest_cxx(text, prof).items():
                        symbols.setdefault(
                            sym, Site(path=rel, line=_line_of(text, off), symbol=sym, context="use")
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
        dep.consumed = sorted(symbols)
        # Keep include sites plus a bounded sample of use sites.
        use_sites = [symbols[s] for s in sorted(symbols)][:max_sites]
        dep.sites = sites + [s for s in use_sites if s.key() not in {x.key() for x in sites}]
        if not dep.sites and dep.scope != "test":
            dep.notes.append(
                "no direct usage found in our sources — may be a transitive or "
                "link-only dependency, or the extractor missed its include style"
            )
