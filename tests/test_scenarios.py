"""Automated reproductions of the 8 validated PoC scenarios.

Each test scaffolds the shared sample project (see ``helpers.py``), commits
it as a git baseline, applies exactly one mutation, runs
``pytest --impact --collect-only``, and asserts the exact selected/deselected
node-id sets via the JSON report.
"""
from __future__ import annotations

import helpers


def test_i_no_change_selects_nothing(pytester):
    helpers.build_sample_project(pytester)

    result, report = helpers.run_impact(pytester)

    assert result.ret == 5  # "no tests ran" (all deselected)
    assert helpers.selected_nodeids(report) == set()
    assert helpers.deselected_nodeids(report) == helpers.ALL_TESTS
    assert report["fail_open"] is None
    assert report["fallback"] is False


def test_ii_edit_user_fixture_is_override_aware(pytester):
    """Editing tests/conftest.py::user must select only test_create_user --
    NOT test_admin_action, whose ``admin_user`` binds tests/sub/conftest.py's
    *override* of ``user``, not this one."""
    helpers.build_sample_project(pytester)
    helpers.patch_file(
        pytester,
        "tests/conftest.py",
        'return {"name": "regular", "session": db_session}',
        'return {"name": "regular_v2", "session": db_session}',
    )

    result, report = helpers.run_impact(pytester)

    assert helpers.selected_nodeids(report) == {helpers.NODEIDS["test_create_user"]}
    assert helpers.deselected_nodeids(report) == helpers.ALL_TESTS - {
        helpers.NODEIDS["test_create_user"]
    }


def test_iii_edit_db_session_propagates_through_override(pytester):
    """``db_session`` is not overridden in tests/sub/, so a change to it must
    reach test_admin_action too (via admin_user -> user(override) ->
    db_session), in addition to the two tests/test_users.py tests."""
    helpers.build_sample_project(pytester)
    helpers.patch_file(
        pytester,
        "tests/conftest.py",
        'return {"engine": db_engine, "tx": "open"}',
        'return {"engine": db_engine, "tx": "open_v2"}',
    )

    result, report = helpers.run_impact(pytester)

    expected = {
        helpers.NODEIDS["test_list_users"],
        helpers.NODEIDS["test_create_user"],
        helpers.NODEIDS["test_admin_action"],
    }
    assert helpers.selected_nodeids(report) == expected
    assert helpers.deselected_nodeids(report) == helpers.ALL_TESTS - expected


def test_iv_root_conftest_hook_change_selects_all(pytester):
    """A changed ``pytest_*`` hook in the root conftest.py conservatively
    selects its entire subtree -- which, for a root-level conftest, is
    everything."""
    helpers.build_sample_project(pytester)
    helpers.patch_file(
        pytester,
        "conftest.py",
        "    return\n",
        "    items.sort(key=lambda i: i.nodeid)\n    return\n",
    )

    result, report = helpers.run_impact(pytester)

    assert helpers.selected_nodeids(report) == helpers.ALL_TESTS
    assert helpers.deselected_nodeids(report) == set()
    for entry in report["selected"]:
        assert any("hook changed" in r for r in entry["reasons"])


def test_v_edit_one_test_body_selects_only_that_test(pytester):
    helpers.build_sample_project(pytester)
    helpers.patch_file(
        pytester,
        "tests/test_pure.py",
        "    assert 2 + 2 == 4\n",
        "    assert 2 + 2 == 5\n",
    )

    result, report = helpers.run_impact(pytester)

    assert helpers.selected_nodeids(report) == {helpers.NODEIDS["test_math"]}
    assert helpers.deselected_nodeids(report) == helpers.ALL_TESTS - {
        helpers.NODEIDS["test_math"]
    }


def test_vi_decorator_only_scope_change_is_detected(pytester):
    """Changing only ``db_engine``'s ``scope=`` kwarg (identical body) must
    still be treated as a fixture change, selecting every test that depends
    on ``db_engine`` (directly or transitively) -- but not app_config/pure
    tests, which don't."""
    helpers.build_sample_project(pytester)
    helpers.patch_file(
        pytester,
        "conftest.py",
        '@pytest.fixture(scope="session")\ndef db_engine():',
        '@pytest.fixture(scope="function")\ndef db_engine():',
    )

    result, report = helpers.run_impact(pytester)

    expected = {
        helpers.NODEIDS["test_create_user"],
        helpers.NODEIDS["test_list_users"],
        helpers.NODEIDS["test_admin_action"],
    }
    assert helpers.selected_nodeids(report) == expected
    assert helpers.deselected_nodeids(report) == helpers.ALL_TESTS - expected


def test_vii_new_test_file_selects_only_the_new_test(pytester):
    helpers.build_sample_project(pytester)
    helpers.write_file(
        pytester,
        "tests/test_new.py",
        "def test_new_thing():\n    assert True\n",
    )

    result, report = helpers.run_impact(pytester)

    new_nodeid = "tests/test_new.py::test_new_thing"
    assert helpers.selected_nodeids(report) == {new_nodeid}
    assert helpers.deselected_nodeids(report) == helpers.ALL_TESTS


def test_viii_requirements_change_falls_back_to_full_run(pytester):
    helpers.build_sample_project(pytester)
    helpers.patch_file(
        pytester,
        "requirements.txt",
        "pytest>=7\n",
        "pytest>=7\nrequests>=2\n",
    )

    result, report = helpers.run_impact(pytester)

    assert report["fallback"] is True
    assert "requirements.txt" in report["fallback_files"]
    assert helpers.selected_nodeids(report) == helpers.ALL_TESTS
    assert helpers.deselected_nodeids(report) == set()
