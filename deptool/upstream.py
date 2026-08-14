"""What versions exist upstream, and what changed in them.

Uses `gh` when available (authenticated, so no 60/hour rate limit) and falls
back to plain HTTPS. Every function degrades to an empty result rather than
raising: a dependency we cannot resolve should be reported as such, not crash
the run.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import urllib.error
import urllib.request

from .model import Dep, bump_kind, parse_version

UA = {"User-Agent": "deptool/0.1 (+https://github.com/dgutson/update-tools)"}
TIMEOUT = 20


def _http_json(url: str, headers: dict | None = None):
    req = urllib.request.Request(url, headers={**UA, **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return json.load(r)
    except (urllib.error.URLError, ValueError, OSError):
        return None


def _gh_api(path: str):
    """GitHub API via `gh` if present, else anonymous HTTPS."""
    if shutil.which("gh"):
        try:
            out = subprocess.run(
                ["gh", "api", path, "--paginate"],
                capture_output=True, text=True, timeout=45,
            )
            if out.returncode == 0 and out.stdout.strip():
                # --paginate can emit several concatenated JSON arrays.
                chunks, dec, idx = [], json.JSONDecoder(), 0
                text = out.stdout.strip()
                while idx < len(text):
                    try:
                        obj, end = dec.raw_decode(text, idx)
                    except ValueError:
                        break
                    chunks.append(obj)
                    idx = end
                    while idx < len(text) and text[idx] in " \n\r\t":
                        idx += 1
                if not chunks:
                    return None
                if all(isinstance(c, list) for c in chunks):
                    return [item for c in chunks for item in c]
                return chunks[0]
        except (OSError, subprocess.SubprocessError):
            pass
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    hdr = {"Authorization": f"Bearer {token}"} if token else {}
    return _http_json(f"https://api.github.com/{path.lstrip('/')}", hdr)


# --------------------------------------------------------- files at a ref
#
# Companion-pin resolution needs to read a dependency's *own* build files at
# the tag we are considering upgrading to: that is where upstream states which
# version of its prebuilt engine / ABI level / protocol it requires.

MAX_FILE_BYTES = 512 * 1024


def _http_text(url: str, headers: dict | None = None) -> str:
    req = urllib.request.Request(url, headers={**UA, **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return r.read(MAX_FILE_BYTES).decode("utf-8", "replace")
    except (urllib.error.URLError, OSError):
        return ""


def fetch_file(repo: str, path: str, ref: str) -> str:
    """One file from a GitHub repo at a ref; "" when unavailable.

    raw.githubusercontent first — it costs no API quota. The contents API via
    `gh` is the fallback, which is also what makes this work for a private
    dependency.
    """
    text = _http_text(f"https://raw.githubusercontent.com/{repo}/{ref}/{path.lstrip('/')}")
    if text:
        return text
    data = _gh_api(f"repos/{repo}/contents/{path.lstrip('/')}?ref={ref}")
    if isinstance(data, dict) and data.get("encoding") == "base64":
        import base64

        try:
            return base64.b64decode(data.get("content") or "").decode("utf-8", "replace")
        except (ValueError, TypeError):
            return ""
    return ""


def list_files(repo: str, ref: str) -> list[str]:
    """Blob paths in a repo at a ref. Empty list when the tree is unavailable."""
    data = _gh_api(f"repos/{repo}/git/trees/{ref}?recursive=1")
    if not isinstance(data, dict):
        return []
    return [
        e.get("path", "")
        for e in (data.get("tree") or [])
        if isinstance(e, dict) and e.get("type") == "blob" and e.get("path")
    ]


MAX_TAG_TEMPLATES = 3


def tag_templates(versions: list[dict] | None) -> list[tuple[str, str]]:
    """Naming patterns upstream demonstrably uses, as (template, separator).

    Derived from its own `(version, tag)` pairs rather than assumed. This is what
    covers the pinned version being *pruned* from the catalogue — the common case,
    and the one that matters most, because the pin is one end of every diff. If
    openssl publishes 3.6.3 as `openssl-3.6.3`, then `openssl-3.2.1` is the
    reading of a declared convention rather than a guess.
    """
    out: list[tuple[str, str]] = []
    for v in versions or []:
        ver, tag = v.get("version") or "", v.get("tag") or ""
        if not ver or not tag or ver == tag:
            continue
        # How does the version appear inside its own tag? Verbatim, or with the
        # dots rewritten — curl publishes 8.21.0 as `curl-8_21_0`.
        for sep in (".", "_", "-"):
            spelling = ver.replace(".", sep)
            if spelling and spelling in tag:
                entry = (tag.replace(spelling, "{v}", 1), sep)
                if entry not in out:
                    out.append(entry)
                break
        if len(out) >= MAX_TAG_TEMPLATES:
            break
    return out


def tag_forms(version: str, versions: list[dict] | None = None) -> list[str]:
    """Candidate git refs for a version, best first.

    Prefers the tag upstream actually published, then the pattern it demonstrably
    follows, then the `v`-prefixed and bare spellings. Each candidate costs one
    tree listing when it misses, so the order is worth keeping honest: read,
    inferred, guessed.
    """
    out: list[str] = []
    target = parse_version(version)
    for v in versions or []:
        tag = v.get("tag") or ""
        if not tag:
            continue
        if v.get("version") == version or (target and parse_version(v.get("version", "")) == target):
            out.append(tag)
    for tmpl, sep in tag_templates(versions):
        cand = tmpl.replace("{v}", version.replace(".", sep))
        if cand and cand not in out:
            out.append(cand)
    for cand in (f"v{version}", version):
        if cand and cand not in out:
            out.append(cand)
    return out


# ------------------------------------------------- what changed, without notes
#
# Release notes are the weakest evidence in this tool, and sometimes there are
# none at all. `apidiff` answers "what changed" factually for C/C++ headers;
# these two cover the rest of the gap for every ecosystem, in descending order
# of trustworthiness: a migration guide is upstream telling you what will break,
# and a commit log is at least a record of what was done.

MIGRATION_DOCS = (
    "MIGRATING.md", "MIGRATION.md", "MIGRATION_GUIDE.md", "UPGRADING.md",
    "UPGRADE.md", "BREAKING_CHANGES.md", "BREAKING.md",
    "docs/migration.md", "docs/upgrading.md", "doc/migration.md",
)
# Matched against a repo's own file listing, because the name is not
# predictable: Symfony ships `UPGRADE-6.4.md`, so a fixed candidate list both
# misses it and spends ten fetches proving that the guesses do not exist.
_MIGRATION_DOC_RE = re.compile(
    r"^(?:docs?/)?(?:upgrad|migrat|breaking)[\w.-]*\.(?:md|rst|txt|adoc)$",
    re.I,
)
MAX_DOC_CHARS = 4000
MAX_COMMITS = 40
MAX_DOCS = 2


_DOC_SERIES_RE = re.compile(r"(\d+)\.(\d+)")


def migration_doc_paths(paths: list[str], version: str = "", since: str = "") -> list[str]:
    """Migration-guide-looking files in a repo listing, most relevant first.

    Projects that version these files keep every one of them — Symfony ships
    `UPGRADE-6.0.md` through `UPGRADE-7.1.md` — so plain name order hands back
    the oldest, describing a migration finished years ago. What is relevant is
    every guide covering a step between the version in use and the target: a
    dependency four releases behind has four migrations to make, not one.
    Unversioned names (`MIGRATING.md`) are always kept.
    """
    hits = [p for p in paths if _MIGRATION_DOC_RE.match(p)]
    lo, hi = parse_version(since), parse_version(version)
    if not hi:
        hits.sort(key=lambda p: (p.count("/"), len(p), p))
        return hits

    def rank(path: str):
        m = _DOC_SERIES_RE.search(path.rsplit("/", 1)[-1])
        if not m:
            return (1, path.count("/"), len(path), path)  # unversioned: keep
        series = (int(m.group(1)), int(m.group(2)))
        if series > hi[:2] or (lo and series <= lo[:2]):
            return (2, 0, 0, path)  # already done, or beyond where we are going
        # Nearest the target first — that is the step most likely to bite.
        return (0, hi[0] - series[0], hi[1] - series[1], path)

    hits.sort(key=rank)
    return [p for p in hits if rank(p)[0] != 2]

# Conventional-commit and plain-English markers for a change that can break us.
_BREAKING_RE = re.compile(
    r"BREAKING[ _-]?CHANGE|^\w+(?:\([^)]*\))?!:|\b(?:remove[sd]?|renam(?:e|ed|ing)|"
    r"delete[sd]?|drop(?:s|ped)?|deprecat\w*|replac\w*|incompatible)\b",
    re.I | re.M,
)


def migration_docs(repo: str, ref: str, paths: list[str] | None = None) -> list[dict]:
    """Upstream's own migration notes at a ref, when it keeps any.

    Stronger evidence than a changelog: a project that writes one of these is
    telling you what it expects to break, in the words of whoever broke it.

    `paths` is the doc paths to try, in order, as produced by
    `migration_doc_paths` from a listing the caller already has — the header
    diff does — which turns this from ten speculative fetches into only fetching
    files known to exist. The order is the caller's and is preserved: re-sorting
    it here discarded the version ranking and fetched Symfony's `UPGRADE-6.0.md`
    when the target was 6.4. Non-doc paths are dropped, so handing over a whole
    repository listing is safe if wasteful.
    """
    if paths is None:
        candidates = list(MIGRATION_DOCS)
    else:
        candidates = [p for p in paths if _MIGRATION_DOC_RE.match(p)]
    out = []
    for path in candidates:
        text = fetch_file(repo, path, ref)
        if not text.strip():
            continue
        out.append({"path": path, "text": text.strip()[:MAX_DOC_CHARS],
                    "truncated": len(text.strip()) > MAX_DOC_CHARS})
        if len(out) >= MAX_DOCS:
            break
    return out


def compare_commits(repo: str, from_ref: str, to_ref: str) -> dict:
    """Commit subjects between two tags — the fallback when notes are empty.

    Subjects only. A full commit-message body is mostly noise and the payload is
    going to a model with a finite context; what is wanted is the shape of the
    release and, specifically, which commits announce a break.
    """
    data = _gh_api(f"repos/{repo}/compare/{from_ref}...{to_ref}")
    if not isinstance(data, dict) or not isinstance(data.get("commits"), list):
        return {"resolved": False, "reason": f"could not compare {from_ref}...{to_ref}"}

    subjects, breaking = [], []
    for c in data["commits"]:
        if not isinstance(c, dict):
            continue
        message = ((c.get("commit") or {}).get("message") or "").strip()
        if not message:
            continue
        subject = message.split("\n", 1)[0][:160]
        subjects.append(subject)
        # Match against the whole message: `BREAKING CHANGE:` lives in the body
        # by convention, not in the subject.
        if _BREAKING_RE.search(message):
            breaking.append(subject)

    total = data.get("total_commits")
    return {
        "resolved": True,
        "total": total if isinstance(total, int) else len(subjects),
        "shown": min(len(subjects), MAX_COMMITS),
        "breaking": breaking[:MAX_COMMITS],
        "subjects": subjects[:MAX_COMMITS],
    }


def change_prose(
    repo: str,
    from_ref: str,
    to_ref: str,
    notes: str = "",
    doc_paths: list[str] | None = None,
) -> dict:
    """Non-header evidence for what changed, gathered only when it can help.

    The commit log is a *fallback*: skipped when release notes already say
    something, because a hundred commit subjects would then be noise competing
    with a written summary. Migration guides are always worth a look — they are
    better evidence than the notes, not a substitute for them.
    """
    out: dict = {"migration_docs": [], "commits": {}}
    if not repo or "/" not in repo:
        return out
    if to_ref:
        out["migration_docs"] = migration_docs(repo, to_ref, doc_paths)
    if not notes.strip() and from_ref and to_ref:
        out["commits"] = compare_commits(repo, from_ref, to_ref)
        out["commits"]["why"] = "release notes for the target version were empty"
    return out


# ------------------------------------------------------------------ providers


# A tag that carries the project name, e.g. `yaml-cpp-0.6.3`, `release-1.11.0`.
_TAG_VERSION_RE = re.compile(r"(?:^|[-_/])v?(\d+(?:\.\d+)*(?:[-.]?[A-Za-z][\w.]*)?)$")


def version_from_tag(tag: str) -> str:
    """The version a tag names, with any project-name prefix removed.

    `v1.2.3` is the common case and is handled by the strip alone. But plenty of
    projects prefix the tag with their own name — yaml-cpp publishes
    `yaml-cpp-0.6.3`, googletest published `release-1.11.0` — and those parse to
    None, which silently removed every such release from consideration: no
    version comparison, no upgrade found, and no readable ref for the header
    diff or companion resolution to work from.
    """
    stripped = tag[1:] if tag[:1] in "vV" and tag[1:2].isdigit() else tag
    if parse_version(stripped):
        return stripped
    m = _TAG_VERSION_RE.search(tag)
    return m.group(1) if m else stripped


def _github_versions(repo: str) -> list[dict]:
    out: list[dict] = []
    releases = _gh_api(f"repos/{repo}/releases?per_page=100")
    if isinstance(releases, list):
        for r in releases:
            if not isinstance(r, dict) or r.get("draft"):
                continue
            tag = r.get("tag_name") or ""
            if not tag:
                continue
            out.append({
                "version": version_from_tag(tag),
                "tag": tag,
                "date": (r.get("published_at") or "")[:10],
                "prerelease": bool(r.get("prerelease")),
                "notes": (r.get("body") or "").strip(),
                "url": r.get("html_url") or "",
            })
    if not out:
        tags = _gh_api(f"repos/{repo}/tags?per_page=100")
        if isinstance(tags, list):
            for t in tags:
                if not isinstance(t, dict):
                    continue
                tag = t.get("name") or ""
                if tag and parse_version(version_from_tag(tag)):
                    out.append({
                        "version": version_from_tag(tag), "tag": tag, "date": "",
                        "prerelease": False, "notes": "",
                        "url": f"https://github.com/{repo}/releases/tag/{tag}",
                    })
    return out


def _pypi_versions(name: str) -> list[dict]:
    data = _http_json(f"https://pypi.org/pypi/{name}/json")
    if not data:
        return []
    out = []
    for ver, files in (data.get("releases") or {}).items():
        date = ""
        if files and isinstance(files, list) and isinstance(files[0], dict):
            date = (files[0].get("upload_time_iso_8601") or "")[:10]
        out.append({"version": ver, "tag": ver, "date": date,
                    "prerelease": bool(parse_version(ver) and parse_version(ver)[3] == 0),
                    "notes": "", "url": f"https://pypi.org/project/{name}/{ver}/"})
    return out


def _npm_versions(name: str) -> list[dict]:
    data = _http_json(f"https://registry.npmjs.org/{name}")
    if not data:
        return []
    times = data.get("time") or {}
    return [
        {"version": v, "tag": v, "date": (times.get(v) or "")[:10],
         "prerelease": "-" in v, "notes": "",
         "url": f"https://www.npmjs.com/package/{name}/v/{v}"}
        for v in (data.get("versions") or {})
    ]


def _crates_versions(name: str) -> list[dict]:
    data = _http_json(f"https://crates.io/api/v1/crates/{name}")
    if not data:
        return []
    return [
        {"version": v.get("num", ""), "tag": v.get("num", ""),
         "date": (v.get("created_at") or "")[:10],
         "prerelease": bool(v.get("yanked")) or "-" in str(v.get("num", "")),
         "notes": "", "url": f"https://crates.io/crates/{name}/{v.get('num','')}"}
        for v in (data.get("versions") or []) if not v.get("yanked")
    ]


# Conan Center's recipe index is a GitHub repository, which this module already
# knows how to read, and `recipes/<name>/config.yml` is the authoritative list of
# what a project can actually pin — a version absent from it cannot be resolved
# by the build no matter what upstream has released. One file per dependency, and
# no API quota when raw.githubusercontent serves it.
CONAN_INDEX_REPO = "conan-io/conan-center-index"
# Version keys sit at exactly one level of indentation under `versions:`, and
# carry no inline value; `folder: all` below them does. A trailing comment is
# allowed because the index uses them — openssl annotates its LTS and FIPS
# versions that way (`3.5.7: # LTS: keep until…`), and requiring end-of-line
# skipped exactly those versions, which then reported as absent from the recipe.
_CONAN_VERSION_KEY = re.compile(r'^  "?([^"\s:#][^"\s:]*)"?\s*:\s*(?:#.*)?$', re.M)


def _conan_versions(name: str) -> list[dict]:
    """Versions of a Conan recipe, newest-first ordering left to the caller.

    `config.yml` records no dates, so `date` is empty — `summarise` already
    tolerates that, and an unknown date is better than a guessed one.
    """
    folders = _conan_folders(name)
    if not folders:
        return []
    # The tag is the one thing `config.yml` does not record, and guessing it is
    # what made the header diff unavailable for openssl and libcurl.
    tags = _conan_tags(name, folders)
    out = []
    for ver in folders:
        if not parse_version(ver):
            continue
        out.append({
            "version": ver, "tag": tags.get(ver) or ver, "date": "",
            "prerelease": bool(parse_version(ver) and parse_version(ver)[3] == 0),
            "notes": "",
            "url": f"https://conan.io/center/recipes/{name}?version={ver}",
        })
    return out


# A recipe name is not a repository, which is why the API-surface diff — the
# tool's central mechanism — has never been able to run on a Conan dependency.
# The recipe itself declares where its sources come from, so read that. A
# hand-written recipe -> repo table is what the generic-rule principle rejects,
# and `DISTRO_GITHUB` below is already one such table too many.
_CONAN_FOLDER = re.compile(r'^\s+folder:\s*"?([^"\s]+?)"?\s*(?:#.*)?$')
_GITHUB_URL = re.compile(
    r"https?://(?:www\.)?github\.com"
    r"/([A-Za-z0-9._-]+)/([A-Za-z0-9._-]+?)(?:\.git)?(?=[/\"'\s]|$)"
)


def _conan_folders(name: str) -> dict[str, str]:
    """version -> recipe folder, from `config.yml`.

    A recipe is not always one folder: openssl splits `3.x.x` from `4.x.x`, so
    the folder cannot be assumed to be `all`.
    """
    text = fetch_file(CONAN_INDEX_REPO, f"recipes/{name}/config.yml", "master")
    out: dict[str, str] = {}
    current = ""
    for line in text.partition("versions:")[2].splitlines():
        key = _CONAN_VERSION_KEY.match(line)
        if key:
            current = key.group(1)
            continue
        folder = _CONAN_FOLDER.match(line)
        if folder and current:
            out[current] = folder.group(1)
            current = ""
    return out


def _mapping_block(text: str, key: str) -> str:
    """One top-level mapping of a `conandata.yml`, and nothing else.

    Stopping at the next top-level key is what keeps `sources:` and `patches:`
    apart, and both readers need that: a patch URL is not where the library's
    own code lives, and a source URL is not a modification of it.
    """
    m = re.search(rf"^{re.escape(key)}:\s*$", text, re.M)
    if not m:
        return ""
    out: list[str] = []
    for line in text[m.end():].splitlines():
        if line.strip() and not line.startswith((" ", "\t")):
            break
        out.append(line)
    return "\n".join(out)


def _version_blocks(block: str) -> dict[str, str]:
    """Every version key of a mapping -> its own lines.

    Everything more deeply indented belongs to the version, which is what makes
    the per-OS/per-architecture recipes (cmake ships one URL per platform) fall
    out without a special case.
    """
    out: dict[str, list[str]] = {}
    current = ""
    for line in block.splitlines():
        key = _CONAN_VERSION_KEY.match(line)
        if key:
            current = key.group(1)
            out.setdefault(current, [])
            continue
        if current:
            out[current].append(line)
    return {k: "\n".join(v) for k, v in out.items()}


def _version_block(block: str, version: str) -> str:
    return _version_blocks(block).get(version, "")


# Where a GitHub source URL carries the tag it was cut from. Both forms appear
# throughout the index: a release asset lives under the tag, and an archive is
# named after it.
_TAG_IN_RELEASE = re.compile(r"/releases/download/([^/]+)/")
_TAG_IN_ARCHIVE = re.compile(
    r"/archive/(?:refs/tags/)?([^/]+?)(?:\.tar\.[a-z0-9]+|\.tgz|\.zip|\.tar)$"
)


def _tag_from_url(url: str) -> str:
    """The git tag a source URL points at; "" when it carries none."""
    for pattern in (_TAG_IN_RELEASE, _TAG_IN_ARCHIVE):
        m = pattern.search(url)
        if m:
            return m.group(1)
    return ""


def _conan_tags(name: str, folders: dict[str, str]) -> dict[str, str]:
    """version -> the tag upstream actually published, read from the recipe.

    `tag_forms` guesses `v<version>` and `<version>`. That is wrong for every
    project which prefixes its tags — openssl publishes `openssl-3.6.3`, curl
    publishes `curl-8_21_0` — and a wrong guess does not degrade the answer, it
    removes it: the diff reports the tag as unreadable and falls back to release
    notes. The recipe's own source URL contains the tag, so read it rather than
    guessing, exactly as the source repository itself is read.

    One fetch per distinct recipe folder (one for most recipes, two for openssl),
    and it yields the tag for *every* version at once, which is what both ends of
    a diff need.
    """
    out: dict[str, str] = {}
    for folder in sorted(set(folders.values())) or ["all"]:
        text = fetch_file(
            CONAN_INDEX_REPO, f"recipes/{name}/{folder}/conandata.yml", "master"
        )
        for ver, body in _version_blocks(_mapping_block(text, "sources")).items():
            gh = _GITHUB_URL.search(body)
            if not gh:
                continue
            # A mirror list can put other hosts first, so start from the GitHub
            # match and take that URL whole.
            url = re.search(r"https?://[^\s\"']+", body[gh.start():])
            tag = _tag_from_url(url.group(0)) if url else ""
            if tag:
                out.setdefault(ver, tag)
    return out


def _recipe_conandata(name: str, version: str) -> tuple[str, str]:
    """(path, text) of the `conandata.yml` that governs a pinned version.

    Split out because the source repository and the patch set are two answers
    from one file, and reading it twice per dependency was pure waste.
    """
    folders = _conan_folders(name)
    # The catalogue is pruned rather than historical, so the pinned version is
    # often already gone (the same fact `pin_unavailable` reports). The recipe is
    # still the right file to read; only the per-version lookups inside it become
    # unanswerable, and each of those says so for itself.
    folder = folders.get(version) or next(iter(folders.values()), "all")
    rel = f"recipes/{name}/{folder}/conandata.yml"
    return rel, fetch_file(CONAN_INDEX_REPO, rel, "master")


def conan_source_repo(name: str, version: str = "") -> tuple[str, str]:
    """`owner/repo` the recipe takes its sources from, plus the evidence for it.

    Returns `("", reason)` when the sources are not on GitHub. That is common
    and not a failure: pkgconf ships from distfiles.ariadne.space, xz_utils from
    tukaani.org, libiconv from a GNU mirror. Saying so is the honest answer, and
    the caller reports the dependency as undiffable rather than unaffected.

    The first URL is deliberately *not* the answer. Several recipes list a
    mirror ahead of the canonical repository — zlib leads with zlib.net and
    libcurl with curl.se — so the rule is the first URL that is a GitHub
    repository, not the first URL.
    """
    rel, text = _recipe_conandata(name, version)
    return _source_repo_from(rel, text, version)


def _source_repo_from(rel: str, text: str, version: str) -> tuple[str, str]:
    block = _mapping_block(text, "sources")
    if not block:
        return "", f"{rel} declares no sources, so there is no repository to read"

    own = _version_block(block, version)
    m = _GITHUB_URL.search(own) if own else None
    if m:
        return f"{m.group(1)}/{m.group(2)}", f"{rel} declares {m.group(0)} for {version}"
    # Falling back to the rest of the mapping is the whole point when the pinned
    # version has been pruned; the repository does not change per version.
    m = _GITHUB_URL.search(block)
    if m:
        return f"{m.group(1)}/{m.group(2)}", (
            f"{rel} declares {m.group(0)} for another version of the recipe; "
            f"{version or 'the pinned version'} is not in it"
        )
    return "", (
        f"{rel} declares no GitHub source URL — this recipe builds from "
        f"elsewhere, so its API surface cannot be diffed"
    )


_PATCH_ITEM = re.compile(r'^\s+-\s+patch_file:\s*"?([^"\n]+?)"?\s*$')
_PATCH_FIELD = re.compile(r'^\s+(patch_description|patch_type):\s*"?(.*?)"?\s*$')


def conan_recipe_patches(name: str, version: str) -> tuple[list[dict], bool]:
    """Patches Conan Center applies to a recipe, and whether that answer is known.

    This is the quiet half of "what actually ships". A Conan package is not
    upstream's release even when nobody here has touched it: the recipe carries
    its own patches and they are applied before the library is built. zlib 1.3.2
    has one, libarchive 3.8.7 has several, openssl and libcurl have none. So an
    upstream-derived claim — the header diff, the release notes, an advisory
    match — is about a *nearby* artifact rather than the one that ships.

    Returns `(patches, authoritative)`. `authoritative` is False when the pinned
    version is no longer in the recipe, because an empty list then means "cannot
    tell" rather than "none"; the pruned catalogue makes that common and the two
    must not be reported the same way.
    """
    return _recipe_patches_from(_recipe_conandata(name, version)[1], version)


def _recipe_patches_from(text: str, version: str) -> tuple[list[dict], bool]:
    # The version must appear under `sources:` for its absence from `patches:` to
    # mean anything. A recipe that never mentions it tells us nothing either way.
    known = bool(_version_block(_mapping_block(text, "sources"), version))

    out: list[dict] = []
    for line in _version_block(_mapping_block(text, "patches"), version).splitlines():
        item = _PATCH_ITEM.match(line)
        if item:
            out.append({"file": item.group(1), "description": "", "type": ""})
            continue
        field = _PATCH_FIELD.match(line)
        if field and out:
            key = "description" if field.group(1) == "patch_description" else "type"
            out[-1][key] = field.group(2)
    return out, known


def patch_evidence(name: str, version: str, patches: list[dict], known: bool) -> str:
    """One line for the report, or "" when there is nothing worth saying.

    `patch_type` is a declared field, so the weighting reads it instead of
    guessing: Conan marks build-system plumbing as `conan`, and a recipe whose
    patches are all of that kind is much less likely to have moved the API than
    one carrying an untyped or behavioural patch.
    """
    if not known:
        return (
            f"whether Conan Center patches {name} at {version} cannot be "
            f"established — that version is no longer in the recipe, so what "
            f"shipped is not reconstructible from the index"
        )
    if not patches:
        return ""
    kinds = {p["type"] for p in patches}
    plumbing = kinds == {"conan"}
    detail = "; ".join(p["description"] or p["file"] for p in patches[:3])
    if len(patches) > 3:
        detail += "; …"
    weight = (
        "all declared `patch_type: conan`, i.e. build-system plumbing rather than "
        "behaviour" if plumbing else
        "at least one is not declared as build-system plumbing, so it may change "
        "behaviour"
    )
    return (
        f"Conan Center applies {len(patches)} patch(es) to {name} {version} before "
        f"building — {weight} — so what ships is not upstream's release: {detail}"
    )


def conan_recipe_facts(name: str, version: str) -> dict:
    """Both answers the recipe holds, from one read of `conandata.yml`.

    `check` wants the source repository *and* the patch set for the same
    dependency, and they live in the same file — so this exists to stop that
    being two fetches of it.
    """
    rel, text = _recipe_conandata(name, version)
    repo, repo_why = _source_repo_from(rel, text, version)
    patches, known = _recipe_patches_from(text, version)
    return {
        "repo": repo,
        "repo_why": repo_why,
        "patches": patches,
        "patches_known": known,
        "patch_why": patch_evidence(name, version, patches, known),
    }


def _repology_versions(name: str) -> list[dict]:
    """Distro-provided libs: what do the major distros ship?

    This is how a pkg-config dependency like fluidsynth becomes visible at
    all — there is no manifest to bump, so 'newer' means 'newer than what
    your build machines install'.
    """
    data = _http_json(f"https://repology.org/api/v1/project/{name.lower()}")
    if not isinstance(data, list):
        return []
    best: dict[str, dict] = {}
    for pkg in data:
        if not isinstance(pkg, dict):
            continue
        ver = pkg.get("version") or ""
        if not ver or pkg.get("status") in ("ignored", "incorrect", "untrusted"):
            continue
        entry = best.setdefault(ver, {
            "version": ver, "tag": ver, "date": "", "notes": "", "url": "",
            "prerelease": pkg.get("status") == "devel", "repos": [],
        })
        repo = pkg.get("repo") or ""
        if repo and repo not in entry["repos"]:
            entry["repos"].append(repo)
    for v in best.values():
        shown = sorted(v["repos"])[:6]
        v["notes"] = "shipped by: " + ", ".join(shown) + ("…" if len(v["repos"]) > 6 else "")
        v["url"] = f"https://repology.org/project/{name.lower()}/versions"
        v.pop("repos", None)
    return list(best.values())


# Distro package names differ from the pkg-config module / find_package name.
DISTRO_ALIAS = {
    "ALSA": "alsa-lib",
    "alsa": "alsa-lib",
    "fluidsynth": "fluidsynth",
    "yaml-cpp": "yaml-cpp",
    "libremidi": "libremidi",
    "sqlite3": "sqlite",
    "openssl": "openssl",
    "zlib": "zlib",
}

# Distro-shipped libs that also have a canonical upstream repo worth reading
# release notes from.
DISTRO_GITHUB = {
    "alsa-lib": "alsa-project/alsa-lib",
    "fluidsynth": "FluidSynth/fluidsynth",
    "yaml-cpp": "jbeder/yaml-cpp",
    "sqlite": "sqlite/sqlite",
    "openssl": "openssl/openssl",
    "zlib": "madler/zlib",
}


def fetch_versions(dep: Dep) -> list[dict]:
    kind, ref = dep.upstream.kind, dep.upstream.ref
    if kind == "github" and "/" in ref:
        return _github_versions(ref)
    if kind == "pypi":
        return _pypi_versions(ref)
    if kind == "npm":
        return _npm_versions(ref)
    if kind == "crates":
        return _crates_versions(ref)
    if kind == "conan":
        return _conan_versions(ref)
    if kind == "distro":
        alias = DISTRO_ALIAS.get(ref, ref)
        versions = _repology_versions(alias)
        gh = DISTRO_GITHUB.get(alias)
        if gh:
            for r in _github_versions(gh):
                if not any(v["version"] == r["version"] for v in versions):
                    versions.append(r)
                else:
                    for v in versions:
                        if v["version"] == r["version"]:
                            v["notes"] = (v["notes"] + "\n\n" + r["notes"]).strip()
                            v["url"] = r["url"] or v["url"]
        return versions
    return []


def newer_than(current: str, versions: list[dict], allow_prerelease: bool = False) -> list[dict]:
    cur = parse_version(current)
    out = []
    for v in versions:
        pv = parse_version(v.get("version", ""))
        if not pv:
            continue
        if v.get("prerelease") and not allow_prerelease:
            continue
        if cur is None or pv > cur:
            out.append(v)
    out.sort(key=lambda v: parse_version(v["version"]) or (0,), reverse=True)
    return out


# Sources whose catalogue is the *currently offered* set rather than a history.
# Conan Center deletes old recipe versions — `zlib` lists exactly one — so a
# count of what is newer is a floor on how far behind we are, not the number of
# releases missed, and saying "1 version behind" without that caveat is the kind
# of confidently wrong number this tool exists to avoid.
_PRUNED_CATALOGUE = {"conan"}

# Sources whose catalogue is authoritative about what can still be installed, so
# a pin missing from it is a finding rather than a gap in our own data. Repology
# is a survey of what distros happen to ship, and a GitHub release listing can be
# paginated short, so neither qualifies.
_AUTHORITATIVE = {"conan", "pypi", "npm", "crates"}


def summarise(dep: Dep, allow_prerelease: bool = False) -> dict:
    """Everything the judgement layer needs about one dependency's upgrades."""
    current = dep.version or dep.installed_version
    versions = fetch_versions(dep)
    if not versions:
        return {
            "resolved": False,
            "reason": f"no upstream resolver for {dep.upstream.kind or 'unknown'}"
                      f":{dep.upstream.ref or '?'}",
            "current": current, "latest": "", "available": [],
        }
    ahead = newer_than(current, versions, allow_prerelease)
    latest = ahead[0]["version"] if ahead else current
    # The tag spelling for the version in use, so companion-pin resolution can
    # read the dependency's build files at exactly that ref instead of guessing.
    cur_parsed = parse_version(current)

    def _is_current(v: dict) -> bool:
        return bool(
            v.get("version") == current
            or (cur_parsed and parse_version(v.get("version", "")) == cur_parsed)
        )

    current_tag = next((v.get("tag", "") for v in versions if _is_current(v)), "")
    pruned = dep.upstream.kind in _PRUNED_CATALOGUE
    pin_missing = (
        bool(current)
        and dep.upstream.kind in _AUTHORITATIVE
        and not any(_is_current(v) for v in versions)
    )

    if not current:
        # An unpinned system dependency has no version gap to measure: the
        # effective version is whatever the build machine installs. Saying
        # "101 releases behind" would be nonsense. Report the ceiling and say
        # plainly that the repo does not control this.
        return {
            "resolved": True,
            "source": dep.upstream.kind,
            "unpinned": True,
            "current": "",
            "latest": latest,
            "behind_by": None,
            "bump": "n/a",
            "reason": "not pinned in this repo — the build environment decides "
                      "which version is used",
            "available": [{**v, "notes": (v.get("notes") or "")[:1500]} for v in ahead[:3]],
        }

    return {
        "resolved": True,
        "source": dep.upstream.kind,
        "unpinned": False,
        "current": current,
        "current_tag": current_tag,
        "latest": latest,
        "behind_by": len(ahead),
        # `behind_by` counts entries in the catalogue, which for a pruned one is
        # a lower bound rather than a release count.
        "behind_by_is_floor": pruned,
        # The pinned version is not offered any more: a fresh install cannot
        # reproduce this build. A finding in its own right, independent of
        # whether an upgrade is otherwise due.
        "pin_unavailable": pin_missing,
        "bump": bump_kind(current, latest) if ahead else "same",
        # Cap the payload: the newest few carry the signal, and release notes
        # are long.
        "available": [
            {**v, "notes": (v.get("notes") or "")[:4000]} for v in ahead[:8]
        ],
    }


