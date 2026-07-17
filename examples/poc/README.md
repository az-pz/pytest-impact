# Reference proof-of-concept

This directory is a verbatim copy of the original, hand-validated
proof-of-concept that `pytest-impact` was designed from. It is kept **for
reference only**:

- `pytest_impact.py` -- the single-file prototype of the core algorithm
  (git diff -> AST symbol fingerprints -> dependency-surface selection).
- `sample_project/` -- the multi-conftest fixture tree used to validate the
  8 scenarios reproduced as automated tests in `tests/test_scenarios.py`.
- `run_poc.py` -- the scenario driver used to validate the prototype by hand.

The real, packaged implementation lives in `src/pytest_impact/` and is a
clean reimplementation (not an import) of the ideas prototyped here -- see
the package docstrings and `README.md` at the repository root for details on
what changed and why (root-relative path resolution via the actual git
toplevel, merge-base PR semantics, dynamic `getfixturevalue` handling, and a
proper test suite, among other hardening).

This example is not installed, imported, or executed by the package or its
test suite.
