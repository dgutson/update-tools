"""What actually changed in a dependency's public API between two tags.

Release notes are the weakest evidence in this tool. They are often absent,
often marketing, and never written with *our* call sites in mind. But for a
C/C++ dependency the API surface is not a matter of opinion: it is in the
headers. Diffing the public headers between the tag we are pinned to and the
tag we are considering yields a *factual* list of what went away and what
changed shape — which can then be intersected with `dep.consumed` to answer the
only question that matters: does this upgrade break something we call?

That intersection is the point. A release with a terrifying changelog that
touches nothing we use is cheap; a release with a one-line changelog that drops
a function we call in twelve places is not, and no changelog tells you which one
you are looking at.

This is a regex extractor, not a compiler, and the output is labelled as such.
Its blind spots, all recorded in `notes`:

  * **Conditional compilation is ignored.** Declarations behind `#if` are read
    unconditionally, so a symbol available only on one platform looks
    unconditional, and one removed only on some platform looks removed.
  * **Out-of-line definitions and constructors are not tracked** — the pattern
    requires a return type before the name, which is also what keeps macro
    invocations and `if (...)` out of the results.
  * **Parameter lists containing parentheses** (function-pointer parameters,
    default arguments that call something) are not matched.

Two rules keep it from crying wolf, which matters more here than coverage:

  * **A removal must be absent from every header read at the target**, not
    merely from the header it used to live in. Upstream moving a declaration
    between headers is routine and is not a breaking change.
  * **When the header budget truncates the read, removals are provisional** and
    say so, because the symbol may sit in a header we never fetched. Symbols we
    actually consume get a second, targeted pass (`_confirm_absent`) before
    being reported, so the claims the report leads with are the checked ones.
"""

from __future__ import annotations

import dataclasses
import difflib
import re

from .model import Dep

# Header fetches are the cost: one per selected path per ref, so the budget is
# doubled in practice. `check` runs this across every dependency, so the default
# is small and prioritised (see `public_headers`); the `apidiff` verb raises it.
MAX_HEADERS = 24
# Extra target-ref fetches allowed to confirm that a symbol we consume really is
# gone, rather than merely absent from the headers we happened to read.
CONFIRM_BUDGET = 12
# Name similarity above which a removed/added pair sharing one signature is
# worth reporting as a possible rename. Inference, and labelled as such.
RENAME_RATIO = 0.6

HEADER_SUFFIXES = (".h", ".hpp", ".hh", ".hxx", ".h++", ".hp", ".inl", ".ipp")

# A public header that exists in the repository only as a build-time template.
# Observed on libsndfile 1.0.28, whose entire C API is `src/sndfile.h.in`: with
# only literal suffixes recognised, the diff read 20 *internal* headers, never
# saw `sf_open`, and reported 28 removals drawn from internal churn. Common
# enough to be a category rather than one library's quirk — autotools and CMake
# both generate headers this way.
_TEMPLATE_SUFFIXES = (".in", ".cmake", ".cmakein", ".meson")

# Directories whose headers are not the published surface. Removing something
# from `detail/` is upstream's business; removing it from `include/` is ours.
_PRIVATE_DIRS = {
    "test", "tests", "testing", "unittest", "unittests", "internal", "detail",
    "details", "private", "impl", "example", "examples", "sample", "samples",
    "benchmark", "benchmarks", "bench", "fuzz", "fuzzing", "third_party",
    "thirdparty", "vendor", "external", "extern", "deps", "_deps", "contrib",
    "doc", "docs", "build", "cmake", "tools", "scripts", "node_modules",
}
_PUBLIC_ROOTS = ("include/", "inc/", "api/", "public/")

# Tokens that can sit where a return type does but do not start a declaration.
_NOT_A_TYPE = {
    "return", "if", "while", "for", "switch", "do", "else", "case", "goto",
    "sizeof", "alignof", "new", "delete", "throw", "catch", "try", "using",
    "typedef", "template", "namespace", "class", "struct", "union", "enum",
    "public", "private", "protected", "friend", "operator", "static_assert",
    "decltype", "explicit", "typename", "and", "or", "not", "constexpr",
    "consteval", "requires", "co_return", "co_await", "co_yield", "extern",
    "static_cast", "dynamic_cast", "const_cast", "reinterpret_cast",
    "assert", "defined", "return_if", "noexcept",
}

# Parameter tokens that are types in their own right, so a single-token
# parameter like `f(int)` must not have its "name" stripped.
_BARE_TYPES = {
    "int", "char", "short", "long", "float", "double", "void", "bool",
    "unsigned", "signed", "size_t", "ssize_t", "ptrdiff_t", "wchar_t",
    "char8_t", "char16_t", "char32_t", "auto", "int8_t", "int16_t", "int32_t",
    "int64_t", "uint8_t", "uint16_t", "uint32_t", "uint64_t", "intptr_t",
    "uintptr_t", "FILE", "va_list", "nullptr_t",
}

