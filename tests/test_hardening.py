"""Hardening scenarios beyond the original 8 PoC cases: dynamic
``request.getfixturevalue`` resolution, non-hook module-dirty conftest
subtree selection, merge-base PR semantics, external/builtin fixtures, the
various fail-open triggers, and the config/dependency fallback list
(including its ini override).
"""
from __future__ import annotations

import helpers


# --------------------------------------------------------------------------- #
# Dynamic request.getfixturevalue(...)
# --------------------------------------------------------------------------- #
def test_getfixturevalue_literal_selects_only_the_named_fixture(pytester):
    helpers.write_file(
        pytester,
        "conftest.py",
        '''\
        import pytest


        @pytest.fixture
        def fixture_a():
            return "a-v1"


        @pytest.fixture
        def fixture_b():
            return "b-v1"
        ''',
    )
    helpers.write_file(
        pytester,
        "test_literal.py",
        '''\
        def test_uses_a_via_literal(request):
            value = request.getfixturevalue("fixture_a")
            assert value == "a-v1"


        def test_uses_b_directly(fixture_b):
            assert fixture_b == "b-v1"
        ''',
    )
    helpers.git_init(pytester)
    helpers.git_commit_all(pytester, "initial")

    helpers.patch_file(pytester, "conftest.py", '"a-v1"', '"a-v2"')

    result, report = helpers.run_impact(pytester)

    assert helpers.selected_nodeids(report) == {"test_literal.py::test_uses_a_via_literal"}
    assert helpers.deselected_nodeids(report) == {"test_literal.py::test_uses_b_directly"}


def test_getfixturevalue_dynamic_arg_is_conservative(pytester):
    """A non-literal ``getfixturevalue`` argument can't be resolved
    statically, so the item must be treated as depending on *every* fixture
    visible from its node -- including ones it never mentions by name."""
    helpers.write_file(
        pytester,
        "conftest.py",
        '''\
        import pytest


        @pytest.fixture
        def fixture_a():
            return "a-v1"


        @pytest.fixture
        def fixture_b():
            return "b-v1"


        @pytest.fixture
        def fixture_c():
            return "c-v1"
        ''',
    )
    helpers.write_file(
        pytester,
        "test_dynamic.py",
        '''\
        FIXTURE_NAME = "fixture_c"


        def test_dynamic_uses_request(request):
            value = request.getfixturevalue(FIXTURE_NAME)
            assert value == "c-v1"


        def test_control():
            assert 1 + 1 == 2
        ''',
    )
    helpers.git_init(pytester)
    helpers.git_commit_all(pytester, "initial")

    # fixture_c is never referenced by a literal string anywhere, so only the
    # "conservative: depends on everything in scope" path can catch this.
    helpers.patch_file(pytester, "conftest.py", '"c-v1"', '"c-v2"')

    result, report = helpers.run_impact(pytester)

    assert helpers.selected_nodeids(report) == {"test_dynamic.py::test_dynamic_uses_request"}
    assert helpers.deselected_nodeids(report) == {"test_dynamic.py::test_control"}
    reasons = helpers.reasons_for(report, "test_dynamic.py::test_dynamic_uses_request")
    assert any("dynamic" in r for r in reasons)


# --------------------------------------------------------------------------- #
# Non-hook module-dirty conftest -> subtree selection
# --------------------------------------------------------------------------- #
def test_module_dirty_nonhook_conftest_selects_only_its_subtree(pytester):
    helpers.write_file(
        pytester,
        "subdir_a/conftest.py",
        '''\
        import pytest


        @pytest.fixture
        def thing_a():
            return "thing-a"
        ''',
    )
    helpers.write_file(
        pytester,
        "subdir_a/test_a.py",
        '''\
        def test_uses_thing_a(thing_a):
            assert thing_a == "thing-a"
        ''',
    )
    helpers.write_file(
        pytester,
        "subdir_b/test_b.py",
        '''\
        def test_b_unrelated():
            assert True
        ''',
    )
    helpers.git_init(pytester)
    helpers.git_commit_all(pytester, "initial")

    # A module-level addition -- no def/class touched, so no hook and no
    # fixture symbol changes, only module_dirty=True for this one conftest.
    helpers.patch_file(
        pytester,
        "subdir_a/conftest.py",
        'def thing_a():\n    return "thing-a"\n',
        'def thing_a():\n    return "thing-a"\n\n\nEXTRA_CONSTANT = 2\n',
    )

    result, report = helpers.run_impact(pytester)

    assert helpers.selected_nodeids(report) == {"subdir_a/test_a.py::test_uses_thing_a"}
    assert helpers.deselected_nodeids(report) == {"subdir_b/test_b.py::test_b_unrelated"}
    reasons = helpers.reasons_for(report, "subdir_a/test_a.py::test_uses_thing_a")
    assert any("module-level change" in r and "subtree" in r for r in reasons)


