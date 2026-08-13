"""deptool — the deterministic half of the dependency reviewer.

Everything printed here is evidence. The judgement ("is this upgrade worth
it") is deliberately *not* made here — that is the LLM's job, and it needs
this output as input.

    python3 -m deptool profile   [--root R] [--json]
    python3 -m deptool status    [--root R] [--json]
    python3 -m deptool check     [--root R] [--json] [--pre] [--only NAME]
    python3 -m deptool apidiff   --dep NAME [--to VERSION] [--from VERSION]
    python3 -m deptool plan      --dep NAME --to VERSION [--root R]
    python3 -m deptool apply     --dep NAME --to VERSION [--root R] [--verify]
    python3 -m deptool revert    --dep NAME --to VERSION [--root R]

`plan` and `apply` also resolve coupled pins — a second version that must move
with the dependency — and bump them in the same edit. `apply` refuses when one
cannot be resolved; `--ignore-companions` overrides that.

`check` diffs each C/C++ dependency's public headers between the pinned and the
target tag and intersects the result with what we consume, so the
breaking-change evidence does not depend on upstream having written good release
notes. `apidiff` runs that alone, over more headers.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date

from . import apply as apply_mod
from . import apidiff, backends, companion, discover, profile, sources, upstream
from .fingerprint import compare
from .model import Dep


_COMPANION_MARK = {"bump": "+>", "unchanged": "==", "unresolved": "!?"}

# Header diffing costs one fetch per header per ref, so it is worth doing only
# where it can produce a finding: a pinned C/C++ dependency, behind, with a
# readable GitHub upstream.
_CXX_KINDS = ("cmake", "conan", "vcpkg", "pkg-config")


def _worth_diffing(dep: Dep, info: dict) -> bool:
    """Whether a header diff could produce a finding for this dependency.

    Deliberately *not* conditioned on `dep.consumed`. It used to be, which meant
    a dependency our extractor found no symbols for was skipped in silence and
    reported exactly like one with nothing to worry about. Now the diff runs —
    it can widen `consumed` from upstream's own declarations — and the one case
    that still cannot produce a finding says so out loud.
    """
    return bool(
        dep.upstream.kind == "github"
        and info.get("resolved")
        and info.get("behind_by")
        and not info.get("unpinned")
        and (dep.kind.startswith(_CXX_KINDS) or any(
            "/" in s or s.endswith(".h") for s in apidiff.include_hints(dep)
        ))
    )


def _api_lines(api: dict, indent: str = "   ") -> list[str]:
    """Render a header diff. `!!` is a symbol we consume that has gone."""
    out = []
    if not api:
        return out
    if not api.get("resolved"):
        if api.get("reason"):
            out.append(f"{indent}api: {api['reason']}")
        return out
    head = (
        f"{indent}api: {api['from_ref']} -> {api['to_ref']}, "
        f"{api['headers_read']}/{api['headers_available']} public header(s)"
    )
    out.append(head)
    for hit in api.get("affects_us") or []:
        where = f" at {', '.join(hit['sites'][:3])}" if hit.get("sites") else ""
        if hit["change"] == "removed":
            mark = "!!" if hit.get("confirmed") else "!?"
            out.append(f"{indent}  {mark} {hit['symbol']} — removed ({hit['kind']}){where}")
        else:
            out.append(f"{indent}  ~~ {hit['symbol']} — signature changed{where}")
            # `before`/`after` hold only what differs, so one side is empty when
            # an overload was purely added or purely dropped.
            if hit["before"]:
                out.append(f"{indent}     gone: {', '.join(hit['before'])}")
            if hit["after"]:
                out.append(f"{indent}     now:  {', '.join(hit['after'])}")
    for ren in api.get("likely_renames") or []:
        out.append(
            f"{indent}  ?> {ren['from']} -> {ren['to']}? same signature, "
            f"names {int(ren['similarity'] * 100)}% alike [inferred]"
        )
    if not api.get("affects_us"):
        if api.get("consumed_count") == 0:
            # An empty intersection with an empty input is a fact about our
            # extractor, not about the upgrade.
            out.append(
                f"{indent}  ?? no consumed surface extracted, so none of the "
                f"{len(api.get('removed') or [])} removal(s) upstream could be "
                f"checked against our code — unmeasured, not unaffected"
            )
        else:
            out.append(
                f"{indent}  == nothing we consume was removed or re-signatured "
                f"({len(api.get('removed') or [])} removal(s) upstream, none ours)"
            )
    # Never let an incomplete read read as a clean bill of health.
    missing = api.get("not_located") or []
    if missing:
        out.append(
            f"{indent}  ?? not declared in any header read: {', '.join(missing[:5])}"
            + (f" (+{len(missing) - 5})" if len(missing) > 5 else "")
            + " — unchecked, not unaffected"
        )
    return out


def _companion_lines(companions: list[dict], indent: str = "  ") -> list[str]:
    """Render coupled-pin resolutions. `!?` is the one that blocks an apply."""
    out = []
    for c in companions:
        mark = _COMPANION_MARK.get(c.get("action", ""), "  ")
        head = f"{indent}{mark} {c['var']} {c.get('current') or '?'}"
        if c.get("action") == "bump":
            head += f" -> {c['required']}"
        elif c.get("action") == "unchanged":
            head += " — unchanged at the target version"
        elif c.get("required"):
            head += f" -> {c['required']}? (not applied)"
        else:
            head += " -> unknown"
        if c.get("confidence"):
            head += f"  [{c['confidence']}]"
        out.append(head)
        if c.get("evidence"):
            out.append(f"{indent}   {c['evidence']}")
        for note in c.get("notes") or []:
            out.append(f"{indent}   ! {note}")
    return out


def _resolve_companions(dep: Dep, target: str) -> list[dict]:
    """Look up what each coupled pin must become at `target`."""
    if not dep.companion_pins:
        return []
    versions = upstream.fetch_versions(dep)
    return companion.resolve_all(
        dep, target, versions, companion.notes_for(target, versions)
    )


def _emit(obj, as_json: bool, human) -> None:
    if as_json:
        json.dump(obj, sys.stdout, indent=2, default=str)
        sys.stdout.write("\n")
    else:
        human(obj)


def _build_profile(root: str, prefer_backend: str = "",
                   ingest: bool = True) -> tuple[list[Dep], dict]:
    deps, manifests = discover.discover(root, ingest=ingest)
    used = backends.analyse(root, deps, force=prefer_backend)
    meta = {
        "repo": os.path.basename(os.path.abspath(root)),
        "generated": date.today().isoformat(),
        "backend": used,
        "sources": sources.describe(root) if ingest else "native",
        "manifests": manifests,
    }
    return deps, meta


# --------------------------------------------------------------------- verbs


def cmd_profile(args) -> int:
    """Regenerate CLAUDE_DEPS.md from the repository."""
    root = args.root
    fresh, meta = _build_profile(root, args.backend, args.ingest)

    old, old_meta = profile.load(root)
    if old and not args.force:
        profile.carry_over(fresh, old)
        if old_meta.get("context"):
            meta["context"] = old_meta["context"]

    path = profile.save(root, fresh, meta)
    result = {
        "wrote": os.path.relpath(path, root),
        "backend": meta["backend"],
        "manifests": meta["manifests"],
        "deps": [
            {"name": d.name, "scope": d.scope, "version": d.version or d.installed_version,
             "consumed": len(d.consumed), "sites": len(d.sites),
             "declarations": len(d.declarations), "divergence": d.divergence_note(),
             "assessed": bool(d.assessment)}
            for d in fresh
        ],
        "carried_over": sum(1 for d in fresh if d.assessment),
    }

    def human(r):
        print(f"wrote {r['wrote']}  (backend: {r['backend']})")
        print(f"manifests: {', '.join(r['manifests']) or 'none'}")
        for d in r["deps"]:
            mark = "•" if d["assessed"] else "○"
            extra = f"  {d['declarations']} declarations" if d["declarations"] > 1 else ""
            print(f"  {mark} {d['name']:<16} {d['scope']:<8} {d['version'] or '(unpinned)':<12} "
                  f"{d['consumed']:>3} symbols  {d['sites']:>3} sites{extra}")
            if d["divergence"]:
                print(f"    != {d['divergence']}")
        if r["carried_over"]:
            print(f"preserved {r['carried_over']} existing assessment(s)")
        unassessed = [d["name"] for d in r["deps"] if not d["assessed"]]
        if unassessed:
            print(f"needs assessment: {', '.join(unassessed)}")

    _emit(result, args.json, human)
    return 0


def cmd_status(args) -> int:
    """Is CLAUDE_DEPS.md still accurate? Pure hashing, no network, no LLM."""
    root = args.root
    if not profile.exists(root):
        result = {"exists": False, "verdict": "missing",
                  "message": f"{profile.FILENAME} does not exist yet"}
        _emit(result, args.json, lambda r: print(r["message"]))
        return 0

    stored, meta = profile.load(root)
    fresh, _ = discover.discover(root, ingest=args.ingest)
    stored_by = {d.name: d for d in stored}
    fresh_by = {d.name: d for d in fresh}

    added = sorted(set(fresh_by) - set(stored_by))
    removed = sorted(set(stored_by) - set(fresh_by))
    drifted = []
    for name in sorted(set(stored_by) & set(fresh_by)):
        old_fp = stored_by[name].stored_fingerprint
        # Compare against the recorded sites, since fresh deps have no sites
        # until a backend runs; the decl+pin hashes are what matter here.
        probe = fresh_by[name]
        probe.sites = stored_by[name].sites
        new_fp = probe.fingerprint(root)
        changed = [k for k in compare(old_fp, new_fp) if k in new_fp]
        if changed:
            drifted.append({"name": name, "changed": changed})

    stale = bool(added or removed or drifted)
    result = {
        "exists": True,
        "verdict": "stale" if stale else "current",
        "generated": meta.get("generated", ""),
        "added": added,
        "removed": removed,
        "drifted": drifted,
        "unassessed": [d.name for d in stored if not d.assessment],
    }

    def human(r):
        print(f"{profile.FILENAME}: {r['verdict']}  (generated {r['generated'] or '?'})")
        for n in r["added"]:
            print(f"  + {n} — new dependency, not in the profile")
        for n in r["removed"]:
            print(f"  - {n} — no longer declared")
        for d in r["drifted"]:
            what = {"decl": "declaration block changed", "pin": "pinned version changed",
                    "sites": "call sites changed"}
            print(f"  ~ {d['name']} — " + "; ".join(what.get(c, c) for c in d["changed"]))
        if r["unassessed"]:
            print(f"  ! never assessed: {', '.join(r['unassessed'])}")
        if not stale:
            print("  profile matches the repository")

    _emit(result, args.json, human)
    return 0


def cmd_check(args) -> int:
    """Gather upgrade evidence for every dependency."""
    root = args.root
    deps, meta = (profile.load(root) if profile.exists(root) else ([], {}))
    if not deps:
        deps, meta = _build_profile(root, args.backend, args.ingest)

    if args.only:
        wanted = {n.strip().lower() for n in args.only.split(",")}
        deps = [d for d in deps if d.name.lower() in wanted]

    findings = []
    for dep in deps:
        info = upstream.summarise(dep, allow_prerelease=args.pre)
        vulns = upstream.advisories(dep)
        # Only worth the network round trip when there is both a coupled pin
        # and somewhere to move to.
        companions = []
        if dep.companion_pins and info.get("resolved") and info.get("behind_by"):
            versions = list(info.get("available") or [])
            if info.get("current_tag"):
                versions.append({"version": info["current"], "tag": info["current_tag"]})
            companions = companion.resolve_all(
                dep, info["latest"], versions,
                companion.notes_for(info["latest"], info.get("available")),
            )

        # What changed upstream, in descending order of trustworthiness:
        # the header diff is factual, a migration guide is upstream's own
        # warning, and the commit log only stands in when there are no notes.
        change: dict = {}
        if not args.no_api_diff and _worth_diffing(dep, info):
            versions = list(info.get("available") or [])
            if info.get("current_tag"):
                versions.append({"version": info["current"], "tag": info["current_tag"]})
            if not dep.sites:
                # Nothing of ours includes it, so widening the consumed surface
                # from upstream's declarations has nothing to match against
                # either. Stating that costs nothing; skipping in silence would
                # look identical to a clean diff.
                api = {"resolved": False, "reason": (
                    "not diffed — no file of ours includes this dependency, so there "
                    "is no consumed surface to intersect against (unmeasured, not "
                    "unaffected)"
                )}
            else:
                api = apidiff.surface_change(
                    dep, info["current"], info["latest"], versions,
                    max_headers=args.max_headers, root=root,
                )
            change["api_diff"] = api
            if api.get("resolved"):
                change.update(upstream.change_prose(
                    dep.upstream.ref, api["from_ref"], api["to_ref"],
                    companion.notes_for(info["latest"], info.get("available")),
                    api["doc_candidates"],
                ))

        findings.append({
            "name": dep.name,
            "kind": dep.kind,
            "scope": dep.scope,
            "declared_in": dep.declared_in,
            "declarations": [d.render() for d in dep.declarations],
            "aliases": dep.aliases,
            # A consistency finding, not an upgrade one: it needs no upstream
            # lookup, and for a project already current on everything it is
            # usually the more valuable of the two.
            "divergence": dep.divergence_note(),
            "pinned": dep.raw_pin,
            "integrity": dep.integrity,
            "installed_here": dep.installed_version,
            "consumed": dep.consumed,
            "sites": [f"{s.path}:{s.line}" + (f" {s.symbol}" if s.symbol else "")
                      for s in dep.sites],
            "call_depth": dep.call_depth,
            "notes": dep.notes,
            "scope_evidence": dep.scope_evidence,
            "companion_pins": [p.render() for p in dep.companion_pins],
            "companions": companions,
            "assessment": dep.assessment,
            "upstream": info,
            "change_evidence": change,
            "advisories": vulns,
        })

    result = {"repo": meta.get("repo", os.path.basename(os.path.abspath(root))),
              "profile_exists": profile.exists(root),
              "findings": findings}

    def human(r):
        for f in r["findings"]:
            up = f["upstream"]
            # Independent of whether upstream resolved, so it is printed before
            # the branches below bail out.
            if f.get("divergence"):
                print(f"!= {f['name']:<16} {f['divergence']}")
            if not up.get("resolved"):
                print(f"?  {f['name']:<16} {up.get('reason','unresolved')}")
                continue
            if up.get("unpinned"):
                here = f["installed_here"] or "not installed on this machine"
                print(f"~  {f['name']:<16} unpinned system dep — here: {here}; "
                      f"newest packaged: {up['latest']}")
                if f["consumed"]:
                    print(f"   we call: {', '.join(f['consumed'][:8])}"
                          f"{' …' if len(f['consumed']) > 8 else ''}")
                continue
            if up["behind_by"] == 0:
                print(f"=  {f['name']:<16} {up['current'] or '(unpinned)'} — current")
                continue
            behind = f"{up['behind_by']}{'+' if up.get('behind_by_is_floor') else ''} behind"
            print(f"^  {f['name']:<16} {up['current'] or '?'} -> {up['latest']}  "
                  f"({behind}, {up['bump']}, scope={f['scope']})")
            if up.get("pin_unavailable"):
                print(f"   !  {up['current']} is no longer offered by "
                      f"{f['upstream'].get('source') or 'the index'} — a fresh "
                      f"install cannot reproduce this build")
            if f["consumed"]:
                print(f"   we call: {', '.join(f['consumed'][:8])}"
                      f"{' …' if len(f['consumed']) > 8 else ''}")
            for line in _companion_lines(f.get("companions") or [], indent="   "):
                print(line)
            for line in _api_lines((f.get("change_evidence") or {}).get("api_diff") or {}):
                print(line)
            for doc in (f.get("change_evidence") or {}).get("migration_docs") or []:
                print(f"   doc: upstream ships {doc['path']} at the target version")
            commits = (f.get("change_evidence") or {}).get("commits") or {}
            if commits.get("resolved"):
                print(f"   log: {commits['total']} commit(s), no release notes"
                      + (f"; {len(commits['breaking'])} mention a break"
                         if commits["breaking"] else ""))
            for v in f["advisories"]:
                mark = "!!" if v.get("version_verified") else "?~"
                fixed = f" fixed={v['fixed']}" if v.get("fixed") else ""
                print(f"   {mark} {v['id']} {v['severity']}{fixed} {v['summary'][:70]}")
            if any(not v.get("version_verified") for v in f["advisories"]):
                print("   (?~ = OSV could not evaluate the version range; may not apply)")

    _emit(result, args.json, human)
    return 0


def cmd_apidiff(args) -> int:
    """Diff one dependency's public headers between two versions."""
    dep = _find_dep(args.root, args.dep, args.ingest)
    if not dep.consumed:
        # Without a consumed surface there is nothing to intersect against, and
        # a bare list of upstream removals is what a changelog already is.
        backends.analyse(args.root, [dep])

    versions = upstream.fetch_versions(dep)
    target = args.to
    if not target:
        ahead = upstream.newer_than(dep.version, versions)
        if not ahead:
            print(f"{dep.name} is at {dep.version or '(unpinned)'} — nothing newer to diff")
            return 0
        target = ahead[0]["version"]

    result = apidiff.surface_change(
        dep, args.from_version or dep.version, target, versions,
        max_headers=args.max_headers, root=args.root,
    )

    def human(r):
        if not r["resolved"]:
            print(f"{dep.name}: {r['reason']}")
            return
        print(f"{dep.name} {r['from_version']} -> {r['to_version']} "
              f"({r['repo']} {r['from_ref']}...{r['to_ref']})")
        print(f"  {r['symbols_before']} public declaration(s) -> {r['symbols_after']}, "
              f"read {r['headers_read']}/{r['headers_available']} header(s)")
        for line in _api_lines(r, indent="  ")[1:]:
            print(line)
        for path in r["removed_headers"]:
            print(f"  -- header gone: {path}")
        for note in r["notes"]:
            print(f"  ! {note}")

    _emit(result, args.json, human)
    return 0


