"""Git subprocess helpers used by pytest-impact.

Every function here is defensive: git is invoked with ``check=False`` and any
``OSError`` (e.g. the ``git`` executable not being on ``PATH``) is swallowed.
Callers use the ``None`` / falsy return values to decide whether to fail open
(select every test) -- this module never raises for "normal" git failures.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Optional, Sequence, Set


def _run(rootdir: Path, *args: str) -> Optional[subprocess.CompletedProcess]:
    try:
        return subprocess.run(
            ["git", *args],
            cwd=str(rootdir),
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        # git not installed / not on PATH.
        return None


def repo_root(start_dir: Path) -> Optional[Path]:
    """Return the top-level directory of the git repo containing ``start_dir``.

    Returns ``None`` if ``start_dir`` is not inside a git working tree (or git
    itself could not be invoked). Using git's own notion of "top level" -- and
    not pytest's ``rootdir`` -- means relative paths computed from it line up
    exactly with the paths reported by ``git diff`` / ``git show``, even when
    pytest is invoked from a subdirectory or has a different config-derived
    rootdir than the repository root (e.g. a monorepo).
    """
    res = _run(start_dir, "rev-parse", "--show-toplevel")
    if res is None or res.returncode != 0:
        return None
    out = res.stdout.strip()
    if not out:
        return None
    return Path(out)


def is_git_repo(rootdir: Path) -> bool:
    return repo_root(rootdir) is not None


def resolve_commit(rootdir: Path, ref: str) -> Optional[str]:
    """Resolve ``ref`` to a commit sha, or ``None`` if it does not resolve."""
    res = _run(rootdir, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
    if res is None or res.returncode != 0:
        return None
    out = res.stdout.strip()
    return out or None


def merge_base(rootdir: Path, ref_a: str, ref_b: str = "HEAD") -> Optional[str]:
    """Return the merge-base commit of ``ref_a`` and ``ref_b``, or ``None``."""
    res = _run(rootdir, "merge-base", ref_a, ref_b)
    if res is None or res.returncode != 0:
        return None
    out = res.stdout.strip()
    return out or None


def changed_paths(rootdir: Path, base: str) -> Optional[Set[str]]:
    """All paths changed between ``base`` and the working tree, plus untracked
    (new, not-yet-added) files.

    Returns ``None`` on a hard git failure (caller should fail open), or a
    (possibly empty) set of repo-relative, forward-slash paths otherwise.
    """
    diff = _run(rootdir, "diff", "--no-color", "--name-only", base, "--")
    if diff is None or diff.returncode != 0:
        return None
    files: Set[str] = {line.strip() for line in diff.stdout.splitlines() if line.strip()}

    untracked = _run(rootdir, "ls-files", "--others", "--exclude-standard")
    if untracked is not None and untracked.returncode == 0:
        files.update(line.strip() for line in untracked.stdout.splitlines() if line.strip())
    return files


def show_file_at_ref(rootdir: Path, ref: str, relpath: str) -> Optional[str]:
    """Return the contents of ``relpath`` as of ``ref``, or ``None`` if the
    file did not exist at that ref."""
    res = _run(rootdir, "show", f"{ref}:{relpath}")
    if res is None or res.returncode != 0:
        return None
    return res.stdout


def relative_to_root(rootdir: Path, abspath: str) -> Optional[str]:
    """POSIX-style path of ``abspath`` relative to ``rootdir``, or ``None`` if
    ``abspath`` is not inside ``rootdir`` (e.g. an external/site-packages
    fixture -- these are intentionally ignored by the selection algorithm)."""
    try:
        rel = Path(abspath).resolve().relative_to(Path(rootdir).resolve())
    except ValueError:
        return None
    return rel.as_posix()


def matches_any(relpath: str, patterns: Sequence[str]) -> bool:
    """True if ``relpath`` (a repo-relative, forward-slash path) matches any
    of ``patterns`` (fnmatch-style globs), gitignore-style: a pattern
    containing ``/`` is anchored and matched against the full relative path
    (e.g. ``custom_data/*.json``), while a plain pattern with no ``/`` is
    matched against just the basename so it applies at any depth (e.g.
    ``requirements*.txt`` matches both ``requirements.txt`` and
    ``sub/requirements-dev.txt``)."""
    import fnmatch
    import os

    name = os.path.basename(relpath)
    for pattern in patterns:
        if "/" in pattern:
            if fnmatch.fnmatch(relpath, pattern):
                return True
        elif fnmatch.fnmatch(name, pattern):
            return True
    return False
