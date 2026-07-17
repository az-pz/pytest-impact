# pytest-impact

Fixture- and conftest-aware **test impact analysis** for pytest: given a git
diff, `pytest-impact` selects (and deselects the rest of) only the tests
actually affected by changes to test files, `conftest.py`, fixtures, and
hooks.

There is no coverage tracing and no persisted database. All state is a
single git ref to diff against. It works cold, on the very first CI run, from
nothing but the diff -- and it is *override-aware*: it always resolves the
winning `FixtureDef` for a given fixture name at a given test, so an override
in a deeper `conftest.py` is never confused with the fixture it shadows.

## Why not just X?

`pytest-impact`'s one job is understanding pytest's own **fixture and
conftest dependency graph** statically, from the diff alone. It intentionally
does *not* try to be a general-purpose "what changed" tool -- there are
already good ones, and they solve a different problem:

| | **pytest-impact** | [pytest-testmon] | [pytest-picked] | [pytest-impacted] | [snob] |
|---|---|---|---|---|---|
| Unit of change tracking | fixture / conftest / hook / test **symbols** (AST) | **line-level coverage** of arbitrary source | changed/new **files** | git diff (file/coverage oriented) | static **import graph** (transitive imports) |
| Understands fixture overrides | **Yes** -- resolves the winning `FixtureDef` per test | N/A (traces real execution, so overrides are implicitly correct) | No (file-level only) | No | No |
| Tracks arbitrary application code | **No** (by design -- see Limitations) | **Yes** (its core strength) | No | Partial | **Yes**, via import edges (its core strength) |
| Needs a persisted DB / prior run | No | Yes (`.testmondata`) | No | No | No |
| Works cold on first CI run | **Yes** | No (needs a baseline run) | Yes | Yes | Yes |
| Detects decorator-only changes (e.g. `scope=`, `parametrize`) | **Yes** | Yes (via re-execution) | No | No | No (no import edge to follow) |
| Coverage instrumentation required | **No** | Yes | No | No | No |
| conftest.py / fixture changes | precise, per fixture (fixture graph) | precise (via coverage) | file-level only | not addressed | **whole-suite fallback** -- no import edge exists for injected fixtures, so `conftest.py` changes just run everything |

[pytest-testmon]: https://pypi.org/project/pytest-testmon/
[pytest-picked]: https://pypi.org/project/pytest-picked/
[pytest-impacted]: https://pypi.org/project/pytest-impacted/
[snob]: https://github.com/alexpasmantier/snob

In short: **pytest-testmon** owns "did any source code this test actually
executed change?" (via real coverage) -- the right tool for arbitrary
application-code changes. **pytest-picked** is file-level and fixture-blind:
if you touch a shared `conftest.py`, it can only tell you *that file*
changed, not *which tests* are actually affected by *which* fixture in it.
**pytest-impacted** is diff/coverage oriented and doesn't reason about the
fixture graph either. **snob** selects from the static *import* graph ("this
changed file is transitively imported by these test files") and is fast and
effective for source-level changes, but test modules never `import conftest`
-- fixtures and hooks are injected by name at collection time, not imported
-- so snob has no edge to follow there and falls back to running the entire
suite on any `conftest.py`/config change. `pytest-impact` fills the specific
gap of test-infrastructure churn -- shared fixtures, conftest hierarchies,
collection hooks -- where a one-line change three `conftest.py` files up can
silently invalidate a handful of specific tests, and file-level, import-graph,
or coverage-based tools either over-select (whole file/whole subtree) or
can't reason about it at all without first executing it once.

Use `pytest-impact` and `pytest-testmon` together for the best of both: run
`pytest-impact` to catch fixture/conftest/hook churn from a clean diff, and
`pytest-testmon` for line-level coverage on everything else.

### Composing with import-graph tools

`pytest-impact` and import-graph selectors like `snob` cover different, mostly
non-overlapping change axes -- app/source-code imports vs. the
fixture/conftest graph -- so they compose well as a union rather than a
choice. A simple CI recipe: run both selectors against the same diff and
execute the union of their selected tests, e.g.

```bash
# snob: source/app-code import impact (prints selected test ids)
snob $(git diff --name-only origin/main...HEAD) > snob_selected.txt

# pytest-impact: fixture/conftest/hook impact
pytest --impact --impact-base=origin/main --impact-explain-json=impact.json

# union snob_selected.txt with the "selected" entries in impact.json, then run that set
```

To be clear about the boundary: `pytest-impact` deliberately does **not**
track arbitrary application/source-code changes (see Limitations below) --
that's exactly what `snob`'s import graph and `pytest-testmon`'s coverage
tracing are for.

## Install

```bash
pip install pytest-impact
```

Registers automatically as a `pytest11` plugin -- no `-p pytest_impact` or
`conftest.py` wiring needed. With no flags passed, it is a complete no-op:
existing behavior is unchanged.

## Usage

### Locally

Diff against `HEAD` (i.e. your uncommitted working-tree changes) and run
only the affected tests:

```bash
pytest --impact
```

Diff against a specific ref (e.g. before pulling, or against a stable
branch):

```bash
pytest --impact --impact-base=main
```

Add `--impact-explain` for a human-readable reason per test:

```bash
pytest --impact --impact-explain
```

```text
-- pytest-impact --
selected 1, deselected 5 (base=HEAD, effective=b00de47e...)
SELECTED   tests/test_users.py::test_create_user
    - fixture 'user' changed in tests/conftest.py
DESELECTED tests/sub/test_admin.py::test_admin_action
DESELECTED tests/sub/test_admin.py::test_sub_config
DESELECTED tests/test_config.py::test_config_defaults
DESELECTED tests/test_pure.py::test_math
DESELECTED tests/test_users.py::test_list_users
```

Add `--impact-explain-json=report.json` for a machine-readable version (CI
artifact, dashboarding, etc.):

```json
{
  "impact_base": "HEAD",
  "resolved_base": "b00de47e...",
  "effective_base": "b00de47e...",
  "merge_base_used": true,
  "fail_open": null,
  "fallback": false,
  "fallback_files": [],
  "notes": [],
  "summary": { "total": 6, "selected": 1, "deselected": 5 },
  "selected": [
    { "nodeid": "tests/test_users.py::test_create_user",
      "reasons": ["fixture 'user' changed in tests/conftest.py"] }
  ],
  "deselected": ["tests/sub/test_admin.py::test_admin_action", "..."]
}
```

### CI / pull requests (merge-base mode)

For a PR build, you want "what did *this branch* change", not "what differs
from `origin/main`'s current tip right now" (which would also pick up
unrelated commits that landed on the base branch after you branched).
`pytest-impact` handles this by default: the effective diff base is always
`merge-base(--impact-base, HEAD)`, so:

