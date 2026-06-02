"""Guards against the test-marker footgun that once made PR CI vacuously
green.

Background: ``tests/<area>/e2e/conftest.py`` stamps its tests with the
``e2e`` marker via ``pytest_collection_modifyitems``. That hook is
SESSION-wide — ``items`` holds every collected test, not only those under
the conftest's directory. An earlier version iterated ``for item in
items: item.add_marker(e2e)`` without filtering by path, so a full-suite
collection from the repo root marked the ENTIRE suite ``e2e``. The
default ``addopts = "-m 'not integration and not e2e'"`` then deselected
everything and ``pytest`` collected ZERO tests while still exiting 0.

These tests fail loudly if that regression returns: one checks the
*symptom* (default collection must not be near-empty), one checks the
*blast radius* (the e2e marker must not engulf the whole suite), and one
checks the *root cause* directly (the hook must be path-scoped).
"""

from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent


def _collect_count(marker_expr: str) -> int:
    """Run ``pytest --collect-only`` in a subprocess under ``marker_expr``
    and return the number of SELECTED tests.

    A subprocess is deliberate: we need a fresh, full collection from the
    repo root (the exact thing CI does), which we cannot introspect from
    inside the current session.
    """
    proc = subprocess.run(
        [
            sys.executable, "-m", "pytest",
            "--collect-only", "-q",
            "-p", "no:cacheprovider",
            "-m", marker_expr,
        ],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=300,
    )
    out = proc.stdout + proc.stderr
    if "no tests collected" in out:
        return 0
    # "<sel> tests collected" or "<sel>/<total> tests collected (<n> deselected)"
    m = re.search(r"(\d+)(?:/\d+)? tests collected", out)
    assert m is not None, f"could not parse collection count from:\n{out[-2000:]}"
    return int(m.group(1))


# A deliberately low floor: the suite has well over a thousand selectable
# tests. We only need to catch "collapsed to ~0", not pin an exact count
# that normal growth/pruning would churn.
_DEFAULT_FLOOR = 500
# The real e2e suite is a few dozen tests. If a marker hook leaks
# session-wide again, this jumps to the entire suite and trips the ceiling.
_E2E_CEILING = 500
_INTEGRATION_CEILING = 400


def test_default_collection_is_not_vacuous() -> None:
    """`pytest` with the default marker filter must collect a substantial
    suite. If this returns ~0, CI is passing without running anything."""
    n = _collect_count("not integration and not e2e")
    assert n >= _DEFAULT_FLOOR, (
        f"default collection selected only {n} tests (floor {_DEFAULT_FLOOR}). "
        "An e2e/integration marker hook likely leaked session-wide again — "
        "check tests/**/e2e/conftest.py path-scoping."
    )


def test_e2e_marker_does_not_engulf_the_suite() -> None:
    """The `e2e` marker must stay scoped to the real e2e directories. A
    session-wide leak shows up as the e2e count ballooning to ~the whole
    suite."""
    n = _collect_count("e2e")
    assert n < _E2E_CEILING, (
        f"e2e marker selected {n} tests (ceiling {_E2E_CEILING}). "
        "A conftest pytest_collection_modifyitems hook is marking tests "
        "outside its own directory — filter by item.path."
    )


def test_integration_marker_does_not_engulf_the_suite() -> None:
    """Same guard for the `integration` marker."""
    n = _collect_count("integration")
    assert n < _INTEGRATION_CEILING, (
        f"integration marker selected {n} tests (ceiling {_INTEGRATION_CEILING})."
    )


def _load_conftest(rel_path: str) -> object:
    """Import a conftest.py by file path under a throwaway module name."""
    path = _REPO_ROOT / rel_path
    spec = importlib.util.spec_from_file_location(
        f"_guard_{rel_path.replace('/', '_')}", path
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _FakeItem:
    """Minimal stand-in for a pytest Item: just what the hook touches."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.markers: list[str] = []

    def add_marker(self, marker: object) -> None:
        # pytest.mark.e2e -> MarkDecorator with .name
        self.markers.append(getattr(marker, "name", str(marker)))


def test_safety_e2e_marker_hook_is_path_scoped() -> None:
    """Root-cause guard: the hook must mark ONLY tests under its own
    directory, never a sibling test elsewhere in the session."""
    conftest = _load_conftest("tests/safety/e2e/conftest.py")
    e2e_dir = _REPO_ROOT / "tests" / "safety" / "e2e"

    in_dir = _FakeItem(e2e_dir / "test_pii_block.py")
    out_of_dir = _FakeItem(_REPO_ROOT / "tests" / "core" / "test_config.py")

    conftest.pytest_collection_modifyitems(  # type: ignore[attr-defined]
        config=None, items=[in_dir, out_of_dir]
    )

    assert "e2e" in in_dir.markers, "in-directory test should get the e2e marker"
    assert "e2e" not in out_of_dir.markers, (
        "out-of-directory test must NOT get the e2e marker — the hook is "
        "leaking session-wide"
    )
