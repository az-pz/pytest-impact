"""pytest-impact: fixture- and conftest-aware test impact analysis for pytest.

Given a git diff, select (and deselect the rest) only the tests affected by
changes to test files, conftest.py, fixtures, and hooks. No coverage tracing,
no persisted database -- state is just a git ref to diff against.
"""

__version__ = "0.1.0"

__all__ = ["__version__"]
