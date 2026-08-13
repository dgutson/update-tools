"""Tests for the deterministic layer.

These cover the places where a wrong answer would be silent and damaging:
scope misclassification (a test-only dep reported as production risk), and the
version-swap regex (a bad edit corrupts CMakeLists.txt).
"""

import json
import os
import shutil
import sys
import textwrap

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from deptool import cmake, companion, profile
from deptool.apply import _declaration_span, _swap_version
from deptool.apply import plan as apply_plan
from deptool.fingerprint import hash_declaration, hash_files, hash_text
from deptool.model import (
    CompanionPin,
    Declaration,
    Dep,
    Site,
    Upstream,
    bump_kind,
    most_restrictive,
    parse_version,
)


# ------------------------------------------------------------------ versions


@pytest.mark.parametrize(
    "text,expected",
    [
        ("1.2.3", (1, 2, 3, 1, "")),
        ("v1.2.3", (1, 2, 3, 1, "")),
        ("0.8", (0, 8, 0, 1, "")),
        ("5", (5, 0, 0, 1, "")),
        ("", None),
        ("not-a-version", None),
    ],
)
def test_parse_version(text, expected):
    assert parse_version(text) == expected


def test_prerelease_sorts_below_release():
    assert parse_version("1.0.0-rc1") < parse_version("1.0.0")


@pytest.mark.parametrize(
    "old,new,kind",
    [
        ("1.2.3", "2.0.0", "major"),
        ("1.2.3", "1.3.0", "minor"),
        ("1.2.3", "1.2.4", "patch"),
        ("1.2.3", "1.2.3", "same"),
        ("", "1.0.0", "unknown"),
    ],
)
def test_bump_kind(old, new, kind):
    assert bump_kind(old, new) == kind


def test_most_restrictive_prefers_test_over_runtime():
    assert most_restrictive("runtime", "test") == "test"
    assert most_restrictive("runtime", "runtime") == "runtime"
    assert most_restrictive() == "runtime"


# ------------------------------------------------------------- version swap


@pytest.mark.parametrize(
    "text,old,new,expected",
    [
        # The case that matters: a tarball URL, where the version is followed
        # by a file extension.
        (
            "URL https://github.com/o/r/archive/refs/tags/v0.7.4.tar.gz",
            "0.7.4", "0.11.1",
            "URL https://github.com/o/r/archive/refs/tags/v0.11.1.tar.gz",
        ),
        # No `v` prefix must stay without one.
        ("archive/0.8.0.zip", "0.8.0", "0.9.0", "archive/0.9.0.zip"),
        # Must not match a longer version that merely starts the same.
        ("v0.7.41.tar.gz", "0.7.4", "0.11.1", "v0.7.41.tar.gz"),
        # Must not match a prefix of a longer dotted version.
        ("1.2.3.4", "1.2.3", "9.9.9", "1.2.3.4"),
        # Must not match inside a larger token.
        ("abc1.2.3", "1.2.3", "9.9.9", "abc1.2.3"),
    ],
)
def test_swap_version(text, old, new, expected):
    assert _swap_version(text, old, new) == expected


def test_declaration_span_isolates_one_command():
    text = textwrap.dedent(
        """\
        FetchContent_Declare(
            alpha
            URL https://example.com/alpha-1.0.0.tar.gz
        )
        FetchContent_Declare(
            beta
            URL https://example.com/beta-1.0.0.tar.gz
        )
        """
    )
    lo, hi = _declaration_span(text, 1)
    block = text[lo:hi]
    assert "alpha" in block
    assert "beta" not in block


def test_swap_scoped_to_span_leaves_sibling_alone():
    """Two deps pinned to the same version must not be edited together."""
    text = textwrap.dedent(
        """\
        FetchContent_Declare(
            alpha
            URL https://example.com/alpha-1.0.0.tar.gz
        )
        FetchContent_Declare(
            beta
            URL https://example.com/beta-1.0.0.tar.gz
        )
        """
    )
    lo, hi = _declaration_span(text, 1)
    updated = text[:lo] + _swap_version(text[lo:hi], "1.0.0", "2.0.0") + text[hi:]
    assert "alpha-2.0.0" in updated
    assert "beta-1.0.0" in updated


# -------------------------------------------------------------- cmake parse


def _write(tmp_path, name, body):
    p = tmp_path / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(textwrap.dedent(body))
    return p


def test_fetchcontent_url_pin_and_github_upstream(tmp_path):
    _write(tmp_path, "CMakeLists.txt", """\
        FetchContent_Declare(
            libremidi
            URL https://github.com/celtera/libremidi/archive/refs/tags/v5.4.3.tar.gz
            URL_HASH SHA256=deadbeef
        )
        """)
    deps, _ = cmake.parse_project(str(tmp_path))
    dep = next(d for d in deps if d.name == "libremidi")
    assert dep.version == "5.4.3"
    assert dep.integrity == "SHA256=deadbeef"
    assert dep.upstream.kind == "github"
    assert dep.upstream.ref == "celtera/libremidi"


def test_test_guard_marks_dependency_test_only(tmp_path):
    _write(tmp_path, "CMakeLists.txt", """\
        if(BUILD_TESTING)
            FetchContent_Declare(
                hegel
                URL https://github.com/hegeldev/hegel-cpp/archive/refs/tags/v0.7.4.tar.gz
            )
            FetchContent_MakeAvailable(hegel)
        endif()
        """)
    deps, _ = cmake.parse_project(str(tmp_path))
    assert next(d for d in deps if d.name == "hegel").scope == "test"


def test_toplevel_declare_used_only_in_tests_is_test_scope(tmp_path):
    """The pin is at top level but nothing outside tests links it."""
    _write(tmp_path, "CMakeLists.txt", """\
        FetchContent_Declare(
            hegel
            URL https://github.com/hegeldev/hegel-cpp/archive/refs/tags/v0.7.4.tar.gz
        )
        if(BUILD_TESTING)
            FetchContent_MakeAvailable(hegel)
        endif()
        """)
    deps, _ = cmake.parse_project(str(tmp_path))
    dep = next(d for d in deps if d.name == "hegel")
    assert dep.scope == "test"
    assert any("MakeAvailable" in e for e in dep.scope_evidence)


def test_else_branch_is_not_governed_by_test_condition(tmp_path):
    _write(tmp_path, "CMakeLists.txt", """\
        if(BUILD_TESTING)
            find_package(GTest REQUIRED)
        else()
            FetchContent_Declare(
                prod_dep
                URL https://github.com/o/r/archive/refs/tags/v1.0.0.tar.gz
            )
        endif()
        """)
    deps, _ = cmake.parse_project(str(tmp_path))
    assert next(d for d in deps if d.name == "prod_dep").scope == "runtime"


def test_pkg_check_modules_with_version_constraint(tmp_path):
    _write(tmp_path, "CMakeLists.txt", """\
        pkg_check_modules(FLUIDSYNTH REQUIRED IMPORTED_TARGET fluidsynth>=2.2)
        """)
    deps, _ = cmake.parse_project(str(tmp_path))
    dep = next(d for d in deps if d.name == "fluidsynth")
    assert dep.kind == "pkg-config"
    assert dep.version == "2.2"
    assert dep.upstream.kind == "distro"


def test_find_package_exact_is_flagged(tmp_path):
    _write(tmp_path, "CMakeLists.txt", """\
        find_package(libremidi 5.4.3 EXACT CONFIG QUIET)
        """)
    deps, _ = cmake.parse_project(str(tmp_path))
    dep = next(d for d in deps if d.name == "libremidi")
    assert any("EXACT" in n for n in dep.notes)


def test_system_or_fetch_merge_keeps_github_upstream(tmp_path):
    """find_package + FetchContent fallback: the GitHub URL must survive."""
    _write(tmp_path, "CMakeLists.txt", """\
        find_package(yaml-cpp CONFIG QUIET)
        if(NOT yaml-cpp_FOUND)
            FetchContent_Declare(
                yaml-cpp
                URL https://github.com/jbeder/yaml-cpp/archive/refs/tags/0.8.0.tar.gz
            )
        endif()
        """)
    deps, _ = cmake.parse_project(str(tmp_path))
    dep = next(d for d in deps if d.name == "yaml-cpp")
    assert dep.kind == "cmake-system-or-fetch"
    assert dep.upstream.kind == "github"
    assert dep.upstream.ref == "jbeder/yaml-cpp"
    assert dep.version == "0.8.0"


def test_comments_do_not_produce_dependencies(tmp_path):
    _write(tmp_path, "CMakeLists.txt", """\
        # FetchContent_Declare(ghost URL https://example.com/ghost-1.0.0.tar.gz)
        #[[ FetchContent_Declare(phantom URL https://example.com/p-1.0.0.tar.gz) ]]
        find_package(ALSA REQUIRED)
        """)
    deps, _ = cmake.parse_project(str(tmp_path))
    names = {d.name for d in deps}
    assert "ghost" not in names
    assert "phantom" not in names
    assert "ALSA" in names


def test_add_subdirectory_is_followed(tmp_path):
    _write(tmp_path, "CMakeLists.txt", "add_subdirectory(sub)\n")
    _write(tmp_path, "sub/CMakeLists.txt", """\
        FetchContent_Declare(
            nested
            URL https://github.com/o/r/archive/refs/tags/v3.1.0.tar.gz
        )
        """)
    deps, files = cmake.parse_project(str(tmp_path))
    assert "nested" in {d.name for d in deps}
    assert os.path.join("sub", "CMakeLists.txt") in files


def test_cpm_shorthand(tmp_path):
    _write(tmp_path, "CMakeLists.txt", 'CPMAddPackage("gh:fmtlib/fmt@10.2.1")\n')
    deps, _ = cmake.parse_project(str(tmp_path))
    dep = next(d for d in deps if d.name == "fmt")
    assert dep.version == "10.2.1"
    assert dep.upstream.ref == "fmtlib/fmt"


def test_companion_pin_is_detected(tmp_path):
    """A coupled native-engine pin must be surfaced.

    Bumping hegel-cpp without HEGEL_LIBHEGEL_VERSION configures cleanly and
    then fails at link time with an undefined symbol — a failure that looks
    nothing like a dependency problem. This is a real breakage observed on
    zeta-daw.
    """
    _write(tmp_path, "CMakeLists.txt", """\
        if(BUILD_TESTING)
            FetchContent_Declare(
                hegel
                URL https://github.com/hegeldev/hegel-cpp/archive/refs/tags/v0.7.4.tar.gz
            )
            set(
                HEGEL_LIBHEGEL_VERSION
                0.29.0
                CACHE STRING "libhegel version required by Hegel C++ v0.7.4"
                FORCE
            )
            FetchContent_MakeAvailable(hegel)
        endif()
        """)
    deps, _ = cmake.parse_project(str(tmp_path))
    dep = next(d for d in deps if d.name == "hegel")
    assert len(dep.companion_pins) == 1
    pin = dep.companion_pins[0]
    assert (pin.var, pin.value) == ("HEGEL_LIBHEGEL_VERSION", "0.29.0")
    assert "libhegel version required" in pin.doc  # CACHE docstring, not FORCE
    # The file:line is what makes an atomic two-part edit possible at all.
    assert pin.file == "CMakeLists.txt" and pin.line > 0
    assert pin.matched_by == "name"
    assert any("coupled pin" in n for n in dep.notes)


def test_dep_own_version_variable_is_not_a_companion(tmp_path):
    _write(tmp_path, "CMakeLists.txt", """\
        set(FOO_VERSION 1.2.3 CACHE STRING "version of foo" FORCE)
        FetchContent_Declare(
            foo
            URL https://github.com/o/foo/archive/refs/tags/v1.2.3.tar.gz
        )
        """)
    deps, _ = cmake.parse_project(str(tmp_path))
    dep = next(d for d in deps if d.name == "foo")
    assert dep.companion_pins == []


def test_unrelated_distant_cache_var_is_not_attached(tmp_path):
    _write(tmp_path, "CMakeLists.txt", """\
        FetchContent_Declare(
            alpha
            URL https://github.com/o/alpha/archive/refs/tags/v1.0.0.tar.gz
        )
        """ + "\n" * 40 + """
        set(UNRELATED_PROTOCOL_VERSION 9.9.9 CACHE STRING "something else" FORCE)
        """)
    deps, _ = cmake.parse_project(str(tmp_path))
    dep = next(d for d in deps if d.name == "alpha")
    assert dep.companion_pins == []


# ------------------------------------------------------------------- CLI flags


@pytest.mark.parametrize(
    "argv",
    [
        ["--root", "R", "--json", "status"],   # both before the verb
        ["status", "--root", "R", "--json"],   # both after the verb
        ["--root", "R", "status", "--json"],   # split across the verb
    ],
)
def test_global_flags_accepted_either_side_of_the_verb(tmp_path, argv, capsys):
    """`deptool check --json` must work, not just `deptool --json check`.

    A subparser copies its whole namespace over the parent's, so a repeated
    flag silently clobbers the top-level value. The plugin's own commands type
    the flags after the verb, so this regressing would break it on first use.
    """
    import json as _json

    from deptool.__main__ import main

    resolved = [str(tmp_path) if a == "R" else a for a in argv]
    assert main(resolved) == 0
    out = capsys.readouterr().out
    assert _json.loads(out)["verdict"] == "missing"  # parsed as JSON, not prose


# ------------------------------------------------------------ graphify backend


def test_graphify_backend_reads_links_and_labels(tmp_path):
    """Graphify puts edges under `links` and names nodes with `label`.

    An earlier version of this backend assumed `edges`/`name` and silently
    produced nothing, so pin the real schema.
    """
    import json

    from deptool.backends import _enrich_graphify

    graph = {
        "directed": True,
        "nodes": [
            {"id": "app_run", "label": ".run()", "source_file": "app.cpp"},
            {"id": "app_note", "label": ".noteOn()", "source_file": "app.cpp"},
            {"id": "ext_fs", "label": "fluid_synth_noteon", "source_file": ""},
        ],
        "links": [
            {"source": "app_note", "target": "ext_fs", "relation": "calls"},
            {"source": "app_run", "target": "app_note", "relation": "calls"},
            {"source": "app_run", "target": "app_note", "relation": "references"},
        ],
    }
    out = tmp_path / "graphify-out"
    out.mkdir()
    (out / "graph.json").write_text(json.dumps(graph))

    dep = Dep(name="fluidsynth", kind="pkg-config", consumed=["fluid_synth_noteon"])
    assert _enrich_graphify(str(tmp_path), [dep]) is True
    # noteOn calls it directly; run reaches it transitively.
    assert dep.blast_radius == 2
    assert dep.call_depth == 1
    assert any("direct caller" in n for n in dep.notes)