# Parameter annotations that never denote a type. Deliberately a short, certain
# list: an all-caps token is just as likely to be a real typedef (`DWORD`,
# `HANDLE`), so guessing by shape would erase parameters instead of noise.
_TAIL_NOISE = {"override", "final"}

_ANNOTATION = re.compile(
    r"(?:__)?restrict(?:__)?|\w*_?RESTRICT|_In_\w*|_Out_\w*|_Inout_\w*|_Outptr_\w*",
    re.I,
)

_MACRO = re.compile(
    r"^[ \t]*#[ \t]*define[ \t]+(?P<name>\w+)(?P<args>\([^)\n]*\))?(?P<body>[^\n]*)", re.M
)
_PREPROC_LINE = re.compile(r"^[ \t]*#[^\n]*", re.M)
# `#ifndef X` / `#if !defined(X)` — the first half of an include guard.
_GUARD_TEST = re.compile(
    r"^[ \t]*#[ \t]*(?:ifndef[ \t]+(?P<a>\w+)|if[ \t]+!\s*defined\s*\(?[ \t]*(?P<b>\w+))",
    re.M,
)
_SCOPE_OPEN = re.compile(r"\b(?:namespace|class|struct|union)\s+(?P<name>[A-Za-z_][\w:]*)")

_TYPE_DECL = re.compile(
    r"\b(?P<kw>class|struct|union|enum[ \t]+class|enum[ \t]+struct|enum)\s+"
    r"(?:(?:alignas|__attribute__|__declspec)\s*\([^)]*\)\s*)?"
    r"(?P<name>[A-Za-z_]\w*)\b\s*(?::[^;{]*)?\s*(?P<end>[;{])"
)

_FUNC_DECL = re.compile(
    r"(?<![\w:.])(?P<ret>[A-Za-z_]\w*(?:::\w+)*)"      # return type, tail-most
    r"(?:\s*<[^<>;{}()]*>)?"                            # its template args
    r"(?P<ptr>[\s*&]+)"                                 # separator / pointer
    r"(?P<name>[A-Za-z_]\w*)\s*"                        # the function name
    r"\((?P<params>[^;{}()]*)\)"                        # params, no nesting
    r"(?P<tail>(?:\s*(?:const|volatile|noexcept|override|final))*)"
    r"\s*(?:->[^;{}]+)?\s*(?:=\s*(?:0|delete|default))?\s*[;{]"
)

# Enumerator names. `enum Foo { A, B = 2 }` publishes A and B as surely as a
# function, and removing or renaming one is a hard compile break for a caller —
# but the extractor only recorded the enum's own tag, so every constant a project
# consumed came back `not_located`: unchecked forever, however many headers were
# read. Values and ordering are still out of scope (an ABI concern, not an API
# one), which is why an enumerator's signature is a constant.
_ENUM_BODY = re.compile(
    r"\benum\b(?:[ \t]+(?:class|struct))?[ \t]*(?P<tag>[A-Za-z_]\w*)?"
    # The brace is as often on its own line as not, so only whitespace may
    # separate it from the tag — a `;` instead means a forward declaration.
    r"(?:\s*:[^{;]{0,80})?\s*\{(?P<body>[^{}]*)\}"
)
_ENUMERATOR = re.compile(r"(?:^|,)\s*(?P<name>[A-Za-z_]\w*)")

_USING_ALIAS = re.compile(r"\busing\s+(?P<name>[A-Za-z_]\w*)\s*=\s*(?P<rhs>[^;{}]+);")
_TYPEDEF = re.compile(r"\btypedef\s+(?P<rhs>[^;{}()]*?)\b(?P<name>[A-Za-z_]\w*)\s*;")

# The tag of an incomplete type, which a consumer of the alias can neither see
# nor spell. libsndfile 1.2.2 changed `typedef struct SNDFILE_tag SNDFILE` to
# `typedef struct sf_private_tag SNDFILE`; reporting that as a signature change
# on `SNDFILE` — which every caller of the library uses — would send someone to
# fix call sites that compile perfectly. Renaming a tag a consumer *does* spell
# directly still shows up, as a removal of that tag's own declaration.
_OPAQUE_TAG = re.compile(r"\b(struct|union|enum)\s+[A-Za-z_]\w*")


