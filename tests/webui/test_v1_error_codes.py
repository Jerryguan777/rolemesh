"""Drift guard for the ``/api/v1`` error-code catalog.

Sibling of the default-deny and pagination-shape meta-tests. The error
``code`` is a contract: clients branch on it (``MISSING_CREDENTIAL`` ->
"configure a key first", ``RESOURCE_IN_USE`` -> "detach first", ...). Left
as a free-form string with a hand-written doc list, that catalog rots —
new codes ship undocumented and stale codes linger.

This module pins three invariants against
:data:`webui.v1.errors.KNOWN_ERROR_CODES`:

  1. Every error ``code`` *string literal* raised under ``src/webui``
     (``raise_error_response("X", ...)`` / ``ErrorResponseException(
     code="X")``) is in the catalog. A new, unregistered code fails CI.
  2. The catalog has no dead entries: it equals the set of raised literals
     plus the small ``_DYNAMICALLY_RAISED`` allowlist (codes sourced from a
     typed constant rather than a literal, which the AST scan can't see).
  3. The published OpenAPI ``ErrorResponse`` doc mentions every catalog
     code, so the contract a consumer reads stays accurate.

Scope note: this is the HTTP error vocabulary only. Safety-check *finding*
codes (JAILBREAK, TOXICITY, ...) live on ``SafetyFinding``, not on
``ErrorResponse.code``, and are deliberately out of scope.
"""

from __future__ import annotations

import ast
import pathlib

import pytest
import yaml

from webui.v1.errors import KNOWN_ERROR_CODES

# Codes raised through a non-literal expression (``exc.code`` /
# ``SomeError.code``) that the literal AST scan cannot resolve. Kept tiny
# and explicit so the catalog still can't silently grow stale.
_DYNAMICALLY_RAISED: frozenset[str] = frozenset({
    "BACKEND_INCOMPAT",       # webui/v1/coworkers.py -> BackendCompatError.code
    "WS_TICKET_SECRET_UNSET",  # webui/v1/auth.py -> WsTicketError.code
})

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_WEBUI = _REPO_ROOT / "src" / "webui"
_OPENAPI = _REPO_ROOT / "contracts" / "openapi.yaml"


def _raised_literal_codes() -> set[str]:
    """Static-scan ``src/webui`` for error-code string literals.

    Catches the first positional arg of ``raise_error_response(...)`` and
    the ``code=`` kwarg of ``ErrorResponseException(...)``. A static scan
    (not runtime) finds codes on handlers this test never imports and is
    robust to a code path that only fires on an error.
    """
    found: set[str] = set()
    for py in _WEBUI.rglob("*.py"):
        tree = ast.parse(py.read_text(), filename=str(py))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = (
                func.id if isinstance(func, ast.Name)
                else func.attr if isinstance(func, ast.Attribute)
                else None
            )
            if name == "raise_error_response" and node.args:
                arg0 = node.args[0]
                if isinstance(arg0, ast.Constant) and isinstance(arg0.value, str):
                    found.add(arg0.value)
            if name == "ErrorResponseException":
                for kw in node.keywords:
                    if (
                        kw.arg == "code"
                        and isinstance(kw.value, ast.Constant)
                        and isinstance(kw.value.value, str)
                    ):
                        found.add(kw.value.value)
    return found


def test_every_raised_code_is_in_the_catalog() -> None:
    literals = _raised_literal_codes()
    assert literals, "No error-code literals found — the scan is broken."
    unknown = sorted(literals - KNOWN_ERROR_CODES)
    assert not unknown, (
        "Error codes raised in src/webui but missing from "
        f"KNOWN_ERROR_CODES (register them there + in the OpenAPI "
        f"ErrorResponse doc): {unknown}"
    )


def test_catalog_has_no_dead_entries() -> None:
    """Catalog == raised literals + the dynamic-source allowlist.

    Fails if a catalog code is never raised (dead weight) or a dynamic code
    isn't actually in the catalog.
    """
    accounted = _raised_literal_codes() | _DYNAMICALLY_RAISED
    dead = sorted(KNOWN_ERROR_CODES - accounted)
    assert not dead, (
        f"KNOWN_ERROR_CODES entries that are never raised (remove them, or "
        f"add to _DYNAMICALLY_RAISED if raised via a constant): {dead}"
    )
    assert _DYNAMICALLY_RAISED <= KNOWN_ERROR_CODES, (
        "_DYNAMICALLY_RAISED has codes missing from KNOWN_ERROR_CODES: "
        f"{sorted(_DYNAMICALLY_RAISED - KNOWN_ERROR_CODES)}"
    )


def _error_response_code_doc() -> str:
    spec = yaml.safe_load(_OPENAPI.read_text())
    return spec["components"]["schemas"]["ErrorResponse"]["properties"]["code"][
        "description"
    ]


@pytest.mark.parametrize("code", sorted(KNOWN_ERROR_CODES))
def test_openapi_doc_lists_every_catalog_code(code: str) -> None:
    """The published ErrorResponse.code doc must mention each known code."""
    assert code in _error_response_code_doc(), (
        f"{code} is in KNOWN_ERROR_CODES but not documented in the OpenAPI "
        "ErrorResponse.code description."
    )
