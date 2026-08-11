"""Work out what a coupled pin must become when its dependency is bumped.

Detection ships in `cmake.py`: a version-valued CACHE variable sitting beside a
dependency is almost always a second pin on the same thing — a prebuilt native
engine, an ABI level, a protocol version. Detecting it only gets you a warning.
This module answers the next question, which is the one that actually unblocks
an upgrade: *what value does the new version need?*

The mechanism is that the coupling is not a secret. A consumer writes

    set(HEGEL_LIBHEGEL_VERSION 0.29.0 CACHE STRING
        "libhegel version required by Hegel C++ v0.7.4")

because upstream declared that requirement somewhere in its own build files.
So read those files *at the tag being considered* and take the value from
there. That is evidence, not inference.

Two rules keep it honest:

  * **Self-check.** Resolve the same variable at the version currently pinned.
    If our extraction reproduces the value already in the repo, the mechanism
    demonstrably works for this dependency and the target answer can be
    trusted. If it disagrees, something is wrong — either the pin is
    deliberate or we are reading the wrong variable — and we refuse to edit.
  * **Never guess.** A companion we cannot resolve is reported as unresolved
    with the refs and files searched. `apply` then refuses rather than handing
    over a build that configures cleanly and dies at link time.
"""

from __future__ import annotations

import re

from .model import CompanionPin, Dep

# Read at most this many of a dependency's build files per ref. The answer is
# in the root CMakeLists.txt in the common case; the rest is for projects that
# keep their pins in cmake/ modules.
MAX_FILES = 8
MAX_DEPTH = 3

# Files worth reading for "which companion version does this release require".
_NAMES = {
    "CMakeLists.txt", "CMakePresets.json", "vcpkg.json", "conanfile.txt",
    "conanfile.py", "package.json", "Cargo.toml", "pyproject.toml",
    "meson.build",
}
_SUFFIXES = (".cmake", ".cmake.in")

# Variable names too generic to search for upstream: in the dependency's own
# repo these describe the dependency's own version, not a companion.
_GENERIC = {
    "VERSION", "PROJECT_VERSION", "CMAKE_PROJECT_VERSION", "PACKAGE_VERSION",
    "SOVERSION", "SO_VERSION", "LIB_VERSION",
}

# A version in prose is usually at the end of a sentence, so the trailing guard
# must reject `0.31.41` and `0.31.0.2` while still matching `0.31.0.`
_VERSION_LITERAL = r"(?<![\w.])v?(\d+\.\d+(?:\.\d+)?)(?!\w)(?!\.\d)"


def var_candidates(var: str, dep_name: str) -> list[str]:
    """Spellings of a pin to look for upstream, strongest first.

    A consumer namespaces the variable by the dependency it belongs to
    (`HEGEL_LIBHEGEL_VERSION`) while the dependency itself declares it plain
    (`LIBHEGEL_VERSION`). Drop leading tokens progressively — but never down to
    something so generic it would match the dependency's own version.
    """
    toks = [t for t in var.split("_") if t]
    own = f"{dep_name.upper().replace('-', '_')}_VERSION"
    out: list[str] = []
    for i in range(len(toks)):
        if len(toks) - i < 2:
            break
        cand = "_".join(toks[i:])
        if cand.upper() in _GENERIC or cand.upper() == own:
            continue
        if cand not in out:
            out.append(cand)
    return out or [var]


def subject_of(var: str, dep_name: str) -> str:
    """The thing a pin is about, for searching prose.

    `HEGEL_LIBHEGEL_VERSION` on dependency `hegel` is about `libhegel`.
    """
    toks = [t for t in var.split("_") if t]
    if toks and toks[-1].upper() == "VERSION":
        toks = toks[:-1]
    key = dep_name.lower().replace("-", "").replace("_", "")
    if len(toks) > 1 and toks[0].lower() == key:
        toks = toks[1:]
    return "_".join(toks)


def candidate_files(paths: list[str]) -> list[str]:
    """Pick and order the build files worth reading."""
    picked = []
    for p in paths:
        if p.count("/") > MAX_DEPTH:
            continue
        name = p.rsplit("/", 1)[-1]
        if name in _NAMES or p.endswith(_SUFFIXES):
            picked.append(p)
    picked.sort(key=_rank)
    return picked[:MAX_FILES] or ["CMakeLists.txt"]