```bash
git fetch origin main
pytest --impact --impact-base=origin/main --impact-explain-json=impact-report.json
```

behaves like `git diff origin/main...HEAD` (three-dot/merge-base semantics),
not `git diff origin/main` (two-dot). Only changes introduced by the PR
branch itself count. If you ever need the literal two-dot diff instead, pass
`--impact-no-merge-base`.

A minimal GitHub Actions job:

```yaml
- run: git fetch origin ${{ github.base_ref }}
- run: >
    pytest --impact --impact-base=origin/${{ github.base_ref }}
    --impact-explain --impact-explain-json=impact-report.json
- uses: actions/upload-artifact@v4
  with:
    name: impact-report
    path: impact-report.json
```

### CLI options

| Option | Default | Meaning |
|---|---|---|
| `--impact` | off | Enable selection. No effect at all unless passed. |
| `--impact-base=REF` | `HEAD` | Git ref to diff against. |
| `--impact-no-merge-base` | off | Diff directly against `--impact-base` instead of `merge-base(base, HEAD)`. |
| `--impact-explain` | off | Print a reason for every selected/deselected test. |
| `--impact-explain-json=PATH` | none | Write the JSON report described above to `PATH`. |
| `impact_fallback_files` (ini) | see below | Extra fnmatch patterns that trigger a full-suite fallback run when changed. |

## How selection works

For each collected test, `pytest-impact` builds its **dependency surface**
and selects it if any part of that surface intersects the changed symbols:

1. **The test itself** -- its own file+qualname changed, or its module has a
   dirty top-level (an import or module-level statement changed).
2. **Every fixture in its closure** (`item._fixtureinfo.name2fixturedefs`),
   using the *winning* definition per name -- so an override in a nested
   `conftest.py` is what's checked, not the fixture it shadows.
3. **Fixtures reached only via `request.getfixturevalue(...)`** -- a literal
   string argument resolves to that one fixture; anything dynamic (a
   variable, an f-string, a computed value) conservatively adds *every*
   fixture visible from that test as a dependency, so a dynamic lookup can
   never be silently missed.
4. **Every ancestor `conftest.py`** -- a changed `pytest_*` hook, or any
   module-level change in a conftest, conservatively selects the whole
   subtree it governs (hooks can alter collection/execution in ways no
   static analysis can fully characterize).

A function/method's fingerprint hashes its **decorators + signature + body**
together, so a decorator-only edit -- `@pytest.fixture(scope=...)`,
`@pytest.mark.parametrize(...)` -- is detected even when the body is
byte-for-byte identical.

If a changed `.py` file can't be parsed, or the diff itself is untrustworthy
(no git repo, an unresolvable `--impact-base`, a failing `git` command), the
whole run **fails open**: every test is selected and a warning is printed.
`pytest-impact` never silently under-selects.

Certain files always trigger a full run regardless of the fixture graph,
because they can change runtime behavior no static analysis captures:
`pyproject.toml`, `setup.cfg`, `setup.py`, `tox.ini`, `pytest.ini`,
`requirements*.txt`/`.in`, `poetry.lock`, `Pipfile`/`Pipfile.lock`,
`uv.lock`. Extend this list per-project via the `impact_fallback_files` ini
option (in `pytest.ini`/`pyproject.toml`/`setup.cfg`):

```ini
[pytest]
impact_fallback_files =
    data/*.json
    migrations/*.sql
```

## Limitations (honest)

- **It does not track arbitrary application/source-code changes.** If your
  test imports and calls a plain function in `myapp/utils.py` with no
  fixture or conftest involved, `pytest-impact` will not know that test is
  affected when that function changes -- that's exactly what
  [pytest-testmon]'s dynamic coverage tracing is for. `pytest-impact`'s
  scope is deliberately the fixture/conftest/hook graph, not your whole
  codebase.
- **Dynamic fixture resolution is conservative, not omniscient.** A
  non-literal `request.getfixturevalue(expr)` can't be resolved statically,
  so it's treated as "depends on everything in scope" -- correct (never
  under-selects) but can over-select in pathological cases.
- **Non-Python inputs aren't tracked.** Data files, environment variables,
  external services, and anything else a test depends on outside of Python
  source and the fixture graph are invisible to this analysis.
- **Conftest hook changes select the whole governed subtree**, not just the
  tests that "really" depend on that specific hook behavior -- hooks can
  alter collection or execution in ways too varied to characterize
  statically, so this is deliberately conservative rather than risk a
  missed test.

## License

MIT
