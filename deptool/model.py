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
class CompanionPin:
    """A version-valued variable that must move together with a dependency.

    The observed case: a source tarball pinned at one version plus its prebuilt
    native engine pinned separately in a CACHE variable. Bumping one alone
    configures cleanly and fails at link time with a missing symbol — a failure
    that looks nothing like a dependency problem.

    Structured rather than a formatted string because `apply` has to *edit*
    these, which needs the file and line, and the resolver has to look the
    variable up in the dependency's own build files upstream, which needs the
    name.
    """

    var: str
    value: str
    file: str = ""
    line: int = 0
    doc: str = ""  # the CACHE docstring, when there is one
    # Why we believe this belongs to the dependency: name | doc | proximity.
    # Proximity is the weakest and the report says so.
    matched_by: str = ""

    def where(self) -> str:
        return f"{self.file}:{self.line}" if self.file else ""

    def render(self) -> str:
        """One line, human-readable, round-trips through `parse`."""
        out = f"{self.var}={self.value}"
        if self.file:
            out += f" — {self.where()}"
        if self.doc:
            out += f" ({self.doc})"
        if self.matched_by:
            out += f" [matched by {self.matched_by}]"
        return out

    @classmethod
    def parse(cls, item: str) -> "CompanionPin | None":
        """Recover a pin from its rendered form in CLAUDE_DEPS.md.

        Parsed from the right, because the CACHE docstring is free prose and
        may itself contain ` — ` or parentheses.
        """
        text = item.strip()
        matched_by = ""
        m = re.search(r"\s*\[matched by ([^\]]+)\]$", text)
        if m:
            matched_by = m.group(1).strip()
            text = text[: m.start()]

        doc = ""
        if text.endswith(")"):
            depth, i = 0, len(text) - 1
            while i >= 0:
                if text[i] == ")":
                    depth += 1
                elif text[i] == "(":
                    depth -= 1
                    if depth == 0:
                        break
                i -= 1
            if i > 0:
                doc = text[i + 1 : -1]
                text = text[:i].rstrip()

        head, _, where = text.partition(" — ")
        var, sep, value = head.partition("=")
        if not sep or not var.strip() or not value.strip():
            return None
        file, line = "", 0
        if where.strip():
            fpart, _, lpart = where.strip().rpartition(":")
            if fpart and lpart.isdigit():
                file, line = fpart, int(lpart)
            else:
                file = where.strip()
        return cls(
            var=var.strip(), value=value.strip(), file=file, line=line,
            doc=doc, matched_by=matched_by,
        )


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
    companion_pins: list[CompanionPin] = field(default_factory=list)
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
        # Tolerate the pre-structured form: a profile written by an older
        # version records companion pins as plain rendered strings.
        pins = []
        for c in d.get("companion_pins", []) or []:
            pin = CompanionPin(**c) if isinstance(c, dict) else CompanionPin.parse(str(c))
            if pin:
                pins.append(pin)
        d["companion_pins"] = pins
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