def _find_dep(root: str, name: str, ingest: bool = True) -> Dep:
    deps, _ = profile.load(root)
    if not deps:
        deps, _ = discover.discover(root, ingest=ingest)
    want = name.lower()
    for d in deps:
        # Aliases matter: after reconciliation `find_package(CURL)` and
        # `libcurl/8.4.0` are one record under one of the two names, and the
        # user may well type the other.
        if want in [d.name.lower()] + [a.lower() for a in d.aliases]:
            return d
    raise SystemExit(f"no dependency named {name!r} in this repository")


def _strip_private(planned: dict) -> dict:
    shown = {k: v for k, v in planned.items() if not k.startswith("_")}
    shown["edits"] = [
        {k: v for k, v in e.items() if not k.startswith("_")} for e in planned.get("edits", [])
    ]
    return shown


def cmd_plan(args) -> int:
    dep = _find_dep(args.root, args.dep, args.ingest)
    companions = [] if args.ignore_companions else _resolve_companions(dep, args.to)
    try:
        planned = apply_mod.plan(args.root, dep, args.to, companions=companions)
    except apply_mod.ApplyError as exc:
        print(f"cannot plan: {exc}", file=sys.stderr)
        return 1

    def human(r):
        print(f"{r['dep']}: {r['from']} -> {r['to']} in {r['file']}")
        if r["new_hash"]:
            print(f"new hash: {r['new_hash']}")
        if r.get("companions"):
            print("coupled pins:")
            for line in _companion_lines(r["companions"]):
                print(line)
        elif r["companion_pins"]:
            print("coupled pin(s) NOT resolved (--ignore-companions) —")
            for c in r["companion_pins"]:
                print(f"  {c}")
        if r.get("regenerate"):
            print("still records the old version, and is not ours to edit: "
                  + "; ".join(r["regenerate"]))
            print("  regenerate it, or the build keeps resolving the old pin.")
        if r["blocked_on"]:
            print("WARNING: apply would refuse — unresolved coupled pin(s): "
                  + "; ".join(r["blocked_on"]))
            print("  bumping the dependency alone may fail at link time.")
        print(r["diff"])

    _emit(_strip_private(planned), args.json, human)
    return 0