@dataclasses.dataclass
class Decl:
    """One declaration found in a public header."""

    name: str  # unqualified, which is what matches a consumed symbol
    qualified: str  # namespace/class-qualified where we could work it out
    kind: str  # function | macro | type | alias
    signature: str
    header: str = ""
    line: int = 0

    def where(self) -> str:
        return f"{self.header}:{self.line}" if self.header else ""


# ------------------------------------------------------------------ extraction


def _strip_comments(text: str) -> str:
    """Blank out comments and string literals, preserving every newline.

    Line numbers are computed from this output, so the newline count must
    survive exactly; the characters themselves need not.
    """
    out: list[str] = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c == "/" and i + 1 < n:
            if text[i + 1] == "/":
                j = text.find("\n", i)
                if j == -1:
                    break
                i = j
                continue
            if text[i + 1] == "*":
                j = text.find("*/", i + 2)
                end = n if j == -1 else j + 2
                out.append(re.sub(r"[^\n]", " ", text[i:end]))
                i = end
                continue
        if c in "\"'":
            j = i + 1
            while j < n and text[j] != c and text[j] != "\n":
                if text[j] == "\\":
                    j += 1
                j += 1
            end = min(j + 1, n)
            out.append(" " * (end - i))
            i = end
            continue
        out.append(c)
        i += 1
    return "".join(out)


def _scope_map(text: str) -> list[str]:
    """Line number -> enclosing `a::b::C` scope, for qualifying declarations.

    Brace counting is per line, so a class opened and closed on one line
    attributes its members to the outer scope. That costs a qualified name, not
    a match: the index is keyed by unqualified name as well.
    """
    lines = text.split("\n")
    out = [""] * (len(lines) + 2)
    stack: list[tuple[str, int]] = []
    depth = 0
    for i, line in enumerate(lines, 1):
        out[i] = "::".join(name for name, _ in stack)
        for m in _SCOPE_OPEN.finditer(line):
            brace = line.find("{", m.end())
            if brace == -1:
                continue
            opened = line.count("{", 0, brace) - line.count("}", 0, brace)
            stack.append((m.group("name"), depth + opened + 1))
        depth += line.count("{") - line.count("}")
        while stack and stack[-1][1] > depth:
            stack.pop()
    return out


def _norm_params(params: str) -> str:
    """Parameter *types*, names dropped, so renaming an argument is not a break."""
    out = []
    for part in params.split(","):
        part = re.sub(r"=.*$", "", part).strip()
        part = re.sub(r"\s+", " ", part)
        if not part or part == "void":
            continue
        toks = part.replace("*", " * ").replace("&", " & ").split()
        if (
            len(toks) > 1
            and re.fullmatch(r"[A-Za-z_]\w*", toks[-1])
            and toks[-1] not in _BARE_TYPES
        ):
            toks = toks[:-1]
        # Annotations are not part of the type. Observed on FluidSynth, where
        # adding FLUID_RESTRICT to two parameters read as a signature change.
        toks = [t for t in toks if not _ANNOTATION.fullmatch(t)] or toks
        out.append(" ".join(toks))
    return "(" + ", ".join(out) + ")"


def _norm_alias(rhs: str) -> str:
    """An alias's target type, with the tag name of an opaque type erased.

    Pointer and `const` decoration is left intact, so `struct A` -> `struct A *`
    is still the change it is.
    """
    return _OPAQUE_TAG.sub(r"\1 <opaque>", " ".join(rhs.split()))


def _line_of(text: str, idx: int) -> int:
    return text.count("\n", 0, idx) + 1


