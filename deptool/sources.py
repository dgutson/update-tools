"""Discovery sources other than our own parsers.

The native parsers in `discover` are what make a dependency *editable*: they
record the file and line a pin lives on, which is the whole of what `apply`
needs. What they do not do is cover twenty ecosystems, and writing a sixth
Gradle parser buys nothing the judgement layer needs (see ROADMAP.md, "Prior
art"). So an external scanner is ingested as an additional source, exactly the
way analysis backends are: auto-detected, never required, and merged additively
rather than replacing anything.

Two rules follow from the data model and are enforced here rather than papered
over:

- **A dependency with no line number is report-only.** It can be profiled,
  judged and reported; `apply` refuses to guess at an edit. That refusal lives
  in `apply.plan`, which declines any dependency whose every declaration is
  uneditable — nothing here needs to special-case it.
- **Merge additively.** An ingested dependency and a natively parsed one
  reconcile into one record with both declaration sites, and identical sites
  collapse. Neither silently wins.

Trivy is the only source implemented. It is a single static Go binary with no
runtime to depend on, it knows the Python managers ours do not, and it records
declaration lines for several ecosystems. ORT needs a JVM and dependabot-core
is Ruby in Docker; neither is worth requiring.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess

from .model import Dep, Upstream

TIMEOUT = 300

# Trivy's ecosystem name -> the kind we already model, so reconciliation groups
# an ingested package with a natively parsed one instead of beside it. Anything
# absent keeps Trivy's own name (`nuget`, `pom`, `gemspec`): those ecosystems
# have no native parser to collide with, and inventing a kind for them would
# claim a fluency this tool does not have.
_KIND = {
    "conan": "conan",
    "pip": "pypi",
    "pipenv": "pypi",
    "poetry": "pypi",
    "uv": "pypi",
    "npm": "npm",
    "yarn": "npm",
    "pnpm": "npm",
    "cargo": "cargo",
    "gomod": "gomod",
}

# Where "what versions exist?" can be answered for an ingested package. An
# ecosystem missing here still gets discovered and reported — it simply has no
# upstream, which is honest: claiming one we cannot query would produce a
# confident silence about whether anything newer exists.
_UPSTREAM = {
    "conan": "conan",
    "pypi": "pypi",
    "npm": "npm",
    "cargo": "crates",
    "gomod": "gomod",
}


def detect(root: str) -> list[str]:
    """Which ingest sources are usable here. Absence is never an error."""
    return ["trivy"] if shutil.which("trivy") else []


def describe(root: str) -> str:
    found = detect(root)
    return " + ".join(["native"] + found)


def ingest(root: str) -> tuple[list[Dep], list[str]]:
    """Every dependency an external scanner can see. Returns (deps, files).

    Failure of any kind — binary absent, non-zero exit, unparsable output — is
    silence, not an error. The tool has to work with nothing installed.
    """
    deps: list[Dep] = []
    files: list[str] = []
    for name in detect(root):
        got, saw = _SOURCES[name](root)
        deps += got
        files += saw
    return deps, files


def _run_trivy(root: str) -> dict | None:
    """Trivy's JSON report, or None.

    `--list-all-pkgs` is what makes it emit packages at all, and the native
    JSON format is the only one carrying `Locations` — the CycloneDX and SPDX
    outputs drop line numbers, which are the reason to prefer Trivy in the
    first place.

    A scanner has to be enabled for package analysis to run, and the choice is
    not free: `vuln` with a cached database took 0.8s on a real repository
    where `secret` took 5.1s, because secret scanning reads every file. So the
    fast path is tried first, offline; `secret` is the fallback for a fresh
    Trivy install whose database has never been downloaded, which fails with
    "--skip-update cannot be specified on the first run" rather than quietly
    fetching 50MB nobody asked for.
    """
    base = ["trivy", "fs", "--format", "json", "--list-all-pkgs", "--quiet",
            "--offline-scan"]
    attempts = [
        base + ["--scanners", "vuln", "--skip-db-update", root],
        base + ["--scanners", "secret", root],
    ]
    for cmd in attempts:
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=TIMEOUT)
        except (OSError, subprocess.SubprocessError):
            return None
        if out.returncode != 0 or not out.stdout.strip():
            continue
        try:
            data = json.loads(out.stdout)
        except ValueError:
            return None
        if isinstance(data, dict):
            return data
    return None


def _trivy(root: str) -> tuple[list[Dep], list[str]]:
    """Read Trivy's report into dependencies.

    Only `lang-pkgs` results are taken. OS package results describe the
    scanning machine's own image rather than anything this repository declares,
    and reporting those as the project's dependencies would be a category
    error.
    """
    data = _run_trivy(root)
    if not data:
        return [], []

    deps: list[Dep] = []
    files: list[str] = []
    for result in data.get("Results") or []:
        if not isinstance(result, dict) or result.get("Class") != "lang-pkgs":
            continue
        target = str(result.get("Target") or "")
        eco = str(result.get("Type") or "")
        packages = result.get("Packages") or []
        # `Target` is normally a path relative to the scan root, but some
        # results use a label ("OS Packages"). Anything that is not a real file
        # cannot anchor a declaration, so it is not treated as one.
        rel = os.path.normpath(target) if target else ""
        if not rel or not os.path.isfile(os.path.join(root, rel)):
            rel = ""
        if packages and rel:
            files.append(rel)
        for pkg in packages:
            dep = _package_dep(pkg, eco, rel)
            if dep:
                deps.append(dep)
    return deps, files


def _package_dep(pkg: dict, eco: str, rel: str) -> Dep | None:
    if not isinstance(pkg, dict):
        return None
    name = str(pkg.get("Name") or "").strip()
    version = str(pkg.get("Version") or "").strip()
    if not name:
        return None

    kind = _KIND.get(eco, eco or "unknown")
    # A lockfile stays a lockfile whoever read it, so an ingested `conan.lock`
    # line reconciles with the one our own parser recorded instead of appearing
    # beside it as a second, editable-looking declaration.
    if kind == "conan" and os.path.basename(rel) == "conan.lock":
        kind = "conan-lock"

    line = 0
    for loc in pkg.get("Locations") or []:
        if isinstance(loc, dict) and loc.get("StartLine"):
            line = int(loc["StartLine"])
            break

    # Trivy marks a development dependency on the ecosystems that record one;
    # `Relationship` is newer still and absent from several parsers, so both are
    # read when present and neither is assumed.
    dep = Dep(
        name=name,
        kind=kind,
        version=version,
        raw_pin=str(pkg.get("ID") or "") or version,
        declared_in=f"{rel}:{line}" if rel and line else rel,
        scope="test" if pkg.get("Dev") else "runtime",
        upstream=Upstream(kind=_UPSTREAM.get(kind, ""), ref=name),
    )
    if str(pkg.get("Relationship") or "") == "indirect":
        dep.notes.append(
            f"transitive — trivy resolved it from {rel or 'the project'}, "
            "no manifest declares it"
        )
    if not line:
        dep.notes.append(
            f"found by trivy in {rel or eco} with no line recorded — report-only: "
            "it can be judged and reported, but a bump has to be made by hand"
        )
    return dep


_SOURCES = {"trivy": _trivy}

# What an ingest may be restricted to. Kept beside `_SOURCES` so a typo in a
# flag cannot silently disable every source.
NAMES = sorted(_SOURCES)
