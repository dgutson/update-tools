"""Apply a version bump, re-hash the artefact, and verify the build.

The re-hash is the part that makes C++ bumps annoying by hand: changing a
`FetchContent_Declare` URL without updating its `URL_HASH SHA256=...` produces
a build that fails at download time with a hash mismatch.

Nothing here runs unless explicitly asked for. `plan()` is read-only and shows
exactly what would change.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import urllib.error
import urllib.request

from .fingerprint import declaration_span
from .model import Dep, sha256_of

TIMEOUT = 300


class ApplyError(RuntimeError):
    pass


def _fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "deptool/0.1"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return r.read()
    except (urllib.error.URLError, OSError) as exc:
        raise ApplyError(f"could not download {url}: {exc}") from exc


def _swap_version(text: str, old: str, new: str) -> str:
    """Replace a version token, tolerating a `v` prefix.

    The trailing guard must reject `0.7.4` inside `0.7.41` and inside a longer
    version like `0.7.4.2`, while still matching `v0.7.4.tar.gz` — hence
    "not followed by a word char, and not followed by .<digit>".
    """
    if not old:
        return text
    pattern = re.compile(rf"(?<![\w.])(v?){re.escape(old)}(?!\w)(?!\.\d)")
    return pattern.sub(lambda m: m.group(1) + new, text)


# Restricting edits to a single declaration span is what stops a bump of one
# dependency from rewriting an unrelated one that shares a version number.
_declaration_span = declaration_span


def plan(root: str, dep: Dep, new_version: str) -> dict:
    """Work out the edit without touching anything."""
    if not dep.declared_in:
        raise ApplyError(f"{dep.name}: no declaration site recorded")
    rel = dep.declared_in.split(":")[0]
    full = os.path.join(root, rel)
    if not os.path.isfile(full):
        raise ApplyError(f"{dep.name}: {rel} not found")

    old_version = dep.version
    if not old_version:
        raise ApplyError(
            f"{dep.name}: not pinned to a version in this repo "
            f"(kind={dep.kind}) — nothing to edit. This is a system dependency; "
            f"update it through the OS package manager or CI image instead."
        )

    text = open(full, encoding="utf-8", errors="replace").read()
    new_url = ""
    new_hash = ""

    if dep.kind in ("cmake-fetchcontent-url", "cmake-system-or-fetch") and dep.raw_pin.startswith("http"):
        new_url = _swap_version(dep.raw_pin, old_version, new_version)
        if new_url == dep.raw_pin:
            raise ApplyError(
                f"{dep.name}: could not locate version {old_version} inside the pinned URL"
            )
        if dep.integrity:
            algo = dep.integrity.split("=")[0].upper() if "=" in dep.integrity else "SHA256"
            if algo != "SHA256":
                raise ApplyError(f"{dep.name}: unsupported URL_HASH algorithm {algo}")
            new_hash = f"SHA256={sha256_of(_fetch(new_url))}"

    # Only rewrite inside this dependency's own declaration.
    try:
        line_no = int(dep.declared_in.split(":")[1])
    except (IndexError, ValueError):
        line_no = 0
    if line_no:
        lo, hi = _declaration_span(text, line_no)
    else:
        lo, hi = 0, len(text)

    block = text[lo:hi]
    new_block = _swap_version(block, old_version, new_version)
    if dep.integrity and new_hash:
        new_block = new_block.replace(dep.integrity, new_hash)
    updated = text[:lo] + new_block + text[hi:]

    if updated == text:
        raise ApplyError(
            f"{dep.name}: computed edit is a no-op — version {old_version} not found "
            f"inside the declaration at {dep.declared_in}"
        )

    return {
        "dep": dep.name,
        "file": rel,
        "from": old_version,
        "to": new_version,
        "new_url": new_url,
        "old_hash": dep.integrity,
        "new_hash": new_hash,
        # Coupled pins this edit does NOT touch. Bumping the source without
        # its companion is a classic silent breakage: it configures fine and
        # fails at link time with a missing symbol.
        "companion_pins": list(dep.companion_pins),
        "diff": _unified(text, updated, rel),
        "_text": updated,
    }


def _unified(a: str, b: str, rel: str) -> str:
    import difflib

    return "".join(
        difflib.unified_diff(
            a.splitlines(keepends=True), b.splitlines(keepends=True),
            fromfile=f"a/{rel}", tofile=f"b/{rel}", n=3,
        )
    )


def write(root: str, planned: dict) -> None:
    full = os.path.join(root, planned["file"])
    shutil.copy2(full, full + ".deptool.bak")
    with open(full, "w", encoding="utf-8") as fh:
        fh.write(planned["_text"])


def revert(root: str, planned: dict) -> bool:
    full = os.path.join(root, planned["file"])
    bak = full + ".deptool.bak"
    if not os.path.isfile(bak):
        return False
    shutil.move(bak, full)
    return True


def _run(cmd: list[str], root: str, timeout: int) -> dict:
    try:
        out = subprocess.run(
            cmd, cwd=root, capture_output=True, text=True, timeout=timeout
        )
    except FileNotFoundError:
        return {"cmd": " ".join(cmd), "skipped": f"{cmd[0]} not installed"}
    except subprocess.TimeoutExpired:
        return {"cmd": " ".join(cmd), "ok": False, "output": f"timed out after {timeout}s"}
    tail = (out.stdout + out.stderr).strip().splitlines()
    return {
        "cmd": " ".join(cmd),
        "ok": out.returncode == 0,
        "output": "\n".join(tail[-60:]),
    }


def verify(root: str, build_dir: str = "build") -> list[dict]:
    """Configure, build and test. Missing tooling is reported, not fatal."""
    steps: list[dict] = []
    if not shutil.which("cmake"):
        return [{"cmd": "cmake", "skipped": "cmake not installed — cannot verify locally"}]

    steps.append(_run(
        ["cmake", "-S", ".", "-B", build_dir, "-DCMAKE_BUILD_TYPE=Debug", "-DBUILD_TESTING=ON"],
        root, 600,
    ))
    if not steps[-1].get("ok"):
        return steps
    steps.append(_run(["cmake", "--build", build_dir, "-j"], root, 1800))
    if not steps[-1].get("ok"):
        return steps
    if shutil.which("ctest"):
        steps.append(_run(["ctest", "--test-dir", build_dir, "--output-on-failure"], root, 1800))
    return steps