def declarations(text: str, header: str = "") -> dict[str, list[Decl]]:
    """Public declarations in one header, keyed by qualified name.

    A list per key because overloads share a name; the diff compares the *set*
    of signatures under a name, so adding an overload is visible and renaming a
    parameter is not.
    """
    body = _strip_comments(text)
    scopes = _scope_map(body)
    found: dict[str, list[Decl]] = {}

    def add(decl: Decl) -> None:
        bucket = found.setdefault(decl.qualified, [])
        if not any(d.signature == decl.signature for d in bucket):
            bucket.append(decl)

    # Include guards are renamed freely and are nobody's API. A `#define` of a
    # name the file also tests with `#ifndef` is a guard only when it defines
    # nothing — `#ifndef X` / `#define X 1` is a configuration knob, which is.
    guarded = {m.group("a") or m.group("b") for m in _GUARD_TEST.finditer(body)}
    for m in _MACRO.finditer(body):
        name = m.group("name")
        if name in guarded and not m.group("args") and not m.group("body").strip():
            continue
        args = _norm_params(m.group("args")[1:-1]) if m.group("args") else ""
        add(Decl(name, name, "macro", args, header, _line_of(body, m.start())))

    # Macro *bodies* are a rich source of spurious function declarations, and
    # #include / #if lines add nothing. Both are gone by this point.
    body = _PREPROC_LINE.sub(lambda m: " " * len(m.group(0)), body)

    for m in _FUNC_DECL.finditer(body):
        ret, name = m.group("ret"), m.group("name")
        if ret in _NOT_A_TYPE or name in _NOT_A_TYPE:
            continue
        line = _line_of(body, m.start())
        scope = scopes[line] if line < len(scopes) else ""
        # `const` and `noexcept` are part of the callable's type; `override` and
        # `final` are notes to the compiler about this declaration and change
        # nothing for a caller. Keeping them made every yaml-cpp virtual that
        # gained an `override` between 0.6.3 and 0.8.0 read as re-signatured.
        tail = " ".join(t for t in m.group("tail").split() if t not in _TAIL_NOISE)
        sig = f"{ret}{m.group('ptr').strip() or ' '}{_norm_params(m.group('params'))}"
        add(Decl(
            name, f"{scope}::{name}" if scope else name, "function",
            (sig + " " + tail).strip(), header, line,
        ))

    for m in _TYPE_DECL.finditer(body):
        name = m.group("name")
        line = _line_of(body, m.start())
        scope = scopes[line] if line < len(scopes) else ""
        kind = " ".join(m.group("kw").split())
        add(Decl(name, f"{scope}::{name}" if scope else name, "type", kind, header, line))

    for m in _ENUM_BODY.finditer(body):
        base = m.start("body")
        for em in _ENUMERATOR.finditer(m.group("body")):
            name = em.group("name")
            if name in _NOT_A_TYPE:
                continue
            line = _line_of(body, base + em.start("name"))
            scope = scopes[line] if line < len(scopes) else ""
            add(Decl(
                name, f"{scope}::{name}" if scope else name, "enumerator",
                "enumerator", header, line,
            ))

    for pattern, kind in ((_USING_ALIAS, "alias"), (_TYPEDEF, "alias")):
        for m in pattern.finditer(body):
            name = m.group("name")
            if name in _NOT_A_TYPE:
                continue
            line = _line_of(body, m.start())
            scope = scopes[line] if line < len(scopes) else ""
            rhs = _norm_alias(m.group("rhs"))[:80]
            add(Decl(name, f"{scope}::{name}" if scope else name, kind, rhs, header, line))

    return found


# -------------------------------------------------------------- header choice


def _is_private(path: str) -> bool:
    return any(part.lower() in _PRIVATE_DIRS for part in path.split("/")[:-1])


def as_header_path(path: str) -> str:
    """`src/sndfile.h.in` -> `src/sndfile.h`; anything else unchanged.

    Everything downstream — the suffix test, the include-hint match, the stem
    used for ranking — wants the header this file *becomes*, not its filename.
    """
    for suffix in _TEMPLATE_SUFFIXES:
        if path.endswith(suffix):
            return path[: -len(suffix)]
    return path


def is_header(path: str) -> bool:
    return as_header_path(path).endswith(HEADER_SUFFIXES)


def include_hints(dep: Dep) -> list[str]:
    """The header paths our own sources actually `#include`.

    A dependency's surface may be a hundred headers; the ones we include are
    where our breakage would come from, so they are read first.
    """
    hints = []
    for site in dep.sites:
        m = re.search(r'#include\s*[<"]([^>"]+)[>"]', site.symbol or "")
        if m and m.group(1) not in hints:
            hints.append(m.group(1))
    return hints


def symbol_stems(consumed: list[str]) -> set[str]:
    """Header-name fragments implied by the symbols we consume.

    C libraries name a header after its subsystem and prefix its symbols with
    the same word: `fluid_synth_noteon` lives in `synth.h`, `fluid_ramsfont_t`
    in `ramsfont.h`. Matching the two is how a header carrying something we
    actually use gets read before one that does not — which matters because the
    budget is smaller than some libraries.
    """
    return {
        tok.lower()
        for sym in consumed
        for tok in re.split(r"[_:.]+|(?<=[a-z0-9])(?=[A-Z])", sym)
        if len(tok) >= 4
    }


def public_headers(
    paths: list[str],
    hints: tuple[str, ...] | list[str] = (),
    stems: set[str] | None = None,
) -> list[str]:
    """Candidate public headers from a repo tree listing, best first.

    A project with an `include/` tree has already answered this question for us,
    so when one exists nothing outside it counts. Observed on FluidSynth: taking
    every non-private header found 66 of them and turned internal churn
    (`fluid_handle_reverbpreset`, the SDL2 -> SDL3 driver rename) into 33
    "removals" that no consumer could ever have called. Its actual surface is
    the 15 headers under `include/`. Only projects with no public root at all
    fall back to the whole tree.
    """
    headers = [p for p in paths if is_header(p) and not _is_private(p)]
    rooted = [p for p in headers if p.startswith(_PUBLIC_ROOTS)]
    picked = rooted or headers
    picked.sort(key=lambda p: _header_rank(p, tuple(hints), stems or set()))
    return picked


