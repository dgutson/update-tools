"""Core data model shared by discovery, profiling and upstream lookup."""

from __future__ import annotations

import dataclasses
import hashlib
import re
from dataclasses import dataclass, field
from typing import Any


# Scope drives most of the "is it worth it" judgement, so keep it ordered:
# the most restrictive scope wins when several sites disagree.
SCOPE_ORDER = ["runtime", "build", "example", "test", "unused"]


def most_restrictive(*scopes: str) -> str:
    known = [s for s in scopes if s in SCOPE_ORDER]
    if not known:
        return "runtime"
    return max(known, key=SCOPE_ORDER.index)


@dataclass
class Site:
    """One place in *our* code that touches a dependency."""

    path: str
    line: int
    symbol: str = ""
    context: str = ""

    def key(self) -> str:
        return f"{self.path}:{self.line}:{self.symbol}"


@dataclass
class Upstream:
    """Where to look for newer versions."""

    kind: str = ""  # github | pypi | npm | crates | gomod | distro | unknown
    ref: str = ""  # owner/repo, package name, ...
    note: str = ""

    def is_resolvable(self) -> bool:
        return bool(self.kind and self.kind != "unknown" and self.ref)


@dataclass
class Dep:
    name: str
    kind: str  # how it is declared: cmake-fetchcontent-url, pkg-config, npm, ...
    version: str = ""  # what we are pinned to, "" when unpinned
    raw_pin: str = ""  # URL / git tag / version spec, verbatim
    integrity: str = ""  # URL_HASH / sha / integrity field, verbatim
    declared_in: str = ""  # "CMakeLists.txt:88"
    scope: str = "runtime"
    scope_evidence: list[str] = field(default_factory=list)
    upstream: Upstream = field(default_factory=Upstream)
    installed_version: str = ""  # for system deps: what is on this machine
    # Filled in by an analysis backend.
    consumed: list[str] = field(default_factory=list)
    sites: list[Site] = field(default_factory=list)
    backend: str = ""
    call_depth: int | None = None
    # How many of our own functions transitively reach this dependency.
    blast_radius: int | None = None
    notes: list[str] = field(default_factory=list)
    # Version-valued CACHE variables that must move together with this
    # dependency (e.g. a source tarball plus its prebuilt native engine).
    companion_pins: list[str] = field(default_factory=list)
    # Prose written by Claude and preserved across regenerations.
    assessment: str = ""
    # Fingerprint as recorded in CLAUDE_DEPS.md, for drift detection.
    stored_fingerprint: dict[str, str] = field(default_factory=dict)

    def source_files(self) -> list[str]:
        return sorted({s.path for s in self.sites})

    def fingerprint(self, root: str) -> dict[str, str]:
        """Hashes that let /deps:sync detect drift without any LLM call."""
        from .fingerprint import hash_declaration, hash_files, hash_text

        return {
            "decl": hash_declaration(root, self.declared_in),
            "sites": hash_files(root, self.source_files()),
            "pin": hash_text(f"{self.version}|{self.raw_pin}|{self.integrity}"),
        }

    def to_dict(self) -> dict[str, Any]:
        d = dataclasses.asdict(self)
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Dep":
        d = dict(d)
        d["upstream"] = Upstream(**d.get("upstream", {}) or {})
        d["sites"] = [Site(**s) for s in d.get("sites", []) or []]
        known = {f.name for f in dataclasses.fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in known})


_VERSION_RE = re.compile(
    r"""^
    v?
    (?P<major>\d+)
    (?:\.(?P<minor>\d+))?
    (?:\.(?P<patch>\d+))?
    (?:[.-]?(?P<pre>[A-Za-z][\w.]*))?
    """,
    re.VERBOSE,
)


def parse_version(text: str) -> tuple | None:
    """Loose semver-ish parse. Returns a sortable tuple or None."""
    if not text:
        return None
    m = _VERSION_RE.match(text.strip())
    if not m:
        return None
    major = int(m.group("major"))
    minor = int(m.group("minor") or 0)
    patch = int(m.group("patch") or 0)
    pre = m.group("pre") or ""
    # A release sorts above its own pre-releases.
    return (major, minor, patch, 1 if not pre else 0, pre)


def bump_kind(old: str, new: str) -> str:
    """major | minor | patch | same | unknown"""
    a, b = parse_version(old), parse_version(new)
    if not a or not b:
        return "unknown"
    if b[:3] == a[:3]:
        return "same"
    if b[0] != a[0]:
        return "major"
    if b[1] != a[1]:
        return "minor"
    return "patch"


def sha256_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