def test_graphify_backend_ignores_unmatched_dependency(tmp_path):
    import json

    from deptool.backends import _enrich_graphify

    graph = {
        "nodes": [{"id": "a", "label": ".run()", "source_file": "a.cpp"}],
        "links": [],
    }
    out = tmp_path / "graphify-out"
    out.mkdir()
    (out / "graph.json").write_text(json.dumps(graph))
    dep = Dep(name="nothing", kind="npm", consumed=["absent_symbol"])
    assert _enrich_graphify(str(tmp_path), [dep]) is False
    assert dep.blast_radius is None


def test_graphify_backend_absent_is_not_an_error(tmp_path):
    from deptool.backends import _enrich_graphify

    assert _enrich_graphify(str(tmp_path), [Dep(name="x", kind="npm")]) is False


def test_graphify_falls_back_to_references_for_macro_use(tmp_path):
    """A macro-consumed symbol has no `calls` edges at all.

    Hegel is used through `HEGEL_TEST(...)`, which graphify emits as a node per
    test file with no outgoing call edge. A calls-only walk matches the seed
    and then reports a blast radius of zero, which reads as "nothing uses
    this" — the exact opposite of the truth.
    """
    import json

    from deptool.backends import _enrich_graphify

    graph = {
        "nodes": [
            {"id": "t1", "label": "HEGEL_TEST", "source_file": "test_a.cpp"},
            {"id": "suite", "label": ".suite()", "source_file": "test_a.cpp"},
            {"id": "main", "label": ".main()", "source_file": "test_a.cpp"},
        ],
        "links": [
            # No `calls` edge anywhere near the macro.
            {"source": "suite", "target": "t1", "relation": "references"},
            {"source": "main", "target": "suite", "relation": "calls"},
        ],
    }
    out = tmp_path / "graphify-out"
    out.mkdir()
    (out / "graph.json").write_text(json.dumps(graph))

    dep = Dep(name="hegel", kind="cmake-fetchcontent-url", consumed=["HEGEL_TEST"])
    assert _enrich_graphify(str(tmp_path), [dep]) is True
    # suite references it; main reaches it transitively through suite.
    assert dep.blast_radius == 2
    # Nothing *called* it, so claiming a call depth would be a fabrication.
    assert dep.call_depth is None
    assert any("no call edges" in n for n in dep.notes)


def test_graphify_prefers_call_edges_over_references(tmp_path):
    """The fallback must not widen the radius when real call edges exist."""
    import json

    from deptool.backends import _enrich_graphify

    graph = {
        "nodes": [
            {"id": "ext", "label": "fluid_synth_noteon", "source_file": ""},
            {"id": "caller", "label": ".noteOn()", "source_file": "a.cpp"},
            {"id": "mentioner", "label": ".docs()", "source_file": "b.cpp"},
        ],
        "links": [
            {"source": "caller", "target": "ext", "relation": "calls"},
            {"source": "mentioner", "target": "ext", "relation": "references"},
        ],
    }
    out = tmp_path / "graphify-out"
    out.mkdir()
    (out / "graph.json").write_text(json.dumps(graph))

    dep = Dep(name="fluidsynth", kind="pkg-config", consumed=["fluid_synth_noteon"])
    assert _enrich_graphify(str(tmp_path), [dep]) is True
    assert dep.blast_radius == 1  # not 2 — `.docs()` only mentions it
    assert dep.call_depth == 1


# ------------------------------------------------------------ backend merging


def test_backends_are_additive_not_first_wins(monkeypatch, tmp_path):
    """Two backends covering different deps must both land.

    graphify resolves the C ABI but is blind to namespaced C++; codebase-memory
    claims the opposite. An earlier version `break`ed after the first backend
    that returned anything, so graphify's partial result suppressed the other
    entirely.
    """
    from deptool import backends

    c_dep = Dep(name="fluidsynth", kind="pkg-config")
    cpp_dep = Dep(name="libremidi", kind="cmake-fetchcontent-url")

    def fake_graphify(root, deps):
        backends._record(deps[0], "graphify", 8, "graphify: 8")
        return True

    def fake_cbmem(root, deps):
        backends._record(deps[1], "codebase-memory", 3, "codebase-memory: 3")
        return True

    monkeypatch.setattr(backends, "detect", lambda root: ["graphify", "codebase-memory", "builtin"])
    monkeypatch.setattr(backends, "_ENRICHERS",
                        {"graphify": fake_graphify, "codebase-memory": fake_cbmem})

    used = backends.analyse(str(tmp_path), [c_dep, cpp_dep])

    assert used == "graphify+codebase-memory+builtin"
    assert c_dep.blast_radius == 8
    assert cpp_dep.blast_radius == 3  # would be None under first-wins


def test_force_backend_runs_only_that_one(monkeypatch, tmp_path):
    from deptool import backends

    dep = Dep(name="fluidsynth", kind="pkg-config")
    ran = []

    def make(name, radius):
        def fake(root, deps):
            ran.append(name)
            backends._record(deps[0], name, radius, f"{name}: {radius}")
            return True
        return fake

    monkeypatch.setattr(backends, "detect", lambda root: ["graphify", "codebase-memory", "builtin"])
    monkeypatch.setattr(backends, "_ENRICHERS",
                        {"graphify": make("graphify", 8),
                         "codebase-memory": make("codebase-memory", 3)})

    used = backends.analyse(str(tmp_path), [dep], force="codebase-memory")

    assert ran == ["codebase-memory"]
    assert used == "codebase-memory+builtin"


def test_record_keeps_largest_radius_and_both_notes():
    """Overlap between backends is unknown, so summing would double-count."""
    from deptool.backends import _record

    dep = Dep(name="fluidsynth", kind="pkg-config")
    _record(dep, "graphify", 8, "graphify: 8 callers", depth=1)
    _record(dep, "codebase-memory", 3, "codebase-memory: 3 callers")

    assert dep.blast_radius == 8  # a lower bound, not 11
    assert dep.backend == "graphify+codebase-memory+builtin"
    assert len(dep.notes) == 2
    assert dep.call_depth == 1  # the backend that had no depth must not erase it


# ------------------------------------------------------------ profile round trip


def test_profile_round_trip_preserves_fields(tmp_path):
    dep = Dep(
        name="hegel",
        kind="cmake-fetchcontent-url",
        version="0.7.4",
        raw_pin="https://github.com/hegeldev/hegel-cpp/archive/refs/tags/v0.7.4.tar.gz",
        integrity="SHA256=abc123",
        declared_in="CMakeLists.txt:115",
        scope="test",
        upstream=Upstream(kind="github", ref="hegeldev/hegel-cpp"),
        consumed=["HEGEL_TEST", "hegel::TestCase"],
        sites=[Site(path="tests/a.cpp", line=4, symbol="#include <hegel/hegel.h>")],
        notes=["a note"],
        assessment="Test-only. Low blast radius.",
    )
    meta = {"repo": "zeta-daw", "generated": "2026-08-10",
            "backend": "builtin", "manifests": ["CMakeLists.txt"]}
    text = profile.render([dep], str(tmp_path), meta)
    back, meta2 = profile.parse(text)

    assert meta2["repo"] == "zeta-daw"
    assert meta2["manifests"] == ["CMakeLists.txt"]
    got = back[0]
    assert got.name == "hegel"
    assert got.version == "0.7.4"
    assert got.scope == "test"
    assert got.integrity == "SHA256=abc123"
    assert got.declared_in == "CMakeLists.txt:115"
    assert got.upstream.ref == "hegeldev/hegel-cpp"
    assert got.consumed == ["HEGEL_TEST", "hegel::TestCase"]
    assert got.sites[0].path == "tests/a.cpp"
    assert got.sites[0].line == 4
    assert got.assessment == "Test-only. Low blast radius."
    assert got.stored_fingerprint  # fingerprint line parsed


def test_unassessed_placeholder_does_not_become_prose(tmp_path):
    dep = Dep(name="x", kind="npm")
    text = profile.render([dep], str(tmp_path), {})
    back, _ = profile.parse(text)
    assert back[0].assessment == ""


def test_carry_over_preserves_assessment():
    old = [Dep(name="hegel", kind="x", assessment="keep me")]
    fresh = [Dep(name="hegel", kind="x"), Dep(name="new", kind="x")]
    profile.carry_over(fresh, old)
    assert fresh[0].assessment == "keep me"
    assert fresh[1].assessment == ""


# ------------------------------------------------------------- fingerprints


def test_fingerprint_changes_when_content_changes(tmp_path):
    f = tmp_path / "CMakeLists.txt"
    f.write_text("original")
    before = hash_files(str(tmp_path), ["CMakeLists.txt"])
    f.write_text("modified")
    assert hash_files(str(tmp_path), ["CMakeLists.txt"]) != before


def test_fingerprint_stable_across_ordering(tmp_path):
    (tmp_path / "a.txt").write_text("a")
    (tmp_path / "b.txt").write_text("b")
    assert hash_files(str(tmp_path), ["a.txt", "b.txt"]) == hash_files(
        str(tmp_path), ["b.txt", "a.txt"]
    )


def test_missing_file_hashes_distinctly(tmp_path):
    (tmp_path / "a.txt").write_text("a")
    with_file = hash_files(str(tmp_path), ["a.txt"])
    os.remove(tmp_path / "a.txt")
    assert hash_files(str(tmp_path), ["a.txt"]) != with_file


def test_declaration_hash_isolates_siblings_in_one_file(tmp_path):
    """Editing one dep in CMakeLists.txt must not mark every other dep stale.

    Every CMake dependency shares a single file, so a whole-file hash would
    flag all of them on any edit and make /deps:sync useless.
    """
    f = tmp_path / "CMakeLists.txt"
    f.write_text(
        textwrap.dedent("""\
            FetchContent_Declare(
                alpha
                URL https://example.com/alpha-1.0.0.tar.gz
            )
            FetchContent_Declare(
                beta
                URL https://example.com/beta-2.0.0.tar.gz
            )
            """)
    )
    alpha_before = hash_declaration(str(tmp_path), "CMakeLists.txt:1")
    beta_before = hash_declaration(str(tmp_path), "CMakeLists.txt:5")

    f.write_text(f.read_text().replace("alpha-1.0.0", "alpha-1.1.0"))

    assert hash_declaration(str(tmp_path), "CMakeLists.txt:1") != alpha_before
    assert hash_declaration(str(tmp_path), "CMakeLists.txt:5") == beta_before


def test_declaration_hash_handles_missing_file(tmp_path):
    assert hash_declaration(str(tmp_path), "nope.txt:1") == hash_text("<missing>")
    assert hash_declaration(str(tmp_path), "") == hash_text("<none>")


def test_hash_text_is_deterministic():
    assert hash_text("x") == hash_text("x")
    assert hash_text("x") != hash_text("y")


# ------------------------------------------------------- coupled pin resolution


def test_companion_pin_round_trips_through_the_profile(tmp_path):
    """The pin must survive CLAUDE_DEPS.md, or `apply` cannot edit it.

    `plan`/`apply` read the committed profile, not a fresh parse, so a pin that
    renders but does not parse back would silently lose its file:line and the
    coupled edit would never happen.
    """
    pin = CompanionPin(
        var="HEGEL_LIBHEGEL_VERSION", value="0.29.0",
        file="CMakeLists.txt", line=123,
        # Prose containing both a dash and parentheses — the parser works from
        # the right for exactly this reason.
        doc="libhegel version required by Hegel C++ v0.7.4 (prebuilt — do not edit)",
        matched_by="doc",
    )
    dep = Dep(name="hegel", kind="cmake-fetchcontent-url", version="0.7.4",
              declared_in="CMakeLists.txt:115", companion_pins=[pin])
    text = profile.render([dep], str(tmp_path), {"repo": "r"})
    back = next(d for d in profile.parse(text)[0] if d.name == "hegel")
    assert back.companion_pins == [pin]


def test_companion_pin_parses_the_pre_structured_form():
    """A profile written before pins were structured still has to load."""
    pin = CompanionPin.parse(
        "HEGEL_LIBHEGEL_VERSION=0.29.0 — CMakeLists.txt:123 (libhegel version required)"
    )
    assert (pin.var, pin.value, pin.file, pin.line) == (
        "HEGEL_LIBHEGEL_VERSION", "0.29.0", "CMakeLists.txt", 123,
    )
    assert pin.matched_by == ""  # unknown, not invented
    assert CompanionPin.parse("not a pin at all") is None


def test_var_candidates_strip_the_consumer_namespace():
    """The consumer namespaces the pin; the dependency declares it plain."""
    assert companion.var_candidates("HEGEL_LIBHEGEL_VERSION", "hegel") == [
        "HEGEL_LIBHEGEL_VERSION", "LIBHEGEL_VERSION",
    ]


def test_var_candidates_never_degrade_to_a_generic_name():
    """`VERSION` upstream is the dependency's own version, not a companion.

    Matching it would confidently report the wrong number, which is worse than
    reporting nothing.
    """
    cands = companion.var_candidates("FOO_BAR_VERSION", "foo")
    assert "VERSION" not in cands
    assert all(c.count("_") >= 1 for c in cands)


@pytest.mark.parametrize(
    "text",
    [
        'set(LIBHEGEL_VERSION 0.31.0 CACHE STRING "x")',
        'set(LIBHEGEL_VERSION "0.31.0")',
        "set(\n    LIBHEGEL_VERSION\n    0.31.0\n    CACHE STRING \"x\"\n)",
        'LIBHEGEL_VERSION = 0.31.0',
        '"LIBHEGEL_VERSION": "0.31.0"',
        'set(LIBHEGEL_VERSION v0.31.0)',
    ],
)
def test_find_version_reads_the_common_declaration_forms(text):
    assert companion.find_version(text, "LIBHEGEL_VERSION")[0] == "0.31.0"


@pytest.mark.parametrize(
    "text",
    [
        # A comparison is not a declaration.
        "if(LIBHEGEL_VERSION VERSION_LESS 0.31.0)",
        # A different variable that merely starts the same way.
        "set(LIBHEGEL_VERSION_MAJOR 0)\nset(OTHER 0.31.0)",
    ],
)
def test_find_version_rejects_non_declarations(text):
    assert companion.find_version(text, "LIBHEGEL_VERSION")[0] == ""


def test_find_in_prose_reads_a_release_note():
    got, evidence = companion.find_in_prose(
        "## 0.11.1\n\nRequires libhegel 0.31.0 or newer.", "libhegel"
    )
    assert got == "0.31.0"
    assert "libhegel" in evidence


