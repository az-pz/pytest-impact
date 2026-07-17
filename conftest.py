"""Enables the ``pytester`` fixture for the whole test suite.

Must live at the repository root (not nested under ``tests/``) so it is
always loaded regardless of ``testpaths``, per pytest's own recommendation
for using ``pytester`` to test pytest plugins.
"""
pytest_plugins = ["pytester"]