def _osv_query(body: dict) -> list[dict]:
    req = urllib.request.Request(
        "https://api.osv.dev/v1/query",
        data=json.dumps(body).encode(),
        headers={**UA, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return json.load(r).get("vulns") or []
    except (urllib.error.URLError, ValueError, OSError):
        return []


# A version no real release will ever reach. Anything OSV still reports as
# affected at this version is not actually being version-filtered.
_SENTINEL_VERSION = "99999.0.0"

MAX_ADVISORIES = 25


def advisories(dep: Dep) -> list[dict]:
    """Known vulnerabilities from OSV.dev for the version in use.

    OSV can only evaluate a version range when it knows the ecosystem. For a
    distro-provided C library there is no ecosystem to give — the upstream
    version string ("2.3.4") is not the Debian package version — so OSV falls
    back to returning advisories whose ranges it cannot evaluate. Empirically,
    a nonexistent fluidsynth 99999.0.0 still "matches" 14 advisories.

    Presenting those as confirmed hits would be wrong, so probe with a sentinel
    version and mark anything that matches it as unverified. The judgement
    layer ranks confirmed hits above unverified ones instead of treating a
    long list as a long list of problems.
    """
    ecosystem = {
        "pypi": "PyPI", "npm": "npm", "crates": "crates.io", "gomod": "Go",
    }.get(dep.upstream.kind, "")
    version = dep.version or dep.installed_version

    if ecosystem:
        package = {"name": dep.upstream.ref, "ecosystem": ecosystem}
    elif dep.upstream.kind == "github" and "/" in dep.upstream.ref:
        package = {"name": f"https://github.com/{dep.upstream.ref}"}
    else:
        package = {"name": DISTRO_ALIAS.get(dep.name, dep.name)}

    body: dict = {"package": package}
    if version:
        body["version"] = version
    vulns = _osv_query(body)
    if not vulns:
        return []

    # Only ecosystem-less queries need the sentinel check; a proper ecosystem
    # query is evaluated exactly.
    unverifiable: set[str] = set()
    if version and not ecosystem:
        sentinel = _osv_query({"package": package, "version": _SENTINEL_VERSION})
        unverifiable = {v.get("id", "") for v in sentinel}

    out = []
    for v in vulns[:MAX_ADVISORIES]:
        sev = ""
        for s in v.get("severity") or []:
            sev = s.get("score", "") or sev
        for a in v.get("affected") or []:
            db = (a.get("database_specific") or {})
            sev = db.get("severity", "") or sev
        vid = v.get("id", "")
        out.append({
            "id": vid,
            "summary": (v.get("summary") or "").strip()[:300],
            "severity": sev,
            "fixed": _first_fixed(v),
            "version_verified": vid not in unverifiable,
            "url": f"https://osv.dev/vulnerability/{vid}",
        })
    # Confirmed hits first.
    out.sort(key=lambda a: (not a["version_verified"], a["id"]))
    if len(vulns) > MAX_ADVISORIES:
        out.append({
            "id": f"(+{len(vulns) - MAX_ADVISORIES} more not shown)",
            "summary": "", "severity": "", "fixed": "",
            "version_verified": False, "url": "",
        })
    return out


def _first_fixed(vuln: dict) -> str:
    for a in vuln.get("affected") or []:
        for r in a.get("ranges") or []:
            for ev in r.get("events") or []:
                if ev.get("fixed"):
                    return ev["fixed"]
    return ""