def test_find_in_prose_ignores_a_short_subject():
    """Two-letter subjects match everywhere; that is noise, not evidence."""
    assert companion.find_in_prose("ab 1.2.3", "ab") == ("", "")


def _hegel_dep():
    return Dep(
        name="hegel", kind="cmake-fetchcontent-url", version="0.7.4",
        declared_in="CMakeLists.txt:2",
        upstream=Upstream(kind="github", ref="hegeldev/hegel-cpp"),
        companion_pins=[CompanionPin(
            var="HEGEL_LIBHEGEL_VERSION", value="0.29.0",
            file="CMakeLists.txt", line=6,
            doc="libhegel version required by Hegel C++ v0.7.4",
            matched_by="name",
        )],
    )


def _fake_upstream(by_ref):
    """(fetch, tree) reading a {ref: {path: text}} map instead of the network."""
    def tree(repo, ref):
        return list(by_ref.get(ref, {}))

    def fetch(repo, path, ref):
        return by_ref.get(ref, {}).get(path, "")

    return fetch, tree


def test_resolve_reads_the_required_version_from_the_new_tag():
    """The whole point: work out that 0.11.1 needs libhegel 0.31.0."""
    fetch, tree = _fake_upstream({
        "v0.7.4": {"CMakeLists.txt": "set(LIBHEGEL_VERSION 0.29.0 CACHE STRING \"\")"},
        "v0.11.1": {"CMakeLists.txt": "set(LIBHEGEL_VERSION 0.31.0 CACHE STRING \"\")"},
    })
    dep = _hegel_dep()
    got = companion.resolve(dep, dep.companion_pins[0], "0.11.1", fetch=fetch, tree=tree)
    assert got["action"] == "bump"
    assert (got["current"], got["required"]) == ("0.29.0", "0.31.0")
    assert got["confidence"] == "declared"
    # Extraction reproduced the value already in the repo, so the answer at the
    # target tag is trustworthy rather than merely plausible.
    assert got["self_check"] == "reproduced"
    assert "v0.11.1" in got["evidence"]


def test_resolve_reports_a_companion_that_does_not_move():
    fetch, tree = _fake_upstream({
        "v0.7.4": {"CMakeLists.txt": "set(LIBHEGEL_VERSION 0.29.0)"},
        "v0.8.0": {"CMakeLists.txt": "set(LIBHEGEL_VERSION 0.29.0)"},
    })
    dep = _hegel_dep()
    got = companion.resolve(dep, dep.companion_pins[0], "0.8.0", fetch=fetch, tree=tree)
    assert got["action"] == "unchanged"
    assert got["required"] == "0.29.0"


def test_resolve_refuses_when_the_self_check_disagrees():
    """If our reading of the *current* tag contradicts the repo, stop.

    Either the pin is deliberate or we are reading the wrong variable. Both
    mean the target value is not trustworthy enough to write into a build file.
    """
    fetch, tree = _fake_upstream({
        "v0.7.4": {"CMakeLists.txt": "set(LIBHEGEL_VERSION 0.20.0)"},   # != pinned 0.29.0
        "v0.11.1": {"CMakeLists.txt": "set(LIBHEGEL_VERSION 0.31.0)"},
    })
    dep = _hegel_dep()
    got = companion.resolve(dep, dep.companion_pins[0], "0.11.1", fetch=fetch, tree=tree)
    assert got["self_check"] == "diverged"
    assert got["action"] == "unresolved"
    assert got["required"] == "0.31.0"  # still reported, just not applied
    assert any("deliberate" in n for n in got["notes"])


def test_resolve_falls_back_to_release_notes_and_says_so():
    fetch, tree = _fake_upstream({
        "v0.7.4": {"CMakeLists.txt": "set(LIBHEGEL_VERSION 0.29.0)"},
        "v0.11.1": {"CMakeLists.txt": "# the pin moved somewhere unreadable\n"},
    })
    dep = _hegel_dep()
    got = companion.resolve(
        dep, dep.companion_pins[0], "0.11.1",
        target_notes="Now requires libhegel 0.31.0.", fetch=fetch, tree=tree,
    )
    assert got["action"] == "bump"
    assert got["required"] == "0.31.0"
    assert got["confidence"] == "notes"
    assert any("prose" in n for n in got["notes"])


def test_resolve_gives_up_rather_than_guessing():
    fetch, tree = _fake_upstream({
        "v0.7.4": {"CMakeLists.txt": "set(LIBHEGEL_VERSION 0.29.0)"},
        "v0.11.1": {"CMakeLists.txt": "# nothing here\n"},
    })
    dep = _hegel_dep()
    got = companion.resolve(dep, dep.companion_pins[0], "0.11.1", fetch=fetch, tree=tree)
    assert got["action"] == "unresolved"
    assert got["required"] == ""
    assert any("not declared" in n for n in got["notes"])


def test_resolve_needs_a_readable_upstream():
    dep = _hegel_dep()
    dep.upstream = Upstream(kind="distro", ref="libhegel")
    got = companion.resolve(dep, dep.companion_pins[0], "0.11.1")
    assert got["action"] == "unresolved"
    assert any("no readable upstream" in n for n in got["notes"])


def test_resolve_finds_a_pin_kept_in_a_cmake_module():
    """Not every project declares its pins in the root CMakeLists.txt."""
    fetch, tree = _fake_upstream({
        "v0.7.4": {
            "CMakeLists.txt": "project(hegel)\n",
            "cmake/libhegel.cmake": "set(LIBHEGEL_VERSION 0.29.0)",
        },
        "v0.11.1": {
            "CMakeLists.txt": "project(hegel)\n",
            "cmake/libhegel.cmake": "set(LIBHEGEL_VERSION 0.31.0)",
        },
    })
    dep = _hegel_dep()
    got = companion.resolve(dep, dep.companion_pins[0], "0.11.1", fetch=fetch, tree=tree)
    assert got["required"] == "0.31.0"
    assert "cmake/libhegel.cmake" in got["evidence"]


# ------------------------------------------------------------- atomic two-part edit


HEGEL_CMAKE = """\
FetchContent_Declare(
    hegel
    URL https://github.com/hegeldev/hegel-cpp/archive/refs/tags/v0.7.4.tar.gz
)
set(
    HEGEL_LIBHEGEL_VERSION
    0.29.0
    CACHE STRING "libhegel version required by Hegel C++ v0.7.4"
    FORCE
)
"""


def _hegel_repo(tmp_path):
    (tmp_path / "CMakeLists.txt").write_text(HEGEL_CMAKE)
    deps, _ = cmake.parse_project(str(tmp_path))
    return next(d for d in deps if d.name == "hegel")


def test_plan_bumps_a_resolved_companion_in_the_same_edit(tmp_path):
    """Source tarball and prebuilt engine move together or not at all."""
    dep = _hegel_repo(tmp_path)
    resolutions = [{
        "var": "HEGEL_LIBHEGEL_VERSION", "file": "CMakeLists.txt",
        "line": dep.companion_pins[0].line, "current": "0.29.0",
        "required": "0.31.0", "action": "bump", "confidence": "declared",
        "evidence": "hegeldev/hegel-cpp@v0.11.1 CMakeLists.txt: …", "notes": [],
    }]
    planned = apply_plan(str(tmp_path), dep, "0.11.1", companions=resolutions)

    assert planned["blocked_on"] == []
    assert [e["from"] for e in planned["companion_edits"]] == ["0.29.0"]
    # One file, one edit, both changes inside it.
    assert len(planned["edits"]) == 1
    new_text = planned["edits"][0]["_text"]
    assert "v0.11.1.tar.gz" in new_text
    assert "0.31.0" in new_text
    assert "0.29.0" not in new_text
    # The docstring names the dependency's old version, not the companion's, so
    # a span-scoped swap must leave it alone.
    assert "Hegel C++ v0.7.4" in new_text


def test_plan_leaves_an_unresolved_companion_alone_and_blocks(tmp_path):
    dep = _hegel_repo(tmp_path)
    resolutions = [{
        "var": "HEGEL_LIBHEGEL_VERSION", "file": "CMakeLists.txt",
        "line": dep.companion_pins[0].line, "current": "0.29.0",
        "required": "", "action": "unresolved", "confidence": "", "notes": [],
    }]
    planned = apply_plan(str(tmp_path), dep, "0.11.1", companions=resolutions)
    assert planned["companion_edits"] == []
    assert planned["blocked_on"] and "HEGEL_LIBHEGEL_VERSION" in planned["blocked_on"][0]
    assert "0.29.0" in planned["edits"][0]["_text"]  # untouched


def test_apply_refuses_on_an_unresolved_companion(tmp_path, monkeypatch, capsys):
    """The refusal is the feature: a half-bump fails at link time, not at
    configure time, so the user pays a full build to learn nothing."""
    from deptool.__main__ import main

    _hegel_repo(tmp_path)
    monkeypatch.setattr("deptool.__main__._resolve_companions", lambda dep, to: [{
        "var": "HEGEL_LIBHEGEL_VERSION", "file": "CMakeLists.txt", "line": 5,
        "current": "0.29.0", "required": "", "action": "unresolved",
        "confidence": "", "notes": ["not declared upstream at 0.11.1"],
    }])
    code = main(["apply", "--root", str(tmp_path), "--dep", "hegel", "--to", "0.11.1"])
    assert code == 2
    assert "refusing to bump hegel" in capsys.readouterr().err
    # Nothing was written.
    assert "v0.7.4.tar.gz" in (tmp_path / "CMakeLists.txt").read_text()
    assert not list(tmp_path.glob("*.deptool.bak"))


def test_apply_ignore_companions_restores_the_old_behaviour(tmp_path, monkeypatch, capsys):
    from deptool.__main__ import main

    _hegel_repo(tmp_path)
    monkeypatch.setattr(
        "deptool.__main__._resolve_companions",
        lambda dep, to: pytest.fail("--ignore-companions must not hit the network"),
    )
    code = main(["apply", "--root", str(tmp_path), "--dep", "hegel", "--to", "0.11.1",
                 "--ignore-companions"])
    assert code == 0
    text = (tmp_path / "CMakeLists.txt").read_text()
    assert "v0.11.1.tar.gz" in text
    assert "0.29.0" in text  # coupled pin deliberately untouched


def test_write_and_revert_cover_a_companion_in_another_file(tmp_path):
    """A coupled pin need not live in the same file as the declaration."""
    (tmp_path / "CMakeLists.txt").write_text(HEGEL_CMAKE.split("set(")[0])
    (tmp_path / "cmake").mkdir()
    (tmp_path / "cmake" / "pins.cmake").write_text(
        'set(HEGEL_LIBHEGEL_VERSION 0.29.0 CACHE STRING "x")\n'
    )
    deps, _ = cmake.parse_project(str(tmp_path))
    dep = next(d for d in deps if d.name == "hegel")
    planned = apply_plan(str(tmp_path), dep, "0.11.1", companions=[{
        "var": "HEGEL_LIBHEGEL_VERSION", "file": os.path.join("cmake", "pins.cmake"),
        "line": 1, "current": "0.29.0", "required": "0.31.0",
        "action": "bump", "confidence": "declared", "notes": [],
    }])
    assert {e["file"] for e in planned["edits"]} == {
        "CMakeLists.txt", os.path.join("cmake", "pins.cmake"),
    }

    from deptool.apply import revert, write

    assert len(write(str(tmp_path), planned)) == 2
    assert "0.31.0" in (tmp_path / "cmake" / "pins.cmake").read_text()
    assert revert(str(tmp_path), planned) is True
    assert "0.29.0" in (tmp_path / "cmake" / "pins.cmake").read_text()
    assert "v0.7.4" in (tmp_path / "CMakeLists.txt").read_text()
    assert not list(tmp_path.rglob("*.deptool.bak"))


# ------------------------------------------------------- public-header diffing
#
# The header diff exists so that "what changed upstream" stops depending on
# whether upstream wrote good release notes. Its dangerous failure is not
# missing a break — it is *inventing* one, or reporting a clean bill of health
# it never established, because either makes the report untrustworthy.


HEADER_OLD = """\
#ifndef SYNTH_H
#define SYNTH_H
/* class Ghost { */
// int commented_out(void);
typedef struct _fluid_synth_t fluid_synth_t;

int fluid_synth_noteon(fluid_synth_t* synth,
                       int chan, int key, int vel);
int fluid_synth_cc(fluid_synth_t* synth, int chan, int num, int val);
void fluid_synth_gone(fluid_synth_t* synth);

namespace hegel {
class TestCase {
 public:
  virtual void run() = 0;
  std::string name() const;
};
namespace generators {
int uniform(int lo, int hi);
}
using Seed = unsigned long;
}
#endif
"""

HEADER_NEW = """\
#ifndef FLUID_SYNTH_H_INCLUDED
#define FLUID_SYNTH_H_INCLUDED
typedef struct _fluid_synth_t fluid_synth_t;

int fluid_synth_noteon(fluid_synth_t* synth,
                       int chan, int key, int vel, int flags);
int fluid_synth_cc(fluid_synth_t* synth, int channel, int number, int value);
void fluid_synth_release(fluid_synth_t* synth);

namespace hegel {
class TestCase {
 public:
  virtual void run() override;
  std::string name() const;
};
namespace generators {
int uniform(int lo, int hi);
}
using Seed = unsigned long;
}
#endif
"""


def test_declarations_reads_real_header_shapes():
    """Multi-line params, namespaces, pure virtuals, aliases, comments."""
    from deptool.apidiff import declarations

    got = declarations(HEADER_OLD, "synth.h")
    assert "fluid_synth_noteon" in got  # parameter list spans two lines
    assert "hegel::TestCase" in got
    assert "hegel::TestCase::run" in got  # qualified by namespace *and* class
    assert "hegel::generators::uniform" in got
    assert "hegel::Seed" in got
    assert got["fluid_synth_t"][0].kind == "alias"
    # A commented-out declaration is not API, and neither is prose in a comment.
    assert "commented_out" not in got
    assert "Ghost" not in got


def test_include_guard_is_not_api():
    """Renaming a guard must not read as removing a macro.

    Guards are renamed freely. `#ifndef X` + a bare `#define X` is a guard;
    `#ifndef X` + `#define X 1` is a configuration knob, which is real API.
    """
    from deptool.apidiff import declarations, diff

    assert "SYNTH_H" not in declarations(HEADER_OLD)
    assert "FLUID_SYNTH_H_INCLUDED" not in declarations(HEADER_NEW)
    # The guard is renamed between these two headers; that must produce no
    # finding of any kind, while the genuine removal beside it still does.
    result = diff(declarations(HEADER_OLD), declarations(HEADER_NEW))
    assert [d.name for d in result["removed"]] == ["fluid_synth_gone"]
    assert not any(d.kind == "macro" for d in result["added"])

    knob = "#ifndef FLUID_BUFSIZE\n#define FLUID_BUFSIZE 64\n#endif\n"
    assert "FLUID_BUFSIZE" in declarations(knob)


