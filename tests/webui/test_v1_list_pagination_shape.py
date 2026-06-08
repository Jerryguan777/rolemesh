"""Pagination-shape meta-test for the ``/api/v1`` list surface.

THE RULE (the one this test enforces, so it can't rot):

  Every ``/api/v1`` GET that returns a *collection* must declare its
  response shape deliberately:

    * Growable collection  -> a named ``*Page`` ENVELOPE
      ``{items, total, limit, offset}`` (offset/limit) or, for
      append-only/time-series data, ``{items, has_more, next_cursor}``
      (cursor). Both are "envelopes" — a Pydantic model with an
      ``items`` field — and that is what this test recognises.

    * Bounded collection (fixed enum, static registry, or a small
      per-parent set that can't grow with tenant size) MAY stay a bare
      ``list[X]`` — but only if it is listed in ``BARE_LIST_ALLOWLIST``
      below with a one-line justification.

A new bare-array list endpoint therefore fails CI until someone either
wraps it in a ``*Page`` or consciously allowlists it (with a reason).
There is no silent "half-migrated" state — that is the whole point
(this guard exists because four list endpoints were moved to envelopes
while their tests, and nearly a fifth endpoint, were left on the old
bare-array convention).

Single-resource GETs (``response_model`` is one model, not a list) and
non-JSON responses (e.g. the CSV export) are not collections and are
skipped.
"""

from __future__ import annotations

import typing

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from pydantic import BaseModel

from webui.api_v1 import router as api_v1_router

# ---------------------------------------------------------------------------
# Bare ``list[X]`` GETs that are intentionally NOT paginated, each with a
# one-line justification. Err toward an envelope; only add here when the
# collection genuinely cannot grow with tenant/usage size. Reviewed by hand.
# ---------------------------------------------------------------------------
BARE_LIST_ALLOWLIST: dict[tuple[str, str], str] = {
    ("GET", "/api/v1/backends"):
        "Static backend×provider×family matrix; fixed at build time.",
    ("GET", "/api/v1/models"):
        "Curated platform model catalog (reference data, operator-managed).",
    ("GET", "/api/v1/safety/checks"):
        "Static in-process safety-check registry.",
    ("GET", "/api/v1/credentials"):
        "At most one row per provider — bounded by the provider enum (~4).",
    ("GET", "/api/v1/platform/credentials"):
        "At most one row per provider — bounded by the provider enum.",
    ("GET", "/api/v1/platform/safety/rules"):
        "Curated platform safety-rule catalog; small, operator-managed.",
    ("GET", "/api/v1/me/channel-links/telegram"):
        "The caller's own linked Telegram identities; small per-user set.",
    ("GET", "/api/v1/coworkers/{coworker_id}/bindings"):
        "One coworker's channel bindings; bounded (a few channels).",
    ("GET", "/api/v1/coworkers/{coworker_id}/mcp-servers"):
        "One coworker's MCP bindings; bounded per coworker.",
    ("GET", "/api/v1/coworkers/{coworker_id}/skills"):
        "One coworker's skill bindings; bounded per coworker.",
    ("GET", "/api/v1/skills/{skill_id}/files"):
        "One skill's files; bounded per skill.",
    ("GET", "/api/v1/conversations/{conversation_id}/approval-requests"):
        "A single conversation's in-flight approvals; bounded per conversation.",
    ("GET", "/api/v1/platform/tenants"):
        "Platform tenant list. NOTE: this grows with tenant count — the one "
        "allowlisted collection that is not intrinsically bounded. Candidate "
        "to convert to a *Page envelope; allowlisted for now (platform_admin "
        "only, and deployments are small today).",
}


def _build_app() -> FastAPI:
    app = FastAPI()
    app.include_router(api_v1_router)
    return app


def _is_envelope(model: object) -> bool:
    """An ``*Page`` envelope is a Pydantic model carrying an ``items`` field.

    Recognises both offset (``{items,total,limit,offset}``) and cursor
    (``{items,has_more,next_cursor}``) envelopes — the distinction between
    the two is a per-endpoint choice this guard does not police.
    """
    return (
        isinstance(model, type)
        and issubclass(model, BaseModel)
        and "items" in model.model_fields
    )


def _is_bare_list(model: object) -> bool:
    return typing.get_origin(model) is list


def _list_get_routes() -> list[tuple[str, APIRoute]]:
    """Every ``/api/v1`` GET whose response_model is a collection."""
    out: list[tuple[str, APIRoute]] = []
    for route in _build_app().routes:
        if not isinstance(route, APIRoute):
            continue
        if not route.path.startswith("/api/v1"):
            continue
        if "GET" not in (route.methods or set()):
            continue
        rm = route.response_model
        if _is_envelope(rm) or _is_bare_list(rm):
            out.append((route.path, route))
    return out


def test_every_list_endpoint_is_enveloped_or_allowlisted_bare() -> None:
    """No silent bare array: each list GET is a *Page OR allowlisted."""
    offenders: list[str] = []
    for path, route in _list_get_routes():
        if _is_envelope(route.response_model):
            continue
        if ("GET", path) in BARE_LIST_ALLOWLIST:
            continue
        offenders.append(path)
    assert not offenders, (
        "These /api/v1 list GETs return a bare array but are neither a "
        f"*Page envelope nor in BARE_LIST_ALLOWLIST: {offenders}"
    )


def test_bare_list_allowlist_has_no_stale_entries() -> None:
    """Every allowlist entry must still be a live bare-array list GET.

    Guards against an entry quietly masking an endpoint that since gained a
    *Page envelope (entry now dead weight) or was renamed/removed.
    """
    live = {
        ("GET", path): route
        for path, route in _list_get_routes()
    }
    stale: list[tuple[str, str]] = []
    now_enveloped: list[tuple[str, str]] = []
    for key in BARE_LIST_ALLOWLIST:
        route = live.get(key)
        if route is None:
            stale.append(key)
        elif _is_envelope(route.response_model):
            now_enveloped.append(key)
    assert not stale, f"Allowlist entries for routes that no longer exist: {stale}"
    assert not now_enveloped, (
        f"Allowlist entries that are now *Page-enveloped (remove them): "
        f"{now_enveloped}"
    )


@pytest.mark.parametrize("key", sorted(BARE_LIST_ALLOWLIST))
def test_bare_list_allowlist_entries_have_justification(
    key: tuple[str, str],
) -> None:
    assert BARE_LIST_ALLOWLIST[key].strip()
