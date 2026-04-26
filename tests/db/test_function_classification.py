"""PR-E: static check that webui never imports admin escapes.

The classification (in pg.py docstrings + the RLS design doc) is
that ``admin_conn`` and ``resolve_*`` are system-only paths.
REST handlers must never reach for them — every webui caller goes
through tenant-scoped reads/writes that have a user.tenant_id in
scope.

This test parses every .py under src/webui/ and fails the moment
someone imports an admin path. It's intentionally a static check
(not a runtime one) so the violation is caught at PR review,
not after the offending build ships.
"""

from __future__ import annotations

import ast
from pathlib import Path

ADMIN_NAMES = frozenset({
    "admin_conn",
    "_get_admin_pool",
    "resolve_user_for_auth",
    "resolve_request_tenant",
    "get_conversation_for_notification",
})


def _admin_imports(tree: ast.Module) -> list[str]:
    """Return any names from ADMIN_NAMES imported by this module."""
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            if "rolemesh.db" in node.module or node.module == "pg":
                for alias in node.names:
                    if alias.name in ADMIN_NAMES:
                        found.append(alias.name)
        if isinstance(node, ast.Import):
            for alias in node.names:
                # Catch `from rolemesh.db import pg` — that's fine,
                # but `pg.admin_conn(...)` calls are a separate
                # check (Attribute access).
                pass
    return found


def _admin_attribute_calls(tree: ast.Module, src_text: str) -> list[str]:
    """Return any ``pg.admin_conn(...)`` / ``pg.resolve_*(...)`` calls."""
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in ADMIN_NAMES:
            # Most callers do ``pg.admin_conn`` — check the value is
            # an attribute on something named pg, but be lenient since
            # the rule is "no use", not "no specific syntax".
            line = src_text.splitlines()[node.lineno - 1] if node.lineno else ""
            found.append(f"{node.attr} (line {node.lineno}: {line.strip()[:80]})")
    return found


def test_webui_never_imports_admin_paths() -> None:
    webui_root = Path(__file__).resolve().parents[2] / "src" / "webui"
    assert webui_root.is_dir(), f"webui dir not found at {webui_root}"
    violations: dict[str, list[str]] = {}
    for py in webui_root.rglob("*.py"):
        text = py.read_text()
        try:
            tree = ast.parse(text)
        except SyntaxError as exc:
            raise AssertionError(f"failed to parse {py}: {exc}") from exc
        bad = _admin_imports(tree) + _admin_attribute_calls(tree, text)
        if bad:
            violations[str(py.relative_to(webui_root.parents[1]))] = bad
    assert not violations, (
        "webui code imports or calls admin paths (must use tenant-scoped "
        "wrappers instead):\n"
        + "\n".join(
            f"  {path}: {', '.join(items)}" for path, items in violations.items()
        )
    )