def test_renaming_a_parameter_is_not_a_breaking_change():
    """`f(int chan)` -> `f(int channel)` changes nothing for a caller.

    Reporting it would bury the real findings under noise, since upstream tidies
    parameter names constantly.
    """
    from deptool.apidiff import declarations, diff

    result = diff(declarations(HEADER_OLD), declarations(HEADER_NEW))
    changed = {c["qualified"] for c in result["changed"]}
    assert "fluid_synth_cc" not in changed  # only the parameter names moved
    assert "fluid_synth_noteon" in changed  # a parameter was actually added


def test_added_override_is_not_a_signature_change():
    """`override` is a note to the compiler, not part of the callable's type.

    Observed on yaml-cpp 0.6.3 -> 0.8.0, where annotating the existing virtuals
    accounted for every single "re-signatured" finding.
    """
    from deptool.apidiff import declarations, diff

    result = diff(declarations(HEADER_OLD), declarations(HEADER_NEW))
    assert "hegel::TestCase::run" not in {c["qualified"] for c in result["changed"]}


def test_restrict_annotation_is_not_part_of_the_type():
    """Observed on FluidSynth: adding FLUID_RESTRICT read as a signature change."""
    from deptool.apidiff import declarations, diff

    before = declarations("int f(const short int *a, unsigned int n);")
    after = declarations("int f(const short int * FLUID_RESTRICT a, unsigned int n);")
    assert diff(before, after)["changed"] == []


def test_declaration_moved_to_another_header_is_not_a_removal():
    """Upstream reorganising its headers is routine and breaks nobody.

    The rule is that a removal must be absent from *every* header read at the
    target, not merely from the one it used to live in.
    """
    from deptool.apidiff import declarations, diff

    before = declarations("void fluid_synth_gone(int a);\n", "a.h")
    after = declarations("void fluid_synth_gone(int a);\n", "b.h")
    assert diff(before, after)["removed"] == []


def test_likely_rename_is_labelled_inference():
    """A rename cannot be proved from two snapshots, so it must not be asserted."""
    from deptool.apidiff import declarations, diff, likely_renames

    before = declarations("void new_fluid_sdl2_audio_driver(int a);")
    after = declarations("void new_fluid_sdl3_audio_driver(int a);")
    result = diff(before, after)
    guesses = likely_renames(result["removed"], result["added"])
    assert len(guesses) == 1
    assert guesses[0]["from"] == "new_fluid_sdl2_audio_driver"
    assert guesses[0]["to"] == "new_fluid_sdl3_audio_driver"
    assert guesses[0]["confidence"] == "inferred"


# ------------------------------------------------------------- header selection


def test_public_headers_prefers_a_declared_public_surface():
    """A project with include/ has already said what its API is.

    Observed on FluidSynth: taking every non-private header found 66 of them and
    turned internal churn into 33 "removals" no consumer could have called. Its
    real surface is the 15 under include/.
    """
    from deptool.apidiff import public_headers

    tree = [
        "include/fluidsynth.h", "include/fluidsynth/synth.h",
        "src/fluid_synth.h", "src/drivers/fluid_adriver.h",
        "test/fluid_test.h", "doc/x.h", "README.md",
    ]
    assert public_headers(tree) == ["include/fluidsynth.h", "include/fluidsynth/synth.h"]


def test_public_headers_falls_back_when_there_is_no_public_root():
    """Plenty of libraries keep their headers beside the sources."""
    from deptool.apidiff import public_headers

    tree = ["single.h", "src/lib.hpp", "tests/helper.h", "vendor/dep.h"]
    assert public_headers(tree) == ["single.h", "src/lib.hpp"]


def test_headers_naming_a_consumed_subsystem_are_read_first():
    """The budget is smaller than some libraries, so order decides coverage.

    Observed: `fluid_ramsfont_t` is declared in `ramsfont.h`, which sorted
    outside a 12-header budget, so a real removal of a type this project uses was
    reported as "nothing we consume was removed".
    """
    from deptool.apidiff import public_headers, symbol_stems

    tree = [f"include/fs/{n}.h" for n in
            ("audio", "event", "log", "midi", "mod", "ramsfont", "seq", "synth")]
    stems = symbol_stems(["fluid_ramsfont_t", "fluid_synth_noteon"])
    ranked = public_headers(tree, (), stems)
    assert set(ranked[:2]) == {"include/fs/ramsfont.h", "include/fs/synth.h"}


# --------------------------------------------------------------- intersection


def _fake_headers(files, tree_paths):
    def tree(repo, ref):
        return tree_paths if ref in {r for r, _ in files} else []

    def fetch(repo, path, ref):
        return files.get((ref, path), "")

    return fetch, tree


def _fs_dep(**kw):
    kw.setdefault("consumed", ["fluid_synth_noteon", "fluid_synth_gone"])
    kw.setdefault("sites", [
        Site("src/a.cpp", 3, "#include <fs/synth.h>"),
        Site("src/a.cpp", 41, "fluid_synth_gone"),
    ])
    return Dep(name="fluidsynth", kind="pkg-config", version="2.3.4",
               upstream=Upstream(kind="github", ref="FluidSynth/fluidsynth"), **kw)


def test_surface_change_intersects_removals_with_our_call_sites():
    """The whole point: a removal we consume, pointing at our own file:line."""
    from deptool import apidiff

    files = {
        ("v2.3.4", "include/fs/synth.h"):
            "int fluid_synth_noteon(int a);\nvoid fluid_synth_gone(int a);\n",
        ("v2.5.7", "include/fs/synth.h"): "int fluid_synth_noteon(int a);\n",
    }
    fetch, tree = _fake_headers(files, ["include/fs/synth.h"])
    got = apidiff.surface_change(_fs_dep(), "2.3.4", "2.5.7", fetch=fetch, tree=tree)

    assert got["resolved"] is True
    assert [h["symbol"] for h in got["affects_us"]] == ["fluid_synth_gone"]
    hit = got["affects_us"][0]
    assert hit["change"] == "removed"
    assert hit["sites"] == ["src/a.cpp:41"]
    # Every public header was read, so absence at the target is established.
    assert hit["confirmed"] is True


def test_a_symbol_outside_the_budget_is_hunted_before_being_called_removed():
    """A truncated read must not manufacture a removal.

    The symbol moved to a header the budget skipped. Reporting it as removed
    would send someone to rewrite a working call site.
    """
    from deptool import apidiff

    files = {
        ("v2.3.4", "include/fs/synth.h"):
            "int fluid_synth_noteon(int a);\nvoid fluid_synth_gone(int a);\n",
        ("v2.5.7", "include/fs/synth.h"): "int fluid_synth_noteon(int a);\n",
        ("v2.3.4", "include/fs/zz.h"): "",
        ("v2.5.7", "include/fs/zz.h"): "void fluid_synth_gone(int a);\n",
    }
    tree_paths = ["include/fs/synth.h", "include/fs/zz.h"]
    fetch, tree = _fake_headers(files, tree_paths)
    got = apidiff.surface_change(
        _fs_dep(), "2.3.4", "2.5.7", fetch=fetch, tree=tree, max_headers=1
    )

    assert got["truncated"] is True
    assert got["affects_us"] == []
    # And the disproved removal is gone from the raw list too, or the summary
    # counts would keep reporting it.
    assert [d["name"] for d in got["removed"]] == []
    assert any("moved, not dropped" in n for n in got["notes"])


def test_a_genuine_removal_survives_the_confirmation_pass():
    from deptool import apidiff

    files = {
        ("v2.3.4", "include/fs/synth.h"):
            "int fluid_synth_noteon(int a);\nvoid fluid_synth_gone(int a);\n",
        ("v2.5.7", "include/fs/synth.h"): "int fluid_synth_noteon(int a);\n",
        ("v2.3.4", "include/fs/zz.h"): "int unrelated(void);\n",
        ("v2.5.7", "include/fs/zz.h"): "int unrelated(void);\n",
    }
    fetch, tree = _fake_headers(files, ["include/fs/synth.h", "include/fs/zz.h"])
    got = apidiff.surface_change(
        _fs_dep(), "2.3.4", "2.5.7", fetch=fetch, tree=tree, max_headers=1
    )
    assert [h["symbol"] for h in got["affects_us"]] == ["fluid_synth_gone"]
    assert got["affects_us"][0]["confirmed"] is True


def test_unread_headers_never_become_a_clean_bill_of_health():
    """A symbol we consume that no header we read declares is *unchecked*.

    Folding it into the unaffected majority is how a truncated read turns into
    "nothing we consume was removed" — the exact wrong answer.
    """
    from deptool import apidiff

    files = {
        ("v2.3.4", "include/fs/synth.h"): "int fluid_synth_noteon(int a);\n",
        ("v2.5.7", "include/fs/synth.h"): "int fluid_synth_noteon(int a);\n",
        ("v2.3.4", "include/fs/zz.h"): "void fluid_synth_gone(int a);\n",
    }
    fetch, tree = _fake_headers(files, ["include/fs/synth.h", "include/fs/zz.h"])
    got = apidiff.surface_change(
        _fs_dep(), "2.3.4", "2.5.7", fetch=fetch, tree=tree, max_headers=1
    )
    assert got["not_located"] == ["fluid_synth_gone"]
    assert any("says nothing about them" in n for n in got["notes"])


def test_unreadable_upstream_is_reported_not_silently_passed():
    """No headers read must never look like no changes found."""
    from deptool import apidiff

    pypi = Dep(name="requests", kind="pypi", version="2.0",
               upstream=Upstream(kind="pypi", ref="requests"), consumed=["get"])
    got = apidiff.surface_change(pypi, "2.0", "2.31.0")
    assert got["resolved"] is False
    assert "no readable upstream repository" in got["reason"]
    assert got["affects_us"] == []

    fetch, tree = _fake_headers({}, [])
    missing = apidiff.surface_change(_fs_dep(), "2.3.4", "9.9.9", fetch=fetch, tree=tree)
    assert missing["resolved"] is False
    assert "could not list" in missing["reason"]


# ---------------------------------------------------- versions and prose

@pytest.mark.parametrize(
    "tag,version",
    [
        ("v1.2.3", "1.2.3"),
        ("1.2.3", "1.2.3"),
        ("yaml-cpp-0.6.3", "0.6.3"),   # jbeder/yaml-cpp
        ("release-1.11.0", "1.11.0"),  # google/googletest
        ("20230125.3", "20230125.3"),  # abseil
        ("v1.0.0-rc1", "1.0.0-rc1"),
    ],
)
def test_version_from_tag_survives_a_project_name_prefix(tag, version):
    """A prefixed tag used to parse to None, deleting the release entirely.

    Every such release vanished from `newer_than`, so yaml-cpp reported nothing
    to upgrade to, and neither companion resolution nor the header diff could
    find a ref to read.
    """
    from deptool.upstream import version_from_tag

    assert version_from_tag(tag) == version
    assert parse_version(version_from_tag(tag)) is not None


def test_migration_docs_cover_the_steps_between_the_two_versions():
    """Symfony keeps every UPGRADE-*.md, including migrations long since done."""
    from deptool.upstream import migration_doc_paths

    tree = ["README.md"] + [f"UPGRADE-6.{n}.md" for n in range(5)] + [
        "UPGRADE-7.0.md", "MIGRATING.md", "src/upgrade.cpp",
    ]
    assert migration_doc_paths(tree, "6.4.0", "6.3.0")[0] == "UPGRADE-6.4.md"
    ranged = migration_doc_paths(tree, "6.4.0", "6.1.0")
    assert ranged[:3] == ["UPGRADE-6.4.md", "UPGRADE-6.3.md", "UPGRADE-6.2.md"]
    assert "UPGRADE-6.1.md" not in ranged  # already done
    assert "UPGRADE-7.0.md" not in ranged  # beyond the target
    assert "MIGRATING.md" in ranged  # unversioned, always relevant
    assert "src/upgrade.cpp" not in ranged


def test_commit_log_only_stands_in_when_there_are_no_release_notes(monkeypatch):
    """A hundred commit subjects beside a written summary is noise, not evidence."""
    from deptool import upstream

    calls = []
    monkeypatch.setattr(upstream, "fetch_file", lambda *a: "")
    monkeypatch.setattr(upstream, "compare_commits",
                        lambda *a: calls.append(a) or {"resolved": True})

    upstream.change_prose("o/r", "v1", "v2", notes="Real notes", doc_paths=[])
    assert calls == []
    upstream.change_prose("o/r", "v1", "v2", notes="   ", doc_paths=[])
    assert len(calls) == 1


def test_compare_commits_flags_the_subjects_that_announce_a_break(monkeypatch):
    """`BREAKING CHANGE:` lives in the body by convention, not the subject."""
    from deptool import upstream

    monkeypatch.setattr(upstream, "_gh_api", lambda path: {
        "total_commits": 3,
        "commits": [
            {"commit": {"message": "Fix rounding\n\nnothing to see"}},
            {"commit": {"message": "Tidy up\n\nBREAKING CHANGE: drops fluid_foo"}},
            {"commit": {"message": "Deprecate LASH support"}},
        ],
    })
    got = upstream.compare_commits("o/r", "v1", "v2")
    assert got["total"] == 3
    assert got["breaking"] == ["Tidy up", "Deprecate LASH support"]
    assert "Fix rounding" in got["subjects"]


# ------------------------------------------------------- the consumed surface
#
# The API-surface diff can only report a break in a symbol the extractor
# recorded, so a miss here is a break that is never reported. These cover the
# categories that used to be missed wholesale, and the guardrails that keep a
# thin harvest from reading as a clean bill of health.


def _write(tmp_path, rel, text):
    p = tmp_path / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(textwrap.dedent(text))
    return p


FLUID_TU = """\
    #include <fluidsynth.h>

    static fluid_synth_t *g_synth = nullptr;
    static fluid_ramsfont_t *g_ram = nullptr;

    void init() {
        fluid_settings_t *s = new_fluid_settings();
        g_synth = new_fluid_synth(s);
        if (fluid_synth_sfload(g_synth, "x.sf2", 1) == FLUID_FAILED) {
            return;
        }
        fluid_synth_noteon(g_synth, 0, 60, 100);
        delete_fluid_synth(g_synth);
    }
"""


