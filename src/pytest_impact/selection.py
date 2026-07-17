"""Turn a git diff into a per-item selection decision.

This module has no pytest-hook code in it (that lives in ``plugin.py``) --
just the pure logic: build a :class:`DiffModel` from git + AST, then compute
each collected item's "dependency surface" against that model.

Dependency surface, per item:

1. The test function itself -- its own qualname changed, or its module is
   dirty (some top-level, non-def/class statement changed: an import, a
   module-level constant, ...).
2. Every fixture in the item's fixture closure (``item._fixtureinfo.
   name2fixturedefs``), using the *winning* definition per name (``[-1]``,
   i.e. the most specific override) so an overridden fixture in a deeper
   conftest correctly shadows a same-named fixture above it.
3. Fixtures reached only through a dynamic ``request.getfixturevalue(...)``
   call (not visible in pytest's own static fixture closure): a literal
   string argument resolves to one specific fixture (via
   ``FixtureManager.getfixturedefs``, override-aware); anything else is
   treated as "depends on every fixture in scope from this node" so we never
   silently miss a dynamic dependency.
4. Every ancestor ``conftest.py`` -- a changed ``pytest_*`` hook or *any*
   module-level change in a conftest conservatively selects the whole
   subtree it governs (hooks can alter collection/execution in ways no
   static analysis can fully characterize).

If anything makes the diff itself untrustworthy (not a git repo, an invalid
``--impact-base``, a git command failing, or an unparseable changed file) we
fail open: select everything and surface a warning. The same is true, by
design, for changes to config/dependency files (``pyproject.toml``,
``requirements*.txt``, lockfiles, ...): those can change *runtime behavior*
in ways no fixture graph captures, so they always trigger a full run.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from . import astdiff, gitio
from ._compat import all_fixture_names, get_fixturedefs


class PytestImpactWarning(UserWarning):
    """Raised (via ``warnings.warn``) whenever pytest-impact fails open."""


#: Filenames/fnmatch patterns whose change always triggers a full-suite run,
#: because they can change runtime behavior in ways the fixture graph can't
#: capture. Extend via the ``impact_fallback_files`` ini option.
DEFAULT_FALLBACK_PATTERNS: Tuple[str, ...] = (
    "pyproject.toml",
    "setup.cfg",
    "setup.py",
    "tox.ini",
    "pytest.ini",
    "requirements*.txt",
    "requirements*.in",
    "poetry.lock",
    "Pipfile",
    "Pipfile.lock",
    "uv.lock",
)


@dataclass(frozen=True)
class FileDiff:
    """What changed inside a single (changed) Python file."""

    changed_symbols: Set[str]
    module_dirty: bool


@dataclass
class DiffModel:
    """Everything known about the current git diff."""

    base_ref: str
    files: Dict[str, FileDiff] = field(default_factory=dict)
    fallback: bool = False
    fallback_files: List[str] = field(default_factory=list)
    fail_open: Optional[str] = None
    resolved_base: Optional[str] = None
    effective_base: Optional[str] = None
    merge_base_used: bool = False
    notes: List[str] = field(default_factory=list)


@dataclass
class ItemDecision:
    nodeid: str
    selected: bool
    reasons: List[str]


@dataclass
class ImpactResult:
    model: DiffModel
    decisions: List[ItemDecision]
    selected_items: List[Any]
    deselected_items: List[Any]

    @property
    def fail_open(self) -> Optional[str]:
        return self.model.fail_open

    @property
    def fallback(self) -> bool:
        return self.model.fallback


# --------------------------------------------------------------------------- #
# Diff model construction
# --------------------------------------------------------------------------- #
def build_diff_model(
    rootdir: Path,
    base_ref: str,
    use_merge_base: bool = True,
    extra_fallback_patterns: Sequence[str] = (),
) -> DiffModel:
    model = DiffModel(base_ref=base_ref)

    resolved = gitio.resolve_commit(rootdir, base_ref)
    if resolved is None:
        model.fail_open = f"--impact-base={base_ref!r} does not resolve to a commit"
        return model
    model.resolved_base = resolved

    effective = resolved
    if use_merge_base:
        mb = gitio.merge_base(rootdir, resolved, "HEAD")
        if mb is not None:
            effective = mb
            model.merge_base_used = True
        else:
            model.notes.append(
                f"merge-base({base_ref}, HEAD) unavailable; diffing directly "
                f"against {base_ref} instead"
            )
    model.effective_base = effective

    changed = gitio.changed_paths(rootdir, effective)
    if changed is None:
        model.fail_open = f"`git diff {effective}` failed"
        return model

    fallback_patterns = tuple(DEFAULT_FALLBACK_PATTERNS) + tuple(extra_fallback_patterns)
    unparseable: List[str] = []
    for rel in sorted(changed):
        if gitio.matches_any(rel, fallback_patterns):
            model.fallback = True
            model.fallback_files.append(rel)
        if not rel.endswith(".py"):
            continue

        old_text = gitio.show_file_at_ref(rootdir, effective, rel)
        new_text = _read_worktree_file(rootdir, rel)
        old_syms = astdiff.extract_symbols(old_text if old_text is not None else "")
        new_syms = astdiff.extract_symbols(new_text if new_text is not None else "")
        if not old_syms.parse_ok or not new_syms.parse_ok:
            unparseable.append(rel)
            continue

        changed_syms, module_dirty = astdiff.diff_symbols(old_syms, new_syms)
        model.files[rel] = FileDiff(changed_symbols=changed_syms, module_dirty=module_dirty)

    if unparseable:
        model.fail_open = "unparseable file(s), can't safely diff: " + ", ".join(
            sorted(unparseable)
        )
        return model

    return model


def _read_worktree_file(rootdir: Path, relpath: str) -> Optional[str]:
    path = rootdir / relpath
    if not path.is_file():
        return None
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


# --------------------------------------------------------------------------- #
# Per-item dependency surface
# --------------------------------------------------------------------------- #
def _func_location(func: Any, rootdir: Path) -> Optional[Tuple[str, str]]:
    """(relpath, qualname) for a live function, or None if it has no code
    object or lives outside the repo (external/site-packages fixtures are
    intentionally, safely ignored -- they can never appear in ``model.files``
    since git only reports paths inside the repository anyway, but we still
    guard explicitly so a cross-drive/relpath quirk can never misattribute
    one file to another)."""
    code = getattr(func, "__code__", None)
    if code is None:
        return None
    rel = gitio.relative_to_root(rootdir, code.co_filename)
    if rel is None:
        return None
    qualname = getattr(func, "__qualname__", None) or getattr(func, "__name__", "")
    return rel, qualname


def _symbol_reason(rel: str, qualname: str, model: DiffModel, label: str) -> Optional[str]:
    diff = model.files.get(rel)
    if diff is None:
        return None
    if qualname in diff.changed_symbols:
        return f"{label} changed in {rel}"
    if diff.module_dirty:
        return f"module-level change in {rel} (defines {label})"
    return None


def _fixturedef_reason(name: str, fd: Any, model: DiffModel, rootdir: Path, via: Optional[str]) -> Optional[str]:
    func = getattr(fd, "func", None)
    if func is None:
        return None
    loc = _func_location(func, rootdir)
    if loc is None:
        return None  # external/builtin fixture -> safely ignored
    rel, qualname = loc
    label = f"fixture {name!r}" if via is None else f"fixture {name!r} (via {via})"
    return _symbol_reason(rel, qualname, model, label)


def _conftest_subtree_reasons(item_rel: str, model: DiffModel) -> List[str]:
    reasons: List[str] = []
    for rel, diff in model.files.items():
        if os.path.basename(rel) != "conftest.py":
            continue
        confdir = os.path.dirname(rel)
        # A root-level conftest.py has confdir == "" and governs the whole
        # repo, so it must match every item -- not just ones with paths
        # literally starting with "/".
        is_ancestor = confdir == "" or item_rel == rel or item_rel.startswith(confdir + "/")
        if not is_ancestor:
            continue
        if any(sym.startswith("pytest_") for sym in diff.changed_symbols):
            reasons.append(f"hook changed in {rel} (subtree)")
        elif diff.module_dirty:
            reasons.append(f"module-level change in {rel} (subtree)")
    return reasons


def _dynamic_fixture_reasons(
    item: Any, func: Any, owner_label: str, model: DiffModel, rootdir: Path, fixturemanager: Any
) -> List[str]:
    """Reasons contributed by ``request.getfixturevalue(...)`` calls inside
    ``func`` (a test or a fixture already in the item's closure)."""
    if func is None or fixturemanager is None:
        return []
    refs = astdiff.scan_dynamic_fixture_refs(func)
    if not refs.literal_names and not refs.dynamic:
        return []

    reasons: List[str] = []
    names = set(refs.literal_names)
    via = owner_label
    if refs.dynamic:
        # Never miss: a non-literal/unresolvable argument means we can't know
        # which fixture is actually requested at runtime, so conservatively
        # depend on every fixture visible from this node.
        names |= set(all_fixture_names(fixturemanager))
        via = f"{owner_label}, dynamic request.getfixturevalue(...)"

    for name in names:
        fd_seq = get_fixturedefs(fixturemanager, name, item)
        if not fd_seq:
            continue
        reason = _fixturedef_reason(name, fd_seq[-1], model, rootdir, via=via)
        if reason:
            reasons.append(reason)
    return reasons


def _item_reasons(item: Any, model: DiffModel, rootdir: Path) -> List[str]:
    reasons: List[str] = []
    func = getattr(item, "function", None)

    loc = _func_location(func, rootdir) if func is not None else None
    if loc is not None:
        rel, qualname = loc
        reason = _symbol_reason(rel, qualname, model, f"test {qualname!r}")
        if reason:
            reasons.append(reason)

    fixtureinfo = getattr(item, "_fixtureinfo", None)
    name2defs: Dict[str, Sequence[Any]] = (
        getattr(fixtureinfo, "name2fixturedefs", {}) or {} if fixtureinfo is not None else {}
    )
    for name, defs in name2defs.items():
        if not defs:
            continue
        reason = _fixturedef_reason(name, defs[-1], model, rootdir, via=None)
        if reason:
            reasons.append(reason)

    fixturemanager = getattr(getattr(item, "session", None), "_fixturemanager", None)
    scan_targets: List[Tuple[str, Any]] = []
    if func is not None:
        scan_targets.append((getattr(func, "__qualname__", "test"), func))
    for name, defs in name2defs.items():
        if defs:
            scan_targets.append((f"fixture {name!r}", defs[-1].func))
    for owner_label, target_func in scan_targets:
        reasons.extend(
            _dynamic_fixture_reasons(item, target_func, owner_label, model, rootdir, fixturemanager)
        )

    if loc is not None:
        reasons.extend(_conftest_subtree_reasons(loc[0], model))

    return sorted(set(reasons))


# --------------------------------------------------------------------------- #
# Top-level entry point
# --------------------------------------------------------------------------- #
def _resolve_rootdir(config: Any) -> Optional[Path]:
    start = Path(str(getattr(config, "rootpath", config.rootdir)))
    return gitio.repo_root(start)


def _all_selected(model: DiffModel, items: Sequence[Any], reason: str) -> ImpactResult:
    decisions = [ItemDecision(nodeid=it.nodeid, selected=True, reasons=[reason]) for it in items]
    return ImpactResult(
        model=model, decisions=decisions, selected_items=list(items), deselected_items=[]
    )


def analyze(session: Any, config: Any, items: Sequence[Any]) -> ImpactResult:
    base_ref = config.getoption("impact_base")
    use_merge_base = not config.getoption("impact_no_merge_base")
    extra_patterns = config.getini("impact_fallback_files")

    rootdir = _resolve_rootdir(config)
    if rootdir is None:
        model = DiffModel(base_ref=base_ref, fail_open="not inside a git repository")
        return _all_selected(model, items, model.fail_open)

    model = build_diff_model(rootdir, base_ref, use_merge_base, extra_patterns)
    if model.fail_open:
        return _all_selected(model, items, model.fail_open)

    if model.fallback:
        reason = "config/deps changed -> full run (" + ", ".join(sorted(model.fallback_files)) + ")"
        return _all_selected(model, items, reason)

    decisions: List[ItemDecision] = []
    selected_items: List[Any] = []
    deselected_items: List[Any] = []
    for item in items:
        reasons = _item_reasons(item, model, rootdir)
        if reasons:
            decisions.append(ItemDecision(item.nodeid, True, reasons))
            selected_items.append(item)
        else:
            decisions.append(ItemDecision(item.nodeid, False, []))
            deselected_items.append(item)

    return ImpactResult(
        model=model,
        decisions=decisions,
        selected_items=selected_items,
        deselected_items=deselected_items,
    )
