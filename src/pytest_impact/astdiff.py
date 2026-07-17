"""AST-level symbol fingerprinting and dynamic-fixture-reference scanning.

Two independent jobs live here:

1. ``extract_symbols`` / ``diff_symbols`` -- turn a Python source string into a
   ``{qualname: fingerprint}`` map plus a "module fingerprint" for everything
   that is *not* a top-level def/class (imports, module-level statements).
   A function/method fingerprint hashes its decorators + signature + body, so
   a decorator-only edit (``@pytest.fixture(scope=...)``, ``@pytest.mark.
   parametrize(...)``) is detected even when the body is byte-for-byte
   identical. Comparing two ``FileSymbols`` (old vs. new blob) yields the set
   of changed qualnames plus a module-dirty flag.

2. ``scan_dynamic_fixture_refs`` -- given a live function object (a test or a
   fixture), find calls shaped like ``request.getfixturevalue("name")``
   inside its body. A literal string argument resolves to a specific fixture
   name; anything else (a variable, an f-string, a computed expression, no
   resolvable argument at all) is reported as "dynamic" so the caller can
   conservatively treat the item as depending on every fixture in scope.
"""
from __future__ import annotations

import ast
import hashlib
import inspect
import textwrap
from dataclasses import dataclass
from typing import Callable, Dict, FrozenSet, Set, Tuple


def sha1_12(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()[:12]


@dataclass(frozen=True)
class FileSymbols:
    """Extracted symbol table for a single Python source blob."""

    symbols: Dict[str, str]
    module_fingerprint: str
    parse_ok: bool


def extract_symbols(source: str) -> FileSymbols:
    """Parse ``source`` into a qualname->fingerprint map and a module-level
    fingerprint. On a syntax error, returns ``parse_ok=False`` with no
    symbols; callers use this to trigger a conservative fail-open rather than
    silently under-selecting on an unparseable file."""
    module_bits = []
    symbols: Dict[str, str] = {}
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return FileSymbols(symbols={}, module_fingerprint=sha1_12(source), parse_ok=False)

    def seg(node: ast.AST) -> str:
        s = ast.get_source_segment(source, node)
        return s if s is not None else ""

    def visit(body, prefix: str) -> None:
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                qn = prefix + node.name
                deco = "\n".join(seg(d) for d in node.decorator_list)
                symbols[qn] = sha1_12(deco + "|" + seg(node))
            elif isinstance(node, ast.ClassDef):
                qn = prefix + node.name
                deco = "\n".join(seg(d) for d in node.decorator_list)
                # The class fingerprint covers its own header (decorators,
                # bases, keywords) -- nested defs are visited (and fingerprinted
                # individually) below so a method-body-only edit doesn't have
                # to touch the class's own hash to be detected.
                header = f"class {node.name}({', '.join(seg(b) for b in node.bases)})"
                symbols[qn] = sha1_12(deco + "|" + header)
                visit(node.body, qn + ".")
            else:
                module_bits.append(seg(node))

    visit(tree.body, "")
    return FileSymbols(
        symbols=symbols, module_fingerprint=sha1_12("\n".join(module_bits)), parse_ok=True
    )


def diff_symbols(old: FileSymbols, new: FileSymbols) -> Tuple[Set[str], bool]:
    """Return (changed qualnames, module_dirty) between two ``FileSymbols``."""
    changed = {
        q
        for q in (set(old.symbols) | set(new.symbols))
        if old.symbols.get(q) != new.symbols.get(q)
    }
    module_dirty = old.module_fingerprint != new.module_fingerprint
    return changed, module_dirty


# --------------------------------------------------------------------------- #
# Dynamic `request.getfixturevalue(...)` scanning
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class DynamicRefs:
    """Result of scanning one function body for ``getfixturevalue`` calls."""

    literal_names: FrozenSet[str] = frozenset()
    dynamic: bool = False


_NO_REFS = DynamicRefs()
_ARGNAME_KEYWORD = "argname"


def _scan_source_for_refs(source: str) -> DynamicRefs:
    try:
        tree = ast.parse(textwrap.dedent(source))
    except (SyntaxError, ValueError):
        # Can't prove there's no dynamic call in there -- be conservative.
        return DynamicRefs(dynamic=True)

    literal_names = set()
    dynamic = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr == "getfixturevalue"):
            continue

        arg_node = None
        if node.args:
            arg_node = node.args[0]
        else:
            for kw in node.keywords:
                if kw.arg == _ARGNAME_KEYWORD:
                    arg_node = kw.value
                    break

        if isinstance(arg_node, ast.Constant) and isinstance(arg_node.value, str):
            literal_names.add(arg_node.value)
        else:
            # No resolvable argument, or a variable/f-string/expression --
            # never miss: fall back to "depends on everything in scope".
            dynamic = True

    if not literal_names and not dynamic:
        return _NO_REFS
    return DynamicRefs(literal_names=frozenset(literal_names), dynamic=dynamic)


#: Keyed by function object identity: parametrized tests share one underlying
#: function across many items, and fixtures are frequently shared across many
#: consumers, so avoid re-parsing the same source repeatedly in one process.
_DYNAMIC_REFS_CACHE: Dict[Callable, DynamicRefs] = {}


def scan_dynamic_fixture_refs(func: Callable) -> DynamicRefs:
    """Scan ``func``'s current source for ``<obj>.getfixturevalue(...)``
    calls. Best-effort: if the source can't be retrieved (e.g. a
    dynamically-generated callable), returns "no dynamic refs" rather than
    raising -- this only *adds* dependency edges on top of the normal
    declared-fixture analysis, it never removes any."""
    real_func = inspect.unwrap(func)
    cached = _DYNAMIC_REFS_CACHE.get(real_func)
    if cached is not None:
        return cached
    try:
        source = inspect.getsource(real_func)
    except (OSError, TypeError):
        result = _NO_REFS
    else:
        result = _scan_source_for_refs(source)
    try:
        _DYNAMIC_REFS_CACHE[real_func] = result
    except TypeError:
        pass  # unhashable func (shouldn't happen for real functions/methods)
    return result