def test_non_function_api_is_harvested(tmp_path):
    """Types and constants are API too, and are how C libraries break you.

    The harvest required a `(` after the symbol, which restricted it to
    functions. FluidSynth's `fluid_ramsfont_t` — the removal this tool's own
    README leads with — is a type, so it could never have been recorded, and the
    header diff had nothing on our side to match the removal against.
    """
    from deptool.backends import builtin

    _write(tmp_path, "src/audio.cpp", FLUID_TU)
    dep = Dep(name="fluidsynth", kind="pkg-config")
    builtin.analyse(str(tmp_path), [dep])

    assert "fluid_ramsfont_t" in dep.consumed   # type, never followed by `(`
    assert "fluid_synth_t" in dep.consumed
    assert "FLUID_FAILED" in dep.consumed       # enum constant
    assert "fluid_synth_noteon" in dep.consumed  # still finds the calls


def test_constructor_affixes_do_not_hide_a_symbol(tmp_path):
    """`new_fluid_synth` is a plain call that does not start with `fluid_`.

    Matching on the library's prefix alone missed the whole constructor and
    destructor surface of any C library that names them this way.
    """
    from deptool.backends import builtin

    _write(tmp_path, "src/audio.cpp", FLUID_TU)
    dep = Dep(name="fluidsynth", kind="pkg-config")
    builtin.analyse(str(tmp_path), [dep])

    assert {"new_fluid_settings", "new_fluid_synth", "delete_fluid_synth"} <= set(dep.consumed)


def test_use_sites_record_what_the_use_looked_like(tmp_path):
    """A call, a type mention and a constant are not equally strong evidence."""
    from deptool.backends import builtin

    _write(tmp_path, "src/audio.cpp", FLUID_TU)
    dep = Dep(name="fluidsynth", kind="pkg-config")
    builtin.analyse(str(tmp_path), [dep])

    ctx = {s.symbol: s.context for s in dep.sites}
    assert ctx["fluid_synth_noteon"] == "call"
    assert ctx["fluid_synth_t"] == "type"
    assert ctx["FLUID_FAILED"] == "constant"


def test_comments_and_literals_are_not_uses(tmp_path):
    """Dropping the `(` requirement would otherwise harvest prose.

    A symbol named in a log message or a doc comment is not a call, and an
    include path is not a symbol at all.
    """
    from deptool.backends import builtin

    _write(tmp_path, "src/audio.cpp", """\
        #include <fluidsynth/fluid_deprecated.h>
        // TODO: stop using fluid_old_thing
        /* fluid_block_comment_thing */
        void log_it() {
            report("fluid_string_thing failed");
            fluid_real_call(1);
        }
    """)
    dep = Dep(name="fluidsynth", kind="pkg-config")
    builtin.analyse(str(tmp_path), [dep])

    assert dep.consumed == ["fluid_real_call"]


def test_a_bare_stem_prefix_still_has_to_look_like_a_call(tmp_path):
    """zlib's prefixes are function names, not namespace markers.

    `compress` and `deflate` are ordinary English words; treating them the way
    `fluid_` is treated would attribute any identifier containing them.
    """
    from deptool.backends import builtin

    _write(tmp_path, "src/z.c", """\
        #include <zlib.h>
        int go(int compress_level, int deflate_mode) {
            return compress2(0, 0, 0, 0, compress_level) + deflate_mode;
        }
    """)
    dep = Dep(name="zlib", kind="pkg-config")
    builtin.analyse(str(tmp_path), [dep])

    assert dep.consumed == ["compress2"]


def test_an_attributed_dep_with_no_symbols_says_so(tmp_path):
    """The prefix guess fails outright for a library named unlike its API.

    libsndfile's package name yields `sndfile_`; its API is `sf_open`. Nothing
    matches, and an empty `consumed` makes every upgrade look harmless — so the
    gap has to be stated where the profile can show it.
    """
    from deptool.backends import builtin

    _write(tmp_path, "src/snd.c", """\
        #include <sndfile.h>
        void go(void) { SNDFILE *f = sf_open("a.wav", 0x10, 0); sf_close(f); }
    """)
    dep = Dep(name="libsndfile", kind="pkg-config")
    builtin.analyse(str(tmp_path), [dep])

    assert dep.consumed == []
    assert dep.sites, "the include was still attributed"
    assert any("unmeasured rather than unaffected" in n for n in dep.notes)


# ------------------------------------ matching upstream's own declared names


SNDFILE_H = """\
    typedef struct SF_INFO { int frames; } SF_INFO;
    typedef struct SNDFILE_tag SNDFILE;
    SNDFILE* sf_open(const char *path, int mode, SF_INFO *info);
    long sf_readf_float(SNDFILE *f, float *ptr, long frames);
    int sf_close(SNDFILE *f);
    int sf_command(SNDFILE *f, int cmd, void *data, int n);
"""


def test_declared_names_replace_the_prefix_guess(tmp_path):
    """No guess is needed once upstream's own declarations are in hand.

    The API diff has already read them out of the headers, so intersecting them
    with our sources costs nothing and fixes every library whose symbols do not
    carry its package name.
    """
    from deptool import apidiff
    from deptool.backends import builtin

    _write(tmp_path, "src/snd.c", """\
        #include <sndfile.h>
        void go(void) {
            SF_INFO info;
            SNDFILE *f = sf_open("a.wav", 0x10, &info);
            sf_close(f);
        }
    """)
    dep = Dep(name="libsndfile", kind="pkg-config")
    builtin.analyse(str(tmp_path), [dep])
    assert dep.consumed == []

    declared = apidiff.unqualified_names(apidiff.declarations(textwrap.dedent(SNDFILE_H)))
    gained = builtin.absorb_declared(str(tmp_path), dep, declared)

    assert set(gained) == {"SF_INFO", "SNDFILE", "sf_open", "sf_close"}
    assert "sf_command" not in gained, "declared upstream but we never mention it"
    assert dep.consumed == sorted(gained)
    assert any("own headers rather than by name prefix" in n for n in dep.notes)


def test_our_own_declarations_are_not_claimed_as_theirs(tmp_path):
    """A dependency and this project can easily declare the same bare name.

    Attributing ours to them puts a symbol in the profile whose removal upstream
    would not affect us at all.
    """
    from deptool import apidiff
    from deptool.backends import builtin

    _write(tmp_path, "src/graph.cpp", """\
        #include <yaml-cpp/yaml.h>
        struct Node { int id; };
        Node *root_node = nullptr;
    """)
    dep = Dep(name="yaml-cpp", kind="cmake-fetchcontent-url")
    builtin.analyse(str(tmp_path), [dep])

    declared = apidiff.unqualified_names(apidiff.declarations("struct Node { int x; };\n"))
    assert "Node" in declared
    assert builtin.absorb_declared(str(tmp_path), dep, declared) == []


def test_namespaced_declarations_are_never_matched_bare():
    """`Node` inside `namespace YAML` is spelled `YAML::Node` at the use site.

    That spelling is already matched by the prefix/namespace harvest, and a bare
    `Node` is far too collision-prone to attribute on the name alone.
    """
    from deptool import apidiff

    index = apidiff.declarations(
        "namespace YAML {\nclass Node { };\n}\nint free_fn(int a);\n"
    )
    assert apidiff.unqualified_names(index) == {"free_fn"}


def test_widening_the_surface_finds_a_break_that_was_invisible(tmp_path):
    """End to end: the case that used to be skipped in silence.

    With no consumed symbols the dependency was never diffed at all, so a
    signature change in a function we call at a known line produced no output
    whatsoever.
    """
    from deptool import apidiff

    after = textwrap.dedent(SNDFILE_H).replace("float *ptr", "double *ptr")
    _write(tmp_path, "src/snd.c", """\
        #include <sndfile.h>
        void go(void) {
            SF_INFO info;
            SNDFILE *f = sf_open("a.wav", 0x10, &info);
            sf_readf_float(f, 0, 0);
        }
    """)
    dep = Dep(name="libsndfile", kind="pkg-config", version="1.0.28",
              upstream=Upstream(kind="github", ref="libsndfile/libsndfile"))

    files = {
        ("1.0.28", "include/sndfile.h"): textwrap.dedent(SNDFILE_H),
        ("1.2.2", "include/sndfile.h"): after,
    }
    fetch, tree = _fake_headers(files, ["include/sndfile.h"])
    got = apidiff.surface_change(
        dep, "1.0.28", "1.2.2",
        versions=[{"version": "1.0.28", "tag": "1.0.28"},
                  {"version": "1.2.2", "tag": "1.2.2"}],
        fetch=fetch, tree=tree, root=str(tmp_path),
    )

    assert "sf_readf_float" in got["consumed_added"]
    hit = next(h for h in got["affects_us"] if h["symbol"] == "sf_readf_float")
    assert hit["change"] == "signature"
    assert hit["sites"] == ["src/snd.c:5"]
    # Nothing we consume went unaccounted for, since the widening drew on the
    # very headers that were read.
    assert got["not_located"] == []


def test_an_empty_consumed_surface_forbids_a_clean_bill_of_health():
    """Zero symbols in means an empty intersection out, whatever upstream did."""
    from deptool import apidiff

    dep = Dep(name="opaque", kind="pkg-config", version="1.0",
              upstream=Upstream(kind="github", ref="o/r"))
    files = {
        ("1.0", "include/o.h"): "int gone_symbol(int a);\nint kept(int a);\n",
        ("2.0", "include/o.h"): "int kept(int a);\n",
    }
    fetch, tree = _fake_headers(files, ["include/o.h"])
    got = apidiff.surface_change(dep, "1.0", "2.0", fetch=fetch, tree=tree)

    assert got["resolved"] is True
    assert got["consumed_count"] == 0
    assert got["affects_us"] == []
    assert any("vacuous" in n for n in got["notes"])


def test_report_distinguishes_unmeasured_from_unaffected():
    """"nothing we consume changed" is unsayable without a consumed surface."""
    from deptool.__main__ import _api_lines

    base = {"resolved": True, "from_ref": "v1", "to_ref": "v2",
            "headers_read": 3, "headers_available": 3, "removed": [{}, {}],
            "affects_us": []}

    measured = "\n".join(_api_lines(dict(base, consumed_count=9)))
    assert "nothing we consume was removed" in measured

    unmeasured = "\n".join(_api_lines(dict(base, consumed_count=0)))
    assert "nothing we consume" not in unmeasured
    assert "unmeasured, not unaffected" in unmeasured


def test_a_dep_with_no_symbols_is_still_worth_diffing():
    """The gate used to require `consumed`, which skipped exactly the deps
    whose surface the diff itself could have recovered."""
    from deptool.__main__ import _worth_diffing

    dep = Dep(name="libsndfile", kind="pkg-config", version="1.0.28",
              upstream=Upstream(kind="github", ref="libsndfile/libsndfile"),
              sites=[Site("src/snd.c", 1, "#include <sndfile.h>")])
    info = {"resolved": True, "behind_by": 4, "unpinned": False}

    assert dep.consumed == []
    assert _worth_diffing(dep, info) is True


# ----------------------------------------------- the upstream side of the match


def test_a_generated_public_header_is_still_a_public_header():
    """libsndfile 1.0.28's entire C API is `src/sndfile.h.in`.

    With only literal suffixes recognised, the diff read 20 *internal* headers,
    never saw `sf_open`, and reported removals drawn from internal churn. The
    include hint has to reach the template too, or it sorts as an also-ran.
    """
    from deptool.apidiff import as_header_path, is_header, public_headers

    assert is_header("src/sndfile.h.in")
    assert as_header_path("src/sndfile.h.in") == "src/sndfile.h"
    assert not is_header("README.in")

    tree = ["Octave/format.h", "src/common.h", "src/sndfile.h.in", "src/sndfile.hh"]
    assert public_headers(tree, ["sndfile.h"])[0] == "src/sndfile.h.in"


def test_enumerators_are_part_of_the_surface():
    """`enum { A, B }` publishes A and B as surely as a function does.

    Only the enum's own tag was recorded, so every constant a project consumed
    came back `not_located` — unchecked however many headers were read. The brace
    sits on its own line as often as not, which is how FluidSynth writes them.
    """
    from deptool.apidiff import declarations

    index = declarations(textwrap.dedent("""\
        enum fluid_chorus_mod
        {
            FLUID_CHORUS_MOD_SINE = 0,      /**< sine */
            FLUID_CHORUS_MOD_TRIANGLE = 1
        };
        typedef enum { FLUID_OK = 0, FLUID_FAILED = -1 } fluid_status;
        enum class Mode : uint8_t { Fast, Slow };
        enum forward_declared_only;
    """))
    names = {d.name for decls in index.values() for d in decls}

    assert {"FLUID_CHORUS_MOD_SINE", "FLUID_CHORUS_MOD_TRIANGLE"} <= names
    assert {"FLUID_OK", "FLUID_FAILED"} <= names
    assert {"Fast", "Slow"} <= names
    assert "uint8_t" not in names, "the base type is not an enumerator"


def test_renumbering_an_enum_is_not_a_signature_change():
    """A changed value is an ABI concern, and reporting it as an API break
    would fire on every enum that gained a member in the middle."""
    from deptool.apidiff import declarations, diff

    before = declarations("enum E { A = 0, B = 1 };\n", "e.h")
    after = declarations("enum E { A = 0, NEW = 1, B = 2 };\n", "e.h")
    got = diff(before, after)

    assert got["changed"] == []
    assert [d.name for d in got["removed"]] == []
    assert "NEW" in {d.name for d in got["added"]}


def test_a_removed_enumerator_we_consume_is_reported():
    """The whole reason to extract them: it is a hard compile break."""
    from deptool import apidiff

    dep = Dep(name="fluidsynth", kind="pkg-config", version="2.3.4",
              upstream=Upstream(kind="github", ref="FluidSynth/fluidsynth"),
              consumed=["FLUID_CHORUS_MOD_SINE"],
              sites=[Site("src/audio.cpp", 13, "FLUID_CHORUS_MOD_SINE")])
    files = {
        ("v2.3.4", "include/fs/synth.h"): "enum m\n{\n  FLUID_CHORUS_MOD_SINE = 0,\n  X = 1\n};\n",
        ("v2.5.7", "include/fs/synth.h"): "enum m\n{\n  X = 1\n};\n",
    }
    fetch, tree = _fake_headers(files, ["include/fs/synth.h"])
    got = apidiff.surface_change(dep, "2.3.4", "2.5.7", fetch=fetch, tree=tree)

    hit = got["affects_us"][0]
    assert hit["symbol"] == "FLUID_CHORUS_MOD_SINE"
    assert hit["change"] == "removed"
    assert hit["kind"] == "enumerator"
    assert hit["sites"] == ["src/audio.cpp:13"]
    assert got["not_located"] == []


