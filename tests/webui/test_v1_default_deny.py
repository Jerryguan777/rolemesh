"""Default-deny meta-test for the ``/api/v1`` surface.

The keystone of the role-gate work: it walks EVERY route registered under
``/api/v1`` and asserts each one is either

  * capability-gated (a ``require_action(...)`` dependency in its chain, tagged
    with ``_required_action``), OR
  * present in the EXPLICIT ``AUTH_ONLY_V1_ROUTES`` allowlist below, with a
    one-line justification for why "authenticated is enough".

A new ungated mutation route therefore fails CI until someone consciously
either gates it or adds it (with a reason) to the allowlist. There is no silent
pass — that is the whole point.

A second test asserts every action string passed to ``require_action`` anywhere
in ``src/webui`` exists in at least one role's set in ``_USER_ROLE_ACTIONS``,
catching typos / drift between the gate and the capability table.
"""

from __future__ import annotations

import ast
import pathlib

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute

from rolemesh.auth.permissions import _USER_ROLE_ACTIONS
from webui.api_v1 import router as api_v1_router

# ---------------------------------------------------------------------------
# Allowlist: (METHOD, path) tuples that are intentionally "authenticated is
# enough". Each entry carries a one-line justification. Err toward GATING; only
# add here when the route genuinely needs no role gate. Reviewed by hand.
# ---------------------------------------------------------------------------
AUTH_ONLY_V1_ROUTES: dict[tuple[str, str], str] = {
    # --- Public / boot-time metadata -------------------------------------
    ("GET", "/api/v1/backends"):
        "Static compatibility matrix; design §2.3 marks it public metadata.",
    ("GET", "/api/v1/auth/config"):
        "Boot-time auth-mode hint the SPA reads before it has a session.",
    # --- Identity (the caller's own row) ---------------------------------
    ("GET", "/api/v1/auth/me"):
        "Returns the caller's own identity only.",
    ("GET", "/api/v1/me"):
        "Returns the caller's own identity only.",
    ("POST", "/api/v1/auth/ws-ticket"):
        "Mints a WS ticket for a conversation the caller is verified to own "
        "(membership checked in-handler); no role escalation.",
    # --- Per-user IM self-service (DB scopes every row to user_id) --------
    ("POST", "/api/v1/me/channel-links/telegram"):
        "User links their OWN Telegram identity; rows are user_id-scoped.",
    ("GET", "/api/v1/me/channel-links/telegram"):
        "Lists the caller's OWN linked identities.",
    ("DELETE", "/api/v1/me/channel-links/{identity_id}"):
        "Unbinds the caller's OWN identity; DB DELETE filters by user_id.",
    # --- Reads of tenant-scoped catalog / own data (RLS = tenant scope) ---
    ("GET", "/api/v1/coworkers"):
        "Read of the tenant's coworker catalog; a member may use agents.",
    ("GET", "/api/v1/coworkers/{coworker_id}"):
        "Read of one tenant coworker.",
    ("GET", "/api/v1/coworkers/{coworker_id}/conversations"):
        "Read of a coworker's conversations within the tenant.",
    ("GET", "/api/v1/coworkers/{coworker_id}/bindings"):
        "Read of a coworker's channel bindings (write-only creds omitted).",
    ("GET", "/api/v1/coworkers/{coworker_id}/bindings/{binding_id}"):
        "Read of one binding (write-only creds omitted from the response).",
    ("GET", "/api/v1/coworkers/{coworker_id}/mcp-servers"):
        "Read of a coworker's MCP bindings.",
    ("GET", "/api/v1/coworkers/{coworker_id}/skills"):
        "Read of a coworker's skill bindings.",
    ("GET", "/api/v1/conversations/{conversation_id}"):
        "Read of one conversation in the caller's tenant.",
    ("GET", "/api/v1/conversations/{conversation_id}/messages"):
        "Read of a conversation's message history.",
    ("GET", "/api/v1/conversations/{conversation_id}/approval-requests"):
        "Read of a conversation's HITL approval cards for re-render.",
    ("GET", "/api/v1/skills"):
        "Read of the tenant's skill catalog.",
    ("GET", "/api/v1/skills/{skill_id}"):
        "Read of one skill.",
    ("GET", "/api/v1/skills/{skill_id}/files"):
        "Read of a skill's file list.",
    ("GET", "/api/v1/skills/{skill_id}/files/{path:path}"):
        "Read of one skill file.",
    ("GET", "/api/v1/mcp-servers"):
        "Read of the tenant's MCP server catalog.",
    ("GET", "/api/v1/mcp-servers/{mcp_id}"):
        "Read of one MCP server (no secrets in the response shape).",
    ("GET", "/api/v1/approval-policies"):
        "Read of the tenant's approval policies.",
    ("GET", "/api/v1/approval-policies/{policy_id}"):
        "Read of one approval policy.",
    ("GET", "/api/v1/approval-requests"):
        "Read of the tenant's pending approval requests (inbox re-render).",
    ("GET", "/api/v1/schedules"):
        "Read-only schedules surface; writes are not exposed on v1.",
    ("GET", "/api/v1/schedules/{task_id}"):
        "Read of one scheduled task.",
    ("GET", "/api/v1/runs/{run_id}"):
        "Read of one run snapshot (SPA reconnect path).",
    ("GET", "/api/v1/models"):
        "Read of the platform model catalog (tenant-agnostic, no secrets).",
    ("GET", "/api/v1/models/{model_id}"):
        "Read of one platform model.",
    # --- Operator-only writes gated IN-HANDLER (not via require_action) ---
    # admin_models.py gates these with its own ``_require_owner(user)`` call at
    # the top of each handler (a deliberate audit-locality choice predating
    # this work). They are NOT ungated; they are simply gated by a mechanism
    # the route-tag walker can't see, so they are allowlisted with this note.
    ("POST", "/api/v1/admin/models"):
        "Owner-gated in-handler via _require_owner (platform catalog write).",
    ("PATCH", "/api/v1/admin/models/{model_id}"):
        "Owner-gated in-handler via _require_owner (platform catalog write).",
    ("DELETE", "/api/v1/admin/models/{model_id}"):
        "Owner-gated in-handler via _require_owner (platform catalog write).",
}