# --------------------------------------------------------------------------- #
# Merge-base PR-mode correctness
# --------------------------------------------------------------------------- #
def _build_merge_base_project(pytester):
    helpers.write_file(
        pytester,
        "conftest.py",
        '''\
        import pytest


        @pytest.fixture
        def fixture_a():
            return "a-v1"


        @pytest.fixture
        def fixture_b():
            return "b-v1"
        ''',
    )
    helpers.write_file(
        pytester,
        "test_a.py",
        '''\
        def test_a(fixture_a):
            assert fixture_a == "a-v1"
        ''',
    )
    helpers.write_file(
        pytester,
        "test_b.py",
        '''\
        def test_b(fixture_b):
            assert fixture_b == "b-v1"
        ''',
    )
    helpers.git(pytester, "init", "-q")
    helpers.git(pytester, "checkout", "-q", "-b", "main")
    helpers.git(pytester, "config", "user.email", "pytest-impact-tests@example.com")
    helpers.git(pytester, "config", "user.name", "pytest-impact tests")
    helpers.git(pytester, "config", "commit.gpgsign", "false")
    helpers.git_commit_all(pytester, "initial")

    helpers.git(pytester, "checkout", "-q", "-b", "feature")
    helpers.patch_file(pytester, "conftest.py", '"a-v1"', '"a-v2"')
    helpers.git_commit_all(pytester, "feature: change fixture_a")

    helpers.git(pytester, "checkout", "-q", "main")
    helpers.patch_file(pytester, "conftest.py", '"b-v1"', '"b-v2"')
    helpers.git_commit_all(pytester, "main: unrelated change to fixture_b")

    helpers.git(pytester, "checkout", "-q", "feature")


def test_merge_base_mode_excludes_upstream_only_changes(pytester):
    """From ``feature`` (which only touched fixture_a), diffing against
    ``main`` in merge-base mode must NOT see main's independent, later change
    to fixture_b -- only the merge-base(main, feature) ancestor is used as
    the diff floor, so test_b stays deselected."""
    _build_merge_base_project(pytester)

    result, report = helpers.run_impact(pytester, impact_base="main")

    assert report["merge_base_used"] is True
    assert report["resolved_base"] != report["effective_base"]
    assert helpers.selected_nodeids(report) == {"test_a.py::test_a"}
    assert helpers.deselected_nodeids(report) == {"test_b.py::test_b"}


def test_no_merge_base_flag_sees_upstream_changes_too(pytester):
    """The ``--impact-no-merge-base`` escape hatch diffs directly against the
    literal ref, so main's independent fixture_b change now shows up too
    (this is the naive/incorrect behavior for PR mode, kept only as an
    explicit opt-out)."""
    _build_merge_base_project(pytester)

    result, report = helpers.run_impact(pytester, impact_base="main", no_merge_base=True)

    assert report["merge_base_used"] is False
    assert report["resolved_base"] == report["effective_base"]
    assert helpers.selected_nodeids(report) == {"test_a.py::test_a", "test_b.py::test_b"}
    assert helpers.deselected_nodeids(report) == set()


# --------------------------------------------------------------------------- #
# External / builtin fixtures safely ignored
# --------------------------------------------------------------------------- #
def test_external_builtin_fixtures_are_safely_ignored(pytester):
    helpers.write_file(
        pytester,
        "conftest.py",
        '''\
        import pytest


        @pytest.fixture
        def fixture_a():
            return "a-v1"
        ''',
    )
    helpers.write_file(
        pytester,
        "test_builtins.py",
        '''\
        def test_uses_tmp_path(tmp_path):
            assert tmp_path.exists()


        def test_uses_monkeypatch(monkeypatch):
            monkeypatch.setenv("X", "1")


        def test_uses_fixture_a(fixture_a):
            assert fixture_a == "a-v1"
        ''',
    )
    helpers.git_init(pytester)
    helpers.git_commit_all(pytester, "initial")

    helpers.patch_file(pytester, "conftest.py", '"a-v1"', '"a-v2"')

    result, report = helpers.run_impact(pytester)

    # No crash/fail-open from encountering tmp_path/monkeypatch (defined
    # outside the repo) in other items' fixture closures...
    assert report["fail_open"] is None
    # ...and they are correctly excluded from selection, since only
    # fixture_a actually changed.
    assert helpers.selected_nodeids(report) == {"test_builtins.py::test_uses_fixture_a"}
    assert helpers.deselected_nodeids(report) == {
        "test_builtins.py::test_uses_tmp_path",
        "test_builtins.py::test_uses_monkeypatch",
    }