def test_renaming_an_opaque_tag_is_not_a_signature_change():
    """libsndfile 1.2.2 renamed the struct behind `SNDFILE`.

    The tag of an incomplete type is not something a consumer of the alias can
    see or spell, so reporting it would send someone to fix call sites that
    compile perfectly — the same class of false positive as a renamed parameter.
    """
    from deptool.apidiff import declarations, diff

    before = declarations("typedef struct SNDFILE_tag SNDFILE;\n", "sndfile.h")
    after = declarations("typedef struct sf_private_tag SNDFILE;\n", "sndfile.h")
    assert diff(before, after)["changed"] == []

    # Decoration is still part of the type, and a concrete target still is too.
    gained_ptr = declarations("typedef struct SNDFILE_tag *SNDFILE;\n", "sndfile.h")
    assert [c["name"] for c in diff(before, gained_ptr)["changed"]] == ["SNDFILE"]
    to_int = declarations("typedef int SNDFILE;\n", "sndfile.h")
    assert [c["name"] for c in diff(before, to_int)["changed"]] == ["SNDFILE"]


# ------------------------------------------------ discovery walk and reconciliation
#
# The failure these cover was not a degraded answer but a confidently wrong one:
# manifests were only ever looked for at the repository root, so a project
# keeping one per target platform reported *no* pins, and what surfaced instead
# was the version-less `find_package` calls resolving to a distro lookup — the
# machine's system library compared against what distros ship.


def _platform_repo(tmp_path, zlib=("1.3", "1.3.1"), ssl=("3.0.15", "3.0.15")):
    """A repo shaped like the second real project: a manifest per platform,
    nested, with the build system naming the same libraries differently."""
    (tmp_path / "CMakeLists.txt").write_text(
        textwrap.dedent("""
            project(sensor CXX)
            find_package(CURL REQUIRED)
            find_package(ZLIB REQUIRED)
            find_package(OpenSSL REQUIRED)
            find_package(LibArchive REQUIRED)
        """)
    )
    for i, (plat, z, s) in enumerate(
        (("linux", zlib[0], ssl[0]), ("windows", zlib[1], ssl[1]))
    ):
        d = tmp_path / "deps" / plat
        d.mkdir(parents=True)
        (d / "conanfile.txt").write_text(
            f"[requires]\nlibcurl/8.4.0\nzlib/{z}\nopenssl/{s}\nlibarchive/3.8.1\n"
        )
    return str(tmp_path)


def test_manifests_are_found_below_the_root(tmp_path):
    from deptool import discover

    root = _platform_repo(tmp_path)
    found = discover.detect_manifests(root)
    assert ("CMakeLists.txt", "cmake") in found
    assert ("deps/linux/conanfile.txt", "conan") in found
    assert ("deps/windows/conanfile.txt", "conan") in found
    # Shallowest first, so the root manifest is the natural primary.
    assert found[0][0] == "CMakeLists.txt"


def test_nested_manifest_pins_are_what_gets_reported(tmp_path):
    """The whole point of the walk: pins become visible, and the unpinned CMake
    declaration no longer wins and drags the dependency to a distro lookup."""
    from deptool import discover

    deps, _ = discover.discover(_platform_repo(tmp_path))
    by_name = {d.name: d for d in deps}
    assert set(by_name) == {"libcurl", "zlib", "openssl", "libarchive"}
    assert by_name["libcurl"].version == "8.4.0"
    # Not `distro:CURL` — that framed the fix as a CI-image change.
    assert by_name["libcurl"].upstream.kind == "conan"
    assert by_name["libcurl"].aliases == ["CURL"]


def test_build_output_and_vendored_containers_are_not_walked(tmp_path):
    from deptool import discover

    root = _platform_repo(tmp_path)
    for junk in ("build/deps", "third_party/foo", "node_modules/bar", ".hidden"):
        d = tmp_path / junk
        d.mkdir(parents=True)
        (d / "conanfile.txt").write_text("[requires]\nbogus/9.9.9\n")
    deps, _ = discover.discover(root)
    assert "bogus" not in {d.name for d in deps}


def test_a_declared_submodule_is_not_our_dependency(tmp_path):
    """A vendored checkout under its own name slips past SKIP_DIRS, so the
    declared fact in .gitmodules is read instead of guessing at directory names."""
    from deptool import discover

    root = _platform_repo(tmp_path)
    vendored = tmp_path / "tests" / "googletest"
    vendored.mkdir(parents=True)
    (vendored / "pyproject.toml").write_text(
        '[project]\nname = "vendored"\ndependencies = ["requests>=2.0"]\n'
    )

    # Without .gitmodules it is still visible: finding F is open, not solved.
    assert "requests" in {d.name for d in discover.discover(root)[0]}

    (tmp_path / ".gitmodules").write_text(
        '[submodule "tests/googletest"]\n\tpath = tests/googletest\n\turl = x\n'
    )
    assert discover.submodule_paths(root) == {"tests/googletest"}
    assert "requests" not in {d.name for d in discover.discover(root)[0]}


# ------------------------------------------------------------- canonical names


@pytest.mark.parametrize(
    "cmake_name,package_name",
    [
        ("CURL", "libcurl"),
        ("ZLIB", "zlib"),
        ("OpenSSL", "openssl"),
        ("LibArchive", "libarchive"),
        ("PNG", "libpng"),
        ("JPEG", "libjpeg"),
        ("SQLite3", "sqlite3"),
        ("nlohmann_json", "nlohmann_json"),
        ("Iconv", "libiconv"),
        ("LibXml2", "libxml2"),
    ],
)
def test_cmake_and_package_names_canonicalise_together(cmake_name, package_name):
    """Every alias observed on a real project, resolved by one generic rule
    rather than a table of pairs — see ROADMAP.md on constants that get guessed
    once and inherited forever."""
    from deptool.discover import canonical_name

    assert canonical_name(cmake_name) == canonical_name(package_name)


def test_canonicalising_does_not_strip_lib_off_a_short_name():
    from deptool.discover import canonical_name

    assert canonical_name("libc") == "libc"
    assert canonical_name("libm") == "libm"


def test_unrelated_libraries_do_not_canonicalise_together():
    from deptool.discover import canonical_name

    assert canonical_name("zlib") != canonical_name("zlib-ng")
    assert canonical_name("openssl") != canonical_name("libressl")


def test_different_ecosystems_are_not_reconciled():
    """npm's `zlib` and Conan's `zlib` are not the same artefact."""
    from deptool.discover import reconcile

    a = Dep(name="zlib", kind="npm", version="1.0.0", declared_in="package.json (dependencies)",
            upstream=Upstream(kind="npm", ref="zlib"))
    b = Dep(name="zlib", kind="conan", version="1.3", declared_in="conanfile.txt:2",
            upstream=Upstream(kind="conan", ref="zlib"))
    assert len(reconcile([a, b])) == 2


# ---------------------------------------------------------- variant divergence


def test_variants_disagreeing_is_a_finding(tmp_path):
    """No upstream lookup produces this, and for a project already current on
    everything it is worth more than "you are a minor version behind"."""
    from deptool import discover

    deps, _ = discover.discover(_platform_repo(tmp_path))
    zlib = next(d for d in deps if d.name == "zlib")
    assert zlib.diverges()
    assert zlib.pin_variants() == {
        "1.3": ["deps/linux/conanfile.txt:3"],
        "1.3.1": ["deps/windows/conanfile.txt:3"],
    }
    assert "disagree" in zlib.divergence_note()

    agreeing = next(d for d in deps if d.name == "libcurl")
    assert not agreeing.diverges()
    assert agreeing.divergence_note() == ""


def test_an_unversioned_declaration_is_not_disagreement(tmp_path):
    """`find_package(CURL)` carries no version, so it cannot contradict a pin."""
    from deptool import discover

    deps, _ = discover.discover(_platform_repo(tmp_path, zlib=("1.3", "1.3")))
    zlib = next(d for d in deps if d.name == "zlib")
    assert len(zlib.declarations) == 3  # two manifests plus the find_package
    assert not zlib.diverges()


def test_the_oldest_pin_is_the_primary_when_variants_disagree(tmp_path):
    """The reported version has to be the worst we ship: it is the copy an
    advisory is most likely to match and the one that breaks first, so leading
    with the newest would understate the exposure."""
    from deptool import discover

    deps, _ = discover.discover(_platform_repo(tmp_path, ssl=("3.4.0", "3.0.15")))
    ssl = next(d for d in deps if d.name == "openssl")
    assert ssl.version == "3.0.15"
    assert ssl.declared_in == "deps/windows/conanfile.txt:4"


def test_reconciliation_keeps_every_declaration_and_name(tmp_path):
    from deptool import discover

    deps, _ = discover.discover(_platform_repo(tmp_path))
    curl = next(d for d in deps if d.name == "libcurl")
    assert [d.where() for d in curl.declarations] == [
        "CMakeLists.txt:3",
        "deps/linux/conanfile.txt:2",
        "deps/windows/conanfile.txt:2",
    ]
    kinds = {d.kind for d in curl.declarations}
    assert kinds == {"cmake-find-package", "conan"}
    assert any("pinned by the package manager" in n for n in curl.notes)
    assert any("pinned in 2 places" in n for n in curl.notes)


# ------------------------------------------------------- declaration round trip


@pytest.mark.parametrize(
    "decl",
    [
        Declaration(path="conanfile.txt", line=4, kind="conan",
                    version="3.0.15", raw_pin="openssl/3.0.15"),
        Declaration(path="CMakeLists.txt", line=3, kind="cmake-find-package"),
        Declaration(path="vcpkg.json", kind="vcpkg", version="1.2", raw_pin="1.2"),
    ],
)
def test_declaration_round_trips_through_its_rendered_form(decl):
    back = Declaration.parse(decl.render())
    assert back is not None
    assert (back.path, back.line, back.kind, back.version) == (
        decl.path, decl.line, decl.kind, decl.version
    )
    assert back.raw_pin == (decl.raw_pin or decl.version)


def test_declaration_variant_is_the_declaring_directory():
    d = Declaration(path="deps/winarm/conanfile.txt", line=2)
    assert d.variant == "deps/winarm"
    assert Declaration(path="conanfile.txt", line=2).variant == ""


def test_declarations_survive_the_profile_file(tmp_path):
    dep = Dep(
        name="openssl", kind="conan", version="3.0.15", raw_pin="openssl/3.0.15",
        declared_in="deps/linux/conanfile.txt:4", aliases=["OpenSSL"],
        declarations=[
            Declaration(path="CMakeLists.txt", line=3, kind="cmake-find-package"),
            Declaration(path="deps/linux/conanfile.txt", line=4, kind="conan",
                        version="3.0.15", raw_pin="openssl/3.0.15"),
            Declaration(path="deps/win/conanfile.txt", line=4, kind="conan",
                        version="3.4.0", raw_pin="openssl/3.4.0"),
        ],
    )
    text = profile.render([dep], str(tmp_path), {})
    back, _ = profile.parse(text)
    assert [d.render() for d in back[0].declarations] == [
        d.render() for d in dep.declarations
    ]
    assert back[0].aliases == ["OpenSSL"]
    assert back[0].diverges()


def test_a_single_declaration_is_not_spelled_out_twice(tmp_path):
    """The `- pinned:` line already says it; a one-item list is noise."""
    dep = Dep(name="fluidsynth", kind="cmake-fetchcontent-url", version="2.3.4",
              declared_in="CMakeLists.txt:4",
              declarations=[Declaration(path="CMakeLists.txt", line=4,
                                        kind="cmake-fetchcontent-url", version="2.3.4")])
    assert "- declarations:" not in profile.render([dep], str(tmp_path), {})


# ------------------------------------------------------ bumping several pins at once


def _multi_pin_dep(tmp_path, second="8.4.0"):
    for plat, ver in (("linux", "8.4.0"), ("win", second)):
        d = tmp_path / "deps" / plat
        d.mkdir(parents=True)
        (d / "conanfile.txt").write_text(f"[requires]\nlibcurl/{ver}\nzlib/1.3\n")
    return Dep(
        name="libcurl", kind="conan", version="8.4.0", raw_pin="libcurl/8.4.0",
        declared_in="deps/linux/conanfile.txt:2",
        declarations=[
            Declaration(path="deps/linux/conanfile.txt", line=2, kind="conan",
                        version="8.4.0", raw_pin="libcurl/8.4.0"),
            Declaration(path="deps/win/conanfile.txt", line=2, kind="conan",
                        version=second, raw_pin=f"libcurl/{second}"),
        ],
    )


def test_a_bump_edits_every_manifest_pinning_the_same_version(tmp_path):
    """Editing one platform's manifest alone would silently create exactly the
    divergence `diverges()` exists to report."""
    dep = _multi_pin_dep(tmp_path)
    planned = apply_plan(str(tmp_path), dep, "8.5.0")
    assert sorted(e["file"] for e in planned["edits"]) == [
        "deps/linux/conanfile.txt",
        "deps/win/conanfile.txt",
    ]
    assert planned["also_pinned_in"] == ["deps/win/conanfile.txt:2"]
    assert "8.5.0" in planned["diff"]
    # And only this dependency moves.
    for edit in planned["edits"]:
        assert "zlib/1.3\n" in edit["_text"]


def test_a_bump_refuses_when_the_manifests_disagree(tmp_path):
    """Same rule as an unresolved companion pin: stopping beats guessing, since
    "bump to X" does not say which of the current versions it starts from."""
    from deptool.apply import ApplyError

    dep = _multi_pin_dep(tmp_path, second="8.2.0")
    with pytest.raises(ApplyError, match="disagree on the version"):
        apply_plan(str(tmp_path), dep, "8.5.0")


def test_a_missing_sibling_manifest_is_reported_not_ignored(tmp_path):
    dep = _multi_pin_dep(tmp_path)
    (tmp_path / "deps" / "win" / "conanfile.txt").unlink()
    planned = apply_plan(str(tmp_path), dep, "8.5.0")
    assert planned["also_pinned_in"] == []
    assert any("deps/win/conanfile.txt:2" in b for b in planned["blocked_on"])


# --------------------------------------------------------- cmake entry points


def test_a_reachable_nested_cmakelists_is_not_a_second_entry_point(tmp_path):
    """`parse_project` already follows add_subdirectory, so parsing it again
    would double-count."""
    from deptool.discover import _cmake_entries

    assert _cmake_entries(["CMakeLists.txt", "app/CMakeLists.txt"]) == ["CMakeLists.txt"]