def _header_rank(path: str, hints: tuple[str, ...], stems: set[str]) -> tuple[int, int, str]:
    # Ranked as the header it becomes, so `#include <sndfile.h>` reaches
    # `src/sndfile.h.in` — which is bucket 0, not the alphabetical also-ran it
    # was when the hint could not match the template's filename.
    path = as_header_path(path)
    stem = path.rsplit("/", 1)[-1].rsplit(".", 1)[0].lower()
    if any(path == h or path.endswith("/" + h) for h in hints):
        bucket = 0
    elif stem in stems or any(s in stem or stem in s for s in stems if len(stem) >= 4):
        bucket = 1
    elif path.startswith(_PUBLIC_ROOTS):
        bucket = 2
    elif "/" not in path:
        bucket = 3
    else:
        bucket = 4
    return (bucket, path.count("/"), path)


# ------------------------------------------------------------------- diffing


MAX_SIGS = 6


def _cap(sigs: list[str]) -> list[str]:
    if len(sigs) <= MAX_SIGS:
        return sigs
    return sigs[:MAX_SIGS] + [f"(+{len(sigs) - MAX_SIGS} more)"]


def _signatures(index: dict[str, list[Decl]]) -> dict[str, set[str]]:
    return {key: {d.signature for d in decls} for key, decls in index.items()}


def _names(index: dict[str, list[Decl]]) -> set[str]:
    return {d.name for decls in index.values() for d in decls}


def unqualified_names(index: dict[str, list[Decl]]) -> set[str]:
    """Declared names that sit in no namespace or class — upstream's C surface.

    This is the half of the surface safe to match against our sources *by name
    alone* (see `backends.builtin.absorb_declared`): a free function, a macro or
    a C typedef is spelled the same at the use site as in the header, whereas a
    member of a namespace is spelled `ns::Name` there and matched already.

    Keywords and builtin type names are dropped. Upstream typedef'ing `size_t`
    is real, but "our code mentions `size_t`" is not evidence of anything.
    """
    return {
        d.name for decls in index.values() for d in decls
        if d.qualified == d.name and d.name not in _NOT_A_TYPE and d.name not in _BARE_TYPES
    }


def diff(before: dict[str, list[Decl]], after: dict[str, list[Decl]]) -> dict:
    """Compare two header-set indexes.

    `removed` requires the unqualified name to be absent from the whole target
    index, not just from its old key — upstream moving a declaration into
    another header, or into a namespace, is not a removal.
    """
    before_sigs, after_sigs = _signatures(before), _signatures(after)
    after_names = _names(after)

    removed: list[Decl] = []
    changed: list[dict] = []
    for key, sigs in before_sigs.items():
        decl = before[key][0]
        if key in after_sigs:
            if sigs != after_sigs[key]:
                # A heavily overloaded name (yaml-cpp's `convert::encode` has 14)
                # would otherwise spend a screen, and a model's context, listing
                # the overloads that did *not* move. Show what differs.
                changed.append({
                    "name": decl.name,
                    "qualified": key,
                    "kind": decl.kind,
                    "before": _cap(sorted(sigs - after_sigs[key])),
                    "after": _cap(sorted(after_sigs[key] - sigs)),
                    "overloads": [len(sigs), len(after_sigs[key])],
                    "header": decl.header,
                    "line": decl.line,
                })
        elif decl.name not in after_names:
            removed.append(decl)

    added = [d for key, decls in after.items() if key not in before_sigs for d in decls]
    return {
        "removed": removed,
        "changed": changed,
        "added": added,
        "before_count": sum(len(v) for v in before.values()),
        "after_count": sum(len(v) for v in after.values()),
    }


def likely_renames(removed: list[Decl], added: list[Decl]) -> list[dict]:
    """Removed/added pairs sharing a signature and a similar name.

    Inference, not evidence — a rename cannot be proved from two header
    snapshots — so callers label it. Still worth surfacing: "gone" and "gone,
    but it is called this now" are very different upgrade costs.
    """
    out = []
    by_sig: dict[str, list[Decl]] = {}
    for d in added:
        if d.kind == "function" and d.signature:
            by_sig.setdefault(d.signature, []).append(d)
    for gone in removed:
        if gone.kind != "function":
            continue
        best, best_ratio = None, 0.0
        for cand in by_sig.get(gone.signature, []):
            ratio = difflib.SequenceMatcher(None, gone.name, cand.name).ratio()
            if ratio > best_ratio:
                best, best_ratio = cand, ratio
        if best and best_ratio >= RENAME_RATIO:
            out.append({
                "from": gone.name,
                "to": best.name,
                "signature": gone.signature,
                "similarity": round(best_ratio, 2),
                "confidence": "inferred",
                "header": best.header,
            })
    return out


