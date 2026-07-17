"""Shared scaffolding for the pytest-impact test suite.

Builds the same multi-conftest fixture tree used in the project's validated
proof-of-concept (root conftest with session fixtures + an autouse fixture +
a ``pytest_collection_modifyitems`` hook; ``tests/conftest.py`` with a
two-level fixture chain; ``tests/sub/conftest.py`` that *overrides* one of
those fixtures and adds one of its own; a fixture-less test file) directly
into a ``pytester`` temp project, git-initializes it, and provides a helper
to invoke ``--impact`` and read back its JSON report.
"""
from __future__ import annotations

import json
import textwrap
from pathlib import Path
from typing import Iterable, Optional

#: nodeid for every test in the scaffolded sample project (see
#: ``build_sample_project``), keyed by short test name for readable asserts.
NODEIDS = {
    "test_config_defaults": "tests/test_config.py::test_config_defaults",
    "test_math": "tests/test_pure.py::test_math",
    "test_create_user": "tests/test_users.py::test_create_user",
    "test_list_users": "tests/test_users.py::test_list_users",
    "test_admin_action": "tests/sub/test_admin.py::test_admin_action",
    "test_sub_config": "tests/sub/test_admin.py::test_sub_config",
}

ALL_TESTS = frozenset(NODEIDS.values())

_ROOT_CONFTEST = '''\
import pytest


@pytest.fixture(scope="session")
def app_config():
    return {"debug": False, "retries": 3}


@pytest.fixture(scope="session")
def db_engine():
    return "engine://memory"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("APP_ENV", "test")


def pytest_collection_modifyitems(session, config, items):
    # A collection-altering hook: any change here should conservatively
    # rerun the whole subtree it governs (here, the entire suite).
    return
'''

_TESTS_CONFTEST = '''\
import pytest


@pytest.fixture
def db_session(db_engine):
    return {"engine": db_engine, "tx": "open"}


@pytest.fixture
def user(db_session):
    return {"name": "regular", "session": db_session}
'''

_TEST_CONFIG = '''\
def test_config_defaults(app_config):
    assert app_config["retries"] == 3
'''

_TEST_PURE = '''\
def test_math():
    assert 2 + 2 == 4
'''

_TEST_USERS = '''\
def test_create_user(user):
    assert user["name"] == "regular"


def test_list_users(db_session):
    assert db_session["tx"] == "open"
'''

_SUB_CONFTEST = '''\
import pytest


@pytest.fixture
def user(db_session):
    # Overrides tests/conftest.py::user for everything under tests/sub/.
    return {"name": "sub-regular", "session": db_session}


@pytest.fixture
def admin_user(user):
    return {"name": "admin", "base": user}
'''

_TEST_ADMIN = '''\
def test_admin_action(admin_user):
    assert admin_user["base"]["name"] == "sub-regular"


def test_sub_config(app_config):
    assert app_config["debug"] is False
'''

_REQUIREMENTS = "pytest>=7\n"

_SAMPLE_FILES = {
    "conftest.py": _ROOT_CONFTEST,
    "tests/conftest.py": _TESTS_CONFTEST,
    "tests/test_config.py": _TEST_CONFIG,
    "tests/test_pure.py": _TEST_PURE,
    "tests/test_users.py": _TEST_USERS,
    "tests/sub/conftest.py": _SUB_CONFTEST,
    "tests/sub/test_admin.py": _TEST_ADMIN,
    "requirements.txt": _REQUIREMENTS,
}


def write_file(pytester, relpath: str, content: str) -> Path:
    path = pytester.path / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(content), encoding="utf-8")
    return path


def patch_file(pytester, relpath: str, old: str, new: str, count: int = 1) -> None:
    path = pytester.path / relpath
    text = path.read_text(encoding="utf-8")
    assert old in text, f"pattern {old!r} not found in {relpath}"
    path.write_text(text.replace(old, new, count), encoding="utf-8")


def git(pytester, *args: str):
    result = pytester.run("git", *args)
    assert result.ret == 0, f"git {' '.join(args)} failed:\n{result.stderr.str()}"
    return result


def git_init(pytester) -> None:
    """Initialize a git repo at ``pytester.path`` with a deterministic
    committer identity (CI runners have no global git config)."""
    git(pytester, "init", "-q")
    git(pytester, "config", "user.email", "pytest-impact-tests@example.com")
    git(pytester, "config", "user.name", "pytest-impact tests")
    git(pytester, "config", "commit.gpgsign", "false")


def git_commit_all(pytester, message: str = "commit") -> None:
    git(pytester, "add", "-A")
    git(pytester, "commit", "-q", "-m", message)


def build_sample_project(pytester, commit: bool = True) -> None:
    """Write the validated PoC's 6-test, 3-conftest fixture tree directly at
    ``pytester.path`` and (by default) git-init + commit it as the baseline
    that later mutations are diffed against."""
    for relpath, content in _SAMPLE_FILES.items():
        write_file(pytester, relpath, content)
    if commit:
        git_init(pytester)
        git_commit_all(pytester, "initial sample project")


def run_impact(
    pytester,
    *extra_args: str,
    impact_base: Optional[str] = None,
    no_merge_base: bool = False,
    report_name: str = "impact-report.json",
):
    """Run ``pytest --impact --collect-only`` (as a subprocess, so entry-point
    plugin discovery behaves like a real install) and return
    ``(result, report_dict_or_None)``."""
    report_path = pytester.path / report_name
    args = [
        "--impact",
        f"--impact-explain-json={report_path}",
        "--impact-explain",
        "--collect-only",
        "-q",
    ]
    if impact_base is not None:
        args.append(f"--impact-base={impact_base}")
    if no_merge_base:
        args.append("--impact-no-merge-base")
    args.extend(extra_args)

    result = pytester.runpytest_subprocess(*args)
    report = json.loads(report_path.read_text(encoding="utf-8")) if report_path.exists() else None
    return result, report


def selected_nodeids(report: dict) -> set:
    return {entry["nodeid"] for entry in report["selected"]}


def deselected_nodeids(report: dict) -> set:
    return set(report["deselected"])


def reasons_for(report: dict, nodeid: str) -> Iterable[str]:
    for entry in report["selected"]:
        if entry["nodeid"] == nodeid:
            return entry["reasons"]
    return []
