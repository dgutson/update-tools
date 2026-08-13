"""Core data model shared by discovery, profiling and upstream lookup."""

from __future__ import annotations

import dataclasses
import hashlib
import os
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


# Kinds that record a *resolved* fact rather than a hand-written declaration.
# A lockfile pin has a perfectly good file and line, but it is generated: the
# version sits beside the recipe revision it was resolved with, so hand-editing
# one desynchronises the pair. The edit belongs in the manifest and the
# regeneration belongs to the package manager.
GENERATED_KINDS = {"conan-lock"}


@dataclass
class Declaration:
    """One place a dependency is declared, and what that place says.

    A repository frequently declares the same dependency more than once: a
    manifest per target platform, a `find_package` beside a package-manager
    pin, a lockfile beside the manifest it was resolved from. Those
    declarations can *disagree*, and the disagreement is a finding in its own
    right — no upstream lookup produces it, and for a project already current
    on everything it is worth more than "you are a minor version behind". So
    each declaration is kept rather than folded into one version string.
    """

    path: str = ""  # relative to the repo root
    line: int = 0  # 0 when the format records no position
    kind: str = ""  # conan | cmake-find-package | npm | ...
    version: str = ""  # "" when this declaration carries no version
    raw_pin: str = ""  # verbatim, e.g. "openssl/3.0.15"

    @property
    def variant(self) -> str:
        """The declaring directory — what tells one platform's manifest from
        another's. Empty for a manifest at the repository root."""
        return os.path.dirname(self.path)

    def where(self) -> str:
        return f"{self.path}:{self.line}" if self.line else self.path

    def is_generated(self) -> bool:
        """Was this written by a tool rather than by a person?"""
        return self.kind in GENERATED_KINDS

    def is_editable(self) -> bool:
        """Can `apply` rewrite this pin? Needs a version *and* a position, and
        the file has to be one a human maintains — see `GENERATED_KINDS`."""
        return bool(self.version and self.path and self.line) and not self.is_generated()

    def render(self) -> str:
        """One line, human-readable, round-trips through `parse`."""
        out = self.version or "(unpinned)"
        if self.raw_pin and self.raw_pin != self.version:
            out += f" [{self.raw_pin}]"
        if self.path:
            out += f" — {self.where()}"
        if self.kind:
            out += f" ({self.kind})"
        return out

    @classmethod
    def from_site(cls, site: str, kind: str = "", version: str = "",
                  raw_pin: str = "") -> "Declaration":
        """Build one from a `declared_in` string.

        That string comes in three shapes — `path:line`, `path (table)` and a
        bare `path` — because the formats record positions to different
        precision.
        """
        text = (site or "").strip()
        head = text.split(" (")[0].strip()
        path, _, lpart = head.rpartition(":")
        if not (path and lpart.isdigit()):
            path, lpart = head, "0"
        return cls(path=path, line=int(lpart), kind=kind, version=version,
                   raw_pin=raw_pin)

    @classmethod
    def parse(cls, item: str) -> "Declaration | None":
        """Recover a declaration from its rendered form in CLAUDE_DEPS.md."""
        text = item.strip()
        kind = ""
        m = re.search(r"\s*\(([\w.\-+]+)\)$", text)
        if m:
            kind = m.group(1)
            text = text[: m.start()]

        head, _, where = text.partition(" — ")
        path, line = "", 0
        if where.strip():
            fpart, _, lpart = where.strip().rpartition(":")
            if fpart and lpart.isdigit():
                path, line = fpart, int(lpart)
            else:
                path = where.strip()

        head = head.strip()
        raw = ""
        rm = re.search(r"\s*\[(.+)\]$", head)
        if rm:
            raw = rm.group(1).strip()
            head = head[: rm.start()].strip()
        version = "" if head == "(unpinned)" else head
        if not (path or version or raw):
            return None
        return cls(
            path=path, line=line, kind=kind, version=version, raw_pin=raw or version
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
    declared_in: str = ""  # "CMakeLists.txt:88" — the site `apply` edits
    # Every site this dependency is declared at, including `declared_in`.
    # Filled in by reconciliation; a dependency declared once has one entry.
    declarations: list[Declaration] = field(default_factory=list)
    # Other names the same library is declared under — a CMake package name
    # beside the package-manager one — so `--dep CURL` still resolves after
    # `find_package(CURL)` and `libcurl/8.4.0` have been folded together.
    aliases: list[str] = field(default_factory=list)
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

    def ensure_declaration(self) -> None:
        """Guarantee the invariant everything downstream relies on: a
        dependency has at least one declaration.

        A dependency declared in exactly one place is not written to
        `CLAUDE_DEPS.md` as a list — the `pinned:` line already says it — so a
        record read back from the file would otherwise arrive with none. The
        list is what carries the fact that a site is *generated* and must not
        be edited, so without this a reload silently makes a lockfile pin
        editable again.
        """
        if not self.declarations and self.declared_in:
            self.declarations = [Declaration.from_site(
                self.declared_in, kind=self.kind, version=self.version,
                raw_pin=self.raw_pin,
            )]

    def pin_variants(self, generated: bool | None = None) -> dict[str, list[str]]:
        """version -> the sites asserting it, for declarations carrying one.

        A declaration with no version (`find_package(CURL)`) is not evidence of
        disagreement, so it does not appear here. `generated` restricts the
        answer to lockfile-style declarations (True) or hand-written ones
        (False); by default every declaration counts.
        """
        out: dict[str, list[str]] = {}
        for d in self.declarations:
            if d.version and (generated is None or d.is_generated() is generated):
                out.setdefault(d.version, []).append(d.where())
        return {v: sorted(set(w)) for v, w in sorted(out.items())}

    def diverges(self) -> bool:
        """True when two *hand-written* declarations pin different versions.

        Deliberately blind to lockfiles. This is the predicate `apply` refuses
        on, and the two disagreements need different treatment: manifests
        contradicting each other means a bump cannot know what it is bumping
        from, whereas a lockfile contradicting the manifests is a fact about
        the past — the edit is still well defined, and the lock is regenerated
        rather than edited. See `lock_drift`.
        """
        return len(self.pin_variants(generated=False)) > 1

    def lock_drift(self) -> dict[str, list[str]]:
        """Versions a lockfile resolved that no manifest asks for.

        Either the lock was never regenerated after the manifests moved, or the
        build is not using it. Both are findings, and neither is visible from an
        upstream lookup: this is a disagreement between two files we already
        have. Restricted to versions absent from the hand-written declarations,
        so a lock that merely agrees with one platform's manifest is silent.

        A dependency the lockfile is the *only* record of is transitive, not
        drifted — there is nothing for it to disagree with — so it says nothing
        here and is reported as transitive instead.
        """
        asked = set(self.pin_variants(generated=False))
        if not asked:
            return {}
        return {
            v: w for v, w in self.pin_variants(generated=True).items() if v not in asked
        }

    def divergence_note(self) -> str:
        """The finding(s), phrased for a human. Empty when there is nothing to say."""
        parts = []
        if self.diverges():
            detail = "; ".join(
                f"{v} in {', '.join(w)}" for v, w in self.pin_variants(generated=False).items()
            )
            parts.append(
                f"declarations disagree on the version — {detail} — so what ships "
                "depends on which manifest the build used"
            )
        locked = self.pin_variants(generated=True)
        drift = self.lock_drift()
        if len(locked) > 1:
            # Legitimate in Conan — two profiles can resolve one build
            # requirement differently — and still worth saying out loud, because
            # "which version ships" then has no single answer and an advisory
            # match against one of them is only half the story.
            detail = "; ".join(f"{v} in {', '.join(w)}" for v, w in locked.items())
            parts.append(
                f"the lockfile resolves this to more than one version at once — "
                f"{detail} — so which one is built depends on the profile that "
                "resolved it"
            )
        elif drift:
            detail = "; ".join(f"{v} in {', '.join(w)}" for v, w in drift.items())
            asked = ", ".join(self.pin_variants(generated=False))
            parts.append(
                f"the lockfile resolved {detail}, which is not what is asked for "
                f"({asked}) — either the lock is stale or the build is not using it"
            )
        return ". ".join(parts)

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
        decls = []
        for c in d.get("declarations", []) or []:
            decl = Declaration(**c) if isinstance(c, dict) else Declaration.parse(str(c))
            if decl:
                decls.append(decl)
        d["declarations"] = decls
        # Tolerate the pre-structured form: a profile written by an older
        # version records companion pins as plain rendered strings.
        pins = []
        for c in d.get("companion_pins", []) or []:
            pin = CompanionPin(**c) if isinstance(c, dict) else CompanionPin.parse(str(c))
            if pin:
                pins.append(pin)
        d["companion_pins"] = pins
        known = {f.name for f in dataclasses.fields(cls)}
        dep = cls(**{k: v for k, v in d.items() if k in known})
        dep.ensure_declaration()
        return dep


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