# --------------------------------------------------------------- intersection


def _match_keys(symbol: str) -> set[str]:
    """The spellings a consumed symbol could appear under in a header index."""
    bare = symbol.split("::")[-1].split(".")[-1]
    return {symbol, bare}


def _sites_for(dep: Dep, symbol: str) -> list[str]:
    bare = symbol.split("::")[-1].split(".")[-1]
    out = []
    for site in dep.sites:
        sym = site.symbol or ""
        if sym == symbol or sym.split("::")[-1] == bare:
            where = f"{site.path}:{site.line}"
            if where not in out:
                out.append(where)
    return out


def affected(dep: Dep, result: dict) -> list[dict]:
    """The intersection: changes to things this project actually consumes."""
    by_removed: dict[str, Decl] = {}
    for decl in result["removed"]:
        by_removed.setdefault(decl.name, decl)
        by_removed.setdefault(decl.qualified, decl)
    by_changed: dict[str, dict] = {}
    for entry in result["changed"]:
        by_changed.setdefault(entry["name"], entry)
        by_changed.setdefault(entry["qualified"], entry)

    hits = []
    for symbol in dep.consumed:
        keys = _match_keys(symbol)
        gone = next((by_removed[k] for k in keys if k in by_removed), None)
        if gone:
            hits.append({
                "symbol": symbol,
                "change": "removed",
                "kind": gone.kind,
                "was": gone.signature,
                "declared_at": gone.where(),
                "sites": _sites_for(dep, symbol),
            })
            continue
        moved = next((by_changed[k] for k in keys if k in by_changed), None)
        if moved:
            hits.append({
                "symbol": symbol,
                "change": "signature",
                "kind": moved["kind"],
                "before": moved["before"],
                "after": moved["after"],
                "declared_at": f"{moved['header']}:{moved['line']}" if moved["header"] else "",
                "sites": _sites_for(dep, symbol),
            })
    # Removals first: they cannot be papered over at the call site.
    hits.sort(key=lambda h: (h["change"] != "removed", h["symbol"]))
    return hits


# -------------------------------------------------------------- orchestration


def _confirm_absent(
    names: set[str], repo: str, ref: str, unread: list[str], fetch, budget: int
) -> tuple[set[str], int]:
    """Hunt for `names` in headers the budget made us skip.

    Only ever called for symbols we consume, which is a handful, so paying a few
    more fetches to avoid reporting a phantom removal is worth it. Returns the
    names actually found (i.e. *not* removed) and how many fetches were spent.
    """
    still_missing = set(names)
    spent = 0
    for path in unread:
        if not still_missing or spent >= budget:
            break
        text = fetch(repo, path, ref)
        spent += 1
        if not text:
            continue
        index = declarations(text, path)
        present = _names(index)
        still_missing -= present
    return names - still_missing, spent


def _unlocated(dep, seen, repo, to_ref, unread, fetch, notes) -> list[str]:
    """Consumed symbols the diff never saw, after searching the skipped headers.

    Not a finding — a coverage gap, and reported as one. A symbol can land here
    because it is a macro from a header we did not read, because it belongs to
    another library and was misattributed, or because our own extraction is a
    heuristic. What matters is that it is *stated* rather than folded into the
    unaffected majority.
    """
    missing = [s for s in dep.consumed if not (_match_keys(s) & seen)]
    if not missing or not unread:
        return missing
    still = {s.split("::")[-1] for s in missing}
    found, _ = _confirm_absent(still, repo, to_ref, unread, fetch, CONFIRM_BUDGET)
    if found:
        notes.append(
            f"{len(found)} consumed symbol(s) were located in headers outside the "
            f"budget and still exist at the target: " + ", ".join(sorted(found))
        )
    return [s for s in missing if s.split("::")[-1] not in found]


