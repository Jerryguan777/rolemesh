"""Static check: the eval module never imports admin escapes.

Eval is purely a tenant-scoped consumer of rolemesh data. Reaching
for ``admin_conn`` / ``_get_admin_pool`` / ``resolve_*`` would put
the framework in cross-tenant territory, defeating the RLS posture.
Modelled after ``tests/db/test_function_classification.py``.
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
    found: list[str] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.ImportFrom)
            and node.module
            and ("rolemesh.db" in node.module or node.module == "pg")
        ):
            for alias in node.names:
                if alias.name in ADMIN_NAMES:
                    found.append(alias.name)
    return found


def _admin_attribute_calls(tree: ast.Module, src_text: str) -> list[str]:
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in ADMIN_NAMES:
            line = src_text.splitlines()[node.lineno - 1] if node.lineno else ""
            found.append(f"{node.attr} (line {node.lineno}: {line.strip()[:80]})")
    return found


def test_evaluation_never_imports_admin_paths() -> None:
    eval_root = Path(__file__).resolve().parents[2] / "src" / "rolemesh" / "evaluation"
    assert eval_root.is_dir(), f"evaluation dir not found at {eval_root}"
    violations: dict[str, list[str]] = {}
    for py in eval_root.rglob("*.py"):
        text = py.read_text()
        try:
            tree = ast.parse(text)
        except SyntaxError as exc:
            raise AssertionError(f"failed to parse {py}: {exc}") from exc
        bad = _admin_imports(tree) + _admin_attribute_calls(tree, text)
        if bad:
            violations[str(py.relative_to(eval_root.parents[1]))] = bad
    assert not violations, (
        "evaluation code imports or calls admin paths (must use tenant-scoped "
        "wrappers instead):\n"
        + "\n".join(
            f"  {path}: {', '.join(items)}" for path, items in violations.items()
        )
    )
