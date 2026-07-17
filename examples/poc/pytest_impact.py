"""
Proof-of-concept: fixture-graph-aware, def-level test impact selection.

No coverage tracing, no persisted DB. State = a git ref to diff against.
Selection happens at collection time using pytest's own fixture graph
(item._fixtureinfo.name2fixturedefs -> FixtureDef.func) intersected with
AST-level "changed symbols" computed from the git diff.
"""
import ast
import hashlib
import os
import subprocess

import pytest

CONFIG_FALLBACK_FILES = {
    "pyproject.toml", "setup.cfg", "setup.py", "tox.ini", "pytest.ini",
    "poetry.lock", "Pipfile.lock",
}


def _sha(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8", "replace")).hexdigest()[:12]


def _run_git(rootdir, *args):
    try:
        out = subprocess.run(
            ["git", *args], cwd=rootdir, capture_output=True, text=True, check=False
        )
        return out.stdout
    except Exception:
        return ""


def _git_show(rootdir, base, relpath):
    out = subprocess.run(
        ["git", "show", f"{base}:{relpath}"],
        cwd=rootdir, capture_output=True, text=True, check=False,
    )
    return out.stdout if out.returncode == 0 else ""


def _extract_symbols(source):
    """Return (symbols: {qualname: fingerprint}, module_fingerprint).

    A function/method fingerprint hashes its decorators + signature + body, so
    a change to @pytest.fixture(scope=...) or @parametrize flips it even with an
    unchanged body. module_fingerprint hashes all top-level non-def/class code
    (imports, module-level statements) -> "module dirty" detection.
    """
    symbols = {}
    module_bits = []
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return {}, _sha(source)  # unparseable -> treat whole file as dirty

    def seg(node):
        s = ast.get_source_segment(source, node)
        return s if s is not None else ""

    def visit(body, prefix):
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                qn = prefix + node.name
                deco = "\n".join(seg(d) for d in node.decorator_list)
                symbols[qn] = _sha(deco + "|" + seg(node))
            elif isinstance(node, ast.ClassDef):
                qn = prefix + node.name
                deco = "\n".join(seg(d) for d in node.decorator_list)
                symbols[qn] = _sha(deco + "|class " + node.name)
                visit(node.body, qn + ".")
            else:
                module_bits.append(seg(node))

    visit(tree.body, "")
    return symbols, _sha("\n".join(module_bits))


def _changed_files(rootdir, base):
    """All paths changed between `base` and the working tree (tracked + new)."""
    files = set()
    for line in _run_git(rootdir, "diff", "--name-only", base).splitlines():
        if line.strip():
            files.add(line.strip())
    for line in _run_git(
        rootdir, "ls-files", "--others", "--exclude-standard"
    ).splitlines():
        if line.strip():
            files.add(line.strip())
    return files


def _read_disk(rootdir, relpath):
    p = os.path.join(rootdir, relpath)
    if not os.path.isfile(p):
        return ""
    with open(p, "r", encoding="utf-8", errors="replace") as fh:
        return fh.read()


def build_diff_model(rootdir, base):
    """relpath -> (changed_symbols:set[str], module_dirty:bool) for changed .py;
    plus a flag if any config/dependency file changed (full-run fallback)."""
    changed = _changed_files(rootdir, base)
    model = {}
    fallback = False
    for rel in changed:
        name = os.path.basename(rel)
        if name in CONFIG_FALLBACK_FILES or name.startswith("requirements"):
            fallback = True
        if not rel.endswith(".py"):
            continue
        old = _git_show(rootdir, base, rel)
        new = _read_disk(rootdir, rel)
        old_syms, old_mod = _extract_symbols(old) if old else ({}, _sha(""))
        new_syms, new_mod = _extract_symbols(new) if new else ({}, _sha(""))
        changed_syms = {
            q for q in (set(old_syms) | set(new_syms))
            if old_syms.get(q) != new_syms.get(q)
        }
        model[rel] = (changed_syms, old_mod != new_mod)
    return model, fallback


def _relof(abspath, rootdir):
    try:
        return os.path.relpath(abspath, rootdir).replace("\\", "/")
    except ValueError:
        return None


# --------------------------------------------------------------------------- #
# pytest plugin hooks
# --------------------------------------------------------------------------- #
def pytest_addoption(parser):
    group = parser.getgroup("impact")
    group.addoption("--impact", action="store_true", help="Select only impacted tests.")
    group.addoption(
        "--impact-base", default="HEAD",
        help="Git ref to diff the working tree against (default: HEAD).",
    )
    group.addoption(
        "--impact-explain", action="store_true",
        help="Emit machine-readable selection reasons.",
    )


def pytest_collection_modifyitems(session, config, items):
    if not config.getoption("--impact"):
        return
    rootdir = str(config.rootdir)
    base = config.getoption("--impact-base")
    model, fallback = build_diff_model(rootdir, base)

    explanations = {}

    if fallback:
        for it in items:
            explanations[it.nodeid] = ["config/deps changed -> full run"]
        config._impact_explain = explanations
        config._impact_deselected = 0
        return

    selected, deselected = [], []
    for item in items:
        reasons = []
        func = getattr(item, "function", None)

        # 1) the test function itself
        if func is not None:
            trel = _relof(func.__code__.co_filename, rootdir)
            if trel in model:
                changed_syms, mdirty = model[trel]
                if func.__qualname__ in changed_syms:
                    reasons.append(f"test '{func.__qualname__}' changed")
                elif mdirty:
                    reasons.append(f"module-level change in {trel}")

        # 2) fixtures in the closure (winning def per name -> override aware)
        fi = getattr(item, "_fixtureinfo", None)
        name2defs = getattr(fi, "name2fixturedefs", {}) if fi else {}
        for name, defs in name2defs.items():
            if not defs:
                continue
            fd = defs[-1]  # most-specific (winning) definition
            ffunc = getattr(fd, "func", None)
            if ffunc is None:
                continue
            frel = _relof(ffunc.__code__.co_filename, rootdir)
            if frel is None or frel not in model:
                continue
            changed_syms, mdirty = model[frel]
            if ffunc.__qualname__ in changed_syms:
                reasons.append(f"fixture '{name}' changed at {frel}")
            elif mdirty:
                reasons.append(f"module-level change in {frel} (defines '{name}')")

        # 3) conftest hooks / module-level -> conservative subtree selection
        titem_rel = _relof(func.__code__.co_filename, rootdir) if func else None
        if titem_rel:
            for rel, (changed_syms, mdirty) in model.items():
                if os.path.basename(rel) != "conftest.py":
                    continue
                confdir = os.path.dirname(rel)
                if not (titem_rel == rel or titem_rel.startswith(confdir + "/")):
                    continue
                if any(q.startswith("pytest_") for q in changed_syms):
                    reasons.append(f"hook changed in {rel} (subtree)")
                elif mdirty:
                    reasons.append(f"conftest module-level change in {rel} (subtree)")

        if reasons:
            explanations[item.nodeid] = sorted(set(reasons))
            selected.append(item)
        else:
            deselected.append(item)

    if deselected:
        config.hook.pytest_deselected(items=deselected)
        items[:] = selected

    config._impact_explain = explanations
    config._impact_deselected = len(deselected)


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    if not config.getoption("--impact"):
        return
    explanations = getattr(config, "_impact_explain", {})
    tw = terminalreporter
    tw.write_line("")
    for nodeid in sorted(explanations):
        tw.write_line(f"IMPACT-SELECTED {nodeid} :: {' | '.join(explanations[nodeid])}")
    tw.write_line(f"IMPACT-DESELECTED-COUNT {getattr(config, '_impact_deselected', 0)}")