def cmd_apply(args) -> int:
    dep = _find_dep(args.root, args.dep, args.ingest)
    companions = [] if args.ignore_companions else _resolve_companions(dep, args.to)
    try:
        planned = apply_mod.plan(args.root, dep, args.to, companions=companions)
    except apply_mod.ApplyError as exc:
        print(f"cannot apply: {exc}", file=sys.stderr)
        return 1

    # A coupled pin we could not resolve is the one case where doing half the
    # job is worse than doing none: the build configures cleanly and fails at
    # link time with a missing symbol, which reads like a compiler problem.
    if planned["blocked_on"]:
        print(
            f"refusing to bump {dep.name}: unresolved coupled pin(s) — "
            + "; ".join(planned["blocked_on"]),
            file=sys.stderr,
        )
        for line in _companion_lines(planned["companions"], indent="  "):
            print(line, file=sys.stderr)
        print(
            "  set them by hand, or re-run with --ignore-companions to bump only "
            f"{dep.name}.",
            file=sys.stderr,
        )
        return 2

    result = _strip_private(planned)

    # Verify *before* writing. A verification that edits the tree and then finds
    # the build broken has already done the thing it was run to prevent.
    sandbox_note = ""
    if args.verify and not args.in_place:
        checked = apply_mod.verify_plan(args.root, planned, args.build_dir)
        result["verify"] = checked["steps"]
        result["verified_ok"] = checked["ok"]
        result["verified_in_sandbox"] = True
        sandbox_note = checked["note"]
        if not checked["ok"]:
            print(
                f"not applying {dep.name}: the bump to {args.to} does not build. "
                "Your tree is unchanged.",
                file=sys.stderr,
            )
            for step in checked["steps"]:
                if "skipped" in step:
                    print(f"  skipped: {step['cmd']} — {step['skipped']}", file=sys.stderr)
                elif not step["ok"]:
                    print(f"  FAIL: {step['cmd']}", file=sys.stderr)
                    print("    " + step["output"].replace("\n", "\n    "), file=sys.stderr)
            print(f"  ({checked['note']})", file=sys.stderr)
            result["applied"] = False
            _emit(result, args.json, lambda r: None)
            return 3
        if not checked["established"]:
            print(
                "  ! nothing was actually verified — the build toolchain is not "
                "installed here; applying anyway because you asked to apply",
                file=sys.stderr,
            )

    apply_mod.write(args.root, planned)
    result["applied"] = True
    result["backup_dir"] = apply_mod.backup_dir(args.root)
    if args.verify and args.in_place:
        result["verify"] = apply_mod.verify(args.root, args.build_dir)
        result["verified_ok"] = all(
            s.get("ok", True) for s in result["verify"] if "skipped" not in s
        )
        result["verified_in_sandbox"] = False

    def human(r):
        print(f"applied {r['dep']} {r['from']} -> {r['to']} in {r['file']}")
        if r.get("new_hash"):
            print(f"  URL_HASH updated: {r['new_hash']}")
        for c in r["companion_edits"]:
            print(f"  coupled pin {c['var']}: {c['from']} -> {c['to']} "
                  f"in {c['file']}:{c['line']}  [{c['confidence']}]")
            if c.get("evidence"):
                print(f"    {c['evidence']}")
            for note in c.get("notes") or []:
                print(f"    ! {note}")
        for where in r.get("regenerate") or []:
            print(f"  ! {where} still records the old version and was left alone — "
                  f"regenerate it, or the build keeps resolving the old pin")
        if sandbox_note:
            print(f"  {sandbox_note}")
        print(f"  backups kept outside your tree in {r['backup_dir']} "
              f"(deptool revert restores them)")
        for step in r.get("verify", []):
            if "skipped" in step:
                print(f"  skipped: {step['cmd']} — {step['skipped']}")
            else:
                print(f"  {'PASS' if step['ok'] else 'FAIL'}: {step['cmd']}")
                if not step["ok"]:
                    print("    " + step["output"].replace("\n", "\n    "))

    _emit(result, args.json, human)
    return 0


