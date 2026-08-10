"""What versions exist upstream, and what changed in them.

Uses `gh` when available (authenticated, so no 60/hour rate limit) and falls
back to plain HTTPS. Every function degrades to an empty result rather than
raising: a dependency we cannot resolve should be reported as such, not crash
the run.
"""

from __future__ import annotations

import json
import os
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


# ------------------------------------------------------------------ providers


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
                "version": tag.lstrip("vV"),
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
                if parse_version(tag):
                    out.append({
                        "version": tag.lstrip("vV"), "tag": tag, "date": "",
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

    if not current:
        # An unpinned system dependency has no version gap to measure: the
        # effective version is whatever the build machine installs. Saying
        # "101 releases behind" would be nonsense. Report the ceiling and say
        # plainly that the repo does not control this.
        return {
            "resolved": True,
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
        "unpinned": False,
        "current": current,
        "latest": latest,
        "behind_by": len(ahead),
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