def test_without_a_root_cmakelists_the_topmost_nested_ones_are_entries():
    from deptool.discover import _cmake_entries

    assert _cmake_entries(
        ["app/CMakeLists.txt", "app/src/CMakeLists.txt", "driver/CMakeLists.txt"]
    ) == ["app/CMakeLists.txt", "driver/CMakeLists.txt"]


# ------------------------------------------------- python manifests, per directory


def test_requirements_is_the_fallback_per_directory_not_per_repository(tmp_path):
    from deptool import discover

    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "top"\ndependencies = ["httpx==0.27"]\n'
    )
    (tmp_path / "requirements.txt").write_text("ignored-mirror==1.0\n")
    tool = tmp_path / "tool"
    tool.mkdir()
    (tool / "requirements.txt").write_text("deepdiff==8.6.2\n")

    deps, files = discover.discover(str(tmp_path))
    names = {d.name for d in deps}
    assert names == {"httpx", "deepdiff"}
    assert "ignored-mirror" not in names
    assert next(d for d in deps if d.name == "deepdiff").declared_in == "tool/requirements.txt:1"


def test_a_manifest_we_cannot_read_is_still_listed(tmp_path):
    """A Poetry pyproject yields nothing today (ROADMAP item 2c). It must not
    look like a project with no dependencies."""
    from deptool import discover

    (tmp_path / "pyproject.toml").write_text(
        '[tool.poetry.dependencies]\nrequests = "^2.0"\n'
    )
    deps, files = discover.discover(str(tmp_path))
    assert deps == []
    assert files == ["pyproject.toml"]


# ------------------------------------------- attribution survives the rename


def test_symbol_attribution_uses_every_name_the_dep_is_declared_under():
    """Reconciliation leaves the package-manager spelling as the record's name,
    and either spelling can be the one that resolves to a header."""
    from deptool.backends.builtin import _profile_for

    plain = _profile_for(Dep(name="libcurl", kind="conan"))
    assert "curl/" in plain["includes"]

    # A curated entry reached through the alias beats the generic stem guess
    # derived from the package name.
    aliased = _profile_for(Dep(name="somepkg", kind="conan", aliases=["OpenSSL"]))
    assert aliased["includes"] == ["openssl/"]
    assert aliased["prefixes"] == ["SSL_", "EVP_", "X509_", "BIO_"]

    # With no curated entry, both names contribute candidates.
    both = _profile_for(Dep(name="libarchive", kind="conan", aliases=["LibArchive"]))
    assert "archive.h" in both["includes"]
    assert "libarchive/" in both["includes"]


def test_a_submodule_reached_by_add_subdirectory_is_not_read(tmp_path):
    """Excluding a vendored tree from the manifest walk is not enough: the CMake
    reader follows add_subdirectory into it and would read its internal
    find_package calls as ours."""
    from deptool import discover

    (tmp_path / "CMakeLists.txt").write_text(
        "project(app CXX)\nfind_package(CURL REQUIRED)\nadd_subdirectory(tests/gtest)\n"
    )
    vendored = tmp_path / "tests" / "gtest"
    vendored.mkdir(parents=True)
    (vendored / "CMakeLists.txt").write_text("find_package(SomeVendoredThing REQUIRED)\n")

    assert "SomeVendoredThing" in {d.name for d in discover.discover(str(tmp_path))[0]}

    (tmp_path / ".gitmodules").write_text(
        '[submodule "tests/gtest"]\n\tpath = tests/gtest\n\turl = x\n'
    )
    deps, files = discover.discover(str(tmp_path))
    assert "SomeVendoredThing" not in {d.name for d in deps}
    assert not [f for f in files if "gtest" in f]


def test_cmake_built_in_find_modules_are_not_dependencies(tmp_path):
    """`find_package(PythonInterp)` inside a vendored test framework was reported
    as a runtime dependency of the product."""
    from deptool import discover

    (tmp_path / "CMakeLists.txt").write_text(
        "project(app CXX)\nfind_package(PythonInterp)\nfind_package(Threads)\n"
        "find_package(CURL REQUIRED)\n"
    )
    assert {d.name for d in discover.discover(str(tmp_path))[0]} == {"CURL"}


# ------------------------------------------------- conan center as a version source
#
# Reconciliation made this necessary rather than optional: once a Conan-pinned
# library stops being reported under its CMake name, `distro:` is no longer the
# upstream, and without a resolver for `conan:` the daily driver went silent about
# every dependency of a whole class of project.

_CONAN_CONFIG = """\
# Versions to keep:
#  - the last patch of each supported release
versions:
  "4.0.1":
    folder: "4.x.x"
  "3.6.3":
    folder: "3.x.x"
  "3.0.15":
    folder: "3.x.x"
"""


def test_conan_versions_come_from_the_recipe_index(monkeypatch):
    from deptool import upstream

    monkeypatch.setattr(upstream, "fetch_file", lambda *a, **k: _CONAN_CONFIG)
    got = upstream._conan_versions("openssl")
    assert [v["version"] for v in got] == ["4.0.1", "3.6.3", "3.0.15"]
    # `folder:` carries a value, so it is not a version key; the comment header
    # is not one either.
    assert all(v["tag"] == v["version"] for v in got)


def test_a_missing_recipe_resolves_to_nothing_rather_than_raising(monkeypatch):
    from deptool import upstream

    monkeypatch.setattr(upstream, "fetch_file", lambda *a, **k: "")
    assert upstream._conan_versions("no-such-package") == []


def test_behind_by_is_marked_a_floor_for_a_pruned_catalogue(monkeypatch):
    """Conan Center deletes old recipe versions — `zlib` lists exactly one — so
    a count of what is newer is a lower bound, not a release count."""
    from deptool import upstream

    monkeypatch.setattr(upstream, "fetch_file", lambda *a, **k: _CONAN_CONFIG)
    dep = Dep(name="openssl", kind="conan", version="3.0.15",
              upstream=Upstream(kind="conan", ref="openssl"))
    info = upstream.summarise(dep)
    assert info["behind_by"] == 2
    assert info["behind_by_is_floor"] is True
    assert info["pin_unavailable"] is False

    pypi = Dep(name="httpx", kind="pypi", version="0.27.0",
               upstream=Upstream(kind="pypi", ref="httpx"))
    monkeypatch.setattr(upstream, "fetch_versions", lambda d: [
        {"version": "0.28.0", "tag": "0.28.0", "date": "", "prerelease": False,
         "notes": "", "url": ""},
        {"version": "0.27.0", "tag": "0.27.0", "date": "", "prerelease": False,
         "notes": "", "url": ""},
    ])
    assert upstream.summarise(pypi)["behind_by_is_floor"] is False


def test_a_pin_the_index_no_longer_offers_is_a_finding(monkeypatch):
    """A fresh install cannot reproduce the build — independent of whether an
    upgrade is otherwise due."""
    from deptool import upstream

    monkeypatch.setattr(upstream, "fetch_file", lambda *a, **k: _CONAN_CONFIG)
    dep = Dep(name="openssl", kind="conan", version="3.0.13",
              upstream=Upstream(kind="conan", ref="openssl"))
    assert upstream.summarise(dep)["pin_unavailable"] is True


def test_a_survey_catalogue_never_reports_a_pin_as_unavailable(monkeypatch):
    """Repology says what distros happen to ship, so a version missing from it
    is a gap in our data rather than a fact about the dependency."""
    from deptool import upstream

    monkeypatch.setattr(upstream, "fetch_versions", lambda d: [
        {"version": "2.5.7", "tag": "2.5.7", "date": "", "prerelease": False,
         "notes": "", "url": ""},
    ])
    dep = Dep(name="fluidsynth", kind="pkg-config", version="2.3.4",
              upstream=Upstream(kind="distro", ref="fluidsynth"))
    assert upstream.summarise(dep)["pin_unavailable"] is False


# --------------------------------------------- verifying without touching the tree
#
# ROADMAP item 3. A verification that edits the working tree and *then* finds the
# build broken has already done the thing it was run to prevent, and the tool is
# only ever invited to edit the pin — not to leave anything else behind.


def _pinned_repo(tmp_path):
    (tmp_path / "CMakeLists.txt").write_text(
        "project(app CXX)\n"
        "include(FetchContent)\n"
        "FetchContent_Declare(hegel\n"
        "  URL https://github.com/h/h/archive/refs/tags/v0.7.4.tar.gz)\n"
    )
    return Dep(
        name="hegel", kind="cmake-fetchcontent-url", version="0.7.4",
        raw_pin="https://github.com/h/h/archive/refs/tags/v0.7.4.tar.gz",
        declared_in="CMakeLists.txt:3",
        declarations=[Declaration(path="CMakeLists.txt", line=3,
                                  kind="cmake-fetchcontent-url", version="0.7.4")],
    )


def test_backups_are_kept_out_of_the_users_tree(tmp_path, monkeypatch):
    from deptool.apply import backup_dir, revert, write

    cache = tmp_path / "cache"
    monkeypatch.setenv("XDG_CACHE_HOME", str(cache))
    repo = tmp_path / "repo"
    repo.mkdir()
    dep = _pinned_repo(repo)

    planned = apply_plan(str(repo), dep, "0.9.0")
    assert write(str(repo), planned) == ["CMakeLists.txt"]
    assert "v0.9.0" in (repo / "CMakeLists.txt").read_text()

    # Nothing new in the tree at all — a `.bak` beside the file lands in
    # `git status` and gets committed by accident.
    assert [p.name for p in repo.iterdir()] == ["CMakeLists.txt"]
    assert os.path.isfile(os.path.join(backup_dir(str(repo)), "CMakeLists.txt"))
    assert str(cache) in backup_dir(str(repo))

    assert revert(str(repo), planned) is True
    assert "v0.7.4" in (repo / "CMakeLists.txt").read_text()


def test_a_legacy_in_tree_backup_is_still_honoured_and_removed(tmp_path, monkeypatch):
    """A bump applied by an older version must still be undoable, and undoing it
    should take the litter with it."""
    from deptool.apply import revert

    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "CMakeLists.txt").write_text("bumped\n")
    (repo / "CMakeLists.txt.deptool.bak").write_text("original\n")

    assert revert(str(repo), {"edits": [{"file": "CMakeLists.txt"}]}) is True
    assert (repo / "CMakeLists.txt").read_text() == "original\n"
    assert not list(repo.glob("*.deptool.bak"))


def test_the_sandbox_copies_sources_and_skips_build_output(tmp_path):
    from deptool.apply import make_sandbox

    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "CMakeLists.txt").write_text("project(app)\n")
    (repo / "src" / "main.cpp").write_text("int main(){}\n")
    for junk in ("build", ".git", "node_modules", "_deps"):
        d = repo / junk
        d.mkdir()
        (d / "heavy.bin").write_text("x" * 100)

    sandbox = make_sandbox(str(repo))
    try:
        assert os.path.isfile(os.path.join(sandbox, "src", "main.cpp"))
        assert sorted(os.listdir(sandbox)) == ["CMakeLists.txt", "src"]
        # And it is not inside the user's checkout.
        assert not os.path.abspath(sandbox).startswith(os.path.abspath(str(repo)))
    finally:
        shutil.rmtree(os.path.dirname(sandbox), ignore_errors=True)


def test_a_failed_verification_leaves_the_tree_untouched(tmp_path, monkeypatch, capsys):
    """The whole point of item 3: the edit is proven in a copy, so a bump that
    does not build is a no-op on the user's checkout."""
    from deptool import apply as apply_mod
    from deptool.__main__ import main

    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    repo = tmp_path / "repo"
    repo.mkdir()
    dep = _pinned_repo(repo)
    monkeypatch.setattr("deptool.__main__._find_dep", lambda root, name, ingest=True: dep)
    monkeypatch.setattr("deptool.__main__._resolve_companions", lambda d, to: [])

    seen: dict = {}

    def fake_verify(root, build_dir="build"):
        seen["root"] = root
        seen["edited"] = open(os.path.join(root, "CMakeLists.txt")).read()
        return [{"cmd": "cmake --build", "ok": False, "output": "error: no such tag"}]

    monkeypatch.setattr(apply_mod, "verify", fake_verify)

    code = main(["apply", "--root", str(repo), "--dep", "hegel",
                 "--to", "0.9.0", "--verify"])
    assert code == 3
    # The build ran somewhere else, against the edited copy.
    assert seen["root"] != str(repo)
    assert "v0.9.0" in seen["edited"]
    # And the real file never changed.
    assert "v0.7.4" in (repo / "CMakeLists.txt").read_text()
    assert list(repo.iterdir()) == [repo / "CMakeLists.txt"]
    err = capsys.readouterr().err
    assert "Your tree is unchanged." in err
    assert "error: no such tag" in err


def test_a_passing_verification_then_writes_the_bump(tmp_path, monkeypatch):
    from deptool import apply as apply_mod
    from deptool.__main__ import main

    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    repo = tmp_path / "repo"
    repo.mkdir()
    dep = _pinned_repo(repo)
    monkeypatch.setattr("deptool.__main__._find_dep", lambda root, name, ingest=True: dep)
    monkeypatch.setattr("deptool.__main__._resolve_companions", lambda d, to: [])
    monkeypatch.setattr(apply_mod, "verify", lambda root, build_dir="build": [
        {"cmd": "cmake --build", "ok": True, "output": ""}
    ])

    assert main(["apply", "--root", str(repo), "--dep", "hegel",
                 "--to", "0.9.0", "--verify"]) == 0
    assert "v0.9.0" in (repo / "CMakeLists.txt").read_text()
    assert not list(repo.glob("*.deptool.bak"))


def test_missing_toolchain_is_not_a_pass(tmp_path, monkeypatch, capsys):
    """A `skipped` step establishes nothing, and must not read as a green build."""
    from deptool import apply as apply_mod

    monkeypatch.setattr(apply_mod, "verify", lambda root, build_dir="build": [
        {"cmd": "cmake", "skipped": "cmake not installed — cannot verify locally"}
    ])
    repo = tmp_path / "repo"
    repo.mkdir()
    dep = _pinned_repo(repo)
    planned = apply_plan(str(repo), dep, "0.9.0")
    checked = apply_mod.verify_plan(str(repo), planned)
    assert checked["ok"] is True          # nothing failed ...
    assert checked["established"] is False  # ... but nothing was shown either


# ------------------------------------------------- the lockfile's separate truth