def cmd_revert(args) -> int:
    dep = _find_dep(args.root, args.dep, args.ingest)
    # An apply may have touched a second file, if the coupled pin lives
    # elsewhere. Restore every file this dependency could have edited.
    rels = [dep.declared_in.split(":")[0]]
    for pin in dep.companion_pins:
        if pin.file and pin.file not in rels:
            rels.append(pin.file)
    restored = [r for r in rels if apply_mod.revert(args.root, {"edits": [{"file": r}]})]
    if not restored:
        print("no backup found for " + ", ".join(rels))
        return 1
    for rel in restored:
        print(f"restored {rel}")
    return 0


def main(argv: list[str] | None = None) -> int:
    # `--root` and `--json` are accepted both before and after the verb.
    # argparse only allows the former by default, but `deptool check --json`
    # is the natural way to type it — and is what the plugin's commands do.
    # A subparser parses into a fresh namespace and copies every attribute
    # back over the parent's, so a repeated flag silently clobbers the
    # top-level value — argparse.SUPPRESS does not reliably prevent this.
    # Give the two levels distinct dests and merge them explicitly instead.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--root", dest="root_sub", default=None,
                        help="repository root (default: cwd)")
    common.add_argument("--json", dest="json_sub", action="store_true",
                        help="machine-readable output")
    common.add_argument("--no-ingest", dest="no_ingest_sub", action="store_true",
                        default=None,
                        help="use only the native parsers, ignoring any installed "
                             f"scanner ({', '.join(sources.NAMES)})")

    p = argparse.ArgumentParser(prog="deptool", description=__doc__)
    p.add_argument("--root", dest="root_top", default=".",
                   help="repository root (default: cwd)")
    p.add_argument("--json", dest="json_top", action="store_true",
                   help="machine-readable output")
    p.add_argument("--no-ingest", dest="no_ingest_top", action="store_true",
                   help="use only the native parsers")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("profile", help="regenerate CLAUDE_DEPS.md", parents=[common])
    sp.add_argument("--backend", default="", choices=["", *backends.NAMES],
                    help="run only this analysis backend (default: all detected)")
    sp.add_argument("--force", action="store_true",
                    help="discard existing assessments instead of preserving them")
    sp.set_defaults(func=cmd_profile)

    sp = sub.add_parser("status", parents=[common], help="is CLAUDE_DEPS.md stale?")
    sp.set_defaults(func=cmd_status)

    sp = sub.add_parser("check", parents=[common], help="gather upgrade evidence")
    sp.add_argument("--pre", action="store_true", help="include pre-releases")
    sp.add_argument("--only", default="", help="comma-separated dependency names")
    sp.add_argument("--backend", default="", choices=["", *backends.NAMES])
    sp.add_argument("--no-api-diff", action="store_true",
                    help="skip the public-header diff (saves network, loses the "
                         "only factual breaking-change evidence)")
    sp.add_argument("--max-headers", type=int, default=apidiff.MAX_HEADERS,
                    help=f"headers to read per dependency, per ref "
                         f"(default: {apidiff.MAX_HEADERS})")
    sp.set_defaults(func=cmd_check)

    sp = sub.add_parser("apidiff", parents=[common],
                        help="diff a dependency's public headers between two versions")
    sp.add_argument("--dep", required=True)
    sp.add_argument("--to", default="", help="target version (default: latest)")
    sp.add_argument("--from", dest="from_version", default="",
                    help="baseline version (default: the pinned one)")
    sp.add_argument("--max-headers", type=int, default=40,
                    help="headers to read per ref (default: 40)")
    sp.set_defaults(func=cmd_apidiff)

    companion_help = (
        "do not resolve or edit coupled pins, and do not refuse when one is "
        "unresolved — bump this dependency only"
    )

    sp = sub.add_parser("plan", parents=[common], help="show the edit for a bump, without writing")
    sp.add_argument("--dep", required=True)
    sp.add_argument("--to", required=True)
    sp.add_argument("--ignore-companions", action="store_true", help=companion_help)
    sp.set_defaults(func=cmd_plan)

    sp = sub.add_parser("apply", parents=[common], help="write the bump (and optionally verify)")
    sp.add_argument("--dep", required=True)
    sp.add_argument("--to", required=True)
    sp.add_argument(
        "--verify", action="store_true",
        help="build and test the bump in a throwaway copy first; apply only if it passes",
    )
    sp.add_argument(
        "--in-place", action="store_true",
        help="with --verify, edit and build in your own tree instead of a copy "
             "(needed only when the build requires VCS metadata)",
    )
    sp.add_argument("--build-dir", default="build")
    sp.add_argument("--ignore-companions", action="store_true", help=companion_help)
    sp.set_defaults(func=cmd_apply)

    sp = sub.add_parser("revert", parents=[common], help="restore the pre-apply backup")
    sp.add_argument("--dep", required=True)
    sp.add_argument("--to", default="")
    sp.set_defaults(func=cmd_revert)

    args = p.parse_args(argv)
    # Either position wins; the sub-level one takes precedence when both given.
    args.root = os.path.abspath(
        args.root_sub if args.root_sub is not None else args.root_top
    )
    args.json = bool(args.json_sub or args.json_top)
    args.ingest = not (args.no_ingest_sub or args.no_ingest_top)
    if not hasattr(args, "backend"):
        args.backend = ""
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
