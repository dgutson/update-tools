"""Tests for the deterministic layer.

These cover the places where a wrong answer would be silent and damaging:
scope misclassification (a test-only dep reported as production risk), and the
version-swap regex (a bad edit corrupts CMakeLists.txt).
"""

import os
import sys
import textwrap

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from deptool import cmake, profile
from deptool.apply import _declaration_span, _swap_version
from deptool.fingerprint import hash_declaration, hash_files, hash_text
from deptool.model import Dep, Site, Upstream, bump_kind, most_restrictive, parse_version


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
    assert "HEGEL_LIBHEGEL_VERSION=0.29.0" in pin
    assert "libhegel version required" in pin  # CACHE docstring, not FORCE
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