def _build_app() -> FastAPI:
    app = FastAPI()
    app.include_router(api_v1_router)
    return app


def _route_required_action(route: APIRoute) -> str | None:
    """Return the action a route's ``require_action`` gate enforces, if any.

    Walks the FastAPI dependency tree looking for the ``_required_action`` tag
    that ``require_action`` stamps on its inner callable.
    """
    for dep in route.dependant.dependencies:
        call = getattr(dep, "call", None)
        action = getattr(call, "_required_action", None)
        if action is not None:
            return str(action)
    return None


def _iter_v1_routes() -> list[tuple[str, str, APIRoute]]:
    app = _build_app()
    out: list[tuple[str, str, APIRoute]] = []
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        if not route.path.startswith("/api/v1"):
            continue
        for method in sorted(route.methods - {"HEAD", "OPTIONS"}):
            out.append((method, route.path, route))
    return out


def test_every_v1_route_is_gated_or_explicitly_auth_only() -> None:
    """No silent pass: each /api/v1 route is gated OR allowlisted."""
    ungated: list[tuple[str, str]] = []
    for method, path, route in _iter_v1_routes():
        if _route_required_action(route) is not None:
            continue
        if (method, path) in AUTH_ONLY_V1_ROUTES:
            continue
        ungated.append((method, path))
    assert not ungated, (
        "These /api/v1 routes are neither role-gated nor in the reviewed "
        f"AUTH_ONLY_V1_ROUTES allowlist: {ungated}"
    )


def test_allowlist_has_no_stale_entries() -> None:
    """Every allowlisted route must still exist AND still be ungated.

    Guards against an allowlist entry quietly masking a route that later GAINED
    a gate (entry now dead weight) or was renamed/removed.
    """
    live: dict[tuple[str, str], APIRoute] = {
        (m, p): r for m, p, r in _iter_v1_routes()
    }
    stale: list[tuple[str, str]] = []
    now_gated: list[tuple[str, str]] = []
    for key in AUTH_ONLY_V1_ROUTES:
        route = live.get(key)
        if route is None:
            stale.append(key)
        elif _route_required_action(route) is not None:
            now_gated.append(key)
    assert not stale, f"Allowlist entries for routes that no longer exist: {stale}"
    assert not now_gated, (
        f"Allowlist entries that are now route-gated (remove them): {now_gated}"
    )


# ---------------------------------------------------------------------------
# Drift guard: every require_action(...) string must exist in the role table.
# ---------------------------------------------------------------------------


def _known_actions() -> set[str]:
    actions: set[str] = set()
    for role_actions in _USER_ROLE_ACTIONS.values():
        actions |= role_actions
    return actions


def _collect_require_action_literals() -> list[tuple[str, str]]:
    """Static-scan src/webui for ``require_action("...")`` string literals.

    A static scan (not runtime introspection) catches actions on routes that
    happen not to be imported by this test's app, and is robust to typos that
    would otherwise only surface as a 403-for-everyone at runtime.
    """
    webui_dir = pathlib.Path(__file__).resolve().parents[2] / "src" / "webui"
    found: list[tuple[str, str]] = []
    for py in webui_dir.rglob("*.py"):
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
            if name != "require_action":
                continue
            if node.args and isinstance(node.args[0], ast.Constant):
                val = node.args[0].value
                if isinstance(val, str):
                    found.append((str(py), val))
    return found


def test_every_require_action_string_exists_in_role_table() -> None:
    literals = _collect_require_action_literals()
    # Sanity: the scan must actually find gates, else the test is vacuous.
    assert literals, "No require_action(...) literals found — scan is broken."
    known = _known_actions()
    unknown = sorted(
        {action for _, action in literals if action not in known}
    )
    assert not unknown, (
        f"require_action uses actions absent from _USER_ROLE_ACTIONS "
        f"(typo or missing table entry): {unknown}"
    )


@pytest.mark.parametrize("key", sorted(AUTH_ONLY_V1_ROUTES))
def test_allowlist_entries_have_nonempty_justification(
    key: tuple[str, str],
) -> None:
    """Force every allowlist entry to carry a real reason (review hygiene)."""
    assert AUTH_ONLY_V1_ROUTES[key].strip()