def _rank(path: str) -> tuple[int, int, str]:
    name = path.rsplit("/", 1)[-1]
    depth = path.count("/")
    if path == "CMakeLists.txt":
        bucket = 0
    elif path.endswith(_SUFFIXES):
        bucket = 1
    elif name == "CMakeLists.txt":
        bucket = 2
    else:
        bucket = 3
    return (bucket, depth, path)


def find_version(text: str, var: str) -> tuple[str, str]:
    """Find `var`'s version value in a build file. Returns (version, line).

    Deliberately syntax-agnostic — it covers `set(V 1.2.3)`, `set(V "1.2.3")`,
    the multi-line `set()` form, `V=1.2.3` and `"V": "1.2.3"` — but nothing
    other than quotes, whitespace and `:`/`=` may sit between the name and the
    value. That is what stops `if(V VERSION_LESS 1.2.3)` from being read as a
    declaration.
    """
    if not text or not var:
        return "", ""
    pat = re.compile(
        rf"(?<!\w){re.escape(var)}(?!\w)[\s\"':=]*v?(\d+\.\d+(?:\.\d+)?)", re.I
    )
    m = pat.search(text)
    if not m:
        return "", ""
    lo = text.rfind("\n", 0, m.start()) + 1
    hi = text.find("\n", m.end())
    line = text[lo : hi if hi != -1 else len(text)].strip()
    return m.group(1), re.sub(r"\s+", " ", line)[:160]


def find_in_prose(text: str, subject: str) -> tuple[str, str]:
    """Weaker fallback: a release note saying "requires libhegel 0.31.0".

    Prose is the least reliable evidence in this tool, so the caller labels
    anything found here as such rather than treating it like a declaration.
    """
    if not text or len(subject) < 4:
        return "", ""
    for m in re.finditer(re.escape(subject), text, re.I):
        after = text[m.end() : m.end() + 120]
        vm = re.search(_VERSION_LITERAL, after)
        if vm:
            return vm.group(1), _snippet(text, m.start(), m.end() + vm.end())
        before = text[max(0, m.start() - 80) : m.start()]
        hits = re.findall(_VERSION_LITERAL, before)
        if hits:
            return hits[-1], _snippet(text, max(0, m.start() - 80), m.end())
    return "", ""


def _snippet(text: str, lo: int, hi: int) -> str:
    return re.sub(r"\s+", " ", text[lo:hi]).strip()[:160]


# ------------------------------------------------------------------ resolution


def _probe(repo, refs, cands, fetch, tree, cache, searched) -> tuple[str, str, str, bool]:
    """Look for any candidate spelling in any build file at any ref.

    Returns (version, evidence, matched_var, read_anything). The variable name
    is the outer loop: an exact-name hit in an obscure file is better evidence
    than a stripped-name hit in the root CMakeLists.txt.
    """
    read_anything = False
    for ref in refs:
        if (repo, ref) not in cache:
            cache[(repo, ref)] = candidate_files(tree(repo, ref))
        paths = cache[(repo, ref)]
        seen_any = False
        for var in cands:
            for path in paths:
                key = (repo, ref, path)
                if key not in cache:
                    cache[key] = fetch(repo, path, ref)
                    if cache[key]:
                        searched.append(f"{ref}:{path}")
                text = cache[key]
                if not text:
                    continue
                seen_any = read_anything = True
                value, line = find_version(text, var)
                if value:
                    return value, f"{repo}@{ref} {path}: {line}", var, True
        if seen_any:
            # The ref exists and we read its build files; the variable simply
            # is not there. Another spelling of the same tag will not help.
            break
    return "", "", "", read_anything