# --------------------------------------------------------------------------- #
# Fail-open
# --------------------------------------------------------------------------- #
def test_fail_open_when_not_a_git_repository(pytester):
    helpers.write_file(pytester, "test_a.py", "def test_a():\n    assert True\n")
    # Deliberately no git init.

    result, report = helpers.run_impact(pytester)

    assert report["fail_open"] is not None
    assert "git repository" in report["fail_open"]
    assert helpers.selected_nodeids(report) == {"test_a.py::test_a"}
    assert helpers.deselected_nodeids(report) == set()


def test_fail_open_on_invalid_base_ref(pytester):
    helpers.write_file(pytester, "test_a.py", "def test_a():\n    assert True\n")
    helpers.git_init(pytester)
    helpers.git_commit_all(pytester, "initial")

    result, report = helpers.run_impact(pytester, impact_base="refs/heads/does-not-exist")

    assert report["fail_open"] is not None
    assert "does-not-exist" in report["fail_open"]
    assert helpers.selected_nodeids(report) == {"test_a.py::test_a"}
    assert helpers.deselected_nodeids(report) == set()


def test_fail_open_on_unparseable_file(pytester):
    helpers.write_file(pytester, "test_a.py", "def test_a():\n    assert True\n")
    helpers.write_file(pytester, "test_b.py", "def test_b():\n    assert True\n")
    helpers.git_init(pytester)
    helpers.git_commit_all(pytester, "initial")

    # A non-test .py file (pytest's default collection patterns never try to
    # import it, so collection itself is unaffected) with invalid syntax --
    # our own AST diffing can't safely parse it, so the *entire* run must
    # fail open rather than silently under-selecting around it.
    helpers.write_file(pytester, "broken_module.py", "def broken(:\n    not python\n")

    result, report = helpers.run_impact(pytester)

    assert result.ret == 0
    assert report["fail_open"] is not None
    assert "unparseable" in report["fail_open"]
    assert "broken_module.py" in report["fail_open"]
    # Both tests -- even the untouched one -- run, since fail-open means the
    # whole diff is untrustworthy, not just the broken file.
    assert helpers.selected_nodeids(report) == {"test_a.py::test_a", "test_b.py::test_b"}
    assert helpers.deselected_nodeids(report) == set()


# --------------------------------------------------------------------------- #
# Config/dependency fallback list (+ ini override)
# --------------------------------------------------------------------------- #
def test_builtin_fallback_pattern_pyproject_toml(pytester):
    helpers.write_file(pytester, "test_a.py", "def test_a():\n    assert True\n")
    helpers.write_file(pytester, "pyproject.toml", "[tool.example]\nvalue = 1\n")
    helpers.git_init(pytester)
    helpers.git_commit_all(pytester, "initial")

    helpers.patch_file(pytester, "pyproject.toml", "value = 1\n", "value = 2\n")

    result, report = helpers.run_impact(pytester)

    assert report["fallback"] is True
    assert "pyproject.toml" in report["fallback_files"]
    assert helpers.selected_nodeids(report) == {"test_a.py::test_a"}


def test_custom_fallback_pattern_via_ini_option(pytester):
    helpers.write_file(
        pytester,
        "pytest.ini",
        "[pytest]\nimpact_fallback_files = custom_data/*.json\n",
    )
    helpers.write_file(pytester, "test_a.py", "def test_a():\n    assert True\n")
    helpers.git_init(pytester)
    helpers.git_commit_all(pytester, "initial")

    # A new file matching only the custom (ini-configured) pattern -- not
    # one of the built-in defaults.
    helpers.write_file(pytester, "custom_data/settings.json", '{"k": "v"}\n')

    result, report = helpers.run_impact(pytester)

    assert report["fallback"] is True
    assert any("settings.json" in f for f in report["fallback_files"])
    assert helpers.selected_nodeids(report) == {"test_a.py::test_a"}
