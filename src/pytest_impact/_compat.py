"""Small compatibility shims for internal pytest APIs that differ across the
pytest versions this plugin supports (pytest >= 7).

Everything here touches ``_pytest`` private internals (there is no public API
for "which FixtureDef wins for this fixture name at this node") -- so we keep
the surface area small and defensive, and always degrade to "nothing found"
rather than raising.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, Optional, Sequence

import pytest

if TYPE_CHECKING:
    from _pytest.fixtures import FixtureDef, FixtureManager
    from _pytest.nodes import Item

#: pytest 8 changed ``FixtureManager.getfixturedefs`` from taking a ``nodeid``
#: string to taking the actual requesting ``Node``. Decide once, up front.
_WANTS_NODE = pytest.version_tuple >= (8, 0)


def get_fixturedefs(
    fixturemanager: "FixtureManager", argname: str, item: "Item"
) -> Optional[Sequence["FixtureDef[Any]"]]:
    """Version-tolerant wrapper for ``FixtureManager.getfixturedefs``.

    Returns the sequence of ``FixtureDef`` applicable to ``item`` for
    ``argname`` (in override order, so ``result[-1]`` is the winning
    definition), or ``None``/empty if there is no such fixture visible from
    that node. Never raises: any incompatibility is treated as "not found".
    """
    node_arg: Any = item if _WANTS_NODE else item.nodeid
    try:
        return fixturemanager.getfixturedefs(argname, node_arg)
    except Exception:
        pass
    # Defend against a future/older signature change we didn't anticipate:
    # retry with the other calling convention before giving up.
    alt_arg: Any = item.nodeid if node_arg is item else item
    try:
        return fixturemanager.getfixturedefs(argname, alt_arg)
    except Exception:
        return None


def all_fixture_names(fixturemanager: "FixtureManager") -> Sequence[str]:
    """All fixture names the fixture manager knows about (any scope, any
    location) -- used to conservatively resolve dynamic
    ``request.getfixturevalue(<non-literal>)`` calls to "all in-scope
    fixtures" for a given item."""
    try:
        return list(fixturemanager._arg2fixturedefs.keys())
    except Exception:
        return []