def resolve(
    dep: Dep,
    pin: CompanionPin,
    target_version: str,
    versions: list[dict] | None = None,
    target_notes: str = "",
    fetch=None,
    tree=None,
) -> dict:
    """What must `pin` become for `dep` to move to `target_version`?"""
    from . import upstream as up

    fetch = fetch or up.fetch_file
    tree = tree or up.list_files

    result = {
        "var": pin.var,
        "file": pin.file,
        "line": pin.line,
        "current": pin.value,
        "required": "",
        "action": "unresolved",
        "confidence": "",
        "evidence": "",
        "matched_by": pin.matched_by,
        "self_check": "unavailable",
        "self_check_value": "",
        "searched": [],
        "notes": [],
    }

    if dep.upstream.kind != "github" or "/" not in (dep.upstream.ref or ""):
        result["notes"].append(
            f"cannot resolve: no readable upstream repository for this dependency "
            f"(upstream is {dep.upstream.kind or 'unknown'}:{dep.upstream.ref or '?'}) — "
            f"check {pin.var} by hand"
        )
        return result

    repo = dep.upstream.ref
    cands = var_candidates(pin.var, dep.name)
    cache: dict = {}
    searched: list[str] = []

    target_refs = up.tag_forms(target_version, versions)
    value, evidence, matched_var, read_any = _probe(
        repo, target_refs, cands, fetch, tree, cache, searched
    )
    if not read_any:
        result["notes"].append(
            f"could not read any build file from {repo} at "
            f"{' or '.join(target_refs)} — the tag may not exist under that name, or "
            f"the repository is not readable from here"
        )

    # Self-check: does this same extraction reproduce the value already pinned?
    if dep.version:
        cur_value, cur_evidence, cur_var, _ = _probe(
            repo, up.tag_forms(dep.version, versions), cands, fetch, tree, cache, searched
        )
        result["self_check_value"] = cur_value
        if not cur_value:
            result["self_check"] = "unavailable"
        elif cur_value == pin.value:
            result["self_check"] = "reproduced"
        else:
            result["self_check"] = "diverged"
            result["notes"].append(
                f"upstream at the currently pinned {dep.version} declares "
                f"{cur_var or pin.var}={cur_value}, but this repo pins {pin.value} "
                f"({cur_evidence}) — either the pin is deliberate or the wrong variable "
                f"is being read; resolve by hand"
            )

    if value:
        result["required"] = value
        result["confidence"] = "declared"
        result["evidence"] = evidence
        if matched_var and matched_var != pin.var:
            result["notes"].append(
                f"upstream spells this pin {matched_var}, not {pin.var}"
            )
    else:
        subject = subject_of(pin.var, dep.name)
        prose_value, prose_evidence = find_in_prose(target_notes, subject)
        if prose_value:
            result["required"] = prose_value
            result["confidence"] = "notes"
            result["evidence"] = f"release notes for {target_version}: “{prose_evidence}”"
            result["notes"].append(
                "taken from prose in the release notes, not from a declaration — verify "
                "before relying on it"
            )

    if result["required"] and result["self_check"] != "diverged":
        result["action"] = "unchanged" if result["required"] == pin.value else "bump"
    elif result["required"]:
        result["action"] = "unresolved"  # self-check disagreed; do not edit
    else:
        result["notes"].append(
            f"{pin.var} not declared in any build file read at {target_version} "
            f"and not mentioned in its release notes — bumping {dep.name} alone risks "
            f"the link-time failure this pin exists to prevent"
        )

    if result["action"] == "bump" and result["self_check"] == "unavailable":
        result["notes"].append(
            "could not confirm the extraction against the currently pinned version, so "
            "this value is unverified"
        )
    if pin.matched_by == "proximity":
        result["notes"].append(
            "this pin was linked to the dependency only by sitting next to its "
            "declaration — confirm it is really coupled"
        )
    if result["action"] == "bump" and dep.version and dep.version in (pin.doc or ""):
        # The docstring is the only record of *why* the pin has that value, so a
        # stale one is worth flagging — but it is free prose, and rewriting it
        # automatically is not this tool's job.
        result["notes"].append(
            f"the CACHE docstring still says {dep.version} and will be stale after "
            f"the bump: “{pin.doc}”"
        )

    result["searched"] = searched
    return result


def resolve_all(
    dep: Dep,
    target_version: str,
    versions: list[dict] | None = None,
    target_notes: str = "",
    fetch=None,
    tree=None,
) -> list[dict]:
    return [
        resolve(dep, pin, target_version, versions, target_notes, fetch, tree)
        for pin in dep.companion_pins
    ]


def notes_for(version: str, available: list[dict] | None) -> str:
    """Release-note body for a version, from an upstream summary."""
    from .model import parse_version

    target = parse_version(version)
    for v in available or []:
        if v.get("version") == version or (target and parse_version(v.get("version", "")) == target):
            return v.get("notes") or ""
    return ""
