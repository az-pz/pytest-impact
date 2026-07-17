"""pytest hook implementations: CLI options, collection-time selection, and
terminal/JSON reporting. Registered via the ``pytest11`` entry point, so
``pytest --impact`` works with no ``-p`` needed.

Everything here is strictly opt-in: with no ``--impact`` flag, every hook in
this module is a no-op and pytest behaves exactly as if the plugin were not
installed.
"""
from __future__ import annotations

import json
import warnings
from pathlib import Path
from typing import Any, Dict, Optional

import pytest

from . import selection

#: Stashes the computed :class:`~pytest_impact.selection.ImpactResult` on
#: ``config`` so ``pytest_terminal_summary`` (a separate hook call) can report
#: on exactly what ``pytest_collection_modifyitems`` decided.
RESULT_KEY: "pytest.StashKey[selection.ImpactResult]" = pytest.StashKey()


def pytest_addoption(parser: "pytest.Parser") -> None:
    group = parser.getgroup("impact", "fixture- and conftest-aware test impact analysis")
    group.addoption(
        "--impact",
        action="store_true",
        default=False,
        help="Select only tests impacted by the current git diff (deselect the rest). "
        "No effect unless passed.",
    )
    group.addoption(
        "--impact-base",
        action="store",
        default="HEAD",
        metavar="REF",
        help="Git ref to diff against (default: HEAD). The effective diff base is "
        "merge-base(REF, HEAD) unless --impact-no-merge-base is given, so e.g. "
        "--impact-base=origin/main behaves like `git diff origin/main...HEAD` and "
        "only changes introduced by the current branch count -- this is what you "
        "want for PR/CI runs.",
    )
    group.addoption(
        "--impact-no-merge-base",
        action="store_true",
        default=False,
        help="Diff directly against --impact-base instead of merge-base(base, HEAD). "
        "Rarely needed; disables the PR-safe merge-base semantics.",
    )
    group.addoption(
        "--impact-explain",
        action="store_true",
        default=False,
        help="Print a human-readable reason for each selected/deselected test.",
    )
    group.addoption(
        "--impact-explain-json",
        action="store",
        default=None,
        metavar="PATH",
        help="Write a machine-readable JSON selection report to PATH (for CI artifacts).",
    )
    parser.addini(
        "impact_fallback_files",
        type="linelist",
        default=[],
        help="Extra fnmatch-style filename patterns (e.g. 'requirements*.txt') that "
        "trigger a full-suite fallback run when changed, on top of the built-in "
        "defaults (pyproject.toml, setup.cfg, setup.py, tox.ini, pytest.ini, "
        "requirements*.txt/in, poetry.lock, Pipfile[.lock], uv.lock).",
    )


def pytest_report_header(config: "pytest.Config") -> Optional[str]:
    if not config.getoption("impact"):
        return None
    mode = "literal-ref" if config.getoption("impact_no_merge_base") else "merge-base"
    return f"pytest-impact: enabled (base={config.getoption('impact_base')}, mode={mode})"


def pytest_collection_modifyitems(session, config, items) -> None:
    if not config.getoption("impact"):
        return

    result = selection.analyze(session, config, items)
    config.stash[RESULT_KEY] = result

    if result.fail_open:
        warnings.warn(
            f"pytest-impact: failing open ({result.fail_open}) -- running the full suite.",
            selection.PytestImpactWarning,
            stacklevel=1,
        )
    elif not result.fallback and result.deselected_items:
        config.hook.pytest_deselected(items=result.deselected_items)
        items[:] = result.selected_items

    json_path = config.getoption("impact_explain_json")
    if json_path:
        _write_json_report(json_path, result)


def _write_json_report(path: str, result: "selection.ImpactResult") -> None:
    model = result.model
    payload: Dict[str, Any] = {
        "impact_base": model.base_ref,
        "resolved_base": model.resolved_base,
        "effective_base": model.effective_base,
        "merge_base_used": model.merge_base_used,
        "fail_open": result.fail_open,
        "fallback": result.fallback,
        "fallback_files": model.fallback_files,
        "notes": model.notes,
        "summary": {
            "total": len(result.decisions),
            "selected": len(result.selected_items),
            "deselected": len(result.deselected_items),
        },
        "selected": [
            {"nodeid": d.nodeid, "reasons": d.reasons} for d in result.decisions if d.selected
        ],
        "deselected": [d.nodeid for d in result.decisions if not d.selected],
    }
    out_path = Path(path)
    if out_path.parent != Path("."):
        out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def pytest_terminal_summary(terminalreporter, exitstatus, config) -> None:
    if not config.getoption("impact"):
        return
    result = config.stash.get(RESULT_KEY, None)
    if result is None:
        return

    tw = terminalreporter
    tw.write_line("")
    tw.write_line("-- pytest-impact --", bold=True)

    if result.fail_open:
        tw.write_line(f"FAIL-OPEN: {result.fail_open} -- ran the full suite.", red=True)
        return

    if result.fallback:
        tw.write_line(
            "FALLBACK: config/dependency file(s) changed -> ran the full suite "
            f"({', '.join(result.model.fallback_files)}).",
            yellow=True,
        )
        return

    model = result.model
    tw.write_line(
        f"selected {len(result.selected_items)}, deselected {len(result.deselected_items)} "
        f"(base={model.base_ref}, effective={model.effective_base})"
    )
    for note in model.notes:
        tw.write_line(f"note: {note}")

    if config.getoption("impact_explain"):
        for decision in sorted(result.decisions, key=lambda d: d.nodeid):
            if decision.selected:
                tw.write_line(f"SELECTED   {decision.nodeid}")
                for reason in decision.reasons:
                    tw.write_line(f"    - {reason}")
        for decision in sorted(result.decisions, key=lambda d: d.nodeid):
            if not decision.selected:
                tw.write_line(f"DESELECTED {decision.nodeid}")