def _locked_repo(tmp_path, locked_zlib="1.3", extra=""):
    """A repo where the lockfile and the manifest were resolved at different
    times — the shape finding C describes."""
    root = tmp_path / "repo"
    (root / "deps").mkdir(parents=True)
    (root / "deps" / "conanfile.txt").write_text(
        "[requires]\nzlib/1.3.1\nlibcurl/8.4.0\n\n[build_requires]\ncmake/3.28.1\n"
    )
    (root / "conan.lock").write_text(
        json.dumps(
            {
                "version": "0.5",
                "requires": [
                    f"zlib/{locked_zlib}#5c0f3a1a222eebb6bff34980bcd3e024%1705999193.7",
                    "libcurl/8.4.0#75a58bcdba79d1a39ff0226cc2955c83%1726198020.146",
                    "zstd/1.5.5#1f239731dc45147c7fc2f54bfbde73df%1715599909.17",
                ],
                "build_requires": [
                    "pkgconf/2.1.0#27f44583701117b571307cf5b5fe5605%1701537936.4",
                    "pkgconf/2.0.3#f996677e96e61e6552d85e83756c328b%1696606182.2",
                ],
            },
            indent=4,
        )
        + extra
    )
    return str(root)


def test_the_lockfile_is_read_as_a_declaration_site(tmp_path):
    from deptool import discover

    deps, files = discover.discover(_locked_repo(tmp_path))
    assert "conan.lock" in files
    curl = next(d for d in deps if d.name == "libcurl")
    kinds = {d.kind for d in curl.declarations}
    assert kinds == {"conan", "conan-lock"}
    lock = next(d for d in curl.declarations if d.kind == "conan-lock")
    assert lock.line == 5  # positions survive, though json.load discards them
    assert lock.raw_pin.endswith("%1726198020.146")  # revision kept verbatim


def test_a_stale_lock_is_a_finding_of_its_own(tmp_path):
    """Not the same finding as two manifests disagreeing: the edit is still well
    defined, so this must not read as "reconcile the manifests first"."""
    from deptool import discover

    deps, _ = discover.discover(_locked_repo(tmp_path))
    zlib = next(d for d in deps if d.name == "zlib")
    assert zlib.lock_drift() == {"1.3": ["conan.lock:4"]}
    assert not zlib.diverges()
    assert "stale or the build is not using it" in zlib.divergence_note()


def test_a_lock_that_agrees_with_the_manifest_says_nothing(tmp_path):
    from deptool import discover

    deps, _ = discover.discover(_locked_repo(tmp_path, locked_zlib="1.3.1"))
    zlib = next(d for d in deps if d.name == "zlib")
    assert zlib.lock_drift() == {}
    assert zlib.divergence_note() == ""


def test_a_lock_only_dependency_is_transitive_not_drifted(tmp_path):
    """Nothing for it to disagree with, and an empty usage profile is expected
    rather than evidence that it is unused."""
    from deptool import discover

    deps, _ = discover.discover(_locked_repo(tmp_path))
    zstd = next(d for d in deps if d.name == "zstd")
    assert zstd.lock_drift() == {}
    assert any("transitive" in n for n in zstd.notes)
    assert not any("stale" in n for n in zstd.notes)


def test_one_package_locked_twice_keeps_both(tmp_path):
    """Conan permits it — two profiles resolving one build requirement
    differently — so the parser must not assume uniqueness."""
    from deptool import discover

    deps, _ = discover.discover(_locked_repo(tmp_path))
    pkgconf = next(d for d in deps if d.name == "pkgconf")
    assert pkgconf.pin_variants() == {
        "2.0.3": ["conan.lock:10"],
        "2.1.0": ["conan.lock:9"],
    }
    assert "more than one version at once" in pkgconf.divergence_note()


def test_build_requirements_are_not_runtime_risk(tmp_path):
    from deptool import discover

    deps, _ = discover.discover(_locked_repo(tmp_path))
    assert next(d for d in deps if d.name == "pkgconf").scope == "build"
    assert next(d for d in deps if d.name == "zstd").scope == "runtime"


def test_a_conan_1_lock_is_read_too(tmp_path):
    from deptool import discover

    root = tmp_path / "v1"
    root.mkdir()
    (root / "conan.lock").write_text(
        json.dumps(
            {
                "graph_lock": {
                    "nodes": {
                        "0": {"path": "conanfile.txt", "requires": ["1"],
                              "build_requires": ["2"]},
                        "1": {"ref": "zlib/1.2.13#abc", "requires": []},
                        "2": {"ref": "ninja/1.11.1#def"},
                    }
                },
                "version": "0.4",
            },
            indent=2,
        )
    )
    deps, _ = discover.discover(str(root))
    got = {d.name: (d.version, d.scope) for d in deps}
    assert got == {"zlib": ("1.2.13", "runtime"), "ninja": ("1.11.1", "build")}


def test_apply_refuses_to_hand_edit_a_lockfile(tmp_path):
    """A lockfile line has a version and a position and is still not ours to
    edit: the version sits beside the revision it was resolved with."""
    from deptool import apply as apply_mod
    from deptool import discover

    root = _locked_repo(tmp_path)
    deps, _ = discover.discover(root)
    zstd = next(d for d in deps if d.name == "zstd")
    with pytest.raises(apply_mod.ApplyError) as exc:
        apply_mod.plan(root, zstd, "1.5.6")
    assert "report-only" in str(exc.value)


def test_a_bump_says_which_generated_file_it_left_behind(tmp_path):
    """Editing the manifest and saying nothing about the lock produces a bump
    that changes the declaration and not the build."""
    from deptool import apply as apply_mod
    from deptool import discover

    root = _locked_repo(tmp_path)
    deps, _ = discover.discover(root)
    curl = next(d for d in deps if d.name == "libcurl")
    planned = apply_mod.plan(root, curl, "8.21.0")
    assert planned["file"] == "deps/conanfile.txt"
    assert planned["regenerate"] == ["conan.lock:5"]
    assert "conan.lock" not in planned["diff"]


def test_a_lockfile_pin_never_wins_the_editable_site(tmp_path):
    from deptool import discover

    deps, _ = discover.discover(_locked_repo(tmp_path))
    zlib = next(d for d in deps if d.name == "zlib")
    assert zlib.declared_in == "deps/conanfile.txt:2"
    assert [d.is_editable() for d in zlib.declarations] == [False, True]


# ----------------------------------------------------------- ingesting a scanner


def _fake_trivy(monkeypatch, report, rc=0, calls=None):
    """Stand in for the binary. The real one cannot be used here: tmp_path lives
    under /tmp, which a snap-confined trivy cannot see at all."""
    import subprocess

    from deptool import sources

    # conftest turns the ingest off for the whole suite; these tests are the
    # ones that turn it back on, against this fake rather than a real install.
    monkeypatch.setattr(sources, "detect", lambda root: ["trivy"])

    def fake_run(cmd, **kwargs):
        if calls is not None:
            calls.append(cmd)
        code = rc(cmd) if callable(rc) else rc
        return subprocess.CompletedProcess(
            cmd, code, stdout="" if code else json.dumps(report), stderr=""
        )

    monkeypatch.setattr(sources.subprocess, "run", fake_run)


def _report(target, eco, packages):
    return {"Results": [{"Target": target, "Class": "lang-pkgs",
                         "Type": eco, "Packages": packages}]}


def test_an_ingested_ecosystem_we_cannot_parse_is_still_reported(tmp_path):
    """The whole point of the ingest: an ecosystem with no native parser is
    invisible today, and invisible is the failure this tool exists to prevent."""
    from deptool import discover

    (tmp_path / "packages.config").write_text("<packages/>\n")
    with pytest.MonkeyPatch.context() as mp:
        _fake_trivy(mp, _report("packages.config", "nuget", [
            {"Name": "Newtonsoft.Json", "Version": "13.0.3"},
        ]))
        deps, files = discover.discover(str(tmp_path))
    pkg = next(d for d in deps if d.name == "Newtonsoft.Json")
    assert pkg.version == "13.0.3"
    assert pkg.kind == "nuget"
    assert "packages.config" in files


def test_an_ingested_dependency_without_a_line_is_report_only(tmp_path):
    from deptool import apply as apply_mod
    from deptool import discover

    (tmp_path / "packages.config").write_text("<packages/>\n")
    with pytest.MonkeyPatch.context() as mp:
        _fake_trivy(mp, _report("packages.config", "nuget", [
            {"Name": "Moq", "Version": "4.7.0"},
        ]))
        deps, _ = discover.discover(str(tmp_path))
    moq = next(d for d in deps if d.name == "Moq")
    assert not any(d.is_editable() for d in moq.declarations)
    assert any("report-only" in n for n in moq.notes)
    with pytest.raises(apply_mod.ApplyError) as exc:
        apply_mod.plan(str(tmp_path), moq, "4.8.0")
    assert "report-only" in str(exc.value)


def test_an_ingested_package_merges_with_the_native_one(tmp_path):
    """Additive, not either/or: one record, the native line still the editable
    site, and the duplicate declaration collapses instead of doubling."""
    from deptool import discover

    root = _locked_repo(tmp_path)
    with pytest.MonkeyPatch.context() as mp:
        _fake_trivy(mp, _report("conan.lock", "conan", [
            {"Name": "zlib", "Version": "1.3",
             "Locations": [{"StartLine": 4, "EndLine": 4}]},
        ]))
        deps, _ = discover.discover(root)
    zlib = [d for d in deps if d.name == "zlib"]
    assert len(zlib) == 1
    assert [d.where() for d in zlib[0].declarations] == [
        "conan.lock:4", "deps/conanfile.txt:2",
    ]
    assert zlib[0].declared_in == "deps/conanfile.txt:2"


def test_an_ingested_package_does_not_merge_across_ecosystems(tmp_path):
    """A NuGet package and a C library that happen to share a name are not the
    same artefact, and merging them would invent a fact."""
    from deptool import discover

    (tmp_path / "conanfile.txt").write_text("[requires]\nzlib/1.3.1\n")
    (tmp_path / "packages.config").write_text("<packages/>\n")
    with pytest.MonkeyPatch.context() as mp:
        _fake_trivy(mp, _report("packages.config", "nuget", [
            {"Name": "zlib", "Version": "9.9.9"},
        ]))
        deps, _ = discover.discover(str(tmp_path))
    assert sorted((d.kind, d.version) for d in deps if d.name == "zlib") == [
        ("conan", "1.3.1"), ("nuget", "9.9.9"),
    ]


def test_the_fast_offline_invocation_is_tried_first(tmp_path):
    """Secret scanning found the same packages and took seven times as long, so
    it is the fallback for a trivy whose database has never been downloaded."""
    from deptool import sources

    calls = []
    with pytest.MonkeyPatch.context() as mp:
        _fake_trivy(mp, _report("requirements.txt", "pip", []), calls=calls)
        sources.ingest(str(tmp_path))
    assert "--skip-db-update" in calls[0] and "vuln" in calls[0]
    assert len(calls) == 1  # the fallback is not run when the first attempt works


def test_a_trivy_with_no_database_falls_back_instead_of_downloading_one(tmp_path):
    from deptool import sources

    calls = []
    with pytest.MonkeyPatch.context() as mp:
        _fake_trivy(
            mp, _report("requirements.txt", "pip",
                        [{"Name": "requests", "Version": "2.31.0"}]),
            rc=lambda cmd: 1 if "--skip-db-update" in cmd else 0,
            calls=calls,
        )
        deps, _ = sources.ingest(str(tmp_path))
    assert [d.name for d in deps] == ["requests"]
    assert "--skip-db-update" not in calls[1] and "secret" in calls[1]


def test_a_broken_scanner_is_silence_not_an_error(tmp_path):
    """It has to work with nothing installed, so a scanner that fails is a
    source that contributed nothing — never a failed run."""
    from deptool import discover, sources

    (tmp_path / "conanfile.txt").write_text("[requires]\nzlib/1.3.1\n")
    with pytest.MonkeyPatch.context() as mp:
        _fake_trivy(mp, {}, rc=2)
        assert sources.ingest(str(tmp_path)) == ([], [])
        deps, _ = discover.discover(str(tmp_path))
    assert [d.name for d in deps] == ["zlib"]


def test_os_packages_are_not_this_projects_dependencies(tmp_path):
    """They describe the machine trivy ran on, not anything declared here."""
    from deptool import sources

    with pytest.MonkeyPatch.context() as mp:
        _fake_trivy(mp, {"Results": [
            {"Target": "OS Packages", "Class": "os-pkgs", "Type": "debian",
             "Packages": [{"Name": "libc6", "Version": "2.39"}]},
        ]})
        assert sources.ingest(str(tmp_path)) == ([], [])


def test_no_ingest_uses_only_the_native_parsers(tmp_path):
    from deptool import discover

    (tmp_path / "packages.config").write_text("<packages/>\n")
    with pytest.MonkeyPatch.context() as mp:
        _fake_trivy(mp, _report("packages.config", "nuget", [
            {"Name": "Moq", "Version": "4.7.0"},
        ]))
        assert discover.discover(str(tmp_path), ingest=False) == ([], [])


def test_a_lockfile_stays_uneditable_across_the_profile_file(tmp_path):
    """`_find_dep` reads dependencies back out of CLAUDE_DEPS.md, so the fact
    that a declaration is generated has to survive the round trip — otherwise a
    reload is all it takes for `apply` to start editing a lockfile."""
    from deptool import apply as apply_mod
    from deptool import discover, profile

    root = _locked_repo(tmp_path)
    deps, _ = discover.discover(root)
    profile.save(root, deps, {"repo": "r", "generated": "2026-01-01"})
    loaded, _ = profile.load(root)

    zstd = next(d for d in loaded if d.name == "zstd")
    assert [d.kind for d in zstd.declarations] == ["conan-lock"]
    assert not any(d.is_editable() for d in zstd.declarations)
    with pytest.raises(apply_mod.ApplyError):
        apply_mod.plan(root, zstd, "1.5.6")

    curl = next(d for d in loaded if d.name == "libcurl")
    assert apply_mod.plan(root, curl, "8.21.0")["regenerate"] == ["conan.lock:5"]


def test_an_unpinned_system_dependency_still_gets_its_own_message(tmp_path):
    """It has no editable declaration either, but "report-only" would be the
    wrong advice: the fix is the CI image, not a hand edit."""
    from deptool import apply as apply_mod
    from deptool.model import Dep

    (tmp_path / "CMakeLists.txt").write_text("find_package(ALSA REQUIRED)\n")
    dep = Dep(name="ALSA", kind="pkg-config", declared_in="CMakeLists.txt:1")
    with pytest.raises(apply_mod.ApplyError) as exc:
        apply_mod.plan(str(tmp_path), dep, "1.2.11")
    assert "system dependency" in str(exc.value)
