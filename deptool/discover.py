"""Find every declared dependency in a repository, across ecosystems.

Everything here is deterministic. No LLM, no network.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tomllib

from . import cmake
from .model import Dep, Upstream

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


def detect_manifests(root: str) -> list[tuple[str, str]]:
    found = []
    for fname, eco in MANIFESTS.items():
        if os.path.isfile(os.path.join(root, fname)):
            found.append((fname, eco))
    return found


# ------------------------------------------------------------------ ecosystems


def _npm(root: str) -> list[Dep]:
    path = os.path.join(root, "package.json")
    try:
        data = json.load(open(path, encoding="utf-8"))
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
                    declared_in=f"package.json ({field})",
                    scope=scope,
                    upstream=Upstream(kind="npm", ref=name),
                )
            )
    return deps


def _cargo(root: str) -> list[Dep]:
    path = os.path.join(root, "Cargo.toml")
    try:
        data = tomllib.load(open(path, "rb"))
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
                    declared_in=f"Cargo.toml ([{field}])",
                    scope=scope,
                    upstream=Upstream(kind="crates", ref=name),
                )
            )
    return deps


_PY_REQ = re.compile(r"^\s*([A-Za-z0-9._\-]+)\s*(?:\[[^\]]+\])?\s*([=<>!~]=?[^;#]*)?")


def _pypi(root: str) -> list[Dep]:
    deps: list[Dep] = []
    pyproject = os.path.join(root, "pyproject.toml")
    if os.path.isfile(pyproject):
        try:
            data = tomllib.load(open(pyproject, "rb"))
        except (OSError, ValueError):
            data = {}
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
                    declared_in=f"pyproject.toml ({where})",
                    scope=scope,
                    upstream=Upstream(kind="pypi", ref=m.group(1)),
                )
            )
    req = os.path.join(root, "requirements.txt")
    if os.path.isfile(req) and not deps:
        for i, line in enumerate(open(req, encoding="utf-8", errors="replace"), 1):
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
                    declared_in=f"requirements.txt:{i}",
                    upstream=Upstream(kind="pypi", ref=m.group(1)),
                )
            )
    return deps


def _gomod(root: str) -> list[Dep]:
    path = os.path.join(root, "go.mod")
    try:
        text = open(path, encoding="utf-8", errors="replace").read()
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
                declared_in=f"go.mod:{i}",
                scope="test" if "// indirect" in line else "runtime",
                upstream=Upstream(kind="gomod", ref=m.group(1)),
            )
        )
    return deps


def _vcpkg(root: str) -> list[Dep]:
    path = os.path.join(root, "vcpkg.json")
    try:
        data = json.load(open(path, encoding="utf-8"))
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
                declared_in="vcpkg.json",
                upstream=Upstream(kind="vcpkg", ref=name),
            )
        )
    ov = data.get("builtin-baseline")
    for d in deps:
        if ov:
            d.notes.append(f"vcpkg baseline {ov[:12]} governs the actual version")
    return deps


def _conan(root: str) -> list[Dep]:
    deps = []
    txt = os.path.join(root, "conanfile.txt")
    if os.path.isfile(txt):
        section = ""
        for i, line in enumerate(open(txt, encoding="utf-8", errors="replace"), 1):
            s = line.strip()
            if s.startswith("["):
                section = s.strip("[]")
                continue
            if not s or s.startswith("#") or section not in ("requires", "tool_requires", "build_requires"):
                continue
            name, _, version = s.partition("/")
            deps.append(
                Dep(
                    name=name,
                    kind="conan",
                    version=version.split("@")[0],
                    raw_pin=s,
                    declared_in=f"conanfile.txt:{i}",
                    scope="build" if "tool" in section or "build" in section else "runtime",
                    upstream=Upstream(kind="conan", ref=name),
                )
            )
    py = os.path.join(root, "conanfile.py")
    if os.path.isfile(py):
        text = open(py, encoding="utf-8", errors="replace").read()
        for m in re.finditer(r'self\.(tool_)?requires\(\s*["\']([\w.\-+]+)/([^"\'@]+)', text):
            deps.append(
                Dep(
                    name=m.group(2),
                    kind="conan",
                    version=m.group(3),
                    raw_pin=f"{m.group(2)}/{m.group(3)}",
                    declared_in="conanfile.py",
                    scope="build" if m.group(1) else "runtime",
                    upstream=Upstream(kind="conan", ref=m.group(2)),
                )
            )
    return deps


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


def discover(root: str) -> tuple[list[Dep], list[str]]:
    """Return (deps, files_that_declare_them)."""
    deps: list[Dep] = []
    files: list[str] = []

    manifests = detect_manifests(root)
    ecos = {eco for _, eco in manifests}

    if "cmake" in ecos:
        cdeps, cfiles = cmake.parse_project(root)
        deps += cdeps
        files += cfiles
    if "npm" in ecos:
        deps += _npm(root)
        files.append("package.json")
    if "cargo" in ecos:
        deps += _cargo(root)
        files.append("Cargo.toml")
    if "pypi" in ecos:
        deps += _pypi(root)
        files += [f for f in ("pyproject.toml", "requirements.txt")
                  if os.path.isfile(os.path.join(root, f))]
    if "gomod" in ecos:
        deps += _gomod(root)
        files.append("go.mod")
    if "vcpkg" in ecos:
        deps += _vcpkg(root)
        files.append("vcpkg.json")
    if "conan" in ecos:
        deps += _conan(root)
        files += [f for f in ("conanfile.txt", "conanfile.py")
                  if os.path.isfile(os.path.join(root, f))]

    probe_installed(deps)

    # Drop CMake's own built-in find modules that are not really dependencies.
    noise = {"Threads", "PkgConfig", "Git", "Doxygen", "Python", "Python3"}
    deps = [d for d in deps if d.name not in noise]

    deps.sort(key=lambda d: (d.scope, d.name.lower()))
    return deps, sorted(set(files))
