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


def plan(root: str, dep: Dep, new_version: str, companions: list[dict] | None = None) -> dict:
    """Work out the edit without touching anything.

    `companions` are resolutions from `companion.resolve_all`. A resolved one
    becomes a second edit in the *same* plan — the point of the exercise is that
    a coupled pin moves atomically with the dependency, because either half on
    its own is a broken build. An unresolved one is reported in `blocked_on`
    and deliberately left alone; guessing at an ABI version is worse than
    stopping.
    """
    if not dep.declared_in:
        raise ApplyError(f"{dep.name}: no declaration site recorded")
    if dep.diverges():
        # Editing one site would deepen the disagreement rather than resolve it,
        # and "bump to X" does not say which of the current versions it is
        # bumping from. Same rule as an unresolved companion pin: stopping beats
        # guessing.
        raise ApplyError(
            f"{dep.name}: {dep.divergence_note()}. Reconcile the declarations "
            f"first — a bump from one of them alone would leave the others behind."
        )
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

    # rel -> (original, edited). A version swap never adds or removes lines, so
    # the line numbers recorded at discovery stay valid for a second edit in the
    # same file — which is the usual case, the companion pin sitting a few lines
    # below the declaration it belongs to.
    files: dict[str, tuple[str, str]] = {rel: (text, updated)}
    companion_edits: list[dict] = []
    blocked_on: list[str] = []

    # The same version pinned in several manifests — one per target platform —
    # has to move in all of them or the bump silently creates the divergence
    # that `Dep.diverges()` exists to report. `diverges()` was checked above, so
    # every sibling here agrees with `old_version`.
    also_pinned_in: list[str] = []
    for sib in dep.declarations:
        if not sib.is_editable() or sib.where() == dep.declared_in:
            continue
        sfull = os.path.join(root, sib.path)
        if not os.path.isfile(sfull):
            blocked_on.append(f"{sib.where()} (also pins {sib.version}, but the file is missing)")
            continue
        if sib.path not in files:
            stext = open(sfull, encoding="utf-8", errors="replace").read()
            files[sib.path] = (stext, stext)
        original, current = files[sib.path]
        slo, shi = _declaration_span(current, sib.line) if sib.line else (0, len(current))
        sblock = current[slo:shi]
        new_sblock = _swap_version(sblock, sib.version, new_version)
        if new_sblock == sblock:
            blocked_on.append(
                f"{sib.where()} (also pins {sib.version}, but it was not found there)"
            )
            continue
        files[sib.path] = (original, current[:slo] + new_sblock + current[shi:])
        also_pinned_in.append(sib.where())

    for c in companions or []:
        if c.get("action") != "bump":
            if c.get("action") == "unresolved":
                blocked_on.append(
                    f"{c['var']} (pinned {c.get('current') or '?'}"
                    + (f", upstream suggests {c['required']}" if c.get("required") else ", unresolved")
                    + ")"
                )
            continue
        crel = c.get("file") or rel
        cfull = os.path.join(root, crel)
        if not os.path.isfile(cfull):
            blocked_on.append(f"{c['var']} (declared in {crel}, which is missing)")
            continue
        if crel not in files:
            ctext = open(cfull, encoding="utf-8", errors="replace").read()
            files[crel] = (ctext, ctext)
        original, current = files[crel]
        line_no = int(c.get("line") or 0)
        clo, chi = _declaration_span(current, line_no) if line_no else (0, len(current))
        cblock = current[clo:chi]
        new_cblock = _swap_version(cblock, c["current"], c["required"])
        if new_cblock == cblock:
            blocked_on.append(
                f"{c['var']} (could not locate {c['current']} at {crel}:{line_no})"
            )
            continue
        files[crel] = (original, current[:clo] + new_cblock + current[chi:])
        companion_edits.append({
            "var": c["var"], "file": crel, "line": line_no,
            "from": c["current"], "to": c["required"],
            "evidence": c.get("evidence", ""), "confidence": c.get("confidence", ""),
            "notes": list(c.get("notes") or []),
        })

    edits = [
        {"file": f, "diff": _unified(o, u, f), "_text": u}
        for f, (o, u) in files.items()
        if o != u
    ]

    return {
        "dep": dep.name,
        "file": rel,
        "from": old_version,
        "to": new_version,
        "new_url": new_url,
        "old_hash": dep.integrity,
        "new_hash": new_hash,
        # Coupled pins, with what this plan does about each. A bump that leaves
        # its companion behind is a classic silent breakage: it configures fine
        # and fails at link time with a missing symbol.
        "companion_pins": [p.render() for p in dep.companion_pins],
        "companions": list(companions or []),
        "companion_edits": companion_edits,
        # Further manifests pinning the same version, bumped in the same plan.
        "also_pinned_in": also_pinned_in,
        "blocked_on": blocked_on,
        "edits": edits,
        "diff": "".join(e["diff"] for e in edits),
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


def _edits_of(planned: dict) -> list[dict]:
    return planned.get("edits") or [
        {"file": planned["file"], "_text": planned.get("_text", "")}
    ]


def write(root: str, planned: dict) -> list[str]:
    """Apply every edit in the plan. Returns the files written."""
    written = []
    for edit in _edits_of(planned):
        full = os.path.join(root, edit["file"])
        shutil.copy2(full, full + ".deptool.bak")
        with open(full, "w", encoding="utf-8") as fh:
            fh.write(edit["_text"])
        written.append(edit["file"])
    return written


def revert(root: str, planned: dict) -> bool:
    """Restore every backup this plan made. True if anything was restored."""
    restored = False
    for edit in _edits_of(planned):
        full = os.path.join(root, edit["file"])
        bak = full + ".deptool.bak"
        if os.path.isfile(bak):
            shutil.move(bak, full)
            restored = True
    return restored


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