def surface_change(
    dep: Dep,
    from_version: str,
    to_version: str,
    versions: list[dict] | None = None,
    fetch=None,
    tree=None,
    max_headers: int = MAX_HEADERS,
    root: str = "",
) -> dict:
    """Diff `dep`'s public headers between two versions, intersected with use.

    Degrades to `resolved: False` with a reason rather than raising: a
    dependency whose headers we cannot read must be reported as unread, not
    reported as unchanged.

    Given `root`, the headers read here are also used to *widen* `dep.consumed`
    before the intersection: any name upstream declares at the pinned version and
    our sources mention is consumed, whatever prefix the package name implied.
    That mutates `dep` deliberately — the tool has learnt something about our own
    surface, and the caller reports the widened list rather than the guess.
    """
    from . import upstream as up

    fetch = fetch or up.fetch_file
    tree = tree or up.list_files

    result: dict = {
        "resolved": False,
        "reason": "",
        "repo": dep.upstream.ref,
        "from_version": from_version,
        "to_version": to_version,
        "from_ref": "",
        "to_ref": "",
        "headers_read": 0,
        "headers_available": 0,
        "truncated": False,
        "symbols_before": 0,
        "symbols_after": 0,
        "removed": [],
        "changed": [],
        "added_count": 0,
        "affects_us": [],
        "not_located": [],
        # Symbols added to dep.consumed by matching upstream's own declarations.
        "consumed_added": [],
        "likely_renames": [],
        "removed_headers": [],
        # Migration-guide paths spotted in the target's tree. Carried out of
        # here because listing that tree is a paid API call and this function
        # has already made it; `upstream.change_prose` fetches them.
        "doc_candidates": [],
        "notes": [],
    }

    repo = dep.diff_repo()
    if not repo:
        result["reason"] = (
            f"no readable upstream repository (upstream is "
            f"{dep.upstream.kind or 'unknown'}:{dep.upstream.ref or '?'}) — the API "
            f"surface cannot be diffed, so the release notes are the only evidence"
        )
        return result
    result["repo"] = repo
    if not (from_version and to_version):
        result["reason"] = "need both a current and a target version to diff"
        return result
    from_ref, from_tree = _first_readable_ref(repo, from_version, versions, tree)
    to_ref, to_tree = _first_readable_ref(repo, to_version, versions, tree)
    if not (from_ref and to_ref):
        missing = from_version if not from_ref else to_version
        tried = ", ".join(up.tag_forms(missing, versions))
        result["reason"] = (
            f"could not list {repo} at {missing} (tried {tried}) — the tag may not "
            f"exist under a name we tried, or the repository is not readable from here"
        )
        return result
    result["from_ref"], result["to_ref"] = from_ref, to_ref
    result["doc_candidates"] = up.migration_doc_paths(to_tree, to_version, from_version)

    hints = include_hints(dep)
    stems = symbol_stems(dep.consumed)
    before_paths = public_headers(from_tree, hints, stems)
    after_paths = public_headers(to_tree, hints, stems)
    if not before_paths and not after_paths:
        result["reason"] = f"{repo} publishes no headers we recognise as public"
        return result

    result["removed_headers"] = [p for p in before_paths if p not in set(after_paths)][:10]

    # Diff the same paths on both sides; a path present in only one ref
    # contributes through its declarations, not by existing.
    ordered = before_paths + [p for p in after_paths if p not in set(before_paths)]
    selected = ordered[:max_headers]
    unread = ordered[max_headers:]
    result["headers_available"] = len(ordered)
    result["headers_read"] = len(selected)
    result["truncated"] = bool(unread)

    before: dict[str, list[Decl]] = {}
    after: dict[str, list[Decl]] = {}
    read_any = False
    for path in selected:
        for ref, index in ((from_ref, before), (to_ref, after)):
            text = fetch(repo, path, ref)
            if not text:
                continue
            read_any = True
            for key, decls in declarations(text, path).items():
                bucket = index.setdefault(key, [])
                for decl in decls:
                    if not any(d.signature == decl.signature for d in bucket):
                        bucket.append(decl)
    if not read_any:
        result["reason"] = f"selected {len(selected)} header(s) but none could be fetched"
        return result

    raw = diff(before, after)
    result["resolved"] = True
    result["added_count"] = len(raw["added"])
    result["symbols_before"] = raw["before_count"]
    result["symbols_after"] = raw["after_count"]

    # Widen what we know we consume before intersecting, using the declarations
    # just read. Header *ranking* above still ran on the pre-widened list, so a
    # header carrying only newly-matched symbols can still fall outside the
    # budget — `_unlocated` is what keeps that visible.
    if root:
        from .backends import builtin

        gained = builtin.absorb_declared(root, dep, unqualified_names(before))
        result["consumed_added"] = gained
        if gained:
            result["notes"].append(
                f"widened our consumed surface by {len(gained)} symbol(s) matched "
                f"against {repo}'s own declarations at {from_ref}, which the "
                f"name-prefix harvest had missed: " + ", ".join(gained[:8])
                + (f" (+{len(gained) - 8})" if len(gained) > 8 else "")
            )

    hits = affected(dep, raw)

    # A removal is confirmed when the symbol is known to be in none of the
    # target's public headers. Reading all of them proves that outright.
    checked = not unread

    # Anything we consume and believe is gone gets checked against the headers
    # the budget skipped, before it reaches the report.
    if unread and hits:
        suspect = {h["symbol"].split("::")[-1] for h in hits if h["change"] == "removed"}
        if suspect:
            found, spent = _confirm_absent(
                suspect, repo, to_ref, unread, fetch, CONFIRM_BUDGET
            )
            if found:
                # Proven present at the target, so it is not a removal at all —
                # drop it from the raw list too, or the summary counts below
                # would go on reporting a removal we just disproved.
                hits = [
                    h for h in hits
                    if not (h["change"] == "removed"
                            and h["symbol"].split("::")[-1] in found)
                ]
                raw["removed"] = [d for d in raw["removed"] if d.name not in found]
                result["notes"].append(
                    f"{len(found)} symbol(s) that looked removed were found in headers "
                    f"outside the first {max_headers} — moved, not dropped: "
                    + ", ".join(sorted(found))
                )
            checked = True
            if spent:
                result["notes"].append(
                    f"spent {spent} extra fetch(es) confirming that the symbol(s) we "
                    f"consume really are gone from the headers the budget skipped"
                )

    for h in hits:
        if h["change"] == "removed":
            h["confirmed"] = checked
    result["affects_us"] = hits
    # How many symbols the intersection actually had to work with, after any
    # widening above. Zero makes "nothing we consume changed" unsayable.
    result["consumed_count"] = len(dep.consumed)

    # A symbol we consume that appears in neither ref's read headers is not
    # evidence of anything — but silently counting it as "unaffected" is how a
    # truncated read turns into a clean bill of health. Observed: fluidsynth's
    # `ramsfont.h` fell outside a 12-header budget, so the removal of a type we
    # consume was reported as "nothing we consume was removed".
    seen = _names(before) | _names(after)
    result["not_located"] = _unlocated(
        dep, seen, repo, to_ref, unread, fetch, result["notes"]
    )
    result["removed"] = [dataclasses.asdict(d) for d in raw["removed"][:40]]
    result["changed"] = raw["changed"][:40]
    result["likely_renames"] = likely_renames(raw["removed"], raw["added"])

    result["notes"].append(
        "extracted by regex, not by a compiler: declarations behind #if are read "
        "unconditionally, and constructors, out-of-line definitions and parameter "
        "lists containing parentheses are not matched"
    )
    if dep.patched:
        # The diff read upstream's headers, and upstream is not what gets built
        # here. Saying so on the diff itself matters more than saying it in the
        # finding, because this is the output that reads as factual.
        result["notes"].append(
            f"this diff is against {repo} as published, which is not what ships: "
            + "; ".join(dep.patched)
        )
    if result["truncated"]:
        result["notes"].append(
            f"read {len(selected)} of {len(ordered)} public headers (budget), chosen "
            f"nearest to what we include — removals below that we did not confirm "
            f"may simply live in a header we did not read"
        )
    if len(raw["removed"]) > 40:
        result["notes"].append(
            f"{len(raw['removed']) - 40} further removed declaration(s) not shown"
        )
    if not dep.consumed:
        # `affected` iterates dep.consumed, so with nothing there the
        # intersection is empty for want of an input rather than because the
        # upgrade is safe. Anything downstream reporting "nothing we consume
        # changed" would be stating a fact about our extractor, not about us.
        result["notes"].append(
            "we extracted no symbols consumed from this dependency, so the "
            "intersection below is vacuous: the removals are upstream facts, but "
            "whether any of them affects us is unmeasured, not measured as safe"
        )
    elif not hits and (raw["removed"] or raw["changed"]):
        covered = len(dep.consumed) - len(result["not_located"])
        result["notes"].append(
            f"{len(raw['removed'])} removal(s) and {len(raw['changed'])} signature "
            f"change(s) upstream, none of them in the {covered} of "
            f"{len(dep.consumed)} symbol(s) we consume that these headers declare"
        )
    if result["not_located"]:
        result["notes"].append(
            f"{len(result['not_located'])} consumed symbol(s) were not found in any "
            f"header read at either version, so this diff says nothing about them: "
            + ", ".join(result["not_located"][:8])
        )
    return result


def _first_readable_ref(repo, version, versions, tree) -> tuple[str, list[str]]:
    """The first candidate tag spelling whose tree we can list, and that tree.

    The listing comes back with the ref because it is a paid API call and the
    caller needs both; asking twice doubled the request count per dependency.
    """
    from . import upstream as up

    for ref in up.tag_forms(version, versions):
        paths = tree(repo, ref)
        if paths:
            return ref, paths
    return "", []
