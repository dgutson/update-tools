"""deptool — the deterministic half of the dependency reviewer.

Everything printed here is evidence. The judgement ("is this upgrade worth
it") is deliberately *not* made here — that is the LLM's job, and it needs
this output as input.

    python3 -m deptool profile   [--root R] [--json]
    python3 -m deptool status    [--root R] [--json]
    python3 -m deptool check     [--root R] [--json] [--pre] [--only NAME]
    python3 -m deptool plan      --dep NAME --to VERSION [--root R]
    python3 -m deptool apply     --dep NAME --to VERSION [--root R] [--verify]
    python3 -m deptool revert    --dep NAME --to VERSION [--root R]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date

from . import apply as apply_mod
from . import backends, discover, profile, upstream
from .fingerprint import compare
from .model import Dep


def _emit(obj, as_json: bool, human) -> None:
    if as_json:
        json.dump(obj, sys.stdout, indent=2, default=str)
        sys.stdout.write("\n")
    else:
        human(obj)


def _build_profile(root: str, prefer_backend: str = "") -> tuple[list[Dep], dict]:
    deps, manifests = discover.discover(root)
    used = backends.analyse(root, deps, force=prefer_backend)
    meta = {
        "repo": os.path.basename(os.path.abspath(root)),
        "generated": date.today().isoformat(),
        "backend": used,
        "manifests": manifests,
    }
    return deps, meta


# --------------------------------------------------------------------- verbs


def cmd_profile(args) -> int:
    """Regenerate CLAUDE_DEPS.md from the repository."""
    root = args.root
    fresh, meta = _build_profile(root, args.backend)

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
            print(f"  {mark} {d['name']:<16} {d['scope']:<8} {d['version'] or '(unpinned)':<12} "
                  f"{d['consumed']:>3} symbols  {d['sites']:>3} sites")
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
    fresh, _ = discover.discover(root)
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
        deps, meta = _build_profile(root, args.backend)

    if args.only:
        wanted = {n.strip().lower() for n in args.only.split(",")}
        deps = [d for d in deps if d.name.lower() in wanted]

    findings = []
    for dep in deps:
        info = upstream.summarise(dep, allow_prerelease=args.pre)
        vulns = upstream.advisories(dep)
        findings.append({
            "name": dep.name,
            "kind": dep.kind,
            "scope": dep.scope,
            "declared_in": dep.declared_in,
            "pinned": dep.raw_pin,
            "integrity": dep.integrity,
            "installed_here": dep.installed_version,
            "consumed": dep.consumed,
            "sites": [f"{s.path}:{s.line}" + (f" {s.symbol}" if s.symbol else "")
                      for s in dep.sites],
            "call_depth": dep.call_depth,
            "notes": dep.notes,
            "scope_evidence": dep.scope_evidence,
            "assessment": dep.assessment,
            "upstream": info,
            "advisories": vulns,
        })

    result = {"repo": meta.get("repo", os.path.basename(os.path.abspath(root))),
              "profile_exists": profile.exists(root),
              "findings": findings}

    def human(r):
        for f in r["findings"]:
            up = f["upstream"]
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
            print(f"^  {f['name']:<16} {up['current'] or '?'} -> {up['latest']}  "
                  f"({up['behind_by']} behind, {up['bump']}, scope={f['scope']})")
            if f["consumed"]:
                print(f"   we call: {', '.join(f['consumed'][:8])}"
                      f"{' …' if len(f['consumed']) > 8 else ''}")
            for v in f["advisories"]:
                mark = "!!" if v.get("version_verified") else "?~"
                fixed = f" fixed={v['fixed']}" if v.get("fixed") else ""
                print(f"   {mark} {v['id']} {v['severity']}{fixed} {v['summary'][:70]}")
            if any(not v.get("version_verified") for v in f["advisories"]):
                print("   (?~ = OSV could not evaluate the version range; may not apply)")

    _emit(result, args.json, human)
    return 0


def _find_dep(root: str, name: str) -> Dep:
    deps, _ = profile.load(root)
    if not deps:
        deps, _ = discover.discover(root)
    for d in deps:
        if d.name.lower() == name.lower():
            return d
    raise SystemExit(f"no dependency named {name!r} in this repository")


def cmd_plan(args) -> int:
    dep = _find_dep(args.root, args.dep)
    try:
        planned = apply_mod.plan(args.root, dep, args.to)
    except apply_mod.ApplyError as exc:
        print(f"cannot plan: {exc}", file=sys.stderr)
        return 1
    shown = {k: v for k, v in planned.items() if not k.startswith("_")}

    def human(r):
        print(f"{r['dep']}: {r['from']} -> {r['to']} in {r['file']}")
        if r["new_hash"]:
            print(f"new hash: {r['new_hash']}")
        if r["companion_pins"]:
            print("WARNING: coupled pin(s) this edit does NOT change —")
            for c in r["companion_pins"]:
                print(f"  {c}")
            print("  bumping the source alone may fail at link time.")
        print(r["diff"])

    _emit(shown, args.json, human)
    return 0


def cmd_apply(args) -> int:
    dep = _find_dep(args.root, args.dep)
    try:
        planned = apply_mod.plan(args.root, dep, args.to)
    except apply_mod.ApplyError as exc:
        print(f"cannot apply: {exc}", file=sys.stderr)
        return 1
    apply_mod.write(args.root, planned)
    result = {k: v for k, v in planned.items() if not k.startswith("_")}
    result["applied"] = True
    if args.verify:
        result["verify"] = apply_mod.verify(args.root, args.build_dir)
        result["verified_ok"] = all(
            s.get("ok", True) for s in result["verify"] if "skipped" not in s
        )

    def human(r):
        print(f"applied {r['dep']} {r['from']} -> {r['to']} in {r['file']}")
        if r.get("new_hash"):
            print(f"  URL_HASH updated: {r['new_hash']}")
        print(f"  backup: {r['file']}.deptool.bak  (deptool revert restores it)")
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
    dep = _find_dep(args.root, args.dep)
    rel = dep.declared_in.split(":")[0]
    ok = apply_mod.revert(args.root, {"file": rel})
    print(f"{'restored' if ok else 'no backup found for'} {rel}")
    return 0 if ok else 1


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

    p = argparse.ArgumentParser(prog="deptool", description=__doc__)
    p.add_argument("--root", dest="root_top", default=".",
                   help="repository root (default: cwd)")
    p.add_argument("--json", dest="json_top", action="store_true",
                   help="machine-readable output")
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
    sp.set_defaults(func=cmd_check)

    sp = sub.add_parser("plan", parents=[common], help="show the edit for a bump, without writing")
    sp.add_argument("--dep", required=True)
    sp.add_argument("--to", required=True)
    sp.set_defaults(func=cmd_plan)

    sp = sub.add_parser("apply", parents=[common], help="write the bump (and optionally verify)")
    sp.add_argument("--dep", required=True)
    sp.add_argument("--to", required=True)
    sp.add_argument("--verify", action="store_true", help="configure, build and test after editing")
    sp.add_argument("--build-dir", default="build")
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
    if not hasattr(args, "backend"):
        args.backend = ""
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
