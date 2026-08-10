"""Content hashing used to answer 'is CLAUDE_DEPS.md still accurate?'

Deliberately dumb and deterministic: no LLM is needed to *detect* drift,
only to repair it.
"""

from __future__ import annotations

import hashlib
import os


def hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:12]


def hash_files(root: str, rel_paths: list[str]) -> str:
    """Stable hash over the *content* of the given files.

    Missing files hash as the literal b"<missing>" so that deleting a file
    changes the fingerprint rather than silently matching.
    """
    h = hashlib.sha256()
    for rel in sorted(set(p for p in rel_paths if p)):
        h.update(rel.encode("utf-8", "replace"))
        h.update(b"\0")
        full = os.path.join(root, rel)
        try:
            with open(full, "rb") as fh:
                h.update(fh.read())
        except OSError:
            h.update(b"<missing>")
        h.update(b"\0")
    return h.hexdigest()[:12]


def declaration_span(text: str, line_no: int) -> tuple[int, int]:
    """Character range of the balanced command starting at `line_no`.

    Used both for scoping edits and for fingerprinting. Hashing the whole
    declaring file would be useless in CMake, where every dependency lives in
    the same CMakeLists.txt and any edit would mark them all as drifted.
    """
    offsets = [0]
    for ln in text.splitlines(keepends=True):
        offsets.append(offsets[-1] + len(ln))
    if line_no < 1 or line_no > len(offsets) - 1:
        return 0, len(text)
    start = offsets[line_no - 1]
    open_paren = text.find("(", start)
    if open_paren == -1:
        return start, offsets[min(line_no, len(offsets) - 1)]
    depth, i = 1, open_paren + 1
    while i < len(text) and depth:
        if text[i] == "(":
            depth += 1
        elif text[i] == ")":
            depth -= 1
        i += 1
    return start, i


def hash_declaration(root: str, declared_in: str) -> str:
    """Hash just this dependency's own declaration, not its whole file."""
    if not declared_in:
        return hash_text("<none>")
    path, _, line = declared_in.partition(":")
    full = os.path.join(root, path)
    try:
        text = open(full, encoding="utf-8", errors="replace").read()
    except OSError:
        return hash_text("<missing>")
    try:
        line_no = int(line)
    except ValueError:
        return hash_text(text)
    lo, hi = declaration_span(text, line_no)
    return hash_text(text[lo:hi])


def compare(old: dict[str, str], new: dict[str, str]) -> list[str]:
    """Return the names of fingerprint components that drifted."""
    drifted = []
    for key in sorted(set(old) | set(new)):
        if old.get(key) != new.get(key):
            drifted.append(key)
    return drifted
